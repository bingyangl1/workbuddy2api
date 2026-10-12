"""登录链路 realm 校验语义（对齐 Go cmd/login flow_test.go）。

覆盖：
- validateRealmMatch：会话 realm 与声称 realm 一致/不一致/空会话放行（Go TestValidateRealmMatch）
- _host_of：cn → workbuddy.cn / global → workbuddy.ai / 非法回落 cn（Go TestRealmConfig 语义）
- 会话 realm 归一：非法值回落 cn
- 路由层：join/start global 被拒、official/start global 走 workbuddy.ai 的 state 端点、
  official/poll 混域 400、join/send 混域 502
"""
import sys
import unittest

sys.path.insert(0, ".")
pytest = None
try:
    import pytest as _pytest
    pytest = _pytest
except ImportError:  # pragma: no cover
    pass

from admin import wb_login


class ValidateRealmMatchTests(unittest.TestCase):
    """镜像 Go TestValidateRealmMatch：state 文件 realm 与命令行 --realm 一致性。"""

    def _call(self, session_realm, claimed_realm):
        try:
            wb_login.validate_realm_match(session_realm, claimed_realm)
            return None
        except wb_login.WbLoginError as e:
            return str(e)

    def test_cn_match(self):
        self.assertIsNone(self._call("cn", "cn"))

    def test_global_match(self):
        self.assertIsNone(self._call("global", "global"))

    def test_cn_vs_global_mismatch(self):
        err = self._call("cn", "global")
        self.assertIsNotNone(err)
        self.assertIn("不一致", err)

    def test_global_vs_cn_mismatch(self):
        err = self._call("global", "cn")
        self.assertIsNotNone(err)
        self.assertIn("不一致", err)

    def test_empty_session_realm_passes(self):
        # 旧会话（无 realm）放行（Go：state 无 realm 放行）
        self.assertIsNone(self._call("", "global"))
        self.assertIsNone(self._call("", "cn"))

    def test_empty_claimed_defaults_cn(self):
        # 声称 realm 为空 → 按 cn 校验；会话 realm=cn 时放行
        self.assertIsNone(self._call("cn", ""))
        # 会话 realm=global、声称空 → 不一致（global 会话必须显式传 global）
        self.assertIsNotNone(self._call("global", ""))

    def test_mismatch_has_reason_marker(self):
        try:
            wb_login.validate_realm_match("cn", "global")
            self.fail("expected WbLoginError")
        except wb_login.WbLoginError as e:
            self.assertEqual(e.reason, "realm_mismatch")
            self.assertEqual(e.step, "realm_mismatch")


class HostOfTests(unittest.TestCase):
    """镜像 Go realmConfig：base 随 realm；非法/缺省回落 cn。"""

    def test_cn(self):
        self.assertEqual(wb_login._host_of("cn"), wb_login.HOST)

    def test_global(self):
        self.assertEqual(wb_login._host_of("global"), wb_login.GLOBAL_HOST)
        self.assertEqual(wb_login.GLOBAL_HOST, "https://www.workbuddy.ai")

    def test_invalid_falls_back_cn(self):
        for bad in ("jp", "us", "GLOBALX", "cn2"):  # 非法一律回落
            self.assertEqual(wb_login._host_of(bad), wb_login.HOST)
        # 大小写/空白归一：'GLOBAL' 合法
        self.assertEqual(wb_login._host_of("GLOBAL"), wb_login.GLOBAL_HOST)
        self.assertEqual(wb_login._host_of(" CN "), wb_login.HOST)


class SessionRealmTests(unittest.TestCase):
    """会话 realm 归一：非法值回落 cn（fail-closed）。"""

    def _realm_of(self, claimed):
        try:
            return wb_login._new_session(claimed).realm
        finally:
            pass

    def test_default_cn(self):
        self.assertEqual(wb_login._new_session().realm, "cn")

    def test_global(self):
        self.assertEqual(wb_login._new_session("global").realm, "global")

    def test_normalized(self):
        self.assertEqual(wb_login._new_session(" GLOBAL ").realm, "global")
        self.assertEqual(wb_login._new_session(" CN ").realm, "cn")

    def test_invalid_falls_back_cn(self):
        for bad in ("jp", "", None, "us", "cn2"):
            self.assertEqual(wb_login._new_session(bad).realm, "cn")


class LoginRouterRealmTests(unittest.TestCase):
    """路由层 realm 分派（mock wb_login 上游函数，验证调用参数与校验顺序）。"""

    def setUp(self):
        import admin.routers.login as login_router
        self.router = login_router
        self._orig = {}

    def _patch(self, name, fn):
        self._orig[name] = getattr(self.router.wb_login, name, None)
        setattr(self.router.wb_login, name, fn)

    def tearDown(self):
        for k, v in self._orig.items():
            if v is None:
                delattr(self.router.wb_login, k)
            else:
                setattr(self.router.wb_login, k, v)

    def test_join_start_rejects_global(self):
        """短信流不支持 global：join/start 传 realm=global → 400。"""
        from fastapi import HTTPException

        called = {"n": 0}

        def fake_start_login():
            called["n"] += 1
            raise AssertionError("不应发起上游登录")

        self._patch("start_login", fake_start_login)
        try:
            self.router.join_start(self.router.JoinIn(realm="global"),
                                   request=None)
            self.fail("expected HTTPException")
        except HTTPException as e:
            self.assertEqual(e.status_code, 400)
            self.assertEqual(e.detail.get("reason"), "realm_unsupported")
        self.assertEqual(called["n"], 0)

    def test_join_start_cn_ok(self):
        self._patch("start_login",
                    lambda: type("S", (), {"sid": "s1", "plugin_auth_url": "u",
                                           "realm": "cn"})())
        out = self.router.join_start(self.router.JoinIn(realm=""),
                                     request=None)
        self.assertEqual(out["sid"], "s1")
        self.assertEqual(out["realm"], "cn")

    def test_official_start_global_uses_global_host(self):
        """official/start realm=global → start_official_login('global')。"""
        calls = []

        def fake_start_official(realm="cn"):
            calls.append(realm)
            return type("S", (), {"sid": "s2", "plugin_auth_url": "u",
                                  "realm": realm})()

        self._patch("start_official_login", fake_start_official)
        out = self.router.join_official_start(
            self.router.OfficialIn(realm="global"), request=None)
        self.assertEqual(calls, ["global"])
        self.assertEqual(out["realm"], "global")

    def test_official_start_invalid_realm_400(self):
        from fastapi import HTTPException

        try:
            self.router.join_official_start(
                self.router.OfficialIn(realm="jp"), request=None)
            self.fail("expected HTTPException")
        except HTTPException as e:
            self.assertEqual(e.status_code, 400)

    def test_official_poll_realm_mismatch_rejected_before_upstream(self):
        """镜像 Go TestPollRealmMismatch：混域在打任何上游请求前被拒。"""
        from fastapi import HTTPException

        class FakeSess:
            realm = "cn"
            plugin_state = "st"

        def fake_get_session(sid):
            return FakeSess()

        def fake_fetch(state, realm="cn"):
            raise AssertionError("混域时不应发起轮询请求")

        self._patch("get_session", fake_get_session)
        self._patch("fetch_credential_by_state", fake_fetch)
        try:
            self.router.join_official_poll(
                self.router.OfficialIn(sid="s", realm="global"),
                request=None, db=None)
            self.fail("expected HTTPException")
        except HTTPException as e:
            self.assertEqual(e.status_code, 400)
            self.assertEqual(e.detail.get("reason"), "realm_mismatch")

    def test_official_poll_realm_match_proceeds(self):
        class FakeSess:
            realm = "global"
            plugin_state = "st"
            lock = __import__("threading").RLock()
            result_cache = None

        fetch_calls = []

        def fake_get_session(sid):
            return FakeSess()

        def fake_fetch(state, realm="cn"):
            fetch_calls.append((state, realm))
            return None  # 还没登录完 → done=false

        self._patch("get_session", fake_get_session)
        self._patch("fetch_credential_by_state", fake_fetch)
        out = self.router.join_official_poll(
            self.router.OfficialIn(sid="s", realm="global"),
            request=None, db=None)
        self.assertEqual(out, {"done": False})
        self.assertEqual(fetch_calls, [("st", "global")])

    def test_join_send_realm_mismatch_502(self):
        from fastapi import HTTPException

        class FakeSess:
            realm = "cn"

        def fake_get_session(sid):
            return FakeSess()

        self._patch("get_session", fake_get_session)
        try:
            self.router.join_send(
                self.router.JoinIn(sid="s", mobile="13800000000",
                                   realm="global"), request=None)
            self.fail("expected HTTPException")
        except HTTPException as e:
            self.assertEqual(e.status_code, 502)
            self.assertEqual(e.detail.get("reason"), "realm_mismatch")


if __name__ == "__main__":
    unittest.main()
