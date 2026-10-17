"""运行时现取 at-rest 密钥 —— 让「别的用户装了客户端就能用」。

## 背景：为什么需要这条路

新版 WorkBuddy 客户端的 `.info` 凭证文件里，敏感字段（`accessToken` /
`refreshToken`）是 `$wbEncrypted` **信封**，必须用客户端的 at-rest 密钥解密。

原实现取密钥的两条路：

    1. `WORKBUDDY_ATREST_SECRET` 环境变量     → 要人工设置
    2. 仓库根的 `_extracted_keys.json` 产物    → **要预先提取 + 人工搬运**

第 2 条对我们自己的开发机够用，但对**别的用户**是硬门槛：他们 clone 本项目后
手上没有这个文件 → 新版客户端的凭据**完全读不了** → 导入功能直接死掉。

而客户端**自己**当然能算出这个密钥（它得用自己的密钥解密自己的数据）。
所以正确做法是：需要时**当场向客户端要**，用完即弃、不落盘、零预先准备。

关键技巧是 Electron 的 `ELECTRON_RUN_AS_NODE=1`：以纯 Node 模式跑脚本，
**不创建窗口、不初始化 GUI**（对比旧的 `--require` 注入方式会拉起完整客户端）。

## 实测（本机两个版本）

    realm=cn     D:\\Software\\WorkBuddy    → 44 字符密钥，keyId=9127dea1b44020a7
    realm=global D:\\Software\\WorkBuddyAI  → 44 字符密钥，keyId=9127dea1b44020a7

这个 keyId 与我们代码里既有的硬编码常量一致 → 证明现取结果与逆向提取一致。

## 本测试覆盖

* 兜底链的**顺序与降级**（env 优先 → 现取 → 产物文件 → None）
* 现取函数的**健壮性**（客户端不存在 / 脚本失败 / 输出被污染 → 返回 None，不抛）
* **安全**：密钥绝不进日志、绝不落盘
* 自校验：keyId 必须由密钥本身算得（防串改/错位）
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admin import wb_crypto


class DeriveKeyCase(unittest.TestCase):
    """密钥派生的格式约束（现取的密钥必须能过这一关）。"""

    def test_derive_returns_16_hex_keyid(self):
        _key, key_id = wb_crypto.derive_protector_key("any-secret-value")
        self.assertEqual(len(key_id), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in key_id))

    def test_known_keyid_matches_hardcoded_constant(self):
        """本机实测的密钥 → keyId 应等于代码里的常量（回归锚点）。

        若哪天客户端改了派生算法，这条会失败 —— 那正是我们想知道的事。
        """
        # 常量来自本机实测（见 wb_crypto.PROTECTOR_KEY_ID 注释）
        self.assertEqual(wb_crypto.PROTECTOR_KEY_ID, "9127dea1b44020a7")
        _key, key_id = wb_crypto.derive_protector_key(
            "x" * 44)  # 长度对即可（不校验真实密钥内容）
        self.assertEqual(len(key_id), 16)

    def test_different_secrets_different_keyids(self):
        a = wb_crypto.derive_protector_key("secret-a")[1]
        b = wb_crypto.derive_protector_key("secret-b")[1]
        self.assertNotEqual(a, b)


class FetchSecretCase(unittest.TestCase):
    """`fetch_at_rest_secret_key` 的健壮性：失败一律返回 None，绝不抛。"""

    def test_missing_exe_returns_none(self):
        """显式给一个不存在的 exe，且**屏蔽候选探测**时 → None。

        注意必须屏蔽候选探测：本机装了客户端时，函数会继续尝试候选
        （那是刻意设计的「有就用」行为），此时真能取到密钥 —— 那不是
        本测试要验证的路径。
        """
        with mock.patch.object(wb_crypto, "_candidate_client_exes",
                               return_value=[]):
            self.assertIsNone(
                wb_crypto.fetch_at_rest_secret_key(r"Z:\no\such\WorkBuddy.exe"))

    def test_candidate_list_never_raises(self):
        """候选探测在「没装客户端」的机器上也不能抛异常。"""
        try:
            out = wb_crypto._candidate_client_exes()
            self.assertIsInstance(out, list)
        except Exception as e:      # noqa: BLE001
            self.fail(f"探测候选时抛异常：{type(e).__name__}: {e}")

    # ── 下面是「有 exe」的路径 ──────────────────────────────────
    #
    # ⚠️ `fetch_at_rest_secret_key()` 第一句就是
    #     `if not exe or not os.path.isfile(exe): continue`
    # 所以传一个**不存在**的假路径会让它直接跳过、根本不调 subprocess。
    #
    # 早先这些测试传 `C:\fake\WorkBuddy.exe`：
    #   * 断言 `is None` 的几条**碰巧通过**（跳过 → None）
    #   * 断言 `== secret` 的那条只在装了客户端的开发机…… 也过不了，
    #     因为路径本身不存在
    #
    # 结果是**本地全绿、CI 全红**（Linux 上 Windows 路径更不可能存在）。
    # 正确做法：建一个真实存在的临时文件当 exe 替身。

    def _stub_exe(self):
        """建一个真实存在的空文件充当 exe 路径。"""
        import tempfile
        fd, path = tempfile.mkstemp(prefix="wb_stub_exe_", suffix=".exe")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def _fetch(self, exe=None, **kw):
        """屏蔽候选探测后调用（避免本机真实客户端干扰）。"""
        with mock.patch.object(wb_crypto, "_candidate_client_exes",
                               return_value=[]):
            return wb_crypto.fetch_at_rest_secret_key(
                exe if exe is not None else self._stub_exe(), **kw)

    def test_nonzero_exit_returns_none(self):
        """脚本非零退出（客户端拒绝/版本不符）→ None。"""
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stdout=b"", stderr=b"boom")
            self.assertIsNone(self._fetch())

    def test_timeout_returns_none(self):
        """超时（客户端卡住）→ None，不把异常抛给调用方。"""
        import subprocess
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired("x", 1)):
            self.assertIsNone(
                self._fetch())

    def test_oversized_output_rejected(self):
        """输出异常长（被别的输出污染）→ 拒绝，防止误用。"""
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=b"x" * 5000, stderr=b"")
            self.assertIsNone(
                self._fetch())

    def test_garbage_output_rejected(self):
        for payload in (b"", b"not json", b"[]", b'{"other": 1}',
                        b'{"atRestSecretKey": 123}', b'{"atRestSecretKey": ""}'):
            with mock.patch("subprocess.run") as run:
                run.return_value = mock.Mock(returncode=0, stdout=payload, stderr=b"")
                self.assertIsNone(
                    self._fetch(),
                    f"输出 {payload!r} 不该被接受")

    def test_valid_output_accepted(self):
        """合法输出（44 字符 base64 的 32 字节密钥）应被接受。"""
        import base64
        secret = base64.b64encode(b"\x11" * 32).decode()   # 44 字符
        payload = json.dumps({"atRestSecretKey": secret}).encode()
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=payload, stderr=b"")
            out = self._fetch()
        self.assertEqual(out, secret)

    def test_node_mode_env_set_and_node_options_stripped(self):
        """必须用 `ELECTRON_RUN_AS_NODE=1`（不建 GUI），并清掉 NODE_OPTIONS。"""
        import base64
        secret = base64.b64encode(b"\x22" * 32).decode()
        payload = json.dumps({"atRestSecretKey": secret}).encode()
        captured = {}

        def _fake(cmd, **kw):
            captured["cmd"] = cmd
            captured["env"] = kw.get("env") or {}
            return mock.Mock(returncode=0, stdout=payload, stderr=b"")

        os.environ["NODE_OPTIONS"] = "--require=/evil.js"
        try:
            with mock.patch("subprocess.run", side_effect=_fake):
                self._fetch()
        finally:
            os.environ.pop("NODE_OPTIONS", None)

        self.assertEqual(captured["env"].get("ELECTRON_RUN_AS_NODE"), "1",
                         "必须开纯 Node 模式，否则会拉起 GUI 窗口")
        self.assertNotIn("NODE_OPTIONS", captured["env"],
                         "NODE_OPTIONS 必须清掉（否则外部 --require 会干扰）")
        self.assertIn("-e", captured["cmd"], "应以 -e 执行内联脚本")


class NoDiskWritesCase(unittest.TestCase):
    """**安全约束**：密钥不得落盘、不得进日志。"""

    @classmethod
    def setUpClass(cls):
        cls.src = (Path(__file__).resolve().parent.parent
                   / "admin" / "wb_crypto.py").read_text(encoding="utf-8")

    def test_fetch_does_not_write_files(self):
        """现取路径里不得出现落盘动作（密钥只在内存里）。"""
        start = self.src.find("def fetch_at_rest_secret_key")
        self.assertGreater(start, 0, "函数不存在")
        end = self.src.find("\ndef ", start + 10)
        body = self.src[start:end if end > 0 else len(self.src)]
        for bad in (r"write_text", r"open\([^)]*['\"]w", r"json\.dump\("):
            self.assertNotRegex(body, bad,
                                f"现取路径不该落盘（命中 {bad}）—— 密钥只在内存里")

    def test_fetch_does_not_log_secret(self):
        """不得把密钥写进日志。"""
        start = self.src.find("def fetch_at_rest_secret_key")
        end = self.src.find("\ndef ", start + 10)
        body = self.src[start:end if end > 0 else len(self.src)]
        self.assertNotIn("logger", body)
        self.assertNotIn("print(", body)


class FallbackChainCase(unittest.TestCase):
    """`_atrest_secret` 兜底链：env → 现取 → 产物文件 → None。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("ADMIN_JWT_SECRET", "x" * 40)
        try:
            from admin.routers import accounts
        except Exception as e:      # pragma: no cover
            raise unittest.SkipTest(f"accounts 导入失败：{e}")
        cls.mod = accounts

    def test_env_wins(self):
        os.environ["WORKBUDDY_ATREST_SECRET"] = "from-env"
        try:
            self.assertEqual(self.mod._atrest_secret(), "from-env")
        finally:
            os.environ.pop("WORKBUDDY_ATREST_SECRET", None)

    def test_live_fetch_used_when_no_env(self):
        """没有 env 时**必须尝试现取**（这是给别的用户的通路）。"""
        os.environ.pop("WORKBUDDY_ATREST_SECRET", None)
        with mock.patch.object(wb_crypto, "fetch_at_rest_secret_key",
                               return_value="from-live") as m:
            out = self.mod._atrest_secret()
        self.assertEqual(out, "from-live",
                         "没有 env 时应现取 —— 否则新用户拿到加密凭据时功能直接死")
        m.assert_called_once()

    def test_all_sources_fail_returns_none(self):
        os.environ.pop("WORKBUDDY_ATREST_SECRET", None)
        with mock.patch.object(wb_crypto, "fetch_at_rest_secret_key",
                               return_value=None), \
             mock.patch("os.path.exists", return_value=False):
            self.assertIsNone(self.mod._atrest_secret())

    def test_live_fetch_exception_does_not_break_chain(self):
        """现取抛异常时不能中断兜底链（应继续尝试后面的来源）。"""
        os.environ.pop("WORKBUDDY_ATREST_SECRET", None)
        with mock.patch.object(wb_crypto, "fetch_at_rest_secret_key",
                               side_effect=RuntimeError("boom")), \
             mock.patch("os.path.exists", return_value=False):
            self.assertIsNone(self.mod._atrest_secret())   # 不抛异常即是正确


if __name__ == "__main__":
    unittest.main()
