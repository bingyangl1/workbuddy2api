"""P5：一键更新器（scripts/update.py）——验收「断网 / 篡改包 / 缺签名全部拒绝安装」。

验签用**真 ssh-keygen**（若系统有）：现场生成 ed25519 密钥对 → 真签名 → 校验通过；
再篡改包 → 校验失败；删签名 → 拒绝。没有 ssh-keygen 时跳过真签名用例
（其余用例仍跑：拒绝逻辑不依赖 ssh-keygen 存在）。

路径穿越、模式白名单、留底、dry-run 全部覆盖。
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import update as U  # noqa: E402

HAS_SSH_KEYGEN = bool(shutil.which("ssh-keygen"))


def _make_zip(path: Path, entries: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return path


def _make_keys(tmp: Path) -> tuple[Path, Path]:
    """生成 ed25519 密钥对（测试专用）。返回 (priv, pub)。"""
    priv = tmp / "signing"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(priv),
                    "-C", "test"], check=True, capture_output=True)
    return priv, priv.with_suffix(".pub")


def _sign(artifact: Path, priv: Path, tmp: Path) -> Path:
    """用命名空间签名（ssh-keygen -Y sign）。"""
    subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(priv), "-n", U.NAMESPACE,
                    str(artifact)], check=True, capture_output=True, cwd=tmp)
    return Path(str(artifact) + ".sig")


class ExtractCase(unittest.TestCase):
    """解压：路径穿越拒绝 + 模式白名单。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wb-upd-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rejects_path_traversal(self):
        z = _make_zip(self.tmp / "evil.zip", {"../evil.txt": "x", "admin/a.py": "y"})
        with self.assertRaises(ValueError):
            U.extract(z, "all", self.tmp / "s1")

    def test_rejects_absolute_path(self):
        z = _make_zip(self.tmp / "abs.zip", {"/etc/passwd": "x"})
        with self.assertRaises(ValueError):
            U.extract(z, "all", self.tmp / "s2")

    def test_rejects_windows_drive_path(self):
        z = _make_zip(self.tmp / "drive.zip", {"C:/Windows/evil.dll": "x"})
        with self.assertRaises(ValueError):
            U.extract(z, "all", self.tmp / "s2b")

    def test_unix_absolute_detected_on_windows(self):
        """踩过的坑：`Path('/etc/passwd').is_absolute()` 在 Windows 上返回 False。

        若用 is_absolute 判定，Unix 绝对路径会在 Windows 上混过去。
        这里直接钉住字符串判定。
        """
        with self.assertRaises(ValueError) as cm:
            U._safe_members(["/etc/passwd"])
        self.assertIn("绝对路径", str(cm.exception))

    def test_mode_whitelist_panel_only(self):
        z = _make_zip(self.tmp / "pack.zip", {
            "admin/secret.py": "engine-code",
            "web/out/index.html": "<html>",
            "web/out/_next/x.js": "js",
            "README.md": "nope",
        })
        files = U.extract(z, "panel", self.tmp / "s3")
        self.assertEqual(sorted(files), ["web/out/_next/x.js", "web/out/index.html"])

    def test_mode_whitelist_engine_excludes_panel(self):
        z = _make_zip(self.tmp / "pack2.zip", {
            "admin/a.py": "1", "web/out/index.html": "<html>", "main.py": "m"})
        files = U.extract(z, "engine", self.tmp / "s4")
        self.assertEqual(sorted(files), ["admin/a.py", "main.py"])

    def test_non_archive_rejected(self):
        bad = self.tmp / "not.zip"
        bad.write_text("hello")
        with self.assertRaises(ValueError):
            U.extract(bad, "all", self.tmp / "s5")


class UrlCase(unittest.TestCase):
    def test_https_only(self):
        self.assertEqual(U._check_url("https://x/y.zip"), "https://x/y.zip")
        with self.assertRaises(ValueError):
            U._check_url("http://x/y.zip")
        with self.assertRaises(ValueError):
            U._check_url("file:///etc/passwd")


@unittest.skipUnless(HAS_SSH_KEYGEN, "需要系统 ssh-keygen")
class SignatureCase(unittest.TestCase):
    """验收核心：缺签名 / 篡改包 必须拒绝。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wb-sig-test-"))
        self.priv, self.pub = _make_keys(self.tmp)
        self.artifact = _make_zip(self.tmp / "pack.zip",
                                  {"admin/a.py": "print(1)"})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_valid_signature_passes(self):
        sig = _sign(self.artifact, self.priv, self.tmp)
        U.verify_signature(self.artifact, sig, self.pub)   # 不抛 = 通过

    def test_missing_signature_rejected(self):
        with self.assertRaises(ValueError) as cm:
            U.verify_signature(self.artifact, self.tmp / "nope.sig", self.pub)
        self.assertIn("没有签名文件", str(cm.exception))

    def test_missing_pubkey_rejected(self):
        sig = _sign(self.artifact, self.priv, self.tmp)
        with self.assertRaises(ValueError) as cm:
            U.verify_signature(self.artifact, sig, self.tmp / "nopub.pub")
        self.assertIn("缺少维护者公钥", str(cm.exception))

    def test_tampered_package_rejected(self):
        sig = _sign(self.artifact, self.priv, self.tmp)
        # 签名后改动包内容（模拟投毒/MITM）
        self.artifact.write_bytes(self.artifact.read_bytes() + b"\n# tampered")
        with self.assertRaises(ValueError) as cm:
            U.verify_signature(self.artifact, sig, self.pub)
        self.assertIn("签名校验失败", str(cm.exception))

    def test_wrong_pubkey_rejected(self):
        sig = _sign(self.artifact, self.priv, self.tmp)
        other = self.tmp / "other"
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(other)],
                       check=True, capture_output=True)
        with self.assertRaises(ValueError):
            U.verify_signature(self.artifact, sig, other.with_suffix(".pub"))

    def test_wrong_namespace_rejected(self):
        """命名空间不符也拒绝（防签名被挪用到别的用途）。"""
        out = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(self.priv),
                              "-n", "some-other-namespace", str(self.artifact)],
                             capture_output=True, cwd=self.tmp)
        self.assertEqual(out.returncode, 0)
        sig = Path(str(self.artifact) + ".sig")
        with self.assertRaises(ValueError):
            U.verify_signature(self.artifact, sig, self.pub)


class OfflineCase(unittest.TestCase):
    """断网：下载失败必须报错退出（不静默继续）。"""

    def test_download_failure_raises(self):
        tmp = Path(tempfile.mkdtemp(prefix="wb-net-test-"))
        try:
            # 不可达地址（保留 TEST-NET-1），超时/连接错都应抛
            with self.assertRaises(Exception):
                U.download("https://192.0.2.1/nope.zip", tmp / "x.zip")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class InstallCase(unittest.TestCase):
    """覆盖 + 留底 + dry-run。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wb-ins-test-"))
        self.staging = self.tmp / "staging"
        self.root = self.tmp / "root"
        (self.root / "admin").mkdir(parents=True)
        (self.root / "admin" / "a.py").write_text("OLD", encoding="utf-8")
        (self.staging / "admin").mkdir(parents=True)
        (self.staging / "admin" / "a.py").write_text("NEW", encoding="utf-8")
        self._orig_root = U.ROOT
        U.ROOT = self.root

    def tearDown(self):
        U.ROOT = self._orig_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_does_not_overwrite(self):
        backups = U.install(self.staging, ["admin/a.py"], apply=False)
        self.assertEqual((self.root / "admin" / "a.py").read_text(encoding="utf-8"), "OLD")
        self.assertEqual(len(backups), 1)   # 仍算出会留底哪些

    def test_apply_overwrites_and_backs_up(self):
        U.install(self.staging, ["admin/a.py"], apply=True)
        self.assertEqual((self.root / "admin" / "a.py").read_text(encoding="utf-8"), "NEW")
        baks = list((self.root / "admin").glob("a.py.bak-*"))
        self.assertEqual(len(baks), 1)
        self.assertEqual(baks[0].read_text(encoding="utf-8"), "OLD")


class BaselineWiringCase(unittest.TestCase):
    """接线测试：更新流程里**基线重施紧跟解压之后**（manager 教训的同构）。"""

    def test_baseline_called_after_install_before_restart(self):
        src = (Path(__file__).resolve().parent.parent / "scripts" / "update.py").read_text(encoding="utf-8")
        i = src.index("backups = install(staging, files")
        tail = src[i:i + 1200]
        self.assertIn("reapply_baseline(", tail,
                      "覆盖了文件却没重施安全基线——发布包里的出厂 .env 会把服务暴露")
        self.assertLess(tail.index("reapply_baseline("),
                        tail.index("请重启服务"),
                        "顺序反了：基线必须在重启提示之前，否则重启时用的是不安全配置")


if __name__ == "__main__":
    unittest.main()
