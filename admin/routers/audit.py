"""操作审计日志：管理端敏感操作的留痕与查询（Phase 2 T2）。

表结构见 admin/models.py::AuditLog；写入走 admin/audit.py 的旁路容错封装。
本文件由 T2 任务填充路由实现（列表 / 清理）。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from admin.audit import current_actor, log_request
from admin.db import get_db
from admin.models import AuditLog
from admin.security import require_admin

router = APIRouter(prefix="/api/audit", tags=["audit"])

#: 列表单页上限：审计是排障入口不是导出工具，超大 limit 会把响应
#: 撑到几 MB（detail 列 500 字符 × 1000 行）。超过一律钳到 1000。
MAX_LIMIT = 1000


@router.get("")
def list_audit(
    limit: int = 200,
    offset: int = 0,
    action: str = "",
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """按 id 倒序列审计日志；action 可选过滤。"""
    limit = max(0, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    q = db.query(AuditLog)
    if action:
        q = q.filter(AuditLog.action == action)
    rows = q.order_by(AuditLog.id.desc()).offset(offset).limit(limit).all()
    return {
        "items": [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "actor": r.actor,
                "action": r.action,
                "target": r.target,
                "detail": r.detail,
                "ip": r.ip,
            }
            for r in rows
        ]
    }


@router.post("/clear")
def clear_audit(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """清空审计表。

    先清数据再写本条 —— 否则这条 clear_audit 会被自己删掉，
    「谁清空了日志」恰恰是最需要留痕的操作。
    （delete → commit 之后再 log()，新记录在空表之后写入。）
    """
    db.query(AuditLog).delete(synchronize_session=False)
    db.commit()
    log_request(current_actor(), "clear_audit", target="audit_logs",
                detail="cleared all audit logs", request=None)
    return {"ok": True, "cleared": True}
