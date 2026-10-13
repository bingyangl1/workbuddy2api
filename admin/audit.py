"""操作审计日志：旁路容错的写入封装（Phase 2 T2）。

为什么必须做：管理端的敏感操作（增删账号、key 变更/查看、清空审计）
本身不产生请求日志，出了事只能靠反代 access log 去猜。管理端是公网
入口，这些操作必须可追溯 —— 参考实现 manager 同款（server/security.py
的 audit()）。

审计属于旁路：任何异常都不能影响请求本身（与请求日志同样的处理原则）。
"""
from fastapi import Request

from admin.config import settings
from admin.db import SessionLocal
from admin.models import AuditLog

#: detail 列宽（models.py 里 String(500)）：超长内容截断而不是报错。
DETAIL_MAX = 500


def current_actor() -> str:
    """默认操作者：本引擎管理端是单用户 JWT（require_admin 只认 ADMIN_USERNAME）。"""
    return settings.ADMIN_USERNAME or "admin"


def _request_ip(request: Request) -> str:
    """从 Request 里取来源 IP（严格口径，见 admin/iputil.py）。

    iputil.client_ip 带「TCP 对端须在可信代理网段」判定：服务直接暴露时
    伪造的 X-Real-IP / XFF 不会被采信 —— 审计里的来源 IP 必须是这个
    口径，否则攻击者能把自己的操作记到任意 IP 头上。
    """
    try:
        from admin import iputil
        return iputil.client_ip(request) or ""
    except Exception:  # noqa: BLE001  旁路容错：IP 取不到不拦请求
        return ""


def log(actor: str, action: str, target: str = "", detail: str = "", ip: str = "") -> None:
    """落一条审计记录。**绝不抛异常**。

    为什么必须 try/except 包死：审计是旁路，埋点都加在业务路由的成功
    路径上 —— 数据库抖一下就让「删除账号」返回 500，等于审计反过来
    打挂了它要保护的业务。失败只损失一条日志，抛出去损失的是请求。

    为什么独立开 SessionLocal：埋点处的调用方可能已经 commit/rollback
    或 close 了请求作用域的 db 会话，复用它要么写不进去要么污染事务。
    """
    try:
        entry = AuditLog(
            actor=(actor or "anonymous")[:120],
            action=str(action or "")[:64],
            target=str(target or "")[:255],
            detail=str(detail or "")[:DETAIL_MAX],
            ip=str(ip or "")[:64],
        )
        with SessionLocal() as db:
            db.add(entry)
            db.commit()
    except Exception:  # noqa: BLE001  旁路容错：见 docstring
        pass


def log_request(actor: str, action: str, target: str = "",
                detail: str = "", request: Request = None) -> None:
    """便捷封装：从 Request 取 IP 后落审计。request 为 None 时 ip=""。"""
    ip = _request_ip(request) if request is not None else ""
    log(actor, action, target=target, detail=detail, ip=ip)
