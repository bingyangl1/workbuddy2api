"""安全：管理后台 JWT 登录 + API Key 校验与配额判断。"""
import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta

import jwt
from fastapi import Header, HTTPException

from admin.config import settings
from admin.db import SessionLocal
from admin.models import ApiKey


# ---------------------------------------------------------------------------
# 管理后台登录
# ---------------------------------------------------------------------------

def create_admin_token() -> str:
    exp = datetime.utcnow() + timedelta(hours=settings.JWT_EXPIRE_HOURS)
    return jwt.encode({"sub": settings.ADMIN_USERNAME, "exp": exp},
                      settings.JWT_SECRET, algorithm="HS256")


def verify_admin_token(token: str | None):
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        if payload.get("sub") != settings.ADMIN_USERNAME:
            raise HTTPException(status_code=401, detail="无效令牌")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="无效令牌")


def require_admin(x_admin_token: str = Header(default=None, alias="X-Admin-Token")):
    """FastAPI 依赖：校验管理后台令牌。"""
    verify_admin_token(x_admin_token)
    return True


def require_admin_read(x_admin_token=Header(default=None, alias="X-Admin-Token"),
                       authorization: str = Header(default=None)):
    """读端点依赖：JWT 管理员 或 wbt_ 只读 token 双通道。

    JWT 全权（本引擎单管理员）；wbt_ 只授权 GET。
    T7 只实现依赖本体；T9 才批量套到既有 GET 端点。

    通道顺序：先走 X-Admin-Token（JWT）；失败再取
    `Authorization: Bearer wbt_...`（只认 Bearer，manager 同款，
    X-API-Key 历史提权后门绝不复活）→ tokensvc.resolve + usable。

    两者都失败 → 401「无效令牌」——**不区分**失败原因
    （不存在 / 已停用 / 已过期 / 格式错误对探测者不可分辨）。
    返回值："admin"（JWT 全权）或 ApiToken 行（wbt_ 只读），
    供路由层/审计区分操作者身份。
    """
    # 先试 JWT 通道：verify_admin_token 失败抛 401，这里捕获后走第二通道。
    try:
        verify_admin_token(x_admin_token)
        return "admin"
    except HTTPException:
        pass

    # wbt_ 只读通道：只认 Authorization: Bearer wbt_...
    if authorization:
        scheme, _, cred = authorization.partition(" ")
        if scheme.lower() == "bearer" and cred.startswith("wbt_"):
            # 局部 import 避免循环依赖：tokensvc 不得 import security。
            # SessionLocal 用本模块顶层已导入的名字（便于测试替换）。
            from admin import tokensvc

            db = SessionLocal()
            try:
                row = tokensvc.resolve(db, cred)
                if row is not None and tokensvc.usable(row):
                    tokensvc.touch(db, row.id, "")  # IP 留空；T9 挂 Request 时补
                    return row
            finally:
                db.close()

    # 不区分「JWT 无效 / token 不存在 / 停用 / 过期」——统一无效。
    raise HTTPException(status_code=401, detail="无效令牌")


# ---------------------------------------------------------------------------
# 管理后台密码（PBKDF2 哈希存储，杜绝明文泄漏）
# ---------------------------------------------------------------------------

_PBKDF2_ITER = 100_000


def hash_password(pw: str) -> str:
    """返回可持久化的哈希串：pbkdf2$<iter>$<salt_b64>$<dk_b64>。"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, _PBKDF2_ITER)
    return "pbkdf2$" + str(_PBKDF2_ITER) + "$" + \
        base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(pw: str, stored: str) -> bool:
    """校验密码；兼容旧明文存储（首次读取时由 server 统一迁移为哈希）。"""
    if not stored:
        return False
    if not stored.startswith("pbkdf2$"):
        return hmac.compare_digest(pw, stored)  # 旧明文兜底
    try:
        _, iter_s, salt_b64, hash_b64 = stored.split("$", 3)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        cand = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, int(iter_s))
        return hmac.compare_digest(cand, expected)
    except Exception:
        return False


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def get_key_row(db, api_key: str) -> ApiKey | None:
    return db.query(ApiKey).filter(ApiKey.key_hash == hash_key(api_key)).first()


def check_quota(key: ApiKey) -> None:
    """超额则直接拒绝，提示『积分已耗尽』。"""
    if key.status != "active":
        raise HTTPException(status_code=403, detail={"error": {"message": "API Key 已停用", "type": "key_disabled"}})
    if not key.unlimited and key.credit_used >= key.credit_limit:
        raise HTTPException(
            status_code=402,
            detail={"error": {"message": "积分已耗尽", "type": "quota_exceeded"}},
        )
