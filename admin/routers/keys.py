"""API Key 管理：创建（带积分限额）/ 列表 / 查看 / 调整 / 吊销 / 用量。"""
import base64
import json
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin.audit import current_actor, log_request
from admin.db import get_db
from admin.models import AccountGroup, ApiKey, ApiKeyIp, ModelGroup
from admin.security import hash_key, key_ip_allowlist, require_admin, _norm_cidrs, require_admin_read

router = APIRouter(prefix="/api/keys", tags=["keys"])

#: 密钥 realm 合法值（''=不限；与 accounts.realm 同口径）
_REALMS = ("", "cn", "global")


class KeyIn(BaseModel):
    name: str = ""
    credit_limit: float = 0
    unlimited: bool = False
    note: str = ""
    group_id: int = 0  # 0 = 不绑定分组（可用全部启用模型）
    account_group_id: int = 0  # P2 T8：0 = 不绑定账号池分组（全池选号）
    # ---- P2 T3：全维度 ----
    realm: str = ""                     # '' / cn / global（P1 列已存在，本次补 API）
    expires_at: str | None = None       # ISO 字符串；None/'' = 永不过期
    max_ips: int = 0                    # 0 = 不限
    ip_allowlist: list[str] = []        # 单 IP 或 CIDR；空 = 不限
    token_quota: int = 0                # 0 = 不限（0 不可能是有效配额）
    # 每密钥并发上限：None=跟随全局 / 0=不限 / N=上限。
    # 用 None 而不是 0 作默认，才能区分「没设置」与「显式不限」。
    max_concurrent: int | None = None


def _validate_concurrent(raw) -> int | None:
    """校验每密钥并发上限：None / 0 / 正整数，其余 400。

    为什么负数要拒而不是当 0：负数没有任何合理语义，静默当 0（=不限）
    会让「本想限制」的配置变成「完全不限」—— 那是相反的效果。
    """
    if raw is None:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400,
                            detail=f"max_concurrent 必须是整数或留空，收到：{raw!r}")
    if n < 0:
        raise HTTPException(
            status_code=400,
            detail="max_concurrent 不能为负（留空=跟随全局，0=不限，正整数=上限）")
    return n


def _parse_expires(raw: str | None) -> datetime | None:
    """ISO 字符串 → datetime；None/空串 → None。非法格式 400。"""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return datetime.fromisoformat(str(raw).strip())
    except ValueError:
        raise HTTPException(status_code=400,
                            detail=f"expires_at 格式非法：{raw}（示例：2027-03-01T00:00:00）")


def _validate_realm(raw: str) -> str:
    v = (raw or "").strip().lower()
    if v not in _REALMS:
        raise HTTPException(status_code=400,
                            detail=f"realm 只能是 '' / cn / global，收到：{raw}")
    return v


def _gen_key() -> str:
    # sk- 前缀 + 256-bit 密码学安全随机（secrets，无时间戳/序号/可推断规律）
    return "sk-" + secrets.token_hex(32)


def _mask(key: str) -> str:
    return key[:10] + "****" + key[-4:] if len(key) > 16 else key[:6] + "****"


def _encode_key(raw: str) -> str:
    """简单编码存储完整 key（base64，非加密；管理后台已有 JWT 保护）。"""
    return base64.b64encode(raw.encode()).decode()


def _validate_account_group(db: Session, gid: int) -> int:
    """账号池分组 id 校验：0 直接放行（未绑定）；>0 必须存在（防手滑绑到
    已删分组 → 选号永远空池，还查不出原因）。"""
    gid = int(gid or 0)
    if gid and not db.query(AccountGroup).filter(AccountGroup.id == gid).first():
        raise HTTPException(status_code=400, detail=f"账号池分组 #{gid} 不存在")
    return gid


def _decode_key(encoded: str) -> str:
    try:
        return base64.b64decode(encoded.encode()).decode()
    except Exception:
        return ""


@router.get("")
def list_keys(_: bool = Depends(require_admin_read), db: Session = Depends(get_db)):
    rows = db.query(ApiKey).order_by(ApiKey.id.desc()).all()
    group_names = {g.id: g.name for g in db.query(ModelGroup).all()}
    acc_group_names = {g.id: g.name for g in db.query(AccountGroup).all()}
    items = [
        {
            "id": k.id,
            "name": k.name,
            "key_prefix": k.key_prefix,
            "masked": _mask(k.key_prefix + "****************"),
            "credit_limit": k.credit_limit,
            "credit_used": k.credit_used,
            "unlimited": bool(k.unlimited),
            "status": k.status,
            "note": k.note,
            "group_id": int(k.group_id or 0),
            "group_name": group_names.get(int(k.group_id or 0), ""),
            "account_group_id": int(getattr(k, "account_group_id", 0) or 0),
            "account_group_name": acc_group_names.get(int(getattr(k, "account_group_id", 0) or 0), ""),
            # ---- P2 T3 ----
            "realm": (k.realm or ""),
            "expires_at": k.expires_at.isoformat() if k.expires_at else None,
            "expired": bool(k.expires_at and k.expires_at < datetime.utcnow()),
            "max_ips": int(k.max_ips or 0),
            "ip_allowlist": key_ip_allowlist(k),
            "bound_ips": db.query(ApiKeyIp).filter(ApiKeyIp.key_id == k.id).count(),
            "token_quota": int(k.token_quota or 0),
            "used_tokens": int(k.used_tokens or 0),
            # 每密钥并发上限：None = 跟随全局（前端据此显示「跟随全局」而不是 0）
            "max_concurrent": (None if getattr(k, "max_concurrent", None) is None
                               else int(k.max_concurrent)),
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            "created_at": k.created_at.isoformat() if k.created_at else None,
        }
        for k in rows
    ]
    return {"items": items}


def _validate_group(db: Session, group_id: int) -> int:
    """校验分组存在；0 表示不绑定（允许）。返回规范化后的 group_id。"""
    gid = int(group_id or 0)
    if gid and not db.query(ModelGroup).filter(ModelGroup.id == gid).first():
        raise HTTPException(status_code=400, detail=f"分组 #{gid} 不存在")
    return gid


@router.post("")
def create_key(body: KeyIn, _: bool = Depends(require_admin), db: Session = Depends(get_db),
               request: Request = None):
    raw = _gen_key()
    gid = _validate_group(db, body.group_id)
    agid = _validate_account_group(db, body.account_group_id)
    k = ApiKey(
        name=body.name or "未命名",
        key_hash=hash_key(raw),
        key_prefix=raw[:14],
        key_full=_encode_key(raw),
        credit_limit=body.credit_limit,
        unlimited=1 if body.unlimited else 0,
        note=body.note,
        group_id=gid,
        account_group_id=agid,
        realm=_validate_realm(body.realm),
        expires_at=_parse_expires(body.expires_at),
        max_ips=int(body.max_ips or 0),
        ip_allowlist=json.dumps(_norm_cidrs(body.ip_allowlist)),
        token_quota=int(body.token_quota or 0),
        max_concurrent=_validate_concurrent(body.max_concurrent),
    )
    db.add(k)
    db.commit()
    db.refresh(k)
    # 审计：key 创建成功（明文只写进响应，绝不落审计日志）
    log_request(current_actor(), "create_key", target=f"{k.id}:{k.name}",
                detail=f"limit={body.credit_limit},unlimited={bool(k.unlimited)},"
                       f"realm={k.realm},token_quota={k.token_quota}",
                request=request)
    # 明文 key 仅此返回一次
    return {
        "id": k.id,
        "name": k.name,
        "key": raw,
        "key_prefix": k.key_prefix,
        "credit_limit": k.credit_limit,
        "unlimited": bool(k.unlimited),
        "group_id": int(k.group_id or 0),
        "account_group_id": int(k.account_group_id or 0),
        "realm": k.realm,
        "expires_at": k.expires_at.isoformat() if k.expires_at else None,
        "max_ips": int(k.max_ips or 0),
        "ip_allowlist": key_ip_allowlist(k),
        "token_quota": int(k.token_quota or 0),
        "max_concurrent": (None if getattr(k, "max_concurrent", None) is None
                           else int(k.max_concurrent)),
    }


@router.get("/{key_id}/view")
def view_key(key_id: int, _: bool = Depends(require_admin), db: Session = Depends(get_db),
             request: Request = None):
    """查看 Key 完整值（创建后可再次查看）。"""
    k = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not k:
        raise HTTPException(status_code=404, detail="Key 不存在")
    raw = _decode_key(k.key_full)
    if not raw:
        raise HTTPException(status_code=410, detail="该 Key 创建于「查看功能」上线前，完整值未存储。建议重新生成一个新 Key。")
    # 审计：查看完整 key 是最高危操作（拿到明文等于拿到配额），必须留痕。
    # detail 只写 name/前缀，绝不写明文。
    log_request(current_actor(), "view_key_full", target=f"{k.id}:{k.name}",
                detail=f"prefix={k.key_prefix}", request=request)
    return {
        "id": k.id,
        "name": k.name,
        "key": raw,
        "key_prefix": k.key_prefix,
        "created_at": k.created_at.isoformat() if k.created_at else None,
    }


@router.get("/{key_id}/usage")
def key_usage(key_id: int, _: bool = Depends(require_admin_read), db: Session = Depends(get_db)):
    """查看 Key 的积分额度消耗情况。"""
    k = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not k:
        raise HTTPException(status_code=404, detail="Key 不存在")
    pct = ((k.credit_used / k.credit_limit) * 100) if k.credit_limit > 0 and not k.unlimited else None
    return {
        "id": k.id,
        "name": k.name,
        "credit_limit": k.credit_limit,
        "credit_used": round(k.credit_used, 2),
        "credit_remaining": (round(max(0, k.credit_limit - k.credit_used), 2) if not k.unlimited else None),
        "usage_percent": round(pct, 1) if pct is not None else None,
        "unlimited": bool(k.unlimited),
        "status": k.status,
        "created_at": k.created_at.isoformat() if k.created_at else None,
    }


@router.patch("/{key_id}")
def patch_key(key_id: int, body: dict, _: bool = Depends(require_admin), db: Session = Depends(get_db),
              request: Request = None):
    k = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not k:
        raise HTTPException(status_code=404, detail="Key 不存在")
    if "name" in body:
        k.name = body["name"]
    if "credit_limit" in body:
        k.credit_limit = float(body["credit_limit"])
    if "unlimited" in body:
        k.unlimited = 1 if body["unlimited"] else 0
    if "status" in body and body["status"] in ("active", "revoked"):
        k.status = body["status"]
    if "note" in body:
        k.note = body["note"]
    if "group_id" in body:
        k.group_id = _validate_group(db, body["group_id"])
    if "account_group_id" in body:
        k.account_group_id = _validate_account_group(db, body["account_group_id"])
    # ---- P2 T3：全维度（显式传 null/'' 是**允许**的——改回「不限」；
    #      所以不能用真值判断，必须按「键在不在 body 里」区分）----
    if "realm" in body:
        k.realm = _validate_realm(body["realm"])
    if "expires_at" in body:
        k.expires_at = _parse_expires(body["expires_at"])
    if "max_ips" in body:
        k.max_ips = int(body["max_ips"] or 0)
    if "ip_allowlist" in body:
        k.ip_allowlist = json.dumps(_norm_cidrs(body["ip_allowlist"]))
    if "token_quota" in body:
        k.token_quota = int(body["token_quota"] or 0)
    if "max_concurrent" in body:
        # 显式传 null = 改回「跟随全局」（所以用「键在不在」判断，不用真值）
        k.max_concurrent = _validate_concurrent(body["max_concurrent"])
    if body.get("reset_used"):
        # 与 manager reset_usage 同口径：两个量一起归零——界面上是同一个
        # 「重置用量」按钮，只清积分不清 token 会留下看不见的残留额度
        k.credit_used = 0
        k.used_tokens = 0
    db.commit()
    # 审计：detail 只记改了哪些**字段名**，不写值（值可能含 key 相关明文）
    log_request(current_actor(), "update_key", target=f"{k.id}:{k.name}",
                detail=",".join(sorted(body.keys())), request=request)
    return {"id": k.id, "ok": True}


@router.delete("/{key_id}")
def delete_key(key_id: int, _: bool = Depends(require_admin), db: Session = Depends(get_db),
               request: Request = None):
    k = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not k:
        raise HTTPException(status_code=404, detail="Key 不存在")
    log_request(current_actor(), "delete_key", target=f"{k.id}:{k.name}",
                detail=f"prefix={k.key_prefix}", request=request)
    db.delete(k)
    # 级联清理 IP 绑定记录（与 manager delete_key 同口径）——留着会让
    # 「删了重建同名密钥」的 max_ips 判定继承旧绑定，用户会莫名其妙被拒
    db.query(ApiKeyIp).filter(ApiKeyIp.key_id == key_id).delete()
    db.commit()
    return {"id": key_id, "ok": True}
