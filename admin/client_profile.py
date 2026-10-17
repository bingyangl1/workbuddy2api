#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端参数档案（UA / 版本号 / 风控头 / 桌面指纹）——探测、保存、生效、同步。

要解决什么问题
--------------
出站请求要伪装成官方桌面端，涉及一批参数：UA、桌面端与 CLI 版本号、
风控头（`X-CodeBuddy-Request` 等）、桌面事件指纹（ideName / os / arch /
cpuCores / commit / releaseDate …）。

这些值散落在代码与 `.env` 里，带来两个实际麻烦：

1. **本地探测到的值传不到线上**。线上服务器没装桌面端，`wb_install` 探测
   不到任何东西，只能退化成内置兜底值 —— 于是线上的 UA 报的是一个
   「谁也不认识的版本」。而本地机器明明装得好好的。
2. **改一个值要改代码或重启**。想在后台把 osVersion 调一下，得改 `.env`
   再重启进程。

所以这里把「客户端参数」收敛成**一份档案（profile）**，支持：

    探测(snapshot) → 保存(saved) → 生效(effective) → 同步(sync push)

取值优先级（`effective`）
------------------------
    1. 环境变量          显式运维覆盖，永远最高（如 WORKBUDDY_DESKTOP_VERSION）
    2. 现场探测 / 已保存   取决于 source 设置（见下）
    3. 内置兜底          保证任何情况下都有值，绝不抛异常

`source` 有两种模式，因为「本地」和「线上」的最优选择正好相反：

    auto  （默认）现场探测 > 已保存 > 兜底
          本机装了客户端时用真实值（版本升级后自动跟上）。
    saved 已保存 > 现场探测 > 兜底
          显式钉住后台里保存的值。线上实例必须用这个 —— 它探测不到
          客户端，只能靠同步过来的档案。

为什么读 DB 还要内存缓存
------------------------
`effective()` 在**每个请求**上被调用（构造上游头、拼桌面指纹）。
每次查库不可接受，所以进程内缓存 30 秒，保存时立即失效。
多进程部署下最坏有 30 秒延迟，对「改配置」这种低频操作完全够用。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

#: 项目根加入 sys.path，便于导入根目录的 wb_install
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

#: SystemSetting 里的键名
#
# 按 realm 分档存两份：`client_profile:cn` / `client_profile:global`。
# 旧的无后缀键（`client_profile`）仍被读取，视为 **cn 的那份**（向后兼容）。
PROFILE_KEY = "client_profile"          # 档案本体（JSON 字符串）
SOURCE_KEY = "client_profile_source"    # auto | saved

#: 支持的两个版本域。顺序即「同时展示 / 一起探测」的顺序。
_REALMS = ("cn", "global")

SOURCE_AUTO = "auto"
SOURCE_SAVED = "saved"
_VALID_SOURCES = (SOURCE_AUTO, SOURCE_SAVED)

#: 桌面端 UA / 风控头支持的 realm 取值（含空 = 本机自动判定）。
def _norm_realm(realm: str | None) -> str:
    """归一化 realm：只接受 cn/global，其余按 cn（fail-closed，与引擎其它处一致）。"""
    r = (realm or "").strip().lower()
    return r if r in _REALMS else "cn"

#: DB 缓存时长（秒）。保存时会立即失效，所以这只是多进程下的收敛延迟。
_CACHE_TTL = 30.0

# ---------------------------------------------------------------------------
# 默认值：描述「一台 Windows 上的官方 WorkBuddy 桌面端」
#
# 这些是**伪装目标**的取值，不是「本机真实环境」。比如服务跑在 Linux 上，
# 指纹依然要报 win32 —— 因为我们要看起来像官方 Windows 客户端。
# 它们都可以被探测值 / 后台保存值 / 环境变量覆盖。
# ---------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    # --- 版本（决定 UA 与 X-IDE-Version）---
    #
    # 这两项是**最后的兜底**。真正分 realm 的值在 `_REALM_DEFAULTS` 里
    # （cn 5.8.3 / global 5.8.4），这里保持与国内版一致以便任何
    # 「没带 realm」的旧调用路径也不算离谱。
    #
    # 历史：这里原来是 `5.5.6` / `2.137.1` —— realm 双域改造之前的旧版客户端。
    # 探测不到客户端时（服务器 / CI / 手机）两版都会报这个值，
    # 既是过时版本号，又会造成「两版同版本」的混用特征。
    "desktop_version": "5.8.3",
    "cli_version": "2.161.0",
    # --- 风控 SDK ---
    "turing_product_name": "WorkBuddy",
    # --- 用量归属（X-IDE-Name / X-IDE-Type / X-Product）---
    "ide_name": "WorkBuddy",
    "ide_type": "WorkBuddy",
    #: 请求头 `X-Product` 的值。
    #:
    #: **不是产品名，是部署形态。** 官方客户端的全局
    #: `ProductEndpointHttpInterceptor` 写死为
    #:     headers["X-Product"] ||= configuration?.deploymentType ?? "SaaS"
    #: WorkBuddy 桌面端的 `product.json` 里 `deploymentType` 就是 "SaaS"。
    #: （唯一硬编码 "WorkBuddy" 的地方是 `stdio-mcp-inspector.js` 里对
    #: `/v2/activity/workbuddy/banner` 那个窄接口，模型请求不走那条路。）
    #: 原来这里填 "WorkBuddy" 是错的 —— 官方客户端不会这样发，
    #: 本身就是个可识别的差异。
    "product": "SaaS",
    #: UA 第一段的产品名（product.json 的 applicationName）。官方在
    #: app-instance.js 里用它覆写 electron 的 userAgentFallback，拼出
    #: `${applicationName}/${version} ...`。
    "application_name": "WorkBuddy",
    #: 桌面事件指纹里的 `product` 字段（遥测用，取 deploymentType）。
    #: **与请求头的 X-Product 不是一回事**：这里是事件里的产品形态。
    "fp_product": "SaaS",
    # --- 风控闸门头（官方客户端所有 API 请求必带）---
    "headers": {
        "X-CodeBuddy-Request": "1",
        "X-Requested-With": "XMLHttpRequest",
        "Accept-Language": "zh-CN",
        # 数据用途声明：官方**每次模型请求**都带（CLI bundle 里
        # `ed[PRIVATE_DATA_HEADER] = enableModelOptimization ? "false" : "true"`）。
        # 语义是「这份数据是否属于不可用于模型优化的私有数据」：
        # 优化开启（默认）→ "false"，关闭 → "true"。
        # 缺失它就是一个可识别的差异，所以默认跟随官方默认行为报 "false"；
        # 若部署方要求「数据不参与优化」，把这里改成 "true" 即可。
        "X-Private-Data": "false",
    },
    # --- 桌面事件指纹 ---
    "ext_name": "workbuddy-desktop",
    "os": "win32",
    "arch": "x64",
    "os_version": "10.0.26220",
    "cpu_cores": 20,
    "memory_size": 24,
    "timezone": "Asia/Shanghai",
    "report_delay": 2000,
}

#: 按 realm 覆盖 `DEFAULTS` 的那几项（**两版的产品身份不同**）。
#:
#: 取值来自两版客户端的实测解包（`cli/product.json` + `install-manifest.json`）：
#:
#:     | 字段                | cn          | global                      |
#:     |---------------------|-------------|-----------------------------|
#:     | applicationName     | WorkBuddy   | workbuddy-ai                |
#:     | productName         | WorkBuddy   | WorkBuddy AI                |
#:     | turingChannelId     | 109144      | 400111                      |
#:     | authentication.id   | workbuddy-desktop | workbuddy-desktop-ai   |
#:     | desktopVersion      | 5.8.3       | 5.8.4                       |
#:
#: 为什么要兜底而不是「探测不到就算了」：线上服务器不装客户端，
#: 全靠同步过来的档案 —— 如果同步的只有一份（或没同步），兜底值就是
#: 实际发出去的身份。混用是**最明显的风控特征**（国际版请求带国内版渠道号）。
_REALM_DEFAULTS: dict[str, dict[str, Any]] = {
    "cn": {
        "ide_name": "WorkBuddy",
        "ide_type": "WorkBuddy",
        "turing_product_name": "WorkBuddy",
        "turing_channel_id": 109144,
        "application_name": "WorkBuddy",
        "ext_name": "workbuddy-desktop",
        # ⚠️ **版本号必须在这里兜底**，不能只靠 `DEFAULTS`。
        #
        # 元凶是一个只在「探测不到客户端」时才暴露的缺陷：
        # `DEFAULTS` 里写的是 `5.5.6` / `2.137.1`（realm 双域改造之前的旧值），
        # 而 `_REALM_DEFAULTS` 原来**没有覆盖这两项** —— 于是服务器 / CI /
        # 手机上探测不到客户端时，两版**都**报 5.5.6：
        #
        #     cn     期望 5.8.3  →  实际 5.5.6  ❌
        #     global 期望 5.8.4  →  实际 5.5.6  ❌
        #
        # 这不只是「版本旧一点」：两版报同一个版本号本身就是混用特征
        # （官方两版版本号不同），而且 5.5.6 是很久以前的旧客户端。
        #
        # 本地开发机装了客户端，探测得到真值，所以一直没暴露；
        # CI（无客户端）才把它打出来。
        "desktop_version": "5.8.3",
        "cli_version": "2.161.0",
    },
    "global": {
        "ide_name": "workbuddy-ai",
        "ide_type": "workbuddy-ai",
        "turing_product_name": "WorkBuddy AI",
        "turing_channel_id": 400111,
        "application_name": "workbuddy-ai",
        "ext_name": "workbuddy-desktop-ai",
        "desktop_version": "5.8.4",
        "cli_version": "2.161.0",
    },
}

#: 值为非负整数的键（保存时按此校验/转换）
_INT_KEYS = ("cpu_cores", "memory_size", "report_delay", "turing_channel_id",
             "release_date_ms")
#: 值为字符串的键
_STR_KEYS = ("desktop_version", "cli_version", "turing_product_name",
             "ide_name", "ide_type", "product", "application_name", "fp_product",
             "ext_name", "os", "arch", "os_version", "timezone", "commit")
#: 字符串字段的最大长度（防止把整个文件塞进配置里）
_MAX_STR = 512
#: 风控头名/值的最大长度
_MAX_HEADER = 256


def _int_or_none(v) -> int | None:
    """把值转成非负整数；失败返回 None。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


# ---------------------------------------------------------------------------
# 现场探测
# ---------------------------------------------------------------------------
def snapshot(realm: str = "") -> dict:
    """探测本机客户端，返回**只包含真实探测到的键**的档案。

    `realm` 指定探测哪一版（cn=国内版目录 / global=国际版目录）；
    为空时探测「候选顺序里第一个命中的」那版（旧行为，保持兼容）。

    探测不到任何东西时返回空 dict —— 调用方据此判断「这台机器上没有客户端」。
    线上服务器就是这种情况，它靠同步过来的 saved 档案工作。

    **关键**：只返回「确实从安装包里读到的」值，绝不返回 `wb_install` 的兜底
    默认值。否则线上（没装客户端）会「探测到」一堆兜底值，被当成现场探测，
    在 source=auto 下反而盖掉刚同步过来的真实参数 —— 那就完全违背了本模块
    的初衷。所以这里逐项对照 `wb_install` 的取值来源来判断。

    绝不抛异常：探测失败按「没有客户端」处理。
    """
    out: dict[str, Any] = {}
    try:
        from wb_install import WB
    except Exception as e:
        _logger.debug("wb_install 不可用，跳过客户端探测：%s", e)
        return out

    r = _norm_realm(realm)
    try:
        # sources 记录每个值的真实来源；带「兜底」字样的一律不算探测到
        data = WB._realm_data(r) if realm else WB._data()
        srcs = data.get("sources") or {}

        def real(key: str) -> bool:
            """该键是否来自真实安装包（而非兜底默认值）。"""
            s = srcs.get(key) or ""
            return bool(s)

        if data.get("desktop_version") and data.get("cli_version"):
            out["desktop_version"] = WB.desktop_version(realm=r) if realm \
                else WB.desktop_version()
            out["cli_version"] = WB.cli_version(realm=r) if realm else WB.cli_version()
        # 下面这几项来自 product.json / 安装目录，与「版本是否动态」无关，
        # 所以单独按各自来源判断；读不到就不放进探测结果。
        if real("turing_channel_id"):
            out["turing_channel_id"] = WB.turing_channel_id(realm=r) if realm \
                else WB.turing_channel_id()
        if real("commit"):
            out["commit"] = WB.commit(realm=r) if realm else WB.commit()
        if real("release_date_ms"):
            out["release_date_ms"] = WB.release_date_ms()
        # 产品身份三件套：同样只在安装包里真读到才采纳。
        #  * deploymentType -> 请求头 X-Product（部署形态，官方 WorkBuddy 为 "SaaS"）
        #  * applicationName -> UA 第一段的产品名（**两版不同**：WorkBuddy / workbuddy-ai）
        #  * authentication.id -> 桌面事件指纹的 extName
        if real("deployment_type"):
            out["product"] = WB.deployment_type()
            # 遥测事件里的 product 字段与请求头同源（都取 deploymentType）
            out["fp_product"] = WB.deployment_type()
        if real("application_name"):
            out["application_name"] = WB.application_name(realm=r) if realm \
                else WB.application_name()
        if real("plugin_name"):
            out["ext_name"] = WB.plugin_name()
        # 产品名只有产品包里写死了才可信；当前安装包里没有这个字段，
        # 所以不进探测结果（保留兜底值即可，不必伪装成探测到的）。
    except Exception as e:
        _logger.debug("客户端探测部分失败：%s", e)
    return out


# ---------------------------------------------------------------------------
# 校验 / 归一化
# ---------------------------------------------------------------------------
def normalize(raw: Any, base: dict | None = None) -> tuple[dict, list[str]]:
    """校验并归一化一份档案。返回 (干净档案, 错误列表)。

    只接受已知的键，未知键被忽略并记一条错误 —— 这样前端多传字段不会写脏库，
    同时用户能看清是哪个字段没被接受。
    """
    errors: list[str] = []
    clean: dict[str, Any] = {}

    if not isinstance(raw, dict):
        return {}, ["档案必须是 JSON 对象"]

    for k in _STR_KEYS:
        if k in raw:
            v = raw[k]
            if v is None or v == "":
                errors.append(f"{k} 不能为空（如不需要请删除该字段）")
                continue
            if not isinstance(v, (str, int, float)):
                errors.append(f"{k} 必须是字符串")
                continue
            s = str(v).strip()
            if len(s) > _MAX_STR:
                errors.append(f"{k} 过长（>{_MAX_STR} 字符）")
                continue
            clean[k] = s

    for k in _INT_KEYS:
        if k in raw:
            n = _int_or_none(raw[k])
            if n is None:
                errors.append(f"{k} 必须是非负整数")
                continue
            clean[k] = n

    if "headers" in raw:
        h = raw["headers"]
        if not isinstance(h, dict):
            errors.append("headers 必须是对象（header 名 -> 值）")
        else:
            hh: dict[str, str] = {}
            for hk, hv in h.items():
                hk = str(hk).strip()
                hv = "" if hv is None else str(hv).strip()
                if not hk or len(hk) > _MAX_HEADER or len(hv) > _MAX_HEADER:
                    errors.append(f"header 名或值非法/过长：{hk[:40]!r}")
                    continue
                hh[hk] = hv
            clean["headers"] = hh

    # 未知键
    known = set(_STR_KEYS) | set(_INT_KEYS) | {"headers", "user_agent"}
    unknown = [k for k in raw if k not in known]
    if unknown:
        errors.append("忽略未知字段：" + ", ".join(sorted(unknown)[:10]))

    # user_agent 不在 _STR_KEYS 里：它由版本号派生，存下来没有意义
    # （下次版本号一变就成了陈旧值）。这里明确拒绝并给出正确做法，
    # 比「静静存下来但永远不生效」好 —— 后者会让人以为改了却看不到效果。
    if raw.get("user_agent"):
        errors.append(
            "user_agent 不可保存（它由 desktop_version + cli_version 自动派生，"
            "保存会变成陈旧值）。要强制指定整条 UA，请设置环境变量 "
            "WORKBUDDY_USER_AGENT")

    return clean, errors


# ---------------------------------------------------------------------------
# 档案仓库
# ---------------------------------------------------------------------------
class ClientProfileStore:
    """档案的读取 / 保存 / 生效值计算（进程内缓存 + 线程安全）。

    缓存三层快照：`saved`（DB）、`source`（DB）、`detected`（现场探测）。
    三者一起刷新（TTL 30 秒，或保存时立即失效）。

    `effective()` 由这三层算出，**结果也缓存**：它在每个请求上被调用
    （拼上游头、拼桌面指纹），现场探测 + 环境变量扫描合计约 40µs，
    对高并发网关来说不该每个请求都付。

    ## 按 realm 分档（**必须**）

    国内版与国际版的客户端参数**本质不同**，实测（解包两个客户端）：

        | 字段              | cn               | global          |
        |-------------------|------------------|-----------------|
        | applicationName   | `WorkBuddy`      | `workbuddy-ai`  |
        | desktop_version   | 5.8.3            | 5.8.4           |
        | turingChannelId   | 109144           | **400111**      |
        | endpoint          | workbuddy.cn     | workbuddy.ai    |

    只存一份的话，国际版账号会带着**国内版的品牌段 + 国内版渠道号**出站 ——
    上游一比对就知道「这不是我们的官方国际版客户端」，风控必然识别。

    所以档案按 realm 存两份（`client_profile:cn` / `client_profile:global`），
    现场探测也按 realm 各探一次（`wb_install` 支持 `realm=` 参数）。

     **向后兼容**：旧的单份 `client_profile` 键仍被读取，视为 **cn 的那份**；
    global 缺失时用「按 realm 现场探测」（探测不到则用已知的官方兜底值）。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        #: realm → 保存的档案（从 DB 读）
        self._saved: dict[str, dict] = {}
        #: realm → 档案来源（auto/saved）
        self._source: dict[str, str] = {}
        #: realm → 现场探测结果
        self._detected: dict[str, dict] = {}
        #: realm → 计算后的生效值
        self._computed: dict[str, dict] = {}
        self._loaded_at: float = 0.0
        self._loaded: bool = False

    # -- DB 读写 ---------------------------------------------------------
    @staticmethod
    def _load_db(db=None) -> tuple[dict[str, dict], dict[str, str]]:
        """从 DB 读 ({realm: saved}, {realm: source})。db=None 时自建会话。

        读两个来源：
          * 新键 `client_profile:cn` / `client_profile:global`（按 realm 分档）
          * 旧键 `client_profile`（单份，**视为 cn 的那份** —— 向后兼容）
        """
        from admin.db import SessionLocal
        from admin.models import SystemSetting

        own = db is None
        if own:
            db = SessionLocal()
        keys = [PROFILE_KEY, SOURCE_KEY] + [f"{PROFILE_KEY}:{r}" for r in _REALMS] \
               + [f"{SOURCE_KEY}:{r}" for r in _REALMS]
        try:
            rows = {r.key: r.value for r in db.query(SystemSetting).filter(
                SystemSetting.key.in_(keys)).all()}
        except Exception as e:
            _logger.debug("读取 client_profile 失败：%s", e)
            return {}, {r: SOURCE_AUTO for r in _REALMS}
        finally:
            if own:
                db.close()

        saved: dict[str, dict] = {}
        sources: dict[str, str] = {}
        for realm in _REALMS:
            raw = rows.get(f"{PROFILE_KEY}:{realm}") or ""
            src = (rows.get(f"{SOURCE_KEY}:{realm}") or "").strip()
            # 向后兼容：旧的无后缀键**只当作 cn 的那份**。
            #
            # 为什么不能给 global 也用：旧档案是国内版的参数
            # （`ide_name=WorkBuddy`、`turing_product_name=WorkBuddy`、
            #  桌面端版本 5.8.3）—— 让国际版继承它，等于把「国内版身份」
            # 直接套到国际版请求上，正是要修的风控问题。
            # 国际版那份若为空，就让它走「按 realm 现场探测 + 已知官方兜底」，
            # 那比继承一份错参数准确得多。
            if not raw and realm == "cn":
                raw = rows.get(PROFILE_KEY) or ""
            if not src and realm == "cn":
                src = (rows.get(SOURCE_KEY) or "").strip()
            src = src or SOURCE_AUTO
            if src not in _VALID_SOURCES:
                src = SOURCE_AUTO
            data: dict = {}
            if raw:
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        data = obj
                except Exception as e:
                    _logger.warning("client_profile[%s] 解析失败，按空档案处理：%s", realm, e)
            saved[realm] = data
            sources[realm] = src
        return saved, sources

    def refresh(self, db=None) -> None:
        """强制重新加载（DB + 按 realm 现场探测），并重算各 realm 的生效值。"""
        data, src = self._load_db(db)
        det: dict[str, dict] = {}
        computed: dict[str, dict] = {}
        for realm in _REALMS:
            #**按 realm 探测**：本机可能同时装了国内版与国际版，
            # 两套参数（版本号/品牌段/渠道号）完全不同。
            try:
                d = snapshot(realm=realm)
            except Exception as e:
                _logger.warning("client_profile[%s] 现场探测失败：%s", realm, e)
                d = {}
            det[realm] = d
            computed[realm] = _compose(data.get(realm) or {}, src.get(realm, SOURCE_AUTO), d,
                                       realm=realm)
        with self._lock:
            self._saved = data
            self._source = src
            self._detected = det
            self._computed = computed
            self._loaded_at = time.time()
            self._loaded = True

    def invalidate(self) -> None:
        """让缓存过期（保存后调用，使新值立即生效）。"""
        with self._lock:
            self._loaded = False
            self._loaded_at = 0.0
            self._computed = None

    def _ensure_loaded(self) -> None:
        with self._lock:
            fresh = (self._loaded and self._computed is not None
                     and (time.time() - self._loaded_at) < _CACHE_TTL)
        if not fresh:
            self.refresh()

    def detected(self, realm: str = "") -> dict:
        """本机现场探测结果（只含真实探测到的键）。

        `realm` 为空时返回 cn 的那份（旧调用零改动）；要 global 的显式传。
        """
        self._ensure_loaded()
        r = _norm_realm(realm)
        with self._lock:
            return dict(self._detected.get(r) or {})

    def detected_all(self) -> dict[str, dict]:
        """两版各自的现场探测结果（后台页面同时展示用）。"""
        self._ensure_loaded()
        with self._lock:
            return {r: dict(self._detected.get(r) or {}) for r in _REALMS}

    # -- 查询 ------------------------------------------------------------
    def saved(self, realm: str = "") -> dict:
        """后台保存的档案（可能为空）。`realm` 为空 = cn（旧调用零改动）。"""
        self._ensure_loaded()
        r = _norm_realm(realm)
        with self._lock:
            out = dict(self._saved.get(r) or {})
            if isinstance(out.get("headers"), dict):
                out["headers"] = dict(out["headers"])
            return out

    def source(self, realm: str = "") -> str:
        """当前模式：auto | saved。`realm` 为空 = cn。"""
        self._ensure_loaded()
        r = _norm_realm(realm)
        with self._lock:
            return self._source.get(r, SOURCE_AUTO)

    def effective(self, db=None, realm: str = "") -> dict:
        """实际生效的档案：兜底 <- 探测/保存 <- 环境变量。

        这是所有调用方（构造 UA / 风控头 / 指纹）唯一该用的入口。
        结果被缓存，保存或 TTL 到期后重算。

        `realm` 决定用哪一份（cn/global 参数本质不同，见类文档）。
        为空 = cn，保持旧调用零改动。
        """
        if db is not None:
            self.refresh(db)
        else:
            self._ensure_loaded()
        r = _norm_realm(realm)
        with self._lock:
            comp = (self._computed or {}).get(r)
            if comp is not None:
                # headers 是嵌套 dict，必须单独复制：否则调用方改动返回值
                # 会直接污染缓存里的档案（下一个请求就带着脏值出去）。
                out = dict(comp)
                out["headers"] = dict(comp.get("headers") or {})
                return out
        # 兜底：理论上 refresh() 一定填了 _computed，这里防御性重算一次
        return _compose(self.saved(r), self.source(r), snapshot(realm=r), realm=r)

    def effective_all(self, db=None) -> dict[str, dict]:
        """两版各自的生效档案（后台同时展示；也便于对比排障）。"""
        if db is not None:
            self.refresh(db)
        else:
            self._ensure_loaded()
        out: dict[str, dict] = {}
        for r in _REALMS:
            out[r] = self.effective(realm=r)
        return out

    def sources(self, db=None, realm: str = "") -> dict:
        """每个键当前取自哪一层（排障用；``realm`` 决定看哪一版）。"""
        if db is not None:
            self.refresh(db)
        else:
            self._ensure_loaded()
        r = _norm_realm(realm)
        with self._lock:
            saved = dict((self._saved or {}).get(r) or {})
            detected = dict((self._detected or {}).get(r) or {})
            src = (self._source or {}).get(r)
        out: dict[str, str] = {}
        for k in set(list(DEFAULTS) + list(saved) + list(detected)):
            if k == "headers":
                out[k] = "合并（兜底+保存+探测）"
                continue
            env = _ENV_FOR.get(k)
            if env and (os.getenv(env) or "").strip():
                out[k] = f"环境变量 {env}"
            elif src == SOURCE_SAVED:
                out[k] = "已保存" if k in saved else (
                    "现场探测" if k in detected else "内置兜底")
            else:
                out[k] = "现场探测" if k in detected else (
                    "已保存" if k in saved else "内置兜底")
        out["source_mode"] = src
        return out


def _compose(saved: dict, src: str, detected: dict, realm: str = "") -> dict:
    """把三层（兜底 / 保存或探测 / 环境变量）合成最终生效档案。

    抽成纯函数是为了能被缓存：`effective()` 在每个请求上被调用，
    现场探测 + 环境变量扫描约 40µs，不该每个请求都重算一遍。

    关于 `layers` 的顺序（**曾经写反过，是个真 bug**）：
    下面用 `out[k] = v` 逐层覆盖，所以**越靠后的层优先级越高**。
    于是「谁优先级高」就要放在**列表末尾**：

        saved 模式：已保存 > 现场探测  ->  [探测, 保存]   保存最后写入
        auto  模式：现场探测 > 已保存  ->  [保存, 探测]   探测最后写入

    历史实现把这两个分支写反了（saved 模式让探测胜出、auto 模式让保存胜出），
    后果是「auto 模式下版本升级后自动跟上」这条承诺根本不成立 —— 只要后台
    保存过一次，真实探测值就永远被那份可能已经过期的档案压住。
    `sources()` 一直是按正确优先级写的，所以会出现「排障视图说取自现场探测、
    实际生效的却是已保存值」的自相矛盾，正是这个 bug 的显性症状。
    """
    out = dict(DEFAULTS)
    # 按 realm 覆盖兜底（两版的产品身份**本质不同**，见类文档）：
    #   cn     : ide_name/turing_product_name = WorkBuddy, channel = 109144
    #   global : ide_name = workbuddy-ai, turing_product_name = WorkBuddy AI,
    #            channel = 400111
    # 这些兜底只在「探测不到且没保存过」时生效，但正因为那种情况在服务器上
    # 很常见（线上不装客户端），兜底值必须是对的 —— 否则国际版会带着
    # 国内版身份出站。
    if realm:
        out.update(_REALM_DEFAULTS.get(_norm_realm(realm), {}))
    # headers 要按「兜底 -> 保存 -> 探测」逐层覆盖，不能整体替换：
    # 否则后台只改 Accept-Language 时，其余默认风控头会消失。
    out["headers"] = dict(DEFAULTS.get("headers") or {})

    layers = ([detected, saved] if src == SOURCE_SAVED else [saved, detected])
    # 注意：`detected` 已经**按 realm 分好**了 —— 调用方传进来的是
    # `snapshot(realm=r)` / `d = self._detected.get(r)`，即「该 realm 对应的
    # 那份探测结果」。所以这里**不需要**再按 realm 过滤。
    #
    # 历史教训：我曾在这里加过「探测值只在 application_name 与 realm 一致时
    # 才采用」的过滤，结果把 `test_realm_fallback_complete` 的
    # 「无客户端环境」用例搞挂了 —— 那种场景下 `detected` 为空，
    # 过滤逻辑误判成「探测值属于另一个 realm」而跳过了更高的层。
    for layer in layers:
        for k, v in layer.items():
            if k == "headers" and isinstance(v, dict):
                out["headers"].update(v)
            elif k == "user_agent":
                # 故意忽略：UA 始终由版本号现拼（见下方注释）。
                # 历史保存过的旧 UA 不能覆盖版本号变化。
                continue
            elif v is not None:
                # turing_channel_id/release_date_ms 为 0 视为「没探测到」
                if k in ("turing_channel_id", "release_date_ms") and not v:
                    continue
                out[k] = v

    # 环境变量最后覆盖（运维逃生门，永远最高优先级）
    out = _apply_env(out)

    # 派生 UA：**始终**按 desktop_version / cli_version 现拼。
    #
    # 为什么不把 UA 当成一个可独立保存的字段：它 100% 由两个版本号决定
    # （官方三段式 `WorkBuddy/<v> WorkBuddy/<v> CLI/<cli>`）。若把它一起
    # 存下来，就会出现「同步过一次 UA 之后，版本号再变而 UA 不变」的
    # 陈旧陷阱 —— 实测踩过：env 覆盖了 desktop_version，UA 却还在报旧版本。
    #
    # 想彻底自定义 UA 的，用 WORKBUDDY_USER_AGENT 环境变量（见 _apply_env）。
    if not out.get("user_agent"):
        out["user_agent"] = _build_ua(out.get("desktop_version"),
                                      out.get("cli_version"),
                                      out.get("application_name"))
    return out


#: 键 -> 环境变量名（用于优先级判定与覆盖）
_ENV_FOR = {
    "desktop_version": "WORKBUDDY_DESKTOP_VERSION",
    "cli_version": "WORKBUDDY_CLI_VERSION",
    "turing_channel_id": "WORKBUDDY_TURING_CHANNEL_ID",
    "turing_product_name": "WORKBUDDY_TURING_PRODUCT_NAME",
    "commit": "WORKBUDDY_COMMIT",
    "release_date_ms": "WORKBUDDY_RELEASE_DATE_MS",
    "os": "WORKBUDDY_OS",
    "arch": "WORKBUDDY_ARCH",
    "os_version": "WORKBUDDY_OS_VERSION",
    "cpu_cores": "WORKBUDDY_CPU_CORES",
    "memory_size": "WORKBUDDY_MEMORY_SIZE",
    "ide_name": "WORKBUDDY_IDE_NAME",
    "ext_name": "WORKBUDDY_EXT_NAME",
    "user_agent": "WORKBUDDY_USER_AGENT",
}


def _apply_env(profile: dict) -> dict:
    """用环境变量覆盖档案里对应的键（只覆盖显式设置了非空值的）。

    `WORKBUDDY_USER_AGENT` 是唯一的「整条 UA 覆盖」入口；其余情况 UA 都由
    版本号现拼，避免出现「UA 报旧版本、X-IDE-Version 报新版本」的错位。
    """
    for key, env in _ENV_FOR.items():
        raw = os.getenv(env)
        if raw is None or not raw.strip():
            continue
        raw = raw.strip()
        if key in _INT_KEYS:
            n = _int_or_none(raw)
            if n is not None:
                profile[key] = n
            continue
        profile[key] = raw
    return profile


def _build_ua(desktop_version: str | None, cli_version: str | None,
              application_name: str | None = None) -> str:
    """按官方三段式拼 UA：`WorkBuddy/<v> WorkBuddy/<v> CLI/<cli>`。

    第一段/第二段是产品名（官方取 `applicationName`，见 app-instance.js 里对
    `electron.app.userAgentFallback` 的覆写），第三段是内嵌 CLI 版本。
    """
    v = (desktop_version or DEFAULTS["desktop_version"]).strip()
    c = (cli_version or DEFAULTS["cli_version"]).strip()
    brand = (application_name or DEFAULTS["application_name"]).strip()
    return f"{brand}/{v} {brand}/{v} CLI/{c}"


# ---------------------------------------------------------------------------
# 单例 + 便捷函数
# ---------------------------------------------------------------------------
STORE = ClientProfileStore()


def effective(db=None, realm: str = "") -> dict:
    return STORE.effective(db, realm=realm)


def effective_all(db=None) -> dict[str, dict]:
    """两版各自的生效档案（后台展示 / 对比排障用）。"""
    return STORE.effective_all(db)


def effective_local(realm: str = "") -> dict:
    """**不含 DB** 的生效档案：兜底 + 现场探测 + 环境变量。

    专给**模块导入期**用（如 `converter.py` / `admin/backend.py` 里的
    模块级常量）。原因很实在：standalone `converter.py` 可能根本没配 MySQL，
    不该为了算一个 UA 去连库 —— 连不上会白等几秒，还打一行没意义的报错。

    发请求请用 `effective()`（含后台保存值），本函数只是「没有 DB 时的等价物」。
    """
    return _compose({}, SOURCE_AUTO, snapshot(realm=realm), realm=realm)


def saved(realm: str = "") -> dict:
    return STORE.saved(realm)


def source(realm: str = "") -> str:
    return STORE.source(realm)


def sources(db=None, realm: str = "") -> dict:
    return STORE.sources(db, realm)


def snapshot_live(realm: str = "") -> dict:
    """公开的现场探测（供后台「探测」按钮用，总是实时扫）。"""
    return snapshot(realm=realm)


def detected(realm: str = "") -> dict:
    """缓存过的现场探测结果（供后台展示；想强制实时用 snapshot_live）。"""
    return STORE.detected(realm)


def detected_all() -> dict[str, dict]:
    """两版的现场探测结果（后台同时展示）。"""
    return STORE.detected_all()


def invalidate() -> None:
    STORE.invalidate()


def ua(realm: str = "") -> str:
    """出站 User-Agent（实时，带缓存）。

    `realm` 决定用哪一版的产品身份：cn 品牌段 `WorkBuddy`、
    global 品牌段 `workbuddy-ai`（实测自客户端源码，UA 第一段取
    `cli/product.json` 的 `applicationName`）。
    """
    p = effective(realm=realm)
    return p.get("user_agent") or _build_ua(
        p.get("desktop_version"), p.get("cli_version"), p.get("application_name"))


def desktop_version(realm: str = "") -> str:
    return str(effective(realm=realm).get("desktop_version") or DEFAULTS["desktop_version"])


def cli_version(realm: str = "") -> str:
    return str(effective(realm=realm).get("cli_version") or DEFAULTS["cli_version"])


def turing_channel_id(realm: str = "") -> int:
    """风控渠道号。按 realm 取：cn=109144 / global=400111（实测两版不同）。

    兜底也按 realm —— 硬编码单一值会让国际版报国内版渠道号（风控特征）。
    """
    v = effective(realm=realm).get("turing_channel_id")
    n = _int_or_none(v)
    if n:
        return n
    r = _norm_realm(realm)
    from wb_install import REALM_FALLBACKS
    return int(REALM_FALLBACKS.get(r, {}).get("turing_channel_id", 109144))


def turing_product_name(realm: str = "") -> str:
    return str(effective(realm=realm).get("turing_product_name")
               or DEFAULTS["turing_product_name"])


def risk_headers(realm: str = "") -> dict:
    """官方客户端的风控/审计头（不含 IP、不含账号级与会话级头）。

    `realm` 决定产品身份（`X-IDE-Name` / `X-IDE-Version` 等）——
    两版的值不同，混用是明显的风控特征。
    """
    p = effective(realm=realm)
    name = str(p.get("ide_name") or DEFAULTS["ide_name"])
    h = {
        "X-IDE-Name": name,
        "X-IDE-Type": str(p.get("ide_type") or name),
        # X-Product 是部署形态（SaaS/…），**不能**回落到 ide_name（那是产品名）。
        # 回落到产品名正是修复前的老 bug，会让这个头报成官方从不发的值。
        "X-Product": str(p.get("product") or DEFAULTS["product"]),
        "X-IDE-Version": str(p.get("desktop_version") or DEFAULTS["desktop_version"]),
    }
    extra = p.get("headers") or {}
    if isinstance(extra, dict):
        for k, v in extra.items():
            # 后加入的不会覆盖上面已定的四个归属头（避免把 IDE 名改成风控头）
            h.setdefault(str(k), str(v))
    return h


def fingerprint(uid: str, machine_id: str = "", session_id: str = "") -> dict:
    """桌面事件指纹（`report_desktop_events` 每个事件都要带）。

    machineId / sessionId 由调用方按 uid 稳定派生后传入；不传则此处派生。
    """
    p = effective()
    dv = str(p.get("desktop_version") or DEFAULTS["desktop_version"])
    if not machine_id or not session_id:
        try:
            from admin.backend import stable_device_id
            machine_id = machine_id or stable_device_id(uid, "machine")
            session_id = session_id or stable_device_id(uid, "session")
        except Exception:
            machine_id = machine_id or ""
            session_id = session_id or ""
    rdm = _int_or_none(p.get("release_date_ms")) or 0
    out = {
        "timezone": str(p.get("timezone") or DEFAULTS["timezone"]),
        "reportDelay": _int_or_none(p.get("report_delay"))
                       or DEFAULTS["report_delay"],
        "product": str(p.get("fp_product") or DEFAULTS["fp_product"]),
        "ideName": str(p.get("ide_name") or DEFAULTS["ide_name"]),
        "ideType": str(p.get("ide_type") or DEFAULTS["ide_type"]),
        "ideVersion": dv,
        "machineId": machine_id,
        "sessionId": session_id,
        "extName": str(p.get("ext_name") or DEFAULTS["ext_name"]),
        "extVersion": dv,
        "os": str(p.get("os") or DEFAULTS["os"]),
        "arch": str(p.get("arch") or DEFAULTS["arch"]),
        "osVersion": str(p.get("os_version") or DEFAULTS["os_version"]),
        "cpuCores": _int_or_none(p.get("cpu_cores")) or DEFAULTS["cpu_cores"],
        "memorySize": _int_or_none(p.get("memory_size")) or DEFAULTS["memory_size"],
    }
    if rdm:
        out["releaseDate"] = rdm
    commit = p.get("commit")
    if commit:
        out["commit"] = str(commit)
    return out


# ---------------------------------------------------------------------------
# 保存 / 重置
# ---------------------------------------------------------------------------
def save(profile: Any, new_source: str | None = None, db=None,
         merge: bool = True, realm: str = "cn") -> dict:
    """保存档案到 DB，并让新值立即生效。

    merge=True 时与已有保存值合并（后台「只改一个字段」的常见用法）；
    False 时整体替换。

    `realm` 决定存到哪一份（`client_profile:cn` / `client_profile:global`）。
    默认 cn —— 但**必须显式传 global 才能改到国际版那份**，避免误改。
    """
    from admin.db import SessionLocal
    from admin.models import SystemSetting

    r = _norm_realm(realm)
    clean, errors = normalize(profile)
    if errors and not clean:
        return {"ok": False, "errors": errors}

    own = db is None
    if own:
        db = SessionLocal()
    try:
        all_saved, all_src = STORE._load_db(db)
        current = all_saved.get(r) or {}
        cur_src = all_src.get(r, SOURCE_AUTO)
        merged = dict(current) if merge else {}
        for k, v in clean.items():
            if k == "headers" and isinstance(v, dict) and merge:
                base = dict(merged.get("headers") or {})
                base.update(v)
                merged["headers"] = base
            else:
                merged[k] = v

        src = (new_source or cur_src or SOURCE_AUTO).strip()
        if src not in _VALID_SOURCES:
            errors.append(f"source 只能是 {_VALID_SOURCES}，已按 {SOURCE_AUTO} 处理")
            src = SOURCE_AUTO

        _upsert(db, SystemSetting, f"{PROFILE_KEY}:{r}",
                json.dumps(merged, ensure_ascii=False))
        _upsert(db, SystemSetting, f"{SOURCE_KEY}:{r}", src)
        db.commit()
        STORE.refresh(db)
    finally:
        if own:
            db.close()

    return {"ok": True, "errors": errors, "saved": merged, "source": src, "realm": r}


def save_both(cn: Any = None, global_: Any = None, new_source: str | None = None,
              db=None, merge: bool = True) -> dict:
    """一次保存两版（后台「探测并保存两版」用）。

    两版分别走 `save()`，任一失败也继续尝试另一个（返回各自的 ok/errors）——
    半成功总比整批失败好，且调用方能看清是哪一版出错。
    """
    out: dict[str, Any] = {}
    if cn is not None:
        out["cn"] = save(cn, new_source=new_source, db=db, merge=merge, realm="cn")
    if global_ is not None:
        out["global"] = save(global_, new_source=new_source, db=db, merge=merge,
                             realm="global")
    return {"ok": all(v.get("ok") for v in out.values()), "results": out}


def reset(db=None, realm: str = "cn") -> dict:
    """清空保存的档案，回到「纯探测 + 兜底」。（按 realm）"""
    from admin.db import SessionLocal
    from admin.models import SystemSetting

    r = _norm_realm(realm)
    own = db is None
    if own:
        db = SessionLocal()
    try:
        _upsert(db, SystemSetting, f"{PROFILE_KEY}:{r}", "")
        _upsert(db, SystemSetting, f"{SOURCE_KEY}:{r}", SOURCE_AUTO)
        # 旧单份键（视为 cn 的那份）也要清，否则 reset 后会「复现」旧值
        if r == "cn":
            _upsert(db, SystemSetting, PROFILE_KEY, "")
            _upsert(db, SystemSetting, SOURCE_KEY, SOURCE_AUTO)
        db.commit()
        STORE.refresh(db)
    finally:
        if own:
            db.close()
    return {"ok": True, "realm": r}


def _upsert(db, model, key: str, value: str) -> None:
    row = db.query(model).filter(model.key == key).first()
    if row:
        row.value = value
    else:
        db.add(model(key=key, value=value))


def diff(a: dict, b: dict) -> dict:
    """比较两份档案，返回 {key: {"old":…, "new":…}}（供后台展示改动）。"""
    out: dict[str, dict] = {}
    for k in sorted(set(list(a) + list(b))):
        if k == "headers":
            ha, hb = a.get("headers") or {}, b.get("headers") or {}
            if ha != hb:
                out["headers"] = {"old": ha, "new": hb}
            continue
        if a.get(k) != b.get(k):
            out[k] = {"old": a.get(k), "new": b.get(k)}
    return out


__all__ = [
    "STORE", "DEFAULTS", "PROFILE_KEY", "SOURCE_KEY",
    "SOURCE_AUTO", "SOURCE_SAVED",
    "effective", "effective_local", "saved", "source", "sources",
    "snapshot_live", "detected",
    "invalidate", "ua", "desktop_version", "cli_version",
    "turing_channel_id", "turing_product_name",
    "risk_headers", "fingerprint", "save", "reset", "normalize", "diff",
    "snapshot",
]
