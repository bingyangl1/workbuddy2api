"""P2 T3 密钥全维度：validate_key 判定链 + keys 路由 CRUD + touch_key 记账。

不依赖 MySQL：validate_key 是纯函数（key 行用 MagicMock 构造）；
max_ips 与 touch_key 的 DB 交互用内存 SQLite（StaticPool 共享连接）。
路由层用 FastAPI TestClient + dependency_overrides 覆写 require_admin / get_db。

覆盖（对应 docs/rejection-semantics.md 判定顺序）：
- 停用 403 → 过期 403 → 积分 402 → token 402 → IP 白名单 400 → max_ips 400 →
  realm 403（判映射后名）→ 放行 None
- is_model_list=True 跳过 realm 与模型白名单
- _norm_cidrs：合法归一化 / 非法 400 / 空值过滤
- key_ip_allowlist：坏 JSON 宽容（不拦请求）
- keys CRUD：create 全字段 / patch 显式 None=改回不限 / realm 非法 400 /
  CIDR 非法 400 / delete 级联清 api_key_ips / reset_used 双清零
- touch_key：ip 绑定幂等 / used_tokens 仅真实值 / last_used_at 更新 / 容错不抛
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


def _make_session_factory():
    from admin.db import Base
    from admin import models as _m  # noqa: F401  注册全部模型

    engine = create_engine("sqlite://", future=True, poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine, tables=[
        Base.metadata.tables["api_keys"],
        Base.metadata.tables["api_key_ips"],
        Base.metadata.tables["model_groups"],
    ])
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def _key(**kw):
    """构造一把「默认放行」的 key 行（MagicMock 属性可逐项覆盖）。"""
    k = mock.MagicMock()
    k.id = kw.get("id", 1)
    k.status = kw.get("status", "active")
    k.expires_at = kw.get("expires_at", None)
    k.unlimited = kw.get("unlimited", 0)
    k.credit_limit = kw.get("credit_limit", 0)
    k.credit_used = kw.get("credit_used", 0)
    k.token_quota = kw.get("token_quota", 0)
    k.used_tokens = kw.get("used_tokens", 0)
    k.max_ips = kw.get("max_ips", 0)
    k.ip_allowlist = kw.get("ip_allowlist", "[]")
    k.realm = kw.get("realm", "")
    k.last_used_at = kw.get("last_used_at", None)
    return k


class TestValidateKeyChain(unittest.TestCase):
    """判定链全序：每个维度单独触发 + 顺序（前者命中不走到后者）。"""

    def _v(self, key, ip="1.2.3.4", model="glm-5.2", **kw):
        from admin.security import validate_key
        return validate_key(key, ip, model, **kw)

    # ---- 顺序 1：停用 ----
    def test_disabled_403(self):
        r = self._v(_key(status="revoked"))
        self.assertIsNotNone(r)
        self.assertEqual(r.status, 403)
        self.assertEqual(r.code, "key_disabled")

    def test_disabled_beats_quota(self):
        """停用优先于配额——顺序 1 在 3 之前。"""
        r = self._v(_key(status="revoked", credit_limit=100, credit_used=200))
        self.assertEqual(r.code, "key_disabled")

    # ---- 顺序 2：过期 ----
    def test_expired_403(self):
        r = self._v(_key(expires_at=datetime.utcnow() - timedelta(hours=1)))
        self.assertEqual(r.status, 403)
        self.assertEqual(r.code, "key_expired")

    def test_not_expired_passes_step2(self):
        r = self._v(_key(expires_at=datetime.utcnow() + timedelta(hours=1)))
        self.assertIsNone(r)

    def test_expired_beats_quota(self):
        r = self._v(_key(expires_at=datetime.utcnow() - timedelta(hours=1),
                         credit_limit=100, credit_used=200))
        self.assertEqual(r.code, "key_expired")

    # ---- 顺序 3：积分配额 ----
    def test_credit_quota_402(self):
        r = self._v(_key(credit_limit=100, credit_used=100))
        self.assertEqual(r.status, 402)
        self.assertEqual(r.code, "quota_exceeded")

    def test_credit_unlimited_never_402(self):
        r = self._v(_key(unlimited=1, credit_limit=0, credit_used=999999))
        self.assertIsNone(r)

    def test_credit_limit_zero_means_unlimited(self):
        """T0 特别裁决：limit=0 且非 unlimited = 无限（引擎现状）。"""
        r = self._v(_key(credit_limit=0, credit_used=999999))
        self.assertIsNone(r)

    # ---- 顺序 4：token 配额 ----
    def test_token_quota_402(self):
        r = self._v(_key(token_quota=1000, used_tokens=1000))
        self.assertEqual(r.status, 402)
        self.assertEqual(r.code, "token_quota_exceeded")

    def test_token_quota_zero_means_unlimited(self):
        r = self._v(_key(token_quota=0, used_tokens=10**9))
        self.assertIsNone(r)

    def test_credit_quota_beats_token_quota(self):
        """积分 402 先于 token 402（顺序 3 在 4 之前）。"""
        r = self._v(_key(credit_limit=1, credit_used=1,
                         token_quota=1, used_tokens=1))
        self.assertEqual(r.code, "quota_exceeded")

    # ---- 顺序 5：IP 白名单 ----
    def test_ip_not_in_allowlist_400(self):
        r = self._v(_key(ip_allowlist='["10.0.0.0/8"]'), ip="1.2.3.4")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.code, "ip_not_allowed")

    def test_ip_in_allowlist_passes(self):
        r = self._v(_key(ip_allowlist='["10.0.0.0/8"]'), ip="10.1.2.3")
        self.assertIsNone(r)

    def test_ip_allowlist_exact_ip(self):
        r = self._v(_key(ip_allowlist='["203.0.113.9"]'), ip="203.0.113.9")
        self.assertIsNone(r)

    def test_ip_allowlist_empty_means_any(self):
        r = self._v(_key(ip_allowlist="[]"), ip="1.2.3.4")
        self.assertIsNone(r)

    def test_ip_allowlist_dirty_space_entry(self):
        """存量脏值带空格也要能匹配（ip_matches 内部 strip）。"""
        r = self._v(_key(ip_allowlist='[" 10.0.0.0/8 "]'), ip="10.9.9.9")
        self.assertIsNone(r)

    # ---- 顺序 6：max_ips ----
    def test_max_ips_exceeded_400(self):
        factory = _make_session_factory()
        with mock.patch("admin.security.SessionLocal", factory):
            with factory() as db:
                from admin.models import ApiKeyIp
                db.add(ApiKeyIp(key_id=1, ip="10.0.0.1"))
                db.add(ApiKeyIp(key_id=1, ip="10.0.0.2"))
                db.commit()
            r = self._v(_key(max_ips=2), ip="10.0.0.3")
            self.assertIsNotNone(r)
            self.assertEqual(r.status, 400)
            self.assertEqual(r.code, "too_many_ips")

    def test_max_ips_known_ip_passes(self):
        factory = _make_session_factory()
        with mock.patch("admin.security.SessionLocal", factory):
            with factory() as db:
                from admin.models import ApiKeyIp
                db.add(ApiKeyIp(key_id=1, ip="10.0.0.1"))
                db.add(ApiKeyIp(key_id=1, ip="10.0.0.2"))
                db.commit()
            r = self._v(_key(max_ips=2), ip="10.0.0.1")
            self.assertIsNone(r)

    def test_max_ips_zero_means_unlimited(self):
        factory = _make_session_factory()
        with mock.patch("admin.security.SessionLocal", factory):
            r = self._v(_key(max_ips=0), ip="9.9.9.9")
            self.assertIsNone(r)

    # ---- 顺序 7：realm ----
    def test_realm_cn_rejects_global_403(self):
        r = self._v(_key(), model="global:deepseek-v4", key_realm="cn")
        self.assertEqual(r.status, 403)
        self.assertEqual(r.code, "realm_forbidden")

    def test_realm_global_rejects_cn_403(self):
        r = self._v(_key(), model="glm-5.2", key_realm="global")
        self.assertEqual(r.status, 403)

    def test_realm_cn_allows_cn(self):
        r = self._v(_key(), model="glm-5.2", key_realm="cn")
        self.assertIsNone(r)

    def test_realm_empty_means_any(self):
        r = self._v(_key(), model="global:deepseek-v4", key_realm="")
        self.assertIsNone(r)

    def test_realm_judges_mapped_model(self):
        """issue #47：realm 判映射后的名字。别名 claude-fable-5 → global:xxx
        时，cn 限定密钥应被拒（按映射后名判是 global）。"""
        r = self._v(_key(), model="claude-fable-5",
                    mapped_model="global:deepseek-v4.1-flash", key_realm="cn")
        self.assertEqual(r.status, 403)
        self.assertEqual(r.code, "realm_forbidden")

    def test_realm_mapped_model_none_falls_back_to_model(self):
        """mapped_model=None（T6 未落地）时回退用 model 判——现状语义。"""
        r = self._v(_key(), model="glm-5.2", mapped_model=None, key_realm="cn")
        self.assertIsNone(r)

    def test_realm_missing_model_400(self):
        """限定 realm 的密钥不带 model → 400（manager 同码）。"""
        r = self._v(_key(), model="", key_realm="cn")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.code, "realm_mismatch")

    # ---- is_model_list ----
    def test_model_list_skips_realm(self):
        r = self._v(_key(), model=None, key_realm="cn", is_model_list=True)
        self.assertIsNone(r)

    def test_model_list_still_checks_disabled(self):
        r = self._v(_key(status="revoked"), model=None, is_model_list=True)
        self.assertEqual(r.status, 403)

    def test_model_list_still_checks_quota(self):
        """/v1/models 判配额（与 manager、引擎现状一致）。"""
        r = self._v(_key(credit_limit=1, credit_used=1), model=None,
                    is_model_list=True)
        self.assertEqual(r.status, 402)


class TestNormCidrs(unittest.TestCase):
    def test_valid_passthrough(self):
        from admin.security import _norm_cidrs
        self.assertEqual(_norm_cidrs(["10.0.0.0/8", " 203.0.113.9 "]),
                         ["10.0.0.0/8", "203.0.113.9"])

    def test_empty_filtered(self):
        from admin.security import _norm_cidrs
        self.assertEqual(_norm_cidrs(["", "  ", "10.0.0.0/8"]), ["10.0.0.0/8"])

    def test_invalid_raises_400(self):
        from admin.security import _norm_cidrs
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            _norm_cidrs(["not-a-cidr"])
        self.assertEqual(ctx.exception.status_code, 400)

    def test_host_bits_allowed(self):
        """10.0.0.1/24 这种主机位非零写法按网段理解（strict=False）。"""
        from admin.security import _norm_cidrs
        self.assertEqual(_norm_cidrs(["10.0.0.1/24"]), ["10.0.0.1/24"])


class TestKeyIpAllowlist(unittest.TestCase):
    def test_bad_json_returns_empty(self):
        from admin.security import key_ip_allowlist
        self.assertEqual(key_ip_allowlist(_key(ip_allowlist="{bad json")), [])

    def test_non_list_returns_empty(self):
        from admin.security import key_ip_allowlist
        self.assertEqual(key_ip_allowlist(_key(ip_allowlist='"str"')), [])

    def test_normal_list(self):
        from admin.security import key_ip_allowlist
        self.assertEqual(key_ip_allowlist(_key(ip_allowlist='["a", " b ", ""]')),
                         ["a", "b"])


class TestTouchKey(unittest.TestCase):
    """touch_key：IP 绑定幂等 + used_tokens 仅真实值 + 容错。"""

    def _factory_with_key(self):
        factory = _make_session_factory()
        with factory() as db:
            from admin.models import ApiKey
            from admin.security import hash_key
            db.add(ApiKey(id=1, name="t", key_hash=hash_key("sk-x"),
                          key_prefix="sk-x", used_tokens=0))
            db.commit()
        return factory

    def test_ip_bound_once(self):
        factory = self._factory_with_key()
        from admin.security import touch_key
        from admin.models import ApiKeyIp
        with mock.patch("admin.security.SessionLocal", factory):
            touch_key(1, "10.0.0.1")
            touch_key(1, "10.0.0.1")  # 重复绑定幂等
            touch_key(1, "10.0.0.2")
        with factory() as db:
            ips = {r.ip for r in db.query(ApiKeyIp).filter(ApiKeyIp.key_id == 1)}
            self.assertEqual(ips, {"10.0.0.1", "10.0.0.2"})

    def test_tokens_accrue_only_real(self):
        factory = self._factory_with_key()
        from admin.security import touch_key
        from admin.models import ApiKey
        with mock.patch("admin.security.SessionLocal", factory):
            touch_key(1, "1.1.1.1", tokens=150)
            touch_key(1, "1.1.1.1", tokens=0)     # 上游未返回 → 不计
            touch_key(1, "1.1.1.1", tokens=50)
        with factory() as db:
            k = db.query(ApiKey).filter(ApiKey.id == 1).first()
            self.assertEqual(k.used_tokens, 200)
            self.assertIsNotNone(k.last_used_at)

    def test_db_down_does_not_raise(self):
        from admin.security import touch_key
        boom = mock.MagicMock()
        boom.side_effect = RuntimeError("db down")
        with mock.patch("admin.security.SessionLocal", boom):
            touch_key(1, "1.1.1.1", tokens=10)  # 不抛 = 通过


class TestKeysRouterCrud(unittest.TestCase):
    """路由层：create/patch/delete 全维度（含 realm 暴露与显式 None 语义）。"""

    def setUp(self):
        self.factory = _make_session_factory()
        # get_db 覆写为 yield 形态（直接给 factory 会 422）
        def _db():
            db = self.factory()
            try:
                yield db
            finally:
                db.close()
        from admin.routers import keys as keys_mod
        from admin.db import get_db
        from admin.security import require_admin

        app = FastAPI()
        app.include_router(keys_mod.router)
        app.dependency_overrides[require_admin] = lambda: True
        app.dependency_overrides[get_db] = _db
        # 审计走旁路容错，但 SessionLocal 指向真 MySQL 会慢——mock 掉
        self._audit_patch = mock.patch("admin.audit.SessionLocal", self.factory)
        self._audit_patch.start()
        self.addCleanup(self._audit_patch.stop)
        self.client = TestClient(app)

    def test_create_full_dimensions(self):
        r = self.client.post("/api/keys", json={
            "name": "全维度", "credit_limit": 50, "realm": "cn",
            "expires_at": "2027-06-01T00:00:00", "max_ips": 3,
            "ip_allowlist": ["10.0.0.0/8"], "token_quota": 100000,
        })
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["realm"], "cn")
        self.assertEqual(body["max_ips"], 3)
        self.assertEqual(body["ip_allowlist"], ["10.0.0.0/8"])
        self.assertEqual(body["token_quota"], 100000)
        self.assertTrue(body["expires_at"].startswith("2027-06-01"))
        self.assertTrue(body["key"].startswith("sk-"))

    def test_create_invalid_realm_400(self):
        r = self.client.post("/api/keys", json={"name": "x", "realm": "mars"})
        self.assertEqual(r.status_code, 400)

    def test_create_invalid_cidr_400(self):
        r = self.client.post("/api/keys", json={"name": "x", "ip_allowlist": ["bad"]})
        self.assertEqual(r.status_code, 400)

    def test_create_invalid_expires_400(self):
        r = self.client.post("/api/keys", json={"name": "x", "expires_at": "not-a-date"})
        self.assertEqual(r.status_code, 400)

    def test_patch_explicit_null_realm_and_expires(self):
        """显式传 null/'' = 改回「不限」——不能用真值判断。"""
        r = self.client.post("/api/keys", json={
            "name": "x", "realm": "cn", "expires_at": "2027-06-01T00:00:00"})
        kid = r.json()["id"]
        r2 = self.client.patch(f"/api/keys/{kid}", json={"realm": "", "expires_at": None})
        self.assertEqual(r2.status_code, 200)
        with self.factory() as db:
            from admin.models import ApiKey
            k = db.query(ApiKey).filter(ApiKey.id == kid).first()
            self.assertEqual(k.realm, "")
            self.assertIsNone(k.expires_at)

    def test_patch_reset_used_clears_both(self):
        """reset_used 双清零（积分 + token），与 manager reset_usage 同口径。"""
        r = self.client.post("/api/keys", json={"name": "x"})
        kid = r.json()["id"]
        with self.factory() as db:
            from admin.models import ApiKey
            k = db.query(ApiKey).filter(ApiKey.id == kid).first()
            k.credit_used = 42.0
            k.used_tokens = 4242
            db.commit()
        r2 = self.client.patch(f"/api/keys/{kid}", json={"reset_used": True})
        self.assertEqual(r2.status_code, 200)
        with self.factory() as db:
            from admin.models import ApiKey
            k = db.query(ApiKey).filter(ApiKey.id == kid).first()
            self.assertEqual(k.credit_used, 0)
            self.assertEqual(k.used_tokens, 0)

    def test_delete_cascades_api_key_ips(self):
        r = self.client.post("/api/keys", json={"name": "x"})
        kid = r.json()["id"]
        with self.factory() as db:
            from admin.models import ApiKeyIp
            db.add(ApiKeyIp(key_id=kid, ip="10.0.0.1"))
            db.commit()
        r2 = self.client.delete(f"/api/keys/{kid}")
        self.assertEqual(r2.status_code, 200)
        with self.factory() as db:
            from admin.models import ApiKeyIp
            cnt = db.query(ApiKeyIp).filter(ApiKeyIp.key_id == kid).count()
            self.assertEqual(cnt, 0)

    def test_list_exposes_new_fields(self):
        self.client.post("/api/keys", json={
            "name": "x", "realm": "global", "token_quota": 500})
        r = self.client.get("/api/keys")
        item = r.json()["items"][0]
        self.assertEqual(item["realm"], "global")
        self.assertEqual(item["token_quota"], 500)
        self.assertIn("used_tokens", item)
        self.assertIn("bound_ips", item)
        self.assertIn("expired", item)


if __name__ == "__main__":
    unittest.main()
