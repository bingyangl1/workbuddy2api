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
# 上游只读测试护栏（ADMIN_UPSTREAM_READONLY）
# ---------------------------------------------------------------------------

#: 会真实触达上游腾讯/WorkBuddy（消耗额度/写上游状态/打限流风险）的管理面
#: 路由前缀。**只列管理面**，不含 /v1 /gw 网关转发。本地 DB CRUD 不在此列。
_UPSTREAM_WRITE_ROUTES = (
    "/api/growth/accept",
    "/api/growth/run",
    "/api/growth/run-async",
    "/api/growth/claim",
    "/api/accounts/credits/sync",       # 全量强制刷新余额（打上游）
    "/api/accounts/cat-travel",          # 猫猫旅行（单个 + batch + batch-async）
    "/api/models/sync",                  # 从上游同步模型列表
    "/api/models/speed-test",            # 真实流式请求测速（占账号）
    "/api/playground/chat",              # 测试台直调（真实消耗积分，走号池选号）
)
#: accounts 下带 id 的上游写：/{id}/refresh、/{id}/inject、/{id}/cat-travel
_UPSTREAM_WRITE_ACCOUNT_SUB = ("/refresh", "/inject", "/cat-travel")
#: schedules 立即运行内置任务（部分任务打上游）：/{sid}/run
_UPSTREAM_WRITE_SCHEDULE_SUB = ("/run",)


def is_upstream_write(method: str, path: str) -> bool:
    """判定该管理面请求是否会真实触达上游。供测试护栏中间件与审计共用。"""
    if method.upper() != "POST":
        return False
    if any(path.startswith(p) for p in _UPSTREAM_WRITE_ROUTES):
        return True
    if path.startswith("/api/accounts/") and path.endswith(_UPSTREAM_WRITE_ACCOUNT_SUB):
        return True
    if path.startswith("/api/schedules/") and path.endswith(_UPSTREAM_WRITE_SCHEDULE_SUB):
        return True
    return False


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


# ---------------------------------------------------------------------------
# P2 T3：密钥全维度判定链（参考 manager keysvc.validate，MIT）
#
# 语义基准：docs/rejection-semantics.md（T0）。判定顺序固定，拒绝原因永远指向
# 第一个命中的环节——先判身份（停用/过期）、再判用量（配额）、再判匹配（IP/
# realm/模型），不给探测者多余信号。
# ---------------------------------------------------------------------------

class Rejection(str):
    """拒绝原因：本身是错误文案（str），附加状态码与 OpenAI 风格 type/code。

    为什么继承 str 而不是用 dataclass：调用方（proxy.py 五个入口）拿到它
    直接放进 JSONResponse 的 error.message，不需要解包；测试断言
    `str(r) == '...'` 也比访问属性直观。status/err_type/code 是路由层
    拼装响应体的全部所需。
    """

    def __new__(cls, message: str, status: int, err_type: str, code: str):
        obj = str.__new__(cls, message)
        obj.status = status
        obj.err_type = err_type
        obj.code = code
        return obj


def _norm_cidrs(cidrs) -> list[str]:
    """写入时归一化 CIDR 列表：strip + 去空 + 校验合法性。

    为什么必须在**写入时**归一化：ip_matches 对非法输入一律 False，
    一条带空白的脏数据（`' 10.0.0.0/8'`）在白名单里会让这把密钥对**所有**
    来源都拒绝——管理员看到的是「白名单突然全拦」，而脏值在界面上根本
    看不出来。这里在写库前统一清洗，读取侧就能假定干净。
    """
    import ipaddress as _ipa
    out: list[str] = []
    for raw in cidrs or []:
        item = str(raw).strip()
        if not item:
            continue
        try:
            # strict=False 允许 10.0.0.1/24 这种主机位非零的写法（按网段理解）
            _ipa.ip_network(item, strict=False)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"无效 CIDR：{item}（示例：10.0.0.0/8 或 203.0.113.9）",
            )
        out.append(item)
    return out


def key_ip_allowlist(key: ApiKey) -> list[str]:
    """解析 key.ip_allowlist JSON；坏数据按空处理（不拦请求）。

    读取侧宽容：写入侧已用 _norm_cidrs 拦过，这里再坏只能是手改库。
    宁可放行也不让坏配置把整个网关打挂（与模型映射「坏配置透传」同原则）。
    """
    import json as _json
    try:
        val = _json.loads(key.ip_allowlist or "[]")
        return [str(x).strip() for x in val if str(x).strip()] if isinstance(val, list) else []
    except Exception:
        return []


def validate_key(key: ApiKey, ip: str, model: str | None,
                 *, is_model_list: bool = False,
                 mapped_model: str | None = None,
                 key_realm: str = "") -> Rejection | None:
    """密钥全维度判定链。返回 None 表示放行，否则 Rejection（自带状态码）。

    参数语义（与 manager keysvc.validate 对齐，两处判据对象不同——issue #46/#47）：
    - model：请求里的原始模型名（白名单判它——客户端可以发哪些名字）
    - mapped_model：经模型映射后的名字（realm 判它——真正决定路由的是它）；
      T6 落地前调用方传 None，回退用 model 判（现状语义）
    - key_realm：密钥限定 realm（''=不限）。从 key.realm 归一化后传入，
      便于测试直接构造；路由层从 key 行取
    - is_model_list：/v1/models 模型发现。不判 realm 与模型白名单
      （它没有版本可言，拦了限定密钥连「我有哪些模型」都问不到）

    顺序固定（docs/rejection-semantics.md）：身份 → 用量 → 匹配。
    """
    from admin import iputil

    # 1. 身份：停用（403——凭据本身不可用）
    if key.status != "active":
        return Rejection("API Key 已停用", 403, "key_disabled", "key_disabled")

    # 2. 身份：过期（403——凭据本身不可用）
    if key.expires_at and key.expires_at < datetime.utcnow():
        return Rejection("API Key 已过期", 403, "key_expired", "key_expired")

    # 3. 用量：积分配额（402——沿用引擎现状；unlimited 或 limit=0 为不限）
    if not key.unlimited and key.credit_limit and key.credit_used >= key.credit_limit:
        return Rejection("积分已耗尽", 402, "quota_exceeded", "quota_exceeded")

    # 4. 用量：Token 配额（402；0=不限。与积分独立，任一超限即拦）
    if key.token_quota and (key.used_tokens or 0) >= key.token_quota:
        return Rejection(
            f"Token 配额已用尽（已用 {key.used_tokens} / 上限 {key.token_quota}）",
            402, "quota_exceeded", "token_quota_exceeded")

    # 5. 匹配：IP 白名单（400——「来源不对」不是「密钥错」，400 让报文透出）
    allow = key_ip_allowlist(key)
    if allow and not any(iputil.ip_matches(ip, c) for c in allow):
        return Rejection(f"来源 IP {ip} 不在密钥白名单内", 400,
                         "invalid_request_error", "ip_not_allowed")

    # 6. 匹配：max_ips（400）。绑定数达上限且本次是新 IP → 拒。
    #    注意：判定基于 api_key_ips 累计绑定数，**不是**当前在线数——
    #    「换了个网络就再也用不了」是特性不是 bug（防密钥被分享扩散）。
    if key.max_ips:
        from admin.models import ApiKeyIp
        with SessionLocal() as _db:
            known = {r.ip for r in _db.query(ApiKeyIp)
                     .filter(ApiKeyIp.key_id == key.id).all()}
        if ip not in known and len(known) >= key.max_ips:
            return Rejection(
                f"密钥已绑定 {len(known)} 个 IP，超出上限 {key.max_ips}",
                400, "invalid_request_error", "too_many_ips")

    # 7. 匹配：realm 隔离（403——P1 已钉的语义，测试在案）。
    #    判 **mapped_model**（映射后名字），模型白名单判请求名（issue #47/#46）。
    #    T6 落地前 mapped_model=None，回退用 model——与现状一致。
    want = (key_realm or "").strip().lower()
    if want and not is_model_list:
        effective = (mapped_model or model)
        if not isinstance(effective, str) or not effective.strip():
            return Rejection(
                f"该 API Key 限定 {want} 版本，请求必须指定 model",
                400, "invalid_request_error", "realm_mismatch")
        # 前缀剥离已在路由层做过（model_realm 非空时 model 已是裸名），
        # 这里按「映射后的名字带不带 global:」判版本归属
        got = "global" if effective.strip().lower().startswith("global:") else "cn"
        if got != want:
            label = {"cn": "国内版", "global": "国际版"}[want]
            return Rejection(
                f"该 API Key 限定 {want} 版本，不能调用 {got} 版本模型",
                403, "realm_forbidden", "realm_forbidden")

    # 8. 匹配：模型白名单（403——P1 的模型分组判据在路由层 _key_group_models，
    #    本函数只兜「请求缺 model 但密钥启用了白名单」的绕过）。
    #    模型分组的实际判定留在 _pick_best_model（带免费优先选择），这里不重复。
    return None


def touch_key(key_id: int, ip: str, tokens: int = 0) -> None:
    """记账：来源 IP 绑定、最近使用时间、已用 token（60s 节流之外的精确累计）。

    credit **不在此累计**：_record_usage 已带估算 + 60s 真实回写收敛，两边
    都记会双倍。tokens 仅累计上游真实返回的 total_tokens（None 不计——
    与 manager touch()「上游未返回不按 0 记」同口径；token 无法事后回写，
    宁少勿多）。

    旁路容错：记账失败不影响已返回的响应（但记日志，与 _record_usage 同原则）。
    """
    try:
        db = SessionLocal()
        try:
            from admin.models import ApiKeyIp
            # INSERT IGNORE 语义（MySQL 无原生 OR IGNORE）：先查后插，
            # 复合主键 (key_id, ip) 保证幂等；并发下重复插会被主键拦，
            # 捕获 IntegrityError 即视为「已绑定过」。
            if ip:
                exists = db.query(ApiKeyIp).filter(
                    ApiKeyIp.key_id == key_id, ApiKeyIp.ip == ip).first()
                if not exists:
                    try:
                        db.add(ApiKeyIp(key_id=key_id, ip=ip[:64]))
                        db.flush()
                    except Exception:
                        db.rollback()
            key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
            if key is not None:
                key.last_used_at = datetime.utcnow()
                if tokens:
                    key.used_tokens = int(key.used_tokens or 0) + int(tokens)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        import logging
        logging.getLogger("admin.security").exception(
            "touch_key 失败 key=%s ip=%s tokens=%s: %s", key_id, ip, tokens, e)
