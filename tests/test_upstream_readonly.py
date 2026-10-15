"""上游只读测试护栏（ADMIN_UPSTREAM_READONLY）验证。

防测试/截图核验误伤账号：开关启用后，管理面里会真实触达上游腾讯/WorkBuddy
的写操作（做成长任务/领奖/强制刷新余额/测速/同步模型/token 注入/猫猫旅行/
定时任务立即运行）返回 403；本地 DB CRUD 与 /v1 /gw 网关转发一律放行。

测两层：
- is_upstream_write 判定函数：被挡路径命中、本地 CRUD/GET/网关不命中
- 中间件接线：开关开→被挡 403、放行 200；开关关→全部直通
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


class IsUpstreamWriteCase(unittest.TestCase):
    """判定函数口径：只挡管理面触上游写，不碰本地 CRUD / 网关 / GET。"""

    def _f(self, method, path):
        from admin.security import is_upstream_write
        return is_upstream_write(method, path)

    # ---- 被挡（触上游）----
    def test_growth_ops_blocked(self):
        for p in ("/api/growth/accept", "/api/growth/run",
                  "/api/growth/run-async", "/api/growth/claim"):
            self.assertTrue(self._f("POST", p), p)

    def test_credits_sync_blocked(self):
        self.assertTrue(self._f("POST", "/api/accounts/credits/sync"))

    def test_cat_travel_blocked(self):
        for p in ("/api/accounts/cat-travel/batch",
                  "/api/accounts/cat-travel/batch-async"):
            self.assertTrue(self._f("POST", p), p)
        self.assertTrue(self._f("POST", "/api/accounts/3/cat-travel"))

    def test_models_upstream_blocked(self):
        self.assertTrue(self._f("POST", "/api/models/sync"))
        self.assertTrue(self._f("POST", "/api/models/speed-test"))

    def test_account_refresh_inject_blocked(self):
        self.assertTrue(self._f("POST", "/api/accounts/5/refresh"))
        self.assertTrue(self._f("POST", "/api/accounts/5/inject"))

    def test_schedule_run_blocked(self):
        self.assertTrue(self._f("POST", "/api/schedules/2/run"))

    # ---- 放行（本地 CRUD / GET / 网关）----
    def test_local_crud_allowed(self):
        # 本地 DB 写，不触上游
        for p in ("/api/accounts", "/api/accounts/batch", "/api/accounts/dedupe",
                  "/api/accounts/import-local", "/api/keys", "/api/account-groups",
                  "/api/groups", "/api/tokens", "/api/audit/clear",
                  "/api/security/config", "/api/security/rules",
                  "/api/schedules", "/api/schedules/2/toggle",
                  "/api/models/batch-toggle", "/api/client-profile/detect"):
            self.assertFalse(self._f("POST", p), p)

    def test_get_never_blocked(self):
        # 护栏只挡写：任何 GET 都放行（读快照/缓存不触上游写）
        for p in ("/api/growth/run", "/api/accounts/credits/sync",
                  "/api/models/speed-test"):
            self.assertFalse(self._f("GET", p), p)

    def test_gateway_untouched(self):
        # /v1 /gw 是正常转发正路，绝不被护栏碰
        for p in ("/v1/chat/completions", "/v1/responses", "/v1/messages",
                  "/gw/v1/chat/completions"):
            self.assertFalse(self._f("POST", p), p)


def _make_app():
    """迷你 app：只挂护栏中间件 + 一个回声 POST，验证接线与开关行为。"""
    from admin.security import is_upstream_write
    from admin.config import settings

    app = FastAPI()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if settings.UPSTREAM_READONLY and is_upstream_write(
                request.method, request.url.path):
            return JSONResponse(status_code=403,
                                content={"detail": "上游只读模式"})
        return await call_next(request)

    @app.post("/api/growth/run")
    def _growth_run():
        return {"ok": True}

    @app.post("/api/keys")
    def _keys():
        return {"ok": True}

    return app


class GuardMiddlewareCase(unittest.TestCase):
    """中间件接线：开关开→触上游 403 / 本地 200；开关关→全 200。"""

    def setUp(self):
        self.client = TestClient(_make_app(), raise_server_exceptions=False)

    def test_disabled_passthrough(self):
        with mock.patch("admin.config.settings.UPSTREAM_READONLY", False):
            self.assertEqual(self.client.post("/api/growth/run").status_code, 200)

    def test_enabled_blocks_upstream_write(self):
        with mock.patch("admin.config.settings.UPSTREAM_READONLY", True):
            r = self.client.post("/api/growth/run")
            self.assertEqual(r.status_code, 403)
            self.assertIn("上游只读", r.json()["detail"])

    def test_enabled_allows_local_crud(self):
        with mock.patch("admin.config.settings.UPSTREAM_READONLY", True):
            self.assertEqual(self.client.post("/api/keys").status_code, 200)


if __name__ == "__main__":
    unittest.main()
