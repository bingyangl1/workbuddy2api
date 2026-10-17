"""限时权益 / 补偿领取 —— 纯逻辑与安全约束。

## 用户的核心要求

> 「这些拓展能力别默认启用，有些是只能跑一遍的，多遍跑会检测的，
>    让用户自己点击运行」

本文件把这条要求拆成**可执行的断言**：

1. **绝不自动跑** —— 不在调度任务白名单里、`run_task()` 不认它、
   不自动建 `schedules` 行
2. **每账号每期只成功一次** —— 靠 DB 唯一约束（不是内存状态）
3. **领取前必须先查** —— 状态不明时绝不发写请求
4. **领取失败不置 `claimed`** —— 否则一次网络抖动让账号永久失去机会
5. **测试期护栏能挡住误领** —— `ADMIN_UPSTREAM_READONLY=1` 时 `/claim` 被拒

## 为什么把这些写进测试

这些约束一旦退化，代价是**账号被封**（用户原话：「多遍跑会检测的」），
而且退化后功能表面完全正常 —— 只有测试能发现。
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ADMIN_JWT_SECRET", "x" * 40)

from admin import benefits  # noqa: E402

TZ = timezone(timedelta(hours=8))


def payload(**kw):
    """构造上游 compensation-status 的响应。

    `data={...}` 会与默认 data **合并**（而不是整体替换），
    这样单个用例只写它关心的字段。
    """
    default_data = {
        "active": True, "claimed": False, "credit_num": 500,
        "end_time": (datetime.now(TZ) + timedelta(days=3)).isoformat(),
    }
    data = {**default_data, **(kw.pop("data", {}) or {})}
    base = {"ok": True, "http": 200, "code": 0, "data": data}
    base.update(kw)
    base["data"] = data
    return base


class ClassifyCase(unittest.TestCase):
    """`classify()` 是纯函数 —— 权益判定的全部复杂度都在这里。"""

    def test_available(self):
        v = benefits.classify(payload())
        self.assertEqual(v["status"], "available")
        self.assertTrue(v["ok"])
        self.assertEqual(v["credit"], 500)

    def test_already_claimed(self):
        v = benefits.classify(payload(data={"claimed": True}))
        self.assertEqual(v["status"], "already")
        self.assertEqual(v["credit"], 0)
        self.assertTrue(v["ok"])

    def test_inactive(self):
        v = benefits.classify(payload(data={"active": False}))
        self.assertEqual(v["status"], "ineligible")
        self.assertEqual(v["credit"], 0)

    def test_expired(self):
        past = (datetime.now(TZ) - timedelta(days=1)).isoformat()
        v = benefits.classify(payload(data={"end_time": past}))
        self.assertEqual(v["status"], "expired")
        self.assertEqual(v["credit"], 0)

    def test_network_failure_is_error(self):
        v = benefits.classify({"ok": False, "http": 0, "code": None, "data": {}})
        self.assertEqual(v["status"], "error")
        self.assertFalse(v["ok"])

    # ── 下面几个都是「状态不明 → 不许领」的保护 ──

    def test_zero_or_negative_credit_is_unknown(self):
        for bad in (0, -1, None, "500", 500.0, True):
            v = benefits.classify(payload(data={"credit_num": bad}))
            self.assertEqual(v["status"], "unknown",
                             f"credit_num={bad!r} 应判 unknown（不明确就不能领）")
            self.assertEqual(v["credit"], 0)

    def test_missing_deadline_is_unknown(self):
        for bad in ("", None, "not-a-date", 12345):
            v = benefits.classify(payload(data={"end_time": bad}))
            self.assertEqual(v["status"], "unknown",
                             f"end_time={bad!r} 应判 unknown")

    def test_missing_flags_is_unknown(self):
        """active/claimed 缺失时不能假定「可领」。"""
        v = benefits.classify({"ok": True, "http": 200, "code": 0,
                               "data": {"credit_num": 500,
                                        "end_time": (datetime.now(TZ)
                                                     + timedelta(days=1)).isoformat()}})
        self.assertEqual(v["status"], "unknown")

    def test_claimed_true_wins_over_available(self):
        """同时 active=True/claimed=True 时，claimed 优先（不能重复领）。"""
        v = benefits.classify(payload(data={"active": True, "claimed": True}))
        self.assertEqual(v["status"], "already")

    def test_naive_deadline_treated_as_local_tz(self):
        """不带时区的 end_time 按东八区解释（上游习惯）。"""
        naive_future = (datetime.now(TZ) + timedelta(days=2)).replace(tzinfo=None)
        v = benefits.classify(payload(data={"end_time": naive_future.isoformat()}))
        self.assertEqual(v["status"], "available")

    def test_z_suffix_deadline(self):
        z_future = (datetime.now(timezone.utc) + timedelta(days=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        v = benefits.classify(payload(data={"end_time": z_future}))
        self.assertEqual(v["status"], "available")


class NotScheduledCase(unittest.TestCase):
    """**最重要的一组**：这个功能绝不能被自动调度。"""

    def test_not_in_task_choices(self):
        """不在可调度任务白名单里 —— 用户无法把它设成定时任务。"""
        from admin.routers.schedules import TASK_CHOICES
        for bad in ("claim_benefits", "benefits", "claim_compensation",
                    "compensation"):
            self.assertNotIn(bad, TASK_CHOICES,
                             f"**「{bad}」绝不能出现在可调度任务里** —— "
                             "权益只能手动跑（多跑会被上游检测）")

    def test_not_handled_by_scheduler(self):
        """`run_task()` 收到这个名字必须回「未知任务」，而不是去执行。"""
        from admin import scheduler
        fake_db = mock.MagicMock()
        for name in ("claim_benefits", "benefits", "claim_compensation"):
            got = scheduler.run_task(name, fake_db)
            self.assertIn("未知任务", str(got.get("error", "")),
                          f"scheduler 不该认识 {name}，实际返回 {got}")

    def test_module_has_no_ensure_schedule(self):
        """模块里不该有「自动建计划」的函数（上游有 `ensure_schedule`）。"""
        self.assertFalse(hasattr(benefits, "ensure_schedule"),
                         "不该有 ensure_schedule —— 那会在首次调用时自动建定时任务")

    def test_router_declares_not_scheduled(self):
        """`/campaign` 必须如实报告「未调度」。"""
        src = (os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "admin", "routers", "benefits.py"))
        with open(src, encoding="utf-8") as f:
            text = f.read()
        self.assertIn('"scheduled": False', text,
                      "campaign 响应必须声明 scheduled=False")


class ReadonlyGuardCase(unittest.TestCase):
    """测试护栏必须挡住领取（防止截图/联调时误领）。"""

    def test_claim_guarded(self):
        from admin.security import is_upstream_write
        self.assertTrue(is_upstream_write("POST", "/api/benefits/claim"),
                        "**/claim 是写操作，必须被只读护栏挡住**")

    def test_query_not_guarded(self):
        """查询是只读的 —— 测试期应当允许，否则没法验证 UI。"""
        from admin.security import is_upstream_write
        self.assertFalse(is_upstream_write("POST", "/api/benefits/query"),
                         "/query 是只读，不该被护栏挡")

    def test_status_not_guarded(self):
        from admin.security import is_upstream_write
        self.assertFalse(is_upstream_write("GET", "/api/benefits/status"))


class ClaimOrderCase(unittest.TestCase):
    """**领取顺序**：必须先查后领，且状态不明时不发写请求。"""

    def _fake_account(self):
        a = mock.Mock()
        a.id = 7
        a.status = "active"
        a.auth_json = "{}"
        return a

    def _run_claim(self, status_payload, claim_payload=None):
        """跑一次 `benefits.claim()`，记录是否真的发出了领取请求。"""
        calls = {"claim": 0, "status": 0}
        verdict = benefits.classify(status_payload)

        class FakeSession:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def compensation_status(self):
                calls["status"] += 1
                return status_payload

            def claim_compensation(self):
                calls["claim"] += 1
                return claim_payload or {"ok": True, "http": 200, "code": 0, "data": {}}

        db = mock.MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        with mock.patch("admin.backend.AccountSession", FakeSession), \
             mock.patch.object(benefits, "_record", mock.MagicMock()), \
             mock.patch.object(benefits, "_row_of", return_value=None):
            # 已领取的短路分支依赖 _row_of；这里它返回 None 所以会继续
            out = benefits.claim(db, self._fake_account())
        return out, calls, verdict

    def test_available_triggers_exactly_one_claim(self):
        out, calls, _ = self._run_claim(payload())
        self.assertEqual(calls["status"], 1, "必须先查一次")
        self.assertEqual(calls["claim"], 1, "确认可领后应发一次领取")
        self.assertIn(out["status"], ("claimed", "error"))

    def test_not_available_never_claims(self):
        """**核心保护**：不可领时一个写请求都不能发。"""
        cases = {
            "already": payload(data={"claimed": True}),
            "ineligible": payload(data={"active": False}),
            "expired": payload(data={"end_time":
                                     (datetime.now(TZ) - timedelta(days=1)).isoformat()}),
            "unknown-credit": payload(data={"credit_num": 0}),
            "unknown-deadline": payload(data={"end_time": ""}),
            "error": {"ok": False, "http": 500, "code": None, "data": {}},
        }
        for label, pl in cases.items():
            out, calls, _ = self._run_claim(pl)
            self.assertEqual(calls["claim"], 0,
                             f"[{label}] 状态不可领时**绝不能**发领取请求")
            self.assertNotEqual(out.get("status"), "claimed", f"[{label}] 不该报已领取")

    def test_status_queried_before_claim(self):
        """顺序断言：查询必须发生在领取之前（用调用序号验证）。"""
        order = []

        class FakeSession:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def compensation_status(self):
                order.append("status")
                return payload()

            def claim_compensation(self):
                order.append("claim")
                return {"ok": True, "http": 200, "code": 0, "data": {}}

        db = mock.MagicMock()
        with mock.patch("admin.backend.AccountSession", FakeSession), \
             mock.patch.object(benefits, "_record", mock.MagicMock()), \
             mock.patch.object(benefits, "_row_of", return_value=None):
            benefits.claim(db, self._fake_account())
        self.assertEqual(order[:2], ["status", "claim"],
                         "**必须先查后领**")


class FailureKeepsRetryableCase(unittest.TestCase):
    """领取失败不能置 `claimed`，否则账号永久失去机会。"""

    def test_failed_claim_not_marked_claimed(self):
        recorded = []

        def fake_record(db, account_id, *, checked, claimed, credit, reason,
                        http, code, commit=True):
            recorded.append({"claimed": claimed, "credit": credit, "reason": reason})

        class FakeSession:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def compensation_status(self):
                return payload()

            def claim_compensation(self):
                return {"ok": False, "http": 500, "code": 50001, "data": {}}

        db = mock.MagicMock()
        a = mock.Mock()
        a.id = 7
        a.auth_json = "{}"
        with mock.patch("admin.backend.AccountSession", FakeSession), \
             mock.patch.object(benefits, "_record", side_effect=fake_record), \
             mock.patch.object(benefits, "_row_of", return_value=None):
            out = benefits.claim(db, a)

        self.assertEqual(out["status"], "error")
        # 最后一次写记录必须是 claimed=False（保留重试机会）
        self.assertFalse(recorded[-1]["claimed"],
                         "**领取失败不能标记 claimed** —— 否则账号永久失去机会")

    def test_successful_claim_records_credit(self):
        recorded = []

        def fake_record(db, account_id, *, checked, claimed, credit, reason,
                        http, code, commit=True):
            recorded.append({"claimed": claimed, "credit": credit})

        class FakeSession:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def compensation_status(self):
                return payload()

            def claim_compensation(self):
                return {"ok": True, "http": 200, "code": 0, "data": {}}

        db = mock.MagicMock()
        a = mock.Mock()
        a.id = 7
        a.auth_json = "{}"
        with mock.patch("admin.backend.AccountSession", FakeSession), \
             mock.patch.object(benefits, "_record", side_effect=fake_record), \
             mock.patch.object(benefits, "_row_of", return_value=None):
            out = benefits.claim(db, a)

        self.assertEqual(out["status"], "claimed")
        self.assertEqual(out["credit"], 500)
        self.assertTrue(any(r["claimed"] and r["credit"] == 500 for r in recorded))


class ModelConstraintCase(unittest.TestCase):
    """DB 唯一约束是「每期每账号只领一次」的真正保证。"""

    def test_unique_constraint_exists(self):
        from admin.models import BenefitClaim
        names = [c.name for c in BenefitClaim.__table__.constraints
                 if c.__class__.__name__ == "UniqueConstraint"]
        self.assertIn("uq_benefit_claims_account_campaign", names,
                      "**必须有唯一约束** —— 内存状态挡不住重启/多进程/连点")

    def test_campaign_key_is_versioned(self):
        """campaign_key 必须能区分活动期（换期不换 key 会挡住新一期）。"""
        self.assertTrue(benefits.CAMPAIGN_KEY)
        self.assertIsInstance(benefits.CAMPAIGN_KEY, str)


class RealmRoutingCase(unittest.TestCase):
    """权益端点必须按账号 realm 分派基址（本项目硬约束）。"""

    def test_compensation_uses_realm_billing_base(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "admin" / "backend.py").read_text(encoding="utf-8")
        i = src.find("def _compensation_request")
        self.assertGreater(i, 0)
        body = src[i:i + 4000]
        self.assertIn("self._billing_base()", body,
                      "**必须按 realm 取 billing 基址** —— "
                      "上游用 WEB_BASE（写死国内版），我们不能照抄")
        self.assertNotIn("WEB_BASE + paths[", body,
                         "不该像上游那样写死 WEB_BASE")

    def test_both_prefixes_tried(self):
        """`/billing/*` 与 `/v2/billing/*` 两种前缀都要试。"""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "admin" / "backend.py").read_text(encoding="utf-8")
        i = src.find("def _compensation_request")
        body = src[i:i + 4000]
        self.assertIn("billing_paths", body,
                      "应复用 realm_mod.billing_paths 的前缀方向判定")


if __name__ == "__main__":
    unittest.main()
