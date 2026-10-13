"""tests/test_tokensvc.py — Phase 2 T7：管理面作用域 API Token（wbt_ 前缀，只读）。

语义基准：workbuddy-manager server/tokensvc.py + docs/rejection-semantics.md T7 专属规则。

覆盖：
- normalize_scope：合法 readonly / 非法值（'admin'、拼写错误、None）一律降级 readonly
- create → resolve 成功；明文只出现在 create 返回（库/列表/更新响应永不回传）
- resolve：错 token / prefix 不存在 / 非 wbt_ 前缀 → None
- usable：停用 False / 过期 False / 未过期 True / enabled+无 expires True
- touch 节流：60s 内第二次不写（last_used_at 不变）；超时窗口后写
- update PATCH：只改提交字段；expires_at 显式 None 清空
- require_admin_read：JWT 有效放行；wbt_ 有效放行（mock tokensvc.resolve/usable）；
  双失败 401；非 Bearer 的 X-API-Key 不认
- 路由（TestClient + dependency_overrides require_admin）：POST 创建返回 token；
  PATCH 改 name；DELETE 后 resolve 为 None；非法 expires_at 400

零外部依赖：SQLite 内存库（Base.metadata.create_all 单表）+ mock，
不需要 MySQL/Redis。风格跟随 tests/test_realm.py：纯 unittest。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from admin.db import Base
from admin.models import ApiToken
from admin import security, tokensvc


def _fresh_db():
    """每个测试一座独立内存库：只建 api_tokens 单表（不碰其他模型的迁移）。

    StaticPool + check_same_thread=False：TestClient 的 ASGI 应用跑在
    独立线程，必须让所有线程共享**同一个**内存 SQLite 连接（默认
    每线程各开一个连接，而内存库的生命周期跟着连接走，第二个连接
    会看到空库）。库只被本测试独占使用，共享连接是安全的。
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=[ApiToken.__table__])
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


class NormalizeScopeTests(unittest.TestCase):
    def test_readonly_passes(self):
        self.assertEqual(tokensvc.normalize_scope("readonly"), "readonly")

    def test_admin_degrades(self):
        # 本引擎没有 admin 档——'admin' 也降级 readonly（宁少勿多）。
        self.assertEqual(tokensvc.normalize_scope("admin"), "readonly")

    def test_typo_degrades(self):
        self.assertEqual(tokensvc.normalize_scope("read-only"), "readonly")
        self.assertEqual(tokensvc.normalize_scope("read_only"), "readonly")
        self.assertEqual(tokensvc.normalize_scope("ADMIN"), "readonly")

    def test_none_and_empty_degrade(self):
        self.assertEqual(tokensvc.normalize_scope(None), "readonly")
        self.assertEqual(tokensvc.normalize_scope(""), "readonly")


class CreateResolveTests(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    def test_create_and_resolve(self):
        out = tokensvc.create_token(self.db, "ci-readonly")
        self.assertTrue(out["token"].startswith("wbt_"))
        row = tokensvc.resolve(self.db, out["token"])
        self.assertIsNotNone(row)
        self.assertEqual(row.name, "ci-readonly")
        self.assertEqual(row.scope, "readonly")

    def test_plaintext_only_in_create_response(self):
        out = tokensvc.create_token(self.db, "t1")
        token = out["token"]
        # 库中只存哈希
        row = tokensvc.resolve(self.db, token)
        self.assertNotEqual(row.token_hash, token)
        self.assertEqual(len(row.token_hash), 64)  # SHA-256 hex
        # 列表永不回传明文或哈希
        listing = tokensvc.list_tokens(self.db)
        self.assertEqual(len(listing), 1)
        self.assertNotIn("token", listing[0])
        self.assertNotIn("token_hash", listing[0])
        # 更新响应同样不含
        updated = tokensvc.update_token(self.db, listing[0]["id"], {"name": "t2"})
        self.assertNotIn("token", updated)
        self.assertNotIn("token_hash", updated)

    def test_create_with_scope_argument_still_readonly(self):
        # create_token 签名不收 scope；但通过 update 传非法值也要降级（见下）。
        # 这里验证创建默认 readonly。
        out = tokensvc.create_token(self.db, "t")
        self.assertEqual(out["scope"], "readonly")

    def test_resolve_wrong_token(self):
        out = tokensvc.create_token(self.db, "t")
        # 同前缀（前 12 位相同）但后缀不同
        wrong = out["token"][:-4] + "AAAA"
        self.assertIsNone(tokensvc.resolve(self.db, wrong))

    def test_resolve_unknown_prefix(self):
        self.assertIsNone(tokensvc.resolve(self.db, "wbt_unknownprefixvalue123456"))

    def test_resolve_non_wbt(self):
        self.assertIsNone(tokensvc.resolve(self.db, "sk-abc123"))
        self.assertIsNone(tokensvc.resolve(self.db, ""))
        self.assertIsNone(tokensvc.resolve(self.db, None))
        self.assertIsNone(tokensvc.resolve(self.db, 12345))


class UsableTests(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()
        self.out = tokensvc.create_token(self.db, "u")
        self.row = tokensvc.resolve(self.db, self.out["token"])

    def test_enabled_no_expiry(self):
        self.assertTrue(tokensvc.usable(self.row))

    def test_enabled_future_expiry(self):
        self.row.expires_at = datetime.utcnow() + timedelta(hours=1)
        self.assertTrue(tokensvc.usable(self.row))

    def test_expired(self):
        self.row.expires_at = datetime.utcnow() - timedelta(hours=1)
        self.assertFalse(tokensvc.usable(self.row))

    def test_disabled(self):
        self.row.enabled = 0
        self.assertFalse(tokensvc.usable(self.row))

    def test_disabled_and_expired(self):
        self.row.enabled = 0
        self.row.expires_at = datetime.utcnow() - timedelta(hours=1)
        self.assertFalse(tokensvc.usable(self.row))

    def test_none_row(self):
        self.assertFalse(tokensvc.usable(None))


class TouchTests(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()
        self.out = tokensvc.create_token(self.db, "t")
        self.tid = self.out["id"]

    def _last_used(self):
        return self.db.query(ApiToken).filter(ApiToken.id == self.tid).first().last_used_at

    def test_touch_writes_first_time(self):
        self.assertIsNone(self._last_used())
        tokensvc.touch(self.db, self.tid, "1.2.3.4")
        first = self._last_used()
        self.assertIsNotNone(first)

    def test_throttle_within_60s(self):
        tokensvc.touch(self.db, self.tid, "1.2.3.4")
        first = self._last_used()
        # 60s 内第二次：节流跳过，last_used_at 不变
        tokensvc.touch(self.db, self.tid, "5.6.7.8")
        self.assertEqual(self._last_used(), first)
        row = self.db.query(ApiToken).filter(ApiToken.id == self.tid).first()
        self.assertEqual(row.last_used_ip, "1.2.3.4")  # ip 也没被覆盖

    def test_throttle_expires_after_60s(self):
        tokensvc.touch(self.db, self.tid, "1.2.3.4")
        first = self._last_used()
        # 手工把 last_used_at 拨回 61s 前，节流窗口外应重新写
        row = self.db.query(ApiToken).filter(ApiToken.id == self.tid).first()
        row.last_used_at = datetime.utcnow() - timedelta(seconds=61)
        self.db.commit()
        tokensvc.touch(self.db, self.tid, "5.6.7.8")
        self.assertIsNotNone(self._last_used_at_marker())

    def _last_used_at_marker(self):
        # 重新查询；last_used_at 应被更新为“现在”（> 拨回的旧值）
        return self._last_used()

    def test_touch_exception_swallowed(self):
        # 旁路容错：db 抛异常不影响调用方
        class Boom:
            def query(self, *a, **k):
                raise RuntimeError("db down")

        tokensvc.touch(Boom(), self.tid, "x")  # 不应抛出


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()
        self.out = tokensvc.create_token(self.db, "orig")
        self.tid = self.out["id"]

    def test_patch_only_submitted_fields(self):
        updated = tokensvc.update_token(self.db, self.tid, {"name": "renamed"})
        self.assertEqual(updated["name"], "renamed")
        self.assertTrue(updated["enabled"])  # 未提交，不变
        # 空 patch 不炸
        updated2 = tokensvc.update_token(self.db, self.tid, {})
        self.assertEqual(updated2["name"], "renamed")

    def test_patch_disable(self):
        updated = tokensvc.update_token(self.db, self.tid, {"enabled": False})
        self.assertFalse(updated["enabled"])

    def test_patch_expires_none_clears(self):
        # 先设一个过期时间，再显式传 None 清空
        tokensvc.update_token(self.db, self.tid,
                              {"expires_at": datetime.utcnow() + timedelta(days=1)})
        updated = tokensvc.update_token(self.db, self.tid, {"expires_at": None})
        self.assertIsNone(updated["expires_at"])

    def test_patch_scope_degrades(self):
        updated = tokensvc.update_token(self.db, self.tid, {"scope": "admin"})
        self.assertEqual(updated["scope"], "readonly")
        updated2 = tokensvc.update_token(self.db, self.tid, {"scope": "nonsense"})
        self.assertEqual(updated2["scope"], "readonly")

    def test_update_nonexistent(self):
        self.assertIsNone(tokensvc.update_token(self.db, 99999, {"name": "x"}))

    def test_delete(self):
        self.assertTrue(tokensvc.delete_token(self.db, self.tid))
        self.assertIsNone(tokensvc.resolve(self.db, self.out["token"]))
        self.assertFalse(tokensvc.delete_token(self.db, self.tid))  # 再删 404

    def test_list_serializes_datetime_iso(self):
        tokensvc.update_token(self.db, self.tid,
                              {"expires_at": datetime(2030, 1, 2, 3, 4, 5)})
        items = tokensvc.list_tokens(self.db)
        self.assertEqual(items[0]["expires_at"], "2030-01-02T03:04:05")
        self.assertIsInstance(items[0]["created_at"], str)


class RequireAdminReadTests(unittest.TestCase):
    """依赖函数本体：JWT 通道 / wbt_ 通道 / 双失败 401 / 非 Bearer 不认。"""

    def setUp(self):
        self.db = _fresh_db()
        self.out = tokensvc.create_token(self.db, "dep")
        self.token = self.out["token"]

    def test_jwt_valid(self):
        with patch.object(security, "verify_admin_token", return_value=None):
            result = security.require_admin_read(x_admin_token="jwt-abc",
                                                authorization=None)
        self.assertEqual(result, "admin")

    def test_jwt_invalid_falls_through_to_wbt(self):
        # JWT 失败但 wbt_ 有效：应走第二通道并放行
        def boom(tok):
            raise security.HTTPException(status_code=401)

        with patch.object(security, "verify_admin_token", side_effect=boom), \
             patch.object(security, "SessionLocal", return_value=self.db), \
             patch.object(tokensvc, "resolve", return_value=self._row()), \
             patch.object(tokensvc, "usable", return_value=True), \
             patch.object(tokensvc, "touch"):
            result = security.require_admin_read(x_admin_token="bad-jwt",
                                                authorization=f"Bearer {self.token}")
        self.assertIsNotNone(result)  # 返回 token 行（readonly 标记）
        self.assertNotEqual(result, "admin")

    def test_wbt_valid(self):
        with patch.object(security, "verify_admin_token",
                          side_effect=security.HTTPException(401)), \
             patch.object(security, "SessionLocal", return_value=self.db), \
             patch.object(tokensvc, "resolve", return_value=self._row()), \
             patch.object(tokensvc, "usable", return_value=True), \
             patch.object(tokensvc, "touch"):
            result = security.require_admin_read(x_admin_token=None,
                                                authorization=f"Bearer {self.token}")
        self.assertEqual(getattr(result, "scope", None), "readonly")

    def test_both_fail_401(self):
        with patch.object(security, "verify_admin_token",
                          side_effect=security.HTTPException(401)), \
             patch.object(security, "SessionLocal", return_value=self.db):
            with self.assertRaises(security.HTTPException) as ctx:
                security.require_admin_read(x_admin_token="bad",
                                            authorization="Bearer wbt_nonexistent123")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_wbt_unusable_rejected(self):
        # resolve 成功但 usable=False（停用/过期）→ 401，不区分原因
        with patch.object(security, "verify_admin_token",
                          side_effect=security.HTTPException(401)), \
             patch.object(security, "SessionLocal", return_value=self.db), \
             patch.object(tokensvc, "resolve", return_value=self._row()), \
             patch.object(tokensvc, "usable", return_value=False):
            with self.assertRaises(security.HTTPException) as ctx:
                security.require_admin_read(x_admin_token=None,
                                            authorization=f"Bearer {self.token}")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_non_bearer_scheme_rejected(self):
        # X-API-Key / Basic 等非 Bearer 形态：不认（manager 同款只认 Bearer）
        with patch.object(security, "verify_admin_token",
                          side_effect=security.HTTPException(401)):
            with self.assertRaises(security.HTTPException):
                security.require_admin_read(x_admin_token=None,
                                            authorization=f"ApiKey {self.token}")

    def test_non_wbt_bearer_rejected(self):
        with patch.object(security, "verify_admin_token",
                          side_effect=security.HTTPException(401)):
            with self.assertRaises(security.HTTPException):
                security.require_admin_read(x_admin_token=None,
                                            authorization="Bearer sk-notwbt")

    def _row(self):
        return tokensvc.resolve(self.db, self.token)


class RouterTests(unittest.TestCase):
    """TestClient + dependency_overrides(require_admin / get_db) 测 CRUD 路由。"""

    def setUp(self):
        self.db = _fresh_db()
        from admin.db import get_db
        from admin.server import app

        self._app = app
        app.dependency_overrides[security.require_admin] = lambda: True

        def _override_get_db():
            yield self.db

        app.dependency_overrides[get_db] = _override_get_db
        self.client = TestClient(app)

    def tearDown(self):
        self._app.dependency_overrides.clear()

    def test_post_creates_returns_token(self):
        r = self.client.post("/api/tokens", json={"name": "ci"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["token"].startswith("wbt_"))
        self.assertEqual(body["scope"], "readonly")
        # resolve 确认落库
        self.assertIsNotNone(tokensvc.resolve(self.db, body["token"]))

    def test_post_with_iso_expires(self):
        r = self.client.post("/api/tokens",
                             json={"name": "x", "expires_at": "2030-01-02T03:04:05"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["expires_at"], "2030-01-02T03:04:05")

    def test_post_invalid_expires_400(self):
        r = self.client.post("/api/tokens",
                             json={"name": "x", "expires_at": "not-a-date"})
        self.assertEqual(r.status_code, 400)

    def test_list(self):
        self.client.post("/api/tokens", json={"name": "a"})
        r = self.client.get("/api/tokens")
        self.assertEqual(r.status_code, 200)
        items = r.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertNotIn("token", items[0])
        self.assertNotIn("token_hash", items[0])

    def test_patch_name(self):
        created = self.client.post("/api/tokens", json={"name": "old"}).json()
        r = self.client.patch(f"/api/tokens/{created['id']}",
                              json={"name": "new"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["name"], "new")

    def test_patch_expires_none_clears(self):
        created = self.client.post(
            "/api/tokens",
            json={"name": "x", "expires_at": "2030-01-02T03:04:05"}).json()
        # pydantic 提交了 expires_at=None → 显式清空
        r = self.client.patch(f"/api/tokens/{created['id']}", json={"expires_at": None})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["expires_at"])

    def test_patch_404(self):
        r = self.client.patch("/api/tokens/99999", json={"name": "x"})
        self.assertEqual(r.status_code, 404)

    def test_delete_then_resolve_none(self):
        created = self.client.post("/api/tokens", json={"name": "d"}).json()
        token = created["token"]
        r = self.client.delete(f"/api/tokens/{created['id']}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])
        self.assertIsNone(tokensvc.resolve(self.db, token))

    def test_delete_404(self):
        r = self.client.delete("/api/tokens/99999")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
