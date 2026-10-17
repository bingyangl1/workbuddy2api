"""上游错误码 → 中文解释（147 条，来自官方客户端）。

# 数据来源

`admin/data/upstream_error_messages.json` 提取自 **WorkBuddy 官方客户端**的
错误码枚举（`ServerErrorCode`）与其面向用户的文案。上游 fork 在 1.0.5
首次引入这份表。

# 为什么要这份映射

上游返回的错误是**业务 code + 英文/中文混杂的 message**，用户看到的东西
通常是这样：

    HTTP 429  {"code": 6004, "msg": "CraftRateTPDLimit"}

这既不知道是什么意思，也不知道该怎么办（换号？等一会？换模型？）。
而官方客户端对每个 code 都有明确的一句话解释 —— 直接复用是最准确的。

# 边界：只做「解释」，不做「决策」

本模块**只回答「这个 code 是什么意思」**，不决定是否重试、冷却多久。

决策逻辑在 `admin/routers/proxy.py`（`_MODEL_RATE_CODES` / `_QUEUE_CODES` /
`_REQUEST_RATE_CODES` 与实际冷却时长）。刻意分开是因为：

  * 解释文案可以随客户端更新而更新，不影响调度行为
  * 决策要基于我们自己对账号池的理解，不能照抄客户端（客户端是单账号视角）

这条边界与上游 1.0.5 的设计一致（`upstream_errors.py` 的文档串：
"Keep upstream evidence separate from gateway decisions and client UI text"）。
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

_log = logging.getLogger("admin.upstream_errors")

_DATA = Path(__file__).with_name("data") / "upstream_error_messages.json"


@lru_cache(maxsize=1)
def _table() -> dict[str, dict]:
    """读错误码表（只读一次，进程内缓存）。"""
    try:
        raw = json.loads(_DATA.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    except (OSError, ValueError) as e:
        # 文件缺失/损坏时**不能影响主流程** —— 错误码解释只是锦上添花
        _log.warning("读取上游错误码表失败（%s）：%s", _DATA, e)
        return {}


def describe(code) -> dict:
    """返回 `{message, description}`；未知 code 返回空串字段。

    `describe("6008")` →
        {"message": "今日次数已用完，明日恢复",
         "description": "...", "known": True}
    """
    key = str(code).strip() if code is not None else ""
    item = _table().get(key)
    if not item:
        return {"message": "", "description": "", "known": False}
    return {
        "message": str(item.get("message") or ""),
        "description": str(item.get("description") or ""),
        "known": True,
    }


def message_of(code, fallback: str = "") -> str:
    """只要一句话解释；未知时返回 `fallback`。"""
    got = describe(code)["message"]
    return got or fallback


def known_codes() -> list[str]:
    """所有有解释的 code（供自检/测试用）。"""
    return sorted(_table(), key=lambda s: (len(s), s))


def coverage() -> dict:
    """覆盖率统计（自检用）：多少条、按位数分布。"""
    codes = known_codes()
    buckets: dict[str, int] = {}
    for c in codes:
        buckets[str(len(c)) + "位"] = buckets.get(str(len(c)) + "位", 0) + 1
    return {"total": len(codes), "by_length": buckets}
