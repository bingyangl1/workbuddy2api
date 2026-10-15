"""P5：Changelog 查看器路由（/api/app/changelog）。

覆盖 _parse_changelog 解析（版本头/日期/小节/条目/续行）+ 路由契约
（current_version + versions 结构）。不读真文件（mock _changelog_raw）。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SAMPLE = """# Changelog

## [Unreleased]

### 新增
- 功能甲
- 功能乙很长
  跨两行的续行内容

### 修复
- 修了 bug

## [0.1.0] - 2026-10-01

### 新增
- 基线能力

## [0.0.1] - 基线

### 安全
- 加固
"""


class ParseCase(unittest.TestCase):
    def test_parses_versions_and_sections(self):
        from admin.routers.releases import _parse_changelog
        v = _parse_changelog(_SAMPLE)
        self.assertEqual(len(v), 3)
        self.assertEqual(v[0]["version"], "Unreleased")
        self.assertEqual(v[0]["date"], "")
        self.assertEqual(v[1]["version"], "0.1.0")
        self.assertEqual(v[1]["date"], "2026-10-01")
        self.assertEqual(v[2]["date"], "基线")
        # 小节与条目
        sec0 = {s["title"]: s["items"] for s in v[0]["sections"]}
        self.assertIn("新增", sec0)
        self.assertIn("修复", sec0)
        self.assertEqual(len(sec0["新增"]), 2)
        # 续行并入上一条
        self.assertIn("跨两行的续行内容", sec0["新增"][1])

    def test_empty_text(self):
        from admin.routers.releases import _parse_changelog
        self.assertEqual(_parse_changelog(""), [])
        self.assertEqual(_parse_changelog("# 只有标题\n"), [])


class RouteCase(unittest.TestCase):
    def test_changelog_route(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from admin.routers import releases

        app = FastAPI()
        app.include_router(releases.router)
        app.dependency_overrides[releases.require_admin] = lambda: True
        client = TestClient(app, raise_server_exceptions=False)
        with mock.patch.object(releases, "_changelog_raw", return_value=_SAMPLE):
            r = client.get("/api/app/changelog")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("current_version", body)
        self.assertEqual(len(body["versions"]), 3)
        self.assertEqual(body["versions"][0]["version"], "Unreleased")


if __name__ == "__main__":
    unittest.main()
