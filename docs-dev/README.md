# 文档索引（docs-dev/）

本目录承载 fork 新增功能的专项文档。主 [README](../README.md) 保持
「索引 + 快速开始」，**新功能的详细文档一律进 `docs-dev/`，不再堆进主 README**
（文档分流约定，见工作区根目录的计划文件 `PLAN-fork`）。

> **为何是 `docs-dev/` 而非 `docs/`**：根目录 `docs/` 被 `.gitignore` 有意忽略——
> 里面存的是协议逆向取证与排查记录（含未脱敏的真实手机号、token 片段、本地账号池
> 真实数据），属内部资料不上传。产品文档与内部取证必须分开，故产品文档放 `docs-dev/`。

---

## 按读者找文档

### 我是新接手的开发者，先看什么

1. [ENGINEERING-BASELINE.md](ENGINEERING-BASELINE.md) —— **工程基线**（测试纪律、
   反向验证、CI、打包，以及几个「已知会骗人」的地方）
2. [LOCAL-DEV-GUIDE.md](LOCAL-DEV-GUIDE.md) —— 本地跑起来
3. [realm.md](realm.md) —— **最核心的安全基础**（不看这个会写出混用身份的代码）

### 我要部署

| 场景 | 文档 |
|---|---|
| 单机 / 桌面 / 安卓（零外部依赖） | [MOBILE-AND-SINGLE-NODE.md](MOBILE-AND-SINGLE-NODE.md) |
| 服务端（MySQL + Redis） | 主 [README §四](../README.md#四环境安装与项目运行) |

### 我要用起来

| 主题 | 文档 |
|---|---|
| 网关接入、定时任务、权益、排障 | [usage.md](usage.md) |

### 我要改面板

| 主题 | 文档 |
|---|---|
| 架构 / i18n / 构建 / 桌面端接入 | [panel.md](panel.md) |

---

## 全部文档

| 文档 | 内容 | 状态 |
|---|---|---|
| [ENGINEERING-BASELINE.md](ENGINEERING-BASELINE.md) | 测试纪律、反向验证（39 条 mutation）、CI、打包、踩坑清单 | ✅ |
| [realm.md](realm.md) | 国际版双域隔离：判定、必须按 realm 分的清单、已修复的 10 处缺口、不变量 | ✅ |
| [security.md](security.md) | 密钥 20 维、入站治理、**两级上游护栏**、审计、免密登录 | ✅ |
| [panel.md](panel.md) | 新面板技术栈、25 个页面、导航单一来源、移动端 portal、i18n、构建 | ✅ |
| [usage.md](usage.md) | 网关接入、6 种定时任务、权益手动流程、故障排查表 | ✅ |
| [MOBILE-AND-SINGLE-NODE.md](MOBILE-AND-SINGLE-NODE.md) | SQLite 零依赖形态、Redis 可选、安卓构建、SQLite↔MySQL 互迁 | ✅ |
| [LOCAL-DEV-GUIDE.md](LOCAL-DEV-GUIDE.md) | 本地开发环境搭建 | ✅ |
| [UPSTREAM-ABSORB.md](UPSTREAM-ABSORB.md) | 上游能力吸收记录（吸了什么、为什么、怎么改的） | ✅ |

---

## 约定

1. 新功能产品文档进 `docs-dev/`（受版本控制）；主 README 只放索引 + 快速开始。
2. 每个 Phase 交付时同步更新对应 `docs-dev/*.md` 与 [CHANGELOG.md](../CHANGELOG.md)。
3. 环境变量以 `.env.example` 为准（`ADMIN_` 前缀命名），文档引用不复制全表。
4. **绝不**把未脱敏的账号 / token / 手机号写进 `docs-dev/`（那是 `docs/` 的事，且本地保留）。
5. **文档里的数字要能复现** —— 写「910 项测试」就要真的能跑出 910 项；
   写「39 条 mutation」就要 `verify_tests.py --list` 数得出来。
   宁可写「约」，也不要写一个从未验证过的精确数字。

### 链接自检

新增或修改文档后跑：

```bash
python -m pytest tests/test_docs_links.py -q
```

它检查 `docs-dev/` 与主 README 里的相对链接是否都指向真实文件
（`realm.md` 曾长期在索引里但实际不存在 —— 就是这类问题）。
