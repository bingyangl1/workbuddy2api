#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 桌面端「安装位置 / 版本号 / 配置」的自动发现（不写死任何盘符）。

为什么需要这个模块
------------------
早前这些值全是写死的常量：

    D:\\WorkBuddy\\resources\\app.asar          # 脚本里的默认路径
    DESKTOP_VERSION = "5.5.6"                 # 桌面端版本
    CLI_VERSION = "2.137.1"                   # 内嵌 CLI 版本
    turing channelId = 109144                 # 风控 SDK 配置

问题有两个，都是**用户侧必然踩到**的：

1. **安装盘符不固定**：用户可能装在 `C:\\Program Files\\WorkBuddy`、`E:\\workbuddy`、
   甚至绿色版放在任意目录。写死 `D:\\` 等于对绝大多数用户直接失效。
2. **版本会变**：官方一发版，写死的 UA 立刻与真实客户端不一致
   （`WorkBuddy/5.5.6 ... CLI/2.137.1`）。网关自报旧版本号是很明显的特征，
   也容易触发上游的版本闸门。这个号必须**从本机安装包里读**。

因此本模块统一负责「找到安装目录 → 读出真实版本/配置」，并带缓存。
所有外部依赖只有标准库，且**任何一步失败都退化成兜底值，绝不抛异常**
（拿不到正确版本也要能跑，只是不如实测准确）。

发现顺序
--------
安装目录（命中即用，第一个含 `resources/app.asar` 的目录胜出）::

    1. WORKBUDDY_INSTALL_DIR            显式指定（最高优先级）
    2. 常见安装基目录下的 WorkBuddy / workbuddy
       （%LOCALAPPDATA% / %APPDATA% / %ProgramFiles% / %ProgramFiles(x86)%
         / %USERPROFILE% / %HOME%）
    3. 各盘根目录下的 workbuddy / WorkBuddy / WorkBuddy-<ver>（默认 C,D,E,F,G）
       —— 盘符可用 WORKBUDDY_DRIVES 覆盖

版本与配置（逐项独立回退，互不影响）::

    桌面端版本  WORKBUDDY_DESKTOP_VERSION
                  > resources/install-manifest.json 的 appVersion
                  > cli/package.json 的 version（排除占位 0.0.0）
                  > cli/product.json 的 genieVersion
                  > **app.asar 内 /package.json 的 version**（纯标准库读，无需 Node）
                  > WorkBuddy.exe 的版本资源（Windows）
                  > DEFAULT_DESKTOP_VERSION
    CLI 版本    WORKBUDDY_CLI_VERSION
                  > cli/package.json 的 publishConfig.customPackage.version
                    （13KB，官方同款判据；排除占位 0.0.0）
                  > cli/dist/codebuddy.js 内嵌的
                    "@tencent-ai/codebuddy-code","version":"x.y.z"（22MB，慢）
                  > DEFAULT_CLI_VERSION
    风控通道    WORKBUDDY_TURING_CHANNEL_ID
                  > cli/product.json 的 config.turingSdk.channelId
                  > DEFAULT_TURING_CHANNEL_ID

「逆向产物不一定有」怎么办
--------------------------
`cli/product.json` 与 `cli/dist/codebuddy.js` 在官方包里是 **unpacked** 的
（数据不在 app.asar 内，而在旁边的 `app.asar.unpacked/`）。若用户机器上
unpacked 目录缺失，这几个来源就全断了。此时本模块会回退到 **app.asar 内部
的文件**（`/package.json` 在数据区，用 `wb_asar.Asar` 纯标准库读出），
所以「没有 unpacked 也能拿到真实版本号」。

反过来，若连 asar 都没有（极端情况），才退化成内置兜底值。

所有路径都从环境变量读，代码里不写死任何目录::

    WORKBUDDY_INSTALL_DIR   安装基目录（也接受 resources/ 或 app.asar 本身）
    WORKBUDDY_ASAR_PATH     直接指定 app.asar
    WORKBUDDY_DRIVES        参与扫描的盘符

用法::

    from wb_install import WB
    WB.desktop_version()   # '5.5.6'
    WB.cli_version()       # '2.137.1'
    WB.desktop_ua()        # 'WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1'
    WB.install_dir()       # Path('D:/WorkBuddy') 或 None
    WB.asar_path()         # Path(.../resources/app.asar) 或 None
    WB.turing_channel_id() # 109144
    WB.describe()          # 一行诊断信息（自检 / 排障用）
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 兜底默认值
#
# 拿不到本机安装包时用这些值。它们的**唯一**作用是「不要让程序起不来」，
# 不代表真实版本 —— 真实版本务必从安装包读取。改这里没有意义，
# 用户装了新版本也不会同步。
# ---------------------------------------------------------------------------
DEFAULT_DESKTOP_VERSION = "5.5.6"
DEFAULT_CLI_VERSION = "2.137.1"
DEFAULT_TURING_CHANNEL_ID = 109144
DEFAULT_TURING_PRODUCT_NAME = "WorkBuddy"
#: UA 品牌段的兜底（客户端源码里用的是 cli/product.json 的 applicationName）。
#: 兜底取国内版值 —— 探测不到任何客户端时，报国内版身份比报一个乱七八糟的
#: 字符串更像「官方客户端」，但**realm=global 时不要用这个**（用 REALM_FALLBACKS）。
DEFAULT_APPLICATION_NAME = "WorkBuddy"
#: 客户端构建 commit 与发布日期（桌面事件指纹用）。同样只是兜底：
#: 真实值从 cli/product.json 的 commit / date 读。
DEFAULT_COMMIT = "5f9692923c93033111c51ad7b003eb80204a9b75"
DEFAULT_RELEASE_DATE_MS = 1789036585355

#: 安装基目录名候选（大小写变体都要试：Windows 上不区分，Linux 上区分）
#:
#: **必须两版都列**：国际版装在 `WorkBuddyAI`（`win32ExecutableName=WorkBuddyAI`、
#: `applicationName=workbuddy-ai`），国内版装在 `WorkBuddy`。
#: 只列国内版名字的话，国际版客户端永远探测不到 —— 后果是国际版账号带着
#: 国内版的版本号/渠道号去请求国际版上游，风控直接识别为「非官方客户端」。
_BASE_NAMES = ("WorkBuddy", "workbuddy", "WorkBuddyAI", "workbuddyai",
               "WorkBuddy AI", "workbuddy-ai")

#: realm → 安装基目录名（按优先级）。用于 `install_dir(realm=...)` 精确挑选。
_REALM_BASE_NAMES = {
    "cn": ("WorkBuddy", "workbuddy"),
    "global": ("WorkBuddyAI", "workbuddyai", "WorkBuddy AI", "workbuddy-ai"),
}

#: realm → 客户端产品身份兜底（当对应目录**探测不到**时使用）。
#:
#: 这些值取自两版客户端实测（cli/product.json + install-manifest.json）：
#:   cn     : applicationName=WorkBuddy      channel=109144 product=WorkBuddy
#:            desktop_version=5.8.3  cli_version=2.161.0
#:   global : applicationName=workbuddy-ai   channel=400111 product=WorkBuddy AI
#:            desktop_version=5.8.4  cli_version=2.161.0
#:
#: 为什么要兜底而不是「探测不到就留空」：留空会让 UA 退化成国内版形态，
#: 那正是要避免的风控特征。宁可报一个已知的官方值。
#:
#: **`desktop_version` 必须按 realm 分开给**（曾经两版共用 DEFAULTS 的版本，
#: 是个真 bug）：UA 是 `${applicationName}/${productVersion}` 拼的，所以
#: 「品牌段对、版本段错」会拼出 `workbuddy-ai/5.8.3` —— 而官方国际版是
#: `workbuddy-ai/5.8.4`。这种「半对」比全错更危险：除了版本号以外一切正常，
#: 出问题时极难想到是版本号。
#:
#: 版本号会随官方发版变化（这里只是「探测不到时的最佳猜测」）。
#: 探测得到时永远以本机实际安装版本为准。
REALM_FALLBACKS: dict[str, dict] = {
    "cn": {
        "application_name": "WorkBuddy",
        "turing_product_name": "WorkBuddy",
        "turing_channel_id": 109144,
        "desktop_version": "5.8.3",
        "cli_version": "2.161.0",
    },
    "global": {
        "application_name": "workbuddy-ai",
        "turing_product_name": "WorkBuddy AI",
        "turing_channel_id": 400111,
        "desktop_version": "5.8.4",
        "cli_version": "2.161.0",
    },
}

#: 参与扫描的默认盘符（Windows）。用 WORKBUDDY_DRIVES 覆盖。
_DEFAULT_DRIVES = "C,D,E,F,G"

#: 用户自建安装目录的常见中间层（`<盘>:\<这些>\<客户端名>`）。
#:
#: 实测踩过：客户端在 `D:\Software\WorkBuddy`，而盘根扫描找不到它 →
#: `install_dir=None` → 所有风控参数退化成兜底值（版本号/渠道号全错）。
_COMMON_SUBDIRS = ("Software", "Programs", "Apps", "Tools", "Dev")

#: asar 相对于安装基目录的路径
_ASAR_REL = ("resources", "app.asar")
_UNPACKED_REL = ("resources", "app.asar.unpacked")

#: CLI 版本内嵌在 dist/codebuddy.js 里，形如
#:   "customPackage":{"name":"@tencent-ai/codebuddy-code","version":"2.137.1",...}
#: 该文件约 22MB，所以只在需要时读一次并缓存。
_CLI_VERSION_RE = re.compile(
    rb'"name"\s*:\s*"@tencent-ai/codebuddy-code"\s*,\s*"version"\s*:\s*"([0-9][0-9A-Za-z.\-+]*)"')
#: 宽松备选：任意位置出现 @tencent-ai/codebuddy-code 后面的 version
_CLI_VERSION_RE_LOOSE = re.compile(
    rb'@tencent-ai/codebuddy-code"\s*,\s*"version"\s*:\s*"([0-9][0-9A-Za-z.\-+]*)"')

#: 缓存有效期（秒）。装/升级客户端后无需重启进程即可生效。
_CACHE_TTL = 300.0


def _dedup(seq):
    """保序去重（候选目录里会有大量重复，去重能省掉大量 stat）。"""
    seen = set()
    out = []
    for x in seq:
        if x is None:
            continue
        k = str(x).lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(x)
    return out


def _usable_version(v) -> str:
    """判断一个版本号是否可用（字符串、非空、且不是 monorepo 占位 0.0.0）。

    为什么要排除 0.0.0：`cli/package.json` 在官方包里 version 就是 `0.0.0`
    （monorepo 内包的占位），真实发布版本在 `publishConfig.customPackage.version`。
    不过滤的话会把 0.0.0 当成有效版本报上去。
    """
    if not isinstance(v, str):
        return ""
    v = v.strip()
    if not v or v == "0.0.0":
        return ""
    return v


def _iso_to_ms(s: str) -> int:
    """把 `2026-09-10T10:36:20.308Z` 这类 ISO 时间转成毫秒时间戳。

    失败返回 0（调用方据此回退到兜底值）。不引入第三方依赖：
    只处理官方 product.json 里实际出现的 `YYYY-MM-DDTHH:MM:SS[.mmm]Z` 形状。
    """
    import datetime as _dt
    try:
        t = s.strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        dt = _dt.datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _looks_like_install(d: Path) -> bool:
    """目录是否是 WorkBuddy 安装基目录（以存在 resources/app.asar 为准）。

    只认 `resources/app.asar` 这一个特征：它是 Electron 打包的必然产物，
    比「目录名像 WorkBuddy」可靠得多（用户可以把目录改名成任意名字）。
    """
    try:
        return (d / _ASAR_REL[0] / _ASAR_REL[1]).is_file()
    except OSError:
        return False


def expand_path(raw) -> Path:
    """展开用户给的路径字符串：引号、`~`、`$VAR`、`%VAR%`。

    Windows 用户从资源管理器复制路径常带引号，`.env` 里也习惯写
    `%LOCALAPPDATA%\\...`（见 ADMIN_CLIENT_AUTH_DIR）。`os.path.expandvars`
    只认 `$VAR`，不认 Windows 的 `%VAR%`，所以这里两种都处理。
    """
    s = str(raw).strip().strip('"').strip("'").strip()
    s = os.path.expandvars(s)                     # $VAR / ${VAR}
    # Windows 风格 %VAR%（expandvars 在 Linux 上不认这种写法）
    s = re.sub(r"%([^%]+)%",
               lambda m: os.getenv(m.group(1)) or m.group(0), s)
    return Path(s).expanduser()


def normalize_install_dir(raw) -> Path | None:
    """把用户给的路径收敛成 WorkBuddy **安装基目录**（无效则 None）。

    后台让用户「手动选择安装目录」时，他给的可能是三种形式中的任意一种：

        D:\\WorkBuddy                        安装基目录
        D:\\WorkBuddy\\resources             资源目录
        D:\\WorkBuddy\\resources\\app.asar   asar 文件本身

    这里统一向上回溯最多两层找 `resources/app.asar`。这样前端不必教用户
    「该选到哪一层」—— 选错了也能自动补齐，正是「自动补齐位置」的含义。

    **输入本身必须存在**才继续判断：否则 `.../resources/随便一个不存在的名字`
    会因为父目录恰好含 `app.asar` 而被「补齐」成一个有效安装目录，
    让用户以为自己填对了（实测踩过）。不存在就直接返回 None。
    """
    if not raw:
        return None
    try:
        p = expand_path(raw)
    except Exception:
        return None
    try:
        if not p.exists():
            return None
    except OSError:
        return None
    # 依次试「原样 / 父 / 祖父」：同时覆盖上述三种输入形式
    for cand in (p, p.parent, p.parent.parent):
        try:
            if cand and _looks_like_install(cand):
                return cand
        except OSError:
            continue
    return None


def _iter_drive_roots(drives: str):
    """把 'C,D,E:\\' 这类配置展开成盘根路径。

    兼容三种写法：纯盘符 `C`、带冒号 `C:`、完整根路径 `C:\\` 或 `/mnt/d`。
    """
    for raw in (drives or "").split(","):
        d = raw.strip()
        if not d:
            continue
        if re.fullmatch(r"[A-Za-z]", d):          # C
            yield Path(d + ":\\")
        elif re.fullmatch(r"[A-Za-z]:", d):       # C:
            yield Path(d + "\\")
        else:                                      # C:\ 或 /mnt/d
            yield Path(d)


class _WorkBuddyInstall:
    """安装目录与元数据的发现器（带 TTL 缓存 + 线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: dict = {}
        self._cache_at: float = 0.0
        #: 按 realm 分开的探测缓存（`{"cn": (ts, data), "global": (...)}`）。
        #: 与主缓存分开，避免「按 realm 探测」把「不带 realm 的结果」污染掉。
        self._realm_cache: dict = {}
        #: 临时目录名过滤器：非空时 `_candidate_dirs` 只产出该 realm 的目录名。
        self._realm_filter: str = ""
        #: 运行期覆盖（来自后台设置，优先级仅次于环境变量）。
        #: 用户不想改 `.env`（也不想重启进程）时，可在后台直接指定安装目录，
        #: 由 admin/wb_paths.py 在启动时注入这里。
        self._override: dict = {}
        self._override_at: float = 0.0

    # -- 运行期覆盖（后台设置用）-------------------------------------------
    def set_overrides(self, install_dir=None, asar_path=None,
                      source_dir=None) -> None:
        """设置运行期覆盖并立即生效（传 None 表示该键不覆盖）。

        这些值**不是环境变量**：环境变量是运维逃生门（最高优先级），
        覆盖是「后台可改」的日常配置（次高）。两者互不干扰 ——
        环境变量仍能压住覆盖，符合「显式运维配置优先」的直觉。
        """
        with self._lock:
            self._override = {
                "install_dir": str(install_dir) if install_dir else "",
                "asar_path": str(asar_path) if asar_path else "",
                "source_dir": str(source_dir) if source_dir else "",
            }
            self._override_at = time.time()
            self._cache = {}
            self._cache_at = 0.0
            # realm 缓存也要清：覆盖变了之后旧的两份探测结果都失效了
            self._realm_cache = {}

    def get_overrides(self) -> dict:
        with self._lock:
            return dict(self._override)

    def _ov(self, key: str) -> str:
        """取覆盖值；**环境变量存在时一律让位**（环境变量优先级更高）。"""
        env_map = {"install_dir": "WORKBUDDY_INSTALL_DIR",
                   "asar_path": "WORKBUDDY_ASAR_PATH",
                   "source_dir": "WORKBUDDY_SOURCE_DIR"}
        if (os.getenv(env_map[key]) or "").strip():
            return ""
        with self._lock:
            return (self._override.get(key) or "").strip()

    # -- 内部：解析一次全部元数据 -----------------------------------------
    def _resolve(self) -> dict:
        inst = self._find_install_dir()
        info = {
            "install_dir": inst,
            "asar": None,
            "unpacked": None,
            "desktop_version": "",
            "cli_version": "",
            "turing_channel_id": 0,
            "turing_product_name": "",
            "commit": "",
            "release_date_ms": 0,
            # 产品身份三件套（都来自 cli/product.json，见下方赋值处的说明）
            "deployment_type": "",    # deploymentType -> 请求头 X-Product
            "application_name": "",   # applicationName -> UA 第一段
            "plugin_name": "",        # authentication.id -> 指纹 extName
            "sources": {},     # 每项值的来源，便于排障
        }
        if inst is not None:
            asar = inst.joinpath(*_ASAR_REL)
            info["asar"] = asar if asar.is_file() else None
            unpacked = inst.joinpath(*_UNPACKED_REL)
            info["unpacked"] = unpacked if unpacked.is_dir() else None

        # --- 桌面端版本 + 风控配置 ---
        # 优先 install-manifest.json（安装器写的，最贴近「用户装的是哪版」）
        manifest = self._read_json(
            (info["install_dir"] / "resources" / "install-manifest.json")
            if info["install_dir"] else None)
        cli_product = self._read_json(
            (info["unpacked"] / "cli" / "product.json")
            if info["unpacked"] else None)
        cli_pkg = self._read_json(
            (info["unpacked"] / "cli" / "package.json")
            if info["unpacked"] else None)

        # 桌面端版本：按优先级逐项尝试，取第一个非空且非占位的值。
        # 用「惰性候选」而不是元组字面量：元组会把所有元素都求值一遍，
        # 于是即使 install-manifest.json 已经给了版本，也会白白去解析一遍
        # asar 头部（实测多花 ~90ms）。这里只在需要时才调用后面的读取函数。
        def _desktop_version_candidates():
            yield (manifest.get("appVersion"), "install-manifest.json")
            # cli/package.json 的 version（monorepo 里常是占位 0.0.0，要排除）
            yield (_usable_version(cli_pkg.get("version")), "cli/package.json")
            yield (cli_product.get("genieVersion"), "cli/product.json")
            # 兜底：直接从 app.asar 内部读 /package.json（不需要 unpacked）
            yield (self._asar_package_version(info["asar"]),
                   "app.asar!/package.json")
            yield (self._exe_version(info["install_dir"]), "WorkBuddy.exe")

        for val, src in _desktop_version_candidates():
            if val and isinstance(val, str) and val.strip():
                info["desktop_version"] = val.strip()
                info["sources"]["desktop_version"] = src
                break

        cfg = (cli_product.get("config") or {}) if isinstance(cli_product, dict) else {}
        turing = cfg.get("turingSdk") if isinstance(cfg, dict) else None
        if isinstance(turing, dict):
            cid = turing.get("channelId")
            if isinstance(cid, int) and cid > 0:
                info["turing_channel_id"] = cid
                info["sources"]["turing_channel_id"] = "cli/product.json"

        # --- 构建元数据（事件指纹要用，见 backend._desktop_fingerprint）---
        # 官方客户端上报的桌面指纹里有 `releaseDate`（毫秒时间戳）与 `commit`，
        # 写死的话就是「同一台机器永远报同一个发布日期」，跨版本也报旧的。
        info["commit"] = ""
        info["release_date_ms"] = 0
        c = cli_product.get("commit") if isinstance(cli_product, dict) else None
        if isinstance(c, str) and c.strip():
            info["commit"] = c.strip()
            info["sources"]["commit"] = "cli/product.json"
        d = cli_product.get("date") if isinstance(cli_product, dict) else None
        if isinstance(d, str) and d.strip():
            info["release_date_ms"] = _iso_to_ms(d.strip())
            if info["release_date_ms"]:
                info["sources"]["release_date_ms"] = "cli/product.json"

        # --- 产品身份三件套（全部来自 cli/product.json）---
        #
        # 这三项以前是写死在代码里的，结果是「换个产品线就报错身份」。它们
        # 在安装包里是明文，能读就读：
        #
        #  * deploymentType —— 出站请求头 `X-Product` 的**真正**取值。
        #    官方客户端的全局 `ProductEndpointHttpInterceptor` 里写的是
        #        headers[PRODUCT] ||= configuration?.deploymentType ?? "SaaS"
        #    也就是说凡走该拦截器的请求，X-Product 报的是"部署形态"
        #    （SaaS / CloudHosted / SelfHosted），而**不是**产品名。
        #    WorkBuddy 桌面端该字段就是 "SaaS"。
        #    注意：官方仅在 /v2/activity/workbuddy/banner 这类窄接口上
        #    硬编码过 X-Product: "WorkBuddy"（stdio-mcp-inspector.js），
        #    模型请求走的是拦截器那条路，所以取 deploymentType 才对。
        #
        #  * applicationName —— UA 第一段的产品名。官方在 app-instance.js 里
        #    用 `applicationName` 覆写 `electron.app.userAgentFallback`，
        #    拼出 `${applicationName}/${version} ...`。当前安装包是 "WorkBuddy"。
        #
        #  * authentication.id —— 桌面事件指纹里的 `extName`（扩展名）。
        #    官方默认 "workbuddy-desktop"，同样来自 product.json。
        for key, val, src_label in (
            ("deployment_type", cli_product.get("deploymentType")
                if isinstance(cli_product, dict) else None, "deploymentType"),
            ("application_name", cli_product.get("applicationName")
                if isinstance(cli_product, dict) else None, "applicationName"),
        ):
            if isinstance(val, str) and val.strip():
                info[key] = val.strip()
                info["sources"][key] = f"cli/product.json:{src_label}"

        auth = cli_product.get("authentication") if isinstance(cli_product, dict) else None
        if isinstance(auth, dict):
            pid = auth.get("id")
            if isinstance(pid, str) and pid.strip():
                info["plugin_name"] = pid.strip()
                info["sources"]["plugin_name"] = "cli/product.json:authentication.id"

        # --- CLI 版本 ---
        # 官方同款判据（见 app.asar 内 resolveBundledCliVersion）：
        #   先看 cli/package.json 的 version（非 0.0.0），
        #   再看 publishConfig.customPackage.version。
        # 这比读 22MB 的 dist/codebuddy.js 快几百倍，且两者同源。
        if isinstance(cli_pkg, dict):
            v = _usable_version(cli_pkg.get("version"))
            if not v:
                pub = cli_pkg.get("publishConfig")
                custom = pub.get("customPackage") if isinstance(pub, dict) else None
                if isinstance(custom, dict):
                    v = _usable_version(custom.get("version"))
            if v:
                info["cli_version"] = v
                info["sources"]["cli_version"] = "cli/package.json"
        # 回退：dist 内嵌（22MB，慢，但 package.json 缺失时仍可用）
        if not info["cli_version"] and info["unpacked"]:
            v = self._cli_version_from_dist(info["unpacked"])
            if v:
                info["cli_version"] = v
                info["sources"]["cli_version"] = "cli/dist/codebuddy.js"

        return info

    # -- 内部：找安装目录 -------------------------------------------------
    def _explicit_asar(self) -> Path | None:
        """用户显式指定的 app.asar 文件（环境变量 > 后台覆盖）。"""
        for raw in (os.getenv("WORKBUDDY_ASAR_PATH") or "",
                    self._override.get("asar_path") or ""):
            raw = (raw or "").strip()
            if not raw:
                continue
            try:
                a = expand_path(raw)
            except Exception:
                continue
            if a.is_file() and a.name.lower() == "app.asar":
                return a
        return None

    def _explicit_install(self) -> str:
        """用户显式指定的安装基目录（环境变量 > 后台覆盖）。"""
        return ((os.getenv("WORKBUDDY_INSTALL_DIR") or "").strip()
                or self._ov("install_dir"))

    def _find_install_dir(self) -> Path | None:
        # 显式给了 app.asar 文件路径且它存在 -> 直接由它反推安装目录，
        # 跳过整个扫描（用户最明确的意图，优先满足）。
        a = self._explicit_asar()
        if a is not None:
            # <install>/resources/app.asar -> <install>
            base = a.parent.parent
            if base.is_dir():
                return base

        for d in self._candidate_dirs():
            if _looks_like_install(d):
                return d
        return None

    def _candidate_dirs(self):
        out: list[Path] = []

        # 1) 显式指定：既接受安装基目录，也接受 resources/ 或 app.asar 本身。
        #    expand_path 顺带处理引号 / %VAR% / ~（用户手输路径的常见写法）。
        #
        # **realm 感知（真 bug 修正）**：覆盖值只有**一个**路径，无法表达
        # 「国内版在这里、国际版在那里」。所以按 realm 探测时，只有当这个
        # 覆盖路径**确实属于该 realm** 才采用；否则跳过它、走自动扫描。
        #
        # 曾经的后果：后台设过 `wb_paths.install_dir = D:\Software\WorkBuddy`
        # （国内版），于是 `_realm_data("global")` 也返回那个国内版目录 ——
        # **国际版账号带着整套国内版身份出站**（品牌/版本/渠道号全错）。
        #
        # 为什么特别难发现：全新进程里直接调用探测是**正确的**（没有 DB 覆盖），
        # 只有长驻引擎读了后台覆盖才触发。本机两版都装了也不会复现。
        explicit_ok = self._explicit_matches_realm()
        explicit = self._explicit_install() if explicit_ok else ""
        if explicit:
            try:
                e = expand_path(explicit)
            except Exception:
                e = None
            if e is not None:
                out.append(e)
                if e.name.lower() == "resources":
                    out.append(e.parent)
                elif e.name.lower() == "app.asar":
                    out.append(e.parent.parent)
                # 也把显式值当基目录，拼一次 resources 再看
                out.append(e / "resources" / "..")

        # 1b) 直接指定 app.asar（最精确，跳过整个扫描）
        a = self._explicit_asar() if explicit_ok else None
        if a is not None:
            out.append(a.parent.parent)          # <install>/resources/app.asar
            out.append(a)                        # 也把文件本身当候选

        # 2) 常见安装基目录
        #    `self._realm_filter` 非空时只产出该 realm 的目录名 —— 这是
        #    「同一台机器同时装了国内版与国际版」时按版本精确探测的关键。
        names = self._base_names()
        drives = (os.getenv("WORKBUDDY_DRIVES") or "").strip() or _DEFAULT_DRIVES
        for ev in ("LOCALAPPDATA", "APPDATA", "ProgramFiles",
                   "ProgramFiles(x86)", "ProgramW6432", "USERPROFILE", "HOME"):
            base = os.getenv(ev)
            if not base:
                continue
            for name in names:
                out.append(Path(base) / name)

        # 2b) **常见的「用户自选目录」**。
        #
        # 实测踩过：客户端装在 `D:\Software\WorkBuddy`，而上面的扫描只覆盖
        # 「盘根 / 系统标准目录」→ 探测不到 → `install_dir=None` → 所有参数
        # 退化成兜底默认值（版本号、渠道号全错，正是风控特征）。
        #
        # 这里补上典型的用户自建目录：`<盘>:\Software\`、`<盘>:\Programs\`、
        # `<盘>:\Apps\`、`<盘>:\Tools\`。多扫 4 个目录名 × 盘符的成本极低
        # （一次 `is_dir` + `resources/app.asar` 检查），换来的
        # 「不用手工配置就能自动发现」收益很高。
        for root in _iter_drive_roots(drives):
            for mid in _COMMON_SUBDIRS:
                for name in names:
                    out.append(root / mid / name)

        # 3) 各盘根目录
        for root in _iter_drive_roots(drives):
            for name in names:
                out.append(root / name)

        return _dedup(out)

    def _base_names(self) -> tuple[str, ...]:
        """当前该扫哪些安装目录名（受 realm 过滤影响）。"""
        rf = (getattr(self, "_realm_filter", "") or "").strip().lower()
        if rf in _REALM_BASE_NAMES:
            return _REALM_BASE_NAMES[rf]
        return _BASE_NAMES

    def _explicit_matches_realm(self) -> bool:
        """当前（若在做 realm 探测）的显式覆盖路径是否属于该 realm。

        * 没在做 realm 探测（`_realm_filter` 为空）→ True：
          主探测必须尊重用户的显式指定（这是既有行为，零回归）。
        * 在做 realm 探测 → 判断覆盖路径的**目录名**是否在该 realm 的名字集合里。

        判据用目录名而不是完整路径：用户可能把国际版装在
        `D:\\Software\\WorkBuddyAI` 或 `E:\\apps\\workbuddy-ai`，
        共同点是**目录名**含该版的标识。
        """
        rf = (getattr(self, "_realm_filter", "") or "").strip().lower()
        if not rf or rf not in _REALM_BASE_NAMES:
            return True
        raw = self._explicit_install() or ""
        asar_raw = ""
        try:
            asar_raw = (os.getenv("WORKBUDDY_ASAR_PATH")
                        or self._override.get("asar_path") or "")
        except Exception:
            asar_raw = ""
        target = raw or asar_raw
        if not target:
            # 没有显式覆盖 → 交回自动扫描（也是 realm 感知的）
            return True
        name = ""
        try:
            p = Path(str(target).strip().strip('"').replace("/", os.sep))
            # 若指向 app.asar 或 resources，往上找到安装目录名
            if p.name.lower() in ("app.asar", "resources"):
                p = p.parent.parent if p.name.lower() == "app.asar" else p.parent
            name = p.name
        except Exception:
            name = str(target)
        low = name.lower()
        allowed = tuple(n.lower() for n in _REALM_BASE_NAMES[rf])
        # 目录名命中该 realm 的任一别名（含前缀匹配：WorkBuddyAI 属于
        # global 的 "workbuddyai"；"WorkBuddy" 属于 cn 的 "workbuddy"）
        if low in allowed:
            return True
        # 兜底：按「最长别名优先」判断归属，避免 WorkBuddy 误判成 global
        for other, names in _REALM_BASE_NAMES.items():
            if other == rf:
                continue
            if any(low == n.lower() for n in names):
                return False
        # 名字里含 realm 标识（如 "MyWorkBuddyAI-5.8.4"）
        return any(n.lower() in low for n in _REALM_BASE_NAMES[rf])

    # -- 内部：读 json / exe 版本 / CLI 版本 ------------------------------
    @staticmethod
    def _read_json(p: Path | None) -> dict:
        if p is None:
            return {}
        try:
            if not p.is_file():
                return {}
            with open(p, "r", encoding="utf-8") as f:
                obj = json.load(f)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _asar_package_version(asar: Path | None) -> str:
        """从 app.asar **内部**读 `/package.json` 的 version。

        这是「unpacked 目录缺失」时的关键回退：`/package.json` 在 asar 数据区
        （实测 5032 字节，offset=0），可以用纯标准库定位读出，**不需要 Node、
        npm，也不需要先把 asar 解开**。而 `/cli/product.json` 是 unpacked 的，
        unpacked 目录没了就读不到 —— 所以必须多这一条路径。

        失败返回空串（还有 exe 版本资源等其它来源兜底）。
        """
        if asar is None or not asar.is_file():
            return ""
        try:
            import sys
            root = os.path.dirname(os.path.abspath(__file__))
            if root not in sys.path:
                sys.path.insert(0, root)
            from wb_asar import Asar
            obj = Asar(asar).read_json("/package.json") or {}
            return _usable_version(obj.get("version"))
        except Exception:
            return ""

    @staticmethod
    def _exe_version(install_dir: Path | None) -> str:
        """从 WorkBuddy.exe 的版本资源读版本（仅 Windows，纯标准库读法）。

        不引入 pywin32 依赖：直接解析 PE 资源太复杂，这里用最稳的
        `powershell` 调用会拖慢启动，所以优先用 `ctypes` 的
        `GetFileVersionInfo`。失败就返回空串（还有别的来源兜底）。
        """
        if install_dir is None or os.name != "nt":
            return ""
        exe = install_dir / "WorkBuddy.exe"
        if not exe.is_file():
            exe = install_dir / "CodeBuddy.exe"
        if not exe.is_file():
            return ""
        try:
            import ctypes
            from ctypes import wintypes

            ver = ctypes.WinDLL("version")
            size = ver.GetFileVersionInfoSizeW(str(exe), None)
            if not size:
                return ""
            buf = ctypes.create_string_buffer(size)
            if not ver.GetFileVersionInfoW(str(exe), 0, size, buf):
                return ""
            r = wintypes.LPVOID()
            ln = wintypes.UINT()
            if not ver.VerQueryValueW(buf, "\\", ctypes.byref(r),
                                      ctypes.byref(ln)):
                return ""
            # VS_FIXEDFILEINFO 结构，前 8 字节是签名+结构版本，接着是
            # FileVersionMS / FileVersionLS（各为 2 个 WORD）
            data = ctypes.cast(r, ctypes.POINTER(ctypes.c_uint32))
            ms, ls = data[2], data[3]
            parts = ((ms >> 16) & 0xFFFF, ms & 0xFFFF,
                     (ls >> 16) & 0xFFFF, ls & 0xFFFF)
            # 去掉末尾的 0（5.5.6.0 -> 5.5.6）；全 0 视为无效
            while len(parts) > 3 and parts[-1] == 0:
                parts = parts[:-1]
            if all(p == 0 for p in parts):
                return ""
            return ".".join(str(p) for p in parts)
        except Exception:
            return ""

    @staticmethod
    def _cli_version_from_dist(unpacked: Path) -> str:
        """从 cli/dist/codebuddy.js 读内嵌的 CLI 版本。

        `cli/package.json` 的 version 是 `0.0.0`（这是 monorepo 内包的占位），
        真正的发布版本写在 dist 里 `customPackage` 的元数据上：
            "@tencent-ai/codebuddy-code","version":"2.137.1"
        所以只能从 dist 读。文件约 22MB，读一次约 40ms，结果会缓存。
        """
        for name in ("codebuddy.js", "codebuddy-headless.js"):
            f = unpacked / "cli" / "dist" / name
            try:
                if not f.is_file():
                    continue
                blob = f.read_bytes()
            except Exception:
                continue
            for rx in (_CLI_VERSION_RE, _CLI_VERSION_RE_LOOSE):
                m = rx.search(blob)
                if m:
                    try:
                        return m.group(1).decode("ascii")
                    except Exception:
                        continue
        return ""

    # -- 对外：缓存包装 ---------------------------------------------------
    def _data(self) -> dict:
        with self._lock:
            now = time.time()
            if self._cache and (now - self._cache_at) < _CACHE_TTL:
                return self._cache
            try:
                self._cache = self._resolve()
            except Exception as e:      # 绝不因为发现失败而让调用方挂掉
                _logger.warning("WorkBuddy 安装目录发现失败：%s", e)
                self._cache = {"install_dir": None, "asar": None, "unpacked": None,
                               "desktop_version": "", "cli_version": "",
                               "turing_channel_id": 0,
                               "turing_product_name": "", "commit": "",
                               "release_date_ms": 0, "deployment_type": "",
                               "application_name": "", "plugin_name": "",
                               "sources": {}}
            self._cache_at = now
            return self._cache

    def refresh(self) -> dict:
        """丢弃缓存，强制重新扫描（用户在运行期装了新版本时可用）。"""
        with self._lock:
            self._cache = {}
            self._cache_at = 0.0
        return self._data()

    # -- 对外：各项取值 ---------------------------------------------------
    def install_dir(self, realm: str = "") -> Path | None:
        """安装基目录。`realm` 非空时**只返回该版本**的目录（没有则 None）。

        为什么要 realm：本机可能同时装了国内版（`WorkBuddy`）与国际版
        （`WorkBuddyAI`）。不带 realm 时返回「按候选顺序第一个命中的」，
        对国际版账号来说很可能是错的目录 → 参数全错 → 风控识别。
        """
        if realm:
            return self._realm_data(realm).get("install_dir")
        return self._data()["install_dir"]

    def _realm_data(self, realm: str) -> dict:
        """按 realm 探测一次并缓存（与主探测分开缓存，互不污染）。"""
        r = (realm or "").strip().lower()
        if r not in _REALM_BASE_NAMES:
            return self._data()
        key = f"__realm_{r}"
        with self._lock:
            now = time.time()
            cache = getattr(self, "_realm_cache", None)
            if cache is None:
                cache = {}
                self._realm_cache = cache
            hit = cache.get(r)
            if hit and (now - hit[0]) < _CACHE_TTL:
                return hit[1]
            try:
                data = self._resolve_for_realm(r)
            except Exception as e:
                _logger.warning("WorkBuddy[%s] 安装目录发现失败：%s", r, e)
                data = {"install_dir": None, "asar": None, "unpacked": None,
                        "desktop_version": "", "cli_version": "",
                        "turing_channel_id": 0, "turing_product_name": "",
                        "commit": "", "release_date_ms": 0, "deployment_type": "",
                        "application_name": "", "plugin_name": "", "sources": {}}
            cache[r] = (now, data)
            return data

    def _resolve_for_realm(self, realm: str) -> dict:
        """把「只含该 realm 目录名」的候选列表喂给 `_find_install_dir`。

        实现方式：临时装一个目录名过滤器，让 `_candidate_dirs` 只产出该 realm
        的候选；`_resolve()` 的其余逻辑（读 manifest / product.json / 兜底）
        完全复用，不重复实现，避免两版行为漂移。

        ## 目录名与内容不一致时**不采信**（真 bug 修复）

        `_candidate_dirs` 是按**目录名**筛的（`WorkBuddy` → cn，
        `WorkBuddyAI` → global）。但目录名不等于内容 —— 实测遇到过：
        国际版客户端被装在 `D:\\Software\\WorkBuddy`（国内版的目录名）里，
        于是 `_realm_data('cn')` 读出了 `applicationName=workbuddy-ai`、
        `desktop_version=5.8.4` —— **国内版档案里混进整套国际版身份**。

        后果：`realm.ua_brand('cn')` 返回 `'workbuddy-ai'`，
        国内版账号会带着国际版 UA 出站（混用特征）。

        所以这里在探测完之后做一次**一致性校验**：读到的
        `application_name` 必须与该 realm 期望的品牌相符，否则视为
        「本机没有这个版本的客户端」，返回空档案让兜底/已保存值接手。

        判定用「包含」而非严格相等：官方两版的 `applicationName`
        分别是 `WorkBuddy` 与 `workbuddy-ai`，用子串包含已经足够区分，
        且对大小写/后缀变化更宽容。
        """
        prev = getattr(self, "_realm_filter", "")
        self._realm_filter = realm
        try:
            data = self._resolve()
        finally:
            self._realm_filter = prev

        if not isinstance(data, dict):
            return data
        inst = data.get("install_dir")
        brand = str(data.get("application_name") or "").strip().lower()
        if not inst or not brand:
            return data       # 没探测到 / 没有品牌信息 —— 保持原样

        # 该 realm 期望的品牌标记：
        #   cn     → 不能出现 "ai"（`workbuddy` vs `workbuddy-ai`）
        #   global → 必须包含 "ai"
        looks_global = "ai" in brand.replace("workbuddy", "").replace("-", "")
        want_global = (realm or "").strip().lower() == "global"
        if looks_global != want_global:
            _logger.warning(
                "WorkBuddy[%s] 目录 %s 里的实际产品是 %r（与目录名不符），"
                "本次不采信该探测结果 —— 避免把另一版的身份套到这一版上",
                realm, inst, data.get("application_name"))
            return {"install_dir": None, "asar": None, "unpacked": None,
                    "desktop_version": "", "cli_version": "",
                    "turing_channel_id": 0, "turing_product_name": "",
                    "commit": "", "release_date_ms": 0, "deployment_type": "",
                    "application_name": "", "plugin_name": "", "sources": {}}
        return data

    def asar_path(self, realm: str = "") -> Path | None:
        """app.asar 的完整路径（找不到返回 None）。"""
        if realm:
            return self._realm_data(realm).get("asar")
        return self._data()["asar"]

    def unpacked_dir(self) -> Path | None:
        """app.asar.unpacked 目录（找不到返回 None）。"""
        return self._data()["unpacked"]

    def source_dir(self) -> Path:
        """抽取出来的源码（逆向产物）放哪里。

        优先级：``WORKBUDDY_SOURCE_DIR`` > 后台覆盖 > 用户缓存目录下的
        workbuddy2api/app_source。

        **默认不放在项目里**：解包产物有几十 MB，放进仓库会被 git 追着跑，
        也容易被误提交。放用户缓存目录（Windows `%LOCALAPPDATA%`、
        Linux `~/.cache`）既不污染仓库，又能跨进程复用。
        需要固定位置时用环境变量或后台设置指定即可（代码里不写死任何绝对路径）。
        """
        env = (os.getenv("WORKBUDDY_SOURCE_DIR") or "").strip()
        if env:
            try:
                return expand_path(env)
            except Exception:
                pass
        ov = self._ov("source_dir")
        if ov:
            try:
                return expand_path(ov)
            except Exception:
                pass
        for ev in ("LOCALAPPDATA", "XDG_CACHE_HOME"):
            base = os.getenv(ev)
            if base:
                return Path(base) / "workbuddy2api" / "app_source"
        home = os.getenv("HOME") or os.getenv("USERPROFILE")
        if home:
            return Path(home) / ".cache" / "workbuddy2api" / "app_source"
        return Path(tempfile.gettempdir()) / "workbuddy2api" / "app_source"

    # -- 对外：安装目录手工指定 / 状态 / 清理 ------------------------------
    def validate_install(self, raw) -> dict:
        """校验用户给的安装目录，返回可直接展示给前端的结果。

        接受安装基目录 / `resources/` / `app.asar` 三种输入（自动向上补齐），
        所以前端不需要教用户「该选到哪一层」。
        """
        if not raw or not str(raw).strip():
            return {"ok": False, "reason": "路径为空"}
        try:
            p = expand_path(raw)
        except Exception as e:
            return {"ok": False, "reason": f"路径无法解析：{e}"}
        if not p.exists():
            return {"ok": False, "reason": f"路径不存在：{p}", "input": str(p)}

        base = normalize_install_dir(raw)
        if base is None:
            return {
                "ok": False,
                "input": str(p),
                "reason": ("该目录下没有 resources/app.asar，不像是 WorkBuddy 安装目录。"
                           "请选择安装根目录（例如 D:\\WorkBuddy）"),
            }

        asar = base.joinpath(*_ASAR_REL)
        unpacked = base.joinpath(*_UNPACKED_REL)
        try:
            size = asar.stat().st_size
        except OSError:
            size = 0
        return {
            "ok": True,
            "input": str(p),
            "install_dir": str(base),
            "asar": str(asar),
            "asar_size": size,
            "unpacked": str(unpacked) if unpacked.is_dir() else None,
            "marker": str(base / "WorkBuddy.exe")
                      if (base / "WorkBuddy.exe").is_file() else None,
        }

    def describe_paths(self) -> dict:
        """当前各关键路径与占用情况（后台「逆向产物」面板用）。"""
        d = self._data()
        src = self.source_dir()
        asar_size = 0
        if d["asar"]:
            try:
                asar_size = d["asar"].stat().st_size
            except OSError:
                asar_size = 0
        return {
            "install_dir": str(d["install_dir"]) if d["install_dir"] else None,
            "asar": str(d["asar"]) if d["asar"] else None,
            "asar_size": asar_size,
            "unpacked": str(d["unpacked"]) if d["unpacked"] else None,
            "source_dir": str(src),
            "source_exists": src.is_dir(),
            "overrides": self.get_overrides(),
            "sync": self.is_version_dynamic(),
            "describe": self.describe(),
        }

    def source_stat(self) -> dict:
        """逆向产物的落盘统计（文件数 / 体积 / 是否已产出）。"""
        src = self.source_dir()
        out = {"dir": str(src), "exists": src.is_dir(), "files": 0,
               "bytes": 0, "extracted_at": None, "marker": None}
        if not src.is_dir():
            return out
        files = 0
        total = 0
        try:
            for p in src.rglob("*"):
                if not p.is_file():
                    continue
                files += 1
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        except OSError:
            pass
        out["files"] = files
        out["bytes"] = total
        marker = src / ".extracted"
        if marker.is_file():
            out["marker"] = str(marker)
            try:
                out["extracted_at"] = int(marker.stat().st_mtime)
            except OSError:
                pass
        return out

    def remove_source(self) -> dict:
        """删除逆向产物目录（只删我们自己产出的那个目录）。

        安全约束：**只删 `source_dir()` 指向的目录**，且要求它看起来像我们的
        产物（含 `.extracted` 标记，或目录名就是 app_source）。
        这样即使用户把 WORKBUDDY_SOURCE_DIR 指到一个重要目录，也不会被误删。
        """
        src = self.source_dir()
        if not src.is_dir():
            return {"ok": True, "removed": False, "reason": "目录不存在"}
        if not ((src / ".extracted").is_file()
                or src.name == "app_source"):
            return {"ok": False, "removed": False,
                    "reason": (f"拒绝删除：{src} 不含 .extracted 标记且目录名不是 "
                               "app_source，不像本工具产出的逆向产物")}
        import shutil
        try:
            shutil.rmtree(src)
        except OSError as e:
            return {"ok": False, "removed": False, "reason": f"删除失败：{e}"}
        return {"ok": True, "removed": True, "dir": str(src)}

    def ensure_source(self, prefixes=None, extensions=None,
                      force: bool = False, dest=None, progress=None) -> dict:
        """确保「逆向产物」存在；缺了就**自动从原始安装位置产出**。

        用户的解包产物不一定还在（可能被删、可能从没做过、也可能装在另一台机器）。
        这里按需从 app.asar 现场抽一份到 `source_dir()`，抽完即可照常检索/阅读，
        不需要用户预先跑 asar 解包，也不需要 Node/npm。

        返回 {ok, dir, extracted, reason}：
          * 已有产物且非 force -> {ok: True, extracted: False}
          * asar 存在 -> 抽取（只补缺失文件）后 {ok: True, extracted: True}
          * asar 也找不到 -> {ok: False, reason: ...}（由调用方决定是否报错）

        `dest` 覆盖输出目录（后台「拆包到指定目录」用）；
        `progress` 是可选回调，透传给 `Asar.extract` 供前端显示进度。
        """
        dest = Path(dest) if dest else self.source_dir()
        marker = dest / ".extracted"
        asar = self.asar_path()

        if not force and dest.is_dir() and any(dest.iterdir()):
            return {"ok": True, "dir": str(dest), "extracted": False,
                    "reason": "已存在抽取产物"}

        if asar is None:
            return {"ok": False, "dir": str(dest), "extracted": False,
                    "reason": "找不到 app.asar（" + self.describe() + "）"}

        try:
            import sys
            root = os.path.dirname(os.path.abspath(__file__))
            if root not in sys.path:
                sys.path.insert(0, root)
            from wb_asar import Asar
            a = Asar(asar)
            stat = a.extract(dest, prefixes=prefixes, extensions=extensions,
                             overwrite=force, progress=progress)
        except Exception as e:
            return {"ok": False, "dir": str(dest), "extracted": False,
                    "reason": f"抽取失败：{e}"}

        # 留一个标记文件，说明这个目录是自动产出的（便于判断新鲜度/来源）
        try:
            marker.write_text(
                f"source={asar}\nwritten={stat['written']}\n", encoding="utf-8")
        except OSError:
            pass

        ok = stat["written"] > 0 or stat["skipped_exists"] > 0
        return {"ok": ok, "dir": str(dest), "extracted": True,
                "reason": (f"已抽取 {stat['written']} 个文件"
                           f"（{stat['bytes'] / 1024 / 1024:.1f}MB）"
                           if ok else "asar 内没有匹配的源码文件"),
                "stat": stat}

    def desktop_version(self, realm: str = "") -> str:
        """桌面端版本：env 覆盖 > 安装包元数据 > **realm 兜底** > 通用默认。

        `realm` 非空时取**该版本客户端**的版本号（本机两版都装时必需：
        国内版 5.8.3 / 国际版 5.8.4，报错版本号是明显风控特征）。

        **为什么 realm 兜底必须在这里**（真 bug）：UA 是
        `${applicationName}/${productVersion}` 拼的。若只对 application_name
        做 realm 兜底、版本号回落到通用 DEFAULTS，会拼出 **`workbuddy-ai/5.8.3`**
        —— 品牌段是国际版、版本段是国内版。这种「半对」比全错更难发现：
        除了版本号，其余一切正常。探测不到客户端的环境（线上服务器、
        扫描范围没覆盖）**每次**都会命中这个组合。
        """
        env = (os.getenv("WORKBUDDY_DESKTOP_VERSION") or "").strip()
        if env:
            return env
        d = self._realm_data(realm) if realm else self._data()
        v = d.get("desktop_version") or ""
        if not v and realm:
            # 探测不到该版客户端：用该版的已知官方版本，而不是通用默认值
            v = REALM_FALLBACKS.get(realm.strip().lower(), {}).get("desktop_version") or ""
        return v or DEFAULT_DESKTOP_VERSION

    def cli_version(self, realm: str = "") -> str:
        """内嵌 CLI 版本：env 覆盖 > dist 内嵌元数据 > **realm 兜底** > 通用默认。

        两版 CLI 版本实测相同（2.161.0），但仍按 realm 查 —— 万一将来分开，
        这里不会静默沿用国内版的值。
        """
        env = (os.getenv("WORKBUDDY_CLI_VERSION") or "").strip()
        if env:
            return env
        d = self._realm_data(realm) if realm else self._data()
        v = d.get("cli_version") or ""
        if not v and realm:
            v = REALM_FALLBACKS.get(realm.strip().lower(), {}).get("cli_version") or ""
        return v or DEFAULT_CLI_VERSION

    def desktop_ua(self, realm: str = "") -> str:
        """官方桌面端三段式 UA：`<applicationName>/<v> <applicationName>/<v> CLI/<cli>`。

        ## 品牌段用 `applicationName` 而不是硬编码 `WorkBuddy`（实测修正）

        客户端源码里的构造是：

            if (r?.applicationName) {
              let e = [`${r.applicationName}/${r.productVersion || 'unknown'}`, ...]
            }

        所以品牌段来自 `cli/product.json` 的 `applicationName`：

            cn      → `WorkBuddy`      → `WorkBuddy/5.8.3 WorkBuddy/5.8.3 CLI/2.161.0`
            global  → `workbuddy-ai`   → `workbuddy-ai/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0`

        原实现硬编码 `WorkBuddy/`，于是**国际版账号带着国内版品牌段的 UA** 出站 ——
        这是很明显的风控特征（realm.py 里那句「Global = WorkBuddy AI」是从 Go 上游
        抄来的旧规格，实际客户端用的是小写连字符的 `workbuddy-ai`）。

        `realm` 为空时按「本机探测到的那一份」走（保持旧调用零改动）。
        """
        v = self.desktop_version(realm=realm)
        name = self.application_name(realm=realm) or DEFAULT_APPLICATION_NAME
        cli = self.cli_version(realm=realm)
        return f"{name}/{v} {name}/{v} CLI/{cli}"

    def turing_channel_id(self, realm: str = "") -> int:
        """风控渠道号（Turing channelId）。按 realm 取对应值。

        实测两版不同（来自各自 cli/product.json 的 channelId）：
            cn     = 109144
            global = 400111

        原实现硬编码 `109144` 作兜底，于是**国际版账号也会报国内版渠道号** ——
        上游一比对就知道不是官方国际版客户端。这是最硬的风控特征之一。
        """
        env = (os.getenv("WORKBUDDY_TURING_CHANNEL_ID") or "").strip()
        if env:
            try:
                return int(env)
            except ValueError:
                pass
        v = self._data().get("turing_channel_id") or 0
        if v:
            # 探测到的是国内版、但调用方要 global → 用 global 的已知值
            if realm and not self._realm_matches(realm):
                return int(REALM_FALLBACKS.get(realm, {}).get(
                    "turing_channel_id", DEFAULT_TURING_CHANNEL_ID))
            return int(v)
        if realm:
            return int(REALM_FALLBACKS.get(realm, {}).get(
                "turing_channel_id", DEFAULT_TURING_CHANNEL_ID))
        return DEFAULT_TURING_CHANNEL_ID

    def turing_product_name(self, realm: str = "") -> str:
        """风控产品名（Turing）。按 realm 兜底：cn=`WorkBuddy` / global=`WorkBuddy AI`。"""
        v = ((os.getenv("WORKBUDDY_TURING_PRODUCT_NAME") or "").strip()
             or self._data().get("turing_product_name"))
        if v:
            if realm and not self._realm_matches(realm):
                return REALM_FALLBACKS.get(realm, {}).get("turing_product_name") \
                       or DEFAULT_TURING_PRODUCT_NAME
            return v
        if realm:
            return REALM_FALLBACKS.get(realm, {}).get("turing_product_name") \
                   or DEFAULT_TURING_PRODUCT_NAME
        return DEFAULT_TURING_PRODUCT_NAME

    def commit(self, realm: str = "") -> str:
        """客户端构建 commit：env 覆盖 > cli/product.json > 兜底默认。

        桌面事件指纹里要报这个值（官方客户端会报真实 commit）。
        **两版不同**（实测 cn=db0e9b51… / global=73952edb…），所以支持 realm。
        """
        env = (os.getenv("WORKBUDDY_COMMIT") or "").strip()
        if env:
            return env
        d = self._realm_data(realm) if realm else self._data()
        # 探测到的是国内版值、但调用方要 global → 不要硬用（宁可报兜底）
        if realm and not self._realm_matches(realm):
            return DEFAULT_COMMIT
        return d.get("commit") or DEFAULT_COMMIT

    def release_date_ms(self, realm: str = "") -> int:
        """客户端发布日期（毫秒时间戳）：env 覆盖 > product.json > 兜底默认。"""
        env = (os.getenv("WORKBUDDY_RELEASE_DATE_MS") or "").strip()
        if env:
            try:
                return int(env)
            except ValueError:
                pass
        return self._data().get("release_date_ms") or DEFAULT_RELEASE_DATE_MS

    def deployment_type(self) -> str:
        """产品部署形态（`cli/product.json` 的 deploymentType），即 `X-Product` 的值。

        读不到时回落到官方硬编码的默认值 "SaaS"（与客户端
        `?? DeploymentType.SaaS` 的兜底行为一致）。
        """
        env = (os.getenv("WORKBUDDY_DEPLOYMENT_TYPE") or "").strip()
        if env:
            return env
        return self._data().get("deployment_type") or "SaaS"

    def application_name(self, realm: str = "") -> str:
        """产品名（`applicationName`），UA 第一段用它。

        读不到时按 realm 回落到已知的官方值（cn=`WorkBuddy` /
        global=`workbuddy-ai`），而不是一律 `WorkBuddy` —— 后者会让国际版
        带着国内版品牌段出站（风控特征）。

        `realm` 为空时用「本机探测到的那份」（旧调用零改动）。
        """
        env = (os.getenv("WORKBUDDY_APPLICATION_NAME") or "").strip()
        if env:
            return env
        v = self._data().get("application_name")
        if v:
            # 探测到的是国内版值、但调用方要 global → 用 global 的兜底
            if realm and not self._realm_matches(realm):
                return REALM_FALLBACKS.get(realm, {}).get("application_name") \
                       or DEFAULT_APPLICATION_NAME
            return v
        if realm:
            return REALM_FALLBACKS.get(realm, {}).get("application_name") \
                   or DEFAULT_APPLICATION_NAME
        return DEFAULT_APPLICATION_NAME

    def _realm_matches(self, realm: str) -> bool:
        """本机探测到的客户端是否属于该 realm。

        ## 判据优先级（重要）

        1. **实际产品名**（`application_name` / `win32ExecutableName`）——
           这是磁盘上真实的东西，最可靠
        2. 目录名（`install_dir` 里是否有 `WorkBuddyAI`）—— 只作兜底

        ## 为什么要优先看产品名（真 bug）

        原来只看目录名：

            s = str(install_dir).lower()
            if "workbuddyai" in s: return realm == "global"
            return realm == "cn"

        但**目录名不等于内容**。实测遇到过：国际版客户端被装在
        `D:\\Software\\WorkBuddy`（国内版的目录名）里 ——
        于是 `_realm_matches('cn')` 返回 True，`turing_channel_id('cn')`
        直接采用主探测的 `400111`（国际版渠道号）→ **国内版报国际版渠道号**。

        这与「国际版报国内版渠道号」正好相反，但同样是混用特征。
        用户装错目录是很常见的事（手动指定路径、旧版残留、覆盖安装）。
        """
        try:
            d = self._data()
            app = str(d.get("application_name") or "").strip().lower()
            exe = str(d.get("plugin_name") or "").strip().lower()
            # ① 产品名优先：`workbuddy-ai` / `WorkBuddyAI` → global
            brand = f"{app} {exe}".replace("-", "").replace(" ", "")
            if "workbuddyai" in brand:
                return realm == "global"
            if brand.strip():
                # 有明确产品名且不含 AI → cn
                return realm == "cn"
            # ② 产品名缺失（老版本 / product.json 读不到）→ 退回目录名
            inst = d.get("install_dir")
            if not inst:
                return False
            s = str(inst).lower().replace("-", "").replace(" ", "")
            if "workbuddyai" in s:
                return realm == "global"
            return realm == "cn"
        except Exception:
            return False

    def plugin_name(self) -> str:
        """扩展名（`authentication.id`），桌面事件指纹的 extName 用它。"""
        env = (os.getenv("WORKBUDDY_PLUGIN_NAME") or "").strip()
        if env:
            return env
        return self._data().get("plugin_name") or "workbuddy-desktop"

    def is_version_dynamic(self) -> bool:
        """桌面端版本是否来自安装包（而非兜底默认）。

        供自检使用：为 False 说明没找到本机安装包，正在用兜底值。
        """
        return bool(self._data()["desktop_version"])

    def describe(self) -> str:
        """一行诊断信息，便于排障与自检。"""
        d = self._data()
        inst = d["install_dir"]
        if inst is None:
            return ("未发现本机 WorkBuddy 安装目录（版本用兜底值 "
                    f"{DEFAULT_DESKTOP_VERSION}/{DEFAULT_CLI_VERSION}）。"
                    "可用 WORKBUDDY_INSTALL_DIR 指定，"
                    "或 WORKBUDDY_DRIVES 增加扫描盘符。")
        src = d["sources"]
        return (f"安装目录={inst} | 桌面端={self.desktop_version()}"
                f"({src.get('desktop_version', '兜底')}) | "
                f"CLI={self.cli_version()}({src.get('cli_version', '兜底')}) | "
                f"turingChannel={self.turing_channel_id()}"
                f"({src.get('turing_channel_id', '兜底')})")


#: 进程内单例。所有调用方共用同一份缓存，避免重复扫描盘符 / 重复读 22MB 文件。
WB = _WorkBuddyInstall()

__all__ = [
    "WB", "_WorkBuddyInstall",
    "DEFAULT_DESKTOP_VERSION", "DEFAULT_CLI_VERSION", "DEFAULT_TURING_CHANNEL_ID",
    "DEFAULT_TURING_PRODUCT_NAME", "DEFAULT_COMMIT", "DEFAULT_RELEASE_DATE_MS",
    "expand_path", "normalize_install_dir",
]
