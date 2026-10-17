"""模型配置管理：获取可用模型列表 / 系统级&用户级白名单 / 启用禁用。

使用方式：
  1. 先调用 POST /api/models/sync 从后端拉取可用模型列表（自动入库）
  2. 在列表中启用/禁用特定模型
  3. 代理转发时自动过滤不在白名单内的模型
"""
import httpx
import json
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin import backend, realm as realm_mod
from admin.config import settings
from admin.db import get_db
from admin.models import Account, ModelConfig
from admin.security import require_admin, require_admin_read

router = APIRouter(prefix="/api/models", tags=["models"])


class ModelToggleIn(BaseModel):
    enabled: bool = True


class ModelBatchIn(BaseModel):
    model_ids: list[str] = []
    enabled: bool = True
    level: str = "system"  # system | user


def _get_enabled_models(db: Session) -> set[str]:
    """获取当前启用的模型 ID 集合（用于代理过滤）。"""
    rows = db.query(ModelConfig).filter(ModelConfig.enabled == 1).all()
    return {m.model_id for m in rows}


def _get_free_models(db: Session) -> set[str]:
    """获取当前启用且免费的模型 ID 集合（credit_multiplier == 0）。"""
    rows = db.query(ModelConfig).filter(
        ModelConfig.enabled == 1,
        (ModelConfig.credit_multiplier == 0) | (ModelConfig.credit_multiplier.is_(None))
    ).all()
    return {m.model_id for m in rows}


def _is_model_allowed(db: Session, model_id: str) -> bool:
    """检查模型是否在白名单内。没有任何配置时放行全部（向后兼容）。"""
    configs = db.query(ModelConfig).all()
    if not configs:
        return True  # 未配置任何模型规则，放行
    enabled = {m.model_id for m in configs if m.enabled == 1}
    if not enabled:
        return True  # 全部禁用视为未配置，放行
    return model_id in enabled


@router.get("/configs")
def list_configs(
    level: Optional[str] = None,
    realm: str = "",
    _: bool = Depends(require_admin_read),
    db: Session = Depends(get_db),
):
    """列出模型配置。**按 realm 过滤**（不传则返回全部）。

    ## 为什么必须按 realm 过滤（真 bug）

    这个端点原来无条件返回 `ModelConfig` 全表 —— 两版模型**混在一起**。
    实测两版清单**交集只有 5 个**（cn 32 个 `hy3`/`hy4-preview` 那套，
    global 18 个 `default-model`/`balanced-model` 那套）。

    后果：面板切到「国际版」后，模型页仍然显示国内版 + 国际版的大杂烩，
    看起来像「同步了但没生效」。用户实测报告过这个现象。

    ## 过滤规则（与 `_models_for_realm` 保持一致）

        realm=global → 只取 `global:` 前缀的（剥掉前缀展示）
        realm=cn     → 取无前缀的 + `cn:` 前缀的
        不传         → 全部（兼容旧调用）

    **不做跨 realm 兜底** —— 该版没有模型时返回空列表，
    让面板如实显示「还没同步」，而不是拿另一版的顶上。
    """
    q = db.query(ModelConfig)
    if level:
        q = q.filter(ModelConfig.level == level)
    rows = q.order_by(ModelConfig.id.asc()).all()

    r = (realm or "").strip().lower()
    if r == "global":
        rows = [m for m in rows if str(m.model_id).startswith("global:")]
    elif r == "cn":
        rows = [m for m in rows
                if not str(m.model_id).startswith("global:")]

    def _bare(mid: str) -> str:
        """剥掉本地 realm 前缀（上游只认裸名，面板展示也该用裸名）。"""
        s = str(mid)
        for p in ("global:", "cn:"):
            if s.startswith(p):
                return s[len(p):]
        return s

    return {
        "realm": r or "all",
        "items": [
            {
                "id": m.id,
                "level": m.level,
                "model_id": _bare(m.model_id),
                "model_id_raw": m.model_id,
                "realm": "global" if str(m.model_id).startswith("global:") else "cn",
                "enabled": bool(m.enabled),
                "note": m.note,
                "credit_multiplier": m.credit_multiplier or 0,
                "credits_raw": m.credits_raw or "",
                "is_free": (m.credit_multiplier or 0) == 0,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in rows
        ]
    }


@router.get("/busy")
def model_busy(realm: str = "",
               _: bool = Depends(require_admin_read), db: Session = Depends(get_db)):
    """上游模型繁忙程度。

    `realm` 限定用哪一版账号去问：**两版的上游是两个域**，繁忙程度是各自
    独立的事实 —— 拿国内版账号问出来的数值去标注国际版模型，数字本身就是错的。
    不传则不限（既有行为，零回归）。

    **模型清单也必须按 realm 过滤**（真 bug）：原先这里取 `ModelConfig` 全表，
    两版混在一起。实测两版清单交集只有 5 个（cn 32 个 / global 18 个，
    国际版全是 `default-model` / `balanced-model` 这种，国内版是 `hy3` /
    `hy4-preview` 那套）→ 把国内版模型 id 发给国际版账号，上游根本不认识
    → 请求失败 → 返回 `stale=True`（前端显示一屏 unknown）。
    """
    from admin.model_busy import get_busy_levels, LABELS
    models = _models_for_realm(db, realm)
    result = get_busy_levels(db, models, realm=realm)
    return {"items": [{"model_id": model, "busy_level": level, "busy_label": LABELS[level]}
                      for model, level in result["levels"].items()],
            "observed_at": result["observed_at"], "stale": result["stale"],
            "realm": result.get("realm", realm or "all"),
            "model_count": len(models)}


def _models_for_realm(db: Session, realm: str) -> list[str]:
    """该 realm 下的模型 id 列表。

    国内版历史上用**无前缀**形态存储（`global:` 前缀只给国际版），
    所以过滤规则是：

        realm=global → 只取 `global:` 开头的，并去掉前缀（上游认裸名）
        realm=cn     → 取无前缀的 + `cn:` 开头的（兼容显式加前缀的写法）
        realm=''     → 全表（既有行为）

    去掉前缀很重要：`ModelConfig.model_id` 是**本地**的隔离标记，
    上游只认裸模型名。
    """
    from sqlalchemy import or_
    q = db.query(ModelConfig.model_id).distinct()
    r = (realm or "").strip().lower()
    if r == "global":
        rows = q.filter(ModelConfig.model_id.like("global:%")).all()
        return [str(x[0])[len("global:"):] for x in rows]
    if r == "cn":
        rows = q.filter(or_(~ModelConfig.model_id.like("%:%"),
                            ModelConfig.model_id.like("cn:%"))).all()
        return [str(x[0])[len("cn:"):] if str(x[0]).startswith("cn:") else str(x[0])
                for x in rows]
    return [str(x[0]) for x in q.all()]


def _pick_sync_account(db: Session, realm: str) -> Account | None:
    """为「同步模型清单」挑一个能调上游的账号（按 realm 过滤）。

    ## 为什么必须按 realm 挑（真实缺陷）

    原实现是 `db.query(Account).filter(status=='active', balance_remain>0).first()`
    —— **取的是 id 最小的那条**。而账号是按导入顺序入库的，国内版通常先导
    → 于是**面板切到国际版再点同步，拉回来的仍是国内版模型清单**。

    用户实测报告过这个现象：「不管选国际版还是国内版，同步的都是国内版的模型」。
    根因就在这里：端点根本没有 realm 参数，挑号也不看 realm。

    挑号顺序（同 realm 内）：
      1. 有余额的活跃账号（能正常调上游）
      2. 退而求其次：任何活跃账号（余额字段可能还没同步过）
    """
    q = db.query(Account).filter(Account.status == "active")
    if realm in ("cn", "global"):
        q = q.filter(Account.realm == realm)
    return (q.filter(Account.balance_remain > 0).first()
            or q.first())


def _do_sync_models(db: Session, realm: str = "cn") -> dict:
    """从后端拉取可用模型列表并同步到本地数据库（upsert）。

    `realm` 决定用哪一版的账号去拉、以及清单归属哪一版（`cn:` / `global:` 前缀）。
    """
    acc = _pick_sync_account(db, realm)
    if not acc:
        # 报错信息要说清是哪一版没有账号 —— 否则用户会以为「账号池是空的」
        label = {"cn": "国内版", "global": "国际版"}.get(realm, realm or "默认")
        raise HTTPException(
            status_code=503,
            detail=f"无可用账号（{label}）：无法连接后端获取模型列表")
    try:
        with backend.AccountSession(acc.auth_json) as sess:
            models_raw = sess.fetch_models()
            acc.auth_json = sess.updated_json()
        db.commit()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取模型列表失败: {e}")

    import re

    # realm 前缀：两版模型可能同名但能力/倍率不同（realm 隔离的核心）。
    # 国内版沿用历史的「无前缀」形态（零回归），国际版加 `global:` 前缀。
    prefix = "" if realm in ("", "cn") else f"{realm}:"

    added = 0
    updated = 0
    seen = {m.model_id for m in db.query(ModelConfig.model_id).all()}
    for m in models_raw:
        mid = m.get("id")
        if not mid or mid.lower() == "auto":
            continue  # 跳过上游聚合模型 "auto"，我们系统有自己的免费优先 auto 逻辑
        mid = f"{prefix}{mid}" if prefix and not mid.startswith(prefix) else mid
        # 解析积分倍率
        raw_credits = m.get("credits") or ""
        multiplier = 0.0
        if raw_credits:
            cre_match = re.search(r"x\s*([0-9]+(?:\.[0-9]+)?)", str(raw_credits))
            if cre_match:
                try:
                    multiplier = float(cre_match.group(1))
                except (ValueError, TypeError):
                    pass
        if mid in seen:
            # 已存在：覆盖倍率 / 原始 credits / 备注（保留用户启停状态）
            mc = db.query(ModelConfig).filter(ModelConfig.model_id == mid).first()
            if mc:
                mc.credit_multiplier = multiplier
                mc.credits_raw = str(raw_credits)
                if not mc.note:
                    mc.note = m.get("name") or ""
                updated += 1
            continue
        seen.add(mid)
        mc = ModelConfig(
            level="system",
            model_id=mid,
            enabled=1,
            note=m.get("name") or "",
            credit_multiplier=multiplier,
            credits_raw=str(raw_credits),
        )
        db.add(mc)
        added += 1
    db.commit()
    total = db.query(ModelConfig).count()
    enabled_count = db.query(ModelConfig).filter(ModelConfig.enabled == 1).count()
    return {
        "realm": realm or "cn",
        "source_account_id": acc.id,
        "total_fetched": len(models_raw),
        "added": added,
        "updated": updated,
        "total_in_db": total,
        "enabled": enabled_count,
    }


@router.post("/sync")
def sync_models(
    realm: str = "cn",
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """从后端拉取可用模型列表并同步到本地数据库（upsert）。

    `realm` 必传对（默认 cn 保持零回归）：面板右上角切到国际版时，
    前端要传 `realm=global`，否则会拿国内版账号去拉、把国内版清单写进库里
    （用户实测报过这个现象）。
    """
    return _do_sync_models(db, realm=realm)


@router.get("/catalog")
def model_catalog(
    realm: str = "cn",
    force: bool = False,
    _: bool = Depends(require_admin_read),
    db: Session = Depends(get_db),
):
    """模型中心目录：按 realm 直连腾讯模型目录（字段更全），回退上游清单。

    返回每个模型的显示名/描述/上下文/最大输出/推理档位/能力标记/系列/积分倍率，
    并如实标注数据来源（source/source_label：tencent 直连 or none）。
    倍率与启停状态来自本地 ModelConfig（按 model_id 关联），目录元数据来自腾讯。

    护栏说明：本端点会真实调用腾讯接口（只读 GET，不消耗积分）。
    UPSTREAM_READONLY 不拦截（它只挡会消耗/改状态的写操作）。
    """
    from admin import model_catalog as mc

    result = mc.catalog(db, realm=realm, force=force)
    # 合并本地 ModelConfig：积分倍率 + 启停状态（目录元数据不含这两个本地字段）
    configs = {m.model_id: m for m in db.query(ModelConfig).all()}
    for item in result.get("models", []):
        cfg = configs.get(item["id"])
        item["credit_multiplier"] = float(cfg.credit_multiplier or 0) if cfg else 0.0
        item["credits_raw"] = (cfg.credits_raw or "") if cfg else ""
        item["enabled"] = bool(cfg.enabled) if cfg else False
        item["is_free"] = (item["credit_multiplier"] == 0)
        item["in_local_config"] = cfg is not None
    return result


@router.patch("/{config_id}")
def toggle_model(
    config_id: int,
    body: ModelToggleIn,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """启用/禁用某个模型。"""
    mc = db.query(ModelConfig).filter(ModelConfig.id == config_id).first()
    if not mc:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    mc.enabled = 1 if body.enabled else 0
    if body.enabled and mc.note:
        pass  # keep existing note
    db.commit()
    return {"id": mc.id, "model_id": mc.model_id, "enabled": bool(mc.enabled)}


@router.post("/batch-toggle")
def batch_toggle(
    body: ModelBatchIn,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """批量启用/禁用模型。"""
    count = 0
    for mid in body.model_ids:
        mc = db.query(ModelConfig).filter(
            ModelConfig.model_id == mid, ModelConfig.level == body.level
        ).first()
        if mc:
            mc.enabled = 1 if body.enabled else 0
            count += 1
    db.commit()
    return {"updated": count, "enabled": body.enabled}


@router.delete("/{config_id}")
def delete_config(
    config_id: int,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """删除某条模型配置（从白名单移除 = 禁用该模型）。"""
    mc = db.query(ModelConfig).filter(ModelConfig.id == config_id).first()
    if not mc:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    mid = mc.model_id
    db.delete(mc)
    db.commit()
    return {"id": config_id, "model_id": mid, "ok": True}


# ---------------------------------------------------------------------------
# 测速：用账号池直接对上游发请求，免 API Key
# ---------------------------------------------------------------------------

_SPEED_TEST_PROMPT = [{"role": "user", "content": "用一句话介绍一下你自己。"}]


@router.post("/speed-test")
async def speed_test(
    body: dict,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """用账号池直接对上游发一次流式请求测速（无需 API Key）。

    测量指标：首字延迟 TTFT、输出 token 数、生成速度(t/s)、端到端耗时。
    仅占用一个可用账号做一次真实上游请求，不扣积分、不改额度。

    `body.realm`（可选）限定用哪一版的账号测速。不传则不限（既有行为）。

    ⚠️ **会真实请求上游** —— 与 `/api/accounts/credits/sync` 同级，
    频繁调用同样是明显的机器特征。
    """
    model = (body.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="缺少 model")
    try:
        max_tokens = int(body.get("max_tokens") or 256)
    except (TypeError, ValueError):
        max_tokens = 256
    messages = body.get("messages") or _SPEED_TEST_PROMPT
    realm = (body.get("realm") or "").strip().lower()
    if realm not in ("", "cn", "global"):
        raise HTTPException(status_code=400, detail="realm 只能是 cn / global / 留空")

    # 选可用账号（与代理一致：active 且余额>0，剩余最多优先）
    q = db.query(Account).filter(
        Account.status == "active", Account.balance_remain > 0)
    if realm:
        q = q.filter(Account.realm == realm)
    acc = q.order_by(Account.balance_remain.desc()).first()
    if not acc:
        # 文案如实：这个接口只挑「启用且余额>0」，**不看在途/冷却**，
        # 所以「全部禁用或额度耗尽」这句话经常是错的（例如账号只是被临时限流）。
        total = db.query(Account).count()
        active = db.query(Account).filter(Account.status == "active").count()
        label = {"cn": "国内版", "global": "国际版"}.get(realm, "")
        raise HTTPException(
            status_code=503,
            detail=(f"无可用账号{('（' + label + '）') if label else ''}："
                    f"共 {total} 个账号，其中启用 {active} 个，"
                    f"但没有任何「启用且余额大于 0」的账号"
                    f"（本接口不判断在途与冷却状态）"))

    # 出站基址必须**按账号 realm 决定**。
    #
    # 原实现写死 `f"{settings.BACKEND}/v2/chat/completions"` —— settings.BACKEND
    # 是国内版基址，于是拿国际版账号测速会把请求发到**国内版端点**：
    # 凭证是国际版的、端点却是国内的，上游直接判非法（而且冷却/错误计数
    # 会记到那个无辜的国际版账号上）。
    acc_realm = (getattr(acc, "realm", "") or "cn").strip().lower()
    base = realm_mod.chat_base(acc_realm)
    paths = realm_mod.chat_paths(acc_realm)
    sess = backend.AccountSession(acc.auth_json)
    headers = sess.get_headers()
    url = base + paths[0]
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": max_tokens,
    }

    t0 = time.perf_counter()
    ttft = None
    completion_tokens = None
    total_tokens = None
    prompt_tokens = None
    sample = ""
    sample_len = 0

    def _persist():
        try:
            acc.auth_json = sess.updated_json()  # 写回可能刷新的 token
            db.commit()
        except Exception:
            pass

    try:
        async with httpx.AsyncClient(timeout=120, limits=backend.HTTP_LIMITS) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as r:
                if r.status_code >= 400:
                    detail = await r.aread()
                    msg = detail.decode("utf-8", "replace")[:300]
                    _persist()
                    sess.close()
                    raise HTTPException(status_code=r.status_code, detail=f"上游返回错误: {msg}")
                async for chunk in r.aiter_text():
                    # TTFT：首个非空增量（content 或 reasoning_content）即首字延迟
                    if ttft is None and chunk.strip():
                        ttft = time.perf_counter() - t0
                    for line in chunk.splitlines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        p = line[5:].strip()
                        if not p or p == "[DONE]":
                            continue
                        try:
                            obj = json.loads(p)
                        except Exception:
                            continue
                        usage = obj.get("usage")
                        if isinstance(usage, dict):
                            if usage.get("prompt_tokens") is not None:
                                prompt_tokens = usage["prompt_tokens"]
                            if usage.get("completion_tokens") is not None:
                                completion_tokens = usage["completion_tokens"]
                            if usage.get("total_tokens") is not None:
                                total_tokens = usage["total_tokens"]
                        for ch in obj.get("choices") or []:
                            delta = ch.get("delta") or {}
                            # 推理类模型把思考内容放在 reasoning_content（content 可能为空），
                            # 样例优先取 content，缺省回退 reasoning_content，保证总能看到输出。
                            piece = (delta.get("content") or "") or (delta.get("reasoning_content") or "")
                            if piece and sample_len < 400:
                                add = piece[:400 - sample_len]
                                sample += add
                                sample_len += len(add)
            t_end = time.perf_counter()
    except HTTPException:
        raise
    except Exception as e:
        _persist()
        sess.close()
        raise HTTPException(status_code=502, detail=f"测速请求失败: {e}")

    _persist()
    sess.close()

    total_dur = t_end - t0
    gen_dur = (total_dur - ttft) if ttft is not None else total_dur
    gen_dur = max(gen_dur, 0.0)
    speed_tps = round(completion_tokens / gen_dur, 2) if completion_tokens and gen_dur > 0 else None
    overall_tps = round(completion_tokens / total_dur, 2) if completion_tokens and total_dur > 0 else None
    ttft_ms = round(ttft * 1000, 1) if ttft is not None else None

    meta = backend.parse_auth_meta(acc.auth_json)
    return {
        "model": model,
        "account": meta.get("nickname") or meta.get("uid") or "",
        "ttft_ms": ttft_ms,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "gen_duration_ms": round(gen_dur * 1000, 1),
        "total_duration_ms": round(total_dur * 1000, 1),
        "speed_tps": speed_tps,
        "overall_tps": overall_tps,
        "sample": sample,
    }
