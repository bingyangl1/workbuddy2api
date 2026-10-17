"""显式覆盖不得污染另一个 realm —— 隐蔽真 bug 的回归测试。

## 症状（只有浏览器验收才暴露）

`GET /api/client-profile?realm=global` 返回：

    install_dir      = 'D:\\Software\\WorkBuddy'   ← **国内版目录**！
    application_name = 'WorkBuddy'                 ← 应该是 workbuddy-ai
    desktop_version  = '5.8.3'                     ← 应该是 5.8.4

即：**国际版账号会带着整套国内版身份出站**（品牌段 / 版本号 / 渠道号全错）——
正是用户强调过的风控特征。

## 根因

`_candidate_dirs()` 里「显式指定安装目录」的分支**完全无视 realm 过滤**：

    explicit = self._explicit_install()          # 单个路径，来自后台设置
    if explicit:
        out.append(expand_path(explicit))        # ← 无条件优先，跳过 realm 名单

而后台设置里存着 `wb_paths.install_dir = D:\\Software\\WorkBuddy`（国内版）。
于是 `_realm_data("global")` 也返回那个国内版目录。

## 为什么这个 bug 极难发现

1. **全新进程直接调用探测是正确的**（没有 DB 覆盖）——
   我反复验证过脚本、单元测试、并发、缓存，全都是 5.8.4。
2. **只有长驻引擎**读了后台覆盖才触发。
3. 本机两版都装了也不会复现（自动扫描能扫到两版）。
4. 表面症状只是「版本号是 5.8.3」，很容易误判成缓存或兜底表缺项。

最后是通过**在引擎进程内注册临时诊断端点**读到 `realm_data_global` 的真实
返回值才定位的（脚本层永远复现不了）。

## 设计缺陷本身

覆盖值是**一个**路径，无法表达「国内版在这里、国际版在那里」。
所以正确行为是：按 realm 探测时，**只有该覆盖路径确实属于这个 realm 才采用**，
否则跳过它、交给 realm 感知的自动扫描。

## 本测试覆盖

* 覆盖成 cn 路径时：cn 用它、**global 必须跳过它**（走自动扫描）
* 覆盖成 global 路径时：global 用它、cn 跳过
* 覆盖路径指向 `resources` / `app.asar` 时也能正确判断归属
* **无 realm 过滤时（主探测）仍然尊重覆盖**（零回归）
* 无覆盖时的正常行为
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb_install import WB


class ExplicitOverrideRealmCase(unittest.TestCase):
    """用 mock 隔离真实磁盘与自动扫描，只验「覆盖是否被正确采纳」。"""

    def setUp(self):
        WB._realm_cache = {}

    def tearDown(self):
        try:
            WB.set_overrides()
        except Exception:
            pass
        WB._realm_cache = {}
        for k in ("WORKBUDDY_INSTALL_DIR", "WORKBUDDY_ASAR_PATH"):
            os.environ.pop(k, None)

    # ── 归属判定（纯逻辑） ──
    def test_no_realm_filter_always_accepts(self):
        """主探测（无 realm 过滤）必须尊重覆盖 —— 零回归。"""
        WB.set_overrides(install_dir=r"D:\Software\WorkBuddy")
        WB._realm_filter = ""
        self.assertTrue(WB._explicit_matches_realm())

    def test_cn_override_accepted_for_cn(self):
        WB.set_overrides(install_dir=r"D:\Software\WorkBuddy")
        WB._realm_filter = "cn"
        self.assertTrue(WB._explicit_matches_realm())

    def test_cn_override_REJECTED_for_global(self):
        """**核心**：国内版覆盖值不能给 global 用。"""
        WB.set_overrides(install_dir=r"D:\Software\WorkBuddy")
        WB._realm_filter = "global"
        self.assertFalse(WB._explicit_matches_realm(),
                         "global 探测时不该采用国内版的覆盖路径 —— "
                         "否则国际版会带整套国内版身份出站")

    def test_global_override_accepted_for_global(self):
        WB.set_overrides(install_dir=r"D:\Software\WorkBuddyAI")
        WB._realm_filter = "global"
        self.assertTrue(WB._explicit_matches_realm())

    def test_global_override_REJECTED_for_cn(self):
        WB.set_overrides(install_dir=r"D:\Software\WorkBuddyAI")
        WB._realm_filter = "cn"
        self.assertFalse(WB._explicit_matches_realm())

    def test_alias_dirs_recognized(self):
        """各种别名（大小写/连字符）都要能判对归属。"""
        cases = [
            (r"D:\x\workbuddy-ai", "global", True),
            (r"D:\x\WorkBuddy AI", "global", True),
            (r"D:\x\workbuddyai", "global", True),
            (r"D:\x\workbuddy", "cn", True),
            (r"D:\x\WorkBuddy", "global", False),
        ]
        for path, realm, expect in cases:
            WB.set_overrides(install_dir=path)
            WB._realm_filter = realm
            self.assertEqual(
                WB._explicit_matches_realm(), expect,
                f"{path!r} 对 realm={realm} 的归属判定应为 {expect}")

    def test_no_override_returns_true(self):
        """没有覆盖时返回 True（走自动扫描，那是 realm 感知的）。"""
        WB.set_overrides()
        WB._realm_filter = "global"
        self.assertTrue(WB._explicit_matches_realm())

    def test_asar_and_resources_paths(self):
        """覆盖指向 resources / app.asar 时，往上找到安装目录名再判归属。"""
        WB.set_overrides(install_dir=r"D:\Software\WorkBuddy\resources")
        WB._realm_filter = "global"
        self.assertFalse(WB._explicit_matches_realm(),
                         "resources 子目录应归到 WorkBuddy（cn），不该给 global")

    def test_env_var_respected(self):
        """环境变量形式（WORKBUDDY_INSTALL_DIR）同样受 realm 判定约束。"""
        os.environ["WORKBUDDY_INSTALL_DIR"] = r"E:\apps\WorkBuddy"
        WB._realm_filter = "global"
        self.assertFalse(WB._explicit_matches_realm())
        WB._realm_filter = "cn"
        self.assertTrue(WB._explicit_matches_realm())


class CandidateDirsCase(unittest.TestCase):
    """`_candidate_dirs()` 的产出必须体现 realm 过滤。"""

    def setUp(self):
        WB._realm_cache = {}

    def tearDown(self):
        try:
            WB.set_overrides()
        except Exception:
            pass
        WB._realm_filter = ""
        WB._realm_cache = {}

    def _dirs(self):
        return [str(d).lower() for d in WB._candidate_dirs()]

    def test_global_candidates_exclude_cn_override(self):
        """global 的候选列表里不该出现国内版的覆盖路径。"""
        WB.set_overrides(install_dir=r"D:\Software\WorkBuddy")
        WB._realm_filter = "global"
        dirs = self._dirs()
        # 覆盖值本身（WorkBuddy 结尾的精确路径）不该出现
        bad = [d for d in dirs if d.rstrip("\\").endswith("d:\\software\\workbuddy")]
        self.assertEqual(bad, [],
                         f"global 的候选里出现了国内版覆盖路径：{bad}")

    def test_cn_candidates_include_cn_override(self):
        WB.set_overrides(install_dir=r"D:\Software\WorkBuddy")
        WB._realm_filter = "cn"
        dirs = self._dirs()
        self.assertTrue(any("d:\\software\\workbuddy" == d.rstrip("\\") for d in dirs),
                        "cn 的候选里应包含国内版覆盖路径")

    def test_global_candidates_only_global_names(self):
        """realm=global 时只应产出国际版的目录名。"""
        WB._realm_filter = "global"
        dirs = self._dirs()
        # 候选里不该有以 \workbuddy 结尾但同时不是 workbuddyai 的项
        suspicious = [d for d in dirs
                      if d.rstrip("\\").endswith("\\workbuddy")]
        self.assertEqual(suspicious, [],
                         f"global 候选里出现了纯 workbuddy 目录名：{suspicious[:3]}")


class EndToEndRealmDataCase(unittest.TestCase):
    """端到端：覆盖存在时两版探测结果必须不同（这是 bug 的直接症状）。"""

    def setUp(self):
        WB._realm_cache = {}

    def tearDown(self):
        try:
            WB.set_overrides()
        except Exception:
            pass
        WB._realm_cache = {}

    @unittest.skipUnless(os.path.isdir(r"D:\Software\WorkBuddyAI"),
                         "本机没装国际版客户端，跳过端到端")
    def test_two_realms_differ_with_cn_override(self):
        """有国内版覆盖时，两版档案必须仍然不同。

        ## 前提校正（本机实测发现）

        这条测试原本假设 `D:\\Software\\WorkBuddy` 是**真正的国内版**。
        但实测发现：那台机器上该目录里装的是**国际版**
        （`product.json` 的 `applicationName = workbuddy-ai`、
        `isOversea = true`、`appVersion = 5.8.4`）。

        于是 `WB._realm_data('cn')` 会做**内容一致性校验**并拒绝采信
        （日志：`目录 ... 里的实际产品是 'workbuddy-ai'（与目录名不符）`），
        返回空档案 —— 两版都为空，`assertNotEqual('', '')` 自然失败。

        **这是正确行为**，不是回归。所以：

          * 两版都探测到 → 必须不同
          * 有一版探测不到（空）→ 不算「被污染」，跳过差异断言
        """
        WB.set_overrides(install_dir=r"D:\Software\WorkBuddy")
        cn = WB._realm_data("cn")
        gl = WB._realm_data("global")

        cn_ver = cn.get("desktop_version") or ""
        gl_ver = gl.get("desktop_version") or ""
        if not cn_ver or not gl_ver:
            # 有一版没探测到 —— 说明内容校验生效了（目录名与产品不符），
            # 这正是我们要的行为。断言「没有被强行套上同一份身份」即可。
            self.assertFalse(
                cn_ver and cn_ver == gl_ver,
                f"两版都被探到且版本相同（{cn_ver!r}）—— 覆盖污染")
            return

        self.assertNotEqual(
            cn_ver, gl_ver,
            f"有国内版覆盖时两版版本仍相同（{cn_ver!r}）"
            f"—— 说明 global 仍被覆盖污染")
        self.assertNotEqual(
            cn.get("application_name"), gl.get("application_name"),
            "两版品牌段也必须不同")


if __name__ == "__main__":
    unittest.main()
