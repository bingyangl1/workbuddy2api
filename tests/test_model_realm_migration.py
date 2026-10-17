"""`model_configs` 的 realm 列迁移与唯一约束。

## 改造前后

**改造前**：两版靠 `model_id` 的字符串前缀区分（国际版 `global:xxx`）。

能工作，但：

  * 没有唯一约束 —— 手写 SQL 能插两条 `global:xxx`
  * 每个读写点都得记得剥/加前缀（漏一处就出 bug）
  * **计费真的错过**：调国际版免费模型按国内版倍率扣费（已修）

**改造后**：显式 `realm` 列 + 裸 `model_id` + 唯一约束
`(level, realm, model_id)`。

## 本测试守什么

1. 迁移把 `global:` 前缀搬到 realm 列（数据不丢）
2. 迁移幂等（跑两次结果一致）
3. 唯一约束真的拦重复
4. **两版同名不算重复**（这是设计要点 —— 它们本来就是两条独立配置）
5. 存量裸名行的 realm 默认是 `cn`
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class ModelRealmMigrationCase(unittest.TestCase):
    """用独立 SQLite 库验证迁移逻辑（不碰真实 dev 库）。"""

    def _make_legacy_db(self):
        """建一个「改造前形态」的库：没有 realm 列，model_id 带前缀。"""
        import tempfile
        from sqlalchemy import create_engine, text

        tmp = tempfile.mkdtemp(prefix="wb_migrate_")
        path = f"{tmp}/legacy.db".replace("\\", "/")
        eng = create_engine(f"sqlite:///{path}", future=True)
        with eng.begin() as conn:
            conn.execute(text("""
                CREATE TABLE model_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level VARCHAR(16),
                    model_id VARCHAR(120) NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    note VARCHAR(255) DEFAULT '',
                    credit_multiplier FLOAT DEFAULT 0,
                    credits_raw VARCHAR(120) DEFAULT '',
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """))
            # 改造前的真实形态：国内版裸名，国际版 global: 前缀
            rows = [
                ("system", "hy3", 1, 0.0),
                ("system", "hy4-preview", 1, 0.29),
                ("system", "glm-5.3", 1, 0.79),
                ("system", "global:hy3", 1, 0.0),
                ("system", "global:hy4-preview", 1, 0.0),
                ("system", "global:default-model", 1, 0.5),
            ]
            for lv, mid, en, mult in rows:
                conn.execute(text(
                    "INSERT INTO model_configs "
                    "(level, model_id, enabled, credit_multiplier) "
                    "VALUES (:lv, :mid, :en, :mult)"),
                    {"lv": lv, "mid": mid, "en": en, "mult": mult})
        return eng, path

    def _run_migration(self, eng):
        """在给定 engine 上跑与 admin.db 同样的迁移步骤。

        直接复用 `admin.db` 的实现（monkeypatch 它的 module-level `engine`），
        保证测的就是生产代码，而不是复制一份。
        """
        from admin import db as dbmod
        old_engine = dbmod.engine
        dbmod.engine = eng
        try:
            dbmod._ensure_column("model_configs", "realm",
                                 "VARCHAR(16)", "DEFAULT 'cn'")
            dbmod._migrate_model_realm()
            dbmod._ensure_unique(
                "model_configs", "uq_model_configs_level_realm_model",
                ("level", "realm", "model_id"))
        finally:
            dbmod.engine = old_engine

    def _rows(self, eng):
        from sqlalchemy import text
        with eng.connect() as conn:
            return conn.execute(text(
                "SELECT level, realm, model_id, credit_multiplier "
                "FROM model_configs ORDER BY id")).fetchall()

    def test_prefix_moved_to_realm(self):
        """`global:` 前缀搬到 realm 列，model_id 变裸名。"""
        eng, _p = self._make_legacy_db()
        self._run_migration(eng)
        rows = self._rows(eng)
        pairs = {(r[1], r[2]) for r in rows}
        self.assertIn(("global", "hy4-preview"), pairs,
                      "**`global:hy4-preview` 应变成 realm='global' + 裸名**")
        self.assertIn(("cn", "hy4-preview"), pairs,
                      "国内版裸名行应保留（realm 默认 cn）")
        # 不该再有带冒号的名字
        for r in rows:
            self.assertNotIn(":", str(r[2]),
                             f"迁移后 model_id 不该带前缀：{r[2]!r}")

    def test_data_not_lost(self):
        """行数与倍率都不能丢。"""
        from sqlalchemy import text
        eng, _p = self._make_legacy_db()
        # 迁移前行数要从**旧表结构**读（那时还没有 realm 列）
        with eng.connect() as conn:
            before = conn.execute(
                text("SELECT COUNT(*) FROM model_configs")).scalar()
        self._run_migration(eng)
        after = self._rows(eng)
        self.assertEqual(len(after), before,
                         f"**迁移不能丢行**（{before} → {len(after)}）")
        mults = {(r[1], r[2]): r[3] for r in after}
        self.assertEqual(mults[("cn", "hy4-preview")], 0.29)
        self.assertEqual(mults[("global", "hy4-preview")], 0.0,
                         "**两版倍率必须各自保留**")

    def test_idempotent(self):
        """跑两次结果相同。"""
        eng, _p = self._make_legacy_db()
        self._run_migration(eng)
        first = sorted((r[0], r[1], r[2]) for r in self._rows(eng))
        self._run_migration(eng)
        second = sorted((r[0], r[1], r[2]) for r in self._rows(eng))
        self.assertEqual(first, second, "**迁移必须幂等**")

    def test_unique_blocks_duplicate(self):
        """唯一约束真的拦重复。"""
        eng, _p = self._make_legacy_db()
        self._run_migration(eng)
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError
        with self.assertRaises(IntegrityError,
                               msg="**同 level+realm+model_id 必须被唯一约束拦住**"):
            with eng.begin() as conn:
                conn.execute(text(
                    "INSERT INTO model_configs "
                    "(level, realm, model_id, enabled) "
                    "VALUES ('system', 'global', 'hy3', 1)"))

    def test_two_realms_same_name_not_duplicate(self):
        """**设计要点**：两版同名是两条独立配置，不该被判重复。"""
        eng, _p = self._make_legacy_db()
        self._run_migration(eng)
        from sqlalchemy import text
        # hy3 两版都有 —— 插入第三条（再一个 realm）应成功
        with eng.begin() as conn:
            conn.execute(text(
                "INSERT INTO model_configs "
                "(level, realm, model_id, enabled) "
                "VALUES ('user', 'cn', 'hy3', 1)"))
        rows = [r for r in self._rows(eng) if r[2] == "hy3"]
        self.assertGreaterEqual(
            len(rows), 3,
            "level/realm 不同时不该被判重复（唯一键是三元组）")


class ModelConfigSchemaCase(unittest.TestCase):
    """模型定义本身必须带 realm 与唯一约束。"""

    @classmethod
    def setUpClass(cls):
        from admin.models import ModelConfig
        cls.MC = ModelConfig

    def test_has_realm_column(self):
        self.assertIn("realm", self.MC.__table__.columns,
                      "**ModelConfig 必须有 realm 列** —— "
                      "靠字符串前缀区分两版太脆弱")

    def test_realm_not_nullable(self):
        col = self.MC.__table__.columns["realm"]
        self.assertFalse(col.nullable,
                         "realm 不该允许 NULL（否则归属不明）")

    def test_has_unique_constraint(self):
        names = {c.name for c in self.MC.__table__.constraints
                 if c.__class__.__name__ == "UniqueConstraint"}
        self.assertIn(
            "uq_model_configs_level_realm_model", names,
            "**必须有 (level, realm, model_id) 唯一约束** —— "
            "防止手写 SQL 插重复")

    def test_model_id_is_bare(self):
        """model_id 的注释应说明它存裸名（不带 realm 前缀）。"""
        col = self.MC.__table__.columns["model_id"]
        self.assertFalse(col.nullable)


class MigrationHelpersCase(unittest.TestCase):
    """`admin/db.py` 里应有迁移辅助。"""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.src = (Path(__file__).resolve().parent.parent / "admin"
                   / "db.py").read_text(encoding="utf-8")

    def test_has_migrate_function(self):
        self.assertIn("def _migrate_model_realm", self.src,
                      "应有 `_migrate_model_realm()` 迁移函数")

    def test_has_ensure_unique(self):
        self.assertIn("def _ensure_unique", self.src,
                      "应有 `_ensure_unique()` —— create_all 对已存在的表不做事，"
                      "老库的新约束必须显式补")

    def test_called_in_init_db(self):
        """两个辅助都必须在 `init_db()` 的 `_do()` 里被真正调用。

        ⚠️ 不能只查 `"_migrate_model_realm()" in src` —— 那个字符串在
        **函数定义行**（`def _migrate_model_realm() -> None:`）里也有，
        于是把调用删掉、断言照样通过（反向验证抓到过这个假绿）。

        所以这里必须切出 `init_db` 的函数体再断言。
        """
        import re
        m = re.search(r"def init_db\(\).*?\n(?=def )", self.src, re.S)
        self.assertIsNotNone(m, "找不到 init_db")
        body = m.group(0)

        self.assertRegex(
            body, r"\n\s+_migrate_model_realm\(\)",
            "**必须在 init_db 里调用 `_migrate_model_realm()`** —— "
            "只定义不调用等于没写")
        self.assertRegex(
            body, r"_ensure_unique\(\s*\"model_configs\"",
            "**必须在 init_db 里为 model_configs 建唯一约束**")


if __name__ == "__main__":
    unittest.main()
