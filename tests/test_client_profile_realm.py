"""客户端参数**按 realm 分档**的测试（两版参数本质不同，混用是风控特征）。

## 为什么必须分档（实测证据）

用 node 实跑两个客户端的官方构造逻辑（`cli/product.json` 的 `applicationName`
+ `productVersion`，见 asar 内 `app-instance.js`）：

    国内版 D:\\Software\\WorkBuddy    applicationName=WorkBuddy
        → `WorkBuddy/5.8.3 WorkBuddy/5.8.3 CLI/2.161.0`
    国际版 D:\\Software\\WorkBuddyAI  applicationName=workbuddy-ai
        → `workbuddy-ai/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0`

两版**渠道号也不同**（109144 vs 400111）。只存一份的话，国际版账号会带着
国内版的品牌段 + 国内版渠道号出站 —— 上游一比对就知道不是官方国际版客户端。

## 本测试覆盖

* `_REALM_DEFAULTS`：两版的兜底身份互不相同且取值正确
* `ua(realm)` / `turing_channel_id(realm)` / `risk_headers(realm)` 按 realm 分开
* **向后兼容**：旧的单份 `client_profile` 键只当 cn 那份，**不得被 global 继承**
  （继承等于把国内版身份套到国际版请求上）
* 两版 db 键名正确（`client_profile:cn` / `client_profile:global`）
"""
import json
import os
import sys
import unittest
from pathlib import Path

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


class RealmDefaultsCase(unittest.TestCase):
    def test_two_realms_differ(self):
        """两版的关键身份必须不同（否则分档没意义）。"""
        from admin import client_profile as cp
        cn = cp._REALM_DEFAULTS["cn"]
        gl = cp._REALM_DEFAULTS["global"]
        for k in ("ide_name", "turing_product_name", "turing_channel_id",
                  "application_name", "ext_name"):
            self.assertIn(k, cn, f"cn 缺 {k}")
            self.assertIn(k, gl, f"global 缺 {k}")
            self.assertNotEqual(cn[k], gl[k], f"{k} 两版不该相同")

    def test_known_values(self):
        """取值对齐客户端实测。"""
        from admin import client_profile as cp
        self.assertEqual(cp._REALM_DEFAULTS["cn"]["application_name"], "WorkBuddy")
        self.assertEqual(cp._REALM_DEFAULTS["global"]["application_name"], "workbuddy-ai")
        self.assertEqual(cp._REALM_DEFAULTS["cn"]["turing_channel_id"], 109144)
        self.assertEqual(cp._REALM_DEFAULTS["global"]["turing_channel_id"], 400111)
        # 国际版产品名是 "WorkBuddy AI"（productName），但 UA 品牌段是 workbuddy-ai
        self.assertEqual(cp._REALM_DEFAULTS["global"]["turing_product_name"], "WorkBuddy AI")

    def test_wb_install_realm_fallbacks(self):
        """wb_install 的兜底也要按 realm 分（它是被 client_profile 调用的下层）。"""
        from wb_install import REALM_FALLBACKS
        self.assertEqual(REALM_FALLBACKS["cn"]["turing_channel_id"], 109144)
        self.assertEqual(REALM_FALLBACKS["global"]["turing_channel_id"], 400111)
        self.assertEqual(REALM_FALLBACKS["global"]["application_name"], "workbuddy-ai")


class NormRealmCase(unittest.TestCase):
    def test_norm_realm(self):
        from admin.client_profile import _norm_realm
        self.assertEqual(_norm_realm("cn"), "cn")
        self.assertEqual(_norm_realm("global"), "global")
        self.assertEqual(_norm_realm("GLOBAL"), "global")
        # 未知值 fail-closed 到 cn（与引擎其余处一致）
        self.assertEqual(_norm_realm(""), "cn")
        self.assertEqual(_norm_realm(None), "cn")
        self.assertEqual(_norm_realm("mars"), "cn")


class KeyNamesCase(unittest.TestCase):
    """DB 键名必须是按 realm 分开的（否则两版会互相覆盖）。"""

    def setUp(self):
        self.db = _db()

    def tearDown(self):
        self.db.close()

    def test_saved_keys_are_per_realm(self):
        from admin import client_profile as cp
        from admin.models import SystemSetting
        cp.save({"desktop_version": "5.8.3"}, db=self.db, realm="cn")
        cp.save({"desktop_version": "5.8.4"}, db=self.db, realm="global")
        keys = {r.key for r in self.db.query(SystemSetting).all()}
        self.assertIn("client_profile:cn", keys)
        self.assertIn("client_profile:global", keys)
        cn = json.loads(self.db.query(SystemSetting).filter(
            SystemSetting.key == "client_profile:cn").first().value)
        gl = json.loads(self.db.query(SystemSetting).filter(
            SystemSetting.key == "client_profile:global").first().value)
        self.assertEqual(cn["desktop_version"], "5.8.3")
        self.assertEqual(gl["desktop_version"], "5.8.4")

    def test_legacy_single_key_only_applies_to_cn(self):
        """**核心安全约束**：旧单份键只当 cn 那份，global 不得继承。

        继承的后果：国际版请求带上国内版的身份（品牌段 WorkBuddy、
        渠道号 109144、版本 5.8.3）—— 正是要修的风控问题。
        """
        from admin import client_profile as cp
        from admin.models import SystemSetting
        legacy = {"ide_name": "WorkBuddy", "desktop_version": "5.8.3",
                  "turing_channel_id": 109144, "turing_product_name": "WorkBuddy"}
        self.db.add(SystemSetting(key="client_profile",
                                  value=json.dumps(legacy, ensure_ascii=False)))
        self.db.add(SystemSetting(key="client_profile_source", value="saved"))
        self.db.commit()

        cp.STORE.refresh(self.db)
        cn = cp.STORE.saved("cn")
        gl = cp.STORE.saved("global")
        self.assertEqual(cn.get("desktop_version"), "5.8.3", "cn 应继承旧单份键")
        self.assertEqual(gl, {}, "global **不该**继承旧单份键（那是国内版身份）")

    def test_save_global_does_not_touch_cn(self):
        from admin import client_profile as cp
        cp.save({"desktop_version": "5.8.3"}, db=self.db, realm="cn")
        cp.save({"desktop_version": "9.9.9"}, db=self.db, realm="global")
        cp.STORE.refresh(self.db)
        self.assertEqual(cp.STORE.saved("cn").get("desktop_version"), "5.8.3")
        self.assertEqual(cp.STORE.saved("global").get("desktop_version"), "9.9.9")

    def test_reset_only_affects_given_realm(self):
        from admin import client_profile as cp
        cp.save({"desktop_version": "5.8.3"}, db=self.db, realm="cn")
        cp.save({"desktop_version": "5.8.4"}, db=self.db, realm="global")
        cp.reset(db=self.db, realm="global")
        cp.STORE.refresh(self.db)
        self.assertEqual(cp.STORE.saved("cn").get("desktop_version"), "5.8.3",
                         "重置 global 不该清掉 cn")
        self.assertEqual(cp.STORE.saved("global"), {})


class OutboundIdentityCase(unittest.TestCase):
    """出站身份（UA / 渠道号 / 风控头）按 realm 分开。

    不依赖本机是否装了客户端：**主要断言两版彼此不同**（用兜底值即可判定），
    这样在 CI（没有客户端）上也能跑。
    """

    def test_ua_differs_by_realm(self):
        from admin import client_profile as cp
        a = cp.ua(realm="cn")
        b = cp.ua(realm="global")
        self.assertNotEqual(a, b, "两版 UA 不该相同")
        self.assertIn("WorkBuddy/", a, f"cn UA 应含 WorkBuddy/：{a!r}")
        self.assertIn("workbuddy-ai/", b, f"global UA 应含 workbuddy-ai/：{b!r}")
        # 三段同源：前两段的品牌必须一致
        segs = b.split(" ")
        self.assertEqual(segs[0].split("/")[0], segs[1].split("/")[0],
                         f"UA 三段品牌段不一致：{b!r}")

    def test_channel_id_differs_by_realm(self):
        from admin import client_profile as cp
        self.assertEqual(cp.turing_channel_id(realm="cn"), 109144)
        self.assertEqual(cp.turing_channel_id(realm="global"), 400111)

    def test_risk_headers_differs_by_realm(self):
        from admin import client_profile as cp
        cn = cp.risk_headers(realm="cn")
        gl = cp.risk_headers(realm="global")
        self.assertNotEqual(cn.get("X-IDE-Name"), gl.get("X-IDE-Name"),
                            "两版 X-IDE-Name 不该相同")
        self.assertEqual(cn.get("X-Product"), gl.get("X-Product"),
                         "X-Product 是部署形态（SaaS），两版相同")

    def test_realm_module_ua_uses_client_profile(self):
        from admin import realm as realm_mod
        cn = realm_mod.chat_ua("cn")
        gl = realm_mod.chat_ua("global")
        self.assertIn("WorkBuddy/", cn)
        self.assertIn("workbuddy-ai/", gl)
        # 不得出现「同一串 UA 里混两个产品名」的旧 bug
        self.assertNotIn("WorkBuddy AI/", gl)
        self.assertEqual(realm_mod.ua_brand("cn"), "WorkBuddy")
        self.assertEqual(realm_mod.ua_brand("global"), "workbuddy-ai")

    def test_billing_ua_per_realm(self):
        from admin import realm as realm_mod
        self.assertTrue(realm_mod.billing_ua("cn").startswith("WorkBuddy/"))
        self.assertTrue(realm_mod.billing_ua("global").startswith("workbuddy-ai/"))
        self.assertNotIn(" CLI/", realm_mod.billing_ua("global"),
                         "billing UA 是单段（无 CLI 段）")


class WbInstallRealmCase(unittest.TestCase):
    """底层探测器的 realm 能力（不依赖本机装没装客户端）。"""

    def test_base_names_cover_both(self):
        import wb_install as wi
        self.assertIn("WorkBuddy", wi._REALM_BASE_NAMES["cn"])
        self.assertIn("WorkBuddyAI", wi._REALM_BASE_NAMES["global"])
        # 全量候选必须两版都覆盖（否则某版永远探测不到）
        self.assertIn("WorkBuddy", wi._BASE_NAMES)
        self.assertIn("WorkBuddyAI", wi._BASE_NAMES)

    def test_common_subdirs_scanned(self):
        """用户自建目录（D:\\Software\\…）必须在扫描范围内。

        实测踩过：客户端装在 `D:\\Software\\WorkBuddy`，而扫描只覆盖盘根 →
        `install_dir=None` → 所有参数退化成兜底值。
        """
        import wb_install as wi
        self.assertIn("Software", wi._COMMON_SUBDIRS)

    def test_realm_base_names_returns_filtered(self):
        import wb_install as wi
        WB = wi.WB
        try:
            WB._realm_filter = "global"
            names = WB._base_names()
            self.assertIn("WorkBuddyAI", names)
            self.assertNotIn("WorkBuddy", names, "global 过滤时不该产出国内版目录名")
            WB._realm_filter = "cn"
            names = WB._base_names()
            self.assertIn("WorkBuddy", names)
            self.assertNotIn("WorkBuddyAI", names, "cn 过滤时不该产出国际版目录名")
        finally:
            WB._realm_filter = ""


if __name__ == "__main__":
    unittest.main()
