# 工程基线：测试 / 反向验证 / CI / 打包

> 这份文档记录**几条必须守住的工程基线**。它们不是风格偏好，
> 而是「踩过坑之后用测试固化下来的规则」。
>
> 新接手的人先看这份，能避免重复踩我们已经踩过的坑。

---

## 1. 当前基线数字

| 项 | 数值 |
|---|---|
| 测试总数 | **910 通过**（+7 环境条件跳过） |
| 测试文件 | 53 个 |
| 反向验证 mutation | **39 条**（全部有效） |
| CI 工作流 | 3 个（tests / desktop-release / android-release） |
| 代码规模 | `admin/` 约 25000 行 + `converter.py` 1682 行 |

**这些数字不是目标，是底线** —— 数量减少意味着覆盖变差。

---

## 2. 测试纪律

### 2.1 绝不真实触达上游

**整套测试零真实上游请求**（已审计：所有 HTTP 调用都被 mock 或指向本机）。

为什么这条排第一位：测试账号是**真实账号**，误发请求会消耗额度甚至触发风控。

```bash
# 自动化场景必须开（连只读也挡）
ADMIN_UPSTREAM_READONLY_ALL=1
```

详见 [security.md §3](security.md#3-上游护栏测试期防止误伤账号)。

### 2.2 反向验证（关键）

**正向测试只能证明「代码现在是对的」，不能证明「测试在测东西」。**

本项目已经踩过 **9 次假绿** —— 测试通过，但把实现改坏了它照样通过：

| 假绿形态 | 例子 |
|---|---|
| 只测纯函数，没测调用 | 把 `allow()` 里的并发判定删掉，测试照过 |
| 断言写成了查字符串 | 查「源码里有 `createPortal`」，而 `import` 那行就满足 |
| mock 没真正制造竞态 | CAS 测试用无条件 UPDATE 也能过 |
| 「永不过期」不看 expires | TTL 测试即使不检查过期也能过 |
| 搜全文匹配到自己 | 断言「不应依赖 X」，结果匹配到了函数定义本身 |
| 断言「不该测新面板」 | 把「没跟上改动」当成正确 |

**只有「把实现故意改坏，看测试会不会红」能发现它们。**

### 用法

```bash
python scripts/verify_tests.py            # 跑全部 39 条
python scripts/verify_tests.py --list     # 看清单
python scripts/verify_tests.py --only lease
python scripts/verify_tests.py --check    # CI 模式（有假绿 → 退出码 1）
```

### 原理

```
注入 bug → pytest 必须红 → 还原 → pytest 必须绿
```

三个关键设计：

1. **锚点必须唯一** —— `old` 出现次数 ≠ 1 就报错（防止改错地方）
2. **`finally` 强制还原** —— 进程被 kill 也不留坏代码
3. **自动注入 `ADMIN_UPSTREAM_READONLY_ALL=1`** —— 物理上碰不到上游

### 加新 mutation

改了一个不变量就加一条：

```python
Mutation(
    name="lease-cas",
    file="admin/growth_lease.py",
    old="...原代码...",
    new="...改坏后的代码...",
    tests=["tests/test_growth_lease.py::LeaseCasCase"],
    why="去掉 CAS → 并发时两个进程都以为抢到了 → 同一账号被跑两遍",
    tags=["lease", "safety"],
)
```

`why` **必填** —— 说明它防的是什么退化。没有 `why` 的 mutation
迟早会被当成「过时的测试」删掉。

---

## 3. CI

三个工作流，职责分开：

| 工作流 | 触发 | 内容 |
|---|---|---|
| **`tests.yml`** | 每次 push / PR | 全量 pytest → 反向验证 `--check` |
| `desktop-release.yml` | 打 `v*` tag | 桌面端测试（43 项）+ 构建 EXE |
| `android-release.yml` | 打 `v*` tag / PR 改 android | 构建 APK + **产物解剖验证** |

### CI 环境刻意「零外部依赖」

```yaml
ADMIN_DATABASE_URL: 'sqlite:///:memory:'
ADMIN_GATEWAY_LIMIT_BACKEND: 'memory'
```

**不装 MySQL、不装 Redis。** 这既让 CI 快（38s vs 75s），
也顺手验证了「零外部依赖形态」确实能跑。

> 这个决定直接抓出一个真 bug：`create_engine` 无条件传
> `pool_size`/`max_overflow`/`pool_timeout`，而 SQLite 用的是
> `SingletonThreadPool` / `StaticPool`，不接受这些参数 →
> **PC/安卓/单机部署的默认形态一启动就崩**。

### 组合式 CI 的价值

本轮 CI 首次真正跑起来后，一共暴露出 **7 个只有 CI 才出现的缺陷**：

| # | 缺陷 | 为什么本地发现不了 |
|---|---|---|
| 1 | SQLite 内存库启动崩溃 | 本地用文件库 |
| 2 | `.gitignore` 的 `logs/` 吞掉日志页面 | 本地文件在磁盘上 |
| 3 | APK 排除了 `server.py` 会 import 的模块 | 本地直接跑源码 |
| 4 | 兜底版本两版都是 5.5.6 | 本地装了客户端，走探测路径 |
| 5 | 安卓自检要求未打包的 `/panel/` | 本地早就 build 过 |
| 6 | pip 源写死国内镜像 | 本地在国内 |
| 7 | 签名用系统 TEMP 被 ACL 拦 | 本地 TEMP ACL 正常 |

**共同点：都依赖「开发机的某个特定状态」。** CI 是从干净 clone 开始的，
所以能照出这些差异。

---

## 4. 打包

### 共同原则：构建 success ≠ 产物可用

Gradle / PyInstaller 退出码 0 只说明「编译过了」，
**不代表产物里真的有我们的代码**。

所以两端都有「解剖产物」的验证：

#### 桌面端

* 冒烟测试（`desktop/app.py::_smoke_test`）在真实 PyWebView 里打开页面
* 双模式：旧面板查 `#app`，新面板查 React 已挂载 + 文本 >= 60 字符
* **不要求登录** —— 空数据目录会停在登录页，那是正常的

#### 安卓端

`tests/test_apk_content.py` 把 APK 当 zip 解剖：

```
assets/chaquopy/app.imy  ← Chaquopy 的源码包（.imy 就是 zip）
  ✅ admin/server.pyc 及全部路由
  ✅ converter.pyc
  ✅ 147 条错误码数据 JSON
  ✅ 无凭证 / 无数据库 / 无签名密钥
```

> 注意：Chaquopy **不把 `.py` 原样放进 APK**，而是编译成 `.pyc`
> 打进 `.imy`。按 `.py` 文件名在 zip 里搜会搜不到 —— 我曾因此误判
> 「代码没打包」，实际是检查方法错了。

### 版本号单一来源

`desktop/version.json` 是**双端共享**的版本唯一来源。CI 会校验
tag 与它一致（`v1.0.0` ↔ `"version": "1.0.0"`）。

### 签名

* 桌面端：SSH 密钥签名（`.sig` + `.sha256`）
* 安卓端：Android keystore，**必须缓存**（否则用户无法覆盖升级）

两者都要注意：**临时目录不能放系统 TEMP**。
Windows runner 的 `%TEMP%` 带 `\OWNER RIGHTS` ACL，
`ssh-keygen -Y sign` 会直接拒绝。详见 `sign_release._work_dir()`。

---

## 5. 几个「已知会骗人」的地方

这些是排查时最容易浪费时间的点，值得单独列出来。

### 5.1 PowerShell `Start-Process` 会丢环境变量

```powershell
# ❌ 环境变量全丢
Start-Process python -ArgumentList "main.py"

# ✅ 写成 .cmd 文件，在里面 set 再运行
```

### 5.2 `.gitignore` 的裸目录名匹配任意深度

```
logs/            ← 匹配任意层级的 logs/ 目录（会误伤 web/app/xxx/logs/）
/logs/           ← 只匹配根目录 ✅
android/app/build/  ← 有路径前缀，指向明确，安全
```

有测试守着：`test_gitignore_source_safety.py`。

### 5.3 i18n 不能用带点的扁平 key

```json
{"benefits": {"st": {"unknown": "..."}}}   // ✅
{"benefits.st.unknown": "..."}             // ❌ 界面直接显示 key 名
```

查表按 `split('.')` 逐层下钻，扁平 key 永远匹配不到。

### 5.4 PyWebView 的 js_api 回调在导航时销毁

`evaluate_js` 可能抛异常，返回 Promise 的桥调用**永远不会 settle**。
必须 `Promise.race` 加超时，并且每次调用单独 try/catch。

### 5.5 静态导出会剥掉 query 参数

`?desktop=1` 在 `boot.html → /panel/login/` 的重定向中丢失。
判断「是不是桌面端」要靠 bridge 注入的全局对象是否存在。

### 5.6 `pytest` 子进程读磁盘，不是内存

反向验证工具跑 pytest 是**子进程**，它读的是文件当前的内容。
改文件后不 `fsync`/正确关闭会读到旧内容。

---

## 6. 提交前自检清单

```bash
# 1. 全量测试
python -m pytest tests/ -q

# 2. 反向验证（改了不变量才需要跑全量，否则 --only 相关标签）
python scripts/verify_tests.py --check

# 3. 敏感信息扫描
git diff --cached | Select-String 'password|token|secret|13306|wb2api_dev'

# 4. 启动自检（改了 import 链 / 打包相关才需要）
python scripts/check_runtime.py
```

### 绝不做的事

* ❌ 提交真实账号 uid / email / token
* ❌ `git merge upstream/master`（上游零 realm 感知，见 [realm.md §7](realm.md#7-为什么不能-git-merge-upstreammaster)）
* ❌ 在测试里发真实上游请求
* ❌ 为了让 CI 变绿而放宽断言（应该 skip 并说明原因）

---

## 7. 相关文档

| 主题 | 文档 |
|---|---|
| realm 双域隔离 | [realm.md](realm.md) |
| 密钥 / 入站 / 上游护栏 | [security.md](security.md) |
| 面板架构 / i18n / 构建 | [panel.md](panel.md) |
| 使用与运维 | [usage.md](usage.md) |
| 移动端 / 单机部署 | [MOBILE-AND-SINGLE-NODE.md](MOBILE-AND-SINGLE-NODE.md) |
| 本地开发环境 | [LOCAL-DEV-GUIDE.md](LOCAL-DEV-GUIDE.md) |
| 上游能力吸收 | [UPSTREAM-ABSORB.md](UPSTREAM-ABSORB.md) |
