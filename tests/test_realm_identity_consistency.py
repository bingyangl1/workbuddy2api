"""realm 一致性审计的回归测试 —— 出站身份必须**全程**按 realm 分派。

## 背景：一次全面审计发现的缺口

用户的诉求是「确认涉及国际版的是否都用国际版的客户端参数和端点」。审计发现
出站身份有**三处**没按 realm 分派（都是真缺口，不是理论问题）：

### ① 主流量路径的 4 个风控头没分 realm（最严重）

`proxy._upstream_extra_headers()` **已经接收 `account` 参数**，但
`cprofile.risk_headers()` **没传 realm** → 恒取国内版：

    X-IDE-Name    = WorkBuddy      （应为 workbuddy-ai）
    X-IDE-Type    = WorkBuddy      （应为 workbuddy-ai）
    X-IDE-Version = 5.8.3          （应为 5.8.4）
    X-Product     = WorkBuddy      （应为 SaaS）

而同一请求的 UA 已经是 `workbuddy-ai/5.8.4` → **同一请求内身份自相矛盾**。
这比「缺头」更糟：缺头是「不像官方客户端」，矛盾是「明显在伪造」。

### ② `X-Device-Token` 两版共用一份

`turing_token.get_device_token()` 没有 realm 参数，且内部用
`WB.install_dir()` / `WB.turing_channel_id()` 的**无 realm** 版本 →
两版共用国内版的设备指纹。

实测算出的 token 完全不同（CN 1046 字符 / Global 982 字符）：
两版是独立安装、独立 SDK、独立 channelId（109144 vs 400111）。

### ③ UA 兜底分支用了固定版本

`realm.chat_ua()` 在 `client_profile` 取不到值时用 `client_version()` 拼，
而那个函数**没传 realm** → 兜底时国际版 UA 变成 `workbuddy-ai/5.8.3`
（品牌段对、版本段错）—— 与主路径修好的风控特征完全一样，只是换了触发路径。

## 本测试覆盖

* 三个缺口的行为级断言（真实调用构造，不 mock 掉被测逻辑）
* 端到端：同一 realm 的 UA / 4 个风控头 / 设备指纹必须**互相一致**
* 兜底路径也不能串版本
"""
import inspect
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ADMIN_JWT_SECRET", "x" * 40)

#: 两版的「身份签名」——用于断言「同一请求内所有头必须指向同一版本」
CN_SIG = {"app": "WorkBuddy", "ver": "5.8.3", "channel": 109144}
GL_SIG = {"app": "workbuddy-ai", "ver": "5.8.4", "channel": 400111}


class RiskHeadersSignaturesCase(unittest.TestCase):
    """签名层：`risk_headers(realm)` 必须真按 realm 产出。"""

    def test_signature_has_realm(self):
        from admin import client_profile as cp
        for fn in (cp.risk_headers, cp.ua, cp.desktop_version, cp.turing_channel_id):
            self.assertIn("realm", inspect.signature(fn).parameters,
                          f"{fn.__name__} 必须接受 realm")

    def test_risk_headers_differ_by_realm(self):
        from admin import client_profile as cp
        cn = cp.risk_headers("cn")
        gl = cp.risk_headers("global")
        self.assertNotEqual(cn.get("X-IDE-Name"), gl.get("X-IDE-Name"))
        self.assertNotEqual(cn.get("X-IDE-Version"), gl.get("X-IDE-Version"))
        self.assertEqual(cn["X-IDE-Name"], CN_SIG["app"])
        self.assertEqual(gl["X-IDE-Name"], GL_SIG["app"])
        self.assertEqual(cn["X-IDE-Version"], CN_SIG["ver"])
        self.assertEqual(gl["X-IDE-Version"], GL_SIG["ver"])

    def test_product_field_should_be_saas(self):
        """`X-Product` 是部署形态（SaaS），两版相同 —— 不是产品名。

        审计时发现主流量路径曾把它设成产品名（`WorkBuddy`），这是错的。
        """
        from admin import client_profile as cp
        for r in ("cn", "global"):
            self.assertEqual(cp.risk_headers(r).get("X-Product"), "SaaS",
                             f"{r} 的 X-Product 应为部署形态 SaaS")


class ProxyExtraHeadersCase(unittest.TestCase):
    """**缺口①**：主流量路径必须按 account.realm 取风控头。"""

    def _make_request(self):
        from fastapi import Request
        scope = {
            "type": "http", "method": "POST", "path": "/v1/chat/completions",
            "headers": [(b"user-agent", b"test")], "query_string": b"",
            "client": ("1.2.3.4", 1), "server": ("t", 80), "scheme": "http",
            "http_version": "1.1",
        }
        return Request(scope)

    def _fake_account(self, realm):
        """最小账号替身：只需要 uid / realm / auth_json 三个属性。"""
        class _A:
            pass
        a = _A()
        a.uid = f"uid-{realm}"
        a.realm = realm
        a.auth_json = '{"auth":{}}'
        return a

    def test_global_account_gets_global_headers(self):
        """**核心**：global 账号的出站头必须是国际版的值。"""
        from admin.routers import proxy as px
        h = px._upstream_extra_headers(self._make_request(),
                                       account=self._fake_account("global"))
        self.assertEqual(h.get("X-IDE-Name"), GL_SIG["app"],
                         "国际版账号的 X-IDE-Name 必须是 workbuddy-ai")
        self.assertEqual(h.get("X-IDE-Version"), GL_SIG["ver"],
                         "国际版账号的 X-IDE-Version 必须是 5.8.4")
        self.assertEqual(h.get("X-Product"), "SaaS")

    def test_cn_account_gets_cn_headers(self):
        from admin.routers import proxy as px
        h = px._upstream_extra_headers(self._make_request(),
                                       account=self._fake_account("cn"))
        self.assertEqual(h.get("X-IDE-Name"), CN_SIG["app"])
        self.assertEqual(h.get("X-IDE-Version"), CN_SIG["ver"])

    def test_two_realms_produce_different_headers(self):
        from admin.routers import proxy as px
        cn = px._upstream_extra_headers(self._make_request(),
                                        account=self._fake_account("cn"))
        gl = px._upstream_extra_headers(self._make_request(),
                                        account=self._fake_account("global"))
        for k in ("X-IDE-Name", "X-IDE-Type", "X-IDE-Version"):
            self.assertNotEqual(cn.get(k), gl.get(k),
                                f"{k} 两版不该相同（分档没生效）")

    def test_no_account_backward_compatible(self):
        """不传 account 时保持历史行为（取默认那份），不能炸。"""
        from admin.routers import proxy as px
        h = px._upstream_extra_headers(self._make_request())
        self.assertIn("X-IDE-Name", h)
        self.assertIn("X-IDE-Version", h)


class DeviceTokenRealmCase(unittest.TestCase):
    """**缺口②**：设备指纹必须按 realm 分开缓存与取值。"""

    def test_signature_has_realm(self):
        from admin import turing_token as tt
        params = inspect.signature(tt.get_device_token).parameters
        self.assertIn("realm", params,
                      "get_device_token 必须接受 realm —— 两版 SDK/channelId 不同")

    def test_cache_slots_per_realm(self):
        """两版各自一个缓存槽，绝不共用。"""
        from admin import turing_token as tt
        tt.clear_cache()
        cn_slot = tt._slot("cn")
        gl_slot = tt._slot("global")
        self.assertIsNot(cn_slot, gl_slot, "两版不能共用同一个缓存槽")
        cn_slot["token"] = "cn-token"
        self.assertIsNone(gl_slot["token"],
                          "写 cn 的槽不该影响 global 的槽")

    def test_clear_cache_specific_realm(self):
        from admin import turing_token as tt
        tt.clear_cache()
        tt._slot("cn")["token"] = "c"
        tt._slot("global")["token"] = "g"
        tt.clear_cache("cn")
        self.assertIsNone(tt._slot("cn")["token"])
        self.assertEqual(tt._slot("global")["token"], "g",
                         "清 cn 不该影响 global")

    def test_env_uses_realm_specific_params(self):
        """下发给 helper 的环境变量必须按 realm 取（否则算的是国内版指纹）。"""
        from admin import turing_token as tt
        from admin import client_profile as cp
        captured = {}

        def _fake_run(cmd, **kw):
            captured["env"] = kw.get("env") or {}
            return mock.Mock(returncode=1, stdout="", stderr="")

        tt.clear_cache()
        with mock.patch("subprocess.run", side_effect=_fake_run), \
             mock.patch.object(tt, "_node_bin", return_value="node"), \
             mock.patch.object(tt, "_HELPER") as helper:
            helper.is_file.return_value = True
            tt.get_device_token(force=True, realm="global")
        env = captured.get("env") or {}
        self.assertEqual(str(env.get("WORKBUDDY_TURING_CHANNEL_ID")),
                         str(GL_SIG["channel"]),
                         "下发给 helper 的 channelId 必须是国际版的 400111")
        self.assertEqual(str(env.get("WORKBUDDY_TURING_VERSION")), GL_SIG["ver"])
        self.assertIn("WorkBuddyAI", str(env.get("WORKBUDDY_INSTALL_DIR", "")),
                      "下发的安装目录必须是国际版目录")


class FallbackUACase(unittest.TestCase):
    """**缺口③**：UA 兜底分支也必须按 realm 取版本。"""

    def test_client_version_accepts_realm(self):
        from admin import realm as realm_mod
        self.assertIn("realm", inspect.signature(realm_mod.client_version).parameters)
        self.assertIn("realm", inspect.signature(realm_mod.cli_version).parameters)

    def test_fallback_ua_correct_per_realm(self):
        """client_profile 不可用时，两版 UA 仍必须各自正确。"""
        from admin import realm as realm_mod
        from admin import client_profile as cp
        with mock.patch.object(cp, "ua", side_effect=RuntimeError("simulated")):
            cn = realm_mod.chat_ua("cn")
            gl = realm_mod.chat_ua("global")
        self.assertIn(f"{CN_SIG['app']}/{CN_SIG['ver']}", cn)
        self.assertIn(f"{GL_SIG['app']}/{GL_SIG['ver']}", gl,
                      f"兜底时国际版 UA 应为 workbuddy-ai/5.8.4，实际 {gl!r}")
        self.assertNotIn(f"{GL_SIG['app']}/{CN_SIG['ver']}", gl,
                         "**兜底路径也不能出现「品牌对、版本错」**")

    def test_extreme_fallback_differs(self):
        """连 wb_install 都不可用时，两版版本号仍必须不同。"""
        from admin import realm as realm_mod
        with mock.patch("wb_install.WB.desktop_version",
                        side_effect=RuntimeError("simulated")):
            cn = realm_mod.client_version("cn")
            gl = realm_mod.client_version("global")
        self.assertNotEqual(cn, gl, "极端兜底时两版版本号也不能相同")
        self.assertEqual(gl, GL_SIG["ver"])

    def test_billing_ua_fallback_per_realm(self):
        from admin import realm as realm_mod
        from admin import client_profile as cp
        with mock.patch.object(cp, "desktop_version", side_effect=RuntimeError("x")):
            cn = realm_mod.billing_ua("cn")
            gl = realm_mod.billing_ua("global")
        self.assertTrue(cn.startswith(f"{CN_SIG['app']}/"))
        self.assertTrue(gl.startswith(f"{GL_SIG['app']}/"))


class EndToEndConsistencyCase(unittest.TestCase):
    """端到端：**同一 realm 的所有身份要素必须指向同一版本**。

    这是审计的核心不变量 —— 单个字段对不代表整体对，
    「UA 是国际版、X-IDE-Name 是国内版」这种自相矛盾才是最危险的。
    """

    def _identity_of(self, realm):
        from admin import client_profile as cp
        from admin import realm as realm_mod
        eff = cp.effective(None, realm=realm)
        risk = cp.risk_headers(realm)
        return {
            "ua_brand": realm_mod.ua_brand(realm),
            "ua": cp.ua(realm=realm),
            "ide_name": risk.get("X-IDE-Name"),
            "ide_version": risk.get("X-IDE-Version"),
            "app_name": eff.get("application_name"),
            "channel": eff.get("turing_channel_id"),
            "desktop_version": eff.get("desktop_version"),
        }

    def test_all_identity_fields_agree(self):
        for realm, sig in (("cn", CN_SIG), ("global", GL_SIG)):
            ident = self._identity_of(realm)
            self.assertEqual(ident["ua_brand"], sig["app"],
                             f"[{realm}] UA 品牌段")
            self.assertEqual(ident["app_name"], sig["app"],
                             f"[{realm}] application_name")
            self.assertEqual(ident["ide_name"], sig["app"],
                             f"[{realm}] X-IDE-Name（必须与 UA 品牌段一致）")
            self.assertEqual(ident["ide_version"], sig["ver"],
                             f"[{realm}] X-IDE-Version")
            self.assertEqual(ident["desktop_version"], sig["ver"],
                             f"[{realm}] desktop_version")
            self.assertEqual(int(ident["channel"]), sig["channel"],
                             f"[{realm}] turing_channel_id")
            # UA 里的版本段也要与 X-IDE-Version 一致
            self.assertIn(f"{sig['app']}/{sig['ver']}", ident["ua"],
                          f"[{realm}] UA 的版本段与身份不一致：{ident['ua']!r}")

    def test_no_cross_contamination(self):
        """两版之间绝不串味：cn 的身份要素里不能出现国际版的值，反之亦然。"""
        cn = self._identity_of("cn")
        gl = self._identity_of("global")
        for k in ("ua_brand", "ide_name", "app_name"):
            self.assertNotEqual(cn[k], gl[k], f"{k} 两版相同 → 串味了")
        for k in ("ide_version", "desktop_version"):
            self.assertNotEqual(cn[k], gl[k], f"{k} 两版相同 → 串味了")
        self.assertNotEqual(int(cn["channel"]), int(gl["channel"]))


if __name__ == "__main__":
    unittest.main()
