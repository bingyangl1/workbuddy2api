"""P5 回归：面板静态产物的 Cache-Control 策略。

钉住的真 bug：面板重建后 chunk 文件名全变，若 HTML 被浏览器长缓存，
用户拿旧 HTML 去请求已删除的旧 chunk → `ChunkLoadError` 整页白屏
（实测在 /tokens 页复现）。

策略：
  * 带内容哈希的 /_next/static/** → 长缓存 immutable
  * HTML / 其它无哈希文件       → no-cache, must-revalidate
  * /api、/admin                 → no-store（原有行为不变）
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse


def _client(panel_path="/panel"):
    """最小 app：装与 server.py 相同的中间件逻辑，避免导入整个 admin.server。"""
    import admin.server as srv

    app = FastAPI()

    @app.get("/api/ping")
    def _api():
        return {"ok": True}

    @app.get("/admin/")
    def _admin():
        return PlainTextResponse("old panel")

    @app.get("/panel/")
    def _panel():
        return PlainTextResponse("<html>")

    @app.get("/panel/dashboard/")
    def _panel_dash():
        return PlainTextResponse("<html>")

    @app.get("/panel/_next/static/chunks/main-abc123.js")
    def _chunk():
        return PlainTextResponse("js")

    # 装真实中间件（同一个函数对象）
    app.middleware("http")(srv._security_headers)
    return TestClient(app)


class CacheControlCase(unittest.TestCase):
    def test_html_no_cache(self):
        c = _client()
        for path in ("/panel/", "/panel/dashboard/"):
            r = c.get(path)
            self.assertEqual(r.status_code, 200, path)
            cc = r.headers.get("cache-control", "")
            self.assertIn("no-cache", cc, f"{path} 的 HTML 必须禁止长缓存：{cc!r}")

    def test_hashed_chunk_immutable(self):
        c = _client()
        r = c.get("/panel/_next/static/chunks/main-abc123.js")
        cc = r.headers.get("cache-control", "")
        self.assertIn("immutable", cc, f"带哈希的 chunk 应长缓存：{cc!r}")
        self.assertIn("max-age=31536000", cc)

    def test_api_and_admin_no_store(self):
        c = _client()
        for path in ("/api/ping", "/admin/"):
            cc = c.get(path).headers.get("cache-control", "")
            self.assertIn("no-store", cc, f"{path} 应为 no-store：{cc!r}")

    def test_security_headers_present(self):
        r = _client().get("/panel/")
        self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(r.headers.get("x-frame-options"), "DENY")
        self.assertEqual(r.headers.get("referrer-policy"), "no-referrer")


if __name__ == "__main__":
    unittest.main()
