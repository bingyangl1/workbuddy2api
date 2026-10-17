# 移动端 / 单机部署（SQLite 模式）

本文说明**不装任何外部组件**跑起来的形态：PC 桌面版、手机（安卓）、
单机 Docker。共同点是 **SQLite + 进程内存限流**，不需要 MySQL，也不需要 Redis。

---

## 1. 三种形态的依赖对照

| 形态 | 数据库 | 缓存 | 说明 |
|---|---|---|---|
| **Windows 桌面版（EXE）** | SQLite（自动） | memory | 双击即用，见 `desktop/README.md` |
| **安卓 App** | SQLite（自动） | memory | 需要 Android 构建工具链，见 §5 |
| **单机 Docker / 源码** | **SQLite 或 MySQL**（可选） | **memory 或 Redis**（可选） | 见 §2 / §3 |
| **多实例部署** | MySQL（必须） | Redis（必须） | SQLite 不能多副本共享 |

> 多实例为什么不能共享 SQLite：它是**单文件**数据库，多个进程同时写会
> 互相锁死（虽然有 WAL，但那是单机多线程场景，不是跨机器）。

---

## 2. 只用 SQLite（零外部依赖）

```bash
# Windows
ADMIN_DATABASE_URL=sqlite:///F:/wb2api/data/workbuddy.db

# Linux / macOS（注意是 4 个斜杠 = 绝对路径）
ADMIN_DATABASE_URL=sqlite:////opt/workbuddy/data/workbuddy.db

# 相对当前工作目录
ADMIN_DATABASE_URL=sqlite:///./data/workbuddy.db
```

**不需要**先建库、建表、建目录 —— 首次启动会全部自动创建
（包括数据库文件的父目录）。

启动方式与平时完全一样：

```bash
python main.py
```

### 已自动处理的事

| 项 | 值 | 为什么 |
|---|---|---|
| `journal_mode` | `WAL` | 让「写日志」不阻塞「查账号」 |
| `busy_timeout` | `30000` ms | 锁冲突时**等待**而不是立刻抛 `database is locked` |
| `synchronous` | `NORMAL` | WAL 下的合理取舍（性能与安全平衡） |
| 父目录 | 自动创建 | 否则 `sqlite:///a/b/c.db` 直接报 `unable to open database file` |

### 备份

两种方式：

1. **后台「快照备份」**（推荐）—— 服务不用停，走 SQLite 的一致性读
2. 直接拷贝 `.db` 文件 —— **必须先停服务**，否则可能拷到写了一半的文件
   （WAL 模式下还要一起拷 `-wal` / `-shm`，更麻烦，所以推荐方式 1）

---

## 3. 不装 Redis

```bash
ADMIN_GATEWAY_LIMIT_BACKEND=memory
```

三种后端：

| 值 | 行为 | 适用 |
|---|---|---|
| `memory` | 完全用进程内存，**不 import / 不连接 Redis** | 单机 / 手机 / 桌面 |
| `auto`（默认） | 能连 Redis 就用，连不上**自动降级**并打 warning | 不确定环境时 |
| `redis` | 强制用，连不上直接报错退出 | 多实例（必须共享计数） |

> `redis` 模式**刻意不静默降级** —— 静默降级会让限流失效且毫无感知，
> 而多实例下限流失效意味着实际配额被放大 N 倍。

### memory 后端的性能（实测）

```
20,000 次 allow() = 24.6ms  →  813,286 次/秒（单次 1.23 微秒）
```

手机端即使 10 QPS 也只用到 **0.005%**。瓶颈不会在限流上。

---

## 4. 手机端（安卓）的运行时约束

安卓 App 用的是与桌面版同一套后端代码，差别只在打包方式。已在代码层确认：

### SQLite 并发（真实验证）

```
WAL 模式生效（journal_mode = wal）
4 线程并发：200 次写 + 240 次读 → 0 失败，0.17s
usage_logs 3200 行 → 取最近 20 条 < 1ms，按账号 count < 1ms
```

这就是手机上真实会发生的场景：网关在记用量，同时后台页面在查账号。

### 移动端布局

新面板已自带响应式布局：

* 窄屏（`<lg`）自动收起侧边栏，改用顶栏汉堡按钮 + 全屏抽屉
* 抽屉 **portal 到 `<body>`** —— 这是必须的：它渲染在 `sticky` +
  `backdrop-blur` 的 header 内部时，`fixed inset-0` 的高度会退化成
  header 的高度（实测只有 52px，菜单全在视口外）

### 不需要的组件

手机上**不需要**也**不能**装：MySQL、Redis、Node.js、桌面端 SDK。
相关能力会自动降级：

* 设备风控令牌取不到 → 走既有的无设备令牌路径
* `client_profile` 探测不到客户端 → 用 `REALM_FALLBACKS` 里的官方版本号

---

## 5. 构建安卓 APK

> ⚠️ **本节描述的是目标形态，当前仓库尚未包含 Android 工程**
> （`android/` 目录不存在）。桌面端（Windows EXE）已完整可用。

需要的外部工具链（都不在本仓库内）：

| 工具 | 版本 | 用途 |
|---|---|---|
| JDK | 17 | Gradle / Android 编译 |
| Gradle | 8.11.1 | 构建 |
| Android SDK | 35 | 编译目标 |
| Python | 3.10 | Chaquopy 内嵌解释器 |

构建思路（与桌面版一致：把 Python 后端与静态产物打进包里）：

1. 用 Chaquopy 把 Python 3.10 + 依赖打进 APK（ARM64）
2. 静态面板产物（`web/out`）放进 assets
3. 启动时把资源解压到应用私有目录，用 SQLite 起服务
4. 原生 WebView 加载 `http://127.0.0.1:<port>/panel/`
5. 前台 Service 保持运行（否则切后台会被系统杀掉）

产物：`dist/android/WorkBuddy2API-<版本>-android-arm64.apk` + `.sha256`。

### 为什么本仓库暂时没有 Android 工程

当前开发机只有 JDK 与 adb，**缺 Android SDK 与 Gradle**，且首次构建要下载
数 GB 工具链与依赖。这部分应在有环境的机器或 CI 上完成（Gradle 缓存可复用）。

**代码层已就绪**：SQLite 后端、memory 限流、响应式面板全部可用且有测试守着
（见 `tests/test_mobile_runtime.py`）。缺的只是打包脚本与 Gradle 工程。

---

## 6. 从 SQLite 迁到 MySQL

两者表结构一致（同一套 SQLAlchemy 模型），迁移就是搬数据：

1. 后台「快照备份」导出一份 JSON
2. 改 `ADMIN_DATABASE_URL` 指向 MySQL
3. 启动一次（自动建表）
4. 后台「快照恢复」导入

反向（MySQL → SQLite）同理。

---

## 7. 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| `unable to open database file` | 旧版本没自动建父目录 | 升级到含 `_sqlite_ensure_parent_dir` 的版本；或手工建目录 |
| `database is locked` | 多实例共享同一个 SQLite 文件 | 改用 MySQL；或确保只有一个实例 |
| 限流不生效 / 配额被放大 | 多实例 + `memory` 后端 | 改用 Redis |
| 手机切后台后 API 断了 | 系统回收进程 | 允许「后台活动」/ 用前台通知服务 |
| 面板中文显示成方块 | 缺字体 | 见桌面版 README 的字体说明 |
