"""安卓 APK 必须同时打包新旧两套面板，且落点可切换。

## 背景

用户问：「安卓的目前没用新的UI吗？」—— 确实没用。

原状态：
  * `build_android.py` 只复制 `admin/static`（旧面板）
  * `MainActivity.java` 硬编码 `loadUrl(origin + "/admin?desktop=1&android=1")`
  * 新面板产物 `web/out` **从不进 APK**

决策：安卓也默认上新面板（与桌面端对齐），旧面板保留作回退。

## 改了什么

1. **打包**：`build_android.py` 复制 `web/out` → `staging/web/out`
2. **落点**：`MainActivity.DEFAULT_PANEL = "next"` → `/panel?desktop=1&android=1&mobile=1`
3. **`isAdmin`**：必须同时认 `/panel`，否则 `WBMobile.initialize(token)`
   永不执行（真机上表现就是「永久卡在正在打开工作台」）
4. **运行时**：`android_runtime.py` 设 `ADMIN_PANEL_STATIC=1` +
   `ADMIN_PANEL_PATH=/panel` + `ADMIN_DEFAULT_PANEL=next`
5. **免密桥**：新面板用 `wb-admin-token`（连字符），旧面板用
   `wb_admin_token`（下划线）—— `mobile.js` 必须**两个都写**
6. **桥检测**：`desktop-bridge.ts` 新增 `hasAndroidBridge()` /
   `waitForAndroidToken()`，因为安卓是 Java 主动推 token，
   不是 JS 去问 `pywebview.api`

## 本测试守什么

这些都是「改了一处忘了另一处」就会静默失效的环节 ——
真机上表现为白屏或卡引导层，且 WebView 不转发 console，极难排查。
"""
import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "scripts" / "build_android.py"
MAIN = ROOT / "android" / "app" / "src" / "main" / "java" / "io" / "github" \
    / "bingyangl1" / "workbuddy2api" / "MainActivity.java"
RUNTIME = ROOT / "android" / "python" / "android_runtime.py"
MOBILE_JS = ROOT / "android" / "web" / "mobile.js"
BRIDGE = ROOT / "web" / "lib" / "desktop-bridge.ts"


class PackagingCase(unittest.TestCase):
    """打包脚本必须把两套面板都放进去。"""

    @classmethod
    def setUpClass(cls):
        cls.src = BUILD.read_text(encoding="utf-8")

    def test_copies_web_out(self):
        """必须复制 `web/out`（新面板产物）。"""
        self.assertIn("web_out", self.src,
                      "build_android.py 应引用 web/out")
        self.assertRegex(
            self.src, r"copytree\(\s*web_out\s*,\s*staging\s*/\s*[\"']web/out[\"']",
            "**必须把 web/out 复制进 staging** —— 否则 APK 里没有新面板")

    def test_still_copies_old_panel(self):
        """旧面板仍要打包（用于回退）。"""
        self.assertIn("admin/static", self.src,
                      "旧面板应继续打包（新面板出问题时可回退）")

    def test_warns_when_panel_missing(self):
        """没构建新面板时要给出明确警告，不能静默少打。"""
        self.assertRegex(self.src, r"未找到\s*web/out|未找到.*web/out",
                         "未构建新面板时应显式提示，而不是静默跳过")


class LandingCase(unittest.TestCase):
    """Runtime 必须能加载新面板。"""

    @classmethod
    def setUpClass(cls):
        cls.java = MAIN.read_text(encoding="utf-8")
        cls.rt = RUNTIME.read_text(encoding="utf-8")

    def test_default_panel_is_next(self):
        self.assertIn('DEFAULT_PANEL = "next"', self.java,
                      "安卓默认落点应为新面板（与桌面端对齐）")

    def test_landing_path_has_panel(self):
        self.assertIn("/panel?desktop=1&android=1", self.java,
                      "落点路径应指向 /panel")

    def test_is_admin_accepts_panel(self):
        """**关键**：`isAdmin` 必须认 `/panel`。

        只认 `/admin` 时，`onPageFinished` 里的 token 注入永不执行，
        界面会永久卡在引导层（真机实测踩过）。
        """
        m = re.search(r"private boolean isAdmin\(String url\)\s*\{(.*?)\n    \}",
                      self.java, re.S)
        self.assertIsNotNone(m, "找不到 isAdmin 方法")
        body = m.group(1)
        self.assertIn("/panel", body,
                      "**isAdmin 必须接受 /panel** —— 否则新面板拿不到 token")
        self.assertIn("startsWith", body,
                      "应支持子路径（/panel/models/ 等）")

    def test_runtime_enables_panel_static(self):
        """运行时必须开 `ADMIN_PANEL_STATIC`，否则 /panel 不挂载（404）。"""
        self.assertIn("ADMIN_PANEL_STATIC", self.rt,
                      "**必须设 ADMIN_PANEL_STATIC=1** —— 否则 /panel 不会挂载")
        self.assertIn("ADMIN_PANEL_PATH", self.rt)
        self.assertIn("ADMIN_DEFAULT_PANEL", self.rt)


class TokenKeyCompatCase(unittest.TestCase):
    """两套面板的 token key 不同，壳必须都写。"""

    def test_mobile_js_writes_both_keys(self):
        """**必须真的调用 setItem('wb-admin-token', ...)**，不是"提到这个词"。

        反向验证抓到过假绿：原断言只查 `'wb-admin-token' in src`，
        而源码的**注释里**也写着这个词（解释为什么两个 key 不同），
        于是把实际那行 `setItem` 删掉，断言依然通过。

        现在要求：**在 `initialize` 函数体内**出现实际的 `setItem` 调用。
        """
        src = MOBILE_JS.read_text(encoding="utf-8")

        # 切出 initialize 函数体
        m = re.search(r"function initialize\(token\)\s*\{(.*?)\n  \}", src, re.S)
        self.assertIsNotNone(m, "找不到 initialize()")
        body = m.group(1)

        # 去掉注释行，只看真实代码
        code_lines = [ln for ln in body.splitlines()
                      if ln.strip() and not ln.strip().startswith("//")]
        code = "\n".join(code_lines)

        self.assertRegex(
            code, r"localStorage\.setItem\(\s*['\"]wb-admin-token['\"]",
            "**initialize 里必须真的 setItem('wb-admin-token', ...)** —— "
            "否则新面板读不到 token，停在登录页")
        # 旧 key 仍要写（回退时用）
        self.assertRegex(
            code, r"localStorage\.setItem\(\s*TOKEN_KEY",
            "旧面板的 key 仍要写（ADMIN_DEFAULT_PANEL=legacy 回退时用）")

    def test_keys_really_differ(self):
        """确认两个 key 确实不同（防止有人「统一」掉）。"""
        index = (ROOT / "admin" / "static" / "index.html").read_text(encoding="utf-8")
        m = re.search(r'const TOKEN_KEY\s*=\s*"([^"]+)"', index)
        self.assertIsNotNone(m, "找不到旧面板的 TOKEN_KEY")
        old_key = m.group(1)
        api = (ROOT / "web" / "lib" / "api.ts").read_text(encoding="utf-8")
        m2 = re.search(r"const TOKEN_KEY\s*=\s*'([^']+)'", api)
        self.assertIsNotNone(m2, "找不到新面板的 TOKEN_KEY")
        new_key = m2.group(1)
        self.assertNotEqual(
            old_key, new_key,
            f"两套面板的 key 本就不同（旧={old_key!r} 新={new_key!r}）—— "
            "正是因为不同，壳才必须两个都写。若你「统一」了它们，"
            "请同时更新这条测试和 mobile.js")


class AndroidBridgeCase(unittest.TestCase):
    """新面板必须认得安卓壳（Java 主动推 token，不问 pywebview）。"""

    @classmethod
    def setUpClass(cls):
        cls.src = BRIDGE.read_text(encoding="utf-8")

    def test_has_android_bridge(self):
        self.assertIn("export function hasAndroidBridge", self.src,
                      "应有 hasAndroidBridge() —— 安卓壳检测")
        self.assertIn("WBAndroid", self.src,
                      "应识别 Java 注入的 WBAndroid")
        self.assertIn("WBMobile", self.src,
                      "应识别 WBMobile")

    def test_waits_for_android_token(self):
        self.assertIn("export function waitForAndroidToken", self.src,
                      "应有 waitForAndroidToken() —— 等壳写 token")
        self.assertIn("wb-admin-token", self.src,
                      "应等待新面板的 key")

    def test_ensure_token_has_android_branch(self):
        """ensureDesktopToken 必须有安卓分支（否则走 pywebview 会一直失败）。"""
        m = re.search(r"export async function ensureDesktopToken\((.*?)\n\}",
                      self.src, re.S)
        self.assertIsNotNone(m, "找不到 ensureDesktopToken")
        body = m.group(1)
        self.assertIn("hasAndroidBridge()", body,
                      "**ensureDesktopToken 必须有安卓分支** —— "
                      "安卓没有 pywebview，走原路径会白等 15 秒")
        self.assertIn("waitForAndroidToken", body)

    def test_is_desktop_launch_covers_android(self):
        m = re.search(r"export function isDesktopLaunch\(\).*?\n\}", self.src, re.S)
        self.assertIsNotNone(m)
        self.assertIn("hasAndroidBridge()", m.group(0),
                      "isDesktopLaunch 应把安卓壳也算作「客户端启动」")


class StagingPanelCase(unittest.TestCase):
    """staging 里必须真的有新面板（跑过 --prepare-only 时）。"""

    STAGING = ROOT / "android" / "app" / "build" / "generated" / "python"

    def test_staging_has_panel(self):
        out = self.STAGING / "web" / "out"
        if not self.STAGING.is_dir():
            self.skipTest("staging 未生成（需先跑 build_android.py --prepare-only）")
        if not (ROOT / "web" / "out").is_dir():
            self.skipTest("web/out 未构建（需先 npm run build）")
        self.assertTrue((out / "index.html").is_file(),
                        "staging 里应有 web/out/index.html")
        n = len(list(out.rglob("*")))
        self.assertGreater(n, 30,
                           f"staging 的新面板只有 {n} 个条目，明显偏少")


if __name__ == "__main__":
    unittest.main()
