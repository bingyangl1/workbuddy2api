"""AccountSession 端点分派单测（mock httpx，零网络）。

验证 realm 化后 backend 的 URL/头形状：
- CN 账号：与改造前完全一致（chat base copilot / billing base codebuddy / /v2 路径）
- Global 账号：workbuddy.ai 双基址、/console 首路径、无前缀 billing 优先

Go 语义源：workbuddy-manager/workbuddy2api/internal/upstream/client.go
（ChatStream 路径循环 / billingMeterPaths / modelsPath）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("admin.realm")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from admin.backend import AccountSession  # noqa: E402


def _fake_info(domain: str, realm_field: str | None = None) -> str:
    """构造最小可用的 .info 凭证（占位 token，不发真请求）。"""
    auth = {
        "domain": domain,
        "accessToken": "PLACEHOLDER",
        "refreshToken": "PLACEHOLDER",
        "expiresAt": 4102444800000,  # 2100 年，永不触发刷新
        "scope": "openid",
        "sessionState": "s",
        "tokenType": "Bearer",
    }
    if realm_field:
        auth["realm"] = realm_field
    acct = {"uid": "test-uid", "nickname": "t", "enterpriseId": "", "type": "personal"}
    return json.dumps({"account": acct, "accounts": [acct], "auth": auth})


class _FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"code": 0, "data": {}}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class EndpointDispatchTests(unittest.TestCase):
    """AccountSession 端点分派：URL 形状断言（mock httpx 收发）。"""

    def _sess(self, domain="www.codebuddy.cn", realm_field=None):
        return AccountSession(_fake_info(domain, realm_field))

    def _capture(self, sess: AccountSession, method: str, *args, **kwargs):
        """跑一次方法，捕获它发出的所有请求 URL。"""
        captured = []

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, url, headers=None, **k):
                captured.append(("GET", url, headers or {}))
                return _FakeResp()

            def post(self, url, headers=None, **k):
                captured.append(("POST", url, headers or {}))
                return _FakeResp()

            def stream(self, m, url, headers=None, **k):
                captured.append(("STREAM", url, headers or {}))
                ctx = self

                class _Ctx:
                    def __enter__(self):
                        return self

                    def __exit__(self, *exc):
                        return False

                    @property
                    def status_code(self):
                        return 200

                    def iter_lines(self):
                        return iter([])

                return _Ctx()

        with mock.patch("admin.backend.httpx.Client", FakeClient):
            sess.cleanup = lambda: None
            try:
                method(*args, **kwargs)
            except Exception:
                pass
        return captured

    def test_cn_chat_completions_url(self):
        sess = self._sess()
        self.assertEqual(
            sess._chat_completions_url(),
            "https://copilot.tencent.com/v2/chat/completions")

    def test_global_chat_completions_url_console_first(self):
        sess = self._sess("www.workbuddy.ai")
        self.assertEqual(
            sess._chat_completions_url(),
            "https://www.workbuddy.ai/console/chat/completions")

    def test_cn_billing_origin(self):
        sess = self._sess()
        h = sess._billing_headers()
        self.assertEqual(h["Origin"], "https://www.codebuddy.cn")

    def test_global_billing_origin(self):
        sess = self._sess("www.workbuddy.ai")
        h = sess._billing_headers()
        self.assertEqual(h["Origin"], "https://www.workbuddy.ai")
        self.assertIn("Bearer PLACEHOLDER", h["Authorization"])

    def test_realm_property(self):
        self.assertEqual(self._sess().realm, "cn")
        self.assertEqual(self._sess("www.workbuddy.ai").realm, "global")
        self.assertEqual(self._sess("app.workbuddy.ai").realm, "global")
        # 显式 realm 字段优先
        self.assertEqual(self._sess("www.workbuddy.ai", "cn").realm, "cn")

    def test_chat_json_uses_realm_base(self):
        cn = self._capture(self._sess(), lambda: None)
        # 用 fetch_experts 走 _chat_json：CN 基址 copilot
        sess = self._sess()
        cap = self._capture(sess, sess.fetch_experts)
        urls = [u for _, u, _ in cap]
        self.assertTrue(any(u.startswith("https://copilot.tencent.com/") for u in urls), urls)

    def test_global_chat_json_uses_workbuddy_base(self):
        sess = self._sess("www.workbuddy.ai")
        cap = self._capture(sess, sess.fetch_experts)
        urls = [u for _, u, _ in cap]
        self.assertTrue(any(u.startswith("https://www.workbuddy.ai/") for u in urls), urls)

    def test_billing_meter_global_no_v2_first(self):
        """Global billing/meter：无 /v2 前缀优先（Go billingMeterPaths 语义）。"""
        urls = []

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def post(self, url, headers=None, **k):
                urls.append(url)
                return _FakeResp()

        sess = self._sess("www.workbuddy.ai")
        with mock.patch("admin.backend.httpx.Client", FakeClient):
            sess._billing_meter({}, kind="user-resource")
        self.assertEqual(urls[0],
                         "https://www.workbuddy.ai/billing/meter/get-user-resource")

    def test_billing_meter_cn_v2(self):
        urls = []

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def post(self, url, headers=None, **k):
                urls.append(url)
                return _FakeResp()

        sess = self._sess()
        with mock.patch("admin.backend.httpx.Client", FakeClient):
            sess._billing_meter({}, kind="user-resource")
        self.assertEqual(urls[0],
                         "https://www.codebuddy.cn/v2/billing/meter/get-user-resource")

    def test_billing_meter_fallback_on_404(self):
        """Global 无前缀 404 → 回落 /v2（方向回落的唯一合法触发）。"""
        urls = []
        calls = {"n": 0}

        class FakeResp404(_FakeResp):
            def __init__(self):
                super().__init__(status=404, payload={"code": 404, "msg": "nf", "data": {}})

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def post(self, url, headers=None, **k):
                urls.append(url)
                calls["n"] += 1
                if calls["n"] == 1:
                    return FakeResp404()
                return _FakeResp()

        sess = self._sess("www.workbuddy.ai")
        with mock.patch("admin.backend.httpx.Client", FakeClient):
            sess._billing_meter({}, kind="user-resource")
        self.assertEqual(len(urls), 2)
        self.assertTrue(urls[1].startswith(
            "https://www.workbuddy.ai/v2/billing/meter/"), urls)

    def test_trial_not_for_cn(self):
        sess = self._sess()
        r = sess.claim_trial()
        self.assertFalse(r.get("claimed"))
        self.assertIn("国际版", r.get("msg", ""))

    def test_trial_global_no_token_shortcircuit(self):
        """token 加密/缺失时本地短路，不发上游（避免 Illegal header value）。"""
        sess = self._sess("www.workbuddy.ai")
        sess._token_str = lambda: ""
        with mock.patch("admin.backend.httpx.Client") as mc:
            r = sess.claim_trial()
        mc.assert_not_called()
        self.assertFalse(r.get("claimed"))
        self.assertIn("未就绪", r.get("msg", ""))

    def test_models_path_cn_enterprise_style(self):
        """CN fetch_models 路径照 Go modelsPath（REVERSED）：/console/enterprises/{eid}/models。"""
        urls = []

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, url, headers=None, **k):
                urls.append(url)
                return _FakeResp(payload={
                    "code": 0, "data": {"models": [{"id": "hy3"}]}})

        sess = self._sess()
        with mock.patch("admin.backend.httpx.Client", FakeClient):
            models = sess.fetch_models()
        self.assertEqual(models, [{"id": "hy3"}])
        self.assertTrue(urls[0].startswith(
            "https://copilot.tencent.com/console/enterprises/personal/models"), urls)

    def test_models_path_global_workbuddy_base(self):
        urls = []

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, url, headers=None, **k):
                urls.append(url)
                return _FakeResp(payload={
                    "code": 0, "data": {"models": [{"id": "gpt-6-astra"}]}})

        sess = self._sess("www.workbuddy.ai")
        with mock.patch("admin.backend.httpx.Client", FakeClient):
            models = sess.fetch_models()
        self.assertEqual(models, [{"id": "gpt-6-astra"}])
        self.assertTrue(urls[0].startswith(
            "https://www.workbuddy.ai/v2/enterprises/personal/models"), urls)


if __name__ == "__main__":
    unittest.main()
