"""模型同步必须按 realm 挑账号 —— 真实缺陷的回归测试。

## 用户实测报告的现象

> 「我看页面模型 tab 里，不管选国际版还是国内版，同步的都是国内版的模型」

## 根因

`_do_sync_models` 挑账号的写法是：

    acc = db.query(Account).filter(status=="active", balance_remain > 0).first()

`.first()` 取 **id 最小的那条**。而账号按导入顺序入库、国内版通常先导
→ **切到国际版点同步，仍然用国内版账号去拉，写回的也是国内版清单**。

端点甚至**没有 realm 参数**。

## 修法

* `_pick_sync_account(db, realm)` 按 realm 过滤后再挑（同 realm 内优先有余额的）
* `_do_sync_models(db, realm=...)` 给国际版模型加 `global:` 前缀（realm 隔离）
* `POST /api/models/sync?realm=...` 暴露参数（默认 `cn` 保持零回归）
* 无账号时报错信息**指明是哪一版**没有账号（否则用户以为账号池是空的）
* 定时任务 `sync_models` 两版各跑一次（单版失败不影响另一版）

## 本测试覆盖

挑号逻辑（纯 DB 操作，不打上游）+ 前缀逻辑 + 端点签名 + 定时任务双跑。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def _db():
    from admin.db import Base
    from admin import models as _m  # noqa: F401
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine, tables=[
        Base.metadata.tables["accounts"],
        Base.metadata.tables["model_configs"],
    ])
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def _mk_account(db, *, uid, realm, status="active", balance=100):
    from admin.models import Account
    a = Account(uid=uid, realm=realm, status=status, balance_remain=balance,
                auth_json='{"auth":{"accessToken":"x"}}')
    db.add(a)
    db.commit()
    return a


class PickAccountCase(unittest.TestCase):
    def setUp(self):
        self.db = _db()
        from admin.routers.models import _pick_sync_account
        self.pick = _pick_sync_account

    def tearDown(self):
        self.db.close()

    def test_cn_picks_cn_account(self):
        """**核心**：cn 必须挑到 cn 账号，即使 global 账号 id 更小。"""
        g = _mk_account(self.db, uid="g1", realm="global")   # 先建 → id 更小
        c = _mk_account(self.db, uid="c1", realm="cn")
        got = self.pick(self.db, "cn")
        self.assertIsNotNone(got)
        self.assertEqual(got.id, c.id,
                         "**修复前会挑到 id 最小的 global 账号** —— "
                         "这正是「选国际版同步出来还是国内版」的根因")

    def test_global_picks_global_account(self):
        _mk_account(self.db, uid="c1", realm="cn")
        g = _mk_account(self.db, uid="g1", realm="global")
        got = self.pick(self.db, "global")
        self.assertIsNotNone(got)
        self.assertEqual(got.id, g.id)

    def test_no_cross_realm_fallback(self):
        """**不该跨 realm 兜底**：cn 没有账号时不能拿 global 的顶上。

        跨 realm 兜底会让国际版账号带国内版身份出站（风控特征），
        正确行为是返回 None → 端点报「该版无可用账号」。
        """
        _mk_account(self.db, uid="g1", realm="global")
        self.assertIsNone(self.pick(self.db, "cn"),
                          "cn 没有账号时**不能**挑 global 账号（那是跨 realm 误用）")

    def test_prefers_account_with_balance(self):
        """同 realm 内优先有余额的（没余额的账号拉不了上游）。"""
        _mk_account(self.db, uid="c-zero", realm="cn", balance=0)
        rich = _mk_account(self.db, uid="c-rich", realm="cn", balance=500)
        got = self.pick(self.db, "cn")
        self.assertEqual(got.id, rich.id, "应优先挑有余额的账号")

    def test_falls_back_to_active_without_balance(self):
        """同 realm 内都没余额时，退而用活跃账号（余额字段可能还没同步过）。"""
        only = _mk_account(self.db, uid="c-only", realm="cn", balance=0)
        got = self.pick(self.db, "cn")
        self.assertIsNotNone(got, "不该因为余额为 0 就完全不挑")
        self.assertEqual(got.id, only.id)

    def test_skips_disabled(self):
        _mk_account(self.db, uid="c-off", realm="cn", status="disabled")
        got = self.pick(self.db, "cn")
        self.assertIsNone(got, "停用账号不该被挑中")

    def test_unknown_realm_filters_nothing(self):
        """realm 为空/未知时不加过滤（保持既有的宽松行为，零回归）。"""
        _mk_account(self.db, uid="c1", realm="cn")
        self.assertIsNotNone(self.pick(self.db, ""))


class PrefixCase(unittest.TestCase):
    """国际版模型加 `global:` 前缀（realm 隔离）；国内版保持无前缀（零回归）。"""

    def _run_sync(self, realm, models):
        """跑 _do_sync_models，把上游调用换成假数据。"""
        db = _db()
        _mk_account(db, uid=f"{realm}-1", realm=realm)
        from unittest import mock
        from admin.routers import models as m
        fake_sess = mock.MagicMock()
        fake_sess.__enter__ = mock.Mock(return_value=fake_sess)
        fake_sess.__exit__ = mock.Mock(return_value=False)
        fake_sess.fetch_models.return_value = models
        fake_sess.updated_json.return_value = '{"auth":{"accessToken":"x"}}'
        with mock.patch.object(m.backend, "AccountSession", return_value=fake_sess):
            m._do_sync_models(db, realm=realm)
        from admin.models import ModelConfig
        pairs = {(x.realm, x.model_id) for x in db.query(ModelConfig).all()}
        db.close()
        return pairs

    def test_global_gets_global_realm(self):
        """国际版模型写入时 `realm='global'`，`model_id` 保持裸名。"""
        pairs = self._run_sync("global", [{"id": "glm-5.1", "name": "GLM"}])
        self.assertIn(("global", "glm-5.1"), pairs,
                      "国际版模型应以 realm='global' + 裸名存储（realm 隔离）")

    def test_cn_gets_cn_realm(self):
        """国内版写入时 `realm='cn'`，`model_id` 保持裸名。"""
        pairs = self._run_sync("cn", [{"id": "glm-5.1", "name": "GLM"}])
        self.assertIn(("cn", "glm-5.1"), pairs)
        self.assertNotIn(("global", "glm-5.1"), pairs,
                         "国内版不该被写成 global")

    def test_auto_skipped_for_both_realms(self):
        for realm in ("cn", "global"):
            pairs = self._run_sync(realm, [{"id": "auto", "name": "Auto"}])
            self.assertNotIn((realm, "auto"), pairs,
                             f"{realm} 的同步不该写入 auto（我们有自己的选型逻辑）")

    def test_realm_reported_in_result(self):
        db = _db()
        _mk_account(db, uid="g-1", realm="global")
        from unittest import mock
        from admin.routers import models as m
        fake = mock.MagicMock()
        fake.__enter__ = mock.Mock(return_value=fake)
        fake.__exit__ = mock.Mock(return_value=False)
        fake.fetch_models.return_value = [{"id": "x"}]
        fake.updated_json.return_value = '{"auth":{"accessToken":"x"}}'
        with mock.patch.object(m.backend, "AccountSession", return_value=fake):
            res = m._do_sync_models(db, realm="global")
        self.assertEqual(res.get("realm"), "global", "返回值要能看出同步的是哪一版")
        self.assertIn("source_account_id", res, "要能追溯用的是哪个账号")
        db.close()


class EndpointSignatureCase(unittest.TestCase):
    """端点必须暴露 realm 参数（否则前端没法表达「我要同步国际版」）。"""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.src = (Path(__file__).resolve().parent.parent
                   / "admin" / "routers" / "models.py").read_text(encoding="utf-8")

    def test_sync_endpoint_accepts_realm(self):
        import inspect
        from admin.routers.models import sync_models
        params = inspect.signature(sync_models).parameters
        self.assertIn("realm", params, "/api/models/sync 必须接受 realm")
        self.assertEqual(params["realm"].default, "cn",
                         "默认 cn 保持零回归（老前端不传参时行为不变）")


class SchedulerDualRealmCase(unittest.TestCase):
    """定时任务 `sync_models` 要两版各跑一次。"""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.src = (Path(__file__).resolve().parent.parent
                   / "admin" / "scheduler.py").read_text(encoding="utf-8")

    def test_scheduler_loops_both_realms(self):
        i = self.src.find('if task == "sync_models":')
        self.assertGreater(i, 0)
        body = self.src[i:i + 2600]
        self.assertIn('for realm in ("cn", "global")', body,
                      "定时任务应两版各同步一次（只同步一版会让另一版一直是旧数据）")
        self.assertIn("realm_results", body, "结果里要能看出每版各自的情况")

    def test_scheduler_tolerates_single_realm_failure(self):
        """单版没有账号时不该让整个任务失败（单版部署是常见形态）。"""
        i = self.src.find('if task == "sync_models":')
        body = self.src[i:i + 2600]
        self.assertIn("except Exception", body,
                      "单版异常要捕获，否则另一版也同步不了")


if __name__ == "__main__":
    unittest.main()
