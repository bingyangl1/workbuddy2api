# 上游更新吸收评估

> 更新时间：2026-10-17
> **上游**：`xiaofan6ya/workbuddy2api` —— Python，MIT，★121
> **我们的 fork 关系**（GitHub API 的 `parent`/`source` 字段，决定性证据）：
> ```
> bingyangl1/workbuddy2api:  fork=True  parent=xiaofan6ya/workbuddy2api  source=xiaofan6ya/workbuddy2api
> ```
> 我们的仓库创建于 **2026-10-12**，上游创建于 2026-09-02。
> 所以「上游更新」= 2026-10-12 之后的提交，共 **25 个**。

---

## 一、更正一处历史错误（重要）

本文件的第一版是基于**错误的上游**写的。教训：

README 里写着「本仓库是 `Sliverkiss/workbuddy2api` 的演进 fork」，我照着这句话去找上游 →
404（该仓库已删库）→ 又去找了一个 Go 语言的社区延续仓库。

**正确的做法是直接查我们仓库的 GitHub API `parent` 字段**（GitHub 自动记录 fork 来源），
一步就能确定是 `xiaofan6ya/workbuddy2api`。**不要从文档描述反推事实。**

> 备注：`Sliverkiss/workbuddy2api` 确实已删库（三重确认：API 404 + 网页 404），
> 它是**更早的直系祖先**（`xiaofan6ya` 也是从它演进/重写而来的），
> 所以 README/LICENSE 里保留它的署名仍然正确，但**它不该作为「上游更新」的来源**。

---

## 二、上游 fork 后的 25 个提交（2026-10-12 起）

> **状态图例**：✅ 已吸收 / ✅ 我们已有 / ⚠️ 待评估 / 🔵 可吸收（低风险小功能）/ ⛔ 不吸收

| SHA | 日期 | 内容 | 与我们关系 |
|---|---|---|---|
| `8d1ee4cd` | 10-16 | 安卓启动报错修复 + 双端打包兼容检查 | ⛔ 无安卓形态 |
| `eb0b1a16` | 10-16 | **1.0.4 每日权益领取** + 批量刷新优化 | ⚠️ 待评估（新功能块） |
| `98e5a930` | 10-16 | 每日计划统一发现并领取权益（重构） | ⚠️ 待评估（新功能块） |
| `c8d816d2` | 10-16 | **领取 DeepSeek 补偿** + 加固批量操作 | ⚠️ 待评估（新功能块） |
| `f3b225c9` | 10-16 | docs 整理 | — |
| `ae9e72af` | 10-16 | 1.0.3 客户端 + 加固账号管理 | 部分重叠 |
| `50c1f906` | 10-15 | **1.0.2 代理调度** + 客户端改进（`growth_center`/`growth_plan`/`maintenance`/`health`） | ⚠️ 待评估（新功能块） |
| `7bed6e3a` | 10-13 | **桌面端系统托盘**（托盘菜单/状态徽章/自启） | 🔵 可吸收 |
| `8ae5c2df` | 10-13 | **每 API Key 可配并发数**（`max_concurrent`） | 🔵 可吸收（我们有全局并发限制，缺 per-key 覆盖） |
| `d7c8eece` | 10-13 | 启动脚本脱离会话 | ✅ 我们已有（`start_daemon.bat`） |
| `1609e23c` | 10-13 | **harness 压缩不吞首轮输入** | ✅ **已吸收**（commit `12e4adc`）——我们中了同一个 bug |
| `06e1b464` | 10-13 | **修 11148 工具配对失败** | ✅ 我们已有（`validate_tool_pairing`） |
| `bd6ad90e` | 10-13 | **OpenAI 反代同步 harness 脱敏**（修 11128） | ✅ **已吸收**（commit `ded39f0`）——我们只有 CC 路径做了脱敏 |
| `dcf640fc` | 10-13 | 账号冷却 UI 一键解除（`POST /api/accounts/{id}/cooldown/clear`） | 🔵 可吸收 |
| `3bd17daa` | 10-13 | 修 Claude Code 渠道验证 11128（设备指纹预热 + 压缩词增强） | ✅ **已吸收**（commit `679aff5`）——预热我们早已有且更完整；诊断日志本次补上 |
| `b7a84df4` | 10-13 | 一键启停脚本 | ✅ 我们已有 |
| `62b9c2e6` | 10-13 | **API Key 绑定专属账号池**（`account_binding.py`） | ✅ 我们已有（账号分组隔离） |
| `12278a24` | 10-13 | 排队 onboarding 任务 + 修正积分与推理日志（`post_login.py`） | ⚠️ 待评估 |
| `a3c82d6c` | 10-13 | **支持加密的桌面凭据**（`credential_codec.py`） | ✅ **已吸收**（commit `e189a33`）——改成运行时现取密钥 |
| `35d7ce06` | 10-12 | 独立安卓 app + `mobile_stream`/`request_metrics`/`model_catalog` | 部分重叠（我们有 `model_catalog`） |
| `ce986e11` | 10-12 | Windows 桌面客户端 + 可选更新 + 持久端口 | ✅ 我们有（P5 更完整） |
| `0c982bf8` | 10-12 | 保留 Anthropic 图片 + 规范化 Zcode 工具选择 | ⚠️ 待评估 |
| `0821cecc` | 10-12 | **队列冷却按模型隔离** + 显示上游容量 | ✅ 我们已有（模型限流台账） |

---

## 二之二、已完成的上游吸收（本轮 4 项）

| 上游 | 我们的提交 | 我们原本的问题 | 修法（自行实现） |
|---|---|---|---|
| `1609e23c` | `12e4adc` | Claude Code 把提问与 `<system-reminder>` 写进同一条 user 消息时，压缩把**整条**换成摘要 → 提问被吞 → 首轮只回 "I'm ready to help..." | 新增 `_extract_user_payload()`：从「替换」改成**减法**（只剥注入、剩下什么留什么）；残留为空才用摘要兜底 |
| `bd6ad90e` | `ded39f0` | 脱敏只在 `/v1/messages` 做 → 「CC 能用、OpenAI 协议发不出去」（11128） | 抽出 `_desensitize_chat_body()` 统一入口，三端点接入；`responses` 的接入必须在投影**之后** |
| `3bd17daa` | `679aff5` | 11128 有**至少四种**成因、现象完全一样，排查靠一行行加 print | 新增 `_describe_outbound_headers()`：一行日志报全 `device_token` / 三个会话头 / 风控头 4 项 / body 特征，**缺哪项标 `**MISSING**`** |
| `a3c82d6c` | `e189a33` | 取 at-rest 密钥只能靠 env 或仓库根的逆向产物 → **别的用户 clone 后导入功能直接死** | 新增 `fetch_at_rest_secret_key()`：用 `ELECTRON_RUN_AS_NODE=1` 纯 Node 模式**当场向客户端要**，不落盘、不弹窗 |

**共同做法**（用户要求「参考上游实现的关键代码，我们自行实现属于我们的」）：
每个吸收项都**不复制上游代码**，而是按其思路、按本仓库结构自行实现，
并配上（1）测试（2）**反向验证**（注入 bug → 测试必须失败 → 还原 → 通过）。

| 项 | 测试数 | 反向验证 |
|---|---|---|
| harness 压缩 | 11 | ✅ |
| 三路径脱敏 | 8 | ✅ |
| 出站诊断 | 11 | ✅ |
| 运行时现取密钥 | 17 | ✅ |

---

## 三、模块级差异（同语言，可直接对照）

**上游有、我们没有（13 个）**：

| 模块 | 作用 | 优先级 |
|---|---|---|
| `account_binding.py` | API Key 绑定专属账号池 | ✅ 我们已有等价能力（`account_groups` + 密钥绑定组） |
| `benefits.py` | 每日权益（自动发现 + 领取） | ⚠️ P4 候选 |
| `compensation.py` | DeepSeek 补偿领取 | ⚠️ P4 候选 |
| `credential_codec.py` | **解加密的桌面凭据** | ⚠️ **重点**（新版客户端可能加密凭据） |
| `growth_center.py` / `growth_plan.py` | 成长中心/计划（我们的是 `growth_plan`？需核对） | ⚠️ 待评估 |
| `maintenance.py` | 维护模式 | 🔵 候选 |
| `mobile_stream.py` | 移动端 SSE 处理 | 无安卓，跳过 |
| `model_listing.py` | 模型清单（Magpie 兼容字段？） | ⚠️ 待评估 |
| `network.py` | 网络工具 | ⚠️ 待评估 |
| `post_login.py` | 登录后自动化（排队 onboarding） | ⚠️ 待评估 |
| `request_metrics.py` | 请求指标 | 🔵 候选 |
| `upstream_http.py` | HTTP 客户端封装 | ⚠️ 待评估 |

**我们有、上游没有（8 个）—— fork 独有，吸收时绝不能丢**：

| 模块 | 作用 |
|---|---|
| `realm.py` | **双域端点分派**（上游是单域） |
| `backup.py` | 备份快照 + 脱敏防线 |
| `layout.py` | 自定义布局（用户菜单） |
| `audit.py` | 操作审计 |
| `iputil.py` | 信任 IP 解析 |
| `tokensvc.py` | 只读管理 Token（wbt_） |
| `wb_crypto.py` | 客户端加密相关 |
| `stdio_utf8.py` | Windows 控制台 UTF-8 修正 |

---

## 四、吸收策略（同语言，可以做真正的 diff）

因为上游是 **Python** 且共享文件结构，这次可以**逐文件 diff**（而不是像上一版那样只能「能力点对照」）：

```bash
# 加上游（一次性）
git remote add upstream https://github.com/xiaofan6ya/workbuddy2api.git
git -c http.proxy= -c https.proxy= fetch upstream master --tags

# 看某文件的上游版本 vs 我们的版本
git diff HEAD upstream/master -- admin/pool.py
```

**但绝不能 `git merge`** —— 我们的 fork 已经大改（realm 双域贯穿所有路由、双面板、
`proxy.py` 加了大量 realm/风控逻辑）。正确做法：

1. **逐文件 diff**，挑「协议/字段级修正」与「独立新功能」
2. 每个吸收项单独提交，注明来源：`port(upstream@<sha>): …`
3. 吸收后跑全量测试 + 反向验证
4. **明确不吸收**的要记进本文件（避免反复评估）

---

## 五、待办

### ✅ 已完成

1. **`git fetch upstream`** 建立本地对照基线 —— 已做
   （`git remote add upstream https://github.com/xiaofan6ya/workbuddy2api.git`；
   分叉点 `ce986e1`；分叉后我们 78 个提交 / 上游 22 个）
2. **重点评估 4 项**（协议级、可能是真 bug）—— **全部完成，4 项我们都中招**：
   * `1609e23c` harness 压缩吞首轮输入 → ✅ 已修（`12e4adc`）
   * `bd6ad90e` OpenAI 反代 harness 脱敏 → ✅ 已修（`ded39f0`）
   * `3bd17daa` 设备指纹预热 + 诊断日志 → ✅ 诊断已补（`679aff5`）；预热我们早已有
   * `a3c82d6c` 加密桌面凭据 → ✅ 已改为运行时现取（`e189a33`）

### ⬜ 待办

3. **候选吸收 3 项** —— **2 项已完成**：
   * ✅ `8ae5c2df` 每 API Key 并发数 → `api_keys.max_concurrent`
     （NULL=跟随全局 / 0=不限 / N=上限）。痛点确实是全局闸被单个跑批客户端占满
   * ✅ `dcf640fc` 冷却一键解除 → `POST /api/accounts/{id}/cooldown/clear`
     （清 cool/breaker/degrade；**不清** `session_dead_fails` 与 `status`）
   * ⬜ `7bed6e3a` 桌面端系统托盘（属 P6 桌面 UI 范畴，未开始）
4. **新功能块**（需新增模块 + 面板页，工作量较大，待决策）：
   * ⬜ 每日权益领取（`eb0b1a16` / `98e5a930`）
   * ⬜ DeepSeek 补偿领取（`c8d816d2`）
   * ⬜ 成长中心（`50c1f906` 的 `growth_center.py` / `growth_plan.py`）
5. **小项待评估**：
   * ⬜ `0c982bf8` 保留 Anthropic 图片 + 规范化 Zcode 工具选择
   * ⬜ `12278a24` 排队 onboarding 任务（`post_login.py`）

### ✅ 本轮额外自查出的 fork 自身 bug（非上游项）

排查 realm 覆盖时顺带发现并修掉 4 个**我们自己的** realm 缺口
（上游没有这些问题，因为它是单域设计）：

| 位置 | 问题 | 影响 |
|---|---|---|
| `_candidate_dirs()` 显式覆盖 | 无视 realm 过滤 | **国际版带整套国内版身份出站** |
| `REALM_FALLBACKS` | 漏 `desktop_version` | 探测不到时 UA 版本段错（`workbuddy-ai/5.8.3`） |
| `/api/models/busy` 模型清单 | 取全表不分 realm | 国际版查国内版模型 → 上游报错 → 一屏 `unknown` |
| `/api/models/speed-test` | 出站 URL 写死 `settings.BACKEND` | 国际版账号打国内版端点 → 必然失败 |

---

## 六、参考上游时的注意事项

**绝不能 `git merge upstream/master`**。原因：

| 文件 | 上游在分叉后改动 | 我们在同一处的独有改动 |
|---|---|---|
| `admin/routers/proxy.py` | **+562 / -236** | realm 路由、`require_realm` 选号、风控头、`X-Conversation-ID`、缓存键、三路径脱敏 |
| `admin/scheduler.py` | +262 / -126 | realm 门控（global 跳过签到/成长） |
| `admin/routers/growth.py` | -420 | 已重构 |

上游那些改动**不知道我们已有 realm 双域**，merge 会直接破坏。

**正确做法**：逐文件 `git diff ce986e1 upstream/master -- <file>` 看差异 →
按本仓库结构自行实现 → 单独提交并在信息里注明 `port(upstream@<sha>)` →
跑全量测试 + **反向验证**。

**为什么必须反向验证**：这轮 4 项吸收里，有 1 项在实现过程中我自己引入了回归
（`_decrypt_info_if_needed` 里漏了一行 `derive_protector_key`，而 `except
Exception` 把它静默吞成「有密钥却解不开」）。是**既有测试断言了明文**才抓到的
—— 只看「没抛异常」的测试会假绿。

