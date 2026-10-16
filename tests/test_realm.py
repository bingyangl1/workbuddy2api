"""tests/test_realm.py — Phase 1 国际版 realm 双域支持：Go 测试语义移植（P1-T1）。

语义源（Go 上游 workbuddy2api，出现出入时一律以 Go 为准）：
  - internal/auth/realm_test.go          域名判定 / 显式优先 / 逃生门 / 纯推导
  - internal/pool/realm_test.go          池按 realm 过滤（此处移植其判定前提部分）
  - internal/upstream/realm_test.go      端点分派 / chat 与 billing 路径回落方向
  - internal/upstream/headers_global_test.go  UA 品牌段（WorkBuddy vs WorkBuddy AI）
  - internal/upstream/trial.go           trial 加油包仅 global
  - internal/server/resolve_model_test.go     模型名 "[realm:]" 前缀协议
  - internal/scheduler/gating_test.go    global 账号任务门控（签到/旅行跳过，
                                         keepalive 与活跃上报照跑）

与 Go 上游的两处**刻意**差异（测试注释逐处注明）：
  1. resolve_model_prefix 对「无前缀/未知前缀」返回 (None, 原串)，而 Go
     resolveModel 返回 ("cn", 原串) —— 本项目需要区分「显式 cn: / 显式
     global: / 未声明」三种情况供密钥 realm 隔离判定（Go 账号池是单值
     语义，无需区分）。
  2. chat_paths("global") 照 Go 上游返回双候选 ["/console/chat/completions",
     "/v2/chat/completions"]（console 404/405 回落 v2）。workbuddy-manager
     的 server/services/realm.py 曾把 global 固化为单条 /v2 —— 那是
     manager 侧的偏离，以 Go 上游为准。

零网络依赖：全部断言纯函数 / 常量 / TTL 缓存行为；.info 凭证用占位
字符串，不含真实 token。system_settings 读取通过钉缓存避免真连 MySQL。

admin/realm.py 尚未实现时：pytest.importorskip 优雅跳过（skip 而非 error）。
风格跟随 tests/test_desktop.py：纯 unittest，不引入 requirements 之外的新依赖。
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── 模块就位检测：admin.realm 缺失 → skip（不是 error）─────────────
try:
    import pytest
except ImportError:  # 环境连 pytest 都没有时退化为 unittest 跳过
    pytest = None

if pytest is not None:
    realm = pytest.importorskip("admin.realm")
else:  # pragma: no cover - 无 pytest 环境的手动 runner 路径
    try:
        from admin import realm  # type: ignore[no-redef]
    except Exception:
        raise unittest.SkipTest("admin.realm 尚未实现（Phase 1 主实现就位前跳过）")


# ── 测试辅助 ──────────────────────────────────────────────────────

def _pin_settings(data: dict) -> None:
    """把 system_settings 读取结果钉进 TTL 缓存（白盒）。

    admin.realm._read_settings 的缓存本就为此存在；钉住它可让测试
    断言「配置如何影响分派」而无需真连 MySQL（库不可用时函数会静默
    回落默认值，这里显式给默认形状，行为等价且快）。
    """
    realm._cfg_cache.update({"at": time.time(), "data": dict(data)})


def make_auth_segment(domain: str = "www.codebuddy.cn",
                      realm_field: str | None = None) -> dict:
    """构造 .info 的 auth 段（占位 token，字段名与桌面端同构）。

    对齐 admin/wb_login.py build_credential_json 的字段集：
    accessToken / refreshToken / expiresAt / tokenType / sessionState /
    domain / lastRefreshTime（realm 为 Phase 1 新增可选键）。
    """
    seg = {
        "accessToken": "placeholder-at",
        "refreshToken": "placeholder-rt",
        "expiresAt": 4102444800000,          # 远期毫秒时间戳（占位）
        "tokenType": "Bearer",
        "sessionState": "",
        "domain": domain,
        "lastRefreshTime": 1750000000000,
    }
    if realm_field is not None:
        seg["realm"] = realm_field
    return seg


def make_info(domain: str = "www.codebuddy.cn",
              realm_field: str | None = None,
              uid: str = "u-test") -> dict:
    """构造完整 .info JSON 形态（顶层 account / auth）。"""
    return {
        "account": {
            "uid": uid,
            "nickname": "占位昵称",
            "enterpriseId": "",
            "type": "personal",
        },
        "auth": make_auth_segment(domain=domain, realm_field=realm_field),
    }


# ── 1. 域名判定（Go internal/auth/realm_test.go + pool/realm_test.go）──

class RealmDomainTests(unittest.TestCase):
    """realm_of / resolve_realm / has_global_domain：判定顺序与逃生门。

    Go 语义（auth.go L67-79）：
      global_enabled=False → 恒 cn（逃生门）；
      否则 realm=="global" 或 domain 是 workbuddy.ai 家族 → global；
      其余（含空/未知 domain）→ cn。
    """

    def setUp(self):
        _pin_settings({})  # settings 无值 → global_enabled 默认开、base 默认

    def tearDown(self):
        realm.invalidate()

    # -- domain 回落（Go TestRealmDomainFallback / TestBackfillRealmDomain）--
    def test_domain_fallback_global(self):
        """workbuddy.ai 本身及任意子域 → global。"""
        for d in ("www.workbuddy.ai", "workbuddy.ai", "sub.workbuddy.ai",
                  "a.b.workbuddy.ai"):
            self.assertEqual(realm.realm_of({"domain": d}), "global", d)

    def test_domain_fallback_cn(self):
        """codebuddy.cn 家族 / 空 domain / None → cn（老 CN 凭证零回归）。"""
        for d in ("www.codebuddy.cn", "codebuddy.cn", "", "unknown.example.com"):
            self.assertEqual(realm.realm_of({"domain": d}), "cn", repr(d))
        self.assertEqual(realm.realm_of(None), "cn")

    def test_pool_realm_partition_premise(self):
        """池按 realm 过滤的判定前提（Go pool/realm_test.go 的账号构造）。

        cn 账号：显式 cn realm 或空 realm+cn domain；global 账号：
        www.workbuddy.ai / workbuddy.ai domain。两者 realm_of 互不串。
        """
        cn_accts = [{"uid": "cn1", "domain": "www.codebuddy.cn"},
                    {"uid": "cn2", "domain": ""}]
        g_accts = [{"uid": "g1", "domain": "www.workbuddy.ai"},
                   {"uid": "g2", "domain": "workbuddy.ai"}]
        for a in cn_accts:
            self.assertEqual(realm.realm_of(a), "cn", a)
        for a in g_accts:
            self.assertEqual(realm.realm_of(a), "global", a)

    # -- 显式 realm 优先（Go TestRealmExplicitGlobal / TestRealmExplicitCN）--
    def test_explicit_realm_overrides_domain(self):
        """显式 realm 字段优先于 domain 回落（Go D1）。"""
        self.assertEqual(
            realm.realm_of({"realm": "global", "domain": "www.codebuddy.cn"}),
            "global")
        self.assertEqual(
            realm.realm_of({"realm": "cn", "domain": "www.workbuddy.ai"}),
            "cn")

    def test_cn_zero_regression(self):
        """开关缺省开启不影响纯 CN 部署（Go TestRealmDefaultOnForCNZeroRegression）。"""
        for a in ({}, {"domain": "www.codebuddy.cn"}, {"domain": "codebuddy.cn"},
                  {"realm": "cn"},
                  {"realm": "cn", "domain": "www.codebuddy.cn"}):
            self.assertEqual(realm.realm_of(a), "cn", a)

    # -- 逃生门（Go TestRealmExplicitOffEscapeHatch）--
    def test_escape_hatch_forces_cn(self):
        """ADMIN_GLOBAL_ENABLED=0 → 即便 realm=global / domain=workbuddy.ai 也恒 cn。"""
        with patch.dict(os.environ, {"ADMIN_GLOBAL_ENABLED": "0"}):
            cases = ({"realm": "global"},
                     {"realm": "global", "domain": "www.workbuddy.ai"},
                     {"domain": "www.workbuddy.ai"})
            for a in cases:
                self.assertEqual(realm.realm_of(a), "cn", a)

    def test_escape_hatch_via_settings(self):
        """system_settings 的 global_enabled='0' 同样生效（env 留空走 settings 途径）。"""
        _pin_settings({"global_enabled": "0"})
        with patch.dict(os.environ, {"ADMIN_GLOBAL_ENABLED": ""}):
            self.assertFalse(realm.global_enabled())
            self.assertEqual(
                realm.realm_of({"realm": "global", "domain": "www.workbuddy.ai"}),
                "cn")

    def test_escape_hatch_is_dynamic(self):
        """逃生门是运行时开关：关→恒 cn，恢复开→判定恢复（非一次性固化）。"""
        with patch.dict(os.environ, {"ADMIN_GLOBAL_ENABLED": "0"}):
            self.assertEqual(realm.realm_of({"domain": "www.workbuddy.ai"}), "cn")
        with patch.dict(os.environ, {"ADMIN_GLOBAL_ENABLED": "1"}):
            self.assertEqual(realm.realm_of({"domain": "www.workbuddy.ai"}),
                             "global")

    # -- isGlobalDomain 边界（Go isGlobalDomain：小写化 + 去空白 + 后缀匹配；
    #    本实现扩展 codebuddy.ai 家族——国际版 IDE 插件凭证实测 domain=www.codebuddy.ai）--
    def test_has_global_domain_table(self):
        """大小写不敏感、去空白；后缀必须落在域边界上。"""
        yes = ("workbuddy.ai", "www.workbuddy.ai", "sub.workbuddy.ai",
               "WWW.WORKBUDDY.AI", "  workbuddy.ai  ", " x.workbuddy.ai ",
               # codebuddy.ai 家族（本机真实凭证实测，P1 判定缺口修正）
               "codebuddy.ai", "www.codebuddy.ai", "login.codebuddy.ai",
               "auth.codebuddy.ai", "www.codebuddy.ai ",
               )
        no = ("", "  ", "www.codebuddy.cn", "workbuddy.ai.evil.com",
              "notworkbuddy.ai", "myworkbuddy.ai",
              "x.workbuddy.ai.evil.com", "workbuddy.ai\x00",
              # 防误判面：CN 域不得被 codebuddy.ai 后缀命中
              "www.codebuddy.cn", "codebuddy.cn", "sub.codebuddy.cn",
              "codebuddy.ai.evil.com", "notcodebuddy.ai",
              "x.codebuddy.ai.evil.com", "codebuddy.ai\x00")
        for d in yes:
            self.assertTrue(realm.has_global_domain(d), repr(d))
        for d in no:
            self.assertFalse(realm.has_global_domain(d), repr(d))

    def test_codebuddy_ai_domain_is_global(self):
        """国际版 IDE 插件凭证（domain=www.codebuddy.ai）必须按 global 路由。

        真实对照（2027-02 冒烟）：同一 token 走 copilot.tencent.com 401、
        走 www.workbuddy.ai 200。原生判定若给 cn，账号必废。
        """
        self.assertEqual(realm.resolve_realm("", "www.codebuddy.ai"), "global")
        self.assertEqual(realm.realm_of({"domain": "www.codebuddy.ai"}), "global")
        self.assertEqual(realm.realm_of({"domain": "codebuddy.ai"}), "global")

    # -- 纯推导（Go TestResolveRealm：不受逃生门影响）--
    def test_resolve_realm_pure_function(self):
        """显式优先 / 缺省按 domain / 双空 → cn（Go TestResolveRealm 全表）。"""
        cases = [
            ("global", "www.codebuddy.cn", "global"),  # 显式优先：cn domain 也写 global
            ("cn", "www.workbuddy.ai", "cn"),          # 显式优先：global domain 也写 cn
            ("", "www.workbuddy.ai", "global"),
            ("", "workbuddy.ai", "global"),
            ("", "codebuddy.cn", "cn"),
            ("", "", "cn"),                            # 双空 → cn（零回归）
            (None, None, "cn"),
        ]
        for explicit, domain, want in cases:
            self.assertEqual(realm.resolve_realm(explicit, domain), want,
                             (explicit, domain))

    def test_resolve_realm_escape_hatch_free(self):
        """逃生门关闭时纯推导仍按原始 domain（Go TestBackfillRealmEscapeHatchFree）。

        账号落库必须用 resolve_realm 而非 realm_of：否则开了逃生门会把
        国际版账号永久写成 cn（Go 上游注释里专门警告过）。
        """
        with patch.dict(os.environ, {"ADMIN_GLOBAL_ENABLED": "0"}):
            # 前提：realm_of 已被逃生门降级
            self.assertEqual(realm.realm_of({"domain": "www.workbuddy.ai"}), "cn")
            # 纯推导不受污染
            self.assertEqual(realm.resolve_realm("", "www.workbuddy.ai"), "global")
            self.assertEqual(realm.resolve_realm("global", "www.codebuddy.cn"),
                             "global")

    # -- 容错与 .info 形态（Go TestParseNestedRealm 的判定面）--
    def test_realm_of_tolerates_none_and_partial(self):
        """非 dict / 空 dict / 缺两键 → cn，不抛异常。"""
        self.assertEqual(realm.realm_of(None), "cn")
        self.assertEqual(realm.realm_of({}), "cn")
        self.assertEqual(realm.realm_of({"accessToken": "x"}), "cn")

    def test_realm_of_nested_info_auth_segment(self):
        """.info 的 auth 段直接可判：显式 realm 键优先，缺失时靠 domain 回落。"""
        info_g = make_info(domain="www.workbuddy.ai", realm_field="global")
        self.assertEqual(realm.realm_of(info_g["auth"]), "global")
        # 嵌套形显式 realm 缺失 → domain 回落
        info_d = make_info(domain="www.workbuddy.ai")
        self.assertEqual(realm.realm_of(info_d["auth"]), "global")
        # 老形态 CN 凭证（无 realm 键、cn domain）
        info_c = make_info(domain="www.codebuddy.cn")
        self.assertEqual(realm.realm_of(info_c["auth"]), "cn")


# ── 2. 端点分派表（Go upstream/client.go + headers.go 常量）─────────

class EndpointDispatchTests(unittest.TestCase):
    """chat_base / billing_base / origin / 路径候选 / 配置覆盖。

    端点差异表（Go client.go New() + defaultGlobalBase）：

      | 维度        | CN                    | Global                |
      |-------------|-----------------------|-----------------------|
      | chat base   | copilot.tencent.com  | www.workbuddy.ai      |
      | billing     | www.codebuddy.cn      | www.workbuddy.ai      |
      | Origin/Ref  | www.codebuddy.cn      | www.workbuddy.ai      |
      | chat 路径   | /v2 only              | /console 404/405→/v2  |
      | billing 路径| /v2 only              | 无前缀优先→/v2 回落    |
    """

    def setUp(self):
        _pin_settings({})

    def tearDown(self):
        realm.invalidate()

    def test_cn_chat_and_billing_base(self):
        self.assertEqual(realm.chat_base("cn"), "https://copilot.tencent.com")
        self.assertEqual(realm.billing_base("cn"), "https://www.codebuddy.cn")

    def test_global_default_bases(self):
        """global 基址缺省 www.workbuddy.ai（Go defaultGlobalBase，D5）。"""
        self.assertEqual(realm.chat_base("global"), "https://www.workbuddy.ai")
        self.assertEqual(realm.billing_base("global"),
                         "https://www.workbuddy.ai")

    def test_origin_and_referer_base_by_realm(self):
        """Origin/Referer 基础域随 realm 切（Go originRefererFor）。"""
        self.assertEqual(realm.origin_of("cn"), "https://www.codebuddy.cn")
        self.assertEqual(realm.origin_of("global"),
                         "https://www.workbuddy.ai")

    # -- 路径回落方向（两边相反，Go client.go L502-519 / L660-667）--
    def test_chat_paths_global_console_first_v2_fallback(self):
        """global chat 双候选：/console 优先，/v2 兜底（Go chatPaths）。

        注：manager realm.py 的 chat_paths 曾把 global 固化为单条 /v2
        （其注释引用 #119）——那是 manager 侧偏离；Go 上游当前代码与
        realm_test.go 的 fallback 断言都是双候选，以 Go 为准。
        """
        paths = realm.chat_paths("global")
        self.assertEqual(paths, ["/console/chat/completions",
                                 "/v2/chat/completions"])

    def test_chat_paths_cn_single_v2(self):
        """CN chat 只有 /v2（现状逐字，零回归）。"""
        self.assertEqual(realm.chat_paths("cn"), ["/v2/chat/completions"])

    def test_chat_fallback_status_gates(self):
        """回落只在 404/405 发生（Go chatFallbackHTTPStatus / TestGlobalChatServerFallbackErrorCode）。"""
        for s in (404, 405):
            self.assertTrue(realm.chat_fallback_status(s), s)
        for s in (200, 400, 403, 429, 500, 502):
            self.assertFalse(realm.chat_fallback_status(s), s)

    def test_billing_paths_global_prefix_free_first(self):
        """global billing 无 /v2 前缀优先、/v2 兜底（Go billingMeterPaths R9）。"""
        self.assertEqual(
            realm.billing_paths("global", "user-resource"),
            ["/billing/meter/get-user-resource",
             "/v2/billing/meter/get-user-resource"])
        self.assertEqual(
            realm.billing_paths("global", "daily-checkin"),
            ["/billing/meter/daily-checkin",
             "/v2/billing/meter/daily-checkin"])

    def test_billing_paths_cn_v2_only(self):
        """CN billing 只有 /v2 形式（现状逐字，零回归）。

        与 global 的回落方向**相反**——这不是笔误，照 Go 上游实现。
        """
        self.assertEqual(
            realm.billing_paths("cn", "user-resource"),
            ["/v2/billing/meter/get-user-resource"])
        self.assertEqual(
            realm.billing_paths("cn", "daily-checkin"),
            ["/v2/billing/meter/daily-checkin"])

    def test_models_path_direction_opposes_chat(self):
        """模型目录端点方向与 chat 相反：CN 走 /console，Global 走 /v2。

        Go client.go L683-684：CN 现状 /console/enterprises/personal/models
        逐字保留；global 走 /v2/enterprises/personal/models（PR #20 实测
        /console 500、/v2 200 含完整模型表）。
        """
        self.assertEqual(realm.models_path("cn"),
                         "/console/enterprises/personal/models")
        self.assertEqual(realm.models_path("global"),
                         "/v2/enterprises/personal/models")

    # -- 配置覆盖（Go Client.ChatBaseGlobal / BillingBaseGlobal 非空取之）--
    def test_config_overrides_global_chat_base(self):
        """global.chat_base 可覆盖默认 www.workbuddy.ai（Go globalChatBase）。"""
        _pin_settings({"global_chat_base": "https://custom.example"})
        self.assertEqual(realm.chat_base("global"), "https://custom.example")
        # 未配置的另一项回落默认
        self.assertEqual(realm.billing_base("global"),
                         "https://www.workbuddy.ai")
        # CN 侧不受 global 配置影响
        self.assertEqual(realm.chat_base("cn"), "https://copilot.tencent.com")

    def test_config_overrides_global_billing_base(self):
        """global.billing_base 可覆盖默认；尾部斜杠被剥（Go base 均无尾斜杠形态）。"""
        _pin_settings({"global_billing_base": "https://bill.example/"})
        self.assertEqual(realm.billing_base("global"), "https://bill.example")
        self.assertEqual(realm.chat_base("global"),
                         "https://www.workbuddy.ai")

    def test_config_cache_invalidate(self):
        """invalidate() 后配置改动在下次读取生效（TTL 缓存的存在意义）。"""
        _pin_settings({"global_chat_base": "https://old.example"})
        self.assertEqual(realm.chat_base("global"), "https://old.example")
        realm.invalidate()
        _pin_settings({"global_chat_base": "https://new.example"})
        self.assertEqual(realm.chat_base("global"), "https://new.example")

    def test_accept_language_by_realm(self):
        """Accept-Language 按账号域切：global → en-US，cn → zh-CN（Go D5）。"""
        self.assertEqual(realm.accept_language("global"), "en-US")
        self.assertEqual(realm.accept_language("cn"), "zh-CN")


# ── 3. UA 品牌段（Go upstream/headers_global_test.go）──────────────

class UserAgentTests(unittest.TestCase):
    """ua_brand / chat_ua / billing_ua。

    Go defaultWorkBuddyUAFor：`WorkBuddy/<ver> <platform>/<ver> CLI/<cli>`，
    平台段 CN 用 `WorkBuddy`、global 用官方国际版 productName
    `WorkBuddy AI`（送错平台段可能触发上游 403 code 11140 风控）。
    版本号走本项目 wb_install 探测，故此处只断言结构不锁具体版本。
    """

    def setUp(self):
        _pin_settings({})

    def tearDown(self):
        realm.invalidate()

    def test_ua_brand_by_realm(self):
        """品牌段 = 客户端 `applicationName`：CN 'WorkBuddy' / Global 'workbuddy-ai'。

        依据：用 node 实跑客户端官方构造逻辑（见 realm.ua_brand 的说明）——
        国际版 applicationName 实测为 `workbuddy-ai`（`WorkBuddy AI` 是 productName，
        官方不用于 UA）。原断言 `WorkBuddy AI` 抄自 Go 上游旧规格。
        """
        self.assertEqual(realm.ua_brand("cn"), "WorkBuddy")
        self.assertEqual(realm.ua_brand("global"), "workbuddy-ai")

    def test_chat_ua_three_segment_shape(self):
        """chat UA 三段式：WorkBuddy/<v> <brand>/<v> CLI/<cli>。

        brand 含空格（'WorkBuddy AI'），故用正则断言结构而非空格切分。
        """
        pat = re.compile(
            r"^(WorkBuddy|workbuddy-ai)/[\w.]+ (WorkBuddy|workbuddy-ai)/[\w.]+ CLI/[\w.]+$")
        for r in ("cn", "global"):
            self.assertRegex(realm.chat_ua(r), pat, r)

    def test_chat_ua_global_carries_ai_brand(self):
        """global UA 的品牌段是 `workbuddy-ai`（客户端 applicationName）。"""
        self.assertIn("workbuddy-ai/", realm.chat_ua("global"))

    def test_chat_ua_cn_never_carries_ai_brand(self):
        """CN UA 不得含 'WorkBuddy AI'（Go TestChatHeadersCNRealm 零回归）。"""
        ua = realm.chat_ua("cn")
        self.assertNotIn("WorkBuddy AI", ua)
        self.assertIn(" WorkBuddy/", ua)

    def test_billing_ua_single_segment(self):
        """billing UA 单段 WorkBuddy/<ver>，不带 CLI 段（Go billingUA 形态）。

        与 Go 老代码的差异：Go billingUA 仅当 ClientName 非空才返回值；
        上游 2026-09-14 起把默认翻转为「官方白名单接口显式覆写 UA」，
        本模块按新默认恒返回单段形态。
        """
        ua = realm.billing_ua()
        self.assertTrue(ua.startswith("WorkBuddy/"), ua)
        self.assertNotIn(" CLI/", ua)
        self.assertNotIn("WorkBuddy AI", ua)


# ── 4. 请求头与设备指纹（Go headers.go / deriveAccountStableID）────

class HeadersTests(unittest.TestCase):
    """headers / billing_headers / device_fingerprint_headers。"""

    def setUp(self):
        _pin_settings({})

    def tearDown(self):
        realm.invalidate()

    def test_global_headers_core_shape(self):
        """global 头：Origin/Referer=workbuddy.ai、UA 含 AI 品牌段、
        Accept-Language=en-US、风控闸门头 X-CodeBuddy-Request=1。"""
        h = realm.headers("global")
        self.assertEqual(h["Origin"], "https://www.workbuddy.ai")
        self.assertEqual(h["Referer"], "https://www.workbuddy.ai/")
        self.assertIn("workbuddy-ai/", h["User-Agent"])
        self.assertEqual(h["Accept-Language"], "en-US")
        self.assertEqual(h["X-CodeBuddy-Request"], "1")

    def test_cn_headers_core_shape(self):
        """CN 头零回归：codebuddy.cn 域 + zh-CN + 无 AI 品牌段。"""
        h = realm.headers("cn")
        self.assertEqual(h["Origin"], "https://www.codebuddy.cn")
        self.assertEqual(h["Referer"], "https://www.codebuddy.cn/")
        self.assertNotIn("WorkBuddy AI", h["User-Agent"])
        self.assertEqual(h["Accept-Language"], "zh-CN")
        self.assertEqual(h["X-CodeBuddy-Request"], "1")

    def test_headers_token_appends_authorization(self):
        h = realm.headers("cn", token="tok-placeholder")
        self.assertEqual(h["Authorization"], "Bearer tok-placeholder")

    def test_headers_without_token_no_authorization(self):
        self.assertNotIn("Authorization", realm.headers("cn"))
        self.assertNotIn("Authorization", realm.headers("cn", token=""))

    def test_billing_headers_identity_fields(self):
        """billing 域身份头 + 单段 UA（Go BillingHeaders / trial_test.go 断言）。"""
        auth = {"access_token": "tok", "uid": "u1", "enterprise_id": "e1",
                "domain": "www.workbuddy.ai", "device_token": "dev-tok"}
        h = realm.billing_headers("global", auth)
        self.assertEqual(h["Authorization"], "Bearer tok")
        self.assertEqual(h["X-User-Id"], "u1")
        self.assertEqual(h["X-Enterprise-Id"], "e1")
        self.assertEqual(h["X-Tenant-Id"], "e1")
        self.assertEqual(h["X-Domain"], "www.workbuddy.ai")
        self.assertEqual(h["X-Device-Token"], "dev-tok")
        # UA 覆写为单段（无 CLI 段）；品牌段按 realm 取客户端 applicationName
        self.assertTrue(h["User-Agent"].startswith("workbuddy-ai/"),
                        f"global 的 billing UA 应以 workbuddy-ai/ 开头，实际 {h['User-Agent']!r}")
        self.assertNotIn(" CLI/", h["User-Agent"])

    def test_billing_headers_tolerates_none(self):
        """auth=None 不抛异常；缺省身份头不发送。"""
        h = realm.billing_headers("cn", None)
        self.assertNotIn("X-User-Id", h)
        self.assertNotIn("X-Enterprise-Id", h)
        self.assertNotIn("X-Domain", h)
        self.assertNotIn("X-Device-Token", h)

    def test_device_fingerprint_formula(self):
        """sha256("wb2a:"+purpose+":"+uid) 前 36 hex 字符（Go deriveAccountStableID 逐字一致）。

        固定盐 / 截 18 字节必须与 Go 逐字一致：同一账号经网关与直连
        两条路必须是同一台「设备」。
        """
        uid = "u1"
        want_machine = hashlib.sha256(
            f"wb2a:machine:{uid}".encode()).hexdigest()[:36]
        want_session = hashlib.sha256(
            f"wb2a:session:{uid}".encode()).hexdigest()[:36]
        got = realm.device_fingerprint_headers(uid)
        self.assertEqual(got["X-Machine-ID"], want_machine)
        self.assertEqual(got["X-Session-ID"], want_session)
        self.assertEqual(len(got["X-Machine-ID"]), 36)
        self.assertEqual(len(got["X-Session-ID"]), 36)

    def test_device_fingerprint_deterministic_and_distinct(self):
        """跨调用恒定（同一虚拟设备）；账号间互异；不同用途互异。"""
        a1 = realm.device_fingerprint_headers("acct-a")
        a1b = realm.device_fingerprint_headers("acct-a")
        a2 = realm.device_fingerprint_headers("acct-b")
        self.assertEqual(a1, a1b)
        self.assertNotEqual(a1["X-Machine-ID"], a2["X-Machine-ID"])
        self.assertNotEqual(a1["X-Machine-ID"], a1["X-Session-ID"])

    def test_device_fingerprint_empty_uid(self):
        """uid 空 → 不返回任何头（匿名请求无设备，Go 同语义）。"""
        for empty in ("", "   ", None):
            self.assertEqual(realm.device_fingerprint_headers(empty), {})


# ── 5. 模型前缀路由（Go server/resolve_model_test.go）──────────────

class ModelPrefixTests(unittest.TestCase):
    """resolve_model_prefix："[realm:]model" 协议（Go PLAN D6）。

    取第一个 ":"，前段恰为 "cn"/"global"（小写精确枚举、大小写敏感）
    才剥离；bare 即出站 / 选号 / 白名单使用的裸模型名。
    """

    def test_resolve_model_table(self):
        """主干表（Go TestResolveModel 全部 4 例）。"""
        cases = [
            ("cn:glm-5.2", "cn", "glm-5.2"),
            ("global:gpt-5.4", "global", "gpt-5.4"),
            ("glm-5.2", None, "glm-5.2"),
            ("deepseek:v3", None, "deepseek:v3"),  # 冒号前段不在枚举内，不剥离
        ]
        for model, want_realm, want_bare in cases:
            self.assertEqual(realm.resolve_model_prefix(model),
                             (want_realm, want_bare), model)

    def test_resolve_model_edge(self):
        """边界（Go TestResolveModelEdge，未声明形态改断言 None）。"""
        cases = [
            ("", None, ""),
            (":", None, ":"),
            (":model", None, ":model"),       # 空前缀不匹配，不剥离
            ("GLOBAL:gpt-5", None, "GLOBAL:gpt-5"),  # 大小写敏感：不做归一
            ("global:", "global", ""),        # 前缀合法 + 空裸名仍剥离
            ("global:gpt-5.4", "global", "gpt-5.4"),
            ("cn:", "cn", ""),
        ]
        for model, want_realm, want_bare in cases:
            self.assertEqual(realm.resolve_model_prefix(model),
                             (want_realm, want_bare), model)

    def test_unspecified_returns_none_not_cn(self):
        """与 Go 的刻意差异：无前缀 → None（未声明）而非 'cn'。

        Go resolveModel 无前缀返回 ("cn", model)，因其账号池是单值
        语义；本项目需区分「显式 cn: / 显式 global: / 未声明」三种
        情况供密钥 realm 隔离判定（key_allows_model 只对显式声明
        生效），故未声明用 None。
        """
        self.assertIsNone(realm.resolve_model_prefix("glm-5.2")[0])
        self.assertIsNone(realm.resolve_model_prefix("deepseek:v3")[0])


# ── 6. 密钥 realm 限定 ─────────────────────────────────────────────

class KeyRealmTests(unittest.TestCase):
    """key_allows_model：密钥 realm 与模型前缀 realm 的组合判定。

    ''/None = 不限 → 放行；显式声明则必须与模型 realm 相等
    （冲突拒绝，proxy 层 403 / /v1/models 按密钥过滤同理）。
    """

    def test_empty_key_realm_unrestricted(self):
        """空密钥 realm（''/None/'  '）= 不限，任何模型 realm 放行。"""
        for kr in ("", None, "   "):
            self.assertTrue(realm.key_allows_model(kr, "cn"), kr)
            self.assertTrue(realm.key_allows_model(kr, "global"), kr)

    def test_exact_match_allowed(self):
        self.assertTrue(realm.key_allows_model("cn", "cn"))
        self.assertTrue(realm.key_allows_model("global", "global"))

    def test_conflict_rejected(self):
        """跨版本调用被拒：cn 密钥不放行 global 模型，反之亦然。"""
        self.assertFalse(realm.key_allows_model("cn", "global"))
        self.assertFalse(realm.key_allows_model("global", "cn"))

    def test_case_insensitive(self):
        """密钥 realm 归一大小写（存储侧宽松，判定侧严格相等）。"""
        self.assertTrue(realm.key_allows_model("CN", "cn"))
        self.assertTrue(realm.key_allows_model(" Global ", "global"))
        self.assertFalse(realm.key_allows_model("CN", "global"))


# ── 7. 任务域门控（Go scheduler/gating_test.go）────────────────────

class SchedulerGatingTests(unittest.TestCase):
    """task_supported / task_skip_reason / trial_available。

    Go 调度器 D4 门控：global 账号**跳过**签到 / 猫猫旅行 / 开学季 /
    夜猫子 / 成长任务（无对应活动体系，不发起任何上游调用避免风控）；
    **照跑**令牌保活与活跃上报（PR #45 实测 /v2/report 在
    workbuddy.ai code=0 OK）。
    """

    def setUp(self):
        _pin_settings({})

    def tearDown(self):
        realm.invalidate()

    def test_global_skips_cn_activity_tasks(self):
        """global 账号跳过签到/猫猫/开学季/夜猫子/成长任务（Go gating_test.go 语义组）。"""
        for task in ("daily_checkin", "cat_travel", "school_season",
                     "black_cat", "growth"):
            self.assertFalse(realm.task_supported("global", task), task)

    def test_global_keeps_keepalive_and_reports(self):
        """保活与活跃上报照跑（Go TestRunKeepaliveStillRefreshesGlobal /
        TestRunActivityNowReportsGlobalAccounts：两域都支持）。"""
        for task in ("keepalive_tokens", "activity_report"):
            self.assertTrue(realm.task_supported("global", task), task)

    def test_cn_supports_all(self):
        """CN 账号全任务域可用（零回归）。"""
        for task in ("daily_checkin", "cat_travel", "school_season",
                     "black_cat", "growth", "keepalive_tokens",
                     "activity_report"):
            self.assertTrue(realm.task_supported("cn", task), task)

    def test_task_skip_reason_human_readable(self):
        """跳过原因是人话（含任务名与「跳过」），供调度回执 / 面板展示。"""
        mapping = {
            "daily_checkin": "签到",
            "cat_travel": "猫猫",
            "school_season": "开学季",
            "black_cat": "夜猫子",
            "growth": "成长任务",
        }
        for task, keyword in mapping.items():
            reason = realm.task_skip_reason(task)
            self.assertIn(keyword, reason, task)
            self.assertIn("跳过", reason, task)
        # 未知任务域：兜底人话而不是抛异常
        self.assertIn("跳过", realm.task_skip_reason("unknown_task"))

    def test_trial_available_global_only(self):
        """一次性 trial 加油包仅国际版有（Go trial.go：仅 global 账号可调，
        CN 无此端点；这是 global 唯一天然的积分增益动作，见 PLAN D4）。"""
        self.assertTrue(realm.trial_available("global"))
        self.assertFalse(realm.trial_available("cn"))


if __name__ == "__main__":
    unittest.main()
