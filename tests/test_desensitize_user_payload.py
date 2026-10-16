"""harness 压缩**不得吞掉用户提问** —— 真实 bug 的回归测试。

## 症状（用户视角）

用 Claude Code / Codex 这类客户端时，**首轮提问没反应**：模型只回
"I'm ready to help..."，第二轮才正常回答。

## 根因

Claude Code 常把**提问**和 harness 注入写进**同一条 user 消息**：

    <system-reminder>
    As you answer the user's questions, you can use the following context:
    ...
    </system-reminder>

    What is 1+1?

而压缩流程**无条件**把整条 user 消息换成固定摘要 → **提问被一并吞掉**。
模型收到的是「只有仓库说明、没有问题」。

## 上游修法（参考）

上游 `xiaofan6ya/workbuddy2api@1609e23c` 引入 `_extract_user_payload`：
把「整条替换」改成「**减法**」——只剥掉注入块与尾部元数据，剩下什么就留什么；
只有「剥完为空」（整条确实都是注入）时才用摘要兜底。

本实现按同一思路自行实现（复用本仓库既有的 `_RUNTIME_BLOCK_REPLACEMENTS` /
`_RUNTIME_TAIL_MARKERS` 数据结构），并额外处理「摘要占位串残留」。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from desensitize import _compact_harness_message, _extract_user_payload


class ExtractUserPayloadCase(unittest.TestCase):
    """`_extract_user_payload` 的纯函数行为（减法的核心）。"""

    def test_keeps_question_after_system_reminder(self):
        """**核心用例**：提问在 `<system-reminder>` 之后就应被保留。"""
        text = (
            "<system-reminder>\n"
            "As you answer the user's questions, you can use the following context:\n"
            "# currentDate\nToday's date is 2026-10-17.\n"
            "</system-reminder>\n\n"
            "What is 1+1?"
        )
        out = _extract_user_payload(text)
        self.assertIn("What is 1+1?", out, "**用户提问必须保留** —— 这是本 bug 的核心")
        self.assertNotIn("system-reminder", out, "注入块标签应被剥掉")

    def test_keeps_question_before_reminder(self):
        """提问在前、注入在后，也要保留。"""
        text = "解释一下这段代码\n<system-reminder>runtime stuff</system-reminder>"
        out = _extract_user_payload(text)
        self.assertIn("解释一下这段代码", out)

    def test_pure_injection_yields_empty(self):
        """整条都是注入 → 返回空（调用方据此用摘要兜底）。"""
        text = (
            "# AGENTS.md instructions for /repo\n"
            "<environment_context>\n  <cwd>/repo</cwd>\n</environment_context>"
        )
        self.assertEqual(_extract_user_payload(text), "",
                         "纯注入应抽不出东西（让调用方走摘要）")

    def test_unclosed_tail_block_removed(self):
        """未闭合的尾块（截断/异常注入）也要删掉。"""
        text = "<system-reminder>\n被截断的注入没有结束标签\n"
        out = _extract_user_payload(text)
        self.assertNotIn("被截断的注入", out)

    def test_tail_markers_cut(self):
        """尾部运行时元数据（工具/agent/skill 清单）应被截断。"""
        from desensitize import _RUNTIME_TAIL_MARKERS
        marker = _RUNTIME_TAIL_MARKERS[0]
        text = f"用户的问题\n\n{marker}\n一大堆运行时工具清单…"
        out = _extract_user_payload(text)
        self.assertIn("用户的问题", out)
        self.assertNotIn(marker, out)

    def test_replacement_placeholders_removed(self):
        """前面流程留下的摘要占位串要清掉（避免「摘要 + 残留提问」的怪组合）。"""
        from desensitize import _RUNTIME_BLOCK_REPLACEMENTS
        replacement = _RUNTIME_BLOCK_REPLACEMENTS[0][2]
        text = f"{replacement}\n\n真正的问题"
        out = _extract_user_payload(text)
        self.assertIn("真正的问题", out)
        self.assertNotIn(replacement, out)

    def test_empty_and_blank(self):
        self.assertEqual(_extract_user_payload(""), "")
        self.assertEqual(_extract_user_payload("   \n\n  "), "")


class CompactHarnessCase(unittest.TestCase):
    """端到端：`_compact_harness_message` 的结果不能丢提问。"""

    def test_mixed_message_keeps_question(self):
        """混排消息：压缩后必须还能看到提问。"""
        content = (
            "# AGENTS.md instructions for /repo\n"
            "<environment_context>\n  <cwd>/repo</cwd>\n"
            "  <shell>powershell</shell>\n</environment_context>\n\n"
            "帮我修一下这个 bug"
        )
        out = _compact_harness_message("user", content)
        self.assertIsNotNone(out, "混排消息应返回压缩后的内容，而不是 None")
        self.assertIn("帮我修一下这个 bug", out,
                      "**提问不能丢** —— 丢了模型只会回 'I'm ready to help'")

    def test_pure_harness_still_summarized(self):
        """纯 harness 注入仍走摘要（保持原有的降误伤能力）。

        注意用**只有 environment_context** 的样本：带 `<skills_instructions>`
        的会先命中 skills 分支返回 skills 摘要 —— 那是既有的分流设计，
        不是本 bug 相关（测试它的语义会跑偏）。
        """
        content = (
            "# AGENTS.md instructions for /repo\n"
            "<environment_context><cwd>/repo</cwd></environment_context>"
        )
        out = _compact_harness_message("user", content)
        self.assertIsNotNone(out, "纯注入应返回摘要")
        self.assertIn("Repository instructions", out,
                      "纯注入情境下摘要文案应保留（原行为不变）")

    def test_multimodal_untouched(self):
        """多模态（含图片）消息不压缩 —— 文本摘要无法表达附件。"""
        content = [
            {"type": "text", "text": "# AGENTS.md instructions for /repo\n<environment_context>x</environment_context>"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]
        self.assertIsNone(_compact_harness_message("user", content))

    def test_normal_message_untouched(self):
        """普通用户消息（无 harness 特征）不应被改动。"""
        self.assertIsNone(_compact_harness_message("user", "今天天气怎么样？"))


if __name__ == "__main__":
    unittest.main()
