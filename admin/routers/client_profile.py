#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端参数档案的 HTTP 接口（探测 / 查看 / 保存 / 重置）。

背景：出站请求要伪装成官方桌面端，涉及 UA、版本号、风控头、桌面指纹等一批
参数。这些值需要「本地探测 → 后台保存 → 线上同步」这条链路，所以给它们
一套 REST 接口（见 `admin/client_profile.py` 的说明）。

接口一览::

    GET  /api/client-profile              当前生效值 + 每项来源 + 已保存值
    POST /api/client-profile/detect       现场探测本机客户端（不保存）
    PUT  /api/client-profile              保存（merge 语义，可只改一个字段）
    POST /api/client-profile/reset        清空保存值，回到纯探测

安全：全部需要管理员鉴权（`require_admin`）。档案本身不含凭据，但
「伪装参数」被随意改动会直接影响线上稳定性，所以不对外开放。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from admin import client_profile as cprofile
from admin.db import get_db
from admin.security import require_admin

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/client-profile", tags=["client-profile"])


class SaveIn(BaseModel):
    """保存请求。

    profile 只需给出要改的字段（merge 语义）：比如只改 os_version，
    其余保持不动。merge=False 则整体替换。

    `realm` 指定存哪一份（``cn`` / ``global``，默认 ``cn``）：
    两版客户端身份本质不同（品牌段 / 渠道号 / 版本号），必须分开存。
    详见 `admin/client_profile.py` 的 `_REALM_DEFAULTS`。
    """

    profile: dict = Field(default_factory=dict)
    source: str | None = Field(
        default=None,
        description="auto=优先现场探测 / saved=钉住已保存值（线上实例应选 saved）")
    merge: bool = True
    realm: str = Field(default="cn", description="cn / global")


def _payload(db: Session, realm: str = "cn") -> dict:
    """组装给前端的完整视图。

    `realm` 决定展示哪一版的档案（默认 cn 保持零回归）。
    另附 `realms` 字段：两版的**并排摘要**，让设置页一次能看到两个版本
    各自的身份与来源，不必来回切换才发现「原来国际版没配」。
    """
    eff = cprofile.effective(db, realm=realm)
    sav = cprofile.saved(realm)
    det = cprofile.snapshot_live(realm)
    return {
        "realm": realm,
        "effective": eff,
        "saved": sav,
        "detected": det,
        "sources": cprofile.sources(db, realm),
        "source": cprofile.source(realm),
        "defaults": cprofile.DEFAULTS,
        # 有探测值 = 这台机器上确实装了客户端（线上通常为空）
        "has_client": bool(det),
        "diff_saved_vs_effective": cprofile.diff(sav, eff),
        # 两版并排（含各自 identity 摘要），前端设置页可直接并排显示
        "realms": _realms_overview(db),
    }


def _realms_overview(db: Session) -> dict:
    """两版档案的并排摘要（供设置页一次看全）。

    为什么需要：两版参数必须各存各用（混用是最明显的风控特征），
    但用户界面上如果只能一版一版切换看，很容易漏配另一版还不自知。
    所以在每次响应里附上两版的**关键身份字段 + 来源 + 是否已配**。
    """
    out: dict = {}
    for r in ("cn", "global"):
        try:
            eff = cprofile.effective(db, realm=r)
            det = cprofile.snapshot_live(r)
            out[r] = {
                "source": cprofile.source(r),
                "has_detected": bool(det),
                "application_name": eff.get("application_name"),
                "turing_channel_id": eff.get("turing_channel_id"),
                "turing_product_name": eff.get("turing_product_name"),
                "desktop_version": eff.get("desktop_version"),
                "ua": cprofile.ua(realm=r),
            }
        except Exception:      # noqa: BLE001 单版取不到不影响另一版
            out[r] = {"source": "", "has_detected": False, "error": True}
    return out


@router.get("")
def get_profile(realm: str = "cn",
                _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """读取客户端参数档案（`realm` 默认 cn，零回归）。

    响应另含 `realms`：两版并排摘要，便于设置页一次看全（避免漏配另一版）。
    """
    return _payload(db, realm)


@router.post("/detect")
def detect(realm: str = "cn", _: bool = Depends(require_admin)):
    """现场探测本机客户端（只读，不写库）。

    `realm` 决定探测**哪一版**的安装目录（国内版 `WorkBuddy` /
    国际版 `WorkBuddyAI`）—— 不传的话无法知道该探哪一版，
    而两版的参数（渠道号 109144 vs 400111、版本 5.8.3 vs 5.8.4）并不通用。

    线上服务器上探测结果通常为空 —— 这正是需要「本地探测后同步过去」的原因。
    """
    det = cprofile.snapshot_live(realm)
    label = {"cn": "国内版", "global": "国际版"}.get(realm, realm or "默认")
    return {
        "ok": True,
        "realm": realm,
        "detected": det,
        "has_client": bool(det),
        "message": (f"探测到本机 {label} 客户端" if det else
                    f"未探测到本机 {label} WorkBuddy 桌面端（线上实例属正常；"
                    "请在装了对应版本客户端的机器上探测后同步过来）"),
    }


@router.put("")
def save(body: SaveIn, _: bool = Depends(require_admin),
         db: Session = Depends(get_db)):
    # profile 可以为空 —— 只切换取值策略（source）也是合法操作：
    # 「钉住已保存值」与「优先现场探测」的切换不涉及任何字段改动。
    if not isinstance(body.profile, dict):
        raise HTTPException(status_code=400, detail="profile 必须是对象")
    if not body.profile and body.source is None:
        raise HTTPException(status_code=400,
                            detail="profile 与 source 不能同时为空")
    if body.source is not None and body.source not in cprofile._VALID_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"source 只能是 {list(cprofile._VALID_SOURCES)}")
    realm = (body.realm or "cn").strip().lower()
    if realm not in ("cn", "global"):
        raise HTTPException(status_code=400, detail="realm 只能是 cn / global")
    res = cprofile.save(body.profile, new_source=body.source, db=db,
                        merge=body.merge, realm=realm)
    if not res.get("ok"):
        raise HTTPException(status_code=400,
                            detail="；".join(res.get("errors") or ["保存失败"]))
    _logger.info("客户端参数档案已更新（realm=%s，source=%s，改动=%s）",
                 realm, res.get("source"), sorted((body.profile or {}).keys()))
    return {"ok": True, "errors": res.get("errors") or [],
            "source": res.get("source"), **_payload(db, realm)}


@router.post("/reset")
def reset(realm: str = "cn",
          _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """重置某一版的档案（默认 cn）。

    **只重置指定版本** —— 重置国际版不该把国内版的配置也清掉（反之亦然）。
    """
    if realm not in ("cn", "global"):
        raise HTTPException(status_code=400, detail="realm 只能是 cn / global")
    cprofile.reset(db, realm=realm)
    _logger.info("客户端参数档案已重置（realm=%s）", realm)
    return {"ok": True, "realm": realm, **_payload(db, realm)}
