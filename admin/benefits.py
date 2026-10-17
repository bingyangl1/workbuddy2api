"""限时权益 / 补偿：查询与领取。

# 设计原则（按用户明确要求）

> 「这些拓展能力别默认启用，有些是只能跑一遍的，多遍跑会检测的，
>    让用户自己点击运行」

所以本模块的设计是：

1. **绝不自动跑**：没有任何调度任务会调用它。`SCHEDULABLE_TASKS` 里没有它，
   `scheduler.run_task()` 也不认它 —— 想跑必须由用户在面板上点。
2. **默认不启用**：不做任何"首次启动自动建计划"的动作。
3. **可以被跑第二遍吗**：技术上可以，但**每账号每期只真正领取一次**。
   靠 DB 唯一约束（`(account_id, campaign_key)`）挡住重复，
   不是靠内存状态 —— 重启也不丢。
4. **领取前必有查询**：先 `compensation_status()` 确认「可领」再 `claim_compensation()`。
   绝不在状态不明时盲目发领取请求（那个才是"多跑会被检测"的风险来源）。

# 为什么有 DB 唯一约束还要查状态

两者防的是不同的事：

* DB 约束防「我们自己重复发请求」（重启 / 并发点击 / 批量重入）
* 状态查询防「上游已经领过了我们还发」（换机器、手动在官网领过）

两道都要，缺一不可。

# realm

端点走 `billing` 域，基址按账号 realm 分派（见 `AccountSession._compensation_request`）。
国际版账号打国际版域 —— 这是本项目的一条硬约束（凭证与域名必须同源）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError

from admin.models import Account, BenefitClaim

_log = logging.getLogger("admin.benefits")

#: 活动期标识。存进 DB 的唯一键组成部分 —— **换期必须换 key**，
#: 否则上一期的「已领取」记录会挡住新一期。
CAMPAIGN_KEY = "meter-benefits"

#: 展示名（面板上用）
CAMPAIGN_TITLE = "限时权益"

#: 上游活动时区（东八区）。`end_time` 缺时区时按这个解释。
DAY_TZ = timezone(timedelta(hours=8))

#: 状态枚举（前端按这个渲染徽标颜色）
STATUSES = ("available", "already", "expired", "ineligible", "unknown", "error")


def _now() -> datetime:
    return datetime.now(DAY_TZ)


def _as_int(value) -> int | None:
    """严格取 int（`bool` 是 int 的子类，必须排除）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _parse_deadline(value) -> datetime | None:
    """解析上游 `end_time`；看不懂就返回 None（当作"截止时间不明确"）。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=DAY_TZ)
    return parsed.astimezone(DAY_TZ)


def classify(payload: dict) -> dict:
    """把上游响应分类成可操作的状态。

    纯函数 —— 不碰网络、不碰 DB，所以可以完整单测（这是本模块最重要的
    可测点：`credit_num` / `end_time` / `claimed` 的每种组合）。

    返回 `{status, ok, reason, credit}`，`credit` 是**本次可领**的积分（不可领时为 0）。
    """
    if not payload.get("ok"):
        return {"status": "error", "ok": False, "credit": 0,
                "reason": "权益状态查询失败，请稍后重试"}
    data = payload.get("data") or {}
    credit = _as_int(data.get("credit_num"))
    deadline = _parse_deadline(data.get("end_time"))
    active = data.get("active")
    claimed = data.get("claimed")

    base = {k: data[k] for k in
            ("active", "claimed", "credit_num", "validity_period", "end_time")
            if k in data}

    if claimed is True:
        return {**base, "status": "already", "ok": True, "credit": 0,
                "reason": "本期权益已领取"}
    if active is False:
        return {**base, "status": "ineligible", "ok": True, "credit": 0,
                "reason": "当前暂无可领取权益"}
    if credit is None or credit <= 0:
        return {**base, "status": "unknown", "ok": False, "credit": 0,
                "reason": "权益金额不明确，未发起领取"}
    if deadline is None:
        return {**base, "status": "unknown", "ok": False, "credit": 0,
                "reason": "权益截止时间不明确，未发起领取"}
    if _now() >= deadline:
        return {**base, "status": "expired", "ok": True, "credit": 0,
                "reason": "本期权益已截止"}
    if active is True and claimed is False:
        return {**base, "status": "available", "ok": True, "credit": credit,
                "reason": f"可领取 {credit} 积分"}
    return {**base, "status": "unknown", "ok": False, "credit": 0,
            "reason": "权益状态不明确，未发起领取"}


def _row_of(db, account_id: int) -> BenefitClaim | None:
    return (db.query(BenefitClaim)
            .filter(and_(BenefitClaim.account_id == account_id,
                         BenefitClaim.campaign_key == CAMPAIGN_KEY))
            .first())


def state(db, account: Account) -> dict:
    """本账号在本期的**已记录**状态（不发任何网络请求）。"""
    row = _row_of(db, account.id)
    if row is None:
        return {"status": "unknown", "ok": True, "credit": 0,
                "reason": "尚未查询", "claimed_at": None, "claim_credit": 0}
    return {
        "status": "already" if row.claimed else "unknown",
        "ok": True,
        "credit": 0,
        "reason": row.last_reason or ("本期已领取" if row.claimed else "尚未查询"),
        "claimed_at": row.claimed_at.isoformat(timespec="seconds")
        if row.claimed_at else None,
        "claim_credit": int(row.credit or 0),
        "checked_at": row.checked_at.isoformat(timespec="seconds")
        if row.checked_at else None,
        "last_http": row.last_http,
        "last_code": row.last_code,
    }


def query(db, account: Account) -> dict:
    """**只读**查询：问上游当前能不能领。不写任何状态、不领取。"""
    from admin.backend import AccountSession

    row = _row_of(db, account.id)
    if row is not None and row.claimed:
        return {"account_id": account.id, "status": "already", "ok": True,
                "credit": 0, "reason": "本期已领取（本地记录）",
                "claimed_at": row.claimed_at.isoformat(timespec="seconds")
                if row.claimed_at else None}

    try:
        with AccountSession(account.auth_json) as sess:
            payload = sess.compensation_status()
    except Exception as e:      # noqa: BLE001  登录态失效等
        _log.warning("权益查询失败 account=%s: %s", account.id, e)
        return {"account_id": account.id, "status": "error", "ok": False,
                "credit": 0, "reason": f"查询失败：{e}"}

    verdict = classify(payload)
    _record(db, account.id, checked=True, claimed=False,
            credit=0, reason=verdict.get("reason") or "",
            http=payload.get("http"), code=payload.get("code"))
    return {"account_id": account.id, **verdict}


def claim(db, account: Account) -> dict:
    """**写操作**：先查后领。每账号每期只真正领一次。

    流程（顺序不可颠倒）：

        ① 本地已领取？        → 直接返回 already（不发请求）
        ② 问上游能不能领？    → 不能就返回原因
        ③ 标记「领取中」（占唯一键）
        ④ 发领取请求
        ⑤ 回写结果

    第 ③ 步用 DB 唯一约束做**互斥**：并发/重入时只有一个能插入成功，
    其余直接返回「已有领取记录」。这比内存锁可靠（重启/多进程都有效）。
    """
    from admin.backend import AccountSession

    # ① 本地记录
    row = _row_of(db, account.id)
    if row is not None and row.claimed:
        return {"account_id": account.id, "status": "already", "ok": True,
                "credit": 0, "reason": "本期已领取（本地记录）"}

    # ② 先查
    try:
        with AccountSession(account.auth_json) as sess:
            payload = sess.compensation_status()
    except Exception as e:      # noqa: BLE001
        return {"account_id": account.id, "status": "error", "ok": False,
                "credit": 0, "reason": f"领取前查询失败，未发起领取：{e}"}

    verdict = classify(payload)
    if verdict["status"] != "available":
        _record(db, account.id, checked=True, claimed=False, credit=0,
                reason=verdict.get("reason") or "",
                http=payload.get("http"), code=payload.get("code"))
        return {"account_id": account.id, **verdict, "skipped": True}

    # ③ 占位（唯一键互斥）
    try:
        _record(db, account.id, checked=True, claimed=False, credit=0,
                reason="领取中", http=payload.get("http"),
                code=payload.get("code"), commit=True)
    except IntegrityError:
        db.rollback()
        return {"account_id": account.id, "status": "already", "ok": True,
                "credit": 0, "reason": "该账号已有领取记录（并发或重复点击）"}

    # ④ 领取
    expect = verdict.get("credit") or 0
    try:
        with AccountSession(account.auth_json) as sess:
            result = sess.claim_compensation()
    except Exception as e:      # noqa: BLE001
        _record(db, account.id, checked=False, claimed=False, credit=0,
                reason=f"领取请求失败：{e}", http=0, code=None)
        return {"account_id": account.id, "status": "error", "ok": False,
                "credit": 0, "reason": f"领取请求失败：{e}"}

    # ⑤ 回写
    if result.get("ok"):
        _record(db, account.id, checked=True, claimed=True, credit=expect,
                reason=f"已领取 {expect} 积分",
                http=result.get("http"), code=result.get("code"))
        return {"account_id": account.id, "status": "claimed", "ok": True,
                "credit": expect, "reason": f"已领取 {expect} 积分"}

    # 领取失败：**不标记 claimed**，让用户下次还能重试
    _record(db, account.id, checked=True, claimed=False, credit=0,
            reason=f"领取未成功（HTTP {result.get('http')} / code {result.get('code')}）",
            http=result.get("http"), code=result.get("code"))
    return {"account_id": account.id, "status": "error", "ok": False, "credit": 0,
            "reason": f"领取未成功（HTTP {result.get('http')} / code {result.get('code')}）"}


def _record(db, account_id: int, *, checked: bool, claimed: bool, credit: int,
            reason: str, http, code, commit: bool = True) -> None:
    """写/更新本期记录（幂等）。"""
    now = datetime.utcnow()
    row = _row_of(db, account_id)
    if row is None:
        row = BenefitClaim(account_id=account_id, campaign_key=CAMPAIGN_KEY,
                           claimed=False, credit=0)
        db.add(row)
        # 让调用方能在下一句 flush 时捕获 IntegrityError（唯一键冲突）
        db.flush()
    row.claimed = bool(row.claimed or claimed)
    if claimed:
        row.claimed_at = now
        row.credit = int(credit or 0)
    if checked:
        row.checked_at = now
    row.last_reason = (reason or "")[:500]
    row.last_http = int(http) if isinstance(http, int) else 0
    row.last_code = int(code) if isinstance(code, int) else None
    if commit:
        db.commit()
    return None


def has_any_claim(db) -> bool:
    """本期是否有过任何成功领取（用于面板上「已跑过」的提示）。"""
    return db.query(BenefitClaim).filter(
        BenefitClaim.campaign_key == CAMPAIGN_KEY,
        BenefitClaim.claimed.is_(True)).count() > 0
