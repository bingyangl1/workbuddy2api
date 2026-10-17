"""安卓 WebView 引导层必须能被可靠移除。

## 真实故障（OnePlus 9 真机实测发现）

界面永久停在「正在打开本地工作台…」+「重新连接」按钮，点重新连接后显示：

    本地连接失败：pywebview is not defined

但**内嵌服务是完全正常的**：

    adb shell curl http://127.0.0.1:8790/api/app/version  →  200

### 根因（两个叠加）

1. `android/web/mobile.js` 的 `initialize()` 里调用了
   `WBDesktop.checkUpdatesAtStartup()`，而 `admin/static/desktop.js`
   **从来没导出过这个名字**（只导出 `openUpdates` / `retryLocal`）
   → `TypeError: WBDesktop.checkUpdatesAtStartup is not a function`

2. 更致命的是**顺序**：`initialize()` 先调 `enterApp()`，然后才
   `document.getElementById('localBoot').remove()`。中间那步抛了异常，
   引导层就**永远不会被移除**。

### 为什么极难排查

* WebView 默认**不转发 console 日志**，`adb logcat` 里一行 JS 错误都没有
* 服务是好的（curl 200），所以看起来像「WebView 连不上服务」
* 错误信息 `pywebview is not defined` 指向了完全无关的方向
  （那是桌面端 PyWebView 的桥，Android 根本不用）

**教训**：引导层（loading overlay）的移除必须是**第一步**，
不能排在任何可能失败的操作之后。否则一个可选功能的异常会
把整个应用永久卡在启动画面上。
"""
import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
DESKTOP_JS = ROOT / "admin" / "static" / "desktop.js"
MOBILE_JS = ROOT / "android" / "web" / "mobile.js"


class WbDesktopExportsCase(unittest.TestCase):
    """`WBDesktop` 暴露的方法必须覆盖移动端实际调用的那些。"""

    @classmethod
    def setUpClass(cls):
        cls.desktop = DESKTOP_JS.read_text(encoding="utf-8")
        cls.mobile = MOBILE_JS.read_text(encoding="utf-8")

    def test_check_updates_at_startup_exported(self):
        """`checkUpdatesAtStartup` 必须被导出（mobile.js 会调它）。"""
        # 在 `return { ... }` 里找
        m = re.search(r"return\s*\{(.*?)\}\s*;", self.desktop, re.S)
        self.assertIsNotNone(m, "desktop.js 应有 return {...} 导出块")
        block = m.group(1)
        self.assertIn(
            "checkUpdatesAtStartup", block,
            "**`checkUpdatesAtStartup` 必须被导出** —— "
            "android/web/mobile.js 的 initialize() 会调它，"
            "没导出就抛 TypeError，引导层永远不消失")

    def test_all_mobile_desktop_calls_exist(self):
        """mobile.js 里调用的每个 `WBDesktop.X` 都必须在导出块里。

        ## 为什么用「精确键名」而不是子串包含

        第一版写的是 `name in exported`（子串匹配）。反向验证发现：
        把导出名改成 `_checkUpdatesAtStartup` 时，子串仍在导出块里 →
        断言照样通过 → 假绿。

        现在抽出导出块的**键名集合**，逐一精确比对。
        """
        m = re.search(r"return\s*\{(.*?)\}\s*;", self.desktop, re.S)
        self.assertIsNotNone(m)
        block = m.group(1)

        # 抽键名：形如 `  foo,` / `  foo: () => {` / `  foo: bar,`
        exported = set(re.findall(r"^\s{4,6}(\w+)\s*[:,]", block, re.M))
        # 简写形式（只有名字，跟逗号）
        exported |= set(re.findall(r"^\s{4,6}(\w+)\s*,", block, re.M))
        self.assertTrue(exported, f"没能从导出块解析出键名：{block[:120]}")

        called = set()
        for ln in self.mobile.splitlines():
            s = ln.strip()
            if s.startswith("//") or s.startswith("*"):
                continue          # 注释里会提到方法名（说明为什么这么写）
            # 同时匹配 `WBDesktop.foo` 与可选链 `WBDesktop?.foo`
            called |= set(re.findall(r"WBDesktop\??\.(\w+)", ln))
        # 只用可选链时也应能抓到（`window.WBDesktop?.checkUpdatesAtStartup?.()`）
        self.assertTrue(
            called,
            "没能从 mobile.js 里解析出任何 WBDesktop 调用 —— "
            "如果确实不再依赖 WBDesktop，可以删除这条测试")

        missing = [name for name in sorted(called)
                   if name not in exported]
        self.assertEqual(
            missing, [],
            f"**mobile.js 调用了不存在的 WBDesktop 方法**：{missing}。"
            f"实际导出的是：{sorted(exported)}。"
            "desktop.js 必须导出它们，否则移动端启动即崩")


class LoadingOverlayCase(unittest.TestCase):
    """引导层的移除必须是第一步（不能排在可能失败的操作之后）。"""

    @classmethod
    def setUpClass(cls):
        cls.mobile = MOBILE_JS.read_text(encoding="utf-8")

    def _initialize_body(self) -> str:
        m = re.search(r"function initialize\(token\)\s*\{(.*?)\n  \}",
                      self.mobile, re.S)
        self.assertIsNotNone(m, "找不到 initialize() 函数")
        return m.group(1)

    def test_local_boot_removed_first(self):
        """**核心**：`localBoot.remove()` 必须排在 `enterApp()` 之前。

        反过来说：任何「可能失败」的调用（enterApp / checkUpdates /
        settings.start）都不能排在它前面。
        """
        body = self._initialize_body()
        lines = [ln.strip() for ln in body.splitlines()
                 if ln.strip() and not ln.strip().startswith("//")]

        idx_remove = next((i for i, ln in enumerate(lines)
                           if "localBoot" in ln and "remove" in ln), None)
        self.assertIsNotNone(idx_remove,
                            "initialize() 里应移除 localBoot 引导层")

        risky = ("enterApp(", "checkUpdatesAtStartup", "WBMobileSettings",
                 "topbarMode")
        for i, ln in enumerate(lines):
            if any(k in ln for k in risky):
                self.assertGreater(
                    i, idx_remove,
                    f"**`{ln[:50]}` 排在移除 localBoot 之前** —— "
                    "它一旦抛异常，引导层就永远不消失，"
                    "界面卡在「正在打开本地工作台…」")

    def test_optional_steps_wrapped_in_try(self):
        """可选步骤必须各自 try（一个失败不该影响进入主界面）。

        ⚠️ 只检查**实际调用行**，不能匹配到注释里的字样 ——
        注释里正好也提到这些方法名（说明为什么这么写），
        直接 `body.find(key)` 会在注释处命中，导致假失败。
        """
        body = self._initialize_body()
        # 先去掉注释行
        code_lines = []
        for ln in body.splitlines():
            s = ln.strip()
            if not s or s.startswith("//"):
                continue
            code_lines.append(ln)
        code = "\n".join(code_lines)

        for key in ("checkUpdatesAtStartup", "WBMobileSettings"):
            # 只看「真的调用」——形如 `X?.key?.()` 或 `X.key(`
            m = re.search(rf"[?.\w]*\b{key}\b\s*\??\(", code)
            if not m:
                continue
            seg = code[max(0, m.start() - 200):m.start()]
            self.assertIn(
                "try", seg,
                f"`{key}` 的实际调用必须包在 try 里 —— 它是可选功能，"
                "失败不该阻断进入主界面")

    def test_uses_optional_chaining(self):
        """调用应该用 `?.`（方法不存在也不抛）。"""
        body = self._initialize_body()
        self.assertRegex(
            body, r"WBDesktop\?\.\w+\?\.\(",
            "应使用 `window.WBDesktop?.checkUpdatesAtStartup?.()` 形式 —— "
            "这样方法不存在时静默跳过而不是抛 TypeError")


class StagedAssetsCase(unittest.TestCase):
    """staging 里的资源和源码必须一致（防「改了源码但没重建」）。"""

    STAGING = ROOT / "android" / "app" / "build" / "generated" / "python"

    def test_staging_matches_source_when_present(self):
        if not self.STAGING.is_dir():
            self.skipTest("staging 未生成（需先跑 build_android.py --prepare-only）")
        pairs = [
            (DESKTOP_JS, self.STAGING / "admin" / "static" / "desktop.js"),
            (MOBILE_JS, self.STAGING / "admin" / "static" / "mobile.js"),
        ]
        for src, dst in pairs:
            if not dst.is_file():
                continue
            self.assertEqual(
                src.read_text(encoding="utf-8"),
                dst.read_text(encoding="utf-8"),
                f"**staging 与源码不一致**：{dst.name}。"
                "改了源码必须重跑 `build_android.py --prepare-only`，"
                "否则 APK 会打进旧版本（这个坑踩过）")


if __name__ == "__main__":
    unittest.main()
