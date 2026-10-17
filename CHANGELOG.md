# Changelog

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 纪律：
每个发布段落按 **新增 / 修复 / 改进 / 安全** 归类（吸收 manager 的发版纪律）。
本 fork 自 v0 起记录。版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增
- **每 API Key 并发上限**（吸收上游 `8ae5c2df`，自行实现）：`api_keys.max_concurrent` 三态（`NULL`=跟随全局 / `0`=不限 / `N`=最多 N 路）。全局 `GATEWAY_MAX_CONCURRENT` 是**所有密钥共享**的总闸 —— 一个跑批客户端（并发 5 路）会把默认 4 路额度占满，其他密钥无故排队；per-key 覆盖让重度/轻度用户互不干扰。用 `NULL` 而非 `0` 表示「未设置」，才能区分「听全局的」与「显式不限」（混用会让全局闸门形同虚设）。
- **账号冷却一键解除**（吸收上游 `dcf640fc`，自行实现）：`POST /api/accounts/{id}/cooldown/clear`。清 `cool_until` / `cool_kind` / `breaker_until` / `breaker_fails` / `degrade_until`；**不清** `session_dead_fails`（上游 401/403 的权威信号，清了会让死号反复被选中、拖慢整池）与 `status`（人工停用决策）。返回的 `cleared` 是**实际清掉的项**，没清到时为空 —— 前端据此如实提示，不谎报「已重置」。
- **Phase 3 收尾 · 全局导航 + 自定义布局**：侧边栏导航（6 分组 21 项，桌面常驻 + 窄屏汉堡抽屉）+ 菜单数据**单一来源** `web/lib/app-nav.ts`；`/panel/settings/layout` 用户自定义布局（显示/隐藏、排序、分组改名、导入导出，**存服务端** `system_settings.ui_layout`）；配置页自身**不可隐藏**（防用户锁死自己）。
- **Phase 4 · 模型中心 + 聊天测试台**：`GET /api/models/catalog` 按 realm 直连腾讯模型目录（显示名/描述/上下文/最大输出/推理档位/能力标记/倍率，realm 分键缓存，回退本地 ModelConfig）；`POST /api/playground/chat` 免密钥直调真实模型（复用号池原子操作，SSE 流式 + 实时积分）。
- **Phase 5 · 备份 + 更新 + 发版闭环**：`admin/backup.py` 全库快照（15 表，单事务恢复，脱敏开关，恢复前自动留底）；`scripts/sign_release.py`（OpenSSH ed25519 签名/验签，命名空间隔离）；`scripts/update.py`（下载→验签→解压→安装→基线复检）；`scripts/security_baseline.py`（安全基线审计 + 可幂等 apply）；CI 签名接 GitHub Secrets。
- **客户端参数按 realm 分档**：`wb_install` 支持 `realm=` 探测（认 `WorkBuddy` 与 `WorkBuddyAI` 两套安装目录）；`client_profile` 按 realm 存两份（`client_profile:cn` / `client_profile:global`）；两版已知身份兜底（`_REALM_DEFAULTS` / `REALM_FALLBACKS`）；UA 品牌段改用客户端 `applicationName`（cn=`WorkBuddy` / global=`workbuddy-ai`）。
- **上游吸收框架**：`docs-dev/UPSTREAM-ABSORB.md` 记录能力对照、待评估项与 fork 独有能力，供每次比对上游复用。
- **Phase 1 · 国际版 realm 双域**：`admin/realm.py` 端点分派表（CN `copilot.tencent.com` / Global `www.workbuddy.ai`，路径回落方向相反照上游）；`accounts.realm` 列；登录 realm 一致性校验；Global 注册激活 + 一次性 trial；模型 `cn:`/`global:` 前缀路由；密钥版本隔离（`api_keys.realm`）；scheduler 按 realm 门控（Global 跳过签到/成长/猫猫/开学季/夜猫，保活+活跃上报照跑）。
- **Phase 2 · 密钥运营面 + 入站治理**：ApiKey 全维度（`expires_at`/`max_ips`/`ip_allowlist`/`token_quota`/`used_tokens`）+ `security.validate_key` 纯函数判定链；`/v1/usage` 密钥自查（耗尽/过期/停用照样可查）；全局 CIDR 白/黑名单 + 每密钥 IP 管控（`admin/iputil.py` 信任 IP 解析）；模型别名映射；管理 API Token（只读作用域，SHA-256）；`audit_logs` 操作审计；账号池分组隔离（绑定空组明确报错，绝不静默回落）。
- **Phase 3 · 面板现代化**：新建 `web/`（Next.js 15 静态导出 + React 19 + shadcn/ui + Tailwind v4 + recharts）；按引擎契约写的 typed API client 层；五语言 i18n（zh-CN/zh-TW/en/ja/ko）；**22 个页面**（仪表盘 / 账号池 / 密钥 / 访问令牌 / 模型管理 / 模型中心 / 测试台 / 用量统计 / 用量日志 / 积分统计 / 定时任务 / 成长计划 / 安全治理 / 操作审计 / 账号分组 / 系统设置 / 线上同步 / 修改密码 / 逆向产物 / 界面布局 / 数据备份 / 更新日志）；右上角 realm 全局切换。**平价验收 35/35 零缺口**（旧面板 11 tab / 53 端点逐项核销）。

### 修复
- **`/api/models/busy` 把国内版模型发给国际版账号**：模型列表原取自 `ModelConfig` **全表**（两版混在一起）。实测两版清单**交集只 5 个**（cn 32 个 `hy3`/`hy4-preview` 那套；global 18 个 `default-model`/`balanced-model` 那套）→ 国际版上游不认识国内版 id → 请求失败 → 返回 `stale=True` + 一屏 `unknown`，**看起来像上游故障**。修：新增 `_models_for_realm()` 按 realm 过滤并**剥掉本地前缀**（`global:` / `cn:` 是本地隔离标记，上游只认裸名）。修复后无国际版模型时如实返回空 + stale（反映「还没同步」），不再伪装成上游失败。
- **测速端点出站基址写死国内版**：`/api/models/speed-test` 原用 `f"{settings.BACKEND}/v2/chat/completions"` —— 拿**国际版账号测速会打国内版端点**，凭证是国际版的、端点却是国内的 → 上游判非法，而且错误/冷却会记到那个无辜的国际版账号上。修：按账号 realm 取 `realm_mod.chat_base()` + `chat_paths()`。
- **模型同步按 realm 挑账号**（用户实测报告：「不管选国际版还是国内版，同步的都是国内版的模型」）：`_do_sync_models` 原用 `.first()` 取 **id 最小**的活跃账号，而账号按导入顺序入库、国内版通常先导 → 切到国际版点同步仍拿国内版账号去拉。修：`_pick_sync_account(db, realm)` 按 realm 过滤（**不做跨 realm 兜底** —— 那会让国际版带国内版身份出站）；国际版模型加 `global:` 前缀（realm 隔离），国内版保持无前缀（零回归）；定时任务两版各跑一次。
- **积分类端点按 realm 过滤**：`/api/credits/expiry`、`/api/credits/usage`、`/api/accounts/credits/sync` 加 `realm`。两版积分体系不通用，混在一起看不出「是哪一版缺积分」。过滤是**硬隔离**：该版无账号时返回空，**绝不退化成「不过滤」**（那会把另一版的消耗混进来，比返回空更误导）。
- **客户端参数路由层按 realm**：`GET /api/client-profile?realm=` / `PUT`（body 加 `realm`）/ `POST /detect?realm=` / `POST /reset?realm=` / `cprofile.sources(db, realm)`。后端早已按 realm 分档，但路由层没有 realm 参数 → 面板无法查看/保存/重置国际版档案，等于「两版只能用同一份」。响应另附 `realms` 两版并排摘要（避免漏配另一版还不自知）。非法 realm 的处理有意不对称：`save`/`reset` 拒绝 400（写操作不能猜错版本），`get`/`detect` 宽松（读操作不该因参数打错而 500）。
- **显式安装目录覆盖污染另一个版本**（**最隐蔽的一个**）：后台设过 `wb_paths.install_dir = D:\Software\WorkBuddy`（国内版），而 `_candidate_dirs()` 的显式覆盖分支**完全无视 realm 过滤** → `_realm_data('global')` 也返回那个国内版目录 → **国际版账号带整套国内版身份出站**（品牌段/版本号/渠道号全错）。修：新增 `_explicit_matches_realm()`，按 realm 探测时只有覆盖路径确实属于该 realm 才采用，否则跳过交给 realm 感知的自动扫描；无 realm 过滤时（主探测）仍无条件尊重覆盖（零回归）。**排查过程**：缓存过期、并发竞态、CWD、`_norm_realm` 映射、脚本/引擎差异五个假设**全部证伪**，最后靠在引擎进程内注册临时诊断端点读到 `realm_data_global.install_dir` 才定位（脚本层永远复现不了 —— 只有长驻引擎读了 DB 覆盖才触发）。
- **`REALM_FALLBACKS` 漏了版本号**：兜底表只给了 `application_name` / `turing_product_name` / `turing_channel_id`，版本号回落到两版共用的 `DEFAULTS` → 探测不到客户端时（线上服务器、扫描未覆盖）拼出「品牌段对、版本段错」的 UA：`workbuddy-ai/5.8.3`（官方是 5.8.4）。修：补齐 `desktop_version`（cn 5.8.3 / global 5.8.4）与 `cli_version`。
- **harness 压缩吞掉用户提问**（吸收上游 `1609e23c`，自行实现）：Claude Code 把提问与 `<system-reminder>` 写进**同一条** user 消息时，压缩流程把整条换成固定摘要 → **提问被一并吞掉** → 模型首轮只回 "I'm ready to help..."，要第二轮才正常回答。用户看到的是「第一次提问没反应」，极难联想到是网关把问题删了。修法：新增 `_extract_user_payload()`，从「替换」改成**减法**（只剥注入块/尾块/裸文件头行/摘要占位串，剩下什么就留什么），残留为空才用摘要兜底（原有降误伤能力不变）。
- **harness 脱敏只在 Claude Code 路径生效**（吸收上游 `bd6ad90e`，自行实现）：`/v1/messages` 做了脱敏，但 `/v1/chat/completions` 与 `/v1/responses` **没做** → 「Claude Code 能用、OpenAI 协议的客户端（DSH / Cursor / Cherry Studio…）同样内容发不出去」（`400 code=11128`，而简单 hello 却通过）。修法：抽出 `_desensitize_chat_body()` 统一入口，三端点接入；`responses` 的接入点是 `project_responses_chat_body()` **之后**（投影会重排 messages，先脱敏再投影等于白做）。
- **加密凭据读取依赖逆向产物文件**（吸收上游 `a3c82d6c`，自行实现）：新版客户端 `.info` 的敏感字段是 `$wbEncrypted` 信封，而取 at-rest 密钥只能靠 env 或仓库根的 `_extracted_keys.json` —— **别的用户 clone 后手上没有这个文件，导入功能直接死**。修法：新增 `wb_crypto.fetch_at_rest_secret_key()`，用 Electron 的 `ELECTRON_RUN_AS_NODE=1` 以**纯 Node 模式当场向客户端索取**（不建窗口、不弹窗、不落盘、零预先准备）；兜底链改为 `env → 运行时现取 → 历史产物文件`。实测 CN/Global 两版均取到密钥，keyId 与逆向提取结果一致。
- **`X-Conversation-ID` 恒发**（吸收上游 `HanawaBanana/workbuddy2api` #36）：原实现「客户端没给就不发」使 DSH/Codex 这类不带 `conversationId` 的 OpenAI 兼容客户端在部分账号上 100% 收到 `400 code=11-128 conversation id missing`。改为客户端原值优先、缺省用轮级聚合键兜底；**兜底值只进出站头、不进 `prompt_cache_key` 会话段**（否则轮级键逐轮变化会打碎前缀缓存）。
- **积分 `CapacityUnit` 单复数**：上游同一批数据里 `credits`（复数，Free Plan）与 `credit`（**单数**，Bonus Pack）并存，原实现严格相等过滤导致 **gift 包被整包丢弃** —— 国际版账号 350 积分（100+250）只显示 100，且「快到期优先用」策略不去消耗那 250（它 `CycleEndTime=2026-10-26` **会过期作废**）。改为容忍单复数与大小写、缺省视为积分。
- **UA 构造两处错**（`admin/realm.py`）：品牌段原为 `"WorkBuddy AI"`（抄自 Go 上游旧规格，实际客户端用 `applicationName` = `workbuddy-ai`）；`chat_ua` 第一段曾恒为 `WorkBuddy`，导致国际版 UA 变成 `WorkBuddy/5.8.4 workbuddy-ai/5.8.4 …`（**同一串里混两个产品名**，官方从不这样发）。已按客户端源码实证修正为三段同源。
- **`_decrypt_info_if_needed` 静默吞掉编程错误**（本轮自己引入、由测试抓到）：改动签名时漏了一行 `derive_protector_key`，留下 `protector` 未定义；`except Exception: return data` 把它伪装成「有密钥却解不开、且无任何错误信息」。教训：**静默兜底会把编程错误伪装成功能不可用** —— 能发现是因为既有测试断言了解密后的**明文**，而不是只看「没抛异常」。
- **子进程日志转发的中文编码崩溃**（用户实测报错）：`Popen(text=True)` 未指定 `encoding` → 中文 Windows 用 GBK 解 UTF-8 → `_pump` 线程被 `UnicodeDecodeError` 打死 → **管道无人排空 → uvicorn 卡死在 write → 端口在听但请求不返回**。修：显式 UTF-8 + 逐行解码容错 + 连续失败上限；并给子进程注入 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`（否则 uvicorn 自己打中文也会崩）。
- **备份恢复防线**（真实事故）：脱敏快照被恢复回库 → 9 个账号 `auth_json` 变成字面量 `"[redacted]"` → 所有上游调用 502。`restore_snapshot` 原来只校验结构、**不读 `include_secrets` 字段**。修：三道防线（自报脱敏 / 值含占位符 / `auth_json` 结构损坏）在**任何写入之前**拒绝，且拒绝后库内容不变。
- **安装目录探测不到自建路径**：候选只覆盖盘根与系统标准目录，客户端装在 `D:\Software\WorkBuddy` 时 `install_dir=None` → 所有风控参数退化成兜底值。补上 `Software`/`Programs`/`Apps`/`Tools`/`Dev` 中间层。
- **导航死链清理**：`command-palette` 里 9 条来自 manager 骨架的页面（`/tasks`、`/red-packets`、`/settings/upstream|proxy|models|users|tokens|system|about`）在本引擎并不存在，点进去 404。改为从 `app-nav.ts` 派生，并加守护测试（登记项必须真实存在、真实页面必须在导航里）。
- **移动端抽屉高度塌陷**：抽屉渲染在 sticky+blur 的 `<header>` 内时 `fixed inset-0` 高度退化成 header 高度（实测 52px vs 视口 843px）→ 菜单项全在滚动区外。改用 `createPortal` 到 body。
- **18 处表格缺横向滚动容器**：表格撑破整页而非自身滚动；`accounts` 页 4 处 + 全站补 `overflow-x-auto`。
- **顶栏窄屏溢出**：加入汉堡按钮后 375px 装不下「汉堡+品牌+主题+语言+版本+登出」→ 窄屏隐藏品牌文字并收紧间距。
- 面板 dev 模式去掉 `next/font/google` 联网依赖：Turbopack dev 离线时整站编译失败（build 用 webpack 不受影响），改本机字体栈。
- **测试自身的假阳性**：移动端溢出判据原为硬编码 `scrollWidth > 375`，而 `mobile=True` 下 Chrome 按页面 `viewport meta` 决定布局宽度（无 meta 的页面拿到 980）→ 21 页全部误报溢出。改用 `scrollWidth > clientWidth`。
- **`/docs` 默认关闭**：`config.py` 本就默认 0，是 `dev.ps1` 默认开了；改为需显式 `-Docs`。

### 可观测性
- **出站请求诊断**（吸收上游 `3bd17daa` 的诊断动机，自行实现为结构化单函数）：`400 code=11128` 有**至少四种**互不相关的成因（设备指纹缺失 / 脱敏未生效 / realm 身份不匹配 / 会话头族缺失），而客户端现象完全一样；且其中两种在简单 hello 测试里都会通过，极易误判成「账号被封」。新增 `_describe_outbound_headers()`，一行日志打全 `device_token` / 三个会话头 / 风控头 4 项 / body 特征，**缺哪项标 `**MISSING**`**、风控头部分缺失时报出**具体头名**；uid 只取前 12 位、UA 截断、输出有长度上限。两条出站路径（流式 / 非流式）都接，且都包在 `try` 里（诊断不能成为新的故障点）。


### 安全
- 网关错误分层加固（P2 既有，本项目主线）：session_dead（HTTP 401/403 权威失效）→ 立即停用账号（阈值 1），杜绝死号被反复选中——对齐封号防护诉求。
- 面板账号池异常状态可视化：cooling/熔断/降权/模型限流显著标色，悬停看真实原因（`gateway_reason` + `last_err_msg`），一键停用供人工研判。
- **上游只读护栏**：`ADMIN_UPSTREAM_READONLY=1` 时所有触上游的写操作（做任务/领奖/强制刷新/测速/同步模型/token 注入/猫猫旅行/定时任务立即运行/测试台直调）统一 403；本地 CRUD、GET、`/v1` `/gw` 转发一律放行。防测试误伤真实账号。
- **私有材料隔离校验**：`_relkey/`（发布签名私钥）与 `_extracted_keys.json`（逆向产物密钥）在 `.gitignore` 且**永不被 git 跟踪**（有测试守护）。

### 本轮新增（P6 收尾 + 上游 1.0.5 跟进 + 全端打包）

#### 新增

- **桌面端接入新面板（P6）**：`ADMIN_DEFAULT_PANEL=next` 时桌面版打开 `/panel`；免密登录由本地启动器签发 token 经 bridge 传递，三道约束（loopback + 端口 + 路径前缀）外加一次性 boot nonce。**踩过的三层坑**：① `?desktop=1` 会被静态导出重定向剥掉 → 改判 bridge 是否存在；② PyWebView 的 `js_api` 回调表在导航时销毁 → Promise 永不 settle → 所有桥调用加 `Promise.race` 超时；③ 桥可能晚于 4.5s 注入 → 去掉提前放弃。
- **版本与更新 UI**：侧边栏版本按钮 + 双模式更新对话框（桥调用 / 浏览器直连 `systemApi.releases()`）；`<pre>` 纯文本渲染 release body（不用 `dangerouslySetInnerHTML`）；链接做前缀守卫。
- **限时权益 / 补偿领取**（吸收上游 `c8d816d`+`98e5a93`+`eb0b1a1`，三合一自行实现）：`/benefits` 页面 + 4 个端点。**只能手动运行** —— 4 条硬约束：不在 `TASK_CHOICES`、数据库唯一约束、领取前必须先查、失败不置 claimed。状态 `unknown` 永不领取。
- **跨进程租约**（吸收上游思路，只取 ~90 行能力）：`admin/growth_lease.py` 基于 `SystemSetting` 表做 CAS 租约，默认 TTL 600s。防多实例重复执行同一账号任务（典型风控触发场景）。
- **成长执行审计**：`GrowthRunRecord` 表记录每次运行的账号/任务/结果/来源；`GET /api/growth/records` 查询。
- **147 条上游错误码中文解释**（跟随上游 1.0.5）：`admin/data/upstream_error_messages.json` + `admin/upstream_errors.py`。**只做解释不做决策** —— 解释文案可随客户端更新，决策仍基于我们对账号池的理解。接进 `_exhaustion_message()`，未知 code 不编造任何内容。
- **两级上游护栏**：`ADMIN_UPSTREAM_READONLY=1`（挡写）/ `ADMIN_UPSTREAM_READONLY_ALL=1`（连只读也挡，自动化专用）。后者为反向验证/CI 提供「物理上碰不到上游」的保证。
- **反向验证工具** `scripts/verify_tests.py`：34 条声明式 mutation，流程「注入 bug → pytest 必须红 → 还原 → 必须绿」。可进 CI（`--check`）。
- **CI 工作流**：`tests.yml`（每次 push/PR 跑全量 + 反向验证）、`android-release.yml`（APK 构建 + 产物解剖 + Release 发布）。
- **安卓 APK 打包**：移植上游 `35d7ce0` 的 Android 工程（19 文件）+ 包名改 `io.github.bingyangl1`。产物 22.9 MB，含 ARM64 Python 3.10 运行时。

#### 修复

- **SQLite 内存库启动崩溃**（CI 暴露）：`create_engine` 无条件传 `pool_size`/`max_overflow`/`pool_timeout`，而 SQLite 用 `SingletonThreadPool`/`StaticPool` 不接受 → `TypeError`。**PC/安卓/单机部署的默认形态一启动就崩**。新增 `_engine_kwargs(url)` 按方言挑参数；内存库用 `StaticPool + check_same_thread=False`。
- **`.gitignore` 的 `logs/` 吞掉新面板日志页面**：裸目录规则会匹配**任意深度** → `web/app/(main)/logs/` 整个目录被忽略，页面**从来没进过仓库**。本地文件在磁盘上所以看不出问题，别人克隆后点 `/logs` 404。改 `/logs/` 并加链接自检测试。
- **兜底默认版本不分 realm**（CI 暴露）：`client_profile.DEFAULTS` 的 `5.5.6` 是双域改造前的旧值，而 `_REALM_DEFAULTS` 没覆盖版本号 → 服务器/CI/手机（无客户端）两版都报 5.5.6。**两版同版本本身就是混用特征**。补齐两版版本号并让 `DEFAULTS` 与 `REALM_FALLBACKS["cn"]` 单一来源。
- **换凭据后仍被判「登录态失效」**（跟随上游 1.0.5）：账号因 401/403 判失效后，重新登录导入新凭据时原来的导入路径只覆盖 `auth_json`、不清 `session_dead_fails` / `cool_kind` / `status` → 导入成功但界面仍显示失效，用户只能删号重加（丢历史）。新增 `_reset_after_credential_update()`，**手动停用的账号不自动启用**（那是人的决定）。
- **6008 恢复时点用错**（跟随上游 1.0.5）：原来统一「官方时间优先，否则保守 24h」。但 6008（每日请求数）官方说明**次日零点**重置 —— 20:00 触发的账号会被白关到再下一天 20:00。新增 `next_cn_midnight()`，仅 6008 使用（6004 保持 24h，两者不同）。
- **APK 排除了 `server.py` 仍会 import 的模块**（CI 暴露）：上游 `build_android.py` 排除了 `app_source.py` 与 `routers/client_profile.py`，但我们的 `server.py` **无条件 import 两者** → APK 启动即 `ImportError`。**装上去根本打不开**。改为不排除任何 admin 模块。
- **安卓自检要求未打包的 `/panel/`**（CI 暴露）：`build_android.py` 只复制 `admin/static`（旧面板），从不复制 `web/out`，自检却无条件检查 `/panel/` → 构建必然失败。改为按形态区分检查项。
- **pip 源写死国内镜像**（CI 暴露）：清华源对国内友好，但 GitHub Actions runner 在海外会 `ReadTimeout`。改读 `WB_PIP_INDEX_URL`，CI 设官方 PyPI。
- **签名用系统 TEMP 被 ACL 拒绝**（CI 暴露）：Windows runner 的 `%TEMP%`（乃至整个 workspace）带 `\OWNER RIGHTS` ACL，`ssh-keygen -Y sign` 直接拒绝。新增 `_work_dir()`：优先 `WB_SIGN_TMPDIR`，否则仓库内 `.sign_tmp/`。
- **`at-rest` 测试传了不存在的路径**：`fetch_at_rest_secret_key()` 第一句就 `os.path.isfile(exe)`，传 `C:\fake\...` 会直接跳过、根本不调 subprocess → 断言 `is None` 的用例碰巧通过，断言 `== secret` 的失败。改为建真实临时文件。
- **冒烟测试只认旧面板**：`#app`/`#appVersion` 在新面板不存在 → 改双模式（旧面板查 id / 新面板查 React 已挂载 + 文本 >= 60）。**且不以登录为前提** —— 空数据目录停在登录页是正常的。

#### 改进

- **`check_runtime.py` 重写**：在**全新临时 SQLite 库**上真起服务、真请求 11 个核心端点。能在 20 分钟的 APK 构建之前暴露「漏打包模块」。
- **`check_runtime.py --android`**：按安卓形态只查旧面板（见上）。
- **APK 内容解剖验证**：`tests/test_apk_content.py` 把 APK 当 zip 解剖 `assets/chaquopy/app.imy`（104 条目），确认 `converter.pyc`、各路由、147 条错误码数据齐全，且**无凭证/数据库/签名密钥**。
- **文档补齐**：新增 [realm.md](docs-dev/realm.md) / [security.md](docs-dev/security.md) / [panel.md](docs-dev/panel.md) / [usage.md](docs-dev/usage.md) / [ENGINEERING-BASELINE.md](docs-dev/ENGINEERING-BASELINE.md)，并加链接自检测试（索引里提到的文件必须真实存在、文档里的数字必须能复现）。

### 测试

- 全量 **910 项通过**（+7 环境条件跳过），53 个测试文件。
- 反向验证 **34 条 mutation 全部有效**，抓到过 9 次假绿（测试通过但改坏实现照样通过）。
- 新增测试文件 12 个：`test_realm_identity_consistency` / `test_realm_audit_rounds` / `test_desktop_panel_p6` / `test_panel_update_ui` / `test_benefits` / `test_growth_lease` / `test_schedule_defaults` / `test_db_backends` / `test_mobile_runtime` / `test_upstream_1_0_5` / `test_android_packaging` / `test_apk_content` / `test_gitignore_source_safety` / `test_docs_links`。

## [0.1.0] - 基线

### 新增
- fork 基线：workbuddy2api（Sliverkiss）既有能力——OpenAI 兼容反代网关、账号池状态机（租约/粘性/错误分类/记账）、积分真实回写、模型白名单（ModelGroup 超集）、桌面版打包。

[Unreleased]: https://github.com/bingyangl1/workbuddy2api/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/bingyangl1/workbuddy2api/releases/tag/v0.1.0
