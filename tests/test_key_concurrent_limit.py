"""A1：每密钥并发上限（per-key override of global admission）。

## 为什么需要（上游 `8ae5c2df` 的动机）

全局 `GATEWAY_MAX_CONCURRENT` 是**所有密钥共享**的总闸。一个跑批客户端
（并发 5 路）会把默认 4 路的额度占满 → 其他密钥全部排队，而那不是他们的
额度问题。per-key 覆盖让重度/轻度用户互不干扰。

## 语义（三态，必须严格区分）

    None = 未设置 → 跟随全局（**存量密钥都是这个，零回归**）
    0    = 显式不限（该密钥可以打满，仍受账号层 ADMIN_POOL_MAX_IN_FLIGHT）
    N>0  = 最多 N 路

用 `None` 而不是 `0` 作默认，才能区分「没设置」与「显式不限」——
否则一旦把默认值写成 0，所有存量密钥都会突然变成「完全不限」，
全局闸门形同虚设。

## 本测试覆盖

* `resolve_concurrent()` 的全部输入组合（含脏数据：负数/非数字/字符串数字）
* 全局闸门与 per-key 的组合
* 模型/API 层：字段可创建、可编辑、可清回 None
* 静态接线：中间件必须把 per-key 值传进 `allow()`
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admin.gateway_limits import resolve_concurrent


class ResolveConcurrentCase(unittest.TestCase):
    """纯函数：全局上限 + per-key 覆盖 → 该密钥的实际上限。"""

    def test_none_follows_global(self):
        """None（未设置）→ 跟随全局。这是存量密钥的行为，必须零回归。"""
        for g in (0, 1, 4, 100):
            self.assertEqual(resolve_concurrent(g, None), g)

    def test_zero_means_unlimited(self):
        """0（显式不限）→ 不限制该密钥，即使全局开了闸。"""
        for g in (0, 4, 100):
            self.assertEqual(resolve_concurrent(g, 0), 0,
                             "0 必须表示「不限」而不是「跟随全局」")

    def test_positive_overrides_global(self):
        self.assertEqual(resolve_concurrent(4, 8), 8, "per-key 可以比全局更宽")
        self.assertEqual(resolve_concurrent(4, 2), 2, "也可以更窄")

    def test_positive_independent_of_global_zero(self):
        """全局没开（0）但 per-key 设了 N → 仍按 N 限。"""
        self.assertEqual(resolve_concurrent(0, 3), 3,
                         "全局未开时 per-key 依然要生效（否则该配置形同虚设）")

    def test_negative_falls_back_to_global(self):
        """负数（脏数据）→ 回落全局，不能被当成「不限」。"""
        for bad in (-1, -100):
            self.assertEqual(resolve_concurrent(4, bad), 4,
                             "负数不该被当成 0（那会变成「不限」，与意图相反）")

    def test_non_numeric_falls_back_to_global(self):
        for bad in ("abc", "", "  ", [], {}, object()):
            self.assertEqual(resolve_concurrent(4, bad), 4,
                             f"{bad!r} 应回落全局而不是崩溃/放开")

    def test_numeric_string_accepted(self):
        """数据库里可能存成字符串（某些驱动/ORM 路径）→ 应能正确解析。"""
        self.assertEqual(resolve_concurrent(4, "6"), 6)
        self.assertEqual(resolve_concurrent(4, "0"), 0)

    def test_bool_not_treated_as_number(self):
        """True/False 是 int 的子类，但语义上不该当并发数用。

        这里只要求**不崩**且结果落在合理集合（跟随全局或按 1/0）。
        """
        for b in (True, False):
            got = resolve_concurrent(4, b)
            self.assertIn(got, (4, 1, 0))


class SemanticsInvariantCase(unittest.TestCase):
    """不变量：per-key 生效时永远不可能是负数；None 永远等于全局。"""

    def test_result_never_negative(self):
        for g in (0, 1, 4, 999):
            for pk in (None, 0, 1, 4, 100, -1, -999, "3"):
                got = resolve_concurrent(g, pk)
                self.assertGreaterEqual(got, 0, f"({g}, {pk!r}) → {got}")

    def test_none_is_exactly_global(self):
        for g in range(0, 20):
            self.assertEqual(resolve_concurrent(g, None), g)


class ApiFieldCase(unittest.TestCase):
    """API 层：字段可创建/编辑/清回 None，且校验拒绝负数。"""

    def setUp(self):
        os.environ.setdefault("ADMIN_JWT_SECRET", "x" * 40)
        from admin.routers.keys import KeyIn, _validate_concurrent
        self.KeyIn = KeyIn
        self.validate = _validate_concurrent

    def test_default_is_none(self):
        """新建密钥不传该字段 → None（跟随全局）。"""
        self.assertIsNone(self.KeyIn().max_concurrent)

    def test_validate_accepts_three_states(self):
        self.assertIsNone(self.validate(None))
        self.assertEqual(self.validate(0), 0)
        self.assertEqual(self.validate(5), 5)
        self.assertEqual(self.validate("5"), 5)

    def test_validate_rejects_negative(self):
        """负数必须 400 —— 静默当 0（不限）会与「本想限制」的意图相反。"""
        from fastapi import HTTPException
        for bad in (-1, -100):
            with self.assertRaises(HTTPException):
                self.validate(bad)

    def test_validate_rejects_non_numeric(self):
        from fastapi import HTTPException
        for bad in ("abc", [], {}):
            with self.assertRaises(HTTPException):
                self.validate(bad)


class ModelFieldCase(unittest.TestCase):
    def test_model_has_nullable_column(self):
        from admin.models import ApiKey
        col = ApiKey.__table__.columns.get("max_concurrent")
        self.assertIsNotNone(col, "ApiKey 必须有 max_concurrent 列")
        self.assertTrue(col.nullable,
                        "必须 nullable —— None 表示「跟随全局」，"
                        "若 NOT NULL 就表达不了这个状态")

    def test_migration_present(self):
        """老库升级路径上必须显式加列（create_all 不会给已有表加列）。"""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "admin" / "db.py").read_text(
            encoding="utf-8")
        self.assertIn('_ensure_column("api_keys", "max_concurrent"', src,
                      "db.py 缺少 max_concurrent 的迁移语句 → 老库升级后该字段不存在")


class AllowBehaviorCase(unittest.TestCase):
    """**行为级**：`allow()` 必须真的采纳 per-key 值。

    只测 `resolve_concurrent()` 是不够的 —— 把它从 `allow()` 里删掉，
    纯函数测试依然全过（假绿）。这里直接打 `allow()`。
    """

    def setUp(self):
        from admin import gateway_limits as gl
        self.gl = gl
        gl._windows.clear()
        gl._active.clear()

    def tearDown(self):
        self.gl._windows.clear()
        self.gl._active.clear()

    def _set_global(self, concurrent, rate=0):
        from admin.config import settings
        self._orig = (getattr(settings, "GATEWAY_MAX_CONCURRENT", 0),
                      getattr(settings, "GATEWAY_RATE_PER_MINUTE", 0))
        settings.GATEWAY_MAX_CONCURRENT = concurrent
        settings.GATEWAY_RATE_PER_MINUTE = rate

    def tearDown2(self):
        from admin.config import settings
        if hasattr(self, "_orig"):
            settings.GATEWAY_MAX_CONCURRENT, settings.GATEWAY_RATE_PER_MINUTE = self._orig

    def test_per_key_limit_enforced(self):
        """**核心行为**：per-key=1 时，第二个并发请求必须被拒。"""
        self._set_global(10)          # 全局很宽
        try:
            a = self.gl.allow("k1", 1)    # per-key=1
            self.assertFalse(isinstance(a, tuple), f"第一个应放行，实际 {a}")
            b = self.gl.allow("k1", 1)    # 第 2 个应被拒
            self.assertIsInstance(b, tuple, "per-key=1 时第二个并发必须被拒")
            self.gl.release(a)
            c = self.gl.allow("k1", 1)    # 释放后又可以
            self.assertFalse(isinstance(c, tuple), "释放后应能再进")
            self.gl.release(c)
        finally:
            self.tearDown2()

    def test_global_limit_used_when_per_key_none(self):
        """per-key=None → 用全局值（零回归）。"""
        self._set_global(1)
        try:
            a = self.gl.allow("k2", None)
            self.assertFalse(isinstance(a, tuple))
            b = self.gl.allow("k2", None)
            self.assertIsInstance(b, tuple, "全局=1 时第二个应被拒")
            self.gl.release(a)
        finally:
            self.tearDown2()

    def test_per_key_zero_means_unlimited(self):
        """per-key=0 → 不限（即使全局=1）。"""
        self._set_global(1)
        try:
            leases = []
            for _ in range(5):
                r = self.gl.allow("k3", 0)
                self.assertFalse(isinstance(r, tuple),
                                 "per-key=0 应完全不限（全局=1 也不该拦）")
                leases.append(r)
            for l in leases:
                self.gl.release(l)
        finally:
            self.tearDown2()

    def test_per_key_wider_than_global(self):
        """per-key 比全局更宽 → 以 per-key 为准。"""
        self._set_global(1)
        try:
            a = self.gl.allow("k4", 3)
            b = self.gl.allow("k4", 3)
            self.assertFalse(isinstance(a, tuple))
            self.assertFalse(isinstance(b, tuple),
                             "per-key=3 时第二个应放行（全局=1 不该限制它）")
            self.gl.release(a)
            self.gl.release(b)
        finally:
            self.tearDown2()

    def test_per_key_works_when_global_zero(self):
        """全局=0（未开闸）但 per-key=N → 仍按 N 限。

        这条守护中间件「全局为 0 时不能直接短路」的修正。
        """
        self._set_global(0)
        try:
            a = self.gl.allow("k5", 1)
            self.assertFalse(isinstance(a, tuple))
            b = self.gl.allow("k5", 1)
            self.assertIsInstance(b, tuple,
                                  "全局未开时 per-key 依然要生效")
            self.gl.release(a)
        finally:
            self.tearDown2()


class WiringCase(unittest.TestCase):
    """静态接线：中间件必须把 per-key 值传进 allow()。"""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.src = (Path(__file__).resolve().parent.parent
                   / "admin" / "gateway_limits.py").read_text(encoding="utf-8")

    def test_allow_accepts_per_key(self):
        import inspect
        from admin.gateway_limits import allow
        self.assertIn("per_key_concurrent", inspect.signature(allow).parameters)

    def test_valid_key_id_returns_concurrent(self):
        import inspect
        from admin.gateway_limits import _valid_key_id
        doc = inspect.getdoc(_valid_key_id) or ""
        self.assertIn("max_concurrent", doc,
                      "_valid_key_id 应一次带回 max_concurrent（避免热路径二次查库）")

    def test_middleware_passes_it(self):
        self.assertIn("allow, key, per_key", self.src,
                      "中间件必须把 per-key 值传给 allow()，否则该配置不生效")

    def test_shortcut_not_bypassed_by_global_zero(self):
        """全局闸都为 0 时不能直接短路 —— 否则 per-key 单独开的情况失效。"""
        i = self.src.find("if scope['type'] != 'http'")
        self.assertGreater(i, 0)
        line = self.src[i:i + 200].split("\n")[0]
        self.assertNotIn("_limits()", line,
                         "快速跳出不该包含 _limits() 判断 —— "
                         "全局未开但 per-key 开了时，这里会整段跳过")


if __name__ == "__main__":
    unittest.main()
