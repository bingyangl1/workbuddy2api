"""import-local 对新版加密 .info 的解密导入路径测试。

模拟新版客户端的加密凭证（合成密钥，不碰真实数据）：
- 有 WORKBUDDY_ATREST_SECRET 时自动解密并入库；
- 无密钥时拒收并给出明确原因。
"""
import base64
import json
import os
import secrets
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admin import wb_crypto

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception:  # pragma: no cover
    AESGCM = None

SECRET = "Sik9U5aXhCdwTVEwsEySDOmDoB9r9ntFxHF1fst9LQI="


def _make_encrypted_info(plain_access_token: str) -> dict:
    key, key_id = wb_crypto.derive_protector_key(SECRET)
    aad = wb_crypto.sym_v1_field_aad(key_id)
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, plain_access_token.encode(), aad)
    env = base64.b64encode(json.dumps({
        "suite": 1, "keyId": key_id,
        "nonce": base64.b64encode(nonce).decode(),
        "authTag": base64.b64encode(ct[-16:]).decode(),
        "ciphertext": base64.b64encode(ct[:-16]).decode(),
    }).encode()).decode()
    return {
        "account": {"uid": "uid-x", "nickname": {"$wbEncrypted": 1, "envelope": env}},
        "auth": {
            "accessToken": {"$wbEncrypted": 1, "envelope": env},
            "refreshToken": {"$wbEncrypted": 1, "envelope": env},
            "domain": "www.workbuddy.ai",
            "expiresAt": 4102444800000,
        },
    }


@unittest.skipIf(AESGCM is None, "cryptography 未安装")
class TestImportLocalDecrypt(unittest.TestCase):
    def _router_mod(self):
        from admin.routers import accounts
        return accounts

    def test_decrypt_info_if_needed_with_secret(self):
        mod = self._router_mod()
        tok = "aaa." + "b" * 20 + "." + "c" * 10
        encrypted = _make_encrypted_info(tok)
        # 必须同时屏蔽「运行时现取」：本机装了客户端时那条路会真取到密钥，
        # 而那把密钥解不开本测试用 SECRET 造的假密文（测试之间要隔离）。
        with mock.patch.dict(os.environ, {"WORKBUDDY_ATREST_SECRET": SECRET}), \
             mock.patch.object(wb_crypto, "fetch_at_rest_secret_key",
                               return_value=None):
            out = mod._decrypt_info_if_needed(encrypted)
        self.assertEqual(out["auth"]["accessToken"], tok)
        self.assertEqual(out["auth"]["refreshToken"], tok)
        self.assertEqual(out["auth"]["domain"], "www.workbuddy.ai")
        self.assertEqual(out["account"]["nickname"], tok)

    def test_decrypt_info_no_secret_passthrough(self):
        mod = self._router_mod()
        tok = "aaa." + "b" * 20 + "." + "c" * 10
        encrypted = _make_encrypted_info(tok)
        env = dict(  # 清掉可能存在的 env
            (k, v) for k, v in os.environ.items()
            if k != "WORKBUDDY_ATREST_SECRET")
        with mock.patch.dict(env, {}, clear=True):
            # 还要挡住密钥文件路径
            with mock.patch.object(mod, "_atrest_secret", return_value=None):
                out = mod._decrypt_info_if_needed(encrypted)
        # 无密钥：原样返回（保持加密态），由 validate_credential 拒收
        self.assertIn("$wbEncrypted", json.dumps(out))

    def test_plaintext_info_passthrough(self):
        mod = self._router_mod()
        plain = {"auth": {"accessToken": "x.y.z", "domain": "www.codebuddy.ai"}}
        out = mod._decrypt_info_if_needed(plain)
        self.assertEqual(out, plain)

    def test_corrupted_envelope_passthrough(self):
        """密钥存在但信封损坏：原样返回不抛异常。"""
        mod = self._router_mod()
        bad = {"auth": {"accessToken": {
            "$wbEncrypted": 1, "envelope": "not-base64!!"}}}
        with mock.patch.dict(os.environ, {"WORKBUDDY_ATREST_SECRET": SECRET}):
            out = mod._decrypt_info_if_needed(bad)
        self.assertEqual(out, bad)


if __name__ == "__main__":
    unittest.main()
