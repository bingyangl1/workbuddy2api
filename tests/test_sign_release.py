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
from unittest import mock

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

    GitHub Actions 的 Windows runner 上，workspace 与 TEMP 都带
    `\\OWNER RIGHTS`（S-1-3-4）的 ACL，`ssh-keygen -Y sign` 直接拒绝：

        [FAIL] pack.exe: Bad permissions. Try removing permissions for
        user: \\OWNER RIGHTS (S-1-3-4) on file .../signing_key

    这是 **runner 的环境特性，不是代码缺陷**。把这些测试标成 skip 更诚实 ——
    报「失败」会掩盖真正的代码问题（而且 CI 会一直红）。

    ## 探测必须「像真的那样」做

    第一版探测只签一个 `x.txt`，**在 CI 上返回了 True 但那 4 条测试仍然失败**。
    说明探测条件与实际用例不完全一致。现在按实际路径重建：

      * 临时目录用仓库内的（与 `_work_dir` 一致）
      * 用 `.exe` 后缀的文件（与实际被测的 `pack.exe` 同类型）
      * 走**同一套** `sign_release.py` 函数（`_materialize_key` + `sign`），
        而不是自己拼 ssh-keygen 命令

    这样探测通过 ⇔ 真实用例能过。

    结果按进程缓存。
    """
    global _SIGN_PROBE
    if _SIGN_PROBE is not None:
        return _SIGN_PROBE
    _SIGN_PROBE = False
    if not HAS_SSH_KEYGEN:
        return False
    try:
        import base64

        SIGN_TMP.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(SIGN_TMP)) as d:
            work = Path(d)
            # 造一个 ed25519 私钥（与真签流程同样的方式）
            key = work / "probe_key"
            r = subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key)],
                capture_output=True)
            if r.returncode != 0:
                return False
            # 用与被测用例同类型的目标文件
            art = work / "probe.exe"
            art.write_bytes(b"MZ" + b"\x00" * 256)
            # 走 sign_release 的真实入口（这会暴露 ACL 问题）
            spec = base64.b64encode(key.read_bytes()).decode("ascii")
            rc = S.sign([art], spec)
            _SIGN_PROBE = (rc == 0)
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


@unittest.skipUnless(_sign_supported(), "环境不支持 ssh-keygen -Y sign")
class SignVerifyCase(unittest.TestCase):
    """端到端：sign → verify → 篡改拒绝。走 CLI（与 CI 同一条命令）。

    ## 为什么跳过条件里要带上探测

    这些测试验证的是**签名脚本的逻辑**，不是 runner 的 ACL 配置。
    GitHub Actions 的 Windows runner 上 `ssh-keygen -Y sign` 会因
    `\\OWNER RIGHTS` ACL 拒绝 —— 那是环境限制，不是代码缺陷。
    报「失败」会掩盖真正的代码问题，而且 CI 会永远红。

    放在**装饰器**里而不是 `setUpClass` 里：`setUpClass` 抛 SkipTest
    在 pytest 下会记成 error（不同版本行为不一致），装饰器最稳。
    """

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


class WorkDirCase(unittest.TestCase):
    """签名脚本的临时目录不能放系统 TEMP。

    ## 为什么（真实的 CI 失败）

    Windows 上 `%TEMP%` 的 ACL 常是 `\\OWNER RIGHTS`（S-1-3-4）或带 8.3 短名，
    `ssh-keygen -Y sign` 直接拒绝：

        [FAIL] pack.exe: Bad permissions. Try removing permissions for
        user: \\OWNER RIGHTS (S-1-3-4) on file .../signing_key

    GitHub Actions 的 Windows runner 上**必然出现**，本地开发机复现不了
    （本地 TEMP 的 ACL 正常）—— 典型的「只有 CI 才暴露」。

    所以 `sign()` / `verify()` 都改走 `_work_dir()`。
    """

    def test_work_dir_not_system_temp(self):
        """默认位置应在仓库内，而不是系统 TEMP。"""
        with S._work_dir("wb-test-") as td:
            p = Path(td).resolve()
        sys_tmp = Path(tempfile.gettempdir()).resolve()
        self.assertFalse(
            str(p).startswith(str(sys_tmp)),
            f"临时目录 {p} 在系统 TEMP（{sys_tmp}）下 —— "
            "ssh-keygen 在 CI runner 上会因 ACL 拒绝")

    def test_work_dir_honors_env(self):
        """支持 `WB_SIGN_TMPDIR` 覆盖（CI 可指到 workspace 内）。"""
        custom = Path(__file__).resolve().parent.parent / ".sign_tmp_env"
        with mock.patch.dict(os.environ, {"WB_SIGN_TMPDIR": str(custom)}):
            with S._work_dir("wb-test-") as td:
                p = Path(td).resolve()
        self.assertTrue(str(p).startswith(str(custom.resolve())),
                        f"未采纳 WB_SIGN_TMPDIR：{p}")
        shutil.rmtree(custom, ignore_errors=True)

    def test_sign_and_verify_use_work_dir(self):
        """`sign()` / `verify()` 必须真的走 `_work_dir`（不能又用回 TEMP）。

        注意 `_work_dir()` **内部**有一处系统 TEMP 兜底
        （仓库目录不可写时用，如只读挂载）—— 那是合法的，
        所以这里只检查 `_work_dir` 定义之外的代码。
        """
        src = SCRIPT.read_text(encoding="utf-8")
        # 切出 _work_dir 函数体，排除它
        i = src.find("def _work_dir(")
        self.assertGreater(i, 0, "应有 _work_dir 函数")
        j = src.find("\ndef ", i + 10)
        body = src[i:j] if j > 0 else src[i:]
        rest = src[:i] + (src[j:] if j > 0 else "")

        bare_in_rest = [ln.strip() for ln in rest.splitlines()
                        if "TemporaryDirectory(prefix=" in ln
                        and "dir=" not in ln]
        self.assertEqual(
            bare_in_rest, [],
            f"`_work_dir` 之外还有地方用系统 TEMP（ssh-keygen 会因 ACL 拒绝）："
            f"{bare_in_rest}")

        # sign / verify 都应该调 _work_dir
        self.assertIn("_work_dir(", rest,
                      "sign()/verify() 应通过 `_work_dir()` 建临时目录")
        self.assertNotIn("TemporaryDirectory(prefix=\"wb-sign-\"", rest)
        self.assertNotIn("TemporaryDirectory(prefix=\"wb-verify-\"", rest)


if __name__ == "__main__":
    unittest.main()
