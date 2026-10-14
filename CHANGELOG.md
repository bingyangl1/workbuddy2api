# Changelog

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 纪律：
每个发布段落按 **新增 / 修复 / 改进 / 安全** 归类（吸收 manager 的发版纪律）。
本 fork 自 v0 起记录。版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增
- **Phase 1 · 国际版 realm 双域**：`admin/realm.py` 端点分派表（CN `copilot.tencent.com` / Global `www.workbuddy.ai`，路径回落方向相反照上游）；`accounts.realm` 列；登录 realm 一致性校验；Global 注册激活 + 一次性 trial；模型 `cn:`/`global:` 前缀路由；密钥版本隔离（`api_keys.realm`）；scheduler 按 realm 门控（Global 跳过签到/成长/猫猫/开学季/夜猫，保活+活跃上报照跑）。
- **Phase 2 · 密钥运营面 + 入站治理**：ApiKey 全维度（`expires_at`/`max_ips`/`ip_allowlist`/`token_quota`/`used_tokens`）+ `security.validate_key` 纯函数判定链；`/v1/usage` 密钥自查（耗尽/过期/停用照样可查）；全局 CIDR 白/黑名单 + 每密钥 IP 管控（`admin/iputil.py` 信任 IP 解析）；模型别名映射；管理 API Token（只读作用域，SHA-256）；`audit_logs` 操作审计；账号池分组隔离（绑定空组明确报错，绝不静默回落）。
- **Phase 3 · 面板现代化（进行中）**：新建 `web/`（Next.js 15 静态导出 + React 19 + shadcn/ui + Tailwind v4 + recharts）；按引擎契约写的 typed API client 层；五语言 i18n（zh-CN/zh-TW/en/ja/ko）；页面：仪表盘 / 账号池（列表/过滤/启停/积分明细/全量同步/异常状态徽章/本机扫描导入/去重/模型限流台账/导出）/ API 密钥（P2 全维度编辑/明文查看/创建明文只显一次/重置用量/用量进度条/范围徽章）/ 登录；右上角 realm 全局切换。

### 修复
- 面板 dev 模式去掉 `next/font/google` 联网依赖：Turbopack dev 离线时整站编译失败（build 用 webpack 不受影响），改本机字体栈。

### 安全
- 网关错误分层加固（P2 既有，本项目主线）：session_dead（HTTP 401/403 权威失效）→ 立即停用账号（阈值 1），杜绝死号被反复选中——对齐封号防护诉求。
- 面板账号池异常状态可视化：cooling/熔断/降权/模型限流显著标色，悬停看真实原因（`gateway_reason` + `last_err_msg`），一键停用供人工研判。

## [0.1.0] - 基线

### 新增
- fork 基线：workbuddy2api（Sliverkiss）既有能力——OpenAI 兼容反代网关、账号池状态机（租约/粘性/错误分类/记账）、积分真实回写、模型白名单（ModelGroup 超集）、桌面版打包。

[Unreleased]: https://github.com/bingyangl1/workbuddy2api/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/bingyangl1/workbuddy2api/releases/tag/v0.1.0
