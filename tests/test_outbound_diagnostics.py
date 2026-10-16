"""出站请求诊断 —— 一行日志定位 `11128` 的**哪一类**成因。

## 为什么需要这个能力

`400 code=11128 "Illegal API invocation from an unapproved channel"` 有
**至少四种**互不相关的成因，而客户端看到的现象**完全一样**：

  1. `X-Device-Token` 缺失（桌面端不在 / Turing SDK 取不到 / 负缓存命中）
  2. harness 脱敏没生效（system/tools 里的合规声明词被内容审核拦）
  3. UA / 渠道号与账号的 realm 不匹配（国际版账号带国内版身份出站）
  4. 会话头族缺失（`X-Conversation-ID` / `-Request-ID` / `-Message-ID`）

排查时只能「一行行加 print」逐个排除，非常低效；而且第 1、2 类在**简单 hello
测试里都会通过**（hello 不带 harness 模板、桌面端在时指纹也有），所以很容易
误判成「账号被封」或「模型不支持」。

`_describe_outbound_headers()` 把这些特征**一次打全**：看一行日志即可定位类别。

## 本测试覆盖

* 头齐全时如实报告（UA / 指纹 / 三个会话头 / 风控头 4 项）
* **缺哪一项就明确标 `**MISSING**`**（这是价值所在 —— 不能只报「有/无」）
* 风控头部分缺失时报出**具体缺了哪个**
* 请求体特征（模型名 / tools 数量 / system 字段类型）
* **绝不抛异常**（诊断代码本身不能成为新的故障点）
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class OutboundDiagnosticsCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("ADMIN_JWT_SECRET", "x" * 40)
        try:
            from admin.routers.proxy import _describe_outbound_headers
        except Exception as e:      # pragma: no cover
            raise unittest.SkipTest(f"proxy 导入失败：{e}")
        cls.fn = staticmethod(_describe_outbound_headers)

    def _full(self):
        return {
            "User-Agent": "workbuddy-ai/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0",
            "X-Device-Token": "t" * 32,
            "X-Conversation-ID": "a" * 32,
            "X-Conversation-Request-ID": "b" * 32,
            "X-Conversation-Message-ID": "c" * 32,
            "X-IDE-Name": "workbuddy-ai",
            "X-IDE-Type": "workbuddy-ai",
            "X-IDE-Version": "5.8.4",
            "X-Product": "SaaS",
        }

    def test_reports_all_present(self):
        out = self.fn(self._full(), tag="chat", uid="bf798385-b868-4660",
                      body={"model": "auto", "tools": [1, 2, 3], "system": []})
        for token in ("[chat]", "device_token=OK(32ch)", "conv_id=OK(32ch)",
                      "conv_req=OK(32ch)", "conv_msg=OK(32ch)", "risk=4/4"):
            self.assertIn(token, out, f"应报出 {token}；实际：{out}")
        self.assertIn("uid=bf798385-b86", out, "uid 只取前 12 位（脱敏）")
        self.assertNotIn("bf798385-b868-4660", out, "不该打完整 uid")

    def test_missing_device_token_flagged(self):
        """**最关键的一类**：设备指纹缺失必须显眼标出。"""
        h = self._full()
        h.pop("X-Device-Token")
        out = self.fn(h, tag="chat-stream", uid="u" * 16)
        self.assertIn("device_token=**MISSING**", out,
                      "指纹缺失是 11128 最常见成因，必须一眼可见")

    def test_missing_conversation_headers_flagged(self):
        h = self._full()
        h.pop("X-Conversation-ID")
        out = self.fn(h, tag="responses", body={"model": "glm-5.1"})
        self.assertIn("conv_id=**MISSING**", out)
        self.assertIn("conv_req=OK", out, "其它会话头仍应如实报出")

    def test_partial_risk_headers_name_the_missing_one(self):
        """风控头部分缺失时要点出**具体缺哪个**（只说 3/4 无法定位）。"""
        h = self._full()
        h.pop("X-IDE-Name")
        out = self.fn(h, tag="chat")
        self.assertIn("risk=3/4", out)
        self.assertIn("risk_missing=['X-IDE-Name']", out,
                      "必须报出缺失的具体头名")

    def test_missing_ua_flagged(self):
        h = self._full()
        h.pop("User-Agent")
        out = self.fn(h, tag="chat")
        self.assertIn("UA=**MISSING**", out)

    def test_body_summary(self):
        out = self.fn(self._full(), tag="chat",
                      body={"model": "cn:glm-5.1", "tools": [1, 2], "system": "text"})
        self.assertIn("model=cn:glm-5.1", out)
        self.assertIn("tools=2", out)
        self.assertIn("sys=str", out, "system 可能是 str 或 list，报类型便于判断脱敏是否生效")

    def test_body_absent_is_fine(self):
        out = self.fn(self._full(), tag="chat")
        self.assertIn("[chat]", out)
        self.assertNotIn("body=", out)

    def test_empty_headers_do_not_raise(self):
        """**诊断代码本身不能成为故障点**：各种畸形输入都不能抛异常。"""
        for bad in ({}, None, {"User-Agent": None}, {"X-Device-Token": ""},
                    {"X-Conversation-ID": 12345}):
            try:
                self.fn(bad if bad is not None else {}, tag="t")
            except Exception as e:      # noqa: BLE001
                self.fail(f"输入 {bad!r} 抛异常：{type(e).__name__}: {e}")

    def test_long_ua_truncated(self):
        """超长 UA 要截断，避免把日志撑爆。"""
        h = self._full()
        h["User-Agent"] = "X" * 400
        out = self.fn(h, tag="chat")
        self.assertLess(len(out), 500, f"输出过长（{len(out)} 字符）")
        self.assertNotIn("X" * 100, out, "UA 应被截断")


class CallSiteWiringCase(unittest.TestCase):
    """静态接线：两条出站路径都必须带诊断（否则排查时又是「有的路径没日志」）。"""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.src = (Path(__file__).resolve().parent.parent
                   / "admin" / "routers" / "proxy.py").read_text(encoding="utf-8")

    def test_both_outbound_paths_logged(self):
        for tag in ("chat-stream", "chat-post"):
            self.assertIn(f'tag="{tag}"', self.src,
                          f"**{tag} 路径没有出站诊断** —— "
                          f"排查 11128 时会缺这一条的现场信息")

    def test_diagnostics_are_guarded(self):
        """诊断调用必须包在 try 里（不能因为日志失败而让请求失败）。"""
        import re
        checked = 0
        for m in re.finditer(r"_describe_outbound_headers\(", self.src):
            # 跳过函数**定义**本身，只看调用点
            if self.src[max(0, m.start() - 4):m.start()].rstrip().endswith("def"):
                continue
            # 往前的 300 字符窗口里应有 try:
            head = self.src[max(0, m.start() - 300):m.start()]
            self.assertIn("try:", head,
                          "诊断调用必须在 try 块内（诊断不能成为故障点）")
            checked += 1
        self.assertGreaterEqual(checked, 2,
                                f"应至少有两处调用点（两条出站路径），实际 {checked}")


if __name__ == "__main__":
    unittest.main()
