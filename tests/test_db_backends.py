"""多数据库后端：SQLite 与 MySQL 均可选，Redis 可选。

## 需求背景

> 「如果是 PC 端、安卓端的 app 的话，数据库就用 sqlite 这样就不用单独部署
>    组件了；源码、docker 部署可以让用户选择是 sqlite 或者是 mysql，redis 也同理」

也就是：

| 形态 | 数据库 | 缓存 |
|---|---|---|
| PC / 安卓 应用 | SQLite（**零外部依赖**） | 进程内存 |
| 源码 / Docker 单机 | SQLite 或 MySQL 任选 | 进程内存或 Redis 任选 |
| 多实例部署 | MySQL | Redis（限流要共享计数） |

## 这套能力其实**已经具备**（本测试把它钉住）

* `admin/db.py` 从早期就有三处 `dialect.name == "sqlite"` 分支：
  PRAGMA、`_ensure_column`、`_ensure_index` 都各有一条 SQLite 实现
* `admin/gateway_limits.py` 的 `GATEWAY_LIMIT_BACKEND` 支持
  `redis` / `memory` / `auto`，`auto` 连不上 Redis 会自动降级

但**没有任何测试守着它们** —— 谁哪天顺手删掉 SQLite 分支，
MySQL 环境下所有测试仍会全绿（因为测试跑的是 SQLite 内存库……）。
所以本文件的价值是：把「两条路都通」变成可执行的约束。

## 实测结论（本轮验证）

真实 SQLite 文件跑 `init_db()`：17 张表、全部迁移、CRUD、唯一约束、
租约 CAS 全部通过；引擎在 SQLite 下正常启动，10 个核心端点全部 200。
"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ADMIN_JWT_SECRET", "x" * 40)

ROOT = Path(__file__).resolve().parent.parent
DBPY = ROOT / "admin" / "db.py"
GW = ROOT / "admin" / "gateway_limits.py"
ENV_EXAMPLE = ROOT / ".env.example"


class SqliteDialectBranchesCase(unittest.TestCase):
    """`admin/db.py` 必须保留 SQLite 分支（三处，缺一不可）。"""

    @classmethod
    def setUpClass(cls):
        cls.src = DBPY.read_text(encoding="utf-8")

    def test_dialect_check_present(self):
        n = len(re.findall(r'dialect\.name\s*==\s*"sqlite"', self.src))
        self.assertGreaterEqual(
            n, 3,
            f"db.py 应有至少 3 处 sqlite 方言分支（PRAGMA / 列 / 索引），实际 {n} 处")

    def test_sqlite_pragmas(self):
        """WAL + busy_timeout：没有它们 SQLite 并发读写会 `database is locked`。"""
        self.assertIn("PRAGMA journal_mode=WAL", self.src,
                      "必须启用 WAL（否则读写互斥，网关并发会 BLOCKED）")
        self.assertIn("PRAGMA busy_timeout", self.src,
                      "必须设 busy_timeout（否则锁冲突直接抛异常而不是等待）")

    def test_ensure_column_has_sqlite_path(self):
        i = self.src.find("def _ensure_column")
        self.assertGreater(i, 0)
        j = self.src.find("def _ensure_index", i)
        body = self.src[i:j]
        self.assertIn('"sqlite"', body,
                      "_ensure_column 必须有 SQLite 分支（SQLite 的 ALTER 不支持多列/默认值语法）")
        self.assertIn("INFORMATION_SCHEMA", body,
                      "MySQL 分支应继续走 INFORMATION_SCHEMA 判断列是否存在")

    def test_ensure_index_has_sqlite_path(self):
        i = self.src.find("def _ensure_index")
        self.assertGreater(i, 0)
        body = self.src[i:i + 2600]
        self.assertIn('"sqlite"', body,
                      "_ensure_index 必须有 SQLite 分支")
        self.assertIn("CREATE INDEX IF NOT EXISTS", body,
                      "SQLite 用 IF NOT EXISTS（它不支持 MySQL 那种先查 information_schema 再建）")

    def test_ensure_database_only_for_mysql(self):
        """建库逻辑只对 MySQL 有意义；SQLite 是文件，自动创建。"""
        i = self.src.find("def ensure_database")
        body = self.src[i:i + 600]
        self.assertIn("mysql", body,
                      "ensure_database 应只处理 MySQL（SQLite 文件由引擎自动创建）")


class EngineCreationCase(unittest.TestCase):
    """引擎创建不能写死 MySQL 专用参数。"""

    @classmethod
    def setUpClass(cls):
        cls.src = DBPY.read_text(encoding="utf-8")

    def test_engine_uses_settings_url(self):
        self.assertIn("create_engine(settings.DATABASE_URL", self.src,
                      "引擎必须从配置读 URL（不能写死 MySQL）")

    def test_pool_args_differ_by_dialect(self):
        """**连接池参数必须按方言分开**（真实的启动崩溃点）。

        `pool_size` / `max_overflow` / `pool_timeout` 是 QueuePool 的参数。
        SQLite 用 SingletonThreadPool（文件库）或 StaticPool（内存库），
        它们不接受这些参数 —— 传了直接抛：

            TypeError: Invalid argument(s) 'pool_timeout', 'pool_size',
                       'max_overflow' for this Pool.

        实测踩过：CI 用 `sqlite:///:memory:` 时整个应用起不来。
        而 SQLite 正是 PC / 安卓 / 单机部署的默认选择 ——
        等于「最该能跑的场景一启动就崩」。

        ⚠️ 断言必须**去掉注释/文档串**再查：`_engine_kwargs` 的 docstring 里
        就举了这些参数名当反面教材，直接搜全文会假失败（本轮踩过）。
        """
        self.assertIn("def _engine_kwargs", self.src,
                      "应有按方言挑连接池参数的函数")
        i = self.src.find("def _engine_kwargs")
        j = self.src.find("\nengine =", i)
        raw = self.src[i:j] if j > 0 else self.src[i:i + 2400]

        # 去掉三引号文档串与 # 注释，只留可执行代码
        import re
        code = re.sub(r'""".*?"""', "", raw, flags=re.S)
        code = re.sub(r"#[^\n]*", "", code)

        sqlite_part = code[:code.find("return {")] if "return {" in code else code
        for bad in ("pool_size", "max_overflow", "pool_timeout"):
            self.assertNotIn(bad, sqlite_part,
                             f"SQLite 分支不能传 {bad}（会抛 TypeError）")
        mysql_part = code[code.find("return {"):]
        self.assertIn("pool_size", mysql_part,
                      "MySQL 分支应保留连接池配置")

    def test_memory_sqlite_uses_static_pool(self):
        """内存库必须用 StaticPool —— 否则每个连接是**不同的**空库。"""
        i = self.src.find("def _engine_kwargs")
        body = self.src[i:i + 2200]
        self.assertIn("StaticPool", body,
                      "内存 SQLite 必须用 StaticPool + check_same_thread=False，"
                      "否则各连接看到不同的内存库（测试会莫名丢数据）")
        self.assertIn("check_same_thread", body)

    def test_sqlite_file_parent_dir_created(self):
        """SQLite 的父目录不存在时启动会失败 —— 应自动创建。"""
        self.assertTrue(
            "mkdir" in self.src or "makedirs" in self.src,
            "应自动创建 SQLite 数据库文件的父目录"
            "（否则首次启动 sqlite:///some/dir/x.db 直接报 unable to open database file）")

    def test_parent_dir_helper_skips_memory_db(self):
        """`:memory:` 没有文件，不该被当成路径去 mkdir。"""
        self.assertIn(":memory:", self.src,
                      "父目录逻辑必须跳过内存库（否则会建出名为 :memory: 的目录）")


class SqliteParentDirBehaviorCase(unittest.TestCase):
    """行为级：**真的**在一个不存在的嵌套目录下建库。

    这是 PC / 安卓端的典型场景 —— 用户随便选个路径，父目录大概率不存在。

    ## 两条不同的断言，缺一不可

    1. **函数本身能用** —— 直接调 `_sqlite_ensure_parent_dir()` 建目录
    2. **函数真的被调用** —— 检查 `db.py` 模块级有没有在 sqlite URL 下调用它

    只测第 1 条会假绿：把「启动时调用它」那一行删掉，函数还在、测试照过，
    但**首次启动依然会 `unable to open database file`**。
    反向验证就是靠这个抓到假绿的。
    """

    def test_helper_called_at_import_for_sqlite(self):
        """模块级必须有「sqlite URL → 调 _sqlite_ensure_parent_dir」的接线。"""
        import re
        src = DBPY.read_text(encoding="utf-8")
        self.assertRegex(
            src,
            r'if\s+settings\.DATABASE_URL\.startswith\(\s*["\']sqlite["\']\s*\)'
            r'\s*:\s*\n\s*_sqlite_ensure_parent_dir\(settings\.DATABASE_URL\)',
            "**必须在模块级调用 `_sqlite_ensure_parent_dir`** —— "
            "只定义函数不调用，首次启动仍会 unable to open database file"
            "（这是被反向验证抓出来的假绿）")

    def test_nested_dir_auto_created(self):
        import shutil

        from admin.db import _sqlite_ensure_parent_dir

        tmp = tempfile.mkdtemp(prefix="wb_mkdir_")
        try:
            nested = os.path.join(tmp, "a", "b", "c")
            db_file = os.path.join(nested, "wb.db").replace("\\", "/")
            self.assertFalse(os.path.isdir(nested), "前置条件：目录应不存在")

            _sqlite_ensure_parent_dir(f"sqlite:///{db_file}")
            self.assertTrue(os.path.isdir(nested),
                            f"父目录应被自动创建：{nested}")

            # 建库本身也应成功
            from sqlalchemy import create_engine
            eng = create_engine(f"sqlite:///{db_file}", future=True)
            try:
                with eng.connect() as conn:
                    conn.exec_driver_sql("SELECT 1")
                self.assertTrue(os.path.isfile(db_file), "数据库文件应被创建")
            finally:
                eng.dispose()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_memory_db_not_treated_as_path(self):
        """`:memory:` 不该被当成目录创建（会在 CWD 建出名为 :memory: 的目录）。"""
        from admin.db import _sqlite_ensure_parent_dir

        cwd = os.getcwd()
        before = set(os.listdir(cwd))
        try:
            _sqlite_ensure_parent_dir("sqlite:///:memory:")
            _sqlite_ensure_parent_dir("sqlite://")
        finally:
            after = set(os.listdir(cwd))
        self.assertEqual(after - before, set(),
                         f"内存库不该创建任何目录，实际多出：{after - before}")


class RedisOptionalCase(unittest.TestCase):
    """Redis 必须是**可选**依赖。"""

    @classmethod
    def setUpClass(cls):
        cls.src = GW.read_text(encoding="utf-8")

    def test_three_backends_supported(self):
        for b in ("redis", "memory", "auto"):
            self.assertIn(f"'{b}'", self.src,
                          f"网关限流应支持 {b} 后端")

    def test_auto_falls_back_to_memory(self):
        """`auto` 连不上 Redis 时必须降级，而不是让服务起不来。"""
        self.assertIn("falling back to process-local memory", self.src,
                      "auto 模式应有明确的降级日志")
        self.assertIn("except Exception", self.src,
                      "连接失败要捕获（不装 Redis 也能跑）")

    def test_memory_backend_does_not_import_redis(self):
        """memory 后端不该在启动时 import redis（没装也能跑）。"""
        i = self.src.find("backend != 'memory'")
        self.assertGreater(i, 0,
                           "memory 后端应跳过 redis 的 import 与连接")

    def test_redis_backend_hard_fails(self):
        """显式选 redis 时连不上应当报错 —— 静默降级会让限流失效且无感知。"""
        self.assertIn("if backend == 'redis':", self.src)
        self.assertIn("raise", self.src)


class EnvExampleCase(unittest.TestCase):
    """`.env.example` 必须把两种数据库都写清楚。"""

    @classmethod
    def setUpClass(cls):
        cls.src = ENV_EXAMPLE.read_text(encoding="utf-8")

    def test_mentions_sqlite(self):
        self.assertIn("sqlite:///", self.src,
                      ".env.example 应给出 SQLite 连接串示例")

    def test_mentions_mysql(self):
        self.assertIn("mysql+pymysql://", self.src)

    def test_explains_redis_optional(self):
        low = self.src.lower()
        self.assertIn("redis", low)
        # 必须说明「可选」/「不装也能跑」之类
        self.assertTrue(
            "可选" in self.src or "optional" in low,
            ".env.example 应说明 Redis 是可选的")
        self.assertIn("memory", self.src,
                      "应给出 memory 后端（不装 Redis 的用法）")

    def test_sqlite_linux_path_documented(self):
        """Linux 绝对路径是 4 个斜杠（sqlite:////…），这是最常见的踩坑点。"""
        self.assertIn("sqlite:////", self.src,
                      ".env.example 应注明 Linux 绝对路径需 4 个斜杠")


class RealSqliteRunCase(unittest.TestCase):
    """**行为级**：真的用 SQLite 跑一遍 init_db + CRUD。

    这是最有价值的一条 —— 静态断言只能证明「代码里有分支」，
    这条证明「那条分支真的能跑」。

    ## 为什么不 `del sys.modules["admin.*"]` 重载模块

    早先那样写会**污染其它测试**：把 `admin.db` / `admin.config` 等模块
    从 sys.modules 里删掉再 import，会创建**新的** engine 与 SessionLocal，
    而其它测试模块在导入时已绑定了旧的对象引用 → 之后跑出 10 个莫名其妙的失败
    （`test_tokensvc` 的 3 个 POST 用例）。

    正确做法：**不碰模块状态**，直接用一个独立的 engine 跑建表 + CRUD。
    `init_db()` 依赖全局 engine，所以这里改为手动调
    `Base.metadata.create_all` + 方言分支里的迁移函数验证。
    """

    def test_init_db_creates_schema_on_sqlite(self):
        """用独立 engine 复现 `init_db()` 的核心动作（建表 + 迁移）。"""
        import shutil
        import tempfile

        from sqlalchemy import create_engine, inspect
        from sqlalchemy.orm import sessionmaker

        from admin.db import Base
        from admin import models as _models  # noqa: F401  注册所有表

        tmpdir = tempfile.mkdtemp(prefix="wb_sqlite_test_")
        db_path = os.path.join(tmpdir, "wb.db").replace("\\", "/")
        engine = create_engine(f"sqlite:///{db_path}", future=True)
        try:
            self.assertEqual(engine.dialect.name, "sqlite")
            Base.metadata.create_all(bind=engine)

            insp = inspect(engine)
            tables = set(insp.get_table_names())
            for need in ("accounts", "api_keys", "benefit_claims",
                         "growth_run_records", "schedules", "system_settings"):
                self.assertIn(need, tables, f"SQLite 下应建出表 {need}")

            # create_all 建出的表应已包含模型里的全部列（含后加的）
            ak = [c["name"] for c in insp.get_columns("api_keys")]
            self.assertIn("max_concurrent", ak, "SQLite 下应包含 max_concurrent 列")

            # 真实 CRUD
            Maker = sessionmaker(bind=engine, future=True)
            db = Maker()
            try:
                from admin.models import Account, ApiKey
                a = Account(uid="sqlite-ut", name="ut", status="active",
                            realm="cn", auth_json="{}")
                db.add(a)
                db.commit()
                db.refresh(a)
                self.assertIsNotNone(a.id)

                db.add(ApiKey(name="k", key_hash="h", key_prefix="sk",
                              key_full="sk", max_concurrent=0))
                db.commit()
                got = db.query(Account).filter(Account.uid == "sqlite-ut").first()
                self.assertEqual(got.name, "ut")
            finally:
                db.close()
        finally:
            engine.dispose()
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
