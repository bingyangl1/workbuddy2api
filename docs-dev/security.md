# 密钥运营面 · 入站治理 · 上游护栏

本文覆盖三件事：

1. **密钥（API Key）的完整维度** —— 一个 Key 能被约束到什么程度
2. **入站治理** —— 频次/并发/IP/配额的执行方式
3. **上游护栏** —— 测试期如何避免误伤账号（**你可能最关心的一节**）

---

## 1. API Key 的完整维度

`admin/models.py::ApiKey` 一共 **20 个可配置维度**。设计原则是
「**默认什么都不限，需要时才收紧**」——避免为了安全把正常用法一起掐死。

| 维度 | 字段 | 语义 |
|---|---|---|
| **配额** | `credit_limit` / `credit_used` | 积分上限与已用 |
| | `unlimited` | 完全无限（忽略积分限制） |
| | `token_quota` / `used_tokens` | Token 上限与已用 |
| **并发** | `max_concurrent` | 该 Key 的在途请求上限 |
| | | `NULL`=跟随全局，`0`=不限，`N`=上限 N |
| **限流** | `max_ips` | 允许的 IP 数量 |
| | `ip_allowlist` | 显式 IP/CIDR 白名单 |
| **时效** | `expires_at` | 到期时间（`NULL`=永不过期） |
| **归属** | `group_id` | 模型分组（限定可用模型） |
| | `account_group_id` | 账号分组（限定可用账号池） |
| | `realm` | **锁定该 Key 只能用某个版本的账号** |
| **状态** | `status` | 启用 / 停用 |

### `realm` 字段特别说明

Key 可以**锁定 realm**。锁定后：

* 该 Key 的请求只会被分到对应版本的账号
* 号池里没有该版本的可用账号时，返回**明确的原因**
  （不是笼统的「无可用账号」）

这个能力对「国内用户只用国内版」「海外用户只用国际版」的场景很关键。

### 密钥存储

* 库里存 `key_hash`（SHA-256），**不存明文**
* `key_prefix` 用于列表展示（如 `sk-wb-abc1...`）
* `key_full` 仅用于「后台回显」（需显式开启，见 §5）

---

## 2. 入站治理

三层，从粗到细：

```
① 全局频次/并发（GatewayAdmissionMiddleware）
        ↓
② 单 Key 频次 + 并发
        ↓
③ IP / 配额 / 到期检查
```

### ① 全局准入

`admin/gateway_limits.py::GatewayAdmissionMiddleware`：

* `ADMIN_GATEWAY_RATE_PER_MINUTE` —— 全局每分钟请求数
* `ADMIN_GATEWAY_MAX_CONCURRENT` —— 全局在途请求上限
* 后端由 `ADMIN_GATEWAY_LIMIT_BACKEND` 决定（`memory` / `auto` / `redis`）

### ② 单 Key 并发

关键实现细节：**判定必须发生在真正的入口**，不能只写个纯函数。

> 假绿教训：早期测试只测了「纯函数算得对」，但把 `allow()` 里的实际调用
> 删掉，测试照样通过。现在 `verify_tests.py` 有一条 mutation 专门守这个。

### ③ IP 治理

* `ADMIN_TRUST_PROXY` —— 是否信任 `X-Forwarded-For`
* `ADMIN_TRUSTED_PROXY_HOPS` / `ADMIN_TRUSTED_PROXY_CIDRS` —— 信任的代理层数/网段
* 不信任代理时用 `request.client.host`（真实 TCP 对端）

> 为什么要显式配：**无条件信任 `X-Forwarded-For` 等于没有 IP 限制** ——
> 任何客户端都能伪造。

---

## 3. 上游护栏（测试期防止误伤账号）

**这一节是本项目最独特的机制，也是安全使用的前提。**

### 3.1 为什么需要

很多操作会**真实触达上游**，其中一部分**不可回滚**：

* 领取权益 —— 上游每账号只能领一次，多领会被判异常
* 做成长任务 —— 大量写操作
* 测速 / 同步模型 —— 消耗额度

测试、截图、录屏时误触这些，可能让测试账号被风控。

### 3.2 两级护栏

```bash
# 第一级：挡「写」，允许只读查询（人工核验 UI 用）
ADMIN_UPSTREAM_READONLY=1

# 第二级：连只读查询也挡（自动化 / 反向验证 / CI 专用）
ADMIN_UPSTREAM_READONLY_ALL=1
```

对应两个判定函数：

| 函数 | 覆盖范围 | 对应开关 |
|---|---|---|
| `is_upstream_write(method, path)` | 改变上游状态或消耗额度的操作 | `READONLY` |
| `is_upstream_touch(method, path)` | **任何**触达上游的请求（含只读） | `READONLY_ALL` |

### 3.3 实际判定表

| 请求 | 写操作 | 触上游 | 说明 |
|---|---|---|---|
| `POST /api/benefits/claim` | ✅ | ✅ | 领取权益（不可回滚） |
| `POST /api/benefits/query` | — | ✅ | 查询可领（只读但打上游） |
| `GET /api/benefits/status` | — | — | 本地 DB |
| `POST /api/growth/run` | ✅ | ✅ | 做成长任务 |
| `POST /api/models/sync` | ✅ | ✅ | 从上游同步模型 |
| `GET /api/accounts` | — | — | 本地 DB |
| `POST /v1/chat/completions` | — | — | **网关业务流量，不挡** |

> 最后一行很重要：护栏**只针对管理面**，不影响网关正常转发。
> 否则一开护栏整个服务就废了。

### 3.4 为什么只读也要能挡住

「只读」不等于零风险：

* **频次高了同样计入上游调用**，成长/权益类活动对重复访问是有检测的
* 反向验证要**反复跑**（注入 bug → 跑测试 → 还原），任何真实调用都会被放大
* CI 里出现真实上游流量本身就是错的

所以自动化场景一律用 `READONLY_ALL=1`。`scripts/verify_tests.py` 会
**自动注入**这个变量作为兜底——即便工具本身出问题也物理上碰不到上游。

### 3.5 测试期的完整安全清单

```bash
# 1. 上游护栏（必须）
ADMIN_UPSTREAM_READONLY=1          # 人工核验
ADMIN_UPSTREAM_READONLY_ALL=1      # 自动化

# 2. 只用 1-2 个账号测试
#    —— 不要把全部账号拿去试

# 3. 低频
#    —— 不要连点、不要写循环反复打
```

已验证的事实：

* **整套测试零真实上游请求**（全 mock，已审计）
* 打开面板页面 **零接口调用**（懒加载）
* 点「领取」**只弹确认框，不发请求**（需二次确认）

---

## 4. 审计

`ADMIN_AUDIT_ALL_ACCESS=1` 时记录所有管理面访问（不只是写操作）。

审计写入 `AuditLog` 表，字段包括：`actor` / `action` / `target` / `detail` /
`ip` / `created_at`。

**审计短码按方向分开**：`enable_account` 与 `disable_account` 是两条
语义不同的记录，不能合成一条 `update_account`。

---

## 5. 后台密码与 token

| 项 | 实现 |
|---|---|
| 存储 | PBKDF2-HMAC-SHA256，10 万轮，**不存明文** |
| 登录 | `POST /api/login` → JWT |
| JWT 有效期 | `ADMIN_JWT_EXPIRE_HOURS`（默认按小时计） |
| 传递 | 请求头 `X-Admin-Token` |
| 前端存储 | `sessionStorage`（**不用 localStorage** —— 关标签页即失效） |
| 登录限流 | `LOGIN_MAX_ATTEMPTS` / `LOGIN_WINDOW_SECONDS` / `LOGIN_LOCK_SECONDS` |

### 桌面端免密登录

桌面版（EXE / APK）由本地启动器直接签发 token，**不需要用户填密码**。

安全性由三道约束保证（`desktop/app.py::_desktop_login_allowed`）：

1. 请求来自 **loopback**（127.0.0.1）
2. 端口匹配本次启动的实际端口
3. 路径前缀在白名单内（`/admin` 或 `/panel`）

外加一个**一次性 boot nonce**（`WB_DESKTOP_BOOT`），换新窗口即失效。

> 只校验域名/前缀是不够的 —— 必须同时校验来源与端口，
> 否则别的站点伪造一个 `/panel` 就能蹭免密。

---

## 6. 相关配置速查

```bash
# 上游护栏
ADMIN_UPSTREAM_READONLY=0             # 1 = 挡写操作
ADMIN_UPSTREAM_READONLY_ALL=0         # 1 = 连只读也挡

# 入站
ADMIN_GATEWAY_RATE_PER_MINUTE=600
ADMIN_GATEWAY_MAX_CONCURRENT=32
ADMIN_GATEWAY_LIMIT_BACKEND=auto      # memory / auto / redis

# 代理信任
ADMIN_TRUST_PROXY=0
ADMIN_TRUSTED_PROXY_HOPS=1
ADMIN_TRUSTED_PROXY_CIDRS=

# 审计
ADMIN_AUDIT_ALL_ACCESS=0

# 登录限流
ADMIN_LOGIN_MAX_ATTEMPTS=10
ADMIN_LOGIN_WINDOW_SECONDS=300
ADMIN_LOGIN_LOCK_SECONDS=900
```

完整清单以 `.env.example` 为准（共 66 项）。
