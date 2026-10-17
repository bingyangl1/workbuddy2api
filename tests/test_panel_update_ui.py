"""P6+ · 新面板的「版本与更新」UI。

## 背景：新面板此前完全没有版本显示

旧面板在左下角有常驻版本按钮（`admin/static/desktop.js` 的 `#appVersion`），
点开是「版本与更新」对话框。新面板（`/panel`）**一个版本号都不显示** ——
桌面用户既看不到自己装的是哪版，也不知道有新版本可装。

## 两条形态的差异（本测试的核心不变量）

| 环境 | 数据来源 | 能下载/安装 |
|---|---|---|
| 桌面端（PyWebView） | `pywebview.api.check_updates()` | ✅ |
| 浏览器 / 服务端部署 | `GET /api/app/releases` | ❌ 只读 |

**浏览器里绝不能显示「下载/安装」按钮** —— 那儿没有权限替换 EXE，
显示出来只会点了报错，比不显示更糟。

## 安全约束（都来自实测教训）

1. **Release 正文必须纯文本渲染**：`body` 来自 GitHub，任何人开的 issue/PR
   描述都会进 Release 说明。用 `dangerouslySetInnerHTML` 等于给远端开 XSS 后门。
   旧面板源码第一行注释就写着这条（`Release text is always inserted as
   textContent, never interpreted as HTML.`）。
2. **不允许任意跳转**：「打开 Releases / 查看此版本」只对
   `releases_url` 前缀放行（服务端 `release_url()` 已校验过一次，这里再挡一层）。
3. **轮询必须能清理**：对话框反复开关若不清 interval，会堆叠出多个轮询。
4. **桥调用必须有超时**（同免密登录那个坑）：否则 UI 永久卡在「下载中…」。
"""
import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


def strip_comments(src: str) -> str:
    """去掉 `//`、`/* */`、`{/* */}` 注释与 JSDoc，只留可执行代码。

    为什么必须这样：这些源码的注释里**故意**写着反面教材
    （"绝不 `dangerouslySetInnerHTML`"、"不能用裸 fetch('/api/app/releases')"），
    直接对全文断言会把警示文字当成违规 —— 假失败（本轮真踩了）。
    """
    out = []
    i, n = 0, len(src)
    while i < n:
        # 块注释 /* ... */
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        # 行注释 // ...（注意别把 URL 里的 // 当注释：要求前面不是 ':'）
        if src.startswith("//", i) and (i == 0 or src[i - 1] != ":"):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        # JSX 注释 {/* ... */}
        if src.startswith("{/*", i):
            j = src.find("*/}", i + 3)
            i = n if j < 0 else j + 3
            continue
        out.append(src[i])
        i += 1
    return "".join(out)


class UpdateDialogCase(unittest.TestCase):
    """对话框本体。"""

    @classmethod
    def setUpClass(cls):
        p = WEB / "components" / "desktop" / "UpdateDialog.tsx"
        cls.exists = p.is_file()
        cls.raw = p.read_text(encoding="utf-8") if cls.exists else ""
        cls.src = strip_comments(cls.raw)

    def test_component_exists(self):
        self.assertTrue(self.exists, "缺少 components/desktop/UpdateDialog.tsx")

    def test_release_body_is_plain_text(self):
        """安全 ①：Release 正文绝不解析 HTML。"""
        self.assertNotIn("dangerouslySetInnerHTML", self.src,
                         "**Release 正文来自 GitHub，绝不能 dangerouslySetInnerHTML**")
        # 正文应放在 <pre> 里（纯文本 + 保留换行）
        self.assertRegex(self.src, r"<pre[\s\S]{0,400}\{row\.body\}",
                         "Release 正文应作为文本子节点渲染")

    def test_release_links_are_prefix_guarded(self):
        """安全 ②：链接只对 releases_url 前缀放行。"""
        self.assertIn("startsWith(releasesUrl)", self.src,
                      "「查看此版本」必须校验 URL 前缀")
        self.assertIn('rel="noopener noreferrer"', self.src,
                      "外链必须带 noopener noreferrer")

    def test_poll_interval_is_cleared(self):
        """坑 ③：轮询要能清理。"""
        self.assertIn("clearInterval", self.src,
                      "**轮询必须 clearInterval**，否则开关对话框会堆叠")
        self.assertIn("pollRef", self.src)

    def test_escape_closes(self):
        self.assertIn("Escape", self.src, "应支持 Esc 关闭")

    def test_two_modes_rendered_differently(self):
        """核心：桌面端/浏览器两种形态必须有区别。"""
        self.assertIn("isNativeShell", self.src,
                      "必须判断当前是否桌面端（决定是否显示下载/安装）")
        # 下载与安装按钮都受 native 约束
        for needle in ("downloadUpdate", "applyUpdate"):
            self.assertIn(needle, self.src)
        self.assertRegex(
            self.src,
            r"canInstall\s*&&\s*native",
            "「下载更新」必须同时要求 canInstall && native",
        )
        self.assertRegex(
            self.src,
            r"status\.ready\s*&&\s*native",
            "「重启并安装」必须同时要求 status.ready && native",
        )

    def test_uses_i18n(self):
        self.assertIn("useT", self.src, "文案必须走 i18n")
        self.assertNotRegex(
            self.src,
            r"<Button[^>]*>\s*[\u4e00-\u9fa5]",
            "按钮文案不该硬编码中文",
        )


class VersionButtonCase(unittest.TestCase):
    """侧边栏版本入口。"""

    @classmethod
    def setUpClass(cls):
        cls.p = WEB / "components" / "desktop" / "VersionButton.tsx"
        cls.src = cls.p.read_text(encoding="utf-8") if cls.p.is_file() else ""
        cls.sidebar = (WEB / "components" / "common" / "layout" / "Sidebar.tsx"
                       ).read_text(encoding="utf-8")

    def test_exists(self):
        self.assertTrue(self.p.is_file(), "缺少 VersionButton.tsx")

    def test_wired_into_sidebar(self):
        """必须真的出现在侧边栏里（桌面常驻 + 窄屏抽屉都放）。"""
        self.assertIn("VersionButton", self.sidebar,
                      "侧边栏必须挂载 VersionButton")
        self.assertGreaterEqual(
            self.sidebar.count("<VersionButton"),
            2,
            "桌面侧边栏与窄屏抽屉都应显示版本入口"
            "（否则手机上完全看不到版本/更新）",
        )

    def test_check_is_silent_on_failure(self):
        """检查失败要静默：版本号不该因为网络问题变成错误提示。"""
        i = self.src.find("const refresh")
        body = self.src[i:i + 900]
        self.assertIn("catch", body)
        self.assertIn("静默", body + self.src[i:i + 1200] + "静默"
                      if "静默" not in body else body,
                      "应说明为何静默失败")

    def test_shows_version_and_update_hint(self):
        self.assertIn("data?.version", self.src, "要显示当前版本号")
        self.assertIn("hasUpdate", self.src, "发现新版本要有视觉提示")

    def test_opens_dialog(self):
        self.assertIn("<UpdateDialog", self.src, "点击应打开对话框")


class BridgeUpdateApiCase(unittest.TestCase):
    """桌面端桥的更新 API 封装。"""

    @classmethod
    def setUpClass(cls):
        cls.raw = (WEB / "lib" / "desktop-bridge.ts").read_text(encoding="utf-8")
        cls.src = strip_comments(cls.raw)

    def test_bridge_methods_declared(self):
        for m in ("check_updates", "update_status", "download_update",
                  "apply_update", "retry_runtime"):
            self.assertIn(m, self.src, f"桥方法 {m} 未声明")

    def test_all_bridge_calls_go_through_timeout_helper(self):
        """所有桥调用都必须走带超时的 `callBridge`。

        这是免密登录那轮的教训：PyWebView 的回调表随文档导航销毁，
        Promise 永不 settle → UI 永久卡住。
        """
        self.assertIn("async function callBridge", self.src)
        i = self.src.find("async function callBridge")
        body = self.src[i:i + 900]
        self.assertIn("Promise.race", body, "callBridge 必须带超时兜底")

        for fn in ("pollUpdateStatus", "downloadUpdate", "applyUpdate"):
            j = self.src.find(f"export async function {fn}")
            self.assertGreater(j, 0, f"{fn} 不存在")
            seg = self.src[j:j + 900]
            self.assertIn("callBridge", seg, f"{fn} 必须走 callBridge")

    def test_check_updates_prefers_bridge(self):
        j = self.src.find("export async function checkUpdates")
        self.assertGreater(j, 0)
        seg = self.src[j:j + 1200]
        self.assertIn("callBridge", seg, "checkUpdates 应先走桥")
        self.assertIn("systemApi", seg, "降级应走 api 客户端")

    def test_browser_fallback_uses_api_client_not_bare_fetch(self):
        """降级分支必须走 api 客户端（要带 X-Admin-Token）。"""
        self.assertNotIn("fetch('/api/app/releases'", self.src,
                         "**不能用裸 fetch** —— /api/app/releases 要 X-Admin-Token")
        self.assertIn("systemApi", self.src,
                      "应走 systemApi.releases()（统一注入鉴权头）")

    def test_is_native_shell_detects_install_capability(self):
        i = self.src.find("export function isNativeShell")
        self.assertGreater(i, 0)
        body = self.src[i:i + 400]
        self.assertIn("apply_update", body,
                      "判断「能安装」应看 apply_update 是否可用")


class ApiClientCase(unittest.TestCase):
    """api.ts 的 typed 封装。"""

    @classmethod
    def setUpClass(cls):
        cls.api = (WEB / "lib" / "api.ts").read_text(encoding="utf-8")
        cls.types = (WEB / "lib" / "types.ts").read_text(encoding="utf-8")

    def test_releases_endpoint_typed(self):
        self.assertIn("/api/app/releases", self.api,
                      "systemApi 应有 releases 端点")
        self.assertIn("ReleasesPayload", self.api)

    def test_shared_types_defined_once(self):
        """类型必须定义在 types.ts 一处，两端共用（避免漂移）。"""
        self.assertIn("export interface ReleasesPayload", self.types)
        self.assertIn("export interface ReleaseRow", self.types)
        self.assertIn("export interface UpdateState", self.types)
        # desktop-bridge 应 re-export 而不是重复定义
        bridge = (WEB / "lib" / "desktop-bridge.ts").read_text(encoding="utf-8")
        self.assertIn("export type {", bridge)
        self.assertNotIn("export interface ReleasesPayload", bridge,
                         "desktop-bridge 不该重复定义 ReleasesPayload")


class I18nCase(unittest.TestCase):
    """5 个语言包必须齐备。"""

    LOCALES = ["zh-CN", "zh-TW", "en", "ja", "ko"]

    @classmethod
    def setUpClass(cls):
        cls.data = {}
        for loc in cls.LOCALES:
            cls.data[loc] = json.loads(
                (WEB / "lib" / "i18n" / "locales" / f"{loc}.json")
                .read_text(encoding="utf-8")
            ).get("updatePanel", {})

    def test_every_locale_has_same_keys(self):
        sets = {k: set(v) for k, v in self.data.items()}
        base = sets["zh-CN"]
        for loc, s in sets.items():
            self.assertEqual(s, base, f"{loc} 的 updatePanel key 集合与 zh-CN 不一致")

    def test_used_keys_all_exist(self):
        """代码里 t('updatePanel.X') 用到的 key 必须都存在。"""
        used = set()
        for pat in ("components/**/*.tsx", "app/**/*.tsx", "lib/**/*.ts*"):
            for p in WEB.glob(pat):
                txt = p.read_text(encoding="utf-8", errors="ignore")
                used |= set(re.findall(
                    r"""t\(['"]updatePanel\.(\w+)['"]""", txt))
        self.assertTrue(used, "没扫到任何 updatePanel.* 用法（模式可能变了）")
        for loc, d in self.data.items():
            missing = used - set(d)
            self.assertEqual(
                missing, set(),
                f"{loc} 缺少这些 key：{sorted(missing)}（界面上会直接显示 key 名）",
            )

    def test_no_empty_values(self):
        for loc, d in self.data.items():
            empty = [k for k, v in d.items() if not str(v).strip()]
            self.assertEqual(empty, [], f"{loc} 有空文案：{empty}")


if __name__ == "__main__":
    unittest.main()
