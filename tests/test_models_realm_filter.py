"""模型配置必须按 realm 过滤。

## 用户实测报告的问题

> 运行 dev.ps1 -Live 切到国际版，模型 tab 同步后显示的**依旧是国内版的**

## 根因

`GET /api/models/configs` **完全不接受 `realm` 参数** —— 无条件返回
`ModelConfig` 全表，两版混在一起。

实测两版清单**交集只有 5 个**：

    cn     32 个（hy3 / hy4-preview / deepseek-* 那套）
    global 18 个（default-model / balanced-model 那套）

混在一起时，切到国际版看到的是「国内版 + 国际版」的大杂烩 ——
而用户会理解成「同步了但没生效」。

## 为什么同步是好的、显示是坏的

`POST /api/models/sync` 早就按 realm 挑账号了（那是之前的修复）。
问题在**读**这一侧：同步写进了正确的版本，但列表读的时候不分版本。

## 本测试守什么

1. `realm=cn` 只返回国内版（无 `global:` 前缀项）
2. `realm=global` 只返回国际版，且**前缀已剥掉**（上游只认裸名）
3. 不传 realm 返回全部（兼容旧调用）
4. **该版无模型时返回空，绝不跨版兜底** —— 拿另一版顶上比返回空更误导
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CN_MODELS = ["hy3", "hy4-preview", "deepseek-v4.1-flash", "glm-5.1"]
GL_MODELS = ["default-model", "balanced-model", "claude-sonnet-4.5"]


class ModelsRealmFilterCase(unittest.TestCase):
    """用独立 SQLite 库验证过滤（不污染开发库）。"""

    @classmethod
    def setUpClass(cls):
        # 独立 engine + 独立 app 实例，避免影响其它测试
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from admin.db import Base
        from admin import models as _m  # noqa: F401

        cls.tmp = tempfile.mkdtemp(prefix="wb_mdl_realm_")
        cls.engine = create_engine(
            f"sqlite:///{cls.tmp}/t.db".replace("\\", "/"), future=True)
        Base.metadata.create_all(bind=cls.engine)
        cls.Session = sessionmaker(bind=cls.engine, future=True)

        db = cls.Session()
        try:
            from admin.models import ModelConfig
            for mid in CN_MODELS:
                db.add(ModelConfig(model_id=mid, level="system",
                                   enabled=1, credit_multiplier=1))
            for mid in GL_MODELS:
                db.add(ModelConfig(model_id=f"global:{mid}", level="system",
                                   enabled=1, credit_multiplier=1))
            db.commit()
        finally:
            db.close()

    def _query(self, **params):
        """直接调路由函数（绕过 app 的 get_db 依赖注入）。"""
        from admin.routers.models import list_configs
        db = self.Session()
        try:
            return list_configs(level=None, db=db, _=True, **params)
        finally:
            db.close()

    def test_cn_only(self):
        """realm=cn → 只含国内版。"""
        res = self._query(realm="cn")
        ids = {i["model_id"] for i in res["items"]}
        self.assertEqual(ids, set(CN_MODELS),
                         f"realm=cn 应只返回国内版，实际 {sorted(ids)}")
        self.assertFalse(any(i.startswith("global:") for i in ids),
                         "国内版清单里不该有 global: 前缀项")

    def test_global_only_and_prefix_stripped(self):
        """realm=global → 只含国际版，且前缀已剥掉。"""
        res = self._query(realm="global")
        ids = {i["model_id"] for i in res["items"]}
        self.assertEqual(ids, set(GL_MODELS),
                         f"realm=global 应只返回国际版（裸名），实际 {sorted(ids)}")
        for i in ids:
            self.assertFalse(i.startswith("global:"),
                             f"**前缀必须剥掉**：上游只认裸名，实际 {i!r}")

    def test_no_realm_returns_all(self):
        """不传 realm → 返回全部（兼容旧调用）。"""
        res = self._query()
        n = len(res["items"])
        self.assertEqual(n, len(CN_MODELS) + len(GL_MODELS),
                         f"不传 realm 应返回全部，实际 {n} 项")

    def test_no_cross_realm_fallback(self):
        """该版无模型 → 返回空，**绝不跨版兜底**。

        拿另一版的模型顶上比返回空更误导：用户会以为国际版有那些模型，
        实际请求过去必然失败。

        ⚠️ 本测试会删掉 global 模型，所以**用独立库**，避免污染其它用例
        （第一版没这么做，导致 `test_no_realm_returns_all` 因为数据被删而失败）。
        """
        import tempfile as _tf

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from admin.db import Base
        from admin.models import ModelConfig
        from admin.routers.models import list_configs

        tmp = _tf.mkdtemp(prefix="wb_mdl_empty_")
        eng = create_engine(f"sqlite:///{tmp}/t.db".replace("\\", "/"),
                            future=True)
        Base.metadata.create_all(bind=eng)
        Sess = sessionmaker(bind=eng, future=True)
        db = Sess()
        try:
            # 只有国内版模型，国际版为空
            for mid in CN_MODELS:
                db.add(ModelConfig(model_id=mid, level="system", enabled=1))
            db.commit()
            res = list_configs(level=None, realm="global", db=db, _=True)
        finally:
            db.close()
            eng.dispose()
        self.assertEqual(res["items"], [],
                         "**国际版无模型时必须返回空** —— 不能拿国内版顶上")

    def test_returns_realm_field(self):
        """响应里带上 realm，便于前端确认当前看的是哪一版。"""
        for r in ("cn", "global"):
            res = self._query(realm=r)
            self.assertEqual(res.get("realm"), r)
        self.assertEqual(self._query().get("realm"), "all")


class FrontendPassesRealmCase(unittest.TestCase):
    """前端必须真的把 realm 传给后端（否则后端改了也没用）。"""

    @classmethod
    def setUpClass(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        self.api = (root / "web" / "lib" / "api.ts").read_text(encoding="utf-8")
        self.page = (root / "web" / "app" / "(main)" / "models"
                     / "page.tsx").read_text(encoding="utf-8")

    def test_api_accepts_realm(self):
        """`modelApi.configs` 必须接受 realm 参数。"""
        import re
        m = re.search(r"configs:\s*\(([^)]*)\)", self.api)
        self.assertIsNotNone(m, "找不到 configs 定义")
        self.assertIn("realm", m.group(1),
                      "**configs 必须接受 realm 参数**，否则后端过滤无从生效")
        self.assertIn("realm", self.api[m.start():m.start() + 400],
                      "configs 应把 realm 拼进查询参数")

    def test_page_passes_realm(self):
        """模型页调用时必须传 realm。"""
        self.assertIn("modelApi.configs(undefined, realm)", self.page,
                      "**模型页必须传 realm** —— 不传会拿到两版混合清单")
        self.assertIn("modelApi.busy(realm)", self.page,
                      "busy 也应带 realm（两版上游是两个域）")

    def test_load_depends_on_realm(self):
        """realm 变化时必须重新加载。"""
        import re
        m = re.search(r"const load = useCallback\(async \(\) => \{(.*?)\n  \}, \[([^\]]*)\]\)",
                      self.page, re.S)
        self.assertIsNotNone(m, "找不到 load 的 useCallback")
        deps = m.group(2)
        self.assertIn("realm", deps,
                      "**load 的依赖数组必须含 realm** —— 否则切换版本后列表不刷新")


if __name__ == "__main__":
    unittest.main()
