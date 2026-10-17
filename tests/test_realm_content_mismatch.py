"""探测到的客户端内容与目录名不符时，不能把身份套到另一个 realm。

## 真机实测发现（本机环境）

`D:\\Software\\WorkBuddy` 这个**国内版命名的目录**里，实际装的是**国际版**：

    product.json:  applicationName = "workbuddy-ai"
                   isOversea       = true
                   appVersion      = "5.8.4"
                   updateUrl       = "https://www.workbuddy.ai"

（证据见 `install-manifest.json` 里 `WorkBuddyAI.exe` / `appVersion: 5.8.4`）

于是出现两处「按目录名判定」导致的混用：

### ① `_realm_data('cn')` 读出了国际版身份

`_candidate_dirs` 按目录名筛（`WorkBuddy` → cn），
但没校验里面到底是什么 → `effective(realm='cn')` 拿到 `workbuddy-ai` / `5.8.4`。

后果：`realm.ua_brand('cn')` 返回 `'workbuddy-ai'`，
**国内版账号带着国际版 UA 出站**。这与「国际版带国内版身份」正好相反，
但同样是混用特征。

### ② `_realm_matches('cn')` 误判

`_realm_matches` 只看目录名（`WorkBuddy` 不含 AI → 认为是 cn），
于是 `turing_channel_id('cn')` 直接采用主探测的 `400111`（国际版渠道号）
→ **国内版报国际版渠道号**。

## 修法

1. `_resolve_for_realm`：探测后做**内容一致性校验** ——
   读到的 `application_name` 必须与该 realm 期望的品牌相符，否则不采信
2. `_realm_matches`：**产品名优先于目录名**（目录名只作兜底）

## 为什么这类 bug 难发现

开发机装的客户端版本决定了测试表现：

  * 装国内版 → 一切正常（探测值与 cn 相符）
  * 装国际版 → 12+ 个 realm 测试同时失败

本次是「本机恰好装了国际版」才照出来的。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_install  # noqa: E402


def _resolve(**kw):
    base = {
        "install_dir": r"D:\Software\WorkBuddy",
        "asar": None, "unpacked": None,
        "desktop_version": "5.8.4", "cli_version": "2.161.0",
        "turing_channel_id": 400111, "turing_product_name": "WorkBuddy AI",
        "commit": "", "release_date_ms": 0, "deployment_type": "SaaS",
        "application_name": "workbuddy-ai", "plugin_name": "workbuddy-desktop-ai",
        "sources": {},
    }
    base.update(kw)
    return base


class ContentMismatchCase(unittest.TestCase):
    """目录名说 cn，内容是 global → 不采信。"""

    def setUp(self):
        wb_install.WB._realm_cache = {}

    def tearDown(self):
        wb_install.WB._realm_cache = {}

    def test_cn_rejects_global_content(self):
        """**核心**：cn 目录里装了国际版 → 不采信该探测结果。"""
        with mock.patch.object(wb_install._WorkBuddyInstall, "_resolve",
                               return_value=_resolve()):
            wb_install.WB._realm_cache = {}
            cn = wb_install.WB._realm_data("cn")
        self.assertEqual(
            cn.get("application_name"), "",
            "**cn 目录里是国际版内容时不能采信** —— 否则国内版带国际版身份出站")
        self.assertEqual(cn.get("desktop_version"), "",
                         "版本号也不该采信")

    def test_global_still_works(self):
        """global 查询不受影响（内容确实是国际版）。"""
        with mock.patch.object(wb_install._WorkBuddyInstall, "_resolve",
                               return_value=_resolve()):
            wb_install.WB._realm_cache = {}
            gl = wb_install.WB._realm_data("global")
        self.assertEqual(gl.get("application_name"), "workbuddy-ai")

    def test_cn_content_accepted(self):
        """内容是 cn 时正常采信（零回归）。"""
        cn_data = _resolve(
            install_dir=r"D:\Software\WorkBuddy",
            desktop_version="5.8.3", turing_channel_id=109144,
            turing_product_name="WorkBuddy",
            application_name="WorkBuddy", plugin_name="workbuddy-desktop")
        with mock.patch.object(wb_install._WorkBuddyInstall, "_resolve",
                               return_value=cn_data):
            wb_install.WB._realm_cache = {}
            cn = wb_install.WB._realm_data("cn")
        self.assertEqual(cn.get("application_name"), "WorkBuddy")
        self.assertEqual(cn.get("desktop_version"), "5.8.3")

    def test_empty_resolve_passthrough(self):
        """探测不到任何东西时透传（不能因为校验把空档案搞坏）。"""
        empty = _resolve(install_dir=None, application_name="",
                         desktop_version="", turing_channel_id=0)
        with mock.patch.object(wb_install._WorkBuddyInstall, "_resolve",
                               return_value=empty):
            wb_install.WB._realm_cache = {}
            cn = wb_install.WB._realm_data("cn")
        self.assertEqual(cn.get("application_name"), "")


class RealmMatchesCase(unittest.TestCase):
    """`_realm_matches` 必须**产品名优先于目录名**。"""

    def test_wrong_dir_global_content(self):
        """目录名像 cn，但产品是国际版 → 不匹配 cn。"""
        with mock.patch.object(wb_install._WorkBuddyInstall, "_data",
                               return_value=_resolve()):
            self.assertFalse(
                wb_install.WB._realm_matches("cn"),
                "**目录名是 WorkBuddy 但产品是 workbuddy-ai 时，"
                "不能认为它匹配 cn** —— 否则 turing_channel_id('cn') "
                "会返回国际版的 400111")
            self.assertTrue(wb_install.WB._realm_matches("global"))

    def test_correct_dir_cn_content(self):
        cn_data = _resolve(
            install_dir=r"D:\Software\WorkBuddy",
            application_name="WorkBuddy", plugin_name="workbuddy-desktop",
            turing_channel_id=109144)
        with mock.patch.object(wb_install._WorkBuddyInstall, "_data",
                               return_value=cn_data):
            self.assertTrue(wb_install.WB._realm_matches("cn"))
            self.assertFalse(wb_install.WB._realm_matches("global"))

    def test_falls_back_to_dir_when_no_brand(self):
        """产品名缺失（老版本）时退回目录名判断。"""
        old = _resolve(application_name="", plugin_name="",
                       install_dir=r"D:\Software\WorkBuddyAI")
        with mock.patch.object(wb_install._WorkBuddyInstall, "_data",
                               return_value=old):
            self.assertTrue(
                wb_install.WB._realm_matches("global"),
                "产品名缺失时应退回目录名判断")


class ChannelIdCase(unittest.TestCase):
    """渠道号必须与查询的 realm 一致。"""

    def test_cn_channel_not_global(self):
        """目录名像 cn、内容像 global → cn 查询**不能**拿到 400111。"""
        with mock.patch.object(wb_install._WorkBuddyInstall,
                               "_realm_data", return_value={}):
            with mock.patch.object(wb_install._WorkBuddyInstall,
                                   "_data", return_value=_resolve()):
                got = wb_install.WB.turing_channel_id("cn")
        self.assertNotEqual(
            got, 400111,
            f"cn 查询返回了国际版渠道号 {got} —— "
            "本机装的是国际版，不该把它算成国内版")
        self.assertEqual(got, 109144, f"cn 渠道号应为 109144，实际 {got}")


if __name__ == "__main__":
    unittest.main()
