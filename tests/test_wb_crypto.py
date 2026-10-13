"""wb_crypto 单元测试：AAD 布局钉死 + seal/open 互操作往返 + 派生链。

不依赖真实密钥/真实凭证文件——全部用合成密钥与已知向量。
"""
import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admin import wb_crypto

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception:  # pragma: no cover
    AESGCM = None

SECRET = "Sik9U5aXhCdwTVEwsEySDOmDoB9r9ntFxHF1fst9LQI="  # 已在会话中暴露的本机密钥
PROTECTOR_KEY_ID = "9127dea1b44020a7"


class TestAadLayout(unittest.TestCase):
    def test_astring(self):
        self.assertEqual(wb_crypto.astring(""), b"\x00\x00\x00\x00")
        self.assertEqual(wb_crypto.astring("ab"), b"\x00\x00\x00\x02ab")

    def test_u32be(self):
        self.assertEqual(wb_crypto.u32be(1), b"\x00\x00\x00\x01")
        self.assertEqual(wb_crypto.u32be(0x01020304), b"\x01\x02\x03\x04")

    def test_sym_v1_field_aad_exact_bytes(self):
        aad = wb_crypto.sym_v1_field_aad("0123456789abcdef")
        expected = (
            b"WB-AAD\x00"
            + b"\x01"
            + b"\x00\x00\x00\x05WBEV1"
            + b"\x00\x00\x00\x06sym-v1"
            + b"\x00\x00\x00\x01"
            + b"\x00\x00\x00\x10" + b"0123456789abcdef"
            + b"\x02\x00\x00"
        )
        self.assertEqual(aad, expected)

    def test_sym_v1_field_aad_rejects_bad_keyid(self):
        with self.assertRaises(ValueError):
            wb_crypto.sym_v1_field_aad("short")
        with self.assertRaises(ValueError):
            wb_crypto.sym_v1_field_aad("Z1234567890abcde")

    def test_known_protector_keyid(self):
        _, key_id = wb_crypto.derive_protector_key(SECRET)
        self.assertEqual(key_id, PROTECTOR_KEY_ID)


@unittest.skipIf(AESGCM is None, "cryptography 未安装")
class TestEnvelopeRoundTrip(unittest.TestCase):
    """自封自解往返：模拟客户端 seal 再用我们的 open_envelope 解。"""

    def _seal(self, plaintext: bytes, key: bytes, key_id: str) -> dict:
        import secrets

        nonce = secrets.token_bytes(12)
        aad = wb_crypto.sym_v1_field_aad(key_id)
        ct = AESGCM(key).encrypt(nonce, plaintext, aad)
        ciphertext, tag = ct[:-16], ct[-16:]
        return {
            "suite": 1,
            "keyId": key_id,
            "nonce": base64.b64encode(nonce).decode(),
            "authTag": base64.b64encode(tag).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        }

    def test_round_trip_raw_string(self):
        key, key_id = wb_crypto.derive_protector_key(SECRET)
        token = "eyJhbGciOiJSUzI1NiIsInR5cCI6OiJKV1QifQ.test.sig"
        env = self._seal(token.encode(), key, key_id)
        env_b64 = base64.b64encode(json.dumps(env).encode()).decode()
        self.assertEqual(wb_crypto.open_envelope(env_b64, key), token)

    def test_round_trip_wrong_key_fails(self):
        key, key_id = wb_crypto.derive_protector_key(SECRET)
        env = self._seal(b"secret", key, key_id)
        env_b64 = base64.b64encode(json.dumps(env).encode()).decode()
        other_key, _ = wb_crypto.derive_protector_key("A" * 43 + "=")
        with self.assertRaises(Exception):
            wb_crypto.open_envelope(env_b64, other_key)

    def test_round_trip_wrong_aad_fails(self):
        # framing='file' 的 AAD 应解不开 field 信封
        key, key_id = wb_crypto.derive_protector_key(SECRET)
        env = self._seal(b"secret", key, key_id)
        file_aad = (
            b"WB-AAD\x00\x01"
            + wb_crypto.astring("WBEF1")
            + wb_crypto.astring("sym-v1")
            + wb_crypto.u32be(1)
            + wb_crypto.astring(key_id)
            + b"\x01\x00\x00"
        )
        raw = json.loads(base64.b64decode(
            base64.b64encode(json.dumps(env).encode()).decode()))
        with self.assertRaises(Exception):
            AESGCM(key).decrypt(
                base64.b64decode(raw["nonce"]),
                base64.b64decode(raw["ciphertext"]) + base64.b64decode(raw["authTag"]),
                file_aad,
            )


class TestDecryptValue(unittest.TestCase):
    def test_is_encrypted(self):
        self.assertTrue(wb_crypto.is_encrypted(
            {"$wbEncrypted": 1, "envelope": "e30="}))
        self.assertFalse(wb_crypto.is_encrypted({"envelope": "e30="}))
        self.assertFalse(wb_crypto.is_encrypted("plain"))
        # asym-v1 高级模式是三键结构，同样被识别为加密态（防止误透传）
        self.assertTrue(wb_crypto.is_encrypted(
            {"$wbEncrypted": 1, "scheme": "asym-v1", "envelope": "e30="}))

    @unittest.skipIf(AESGCM is None, "cryptography 未安装")
    def test_decrypt_nested(self):
        import secrets

        key, key_id = wb_crypto.derive_protector_key(SECRET)
        aad = wb_crypto.sym_v1_field_aad(key_id)
        nonce = secrets.token_bytes(12)
        ct = AESGCM(key).encrypt(nonce, b"tok", aad)
        env = base64.b64encode(json.dumps({
            "suite": 1, "keyId": key_id,
            "nonce": base64.b64encode(nonce).decode(),
            "authTag": base64.b64encode(ct[-16:]).decode(),
            "ciphertext": base64.b64encode(ct[:-16]).decode(),
        }).encode()).decode()
        payload = {
            "auth": {
                "accessToken": {"$wbEncrypted": 1, "envelope": env},
                "domain": "www.workbuddy.ai",
                "keep": 1,
            },
            "list": [{"$wbEncrypted": 1, "envelope": env}],
        }
        out = wb_crypto.decrypt_info_payload(payload, key)
        self.assertEqual(out["auth"]["accessToken"], "tok")
        self.assertEqual(out["auth"]["domain"], "www.workbuddy.ai")
        self.assertEqual(out["auth"]["keep"], 1)
        self.assertEqual(out["list"][0], "tok")


class TestLoadSecretKey(unittest.TestCase):
    def test_load_from_string_payload(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "k.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "atRestSecretKey": SECRET}, f)
            self.assertEqual(wb_crypto.load_secret_key(p), SECRET)
            # loggerGet 的原始返回是 JSON 字符串（双层）
            p2 = os.path.join(d, "k2.json")
            with open(p2, "w", encoding="utf-8") as f:
                json.dump(json.dumps({"version": 1, "atRestSecretKey": SECRET}), f)
            self.assertEqual(wb_crypto.load_secret_key(p2), SECRET)

    def test_load_missing_raises(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "k.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"version": 1}, f)
            with self.assertRaises(ValueError):
                wb_crypto.load_secret_key(p)


if __name__ == "__main__":
    unittest.main()
