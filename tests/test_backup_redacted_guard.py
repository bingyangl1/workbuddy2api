"""备份恢复必须拒绝「脱敏快照」——真实事故的回归测试。

## 事故经过（2026-10-16 实测，本测试的由来）

1. 导出了一份**脱敏**快照（`include_secrets=False`，auth_json → `"[redacted]"`）
2. 又把它恢复进了生产库
3. 结果：9 个国内版账号的 `auth_json` 变成 10 字符的 `"[redacted]"`

**症状极隐蔽**：服务照常启动、面板照常打开、账号列表照常有 10 行
（`status=active`、余额字段还在），**只有所有上游调用开始失败**：

    POST /api/models/sync      → 502  "获取模型列表失败: Expecting value: line 1 column 2"
    POST /api/accounts/N/refresh → 502

排查过程也绕了一圈：`_enterprise_path_key()` → `_session()` → `CredentialManager`
逐层看下去，最后才发现根因是**数据**而不是代码（`json.loads("[redacted]")`
必然抛 `Expecting value: line 1 column 2 (char 1)` —— `[` 后面跟 `r` 不是合法 JSON）。

## 为什么原来的实现不够

`restore_snapshot` 只校验了 `format` 与 `tables` 结构，**没有校验内容是否已脱敏**。
而 `export_snapshot` 明明写了 `include_secrets` 字段，恢复侧却从不读它。

## 本测试钉住的三道防线

1. 快照自报 `include_secrets=False` → 拒绝
2. 值里出现占位符字面量 → 拒绝（防「自报 True 但实际被脱敏」）
3. `accounts.auth_json` 不是合法 JSON / 缺 `auth` 段 → 拒绝
"""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def _db():
    from admin.db import Base
    from admin import models as _m  # noqa: F401
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    # 只建用到的两张表，避免无关表结构干扰
    Base.metadata.create_all(bind=engine, tables=[
        Base.metadata.tables["accounts"],
        Base.metadata.tables["system_settings"],
    ])
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


GOOD_AUTH = json.dumps({
    "auth": {"accessToken": "tok-abc", "refreshToken": "ref-def",
             "domain": "www.workbuddy.cn", "tokenType": "Bearer"},
    "account": {"type": "personal"},
})


def _snapshot(accounts, *, include_secrets=True, extra=None):
    payload = {
        "format": 1,
        "generated_at": "2026-10-16T00:00:00",
        "include_secrets": include_secrets,
        "tables": {
            "accounts": accounts,
            "system_settings": [],
        },
    }
    if extra:
        payload.update(extra)
    return payload


def _acc_row(uid="u1", auth=GOOD_AUTH, **kw):
    """构造一行 accounts（列名与 NOT NULL 约束以 ORM 定义为准）。

    注意 `name` 与 `group_id` 是 NOT NULL（模型定义如此）—— 少了它们 INSERT 会失败。
    """
    row = {
        "id": 1, "name": "acct-1", "uid": uid, "auth_json": auth,
        "status": "active", "realm": "cn", "domain": "www.workbuddy.cn",
        "balance_remain": 100, "balance_total": 200,
        "group_id": 0, "created_at": None, "updated_at": None,
    }
    row.update(kw)
    return row


class RejectRedactedCase(unittest.TestCase):
    def setUp(self):
        from admin import backup
        self.backup = backup
        self.db = _db()

    def tearDown(self):
        self.db.close()

    # ── 正例：含真实凭据的快照必须能恢复 ──
    def test_good_snapshot_restores(self):
        r = self.backup.restore_snapshot(self.db, _snapshot([_acc_row()]))
        self.assertEqual(r["restored"].get("accounts"), 1)
        from admin.models import Account
        acc = self.db.query(Account).first()
        self.assertEqual(json.loads(acc.auth_json)["auth"]["accessToken"], "tok-abc")

    # ── 防线 1：自报脱敏 ──
    def test_rejects_snapshot_that_says_no_secrets(self):
        with self.assertRaises(ValueError) as cm:
            self.backup.restore_snapshot(
                self.db, _snapshot([_acc_row(auth="[redacted]")], include_secrets=False))
        msg = str(cm.exception)
        self.assertIn("脱敏", msg)
        # 关键：库**没被改动**（拒绝发生在写之前）
        from admin.models import Account
        self.assertEqual(self.db.query(Account).count(), 0)

    # ── 防线 2：值里含占位符（即使自报 include_secrets=True）──
    def test_rejects_redacted_values_even_if_claimed_secure(self):
        """防「自报 True 但内容被脱敏」——这份快照会被断言拦下。"""
        with self.assertRaises(ValueError) as cm:
            self.backup.restore_snapshot(
                self.db, _snapshot([_acc_row(auth="[redacted]")], include_secrets=True))
        self.assertIn("占位符", str(cm.exception))

    def test_rejects_other_redact_markers(self):
        for marker in ("[redacted-token]", "[redacted-hash]", "[redacted-key]", "[redacted-ip]"):
            with self.subTest(marker=marker):
                with self.assertRaises(ValueError):
                    self.backup.restore_snapshot(
                        self.db, {"format": 1, "tables": {"accounts": [], "system_settings": [
                            {"key": "k", "value": marker, "updated_at": None}]}})

    # ── 防线 3：auth_json 结构损坏 ──
    def test_rejects_broken_auth_json(self):
        with self.assertRaises(ValueError) as cm:
            self.backup.restore_snapshot(self.db, _snapshot([_acc_row(auth='{"auth": ')]))
        self.assertIn("合法 JSON", str(cm.exception))

    def test_rejects_auth_without_auth_section(self):
        """合法 JSON 但缺 auth 段 → 上游调用必然失败，也要拒。"""
        with self.assertRaises(ValueError) as cm:
            self.backup.restore_snapshot(self.db, _snapshot([_acc_row(auth='{"foo": 1}')]))
        self.assertIn("合法 JSON", str(cm.exception))

    # ── 逃生门：显式允许时才放行（结构演示用）──
    def test_allow_redacted_opt_in(self):
        r = self.backup.restore_snapshot(
            self.db, _snapshot([_acc_row(auth="[redacted]")], include_secrets=False),
            allow_redacted=True)
        self.assertEqual(r["restored"].get("accounts"), 1)

    # ── 空 auth_json 不误伤（可能是未完成导入的账号）──
    def test_empty_auth_not_treated_as_broken(self):
        r = self.backup.restore_snapshot(self.db, _snapshot([_acc_row(auth="")]))
        self.assertEqual(r["restored"].get("accounts"), 1)

    # ── 拒绝必须是「整体拒绝」，不能留半份数据 ──
    def test_rejection_leaves_db_untouched(self):
        from admin.models import Account
        self.backup.restore_snapshot(self.db, _snapshot([_acc_row(uid="old")]))
        self.assertEqual(self.db.query(Account).count(), 1)
        with self.assertRaises(ValueError):
            self.backup.restore_snapshot(self.db, _snapshot([_acc_row(uid="new", auth="[redacted]")]))
        acc = self.db.query(Account).first()
        self.assertEqual(acc.uid, "old", "被拒绝的恢复不该动到现有数据")


class RedactHelpersCase(unittest.TestCase):
    """辅助函数的单元行为（它们是防线 2/3 的实现）。"""

    def test_find_redacted(self):
        from admin import backup
        self.assertEqual(backup._find_redacted({"t": [{"a": "[redacted]"}]}), "[redacted]")
        self.assertEqual(backup._find_redacted({"t": [{"a": "ok"}]}), "")
        self.assertEqual(backup._find_redacted({"t": "not-a-list"}), "")

    def test_find_broken_auth(self):
        from admin import backup
        good = {"accounts": [{"auth_json": GOOD_AUTH}, {"auth_json": ""}]}
        self.assertEqual(backup._find_broken_auth(good), 0)
        bad = {"accounts": [{"auth_json": "[redacted]"}, {"auth_json": '{"x":1}'}]}
        self.assertEqual(backup._find_broken_auth(bad), 2)


if __name__ == "__main__":
    unittest.main()
