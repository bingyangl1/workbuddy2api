"""P2 T5 全局 IP 管控：规则 CRUD + /v1 链路拦截 + 访问审计（拦截留痕）。

验收要点（PLAN P2 验收口径）：
- 白名单模式默认拒 / 黑名单模式默认放（未命中规则时）
- 拦截 403 + 原因短码（global_deny / global_not_allowed）入 ip_access_logs
- 伪造 X-Forwarded-For 不能绕过（TRUST_PROXY=0 默认下只认 socket IP）
- 未启用时完全不介入（默认配置 enabled=False）
- 配置/规则读写失败 fail-open（不把网关打成 500）
- /api/security 路由：config 读写、规则增删（CIDR 校验 400）、日志与清空

不依赖 MySQL：内存 SQLite（StaticPool）+ TestClient + dependency_overrides。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def _factory():
    from admin.db import Base
    from admin import models as _m  # noqa: F401

    engine = create_engine("sqlite://", future=True, poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine, tables=[
        Base.metadata.tables["api_keys"],
        Base.metadata.tables["api_key_ips"],
        Base.metadata.tables["usage_logs"],
        Base.metadata.tables["model_groups"],
        Base.metadata.tables["model_group_items"],
        Base.metadata.tables["model_configs"],
        Base.metadata.tables["system_settings"],
        Base.metadata.tables["ip_rules"],
        Base.metadata.tables["ip_access_logs"],
        Base.metadata.tables["audit_logs"],
    ])
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class SecurityRouterCase(unittest.TestCase):
    """管理面：/api/security 路由 CRUD。"""

    def setUp(self):
        self.factory = _factory()

        def _db():
            db = self.factory()
            try:
                yield db
            finally:
                db.close()

        from admin.routers import security as sec_mod
        from admin.db import get_db
        from admin.security import require_admin, require_admin_read

        app = FastAPI()
        app.include_router(sec_mod.router)
        app.dependency_overrides[require_admin] = lambda: True
        app.dependency_overrides[require_admin_read] = lambda: True
        app.dependency_overrides[get_db] = _db
        self._audit_patch = mock.patch("admin.audit.SessionLocal", self.factory)
        self._audit_patch.start()
        self.addCleanup(self._audit_patch.stop)
        self.client = TestClient(app)

    def test_default_config_disabled_blacklist(self):
        r = self.client.get("/api/security/config")
        self.assertEqual(r.json(), {"enabled": False, "mode": "blacklist"})

    def test_write_and_read_config(self):
        r = self.client.post("/api/security/config", json={"enabled": True, "mode": "whitelist"})
        self.assertEqual(r.status_code, 200)
        r2 = self.client.get("/api/security/config")
        self.assertEqual(r2.json(), {"enabled": True, "mode": "whitelist"})

    def test_invalid_mode_422(self):
        r = self.client.post("/api/security/config", json={"enabled": True, "mode": "gray"})
        self.assertEqual(r.status_code, 422)

    def test_add_and_list_rules(self):
        r = self.client.post("/api/security/rules",
                             json={"kind": "deny", "cidr": "203.0.113.0/24", "note": "扫描器"})
        self.assertEqual(r.status_code, 200, r.text)
        items = self.client.get("/api/security/rules").json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["cidr"], "203.0.113.0/24")
        self.assertEqual(items[0]["kind"], "deny")

    def test_add_rule_invalid_cidr_400(self):
        r = self.client.post("/api/security/rules", json={"kind": "deny", "cidr": "not-a-cidr"})
        self.assertEqual(r.status_code, 400)

    def test_add_rule_host_bits_cidr_ok(self):
        r = self.client.post("/api/security/rules", json={"kind": "allow", "cidr": "10.0.0.5/24"})
        self.assertEqual(r.status_code, 200)

    def test_delete_rule(self):
        rid = self.client.post("/api/security/rules",
                               json={"kind": "deny", "cidr": "192.0.2.1"}).json()["id"]
        r = self.client.delete(f"/api/security/rules/{rid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/security/rules").json()["items"], [])

    def test_delete_missing_rule_404(self):
        r = self.client.delete("/api/security/rules/999")
        self.assertEqual(r.status_code, 404)

    def test_logs_clear(self):
        with self.factory() as db:
            from admin.models import IpAccessLog
            db.add(IpAccessLog(ip="1.2.3.4", path="/v1/models", blocked=1, reason="global_deny"))
            db.commit()
        items = self.client.get("/api/security/logs").json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["reason"], "global_deny")
        r = self.client.post("/api/security/logs/clear")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/security/logs").json()["items"], [])


class GlobalIpGateCase(unittest.TestCase):
    """网关链路：_global_ip_gate 在 /v1/* 上的真实拦截。

    用 /v1/usage 做链路载体（它是最简单的已接线入口：无需上游账号）。
    TestClient 默认 peer 是字面量 'testclient'（非 IP，任何 CIDR 都匹配不上），
    必须显式 client=('IP', port) 给出真实 socket 对端。

    evaluate 语义契约（T1 tests/test_iputil.py 钉死）：deny 命中 → 拒（无论
    模式）；whitelist 模式默认拒、allow 命中才放行；blacklist 模式默认放、
    **allow 条目不参与**（allow 的「优先覆盖」语义只在 whitelist 模式里有意义）。
    """

    def setUp(self):
        self.factory = _factory()
        with self.factory() as db:
            from admin.models import ApiKey
            from admin.security import hash_key
            db.add(ApiKey(id=1, name="t", key_hash=hash_key("sk-t5"), key_prefix="sk-t5"))
            db.commit()

        def _db():
            db = self.factory()
            try:
                yield db
            finally:
                db.close()

        from admin.routers import proxy as proxy_mod
        from admin.db import get_db

        app = FastAPI()
        app.include_router(proxy_mod.router)
        app.dependency_overrides[get_db] = _db
        self._sec_patch = mock.patch("admin.security.SessionLocal", self.factory)
        self._sec_patch.start()
        self.addCleanup(self._sec_patch.stop)
        self._app = app

    def _client(self, ip="10.0.0.9"):
        return TestClient(self._app, raise_server_exceptions=False, client=(ip, 51337))

    def _enable(self, mode, rules=()):
        with self.factory() as db:
            from admin.routers.security import _set_config
            from admin.models import IpRule
            _set_config(db, {"enabled": True, "mode": mode})
            for kind, cidr in rules:
                db.add(IpRule(kind=kind, cidr=cidr, note="t"))
            db.commit()

    def _usage(self, ip="10.0.0.9"):
        return self._client(ip).get("/v1/usage", headers={"Authorization": "Bearer sk-t5"})

    # ---- 黑名单模式 ----
    def test_blacklist_hit_blocks_403(self):
        self._enable("blacklist", [("deny", "10.0.0.0/8")])
        r = self._usage("10.0.0.9")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"]["code"], "global_deny")

    def test_blacklist_miss_allows(self):
        self._enable("blacklist", [("deny", "203.0.113.0/24")])
        r = self._usage("10.0.0.9")
        self.assertEqual(r.status_code, 200, r.text)

    def test_blacklist_allow_entry_does_not_override_deny(self):
        """T1 契约：blacklist 模式 allow 不参与——deny 命中即拒。"""
        self._enable("blacklist", [("deny", "10.0.0.0/8"), ("allow", "10.0.0.9")])
        r = self._usage("10.0.0.9")
        self.assertEqual(r.status_code, 403)

    # ---- 白名单模式 ----
    def test_whitelist_miss_blocks_403(self):
        self._enable("whitelist", [("allow", "192.0.2.0/24")])
        r = self._usage("10.0.0.9")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"]["code"], "global_not_allowed")

    def test_whitelist_hit_allows(self):
        self._enable("whitelist", [("allow", "10.0.0.0/8")])
        r = self._usage("10.0.0.9")
        self.assertEqual(r.status_code, 200, r.text)

    def test_whitelist_deny_beats_allow(self):
        """whitelist 模式里 deny 仍优先（命中即拒，哪怕也在 allow 里）。"""
        self._enable("whitelist", [("allow", "10.0.0.0/8"), ("deny", "10.0.0.9")])
        r = self._usage("10.0.0.9")
        self.assertEqual(r.status_code, 403)

    # ---- 拦截留痕 ----
    def test_blocked_logged_with_reason(self):
        self._enable("blacklist", [("deny", "10.0.0.0/8")])
        self._usage("10.0.0.9")
        with self.factory() as db:
            from admin.models import IpAccessLog
            rows = db.query(IpAccessLog).filter(IpAccessLog.blocked == 1).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].reason, "global_deny")
            self.assertEqual(rows[0].path, "/v1/usage")
            self.assertEqual(rows[0].ip, "10.0.0.9")

    def test_allowed_not_logged_by_default(self):
        """AUDIT_ALL_ACCESS 默认 0：放行不入访问日志（不淹没拦截信号）。"""
        self._enable("blacklist", [("deny", "203.0.113.0/24")])
        self._usage("10.0.0.9")
        with self.factory() as db:
            from admin.models import IpAccessLog
            self.assertEqual(db.query(IpAccessLog).count(), 0)

    # ---- 伪造头绕过 ----
    def test_forged_xff_cannot_bypass(self):
        """socket 对端不可信（203.0.113.x 不在 TRUSTED_PROXY_CIDRS 默认集）：
        伪造 XFF/X-Real-IP 一律无视，按 socket IP 判 → 黑名单命中。"""
        self._enable("blacklist", [("deny", "203.0.113.0/24")])
        r = self._client("203.0.113.9").get("/v1/usage", headers={
            "Authorization": "Bearer sk-t5",
            "X-Forwarded-For": "8.8.8.8",
            "X-Real-IP": "8.8.8.8"})
        self.assertEqual(r.status_code, 403)

    def test_forged_xff_cannot_enter_whitelist(self):
        """反向：不可信对端伪造 XFF 成白名单 IP 也混不进白名单模式。"""
        self._enable("whitelist", [("allow", "8.8.8.8")])
        r = self._client("203.0.113.9").get("/v1/usage", headers={
            "Authorization": "Bearer sk-t5",
            "X-Forwarded-For": "8.8.8.8"})
        self.assertEqual(r.status_code, 403)

    # ---- 未启用 / fail-open ----
    def test_disabled_by_default_no_gate(self):
        # 不写任何配置：默认 enabled=False → 直通
        r = self._usage("10.0.0.9")
        self.assertEqual(r.status_code, 200, r.text)

    def test_gate_fail_open_on_db_error(self):
        """规则表读取异常时放行（不 500）。"""
        self._enable("blacklist", [("deny", "10.0.0.0/8")])
        with self.factory() as db:
            from sqlalchemy import text
            db.execute(text("DROP TABLE ip_rules"))
            db.commit()
        r = self._usage("10.0.0.9")
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
