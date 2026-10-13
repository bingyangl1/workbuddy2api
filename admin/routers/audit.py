"""操作审计日志：管理端敏感操作的留痕与查询（Phase 2 T2）。

表结构见 admin/models.py::AuditLog；写入走 admin/audit.py 的旁路容错封装。
本文件由 T2 任务填充路由实现（列表 / 清理）。
"""
from fastapi import APIRouter

router = APIRouter(prefix="/api/audit", tags=["audit"])
