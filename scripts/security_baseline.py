#!/usr/bin/env python3
"""更新后安全基线重施 / 校验（P5，吸收 manager 的「上游更新后重施基线」教训）。

## 为什么必须有它（manager 实测踩过的坑）

manager 的 `test_update_manager_reapplies_baseline_after_sync` 钉住一条：
**同步了上游源码后必须紧跟重施端口收敛，且顺序在建重建之前**。原话：
「同步了包内源码却没重新施加端口收敛 —— 上游会暴露到公网」「顺序反了：
收敛必须在重建之前，否则重建时用的是公网可达那份 compose」。

同构风险在本引擎：一键更新会覆盖 `admin/` 源码与 `.env` 模板，上游默认值
里有几处**出厂即不安全**的项（`ADMIN_HOST=0.0.0.0`、弱口令、docs 开启、
CONVERTER_API_KEY 为空时 /gw 无鉴权）。更新脚本必须在「解压覆盖之后、
重启服务之前」跑一次本脚本，把这些项收敛回安全取值。

## 本脚本做两件事

1. **audit（默认）**：只读检查，列出不合规项 + 修复建议。退出码 0=全通过。
2. **--apply**：把**可安全自动收敛**的项写回 `.env`（幂等）。不碰业务配置，
   不覆盖已显式设置的值（只补/纠正安全项）。

刻意不做的事：不改数据库、不改端口、不重启服务（重启是更新脚本的事）。

用法：
    python scripts/security_baseline.py                 # 只检查
    python scripts/security_baseline.py --apply         # 收敛（写 .env，备份原文件）
    python scripts/security_baseline.py --apply --env /path/to/.env
"""
from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent

#: 基线规则：(env 键, 期望值, 说明, 是否可自动收敛)
#: 「可自动收敛」= 写 .env 不会破坏业务（纯安全开关）；不可收敛的只报告。
RULES: tuple[tuple[str, str, str, bool], ...] = (
    ("ADMIN_HOST", "127.0.0.1",
     "服务只监听回环，公网入口靠 nginx 反代（仅当本机无 nginx 时该项才应放宽）", False),
    ("ADMIN_ENABLE_DOCS", "0",
     "关闭 /docs /redoc /openapi.json（对攻击者等于现成攻击面清单）", True),
    ("ADMIN_UPSTREAM_READONLY", "0",
     "生产应关闭测试护栏（=1 会拦截做任务/测速等真实上游写）", False),
)

#: 弱口令黑名单（值与用户名相同 / 出厂常见值）。
_WEAK_PASSWORDS = {"admin", "admin123", "password", "123456", "workbuddy", "root"}

#: 认证类环境变量（值不能为空）。
_REQUIRED_SECRETS = (
    ("ADMIN_USERNAME", "管理员用户名"),
    ("ADMIN_PASSWORD", "管理员口令"),
    ("ADMIN_JWT_SECRET", "JWT 签名密钥"),
)


def parse_env(text: str) -> dict[str, str]:
    """极简 .env 解析（KEY=VALUE，忽略注释与空行，去引号）。"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key.strip()] = value
    return out


def audit(env: dict[str, str], *, has_nginx: bool | None = None) -> list[tuple[str, str, str, str]]:
    """返回 [(级别, 键, 现状, 建议)]。级别：FAIL / WARN / INFO。"""
    findings: list[tuple[str, str, str, str]] = []

    for key, want, why, _auto in RULES:
        cur = env.get(key, "").strip()
        if key == "ADMIN_HOST":
            if cur in ("", "0.0.0.0", "::"):
                # 监听全网卡：有 nginx 反代时仍属「纵深防御缺失」，标 WARN
                level = "WARN" if has_nginx else "FAIL"
                findings.append((level, key, cur or "(默认 0.0.0.0)",
                                 f"建议设 {want}（{why}）"))
            continue
        if cur != want:
            findings.append(("WARN", key, cur or "(未设置)",
                             f"建议设 {want}（{why}）"))

    # 弱口令 / 缺失密钥
    for key, label in _REQUIRED_SECRETS:
        cur = env.get(key, "").strip()
        if not cur:
            findings.append(("FAIL", key, "(空)", f"{label}不能为空"))
        elif key == "ADMIN_PASSWORD" and cur.lower() in _WEAK_PASSWORDS:
            findings.append(("FAIL", key, "(弱口令)", f"{label}是出厂常见弱口令，必须更换"))
        elif key == "ADMIN_JWT_SECRET" and len(cur) < 16:
            findings.append(("FAIL", key, f"(长度 {len(cur)})",
                             "JWT 签名密钥过短（<16），建议 32+ 位随机串"))

    # /gw 无鉴权风险
    if not env.get("CONVERTER_API_KEY", "").strip():
        findings.append(("INFO", "CONVERTER_API_KEY", "(空)",
                         "未设置时引擎会**拒绝挂载 /gw**（安全默认）；如需内嵌网关再配置"))

    # 信任代理网段（配置了过宽的 XFF 信任会让审计 IP 可伪造）
    if env.get("ADMIN_TRUSTED_PROXY_CIDRS", "").strip() in ("0.0.0.0/0", "::/0"):
        findings.append(("FAIL", "ADMIN_TRUSTED_PROXY_CIDRS", "0.0.0.0/0",
                         "信任全部来源 = 审计/风控的客户端 IP 可任意伪造"))

    return findings


def apply_fixes(env_path: Path, env: dict[str, str]) -> list[str]:
    """把「可自动收敛」的规则写回 .env（幂等）。返回实际改动列表。

    只动 RULES 里标记可自动收敛的键，且**不覆盖已有的合规值**。
    改前备份成 .env.bak-<时间戳>（不覆盖旧备份）。
    """
    import datetime
    changes: list[str] = []
    text = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    for key, want, _why, auto in RULES:
        if not auto:
            continue
        if env.get(key, "").strip() == want:
            continue
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=.*$", re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(f"{key}={want}", text, count=1)
            changes.append(f"{key}={want} (已存在，改值)")
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += f"{key}={want}\n"
            changes.append(f"{key}={want} (新增)")
    if changes:
        if env_path.is_file():
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(env_path, env_path.with_name(env_path.name + f".bak-{stamp}"))
        env_path.write_text(text, encoding="utf-8")
    return changes


def main() -> int:
    ap = argparse.ArgumentParser(description="更新后安全基线校验 / 收敛")
    ap.add_argument("--apply", action="store_true", help="写回 .env（可安全收敛的项）")
    ap.add_argument("--env", default=str(ROOT / ".env"), help=".env 路径")
    ap.add_argument("--nginx", action="store_true",
                    help="本机有 nginx 反代（放宽 ADMIN_HOST 判定为 WARN）")
    args = ap.parse_args()

    env_path = Path(args.env)
    if not env_path.is_file():
        print(f"[!] 未找到 {env_path}（生产请先由部署模板生成）")
        return 1
    env = parse_env(env_path.read_text(encoding="utf-8"))
    findings = audit(env, has_nginx=args.nginx or None)

    fails = [f for f in findings if f[0] == "FAIL"]
    warns = [f for f in findings if f[0] == "WARN"]
    infos = [f for f in findings if f[0] == "INFO"]

    for level, key, cur, advice in findings:
        icon = {"FAIL": "[FAIL]", "WARN": "[WARN]", "INFO": "[info]"}[level]
        print(f"{icon} {key} = {cur} → {advice}")

    if args.apply:
        changes = apply_fixes(env_path, env)
        if changes:
            print("\n[+] 已收敛（原文件已备份 .bak-<时间戳>）：")
            for c in changes:
                print("    -", c)
        else:
            print("\n[+] 无需收敛（可自动项均已合规）")

    print(f"\n汇总：{len(fails)} 项必须处理 / {len(warns)} 项建议 / {len(infos)} 项知悉")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
