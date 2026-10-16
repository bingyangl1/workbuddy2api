"""converter.CredentialManager realm 化测试（P1 L90）。

- CN 凭证（domain=codebuddy.cn）：chat base=CN、路径=/v2/chat/completions、
  models 路径=/console/enterprises/personal/models、UA 品牌 WorkBuddy —— 零回归
- Global 凭证（domain=workbuddy.ai）：chat base=workbuddy.ai、路径=/console 优先、
  models 路径=/v2/...（与 chat 方向相反）、UA 品牌 WorkBuddy AI
- 逃生门：ADMIN_GLOBAL_ENABLED=0 时 global 凭证判 cn（realm_of 语义）
- 未知/非法 domain → cn
- _realm_chat_urls / _collect_raw_sse 纯函数行为
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, ".")

pytest = None
try:
    import pytest as _pytest
    pytest = _pytest
except ImportError:  # pragma: no cover
    pass

import converter
from converter import CredentialManager, _realm_chat_urls, _collect_raw_sse


def _write_info(tmp, domain="www.codebuddy.cn", realm=""):
    auth = {
        "accessToken": "tok",
        "refreshToken": "rtok",
        "expiresIn": 7200,
        "domain": domain,
    }
    if realm:
        auth["realm"] = realm
    info = {"account": {"uid": "u1", "type": "personal", "enterpriseId": ""},
            "auth": auth}
    p = Path(tmp) / "cred.info"
    p.write_text(__import__("json").dumps(info), encoding="utf-8")
    return p


class CredentialManagerRealmTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _cm(self, domain="www.codebuddy.cn", realm=""):
        return CredentialManager(_write_info(self._tmp.name, domain, realm))

    def test_cn_default_realm(self):
        cm = self._cm()
        self.assertEqual(cm.realm, "cn")

    def test_global_domain(self):
        cm = self._cm(domain="www.workbuddy.ai")
        self.assertEqual(cm.realm, "global")

    def test_global_subdomain(self):
        cm = self._cm(domain="login.workbuddy.ai")
        self.assertEqual(cm.realm, "global")

    def test_explicit_realm_wins(self):
        # auth.realm 显式声明优先于 domain（Go ResolveRealm 语义）
        cm = self._cm(domain="www.codebuddy.cn", realm="global")
        self.assertEqual(cm.realm, "global")
        cm2 = self._cm(domain="www.workbuddy.ai", realm="cn")
        self.assertEqual(cm2.realm, "cn")

    def test_unknown_domain_falls_cn(self):
        cm = self._cm(domain="example.com")
        self.assertEqual(cm.realm, "cn")

    def test_escape_hatch_global_becomes_cn(self):
        os.environ["ADMIN_GLOBAL_ENABLED"] = "0"
        try:
            from admin import realm as realm_mod
            realm_mod.invalidate()
            cm = self._cm(domain="www.workbuddy.ai")
            self.assertEqual(cm.realm, "cn")
        finally:
            del os.environ["ADMIN_GLOBAL_ENABLED"]
            try:
                from admin import realm as realm_mod
                realm_mod.invalidate()
            except Exception:  # noqa: BLE001
                pass

    def test_chat_base_per_realm(self):
        self.assertEqual(self._cm()._chat_base(), "https://copilot.tencent.com")
        self.assertEqual(self._cm(domain="www.workbuddy.ai")._chat_base(),
                         "https://www.workbuddy.ai")

    def test_chat_paths_per_realm(self):
        cn = self._cm()._chat_paths()
        self.assertEqual(cn, ["/v2/chat/completions"])
        g = self._cm(domain="www.workbuddy.ai")._chat_paths()
        self.assertEqual(g[0], "/console/chat/completions")
        self.assertIn("/v2/chat/completions", g)

    def test_models_path_per_realm(self):
        # 方向与 chat 相反（Go modelsPath REVERSED）
        self.assertEqual(self._cm()._models_path("personal"),
                         "/console/enterprises/personal/models")
        self.assertEqual(self._cm(domain="www.workbuddy.ai")._models_path("personal"),
                         "/v2/enterprises/personal/models")

    def test_headers_ua_brand(self):
        cn = self._cm()._build_headers_from(
            {"accessToken": "t", "domain": "www.codebuddy.cn"}, {"uid": "u"})
        self.assertIn("WorkBuddy", cn["User-Agent"])
        self.assertNotIn("WorkBuddy AI", cn["User-Agent"])
        g = self._cm(domain="www.workbuddy.ai")._build_headers_from(
            {"accessToken": "t", "domain": "www.workbuddy.ai"}, {"uid": "u"})
        self.assertIn("workbuddy-ai", g["User-Agent"])

    def test_realm_chat_urls(self):
        cm = self._cm()
        urls = _realm_chat_urls(cm)
        self.assertEqual(urls, ["https://copilot.tencent.com/v2/chat/completions"])
        g = self._cm(domain="www.workbuddy.ai")
        gurls = _realm_chat_urls(g)
        self.assertEqual(gurls[0], "https://www.workbuddy.ai/console/chat/completions")
        self.assertEqual(gurls[1], "https://www.workbuddy.ai/v2/chat/completions")


class CollectRawSseTests(unittest.TestCase):
    def test_aggregates_chunks(self):
        raw = b'data: {"choices":[{"delta":{"content":"hi"}}],"model":"m"}\n\n' \
              b'data: {"choices":[{"delta":{"content":" there"}}],"usage":{"total_tokens":7}}\n\n' \
              b'data: [DONE]\n\n'
        agg = _collect_raw_sse(raw)
        msg = agg["choices"][0]["message"]
        self.assertEqual(msg["content"], "hi there")
        self.assertEqual(agg["usage"]["total_tokens"], 7)
        self.assertEqual(agg["model"], "m")

    def test_tool_call_merge(self):
        raw = (b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
               b'"function":{"name":"f","arguments":"{\\"a\\":"}}]}}]}\n\n'
               b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
               b'"function":{"arguments":"1}"}}]}}],"finish_reason":"tool_calls"}\n\n'
               b'data: [DONE]\n\n')
        agg = _collect_raw_sse(raw)
        tc = agg["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(tc["id"], "c1")
        self.assertEqual(tc["function"]["name"], "f")
        self.assertEqual(tc["function"]["arguments"], '{"a":1}')
        self.assertEqual(agg["choices"][0]["finish_reason"], "tool_calls")


if __name__ == "__main__":
    unittest.main()
