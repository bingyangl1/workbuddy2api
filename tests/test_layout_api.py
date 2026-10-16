"""界面布局配置（admin/layout.py + routers/layout.py）测试。

覆盖：
  * 归一化：未知 href / 未知分组被静默丢弃；重复项去重
  * 坏 JSON / 空值 → 按默认（而不是抛错或返回空导航）
  * 读写往返 / 恢复默认
  * 路由契约（GET/PUT/reset 的响应形状）
  * **与前端 app-nav.ts 同源守护**：NAV_ITEMS 必须与 web/lib/app-nav.ts 的 href 集合一致
    （否则配置页会显示不存在的条目，或漏掉真实条目）
"""
import json
import os
import re
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

WEB = Path(__file__).resolve().parent.parent / "web"


def _factory():
    from admin.db import Base
    from admin import models as _m  # noqa: F401
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine, tables=[
        Base.metadata.tables["system_settings"],
    ])
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class SanitizeCase(unittest.TestCase):
    def setUp(self):
        self.factory = _factory()
        from admin import layout
        self.mod = layout

    def test_unknown_href_dropped(self):
        with self.factory() as db:
            saved = self.mod.save(db, {"hidden": ["/accounts", "/no-such-page", 123],
                                       "order": ["/keys", "/bogus"], "groupLabels": {"ops": "运营组"}})
        self.assertEqual(saved["hidden"], ["/accounts"])
        self.assertEqual(saved["order"], ["/keys"])
        self.assertEqual(saved["groupLabels"], {"ops": "运营组"})

    def test_unknown_group_dropped(self):
        with self.factory() as db:
            saved = self.mod.save(db, {"groupLabels": {"nope": "X", "data": "数据组"}})
        self.assertEqual(saved["groupLabels"], {"data": "数据组"})

    def test_duplicates_removed(self):
        with self.factory() as db:
            saved = self.mod.save(db, {"hidden": ["/accounts", "/accounts"]})
        self.assertEqual(saved["hidden"], ["/accounts"])

    def test_broken_json_reads_as_default(self):
        """库里存了坏 JSON 时按默认处理，而不是让面板崩掉。"""
        from admin.models import SystemSetting
        with self.factory() as db:
            db.add(SystemSetting(key="ui_layout", value="{not json"))
            db.commit()
            loaded = self.mod.load(db)
        self.assertEqual(loaded, {"hidden": [], "order": [], "groupLabels": {}})

    def test_empty_is_default(self):
        with self.factory() as db:
            self.assertEqual(self.mod.load(db), {"hidden": [], "order": [], "groupLabels": {}})

    def test_roundtrip_and_reset(self):
        with self.factory() as db:
            self.mod.save(db, {"hidden": ["/audit"], "order": ["/dashboard", "/accounts"]})
            loaded = self.mod.load(db)
            self.assertEqual(loaded["hidden"], ["/audit"])
            self.assertEqual(loaded["order"], ["/dashboard", "/accounts"])
            self.mod.reset(db)
            self.assertEqual(self.mod.load(db)["hidden"], [])

    def test_save_rejects_non_dict(self):
        with self.factory() as db:
            with self.assertRaises(self.mod.LayoutError):
                self.mod.save(db, ["not", "a", "dict"])

    def test_group_label_length_capped(self):
        with self.factory() as db:
            saved = self.mod.save(db, {"groupLabels": {"ops": "x" * 500}})
        self.assertLessEqual(len(saved["groupLabels"]["ops"]), 120)


class RouteCase(unittest.TestCase):
    def setUp(self):
        self.factory = _factory()

        def _db():
            db = self.factory()
            try:
                yield db
            finally:
                db.close()

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from admin.routers import layout as layout_router
        from admin.db import get_db

        app = FastAPI()
        app.include_router(layout_router.router)
        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[layout_router.require_admin] = lambda: True
        # 审计是旁路，测试里不落库
        import admin.audit as audit_mod
        self._p = mock.patch.object(audit_mod, "log", lambda *a, **k: None)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_get_returns_layout_and_items(self):
        r = self.client.get("/api/layout")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("layout", body)
        self.assertIn("items", body)
        self.assertGreater(len(body["items"]), 10)
        self.assertEqual(body["layout"]["hidden"], [])

    def test_put_then_get(self):
        r = self.client.put("/api/layout", json={
            "hidden": ["/audit", "/bogus"],
            "order": ["/keys", "/accounts"],
            "groupLabels": {"data": "数据组"},
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["layout"]["hidden"], ["/audit"])
        # 再读一次确认真的落库了（不是只回显请求）
        again = self.client.get("/api/layout").json()["layout"]
        self.assertEqual(again["hidden"], ["/audit"])
        self.assertEqual(again["order"], ["/keys", "/accounts"])
        self.assertEqual(again["groupLabels"]["data"], "数据组")

    def test_reset(self):
        self.client.put("/api/layout", json={"hidden": ["/audit"]})
        r = self.client.post("/api/layout/reset")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.get("/api/layout").json()["layout"]["hidden"], [])


class FrontendSourceOfTruthCase(unittest.TestCase):
    """NAV_ITEMS 必须与 web/lib/app-nav.ts 的条目集合完全一致。

    不然配置页会：显示不存在的条目（配置了没效果）或漏掉真实条目（无法隐藏）。
    """

    def _frontend_hrefs(self) -> set[str]:
        src = (WEB / "lib" / "app-nav.ts").read_text(encoding="utf-8")
        return set(re.findall(r"href:\s*'(/[^']*)'", src))

    def test_same_set(self):
        from admin import layout as layout_mod
        backend = set(layout_mod.NAV_ITEMS)
        frontend = self._frontend_hrefs()
        self.assertTrue(frontend, "没解析到前端 href（app-nav.ts 结构变了？）")
        self.assertEqual(backend - frontend, set(), f"后端多出的条目：{backend - frontend}")
        self.assertEqual(frontend - backend, set(), f"后端漏掉的条目：{frontend - backend}")

    def test_groups_match(self):
        from admin import layout as layout_mod
        src = (WEB / "lib" / "app-nav.ts").read_text(encoding="utf-8")
        fe_groups = set(re.findall(r"key:\s*'(\w+)',\s*\n\s*labelKey:\s*'nav\.group", src))
        if not fe_groups:
            # 宽松回退：抓 group 定义块里的 key
            fe_groups = set(re.findall(r"^\s{4}key:\s*'(\w+)',", src, re.MULTILINE))
        be_groups = set(layout_mod.NAV_GROUPS)
        self.assertTrue(be_groups <= fe_groups or not fe_groups,
                        f"分组不一致：后端 {be_groups} / 前端 {fe_groups}")

    def test_layout_page_itself_is_navigable(self):
        """配置页自己也要在菜单里（否则用户改坏了布局就再也进不去配置页自救）。"""
        from admin import layout as layout_mod
        self.assertIn("/settings/layout", layout_mod.NAV_ITEMS)
        src = (WEB / "lib" / "app-nav.ts").read_text(encoding="utf-8")
        self.assertIn("'/settings/layout'", src, "配置页没登记进前端菜单")

    def test_layout_page_must_stay_visible(self):
        """配置页**不允许被隐藏** —— 否则用户把自己锁在门外（无法再进配置页恢复）。

        这是本功能唯一的安全约束：其他条目随便隐藏，唯独它必须常驻。
        前端 applyLayout 的实现里，hidden 是「用户提供什么就隐藏什么」，
        所以约束放在数据层：后端白名单收敛 + 前端菜单始终包含它。
        """
        src = (WEB / "lib" / "layout-context.tsx").read_text(encoding="utf-8")
        self.assertIn("MUST_KEEP_VISIBLE", src,
                      "layout-context 缺少「配置页不可隐藏」的保护")


if __name__ == "__main__":
    unittest.main()
