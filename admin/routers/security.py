"""入站 IP 管控（Phase 2 T5）：全局开关/模式、白黑名单规则、访问审计。

参考 ithtelab/workbuddy-manager server/routers/security.py（MIT）。
配置存 system_settings.security（JSON：{enabled, mode}）；规则与访问日志
是独立表（models.IpRule / IpAccessLog）。判定逻辑见 proxy._global_ip_gate
（/v1/* 链路在密钥校验**之前**调用）。
"""
import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from admin import iputil
from admin.audit import current_actor, log_request
from admin.db import get_db
from admin.models import IpAccessLog, IpRule, SystemSetting
from admin.security import require_admin

router = APIRouter(prefix="/api/security", tags=["security"])

DEFAULT_CONFIG = {"enabled": False, "mode": "blacklist"}


class ConfigIn(BaseModel):
    enabled: bool
    mode: str = Field(pattern="^(whitelist|blacklist)$")


class RuleIn(BaseModel):
    kind: str = Field(pattern="^(allow|deny)$")
    cidr: str = Field(min_length=1, max_length=64)
    note: str = ""


def get_config(db: Session) -> dict:
    """读取全局 IP 管控配置；system_settings 未配置时回默认（关/黑名单）。"""
    import json
    row = db.query(SystemSetting).filter(SystemSetting.key == "security").first()
    try:
        saved = __import__("json").loads(row.value) if row and row.value else {}
    except Exception:
        saved = {}
    return {**DEFAULT_CONFIG, **(saved if isinstance(saved, dict) else {})}


def _set_config(db: Session, cfg: dict) -> None:
    import json
    row = db.query(SystemSetting).filter(SystemSetting.key == "security").first()
    if row is None:
        row = SystemSetting(key="security", value="")
        db.add(row)
    row.value = json.dumps(cfg, ensure_ascii=False)
    db.commit()


@router.get("/config")
def read_config(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    return get_config(db)


@router.post("/config")
def write_config(body: ConfigIn, request: Request,
                 _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    cfg = {"enabled": body.enabled, "mode": body.mode}
    _set_config(db, cfg)
    # 改 IP 管控开关/模式会直接影响对外放行策略，必须留痕
    log_request(current_actor(), "update_security", target="security.config",
                detail=f"enabled={cfg['enabled']},mode={cfg['mode']}", request=request)
    return cfg


@router.get("/rules")
def list_rules(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.query(IpRule).order_by(IpRule.id.desc()).all()
    return {"items": [
        {"id": r.id, "kind": r.kind, "cidr": r.cidr, "note": r.note,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows
    ]}


@router.post("/rules")
def add_rule(body: RuleIn, request: Request,
             _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    cidr = body.cidr.strip()
    try:
        # strict=False：10.0.0.1/24 这种主机位非零写法按网段理解
        ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"非法的 IP / CIDR: {body.cidr}")
    rule = IpRule(kind=body.kind, cidr=cidr, note=body.note)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    log_request(current_actor(), "add_ip_rule", target=f"{rule.id}",
                detail=f"{body.kind} {cidr}", request=request)
    return {"id": rule.id, "kind": rule.kind, "cidr": rule.cidr, "note": rule.note,
            "created_at": rule.created_at.isoformat() if rule.created_at else None}


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int, request: Request,
                _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    rule = db.query(IpRule).filter(IpRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="规则不存在")
    log_request(current_actor(), "delete_ip_rule", target=f"{rule_id}",
                detail=f"{rule.kind} {rule.cidr}", request=request)
    db.delete(rule)
    db.commit()
    return {"ok": True}


@router.get("/logs")
def access_logs(limit: int = 200, _: bool = Depends(require_admin),
                db: Session = Depends(get_db)):
    rows = (db.query(IpAccessLog).order_by(IpAccessLog.id.desc())
            .limit(min(1000, max(1, limit))).all())
    return {"items": [
        {"id": r.id, "created_at": r.created_at.isoformat() if r.created_at else None,
         "ip": r.ip, "path": r.path, "blocked": bool(r.blocked), "ua": r.ua,
         "reason": r.reason or None}
        for r in rows
    ]}


@router.post("/logs/clear")
def clear_logs(request: Request, _: bool = Depends(require_admin),
               db: Session = Depends(get_db)):
    db.query(IpAccessLog).delete()
    db.commit()
    log_request(current_actor(), "clear_ip_access_logs", target="ip_access_logs",
                request=request)
    return {"ok": True}


# ---------------------------------------------------------------------------
# P2 T6：模型别名映射（model_map：别名 → 真名；网关侧见 proxy._map_model）
# ---------------------------------------------------------------------------

def _get_model_map(db: Session) -> dict:
    import json
    row = db.query(SystemSetting).filter(SystemSetting.key == "model_map").first()
    try:
        m = json.loads(row.value) if row and row.value else {}
    except Exception:
        m = {}
    return m if isinstance(m, dict) else {}


@router.get("/model-map")
def read_model_map(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    return {"map": _get_model_map(db)}


@router.post("/model-map")
def write_model_map(body: dict, request: Request,
                    _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """整体覆盖。清理：空键/空值剔除；值必须是字符串（防写入坏数据让
    网关 _map_model 拿到非 dict——虽然网关侧有兜底，写入侧也该拦）。"""
    import json
    raw = body.get("map", body) if isinstance(body, dict) else {}
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="map 必须是对象")
    clean = {}
    for k, v in raw.items():
        ks, vs = str(k).strip(), str(v).strip()
        if not ks or not vs:
            continue
        if ks == vs:
            continue  # 自映射无意义，拒掉免得误以为「映射生效了」
        clean[ks] = vs
    row = db.query(SystemSetting).filter(SystemSetting.key == "model_map").first()
    if row is None:
        row = SystemSetting(key="model_map", value="")
        db.add(row)
    row.value = json.dumps(clean, ensure_ascii=False)
    db.commit()
    log_request(current_actor(), "update_model_map", target="model_map",
                detail=f"{len(clean)} 条映射", request=request)
    return {"map": clean}
