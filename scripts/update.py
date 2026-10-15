#!/usr/bin/env python3
"""一键更新器（P5）：下载发布包 → **先验签** → 解压覆盖 → **重施安全基线** → 重启。

## 三种模式（--mode）

  * `all`     全部（引擎源码 + 面板产物 + 依赖声明）
  * `engine`  仅引擎源码（admin/ scripts/ 根模块 *.py）
  * `panel`   仅面板产物（web/out/** 静态文件）

## 为什么「先验签后解压」不是可选项

发布包里的内容会被解压**覆盖到运行中的服务目录**。没有验签，一个被投毒的
Release（或 MITM）就能直接改写引擎代码——等于把服务器交给攻击者。顺序必须是
「拿到包 → 验签 → 验签失败即拒绝（不解压不落盘）」。签名缺失同样拒绝：
「没签名」不能当作「默认可信」。

## 为什么更新后必须重施安全基线（吸收 manager 教训）

manager 的 `test_update_manager_reapplies_baseline_after_sync` 钉住：
「同步了包内源码却没重新施加端口收敛 —— 上游会暴露到公网」，
且**顺序必须在重建之前**（否则重建用的是公网可达那份配置）。
同构风险：发布包可能带出厂 .env 模板（ADMIN_HOST=0.0.0.0 / 弱口令 / docs 开启）。
所以本脚本在「解压之后、重启之前」调 scripts/security_baseline.py --apply。

## 签名格式

发布包同名 `.sig` 文件，内容是 **ssh-keygen -Y sign**（OpenSSH 签名，命名空间
`workbuddy2api-release`）的文本；公钥放部署目录 `release-signing.pub`
（或 --pubkey 指定）。ed25519 首选。校验用系统 `ssh-keygen`（Linux 自带，
零 Python 依赖）。缺失 `ssh-keygen` 时拒绝（宁可不更新，也不裸装）。

## 安全默认

  * 默认 `--dry-run`：只下载+验签+打印将要做什么，不落盘覆盖。
  * 覆盖前对将被替换的文件做 `.bak-<时间戳>` 留底（面板产物整体留底 tar）。
  * 只接受 https 且必须是本项目仓库域（--url 可覆盖，但 scheme 仍必须 https）。
  * 解压用 zipfile 并**显式拒绝路径穿越**（成员含 `..` 或绝对路径即中止）。

用法：
    python scripts/update.py --mode all  --url https://github.com/OWNER/REPO/releases/download/v1.0.1/pack.zip
    python scripts/update.py --mode panel --url ... --apply
"""
from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
NAMESPACE = "workbuddy2api-release"

#: 三种模式各自允许覆盖的路径前缀（白名单——发布包里其它文件一律不落地）。
MODE_PREFIXES: dict[str, tuple[str, ...]] = {
    "all": ("admin/", "scripts/", "web/out/", "desktop/",
            "main.py", "converter.py", "wb_install.py", "requirements.txt"),
    "engine": ("admin/", "scripts/", "main.py", "converter.py", "wb_install.py",
               "requirements.txt"),
    "panel": ("web/out/",),
}


def _check_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise ValueError("更新地址必须是 https")
    return url


def download(url: str, dest: Path, progress=None) -> Path:
    url = _check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "WorkBuddy2API-Updater"})
    with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    return dest


def _allowed_signers(pubkey: Path, tmp: Path) -> Path:
    """把裸公钥（.pub）转成 ssh-keygen -Y verify 需要的 allowed_signers 文件。

    为什么必须转：`-Y verify -f` 要的是 **allowed_signers** 格式
    （`principal [options] keytype key`），直接丢 `.pub` 过去会
    "Could not verify signature"（实测踩过）。且带上 `namespaces=` 限定，
    防止本项目的签名被挪用到别的命名空间。
    """
    text = pubkey.read_text(encoding="utf-8").strip()
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        lines.append(f'{NAMESPACE} namespaces="{NAMESPACE}" {parts[0]} {parts[1]}')
    if not lines:
        raise ValueError(f"公钥文件 {pubkey} 内容无法解析")
    target = tmp / "allowed_signers"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def verify_signature(artifact: Path, sig: Path, pubkey: Path) -> None:
    """用 ssh-keygen -Y verify 校验。任何不合规都抛 ValueError（拒绝安装）。"""
    if not pubkey.is_file():
        raise ValueError(f"缺少维护者公钥 {pubkey}——无法验签，拒绝安装")
    if not sig.is_file():
        raise ValueError("发布包没有签名文件（.sig）——拒绝安装")
    ssh_keygen = shutil.which("ssh-keygen")
    if not ssh_keygen:
        raise ValueError("系统缺少 ssh-keygen，无法验签——拒绝安装")
    with tempfile.TemporaryDirectory(prefix="wb-verify-") as td:
        allowed = _allowed_signers(pubkey, Path(td))
        cmd = [ssh_keygen, "-Y", "verify", "-f", str(allowed), "-n", NAMESPACE,
               "-I", NAMESPACE, "-s", str(sig)]
        try:
            with artifact.open("rb") as payload:
                proc = subprocess.run(cmd, stdin=payload,
                                      capture_output=True, timeout=60)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"验签执行失败：{e}")
        if proc.returncode != 0:
            detail = (proc.stdout or proc.stderr).decode("utf-8", "ignore").strip()[:200]
            raise ValueError(f"签名校验失败（包被篡改 / 签名不匹配 / 公钥不对）"
                             f"——拒绝安装{': ' + detail if detail else ''}")


def _safe_members(names: list[str]) -> None:
    """拒绝路径穿越：成员含 .. 、以 / 开头、或带盘符即中止。

    注意**不能**用 `Path(name).is_absolute()`：在 Windows 上
    `Path('/etc/passwd').is_absolute()` 返回 False（它认为无盘符就不算绝对），
    于是 Unix 绝对路径能混过去。这里直接看字符串前导字符，跨平台一致。
    """
    for name in names:
        norm = name.replace("\\", "/")
        if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
            raise ValueError(f"发布包含非法路径成员（绝对路径）：{name}")
        parts = [p for p in norm.split("/") if p not in ("", ".")]
        if ".." in parts:
            raise ValueError(f"发布包含非法路径成员（路径穿越）：{name}")


def extract(archive: Path, mode: str, staging: Path) -> list[str]:
    """解压到 staging（先不落正式目录），只保留该模式的允许路径。返回文件清单。"""
    allowed = MODE_PREFIXES[mode]
    staging.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    def keep(name: str) -> bool:
        norm = name.replace("\\", "/").lstrip("./")
        return any(norm == pre or norm.startswith(pre) for pre in allowed)

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            _safe_members(names)
            for name in names:
                if name.endswith("/") or not keep(name):
                    continue
                norm = name.replace("\\", "/").lstrip("./")
                target = staging / norm
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                written.append(norm)
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as tf:
            names = [m.name for m in tf.getmembers()]
            _safe_members(names)
            for member in tf.getmembers():
                if not member.isfile() or not keep(member.name):
                    continue
                norm = member.name.replace("\\", "/").lstrip("./")
                src = tf.extractfile(member)
                if src is None:
                    continue
                target = staging / norm
                target.parent.mkdir(parents=True, exist_ok=True)
                with src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                written.append(norm)
    else:
        raise ValueError("发布包既不是 zip 也不是 tar")
    return written


def install(staging: Path, files: list[str], *, apply: bool) -> list[str]:
    """把 staging 里的文件覆盖到 ROOT（覆盖前逐个留底 .bak-<时间戳>）。"""
    import datetime
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backups: list[str] = []
    for norm in files:
        src = staging / norm
        dst = ROOT / norm
        if not src.is_file():
            continue
        if dst.is_file():
            bak = dst.with_name(dst.name + f".bak-{stamp}")
            try:
                shutil.copy2(dst, bak)
                backups.append(str(bak.relative_to(ROOT)))
            except OSError:
                pass
        dst.parent.mkdir(parents=True, exist_ok=True)
        if apply:
            shutil.copy2(src, dst)
    return backups


def reapply_baseline(*, apply: bool, env_path: Path) -> int:
    """解压之后、重启之前：重施安全基线（manager 教训的同构接线）。"""
    script = ROOT / "scripts" / "security_baseline.py"
    if not script.is_file():
        print("[!] 未找到 scripts/security_baseline.py，跳过基线重施")
        return 0
    cmd = [sys.executable, str(script), "--env", str(env_path)]
    if apply:
        cmd.append("--apply")
    print(f"[*] 重施安全基线：{' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    print(proc.stdout.rstrip())
    if proc.stderr.strip():
        print(proc.stderr.rstrip())
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="WorkBuddy2API 一键更新（先验签后解压）")
    ap.add_argument("--mode", choices=sorted(MODE_PREFIXES), default="all")
    ap.add_argument("--url", required=True, help="发布包 https 地址")
    ap.add_argument("--sig-url", default="", help="签名文件地址（默认 url + .sig）")
    ap.add_argument("--pubkey", default=str(ROOT / "release-signing.pub"))
    ap.add_argument("--env", default=str(ROOT / ".env"))
    ap.add_argument("--apply", action="store_true", help="真正覆盖（默认 dry-run）")
    ap.add_argument("--skip-baseline", action="store_true", help="跳过基线重施（不推荐）")
    args = ap.parse_args()

    sig_url = args.sig_url or (args.url + ".sig")
    pubkey = Path(args.pubkey)
    env_path = Path(args.env)

    with tempfile.TemporaryDirectory(prefix="wb-update-") as tmp:
        tmp_path = Path(tmp)
        artifact = tmp_path / "package"
        sig = tmp_path / "package.sig"
        print(f"[*] 模式：{args.mode}；下载发布包…")
        try:
            download(args.url, artifact)
            download(sig_url, sig)
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] 下载失败：{e}")
            return 1

        print("[*] 先验签（失败即拒绝，不解压）…")
        try:
            verify_signature(artifact, sig, pubkey)
        except ValueError as e:
            print(f"[FAIL] {e}")
            return 1
        print("[OK] 已验签")

        staging = tmp_path / "staging"
        try:
            files = extract(artifact, args.mode, staging)
        except ValueError as e:
            print(f"[FAIL] 解压被拒绝：{e}")
            return 1
        if not files:
            print("[FAIL] 发布包里没有匹配该模式的文件")
            return 1
        print(f"[*] 将覆盖 {len(files)} 个文件（模式 {args.mode}）：")
        for name in files[:20]:
            print("    -", name)
        if len(files) > 20:
            print(f"    … 其余 {len(files) - 20} 个")

        backups = install(staging, files, apply=args.apply)
        if args.apply:
            print(f"[OK] 已覆盖（{len(backups)} 个原文件留底 .bak-<时间戳>）")
        else:
            print(f"[dry-run] 未覆盖任何文件（将留底 {len(backups)} 个）")

        if not args.skip_baseline:
            rc = reapply_baseline(apply=args.apply, env_path=env_path)
            if rc != 0:
                print("[!] 基线检查有 FAIL 项——按部署手册处理后再重启服务")

    if args.apply:
        print("[*] 之后请重启服务：systemctl restart workbuddy2api（或 scripts/watchdog.sh）")
    else:
        print("[dry-run] 加 --apply 才会真正覆盖并重施基线")
    return 0


if __name__ == "__main__":
    sys.exit(main())
