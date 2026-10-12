"""realm：国内版（CN）与国际版（Global）的判定与端点分派。

本模块吸收自 workbuddy-manager 的 server/services/realm.py（MIT, ithtelab），
语义严格镜像 Go 上游 Sliverkiss/workbuddy2api（MIT）：

  - 判定顺序：global_enabled=False → 恒 cn（逃生门）；
    否则 realm == "global" 或 domain 是 workbuddy.ai / *.workbuddy.ai → global；
    其余（含 domain 为空/未知）→ cn
  - 端点差异（Go internal/upstream/client.go）：

    | 维度        | CN                        | Global                    |
    |-------------|---------------------------|---------------------------|
    | chat base   | copilot.tencent.com       | www.workbuddy.ai          |
    | billing     | www.codebuddy.cn          | www.workbuddy.ai          |
    | Origin/Ref  | www.codebuddy.cn          | www.workbuddy.ai          |
    | UA 品牌段   | WorkBuddy                 | WorkBuddy AI              |
    | chat 路径   | /v2/chat/completions      | /console/... 404/405 回落 /v2 |
    | billing 路径| /v2/billing/meter/*       | 无前缀优先，404 回落 /v2  |
    | models 路径 | /console/enterprises/...  | /v2/enterprises/...       |

  billing 的回落方向两边相反（CN 只有 /v2、Global 以无前缀优先），
  这不是笔误，照 Go 上游实现（client.go L502-519）。

与 manager 版的差异（适配本项目）：
  - 配置源从「上游 config.json 文件」改为 system_settings 表
    （本项目无外部 Go 上游，配置就在自己库里），TTL 缓存模式保留
  - 客户端版本号取自本项目的 wb_install / client_profile（已有体系），
    不重复实现探测
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Literal

logger = logging.getLogger("admin.realm")

Realm = Literal["cn", "global"]

CN: Realm = "cn"
GLOBAL: Realm = "global"

# 国际版默认基址（system_settings 的 global_chat_base / global_billing_base 留空时用它）
DEFAULT_GLOBAL_BASE = "https://www.workbuddy.ai"
GLOBAL_ORIGIN = "https://www.workbuddy.ai"
CN_ORIGIN = "https://www.codebuddy.cn"
# CN chat 基址（现有 converter.py BACKEND 同值；集中到这里成为唯一事实来源）
CN_CHAT_BASE = "https://copilot.tencent.com"
CN_BILLING_BASE = "https://www.codebuddy.cn"

# 路径常量（照 Go upstream/client.go L595-601、L495-499）
CHAT_COMPLETIONS_PATH = "/v2/chat/completions"
GLOBAL_CHAT_CONSOLE_PATH = "/console/chat/completions"
BILLING_METER_PATH = "/billing/meter/{suffix}"          # global 首选（无 /v2 前缀）
BILLING_METER_PATH_V2 = "/v2/billing/meter/{suffix}"    # CN 现状 / global fallback
# 模型目录端点（Go client.go L682-685）：CN 走 /console，Global 走 /v2（方向与 chat 相反）
CN_MODELS_PATH = "/console/enterprises/personal/models"
GLOBAL_MODELS_PATH = "/v2/enterprises/personal/models"

# 配置缓存 TTL（秒）：改动要在窗口内生效，又不必每请求读库
_CFG_TTL = 10.0
_cfg_cache: dict = {"at": 0.0, "data": None}


def _read_settings() -> dict:
    """读 system_settings 里的 realm 相关配置（带 TTL 缓存）。

    读不到（库不可用 / 表未建）时按默认返回，绝不抛异常——
    realm 层是热路径，不能因为配置读不到就打断请求。
    """
    now = time.time()
    cached = _cfg_cache.get("data")
    if cached is not None and now - float(_cfg_cache["at"]) < _CFG_TTL:
        return cached
    data: dict = {}
    try:
        from admin.db import SessionLocal
        from admin.models import SystemSetting

        wanted = ("global_enabled", "global_chat_base", "global_billing_base")
        db = SessionLocal()
        try:
            rows = db.query(SystemSetting).filter(
                SystemSetting.key.in_(wanted)
            ).all()
            for r in rows:
                data[str(r.key)] = str(r.value or "")
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        pass
    _cfg_cache.update({"at": now, "data": data})
    return data


def invalidate() -> None:
    """清空配置缓存（保存设置后调用，让改动立即生效）。"""
    _cfg_cache.update({"at": 0.0, "data": None})


def global_enabled() -> bool:
    """国际版路由是否开启（镜像 Go config global.enabled 缺省 true）。

    优先级：环境变量 ADMIN_GLOBAL_ENABLED（'0'/'false' = 关）> system_settings
    的 global_enabled（'0'/'false' = 关，缺省开）> 默认开。
    逃生门：纯 CN 部署即便 auth 写了 realm=global 或 domain 是 .workbuddy.ai
    也恒判 cn——单一闸口集中收敛在 realm_of() 里。
    """
    env = (os.getenv("ADMIN_GLOBAL_ENABLED") or "").strip().lower()
    if env:
        return env not in ("0", "false", "off", "no")
    v = (_read_settings().get("global_enabled") or "").strip().lower()
    if v:
        return v not in ("0", "false", "off", "no")
    return True


def has_global_domain(domain: str | None) -> bool:
    """域名是否属于国际版：workbuddy.ai 本身或其任意子域（镜像 Go isGlobalDomain）。"""
    d = (domain or "").strip().lower()
    return d == "workbuddy.ai" or d.endswith(".workbuddy.ai")


def resolve_realm(explicit: str | None, domain: str | None) -> Realm:
    """纯推导，**不受逃生门影响**（对应 Go ResolveRealm）。

    显式值优先；其次按域名；都不满足则 cn。
    账号落库时用这个而不是 realm_of：否则一旦开了逃生门，
    会把国际版账号永久写成 cn（Go 上游注释里专门警告过这点）。
    """
    e = (explicit or "").strip().lower()
    if e in ("cn", "global"):
        return e  # type: ignore[return-value]
    return GLOBAL if has_global_domain(domain) else CN


def realm_of(auth: dict | None) -> Realm:
    """按账号判定其 realm，**含逃生门**（对应 Go Auth.Realm）。

    auth 可以是 `{'realm':..,'domain':..}` 或含这两键的更大字典
    （如 .info 的 auth 段：{'domain':..,'accessToken':..,...}）。
    """
    if not global_enabled():
        return CN
    if not isinstance(auth, dict):
        return CN
    return resolve_realm(auth.get("realm"), auth.get("domain"))


# ── 模型名前缀路由（Go server/resolve_model.go）─────────────────

def resolve_model_prefix(model: str) -> tuple[Realm | None, str]:
    """解析 `"[realm:]model"` 协议。

    取第一个 ":"，前段恰为 "cn"/"global" 才剥离（大小写敏感，精确小写枚举）；
    否则视为裸名返回 (None, 原串)——None 表示「未声明 realm，按账号自身域路由」。
    bare 即出站/选号/白名单使用的裸模型名。

    注意：与 Go 的差异是 Go 无前缀时返回 "cn"（其账号池单值语义），
    本项目需要区分「显式 cn:」「显式 global:」「未声明」三种情况供
    密钥 realm 隔离判定，故未声明用 None。
    """
    m = model or ""
    idx = m.find(":")
    if idx < 0:
        return None, m
    prefix = m[:idx]
    if prefix not in ("cn", "global"):
        return None, m
    return prefix, m[idx + 1:]  # type: ignore[return-value]


def key_allows_model(key_realm: str | None, model_realm: Realm) -> bool:
    """密钥 realm 是否允许调用该 realm 的模型。

    key_realm 为空/None = 不限 → True；否则必须与模型 realm 相等。
    跨版本调用在 proxy 层被拒（403），/v1/models 按密钥过滤同理。
    """
    kr = (key_realm or "").strip().lower()
    if not kr:
        return True
    return kr == model_realm


# ── 端点分派（Go upstream/client.go）────────────────────────────

def chat_base(realm: Realm) -> str:
    """该 realm 的 chat/登录/模型接口基址。"""
    if realm == GLOBAL:
        base = (_read_settings().get("global_chat_base") or "").strip().rstrip("/")
        return base or DEFAULT_GLOBAL_BASE
    return CN_CHAT_BASE


def billing_base(realm: Realm) -> str:
    """该 realm 的 billing（签到 / 积分 / trial）基址。"""
    if realm == GLOBAL:
        base = (_read_settings().get("global_billing_base") or "").strip().rstrip("/")
        return base or DEFAULT_GLOBAL_BASE
    return CN_BILLING_BASE


def origin_of(realm: Realm) -> str:
    return GLOBAL_ORIGIN if realm == GLOBAL else CN_ORIGIN


def chat_paths(realm: Realm) -> list[str]:
    """聊天补全的候选路径，按尝试顺序（Go client.go L660-667）。

    global → ['/console/chat/completions', '/v2/chat/completions']
    （首路径 404/405 时换 fallback 重试，chatFallbackHTTPStatus）；
    cn → ['/v2/chat/completions']（单元素，现状零回归）。
    """
    if realm == GLOBAL:
        return [GLOBAL_CHAT_CONSOLE_PATH, CHAT_COMPLETIONS_PATH]
    return [CHAT_COMPLETIONS_PATH]


def chat_fallback_status(status: int) -> bool:
    """global chat 路径回落只发生在 404/405（Go chatFallbackHTTPStatus）。"""
    return status in (404, 405)


def billing_paths(realm: Realm, kind: str) -> list[str]:
    """billing/meter 域路径候选。kind: 'user-resource' | 'daily-checkin'。

    **两边顺序相反**（照 Go billingMeterPaths / checkinMeterPaths）：
    global → 无 /v2 前缀首选、/v2 兜底；cn → 只有 /v2（现状逐字，零回归）。
    仅作用于 /billing/meter/* 族；growth 等其他端点不参与。
    """
    suffix = {
        "user-resource": "get-user-resource",
        "daily-checkin": "daily-checkin",
    }[kind]
    if realm == GLOBAL:
        return [
            BILLING_METER_PATH.format(suffix=suffix),
            BILLING_METER_PATH_V2.format(suffix=suffix),
        ]
    return [BILLING_METER_PATH_V2.format(suffix=suffix)]


def models_path(realm: Realm) -> str:
    """动态模型目录端点路径（不含 base）。**方向与 chat 相反**：

    CN → /console/enterprises/personal/models（现状零回归）；
    Global → /v2/enterprises/personal/models（实测 /console 500、/v2 200，
    Go global_models.go probe 家族结论）。
    """
    return GLOBAL_MODELS_PATH if realm == GLOBAL else CN_MODELS_PATH


def accept_language(realm: Realm) -> str:
    """Accept-Language 按账号域切：global → en-US，cn → zh-CN（对齐 Go D5）。"""
    return "en-US" if realm == GLOBAL else "zh-CN"


# ── 客户端身份头（骨架；backend.py 改造时全量接线）──────────────

def client_version() -> str:
    """出站 UA 的客户端版本段——复用本项目已有的 wb_install 探测，不重复实现。"""
    try:
        from wb_install import WB
        return WB.desktop_version()
    except Exception:  # noqa: BLE001
        return "5.5.6"


def cli_version() -> str:
    try:
        from wb_install import WB
        return WB.cli_version()
    except Exception:  # noqa: BLE001
        return "2.137.1"


def ua_brand(realm: Realm) -> str:
    """UA 平台品牌段：CN 'WorkBuddy' / Global 'WorkBuddy AI'（Go defaultWorkBuddyUAFor）。"""
    return "WorkBuddy AI" if realm == GLOBAL else "WorkBuddy"


def chat_ua(realm: Realm) -> str:
    """chat 域出站 UA：`WorkBuddy/<ver> <brand>/<ver> CLI/<cli>`（镜像 Go 形状）。"""
    ver = client_version()
    return f"WorkBuddy/{ver} {ua_brand(realm)}/{ver} CLI/{cli_version()}"


def billing_ua() -> str:
    """billing 域出站 UA：单段 `WorkBuddy/<ver>`（官方白名单接口显式覆写形态）。"""
    return f"WorkBuddy/{client_version()}"


def device_fingerprint_headers(uid: str | None) -> dict:
    """按账号稳定派生的设备指纹头（镜像 Go deriveAccountStableID）。

        sha256("wb2a:" + purpose + ":" + uid) 前 18 字节的 hex = 36 字符

    「每个账号一台固定虚拟设备」：跨重启恒定、账号间互异、同 uid 同用途恒同值。
    uid 为空时不返回任何头（匿名请求无设备可言）。
    实现与 Go 逐字一致（固定盐、截 18 字节）：同一账号在经网关与直连两条路上
    必须是同一台「设备」，否则制造出可被关联的异常。
    """
    u = str(uid or "").strip()
    if not u:
        return {}

    def derive(purpose: str) -> str:
        return hashlib.sha256(f"wb2a:{purpose}:{u}".encode()).hexdigest()[:36]

    return {
        "X-Machine-ID": derive("machine"),
        "X-Session-ID": derive("session"),
    }


def headers(realm: Realm, token: str | None = None, uid: str | None = None) -> dict:
    """该 realm 的通用请求头（Origin/Referer/UA 随 realm 变）。

    token 非空时附 Authorization；uid 非空时附账号级设备指纹头。
    X-CodeBuddy-Request: 1 是官方客户端风控闸门头（所有 API 请求必带）。
    """
    origin = origin_of(realm)
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Language": accept_language(realm),
        "X-Requested-With": "XMLHttpRequest",
        "X-CodeBuddy-Request": "1",
        "User-Agent": chat_ua(realm),
        "Origin": origin,
        "Referer": f"{origin}/",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    h.update(device_fingerprint_headers(uid))
    return h


def billing_headers(realm: Realm, auth: dict | None = None) -> dict:
    """billing 域请求头（对齐 Go BillingHeaders）。

    与 headers() 的差别是身份头：X-User-Id / X-Enterprise-Id / X-Tenant-Id /
    X-Domain / X-Device-Token（设备风控 token 由 admin.turing_token 提供，
    本函数只留注入位，取不到就不发——与官方客户端降级路径一致）。
    UA 覆写为单段 WorkBuddy/<ver>（billing_ua）。
    """
    auth = auth if isinstance(auth, dict) else {}
    token = str(auth.get("access_token") or "")
    h = headers(realm, token or None)
    h["User-Agent"] = billing_ua()
    uid = str(auth.get("uid") or "")
    if uid:
        h["X-User-Id"] = uid
    h.update(device_fingerprint_headers(uid))
    ent = str(auth.get("enterprise_id") or "")
    if ent:
        h["X-Enterprise-Id"] = ent
        h["X-Tenant-Id"] = ent
    domain = str(auth.get("domain") or "")
    if domain:
        h["X-Domain"] = domain
    dt = str(auth.get("device_token") or "").strip()
    if dt:
        h["X-Device-Token"] = dt
    return h


# ── 任务域门控（Go scheduler 对 global 账号的过滤）──────────────

#: 国际版**没有**的活动域（照 Go 调度器过滤 + manager features.md 能力边界）：
#: 无签到 / 猫猫旅行 / 开学季 / 夜猫子 / 成长任务（growthEvent 是 CN 活动体系）。
#: 令牌保活与活跃上报**照常**（两域都支持，且保活恰是 global 账号唯一积分来源外
#: 的登录态维持手段）。
_GLOBAL_UNSUPPORTED_TASKS = frozenset({
    "daily_checkin",        # 签到
    "cat_travel",           # 猫猫旅行
    "school_season",        # 开学季
    "black_cat",            # 夜猫子
    "growth",               # 成长任务（含 refresh_growth_tasks / run_growth_tasks）
})


def task_supported(realm: Realm, task: str) -> bool:
    """该 realm 是否支持此定时任务域（global 账号被调度器直接过滤，不打上游）。"""
    if realm != GLOBAL:
        return True
    return task not in _GLOBAL_UNSUPPORTED_TASKS


def task_skip_reason(task: str) -> str:
    """global 账号跳过该任务时的人话原因（供调度结果摘要 / 面板展示）。"""
    names = {
        "daily_checkin": "签到",
        "cat_travel": "猫猫旅行",
        "school_season": "开学季",
        "black_cat": "夜猫子",
        "growth": "成长任务",
    }
    return f"国际版无{names.get(task, '该活动')}活动，已跳过"


def trial_available(realm: Realm) -> bool:
    """一次性 trial 加油包仅国际版有（Go trial.sh / trial.go 语义）。"""
    return realm == GLOBAL
