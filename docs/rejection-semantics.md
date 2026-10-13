# 网关拒绝语义表（Phase 2 T0）

> 本文是 P2 所有新判定逻辑的**唯一语义基准**：每个拦截点落码前先在此对号，
> 测试断言按此表写。P1 已钉死的语义（403 realm 隔离）不动摇。

## 全表

| 状态码 | 语义 | 触发条件（P2 落地后） | type | code |
|-------|------|---------------------|------|------|
| **401** | 认证失败——「不知道你是谁」 | 缺 API Key / Key 不存在 | `auth_error` | `missing_api_key` / `invalid_api_key` |
| **400** | 请求与密钥配置不匹配——「知道你是谁，但这次请求不该这样发」 | IP 不在密钥白名单；IP 数超 max_ips；限定 realm 的密钥请求必须指定 model；启用白名单的密钥请求缺 model；模型不在密钥白名单 | `invalid_request_error` | `ip_not_allowed` / `too_many_ips` / `realm_mismatch` / `model_not_allowed` |
| **402** | 配额耗尽（积分或 token，任一） | `credit_used >= credit_limit`（非 unlimited）；`used_tokens >= token_quota` | `quota_exceeded` | `quota_exceeded` / `token_quota_exceeded` |
| **403** | 越权——「你的密钥没这个权限」 | Key 已停用/吊销；Key 已过期；realm 隔离（P1）；模型不在绑定的分组内（P1） | `key_disabled` / `key_expired` / `realm_forbidden` / `model_not_in_group` | 同 type |
| **429** | 限流 | 网关速率闸门（现状 `gateway_limits`）；未来按密钥限流 | `rate_limit_exceeded` | — |
| **503** | 号池/上游不可用 | 无可用账号（含 realm 无号、分组空）；上游不可达 | `no_account` / `upstream` | 同 type |

## 为什么 400 和 403 必须分开（manager 实测教训）

一批客户端（DeepSeek Harness 等）把 **401/403 整段吞掉**，只显示本地化的「API 密钥
无效」。于是「密钥限定国内版、却去调国际版模型」这种**配置问题**被显示成密钥坏了，
用户反复新建密钥——而新建的每一把还是同样配置，永远好不了。

分流的判断标准是「**该怪谁**」：

- **怪这次请求**（发错了模型名 / 来源 IP / 缺参数）→ `400`：客户端会把报文
  原样显示，用户一眼看到该改什么。
- **怪凭据本身**（停用 / 过期）→ `403`：显示「密钥无效」是贴切的。
- **怪用量**（配额用尽）→ `402`（本引擎现状；OpenAI 同语义用 429+
  insufficient_quota，manager 用 429——**不改引擎现有 402**，避免存量客户端
  适配失效）。
- **怪服务**（号池空 / 上游挂）→ `503`：客户端会重试，不该引导用户改配置。

## 判定顺序（T3 `validate_key` 落码基准）

顺序有意义——先判「身份」再判「权限」再判「用量」再判「匹配」，拒绝原因
永远指向**第一个**命中的环节，不给探测者多余信号：

```
1. 停用(status)      → 403 key_disabled
2. 过期(expires_at)  → 403 key_expired
3. 积分配额          → 402 quota_exceeded
4. Token 配额        → 402 token_quota_exceeded
5. IP 白名单         → 400 ip_not_allowed
6. max_ips           → 400 too_many_ips
7. realm 隔离        → 403 realm_forbidden（P1 既有，判「映射后模型名」——T6）
8. 模型组白名单      → 403 model_not_in_group（P1 既有）
```

`/v1/models`（模型发现，不带 model）**不判** 7/8（manager 同口径）：它没有版本
可言，拦了会让限定密钥连「我有哪些模型」都问不到；真正的隔离由调用时的模型名把关。

## 特别裁决

- **`/v1/messages/count_tokens`**：本地估算不耗额度 → 不判配额（3/4 跳过）；
  但判停用/过期/IP 规则（1/2/5/6）。
- **`/v1/usage`**：只认 Key 存在——耗尽/过期/停用**照样可查**（用户得知道自己
  为什么被拒）。
- **`/v1/models`**：判停用/过期（403），不判配额与 IP 白名单外的匹配项
  （列表本身不产生用量）。
- **配额边界**：`credit_limit=0` 且非 unlimited = **无限**（引擎现状，
  unlimited 才是开关）；`token_quota=0` = 不限（manager 口径，0 不可能是有效配额）。
