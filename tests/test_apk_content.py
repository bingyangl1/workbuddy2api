"""验证打出来的 APK 里**真的有可用代码**（不是「CI 绿了就行」）。

## 这条测试的由来

APK 构建在 CI 上显示 success、产物 22.9 MB、发布到了 Release。
我用 zip 列出条目一看：只有 35 个条目，`*.py` 只有 2 个 ——
立刻怀疑「代码没打包进去」。

**结果是我的检查方法错了。** Chaquopy 并不把 `.py` 原样放进 APK，
而是编译成 `.pyc` 再打包进 `assets/chaquopy/app.imy`
（`.imy` 就是 zip，里面 104 个条目）。

但这个「虚惊」揭示了一个真实风险：**构建 success ≠ 产物可用**。
CI 只知道 Gradle 退出码是 0，不知道 APK 里到底装了什么。
所以需要一层「解剖产物」的验证。

## 本测试做什么

1. 如果本地有 APK（`_apkcheck/`），直接解剖验证
2. 没有就跳过（CI 上 APK 在 Artifacts 里，另有 workflow 步骤验证）

## 关键判据

* `app.imy` 里必须有 `converter.pyc`（README 特别强调必须打包）
* 必须有 `admin/server.pyc` 及其 import 的每个路由模块
* 必须有 `admin/data/upstream_error_messages.json`（147 条错误码数据）
* **不能有**凭证 / 数据库 / 密钥类文件
"""
import io
import os
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
APK_DIRS = [ROOT / "_apkcheck", ROOT / "dist" / "android"]


def _find_apk() -> Path | None:
    for d in APK_DIRS:
        if d.is_dir():
            hits = sorted(d.glob("*.apk"))
            if hits:
                return hits[0]
    return None


def _inner_names(apk: Path, entry: str) -> list[str]:
    """取出 chaquopy `.imy` 里的条目名（`.imy` 本身就是 zip）。"""
    with zipfile.ZipFile(apk) as z:
        if entry not in z.namelist():
            return []
        data = z.read(entry)
    idx = data.find(b"PK\x03\x04")
    if idx < 0:
        return []
    with zipfile.ZipFile(io.BytesIO(data[idx:])) as inner:
        return inner.namelist()


class ApkContentCase(unittest.TestCase):
    """APK 里必须真的有后端代码。"""

    @classmethod
    def setUpClass(cls):
        cls.apk = _find_apk()
        if cls.apk is None:
            raise unittest.SkipTest(
                "本地没有 APK（CI 上从 Artifacts 取），跳过产物解剖")
        cls.names = _inner_names(cls.apk, "assets/chaquopy/app.imy")

    def test_app_imy_exists(self):
        """`app.imy` 必须存在且非空 —— 它就是我们的源码包。"""
        self.assertTrue(
            self.names,
            "**APK 里没有 `assets/chaquopy/app.imy`** —— "
            "说明打进去的是空壳，装上去会起不来")

    def test_has_expected_module_count(self):
        """模块数量要在合理范围（防止「只打进去几个文件」）。"""
        self.assertGreater(
            len(self.names), 50,
            f"app.imy 只有 {len(self.names)} 个条目，明显偏少 —— "
            "多半是打包时漏了 admin 源码")

    def test_converter_packed(self):
        """`converter.py` 必须打包（README 特别强调过）。"""
        joined = "\n".join(self.names)
        self.assertIn("converter", joined,
                      "**converter.py 没打包** —— 网关核心逻辑会缺失")

    def test_server_and_routers_packed(self):
        """`admin/server.py` 及关键路由模块必须都在。"""
        joined = "\n".join(self.names)
        for key in ("admin/server", "app_source", "client_profile",
                    "benefits", "growth", "upstream_errors"):
            self.assertIn(key, joined,
                          f"**{key} 没打包** —— APK 启动会 ImportError"
                          "（server.py 无条件 import 它）")

    def test_error_code_data_packed(self):
        """147 条错误码数据文件必须打包（否则错误提示退化成空）。"""
        joined = "\n".join(self.names)
        self.assertIn("upstream_error_messages", joined,
                      "错误码数据 JSON 没打包 —— 上游错误将没有中文解释")

    def test_no_secrets_in_apk(self):
        """**绝不能**把凭证/数据库/密钥打进 APK。"""
        with zipfile.ZipFile(self.apk) as z:
            all_names = z.namelist()
        joined = "\n".join(all_names + self.names)
        forbidden = {
            "workbuddy.db": "本地数据库",
            "_atrest_secret": "at-rest 密钥",
            "signing.json": "签名配置",
            "release.jks": "签名密钥库",
        }
        leaked = [f"{desc}({key})" for key, desc in forbidden.items()
                  if key in joined]
        # `.env` 单独判断（避免误匹配到 `.env.example`）
        if any(n.endswith("/.env") or n == ".env" for n in all_names):
            leaked.append("环境变量文件(.env)")
        self.assertEqual(leaked, [],
                         f"**APK 里含敏感文件**：{leaked}")

    def test_arm64_python_runtime(self):
        """必须有 ARM64 的 Python 运行时（否则装上也跑不了）。"""
        with zipfile.ZipFile(self.apk) as z:
            all_names = z.namelist()
        so = [n for n in all_names
              if "arm64" in n.lower() and n.endswith(".so")]
        self.assertGreater(len(so), 5,
                           f"ARM64 的 .so 只有 {len(so)} 个，运行时可能不全")


if __name__ == "__main__":
    unittest.main()
