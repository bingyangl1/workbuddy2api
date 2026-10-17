"""跟上上游 1.0.5 / 1.0.6 的三项修复。

## 背景：上游发了两个版本

我们在 fork 之后，上游又发了 1.0.5 与 1.0.6。审阅后判断三项值得跟进：

### ① 账号更新凭据后仍被判「登录态失效」（1.0.5）

账号因上游 401/403 被判失效后，`session_dead_fails` 累积、`status` 可能被
置为 `disabled`、`cool_kind='session_dead'`。用户重新登录拿新凭据再导入 ——
不清状态的话账号会**继续被判失效**：

* 导入明明成功，界面仍显示「登录态失效」
* 调度/选号继续跳过它
* 用户只能删号重加（丢掉历史用量与分组归属）

这个 bug 特别隐蔽：凭据是真的换好了，只是状态没跟着复位。

### ② 6008 的恢复时点应是「次日零点」（1.0.5）

我们原来的统一策略是「官方给时间就用，否则保守 24h」。
但官方客户端说明：6008（每日**请求数**上限）在**次日零点**重置。

用 24h 会让账号白白多等几小时：20:00 触发的账号，次日 00:00 就该恢复，
24h 会拖到再下一天 20:00。

（6004 是每日 **token** 上限，没有官方时点，继续保守 24h —— 两者不同。）

### ③ 147 条上游错误码的中文解释（1.0.5）

上游返回的是业务 code + 英文 message：

    HTTP 429  {"code": 6004, "msg": "CraftRateTPDLimit"}

用户既看不懂也不知道该怎么办。官方客户端对每个 code 都有明确解释，
直接复用最准确。

## 关于 1.0.6（Android 流式心跳 + 取消清理）

**未跟进** —— 它改的是 `admin/mobile_stream.py`，而该模块我们**没有**
（安卓打包尚未做）。等做安卓时再一并跟进。
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ADMIN_JWT_SECRET", "x" * 40)

ROOT = Path(__file__).resolve().parent.parent


class CredentialResetCase(unittest.TestCase):
    """① 凭据更新后清「登录态失效」状态。"""

    def _account(self, **kw):
        from admin.models import Account
        a = Account()
        a.id = 1
        a.uid = "u1"
        a.status = "active"
        a.auth_json = '{"auth":{"accessToken":"OLD"}}'
        a.session_dead_fails = 5
        a.consecutive_fails = 3
        a.cool_kind = ""
        a.cool_until = None
        a.last_err_msg = "HTTP 401"
        a.last_err_at = datetime.utcnow()
        for k, v in kw.items():
            setattr(a, k, v)
        return a

    def test_reset_helper_exists(self):
        from admin.routers.accounts import _reset_after_credential_update
        self.assertTrue(callable(_reset_after_credential_update))

    def test_new_credential_clears_session_dead(self):
        """**核心**：换凭据后 session_dead_fails 必须归零。"""
        from admin.routers.accounts import _reset_after_credential_update
        a = self._account()
        changed = _reset_after_credential_update(
            a, '{"auth":{"accessToken":"NEW"}}')
        self.assertTrue(changed)
        self.assertEqual(a.session_dead_fails, 0,
                         "**换凭据后必须清零 session_dead_fails** —— 否则"
                         "账号会继续被判「登录态失效」")
        self.assertEqual(a.last_err_msg, "", "旧错误信息应清掉")

    def test_new_credential_clears_session_dead_cooldown(self):
        from admin.routers.accounts import _reset_after_credential_update
        a = self._account(cool_kind="session_dead",
                          cool_until=datetime.utcnow() + timedelta(hours=1))
        _reset_after_credential_update(a, '{"auth":{"accessToken":"NEW"}}')
        self.assertEqual(a.cool_kind, "")
        self.assertIsNone(a.cool_until)

    def test_auto_disabled_account_reactivated(self):
        """因 session_dead 被**自动**停用的账号，换凭据后应恢复 active。"""
        from admin.routers.accounts import _reset_after_credential_update
        a = self._account(status="disabled", cool_kind="session_dead")
        _reset_after_credential_update(a, '{"auth":{"accessToken":"NEW"}}')
        self.assertEqual(a.status, "active",
                         "session_dead 导致的停用应自动恢复")

    def test_manually_disabled_account_stays_disabled(self):
        """**用户手动停用的不能被自动启用** —— 那是人的决定。

        区分方式：自动停用一定伴随 `cool_kind='session_dead'`，
        手动停用不会设这个字段。
        """
        from admin.routers.accounts import _reset_after_credential_update
        a = self._account(status="disabled", cool_kind="")
        _reset_after_credential_update(a, '{"auth":{"accessToken":"NEW"}}')
        self.assertEqual(a.status, "disabled",
                         "**手动停用的账号不该因换凭据被自动启用**")

    def test_same_credential_does_not_touch_state(self):
        """凭据没变时不该动任何状态（避免「重传同一份」把冷却清掉）。"""
        from admin.routers.accounts import _reset_after_credential_update
        a = self._account()
        changed = _reset_after_credential_update(a, a.auth_json)
        self.assertFalse(changed, "凭据相同应返回 False")
        self.assertEqual(a.session_dead_fails, 5, "状态不该被清")

    def test_other_cooldown_reason_preserved(self):
        """别的冷却原因（如积分用完）与登录态无关，不能一起清掉。"""
        from admin.routers.accounts import _reset_after_credential_update
        until = datetime.utcnow() + timedelta(hours=5)
        a = self._account(cool_kind="hard_credit", cool_until=until)
        _reset_after_credential_update(a, '{"auth":{"accessToken":"NEW"}}')
        self.assertEqual(a.cool_kind, "hard_credit",
                         "非 session_dead 的冷却原因应保留")
        self.assertEqual(a.cool_until, until)

    def test_consecutive_fails_preserved(self):
        """`consecutive_fails` 反映整体健康度，与凭据无关，不清。"""
        from admin.routers.accounts import _reset_after_credential_update
        a = self._account()
        _reset_after_credential_update(a, '{"auth":{"accessToken":"NEW"}}')
        self.assertEqual(a.consecutive_fails, 3)

    def test_wired_into_import_paths(self):
        """必须真的接到两条导入路径上（不是只定义了函数）。"""
        src = (ROOT / "admin" / "routers" / "accounts.py").read_text(encoding="utf-8")
        # 单条上传的覆盖分支 + 批量导入的覆盖分支
        self.assertGreaterEqual(
            src.count("_reset_after_credential_update("), 3,
            "应至少有 1 处定义 + 2 处调用（单条上传 / 批量导入）")


class NextCnMidnightCase(unittest.TestCase):
    """② 6008 的次日零点。"""

    def test_exists(self):
        from admin.model_limits import next_cn_midnight
        self.assertTrue(callable(next_cn_midnight))

    def test_returns_next_cn_midnight(self):
        """东八区 20:00 → 东八区次日 00:00（= UTC 当日 16:00）。"""
        from admin.model_limits import next_cn_midnight
        # UTC 12:00 = 东八区 20:00
        got = next_cn_midnight(datetime(2026, 10, 17, 12, 0, 0))
        self.assertEqual(got, datetime(2026, 10, 17, 16, 0, 0),
                         f"应为 UTC 2026-10-17 16:00，实际 {got}")

    def test_early_morning(self):
        """东八区 01:00（UTC 前一日 17:00）→ 次日零点。"""
        from admin.model_limits import next_cn_midnight
        got = next_cn_midnight(datetime(2026, 10, 16, 17, 0, 0))  # = 10-17 01:00
        self.assertEqual(got, datetime(2026, 10, 17, 16, 0, 0))

    def test_right_before_midnight(self):
        """东八区 23:59 → 几分钟后就是次日零点。"""
        from admin.model_limits import next_cn_midnight
        got = next_cn_midnight(datetime(2026, 10, 17, 15, 59, 0))  # = 23:59
        self.assertEqual(got, datetime(2026, 10, 17, 16, 0, 0))

    def test_aware_input_handled(self):
        """带时区的输入也要正确（上游实现只接受 naive，我们做了兼容）。"""
        from admin.model_limits import next_cn_midnight
        aware = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(next_cn_midnight(aware), datetime(2026, 10, 17, 16, 0, 0))

    def test_result_is_naive_utc(self):
        """返回值必须是 naive UTC（库里就是这样存的）。"""
        from admin.model_limits import next_cn_midnight
        got = next_cn_midnight(datetime(2026, 10, 17, 12, 0, 0))
        self.assertIsNone(got.tzinfo, "返回值必须是 naive（与库口径一致）")

    def test_actually_wired_into_6008_cooldown(self):
        """**函数写好了还得真接上** —— 这是被反向验证抓出的假绿。

        早先只测了 `next_cn_midnight()` 本身能算对，但把它从
        `proxy.py` 的 6008 分支里删掉，测试照样通过。
        典型「只测纯函数、没测调用」的假绿形态。
        """
        src = (ROOT / "admin" / "routers" / "proxy.py").read_text(encoding="utf-8")
        self.assertIn("next_cn_midnight", src,
                      "**必须在 proxy.py 里真的调用 next_cn_midnight**")
        # 必须出现在 6008 的分支里，而不是随便是哪
        i = src.find('_json_code(msg) == "6008"')
        self.assertGreater(i, 0,
                           "应有一个专门判断 6008 的分支")
        seg = src[i:i + 400]
        self.assertIn("next_cn_midnight", seg,
                      "6008 分支里应调用 next_cn_midnight")

    def test_6004_still_uses_fallback(self):
        """6004（每日 token）没有官方时点，应继续走保守兜底，不能也用零点。"""
        src = (ROOT / "admin" / "routers" / "proxy.py").read_text(encoding="utf-8")
        i = src.find('_json_code(msg) == "6008"')
        seg = src[max(0, i - 600):i]
        # 6014 分支只在 6008 时替换 reset_at，其它码保持原样
        self.assertIn("_MODEL_RATE_CODES", src)
        self.assertIn("fallback_seconds", src[i:i + 900])


class UpstreamErrorsCase(unittest.TestCase):
    """③ 147 条错误码解释。"""

    def test_data_file_exists(self):
        p = ROOT / "admin" / "data" / "upstream_error_messages.json"
        self.assertTrue(p.is_file(), "缺少错误码数据文件")

    def test_table_has_entries(self):
        from admin import upstream_errors as ue
        cov = ue.coverage()
        self.assertGreaterEqual(cov["total"], 140,
                                f"错误码表条目过少：{cov}")

    def test_known_code_described(self):
        from admin import upstream_errors as ue
        for code, expect in (
            ("6004", "上限"),
            ("6008", "次数"),
            ("6000", "频繁"),
        ):
            got = ue.describe(code)
            self.assertTrue(got["known"], f"{code} 应被收录")
            self.assertIn(expect, got["message"], f"{code} 的解释应含「{expect}」")

    def test_unknown_code_returns_empty_not_fabricated(self):
        """**未知码必须返回空，不能编造解释**。"""
        from admin import upstream_errors as ue
        for bad in ("999999", "", None, "abc"):
            got = ue.describe(bad)
            self.assertFalse(got["known"])
            self.assertEqual(got["message"], "",
                             f"未知码 {bad!r} 不该编造解释")

    def test_message_of_fallback(self):
        from admin import upstream_errors as ue
        self.assertEqual(ue.message_of("6008"), "今日次数已用完，明日恢复")
        self.assertEqual(ue.message_of("999999", "兜底"), "兜底")

    def test_all_entries_have_message(self):
        """每条都必须有非空 message（描述可以没有）。"""
        from admin import upstream_errors as ue
        missing = [c for c in ue.known_codes()
                   if not ue.describe(c)["message"].strip()]
        self.assertEqual(missing, [], f"这些 code 缺文案：{missing[:5]}")

    def test_broken_file_does_not_crash(self):
        """数据文件坏掉不能让服务起不来（解释只是锦上添花）。"""
        from unittest import mock

        from admin import upstream_errors as ue
        ue._table.cache_clear()
        with mock.patch.object(ue, "_DATA", Path("__no_such_file__.json")):
            got = ue.describe("6008")
            self.assertFalse(got["known"], "读不到数据时应返回未知，而不是抛异常")
        ue._table.cache_clear()


class ExhaustionMessageCase(unittest.TestCase):
    """错误码解释要真的出现在给用户的消息里。"""

    def _msg(self, kind, last_msg=""):
        from admin.routers.proxy import _exhaustion_message
        return _exhaustion_message(kind, last_msg, 2, 3)

    def test_appends_official_explanation(self):
        """last_msg 里带 6008 → 消息里应出现官方解释。"""
        got = self._msg("model_rate",
                        '{"code":6008,"msg":"CraftRateRPDLimit"}')
        self.assertIn("6008", got, f"应带 code：{got}")
        self.assertIn("今日次数已用完", got, f"应带官方文案：{got}")

    def test_unknown_code_adds_nothing(self):
        """未知 code 不编造任何东西。"""
        got = self._msg("model_rate", '{"code":999999,"msg":"mystery"}')
        self.assertNotIn("999999", got, f"未知码不该被带出：{got}")

    def test_no_code_keeps_original_behavior(self):
        """没有 code 时行为与原实现一致（零回归）。"""
        got = self._msg("transport", "ReadTimeout")
        self.assertIn("网络连接异常", got)
        self.assertIn("ReadTimeout", got)

    def test_never_raises_on_garbage(self):
        """任何输入都不能抛异常（这是错误路径上的代码）。"""
        for bad in ("", "{", "not json", '{"code":"abc"}', None):
            self._msg("model_rate", bad or "")   # 不抛即通过


class NotFollowing10_6Case(unittest.TestCase):
    """1.0.6 依赖的模块我们还没有 —— 记录这个已知缺口。"""

    def test_mobile_stream_absent(self):
        """`admin/mobile_stream.py` 不存在 → 1.0.6 的改动无从落地。"""
        self.assertFalse(
            (ROOT / "admin" / "mobile_stream.py").is_file(),
            "如果这个文件被加进来了，说明安卓支持已开始，"
            "应同时跟进 1.0.6 的流式心跳与取消清理")


if __name__ == "__main__":
    unittest.main()
