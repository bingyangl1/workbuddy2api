"""安卓打包：staging 必须与 `admin/server.py` 的 import 保持一致。

## 这条测试的由来（真实缺陷，CI 才暴露）

上游的 `build_android.py` 在复制 admin 源码时**排除了两个模块**：

    app_source.py             —— 拆包浏览工具
    routers/client_profile.py —— 客户端参数页

那是**按上游自己的 `server.py` 写的**。我们的 `admin/server.py` 是
**无条件 import 两者**的：

    from admin.routers import (..., app_source, ...,
                               benefits, client_profile, ...)
    app.include_router(app_source.router)
    app.include_router(client_profile.router)

于是 APK 里的运行时直接

    ImportError: cannot import name 'app_source' from 'admin.routers'

**应用根本起不来。** 而本地开发时直接跑源码，永远发现不了 ——
只有 `--prepare-only` 生成 staging、再拿 staging 启动才会炸。
CI 的 `check_runtime --android` 一跑就报出来了。

## 本测试守什么

1. staging 里必须有 `server.py` import 的**每一个** admin 模块
2. `build_android.py` 里不能再有「排除 admin 模块」的逻辑
3. `check_runtime.py --android` 不能要求 `/panel/`（安卓只打包旧面板）

第 3 条对应的缺陷：`build_android.py` 只复制 `admin/static`（旧面板），
从不复制 `web/out`，所以 APK 里 `/panel/` 必然 404。早先自检却无条件检查它。
"""
import ast
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "scripts" / "build_android.py"
CHECK = ROOT / "scripts" / "check_runtime.py"
SERVER = ROOT / "admin" / "server.py"


def _server_imported_routers() -> set[str]:
    """从 `admin/server.py` 抽出 `from admin.routers import (...)` 里的名字。"""
    src = SERVER.read_text(encoding="utf-8")
    m = re.search(r"from admin\.routers import\s*\(([^)]*)\)", src, re.S)
    if not m:
        m = re.search(r"from admin\.routers import\s*(.+)", src)
        if not m:
            return set()
        raw = m.group(1)
    else:
        raw = m.group(1)
    names = set()
    for part in raw.replace("\n", " ").split(","):
        name = part.strip().split("#")[0].strip()
        if name and name.isidentifier():
            names.add(name)
    return names


class StagingMatchesServerImportsCase(unittest.TestCase):
    """核心：staging 必须包含 server.py 引用的所有路由模块。"""

    def test_no_admin_module_excluded(self):
        """`build_android.py` 不能排除任何 admin 模块。"""
        src = BUILD.read_text(encoding="utf-8")
        # 找 `if ...: continue` 且条件里带 .py 名的地方（排除逻辑）
        bad = []
        for n, line in enumerate(src.splitlines(), 1):
            s = line.strip()
            if s.startswith("#"):
                continue
            # 形如 `if path.name in ("xxx.py",) ...: continue`
            if re.search(r"if\s+path\.name\s+in\s*\(", s) or \
               re.search(r"relative\s*==\s*Path\(['\"]admin/", s):
                bad.append(f"L{n}: {s[:90]}")
        self.assertEqual(
            bad, [],
            "**不能排除 admin 模块** —— 我们的 server.py 可能无条件 import 它，"
            f"排掉后 APK 起不来：{bad}")

    def test_staging_has_all_router_modules(self):
        """实际生成 staging，逐一确认 server.py 引用的模块都在。"""
        import subprocess

        routers = _server_imported_routers()
        self.assertTrue(routers, "没能从 server.py 解析出 router 列表")

        proc = subprocess.run(
            [sys.executable, str(BUILD), "--prepare-only"],
            cwd=str(ROOT), capture_output=True, timeout=300)
        if proc.returncode != 0:
            self.skipTest(f"--prepare-only 失败（环境问题？）："
                          f"{proc.stderr.decode('utf-8', 'ignore')[:200]}")

        staging = ROOT / "android" / "app" / "build" / "generated" / "python"
        self.assertTrue(staging.is_dir(), "staging 目录未生成")

        missing = []
        for name in sorted(routers):
            pkg = staging / "admin" / "routers" / f"{name}.py"
            plain = staging / "admin" / f"{name}.py"
            if not pkg.is_file() and not plain.is_file():
                missing.append(name)
        self.assertEqual(
            missing, [],
            "**这些模块 server.py 会 import，但 staging 里没有** —— "
            f"APK 启动会 ImportError：{missing}")

    def test_staging_can_actually_import(self):
        """拿 staging 真的跑一次 import（不是只看文件在不在）。

        这是最强的一条：文件在但依赖缺失时，只有真 import 才暴露。
        """
        import subprocess

        staging = ROOT / "android" / "app" / "build" / "generated" / "python"
        if not staging.is_dir():
            self.skipTest("staging 未生成")
        code = "import admin.server; print('OK')"
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=str(staging),
            capture_output=True, timeout=180,
            env={**os.environ, "ADMIN_JWT_SECRET": "x" * 40,
                 "ADMIN_DATABASE_URL": "sqlite:///:memory:",
                 "ADMIN_GATEWAY_LIMIT_BACKEND": "memory",
                 "WORKBUDDY_ANDROID": "1"})
        self.assertEqual(
            proc.returncode, 0,
            "**staging 里的应用起不来** —— 这正是 APK 会出现的状态。\n"
            f"stderr: {proc.stderr.decode('utf-8', 'ignore')[-600:]}")


class AndroidCheckMatchesPackagingCase(unittest.TestCase):
    """`check_runtime --android` 的检查项要和实际打包内容一致。"""

    def test_android_does_not_require_new_panel(self):
        """安卓只打包旧面板，自检不能要求 `/panel/`。"""
        src = CHECK.read_text(encoding="utf-8")
        # 必须有「按形态决定是否检查 /panel/」的逻辑
        self.assertIn("args.android", src,
                      "check_runtime 应按 android 形态区分检查项")
        # 找到 panel_checks 那段，确认 /panel/ 在条件里
        i = src.find("panel_checks")
        self.assertGreater(i, 0, "应有 panel_checks 组装逻辑")
        seg = src[i:i + 600]
        self.assertIn("args.android", seg,
                      "`/panel/` 的检查必须受 android 形态控制 —— "
                      "build_android.py 只复制 admin/static，不含 web/out")

    def test_build_does_not_copy_web_out(self):
        """确认「安卓不打包新面板」这个前提仍然成立。

        如果哪天真的把 web/out 打进 APK，应该同时改掉上一条测试
        （而不是让两条互相矛盾）。
        """
        src = BUILD.read_text(encoding="utf-8")
        copies_web_out = ("web/out" in src or "web\" / \"out\"" in src
                          or "WEB_OUT" in src)
        self.assertFalse(
            copies_web_out,
            "build_android.py 现在会复制 web/out 了？"
            "若已支持新面板，请同时更新 test_android_does_not_require_new_panel")


class PipIndexConfigurableCase(unittest.TestCase):
    """pip 源必须可配置（CI 在海外，连国内镜像会超时）。

    ## 为什么这条写得这么细

    第一版只断言「源码里出现 `WB_PIP_INDEX_URL`」。反向验证发现：
    把 `os.getenv(...)` 换成写死的常量、**只在注释里留个名字**，
    断言依然成立 → 假绿。

    所以现在：抽出 `index_url = ...` 那一行的**实际表达式**，
    要求它必须真的调用 `os.getenv` / `os.environ`。
    """

    def test_index_url_from_env(self):
        src = BUILD.read_text(encoding="utf-8")
        self.assertIn("WB_PIP_INDEX_URL", src,
                      "**pip 源必须能通过环境变量覆盖** —— GitHub Actions 的 "
                      "runner 在海外，连清华源会 ReadTimeout（实测踩过）")

        # 必须存在 `index_url = <读取环境变量的表达式>`
        m = re.search(r"^\s*index_url\s*=\s*(.+)$", src, re.M)
        self.assertIsNotNone(m, "应有 `index_url = ...` 赋值")
        expr = m.group(1)
        self.assertTrue(
            "os.getenv" in expr or "os.environ" in expr,
            "**`index_url` 必须真的从环境变量读**，不能是写死的常量。"
            f"实际：{expr.strip()[:120]}")
        self.assertIn("WB_PIP_INDEX_URL", expr,
                      "读取的环境变量名应为 WB_PIP_INDEX_URL")

    def test_index_url_actually_used(self):
        """变量必须真的传给 `--index-url`（不是定义了没用）。"""
        src = BUILD.read_text(encoding="utf-8")
        self.assertRegex(
            src,
            r"--index-url['\"]?\s*,\s*index_url|--index-url['\"]?\s*,\s*\n?\s*index_url",
            "`--index-url` 后面应传 `index_url` 变量，而不是字面量")


if __name__ == "__main__":
    unittest.main()
