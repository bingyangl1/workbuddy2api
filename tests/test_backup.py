"""P5：本地快照备份 / 恢复（admin/backup.py）。

覆盖：导出（含/不含密）→ 脱敏（结构保留、敏感列替换）→ 导入（覆盖式、单事务回滚）
→ 文件读写（名字校验防穿越）→ 恢复前自动留底。

用内存 SQLite 建最小表集（accounts / api_keys / model_configs），不碰真库。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def _factory():
    from admin.db import Base
    from admin import models as _m  # noqa: F401
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine, tables=[
        Base.metadata.tables["accounts"],
        Base.metadata.tables["api_keys"],
        Base.metadata.tables["model_configs"],
        Base.metadata.tables["system_settings"],
    ])
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class ExportCase(unittest.TestCase):
    def setUp(self):
        self.factory = _factory()
        with self.factory() as db:
            from admin.models import Account, ApiKey, ModelConfig
            db.add(Account(name="a1", uid="u1", realm="cn",
                           auth_json='{"token":"SECRET-TOKEN"}', status="active",
                           balance_remain=500))
            db.add(ApiKey(name="k1", key_hash="HASH-SECRET", key_prefix="wb-ab",
                          key_full="FULL-SECRET", credit_limit=10.0))
            db.add(ModelConfig(model_id="glm-5.2", level="system", enabled=1,
                               credit_multiplier=1.5))
            db.commit()

    def test_export_with_secrets(self):
        from admin.backup import export_snapshot, snapshot_stats
        with self.factory() as db:
            snap = export_snapshot(db, include_secrets=True)
        self.assertEqual(snap["format"], 1)
        self.assertTrue(snap["include_secrets"])
        rows = snap["tables"]["accounts"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["auth_json"], '{"token":"SECRET-TOKEN"}')
        self.assertEqual(snap["tables"]["api_keys"][0]["key_hash"], "HASH-SECRET")
        self.assertEqual(snapshot_stats(snap)["accounts"], 1)

    def test_export_redacted_keeps_structure(self):
        from admin.backup import export_snapshot
        with self.factory() as db:
            snap = export_snapshot(db, include_secrets=False)
        self.assertFalse(snap["include_secrets"])
        acc = snap["tables"]["accounts"][0]
        # 结构（列）完整，敏感值替换
        self.assertIn("auth_json", acc)
        self.assertEqual(acc["auth_json"], "[redacted]")
        self.assertEqual(acc["name"], "a1")            # 非敏感列保留
        self.assertEqual(acc["balance_remain"], 500)
        key = snap["tables"]["api_keys"][0]
        self.assertEqual(key["key_hash"], "[redacted-hash]")
        self.assertEqual(key["key_full"], "[redacted-key]")

    def test_redaction_does_not_mangle_multiplier(self):
        """`credit_multiplier` 含子串 "ip"（mult**ip**lier）——绝不能被 IP 规则误脱敏。

        这是实测踩过的真 bug：IP 类敏感列若用子串匹配，积分倍率会被替换成
        [redacted-ip]，快照里所有模型的倍率全丢。
        """
        from admin.backup import export_snapshot, _redact_row
        self.assertEqual(_redact_row({"credit_multiplier": 1.5}, False)["credit_multiplier"], 1.5)
        self.assertEqual(_redact_row({"description": "x"}, False)["description"], "x")
        # 真 IP 列仍要脱敏（精确列名）
        self.assertEqual(_redact_row({"ip": "1.2.3.4"}, False)["ip"], "[redacted-ip]")
        self.assertEqual(_redact_row({"client_ip": "1.2.3.4"}, False)["client_ip"], "[redacted-ip]")
        with self.factory() as db:
            snap = export_snapshot(db, include_secrets=False)
        mc = snap["tables"]["model_configs"][0]
        self.assertEqual(mc["credit_multiplier"], 1.5)

    def test_datetime_serialized(self):
        from admin.backup import export_snapshot, _deserialize_value
        with self.factory() as db:
            snap = export_snapshot(db, include_secrets=True)
        acc = snap["tables"]["accounts"][0]
        self.assertIsInstance(acc["created_at"], dict)
        self.assertIn("__dt__", acc["created_at"])
        self.assertIsNotNone(_deserialize_value(acc["created_at"]))

    def test_sqlite_string_timestamp_normalized(self):
        """SQLite 裸 SQL 把 DATETIME 返回成字符串——也必须归一成 {"__dt__":...}。

        跨方言陷阱：MySQL 驱动返回 datetime 对象、SQLite 返回 str，
        不收敛的话同一份快照在不同库上结构不同，导入会静默写错类型。
        """
        from admin.backup import _serialize_value, _deserialize_value
        out = _serialize_value("2026-10-15 16:57:06.631755", "created_at")
        self.assertIsInstance(out, dict)
        self.assertEqual(out["__dt__"], "2026-10-15T16:57:06.631755")
        self.assertIsNotNone(_deserialize_value(out))
        # 普通字符串（非时间列，或时间列但格式不符）原样保留，不误判
        self.assertEqual(_serialize_value("hello", "note"), "hello")
        self.assertEqual(_serialize_value("not-a-date", "created_at"), "not-a-date")


class RestoreCase(unittest.TestCase):
    def setUp(self):
        self.factory = _factory()

    def _seed(self, name="a1"):
        with self.factory() as db:
            from admin.models import Account
            db.add(Account(name=name, uid="u-" + name, realm="cn",
                           auth_json='{"token":"t"}', status="active", balance_remain=1))
            db.commit()

    def test_restore_overwrites(self):
        from admin.backup import export_snapshot, restore_snapshot
        from admin.models import Account
        self._seed("old-a")
        with self.factory() as db:
            snap = export_snapshot(db, include_secrets=True)
        # 改库：删旧、加新
        with self.factory() as db:
            db.execute(text("DELETE FROM accounts"))
            db.add(Account(name="new-a", uid="u-new", realm="cn",
                           auth_json='{"token":"t"}', status="active", balance_remain=9))
            db.commit()
        with self.factory() as db:
            result = restore_snapshot(db, snap)
            self.assertEqual(result["restored"]["accounts"], 1)
            names = [a.name for a in db.query(Account).all()]
        self.assertEqual(names, ["old-a"])   # 恢复回快照状态（覆盖，非合并）

    def test_restore_quotes_reserved_column_names(self):
        """`system_settings.key` 是 MySQL 保留字——裸拼列名会 1064 语法错误。

        实测踩过：restore 到 MySQL 时 `INSERT INTO system_settings (key, ...)`
        直接语法错误。列名必须按方言引用（dialect.identifier_preparer.quote）。
        本测试在 SQLite 上跑同样的路径，确保引用逻辑生效（SQLite 也用引号）。
        """
        from admin.backup import export_snapshot, restore_snapshot
        from admin.models import SystemSetting
        with self.factory() as db:
            db.add(SystemSetting(key="admin_password", value="pbkdf2$x"))
            db.add(SystemSetting(key="security", value='{"enabled":false}'))
            db.commit()
        with self.factory() as db:
            snap = export_snapshot(db, include_secrets=True)
        self.assertEqual(len(snap["tables"]["system_settings"]), 2)
        with self.factory() as db:
            db.execute(text("DELETE FROM system_settings"))
            db.commit()
        with self.factory() as db:
            result = restore_snapshot(db, snap)
            self.assertEqual(result["restored"]["system_settings"], 2)
            keys = sorted(s.key for s in db.query(SystemSetting).all())
        self.assertEqual(keys, ["admin_password", "security"])

    def test_restore_bad_format_rejected(self):
        from admin.backup import restore_snapshot
        with self.factory() as db:
            with self.assertRaises(ValueError):
                restore_snapshot(db, {"format": 999, "tables": {}})
            with self.assertRaises(ValueError):
                restore_snapshot(db, {"format": 1})

    def test_restore_rolls_back_on_bad_column(self):
        """非法列名（注入面）→ 整事务回滚，不留半份数据。"""
        from admin.backup import restore_snapshot
        from admin.models import Account
        self._seed("keep-me")
        bad = {"format": 1, "tables": {"accounts": [
            {"id": 1, "name": "evil", "uid": "u", "realm": "cn",
             "auth_json": "{}", "status": "active", "balance_remain": 1,
             "1=1; --": "x"}]}}
        with self.factory() as db:
            with self.assertRaises(ValueError):
                restore_snapshot(db, bad)
        with self.factory() as db:
            names = [a.name for a in db.query(Account).all()]
        self.assertEqual(names, ["keep-me"])   # 回滚，原数据在


class FileCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="wb-backup-test-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_write_list_read(self):
        from admin.backup import write_snapshot, list_snapshots, read_snapshot
        payload = {"format": 1, "tables": {"accounts": []}}
        p = write_snapshot(self.dir, payload, "snap-1.json")
        self.assertTrue(p.is_file())
        items = list_snapshots(self.dir)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "snap-1.json")
        back = read_snapshot(self.dir, "snap-1.json")
        self.assertEqual(back["format"], 1)

    def test_name_traversal_rejected(self):
        from admin.backup import write_snapshot, read_snapshot
        with self.assertRaises(ValueError):
            write_snapshot(self.dir, {}, "../../evil.json")
        with self.assertRaises(ValueError):
            read_snapshot(self.dir, "../secret.json")
        with self.assertRaises(ValueError):
            read_snapshot(self.dir, "a/b.json")

    def test_pre_restore_backup_has_secrets(self):
        from admin.backup import pre_restore_backup, read_snapshot
        factory = _factory()
        with factory() as db:
            from admin.models import Account
            db.add(Account(name="a", uid="u", realm="cn",
                           auth_json='{"token":"REAL"}', status="active", balance_remain=1))
            db.commit()
        with factory() as db:
            path = pre_restore_backup(db, self.dir)
        self.assertTrue(path.name.startswith("pre-restore-"))
        snap = read_snapshot(self.dir, path.name)
        # 留底必须能完整还原 → 含真实凭据
        self.assertEqual(snap["tables"]["accounts"][0]["auth_json"], '{"token":"REAL"}')


class TableListCase(unittest.TestCase):
    def test_all_business_tables_covered(self):
        """TABLES 必须覆盖 models.py 里除运行期外的全部业务表。"""
        from admin.backup import TABLES
        import re
        src = Path("admin/models.py").read_text(encoding="utf-8")
        declared = set(re.findall(r'__tablename__\s*=\s*"([^"]+)"', src))
        missing = declared - set(TABLES)
        self.assertEqual(missing, set(), f"备份漏表: {missing}")

    def test_no_duplicate_tables(self):
        from admin.backup import TABLES
        self.assertEqual(len(TABLES), len(set(TABLES)))


if __name__ == "__main__":
    unittest.main()
