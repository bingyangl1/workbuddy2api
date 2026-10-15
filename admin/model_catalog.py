"""模型目录服务：给「模型中心」页提供带完整字段的模型清单。

借鉴 manager `services/modelcatalog.py` 的两级来源思路，但适配本引擎的账号模型
（auth_json 凭据 / realm 字段 / AccountSession 同步调用）与 FastAPI 路由形态。

数据来源分两级，UI 如实标注当前用的是哪一级（source / source_label）：

  1. **腾讯模型接口**（首选）：`AccountSession.fetch_models()` 直连腾讯 realm 端点
     （CN=/console/...、Global=/v2/...），能拿到显示名（name）、描述（descriptionZh）、
     真实上下文（maxInputTokens）、最大输出（maxOutputTokens）、推理档位
     （reasoning.supportedEfforts / defaultEffort / canDisableThinking）、能力标记
     （supportsReasoning / supportsImages / supportsToolCall / onlyReasoning）。
     这些字段上游 `/v1/models` 会丢。
  2. **上游 /v1/models**（回退）：字段少（id + 上下文 + 最大输出 + 上游透出的档位），
     腾讯接口调不通（账号全过期、网络问题）时兜底，保证页面不空白。

缓存：每次进页面都打腾讯接口既慢又容易被风控。按 realm 分键缓存（CN 与 Global
清单完全不同，共用会互相覆盖），成功 5 分钟、失败负缓存 60 秒。

安全：不返回任何凭据；只暴露模型元数据。
"""
from __future__ import annotations

import threading
import time

from sqlalchemy.orm import Session

from admin import backend
from admin.models import Account

# 成功缓存 5 分钟：模型清单变化很慢，没必要每次进页面都打腾讯
_TTL_OK = 300
# 失败负缓存 60 秒：避免连续失败时反复重试（尤其账号全过期时）
_TTL_FAIL = 60

# 缓存按 realm 分键 + 进程内线程安全（FastAPI 同步路由跑线程池，可能并发进）
_cache: dict[str, dict] = {}
_lock = threading.Lock()


def _slot(realm: str) -> dict:
    return _cache.setdefault(realm, {"at": 0.0, "ttl": 0, "payload": None})


# 系列归属：腾讯接口不返回，按模型 id 前缀做命名约定推导（仅为分组浏览，
# UI 标明是推导值；认不出的一律归「其他」，不做猜测性归类）。
_SERIES_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("glm",), "GLM"),
    (("deepseek",), "DeepSeek"),
    (("kimi", "moonshot"), "Kimi"),
    (("minimax",), "MiniMax"),
    (("hy", "hunyuan"), "Hunyuan"),
    (("qwen",), "Qwen"),
    (("step",), "Step"),
    (("gpt",), "GPT"),
    (("gemini",), "Gemini"),
    (("auto",), "Auto"),
)


def series_of(model_id: str) -> str:
    """按 id 前缀推导系列名。认不出返回「其他」。"""
    mid = (model_id or "").lower()
    for prefixes, label in _SERIES_RULES:
        if mid.startswith(prefixes):
            return label
    return "其他"


def _decorate(items: list[dict], realm: str) -> list[dict]:
    """规范化腾讯原始模型条目为前端统一形状。

    字段名对齐 manager modelcatalog 的输出契约（模型中心前端直接复用）：
    id/name/description/context_length/max_output_tokens/efforts/default_effort/
    supports_reasoning/supports_images/supports_tool_call/only_reasoning/
    reasoning_summary/tags/vendor/series/is_default/credits/can_disable_thinking。
    """
    out = []
    for m in items:
        mid = str(m.get("id") or "")
        if not mid:
            continue
        reasoning = m.get("reasoning") or {}
        if not isinstance(reasoning, dict):
            reasoning = {}
        efforts = [str(x) for x in (reasoning.get("supportedEfforts") or []) if x]
        default_effort = str(reasoning.get("defaultEffort") or "").strip()
        # 单档形态（reasoning.effort，无 supportedEfforts）：视为仅支持该档
        if not efforts and reasoning.get("effort"):
            efforts = [str(reasoning["effort"])]
            default_effort = default_effort or str(reasoning["effort"])
        # 默认档必须在支持列表内才有效
        if default_effort and default_effort not in efforts:
            default_effort = ""
        # 积分倍率：腾讯原始条目无 credits 字段（在 ModelConfig），前端合并；
        # 这里留空字符串占位，由路由层按 model_id 关联 ModelConfig.credit_multiplier。
        out.append({
            "id": mid,
            "name": str(m.get("name") or ""),
            "description": str(m.get("descriptionZh") or m.get("descriptionEn") or ""),
            "context_length": int(m.get("maxInputTokens") or 0),
            "max_output_tokens": int(m.get("maxOutputTokens") or 0),
            "efforts": efforts,
            "default_effort": default_effort,
            "can_disable_thinking": bool(reasoning.get("canDisableThinking")),
            "supports_reasoning": bool(m.get("supportsReasoning")),
            "supports_images": bool(m.get("supportsImages")),
            "supports_tool_call": bool(m.get("supportsToolCall")),
            "only_reasoning": bool(m.get("onlyReasoning")),
            "reasoning_summary": str(reasoning.get("summary") or ""),
            "tags": list(m.get("tags") or []),
            "vendor": str(m.get("vendor") or ""),
            "series": series_of(mid),
            "is_default": bool(m.get("isDefault")),
            "disabled_multimodal": bool(m.get("disabledMultimodal")),
        })
    return out


def _pick_candidates(db: Session, realm: str) -> list[Account]:
    """挑该 realm 最可能可用的账号（active + 有余额），最多 3 个。

    只取该 realm 的账号——拿国际版账号去问国内版端点（或反过来）既取不到
    也平白多一次失败。
    """
    rows = db.query(Account).filter(
        Account.status == "active", Account.balance_remain > 0,
        Account.realm == realm,
    ).order_by(Account.balance_remain.desc()).limit(3).all()
    return rows


def _build(db: Session, realm: str) -> dict:
    """依次尝试：腾讯接口（该 realm 多账号）→ 失败则 none（上游回退由路由层叠加）。"""
    errors: list[str] = []
    candidates = _pick_candidates(db, realm)
    if not candidates:
        errors.append("没有可用的{}账号".format("国际版" if realm == "global" else "国内版"))

    for acc in candidates:
        try:
            with backend.AccountSession(acc.auth_json) as sess:
                raw = sess.fetch_models()
                updated = sess.updated_json()
            # 凭据可能被刷新——写回（与 _do_sync_models 一致的行为）
            if updated and updated != acc.auth_json:
                acc.auth_json = updated
                db.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{acc.name or acc.uid}: {type(exc).__name__} {str(exc)[:120]}")
            continue
        if isinstance(raw, list) and raw:
            return {
                "models": _decorate(raw, realm),
                "source": "tencent",
                "source_label": "腾讯模型接口（含显示名 / 描述 / 推理档位）",
                # via 不返回账号名/uid——模型中心页不该暴露「用哪个账号拉的」，
                # 那是账号标识（uid/昵称/邮箱），对用户无价值且属隐私。
                "via": "",
                "errors": errors,
            }
        errors.append(f"{acc.name or acc.uid}: 返回为空")

    return {
        "models": [],
        "source": "none",
        "source_label": "暂无可用的模型数据来源",
        "via": "",
        "errors": errors,
    }


def catalog(db: Session, realm: str = "cn", force: bool = False) -> dict:
    """返回指定 realm 的模型目录（含 source / fetched_at 等元信息）。

    realm 必须分开取、分开缓存：两边的模型几乎不重叠，共用会互相覆盖。
    """
    realm = realm if realm in ("cn", "global") else "cn"
    with _lock:
        slot = _slot(realm)
        now = time.time()
        if not force and slot["payload"] is not None and now - slot["at"] < slot["ttl"]:
            out = dict(slot["payload"])
            out["cached"] = True
            out["cache_age"] = int(now - slot["at"])
            out["realm"] = realm
            return out

    # 构建放锁外：腾讯调用慢，不能持锁阻塞另一 realm 的请求
    result = _build(db, realm)
    with _lock:
        _slot(realm).update({
            "at": time.time(),
            "ttl": _TTL_OK if result.get("source") == "tencent" else _TTL_FAIL,
            "payload": result,
        })
    out = dict(result)
    out.update(cached=False, cache_age=0, realm=realm,
               fetched_at=int(time.time()))
    return out


def clear_cache(realm: str | None = None) -> None:
    """清缓存（测试用 / sync 后强制刷新）。"""
    with _lock:
        if realm:
            _cache.pop(realm, None)
        else:
            _cache.clear()
