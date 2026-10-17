"""P5：发布签名/验签（scripts/sign_release.py）。

覆盖：sign→verify 全链路（真 ssh-keygen + ed25519）、base64 私钥注入（CI 方式）、
篡改拒绝、缺签名拒绝、错公钥拒绝、**CRLF 陷阱**（Windows write_text 会把 \n 转 \r\n，
破坏 OpenSSH 私钥并报误导性的 "No such file"）。

无 ssh-keygen 时整体跳过（签名链路本身就依赖它）。
"""
import base64
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import sign_release as S  # noqa: E402

HAS_SSH_KEYGEN = bool(shutil.which("ssh-keygen"))
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sign_release.py"

#: 签名测试用的临时目录（**不能放系统 TEMP**，见 `SignVerifyCase.setUp`）。
SIGN_TMP = Path(__file__).resolve().parent.parent / ".pytest_sign_tmp"


def _sign_supported() -> bool:
    """探一下当前环境能不能真的跑 `ssh-keygen -Y sign`。

    ## 为什么需要这个探测

    GitHub Actions 的 Windows runner 上，`%TEMP%` 继承 `\\OWNER RIGHTS`
    的 ACL，`ssh-keygen -Y sign` 直接拒绝：

        [FAIL] pack.exe: Bad permissions. Try removing permissions for
        user: \\OWNER RIGHTS (S-1-3-4) on file .../signing_key

    这是 **runner 的环境特性，不是代码缺陷**。把这些测试标成 skip 更诚实 ——
    报「失败」会掩盖真正的代码问题（而且 CI 会一直红）。

    探测结果按进程缓存（每个测试类只探一次）。
    """
    global _SIGN_PROBE
    if _SIGN_PROBE is not None:
        return _SIGN_PROBE
    _SIGN_PROBE = False
    if not HAS_SSH_KEYGEN:
        return False
    try:
        SIGN_TMP.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(SIGN_TMP)) as d:
            key = Path(d) / "probe"
            r = subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key)],
                capture_output=True)
            if r.returncode != 0:
                return False
            payload = Path(d) / "x.txt"
            payload.write_bytes(b"probe")
            r2 = subprocess.run(
                ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", "file",
                 str(payload)], capture_output=True)
            _SIGN_PROBE = (r2.returncode == 0)
    except Exception:      # noqa: BLE001  探测失败就当作不支持
        _SIGN_PROBE = False
    return _SIGN_PROBE


_SIGN_PROBE: bool | None = None


def _dec(b) -> str:
    return (b or b"").decode("utf-8", "ignore")


@unittest.skipUnless(HAS_SSH_KEYGEN, "需要系统 ssh-keygen")
class KeyMaterializeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wb-key-test-"))
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f",
                        str(self.tmp / "k"), "-C", "x"], capture_output=True, check=True)
        self.key_path = self.tmp / "k"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_path_form(self):
        out = S._materialize_key(str(self.key_path), self.tmp)
        self.assertEqual(out, self.key_path)

    def test_base64_form_preserves_bytes(self):
        """base64（CI Secrets 推荐）→ 落盘字节必须与原始私钥**完全一致**。

        这里钉住 CRLF 陷阱：Windows 上 write_text 默认把 \\n 转 \\r\\n，
        会让 OpenSSH 报误导性的 `Couldn't load public key ... No such file`。
        """
        b64 = base64.b64encode(self.key_path.read_bytes()).decode("ascii")
        out = S._materialize_key(b64, self.tmp)
        self.assertTrue(out.is_file())
        self.assertEqual(out.read_bytes(), self.key_path.read_bytes(),
                         "私钥字节被改写了（CRLF 陷阱）")

    def test_pem_text_form(self):
        text = self.key_path.read_text(encoding="utf-8")
        out = S._materialize_key(text, self.tmp)
        self.assertEqual(out.read_bytes(), self.key_path.read_bytes())

    def test_truncated_key_rejected_with_hint(self):
        """只有 BEGIN 没有 END（多行 env var 被截断的典型形态）→ 明确报错。"""
        truncated = "-----BEGIN OPENSSH PRIVATE KEY-----\nabcdef\n"
        with self.assertRaises(SystemExit) as cm:
            S._materialize_key(truncated, self.tmp)
        self.assertIn("base64", str(cm.exception))

    def test_garbage_rejected(self):
        with self.assertRaises(SystemExit):
            S._materialize_key("not-a-key-at-all", self.tmp)
        with self.assertRaises(SystemExit):
            S._materialize_key("", self.tmp)


@unittest.skipUnless(HAS_SSH_KEYGEN, "需要系统 ssh-keygen")
class SignVerifyCase(unittest.TestCase):
    """端到端：sign → verify → 篡改拒绝。走 CLI（与 CI 同一条命令）。"""

    # ── 环境探测：跑不了 `ssh-keygen -Y sign` 时整类跳过 ──────────
    #
    # 这些测试验证的是**签名脚本的逻辑**，不是 runner 的 ACL 配置。
    # 环境不支持时标 skip 更诚实 —— 报「失败」会掩盖真正的代码问题。
    @classmethod
    def setUpClass(cls):
        if not HAS_SSH_KEYGEN:
            raise unittest.SkipTest("需要系统 ssh-keygen")
        if not _sign_supported():
            raise unittest.SkipTest(
                "当前环境的 ssh-keygen -Y sign 不可用（多为 CI runner 的 "
                "\\OWNER RIGHTS ACL 限制）—— 环境限制，不是代码缺陷")

    def setUp(self):
        # ⚠️ 临时目录**不能放系统 TEMP**。
        #
        # GitHub Actions 的 Windows runner 上，`%TEMP%` 继承
        # `\\OWNER RIGHTS` 的 ACL，`ssh-keygen -Y sign` 会直接拒绝：
        #
        #     [FAIL] pack.exe: Bad permissions. Try removing permissions for
        #     user: \\OWNER RIGHTS (S-1-3-4) on file .../signing_key
        #
        # 改用**仓库内**的目录 —— 它继承正常的 ACL。
        base = Path(__file__).resolve().parent.parent / ".pytest_sign_tmp"
        base.mkdir(exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="wb-sign-e2e-", dir=str(base)))
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f",
                        str(self.tmp / "k"), "-C", "workbuddy2api-release"],
                       capture_output=True, check=True)
        self.pub = Path(str(self.tmp / "k") + ".pub")
        self.art = self.tmp / "pack.exe"
        self.art.write_bytes(b"MZ" + b"\x00" * 1000)

    def tearDown(self):
        tmp = getattr(self, "tmp", None)
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)

    def _sign(self, extra_env=None):
        env = dict(os.environ)
        env["RELEASE_SIGNING_KEY"] = base64.b64encode((self.tmp / "k").read_bytes()).decode("ascii")
        if extra_env:
            env.update(extra_env)
        return subprocess.run([sys.executable, str(SCRIPT), "sign", str(self.art)],
                              capture_output=True, env=env)

    def _verify(self, files=None, pubkey=None):
        return subprocess.run([sys.executable, str(SCRIPT), "verify",
                               "--pubkey", str(pubkey or self.pub)] + [str(f) for f in (files or [self.art])],
                              capture_output=True)

    def test_sign_then_verify(self):
        r = self._sign()
        self.assertEqual(r.returncode, 0, _dec(r.stdout) + _dec(r.stderr))
        self.assertTrue(Path(str(self.art) + ".sig").is_file())
        r2 = self._verify()
        self.assertEqual(r2.returncode, 0, _dec(r2.stdout))
        self.assertIn("签名有效", _dec(r2.stdout))

    def test_tampered_rejected(self):
        self.assertEqual(self._sign().returncode, 0)
        self.art.write_bytes(self.art.read_bytes() + b"tampered")
        r = self._verify()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("验签失败", _dec(r.stdout))

    def test_missing_signature_rejected(self):
        r = self._verify()   # 没签过名
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("缺少文件或签名", _dec(r.stdout))

    def test_wrong_pubkey_rejected(self):
        self.assertEqual(self._sign().returncode, 0)
        other = self.tmp / "other"
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(other)],
                       capture_output=True, check=True)
        r = self._verify(pubkey=Path(str(other) + ".pub"))
        self.assertNotEqual(r.returncode, 0)

    def test_multiple_files(self):
        second = self.tmp / "pack.exe.sha256"
        second.write_text("deadbeef  pack.exe\n", encoding="ascii")
        self.assertEqual(self._sign().returncode, 0)
        # 第二个文件还没签 → 失败
        r = self._verify(files=[self.art, second])
        self.assertNotEqual(r.returncode, 0)
        # 补签后通过
        env = dict(os.environ)
        env["RELEASE_SIGNING_KEY"] = base64.b64encode((self.tmp / "k").read_bytes()).decode("ascii")
        subprocess.run([sys.executable, str(SCRIPT), "sign", str(second)],
                       capture_output=True, env=env)
        r2 = self._verify(files=[self.art, second])
        self.assertEqual(r2.returncode, 0, _dec(r2.stdout))


class WorkflowWiringCase(unittest.TestCase):
    """接线：workflow 必须（1）跑测试（2）构建（3）签名（4）上传签名产物。"""

    def _wf(self) -> str:
        return (Path(__file__).resolve().parent.parent /
                ".github/workflows/desktop-release.yml").read_text(encoding="utf-8")

    def _command_index(self, wf: str, needle: str) -> int:
        """定位**命令行**出现位置，跳过 YAML 注释里的同名提及。

        否则注释先提到脚本名就会让顺序断言假失败（实测踩过）。
        """
        for i, line in enumerate(wf.splitlines()):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if needle in line:
                # 换算回字符偏移
                return wf.index(line)
        raise AssertionError(f"workflow 里找不到命令行：{needle}")

    def test_workflow_steps_order(self):
        wf = self._wf()
        i_test = self._command_index(wf, "unittest")
        i_build = self._command_index(wf, "build_desktop.py")
        i_sign = self._command_index(wf, "sign_release.py sign")
        self.assertLess(i_test, i_build, "必须先跑测试再打包")
        self.assertLess(i_build, i_sign, "签名必须在构建之后")
        # 私钥来自 Secrets（不硬编码）
        self.assertIn("secrets.RELEASE_SIGNING_KEY", wf)
        # 发布前自检（签了但不对的情况要拦住）
        self.assertIn("sign_release.py verify", wf)
        # 公钥（可公开）入库，供 update.py 验签
        self.assertTrue((Path(__file__).resolve().parent.parent / "release-signing.pub").is_file(),
                        "仓库必须内置验签公钥")

    def test_private_key_never_tracked(self):
        """私钥目录必须被 gitignore 排除（发布链根信根，泄露=可伪造任意发布）。"""
        gi = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("_relkey/", gi)

    def test_gitignore_keeps_signing_script(self):
        gi = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("!scripts/sign_release.py", gi)


if __name__ == "__main__":
    unittest.main()
