"""反向验证工具：确认测试**真的能抓到 bug**（防假绿）。

# 为什么需要这个工具

正向跑测试只能证明「代码现在是对的」，不能证明「测试在测东西」。

本项目已经踩过 6 次**假绿**（测试通过，但把实现改坏了它照样通过）：

| 测试 | 假绿原因 |
|---|---|
| 每 Key 并发数 | 只测了纯函数，把 `allow()` 里的调用删掉照样过 |
| CAS 租约（第 1 版） | 无条件 UPDATE 也能过（它压根不看旧值） |
| CAS 租约（第 2 版） | mock 没真正制造竞态 |
| TTL 过期（第 1 版） | 「永不过期」的实现根本不看 expires |
| `test_does_not_rely_on_query_flag` | 搜全文匹配到了函数定义自己 |
| 冒烟测试（旧断言） | 断言「不该测新面板」，把没跟上改动当成正确 |

这 6 条**正向全部通过**。只有「把实现故意改坏，看测试会不会红」能发现它们。

# 用法

    python scripts/verify_tests.py                # 跑全部 mutation
    python scripts/verify_tests.py --list         # 只列出清单
    python scripts/verify_tests.py --only lease   # 按名字过滤
    python scripts/verify_tests.py --check        # CI 模式（有假绿则退出码 1）

# 安全：它**物理上碰不到上游**

本工具只做两件事：

    1. 改本地源码文件（写文本）
    2. 跑 `pytest <测试选择器>`

**不会启动引擎、不会发任何 HTTP 请求、不会连接腾讯/WorkBuddy 的接口。**
测试本身也全是 mock 的（已审计：整套测试零真实上游调用）。

即便如此，仍建议设置 `ADMIN_UPSTREAM_READONLY_ALL=1`（本工具会自动注入），
作为「万一」的兜底 —— 见 `admin/security.py::is_upstream_touch`。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Mutation:
    """一次「把实现改坏」的定义。

    `old` 必须在文件里**唯一出现**，否则会改错地方（工具会校验并报错）。
    """

    name: str
    file: str
    old: str
    new: str
    tests: list[str]
    why: str = ""
    tags: list[str] = field(default_factory=list)


#: ── mutation 清单 ───────────────────────────────────────────────────
#:
#: 每一条都对应一个「曾经是假绿」或「必须守住」的不变量。
#: 加新条目时请一并写 `why` —— 说明它防的是什么退化。
MUTATIONS: list[Mutation] = [
    # ── 租约（跨进程互斥）──────────────────────────────────────────
    Mutation(
        name="lease-cas",
        file="admin/growth_lease.py",
        old=("                   .filter(SystemSetting.key == key,\n"
             "                           SystemSetting.value == previous)\n"
             "                   .update({SystemSetting.value: value},"),
        new=("                   .filter(SystemSetting.key == key)\n"
             "                   .update({SystemSetting.value: value},"),
        tests=["tests/test_growth_lease.py::LeaseCasCase"],
        why="去掉 CAS → 并发时两个进程都以为自己抢到了 → 同一账号被跑两遍（封号风险）",
        tags=["lease", "safety"],
    ),
    Mutation(
        name="lease-owner-check",
        file="admin/growth_lease.py",
        old=('        if str(current.get("owner") or "") != str(owner):\n'
             '            return False          # 不是自己的锁，不动'),
        new="        if False:\n            return False",
        tests=["tests/test_growth_lease.py::LeaseBasicsCase::test_release_only_by_owner"],
        why="不校验 owner → A 的租约过期后能解掉 B 的锁",
        tags=["lease", "safety"],
    ),
    Mutation(
        name="lease-ttl",
        file="admin/growth_lease.py",
        old=("    if expiry > time.time():\n"
             "        return None          # 还在有效期内，别人占着"),
        new="    if False:\n        return None",
        tests=["tests/test_growth_lease.py::LeaseBasicsCase"],
        why="TTL 失效 → 进程被 kill 后账号永久锁死",
        tags=["lease", "safety"],
    ),
    Mutation(
        name="lease-not-wired",
        file="admin/routers/growth.py",
        old="        lease_owner = growth_lease.acquire(db, aid)",
        new="        lease_owner = 'no-lease'",
        tests=["tests/test_growth_lease.py::RunAccountsUsesLeaseCase"],
        why="函数写好了但没接进去 —— 这是最典型的假绿形态",
        tags=["lease", "wiring"],
    ),
    Mutation(
        name="lease-release-not-finally",
        file="admin/routers/growth.py",
        old=("        finally:\n"
             "            # **必须释放**：否则这个号会被锁到 TTL 到期（默认 600 秒），"),
        new=("        if True:\n"
             "            # **必须释放**：否则这个号会被锁到 TTL 到期（默认 600 秒），"),
        tests=["tests/test_growth_lease.py::RunAccountsUsesLeaseCase::test_release_in_finally"],
        why="释放不在 finally → 任务抛异常时不解锁",
        tags=["lease", "safety"],
    ),
    # ── 权益领取（能跑一遍，多跑会被检测）────────────────────────────
    Mutation(
        name="benefits-scheduled",
        file="admin/routers/schedules.py",
        old='"refresh_growth_tasks", "run_growth_tasks", "keepalive_tokens"]',
        new=('"refresh_growth_tasks", "run_growth_tasks", "keepalive_tokens", '
             '"claim_benefits"]'),
        tests=["tests/test_benefits.py::NotScheduledCase"],
        why="把权益加进定时任务 → 会被自动跑（用户明确要求只能手动）",
        tags=["benefits", "safety"],
    ),
    Mutation(
        name="benefits-claim-without-check",
        file="admin/benefits.py",
        old=('    verdict = classify(payload)\n'
             '    if verdict["status"] != "available":'),
        new="    verdict = classify(payload)\n    if False:",
        tests=["tests/test_benefits.py::ClaimOrderCase"],
        why="状态不明时也发领取请求 → 最危险的一类（上游判重复领取）",
        tags=["benefits", "safety"],
    ),
    Mutation(
        name="benefits-failure-marked-claimed",
        file="admin/benefits.py",
        old=('    _record(db, account.id, checked=True, claimed=False, credit=0,\n'
             '            reason=f"领取未成功'),
        new=('    _record(db, account.id, checked=True, claimed=True, credit=0,\n'
             '            reason=f"领取未成功'),
        tests=["tests/test_benefits.py::FailureKeepsRetryableCase"],
        why="领取失败也置 claimed → 一次网络抖动让账号永久失去机会",
        tags=["benefits", "safety"],
    ),
    Mutation(
        name="benefits-guard-removed",
        file="admin/security.py",
        old='    "/api/benefits/claim",\n)',
        new=")",
        tests=["tests/test_benefits.py::ReadonlyGuardCase"],
        why="把 /claim 从护栏移除 → 测试期可能误领（不可回滚）",
        tags=["benefits", "guard"],
    ),
    Mutation(
        name="benefits-unique-constraint",
        file="admin/models.py",
        old=('        UniqueConstraint("account_id", "campaign_key",\n'
             '                         name="uq_benefit_claims_account_campaign"),'),
        new='        Index("ix_benefit_claims_tmp", "campaign_key"),',
        tests=["tests/test_benefits.py::ModelConstraintCase"],
        why="删掉唯一约束 → 重复领取挡不住（重启/多进程/连点）",
        tags=["benefits", "safety"],
    ),
    # ── realm 隔离（凭证与域名必须同源）──────────────────────────────
    Mutation(
        name="realm-risk-headers",
        file="admin/routers/proxy.py",
        old="h.update(cprofile.risk_headers(realm=acc_realm) if acc_realm",
        new="h.update(cprofile.risk_headers() if acc_realm",
        tests=["tests/test_realm_identity_consistency.py::ProxyExtraHeadersCase"],
        why="风控头不分 realm → 国际版凭证带国内版身份（自相矛盾最易被判伪造）",
        tags=["realm", "safety"],
    ),
    Mutation(
        name="realm-ua-fallback",
        file="admin/realm.py",
        old="    ver = client_version(realm)",
        new="    ver = client_version()",
        tests=["tests/test_realm_identity_consistency.py::FallbackUACase"],
        why="UA 兜底用了固定版本 → 国际版变成 workbuddy-ai/5.8.3（品牌对版本错）",
        tags=["realm", "safety"],
    ),
    Mutation(
        name="realm-cache-slot",
        file="converter.py",
        old="    rk = _realm_key(cred)\n    slot = _MODELS_CACHE.setdefault(rk,",
        new="    rk = 'cn'\n    slot = _MODELS_CACHE.setdefault(rk,",
        tests=["tests/test_realm_audit_rounds.py::ConverterCacheCase"],
        why="缓存不分 realm → 切版本后返回上一版模型清单（两版交集只 5 个）",
        tags=["realm", "cache"],
    ),
    # ── 默认策略（不能自动跑）────────────────────────────────────────
    Mutation(
        name="schedule-growth-default-on",
        file="admin/scheduler.py",
        # 注意：同一段 `db.add(Schedule(name="每日自动做成长任务"...)` 在
        # `seed_defaults` 和 `ensure_growth_schedules` 里各有一份（都必须是
        # enabled=0）。所以锚点不能用完整语句（会出现 2 次），
        # 这里只取「两者完全相同的那一行」—— 工具会校验唯一性。
        old=('task="run_growth_tasks",\n'
             '                        interval_minutes=1440, enabled=0,\n'
             '                        next_run_at=now + timedelta'),
        new=('task="run_growth_tasks",\n'
             '                        interval_minutes=1440, enabled=1,\n'
             '                        next_run_at=now + timedelta'),
        tests=["tests/test_schedule_defaults.py::SeedDefaultsCase",
               "tests/test_schedule_defaults.py::BehaviorCase"],
        why="默认启用 → 新装实例在用户不知情时给全部账号发任务请求",
        tags=["schedule", "safety"],
    ),
    # ── 上游 1.0.5 跟进（凭据复位 / 6008 零点 / 错误码解释）──────────
    Mutation(
        name="credential-not-reset",
        file="admin/routers/accounts.py",
        old=('    acc.session_dead_fails = 0\n'
             '    acc.last_err_msg = ""'),
        new=('    acc.last_err_msg = ""'),
        tests=["tests/test_upstream_1_0_5.py::CredentialResetCase"],
        why="换凭据后不清 session_dead_fails → 账号继续被判「登录态失效」，"
            "用户只能删号重加（丢历史）。上游 1.0.5 修的同款 bug",
        tags=["upstream", "account", "safety"],
    ),
    Mutation(
        name="credential-manual-disable-overridden",
        file="admin/routers/accounts.py",
        old=('    auto_disabled = (acc.cool_kind == "session_dead"\n'
             '                     and acc.status == "disabled")'),
        new=('    auto_disabled = acc.status == "disabled"'),
        tests=["tests/test_upstream_1_0_5.py::CredentialResetCase::"
               "test_manually_disabled_account_stays_disabled"],
        why="把「手动停用」也自动启用 → 用户明确停掉的账号被偷偷打开",
        tags=["upstream", "account"],
    ),
    Mutation(
        name="midnight-not-used",
        file="admin/routers/proxy.py",
        old=('            if _json_code(msg) == "6008" and '
             '(reset_at is None or reset_at <= now):'),
        new='            if False:',
        tests=["tests/test_upstream_1_0_5.py"],
        why="6008 不用次日零点 → 保守 24h 让账号白白多等几小时",
        tags=["upstream", "limits"],
    ),
    Mutation(
        name="error-describe-fabricates",
        file="admin/upstream_errors.py",
        old=('    if not item:\n'
             '        return {"message": "", "description": "", "known": False}'),
        new=('    if not item:\n'
             '        return {"message": "未知错误", "description": "", '
             '"known": False}'),
        tests=["tests/test_upstream_1_0_5.py::UpstreamErrorsCase::"
               "test_unknown_code_returns_empty_not_fabricated"],
        why="未知码编造解释 → 用户被错误信息误导",
        tags=["upstream", "errors"],
    ),
    Mutation(
        name="error-explain-not-wired",
        file="admin/routers/proxy.py",
        old=('            if got["known"] and got["message"]:\n'
             '                detail = f"（上游 {code}：{got[\'message\']}）"'),
        new=('            if False:\n'
             '                detail = ""'),
        tests=["tests/test_upstream_1_0_5.py::ExhaustionMessageCase"],
        why="解释函数写好了但没接进用户可见消息（典型假绿形态）",
        tags=["upstream", "wiring"],
    ),
    # ── 安卓打包（staging 与 server.py import 必须一致）────────────
    Mutation(
        name="android-excludes-module",
        file="scripts/build_android.py",
        old=("        target = staging / relative\n"
             "        target.parent.mkdir(parents=True, exist_ok=True)\n"
             "        shutil.copy2(path, target)"),
        new=("        if path.name in ('app_source.py',):\n"
             "            continue\n"
             "        target = staging / relative\n"
             "        target.parent.mkdir(parents=True, exist_ok=True)\n"
             "        shutil.copy2(path, target)"),
        tests=["tests/test_android_packaging.py::StagingMatchesServerImportsCase"],
        why="排除 app_source.py → server.py 无条件 import 它 → APK 启动 ImportError"
            "（CI 实测踩过：应用根本起不来）",
        tags=["android", "packaging"],
    ),
    Mutation(
        name="android-check-requires-panel",
        file="scripts/check_runtime.py",
        old=("    panel_checks = [(\"/api/app/version\", \"版本\"), (\"/admin\", \"旧面板\")]\n"
             "    # 安卓与其它形态一样都有新面板；只有「明确只有旧面板」时才跳过\n"
             "    if not (args.android and os.getenv(\"ADMIN_DEFAULT_PANEL\") == \"legacy\"):\n"
             "        panel_checks.append((\"/panel/\", \"新面板\"))"),
        new=("    panel_checks = [(\"/api/app/version\", \"版本\"), (\"/admin\", \"旧面板\")]\n"
             "    if not args.android:\n"
             "        panel_checks.append((\"/panel/\", \"新面板\"))"),
        tests=["tests/test_android_packaging.py::AndroidCheckMatchesPackagingCase"],
        why="安卓也打包新面板了，自检却跳过 /panel/ → 白屏时 CI 检不出来",
        tags=["android", "packaging"],
    ),
    Mutation(
        name="android-pip-mirror-hardcoded",
        file="scripts/build_android.py",
        old=("    index_url = os.getenv('WB_PIP_INDEX_URL',\n"
             "                          'https://pypi.tuna.tsinghua.edu.cn/simple')"),
        new="    index_url = 'https://pypi.tuna.tsinghua.edu.cn/simple'",
        tests=["tests/test_android_packaging.py::PipIndexConfigurableCase"],
        why="写死国内镜像 → GitHub Actions（海外 runner）连它 ReadTimeout",
        tags=["android", "ci"],
    ),
    # ── 签名脚本的临时目录（CI runner 的 ACL 限制）──────────────────
    Mutation(
        name="sign-uses-system-temp",
        file="scripts/sign_release.py",
        old="    with _work_dir(\"wb-sign-\") as td:",
        new="    with tempfile.TemporaryDirectory(prefix=\"wb-sign-\") as td:",
        tests=["tests/test_sign_release.py::WorkDirCase"],
        why="用系统 TEMP → GitHub Actions 的 Windows runner 上 ACL 是"
            "\\OWNER RIGHTS，ssh-keygen 直接拒绝签名（本地复现不了）",
        tags=["sign", "ci"],
    ),
    # ── 文档（链接必须真实、数字不能过期）──────────────────────────
    Mutation(
        name="docs-mutation-count-stale",
        file="docs-dev/ENGINEERING-BASELINE.md",
        # 数字会随 mutation 增删变化，所以**动态读当前值**再改成离谱的 999。
        # 硬编码某一个具体数字的话，每加一条 mutation 这条锚点就失效。
        old="| 反向验证 mutation | **{n} 条**（全部有效） |",
        new="| 反向验证 mutation | **999 条**（全部有效） |",
        tests=["tests/test_docs_links.py::DocumentedNumbersCase"],
        why="文档里的数字过期 → 读者按 99 条去找却只有 34 条，"
            "从此不再相信文档里的任何数字",
        tags=["docs"],
    ),
    Mutation(
        name="docs-broken-link",
        file="docs-dev/README.md",
        old="| [realm.md](realm.md) |",
        new="| [realm.md](realm-missing.md) |",
        tests=["tests/test_docs_links.py::DocsDevLinksCase"],
        why="索引指向不存在的文件 → 点进去 404"
            "（realm.md 等 4 份文档曾长期这样）",
        tags=["docs"],
    ),
    Mutation(
        name="plan-test-count-stale",
        file="../PLAN-fork.md",
        # 同上：动态读当前文件数，避免每加一个测试文件就失效
        old="| 测试文件 | **{n} 个** |",
        new="| 测试文件 | **999 个** |",
        tests=["tests/test_docs_links.py::PlanFileNumbersCase"],
        why="计划文件里的数字过期 → 它是判断进度最常用的依据，"
            "数字不可信就等于进度不可信",
        tags=["docs"],
    ),
    # ── Anthropic 工具配对顺序（上游 06e1b46 的同一个 bug）──────────
    Mutation(
        name="anthropic-tool-order",
        file="anthropic_adapter.py",
        old='            result.append({"role": "user", "content": content})',
        new='            result.insert(0, {"role": "user", "content": content})',
        tests=["tests/test_anthropic_tool_ordering.py::ToolOrderingCase"],
        why="把 user 文本插到 tool 结果之前 → 切断 assistant.tool_calls 与 "
            "role:tool 的紧邻关系 → 上游报 11148（Claude Code 子 Agent 必现，"
            "普通对话不触发所以极难发现）",
        tags=["anthropic", "safety"],
    ),
    Mutation(
        name="anthropic-empty-tool-id",
        file="anthropic_adapter.py",
        old=('                if not tc_id or not str(tc_id).strip():\n'
             '                    continue\n'),
        new="",
        tests=["tests/test_anthropic_tool_ordering.py::"
               "ToolOrderingCase::test_empty_tool_use_id_dropped"],
        why="保留空 tool_use_id → 上游看到无法配对的 tool 消息，同样 11148",
        tags=["anthropic", "safety"],
    ),
    # ── 安卓 WebView 引导层（真机实测发现）──────────────────────────
    Mutation(
        name="android-missing-export",
        file="admin/static/desktop.js",
        old="    checkUpdatesAtStartup: () => {",
        new="    _checkUpdatesAtStartup: () => {",
        tests=["tests/test_android_webview_boot.py::WbDesktopExportsCase"],
        why="mobile.js 调用 WBDesktop.checkUpdatesAtStartup 但没导出 → "
            "TypeError → 引导层永不消失 → 界面永久卡在「正在打开本地工作台」"
            "（OnePlus 9 真机实测）",
        tags=["android", "webview"],
    ),
    Mutation(
        name="android-overlay-order",
        file="android/web/mobile.js",
        old=("      document.getElementById('localBoot')?.remove();\n"
             "      try { enterApp(); }"),
        new=("      try { enterApp(); }\n"
             "      document.getElementById('localBoot')?.remove();"),
        tests=["tests/test_android_webview_boot.py::LoadingOverlayCase"],
        why="把移除引导层放到 enterApp 之后 → enterApp 一抛异常，"
            "用户永久卡在启动画面（服务其实是好的，且 WebView 不转发 console）",
        tags=["android", "webview"],
    ),
    # ── 安卓新面板打包（两套面板 + 落点 + token key 兼容）──────────
    Mutation(
        name="android-no-panel-copy",
        file="scripts/build_android.py",
        old="        shutil.copytree(web_out, staging / \"web/out\")",
        new="        pass  # (mutation) 不复制新面板",
        tests=["tests/test_android_panel_bundle.py::PackagingCase"],
        why="不把 web/out 复制进 staging → APK 里没有新面板，"
            "而 runtime 又设了 DEFAULT_PANEL=next → 白屏"
            "（这是「改了打包忘了 runtime」或反之的典型）",
        tags=["android", "packaging"],
    ),
    Mutation(
        name="android-isadmin-no-panel",
        file="android/app/src/main/java/io/github/bingyangl1/workbuddy2api/MainActivity.java",
        old=("        return path.equals(\"/admin\") || path.startsWith(\"/admin/\")\n"
             "            || path.equals(\"/panel\") || path.startsWith(\"/panel/\");"),
        new="        return path.equals(\"/admin\") || path.startsWith(\"/admin/\");",
        tests=["tests/test_android_panel_bundle.py::LandingCase"],
        why="isAdmin 只认 /admin → 新面板拿不到 token 注入 → "
            "永久卡在「正在打开工作台」引导层（真机实测踩过）",
        tags=["android", "safety"],
    ),
    Mutation(
        name="android-default-legacy",
        file="android/app/src/main/java/io/github/bingyangl1/workbuddy2api/MainActivity.java",
        old='    private static final String DEFAULT_PANEL = "next";',
        new='    private static final String DEFAULT_PANEL = "legacy";',
        tests=["tests/test_android_panel_bundle.py::LandingCase::"
               "test_default_panel_is_next"],
        why="默认落点改回旧面板 → 与桌面端不一致，用户以为新 UI 没生效",
        tags=["android"],
    ),
    Mutation(
        name="android-token-key-single",
        file="android/web/mobile.js",
        old="    try { localStorage.setItem('wb-admin-token', token); } "
            "catch (e) { /* 隐私模式 */ }",
        new="    // (mutation) 不写新面板的 key",
        tests=["tests/test_android_panel_bundle.py::TokenKeyCompatCase"],
        why="只写旧面板的 key → 新面板读不到 token → 停在登录页",
        tags=["android", "safety"],
    ),
    # ── 两版计费隔离（同名模型倍率不同）──────────────────────────
    Mutation(
        name="billing-bare-model-lookup",
        file="admin/routers/proxy.py",
        old=("                req_realm, req_bare = "
             "realm_mod.resolve_model_prefix(req)"),
        new=("                req_realm, req_bare = None, req  "
             "# (mutation) 不解析 realm，一律按裸名查"),
        tests=["tests/test_realm_billing_isolation.py"
               "::ProxyUsesRequestedModelCase"],
        why="用裸名查倍率 → 永远命中 cn 那行 → 国际版免费模型被按国内版"
            " 0.29 扣费（用户多付积分）",
        tags=["realm", "billing"],
    ),
    # ── model_configs 的 realm 列与迁移 ────────────────────────────
    Mutation(
        name="model-realm-no-migrate",
        file="admin/db.py",
        old="        _migrate_model_realm()",
        new="        pass  # (mutation) 不跑 realm 迁移",
        tests=["tests/test_model_realm_migration.py"
               "::MigrationHelpersCase::test_called_in_init_db"],
        why="不把老的 global: 前缀搬到 realm 列 → 老库升级后 "
            "国际版模型仍靠前缀、新查询按 realm 列查不到 → 国际版看不到模型",
        tags=["db", "realm"],
    ),
    Mutation(
        name="model-realm-no-unique",
        file="admin/db.py",
        old=("        _ensure_unique(\"model_configs\", "
             "\"uq_model_configs_level_realm_model\",\n"
             "                       (\"level\", \"realm\", \"model_id\"))"),
        new="        pass  # (mutation) 不建唯一约束",
        tests=["tests/test_model_realm_migration.py"
               "::MigrationHelpersCase::test_called_in_init_db"],
        why="不建唯一约束 → 手写 SQL / 并发同步能插重复行 → "
            "同一个模型被算两次、倍率随机命中一条",
        tags=["db", "realm"],
    ),
    # ── #5/#2/#4/#6 用户需求（2026-10 第二批）──────────────────────
    Mutation(
        name="usage-log-no-realm",
        file="admin/routers/proxy.py",
        old=("                seq=seq, ttfb_ms=ttfb_ms, latency_ms=latency_ms, error_kind=error_kind or \"\",\n"
             "                realm=billing_realm,"),
        new="                seq=seq, ttfb_ms=ttfb_ms, latency_ms=latency_ms, error_kind=error_kind or \"\",  # (mutation) 不记 realm",
        tests=["tests/test_panel_requirements.py::UsageLogRealmCase"
               "::test_record_usage_writes_realm"],
        why="日志不记版本 → 两版同名模型混计，用户看统计被误导",
        tags=["realm", "billing"],
    ),
    Mutation(
        name="group-dialog-loses-multiplier",
        file="web/app/(main)/models/page.tsx",
        old="      <GroupEditDialog target={groupEdit} allModels={configs}",
        new="      <GroupEditDialog target={groupEdit} allModels={configs.map((c) => c.model_id) as never}",
        tests=["tests/test_panel_requirements.py::GroupDialogMultiplierCase"
               "::test_dialog_receives_full_configs"],
        why="只传 model_id 字符串数组时倍率被丢弃，按价格建分组无从判断",
        tags=["panel"],
    ),
    Mutation(
        name="playground-no-realm-guard",
        file="admin/routers/playground.py",
        old=("        if want_realm and acc_realm and acc_realm != want_realm:\n"
             "            return JSONResponse(\n"
             "                status_code=400,"),
        new=("        if False and want_realm and acc_realm != want_realm:\n"
             "            return JSONResponse(\n"
             "                status_code=400,"),
        tests=["tests/test_panel_requirements.py::PlaygroundAccountCase"
               "::test_backend_realm_guard"],
        why="拿 cn 账号测 global 模型 → 混用身份出站（风控特征）",
        tags=["realm", "safety"],
    ),
    Mutation(
        name="appsource-no-realms",
        file="admin/routers/app_source.py",
        old="        \"paths_config\": wb_paths.current(),\n        \"realms\": realms_out,",
        new="        \"paths_config\": wb_paths.current(),  # (mutation) 不暴露 realms",
        tests=["tests/test_panel_requirements.py::AppSourceRealmCase"
               "::test_status_exposes_realms"],
        why="status 不返回两版目录 → 用户以为另一版没装",
        tags=["panel"],
    ),
    # ── 数据库后端 ──────────────────────────────────────────────────
    Mutation(
        name="sqlite-pragmas",
        file="admin/db.py",
        old=('        connection.execute("PRAGMA journal_mode=WAL")\n'
             '        connection.execute("PRAGMA busy_timeout=30000")\n'
             '        connection.execute("PRAGMA synchronous=NORMAL")'),
        new="        pass",
        tests=["tests/test_db_backends.py::SqliteDialectBranchesCase"],
        why="去掉 WAL/busy_timeout → SQLite 并发读写会 database is locked（手机端必崩）",
        tags=["db", "sqlite"],
    ),
    Mutation(
        name="sqlite-parent-dir",
        file="admin/db.py",
        old=('if settings.DATABASE_URL.startswith("sqlite"):\n'
             '    _sqlite_ensure_parent_dir(settings.DATABASE_URL)'),
        new="pass",
        tests=["tests/test_db_backends.py::SqliteParentDirBehaviorCase"],
        why="不自动建父目录 → 嵌套路径首次启动直接 unable to open database file",
        tags=["db", "sqlite"],
    ),
    Mutation(
        name="sqlite-pool-args",
        file="admin/db.py",
        old=('    if url.startswith("sqlite"):\n'
             '        kwargs: dict = {"pool_pre_ping": True, "future": True}'),
        new=('    if url.startswith("sqlite"):\n'
             '        kwargs: dict = {"pool_size": 20, "max_overflow": 40,\n'
             '                         "pool_timeout": 30, "future": True}'),
        tests=["tests/test_db_backends.py::EngineCreationCase"],
        why="SQLite 传入 QueuePool 参数 → 启动直接 TypeError"
            "（PC/安卓/单机部署的默认形态一启动就崩）",
        tags=["db", "sqlite", "startup"],
    ),
    Mutation(
        name="redis-hard-dependency",
        file="admin/gateway_limits.py",
        old=("                    if backend == 'redis':\n                        raise"),
        new="                    raise",
        tests=["tests/test_db_backends.py::RedisOptionalCase"],
        why="Redis 变硬依赖 → 不装就起不来（手机/单机场景直接废掉）",
        tags=["db", "redis"],
    ),
    # ── 桌面端 ──────────────────────────────────────────────────────
    Mutation(
        name="desktop-login-legacy-only",
        file="desktop/app.py",
        old='_DESKTOP_LOGIN_PREFIXES = ("/admin", "/panel")',
        new='_DESKTOP_LOGIN_PREFIXES = ("/admin",)',
        tests=["tests/test_desktop_panel_p6.py::DesktopLoginAllowlistCase"],
        why="只认旧面板 → 桌面版打开新面板停在登录页",
        tags=["desktop"],
    ),
    Mutation(
        name="desktop-bridge-no-timeout",
        file="web/lib/desktop-bridge.ts",
        old="const res = await Promise.race([",
        new="const res = await (",
        tests=["tests/test_desktop_panel_p6.py::FrontendBridgeCase::test_bridge_call_has_timeout"],
        why="桥调用没超时 → 页面导航后 Promise 永不 settle，UI 永久卡住",
        tags=["desktop"],
    ),
    Mutation(
        name="desktop-smoke-legacy-only",
        file="desktop/app.py",
        old='            "var n=0;"',
        new='            "return false;"',
        tests=["tests/test_desktop_panel_p6.py::SmokeTestBothPanelsCase"],
        why="冒烟只认旧面板 → next 构建的 release 永远失败",
        tags=["desktop"],
    ),
    # ── i18n ────────────────────────────────────────────────────────
    Mutation(
        name="i18n-dotted-key",
        file="web/lib/i18n/locales/zh-CN.json",
        old='"st": {',
        new='"st.placeholder": "x",\n    "st": {',
        tests=["tests/test_panel_update_ui.py::I18nCase::test_no_dotted_flat_keys"],
        why="带点的扁平 key → lookup 逐层下钻匹配不到，界面直接显示 key 名",
        tags=["i18n"],
    ),
    # ── 移动端 ──────────────────────────────────────────────────────
    Mutation(
        name="mobile-no-portal",
        file="web/components/common/layout/Sidebar.tsx",
        old="      {mounted && drawer && createPortal(drawer, document.body)}",
        new="      {mounted && drawer}",
        tests=["tests/test_mobile_runtime.py::MobileLayoutCase"],
        why="抽屉不 portal → 高度塌成 header 高度（实测 52px，菜单看不见）",
        tags=["mobile"],
    ),
]


def _is_ascii(s: str) -> bool:
    try:
        s.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def run_pytest(selectors: list[str], env: dict) -> tuple[int, str]:
    """跑 pytest，返回 (returncode, 输出尾部)。"""
    cmd = [sys.executable, "-m", "pytest", *selectors, "-q", "--no-header", "-x"]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, env=env)
    out = (proc.stdout or b"").decode("utf-8", "ignore")
    err = (proc.stderr or b"").decode("utf-8", "ignore")
    tail = "\n".join((out + err).strip().splitlines()[-4:])
    return proc.returncode, tail


def _resolve_anchor(m: Mutation, text: str) -> tuple[str, str] | None:
    """把锚点里的 `{n}` 占位符解析成文档中的**当前数字**。

    ## 为什么需要这个

    有些 mutation 注入的是「文档里写过期的数字」，锚点自然包含那个数字。
    但数字会随「加了几个测试文件 / 加了几条 mutation」变化 ——
    硬编码某个具体数字的话，每变一次锚点就失效（实测踩过两次）。

    用 `{n}` 占位后：从目标文件里按**同一行的其余文字**反查出当前数字，
    再用它拼出真实锚点。

    返回 `(old, new)`；解析不出来返回 None（调用方按锚点失效处理）。
    """
    if "{n}" not in m.old and "{n}" not in m.new:
        return (m.old, m.new)

    import re as _re
    # 把 `{n}` 换成正则分组，其余部分转义
    pattern = _re.escape(m.old).replace(_re.escape("{n}"), r"(\d+)")
    hit = _re.search(pattern, text)
    if not hit:
        return None
    n = hit.group(1)
    return (m.old.replace("{n}", n), m.new.replace("{n}", n))


def verify_one(m: Mutation, env: dict, verbose: bool) -> dict:
    """验证一条 mutation：注入 → 测试必须红 → 还原 → 测试必须绿。"""
    target = ROOT / m.file
    result = {"name": m.name, "ok": False, "stage": "", "detail": ""}

    if not target.is_file():
        result.update(stage="missing", detail=f"文件不存在: {m.file}")
        return result

    original = target.read_text(encoding="utf-8")
    resolved = _resolve_anchor(m, original)
    if resolved is None:
        result.update(
            stage="anchor",
            detail=("锚点里的 `{n}` 解析失败 —— 目标文件里找不到匹配行。"
                    "文档被改过？还是占位符写错了？"))
        return result
    old, new = resolved
    occurrences = original.count(old)
    if occurrences != 1:
        result.update(
            stage="anchor",
            detail=f"锚点出现 {occurrences} 次（必须恰好 1 次）—— 代码改过了？请更新清单")
        return result

    # 备份到临时文件（比内存更可靠：进程被 kill 也能人工找回）
    #
    # ⚠️ 必须 `os.close(fd)`：`tempfile.mkstemp()` 返回的是**已打开的 fd**，
    # 不关掉的话 Windows 上这个文件一直被占用，后面 `unlink` 会报
    # `PermissionError: [WinError 32]`（Linux 上不会，属于平台差异坑）。
    fd, backup_name = tempfile.mkstemp(prefix="verify_bak_", suffix=".bak")
    os.close(fd)
    backup = Path(backup_name)
    shutil.copy2(target, backup)

    # 注意：写盘必须用 newline="" 保持行尾原样，否则 CRLF/LF 转换会让探针失配
    try:
        target.write_text(original.replace(old, new, 1),
                          encoding="utf-8", newline="")

        # ① 注入后：测试必须失败
        rc_bad, tail_bad = run_pytest(m.tests, env)
        if rc_bad == 0:
            result.update(
                stage="false-green",
                detail=("**注入 bug 后测试仍然通过 —— 这是假绿**\n"
                        f"      {tail_bad}"))
        else:
            # ② 还原后：测试必须通过
            shutil.copy2(backup, target)
            rc_good, tail_good = run_pytest(m.tests, env)
            if rc_good != 0:
                result.update(
                    stage="restore-broken",
                    detail=("还原后测试仍失败 —— 还原不干净或测试本身不稳\n"
                            f"      {tail_good}"))
            else:
                result.update(ok=True, stage="ok",
                              detail=f"注入后红 / 还原后绿")
    finally:
        # 无论如何都要还原（异常/中断时也不留坏代码）
        try:
            if target.read_text(encoding="utf-8") != original:
                shutil.copy2(backup, target)
        except Exception:
            shutil.copy2(backup, target)
        try:
            backup.unlink(missing_ok=True)
        except OSError:
            pass      # 极端情况下删不掉，留个临时文件无害（在系统 temp 目录）

    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="反向验证：把实现改坏，确认测试真的会红（防假绿）")
    ap.add_argument("--list", action="store_true", help="只列出清单，不执行")
    ap.add_argument("--only", default="", help="按名字/标签过滤（子串匹配）")
    ap.add_argument("--check", action="store_true",
                    help="CI 模式：有假绿时退出码 1")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    selected = MUTATIONS
    if args.only:
        key = args.only.lower()
        selected = [m for m in MUTATIONS
                    if key in m.name.lower()
                    or any(key in t for t in m.tags)]

    if args.list:
        print(f"共 {len(MUTATIONS)} 条 mutation（筛选后 {len(selected)} 条）\n")
        for m in selected:
            print(f"  {m.name:<34} {m.file}")
            print(f"      tags: {', '.join(m.tags)}")
            print(f"      为什么: {m.why}")
            print(f"      测试  : {', '.join(m.tests)}")
            print()
        return 0

    # 安全兜底：即便工具本身不发 HTTP，也把上游彻底隔离
    env = dict(os.environ)
    env["ADMIN_UPSTREAM_READONLY"] = "1"
    env["ADMIN_UPSTREAM_READONLY_ALL"] = "1"
    env.setdefault("ADMIN_JWT_SECRET", "verify-tests-secret-placeholder-000000")

    print("=" * 92)
    print(f"反向验证：{len(selected)} 条 mutation")
    print("=" * 92)
    print("  原理：把实现故意改坏 → 跑测试 → **必须变红** → 还原 → 必须变绿")
    print("  安全：只改本地文件 + 跑 pytest，不发任何 HTTP 请求")
    print(f"       已注入 ADMIN_UPSTREAM_READONLY_ALL=1 作为兜底")
    print()

    results = []
    for i, m in enumerate(selected, 1):
        print(f"[{i}/{len(selected)}] {m.name}")
        if args.verbose:
            print(f"      {m.why}")
        r = verify_one(m, env, args.verbose)
        results.append(r)
        mark = "✅" if r["ok"] else "❌"
        print(f"   {mark} {r['stage']}  {r['detail']}")
        print()

    ok_n = sum(1 for r in results if r["ok"])
    bad = [r for r in results if not r["ok"]]

    print("=" * 92)
    print("汇总")
    print("=" * 92)
    print(f"   ✅ 有效（注入后必红）: {ok_n}/{len(results)}")
    if bad:
        print(f"   ❌ 有问题: {len(bad)}")
        for r in bad:
            print(f"      - {r['name']}  [{r['stage']}]")
            for line in r["detail"].splitlines():
                print(f"        {line}")
    print()

    if not bad:
        print("   ✅ 全部测试都真的能抓到 bug（无假绿）")
        return 0
    print("   ❌ 存在假绿 / 锚点失效，见上")
    return 1 if args.check else 0


if __name__ == "__main__":
    sys.exit(main())
