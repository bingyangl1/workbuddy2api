"""WorkBuddy 桌面端 $wbEncrypted 静态加密逆向解密（sym-v1 / suite 1）。

逆向结论（2027-02，依据客户端 app_source 逆向 + 实测验证）：
=================================================================
新版 WorkBuddy 桌面端把 ``.info`` 凭证文件里的敏感字段（accessToken /
refreshToken / nickname）整体加密为::

    {"$wbEncrypted": 1, "envelope": "<base64(json)>"}   # 标准 sym-v1
    {"$wbEncrypted": 1, "scheme": "asym-v1", ...}       # 高级模式（少见）

envelope JSON（suite 1）::

    {"suite": 1, "keyId": "<16hex>", "nonce": "<b64,12B>",
     "authTag": "<b64,16B>", "ciphertext": "<b64>"}

密钥链（全部在客户端主进程完成，磁盘无明文）::

    atRestSecretKey  (44 字符 base64, 32 字节)
        --sha256(utf8)-->  protectorKey (32B)          # 字段加密直接用它
        --sha256(protectorKey).hex[:16]-->  protectorKeyId
                                          = envelope.keyId = "9127dea1b44020a7"
    （keyblob 里的 userKey 914b93ee… 只用于 asym-v1 高级模式，标准模式用不到）

atRestSecretKey 的来源是定制 Electron 的原生 API
``process._linkedBinding('electron_browser_workbuddy_storage').loggerGet()``，
返回 JSON 字符串 ``{"version":1,"atRestSecretKey":"…",
"atRestDeveloperPublicKey":{…}}``。它不落盘；提取方法是给客户端传
``--require=hook.js``，在主进程启动前调用 loggerGet() 写出密钥后退出
（见 :func:`extract_at_rest_secret_key`）。

AES-256-GCM 的 AAD（sym-v1，framing='field'，整值封包）按
``Pe(keyId, suite, {framing:'field'}, 'sym-v1')`` 布局::

    "WB-AAD\\0" || [0x01]                        # magic + 版本
    || astring("WBEV1")                          # framing 常量 Xe['field']
    || astring("sym-v1")                         # scheme
    || u32be(suite)                              # g(1)
    || astring(keyId)                            # 16 hex
    || [0x02]                                    # S['field']
    || [0x00]                                    # je(sequence=undefined)
    || [0x00]                                    # final=undefined

其中 ``astring(s) = u32be(len(s)) || utf8(s)``。
加密的明文是**字段原值的 UTF-8 字节**（token 是纯字符串，不是 JSON）。

实测（2027-02）：CN / Global 两台真实客户端的 .info 全部解出明文 token，
且 token 在各自域上实测可用（CN 32 模型/余额 2715；Global 18 模型/100）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

try:  # cryptography 是桌面端依赖；服务器部署无此依赖时解密功能不可用
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    _HAS_CRYPTO = True
except Exception:  # pragma: no cover
    _HAS_CRYPTO = False

AAD_MAGIC = b"WB-AAD\x00"
PROTECTOR_KEY_ID = "9127dea1b44020a7"  # 本机实测；其他机器 keyId 可能不同

# 常见需要解密的字段（与客户端 credential-fields 适配器一致）
SENSITIVE_FIELDS = ("accessToken", "refreshToken")


def u32be(n: int) -> bytes:
    return n.to_bytes(4, "big")


def astring(s: str) -> bytes:
    """长度前缀字符串（utf8）。"""
    b = s.encode("utf-8")
    return u32be(len(b)) + b


def sym_v1_field_aad(key_id: str, suite: int = 1) -> bytes:
    """构造 sym-v1 / framing='field' 的 GCM AAD。

    对应客户端 process-cpu-sampler.js 的 ``Pe(keyId, suite, {framing:'field'},
    'sym-v1')``；整值封包时 sequence/final 均为 undefined（编码为单字节 0）。
    """
    if len(key_id) != 16 or any(c not in "0123456789abcdef" for c in key_id):
        raise ValueError(f"keyId must be 16 lowercase hex chars: {key_id!r}")
    return (
        AAD_MAGIC
        + bytes([1])  # 版本 1
        + astring("WBEV1")  # Xe['field']
        + astring("sym-v1")  # scheme
        + u32be(suite)  # g(suite)
        + astring(key_id)  # _(keyId)
        + bytes([2])  # S['field']
        + bytes([0])  # je(sequence=undefined)
        + bytes([0])  # final=undefined
    )


def derive_protector_key(at_rest_secret_key: str) -> tuple[bytes, str]:
    """atRestSecretKey -> (protectorKey, protectorKeyId)。

    对应客户端 ``Ue()``：
    ``key = sha256(atRestSecretKey utf8)``、
    ``keyId = sha256(key).hexdigest()[:16]``。
    """
    key = hashlib.sha256(at_rest_secret_key.encode("utf-8")).digest()
    key_id = hashlib.sha256(key).hexdigest()[:16]
    return key, key_id


def open_envelope(envelope_b64: str, protector_key: bytes) -> str:
    """解开单个 suite-1 字段信封，返回明文 UTF-8 字符串。

    envelope 是 base64(JSON)；ciphertext 就是字段原值（通常为 JWT 字符串）。
    """
    if not _HAS_CRYPTO:  # pragma: no cover
        raise RuntimeError("cryptography 未安装，无法解密 $wbEncrypted 信封")
    env = json.loads(base64.b64decode(envelope_b64))
    if env.get("suite") != 1:
        raise ValueError(f"不支持的 suite: {env.get('suite')}")
    aad = sym_v1_field_aad(env["keyId"])
    plain = AESGCM(protector_key).decrypt(
        base64.b64decode(env["nonce"]),
        base64.b64decode(env["ciphertext"]) + base64.b64decode(env["authTag"]),
        aad,
    )
    return plain.decode("utf-8")


def is_encrypted(value: Any) -> bool:
    """判断一个 JSON 值是否是 $wbEncrypted 包装。"""
    return (
        isinstance(value, dict)
        and value.get("$wbEncrypted") == 1
        and "envelope" in value
    )


def decrypt_value(value: Any, protector_key: bytes) -> Any:
    """递归解密 dict/list 中所有 $wbEncrypted 字段；其余原样返回。"""
    if isinstance(value, dict):
        if is_encrypted(value):
            text = open_envelope(value["envelope"], protector_key)
            try:
                return json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return text
        return {k: decrypt_value(v, protector_key) for k, v in value.items()}
    if isinstance(value, list):
        return [decrypt_value(v, protector_key) for v in value]
    return value


def decrypt_info_payload(payload: dict, protector_key: bytes) -> dict:
    """解密整个 .info JSON 对象（顶层或 auth 层均可）。"""
    if not isinstance(payload, dict):
        raise TypeError("payload 必须是 dict")
    return decrypt_value(payload, protector_key)


def load_secret_key(path: str) -> str:
    """读取提取出的 loggerGet 结果（_extracted_keys.json 同构）。"""
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    if isinstance(payload, str):  # loggerGet 返回的是 JSON 字符串
        payload = json.loads(payload)
    secret = payload.get("atRestSecretKey")
    if not secret:
        raise ValueError(f"{path} 缺少 atRestSecretKey")
    return secret


HOOK_TEMPLATE = r"""// auto-generated by admin/wb_crypto.py --require hook
const fs = require("fs");
try {{
  const b = process._linkedBinding("electron_browser_workbuddy_storage");
  fs.writeFileSync({out_path!r}, JSON.stringify(b.loggerGet()));
}} catch (e) {{
  fs.writeFileSync({err_path!r}, String(e && e.stack || e));
}}
try {{
  require("electron").app.once("ready", () => require("electron").app.exit(0));
}} catch (_) {{}}
"""


def extract_at_rest_secret_key(
    exe_path: str,
    workdir: str,
    timeout_s: int = 60,
) -> str:
    """通过 ``--require`` 注入从桌面客户端提取 atRestSecretKey。

    原理：定制 Electron 主进程启动前加载我们的 hook，调用原生
    ``workbuddyStorage.loggerGet()`` 把密钥 JSON 写到 out_path，然后 app
    在 ready 时直接退出（不会弹出完整客户端窗口）。

    依赖 ``cryptography`` 之外的运行时都不需要；客户端 exe 路径必须真实存在。
    提取一次即可长期复用（密钥不会轮换，除非客户端重置静态加密）。
    """
    import subprocess
    import tempfile

    out_path = os.path.join(workdir, "_atrest_secret.json")
    err_path = os.path.join(workdir, "_atrest_secret.err")
    hook_path = os.path.join(workdir, "_wb_keyhook.js")
    with open(hook_path, "w", encoding="utf-8") as f:
        f.write(HOOK_TEMPLATE.format(out_path=out_path, err_path=err_path))

    if os.path.exists(out_path):
        os.remove(out_path)
    if os.path.exists(err_path):
        os.remove(err_path)

    subprocess.run(
        [exe_path, f"--require={hook_path}"],
        capture_output=True,
        timeout=timeout_s,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if os.path.exists(err_path):
        with open(err_path, encoding="utf-8") as f:
            raise RuntimeError(f"hook 执行失败: {f.read()}")
    if not os.path.exists(out_path):
        raise RuntimeError(
            "未能提取 atRestSecretKey：客户端未写入密钥文件"
            "（确认 exe 路径正确、且不是 --require 被忽略的旧版本）"
        )
    return load_secret_key(out_path)


def decrypt_info_file(
    info_path: str,
    at_rest_secret_key: str,
    out_path: str | None = None,
) -> dict:
    """解密一个 .info 凭证文件；默认在旁边写 .decrypted.json。"""
    with open(info_path, encoding="utf-8") as f:
        payload = json.load(f)
    protector_key, _key_id = derive_protector_key(at_rest_secret_key)
    result = decrypt_info_payload(payload, protector_key)
    if out_path is None:
        out_path = str(info_path)[: -len(".info")] + ".decrypted.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    return result


# ── 运行时现取密钥（无需预先提取）─────────────────────────────────────────
#
# ## 为什么需要这条路
#
# `extract_at_rest_secret_key()` 用 `--require` 注入 hook 拉起客户端进程。
# 它**能用**，但有三个缺点：
#
#   1. 会**短暂启动完整客户端**（可能弹窗/抢焦点/写用户数据目录）
#   2. 产物需要落到磁盘（`_atrest_secret.json`）、需要人工搬运
#   3. 对**别的用户**而言，他们 clone 这个项目后手上**没有**这个文件 ——
#      于是「新版客户端凭据是加密的」直接把导入功能卡死
#
# 客户端**自己**当然能算出这个密钥（它得用自己的密钥解密自己的数据）。
# 所以正确做法是：需要时**当场向客户端要**，用完即弃，不落盘、不依赖任何
# 预先准备。`ELECTRON_RUN_AS_NODE=1` 让 Electron 以纯 Node 模式跑一段脚本
# （不创建窗口、不初始化 GUI），正是为此设计的开关。
#
# 安全：密钥只在本进程内存里活着，**不写日志、不写磁盘**。
# 日志里一旦出现它，等于凭据裸奔 —— 所以本模块从不打印密钥内容。

#: 以「纯 Node 模式」向客户端索取密钥的脚本。
#:
#: 回传 **`atRestSecretKey` 原文**（不是派生后的 key）—— 因为调用方会把它
#: 交给 `derive_protector_key()`，那里自己会做 sha256。若这里回传派生值，
#: 会被再哈希一次，keyId 与信封里的对不上，解密必然失败。
#:
#: fail-closed：任何一步不满足就非零退出，绝不给半个结果。
_NODE_SECRET_SCRIPT = r"""
try {
  const b = process._linkedBinding('electron_browser_workbuddy_storage');
  const payload = JSON.parse(b.loggerGet());
  if (!payload || payload.version !== 1) throw new Error('unexpected version');
  const secret = payload.atRestSecretKey;
  if (typeof secret !== 'string' || !secret) throw new Error('no secret');
  // 必须是标准 base64 的 32 字节且非全零（占位/损坏值要挡掉）
  const raw = Buffer.from(secret, 'base64');
  if (raw.length !== 32 || raw.toString('base64') !== secret || raw.every(x => x === 0)) {
    throw new Error('bad secret shape');
  }
  process.stdout.write(JSON.stringify({ atRestSecretKey: secret }));
} catch (e) {
  process.stderr.write(String((e && e.message) || e));
  process.exitCode = 1;
}
"""


def _candidate_client_exes(realm: str = "") -> list[str]:
    """可能的客户端可执行文件（用 `wb_install` 的探测结果）。

    探测失败不报错 —— 这条路本来就是「有就用、没有就走别的兜底」。
    """
    out: list[str] = []
    try:
        import wb_install
    except Exception:
        return out
    realms = [realm] if realm in ("cn", "global") else ["cn", "global"]
    for r in realms:
        try:
            d = wb_install.WB.install_dir(realm=r)
        except Exception:
            d = None
        if not d:
            continue
        for name in ("WorkBuddy.exe", "WorkBuddyAI.exe", "workbuddy-ai.exe",
                     "workbuddy.exe"):
            p = os.path.join(str(d), name)
            if os.path.isfile(p) and p not in out:
                out.append(p)
    return out


def fetch_at_rest_secret_key(
    exe_path: str | None = None,
    *,
    realm: str = "",
    timeout_s: int = 20,
) -> str | None:
    """**当场**向已安装的客户端索取 at-rest 密钥（不落盘、无需预先提取）。

    返回密钥字符串；不可用时返回 None（**不抛异常** —— 调用方按兜底链继续）。

    与 `extract_at_rest_secret_key()` 的区别：

    | | `extract_...` | `fetch_...`（本函数） |
    |---|---|---|
    | 启动方式 | `--require` 注入，**拉起完整客户端** | `ELECTRON_RUN_AS_NODE=1`，纯 Node |
    | 是否可能弹窗 | 会（抢焦点） | 不会（不初始化 GUI） |
    | 产物落盘 | 写 `_atrest_secret.json` | **不落盘**，只在内存 |
    | 别的用户能否用 | ❌ 需预先提取好的文件 | ✅ 装了客户端就能用 |
    """
    import subprocess

    exes: list[str] = []
    if exe_path:
        exes.append(exe_path)
    exes += [p for p in _candidate_client_exes(realm) if p not in exes]

    for exe in exes:
        if not exe or not os.path.isfile(exe):
            continue
        env = dict(os.environ)
        env["ELECTRON_RUN_AS_NODE"] = "1"
        # NODE_OPTIONS 可能带 `--require` 之类，干扰我们的脚本
        env.pop("NODE_OPTIONS", None)
        try:
            r = subprocess.run(
                [exe, "-e", _NODE_SECRET_SCRIPT],
                env=env, capture_output=True, timeout=timeout_s, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            continue
        if r.returncode != 0:
            continue
        raw = (r.stdout or b"").decode("utf-8", "replace").strip()
        if not raw or len(raw) > 4096:
            continue
        try:
            value = json.loads(raw)
        except Exception:
            continue
        secret = value.get("atRestSecretKey") if isinstance(value, dict) else None
        if not isinstance(secret, str) or not secret:
            continue
        # 自校验：必须能由它派生出 16 位小写 hex 的 keyId（防串改/错位）
        try:
            _key, key_id = derive_protector_key(secret)
        except Exception:
            continue
        if len(key_id) != 16:
            continue
        return secret
    return None
