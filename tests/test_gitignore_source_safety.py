"""`.gitignore` 不能误伤源码文件。

## 这条测试的由来（一个非常隐蔽的真实缺陷）

`.gitignore` 里原本写着：

    logs/

git 的这条规则会匹配**任意深度**的同名目录 —— 于是新面板的日志页面
`web/app/(main)/logs/page.tsx` **也被忽略掉了**，从来没进过仓库。

后果：

* 别人克隆下来，导航里点 `/logs` 直接 404
* 本地开发机上文件明明在（磁盘上有，只是 git 没跟踪）
* **只有 CI 才暴露** —— 它从干净 clone 开始跑，
  `test_nav_single_source` 发现「导航登记的页面不存在」

这个 bug 存在了很久却没被发现，因为本地永远看不出问题。

## 修法

改成 `/logs/`（只匹配根目录的运行日志），并把页面加进仓库。

## 本测试守什么

* 那个页面必须**真的被 git 跟踪**（不是只在磁盘上）
* `.gitignore` 里不能有会匹配到 `web/app/**` 的宽泛规则
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
GITIGNORE = ROOT / ".gitignore"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(ROOT),
                          capture_output=True).stdout.decode("utf-8", "ignore")


class GitignoreCase(unittest.TestCase):
    def test_logs_page_tracked(self):
        """**核心**：日志页面必须真的在仓库里。

        只看磁盘上有没有是不够的 —— 本地一定「有」，所以这个断言
        必须问 git 是否跟踪。
        """
        page = "web/app/(main)/logs/page.tsx"
        self.assertTrue((ROOT / page).is_file(), f"页面对应文件不存在：{page}")
        tracked = _git("ls-files", "--", page).strip()
        self.assertEqual(tracked.replace("\\", "/"), page,
                         f"**{page} 没被 git 跟踪** —— 克隆下来会 404。"
                         "多半是被 .gitignore 的宽泛规则误伤（如裸 `logs/`）")

    def test_no_bare_dir_ignore(self):
        """不能有**裸目录名**规则（会匹配任意深度，误伤 web/app 下的同名目录）。

        ## 什么叫「裸」

            裸（危险）：  logs/          ← 匹配任意深度的 logs/ 目录
            有前缀（安全）：android/app/build/   ← 只在那个位置匹配
            根锚定（安全）：/logs/        ← 只匹配根目录

        真正的判据是「这条规则里**有没有路径分隔符**（除结尾那个）」：
        只有 `logs/` 这种单段目录名才可能误伤到别处的同名目录。
        """
        text = GITIGNORE.read_text(encoding="utf-8")
        bad = []
        for n, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#") or s.startswith("!"):
                continue
            if not s.endswith("/"):
                continue                      # 不是目录规则
            if s.startswith("/"):
                continue                      # 已根锚定
            if "*" in s or "?" in s:
                continue                      # 通配规则，单独看
            body = s.rstrip("/")
            if "/" in body:
                continue                      # 带路径前缀 → 指向明确，安全
            # 到这里就是裸目录名（单段）
            if body in {"node_modules", "__pycache__", ".next", "dist",
                        "build", ".pytest_cache", ".gradle", ".venv",
                        "venv", "env", ".eggs", ".idea", ".vscode", "test",
                        "tests", "docs", "_relkey", "web-wip",
                        ".desktop-build",
                        # 本项目特有的根级工具/缓存目录（不会与源码撞名）：
                        # .workbuddy/ 是客户端数据目录，_zcode_probe/ 是探针临时目录
                        ".workbuddy", "_zcode_probe"}:
                continue                      # 已知的产物/工具目录，不可能撞源码
            bad.append(f"L{n}: {s}")
        self.assertEqual(
            bad, [],
            "这些**裸目录名**规则会匹配任意深度的同名目录，可能误伤源码："
            f"{bad}。请改成 `/name/`（只匹配根目录）或加路径前缀。")

    def test_logs_rule_is_root_anchored(self):
        """`logs/` 必须是 `/logs/`（根锚定）。"""
        text = GITIGNORE.read_text(encoding="utf-8")
        lines = [ln.strip() for ln in text.splitlines()]
        self.assertNotIn("logs/", lines,
                         "**不能留裸 `logs/`** —— 它会忽略 web/app/(main)/logs/")
        self.assertIn("/logs/", lines, "应保留根锚定的 `/logs/`")

    def test_all_nav_pages_tracked(self):
        """导航里登记的所有页面都必须被 git 跟踪。

        这是上一条的泛化版本：不只 logs，任何页面被误忽略都要抓出来。
        """
        import re
        nav = (ROOT / "web" / "lib" / "app-nav.ts").read_text(encoding="utf-8")
        hrefs = set(re.findall(r"href:\s*'(/[^']*)'", nav))
        self.assertTrue(hrefs, "没解析到导航条目")

        app_main = ROOT / "web" / "app" / "(main)"
        missing_tracked = []
        for href in sorted(hrefs):
            rel = href.strip("/")
            page = app_main / rel / "page.tsx" if rel else \
                app_main / "page.tsx"
            if not page.is_file():
                continue        # 路由不存在，由 test_nav_single_source 管
            rel_git = page.relative_to(ROOT).as_posix()
            if _git("ls-files", "--", rel_git).strip() == "":
                missing_tracked.append(f"{href} -> {rel_git}")
        self.assertEqual(
            missing_tracked, [],
            "这些页面在磁盘上但**没被 git 跟踪**（克隆后会 404）："
            f"{missing_tracked}")


if __name__ == "__main__":
    unittest.main()
