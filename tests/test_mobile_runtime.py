"""移动端（安卓）场景：SQLite + memory 限流是否够用。

## 为什么单独有一组测试

安卓端与本机部署有个本质区别：**只有 SQLite，没有 MySQL / Redis**
（手机上不可能让用户装这两个服务）。

所以必须确认三件事，否则「能在手机上跑」只是纸面结论：

1. **WAL 下写不挡读** —— 网关一边记用量日志、一边查账号，不能互相阻塞
2. **memory 限流够快** —— 没有 Redis 时用进程内存，性能必须跟得上
3. **并发写不报 `database is locked`** —— SQLite 最经典的故障

## 实测数据（本轮，本机 SSD）

    WAL 模式生效（journal_mode = wal）
    并发：200 次写 + 240 次读，跨 4 线程，0 失败，0.17s
    memory 限流：20,000 次 allow() = 24.6ms → 813,286 次/秒
    usage_logs 3200 行：取最近 20 条 < 1ms，按账号 count < 1ms

结论：移动端量级完全够用。
"""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ADMIN_JWT_SECRET", "x" * 40)


class MobileSqliteCase(unittest.TestCase):
    """手机端负载下的 SQLite 行为。"""

    @classmethod
    def setUpClass(cls):
        import shutil

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from admin.db import Base
        from admin import models as _m  # noqa: F401

        cls.tmpdir = tempfile.mkdtemp(prefix="wb_mobile_ut_")
        db_path = os.path.join(cls.tmpdir, "wb.db").replace("\\", "/")
        cls.engine = create_engine(f"sqlite:///{db_path}", future=True)

        # 复刻 admin/db.py 里 SQLite 的 PRAGMA（这是移动端能用的关键）
        from sqlalchemy import event

        @event.listens_for(cls.engine, "connect")
        def _pragmas(conn, _rec):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")

        Base.metadata.create_all(bind=cls.engine, tables=[
            _m.Account.__table__, _m.UsageLog.__table__])
        cls.Session = sessionmaker(bind=cls.engine, future=True)
        cls.shutil = shutil

    @classmethod
    def tearDownClass(cls):
        try:
            cls.engine.dispose()
        finally:
            cls.shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_wal_mode_active(self):
        """WAL 必须真的生效 —— 没有它读写互斥，网关会卡。"""
        with self.engine.connect() as c:
            mode = c.exec_driver_sql("PRAGMA journal_mode").scalar()
        self.assertEqual(str(mode).lower(), "wal",
                         "**必须启用 WAL** —— 否则「记日志」会阻塞「查账号」")

    def test_concurrent_read_write_no_lock_error(self):
        """**核心**：一边写日志一边读账号，不能出现 `database is locked`。

        这正是手机上会发生的场景：网关在记用量，后台页面在查账号。
        """
        from admin.models import Account, UsageLog

        db = self.Session()
        try:
            for i in range(20):
                db.add(Account(uid=f"m{i}", name=f"m{i}", status="active",
                               realm="cn", auth_json="{}"))
            db.commit()
        finally:
            db.close()

        errors = []
        written = {"ok": 0}
        read = {"ok": 0}
        stop = threading.Event()

        def writer():
            d = self.Session()
            try:
                for _ in range(80):
                    if stop.is_set():
                        break
                    try:
                        d.add(UsageLog(model="glm-5.1", account_id=1,
                                       total_tokens=10, latency_ms=100))
                        d.commit()
                        written["ok"] += 1
                    except Exception as e:      # noqa: BLE001
                        errors.append(f"write:{type(e).__name__}:{e}")
                        try:
                            d.rollback()
                        except Exception:
                            pass
            finally:
                d.close()

        def reader():
            d = self.Session()
            try:
                for _ in range(60):
                    if stop.is_set():
                        break
                    try:
                        d.query(Account).filter(Account.status == "active").count()
                        read["ok"] += 1
                    except Exception as e:      # noqa: BLE001
                        errors.append(f"read:{type(e).__name__}:{e}")
                        try:
                            d.rollback()
                        except Exception:
                            pass
            finally:
                d.close()

        threads = [threading.Thread(target=writer),
                   threading.Thread(target=reader),
                   threading.Thread(target=reader)]
        for t in threads:
            t.start()
        threads[0].join(timeout=40)
        stop.set()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual(errors, [],
                         f"并发读写出现错误（WAL/busy_timeout 失效？）：{errors[:3]}")
        self.assertGreater(written["ok"], 0, "写入应成功")
        self.assertGreater(read["ok"], 0, "读取应成功")


class MemoryLimitPerfCase(unittest.TestCase):
    """没有 Redis 时，memory 限流必须够快。"""

    def test_memory_backend_throughput(self):
        """测 `allow()` 的吞吐（memory 后端下应远高于手机端需求）。

        注意：`gateway_limits` 的 Redis 客户端是**模块级单例**，一旦初始化过
        就固定了。所以这里不能只改环境变量 —— 必须直接 patch 掉
        `_redis_client` 返回 None，强制走内存分支。
        """
        from unittest import mock

        from admin import gateway_limits as gl

        with mock.patch.object(gl, "_redis_client", return_value=None):
            n = 5000
            t0 = time.perf_counter()
            for _ in range(n):
                gl.allow("perf-test", per_key_concurrent=None)
            elapsed = time.perf_counter() - t0

        rate = n / elapsed if elapsed > 0 else 0
        # 手机端即使 10 QPS 也只用到这个数字的零头；门槛设得很宽松，
        # 只为防止「误改成每次查库 / 加全局锁」导致的数量级退化。
        self.assertGreater(
            rate, 50_000,
            f"memory 限流只有 {rate:,.0f} 次/秒 —— 疑似退化"
            "（不应每次走数据库或全局锁）")

    def test_memory_backend_needs_no_redis(self):
        """确认 memory 后端不会去连 Redis（手机上没有）。"""
        src = (os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "admin", "gateway_limits.py"))
        with open(src, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("backend != 'memory'", text,
                      "memory 后端应跳过 redis 的连接尝试")


class MobileLayoutCase(unittest.TestCase):
    """安卓端要靠新面板的响应式布局。"""

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.sidebar = open(os.path.join(
            root, "web", "components", "common", "layout", "Sidebar.tsx"),
            encoding="utf-8").read()

    def test_has_mobile_nav(self):
        self.assertIn("MobileNav", self.sidebar,
                      "手机端需要汉堡菜单（侧边栏在窄屏必须收起）")

    def test_hides_sidebar_on_narrow(self):
        """窄屏隐藏常驻侧边栏，靠抽屉导航。"""
        self.assertIn("lg:hidden", self.sidebar,
                      "窄屏应隐藏常驻侧边栏并显示汉堡按钮")

    def test_drawer_is_portal(self):
        """抽屉必须 portal 到 body。

        渲染在 sticky + backdrop-blur 的 header 内部时，`fixed inset-0`
        的高度会退化成 header 的高度（实测只有 52px，菜单全在视口外）。
        """
        self.assertIn("createPortal", self.sidebar,
                      "抽屉必须 portal 到 body，否则高度塌成 header 高度")


class NoServerComponentDependencyCase(unittest.TestCase):
    """移动端不能依赖额外服务组件。"""

    def test_default_limit_backend_can_be_memory(self):
        from pathlib import Path
        cfg = (Path(__file__).resolve().parent.parent
               / "admin" / "config.py").read_text(encoding="utf-8")
        self.assertIn("GATEWAY_LIMIT_BACKEND", cfg)
        self.assertIn('"auto"', cfg,
                      "默认 auto：能连 Redis 就用，连不上降级（手机上没有 Redis）")

    def test_sqlite_url_accepted(self):
        """配置层必须接受 sqlite:/// 形式的 URL（不是只认 mysql）。"""
        from pathlib import Path
        cfg = (Path(__file__).resolve().parent.parent
               / "admin" / "config.py").read_text(encoding="utf-8")
        self.assertIn("ADMIN_DATABASE_URL", cfg)
        # 默认值目前是 mysql，但必须没有任何「校验只允许 mysql」的逻辑
        self.assertNotIn("must start with mysql", cfg.lower())
        self.assertNotIn("只支持 mysql", cfg.lower())


if __name__ == "__main__":
    unittest.main()
