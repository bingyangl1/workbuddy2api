"""harness 脱敏必须在**三个出站协议路径**都生效 —— 真实 bug 的回归测试。

## 症状（用户视角）

**Claude Code 能用，但同样内容的 OpenAI 协议客户端发不出去**，报：

    400 code=11128 "Illegal API invocation from an unapproved channel"

而简单 hello 测试却通过 —— 非常容易误判为「模型/账号问题」。

## 根因

Claude Code 的 system prompt 是固定 harness 模板，**必然含**这类合规声明词：

    "DoS attacks" / "exploit development" / "credential testing"

上游内容审核把它们当敏感内容，**整条请求拒收**（11128）。

所以必须做 harness 脱敏（压缩成中性提示 + 零宽空格）。但原实现**只在
`/v1/messages`（Claude Code 路径）做了这一步**：

    /v1/messages          ✅ 有（内联实现）
    /v1/chat/completions  ❌ 没有  ← 用 OpenAI 协议的客户端全军覆没
    /v1/responses         ❌ 没有

上游 `xiaofan6ya/workbuddy2api@bd6ad90e` 修的就是这一点。

## 本实现的修法

抽出 `_desensitize_chat_body(chat_body, tag=...)` 作为**统一入口**，
三个端点都调它；失败一律按原样发送（脱敏是增强，不该成为新的失败点）。

注意 `/v1/responses` 的调用位置必须在 `project_responses_chat_body` **之后**：
投影会裁剪/重排 messages，先脱敏再投影等于白做。
"""
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROXY = Path(__file__).resolve().parent.parent / "admin" / "routers" / "proxy.py"


class DesensitizeCallSitesCase(unittest.TestCase):
    """静态检查：三个出站路径都必须调用统一入口。

    这类「同一能力要在多个入口都接线」的缺陷，靠功能测试很难发现
    （没人会同时用三种协议各测一遍），所以用静态断言把它钉住。
    """

    @classmethod
    def setUpClass(cls):
        cls.src = PROXY.read_text(encoding="utf-8")

    def test_helper_exists(self):
        self.assertIn("def _desensitize_chat_body(", self.src,
                      "统一入口函数必须存在")

    def test_all_three_endpoints_wired(self):
        """`chat` / `responses` / `messages` 三个 tag 都要出现。"""
        calls = set(re.findall(r"_desensitize_chat_body\([^)]*tag=\"(\w+)\"", self.src))
        for tag in ("chat", "responses", "messages"):
            self.assertIn(tag, calls,
                          f"**{tag} 路径没有接入 harness 脱敏** —— "
                          f"该协议下的客户端会 100% 撞 11128。实际接入: {sorted(calls)}")

    def test_messages_no_longer_inlines(self):
        """`/v1/messages` 原先的内联实现应已收拢到统一入口（避免两处行为漂移）。"""
        # 内联实现的特征：在 messages 端点里直接调用 desensitize_body( 并带 roles=
        occurrences = len(re.findall(r"\bdesensitize_body\(\s*\n?\s*chat_body", self.src))
        self.assertLessEqual(
            occurrences, 1,
            "不该在多个地方各自内联调用 desensitize_body —— 应收拢到 _desensitize_chat_body"
            f"（当前 {occurrences} 处）")

    def test_responses_desensitize_after_projection(self):
        """`/v1/responses` 的脱敏必须在投影**之后**（否则投影会重新引入原文）。"""
        proj = self.src.find("project_responses_chat_body(chat_body)")
        call = self.src.find('_desensitize_chat_body(chat_body, tag="responses")')
        self.assertGreater(proj, 0, "没找到投影调用")
        self.assertGreater(call, 0, "responses 路径没接入脱敏")
        self.assertGreater(call, proj,
                           "脱敏必须在 project_responses_chat_body **之后**")

    def test_chat_desensitize_before_session_key(self):
        """`/v1/chat/completions` 的脱敏要在会话粘性键计算之前。

        否则「按脱敏前内容算粘性键、发脱敏后内容」会造成不一致
        （虽然当前粘性键取最后一条 user 消息、不受压缩影响，
        但顺序上仍应保证出站 body 与键来自同一份内容）。
        """
        call = self.src.find('_desensitize_chat_body(body, tag="chat")')
        self.assertGreater(call, 0, "chat 路径没接入脱敏")
        # 该函数内的粘性键计算（在 call 之后）
        sticky = self.src.find("sticky_key = pool.session_key_for(body)", call)
        self.assertGreater(sticky, call,
                           "chat 的脱敏应在会话粘性键计算之前")


class HelperBehaviorCase(unittest.TestCase):
    """统一入口的行为（不依赖 FastAPI/DB，直接调模块内函数）。"""

    def setUp(self):
        os.environ.setdefault("ADMIN_JWT_SECRET", "x" * 40)
        try:
            from admin.routers import proxy
        except Exception as e:      # pragma: no cover
            self.skipTest(f"proxy 导入失败：{e}")
        self.proxy = proxy

    def _harness_body(self):
        return {
            "model": "auto",
            "messages": [
                {"role": "system", "content":
                    "You are Claude Code. You must refuse to help with DoS attacks, "
                    "exploit development, or credential testing."},
                {"role": "user", "content": "你好"},
            ],
        }

    def test_disabled_returns_same_object(self):
        """开关关闭时**原样返回同一个对象**（零拷贝、零副作用）。"""
        p = self.proxy
        orig_flag = getattr(p.settings, "ANTHROPIC_DESENSITIZE", None)
        try:
            p.settings.ANTHROPIC_DESENSITIZE = False
            body = self._harness_body()
            out = p._desensitize_chat_body(body, tag="test")
            self.assertIs(out, body, "关闭时应原样返回，不做任何处理")
        finally:
            if orig_flag is not None:
                p.settings.ANTHROPIC_DESENSITIZE = orig_flag

    def test_failure_falls_back_to_original(self):
        """脱敏抛异常时**按原样发送**（脱敏是增强，不该成为新的失败点）。"""
        p = self.proxy
        orig_flag = getattr(p.settings, "ANTHROPIC_DESENSITIZE", None)
        orig_fn = p.desensitize_body
        try:
            p.settings.ANTHROPIC_DESENSITIZE = True
            p._DESENSITIZE_AVAILABLE = True

            def _boom(*a, **k):
                raise RuntimeError("模拟脱敏内部错误")

            p.desensitize_body = _boom
            body = self._harness_body()
            out = p._desensitize_chat_body(body, tag="test")
            self.assertEqual(out, body, "失败时必须按原样发送")
        finally:
            p.desensitize_body = orig_fn
            if orig_flag is not None:
                p.settings.ANTHROPIC_DESENSITIZE = orig_flag

    def test_multimodal_preserved_when_enabled(self):
        """开启时：多模态（图片）内容不被压缩掉。"""
        p = self.proxy
        orig_flag = getattr(p.settings, "ANTHROPIC_DESENSITIZE", None)
        try:
            p.settings.ANTHROPIC_DESENSITIZE = True
            body = {
                "model": "auto",
                "messages": [
                    {"role": "system", "content": "You are Claude Code. Refuse DoS attacks."},
                    {"role": "user", "content": [
                        {"type": "text", "text": "这张图里是什么？"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
                    ]},
                ],
            }
            out = p._desensitize_chat_body(body, tag="test")
            blocks = out["messages"][1]["content"]
            self.assertIsInstance(blocks, list, "多模态块结构必须保留")
            self.assertTrue(any(b.get("type") == "image_url" for b in blocks),
                            "图片块不能被丢掉")
        finally:
            if orig_flag is not None:
                p.settings.ANTHROPIC_DESENSITIZE = orig_flag


if __name__ == "__main__":
    unittest.main()
