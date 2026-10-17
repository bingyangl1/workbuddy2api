"""多轮审计发现的 realm 一致性缺口 —— 回归测试。

用户要求「整个项目多审计几遍」，共做了 9 轮（静态扫描 → 数据流追踪 →
AST 精确分类 → 路径 → 同类 bug 模式 → 选号/调度 → 跨模块不变量 →
运行时缓存 → 模型前缀链路）。本文件锁定其中**修掉的 6 个真缺口**。

## 修掉的缺口

### ① `fetch_appearance_themes` 用硬编码 `BILL_BASE`
模块级常量 = `www.codebuddy.cn`（国内版）→ 国际版账号打国内版 billing 域。

### ② `converter` 的 3 个资源缓存是**单槽**（不分 realm）
`_MODELS_CACHE` / `_BALANCE_CACHE` / `_CATALOG_CACHE` 原来都是
`{"ts","data"}` 单槽。本机凭证切到另一版后仍返回上一版的模型清单/余额
（最长 60s）。两版清单**交集只有 5 个**，拿错版本会让模型名全部对不上。

### ③ `converter.py` 另有两处 realm 缺口
* `/v2/billing/meter/get-user-resource` 写死 `/v2` 前缀（国际版该用无前缀）
* `CapacityUnit != "credits"` 严格相等（gift 包 `credit` 被丢）——
  与我们先前在 `admin/billing.py` 修的**同一个 bug 的另一份拷贝**

### ④ UA 兜底路径用固定版本（已在上一轮修，这里加守护）

### ⑤ 模块级 `DESKTOP_VERSION` / `DESKTOP_UA` / `USER_AGENT` 是**单值**
虽然是导入期快照、当前无代码引用，但保留单值会诱导后来者误用 → 改为按 realm 字典。

### ⑥ `/v1/messages` 没把剥前缀的裸模型名写回 `chat_body`
`anthropic_request_to_chat()` **原样保留** payload 的 model，所以
`global:xxx` 会一路带到出站请求 —— 上游只认裸名。
`/v1/responses` 一直有这一行，只有 messages 漏了 → 用前缀调 Anthropic
协议必然「模型不存在」，不带前缀时正常，极易误判为客户端问题。

---
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ADMIN_JWT_SECRET", "x" * 40)


class HardcodedBaseCase(unittest.TestCase):
    """缺口①：出站基址不能写死模块级域常量。"""

    @classmethod
    def setUpClass(cls):
        cls.src = (Path(__file__).resolve().parent.parent
                   / "admin" / "backend.py").read_text(encoding="utf-8")

    def test_appearance_themes_uses_realm_base(self):
        i = self.src.find("def fetch_appearance_themes")
        self.assertGreater(i, 0)
        body = self.src[i:i + 1200]
        self.assertIn("_billing_base()", body,
                      "外观主题必须按 realm 取 billing 基址")
        self.assertNotIn("BILL_BASE +", body,
                         "**不能**再用硬编码 BILL_BASE（那是国内版域）")

    def test_web_base_is_realm_aware(self):
        """web 域也要按 realm（原来是 `WEB_BASE + \"/v2/report\"`）。"""
        self.assertIn("def _web_base", self.src, "应有 realm 感知的 _web_base()")
        i = self.src.find("def report_web_event")
        body = self.src[i:i + 1400]
        self.assertIn("_web_base()", body,
                      "web 上报应走 _web_base() 而不是 WEB_BASE 常量")

    def test_no_other_hardcoded_module_base_in_outbound(self):
        """排除注释后，出站 URL 拼接里不该再出现 WEB_BASE / BILL_BASE。"""
        bad = []
        for n, line in enumerate(self.src.splitlines(), 1):
            s = line.strip()
            if s.startswith("#") or s.startswith('"""'):
                continue
            if ("WEB_BASE +" in s or "BILL_BASE +" in s) and "http" not in s:
                bad.append(f"L{n}: {s[:80]}")
        self.assertEqual(bad, [], f"仍有硬编码基址拼接：{bad}")


class ConverterCacheCase(unittest.TestCase):
    """缺口②：converter 的资源缓存必须按 realm 分槽。"""

    @classmethod
    def setUpClass(cls):
        cls.src = (Path(__file__).resolve().parent.parent
                   / "converter.py").read_text(encoding="utf-8")

    def test_caches_are_dicts_not_single_slot(self):
        for name in ("_MODELS_CACHE", "_BALANCE_CACHE", "_CATALOG_CACHE"):
            # 定义应为 `NAME: dict = {}`（或 `NAME = {}`），而不是带 "ts" 的单槽
            self.assertNotRegex(
                self.src, rf"(?m)^{name}\s*=\s*\{{\s*[\"']ts",
                f"{name} 仍是单槽定义 → 两版会串数据")
            # 定义必须存在
            self.assertRegex(
                self.src, rf"(?m)^{name}\s*(?::\s*dict)?\s*=\s*\{{",
                f"{name} 未按预期定义")

    def test_realm_key_helper_exists(self):
        self.assertIn("def _realm_key", self.src)
        i = self.src.find("def _realm_key")
        body = self.src[i:i + 900]
        self.assertIn("realm", body)

    def test_cached_functions_use_realm_slot(self):
        for fn in ("_cached_models", "_cached_balance", "_cached_catalog"):
            i = self.src.find(f"def {fn}")
            self.assertGreater(i, 0, f"{fn} 不存在")
            end = self.src.find("\ndef ", i + 10)
            body = self.src[i:end if end > 0 else len(self.src)]
            self.assertIn("_realm_key(cred)", body,
                          f"**{fn} 没有按 realm 分槽** —— 切换版本后会返回旧数据")
            self.assertIn("setdefault(rk", body.replace(" ", "").replace("setdefault(rk,", "setdefault(rk")
                          or True, "")   # 占位，实际断言在上一条

    def test_behavior_isolated_per_realm(self):
        """行为级：两版各取各的缓存，互不覆盖。"""
        import converter as cv

        class _FakeCred:
            def __init__(self, realm, models):
                self.realm = realm
                self._models = models
                self.calls = 0
            def fetch_models(self):
                self.calls += 1
                return self._models

        cv._MODELS_CACHE.clear()
        cn = _FakeCred("cn", [{"id": "hy3"}])
        gl = _FakeCred("global", [{"id": "default-model"}])
        a = cv._cached_models(cn)
        b = cv._cached_models(gl)
        self.assertEqual(a[0]["id"], "hy3")
        self.assertEqual(b[0]["id"], "default-model",
                         "国际版拿到了国内版的缓存 → 分槽没生效")
        # 二次调用应命中各自缓存（不再调 fetch）
        cv._cached_models(cn)
        cv._cached_models(gl)
        self.assertEqual(cn.calls, 1)
        self.assertEqual(gl.calls, 1)


class ConverterBillingCase(unittest.TestCase):
    """缺口③：converter 的 billing 路径与单位判定。"""

    @classmethod
    def setUpClass(cls):
        cls.src = (Path(__file__).resolve().parent.parent
                   / "converter.py").read_text(encoding="utf-8")

    def test_billing_path_realm_aware(self):
        """余额查询的 /v2 前缀方向要按 realm 走。"""
        i = self.src.find("def fetch_balance")
        if i < 0:
            i = self.src.find("get-user-resource")
        body = self.src[max(0, i - 200):i + 2200]
        self.assertIn("billing_paths", body,
                      "应使用 realm_mod.billing_paths 取候选路径")

    def test_credit_unit_tolerant(self):
        """**同一个单复数 bug 的第二份拷贝**必须也修。"""
        self.assertIn("_is_credit_unit", self.src,
                      "converter 里的 CapacityUnit 判定也要容忍单复数")
        self.assertNotIn('a.get("CapacityUnit") != "credits"', self.src,
                         "仍存在严格相等判定 → gift 包会被丢")

    def test_all_capacity_unit_checks_unified(self):
        """全库不该再有「严格等于 credits」的单位判定。"""
        root = Path(__file__).resolve().parent.parent
        needle = 'CapacityUnit") != "credits"'
        offenders = []
        # 只扫**交付代码**：排除以 `_` 开头的临时/草稿脚本与测试目录，
        # 否则「临时验证脚本里写着坏字符串」会让这条断言误报
        # （真实踩过：我的反向验证脚本把它当注入内容，导致这里假失败）。
        files = [p for p in root.glob("*.py") if not p.name.startswith("_")] + \
                [p for p in (root / "admin").glob("*.py") if not p.name.startswith("_")]
        for p in files:
            t = p.read_text(encoding="utf-8", errors="ignore")
            for n, line in enumerate(t.splitlines(), 1):
                if line.strip().startswith("#"):
                    continue
                if needle in line:
                    offenders.append(f"{p.name}:{n}")
        self.assertEqual(offenders, [],
                         f"这些地方仍用严格相等判单位：{offenders}")


class ModuleConstantCase(unittest.TestCase):
    """缺口⑤：模块级版本常量必须是按 realm 的字典。"""

    def test_backend_constants_are_dicts(self):
        from admin import backend
        for name in ("DESKTOP_VERSION", "CLI_VERSION", "DESKTOP_UA"):
            v = getattr(backend, name)
            self.assertIsInstance(v, dict,
                                  f"backend.{name} 应是按 realm 的字典，"
                                  f"实际 {type(v).__name__}")
            self.assertIn("cn", v)
            self.assertIn("global", v)

    def test_backend_constants_differ_by_realm(self):
        from admin import backend
        self.assertNotEqual(backend.DESKTOP_VERSION["cn"],
                            backend.DESKTOP_VERSION["global"])
        self.assertNotEqual(backend.DESKTOP_UA["cn"],
                            backend.DESKTOP_UA["global"])

    def test_converter_constants_are_dicts(self):
        import converter as cv
        for name in ("DESKTOP_VERSION", "CLI_VERSION", "USER_AGENT"):
            v = getattr(cv, name)
            self.assertIsInstance(v, dict, f"converter.{name} 应是字典")

    def test_converter_constants_differ(self):
        import converter as cv
        self.assertNotEqual(cv.DESKTOP_VERSION["cn"],
                            cv.DESKTOP_VERSION["global"])
        self.assertNotEqual(cv.USER_AGENT["cn"], cv.USER_AGENT["global"])


class MessagesModelPrefixCase(unittest.TestCase):
    """缺口⑥：`/v1/messages` 必须把裸模型名写回 chat_body。"""

    @classmethod
    def setUpClass(cls):
        cls.src = (Path(__file__).resolve().parent.parent
                   / "admin" / "routers" / "proxy.py").read_text(encoding="utf-8")

    def test_anthropic_adapter_keeps_prefix(self):
        """先证明「转换函数确实保留前缀」——否则本缺口不成立。"""
        from anthropic_adapter import anthropic_request_to_chat
        out = anthropic_request_to_chat({
            "model": "global:glm-5.1", "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(out.get("model"), "global:glm-5.1",
                         "转换函数会保留前缀 → 所以必须显式写回裸名")

    def test_messages_writes_bare_model_back(self):
        i = self.src.find('router.post("/v1/messages")')
        self.assertGreater(i, 0)
        end = self.src.find("@router.", i + 20)
        body = self.src[i:end if end > 0 else len(self.src)]
        self.assertIn('chat_body["model"] = model_bare', body,
                      "**/v1/messages 没有把裸名写回 chat_body** —— "
                      "带前缀调 Anthropic 协议会「模型不存在」")

    def test_responses_also_writes_bare_model(self):
        """对照项：/v1/responses 一直是对的，不能改坏。"""
        i = self.src.find('router.post("/v1/responses")')
        end = self.src.find('router.post("/v1/messages")')
        body = self.src[i:end]
        self.assertIn('chat_body["model"] = model_bare', body)


class NoSingleSlotCacheCase(unittest.TestCase):
    """R1 通则：模块级「缓存」字典不该是单槽形态。"""

    def test_no_ts_data_single_slot_caches(self):
        root = Path(__file__).resolve().parent.parent
        # 同样只扫交付代码（排除 `_` 开头的临时脚本）
        files = [p for p in root.glob("*.py") if not p.name.startswith("_")] + \
                [p for p in (root / "admin").glob("*.py") if not p.name.startswith("_")] + \
                [p for p in (root / "admin" / "routers").glob("*.py")
                 if not p.name.startswith("_")]
        bad = []
        import re
        for p in files:
            try:
                t = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for n, line in enumerate(t.splitlines(), 1):
                # 形如 `_X_CACHE = {"ts": 0.0, "data": None}`
                if re.match(r"^_[A-Z_]*(CACHE|MEMO)\w*\s*=\s*\{\s*[\"']ts[\"']",
                            line):
                    bad.append(f"{p.name}:{n}: {line.strip()[:70]}")
        self.assertEqual(bad, [],
                         f"这些缓存仍是单槽（两版会串数据）：{bad}")


if __name__ == "__main__":
    unittest.main()
