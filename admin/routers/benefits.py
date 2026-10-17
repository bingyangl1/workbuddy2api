"""限时权益 / 补偿接口。

# 默认关闭，只能手动跑（用户明确要求）

> 「这些拓展能力别默认启用，有些是只能跑一遍的，多遍跑会检测的，
>    让用户自己点击运行」

因此本 router：

* **没有任何定时任务** —— `scheduler.SCHEDULABLE_TASKS` 里没有它，
  `run_task()` 也不认它。想跑必须由用户点。
* **不自动建计划** —— 不碰 `schedules` 表。
* 每次请求都要管理员鉴权（挂在 router 上，杜绝以后新增接口忘加）。

# 两步式：先查后领

    GET  /api/benefits/status            每账号当前能不能领（**只读**）
    POST /api/benefits/query             向

上游真实查询（不领取，可反复点）
    POST /api/benefits/claim             领取（**写操作**，每期每账号只成功一次）

前端必须让用户看到「查询结果」之后再决定要不要点领取 ——
盲领是最容易出事的（状态不明时发写请求）。
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin import benefits
from admin.db import get_db
from admin.models import Account, BenefitClaim
from admin.security import require_admin

_log = logging.getLogger("admin.routers.benefits")

router = APIRouter(prefix="/api/benefits", tags=["benefits"],
                   dependencies=[Depends(require_admin)])


class ClaimIn(BaseModel):
    """领取请求。

    `account_ids` 必填 —— 刻意**不提供「全部账号」的快捷参数**：
    领取是写操作且不可回滚，逼调用方明确点名，避免误点一下批量领完。
    """
    account_ids: list[int]


@router.get("/campaign")
def campaign(db: Session = Depends(get_db)):
    """本期活动信息 + 我们这边的开启状态。

    `scheduled` 恒为 False —— 这是**设计保证**，不是配置项：
    本功能没有调度入口，只能手动跑。
    """
    rows = (db.query(BenefitClaim)
            .filter(BenefitClaim.campaign_key == benefits.CAMPAIGN_KEY).all())
    claimed = [r for r in rows if r.claimed]
    return {
        "campaign_key": benefits.CAMPAIGN_KEY,
        "title": benefits.CAMPAIGN_TITLE,
        "scheduled": False,
        "note": "手动运行：本功能不参与任何定时任务",
        "statuses": list(benefits.STATUSES),
        "accounts_checked": len(rows),
        "accounts_claimed": len(claimed),
        "credits_claimed": sum(int(r.credit or 0) for r in claimed),
    }


@router.get("/status")
def status(db: Session = Depends(get_db)):
    """每账号的**本地**状态（不发网络请求，秒回）。

    用于面板列表渲染。要知道「现在能不能领」必须调 `/query`。
    """
    accounts = (db.query(Account)
                .filter(Account.status == "active")
                .order_by(Account.id.asc()).all())
    out = []
    for a in accounts:
        st = benefits.state(db, a)
        out.append({
            "account_id": a.id,
            "uid": getattr(a, "uid", "") or "",
            "nickname": getattr(a, "nickname", "") or "",
            "realm": getattr(a, "realm", "") or "",
            **st,
        })
    return {"campaign_key": benefits.CAMPAIGN_KEY,
            "title": benefits.CAMPAIGN_TITLE,
            "scheduled": False,
            "accounts": out}


@router.post("/query")
def query(body: ClaimIn, db: Session = Depends(get_db)):
    """向

上游**真实查询**（只读，不领取）。可反复调用。

    风控上这是安全的：查询端点不改变账号状态，官方网页每次打开都会调。
    但为了不被当成刷接口，前端应只在用户点击时调用（本接口不做自动轮询）。
    """
    if not body.account_ids:
        raise HTTPException(400, "请至少选择一个账号")
    accounts = _load(db, body.account_ids)
    results = [benefits.query(db, a) for a in accounts]
    return {"ok": all(r.get("ok") for r in results), "results": results}


@router.post("/claim")
def claim(body: ClaimIn, db: Session = Depends(get_db)):
    """领取（**写操作，不可回滚**）。

    服务端会**先查后领**：只有上游明确说「可领」才会发领取请求。
    每账号每期只成功一次（DB 唯一约束 + `claimed` 标记双重保证）。
    """
    if not body.account_ids:
        raise HTTPException(400, "请至少选择一个账号")
    accounts = _load(db, body.account_ids)
    results = [benefits.claim(db, a) for a in accounts]
    claimed = [r for r in results if r.get("status") == "claimed"]
    return {
        "ok": all(r.get("ok") for r in results),
        "claimed_count": len(claimed),
        "credits": sum(int(r.get("credit") or 0) for r in claimed),
        "results": results,
    }


def _load(db: Session, ids: list[int]) -> list[Account]:
    """按 id 取账号；校验存在且启用（**不静默跳过**，避免「以为领了其实没领」）。"""
    if len(ids) > 200:
        raise HTTPException(400, "单次最多 200 个账号")
    accounts = (db.query(Account)
                .filter(Account.id.in_(ids))
                .order_by(Account.id.asc()).all())
    found = {a.id for a in accounts}
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(404, f"账号不存在：{missing}")
    off = [a.id for a in accounts if a.status != "active"]
    if off:
        raise HTTPException(400, f"账号已停用，请先启用：{off}")
    return accounts
