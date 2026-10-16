"""导航单一数据源守护（防「登记了但不存在」的死链）。

## 为什么需要这条测试

P3 逐页做 parity 时，页面本身都对（21 个全部可访问），但**导航索引是从
workbuddy-manager 抄来的骨架**，里面带着 9 条本引擎不存在的页面
（`/tasks`、`/red-packets`、`/settings/upstream|proxy|models|users|tokens|system|about`）。
逐页验收只验了「直接访问 URL 是否正常」，**没人从菜单点一遍**，所以死链一直没暴露；
一旦接上侧边栏/⌘K，用户点进去就是 404。

这条测试把「登记」与「存在」钉在一起：
  1. 导航清单（`lib/app-nav.ts`）里的每个 href 都必须在 `app/(main)/` 下有对应 page.tsx
  2. `app/(main)/` 下的每个页面都必须在导航清单里（否则用户到不了）
  3. `lib/section-nav.ts`（页内 Tab）与 `lib/command-palette.ts` 的 href 也必须真实存在
  4. 不得出现已知的 manager 遗留死链

运行：`python -m pytest tests/test_nav_single_source.py -q`
"""
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WEB = Path(__file__).resolve().parent.parent / "web"
APP_MAIN = WEB / "app" / "(main)"
APP_AUTH = WEB / "app" / "(auth)"

#: manager 骨架遗留、本引擎**不存在**的页面。曾经出现在导航索引里，必须不再出现。
KNOWN_DEAD = (
    "/tasks", "/red-packets",
    "/settings/upstream", "/settings/proxy", "/settings/models",
    "/settings/users", "/settings/tokens", "/settings/system", "/settings/about",
)


def _page_routes() -> set[str]:
    """扫 `app/**/page.tsx` 得到真实路由集合（不含 basePath）。"""
    routes: set[str] = set()
    for root in (APP_MAIN, APP_AUTH):
        if not root.is_dir():
            continue
        for p in root.rglob("page.tsx"):
            rel = p.parent.relative_to(root).as_posix()
            routes.add("/" if rel == "." else "/" + rel)
    return routes


def _hrefs_in(path: Path) -> set[str]:
    """从某个 TS 文件里抽 `href: '/xxx'` 字面量。"""
    src = path.read_text(encoding="utf-8")
    return set(re.findall(r"href:\s*'(/[^']*)'", src))


class NavSingleSourceCase(unittest.TestCase):
    def test_all_nav_hrefs_exist(self):
        """导航清单里的每个 href 都要有真实页面。"""
        routes = _page_routes()
        hrefs = _hrefs_in(WEB / "lib" / "app-nav.ts")
        self.assertTrue(hrefs, "app-nav.ts 里没解析到任何 href（文件结构变了？）")
        missing = sorted(h for h in hrefs if h not in routes)
        self.assertEqual(missing, [], f"导航登记了不存在的页面（会 404）：{missing}")

    def test_all_pages_are_in_nav(self):
        """每个真实页面都要能从侧边栏到达（否则用户找不到）。"""
        routes = _page_routes() - {"/login"}   # 登录页不需要在菜单里
        hrefs = _hrefs_in(WEB / "lib" / "app-nav.ts")
        unreachable = sorted(r for r in routes if r not in hrefs)
        self.assertEqual(unreachable, [], f"这些页面不在导航清单里（用户到不了）：{unreachable}")

    def test_section_nav_hrefs_exist(self):
        """页内二级 Tab 的 href 也要真实存在。"""
        routes = _page_routes()
        hrefs = _hrefs_in(WEB / "lib" / "section-nav.ts")
        missing = sorted(h for h in hrefs if h not in routes)
        self.assertEqual(missing, [], f"页内 Tab 指向不存在的页面：{missing}")

    def test_command_palette_hrefs_exist(self):
        """⌘K 命令面板的 href 也要真实存在（它现在从 app-nav 派生，此处兜底）。"""
        routes = _page_routes()
        palette = WEB / "lib" / "command-palette.ts"
        src = palette.read_text(encoding="utf-8")
        # 派生版：list 来自 app-nav，直接检查 NAV_KEYWORDS 的键
        m = re.search(r"NAV_KEYWORDS: Record<string, string> = \{(.*?)\n\};", src, re.S)
        if m:
            keys = set(re.findall(r"'(/[^']*)':", m.group(1)))
            missing = sorted(k for k in keys if k not in routes)
            self.assertEqual(missing, [], f"⌘K 关键词表里有不存在的页面：{missing}")
        else:
            # 回退：老式手写数组
            missing = sorted(h for h in _hrefs_in(palette) if h not in routes)
            self.assertEqual(missing, [], f"⌘K 指向不存在的页面：{missing}")

    def test_no_known_dead_links(self):
        """已知的 manager 遗留死链不得再出现在任何导航文件里。"""
        files = [
            WEB / "lib" / "app-nav.ts",
            WEB / "lib" / "section-nav.ts",
            WEB / "lib" / "command-palette.ts",
        ]
        found: list[str] = []
        for f in files:
            src = f.read_text(encoding="utf-8")
            for dead in KNOWN_DEAD:
                # 只匹配「当作 href 用」的形态，避免误伤注释里的说明
                if f"href: '{dead}'" in src or f"'{dead}':" in src:
                    found.append(f"{f.name}: {dead}")
        self.assertEqual(found, [], f"导航里仍有死链：{found}")

    def test_sidebar_wired_into_layout(self):
        """侧边栏必须真的接进 (main)/layout —— 否则「有菜单」只是文件存在。"""
        layout = (WEB / "app" / "(main)" / "layout.tsx").read_text(encoding="utf-8")
        self.assertIn("Sidebar", layout, "layout 没渲染 Sidebar")
        self.assertIn("<Sidebar", layout, "layout 没渲染 <Sidebar />")
        self.assertIn("MobileNav", layout, "layout 没渲染窄屏导航")

    def test_nav_uses_only_data_file(self):
        """侧边栏必须从 app-nav 取数据（而不是自己写一份清单）。"""
        sidebar = (WEB / "components" / "common" / "layout" / "Sidebar.tsx").read_text(encoding="utf-8")
        self.assertIn("from '@/lib/app-nav'", sidebar, "Sidebar 没有从 app-nav 取数据")

    def test_mobile_drawer_uses_portal(self):
        """窄屏抽屉必须 portal 到 body。

        踩过的坑：抽屉渲染在 `<header>`（sticky + backdrop-blur）内部时，
        `position: fixed; inset: 0` 的高度会退化成 header 的高度 —— 实测只有
        52px 高（视口是 843px），菜单项全被压在滚动区外看不见，而
        「菜单项数量」的断言仍然通过（DOM 里有 21 项），所以是个**假绿陷阱**。
        portal 到 body 后高度恢复视口高。
        """
        sidebar = (WEB / "components" / "common" / "layout" / "Sidebar.tsx").read_text(encoding="utf-8")
        self.assertIn("createPortal", sidebar, "抽屉没有用 createPortal（fixed 高度会塌）")
        self.assertIn("document.body", sidebar, "portal 目标必须是 document.body")

    def test_sidebar_no_double_basepath(self):
        """侧边栏用 <Link>（自动加 basePath）时不得再手动 withBasePath。

        踩过的坑：basePath=/panel 下 `<Link href={withBasePath('/x')}>` 会变成
        `/panel/panel/x` —— 所有菜单项都跳 404。原生 `<a>` 才需要手动加。
        """
        sidebar = (WEB / "components" / "common" / "layout" / "Sidebar.tsx").read_text(encoding="utf-8")
        self.assertIn("next/link", sidebar, "预期使用 next/link")
        self.assertNotIn("withBasePath(item.href)", sidebar,
                         "用 <Link> 时手动加 basePath 会导致双前缀（/panel/panel/...）")


if __name__ == "__main__":
    unittest.main()
