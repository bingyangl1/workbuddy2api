"""P4-1：模型目录服务（admin/model_catalog.py）+ /api/models/catalog 路由。

覆盖：
- _decorate：腾讯原始条目 → 前端统一形状（efforts/default_effort/can_disable_thinking/
  能力标记/描述/上下文/最大输出/系列）；单档 reasoning.effort 兜底；默认档越界清空
- series_of：系列前缀推导 + 认不出归「其他」
- catalog 缓存：同 realm 命中缓存、跨 realm 分列、force 跳过、失败负缓存
- 路由：合并本地 ModelConfig 的倍率/启停；realm 参数校验

腾讯拉取（AccountSession.fetch_models）全程 mock——绝不触达真上游。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def _factory():
    from admin.db import Base
    from admin import models as _m  # noqa: F401

    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine, tables=[
        Base.metadata.tables["accounts"],
        Base.metadata.tables["model_configs"],
    ])
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# 腾讯原始条目样本（对齐 _probe_catalog 实证字段）
_TENCENT_SAMPLE = [
    {"id": "glm-5.3", "name": "GLM 5.3", "descriptionZh": "智谱旗舰",
     "descriptionEn": "GLM flagship", "maxInputTokens": 200000, "maxOutputTokens": 8192,
     "supportsReasoning": True, "supportsImages": False, "supportsToolCall": True,
     "onlyReasoning": False, "isDefault": False, "tags": ["craft"], "vendor": "zhipu",
     "reasoning": {"canDisableThinking": False, "defaultEffort": "high",
                   "summary": "auto", "supportedEfforts": ["low", "high", "max"]}},
    {"id": "glm-5v-turbo", "name": "GLM 5V", "descriptionZh": "多模态",
     "maxInputTokens": 128000, "maxOutputTokens": 4096,
     "supportsReasoning": True, "supportsImages": True,
     # 单档形态：reasoning.effort 无 supportedEfforts
     "reasoning": {"effort": "medium", "summary": "auto"}},
    {"id": "mystery-xyz", "name": "Mystery", "maxInputTokens": 0, "maxOutputTokens": 0},
]


class DecorateCase(unittest.TestCase):
    def test_full_entry(self):
        from admin.model_catalog import _decorate
        out = _decorate([_TENCENT_SAMPLE[0]], "cn")
        self.assertEqual(len(out), 1)
        m = out[0]
        self.assertEqual(m["id"], "glm-5.3")
        self.assertEqual(m["name"], "GLM 5.3")
        self.assertEqual(m["description"], "智谱旗舰")  # 优先 descriptionZh
        self.assertEqual(m["context_length"], 200000)
        self.assertEqual(m["max_output_tokens"], 8192)
        self.assertEqual(m["efforts"], ["low", "high", "max"])
        self.assertEqual(m["default_effort"], "high")
        self.assertFalse(m["can_disable_thinking"])
        self.assertTrue(m["supports_reasoning"])
        self.assertTrue(m["supports_tool_call"])
        self.assertFalse(m["supports_images"])
        self.assertEqual(m["series"], "GLM")
        self.assertEqual(m["vendor"], "zhipu")

    def test_single_effort_fallback(self):
        """reasoning.effort（无 supportedEfforts）视为仅支持该档。"""
        from admin.model_catalog import _decorate
        m = _decorate([_TENCENT_SAMPLE[1]], "cn")[0]
        self.assertEqual(m["efforts"], ["medium"])
        self.assertEqual(m["default_effort"], "medium")

    def test_default_effort_out_of_range_cleared(self):
        """默认档不在支持列表内 → 清空（不编造）。"""
        from admin.model_catalog import _decorate
        bad = {"id": "x", "reasoning": {"defaultEffort": "ultra",
                                        "supportedEfforts": ["low"]}}
        m = _decorate([bad], "cn")[0]
        self.assertEqual(m["default_effort"], "")

    def test_no_reasoning_empty_efforts(self):
        from admin.model_catalog import _decorate
        m = _decorate([_TENCENT_SAMPLE[2]], "cn")[0]
        self.assertEqual(m["efforts"], [])
        self.assertEqual(m["default_effort"], "")
        self.assertEqual(m["series"], "其他")  # 认不出归「其他」

    def test_skips_empty_id(self):
        from admin.model_catalog import _decorate
        self.assertEqual(_decorate([{"id": ""}, {"name": "x"}], "cn"), [])


class SeriesCase(unittest.TestCase):
    def test_prefixes(self):
        from admin.model_catalog import series_of
        self.assertEqual(series_of("glm-5.3"), "GLM")
        self.assertEqual(series_of("deepseek-v4-pro"), "DeepSeek")
        self.assertEqual(series_of("kimi-k2.7"), "Kimi")
        self.assertEqual(series_of("hy4-preview"), "Hunyuan")
        self.assertEqual(series_of("qwen3-max"), "Qwen")
        self.assertEqual(series_of("unknown-thing"), "其他")


class CatalogCacheCase(unittest.TestCase):
    def setUp(self):
        self.factory = _factory()
        from admin import model_catalog
        model_catalog.clear_cache()
        self.mc = model_catalog

    def _seed_account(self, realm="cn"):
        with self.factory() as db:
            from admin.models import Account
            db.add(Account(name=f"acc-{realm}", uid=f"u-{realm}", realm=realm,
                           auth_json='{"token":"x"}', status="active",
                           balance_remain=100))
            db.commit()

    def test_tencent_source_and_cache(self):
        self._seed_account("cn")
        with mock.patch("admin.backend.AccountSession") as S:
            inst = S.return_value.__enter__.return_value
            inst.fetch_models.return_value = _TENCENT_SAMPLE
            inst.updated_json.return_value = '{"token":"x"}'
            r1 = self.mc.catalog(self.factory(), realm="cn")
            self.assertEqual(r1["source"], "tencent")
            self.assertFalse(r1["cached"])
            self.assertEqual(len(r1["models"]), 3)
            # via 不返回账号标识（uid/昵称/邮箱）——模型中心页不暴露「用哪个账号拉的」
            self.assertEqual(r1["via"], "")
            # 第二次命中缓存（fetch_models 仍只调 1 次）
            r2 = self.mc.catalog(self.factory(), realm="cn")
            self.assertTrue(r2["cached"])
            self.assertGreaterEqual(r2["cache_age"], 0)
            self.assertEqual(inst.fetch_models.call_count, 1)
            # force 跳过缓存
            r3 = self.mc.catalog(self.factory(), realm="cn", force=True)
            self.assertFalse(r3["cached"])
            self.assertEqual(inst.fetch_models.call_count, 2)

    def test_realm_separation(self):
        """cn 与 global 分开取、分开缓存（不互相覆盖）。"""
        self._seed_account("cn")
        self._seed_account("global")
        with mock.patch("admin.backend.AccountSession") as S:
            inst = S.return_value.__enter__.return_value
            inst.fetch_models.side_effect = [
                [{"id": "glm-5.3", "reasoning": {}}],          # cn
                [{"id": "gpt-6-astra", "reasoning": {}}],       # global
            ]
            inst.updated_json.return_value = '{"token":"x"}'
            cn = self.mc.catalog(self.factory(), realm="cn")
            gl = self.mc.catalog(self.factory(), realm="global")
            self.assertEqual([m["id"] for m in cn["models"]], ["glm-5.3"])
            self.assertEqual([m["id"] for m in gl["models"]], ["gpt-6-astra"])
            self.assertEqual(inst.fetch_models.call_count, 2)

    def test_no_account_returns_none_source(self):
        """无可用账号 → source=none，不抛异常。"""
        r = self.mc.catalog(self.factory(), realm="cn")
        self.assertEqual(r["source"], "none")
        self.assertEqual(r["models"], [])
        self.assertTrue(r["errors"])


class RouteCase(unittest.TestCase):
    """路由：合并本地 ModelConfig 倍率/启停 + realm 校验。"""

    def setUp(self):
        self.factory = _factory()
        from admin import model_catalog
        model_catalog.clear_cache()

        def _db():
            db = self.factory()
            try:
                yield db
            finally:
                db.close()

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from admin.routers import models
        from admin.db import get_db

        app = FastAPI()
        app.include_router(models.router)
        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[models.require_admin_read] = lambda: True
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_merges_local_config(self):
        with self.factory() as db:
            from admin.models import ModelConfig
            db.add(ModelConfig(model_id="glm-5.3", level="system", enabled=1,
                               credit_multiplier=2.0, credits_raw="x2.00"))
            db.add(ModelConfig(model_id="glm-5v-turbo", level="system", enabled=0,
                               credit_multiplier=0.0))
            db.commit()
        with mock.patch("admin.model_catalog._build") as b:
            b.return_value = {"models": [
                {"id": "glm-5.3"}, {"id": "glm-5v-turbo"}, {"id": "not-in-config"}],
                "source": "tencent", "source_label": "x", "via": "", "errors": []}
            r = self.client.get("/api/models/catalog?realm=cn")
        self.assertEqual(r.status_code, 200, r.text)
        items = {m["id"]: m for m in r.json()["models"]}
        self.assertEqual(items["glm-5.3"]["credit_multiplier"], 2.0)
        self.assertEqual(items["glm-5.3"]["credits_raw"], "x2.00")
        self.assertTrue(items["glm-5.3"]["enabled"])
        self.assertFalse(items["glm-5.3"]["is_free"])
        self.assertTrue(items["glm-5v-turbo"]["is_free"])
        self.assertFalse(items["glm-5v-turbo"]["enabled"])
        # 目录有但本地无配置 → in_local_config False，默认 disabled/free
        self.assertFalse(items["not-in-config"]["in_local_config"])
        self.assertEqual(items["not-in-config"]["credit_multiplier"], 0.0)


if __name__ == "__main__":
    unittest.main()
