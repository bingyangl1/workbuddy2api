"""两版兜底值必须完整（含版本号）—— 一个隐蔽真 bug 的回归测试。

## 症状（浏览器验收时才暴露）

`GET /api/client-profile?realm=global` 返回：

    application_name = 'workbuddy-ai'   ✅ 品牌段对
    desktop_version  = '5.8.3'          ❌ 这是**国内版**的版本号

拼出来的 UA 是 **`workbuddy-ai/5.8.3 workbuddy-ai/5.8.3 CLI/2.161.0`**，
而官方国际版客户端发的是 `workbuddy-ai/5.8.4 …`。

## 根因

`REALM_FALLBACKS` 只给了 `application_name` / `turing_product_name` /
`turing_channel_id` 三个键，**漏了 `desktop_version` 和 `cli_version`**。

于是探测不到对应版本客户端时：
* 品牌段 → 走 realm 兜底 ✅
* 版本段 → 回落到通用 `DEFAULTS.desktop_version`（两版同一个值）❌

而 UA 是 `${applicationName}/${productVersion}` 拼的 —— 两段来自**不同层**，
就拼出了「品牌是国际版、版本是国内版」的组合。

## 为什么这个 bug 特别隐蔽

1. **本机两版都装时不会复现**（探测得到 → 走真实值）——
   我单跑单元测试、直接调 python 都是对的，只有走 HTTP 长驻进程才暴露。
2. **只有「探测不到客户端」的环境才命中**：线上服务器、扫描范围没覆盖、
   客户端未安装 —— 而这些恰恰是部署后的常态。
3. 「半对」比全错更难定位：渠道号对、风控头对、UA 结构对，只有版本号错。

而版本号错就是**可识别的风控特征**（用户特别强调过「对应版本要用对应的
客户端参数」）。

## 本测试覆盖

模拟「探测不到任何客户端」的环境，断言两版的关键身份仍然各自正确。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_install
from wb_install import REALM_FALLBACKS

_EMPTY_RESOLVE = {
    "install_dir": None, "asar": None, "unpacked": None,
    "desktop_version": "", "cli_version": "",
    "turing_channel_id": 0, "turing_product_name": "",
    "commit": "", "release_date_ms": 0, "deployment_type": "",
    "application_name": "", "plugin_name": "", "sources": {},
}


class FallbackCompletenessCase(unittest.TestCase):
    """兜底表本身必须完整（这是根因所在）。"""

    def test_both_realms_have_version_keys(self):
        for realm in ("cn", "global"):
            fb = REALM_FALLBACKS.get(realm) or {}
            for key in ("application_name", "turing_product_name",
                        "turing_channel_id", "desktop_version", "cli_version"):
                self.assertIn(key, fb,
                              f"**{realm} 的兜底缺 {key}** —— "
                              f"UA 由 applicationName + productVersion 拼成，"
                              f"缺任一段都会拼出「品牌对、版本错」的组合")
                self.assertTrue(fb[key] not in (None, ""),
                                f"{realm}.{key} 不能为空")

    def test_versions_differ_between_realms(self):
        """两版版本号必须不同（实测：cn=5.8.3 / global=5.8.4）。"""
        cn = REALM_FALLBACKS["cn"]["desktop_version"]
        gl = REALM_FALLBACKS["global"]["desktop_version"]
        self.assertNotEqual(cn, gl,
                            "两版 desktop_version 相同 → 国际版会报国内版版本号")

    def test_brands_differ_between_realms(self):
        self.assertNotEqual(REALM_FALLBACKS["cn"]["application_name"],
                            REALM_FALLBACKS["global"]["application_name"])
        self.assertEqual(REALM_FALLBACKS["global"]["application_name"], "workbuddy-ai",
                         "国际版品牌段（实测 applicationName）")
        self.assertEqual(REALM_FALLBACKS["cn"]["application_name"], "WorkBuddy")


class NoClientEnvironmentCase(unittest.TestCase):
    """模拟线上服务器（探测不到客户端）：两版身份仍须各自正确。"""

    def setUp(self):
        self.patcher = mock.patch.object(wb_install._WorkBuddyInstall, "_resolve",
                                         return_value=dict(_EMPTY_RESOLVE))
        self.patcher.start()
        wb_install.WB._realm_cache = {}

    def tearDown(self):
        self.patcher.stop()
        wb_install.WB._realm_cache = {}

    def test_versions_differ_without_client(self):
        """**核心**：没有客户端时两版版本号也必须不同。

        修复前两者都回落到 DEFAULTS（同一个值）→ 国际版报国内版版本号。
        """
        cn = wb_install.WB.desktop_version(realm="cn")
        gl = wb_install.WB.desktop_version(realm="global")
        self.assertNotEqual(cn, gl,
                            f"无客户端环境下两版版本号相同（{cn!r}）—— "
                            f"国际版会带着国内版版本号出站")
        self.assertEqual(gl, "5.8.4", f"国际版版本号应为 5.8.4，实际 {gl!r}")
        self.assertEqual(cn, "5.8.3", f"国内版版本号应为 5.8.3，实际 {cn!r}")

    def test_ua_correct_without_client(self):
        """无客户端环境下，两版 UA 的**三段**都必须同源正确。"""
        for realm, brand, ver in (("cn", "WorkBuddy", "5.8.3"),
                                  ("global", "workbuddy-ai", "5.8.4")):
            ua = wb_install.WB.desktop_ua(realm=realm)
            self.assertIn(f"{brand}/{ver}", ua,
                          f"{realm} 的 UA 应含 {brand}/{ver}，实际 {ua!r}")
            # 三段同源：前两段的品牌必须一致（历史上曾第一段硬编码 WorkBuddy）
            segs = ua.split(" ")
            self.assertEqual(segs[0].split("/")[0], segs[1].split("/")[0],
                             f"{realm} UA 两段品牌不一致：{ua!r}")

    def test_global_ua_never_carries_cn_version(self):
        """国际版 UA 绝不能出现国内版版本号（这是本 bug 的显性症状）。"""
        gl_ua = wb_install.WB.desktop_ua(realm="global")
        cn_ver = wb_install.WB.desktop_version(realm="cn")
        self.assertNotIn(f"workbuddy-ai/{cn_ver}", gl_ua,
                         f"国际版 UA 里出现了国内版版本号：{gl_ua!r}")

    def test_channel_and_brand_still_realm_aware(self):
        """顺带确认既有的 realm 兜底没被破坏。"""
        self.assertEqual(wb_install.WB.turing_channel_id(realm="cn"), 109144)
        self.assertEqual(wb_install.WB.turing_channel_id(realm="global"), 400111)
        self.assertEqual(wb_install.WB.application_name(realm="global"), "workbuddy-ai")

    def test_cli_version_per_realm(self):
        """CLI 版本也走 realm 兜底（两版实测相同，但结构上必须按 realm 查）。"""
        for realm in ("cn", "global"):
            v = wb_install.WB.cli_version(realm=realm)
            self.assertTrue(v, f"{realm} 的 cli_version 不该为空")
            self.assertNotEqual(v, "", "空值会让 UA 最后一段缺失")


class EnvOverrideStillWinsCase(unittest.TestCase):
    """env 覆盖仍然最高优先级（修复不能破坏既有覆盖能力）。"""

    def setUp(self):
        wb_install.WB._realm_cache = {}

    def tearDown(self):
        wb_install.WB._realm_cache = {}
        os.environ.pop("WORKBUDDY_DESKTOP_VERSION", None)
        os.environ.pop("WORKBUDDY_CLI_VERSION", None)

    def test_env_wins_over_realm_fallback(self):
        os.environ["WORKBUDDY_DESKTOP_VERSION"] = "9.9.9"
        for realm in ("cn", "global"):
            self.assertEqual(wb_install.WB.desktop_version(realm=realm), "9.9.9")

    def test_env_cli_wins(self):
        os.environ["WORKBUDDY_CLI_VERSION"] = "9.9.9"
        for realm in ("cn", "global"):
            self.assertEqual(wb_install.WB.cli_version(realm=realm), "9.9.9")


if __name__ == "__main__":
    unittest.main()
