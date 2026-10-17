"""Official model capacity observations, cached independently of account cooldowns."""
from datetime import datetime
import logging
import threading
import time

LABELS = {"idle": "空闲", "normal": "空闲", "busy": "轻微拥堵",
          "congested": "极度拥堵", "unavailable": "不可用", "unknown": "未知"}
CACHE_SECONDS = 50
_lock = threading.Lock()
_cache = {}
_logger = logging.getLogger(__name__)


def normalize_response(models, response):
    if not isinstance(response, dict) or response.get("code") != 0 or not isinstance(response.get("data"), list):
        raise ValueError("Invalid model busy response")
    levels = {model: "unknown" for model in models}
    for item in response["data"]:
        if isinstance(item, dict) and isinstance(item.get("model"), str) and item["model"] in levels:
            value = item.get("busy_level")
            levels[item["model"]] = value if isinstance(value, str) and value in LABELS else "unknown"
    return levels


def get_busy_levels(db, models, realm: str = ""):
    """查上游的模型繁忙程度。

    `realm`（''/cn/global）限定用哪一版的账号去问 —— **两版的上游是两个域**，
    繁忙程度也是各自独立的事实：拿国内版账号问出来的繁忙度去标注国际版模型，
    数值本身就是错的。不传则不限（既有行为，零回归）。
    """
    from admin import backend
    from admin.models import Account
    from sqlalchemy import or_

    models = sorted(set(models))
    now = datetime.utcnow()
    q = db.query(Account).filter(Account.status == "active", *[
        or_(getattr(Account, field).is_(None), getattr(Account, field) <= now)
        for field in ("cool_until", "breaker_until", "degrade_until")
    ])
    r = (realm or "").strip().lower()
    if r in ("cn", "global"):
        q = q.filter(Account.realm == r)
    account = q.order_by(Account.id).first()
    unknown = {m: "unknown" for m in models}
    if not models or not account:
        return {"levels": unknown, "observed_at": None, "stale": True, "realm": r or "all"}
    # Cache is scoped to the upstream identity and the requested model set.
    # （`account.id` 已隐含 realm —— 不同版本的账号必然不同，所以按账号分键
    #   就够了，不必再往 key 里塞 realm。）
    key = (account.id, account.uid, tuple(models))
    with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached["checked"] < CACHE_SECONDS:
            out = {k: v for k, v in cached.items() if k != "checked"}
            out["realm"] = r or "all"
            return out
        levels = {}
        try:
            with backend.AccountSession(account.auth_json) as session:
                try:
                    for offset in range(0, len(models), 50):
                        batch = models[offset:offset + 50]
                        levels.update(normalize_response(batch, session.fetch_model_busy(batch)))
                finally:
                    account.auth_json = session.updated_json()
            db.commit()
            result = {"levels": levels, "observed_at": datetime.utcnow().isoformat() + "Z",
                      "stale": False, "realm": r or "all"}
        except Exception:
            db.rollback()
            _logger.warning("上游模型繁忙程度获取失败，使用缓存或未知状态")
            result = {"levels": cached["levels"] if cached else unknown,
                      "observed_at": cached["observed_at"] if cached else None,
                      "stale": True, "realm": r or "all"}
        # Negative caching also prevents repeated failed requests from every admin view.
        _cache.clear()
        _cache[key] = {**result, "checked": time.monotonic()}
        return result
