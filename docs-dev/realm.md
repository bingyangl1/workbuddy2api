# 国际版 realm 双域隔离

> 这是本项目**最核心的安全基础**。搞错它不会报错、不会崩，只会让账号
> 悄无声息地被上游判定为「非官方客户端」——这是最难排查的一类问题。

---

## 1. 为什么需要 realm

WorkBuddy 有两套**互不相同**的客户端与上游：

| | 国内版（cn） | 国际版（global） |
|---|---|---|
| 域名 | `copilot.tencent.com` / `codebuddy.cn` | `www.workbuddy.ai` |
| applicationName | `WorkBuddy` | `workbuddy-ai` |
| 桌面端版本 | `5.8.3` | `5.8.4` |
| turingChannelId | `109144` | `400111` |
| CLI 版本 | `2.161.0` | `2.161.0` |
| 安装目录 | `WorkBuddy` | `WorkBuddyAI` |

**两版的版本号明确不同。** 这意味着：

* 拿国内版的 UA 去请求国际版上游 → 上游看到「国际版账号 + 国内版客户端」，自相矛盾
* 两版报同一个版本号 → 本身就是最明显的混用特征

所以硬编码一套身份是**根本性错误**，不是「不够优雅」。

---

## 2. 通行证：realm 是怎么被判定出来的

```
凭证里的 domain / realm 字段
        ↓
admin/realm.py::resolve_realm()
        ↓
  cn  /  global
```

判定优先序（`resolve_realm`）：

1. 凭证里**显式声明**的 `realm` 字段（未来全球版登录会带）
2. `domain` 是否属于国际版域名集合
3. 都不满足 → `cn`（默认值，不猜测）

判定结果落到 `Account.realm`，**后续所有出站请求都以它为准**。

### 关键设计：落库用纯推导，不受「逃生门」影响

`resolve_realm` 有一个逃生门（`ADMIN_REALM_ESCAPE`）用于调试，
但**落库时走的是 `resolve_realm` 的纯推导路径**，不会被逃生门污染。
否则一次调试就会把错误的 realm 永久写进库。

---

## 3. 一张表：哪些东西必须按 realm 分

**这个清单就是审计用的检查表。** 任何新增的出站调用都必须先在这里找位置。

| 类别 | API | 说明 |
|---|---|---|
| **基址** | `chat_base(realm)` | 模型请求域名 |
| | `billing_base(realm)` | 计费/权益域名 |
| | `models_path(realm)` | 模型列表路径（两版不同） |
| **路径** | `chat_paths(realm)` | 候选路径 + 回退状态 |
| | `billing_paths(realm)` | 计费路径前缀族 |
| **身份** | `client_version(realm)` | 桌面端版本 |
| | `cli_version(realm)` | CLI 版本 |
| | `ua_brand(realm)` | UA 品牌名 |
| | `chat_ua(realm)` / `billing_ua(realm)` | 完整 UA 串 |
| **风控** | `device_fingerprint_headers(realm)` | 设备指纹 |
| | `headers(realm)` / `billing_headers(realm)` | 请求头全套 |
| | `client_profile.risk_headers(realm)` | 风控闸门头 |
| **语言** | `accept_language(realm)` | 两版不同 |
| **能力** | `task_supported(realm, task)` | 该版本是否支持某任务 |
| | `trial_available(realm)` | 是否有试用 |

### 兜底值

探测不到客户端时（服务器 / CI / 手机），用 `wb_install.REALM_FALLBACKS`：

```python
{
  "cn":     {"application_name": "WorkBuddy",   "desktop_version": "5.8.3",
             "turing_channel_id": 109144, "cli_version": "2.161.0"},
  "global": {"application_name": "workbuddy-ai", "desktop_version": "5.8.4",
             "turing_channel_id": 400111, "cli_version": "2.161.0"},
}
```

**兜底必须按 realm 分。** 只写一份单值就会出现「两版同版本」的混用特征。

> ⚠️ 这个坑踩过：`client_profile.DEFAULTS` 里的版本号曾是 `5.5.6`（双域改造
> 之前的旧值），而 `_REALM_DEFAULTS` 没有覆盖它 → 服务器/CI/手机上两版都报
> 5.5.6。本地装了客户端所以一直没暴露，**CI 首次跑全量时 6 个测试同时失败**。
> 现在 `DEFAULTS` 与 `REALM_FALLBACKS["cn"]` 保持单一事实来源。

---

## 4. 已修复的 10 处缺口（审计记录）

这些是 11 轮审计中实际抓到的问题。**每一处都对应一个真实的混用场景**，
新代码要避免重犯同类错误。

| # | 缺口 | 后果 |
|---|---|---|
| 1 | `_upstream_extra_headers` 忽略 `account` 参数 | 永远发国内版风控头 |
| 2 | `_build_headers_from` 只设 UA，无风控头 | `risk=0/4`，风控直接识别 |
| 3 | `turing_token` 无 realm 参数 | 两版共用同一设备指纹 |
| 4 | `fetch_model_busy` 未接 realm | 基址正确但路径错误 |
| 5 | `ADMIN_UPSTREAM_CLIENT_NAME` 默认 `"WorkBuddy"` | 强制覆盖国际版为国内版 |
| 6 | `realm.chat_ua` 兜底用国内版版本号 | `workbuddy-ai/5.8.3` 品牌+版本错配 |
| 7 | `fetch_appearance_themes` 用硬编码 `BILL_BASE` | 国际版请求打到国内版域 |
| 8 | `WEB_BASE` 硬编码 | 同上 |
| 9 | `converter` 三个资源缓存单槽 | 切版本后返回上一版模型清单（两版交集仅 5 个） |
| 10 | `converter` 计费路径硬编码 `/v2/...` | 国际版路径不同 |
| 10b | `CapacityUnit != "credits"` 严格比较 | 国际版单位不同被判为无额度 |
| 10c | `/v1/messages` 未写回 `chat_body["model"]` | 带前缀的模型名被发到上游 |

---

## 5. 不变量（测试守着）

`tests/test_realm_identity_consistency.py` 等 8 个 realm 测试文件，
共 **32 项不变量检查**。核心思想：

> **同一 realm 的 UA / X-IDE-* / 渠道号 / 版本号 / chat_base / billing_base /
> accept_language 必须全部指向同一版本；跨 realm 零相同要素。**

具体断言：

* cn 与 global 的 `desktop_version` 必须不同
* cn 的 UA 里品牌必须是 `WorkBuddy`，global 必须是 `workbuddy-ai`
* UA 里的版本号必须与该 realm 的 `desktop_version` 一致
* 请求头的 `X-IDE-Name` / `X-IDE-Type` 必须与该 realm 的 `application_name` 一致
* 兜底分支（探测不到客户端）也必须满足上述全部

---

## 6. 新增功能时的检查清单

写任何**会触达上游**的代码前，逐条过：

- [ ] 这个函数拿到 `Account` 了吗？如果只拿到 `uid`，realm 信息够不够？
- [ ] 用 `billing_base()` / `chat_base()` 还是硬编码了域名？
- [ ] 请求头是从 `client_profile` 拿的，还是手写的？
- [ ] 如果做了缓存，缓存的 key 里带 realm 了吗？
- [ ] 版本号是从 `client_version(realm)` 拿的，还是有默认值？
- [ ] 加测试了吗？**必须有一条断言「两版不同」**，否则混用不会被发现。

---

## 7. 为什么不能 `git merge upstream/master`

上游 fork（`xiaofan6ya/workbuddy2api`）**完全没有 realm 概念**
（`growth_plan.py` / `growth_center.py` 里 "realm" 出现 0 次）。

直接合并会一次性引入大量硬编码单域的实现。正确做法：

**参考上游的实现思路，用我们自己的 realm 感知代码重写。**

见 [UPSTREAM-ABSORB.md](UPSTREAM-ABSORB.md)。
