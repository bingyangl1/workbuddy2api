#!/usr/bin/env python3
"""发布产物签名 / 验签（P5）。

签名算法：**OpenSSH 签名**（`ssh-keygen -Y sign`，ed25519），命名空间
`workbuddy2api-release`。选它的理由：`ssh-keygen` 是 Linux/macOS/Windows 自带的
系统工具（零 Python 依赖，不像 minisign/gpg 需要额外装），且 `scripts/update.py`
的验签链路已经用它——发布端与消费端同一条工具链，少一处可漂移的实现。

## 两种运行模式

  * `sign`（发布端）：对指定文件逐个生成 `<file>.sig`。
    私钥来源优先级：`--key <path>` > 环境变量 `RELEASE_SIGNING_KEY`（**CI 用这个**，
    内容可以是私钥 PEM 文本本身）。CI 里私钥放 GitHub Actions Secrets。
  * `verify`（消费端 / 校验）：用公钥校验 `<file>` 与 `<file>.sig`。
    公钥来源：`--pubkey <path>` > 仓库内置 `release-signing.pub`。

## 为什么签名要独立成脚本（而不是塞进 workflow 内联 shell）

内联 shell 无法本地复现——出问题时只能靠猜「CI 上那行到底怎么跑的」。
独立脚本让「签名」与「验签」都能在本地用同一份代码跑一遍，
CI 只是把同一命令换个环境执行（`--key` 换成 Secrets 注入）。

## 本地演练（无需真私钥）

    # 1) 生成一对测试密钥
    ssh-keygen -t ed25519 -N "" -f /tmp/testkey -C workbuddy2api-release
    # 2) 签名
    python scripts/sign_release.py sign --key /tmp/testkey dist/*.exe
    # 3) 用对应公钥验签
    python scripts/sign_release.py verify --pubkey /tmp/testkey.pub dist/*.exe
"""
from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
NAMESPACE = "workbuddy2api-release"
PRINCIPAL = "workbuddy2api-release"
DEFAULT_PUBKEY = ROOT / "release-signing.pub"


def _ssh_keygen() -> str:
    exe = shutil.which("ssh-keygen")
    if not exe:
        raise SystemExit("找不到 ssh-keygen（OpenSSH 客户端），无法签名/验签")
    return exe


def _materialize_key(spec: str, tmp: Path) -> Path:
    """把密钥「路径 / base64 / 私钥文本」统一落成一个临时文件。

    三种形态都支持，因为注入方式不同：
      * **路径**：本地开发最直观（`--key ~/.ssh/release`）。
      * **base64**（推荐用于 CI Secrets）：私钥是多行 PEM，而 Windows 的
        CreateProcess 环境块**遇到换行会截断**——GitHub Actions 虽是 Linux，
        但同样的 workflow 在 Windows runner 上会静默拿到残缺私钥。
        base64 是单行，跨平台无坑（实测踩过：多行 env var 在 Windows 上丢内容）。
      * **原始 PEM 文本**：本机 `RELEASE_SIGNING_KEY="$(cat key)"` 的用法。
    """
    if not spec:
        raise SystemExit("未提供私钥（--key 或环境变量 RELEASE_SIGNING_KEY）")
    p = Path(spec)
    if p.is_file():
        return p

    raw = spec.strip()
    # base64（单行、无 PEM 标记）优先尝试解码
    if "BEGIN" not in raw:
        try:
            import base64
            decoded = base64.b64decode(raw, validate=True).decode("utf-8")
            if "BEGIN OPENSSH PRIVATE KEY" in decoded or "BEGIN PRIVATE KEY" in decoded:
                raw = decoded
        except Exception:  # noqa: BLE001  不是 base64 就按原文处理
            pass
    if "BEGIN OPENSSH PRIVATE KEY" not in raw and "BEGIN PRIVATE KEY" not in raw:
        raise SystemExit("私钥既不是文件路径，也不是 base64 / OpenSSH 私钥文本")
    # 完整性校验：多行 key 若被环境变量/换行截断，只会剩 BEGIN 行而缺 END 行，
    # 直接交给 ssh-keygen 会报含糊的 "Couldn't load public key"，难排查。
    if "END OPENSSH PRIVATE KEY" not in raw and "END PRIVATE KEY" not in raw:
        raise SystemExit(
            "私钥内容不完整（只有 BEGIN 没有 END 行）——多行私钥不能用环境变量直接传，"
            "请改传 base64（GitHub Secrets 推荐）："
            "base64 -w0 release_signing_key > RELEASE_SIGNING_KEY")
    if not raw.endswith("\n"):
        raw += "\n"
    target = tmp / "signing_key"
    # **必须 newline=""**：Windows 上 write_text 默认把 \n 转成 \r\n，
    # 而 OpenSSH 私钥对 CRLF 不宽容——它会以极具误导性的
    # `Couldn't load public key <path>: No such file or directory` 失败
    # （文件明明存在、大小也对不上 7 字节）。实测踩过。
    target.write_text(raw, encoding="utf-8", newline="")
    target.chmod(0o600)
    return target


def _work_dir(prefix: str):
    """给临时目录挑一个好位置（`ssh-keygen` 对 ACL 很挑）。

    ## 为什么不能直接用系统 TEMP

    Windows 上 `%TEMP%` 的 ACL 常常是 `\\OWNER RIGHTS`（S-1-3-4）或带 8.3
    短名（`C:/Users/RUNNER~1/...`）。`ssh-keygen -Y sign` 会直接拒绝：

        [FAIL] pack.exe: Bad permissions. Try removing permissions for
        user: \\OWNER RIGHTS (S-1-3-4) on file .../signing_key

    这条错误在 GitHub Actions 的 Windows runner 上**必然出现**，
    在普通开发机上却复现不了（本地 TEMP 的 ACL 正常）。

    ## 策略

    优先用环境变量 `WB_SIGN_TMPDIR`（CI 可以指到 workspace 内），
    否则用「脚本所在仓库」下的目录 —— 它继承正常 ACL，
    且不污染用户系统 TEMP。
    """
    base = os.getenv("WB_SIGN_TMPDIR")
    if base:
        p = Path(base)
        p.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(prefix=prefix, dir=str(p))
    repo_tmp = Path(__file__).resolve().parent.parent / ".sign_tmp"
    try:
        repo_tmp.mkdir(exist_ok=True)
        return tempfile.TemporaryDirectory(prefix=prefix, dir=str(repo_tmp))
    except OSError:
        # 仓库目录不可写（只读挂载等）→ 退回系统 TEMP
        return tempfile.TemporaryDirectory(prefix=prefix)


def sign(files: list[Path], key_spec: str) -> int:
    keygen = _ssh_keygen()
    with _work_dir("wb-sign-") as td:
        tmp = Path(td)
        key = _materialize_key(key_spec, tmp)
        failed = 0
        for f in files:
            if not f.is_file():
                print(f"[skip] {f} 不存在")
                failed += 1
                continue
            proc = subprocess.run(
                [keygen, "-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(f)],
                capture_output=True)
            sig = Path(str(f) + ".sig")
            if proc.returncode != 0 or not sig.is_file():
                print(f"[FAIL] {f.name}: {proc.stderr.decode('utf-8', 'ignore')[:200]}")
                failed += 1
            else:
                print(f"[OK] {f.name} → {sig.name} ({sig.stat().st_size} bytes)")
        return 1 if failed else 0


def _allowed_signers(pubkey: Path, tmp: Path) -> Path:
    """裸公钥 → allowed_signers（`ssh-keygen -Y verify -f` 要的是后者）。"""
    text = pubkey.read_text(encoding="utf-8").strip()
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            lines.append(f'{PRINCIPAL} namespaces="{NAMESPACE}" {parts[0]} {parts[1]}')
    if not lines:
        raise SystemExit(f"公钥 {pubkey} 无法解析")
    target = tmp / "allowed_signers"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def verify(files: list[Path], pubkey: Path) -> int:
    keygen = _ssh_keygen()
    if not pubkey.is_file():
        raise SystemExit(f"公钥不存在：{pubkey}")
    failed = 0
    with _work_dir("wb-verify-") as td:
        allowed = _allowed_signers(pubkey, Path(td))
        for f in files:
            sig = Path(str(f) + ".sig")
            if not f.is_file() or not sig.is_file():
                print(f"[FAIL] {f.name}: 缺少文件或签名")
                failed += 1
                continue
            with f.open("rb") as payload:
                proc = subprocess.run(
                    [keygen, "-Y", "verify", "-f", str(allowed), "-n", NAMESPACE,
                     "-I", PRINCIPAL, "-s", str(sig)],
                    stdin=payload, capture_output=True)
            if proc.returncode == 0:
                print(f"[OK] {f.name} 签名有效")
            else:
                detail = (proc.stdout or proc.stderr).decode("utf-8", "ignore").strip()[:160]
                print(f"[FAIL] {f.name} 验签失败{': ' + detail if detail else ''}")
                failed += 1
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="发布产物签名 / 验签（OpenSSH ed25519）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sign", help="对文件签名（生成 <file>.sig）")
    s.add_argument("files", nargs="+", type=Path)
    s.add_argument("--key", default=os.environ.get("RELEASE_SIGNING_KEY", ""),
                   help="私钥路径，或直接给 OpenSSH 私钥文本（默认读环境变量 RELEASE_SIGNING_KEY）")

    v = sub.add_parser("verify", help="用公钥验签")
    v.add_argument("files", nargs="+", type=Path)
    v.add_argument("--pubkey", type=Path, default=DEFAULT_PUBKEY)

    args = ap.parse_args()
    if args.cmd == "sign":
        return sign(args.files, args.key)
    return verify(args.files, args.pubkey)


if __name__ == "__main__":
    sys.exit(main())
