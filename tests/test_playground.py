"""P4-3：聊天测试台 /api/playground/chat。

覆盖：
- 缺 model / messages → 400
- 无可用账号 → 503 no_account
- 成功路径：mock 上游 SSE，验证走号池状态机（_select_account 被调、POOL acquire/release、
  _record_usage 以 key_id=0 + use_case='playground' 记账、usage 尾帧发出）
- 上游错误（>=400）：_classify_error + _apply_account_policy 错误处置照常调用
- 护栏：UPSTREAM_READONLY=1 时 /api/playground/chat 被 is_upstream_write 拦截

全程 mock 上游与选号——绝不触达真上游、不烧真积分。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def _factory():
    from admin.db import Base
    from admin import models as _m  # noqa: F401
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine, tables=[
        Base.metadata.tables["accounts"],
        Base.metadata.tables["api_keys"],
        Base.metadata.tables["usage_logs"],
        Base.metadata.tables["model_configs"],
    ])
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _app(factory):
    def _db():
        db = factory()
        try:
            yield db
        finally:
            db.close()
    from admin.routers import playground
    from admin.db import get_db
    app = FastAPI()
    app.include_router(playground.router)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[playground.require_admin] = lambda: True
    return app, playground


class ValidationCase(unittest.TestCase):
    def setUp(self):
        self.factory = _factory()
        self.app, _ = _app(self.factory)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_missing_model_400(self):
        r = self.client.post("/api/playground/chat",
                             json={"model": "", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 400)

    def test_missing_messages_400(self):
        r = self.client.post("/api/playground/chat", json={"model": "glm-5.2", "messages": []})
        self.assertEqual(r.status_code, 400)

    def test_no_account_503(self):
        # 空库无账号 → _select_account 返回 None（用真函数，库为空必然选不出）
        r = self.client.post("/api/playground/chat",
                             json={"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 503)
        self.assertIn("no_account", r.text)


class FlowCase(unittest.TestCase):
    """成功/失败路径：mock 选号 + 会话 + 上游流，验证状态机原子函数被复用。"""

    def setUp(self):
        self.factory = _factory()
        self.app, self.pg = _app(self.factory)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        # 造一个账号（_record_usage 扣余额要用）
        with self.factory() as db:
            from admin.models import Account
            db.add(Account(name="a1", uid="u1", realm="cn", auth_json='{"token":"x"}',
                           status="active", balance_remain=1000))
            db.commit()

    def _fake_session(self):
        s = mock.MagicMock()
        s.get_headers.return_value = {}
        s.updated_json.return_value = '{"token":"x"}'
        s.realm = "cn"
        return s

    def test_success_records_with_key0_playground(self):
        sse = ('data: {"choices":[{"delta":{"content":"你好"}}]}\n\n'
               'data: {"choices":[{"delta":{"content":"世界"}}],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n'
               'data: [DONE]\n\n')
        captured = {}

        class _FakeResp:
            status_code = 200
            headers = {}
            async def aiter_text(self):
                for line in sse.split("\n\n"):
                    if line.strip():
                        yield line + "\n\n"

        class _FakeCM:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return _FakeResp()
            async def __aexit__(self, *a): return False

        def _record(key_id, account_id, model, credits, updated_auth_json, **kw):
            captured.update(key_id=key_id, account_id=account_id, model=model,
                            use_case=kw.get("use_case"), credits=credits)

        from admin.models import Account
        with self.factory() as db:
            acc = db.query(Account).first()
        with mock.patch.object(self.pg, "_select_account", return_value=acc), \
             mock.patch.object(self.pg.POOL, "acquire", return_value=True), \
             mock.patch.object(self.pg.POOL, "release") as rel, \
             mock.patch.object(self.pg, "_account_session_async",
                               new=mock.AsyncMock(return_value=self._fake_session())), \
             mock.patch.object(self.pg, "_ChatStreamCM", _FakeCM), \
             mock.patch.object(self.pg, "_record_usage", side_effect=_record):
            r = self.client.post("/api/playground/chat",
                                 json={"model": "glm-5.2",
                                       "messages": [{"role": "user", "content": "hi"}],
                                       "reasoning_effort": "high"})
        self.assertEqual(r.status_code, 200, r.text)
        # 记账口径：key_id=0 + use_case=playground（与 /v1 同一 usage_logs，区分测试台）
        self.assertEqual(captured.get("key_id"), 0)
        self.assertEqual(captured.get("use_case"), "playground")
        self.assertEqual(captured.get("model"), "glm-5.2")
        # 租约释放（不泄漏）
        self.assertTrue(rel.called)
        # 流内容透传 + usage 尾帧
        self.assertIn("你好", r.text)
        self.assertIn("playground_usage", r.text)

    def test_upstream_error_applies_policy(self):
        applied = {}

        class _FakeErrResp:
            status_code = 500
            headers = {}
            async def aread(self): return b'{"error":"upstream boom"}'

        class _FakeErrCM:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return _FakeErrResp()
            async def __aexit__(self, *a): return False

        def _policy(db, acc, kind, status, text, **kw):
            applied.update(kind=kind, status=status)

        from admin.models import Account
        with self.factory() as db:
            acc = db.query(Account).first()
        with mock.patch.object(self.pg, "_select_account", return_value=acc), \
             mock.patch.object(self.pg.POOL, "acquire", return_value=True), \
             mock.patch.object(self.pg.POOL, "release"), \
             mock.patch.object(self.pg, "_account_session_async",
                               new=mock.AsyncMock(return_value=self._fake_session())), \
             mock.patch.object(self.pg, "_ChatStreamCM", _FakeErrCM), \
             mock.patch.object(self.pg, "_apply_account_policy", side_effect=_policy), \
             mock.patch.object(self.pg, "_maybe_degrade"), \
             mock.patch.object(self.pg, "_record_usage"):
            r = self.client.post("/api/playground/chat",
                                 json={"model": "glm-5.2",
                                       "messages": [{"role": "user", "content": "hi"}]})
        # 错误分类 + 账号处置照常（与 /v1 同一函数）
        self.assertEqual(applied.get("status"), 500)
        self.assertTrue(applied.get("kind"))
        # 错误以 SSE error 帧返回
        self.assertIn("upstream_error", r.text)


class GuardCase(unittest.TestCase):
    """护栏：UPSTREAM_READONLY=1 时测试台 chat 被拦截（它真实消耗积分）。"""

    def test_is_upstream_write_includes_playground(self):
        from admin.security import is_upstream_write
        self.assertTrue(is_upstream_write("POST", "/api/playground/chat"))
        # GET 不算（护栏只管写）
        self.assertFalse(is_upstream_write("GET", "/api/playground/chat"))


if __name__ == "__main__":
    unittest.main()
