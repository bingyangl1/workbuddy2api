"""界面布局配置路由（P3 收尾：用户自定义菜单）。

端点：
    GET    /api/layout          读当前布局（未配置 = 默认）
    PUT    /api/layout          保存布局（归一化；结构非法 400）
    POST   /api/layout/reset    恢复默认
    GET    /api/layout/items    可配置条目清单（配置页渲染用）

权限：`require_admin`（单管理员面板，非只读——布局是**会改行为**的配置）。
存储：system_settings 表的 ui_layout 键（见 admin/layout.py 的说明）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin import layout as layout_mod
from admin.audit import current_actor, log_request
from fastapi import Request
from admin.db import get_db
from admin.security import require_admin

router = APIRouter(prefix="/api/layout", tags=["layout"])


class LayoutIn(BaseModel):
    hidden: list[str] = []
    order: list[str] = []
    groupLabels: dict[str, str] = {}


@router.get("", dependencies=[Depends(require_admin)])
def get_layout(db: Session = Depends(get_db)):
    """当前布局 + 可配置条目（一次请求拿全，配置页少一次往返）。"""
    return {
        "layout": layout_mod.load(db),
        "items": layout_mod.available_items(),
        "groups": list(layout_mod.NAV_GROUPS),
    }


@router.put("")
def put_layout(body: LayoutIn, request: Request,
               _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """保存布局。未知条目会被静默丢弃（不报错），结构非法才 400。"""
    try:
        saved = layout_mod.save(db, body.model_dump())
    except layout_mod.LayoutError as e:
        raise HTTPException(status_code=400, detail=str(e))
    log_request(current_actor(), "layout.save",
                detail=f"hidden={len(saved['hidden'])} order={len(saved['order'])} "
                       f"groupLabels={len(saved['groupLabels'])}",
                request=request)
    return {"ok": True, "layout": saved}


@router.post("/reset")
def reset_layout(request: Request, _: bool = Depends(require_admin),
                 db: Session = Depends(get_db)):
    """恢复默认布局（删除 ui_layout 键）。"""
    result = layout_mod.reset(db)
    log_request(current_actor(), "layout.reset", request=request)
    return {"ok": True, "layout": result}
