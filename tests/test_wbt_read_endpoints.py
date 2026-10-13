"""P2 T9：require_admin_read 批量上端点的端到端验证。

T7 已测依赖本体（JWT/wbt_ 双通道、401 不区分）。本文件验证**接线**：
- wbt_ 只读 token 能读 GET（账号/密钥/日志/统计/审计/IP 管控/分组）
- 同一个 token 写 POST 仍 401/403（只读语义不被「能上 GET」误扩到写）
- 无明文泄漏端点保持 require_admin（keys/{id}/view 给完整 key 明文，
  wbt_ 只读 token 绝不能碰到）

内存 SQLite + TestClient；wbt_ 通道走 tokensvc.resolve/usable 真链路
（不是 mock——mock 掉就测不出「路由真接了 require_admin_read」）。
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
        Base.metadata.tables["accounts"],
        Base.metadata.tables["account_groups"],
        Base.metadata.tables["api_keys"],
        Base.metadata.tables["api_key_ips"],
        Base.metadata.tables["api_tokens"],
        Base.metadata.tables["audit_logs"],
        Base.metadata.tables["usage_logs"],
        Base.metadata.tables["model_groups"],
        Base.metadata.tables["model_group_items"],
        Base.metadata.tables["model_configs"],
        Base.metadata.tables["system_settings"],
        Base.metadata.tables["ip_rules"],
        Base.metadata.tables["ip_access_logs"],
        Base.metadata.tables["account_model_cool"],  # list_accounts 会查
    ])
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class WbtReadEndpointsCase(unittest.TestCase):
    """wbt_ token 走真 tokensvc 链路访问已接线的 GET 端点。"""

    def setUp(self):
        self.factory = _factory()
        # 造一把真 wbt_ token（走 tokensvc 真实建库路径；返回 dict，token 是明文）
        with self.factory() as db:
            from admin import tokensvc
            self.raw = tokensvc.create_token(db, name="t9-dashboard")["token"]

        def _db():
            db = self.factory()
            try:
                yield db
            finally:
                db.close()

        from admin.routers import (account_groups, accounts, audit, groups,
                                   keys, logs, models, security, stats, credits)
        from admin.db import get_db
        from admin.security import require_admin, require_admin_read

        app = FastAPI()
        for m in (account_groups, accounts, audit, groups, keys, logs,
                  models, security, stats, credits):
            app.include_router(m.router)
        # JWT 通道留 401（不 mock require_admin_read——要的就是走真 wbt_ 链路）；
        # 只 mock 写端点的 require_admin（本文件不测写鉴权，只测 wbt_ 摸不到写）
        app.dependency_overrides[get_db] = _db
        # require_admin_read 内部 SessionLocal 指向内存库（tokensvc 查 token 用）
        self._p = mock.patch("admin.security.SessionLocal", self.factory)
        self._p.start()
        self.addCleanup(self._p.stop)
        self._audit_p = mock.patch("admin.audit.SessionLocal", self.factory)
        self._audit_p.start()
        self.addCleanup(self._audit_p.stop)
        self.client = TestClient(app, raise_server_exceptions=False)

    def _get(self, path):
        return self.client.get(path, headers={"Authorization": f"Bearer {self.raw}"})

    # ---- wbt_ 可读 ----
    def test_wbt_can_read_accounts(self):
        r = self._get("/api/accounts")
        self.assertEqual(r.status_code, 200, r.text)

    def test_wbt_can_read_keys(self):
        r = self._get("/api/keys")
        self.assertEqual(r.status_code, 200, r.text)

    def test_wbt_can_read_logs(self):
        r = self._get("/api/logs")
        self.assertEqual(r.status_code, 200, r.text)

    def test_wbt_can_read_stats_usage(self):
        r = self._get("/api/stats/usage")
        self.assertEqual(r.status_code, 200, r.text)

    def test_wbt_can_read_audit(self):
        r = self._get("/api/audit")
        self.assertEqual(r.status_code, 200, r.text)

    def test_wbt_can_read_security_config(self):
        r = self._get("/api/security/config")
        self.assertEqual(r.status_code, 200, r.text)

    def test_wbt_can_read_security_rules(self):
        r = self._get("/api/security/rules")
        self.assertEqual(r.status_code, 200, r.text)

    def test_wbt_can_read_model_map(self):
        r = self._get("/api/security/model-map")
        self.assertEqual(r.status_code, 200, r.text)

    def test_wbt_can_read_account_groups(self):
        r = self._get("/api/account-groups")
        self.assertEqual(r.status_code, 200, r.text)

    def test_wbt_can_read_model_groups(self):
        r = self._get("/api/groups")
        self.assertEqual(r.status_code, 200, r.text)

    def test_wbt_can_read_model_configs(self):
        r = self._get("/api/models/configs")
        self.assertEqual(r.status_code, 200, r.text)

    def test_wbt_can_read_credits_expiry(self):
        r = self._get("/api/credits/expiry")
        self.assertEqual(r.status_code, 200, r.text)

    # ---- wbt_ 不可写（只读语义）----
    def test_wbt_cannot_write_security_config(self):
        r = self.client.post("/api/security/config",
                             headers={"Authorization": f"Bearer {self.raw}"},
                             json={"enabled": True, "mode": "whitelist"})
        # 写端点是 require_admin（JWT-only），wbt_ 不应通过（401 或 403 皆可）
        self.assertIn(r.status_code, (401, 403), r.text)

    def test_wbt_cannot_create_key(self):
        r = self.client.post("/api/keys",
                             headers={"Authorization": f"Bearer {self.raw}"},
                             json={"name": "x"})
        self.assertIn(r.status_code, (401, 403), r.text)

    def test_wbt_cannot_view_plaintext_key(self):
        """keys/{id}/view 给完整 key 明文——只读 token 绝不能碰（保持 JWT-only）。"""
        with self.factory() as db:
            from admin.models import ApiKey
            from admin.security import hash_key
            db.add(ApiKey(id=1, name="k", key_hash=hash_key("sk-real"),
                          key_prefix="sk-real", key_full="c2stcmVhbA=="))
            db.commit()
        r = self._get("/api/keys/1/view")
        self.assertIn(r.status_code, (401, 403), r.text)

    # ---- 匿名全拒 ----
    def test_anonymous_rejected(self):
        r = self.client.get("/api/accounts")
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
