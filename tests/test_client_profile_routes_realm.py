"""`/api/client-profile/*` 必须支持按 realm 操作。

## 为什么（用户实测指出的风控问题）

> 「客户端参数是需要同时存国际版和国内版的，否则风控肯定是无法通过的，
>   对应版本要用对应的客户端参数和对应的上游。」

两版客户端身份**本质不同**（解包实测）：

| 字段 | cn | global |
|---|---|---|
| applicationName | `WorkBuddy` | `workbuddy-ai` |
| desktop_version | 5.8.3 | 5.8.4 |
| turingChannelId | **109144** | **400111** |
| turingProductName | `WorkBuddy` | `WorkBuddy AI` |

只存一份的话，国际版账号会带着国内版的品牌段与渠道号出站 —— 上游一比对
就知道不是官方国际版客户端。

## 本测试覆盖（零上游请求）

* 三个读取/写入/重置路由都接受 `realm`
* 两版的 identity 与 UA **必须不同**（分档生效的直接证据）
* 按 realm 保存**互不影响**；`reset?realm=X` **只清 X**
* 非法 realm 的行为：reset/save 拒绝，读取宽松（按 cn）
* 响应含 `realms` 并排摘要（设置页一次看全两版）
* `sources(db, realm)` 也要按版返回（排障用）
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def _db():
    from admin.db import Base
    from admin import models as _m  # noqa: F401
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine, tables=[
        Base.metadata.tables["system_settings"],
    ])
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


class RouteRealmCase(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("ADMIN_JWT_SECRET", "x" * 40)
        self.db = _db()
        from admin.routers import client_profile as R
        self.R = R

    def tearDown(self):
        self.db.close()

    def _get(self, realm="cn"):
        return self.R.get_profile(realm=realm, _=None, db=self.db)

    # ── 签名 ──
    def test_all_routes_accept_realm(self):
        import inspect
        for fn, default in ((self.R.get_profile, "cn"), (self.R.detect, "cn"),
                            (self.R.reset, "cn")):
            params = inspect.signature(fn).parameters
            self.assertIn("realm", params, f"{fn.__name__} 必须接受 realm")
            self.assertEqual(params["realm"].default, default,
                             f"{fn.__name__} 的 realm 默认应为 {default}")
        # save 走 body，检查模型字段
        self.assertIn("realm", self.R.SaveIn.model_fields)

    # ── 身份分档（核心） ──
    def test_two_realms_have_distinct_identity(self):
        """两版的关键身份**必须不同** —— 这是分档生效的直接证据。"""
        cn = self._get("cn")["effective"]
        gl = self._get("global")["effective"]
        for key in ("application_name", "turing_channel_id",
                    "turing_product_name", "desktop_version"):
            self.assertIn(key, cn, f"cn 缺 {key}")
            self.assertIn(key, gl, f"global 缺 {key}")
            self.assertNotEqual(cn[key], gl[key],
                                f"{key} 两版不该相同（混用是风控特征）")

    def test_realm_echoed(self):
        self.assertEqual(self._get("cn").get("realm"), "cn")
        self.assertEqual(self._get("global").get("realm"), "global")

    def test_ua_differs_by_realm(self):
        cn_ua = self.R.cprofile.ua(realm="cn")
        gl_ua = self.R.cprofile.ua(realm="global")
        self.assertNotEqual(cn_ua, gl_ua, "两版 UA 不该相同")
        self.assertIn("WorkBuddy/", cn_ua)
        self.assertIn("workbuddy-ai/", gl_ua)

    # ── 并排摘要 ──
    def test_realms_overview_present(self):
        """响应要含两版并排摘要（否则设置页得来回切才发现另一版没配）。"""
        p = self._get()
        realms = p.get("realms") or {}
        self.assertEqual(sorted(realms.keys()), ["cn", "global"])
        for r in ("cn", "global"):
            self.assertIn("source", realms[r])
            self.assertIn("application_name", realms[r])
            self.assertIn("ua", realms[r])

    def test_overview_two_realms_differ(self):
        realms = self._get().get("realms") or {}
        self.assertNotEqual(realms["cn"].get("application_name"),
                            realms["global"].get("application_name"))
        self.assertNotEqual(realms["cn"].get("turing_channel_id"),
                            realms["global"].get("turing_channel_id"))

    # ── 隔离性 ──
    def test_save_per_realm_isolated(self):
        self.R.save(self.R.SaveIn(profile={"os_version": "10.0.22631"}, realm="cn"),
                    _=None, db=self.db)
        self.R.save(self.R.SaveIn(profile={"os_version": "10.0.26100"}, realm="global"),
                    _=None, db=self.db)
        self.assertEqual(self._get("cn")["saved"].get("os_version"), "10.0.22631")
        self.assertEqual(self._get("global")["saved"].get("os_version"), "10.0.26100")

    def test_reset_only_affects_given_realm(self):
        self.R.save(self.R.SaveIn(profile={"os_version": "10.0.22631"}, realm="cn"),
                    _=None, db=self.db)
        self.R.save(self.R.SaveIn(profile={"os_version": "10.0.26100"}, realm="global"),
                    _=None, db=self.db)
        self.R.reset(realm="global", _=None, db=self.db)
        self.assertEqual(self._get("cn")["saved"].get("os_version"), "10.0.22631",
                         "**重置 global 不该清掉 cn**")
        self.assertEqual(self._get("global")["saved"], {})

    # ── 非法 realm ──
    def test_reset_rejects_invalid_realm(self):
        with self.assertRaises(Exception) as ctx:
            self.R.reset(realm="mars", _=None, db=self.db)
        self.assertIn("cn", str(ctx.exception).lower() + str(ctx.exception))

    def test_save_rejects_invalid_realm(self):
        with self.assertRaises(Exception):
            self.R.save(self.R.SaveIn(profile={"os_version": "x"}, realm="mars"),
                        _=None, db=self.db)

    def test_get_tolerates_unknown_realm(self):
        """读取宽松（未知值按 cn）—— 读操作不该因为参数打错就 500。"""
        try:
            self._get("mars")
        except Exception as e:      # noqa: BLE001
            self.fail(f"读取不该抛异常：{type(e).__name__}: {e}")

    # ── 探测 ──
    def test_detect_reports_realm_and_label(self):
        for r, label in (("cn", "国内版"), ("global", "国际版")):
            d = self.R.detect(realm=r, _=None)
            self.assertEqual(d.get("realm"), r)
            self.assertIn(label, d.get("message") or "",
                          f"{r} 的提示文案应指明探测的是{label}")


class SourcesRealmCase(unittest.TestCase):
    """`sources(db, realm)` 按版返回（排障用：看每个键取自哪一层）。"""

    def setUp(self):
        os.environ.setdefault("ADMIN_JWT_SECRET", "x" * 40)
        self.db = _db()

    def tearDown(self):
        self.db.close()

    def test_sources_accepts_realm(self):
        import inspect
        from admin import client_profile as cp
        params = inspect.signature(cp.sources).parameters
        self.assertIn("realm", params)
        params2 = inspect.signature(cp.ClientProfileStore.sources).parameters
        self.assertIn("realm", params2, "Store 层也要支持（模块函数只是转发）")

    def test_sources_returns_dict_per_realm(self):
        from admin import client_profile as cp
        for r in ("cn", "global"):
            out = cp.sources(self.db, realm=r)
            self.assertIsInstance(out, dict)
            self.assertTrue(out, f"{r} 的 sources 不该为空（至少有兜底值）")

    def test_sources_differ_when_saved_differs(self):
        """两版保存值不同 → sources 的「已保存」判定也应各按各的。"""
        from admin import client_profile as cp
        cp.save({"os_version": "10.0.1"}, db=self.db, realm="cn")
        cp.save({"os_version": "10.0.2"}, db=self.db, realm="global")
        import json
        from admin.models import SystemSetting
        cn_raw = self.db.query(SystemSetting).filter(
            SystemSetting.key == "client_profile:cn").first()
        gl_raw = self.db.query(SystemSetting).filter(
            SystemSetting.key == "client_profile:global").first()
        self.assertIsNotNone(cn_raw, "cn 应存在 client_profile:cn")
        self.assertIsNotNone(gl_raw, "global 应存在 client_profile:global")
        self.assertNotEqual(json.loads(cn_raw.value)["os_version"],
                            json.loads(gl_raw.value)["os_version"])


if __name__ == "__main__":
    unittest.main()
