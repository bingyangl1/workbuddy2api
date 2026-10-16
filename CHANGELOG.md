# Changelog

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 纪律：
每个发布段落按 **新增 / 修复 / 改进 / 安全** 归类（吸收 manager 的发版纪律）。
本 fork 自 v0 起记录。版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增
- **Phase 3 收尾 · 全局导航 + 自定义布局**：侧边栏导航（6 分组 21 项，桌面常驻 + 窄屏汉堡抽屉）+ 菜单数据**单一来源** `web/lib/app-nav.ts`；`/panel/settings/layout` 用户自定义布局（显示/隐藏、排序、分组改名、导入导出，**存服务端** `system_settings.ui_layout`）；配置页自身**不可隐藏**（防用户锁死自己）。
- **Phase 4 · 模型中心 + 聊天测试台**：`GET /api/models/catalog` 按 realm 直连腾讯模型目录（显示名/描述/上下文/最大输出/推理档位/能力标记/倍率，realm 分键缓存，回退本地 ModelConfig）；`POST /api/playground/chat` 免密钥直调真实模型（复用号池原子操作，SSE 流式 + 实时积分）。
- **Phase 5 · 备份 + 更新 + 发版闭环**：`admin/backup.py` 全库快照（15 表，单事务恢复，脱敏开关，恢复前自动留底）；`scripts/sign_release.py`（OpenSSH ed25519 签名/验签，命名空间隔离）；`scripts/update.py`（下载→验签→解压→安装→基线复检）；`scripts/security_baseline.py`（安全基线审计 + 可幂等 apply）；CI 签名接 GitHub Secrets。
- **客户端参数按 realm 分档**：`wb_install` 支持 `realm=` 探测（认 `WorkBuddy` 与 `WorkBuddyAI` 两套安装目录）；`client_profile` 按 realm 存两份（`client_profile:cn` / `client_profile:global`）；两版已知身份兜底（`_REALM_DEFAULTS` / `REALM_FALLBACKS`）；UA 品牌段改用客户端 `applicationName`（cn=`WorkBuddy` / global=`workbuddy-ai`）。
- **上游吸收框架**：`docs-dev/UPSTREAM-ABSORB.md` 记录能力对照、待评估项与 fork 独有能力，供每次比对上游复用。
- **Phase 1 · 国际版 realm 双域**：`admin/realm.py` 端点分派表（CN `copilot.tencent.com` / Global `www.workbuddy.ai`，路径回落方向相反照上游）；`accounts.realm` 列；登录 realm 一致性校验；Global 注册激活 + 一次性 trial；模型 `cn:`/`global:` 前缀路由；密钥版本隔离（`api_keys.realm`）；scheduler 按 realm 门控（Global 跳过签到/成长/猫猫/开学季/夜猫，保活+活跃上报照跑）。
- **Phase 2 · 密钥运营面 + 入站治理**：ApiKey 全维度（`expires_at`/`max_ips`/`ip_allowlist`/`token_quota`/`used_tokens`）+ `security.validate_key` 纯函数判定链；`/v1/usage` 密钥自查（耗尽/过期/停用照样可查）；全局 CIDR 白/黑名单 + 每密钥 IP 管控（`admin/iputil.py` 信任 IP 解析）；模型别名映射；管理 API Token（只读作用域，SHA-256）；`audit_logs` 操作审计；账号池分组隔离（绑定空组明确报错，绝不静默回落）。
- **Phase 3 · 面板现代化**：新建 `web/`（Next.js 15 静态导出 + React 19 + shadcn/ui + Tailwind v4 + recharts）；按引擎契约写的 typed API client 层；五语言 i18n（zh-CN/zh-TW/en/ja/ko）；**22 个页面**（仪表盘 / 账号池 / 密钥 / 访问令牌 / 模型管理 / 模型中心 / 测试台 / 用量统计 / 用量日志 / 积分统计 / 定时任务 / 成长计划 / 安全治理 / 操作审计 / 账号分组 / 系统设置 / 线上同步 / 修改密码 / 逆向产物 / 界面布局 / 数据备份 / 更新日志）；右上角 realm 全局切换。**平价验收 35/35 零缺口**（旧面板 11 tab / 53 端点逐项核销）。

### 修复
- **`X-Conversation-ID` 恒发**（吸收上游 `HanawaBanana/workbuddy2api` #36）：原实现「客户端没给就不发」使 DSH/Codex 这类不带 `conversationId` 的 OpenAI 兼容客户端在部分账号上 100% 收到 `400 code=11-128 conversation id missing`。改为客户端原值优先、缺省用轮级聚合键兜底；**兜底值只进出站头、不进 `prompt_cache_key` 会话段**（否则轮级键逐轮变化会打碎前缀缓存）。
- **积分 `CapacityUnit` 单复数**：上游同一批数据里 `credits`（复数，Free Plan）与 `credit`（**单数**，Bonus Pack）并存，原实现严格相等过滤导致 **gift 包被整包丢弃** —— 国际版账号 350 积分（100+250）只显示 100，且「快到期优先用」策略不去消耗那 250（它 `CycleEndTime=2026-10-26` **会过期作废**）。改为容忍单复数与大小写、缺省视为积分。
- **子进程日志转发的中文编码崩溃**（用户实测报错）：`Popen(text=True)` 未指定 `encoding` → 中文 Windows 用 GBK 解 UTF-8 → `_pump` 线程被 `UnicodeDecodeError` 打死 → **管道无人排空 → uvicorn 卡死在 write → 端口在听但请求不返回**。修：显式 UTF-8 + 逐行解码容错 + 连续失败上限；并给子进程注入 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`（否则 uvicorn 自己打中文也会崩）。
- **备份恢复防线**（真实事故）：脱敏快照被恢复回库 → 9 个账号 `auth_json` 变成字面量 `"[redacted]"` → 所有上游调用 502。`restore_snapshot` 原来只校验结构、**不读 `include_secrets` 字段**。修：三道防线（自报脱敏 / 值含占位符 / `auth_json` 结构损坏）在**任何写入之前**拒绝，且拒绝后库内容不变。
- **UA 构造两处错**（`admin/realm.py`）：品牌段原为 `"WorkBuddy AI"`（抄自 Go 上游旧规格，实际客户端用 `applicationName` = `workbuddy-ai`）；`chat_ua` 第一段曾恒为 `WorkBuddy`，导致国际版 UA 变成 `WorkBuddy/5.8.4 workbuddy-ai/5.8.4 …`（**同一串里混两个产品名**）。已按客户端源码实证修正为三段同源。
- **安装目录探测不到自建路径**：候选只覆盖盘根与系统标准目录，客户端装在 `D:\Software\WorkBuddy` 时 `install_dir=None` → 所有风控参数退化成兜底值。补上 `Software`/`Programs`/`Apps`/`Tools`/`Dev` 中间层。
- **导航死链清理**：`command-palette` 里 9 条来自 manager 骨架的页面（`/tasks`、`/red-packets`、`/settings/upstream|proxy|models|users|tokens|system|about`）在本引擎并不存在，点进去 404。改为从 `app-nav.ts` 派生，并加守护测试（登记项必须真实存在、真实页面必须在导航里）。
- **移动端抽屉高度塌陷**：抽屉渲染在 sticky+blur 的 `<header>` 内时 `fixed inset-0` 高度退化成 header 高度（实测 52px vs 视口 843px）→ 菜单项全在滚动区外。改用 `createPortal` 到 body。
- **18 处表格缺横向滚动容器**：表格撑破整页而非自身滚动；`accounts` 页 4 处 + 全站补 `overflow-x-auto`。
- **顶栏窄屏溢出**：加入汉堡按钮后 375px 装不下「汉堡+品牌+主题+语言+版本+登出」→ 窄屏隐藏品牌文字并收紧间距。
- 面板 dev 模式去掉 `next/font/google` 联网依赖：Turbopack dev 离线时整站编译失败（build 用 webpack 不受影响），改本机字体栈。
- **测试自身的假阳性**：移动端溢出判据原为硬编码 `scrollWidth > 375`，而 `mobile=True` 下 Chrome 按页面 `viewport meta` 决定布局宽度（无 meta 的页面拿到 980）→ 21 页全部误报溢出。改用 `scrollWidth > clientWidth`。
- **`/docs` 默认关闭**：`config.py` 本就默认 0，是 `dev.ps1` 默认开了；改为需显式 `-Docs`。

### 安全
- 网关错误分层加固（P2 既有，本项目主线）：session_dead（HTTP 401/403 权威失效）→ 立即停用账号（阈值 1），杜绝死号被反复选中——对齐封号防护诉求。
- 面板账号池异常状态可视化：cooling/熔断/降权/模型限流显著标色，悬停看真实原因（`gateway_reason` + `last_err_msg`），一键停用供人工研判。
- **上游只读护栏**：`ADMIN_UPSTREAM_READONLY=1` 时所有触上游的写操作（做任务/领奖/强制刷新/测速/同步模型/token 注入/猫猫旅行/定时任务立即运行/测试台直调）统一 403；本地 CRUD、GET、`/v1` `/gw` 转发一律放行。防测试误伤真实账号。
- **私有材料隔离校验**：`_relkey/`（发布签名私钥）与 `_extracted_keys.json`（逆向产物密钥）在 `.gitignore` 且**永不被 git 跟踪**（有测试守护）。

## [0.1.0] - 基线

### 新增
- fork 基线：workbuddy2api（Sliverkiss）既有能力——OpenAI 兼容反代网关、账号池状态机（租约/粘性/错误分类/记账）、积分真实回写、模型白名单（ModelGroup 超集）、桌面版打包。

[Unreleased]: https://github.com/bingyangl1/workbuddy2api/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/bingyangl1/workbuddy2api/releases/tag/v0.1.0
