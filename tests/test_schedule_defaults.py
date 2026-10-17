"""默认调度策略：`run_growth_tasks` **默认不启用**。

## 用户要求

> 「run_growth_tasks 就默认不启用就行，不用手动设置」

意思是：**不要加设置项/开关**，直接把默认值定成「关着」，用户想用时
自己在「定时任务」页打开。

## 为什么这个默认值很重要

`run_growth_tasks` 与其它定时任务有本质区别：

| 任务 | 打上游写请求？ | 上游有重复检测？ |
|---|---|---|
| refresh_balances | 读（查余额） | 否 |
| refresh_credits | 读（查积分包） | 否 |
| sync_models | 读（查模型列表） | 否 |
| keepalive_tokens | 轻写（刷新登录态） | 宽容 |
| **run_growth_tasks** | **写（做任务、领奖）** | **是** |

所以它必须默认关 —— 一旦默认开，每个新装实例都会在用户不知情时
给全部账号发任务请求。这与用户反复强调的「多遍跑会检测」直接冲突。

## 覆盖的两个创建点

`enabled=0` 必须在**两处**都正确（漏一处就等于没改）：

1. `seed_defaults()`      —— 全新安装、schedules 表为空时
2. `ensure_growth_schedules()` —— 表里没有这一行时补建（老库升级路径）

本轮就是靠这两处都检查才没漏掉第二个。
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ADMIN_JWT_SECRET", "x" * 40)

ROOT = Path(__file__).resolve().parent.parent
SCHED = ROOT / "admin" / "scheduler.py"


class SeedDefaultsCase(unittest.TestCase):
    """`seed_defaults()`（全新安装路径）。"""

    @classmethod
    def setUpClass(cls):
        cls.src = SCHED.read_text(encoding="utf-8")

    def test_seed_creates_growth_run_disabled(self):
        """seed_defaults 里 run_growth_tasks 必须是 enabled=0。"""
        i = self.src.find("def seed_defaults")
        self.assertGreater(i, 0)
        end = self.src.find("\ndef ", i + 10)
        body = self.src[i:end]
        # 找到创建 run_growth_tasks 的那一段
        j = body.find('task="run_growth_tasks"')
        self.assertGreater(j, 0, "seed_defaults 应变出 run_growth_tasks 记录")
        seg = body[max(0, j - 300):j + 300]
        self.assertIn("enabled=0", seg,
                      "**run_growth_tasks 在 seed_defaults 里必须 enabled=0**"
                      "（默认启用 = 新装实例在用户不知情时给全部账号发任务请求）")
        self.assertNotIn("enabled=1", seg,
                         "不该在 run_growth_tasks 附近出现 enabled=1")

    def test_other_read_only_tasks_still_enabled(self):
        """其它（只读）任务仍应默认启用 —— 不能顺手全关掉。"""
        i = self.src.find("def seed_defaults")
        end = self.src.find("\ndef ", i + 10)
        body = self.src[i:end]
        for task in ("refresh_balances", "refresh_credits", "sync_models",
                     "daily_checkin"):
            j = body.find(f'task="{task}"')
            self.assertGreater(j, 0, f"{task} 应仍在默认种子里")
            seg = body[max(0, j - 300):j + 200]
            self.assertIn("enabled=1", seg,
                          f"{task} 是只读任务，应保持默认启用")

    def test_row_still_created(self):
        """**仍然建出这一行**（只是关着）—— 不建等于把功能藏起来。"""
        i = self.src.find("def seed_defaults")
        end = self.src.find("\ndef ", i + 10)
        body = self.src[i:end]
        self.assertIn('task="run_growth_tasks"', body,
                      "应建出记录让用户在列表里看到这个能力")


class EnsureOrderCase(unittest.TestCase):
    """`ensure_growth_schedules()`（老库升级路径）。

    **这是最容易漏的一处**：seed_defaults 只在 schedules 表为空时跑，
    老库升级时走的是这条补建路径。只改一处 = 老用户仍然被默认启用。
    """

    @classmethod
    def setUpClass(cls):
        cls.src = SCHED.read_text(encoding="utf-8")

    def test_ensure_creates_growth_run_disabled(self):
        i = self.src.find("def ensure_growth_schedules")
        self.assertGreater(i, 0, "应有 ensure_growth_schedules 函数")
        end = self.src.find("\ndef ", i + 10)
        body = self.src[i:end]
        # 找补建 run_growth_tasks 的那段（Schedule(...) 构造）
        j = body.find('task="run_growth_tasks"')
        self.assertGreater(j, 0)
        seg = body[max(0, j - 300):j + 320]
        self.assertIn("enabled=0", seg,
                      "**补建路径也必须 enabled=0** —— 否则老库升级后"
                      "run_growth_tasks 会被默认启用")

    def test_refresh_task_still_enabled(self):
        """「更新任务列表」是只读的，应保持启用。"""
        i = self.src.find("def ensure_growth_schedules")
        end = self.src.find("\ndef ", i + 10)
        body = self.src[i:end]
        j = body.find('task="refresh_growth_tasks"')
        self.assertGreater(j, 0)
        seg = body[max(0, j - 200):j + 300]
        self.assertIn("enabled=1", seg,
                      "refresh_growth_tasks 只读，应保持默认启用")


class NoOtherAutoEnableCase(unittest.TestCase):
    """全局扫描：不该有第三处把 run_growth_tasks 默认启用。

    判据必须精确到「**同一个 Schedule(...) 构造内部**」：
    早先用 ±300 字符窗口，会把紧邻的 `refresh_growth_tasks`
    （它本来就该 enabled=1）一起框进来 → 误报。
    """

    def test_no_enabled_one_near_growth_run(self):
        import re

        offenders = []
        for p in list(ROOT.glob("admin/**/*.py")) + list(ROOT.glob("*.py")):
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            lines = text.splitlines()
            for idx, line in enumerate(lines):
                if "run_growth_tasks" not in line:
                    continue
                # 往回找这一条语句的起点 Schedule(
                start = None
                for back in range(idx, max(-1, idx - 12), -1):
                    if "Schedule(" in lines[back]:
                        start = back
                        break
                if start is None:
                    continue
                stmt = " ".join(lines[start:idx + 4])
                # 只截到这条构造的右括号
                m = re.search(r"Schedule\((?:[^()]|\([^()]*\))*\)", stmt)
                one = m.group(0) if m else stmt[:400]
                if "run_growth_tasks" in one and "enabled=1" in one:
                    offenders.append(f"{p.relative_to(ROOT)}:{idx + 1}")
        self.assertEqual(offenders, [],
                         f"这些地方仍会默认启用 run_growth_tasks：{offenders}")


class BehaviorCase(unittest.TestCase):
    """行为级：真的建一次种子，断言库里那条是停用的。"""

    def test_seed_defaults_marks_growth_run_disabled(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from admin.db import Base
        from admin.models import Schedule
        from admin.scheduler import seed_defaults

        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(engine, tables=[Schedule.__table__])
        db = sessionmaker(bind=engine, future=True)()
        try:
            seed_defaults(db)
            row = (db.query(Schedule)
                   .filter(Schedule.task == "run_growth_tasks").first())
            self.assertIsNotNone(row, "应建出 run_growth_tasks 记录")
            self.assertEqual(int(row.enabled or 0), 0,
                             "**seed_defaults 建出来的 run_growth_tasks 必须停用**")
            # 对照：只读任务仍启用
            for task in ("refresh_balances", "sync_models", "daily_checkin"):
                r = db.query(Schedule).filter(Schedule.task == task).first()
                self.assertIsNotNone(r, f"{task} 应存在")
                self.assertEqual(int(r.enabled or 0), 1,
                                 f"{task} 应保持启用")
        finally:
            db.close()
            engine.dispose()


class DocConsistencyCase(unittest.TestCase):
    """文档/注释不能说反（否则后来者会以为它默认开着）。"""

    def test_lease_module_comment_is_accurate(self):
        text = (ROOT / "admin" / "growth_lease.py").read_text(encoding="utf-8")
        if "run_growth_tasks" in text:
            seg = text[max(0, text.find("run_growth_tasks") - 200):
                       text.find("run_growth_tasks") + 200]
            self.assertNotIn("已启用", seg,
                             "注释不该写「已启用」——它默认是关的")


if __name__ == "__main__":
    unittest.main()
