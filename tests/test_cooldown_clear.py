"""A2：账号冷却一键解除（`POST /api/accounts/{id}/cooldown/clear`）。

## 为什么需要（上游 `dcf640fc` 的动机）

账号进冷却通常是因为上游真返回了限流/错误 —— 那时等它自己恢复是对的。
但有几类是**误判**，干等只会白白少一个可用账号：

  * 上游临时抖动（网络闪断被计成失败）→ `cool_until` 被设了几分钟
  * 熔断阈值偏保守（连续几次 5xx 就熔断 10 分钟）
  * 降权在容量恢复后仍持续
  * 人工排查完确认号没问题，想立刻放回池子

此前界面上「账号冷却中」**没有任何出口**，只能改库或干等。

## 关键安全约束（本测试的重点）

**清**：`cool_until` / `cool_kind` / `breaker_until` / `breaker_fails` /
`degrade_until` —— 都是**本地推断的状态**，可以安全重置。

**不清**：
  * `session_dead_fails` —— 来自上游 401/403，是**凭证真的废了**的权威信号。
    一键清掉会让死号立刻被重新选中、反复 401，拖慢整个池子。
    它只能通过「重新导入有效凭证」来清（那才是真的修好了）。
  * `status` —— 停用是人工决策；解除冷却 ≠ 重新启用。
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

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
    ])
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def _mk(db, *, uid="u1", **kw):
    from admin.models import Account
    a = Account(uid=uid, realm=kw.pop("realm", "cn"), status=kw.pop("status", "active"),
                auth_json='{"auth":{"accessToken":"x"}}', **kw)
    db.add(a)
    db.commit()
    return a


class ClearCooldownCase(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("ADMIN_JWT_SECRET", "x" * 40)
        self.db = _db()
        from admin.routers.accounts import clear_account_cooldown
        self.fn = clear_account_cooldown

    def tearDown(self):
        self.db.close()

    def _call(self, acc_id):
        # request=None：审计里 request 可为空（log_request 内部已处理）
        return self.fn(acc_id, _=None, db=self.db, request=None)

    def test_clears_cool(self):
        now = datetime.utcnow()
        a = _mk(self.db, cool_until=now + timedelta(minutes=5),
                cool_kind="rate_limit")
        r = self._call(a.id)
        self.assertTrue(r["ok"])
        self.assertIn("cool", r["cleared"])
        self.db.refresh(a)
        self.assertIsNone(a.cool_until)
        self.assertEqual(a.cool_kind, "")

    def test_clears_breaker_and_resets_fail_count(self):
        now = datetime.utcnow()
        a = _mk(self.db, breaker_until=now + timedelta(minutes=10),
                breaker_fails=5)
        r = self._call(a.id)
        self.assertIn("breaker", r["cleared"])
        self.db.refresh(a)
        self.assertIsNone(a.breaker_until)
        self.assertEqual(a.breaker_fails, 0,
                         "熔断计数也要归零 —— 否则下次一次失败立刻又熔断")

    def test_clears_degrade(self):
        now = datetime.utcnow()
        a = _mk(self.db, degrade_until=now + timedelta(minutes=30))
        r = self._call(a.id)
        self.assertIn("degraded", r["cleared"])
        self.db.refresh(a)
        self.assertIsNone(a.degrade_until)

    def test_clears_all_three_at_once(self):
        now = datetime.utcnow()
        a = _mk(self.db, cool_until=now + timedelta(minutes=1), cool_kind="cooling",
                breaker_until=now + timedelta(minutes=2), breaker_fails=3,
                degrade_until=now + timedelta(minutes=3))
        r = self._call(a.id)
        self.assertEqual(sorted(r["cleared"]), ["breaker", "cool", "degraded"])
        self.db.refresh(a)
        self.assertIsNone(a.cool_until)
        self.assertIsNone(a.breaker_until)
        self.assertIsNone(a.degrade_until)

    # ── 安全约束（重点） ──
    def test_does_NOT_clear_session_dead_fails(self):
        """**核心安全约束**：凭证失效的权威计数不能一键清零。

        清掉会让死号立刻被重新选中、反复 401，把整个池子拖慢。
        它只能通过「重新导入有效凭证」来清。
        """
        a = _mk(self.db, session_dead_fails=3,
                cool_until=datetime.utcnow() + timedelta(minutes=1))
        r = self._call(a.id)
        self.db.refresh(a)
        self.assertEqual(a.session_dead_fails, 3,
                         "session_dead_fails 必须保留（那是上游 401/403 的权威信号）")
        self.assertEqual(r["kept"]["session_dead_fails"], 3)

    def test_does_NOT_change_status(self):
        """解除冷却不等于重新启用（停用是人工决策）。"""
        a = _mk(self.db, status="disabled",
                cool_until=datetime.utcnow() + timedelta(minutes=1))
        r = self._call(a.id)
        self.db.refresh(a)
        self.assertEqual(a.status, "disabled", "不该顺手把账号启用回来")
        self.assertEqual(r["kept"]["status"], "disabled")

    def test_does_NOT_touch_consecutive_fails(self):
        """连续失败计数不清 —— 它是错误分类的依据，不是本地冷却状态。"""
        a = _mk(self.db, consecutive_fails=4)
        self._call(a.id)
        self.db.refresh(a)
        self.assertEqual(a.consecutive_fails, 4)

    # ── 无状态时如实报告 ──
    def test_no_state_reports_nothing_cleared(self):
        a = _mk(self.db)
        r = self._call(a.id)
        self.assertEqual(r["cleared"], [],
                         "没有冷却状态时 cleared 应为空（如实告知，不谎报「已重置」）")
        self.assertIn("无需解除", r["message"])

    def test_missing_account_404(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self._call(999999)
        self.assertEqual(ctx.exception.status_code, 404)

    # ── 幂等 ──
    def test_second_call_is_noop(self):
        a = _mk(self.db, cool_until=datetime.utcnow() + timedelta(minutes=1))
        first = self._call(a.id)
        second = self._call(a.id)
        self.assertIn("cool", first["cleared"])
        self.assertEqual(second["cleared"], [],
                         "第二次调用应如实报告「没什么可清的」（幂等）")


class EndpointWiringCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.src = (Path(__file__).resolve().parent.parent
                   / "admin" / "routers" / "accounts.py").read_text(encoding="utf-8")

    def test_route_registered(self):
        self.assertIn('@router.post("/{acc_id}/cooldown/clear")', self.src)

    def test_requires_admin(self):
        i = self.src.find('@router.post("/{acc_id}/cooldown/clear")')
        body = self.src[i:i + 400]
        self.assertIn("require_admin", body, "必须要求管理员权限")

    def test_audited(self):
        i = self.src.find("def clear_account_cooldown")
        body = self.src[i:i + 2600]
        self.assertIn("log_request", body, "这类改状态的操作必须留审计")

    def test_docstring_warns_about_kept_fields(self):
        i = self.src.find("def clear_account_cooldown")
        body = self.src[i:i + 2600]
        self.assertIn("session_dead", body, "文档必须说明「不清哪些」及原因")


if __name__ == "__main__":
    unittest.main()
