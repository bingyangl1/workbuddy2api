"""P2 T4 /v1/usage 密钥自查端点。

核心验收（manager gateway.key_usage 语义，MIT 参考）：
- 耗尽 / 过期 / 停用**照样可查**（只认 Key 存在，不调 validate_key）
- 零上游调用（data_source=panel-recorded，数字全是面板已记录值）
- allowance 形状：{used, limit, remaining}；0/None = 不限制（None）
- 记一行 0-token UsageLog（use_case=key-usage；监控轮询可见流量来源）
- 401 只在 Key 缺失/不存在（与其它 /v1 端点同口径）

不依赖 MySQL：内存 SQLite（StaticPool）+ TestClient + dependency_overrides。
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
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
        Base.metadata.tables["model_configs"],  # effective_group_models 会查
    ])
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class UsageEndpointCase(unittest.TestCase):
    def setUp(self):
        self.factory = _factory()
        with self.factory() as db:
            from admin.models import ApiKey
            from admin.security import hash_key
            db.add(ApiKey(
                id=1, name="自查", key_hash=hash_key("sk-self"), key_prefix="sk-self",
                credit_limit=100.0, credit_used=30.0, unlimited=0,
                token_quota=10000, used_tokens=2500, status="active",
                realm="cn", expires_at=datetime.now() + timedelta(days=10),
            ))
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
        # touch_key 内部自己开 SessionLocal——指向同一内存库
        self._p = mock.patch("admin.security.SessionLocal", self.factory)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.client = TestClient(app, raise_server_exceptions=False)

    def _usage(self, token="sk-self"):
        return self.client.get("/v1/usage", headers={"Authorization": f"Bearer {token}"})

    def test_happy_path_shape(self):
        r = self._usage()
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertEqual(b["object"], "key.usage")
        self.assertEqual(b["data_source"], "panel-recorded")
        k = b["key"]
        self.assertEqual(k["name"], "自查")
        self.assertEqual(k["prefix"], "sk-self")
        self.assertEqual(k["realm"], "cn")
        self.assertTrue(k["enabled"])
        self.assertFalse(k["expired"])
        self.assertEqual(b["credits"], {"used": 30.0, "limit": 100.0, "remaining": 70.0})
        self.assertEqual(b["tokens"], {"used": 2500, "limit": 10000, "remaining": 7500})
        self.assertIn("snapshot_at", b)

    def test_exhausted_still_queryable(self):
        """积分耗尽的密钥照样能查（402 只拦调用，不拦自查）。"""
        with self.factory() as db:
            from admin.models import ApiKey
            k = db.query(ApiKey).filter(ApiKey.id == 1).first()
            k.credit_used = 100.0  # = limit
            db.commit()
        r = self._usage()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["credits"]["remaining"], 0.0)

    def test_expired_still_queryable(self):
        with self.factory() as db:
            from admin.models import ApiKey
            k = db.query(ApiKey).filter(ApiKey.id == 1).first()
            # 与端点同口径 utcnow（本机 UTC+8，用 now() 会差 8h 测不准）
            k.expires_at = datetime.utcnow() - timedelta(hours=1)
            db.commit()
        r = self._usage()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["key"]["expired"])

    def test_disabled_still_queryable(self):
        with self.factory() as db:
            from admin.models import ApiKey
            k = db.query(ApiKey).filter(ApiKey.id == 1).first()
            k.status = "revoked"
            db.commit()
        r = self._usage()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["key"]["enabled"])

    def test_invalid_key_401(self):
        r = self._usage("sk-nonexistent")
        self.assertEqual(r.status_code, 401)

    def test_missing_key_401(self):
        r = self.client.get("/v1/usage")
        self.assertEqual(r.status_code, 401)

    def test_unlimited_credits_none_limit(self):
        with self.factory() as db:
            from admin.models import ApiKey
            k = db.query(ApiKey).filter(ApiKey.id == 1).first()
            k.unlimited = 1
            db.commit()
        b = self._usage().json()
        self.assertIsNone(b["credits"]["limit"])
        self.assertIsNone(b["credits"]["remaining"])
        self.assertEqual(b["credits"]["used"], 30.0)

    def test_zero_token_quota_none_limit(self):
        with self.factory() as db:
            from admin.models import ApiKey
            k = db.query(ApiKey).filter(ApiKey.id == 1).first()
            k.token_quota = 0
            db.commit()
        b = self._usage().json()
        self.assertIsNone(b["tokens"]["limit"])
        self.assertIsNone(b["tokens"]["remaining"])

    def test_zero_token_usage_log_row(self):
        """每次自查落一行 use_case=key-usage 的 0-token UsageLog。"""
        self._usage()
        self._usage()
        with self.factory() as db:
            from admin.models import UsageLog
            rows = db.query(UsageLog).filter(UsageLog.use_case == "key-usage").all()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0].credits, 0.0)
            self.assertIsNone(rows[0].total_tokens)

    def test_models_from_group_binding(self):
        """models 字段来自引擎的模型分组绑定（空组绑定 = []）。"""
        with self.factory() as db:
            from admin.models import ModelGroup, ModelGroupItem, ApiKey
            db.add(ModelGroup(id=5, name="g"))
            db.add(ModelGroupItem(group_id=5, model_id="glm-5.2"))
            k = db.query(ApiKey).filter(ApiKey.id == 1).first()
            k.group_id = 5
            db.commit()
        b = self._usage().json()
        self.assertEqual(b["key"]["models"], ["glm-5.2"])


if __name__ == "__main__":
    unittest.main()
