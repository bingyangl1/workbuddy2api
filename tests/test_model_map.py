"""P2 T6 模型别名映射：system_settings.model_map（别名 → 真名）。

核心验收（manager issue #47 移植 + PLAN P2 验收口径）：
- 映射在**模型白名单与 realm 判定之前**：下游所有判定看到的都是映射目标
- realm 判映射后的名字：cn 限定密钥不能通过别名调到 global 模型（隔离失效
  且无人察觉——这是 issue #47 的本体）
- UsageLog 双名：model=实际路由名，requested_model=客户端原始名（对账）
- 坏数据宽容：model_map 存成非对象时按「没有映射」处理（不 5xx）
- /api/security/model-map CRUD：整体覆盖、自映射剔除、空项剔除、审计

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


def _set_map(factory, mapping):
    import json
    with factory() as db:
        from admin.models import SystemSetting
        row = db.query(SystemSetting).filter(SystemSetting.key == "model_map").first()
        if row is None:
            row = SystemSetting(key="model_map", value="")
            db.add(row)
        row.value = json.dumps(mapping, ensure_ascii=False)
        db.commit()


class ModelMapRouterCase(unittest.TestCase):
    """管理面：/api/security/model-map CRUD。"""

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

    def test_default_empty(self):
        self.assertEqual(self.client.get("/api/security/model-map").json(), {"map": {}})

    def test_write_and_read(self):
        r = self.client.post("/api/security/model-map",
                             json={"map": {"claude-fable-5": "global:deepseek-v4.1-flash"}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["map"], {"claude-fable-5": "global:deepseek-v4.1-flash"})
        r2 = self.client.get("/api/security/model-map")
        self.assertEqual(r2.json()["map"]["claude-fable-5"], "global:deepseek-v4.1-flash")

    def test_overwrite_replaces(self):
        self.client.post("/api/security/model-map", json={"map": {"a": "b"}})
        r = self.client.post("/api/security/model-map", json={"map": {"c": "d"}})
        self.assertEqual(r.json()["map"], {"c": "d"})

    def test_self_mapping_stripped(self):
        r = self.client.post("/api/security/model-map", json={"map": {"a": "a", "b": "c"}})
        self.assertEqual(r.json()["map"], {"b": "c"})

    def test_empty_entries_stripped(self):
        r = self.client.post("/api/security/model-map",
                             json={"map": {"  ": "x", "y": "  ", "ok": "fine"}})
        self.assertEqual(r.json()["map"], {"ok": "fine"})

    def test_audit_logged(self):
        self.client.post("/api/security/model-map", json={"map": {"a": "b"}})
        with self.factory() as db:
            from admin.models import AuditLog
            rows = db.query(AuditLog).filter(AuditLog.action == "update_model_map").all()
            self.assertEqual(len(rows), 1)


class MapModelHelperCase(unittest.TestCase):
    """proxy._map_model 纯逻辑：命中/未命中/坏数据。"""

    def setUp(self):
        self.factory = _factory()

    def _map(self, model):
        from admin.routers.proxy import _map_model
        with self.factory() as db:
            return _map_model(db, model)

    def test_hit(self):
        _set_map(self.factory, {"alias-x": "global:real-model"})
        self.assertEqual(self._map("alias-x"), "global:real-model")

    def test_miss_passthrough(self):
        _set_map(self.factory, {"alias-x": "y"})
        self.assertEqual(self._map("other"), "other")

    def test_empty_model_passthrough(self):
        _set_map(self.factory, {"a": "b"})
        self.assertEqual(self._map(""), "")
        self.assertIsNone(self._map(None))

    def test_bad_json_treated_as_no_map(self):
        with self.factory() as db:
            from admin.models import SystemSetting
            db.add(SystemSetting(key="model_map", value="{bad json"))
            db.commit()
        self.assertEqual(self._map("alias-x"), "alias-x")

    def test_non_dict_treated_as_no_map(self):
        with self.factory() as db:
            from admin.models import SystemSetting
            db.add(SystemSetting(key="model_map", value='["a","b"]'))
            db.commit()
        self.assertEqual(self._map("alias-x"), "alias-x")


class AliasRealmIsolationCase(unittest.TestCase):
    """issue #47 移植：realm 判映射后的名字。

    链路载体用 validate_key 直接调（mapped_model 参数在 T3 已就位）+
    网关侧「cn 密钥 + 别名指向 global 模型」的端到端拒绝。
    """

    def setUp(self):
        self.factory = _factory()
        with self.factory() as db:
            from admin.models import ApiKey
            from admin.security import hash_key
            # cn 限定密钥
            db.add(ApiKey(id=1, name="cn-key", key_hash=hash_key("sk-cn"),
                          key_prefix="sk-cn", realm="cn"))
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

    def _client(self):
        return TestClient(self._app, raise_server_exceptions=False, client=("10.0.0.9", 51337))

    def test_validate_key_cn_key_rejects_alias_to_global(self):
        """validate_key 单元层：别名 → global:xxx，cn 密钥必须 403。"""
        from admin.security import validate_key
        with self.factory() as db:
            from admin.models import ApiKey
            k = db.query(ApiKey).filter(ApiKey.id == 1).first()
            r = validate_key(k, "10.0.0.9", "claude-fable-5",
                             mapped_model="global:deepseek-v4.1-flash",
                             key_realm="cn")
            self.assertIsNotNone(r)
            self.assertEqual(r.status, 403)
            self.assertEqual(r.code, "realm_forbidden")

    def test_gateway_cn_key_alias_to_global_blocked(self):
        """网关端到端：cn 密钥 + model_map 别名指向 global → 403（不是 404）。"""
        _set_map(self.factory, {"claude-fable-5": "global:deepseek-v4.1-flash"})
        r = self._client().post("/v1/chat/completions",
                                headers={"Authorization": "Bearer sk-cn"},
                                json={"model": "claude-fable-5",
                                      "messages": [{"role": "user", "content": "hi"}]})
        # 403 realm_forbidden：在触碰任何账号池/上游之前就该被拦下
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("realm", r.text)

    def test_gateway_cn_key_alias_to_cn_allowed_past_gate(self):
        """别名指向 cn 模型时 cn 密钥**不**被 realm 拦（往后走到选号阶段，
        测试库无账号 → 应是 503/502 而非 403 realm_forbidden）。"""
        _set_map(self.factory, {"alias-cn": "glm-5.2"})
        with self.factory() as db:
            from admin.models import ModelConfig
            db.add(ModelConfig(model_id="glm-5.2", enabled=1))
            db.commit()
        r = self._client().post("/v1/chat/completions",
                                headers={"Authorization": "Bearer sk-cn"},
                                json={"model": "alias-cn",
                                      "messages": [{"role": "user", "content": "hi"}]})
        self.assertNotEqual(r.status_code, 403, r.text)

    def test_unlimited_realm_key_alias_anywhere(self):
        """realm='' 的密钥别名指向 global 不被 realm 拦。"""
        with self.factory() as db:
            from admin.models import ApiKey
            from admin.security import hash_key
            db.add(ApiKey(id=2, name="any", key_hash=hash_key("sk-any"),
                          key_prefix="sk-any", realm=""))
            db.commit()
        _set_map(self.factory, {"alias-g": "global:deepseek-v4.1-flash"})
        with self.factory() as db:
            from admin.models import ModelConfig
            db.add(ModelConfig(model_id="deepseek-v4.1-flash", enabled=1))
            db.commit()
        r = self._client().post("/v1/chat/completions",
                                headers={"Authorization": "Bearer sk-any"},
                                json={"model": "alias-g",
                                      "messages": [{"role": "user", "content": "hi"}]})
        self.assertNotEqual(r.status_code, 403, r.text)


if __name__ == "__main__":
    unittest.main()
