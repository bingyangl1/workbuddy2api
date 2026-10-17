"""P6 · 桌面端接入新面板 —— 免密登录与落点可配。

## 背景：为什么这是一个真实阻塞项

桌面版 EXE 是本机单用户程序，「本地所有权」本身就是鉴权 —— 用户不该再输
一遍用户名密码。旧面板靠 `admin/static/desktop.js` 做这件事：

    const {token} = await pywebview.api.desktop_login()
    localStorage.setItem(TOKEN_KEY, token)

而 `desktop_login` 的后端实现**硬编码只认旧面板**：

    if self.window.get_current_url() != f"http://127.0.0.1:{port}/admin?desktop=1":
        raise RuntimeError("只允许本地桌面界面登录")

于是切到新面板（`/panel`）后：
* 桌面端打开 `/panel` → 停在登录页 → 加账号等功能全部不可用
* 同样代码**在浏览器里输密码却正常** → 极易误判成新面板的 bug

## P6 改的三件事

### ① `desktop_login` 的 URL 白名单
从「精确等于 `/admin?desktop=1`」改成「**回环 + 本进程端口 + 路径前缀白名单**」
（`/admin` 或 `/panel`）。安全边界不放松：
* 必须 127.0.0.1 / localhost / ::1
* 端口必须是本进程的端口
* 路径必须命中白名单（允许尾随斜杠与子路径）

### ② 启动落点可配
`load_url(".../admin?desktop=1")` 硬编码 → `_landing_url()`，按
`ADMIN_DEFAULT_PANEL` 决定（legacy → `/admin?desktop=1`；next → `/panel?desktop=1`）。

### ③ 新面板的免密引导
新增 `web/lib/desktop-bridge.ts` + 接进 `AuthProvider`：
桌面端打开时自动 `pywebview.api.desktop_login()` 换 token；失败**退化成
登录框**（绝不把面板打成错误页）。

## 安全说明（本测试的重点之一）

只判「有没有 `window.pywebview`」是不够的 —— **网页里也能伪造它**。
所以真正的闸门是后端那次 URL 校验；前端的判断只是「不要白跑一趟」。
本测试对后端的边界条件做了完整覆盖（伪造域名/端口/路径都必须被拒）。
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent


class DesktopLoginAllowlistCase(unittest.TestCase):
    """① `desktop_login` 的 URL 白名单与安全边界。"""

    def setUp(self):
        from desktop.app import DesktopAPI
        self.port = 8790
        # 只构造实例，不启动任何东西
        self.api = DesktopAPI.__new__(DesktopAPI)
        self.api.port = self.port
        self.api.window = mock.MagicMock()
        self.api.config = {"username": "desktop", "jwt_secret": "x" * 48}

    def _url(self, url):
        self.api.window.get_current_url.return_value = url
        return url

    # ── 允许的 ──
    def test_allows_legacy_panel(self):
        self._url(f"http://127.0.0.1:{self.port}/admin?desktop=1")
        self.assertTrue(self.api._desktop_login_allowed())

    def test_allows_new_panel(self):
        """**核心**：新面板必须允许 —— 这是 P6 要修的那条。"""
        self._url(f"http://127.0.0.1:{self.port}/panel?desktop=1")
        self.assertTrue(self.api._desktop_login_allowed(),
                        "**新面板必须允许免密登录** —— 否则桌面版停在登录页")

    def test_allows_new_panel_subpages(self):
        """新面板是多页应用（/panel/accounts、/panel/settings …）。"""
        for path in ("/panel/", "/panel/accounts", "/panel/settings/layout",
                     "/panel/models/catalog", "/admin/"):
            self._url(f"http://127.0.0.1:{self.port}{path}")
            self.assertTrue(self.api._desktop_login_allowed(),
                            f"{path} 应被允许")

    def test_allows_localhost_alias(self):
        self._url(f"http://localhost:{self.port}/panel")
        self.assertTrue(self.api._desktop_login_allowed())

    def test_allows_ipv6_loopback(self):
        self._url(f"http://[::1]:{self.port}/panel")
        self.assertTrue(self.api._desktop_login_allowed())

    # ── 必须拒绝的（安全边界） ──
    def test_rejects_external_host(self):
        for host in ("evil.com", "192.168.1.50", "10.0.0.1", "127.0.0.1.evil.com"):
            self._url(f"http://{host}:{self.port}/panel")
            self.assertFalse(self.api._desktop_login_allowed(),
                             f"{host} 不是回环地址，必须拒绝")

    def test_rejects_other_port(self):
        self._url("http://127.0.0.1:9999/panel")
        self.assertFalse(self.api._desktop_login_allowed(),
                         "别的端口不是本进程的界面，必须拒绝")

    def test_rejects_non_panel_path(self):
        for path in ("/", "/v1/chat/completions", "/api/accounts",
                     "/panelx", "/adminx"):
            self._url(f"http://127.0.0.1:{self.port}{path}")
            self.assertFalse(self.api._desktop_login_allowed(),
                             f"{path} 不在白名单前缀内，必须拒绝")

    def test_rejects_when_window_gone(self):
        self.api.window.get_current_url.side_effect = RuntimeError("window destroyed")
        self.assertFalse(self.api._desktop_login_allowed())

    def test_rejects_weird_scheme(self):
        for url in ("file:///C:/panel.html", "javascript:alert(1)",
                    "about:blank", ""):
            self._url(url)
            self.assertFalse(self.api._desktop_login_allowed(),
                             f"{url!r} 不该被允许")

    # ── desktop_login 的行为 ──
    def test_issues_token_for_allowed_url(self):
        self._url(f"http://127.0.0.1:{self.port}/panel?desktop=1")
        out = self.api.desktop_login()
        self.assertIn("token", out)
        self.assertTrue(out["token"])

    def test_raises_for_disallowed_url(self):
        self._url("http://evil.com/panel")
        with self.assertRaises(RuntimeError):
            self.api.desktop_login()

    def test_not_ready_message_when_port_unset(self):
        """后台还没起来时要报「稍候」，而不是「不允许登录」。"""
        self.api.port = 0
        with self.assertRaises(RuntimeError) as ctx:
            self.api.desktop_login()
        msg = str(ctx.exception)
        self.assertIn("稍候", msg,
                      f"文案应提示稍候而不是权限问题，实际：{msg}")


class LandingUrlCase(unittest.TestCase):
    """② 启动落点按 ADMIN_DEFAULT_PANEL 决定。"""

    def setUp(self):
        from desktop.app import DesktopAPI
        self.api = DesktopAPI.__new__(DesktopAPI)
        self.api.port = 8790

    def _landing(self, **env):
        keys = ("ADMIN_DEFAULT_PANEL", "ADMIN_PANEL_PATH")
        old = {k: os.environ.get(k) for k in keys}
        try:
            for k in keys:
                os.environ.pop(k, None)
            os.environ.update({k: v for k, v in env.items() if v is not None})
            return self.api._landing_url()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_default_is_legacy(self):
        url = self._landing()
        self.assertEqual(urlparse(url).path, "/admin")
        self.assertEqual(urlparse(url).query, "desktop=1")

    def test_next_goes_to_panel(self):
        url = self._landing(ADMIN_DEFAULT_PANEL="next")
        self.assertEqual(urlparse(url).path, "/panel",
                         "next 应落到新面板")
        self.assertEqual(urlparse(url).query, "desktop=1",
                         "必须带 desktop=1（前端据此走免密引导）")

    def test_custom_panel_path(self):
        url = self._landing(ADMIN_DEFAULT_PANEL="next", ADMIN_PANEL_PATH="ui")
        self.assertEqual(urlparse(url).path, "/ui",
                         "自定义路径要补上前导斜杠")

    def test_always_loopback(self):
        for panel in ("legacy", "next"):
            url = self._landing(ADMIN_DEFAULT_PANEL=panel)
            self.assertEqual(urlparse(url).hostname, "127.0.0.1",
                             "桌面端只该连回环")
            self.assertEqual(urlparse(url).port, 8790)


class LandingWiredIntoStartupCase(unittest.TestCase):
    """启动流程必须用 `_landing_url()`（不能还硬编码 /admin）。"""

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "desktop" / "app.py").read_text(encoding="utf-8")

    def test_startup_uses_landing_url(self):
        self.assertIn("load_url(self._landing_url())", self.src,
                      "启动应调用 _landing_url()")
        self.assertNotIn('load_url(f"http://127.0.0.1:{self.port}/admin?desktop=1")',
                         self.src,
                         "不该再有硬编码的 /admin 落点")


class DesktopEnvDefaultsCase(unittest.TestCase):
    """③ 桌面端默认进新面板（但显式设置要优先）。"""

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "desktop" / "app.py").read_text(encoding="utf-8")

    def test_sets_panel_env(self):
        i = self.src.find("def configure_environment")
        body = self.src[i:i + 2600]
        for key, val in (("ADMIN_PANEL_STATIC", "1"),
                         ("ADMIN_PANEL_PATH", "/panel"),
                         ("ADMIN_DEFAULT_PANEL", "next")):
            self.assertIn(f'setdefault("{key}", "{val}")', body,
                          f"桌面端应默认设置 {key}={val}")

    def test_uses_setdefault_not_override(self):
        """必须用 setdefault —— 运维显式配置不能被桌面端覆盖。"""
        i = self.src.find("def configure_environment")
        body = self.src[i:i + 2600]
        for key in ("ADMIN_PANEL_STATIC", "ADMIN_PANEL_PATH", "ADMIN_DEFAULT_PANEL"):
            self.assertNotIn(f'"{key}": ', body,
                             f"{key} 不该用 os.environ.update 覆盖（应用 setdefault）")


class FrontendBridgeCase(unittest.TestCase):
    """③ 新面板的前端免密引导。

    ## 这里钉住的都是**实测踩过的坑**（不是凭想象写的规范）

    调试过程中有三个「看起来没问题、实际完全不工作」的实现，都已写入断言：

    1. **不能靠 `?desktop=1` 判断** —— `/panel?desktop=1` 未登录时会重定向到
       `/panel/login/`，而 Next 静态导出的重定向**不保留 query**。
       靠 URL 判断 → 桌面端永远认不出自己是桌面端。

    2. **桥调用必须加超时** —— PyWebView 的 js_api 回调依赖「发起调用那个文档」
       的 `_returnValuesCallbacks` 表。面板加载会经历 `/panel` → `/panel/login/`
       的导航，回调表随文档销毁 → Promise **永不 settle** → `await` 挂死，
       后面写 token、关 loading 全都不执行。
       表现极具迷惑性：桥是 function、手动调用能成功，但 React 那段代码
       「看起来没跑」。

    3. **失败不能提前放弃** —— 早先有「几次拿不到桥就 return null」的优化，
       但 PyWebView 的桥可能晚于 4.5 秒才注入（要等后端起 uvicorn +
       窗口导航 + WebView2 注入），正常桌面端会被误判成「不是桌面端」。
    """

    @classmethod
    def setUpClass(cls):
        p = ROOT / "web" / "lib" / "desktop-bridge.ts"
        cls.exists = p.is_file()
        cls.src = p.read_text(encoding="utf-8") if cls.exists else ""

    def test_bridge_module_exists(self):
        self.assertTrue(self.exists, "缺少 web/lib/desktop-bridge.ts")

    def test_does_not_rely_on_query_flag(self):
        """坑 1：判据必须是「桥存在」，不能只看 query。

        断言限定在 `isDesktopLaunch` **函数体内部**（用 `}` 收尾切段）——
        早先直接搜全文，结果匹配到了下面 `hasDesktopBridge` 自己的定义，
        删掉真实调用后测试仍然通过（反向验证才发现）。
        """
        i = self.src.find("export function isDesktopLaunch")
        self.assertGreater(i, 0)
        # 取函数体：从函数起点到下一个顶层 `}` 后紧跟空行/注释的位置
        j = self.src.find("\n}\n", i)
        body = self.src[i:j + 2]
        self.assertIn("hasDesktopBridge()", body,
                      "isDesktopLaunch 的函数体里必须优先看桥是否存在"
                      "（query 会被静态导出的重定向剥掉）")
        # 反向断言：桥判断必须在 query 判断之前
        self.assertLess(body.find("hasDesktopBridge()"),
                        body.find("URLSearchParams"),
                        "桥判断应在 query 判断之前")

    def test_waits_for_bridge_ready(self):
        self.assertIn("pywebviewready", self.src,
                      "必须监听 pywebviewready 事件")
        self.assertIn("setInterval", self.src,
                      "还必须轮询兜底 —— 事件可能早于监听器就已派发")

    def test_bridge_call_has_timeout(self):
        """坑 2：桥调用必须有超时，否则 Promise 永不 settle。"""
        self.assertIn("Promise.race", self.src,
                      "**桥调用必须加超时**（回调表随文档销毁 → 永不 settle）")
        i = self.src.find("Promise.race")
        self.assertIn("setTimeout", self.src[i:i + 500],
                      "race 的另一路应是 setTimeout 兜底")

    def test_never_throws(self):
        """引导失败必须返回 null，不能抛（否则面板变错误页）。"""
        i = self.src.find("export async function requestDesktopToken")
        body = self.src[i:]
        self.assertIn("catch", body, "requestDesktopToken 必须捕获异常")
        self.assertIn("return null", body)

    def test_no_early_giveup(self):
        """坑 3：不能「几次没桥就放弃」。"""
        i = self.src.find("export async function ensureDesktopToken")
        body = self.src[i:i + 1400]
        self.assertNotIn("hasDesktopBridge() && i >=", body,
                         "**不能有「几次失败就放弃」的提前返回**")

    def test_auth_context_wired(self):
        """AuthProvider 必须真的调用它。"""
        p = ROOT / "web" / "lib" / "auth-context.tsx"
        src = p.read_text(encoding="utf-8")
        self.assertIn("desktop-bridge", src, "AuthProvider 应导入 desktop-bridge")
        self.assertIn("ensureDesktopToken", src, "AuthProvider 应调用免密引导")

    def test_login_page_also_bootstraps(self):
        """登录页是稳定页面，最可靠的重试点 —— 必须也做一次。"""
        p = ROOT / "web" / "app" / "(auth)" / "login" / "page.tsx"
        src = p.read_text(encoding="utf-8")
        self.assertIn("ensureDesktopToken", src,
                      "登录页也要尝试免密（那里不会导航，回调表不会被销毁）")


class SmokeTestBothPanelsCase(unittest.TestCase):
    """冒烟测试必须同时支持两种面板。

    ## 为什么（这是一条真实回归）

    P6 把桌面端默认落点从 `/admin` 改成 `/panel`，但冒烟测试仍只断言旧面板的
    `#app` / `#appVersion` —— 新面板是 React 应用，没有这些 id。

    后果：**release 工作流的冒烟测试会永远失败**，而本地手动打开一切正常。
    这种「只在 CI 挂」的故障最难查。

    原测试甚至写了一条反向断言「冒烟测试不该断言新面板」—— 那是**错的**，
    它把「没跟上改动」当成了正确行为。已改为要求同时兼容两种。
    """

    @classmethod
    def setUpClass(cls):
        import re
        src = (ROOT / "desktop" / "app.py").read_text(encoding="utf-8")
        i = src.find("def _smoke_test")
        # 取到下一个顶层 def 为止（固定长度会把 mode="modern" 截掉，踩过）
        if i > 0:
            m = re.search(r"\n    def ", src[i + 20:])
            end = i + 20 + m.start() if m else len(src)
            cls.body = src[i:end]
        else:
            cls.body = ""

    def test_checks_legacy_panel(self):
        """旧面板：保持原判据（#app 可见 + #appVersion 含版本号）。"""
        self.assertIn("appVersion", self.body,
                      "旧面板的判据应保留（legacy 构建仍要能冒烟）")
        self.assertIn("classList.contains('hidden')", self.body)

    def test_checks_modern_panel(self):
        """新面板：必须有自己的判据，否则 next 构建冒烟必挂。"""
        self.assertIn("__reactFiber", self.body,
                      "新面板判据应检查 React 是否已挂载")
        self.assertIn("innerText", self.body,
                      "新面板判据应检查页面有实际内容")

    def test_modern_does_not_require_login(self):
        """**冒烟测试不该要求已登录**（这是实测踩出来的）。

        最初写的判据是「已离开 /login + 文本 > 200」，实测**永远失败**：
        冒烟跑在全新数据目录里，没有账号，免密登录后也停在
        `/panel/login/`（94 字符）。

        登录是用户数据的事，不是「构建是否可用」的事。
        """
        # 判据里不该出现「路径含 /login 就判失败」
        self.assertNotIn("pathname.indexOf('/login')>=0) return false",
                         self.body,
                         "冒烟不该因为停在登录页就判失败"
                         "（空数据目录里本来就没有登录态）")

    def test_modern_threshold_is_reachable(self):
        """内容阈值必须低于登录页的实际字符数（实测 94）。"""
        import re
        m = re.search(r"t\.length\s*<\s*(\d+)", self.body)
        self.assertIsNotNone(m, "应能从判据里取到文本长度阈值")
        threshold = int(m.group(1))
        self.assertLess(threshold, 90,
                        f"阈值 {threshold} 应低于登录页实测的 94 字符，"
                        "否则空数据目录下冒烟永远失败")

    def test_each_eval_is_guarded(self):
        """每次 evaluate_js 都必须单独兜异常（导航会销毁 js_api 回调表）。"""
        self.assertIn("_safe_eval", self.body,
                      "**必须用 _safe_eval** —— 面板导航时 evaluate_js 会抛异常，"
                      "不兜住就一次抖动判失败，直接卡住 release")

    def test_reports_which_panel(self):
        """结果里要写明走的是哪个分支（排障时需要知道）。"""
        self.assertIn('"legacy"', self.body)
        self.assertIn('"modern"', self.body)

    def test_both_paths_keep_version_field(self):
        """两种分支都要带上版本信息，便于核对。"""
        self.assertIn('"version": VERSION', self.body)


if __name__ == "__main__":
    unittest.main()
