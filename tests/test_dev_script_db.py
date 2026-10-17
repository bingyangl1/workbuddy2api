"""dev.ps1 的数据库默认连法必须是**局域网直连**，不是 SSH 隧道。

## 背景

`dev.ps1` 原来写死 `127.0.0.1:13306`（SSH 隧道），注释声称
「本机没有 MySQL，dev 库在远程，需要 SSH 隧道」。

**那个断言从来没被验证过。** 实测：

    直连 192.168.5.47:3306   →   31 ms   ✅
    隧道 127.0.0.1:13306     →  278 ms   ✅（慢 8 倍）

原因：dev 库跑在 `192.168.5.47` 的 **Docker** 里，`docker-proxy` 默认把
3306 映射到**所有网卡**：

    LISTEN 0 4096  0.0.0.0:3306  0.0.0.0:*  users:(("docker-proxy",pid=199593))

所以同一网段直接能连，隧道纯属多绕一跳。

## 本测试守什么

1. 默认连的是 `192.168.5.47:3306`（直连）
2. 保留 `-Tunnel` 开关（不在同一网段时仍可用）
3. **自检按实际 URL 探测** —— 不能硬编码测某个地址，
   否则改了默认值之后自检会永远报错或永远通过（两种都骗人）
4. 文档同步（不能还写着「必须建隧道」）
"""
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
DEV = ROOT / "dev.ps1"
GUIDE = ROOT / "docs-dev" / "LOCAL-DEV-GUIDE.md"


class DevScriptDbCase(unittest.TestCase):
    """dev.ps1 的默认数据库地址。"""

    @classmethod
    def setUpClass(cls):
        cls.src = DEV.read_text(encoding="utf-8-sig")

    def test_defaults_to_direct(self):
        """默认 URL 必须是 192.168.5.47:3306。"""
        m = re.search(r"ADMIN_DATABASE_URL\s*=\s*'([^']+)'", self.src)
        self.assertIsNotNone(m, "找不到 ADMIN_DATABASE_URL 默认值")
        url = m.group(1)
        self.assertIn("192.168.5.47:3306", url,
                      f"**默认应直连 192.168.5.47:3306**，实际 {url!r}")
        self.assertNotIn("127.0.0.1:13306", url,
                         "默认不该走隧道（除非你确认直连不可用）")

    def test_tunnel_switch_exists(self):
        """`-Tunnel` 开关要保留（不在同一网段时用）。"""
        self.assertIn("[switch]$Tunnel", self.src,
                      "应保留 -Tunnel 开关")
        # 开关要真的改 URL
        m = re.search(r"if\s*\(\$Tunnel\)\s*\{(.*?)\}", self.src, re.S)
        self.assertIsNotNone(m, "找不到 $Tunnel 分支")
        self.assertIn("13306", m.group(1),
                      "-Tunnel 分支应把 URL 改成隧道地址")

    def test_selfcheck_parses_url(self):
        """自检必须**从 URL 解析** host:port，不能硬编码。

        原来硬编码测 `127.0.0.1:13306`；改成直连后那条检查要么
        永远失败（误报），要么被改成 `-not` 而永远通过。两种都在骗人。
        """
        # 找到「3/5」那一段
        i = self.src.find("3/5")
        self.assertGreater(i, 0, "找不到 3/5 自检段")
        seg = self.src[i:i + 1800]
        self.assertRegex(
            seg, r"\$dbHost|ADMIN_DATABASE_URL",
            "**自检应从 ADMIN_DATABASE_URL 解析地址** —— "
            "硬编码某个 host:port 会在改了默认值之后失效")
        self.assertRegex(
            seg, r"Test-NetConnection[^\n]*\$dbHost",
            "自检应测解析出来的 host")

    def test_mentions_tunnel_alternative_in_help(self):
        """帮助里要说明什么时候用 -Tunnel。"""
        self.assertIn("-Tunnel", self.src)
        self.assertRegex(
            self.src, r"(出差|VPN|网段)",
            "帮助里应说明「不在同一网段时用 -Tunnel」")


class LocalDevGuideCase(unittest.TestCase):
    """本地开发文档不能还写着「必须建隧道」。"""

    @classmethod
    def setUpClass(cls):
        cls.src = GUIDE.read_text(encoding="utf-8")

    def test_tldr_does_not_require_tunnel(self):
        """TL;DR 不应要求先建隧道。"""
        i = self.src.find("## 0.")
        j = self.src.find("## 1.")
        self.assertGreater(i, 0)
        self.assertGreater(j, i)
        tldr = self.src[i:j]
        self.assertNotIn(
            "ssh -N -L 13306",
            tldr,
            "**TL;DR 里不该再要求先建 SSH 隧道** —— 默认是直连")
        self.assertIn("dev.ps1", tldr,
                      "TL;DR 应给出 `dev.ps1` 这条一键命令")

    def test_mentions_direct_is_default(self):
        self.assertRegex(
            self.src, r"直连",
            "文档应说明默认是直连")
        self.assertIn("192.168.5.47:3306", self.src,
                      "文档应给出直连地址")


if __name__ == "__main__":
    unittest.main()
