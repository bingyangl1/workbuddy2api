"""界面布局配置（用户自定义菜单）——服务端存储。

## 为什么放服务端（用户选定）

存浏览器 localStorage 的话，换设备 / 换浏览器 / 清缓存就丢。用户明确要**后端存储**，
所以落在 `system_settings` 表的 `ui_layout` 键上（该表本就是键值配置，
不需要新表 + 迁移）。

## 为什么要**校验**（这是本模块存在的主要价值）

布局配置会**直接决定侧边栏渲染什么**。如果把用户提交的 JSON 原样存下、原样返回，
一个坏配置（写错键名、href 拼错、数组里混进对象）就能把整个导航搞没 ——
用户唯一的自救途径是手改数据库。所以：

  * 白名单校验：`href` 必须在 `NAV_ITEMS`（sections.py 同源的清单）里，
    不在的一律丢弃（不报错，静默忽略未知项）
  * 结构校验：`hidden` 必须是字符串数组；`order` 必须是字符串数组
  * **缺失即默认**：没有条目 = 显示全部并按默认顺序，而不是「一个都不显示」
  * 保存的是**归一化后**的数据（只留认识的 href），前端拿到的永远是可渲染的

这样最坏情况是「布局回到默认」，而不是「导航消失」。
"""
from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from admin.models import SystemSetting

_logger = logging.getLogger(__name__)

#: system_settings 里的键名。
SETTING_KEY = "ui_layout"

#: 侧边栏可选条目的 href 白名单。
#:
#: ⚠️ 必须与 `web/lib/app-nav.ts` 的 NAV_GROUPS 保持同源 ——
#: `tests/test_layout_api.py` 会拿它和那个文件逐条比对，漏改会被测出来。
NAV_ITEMS: tuple[str, ...] = (
    # 总览
    "/dashboard",
    # 运营
    "/accounts", "/keys", "/tokens", "/models", "/models/catalog", "/playground",
    # 数据
    "/stats", "/logs", "/credits",
    # 自动化
    "/schedules", "/growth",
    # 治理
    "/security", "/audit", "/account-groups",
    # 系统
    "/settings", "/settings/sync", "/settings/password", "/settings/app-source",
    "/settings/layout", "/backup", "/changelog",
)

#: 分组白名单（同样与前端一致）。
NAV_GROUPS: tuple[str, ...] = ("overview", "ops", "data", "automation", "governance", "system")

#: 单个键的长度上限（防超长字符串灌库）。
_MAX_ITEM = 120


class LayoutError(ValueError):
    """布局配置非法（结构层面，无法归一化）。"""


def _read_raw(db: Session) -> dict | None:
    row = db.query(SystemSetting).filter(SystemSetting.key == SETTING_KEY).first()
    if not row or not (row.value or "").strip():
        return None
    try:
        data = json.loads(row.value)
    except (ValueError, TypeError):
        # 库里存了坏 JSON：当作没配置（而不是抛错让面板打不开）
        _logger.warning("ui_layout 存储的 JSON 无法解析，已按默认布局处理")
        return None
    return data if isinstance(data, dict) else None


def _sanitize(data: dict | None) -> dict:
    """归一化：只保留认识的 href / 分组，未知项静默丢弃。"""
    hidden: list[str] = []
    order: list[str] = []
    groups: dict[str, str] = {}

    if isinstance(data, dict):
        raw_hidden = data.get("hidden")
        if isinstance(raw_hidden, list):
            for h in raw_hidden:
                if isinstance(h, str) and h in NAV_ITEMS and h not in hidden:
                    hidden.append(h)

        raw_order = data.get("order")
        if isinstance(raw_order, list):
            for h in raw_order:
                if isinstance(h, str) and h in NAV_ITEMS and h not in order:
                    order.append(h)

        raw_groups = data.get("groupLabels")
        if isinstance(raw_groups, dict):
            for k, v in raw_groups.items():
                if k in NAV_GROUPS and isinstance(v, str) and v.strip():
                    groups[k] = v.strip()[:_MAX_ITEM]

    return {"hidden": hidden, "order": order, "groupLabels": groups}


def load(db: Session) -> dict:
    """读布局配置（永远返回可渲染的结构；未配置 = 默认布局）。"""
    return _sanitize(_read_raw(db))


def save(db: Session, data: dict) -> dict:
    """保存布局配置（归一化后落库）。结构非法抛 LayoutError。"""
    if data is not None and not isinstance(data, dict):
        raise LayoutError("布局配置必须是对象")
    clean = _sanitize(data)
    payload = json.dumps(clean, ensure_ascii=False)
    row = db.query(SystemSetting).filter(SystemSetting.key == SETTING_KEY).first()
    if row:
        row.value = payload
    else:
        db.add(SystemSetting(key=SETTING_KEY, value=payload))
    db.commit()
    return clean


def reset(db: Session) -> dict:
    """恢复默认布局（删除该键）。"""
    row = db.query(SystemSetting).filter(SystemSetting.key == SETTING_KEY).first()
    if row:
        db.delete(row)
        db.commit()
    return _sanitize(None)


def available_items() -> list[str]:
    """全部可配置条目（前端配置页用）。"""
    return list(NAV_ITEMS)
