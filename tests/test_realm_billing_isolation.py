"""两版同名模型的计费必须各算各的。

## 用户担心的问题（真实存在过）

> 国际版和国内版的模型有一样的，你不会只存一个吧？他们计费是不一样的。

### 存储层：没问题

`ModelConfig` 靠 `model_id` 前缀区分两版：

    hy4-preview          → cn     倍率 0.29
    global:hy4-preview   → global 倍率 0.00（免费）

物理上是两条独立记录，不会互相覆盖。

### 计费层：**曾经有 bug**

`_record_usage()` 里按 `ModelConfig.model_id == model` 查倍率，
而 `model` 在 proxy 入口就被剥成了裸名：

    if model_realm:
        model = model_bare      # "global:hy4-preview" → "hy4-preview"

于是**永远是命中 cn 那一行** —— 调用国际版免费模型（0.00）
却按国内版 0.29 扣费。用户多付积分。

### 修法

`_record_usage` 新增/复用了 `requested_model`（**客户端原始请求名，带前缀**）
来查配置，查不到再退回裸名。

## 本测试守什么

1. 带 `global:` 前缀 → 命中国际版配置（不是国内版）
2. 带 `cn:` 前缀 → 命中 cn 配置
3. 裸名 → 命中 cn 配置（默认行为不变）
4. **同一模型两版倍率不同时，必须各取各的**
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class BillingLookupCase(unittest.TestCase):
    """复刻 `_record_usage` 的查找逻辑，验证 realm 区分。"""

    @classmethod
    def setUpClass(cls):
        """用独立 SQLite，数据可控（不依赖本机 dev 库）。"""
        import tempfile

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from admin.db import Base
        from admin.models import ModelConfig

        cls.tmp = tempfile.mkdtemp(prefix="wb_billing_")
        cls.engine = create_engine(
            f"sqlite:///{cls.tmp}/t.db".replace("\\", "/"), future=True)
        Base.metadata.create_all(bind=cls.engine)
        cls.Session = sessionmaker(bind=cls.engine, future=True)

        db = cls.Session()
        try:
            # 与 dev 库真实数据一致：同名模型两版倍率不同
            db.add(ModelConfig(model_id="hy4-preview", level="system",
                               enabled=1, credit_multiplier=0.29,
                               credits_raw="x0.29"))
            db.add(ModelConfig(model_id="global:hy4-preview", level="system",
                               enabled=1, credit_multiplier=0.00,
                               credits_raw="x0.00"))
            # 两版都免费的模型
            db.add(ModelConfig(model_id="hy3", level="system",
                               enabled=1, credit_multiplier=0.0))
            db.add(ModelConfig(model_id="global:hy3", level="system",
                               enabled=1, credit_multiplier=0.0))
            db.commit()
        finally:
            db.close()

    def _lookup(self, requested: str, model: str):
        """复刻修复后的查找逻辑（见 proxy.py::_record_usage）。"""
        from admin.models import ModelConfig
        db = self.Session()
        try:
            lookup = (requested or model or "").strip()
            mc = None
            if lookup:
                mc = (db.query(ModelConfig)
                      .filter(ModelConfig.model_id == lookup,
                              ModelConfig.enabled == 1).first())
            if mc is None and lookup != (model or "").strip():
                mc = (db.query(ModelConfig)
                      .filter(ModelConfig.model_id == (model or ""),
                              ModelConfig.enabled == 1).first())
            return mc
        finally:
            db.close()

    def test_global_prefix_hits_global_config(self):
        """**核心**：`global:` 前缀必须命中国际版配置。"""
        mc = self._lookup("global:hy4-preview", "hy4-preview")
        self.assertIsNotNone(mc)
        self.assertEqual(mc.model_id, "global:hy4-preview",
                         "**带 global: 前缀时必须命中国际版那行** —— "
                         "否则会按国内版倍率扣费")
        self.assertEqual(mc.credit_multiplier, 0.0)

    def test_cn_prefix_hits_cn_config(self):
        mc = self._lookup("cn:hy4-preview", "hy4-preview")
        self.assertIsNotNone(mc)
        self.assertEqual(mc.model_id, "hy4-preview")
        self.assertEqual(mc.credit_multiplier, 0.29)

    def test_bare_name_defaults_to_cn(self):
        """裸名（未声明 realm）→ cn 配置，行为不变。"""
        mc = self._lookup("hy4-preview", "hy4-preview")
        self.assertEqual(mc.model_id, "hy4-preview")
        self.assertEqual(mc.credit_multiplier, 0.29)

    def test_two_realms_get_different_multipliers(self):
        """**用户真正关心的**：同名模型两版倍率不同，必须各算各的。"""
        cn = self._lookup("hy4-preview", "hy4-preview")
        gl = self._lookup("global:hy4-preview", "hy4-preview")
        self.assertNotEqual(
            cn.credit_multiplier, gl.credit_multiplier,
            f"两版倍率应不同（cn={cn.credit_multiplier} "
            f"global={gl.credit_multiplier}）—— 若相同说明查到了同一行")

    def test_falls_back_when_prefix_row_missing(self):
        """带前缀但库里没有该行 → 退回裸名（不能让计费变成 0）。"""
        mc = self._lookup("global:no-such-model", "hy4-preview")
        # 前缀行不存在，退回裸名 hy4-preview
        self.assertIsNotNone(mc)
        self.assertEqual(mc.model_id, "hy4-preview")


class ProxyUsesRequestedModelCase(unittest.TestCase):
    """静态检查：`_record_usage` 必须用 `requested_model` 查配置。"""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        cls.src = (root / "admin" / "routers" / "proxy.py").read_text(
            encoding="utf-8")

    def test_lookup_uses_realm_and_bare_name(self):
        """**行为级**断言：必须按 `(realm, 裸名)` 查配置。

        改造后归属存进显式的 `ModelConfig.realm` 列，所以查找必须：

          1. 从 `requested_model` 解析出 realm（`resolve_model_prefix`）
          2. 按 `ModelConfig.realm == rv` 过滤
          3. 用**裸名**匹配 `ModelConfig.model_id`

        不能只查 `"requested_model" in body` —— 反向验证抓到过假绿：
        mutation 把逻辑改掉时，注释里仍有那个词，断言照样通过。
        """
        import re
        m = re.search(
            r"def _record_usage\(.*?(?=\ndef |\nclass )", self.src, re.S)
        self.assertIsNotNone(m, "找不到 _record_usage")
        body = m.group(0)
        # 必须解析 realm
        self.assertIn("resolve_model_prefix", body,
                      "**估算倍率时必须先解析模型的 realm** —— "
                      "只用裸名查会永远命中 cn 那行")
        # 必须按 realm 列过滤
        self.assertRegex(
            body, r"ModelConfig\.realm\s*==",
            "**必须按 ModelConfig.realm 过滤** —— 否则两版同名模型查询会串")
        # 必须从 requested_model 取原始名（带前缀）
        self.assertRegex(
            body, r"requested_model",
            "应从 requested_model（客户端原始名）解析 realm")

    def test_comment_explains_realm(self):
        """注释里应说明两版同名模型倍率不同（防后人「简化」掉）。"""
        import re
        m = re.search(
            r"def _record_usage\(.*?(?=\ndef |\nclass )", self.src, re.S)
        body = m.group(0)
        self.assertRegex(
            body, r"realm|global",
            "注释应解释两版倍率不同、必须按 realm 查")


if __name__ == "__main__":
    unittest.main()
