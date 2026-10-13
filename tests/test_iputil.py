"""iputil 信任代理模型单测（Phase 2 T1，移植自 manager server/iputil.py）。

不依赖真实 DB / 网络。验证：
1. `_clean_ip`：IPv4:port / [::1]:1234 / 纯 IPv6 / 空串；
2. `ip_matches`：单 IP / CIDR / 带空格脏值（存量库兼容）/ 非法输入一律 False；
3. `evaluate`：whitelist / blacklist 各态 + deny 优先于 allow；
4. `client_ip`：TRUST_PROXY=0 / 对端不可信（伪造头无效，本次移植要钉死的
   安全行为）/ 对端可信 + X-Real-IP / XFF 按 HOPS 从右往左数 / 无头回退 socket；
5. `trusted_ip_from_headers`：direct_ip 可信 / 不可信两态。

mock 策略：Request 用 MagicMock 构造 headers dict + client.host；
配置项用 mock.patch.object(settings, 'TRUST_PROXY', ...) 直接改铺轨配置。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from admin import iputil  # noqa: E402
from admin.config import settings  # noqa: E402

#: 默认可信网段（与 admin/config.py 铺轨一致）：回环 + 常见私网。
_DEFAULT_CIDRS = ["127.0.0.0/8", "::1/128", "10.0.0.0/8",
                  "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"]


def _request(host: str, headers: dict | None = None) -> mock.MagicMock:
    """构造最小 Request 替身：client.host + headers dict（大小写不敏感由真实
    Starlette 保证，这里直接用 dict 模拟 get 语义）。"""
    req = mock.MagicMock()
    req.client = mock.MagicMock()
    req.client.host = host
    hdrs = dict(headers or {})
    req.headers = mock.MagicMock()
    req.headers.get = lambda key, default='': hdrs.get(key.lower(), default)
    return req


class CleanIpTests(unittest.TestCase):
    """_clean_ip：端口与方括号剥离。"""

    def test_ipv4_with_port(self):
        self.assertEqual(iputil._clean_ip("1.2.3.4:5678"), "1.2.3.4")

    def test_ipv6_bracketed_with_port(self):
        self.assertEqual(iputil._clean_ip("[::1]:1234"), "::1")

    def test_pure_ipv6_untouched(self):
        # 纯 IPv6 冒号数 != 1，不会被误当 IPv4:port 处理
        self.assertEqual(iputil._clean_ip("2001:db8::1"), "2001:db8::1")

    def test_empty(self):
        self.assertEqual(iputil._clean_ip(""), "")
        self.assertEqual(iputil._clean_ip(None), "")


class IpMatchesTests(unittest.TestCase):
    """ip_matches：单 IP / CIDR / 脏值 / 非法输入。"""

    def test_single_ip_match(self):
        self.assertTrue(iputil.ip_matches("9.9.9.9", "9.9.9.9"))

    def test_cidr_match(self):
        self.assertTrue(iputil.ip_matches("10.1.2.3", "10.0.0.0/8"))
        self.assertFalse(iputil.ip_matches("11.1.2.3", "10.0.0.0/8"))

    def test_dirty_cidr_with_spaces(self):
        # 存量库兼容：`ip_network(' 10.0.0.0/8')` 会解析失败 → 那条规则
        # 永远失配 → 白名单里只要有一条就把所有来源拒了。必须 strip 后仍能匹配。
        self.assertTrue(iputil.ip_matches("10.1.2.3", " 10.0.0.0/8"))

    def test_invalid_cidr_false(self):
        self.assertFalse(iputil.ip_matches("10.1.2.3", "not-a-cidr"))

    def test_invalid_ip_false(self):
        self.assertFalse(iputil.ip_matches("not-an-ip", "10.0.0.0/8"))


class EvaluateTests(unittest.TestCase):
    """evaluate：whitelist / blacklist / deny 优先。"""

    def test_whitelist_hit(self):
        rules = [{"kind": "allow", "cidr": "10.0.0.0/8"}]
        self.assertTrue(iputil.evaluate("10.1.2.3", rules, "whitelist"))

    def test_whitelist_miss(self):
        rules = [{"kind": "allow", "cidr": "10.0.0.0/8"}]
        self.assertFalse(iputil.evaluate("11.1.2.3", rules, "whitelist"))

    def test_whitelist_empty_rules(self):
        self.assertFalse(iputil.evaluate("1.2.3.4", [], "whitelist"))

    def test_blacklist_hit(self):
        rules = [{"kind": "deny", "cidr": "9.9.9.9"}]
        self.assertFalse(iputil.evaluate("9.9.9.9", rules, "blacklist"))

    def test_blacklist_miss(self):
        rules = [{"kind": "deny", "cidr": "9.9.9.9"}]
        self.assertTrue(iputil.evaluate("1.2.3.4", rules, "blacklist"))

    def test_blacklist_empty_rules(self):
        self.assertTrue(iputil.evaluate("1.2.3.4", [], "blacklist"))

    def test_deny_overrides_allow(self):
        # 同一 IP 同时命中 allow 与 deny → 拒绝（deny 优先）
        rules = [{"kind": "allow", "cidr": "10.0.0.0/8"},
                 {"kind": "deny", "cidr": "10.9.9.9"}]
        self.assertFalse(iputil.evaluate("10.9.9.9", rules, "whitelist"))
        self.assertFalse(iputil.evaluate("10.9.9.9", rules, "blacklist"))

    def test_blacklist_allow_irrelevant(self):
        # blacklist 模式下 allow 不影响结果
        rules = [{"kind": "allow", "cidr": "10.0.0.0/8"}]
        self.assertTrue(iputil.evaluate("1.2.3.4", rules, "blacklist"))


class ClientIpTests(unittest.TestCase):
    """client_ip：信任代理取值链（安全判定核心）。"""

    def test_trust_proxy_off_always_socket(self):
        # a) TRUST_PROXY=0 → 永远 socket，转发头一律不看
        with mock.patch.object(settings, "TRUST_PROXY", False), \
             mock.patch.object(settings, "TRUSTED_PROXY_CIDRS", _DEFAULT_CIDRS):
            req = _request("203.0.113.7",
                           {"x-real-ip": "9.9.9.9", "x-forwarded-for": "8.8.8.8"})
            self.assertEqual(iputil.client_ip(req), "203.0.113.7")

    def test_untrusted_peer_ignores_forged_headers(self):
        # b) 对端不在可信网段（公网直连）→ 伪造 X-Real-IP 无效，回 socket。
        # 这是本次移植要钉死的安全行为（manager 实测绕过的漏洞形态）。
        with mock.patch.object(settings, "TRUST_PROXY", True), \
             mock.patch.object(settings, "TRUSTED_PROXY_CIDRS", _DEFAULT_CIDRS), \
             mock.patch.object(settings, "TRUSTED_PROXY_HOPS", 1):
            req = _request("203.0.113.7",
                           {"x-real-ip": "9.9.9.9",
                            "x-forwarded-for": "8.8.8.8, 7.7.7.7"})
            self.assertEqual(iputil.client_ip(req), "203.0.113.7")

    def test_trusted_peer_real_ip_accepted(self):
        # c) 对端 127.0.0.1（可信）+ X-Real-IP → 采信
        with mock.patch.object(settings, "TRUST_PROXY", True), \
             mock.patch.object(settings, "TRUSTED_PROXY_CIDRS", _DEFAULT_CIDRS), \
             mock.patch.object(settings, "TRUSTED_PROXY_HOPS", 1):
            req = _request("127.0.0.1", {"x-real-ip": "9.9.9.9"})
            self.assertEqual(iputil.client_ip(req), "9.9.9.9")

    def test_trusted_peer_xff_last_hop(self):
        # d) 对端可信 + XFF 多段 → 从右往左数 HOPS=1 个（末段 = 反代追加的真实对端）
        with mock.patch.object(settings, "TRUST_PROXY", True), \
             mock.patch.object(settings, "TRUSTED_PROXY_CIDRS", _DEFAULT_CIDRS), \
             mock.patch.object(settings, "TRUSTED_PROXY_HOPS", 1):
            req = _request("127.0.0.1",
                           {"x-forwarded-for": "1.1.1.1, 2.2.2.2, 3.3.3.3"})
            self.assertEqual(iputil.client_ip(req), "3.3.3.3")

    def test_trusted_peer_no_headers_fallback_socket(self):
        # e) 对端可信 + 无 X-Real-IP 无 XFF → socket
        with mock.patch.object(settings, "TRUST_PROXY", True), \
             mock.patch.object(settings, "TRUSTED_PROXY_CIDRS", _DEFAULT_CIDRS), \
             mock.patch.object(settings, "TRUSTED_PROXY_HOPS", 1):
            req = _request("127.0.0.1")
            self.assertEqual(iputil.client_ip(req), "127.0.0.1")

    def test_trusted_peer_hops_two(self):
        # f) HOPS=2 时取倒数第二个（XFF 右侧两跳都是可信代理追加的）
        with mock.patch.object(settings, "TRUST_PROXY", True), \
             mock.patch.object(settings, "TRUSTED_PROXY_CIDRS", _DEFAULT_CIDRS), \
             mock.patch.object(settings, "TRUSTED_PROXY_HOPS", 2):
            req = _request("127.0.0.1",
                           {"x-forwarded-for": "1.1.1.1, 2.2.2.2, 3.3.3.3"})
            self.assertEqual(iputil.client_ip(req), "2.2.2.2")

    def test_trusted_peer_ipv6_loopback(self):
        # ::1 也在默认可信网段内（fc00::/7 之外的 ::1/128）
        with mock.patch.object(settings, "TRUST_PROXY", True), \
             mock.patch.object(settings, "TRUSTED_PROXY_CIDRS", _DEFAULT_CIDRS), \
             mock.patch.object(settings, "TRUSTED_PROXY_HOPS", 1):
            req = _request("::1", {"x-real-ip": "9.9.9.9"})
            self.assertEqual(iputil.client_ip(req), "9.9.9.9")


class TrustedIpFromHeadersTests(unittest.TestCase):
    """trusted_ip_from_headers：无 Request 的调用方（ratelimit 收编用）。"""

    def test_direct_trusted_uses_headers(self):
        # direct_ip 可信（127.0.0.1）→ 采信 X-Real-IP
        with mock.patch.object(settings, "TRUST_PROXY", True), \
             mock.patch.object(settings, "TRUSTED_PROXY_CIDRS", _DEFAULT_CIDRS), \
             mock.patch.object(settings, "TRUSTED_PROXY_HOPS", 1):
            self.assertEqual(
                iputil.trusted_ip_from_headers("9.9.9.9", "1.1.1.1, 2.2.2.2",
                                               "127.0.0.1"),
                "9.9.9.9")
            # 无 real_ip → XFF 末跳
            self.assertEqual(
                iputil.trusted_ip_from_headers(None, "1.1.1.1, 2.2.2.2",
                                               "127.0.0.1"),
                "2.2.2.2")

    def test_direct_untrusted_ignores_headers(self):
        # direct_ip 不可信（公网）→ 伪造头一律无视，回 direct_ip
        with mock.patch.object(settings, "TRUST_PROXY", True), \
             mock.patch.object(settings, "TRUSTED_PROXY_CIDRS", _DEFAULT_CIDRS), \
             mock.patch.object(settings, "TRUSTED_PROXY_HOPS", 1):
            self.assertEqual(
                iputil.trusted_ip_from_headers("9.9.9.9", "1.1.1.1, 2.2.2.2",
                                               "203.0.113.7"),
                "203.0.113.7")

    def test_no_direct_ip_returns_unknown(self):
        with mock.patch.object(settings, "TRUST_PROXY", True), \
             mock.patch.object(settings, "TRUSTED_PROXY_CIDRS", _DEFAULT_CIDRS), \
             mock.patch.object(settings, "TRUSTED_PROXY_HOPS", 1):
            self.assertEqual(
                iputil.trusted_ip_from_headers("9.9.9.9", None, None),
                "unknown")


class RatelimitDelegationTests(unittest.TestCase):
    """ratelimit.get_trusted_client_ip 委托形态（收编后语义不变性抽查）。"""

    def test_delegates_to_iputil(self):
        from admin import ratelimit
        with mock.patch.object(settings, "TRUST_PROXY", True), \
             mock.patch.object(settings, "TRUSTED_PROXY_CIDRS", _DEFAULT_CIDRS), \
             mock.patch.object(settings, "TRUSTED_PROXY_HOPS", 1):
            # 可信直连 + real_ip → 采信
            self.assertEqual(
                ratelimit.get_trusted_client_ip("9.9.9.9", None, "127.0.0.1"),
                "9.9.9.9")
            # 不可信直连 → 无视伪造头
            self.assertEqual(
                ratelimit.get_trusted_client_ip("9.9.9.9", "1.1.1.1",
                                                "203.0.113.7"),
                "203.0.113.7")


if __name__ == "__main__":
    unittest.main()
