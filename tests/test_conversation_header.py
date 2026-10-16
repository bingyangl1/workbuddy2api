"""X-Conversation-ID 必须恒发 —— 上游渠道校验的回归测试。

## 上游实证（HanawaBanana/workbuddy2api #36）

上游逐账号直连实测的结论：

> 只要该头在场（**固定字符串即可，不校验会话存在性**）即全部通过，与账号无关
> —— 是**请求维度**不是账号维度（此前易被 displayTips 误导为「账号被封」）。
> 缺头则返回 `400 code=11-128 "conversation id missing"`，
> displayTips：「这次调用没通过渠道校验，可能不是官方客户端发出的」。

而 DSH / Codex / Cherry Studio 这类 OpenAI 兼容客户端**普遍不在 body 里带
conversationId** —— 原实现「客户端没给就不发」让这类流量在部分账号上 100% 失败。

## 修法（照抄上游的两层设计）

* 客户端给了原值 → 用原值
* 客户端没给 → 用**轮级聚合键**兜底（与 X-Conversation-Request-ID 同源），
  使「同一轮内换号/重试/降级」复用同一会话 ID
* 兜底值**不进** body 的 `prompt_cache_key` → 不破坏无会话键客户端的
  「每账号恒键」前缀缓存命中率（那是轮级键，逐轮变化会打碎缓存）
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HEX32 = re.compile(r"^[0-9a-f]{32}$")


class ConversationHeaderCase(unittest.TestCase):
    def setUp(self):
        # 直接导入被测函数（不依赖 DB）
        os.environ.setdefault("ADMIN_JWT_SECRET", "x" * 40)
        try:
            from admin.routers.proxy import _session_headers
        except Exception as e:      # pragma: no cover
            self.skipTest(f"proxy 模块导入失败：{e}")
        self.fn = _session_headers

    # ── 核心：恒发 ──
    def test_header_always_present_without_client_id(self):
        """**客户端没给 conversationId 时也必须发该头**（修复的核心）。

        这是原 bug 的场景：DSH/Codex 这类客户端不带 conversationId。
        """
        h = self.fn({"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
                    "uid-1")
        self.assertIn("X-Conversation-ID", h,
                      "**缺会话 ID 时必须兜底发头** —— 否则上游 400 11-128")
        self.assertTrue(HEX32.match(h["X-Conversation-ID"]),
                        f"兜底值应是 32 位 hex，实际 {h['X-Conversation-ID']!r}")

    def test_client_value_takes_priority(self):
        """客户端给了原值 → 透传（原值优先）。"""
        h = self.fn({"conversation_id": "client-conv-abc"}, "uid-1")
        self.assertEqual(h["X-Conversation-ID"], "client-conv-abc")

    def test_camel_case_client_value(self):
        h = self.fn({"conversationId": "camel-xyz"}, "uid-1")
        self.assertEqual(h["X-Conversation-ID"], "camel-xyz")

    def test_metadata_conversation_id(self):
        h = self.fn({"metadata": {"conversation_id": "meta-conv"}}, "uid-1")
        self.assertEqual(h["X-Conversation-ID"], "meta-conv")

    def test_prompt_cache_key_used_as_client_value(self):
        """pi-ai 系客户端把会话 ID 放在 prompt_cache_key 里 → 视为客户端原值。"""
        h = self.fn({"prompt_cache_key": "pck-123"}, "uid-1")
        self.assertEqual(h["X-Conversation-ID"], "pck-123",
                         "prompt_cache_key 是客户端的会话标识，应透传而非另造")

    # ── 同一轮内稳定（换号重试复用同 ID）──
    def test_fallback_stable_within_same_turn(self):
        """同一轮（同一条 user 消息）反复调用应得到**相同**的兜底会话 ID。

        这保证「换号/重试/降级」时上游看到的是同一次对话轮，
        而不是 N 个并发会话（与 X-Conversation-Request-ID 的聚合语义对齐）。
        """
        body = {"messages": [{"role": "user", "content": "同一个问题"}]}
        a = self.fn(body, "uid-1")["X-Conversation-ID"]
        b = self.fn(body, "uid-1")["X-Conversation-ID"]
        self.assertEqual(a, b, "同轮内兜底 ID 必须稳定（换号重试要复用）")

    def test_fallback_differs_across_turns(self):
        """不同 user 消息 → 不同兜底 ID（轮级键，逐轮变化）。"""
        a = self.fn({"messages": [{"role": "user", "content": "问题 A"}]},
                    "uid-1")["X-Conversation-ID"]
        b = self.fn({"messages": [{"role": "user", "content": "问题 B"}]},
                    "uid-1")["X-Conversation-ID"]
        self.assertNotEqual(a, b, "换 user 消息应换兜底会话 ID")

    # ── 不破坏既有头族 ──
    def test_other_conversation_headers_still_present(self):
        h = self.fn({"messages": [{"role": "user", "content": "hi"}]}, "uid-1")
        for k in ("X-Conversation-Request-ID", "X-Conversation-Message-ID",
                  "X-Request-ID", "X-Root-Request-ID", "X-B3-TraceId",
                  "X-B3-SpanId", "b3", "traceparent", "X-Trace-ID",
                  "X-Agent-Intent"):
            self.assertIn(k, h, f"既有头族不该丢：{k}")
        # 三个会话头都必须是 32 hex（与官方形态一致）
        for k in ("X-Conversation-ID", "X-Conversation-Request-ID",
                  "X-Conversation-Message-ID"):
            self.assertTrue(HEX32.match(h[k]), f"{k} 应为 32 hex，实际 {h[k]!r}")

    def test_trace_and_b3_consistent(self):
        """b3 / traceparent / X-B3-* 必须共享同一 trace/span（否则自相矛盾更易被识别）。"""
        h = self.fn({"messages": [{"role": "user", "content": "hi"}]}, "uid-1")
        trace = h["X-B3-TraceId"]
        span = h["X-B3-SpanId"]
        self.assertEqual(h["X-Trace-ID"], trace)
        self.assertTrue(h["traceparent"].startswith(f"00-{trace}-{span}-"))
        self.assertTrue(h["b3"].startswith(f"{trace}-{span}-"))

    # ── 兜底值不污染缓存键 ──
    def test_fallback_not_derived_from_cache_key(self):
        """兜底逻辑**不改 body**（prompt_cache_key 仍是客户端原值）。

        上游的关键细节：兜底值只进出站头，不进 prompt_cache_key 会话段 ——
        否则轮级键逐轮变化会让「无会话键客户端」的前缀缓存每轮失效。
        我们的实现是「头与 body 分离」，所以 body 不该被函数改动。
        """
        body = {"messages": [{"role": "user", "content": "hi"}]}
        snapshot = dict(body)
        self.fn(body, "uid-1")
        self.assertEqual(body, snapshot, "该函数不该改动请求体")


if __name__ == "__main__":
    unittest.main()
