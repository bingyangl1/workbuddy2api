"""Anthropic → Chat 转换：tool 消息必须**紧邻** assistant.tool_calls。

## 这条测试的由来（上游 06e1b46 的同一个 bug）

OpenAI 协议要求：

    assistant(tool_calls) → tool → tool → ... → user

`role: tool` 消息必须**紧接**在 `assistant.tool_calls` 之后，中间不能插别的东西。

我们的 `_convert_anthropic_message` 原来写的是：

    result.insert(0, {"role": "user", "content": content})

—— 把用户文本插到了 tool 结果**前面**，配对被打断。

### 为什么极难发现

普通对话几乎不会触发（很少有多轮工具调用）。但 **Claude Code 的子 Agent**
会把 `tool_result` 与 `<system-reminder>` 混排在同一条 user 消息里，
此时上游立刻判「对话损坏」返回 **11148**（工具配对失败）。

用户看到的现象是「子 Agent 一用就失败」，而主对话完全正常 ——
极难联想到是消息顺序问题。

上游在这个提交里同时加了 `validate_tool_pairing` 诊断，
本测试就是那条判据的等价实现。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anthropic_adapter import _convert_anthropic_message  # noqa: E402


def _user_msg(*blocks) -> dict:
    return {"role": "user", "content": list(blocks)}


class ToolOrderingCase(unittest.TestCase):
    def test_tool_message_comes_before_user_text(self):
        """**核心**：`role: tool` 必须排在 `role: user` **之前**。

        这是 11148 的直接判据。
        """
        out = _convert_anthropic_message(_user_msg(
            {"type": "tool_result", "tool_use_id": "call_abc",
             "content": "文件内容"},
            {"type": "text", "text": "继续"},
        ))
        roles = [m["role"] for m in out]
        self.assertEqual(
            roles, ["tool", "user"],
            f"**tool 必须在 user 之前**（否则切断 assistant.tool_calls 配对，"
            f"上游报 11148）。实际顺序：{roles}")

    def test_tool_result_only(self):
        """只有 tool_result 时，不应插入空的 user 消息。"""
        out = _convert_anthropic_message(_user_msg(
            {"type": "tool_result", "tool_use_id": "c1", "content": "ok"},
        ))
        self.assertEqual([m["role"] for m in out], ["tool"])

    def test_text_only_stays_user(self):
        """纯文本（无 tool_result）行为不变 —— 零回归。"""
        out = _convert_anthropic_message(_user_msg(
            {"type": "text", "text": "你好"},
        ))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["role"], "user")
        self.assertEqual(out[0]["content"], "你好")

    def test_multiple_tool_results_keep_order(self):
        """多个 tool_result 的相对顺序要保留。"""
        out = _convert_anthropic_message(_user_msg(
            {"type": "tool_result", "tool_use_id": "c1", "content": "a"},
            {"type": "tool_result", "tool_use_id": "c2", "content": "b"},
            {"type": "text", "text": "总结"},
        ))
        self.assertEqual([m["role"] for m in out], ["tool", "tool", "user"])
        self.assertEqual([m["tool_call_id"] for m in out[:2]], ["c1", "c2"])

    def test_empty_tool_use_id_dropped(self):
        """空 `tool_use_id` 必须丢弃。

        留着会让上游看到一条**无法配对**的 tool 消息，同样报 11148。
        """
        out = _convert_anthropic_message(_user_msg(
            {"type": "tool_result", "tool_use_id": "", "content": "x"},
            {"type": "tool_result", "tool_use_id": "  ", "content": "y"},
            {"type": "tool_result", "tool_use_id": "c1", "content": "z"},
        ))
        tools = [m for m in out if m["role"] == "tool"]
        self.assertEqual(len(tools), 1,
                         f"空 id 的 tool_result 应被丢弃，实际保留 {len(tools)} 条")
        self.assertEqual(tools[0]["tool_call_id"], "c1")

    def test_list_content_flattened(self):
        """list 形式的 content 要拍平成字符串（否则上游不认）。"""
        out = _convert_anthropic_message(_user_msg(
            {"type": "tool_result", "tool_use_id": "c1",
             "content": [{"type": "text", "text": "第一段"},
                         {"type": "text", "text": "第二段"}]},
        ))
        self.assertEqual(out[0]["content"], "第一段第二段")

    def test_non_string_content_not_crashing(self):
        """异常 content 类型不能崩（错误路径上的代码）。"""
        for bad in (None, 123, {"k": "v"}):
            out = _convert_anthropic_message(_user_msg(
                {"type": "tool_result", "tool_use_id": "c1", "content": bad},
            ))
            self.assertEqual(out[0]["role"], "tool")
            self.assertIsInstance(out[0]["content"], str,
                                  f"content={bad!r} 时应转成字符串")


class NoPairingValidatorCase(unittest.TestCase):
    """记录一个已知缺口：我们还没有 `validate_tool_pairing` 诊断。

    上游 06e1b46 加了它用于「11148 时输出 role 序列与配对问题摘要，便于再查」。
    我们没有 —— 校验手段是这条测试文件本身，但**运行时诊断**缺失。

    这条测试的存在意义：让这个缺口可见，而不是假装没有。
    """

    def test_validator_absent_is_known(self):
        import anthropic_adapter as A
        import upstream_compat as U

        has_it = (hasattr(A, "validate_tool_pairing")
                  or hasattr(U, "validate_tool_pairing"))
        if has_it:
            self.skipTest("已补上 validate_tool_pairing，可删除本条")
        # 未补上：不失败，但明确记录（用 skip 让它可见）
        self.skipTest(
            "已知缺口：还没有 validate_tool_pairing 运行时诊断"
            "（上游 06e1b46 有）。顺序修复已完成，诊断待补。")


if __name__ == "__main__":
    unittest.main()
