# 使用与运维总览

面向「已经跑起来了，接下来怎么用」的读者。安装步骤见主
[README §四](../README.md#四环境安装与项目运行)。

---

## 1. 三条使用路径

| 你想做什么 | 入口 | 说明 |
|---|---|---|
| **把 WorkBuddy 当 API 用** | `/v1` 端点 + API Key | 最常用。任何 OpenAI/Anthropic 兼容客户端都能接 |
| **管理多个账号** | 后台面板 | 账号池、密钥、分组、统计、定时任务 |
| **做逆向研究** | `/settings/app-source` | 浏览解包出来的客户端源码 |

---

## 2. 网关接入

### 端点

| 端点 | 兼容协议 |
|---|---|
| `POST /v1/chat/completions` | OpenAI Chat |
| `POST /v1/responses` | OpenAI Responses |
| `POST /v1/messages` | Anthropic Messages |
| `POST /v1/messages/count_tokens` | Anthropic |
| `GET /v1/models` | 模型列表 |
| `GET /v1/usage` | 用量查询 |

三种协议走**不同的适配器**：

* `anthropic_adapter.py`（498 行）—— Anthropic ↔ 内部格式
* `responses_adapter.py`（558 行）—— OpenAI Responses
* `responses_projection.py`（561 行）—— 响应投影

### 客户端配置

```bash
# OpenAI 兼容
export OPENAI_BASE_URL=http://127.0.0.1:8790/v1
export OPENAI_API_KEY=sk-wb-xxxxxxxx

# Anthropic 兼容
export ANTHROPIC_BASE_URL=http://127.0.0.1:8790
export ANTHROPIC_API_KEY=sk-wb-xxxxxxxx
```

### 模型名

支持两种写法：

* **真名**：`deepseek-v4.1-flash` 等上游模型 ID
* **别名**：后台 `settings → 模型映射` 里配的别名（`system_settings.model_map`）

区别在于「客户端原始请求名」会被记录（`requested_model` 字段），
方便排查「别名映射到哪去了」。

---

## 3. 定时任务

**6 种内置任务，5 个默认启用，1 个默认禁用。**

| 任务码 | 名称 | 默认 | 触达上游 |
|---|---|---|---|
| `refresh_balances` | 整点刷新平台总积分 | ✅ 启用 | 是（只读） |
| `refresh_credits` | 每 2 小时更新积分到期快照 | ✅ 启用 | 是（只读） |
| `sync_models` | 每日同步模型列表 | ✅ 启用 | 是（只读） |
| `daily_checkin` | 每日签到领取积分 | ✅ 启用 | **是（写）** |
| `refresh_growth_tasks` | 每日更新成长任务列表 | ✅ 启用 | 是（只读） |
| `run_growth_tasks` | 每日自动做成长任务 | ❌ **禁用** | **是（大量写）** |
| `keepalive_tokens` | 每日 token 保活刷新 | ✅ 启用 | 是（写） |

### 为什么 `run_growth_tasks` 默认禁用

它会给**全部账号**批量发任务请求。新装实例的用户没有预期，
开机就自动跑一遍是很糟糕的体验，也有风控风险。

**想用就手动开。** 默认值在两处硬编码为 `enabled=0`
（`seed_defaults()` 与 `ensure_growth_schedules()`），有测试守着。

### 执行审计

每次运行写一条 `GrowthRunRecord`：

* 哪个账号、什么任务、开始/结束时间、结果
* 来源（`schedule` / `manual`）
* 失败原因（脱敏）

查：`GET /api/growth/records`

### 跨进程租约

多个调度实例同时跑会**重复执行**同账号任务。用
`admin/growth_lease.py` 的 CAS 租约做互斥：

* 基于 `SystemSetting` 表实现（不引入 Redis）
* 默认 TTL 600 秒
* 被占用时该账号标记为 `skipped` 并说明原因

> 为什么需要：重复执行同一个账号的成长任务是**典型的风控触发场景**。

---

## 4. 限时权益 / 补偿（只能手动）

**这是本项目唯一一个「不给自动跑」的功能。**

| 入口 | 说明 |
|---|---|
| `/benefits` 页面 | 三步：选账号 → 查询可领 → 领取 |
| `POST /api/benefits/campaign` | 活动信息 |
| `GET /api/benefits/status` | 各账号状态（本地 DB） |
| `POST /api/benefits/query` | 查询上游可领情况（**打上游**） |
| `POST /api/benefits/claim` | 领取（**打上游，不可回滚**） |

### 四条硬约束

1. **不在 `TASK_CHOICES` 里** —— 无法被调度系统调用
2. **数据库唯一约束**（`account_id` + `campaign_key`）—— 重复领取挡在库层面
3. **领取前必须先查** —— 状态不是明确的 `available` 就拒绝
4. **失败不置 claimed** —— 一次网络抖动不该让账号永久失去机会

### 状态判定

`admin/benefits.py::classify()` 是纯函数，把上游返回分成：

| 状态 | 含义 | 能否领取 |
|---|---|---|
| `available` | 确实可领 | ✅ |
| `already` | 已经领过 | ❌ |
| `expired` | 已过期 | ❌ |
| `ineligible` | 不满足条件 | ❌ |
| `unknown` | **无法确定** | ❌ **永不领取** |
| `error` | 查询失败 | ❌ |

`unknown` 是最重要的一个：上游字段类型/语义不符预期时**宁可不动**。

---

## 5. 备份与恢复

### 两种方式

| 方式 | 服务需停？ | 适用 |
|---|---|---|
| **后台快照备份** | ❌ 不用 | 推荐。走一致性读 |
| 直接拷数据库文件 | ✅ 必须停 | 应急 |

**SQLite 特别注意**：WAL 模式下直接拷 `.db` 还要一起拷 `-wal` / `-shm`，
容易出错。所以推荐用快照备份。

### 脱敏

备份可选**脱敏导出**（`desensitize.py`，488 行）：

* 账号 token 打码
* 手机号打码
* 保留结构与关联关系，可用于排障/分享

---

## 6. 故障排查

### 请求全部失败

先看**错误分类**（面板「调用日志」或 `GET /api/logs`）：

| 现象 | 分类 | 处置 |
|---|---|---|
| 401 / 403 + 空体 | `waf` | 出口 IP 被拦。换 IP 或等 |
| 401 / 403 + 业务信封 | `session_dead` | 凭证失效。**重新登录导入** |
| 6004 / 6008 | `model_daily` | 该账号×该模型日额度用完。**换模型或换号** |
| 14003 | `model_rate` | 瞬时繁忙。**等几秒** |
| 积分包为空 | `hard_credit` | 次日 04:00 重置 |
| 连接超时 | `transport` | 网络问题，看底层原因 |

### 6004 与 6008 的区别

**这两个经常被混为一谈，但恢复时点不同：**

| 码 | 含义 | 恢复时点 |
|---|---|---|
| `6004` | 每日 **token** 上限 | 无官方时点 → 保守 24h |
| `6008` | 每日**请求数**上限 | 官方说明**次日零点** |

> 用 24h 处理 6008 会让账号白白多等几小时：20:00 触发的账号
> 次日 00:00 就该恢复，24h 会拖到再下一天 20:00。

两者都是「**该账号 × 该模型**」维度 —— 换号可用、同号换模型也可用。

### 账号被判「登录态失效」后怎么恢复

**重新登录拿新凭证，再导入。** 导入时会自动清除：

* `session_dead_fails` 归零
* `cool_kind='session_dead'` 清空
* 因 `session_dead` **自动**停用的账号恢复 `active`

> 但**手动停用**的账号不会被自动启用 —— 那是人的决定。
> 区分方式：自动停用一定伴随 `cool_kind='session_dead'`。

### 上游错误码解释

内置 **147 条**官方客户端的错误码中文解释
（`admin/data/upstream_error_messages.json`）。

当最终错误消息里带业务 code 时，会附上官方解释，例如：

```
模型已达今日额度；已尝试 2 个账号 / 3 个候选模型
（上游 6008：今日次数已用完，明日恢复）。请稍后重试…
```

未知 code 不会编造任何解释。

---

## 7. 运维要点

### 日志

* 应用日志：`logs/`（已 gitignore）
* 调用日志：数据库 `usage_logs` 表 + 面板「调用日志」页
* 审计日志：数据库 `audit_logs` 表 + 面板「审计」页

### 数据库选择

| 场景 | 选择 |
|---|---|
| 桌面端 / 安卓 / 单机 | **SQLite**（零外部依赖） |
| 多实例 / 大量账号 | **MySQL** |

详见 [MOBILE-AND-SINGLE-NODE.md](MOBILE-AND-SINGLE-NODE.md)。

### Redis 是否必需

**不必需。** 三种后端：

* `memory` —— 完全不连 Redis
* `auto`（默认）—— 能连就用，连不上降级并警告
* `redis` —— 强制，连不上直接失败（**刻意不静默降级**）

> 为什么 `redis` 模式不降级：多实例下限流若静默失效，
> 实际配额会被放大 N 倍，而用户毫无感知。

### 升级

* 桌面端 / 安卓：覆盖安装即可（保留账号与配置）
* 服务端：替换代码 → 重启（数据库自动迁移）

---

## 8. 已知限制

| 限制 | 说明 |
|---|---|
| SQLite 不能多实例共享 | 单文件数据库，多进程写会锁死 |
| 新面板需 Node 构建 | 移动端只打包旧面板 |
| 安卓 APK 未真机验证 | 结构已验证，实际启动待确认 |
| 成长任务不做自动调度 | 默认禁用，需手动开 |
