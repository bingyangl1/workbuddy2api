"""P2 T8 账号池分组：account_groups + accounts.group_id + ApiKey.account_group_id。

核心验收（PLAN P2 验收口径）：
- **组隔离**：绑定分组的密钥只从该组选号；跨组借号绝不发生（含临时冷却兜底）
- **空组显式报错**：组内无可用账号 → 选号返回 None（网关层如实 503，
  绝不静默回落全池——静默回落等于隔离失效且无人察觉，manager 同原则）
- CRUD：创建重名 409、删除守卫（有账号/有密钥绑定 409）、划组/移出、审计
- 存量兼容：group_id=0（未分组）的账号与未绑定密钥行为与之前完全一致

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
        Base.metadata.tables["accounts"],
        Base.metadata.tables["account_groups"],
        Base.metadata.tables["api_keys"],
        Base.metadata.tables["api_key_ips"],
        Base.metadata.tables["audit_logs"],
        Base.metadata.tables["model_groups"],      # keys list_keys 会查
        Base.metadata.tables["model_group_items"],
    ])
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def _account(factory, name, group_id=0, status="active", balance=100):
    with factory() as db:
        from admin.models import Account
        a = Account(name=name, uid=f"uid-{name}", auth_json="{}",
                    status=status, balance_remain=balance, group_id=group_id)
        db.add(a)
        db.commit()
        db.refresh(a)
        return a.id


class SelectAccountGroupCase(unittest.TestCase):
    """选号隔离：_select_account(group_id=...) 只选组内号。"""

    def setUp(self):
        self.factory = _factory()

    def _pick(self, group_id=0, exclude=()):
        from admin.routers.proxy import _select_account
        with self.factory() as db:
            acc = _select_account(db, exclude_ids=set(exclude), group_id=group_id)
            return (acc.id, acc.name, acc.group_id) if acc else None

    def test_unbound_picks_from_all(self):
        a = _account(self.factory, "free1")
        _account(self.factory, "free2")
        hit = self._pick(group_id=0)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[2], 0)

    def test_bound_picks_only_in_group(self):
        from admin.models import AccountGroup
        with self.factory() as db:
            db.add(AccountGroup(id=1, name="vip"))
            db.commit()
        _account(self.factory, "outsider", group_id=0)
        vip = _account(self.factory, "vip1", group_id=1)
        hit = self._pick(group_id=1)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], vip)

    def test_bound_group_never_borrows_from_pool(self):
        """组内只有一个号且被排除 → None（不是去全池借）。"""
        from admin.models import AccountGroup
        with self.factory() as db:
            db.add(AccountGroup(id=1, name="vip"))
            db.commit()
        _account(self.factory, "outsider", group_id=0, balance=999)
        vip = _account(self.factory, "vip1", group_id=1)
        hit = self._pick(group_id=1, exclude={vip})
        self.assertIsNone(hit)

    def test_empty_group_returns_none(self):
        """空组 → None（网关层如实 503，绝不静默回落全池）。"""
        from admin.models import AccountGroup
        with self.factory() as db:
            db.add(AccountGroup(id=1, name="empty"))
            db.commit()
        _account(self.factory, "outsider", group_id=0)
        self.assertIsNone(self._pick(group_id=1))

    def test_group_with_only_disabled_accounts_returns_none(self):
        from admin.models import AccountGroup
        with self.factory() as db:
            db.add(AccountGroup(id=1, name="dead"))
            db.commit()
        _account(self.factory, "dead1", group_id=1, status="disabled")
        _account(self.factory, "outsider", group_id=0)
        self.assertIsNone(self._pick(group_id=1))

    def test_missing_group_column_defaults_zero(self):
        """迁移列默认 0：存量账号自动归未分组（全池可见）。"""
        a = _account(self.factory, "legacy")
        with self.factory() as db:
            from admin.models import Account
            acc = db.query(Account).filter(Account.id == a).first()
            self.assertEqual(acc.group_id, 0)


class AccountGroupRouterCase(unittest.TestCase):
    """管理面：/api/account-groups CRUD + 删除守卫。"""

    def setUp(self):
        self.factory = _factory()

        def _db():
            db = self.factory()
            try:
                yield db
            finally:
                db.close()

        from admin.routers import account_groups as ag_mod
        from admin.db import get_db
        from admin.security import require_admin

        app = FastAPI()
        app.include_router(ag_mod.router)
        app.dependency_overrides[require_admin] = lambda: True
        app.dependency_overrides[get_db] = _db
        self._audit_patch = mock.patch("admin.audit.SessionLocal", self.factory)
        self._audit_patch.start()
        self.addCleanup(self._audit_patch.stop)
        self.client = TestClient(app)

    def test_create_and_list_with_counts(self):
        r = self.client.post("/api/account-groups", json={"name": "vip", "note": "大客户"})
        self.assertEqual(r.status_code, 200, r.text)
        gid = r.json()["id"]
        items = self.client.get("/api/account-groups").json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["accounts"], 0)
        self.assertEqual(items[0]["bound_keys"], 0)

    def test_create_duplicate_name_409(self):
        self.client.post("/api/account-groups", json={"name": "vip"})
        r = self.client.post("/api/account-groups", json={"name": "vip"})
        self.assertEqual(r.status_code, 409)

    def test_patch_rename(self):
        gid = self.client.post("/api/account-groups", json={"name": "a"}).json()["id"]
        r = self.client.patch(f"/api/account-groups/{gid}", json={"name": "b", "note": "x"})
        self.assertEqual(r.status_code, 200)
        items = self.client.get("/api/account-groups").json()["items"]
        self.assertEqual(items[0]["name"], "b")
        self.assertEqual(items[0]["note"], "x")

    def test_patch_rename_clash_409(self):
        self.client.post("/api/account-groups", json={"name": "a"})
        gid = self.client.post("/api/account-groups", json={"name": "b"}).json()["id"]
        r = self.client.patch(f"/api/account-groups/{gid}", json={"name": "a"})
        self.assertEqual(r.status_code, 409)

    def test_delete_empty_group_ok(self):
        gid = self.client.post("/api/account-groups", json={"name": "tmp"}).json()["id"]
        r = self.client.delete(f"/api/account-groups/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/account-groups").json()["items"], [])

    def test_delete_guard_with_accounts_409(self):
        gid = self.client.post("/api/account-groups", json={"name": "vip"}).json()["id"]
        _account(self.factory, "a1", group_id=gid)
        r = self.client.delete(f"/api/account-groups/{gid}")
        self.assertEqual(r.status_code, 409)
        self.assertIn("1 个账号", r.json()["detail"])

    def test_delete_guard_with_bound_keys_409(self):
        gid = self.client.post("/api/account-groups", json={"name": "vip"}).json()["id"]
        with self.factory() as db:
            from admin.models import ApiKey
            from admin.security import hash_key
            db.add(ApiKey(name="k", key_hash=hash_key("sk-g"),
                          key_prefix="sk-g", account_group_id=gid))
            db.commit()
        r = self.client.delete(f"/api/account-groups/{gid}")
        self.assertEqual(r.status_code, 409)
        self.assertIn("1 把密钥", r.json()["detail"])

    def test_delete_missing_404(self):
        r = self.client.delete("/api/account-groups/999")
        self.assertEqual(r.status_code, 404)

    def test_assign_and_unassign_account(self):
        gid = self.client.post("/api/account-groups", json={"name": "vip"}).json()["id"]
        aid = _account(self.factory, "a1")
        r = self.client.post(f"/api/account-groups/{gid}/accounts/{aid}")
        self.assertEqual(r.status_code, 200)
        with self.factory() as db:
            from admin.models import Account
            self.assertEqual(db.query(Account).filter(Account.id == aid).first().group_id, gid)
        r2 = self.client.post(f"/api/account-groups/accounts/{aid}/unassign")
        self.assertEqual(r2.status_code, 200)
        with self.factory() as db:
            from admin.models import Account
            self.assertEqual(db.query(Account).filter(Account.id == aid).first().group_id, 0)

    def test_assign_missing_group_404(self):
        aid = _account(self.factory, "a1")
        r = self.client.post(f"/api/account-groups/999/accounts/{aid}")
        self.assertEqual(r.status_code, 404)

    def test_assign_missing_account_404(self):
        gid = self.client.post("/api/account-groups", json={"name": "vip"}).json()["id"]
        r = self.client.post(f"/api/account-groups/{gid}/accounts/999")
        self.assertEqual(r.status_code, 404)

    def test_audit_trail(self):
        gid = self.client.post("/api/account-groups", json={"name": "vip"}).json()["id"]
        aid = _account(self.factory, "a1")
        self.client.post(f"/api/account-groups/{gid}/accounts/{aid}")
        with self.factory() as db:
            from admin.models import AuditLog
            actions = [r.action for r in db.query(AuditLog).all()]
            self.assertIn("create_account_group", actions)
            self.assertIn("assign_account_group", actions)


class KeyAccountGroupBindingCase(unittest.TestCase):
    """keys CRUD 的 account_group_id 字段（含存在性校验）。"""

    def setUp(self):
        self.factory = _factory()
        with self.factory() as db:
            from admin.models import AccountGroup
            db.add(AccountGroup(id=7, name="vip"))
            db.commit()

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
        self._audit_patch = mock.patch("admin.audit.SessionLocal", self.factory)
        self._audit_patch.start()
        self.addCleanup(self._audit_patch.stop)
        self.client = TestClient(app)

    def test_create_with_account_group(self):
        r = self.client.post("/api/keys", json={"name": "k", "account_group_id": 7})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["account_group_id"], 7)

    def test_create_with_missing_group_400(self):
        r = self.client.post("/api/keys", json={"name": "k", "account_group_id": 999})
        self.assertEqual(r.status_code, 400)

    def test_patch_rebind_group(self):
        kid = self.client.post("/api/keys", json={"name": "k"}).json()["id"]
        r = self.client.patch(f"/api/keys/{kid}", json={"account_group_id": 7})
        self.assertEqual(r.status_code, 200)
        items = self.client.get("/api/keys").json()["items"]
        self.assertEqual(items[0]["account_group_id"], 7)
        self.assertEqual(items[0]["account_group_name"], "vip")

    def test_patch_unbind_with_zero(self):
        kid = self.client.post("/api/keys", json={"name": "k", "account_group_id": 7}).json()["id"]
        r = self.client.patch(f"/api/keys/{kid}", json={"account_group_id": 0})
        self.assertEqual(r.status_code, 200)
        items = self.client.get("/api/keys").json()["items"]
        self.assertEqual(items[0]["account_group_id"], 0)


if __name__ == "__main__":
    unittest.main()
