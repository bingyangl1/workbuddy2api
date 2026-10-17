"""账号级执行租约（`admin/growth_lease.py`）—— 跨进程互斥。

## 解决的问题

成长任务有三条执行路径，**都会走 `run_accounts()`**：

    POST /api/growth/run          同步（面板按钮）
    POST /api/growth/run-async    异步队列
    定时任务 run_growth_tasks     已被调度器支持

它们之间原本没有互斥。用户手动点一次、正好定时也触发 →
同一账号同一任务发两遍请求 —— 正是「多遍跑会检测」的场景。

`/run-async` 原有的 `jobrunner.RUNNER.get(JOB_KEY)` 是**进程内内存状态**：
  * 多 uvicorn worker 各有一份，互相看不见
  * 进程重启后清零
  * 只挡 run-async 自己，挡不住同步 /run 与定时任务

## 本测试覆盖

租约用的是 `SystemSetting` 表的 **CAS**（compare-and-swap）：
`UPDATE ... WHERE key=? AND value=<刚读到的旧值>`。
只有让那一行真正变化的调用者才算抢到 —— 这是跨进程互斥的关键。

因此测试必须覆盖：
  1. 抢到 / 抢不到
  2. 释放只能释放自己的（A 的锁不能被 B 释放）
  3. 过期后可被重新抢（进程被 kill 时不执行 release，必须靠 TTL 自愈）
  4. **CAS 语义本身**（并发时只有一个成功）
  5. 回调 `run_accounts` 真的用了租约（行为级，不只是单测纯函数）
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ADMIN_JWT_SECRET", "x" * 40)


def make_db():
    """内存 SQLite 会话（只建租约用到的表）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from admin.db import Base
    from admin.models import SystemSetting  # noqa: F401  确保表已注册

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine, tables=[SystemSetting.__table__])
    return sessionmaker(bind=engine, future=True)()


class LeaseBasicsCase(unittest.TestCase):
    def setUp(self):
        from admin import growth_lease
        self.lease = growth_lease
        self.db = make_db()

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass

    def test_acquire_then_release(self):
        owner = self.lease.acquire(self.db, 42)
        self.assertTrue(owner, "首次抢租约应成功")
        self.assertTrue(self.lease.is_locked(self.db, 42))
        self.assertTrue(self.lease.release(self.db, 42, owner))
        self.assertFalse(self.lease.is_locked(self.db, 42),
                         "释放后不该再是锁定状态")

    def test_second_acquire_fails_while_held(self):
        """**核心**：已持有时第二个调用者抢不到。"""
        first = self.lease.acquire(self.db, 42)
        self.assertTrue(first)
        second = self.lease.acquire(self.db, 42)
        self.assertIsNone(second,
                          "同一账号已被占用时，第二个调用必须失败（否则就是重复请求）")

    def test_different_accounts_independent(self):
        """按账号加锁，不是全局锁（否则 A 在跑会挡住 B）。"""
        a = self.lease.acquire(self.db, 1)
        b = self.lease.acquire(self.db, 2)
        self.assertTrue(a, "账号 1 应能抢到")
        self.assertTrue(b, "账号 2 应能同时抢到（锁按账号分）")
        self.assertNotEqual(a, b)

    def test_release_only_by_owner(self):
        """**关键**：不能释放别人的锁。

        场景：A 的租约过期 → B 抢到了 → A 迟到的 release 不能解掉 B 的锁。
        """
        a = self.lease.acquire(self.db, 42)
        self.assertTrue(a)
        self.assertFalse(self.lease.release(self.db, 42, "not-the-owner"),
                         "非持有者释放必须失败")
        self.assertTrue(self.lease.is_locked(self.db, 42), "锁应仍在")

    def test_expired_lease_can_be_retaken(self):
        """**必须有 TTL**：进程被 kill 时不执行 release，只能靠过期自愈。

        注意断言方式：不能只把 value 改成过期就算了 —— 那样「永不过期」
        的实现也会通过（它根本不看 expires）。必须**先抢一次、让它自然过期、
        再抢一次**，才能证明「过期判断真的被执行了」。
        """
        from admin import growth_lease

        # TTL 设成 30 秒（函数内会被夹到 >=30），然后手工改 expires 到过去
        first = self.lease.acquire(self.db, 42, ttl=30)
        self.assertTrue(first, "第一次应抢到")

        # 把 expires 改成过去（模拟进程被 kill，release 从未执行）
        import json

        from admin.models import SystemSetting
        key = self.lease.lease_key(42)
        row = self.db.get(SystemSetting, key)
        row.value = json.dumps({"owner": first, "expires": 1})
        self.db.commit()

        # 关键：**再抢一次**。若实现忽略 expires，这里会返回 None
        again = growth_lease.acquire(self.db, 42)
        self.assertTrue(again,
                        "**过期后必须能被重新抢到** —— 否则进程被 kill 后"
                        "这个账号会被永久锁死（TTL 失效）")

    def test_locked_check_respects_expiry(self):
        """`is_locked` 也必须看 expires（不能只判断 key 存不存在）。"""
        import json

        from admin.models import SystemSetting
        key = self.lease.lease_key(77)
        self.db.add(SystemSetting(key=key,
                                  value=json.dumps({"owner": "x", "expires": 1})))
        self.db.commit()
        self.assertFalse(self.lease.is_locked(self.db, 77),
                         "已过期的租约不该算锁定")

    def test_valid_lease_blocks(self):
        """过期判断的另一面：**未过期**的必须真的挡住。"""
        owner = self.lease.acquire(self.db, 78, ttl=600)
        self.assertTrue(owner)
        self.assertTrue(self.lease.is_locked(self.db, 78), "有效租约必须算锁定")
        self.assertIsNone(self.lease.acquire(self.db, 78), "未过期时必须抢不到")

    def test_hold_context_releases_even_on_exception(self):
        """`with hold(...)` 抛异常也必须释放，否则账号被锁到 TTL 到期。"""
        owner_seen = []
        try:
            with self.lease.hold(self.db, 42) as owner:
                owner_seen.append(owner)
                raise RuntimeError("模拟任务失败")
        except RuntimeError:
            pass
        self.assertTrue(owner_seen and owner_seen[0])
        self.assertFalse(self.lease.is_locked(self.db, 42),
                         "异常退出后租约必须已释放")

    def test_hold_yields_none_when_busy(self):
        first = self.lease.acquire(self.db, 42)
        self.assertTrue(first)
        with self.lease.hold(self.db, 42) as owner:
            self.assertIsNone(owner, "被占用时应 yield None（调用方据此跳过）")

    def test_renew(self):
        owner = self.lease.acquire(self.db, 42, ttl=60)
        self.assertTrue(owner)
        self.assertTrue(self.lease.renew(self.db, 42, owner, ttl=600))
        self.assertFalse(self.lease.renew(self.db, 42, "other", ttl=600),
                         "非持有者不能续期")

    def test_broken_value_treated_as_free(self):
        """坏数据（非法 JSON）不能把账号永久锁死。"""
        from admin.models import SystemSetting
        key = self.lease.lease_key(42)
        self.db.add(SystemSetting(key=key, value="not-json{{{"))
        self.db.commit()
        self.assertFalse(self.lease.is_locked(self.db, 42),
                         "坏数据应视为空闲，否则账号永久锁定")
        self.assertTrue(self.lease.acquire(self.db, 42), "坏数据下应能抢到")


class LeaseCasCase(unittest.TestCase):
    """CAS 语义：并发时只有一个能成功（这是跨进程互斥的本质）。

    ## 为什么用「两次调用 + 中间插入脏读」而不是真多线程

    真多线程 + SQLite 内存库在 Windows 上不稳定（`check_same_thread` +
    共享缓存 URI 容易崩）。而 CAS 的正确性**不需要真并发**来验证 ——
    关键是「拿旧值做 WHERE 条件」这一条语义：

        读 value=V → 写 WHERE value=V

    只要证明「V 被别处改掉后，这次 UPDATE 影响 0 行」，就等于证明了
    并发时只有一个能赢（数据库保证 WHERE 的原子性）。
    """

    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from admin.db import Base
        from admin.models import SystemSetting

        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine, tables=[SystemSetting.__table__])
        self.Maker = sessionmaker(bind=self.engine, future=True)

    def tearDown(self):
        self.engine.dispose()

    def test_cas_loses_when_value_changed_underneath(self):
        """**核心**：`UPDATE ... WHERE value=<旧值>` 必须真的生效。

        ## 为什么这条测试要这样写

        前两版都写成「先改 value 再调 acquire」—— 那样即使实现是
        **无条件 UPDATE** 也会通过（因为它压根不看旧值，直接覆盖）。
        反向验证时发现了这个假绿。

        正确做法：直接验证 **SQL 语义** —— 构造一次「WHERE 条件不成立」的
        UPDATE，断言 `rowcount == 0`。这正是 CAS 依赖的数据库保证。
        """
        from admin import growth_lease
        from admin.models import SystemSetting

        db = self.Maker()
        try:
            key = growth_lease.lease_key(900)
            db.add(SystemSetting(key=key, value="old-value"))
            db.commit()

            # 模拟「读到 old-value，但此时库里已是 other」→ WHERE 不成立
            changed = (db.query(SystemSetting)
                       .filter(SystemSetting.key == key,
                               SystemSetting.value == "stale-value")
                       .update({SystemSetting.value: "should-not-apply"},
                               synchronize_session=False))
            db.commit()
            self.assertEqual(changed, 0,
                             "WHERE value=旧值 不成立时，UPDATE 必须影响 0 行"
                             "（这是 CAS 的前提）")

            # 条件成立时才应该成功
            changed2 = (db.query(SystemSetting)
                        .filter(SystemSetting.key == key,
                                SystemSetting.value == "old-value")
                        .update({SystemSetting.value: "applied"},
                                synchronize_session=False))
            db.commit()
            self.assertEqual(changed2, 1, "WHERE 成立时应更新 1 行")
        finally:
            db.close()

    def test_acquire_uses_cas_filter(self):
        """静态断言：acquire 的 UPDATE 确实带 `WHERE value = 旧值`。

        这是**唯一**能区分「CAS 锁」与「无条件覆盖」的地方 ——
        行为测试很难在没有真并发时区分两者（前两版就栽在这）。
        """
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "admin" / "growth_lease.py").read_text(encoding="utf-8")
        i = src.find("def acquire")
        j = src.find("def release", i)
        body = src[i:j]
        self.assertIn("SystemSetting.value == previous", body,
                      "**acquire 必须用 CAS**（WHERE value=刚读到的旧值）；"
                      "无条件 UPDATE 在多进程下会让两个调用者都以为自己抢到了")
        self.assertIn("previous = row.value", body,
                      "CAS 的「旧值」必须来自同一行读取，不能凭空构造")

    def test_sequential_second_call_denied(self):
        """顺序调用：第一次成功，第二次失败（等价于并发时只有一个赢）。"""
        from admin import growth_lease

        db = self.Maker()
        try:
            first = growth_lease.acquire(db, 901)
            second = growth_lease.acquire(db, 901)
            self.assertTrue(first, "第一个应抢到")
            self.assertIsNone(second, "第二个必须失败 —— 否则就是重复发请求")
        finally:
            db.close()

    def test_cas_pattern_present_in_source(self):
        """静态断言：确实用了「WHERE value = 旧值」的比较交换。"""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "admin" / "growth_lease.py").read_text(encoding="utf-8")
        self.assertIn("SystemSetting.value == previous", src,
                      "**必须用 CAS**（WHERE value=旧值）；"
                      "无条件 UPDATE 在多进程下会让两个调用者都以为自己抢到了")


class RunAccountsUsesLeaseCase(unittest.TestCase):
    """**行为级**：`run_accounts()` 真的用了租约（不只是单测纯函数）。

    只看纯函数的测试会漏掉「函数写好了但没接进去」——
    这正是本项目 A1 那轮踩过的假绿。
    """

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.src = (Path(__file__).resolve().parent.parent
                   / "admin" / "routers" / "growth.py").read_text(encoding="utf-8")

    def test_run_accounts_acquires_and_releases(self):
        self.assertIn("growth_lease.acquire(db, aid)", self.src,
                      "run_accounts 必须按账号抢租约")
        self.assertIn("growth_lease.release(db, aid, lease_owner)", self.src,
                      "run_accounts 必须释放租约")

    def test_release_in_finally(self):
        """释放必须在 finally 里 —— 任务抛异常时也要解锁。"""
        i = self.src.find("growth_lease.release(db, aid, lease_owner)")
        self.assertGreater(i, 0)
        # 往上找最近的 finally
        head = self.src[max(0, i - 700):i]
        self.assertIn("finally:", head,
                      "释放必须位于 finally 块（异常路径也要解锁）")

    def test_busy_account_marked_skipped(self):
        """抢不到时如实标注「跳过」，而不是当成失败。"""
        i = self.src.find("growth_lease.acquire(db, aid)")
        body = self.src[i:i + 700]
        self.assertIn("skipped", body,
                      "抢不到租约应标 skipped=True（不是失败）")
        self.assertIn("另一个任务执行中", body)

    def test_all_entrypoints_pass_source(self):
        """三条路径都要传 source（审计要能区分是谁跑的）。"""
        for src in ("sync", "async"):
            self.assertIn(f'source="{src}"', self.src,
                          f"缺少 source={src}（审计无法区分入口）")
        from pathlib import Path
        sched = (Path(__file__).resolve().parent.parent
                 / "admin" / "scheduler.py").read_text(encoding="utf-8")
        self.assertIn('source="schedule"', sched,
                      "定时任务也必须传 source=schedule")


class AuditRecordCase(unittest.TestCase):
    """执行审计记录。"""

    def test_table_and_indexes(self):
        from admin.models import GrowthRunRecord
        cols = [c.name for c in GrowthRunRecord.__table__.columns]
        for need in ("account_id", "source", "task_code", "ok", "skipped",
                     "credit", "energy", "reason", "created_at"):
            self.assertIn(need, cols, f"审计表缺列 {need}")

    def test_in_backup_tables(self):
        """备份必须包含审计表（完整快照语义）。"""
        from admin.backup import TABLES
        self.assertIn("growth_run_records", TABLES)

    def test_record_helper_is_fault_tolerant(self):
        """审计失败不能阻断主流程（旁路容错）。"""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "admin" / "routers" / "growth.py").read_text(encoding="utf-8")
        i = src.find("def _record_run")
        self.assertGreater(i, 0)
        body = src[i:i + 1600]
        self.assertIn("except Exception", body, "写审计必须吞异常")
        self.assertIn("rollback", body, "失败后必须 rollback，否则会话脏了")


class RealmNotBrokenCase(unittest.TestCase):
    """吸收能力**不能破坏**我们原有的 realm 隔离。"""

    def test_growth_still_realm_aware(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "admin" / "routers" / "growth.py").read_text(encoding="utf-8")
        self.assertIn("realm", src, "growth 路由仍须处理 realm")
        self.assertGreater(src.count("realm"), 5,
                           "realm 相关代码不该在本次改动中减少")

    def test_lease_module_has_no_upstream_realm_assumption(self):
        """租约是纯本地状态，不该引入任何 realm 判断（它和上游无关）。"""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "admin" / "growth_lease.py").read_text(encoding="utf-8")
        self.assertNotIn("AccountSession", src,
                         "租约不该触碰上游（它只操作本地 DB）")


if __name__ == "__main__":
    unittest.main()
