"""管理面作用域 API Token（wbt_ 前缀）：生成、解析与校验（Phase 2 T7）。

语义基准：workbuddy-manager 的 server/tokensvc.py（docs/api-tokens.md），
但针对本引擎做了三处**刻意**适配（也是与 manager 的全部差异）：

  1. **scope 一律 readonly**。本引擎管理端没有 admin 角色（单用户 JWT，
     见 admin/security.py 的 verify_admin_token），wbt_ 永远只读；
     非法 scope 值**降级为 readonly 而不报错**（manager 同款「宁少勿多」
     原则——宁可给少了权限，也不要因为拼写错误意外提权）。
  2. **expires_at 是 DateTime 对象**（本引擎 ORM 列类型），manager 是
     int 时间戳。API 层负责 ISO 字符串 ↔ datetime 的转换（见
     admin/routers/tokens.py）。
  3. **db 参数注入**。本引擎是 SQLAlchemy Session 注入风格（get_db
     依赖），tokensvc 不自建连接；manager 的 db 模块是模块内建连接。

安全要求（与 manager 一致，管理面 token 授权的是 /api/*，比网关密钥
wbk_/sk- 敏感）：

  * 明文只在创建时返回一次，库中只存 SHA-256 哈希；
  * 按 prefix 定位 + 常量时间比较（secrets.compare_digest），不全表扫描；
  * 失败时**不区分**「不存在 / 已停用 / 已过期」——都回「无效令牌」，
    不给探测者任何信号。

本模块不得 import admin.security（require_admin_read 局部反引本模块，
顶层互引会成环）。
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from admin.models import ApiToken

# 与网关密钥 sk_ / wbk_ 刻意区分，避免两类凭据在同一处被混用。
TOKEN_PREFIX = "wbt_"

# 本引擎唯一合法 scope。manager 有 ('readonly', 'admin') 两档，这里没有
# admin 档——单用户 JWT 已是全权，token 只承担只读共享场景。
SCOPE_READONLY = "readonly"

# last_used 写库节流（秒）。管理面会被看板 30s 轮询，逐请求写库会打热。
LAST_USED_THROTTLE = 60


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_scope(value: object) -> str:
    """归一化 scope；非法值一律降级为 readonly（不报错）。

    manager 侧非法值降级为「最小权限」，本引擎最小权限就是唯一权限
    readonly——所以任何输入（包括 'admin'、拼写错误、None）都收敛到
    readonly。创建时传非 readonly 也存 readonly。
    """
    v = str(value or "").strip().lower()
    return v if v == SCOPE_READONLY else SCOPE_READONLY


def _parse(row: ApiToken) -> dict:
    """ORM 行 → 对外字典。datetime 序列化 ISO 字符串；永不回传明文或哈希。"""
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "scope": row.scope,
        "enabled": bool(row.enabled),
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "created_by": row.created_by,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "last_used_ip": row.last_used_ip,
    }


def list_tokens(db: Session) -> list[dict]:
    """列出全部令牌。**永不回传明文或哈希。**"""
    rows = db.query(ApiToken).order_by(ApiToken.id.desc()).all()
    return [_parse(r) for r in rows]


def create_token(db: Session, name: str, expires_at: datetime | None = None,
                 created_by: str = "") -> dict:
    """创建一个令牌。返回体里的 `token` 是**明文，仅此一次**。

    expires_at 是 datetime 对象（本引擎列类型；ISO 字符串在路由层 parse）。
    """
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = ApiToken(
        name=str(name)[:64],
        token_hash=_hash(token),
        prefix=token[:12],
        scope=SCOPE_READONLY,
        enabled=1,
        expires_at=expires_at,
        created_at=datetime.utcnow(),
        created_by=str(created_by or "")[:64],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    out = _parse(row)
    out["token"] = token  # 仅此一次
    return out


def update_token(db: Session, token_id: int, patch: dict) -> dict | None:
    """部分更新（PATCH 语义：只改真的提交了的字段）。不存在返回 None。

    expires_at 显式传 None 表示「改为永不过期」——不能用真值判断。
    """
    row = db.query(ApiToken).filter(ApiToken.id == token_id).first()
    if not row:
        return None
    if "name" in patch and patch["name"]:
        row.name = str(patch["name"])[:64]
    if "enabled" in patch:
        row.enabled = 1 if patch["enabled"] else 0
    if "scope" in patch:
        # 非法值降级 readonly（manager 同款「宁少勿多」），不报错。
        row.scope = normalize_scope(patch["scope"])
    if "expires_at" in patch:
        # 显式传 None = 永不过期；不能写成 `if patch['expires_at']`。
        row.expires_at = patch["expires_at"]
    db.commit()
    db.refresh(row)
    return _parse(row)


def delete_token(db: Session, token_id: int) -> bool:
    row = db.query(ApiToken).filter(ApiToken.id == token_id).first()
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True


def resolve(db: Session, token: object) -> ApiToken | None:
    """校验明文令牌：先按 prefix 定位候选行，再对哈希做常量时间比较。

    返回 ApiToken ORM 行（含 token_hash，仅内部使用）；不匹配返回 None。
    注意：resolve 只做「认得出」，不做「用得了」——可用性由 usable() 判定，
    两者分离让依赖层能对停用/过期统一回 401 而不泄露具体原因。
    """
    if not token or not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
        return None
    digest = _hash(token)
    for row in db.query(ApiToken).filter(ApiToken.prefix == token[:12]).all():
        if secrets.compare_digest(row.token_hash, digest):
            return row
    return None


def usable(row: ApiToken) -> bool:
    """令牌是否可用：已启用，且未过期（无 expires_at = 永不过期）。

    逐次校验、不做缓存——保证吊销（enabled=0）与过期**即时生效**。
    """
    if not row or not row.enabled:
        return False
    exp = row.expires_at
    return not exp or exp > datetime.utcnow()


def touch(db: Session, token_id: int, ip: str) -> None:
    """记录最近使用（**节流**：距上次记录不足 LAST_USED_THROTTLE 秒则跳过）。

    记账属于旁路，任何异常都不能影响请求本身（与请求日志同一原则）。
    """
    try:
        now = datetime.utcnow()
        row = db.query(ApiToken).filter(ApiToken.id == token_id).first()
        if not row:
            return
        if row.last_used_at and (now - row.last_used_at) < timedelta(seconds=LAST_USED_THROTTLE):
            return
        row.last_used_at = now
        row.last_used_ip = str(ip or "")[:64]
        db.commit()
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:
            pass
