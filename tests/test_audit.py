"""操作审计日志（admin/audit.py + routers/audit.py）单元测试。

不依赖 MySQL：mock `admin.audit.SessionLocal` 指向带内存 SQLite 的
sessionmaker（admin.db 的 engine 是模块级、随配置指向 MySQL，无法
在测试里整体替换，所以只在写入侧注入测试会话工厂）。

覆盖：
- log() 落库字段 / detail 截断 / 旁路容错 / actor 空回 anonymous
- current_actor() 取 ADMIN_USERNAME
- log_request 从 Request 取 IP（X-Real-IP / XFF / 直连）
- 路由层 GET 列表 shape、action 过滤、limit 钳制、clear 幂等与先清后写
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def _make_session_factory():
    """内存 SQLite + 只建 AuditLog 表 → sessionmaker（测试专用工厂）。

    必须 StaticPool：`sqlite://` 默认每个连接一个独立内存库，
    多线程 / 多会话下彼此看不见对方建的表。
    """
    from admin.db import Base
    from admin.models import AuditLog  # noqa: F401  确保模型已注册

    engine = create_engine(
        "sqlite://",
        future=True,
        poolclass=StaticPool,          # 全部连接共享同一个内存库
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=[Base.metadata.tables["audit_logs"]])
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class _FakeClient:
    """starlette Request.client 的替身（(host, port) 命名元组）。"""

    def __init__(self, host):
        self.host = host


def _make_request(headers=None, client_host=None):
    """构造一个最小的 Request 替身，只带 headers / client。

    headers 用 starlette 的 Headers（大小写不敏感——生产 Request 就是它；
    普通 dict 会让 iputil 的 `headers.get('x-real-ip')` 小写取值失配，
    T1 合并后 _request_ip 走 iputil.client_ip，必须用真实 Headers 形态）。
    """
    from starlette.datastructures import Headers as _H

    req = mock.MagicMock(spec=Request)
    req.headers = _H(headers or {})
    req.client = _FakeClient(client_host) if client_host else None
    return req


class AuditTestCase(unittest.TestCase):
    def setUp(self):
        self.factory = _make_session_factory()
        patcher = mock.patch("admin.audit.SessionLocal", self.factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _rows(self):
        with self.factory() as db:
            from admin.models import AuditLog
            return db.query(AuditLog).order_by(AuditLog.id).all()


class TestLog(AuditTestCase):
    def test_log_writes_all_fields(self):
        """log() 落库字段全：actor/action/target/detail/ip。"""
        from admin import audit as A
        A.log("alice", "create_key", target="1:k1", detail="limit=100", ip="1.2.3.4")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r.actor, r.action, r.target, r.detail, r.ip),
                         ("alice", "create_key", "1:k1", "limit=100", "1.2.3.4"))

    def test_log_truncates_detail_500(self):
        """detail 超 500 截断（列宽 String(500)，写超长会整条失败）。"""
        from admin import audit as A
        A.log("a", "x", detail="y" * 600)
        self.assertEqual(len(self._rows()[0].detail), 500)

    def test_log_swallows_db_error(self):
        """旁路容错：SessionLocal 抛异常时 log() 绝不抛（审计不能打挂请求）。"""
        from admin import audit as A
        boom = mock.MagicMock()
        boom.side_effect = RuntimeError("db down")
        with mock.patch("admin.audit.SessionLocal", boom):
            A.log("a", "x")  # 不抛 = 通过

    def test_log_empty_actor_anonymous(self):
        """actor 空回 anonymous。"""
        from admin import audit as A
        A.log("", "x")
        A.log(None, "y")
        rows = self._rows()
        self.assertEqual(rows[0].actor, "anonymous")
        self.assertEqual(rows[1].actor, "anonymous")

    def test_current_actor_uses_admin_username(self):
        """current_actor() 返回 ADMIN_USERNAME；未配置回 'admin'。"""
        from admin import audit as A
        from admin.config import settings
        with mock.patch.object(settings, "ADMIN_USERNAME", "boss"):
            self.assertEqual(A.current_actor(), "boss")
        with mock.patch.object(settings, "ADMIN_USERNAME", ""):
            self.assertEqual(A.current_actor(), "admin")

    def test_log_request_ip_from_xreal(self):
        """X-Real-IP 优先（nginx 用 $remote_addr 覆写，不可伪造）。"""
        from admin import audit as A
        A.log_request("a", "x", request=_make_request(
            {"X-Real-IP": "9.9.9.9", "X-Forwarded-For": "1.1.1.1, 2.2.2.2"},
            client_host="127.0.0.1"))
        self.assertEqual(self._rows()[0].ip, "9.9.9.9")

    def test_log_request_ip_xff_last_then_direct(self):
        """无 X-Real-IP 时取 XFF 最后一段；再退直连 socket。"""
        from admin import audit as A
        A.log_request("a", "x", request=_make_request(
            {"X-Forwarded-For": "1.1.1.1, 2.2.2.2"}, client_host="127.0.0.1"))
        self.assertEqual(self._rows()[0].ip, "2.2.2.2")
        A.log_request("a", "x", request=_make_request(client_host="127.0.0.1"))
        self.assertEqual(self._rows()[-1].ip, "127.0.0.1")

    def test_log_request_none_request_empty_ip(self):
        """request=None 时 ip=""（不影响落库）。"""
        from admin import audit as A
        A.log_request("a", "x", request=None)
        self.assertEqual(self._rows()[0].ip, "")


class TestAuditRouter(AuditTestCase):
    """路由层：TestClient + dependency_overrides 覆写 require_admin。"""

    def _client(self):
        from admin.routers import audit as audit_router
        from admin.security import require_admin, require_admin_read
        from admin.db import get_db

        app = FastAPI()
        app.include_router(audit_router.router)
        app.dependency_overrides[require_admin] = lambda: True
        app.dependency_overrides[require_admin_read] = lambda: True
        # 路由的 get_db 也指向测试工厂：与 admin.audit.SessionLocal 同一个内存库。
        # 必须保持生成器形态（原 get_db 是 yield 依赖，直接给 factory 会 422）。
        def _test_get_db():
            db = self.factory()
            try:
                yield db
            finally:
                db.close()
        app.dependency_overrides[get_db] = _test_get_db
        return TestClient(app)

    def _seed(self, count=3, action="create_account"):
        from admin import audit as A
        for i in range(count):
            A.log("op", action, target=f"t{i}", detail=f"d{i}", ip="1.1.1.1")

    def test_list_shape_and_desc_order(self):
        """GET /api/audit 返回 shape 全（id/created_at/actor/action/target/detail/ip），id 倒序。"""
        self._seed(3)
        resp = self._client().get("/api/audit")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        self.assertEqual(len(items), 3)
        self.assertEqual(
            set(items[0].keys()),
            {"id", "created_at", "actor", "action", "target", "detail", "ip"})
        self.assertEqual([i["id"] for i in items], [3, 2, 1])
        self.assertEqual(items[0]["actor"], "op")
        self.assertEqual(items[0]["target"], "t2")

    def test_list_action_filter(self):
        """action 过滤只返回匹配短码的记录。"""
        self._seed(2, action="create_account")
        self._seed(1, action="delete_account")
        items = self._client().get("/api/audit", params={"action": "delete_account"}).json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["action"], "delete_account")

    def test_list_limit_clamped_to_1000(self):
        """limit > 1000 被钳到 1000（不报错、不放大查询）。"""
        resp = self._client().get("/api/audit", params={"limit": 99999})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["items"], [])

    def test_list_offset(self):
        """offset 翻页正确。"""
        self._seed(3)
        items = self._client().get("/api/audit", params={"limit": 2, "offset": 2}).json()["items"]
        self.assertEqual([i["id"] for i in items], [1])

    def test_clear_then_logs_itself(self):
        """clear 先清数据再写本条 —— 清完后能看到这条 clear_audit。"""
        self._seed(3)
        c = self._client()
        resp = c.post("/api/audit/clear")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        # 先清后写：表里只剩 clear 这一条，而不是空
        items = c.get("/api/audit").json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["action"], "clear_audit")

    def test_clear_idempotent_on_empty_table(self):
        """空表 clear 幂等（不报错），且同样留下一条 clear_audit。"""
        c = self._client()
        resp = c.post("/api/audit/clear")
        self.assertEqual(resp.status_code, 200)
        items = c.get("/api/audit").json()["items"]
        self.assertEqual([i["action"] for i in items], ["clear_audit"])

    def test_clear_requires_admin(self):
        """未覆写鉴权的独立 app：无 token 访问 clear 返回 401。"""
        from admin.routers import audit as audit_router
        app = FastAPI()
        app.include_router(audit_router.router)
        resp = TestClient(app).post("/api/audit/clear")
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
