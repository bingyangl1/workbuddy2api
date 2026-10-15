# -*- coding: utf-8 -*-
"""面板开发 fixtures：无账号环境下往**本地 SQLite dev 库**灌确定性假数据，
让 14 个前端页面都有内容可看——零上游触达、零真实凭据。

字段 100% 对齐 admin/models.py 真实 ORM（success_count/endpoint/display_name
这类是 manager 的字段，本引擎没有——一律不造）。

用法（独立 SQLite，不碰真 dev 库）：
    set ADMIN_DATABASE_URL=sqlite:///./dev_fixtures.db
    python dev/seed_fixtures.py
    python main.py

铁律：本脚本只写本地 SQLite，import 任何上游调用都是 bug。
"""
from __future__ import annotations

import base64
import json
import os
import random
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.environ.get("ADMIN_DATABASE_URL", "")
if not _DB.startswith("sqlite"):
    raise SystemExit(
        "seed_fixtures 只在 SQLite 上跑（ADMIN_DATABASE_URL 须 sqlite:///...）。"
        "当前：%r" % _DB)

from admin.db import Base, SessionLocal, engine, init_db  # noqa: E402
from admin import models as M  # noqa: E402

random.seed(20270221)  # 确定性：每次灌出同一份数据，截图可复现
NOW = datetime.utcnow()


def _reset():
    Base.metadata.drop_all(bind=engine)
    init_db()


def _accounts(db):
    """10 个假账号：9 cn + 1 global，覆盖 active/冷却/熔断/降权/零余额/停用。"""
    fake_auth = json.dumps({"token": "fixture-not-a-real-token", "uid": "0"})
    rows = []
    specs = [
        # name, realm, status, remain, 特殊状态
        ("黑天鹅", "cn", "active", 1300, None),
        ("测试号", "cn", "active", 148, None),
        ("运营小组", "cn", "active", 2600, None),
        ("备用账号", "cn", "active", 0, None),                 # 零余额
        ("边缘节点", "cn", "active", 880, None),
        ("冷却中", "cn", "active", 500, "cool"),
        ("熔断账号", "cn", "active", 300, "breaker"),
        ("降权账号", "cn", "active", 200, "degrade"),
        ("停用号", "cn", "disabled", 0, None),
        ("GlobalOne", "global", "active", 1500, None),
    ]
    for i, (name, realm, status, bal, special) in enumerate(specs, start=1):
        a = M.Account(
            name=name, uid=f"fixture{10000000 + i}", realm=realm,
            enterprise_id=f"ent{i:04d}", domain="example.com",
            auth_json=fake_auth, status=status,
            balance_total=bal + 500, balance_remain=bal,
            credits_expiring_soon=bal // 4, credits_evergreen=bal // 2,
            credits_synced_at=NOW - timedelta(minutes=i * 3),
            last_sync_at=NOW - timedelta(minutes=i * 7),
            last_used_at=NOW - timedelta(hours=i))
        if special == "cool":
            a.cool_until = NOW + timedelta(minutes=25)
            a.cool_kind = "soft_rate"
            a.last_err_msg = "fixture: rate limited"
            a.last_err_at = NOW - timedelta(minutes=5)
        elif special == "breaker":
            a.breaker_until = NOW + timedelta(minutes=40)
            a.breaker_fails = 5
            a.err_count = 5
            a.last_err_msg = "fixture: upstream 5xx x5"
        elif special == "degrade":
            a.degrade_until = NOW + timedelta(hours=2)
            a.consecutive_fails = 3
        rows.append(a)
    db.add_all(rows)
    db.commit()
    return rows


def _models(db):
    """模型配置：30 个（对齐 dev 实测数量），覆盖免费/付费/启停用。"""
    names = ["deepseek-v3-2-volc", "deepseek-v4-flash", "deepseek-v4-pro",
             "deepseek-v4.1-flash", "default", "glm-4.6", "glm-4.6v", "glm-4.7",
             "glm-5.0", "glm-5.1", "glm-5.2", "glm-5.3", "glm-5.3-flash",
             "glm-5v-turbo", "hunyuan-2.0-thinking", "hunyuan-chat",
             "hunyuan-image-alpha", "hy3", "hy3-x", "hy4-preview", "hy4-preview-x",
             "kimi-k2-thinking", "kimi-k2.5", "kimi-k2.6", "kimi-k2.7",
             "minimax-m3", "minimax-m3.5", "qwen3-coder", "qwen3-max", "default-v2"]
    rows = []
    for i, m in enumerate(names):
        free = (i % 5 == 0)
        mult = 0.0 if free else float(1 + (i % 3))
        rows.append(M.ModelConfig(
            model_id=m, level="system", enabled=0 if i % 7 == 6 else 1,
            credit_multiplier=mult,
            credits_raw=f"x{mult:.2f}" if mult else "x0.00 credits"))
    db.add_all(rows)
    db.commit()


def _keys(db):
    """3 把 API Key：对齐真实字段 key_hash/key_prefix/key_full/credit_limit。"""
    rows = []
    specs = [("生产 Key", 100.0, 0), ("测试 Key", 10.0, 0), ("无限 Key", 0.0, 1)]
    for i, (name, limit, unlimited) in enumerate(specs, start=1):
        full = f"wb-fixture-key-{i:02d}-" + "x" * 32
        rows.append(M.ApiKey(
            name=name,
            # key_hash 全局唯一：用序号散列（sha256），不是拼接截断（会撞）
            key_hash=__import__("hashlib").sha256(full.encode()).hexdigest(),
            key_prefix=full[:12],
            key_full=base64.b64encode(full.encode()).decode(),
            credit_limit=limit, credit_used=round(limit * 0.1 * i, 2),
            unlimited=unlimited, status="active",
            token_quota=1000000 if i == 1 else 0,
            used_tokens=int(1000000 * 0.08) if i == 1 else 0,
            note=f"fixture key {i}",
            created_at=NOW - timedelta(days=30 - i)))
    db.add_all(rows)
    db.commit()
    return rows


def _usage(db, accounts, keys):
    """近 14 天用量日志：让 stats 趋势/分布/模型健康/dashboard 全有数据。"""
    models = ["glm-5.2", "deepseek-v4-pro", "kimi-k2.7", "glm-5v-turbo",
              "hunyuan-chat", "minimax-m3"]
    active = [a for a in accounts if a.status == "active" and a.balance_remain > 0]
    # error_kind 口径（模型注释）：hard_credit|soft_rate|server|not_found|
    # session_dead|transport|client|success；空串=历史未分类按成功计
    kinds = ["success"] * 7 + ["", "soft_rate", "server"]
    rows = []
    for day in range(14):
        base = NOW - timedelta(days=day)
        n = random.randint(10, 42) if day < 7 else random.randint(3, 12)
        for _ in range(n):
            acc = random.choice(active)
            key = random.choice(keys)
            model = random.choice(models)
            kind = random.choice(kinds)
            ok = kind in ("", "success")
            ptok = random.randint(50, 2000)
            ctok = random.randint(20, 1500)
            rows.append(M.UsageLog(
                api_key_id=key.id, account_id=acc.id, model=model,
                requested_model=model,
                prompt_tokens=ptok, completion_tokens=ctok,
                total_tokens=ptok + ctok,
                cached_tokens=random.randint(0, ptok // 2),
                credits=round((ptok + ctok) / 1000.0, 2) if ok else 0,
                client_ip=f"203.0.113.{random.randint(2, 250)}",  # TEST-NET-3 文档段
                use_case=random.choice(["chat", "code", "agent"]),
                ttfb_ms=random.randint(80, 900) if ok else None,
                latency_ms=random.randint(200, 4000) if ok else None,
                error_kind=kind,
                created_at=base - timedelta(
                    hours=random.randint(0, 23), minutes=random.randint(0, 59))))
    db.add_all(rows)
    db.commit()


def main():
    _reset()
    db = SessionLocal()
    try:
        accounts = _accounts(db)
        _models(db)
        keys = _keys(db)
        _usage(db, accounts, keys)
        print("fixtures 灌入完成：")
        print("  accounts:", db.query(M.Account).count())
        print("  model_configs:", db.query(M.ModelConfig).count())
        print("  api_keys:", db.query(M.ApiKey).count())
        print("  usage_logs:", db.query(M.UsageLog).count())
    finally:
        db.close()


if __name__ == "__main__":
    main()
