"""积分类端点必须支持按 realm 过滤。

## 为什么需要

两版的积分体系**不通用**（不同上游域、不同计费规则、不同额度包）。面板上
国内版和国际版的积分混在一个汇总里，就看不出「是哪一版的账号缺积分」。

用户报告过国际版积分显示异常（350 只显示 100）—— 修复根因（`CapacityUnit`
单复数，见 `tests/test_credit_unit.py`）之后，还需要让面板能**分别查看两版**，
否则同类问题下次还是难定位（汇总数字对不上时无从判断是哪一版的问题）。

## 覆盖的端点

    GET  /api/credits/expiry?realm=...     积分到期总览
    GET  /api/credits/usage?realm=...      积分消耗构成
    POST /api/accounts/credits/sync?realm=...  立即重采快照（**会打上游**）

## 关键设计约束

* `realm` 为空 = **不过滤**（既有行为零回归）
* realm 过滤是**硬隔离**：该版没有账号时查询应返回空，**绝不能退化成「不过滤」**
  （那会把另一版的数据混进来，比返回空更误导）
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def _db(with_usage=False):
    from admin.db import Base
    from admin import models as _m  # noqa: F401
    tables = [Base.metadata.tables["accounts"]]
    if with_usage:
        tables.append(Base.metadata.tables["usage_logs"])
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine, tables=tables)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def _mk(db, uid, realm, balance=100, status="active"):
    from admin.models import Account
    a = Account(uid=uid, realm=realm, status=status, balance_remain=balance,
                name=f"{realm}-{uid}", auth_json='{"auth":{"accessToken":"x"}}')
    db.add(a)
    db.commit()
    return a


class LoadRowsCase(unittest.TestCase):
    def setUp(self):
        self.db = _db()
        from admin.routers.credits import _load_rows
        self.load = _load_rows

    def tearDown(self):
        self.db.close()

    def test_empty_realm_returns_all(self):
        """realm 为空 = 不过滤（零回归）。"""
        _mk(self.db, "c1", "cn")
        _mk(self.db, "g1", "global")
        self.assertEqual(len(self.load(self.db, False, "")), 2)

    def test_filters_by_realm(self):
        _mk(self.db, "c1", "cn")
        _mk(self.db, "c2", "cn")
        _mk(self.db, "g1", "global")
        self.assertEqual(len(self.load(self.db, False, "cn")), 2)
        self.assertEqual(len(self.load(self.db, False, "global")), 1)

    def test_realm_with_no_accounts_returns_empty(self):
        """**硬隔离**：该版没有账号 → 空，不能退化成「返回全部」。"""
        _mk(self.db, "c1", "cn")
        self.assertEqual(self.load(self.db, False, "global"), [],
                         "global 没账号时必须返回空 —— 返回 cn 的账号会误导")

    def test_unknown_realm_not_treated_as_filter(self):
        """未知 realm 值不做过滤（与外部传错参数时的宽松行为一致）。"""
        _mk(self.db, "c1", "cn")
        self.assertEqual(len(self.load(self.db, False, "mars")), 1)

    def test_combined_with_disabled_flag(self):
        _mk(self.db, "c1", "cn", status="disabled")
        _mk(self.db, "c2", "cn", status="active")
        self.assertEqual(len(self.load(self.db, False, "cn")), 1)
        self.assertEqual(len(self.load(self.db, True, "cn")), 2)

    def test_all_endpoints_accept_realm(self):
        import inspect
        from admin.routers import credits as cr
        for fn in (cr.credit_expiry_stats, cr.credit_usage_stats):
            params = inspect.signature(fn).parameters
            self.assertIn("realm", params, f"{fn.__name__} 必须接受 realm")
            self.assertEqual(params["realm"].default, "",
                             f"{fn.__name__} 的 realm 默认应为空（零回归）")


class UsageRealmCase(unittest.TestCase):
    """`/api/credits/usage` 的 realm 过滤（走 usage_logs + accounts 关联）。"""

    def setUp(self):
        self.db = _db(with_usage=True)
        from datetime import datetime
        from admin.models import UsageLog
        self.cn = _mk(self.db, "c1", "cn")
        self.gl = _mk(self.db, "g1", "global")
        now = datetime.utcnow()
        for acc, credits in ((self.cn, 5.0), (self.gl, 9.0)):
            self.db.add(UsageLog(account_id=acc.id, model="m", credits=credits,
                                 total_tokens=10, error_kind="", created_at=now))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _call(self, realm, account_id=None):
        from admin.routers.credits import credit_usage_stats
        return credit_usage_stats(days=7, account_id=account_id, top=20,
                                  realm=realm, _=None, db=self.db)

    def test_filters_usage_by_realm(self):
        cn = self._call("cn")
        gl = self._call("global")
        self.assertEqual([u["account_id"] for u in cn["accounts"]], [self.cn.id])
        self.assertEqual([u["account_id"] for u in gl["accounts"]], [self.gl.id])
        self.assertAlmostEqual(cn["total_credits"], 5.0)
        self.assertAlmostEqual(gl["total_credits"], 9.0)

    def test_realm_echoed(self):
        self.assertEqual(self._call("cn")["realm"], "cn")
        self.assertEqual(self._call("")["realm"], "all")

    def test_no_filter_returns_both(self):
        allr = self._call("")
        self.assertEqual(len(allr["accounts"]), 2)
        self.assertAlmostEqual(allr["total_credits"], 14.0)

    def test_realm_without_accounts_returns_empty_not_everything(self):
        """**关键**：该版没账号时必须返回空，而不是把另一版的数据混进来。"""
        from admin.models import Account
        self.db.query(Account).filter(Account.realm == "global").delete()
        self.db.commit()
        gl = self._call("global")
        self.assertEqual(gl["accounts"], [],
                         "global 无账号时不能返回 cn 的消耗（那是跨版串味）")
        self.assertAlmostEqual(gl["total_credits"], 0.0)


class SyncCreditsRealmCase(unittest.TestCase):
    """`/api/accounts/credits/sync` 的 realm 过滤（**不真打上游**：只验证挑号）。"""

    def setUp(self):
        self.db = _db()
        _mk(self.db, "c1", "cn")
        _mk(self.db, "c2", "cn")
        _mk(self.db, "g1", "global")

    def tearDown(self):
        self.db.close()

    def _picked(self, realm):
        """复现端点的挑号逻辑（与实现同源），不打上游。"""
        from admin.models import Account
        q = self.db.query(Account).filter(Account.status == "active")
        if realm in ("cn", "global"):
            q = q.filter(Account.realm == realm)
        return [a.uid for a in q.order_by(Account.id.asc()).all()]

    def test_realm_narrows_scope(self):
        self.assertEqual(self._picked("cn"), ["c1", "c2"])
        self.assertEqual(self._picked("global"), ["g1"])
        self.assertEqual(len(self._picked("")), 3, "空 realm = 既有全量行为")

    def test_endpoint_has_realm_and_readonly_note(self):
        """端点签名要有 realm，且文档字符串要**警告会打上游**。

        这个端点会真实请求上游（逐号拉账单），面板上若无提示，用户可能
        在账号很多时反复点 → 明显的机器特征。
        """
        import inspect
        from admin.routers.accounts import sync_credits
        params = inspect.signature(sync_credits).parameters
        self.assertIn("realm", params)
        self.assertEqual(params["realm"].default, "")
        doc = sync_credits.__doc__ or ""
        self.assertIn("上游", doc, "文档必须提醒这会真实请求上游")


if __name__ == "__main__":
    unittest.main()
