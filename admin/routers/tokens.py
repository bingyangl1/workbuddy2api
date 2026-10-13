"""管理面作用域 API Token（wbt_ 前缀）：给脚本 / 看板 / CI 的只读凭据（Phase 2 T7）。

实现见 admin/tokensvc.py（SHA-256 存储、prefix 定位、常量时间比较）。
本文件由 T7 任务填充路由实现（CRUD，明文仅创建时返回一次）。
"""
from fastapi import APIRouter

router = APIRouter(prefix="/api/tokens", tags=["tokens"])
