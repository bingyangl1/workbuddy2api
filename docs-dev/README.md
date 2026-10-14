# 文档索引（docs-dev/）

本目录承载 fork 新增功能的专项文档。主 [README](../README.md) 保持「索引 + 快速开始」，
**新功能的详细文档一律进 `docs-dev/`，不再堆进主 README**（文档分流约定，见 PLAN-fork.md P0）。

> **为何是 `docs-dev/` 而非 `docs/`**：根目录 `docs/` 被 `.gitignore` 有意忽略——
> 里面存的是协议逆向取证与排查记录（含未脱敏的真实手机号、token 片段、本地账号池
> 真实数据），属内部资料不上传。产品文档与内部取证必须分开，故产品文档放 `docs-dev/`。

## 已交付功能的文档落点

| 主题 | 文档 | 状态 |
|------|------|------|
| 国际版 realm 双域 | [realm.md](realm.md) | ⏳ 待补（P1 已交付，文档欠账） |
| 密钥运营面 / 入站治理 | [security.md](security.md) | ⏳ 待补（P2 已交付，文档欠账） |
| 面板现代化 / i18n / 部署 | [panel.md](panel.md) | ⏳ 待补（P3 进行中） |
| 使用与运维总览 | [usage.md](usage.md) | ⏳ 待补 |

## 约定

1. 新功能产品文档进 `docs-dev/`（受版本控制）；主 README 只放索引 + 快速开始。
2. 每个 Phase 交付时同步更新对应 `docs-dev/*.md` 与 [CHANGELOG.md](../CHANGELOG.md)。
3. 环境变量以 `.env.example` 为准（ADMIN_ 前缀命名），文档引用不复制全表。
4. **绝不**把未脱敏的账号/token/手机号写进 `docs-dev/`（那是 `docs/` 的事，且本地保留）。
