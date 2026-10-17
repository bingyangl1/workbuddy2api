"""ORM 模型：账号、API Key、用量日志。"""
from datetime import datetime

from sqlalchemy import (Column, DateTime, Float, Index, Integer, String, Text,
                        UniqueConstraint)

from admin.db import Base


class Account(Base):
    """一个 WorkBuddy / CodeBuddy 登录态（.info 凭据）。"""

    __tablename__ = "accounts"
    # 选号热路径索引：每次代理请求都要按 status + 余额 + 冷却扫描候选池。
    # 号池通常只有几十行，但 idx_status 让「活跃账号」这条永不消失的过滤
    # 不必全表扫，冷启动与批量任务下更稳。
    __table_args__ = (
        Index("ix_accounts_status", "status"),
        Index("ix_accounts_uid", "uid"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False, default="")
    uid = Column(String(120), default="")
    enterprise_id = Column(String(120), default="")
    domain = Column(String(120), default="")
    # 账号域（'cn' / 'global'，见 admin/realm.py）：决定端点分派、UA 品牌、
    # 任务域门控与模型前缀路由。存量账号在 init_db 迁移时按 domain 回填。
    realm = Column(String(8), nullable=False, default="cn")
    auth_json = Column(Text, nullable=False)  # 原始 .info 内容（含 token）
    status = Column(String(16), default="active")  # active | disabled
    balance_total = Column(Integer, default=0)
    balance_remain = Column(Integer, default=0)
    last_sync_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    # 稳定性状态机：错误计数 / 冷却 / 防撞号 / 禁用原因
    err_count = Column(Integer, default=0)          # 连续上游 5xx 计数
    cool_until = Column(DateTime, nullable=True)    # 冷却截止时间
    cool_kind = Column(String(16), default="")       # hard_credit | soft_rate | error_threshold | not_found
    last_err_at = Column(DateTime, nullable=True)
    last_err_msg = Column(String(255), default="")
    last_picked_at = Column(DateTime, nullable=True)  # 最近一次被选中（仅观测；防撞号窗口已移入进程内存）
    # 熔断：连续失败达阈值后指数退避，封顶 breaker_cooldown_max。
    # 与 cool_until 是**或门**关系（任一未到期即不可选），生效的是更晚的那个。
    breaker_until = Column(DateTime, nullable=True)
    breaker_fails = Column(Integer, default=0)
    # 连续 12153「session dead」计数：达到阈值才禁用。
    # 一次就禁用等于误杀 —— 该错误在真实环境会被临时性触发（上游抖动/并发刷新）。
    session_dead_fails = Column(Integer, default=0)
    # 连败降权（「不知道原因的失败」兜底：未知 4xx 与传输层抖动）：
    # 带权威分类的错误各有自己的惩罚，不喂这个计数器，避免重复计罚。
    consecutive_fails = Column(Integer, default=0)
    degrade_until = Column(DateTime, nullable=True)
    #: 快过期积分（供三因子权重的「快过期先用」项）。
    credits_expiring = Column(Integer, default=0)
    # ---- 积分到期口径（详见 admin/credits.py）----------------------------
    #: 7 天内到期的剩余额度合计。与 credits_expiring 的区别：后者是历史字段，
    #: 窗口口径曾为 30 天；本列严格按 EXPIRING_SOON_DAYS(=7) 统计，
    #: 是「再不烧就作废」的那部分，也是选号的首要依据。
    credits_expiring_soon = Column(Integer, default=0)
    #: 所有**还有剩余**的积分包里最早的到期时间（真实到期，非占位值）。
    #: 为空表示手里全是长期有效额度（没有作废风险）。
    credits_soonest_expire_at = Column(DateTime, nullable=True)
    #: 长期有效（无到期时间）的剩余额度合计。
    credits_evergreen = Column(Integer, default=0)
    #: 已过期但账面仍有剩余的额度（上游扣减延迟时出现），仅供对账。
    credits_expired = Column(Integer, default=0)
    #: 逐包快照明细（JSON 文本，按到期升序）。存库而不是每次现查，
    #: 是为了让「最近快到期的积分包」这类展示与统计不必对上游逐号发请求。
    credits_snapshot = Column(Text, default="")
    #: 快照采集时间，用于展示「N 分钟前更新」并判断数据是否可信。
    credits_synced_at = Column(DateTime, nullable=True)
    # P2 T8：账号所属分组（0 = 未分组，归默认池；与 ApiKey.account_group_id
    # 配对实现「密钥只碰某组账号」的池级隔离）
    group_id = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AccountGroup(Base):
    """账号池分组（Phase 2 T8）。

    用途：把账号划进命名分组，密钥经 account_group_id 绑定后**只**从该组
    选号——与模型分组（ModelGroup，管「能用哪些模型」）正交，这张管
    「能用哪些账号」。删除守卫：组内还有账号、或还有密钥绑定时拒绝删除
    （409），删除那一刻没人用不代表下一秒没有——并发请求刚选好号正要绑。
    """

    __tablename__ = "account_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False, unique=True)
    note = Column(String(255), default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class AccountModelCooldown(Base):
    """账号×模型级的冷却（与账号级冷却**互相独立**）。

    为什么必须独立（上游 code 6004「该模型使用量超限」）：
      * 账号级冷却会让「换个模型就能用」的号被整体摘出池子 —— 明明切模型立即可用，
        却白扔一个账号直到冷却结束；
      * 反过来，若把模型级限流当成账号级，用户会遇到「限额后换不动号」。

    所以 6004 只冷却**触发调用的那个模型**：该账号对其他模型照常可选，
    而对同一模型则被 `_select_account` 跳过。

    code 11102「该后端无此模型」复用同一张表：它不是限流而是确定性答复
    （重试无意义），所以 TTL 更长（默认 6h）且按指数退避。
    """

    __tablename__ = "account_model_cool"
    __table_args__ = (Index("ix_account_model_cool_model_until", "model", "until"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, nullable=False, index=True)
    model = Column(String(120), nullable=False, default="")
    until = Column(DateTime, nullable=True)          # 冷却截止
    kind = Column(String(24), default="")            # model_daily | model_rate | model_queue | model_block
    reason = Column(String(255), default="")
    hits = Column(Integer, default=0)                # 命中次数（model_block 指数退避用）
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ApiKey(Base):
    """对外共享的 API Key，带积分限额。"""

    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), default="")
    key_hash = Column(String(128), unique=True, nullable=False)
    key_prefix = Column(String(16), default="")  # 展示用前缀
    key_full = Column(String(2048), default="")  # 完整密钥（仅管理后台查看用，base64 编码存储）
    credit_limit = Column(Float, default=0)  # 限额（credits）；unlimited=True 时忽略
    credit_used = Column(Float, default=0)
    unlimited = Column(Integer, default=0)  # 0/1
    status = Column(String(16), default="active")  # active | revoked
    note = Column(String(255), default="")
    # 绑定的模型分组 id；0 = 未绑定（可用全部启用模型）。分组被删时按 0 处理（自动降级）
    group_id = Column(Integer, default=0)
    # P2 T8：绑定的**账号池**分组 id；0 = 未绑定（从全池选号）。
    # 与上面的 group_id（模型分组）正交：那个管「能用哪些模型」，
    # 这个管「能用哪些账号」。命名刻意带 account_ 前缀避免混淆。
    account_group_id = Column(Integer, nullable=False, default=0)
    # realm 限定：''=不限（存量默认） / 'cn' / 'global'。跨 realm 调用 403
    realm = Column(String(8), nullable=False, default="")
    # ---- P2 T3：密钥全维度 ----
    # 有效期：NULL = 永不过期
    expires_at = Column(DateTime, nullable=True)
    # 每密钥 IP 数上限（0 = 不限）。判定基于 api_key_ips 的累计绑定数
    max_ips = Column(Integer, default=0)
    # 每密钥 IP 白名单（JSON 数组，元素为单 IP 或 CIDR）。空数组 = 不限
    ip_allowlist = Column(Text, default="[]")
    # Token 配额：0 = 不限（0 不可能是有效配额）。仅计上游真实返回的 total_tokens
    token_quota = Column(Integer, default=0)
    used_tokens = Column(Integer, default=0)
    # 每密钥并发上限（覆盖全局 GATEWAY_MAX_CONCURRENT）。
    #
    #   NULL = 跟随全局（存量默认，零回归）
    #   0    = 不限（该密钥可以打满，仍受账号层 ADMIN_POOL_MAX_IN_FLIGHT 约束）
    #   N    = 该密钥最多 N 路并发
    #
    # 为什么需要它：全局并发是「所有密钥共享」的总闸。一个跑批客户端
    # （并发 5 路）会把默认 4 路的全局额度占满，其他密钥全部排队 ——
    # 明明那不是他们的额度问题。per-key 覆盖让重度/轻度用户互不干扰。
    #
    # 注意：本字段只管**网关准入**；账号层在途上限（ADMIN_POOL_MAX_IN_FLIGHT）
    # 是独立约束 —— 单号被打满时仍会 503，那是账号维度的保护，不该被绕过。
    max_concurrent = Column(Integer, nullable=True)
    # 最近一次成功发起请求的时间（touch() 落库，60s 节流之外始终精确到请求级）
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ApiKeyIp(Base):
    """密钥绑定过的来源 IP（max_ips 判定的累计事实）。

    为什么单独一张表而不是记在 key 上：max_ips 是「**绑定数**上限」，
    语义是「这把密钥一共从多少个不同的 IP 发过请求」，而不是「当前在线」。
    记录在删除密钥时级联清理（keys 路由 delete 里显式删，见 manager delete_key）。
    """

    __tablename__ = "api_key_ips"

    # 复合主键：同一密钥同一 IP 只记一次（INSERT IGNORE 语义，MySQL 无原生
    # OR IGNORE，用 merge 或先查后插——见 security.touch_key）
    key_id = Column(Integer, primary_key=True)
    ip = Column(String(64), primary_key=True)
    first_seen = Column(DateTime, default=datetime.utcnow)


class ModelGroup(Base):
    """模型分组：一组模型的命名集合，供 API Key 绑定以限制可用模型范围。"""

    __tablename__ = "model_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False, default="")
    note = Column(String(255), default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ModelGroupItem(Base):
    """分组内的模型成员（一个分组多条）。"""

    __tablename__ = "model_group_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, nullable=False, index=True)
    model_id = Column(String(120), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class UsageLog(Base):
    """每次代理调用的用量记录（用于管理后台审计）。

    记录内容：调用方 API Key、实际使用的上游账号、模型、积分消耗、token 明细，
    以及发起请求的真实客户端 IP（来自 X-Forwarded-For / X-Real-IP / 直连 socket），
    便于风控对账与上游客用途日志对齐。
    """

    __tablename__ = "usage_logs"
    # 版本归属（cn / global）—— 同名模型两版计费不同，日志不区分版本
    # 就会把两版混成一条统计（#5：用户实测反馈过这个问题）。
    # 由 _record_usage 按「requested_model 前缀 → 账号自身 realm」解析后写入。
    realm = Column(String(16), default="cn", nullable=False)
    # 索引说明（都是为「使用记录 / 日志」的筛选与排序服务的）：
    #   ix_usage_logs_created_id  —— 覆盖「按时间倒序分页」这条**永不消失**的主查询。
    #       原来是 PRIMARY(id) 倒序扫：日志一多，`ORDER BY id DESC LIMIT 20`
    #       在有 where 条件时仍要走全表再排序；有了 (created_at, id) 复合索引，
    #       时间范围过滤 + 倒序取前 N 条能直接在索引上完成，回表只取 20 行。
    #   ix_usage_logs_account_created —— 「按账号查询」的专用索引（本次新增需求）。
    #       账号维度是排查「某个号今天花了多少」最常用的入口，
    #       单列 account_id 索引仍要排序，复合索引把过滤 + 排序一次做完。
    #   ix_usage_logs_key_created / ix_usage_logs_model_created 同理，覆盖按 Key/模型筛选。
    __table_args__ = (
        Index("ix_usage_logs_created_id", "created_at", "id"),
        Index("ix_usage_logs_account_created", "account_id", "created_at"),
        Index("ix_usage_logs_key_created", "api_key_id", "created_at"),
        Index("ix_usage_logs_model_created", "model", "created_at"),
        Index("ix_usage_logs_error_created", "error_kind", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    api_key_id = Column(Integer, nullable=False, default=0)
    account_id = Column(Integer, nullable=False, default=0)
    model = Column(String(120), default="")
    # P2 T6：客户端**原始请求**的模型名（model_map 别名命中时与 model 不同：
    # model 记实际路由的映射目标，这里留原始名便于「谁在按别名调」对账；
    # 未命中映射时与 model 相同。manager request_logs.model/mapped_model 同构）
    requested_model = Column(String(120), default="")
    credits = Column(Float, default=0)
    # 详细用量：优先取上游 usage 字段；缺省为 NULL
    prompt_tokens = Column(Integer, nullable=True, default=None)
    completion_tokens = Column(Integer, nullable=True, default=None)
    total_tokens = Column(Integer, nullable=True, default=None)
    cached_tokens = Column(Integer, nullable=True, default=None)
    # 发起请求的真实客户端 IP（经反代时取 X-Forwarded-For 首个，否则 X-Real-IP / 直连 IP）
    client_ip = Column(String(64), default="")
    # 用途标识（透传给上游的 X-Agent-Purpose），便于风控审计与上游请求用量对齐
    use_case = Column(String(64), default="")
    # 请求级表格日志字段（ logging）：TTFB / 总耗时 / 序号 / 错误分类
    seq = Column(Integer, default=0)
    ttfb_ms = Column(Integer, nullable=True, default=None)
    latency_ms = Column(Integer, nullable=True, default=None)
    error_kind = Column(String(32), default="")  # hard_credit | soft_rate | server | not_found | session_dead | transport | client | success
    created_at = Column(DateTime, default=datetime.utcnow)


class ModelConfig(Base):
    """模型白名单配置（系统级 / 用户级）。

    ## realm 列（2026-10 改造）

    两版**可能同名但能力/计费不同**（实测 `hy4-preview`：
    cn=0.29、global=0.00），所以必须能并存两条。

    改造前靠 `model_id` 的字符串前缀（`global:`）区分 ——
    能工作，但语义脆弱：

      * 没有唯一约束，手写 SQL 能插两条重复的 `global:xxx`
      * 查询「两版共有模型」要靠前缀解析，每个读写点都得记得剥/加
      * 计费曾因此在裸名上查错版本（已修，见 proxy.py::_record_usage 注释）

    改造后：

      realm    显式列（`cn` / `global`），带默认值 `cn`
      model_id **裸名**（`hy4-preview`），不带任何前缀

    唯一约束 `(level, realm, model_id)` —— 同一层级、同一版本下
    不可能有重复模型。

    ## 兼容

    * 老数据（`global:xxx`）由迁移脚本 `_migrate_model_realm()` 转换
    * 对外 API 仍接受/返回 `cn:` / `global:` 前缀（客户端协议不变），
      见 `admin/realm.py::resolve_model_prefix` —— 那层是**协议**，
      不是存储形态
    """

    __tablename__ = "model_configs"
    __table_args__ = (
        # 同一层级 + 同一版本 + 同一模型名 = 唯一
        UniqueConstraint("level", "realm", "model_id",
                         name="uq_model_configs_level_realm_model"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    level = Column(String(16), default="system")  # system | user
    # 版本归属：cn（国内版）| global（国际版）。
    # 为什么给默认值 "cn"：老库升级时既有行全是国内版（改造前国内版
    # 不带前缀），有默认值就能让 ALTER TABLE 直接补上而不必回填。
    realm = Column(String(16), default="cn", nullable=False)
    model_id = Column(String(120), nullable=False)  # **裸名**，如 "deepseek-v4.1-flash"
    enabled = Column(Integer, default=1)  # 0/1
    note = Column(String(255), default="")
    credit_multiplier = Column(Float, default=0)  # 积分消耗倍率；0=免费模型
    credits_raw = Column(String(120), default="")  # 原始 credits 字符串（如 "x0.05" / "x0.00 credits"）
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SystemSetting(Base):
    """简单的键值配置（同步地址 / 密钥 / 其他开关）。"""

    __tablename__ = "system_settings"

    key = Column(String(120), primary_key=True)
    value = Column(Text, default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AuditLog(Base):
    """管理端审计日志：谁、何时、对什么目标、做了什么、结果与来源。

    为什么单独一张表（manager 同款）：这些操作不产生请求日志（那是网关的），
    出了事只能靠反代 access log 去猜。管理端是公网入口，敏感操作（增删账号、
    key 变更/查看、改密码、改安全配置）必须可追溯。

    写入永远走 admin/audit.py 的旁路容错封装——审计挂了不能影响请求本身。
    """

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_created_id", "created_at", "id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 操作者：管理端 JWT 用户名 / wbt_ token 名 / 'anonymous'（未认证触达）
    actor = Column(String(120), nullable=False, default="")
    # 动作短码：create_account / delete_key / view_key_full / update_security ...
    action = Column(String(64), nullable=False, default="")
    # 被操作对象（如被删的账号 id、被改的密钥名）
    target = Column(String(255), default="")
    # 结果/细节摘要（不含敏感明文：token/密码永不写入）
    detail = Column(String(500), default="")
    # 来源 IP（严格口径，见 admin/iputil.py）
    ip = Column(String(64), default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class ApiToken(Base):
    """管理面作用域 API Token（wbt_ 前缀）：给脚本 / 看板 / CI 的只读凭据。

    与网关密钥（ApiKey，sk_ 前缀）**刻意分开**：那套授权的是模型调用，
    这套授权的是管理面读取。安全要求更高（见 admin/tokensvc.py）：
    明文只在创建时返回一次，库中只存 SHA-256；scope 一律 readonly。
    """

    __tablename__ = "api_tokens"
    __table_args__ = (Index("ix_api_tokens_prefix", "prefix"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), default="")
    token_hash = Column(String(128), nullable=False)  # SHA-256 hex
    prefix = Column(String(16), default="")            # wbt_ + 前 8 位，定位用
    # 作用域：'readonly'（本引擎唯一合法值；非法值读取时降级为 readonly）
    scope = Column(String(16), nullable=False, default="readonly")
    enabled = Column(Integer, default=1)
    expires_at = Column(DateTime, nullable=True)       # NULL = 永不过期
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(String(120), default="")
    last_used_at = Column(DateTime, nullable=True)
    last_used_ip = Column(String(64), default="")


class IpRule(Base):
    """全局入站 IP 白/黑名单规则（Phase 2 T5）。

    与每密钥 ip_allowlist 的分工：这张表是**网关级**的第一道闸（在密钥
    校验**之前**，见 proxy._global_ip_gate），管「哪些来源能摸到 /v1」；
    每密钥白名单是第二道，管「这把密钥允许哪些来源」。kind: allow/deny；
    模式（whitelist 默认拒 / blacklist 默认放）在 system_settings.security。
    """

    __tablename__ = "ip_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    kind = Column(String(8), nullable=False, default="deny")  # allow | deny
    cidr = Column(String(64), nullable=False)
    note = Column(String(255), default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class IpAccessLog(Base):
    """入站访问审计（默认只记**被拦**的，AUDIT_ALL_ACCESS=1 才记全量）。

    为什么默认不记放行：这张表的用途是安全审计（谁在扫我、谁被挡了），
    记全量会把信号淹没（manager 实测 2 万行里约 0.2% 是拦截记录）；放行
    明细在 usage_logs 有完整记录，不重复记不丢信息。reason 是短码
    （global_deny/global_not_allowed/missing_key/invalid_key），
    界面按短码着色；存量 NULL 按「未记录」展示，不编造。
    """

    __tablename__ = "ip_access_logs"
    __table_args__ = (Index("ix_ip_access_logs_created_id", "created_at", "id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    ip = Column(String(64), default="")
    path = Column(String(255), default="")
    blocked = Column(Integer, default=0)     # 0/1
    ua = Column(String(255), default="")
    reason = Column(String(32), default="")  # 拦截原因短码
    created_at = Column(DateTime, default=datetime.utcnow)


class Schedule(Base):
    """后台定时任务（如：整点刷新平台总积分、每日同步模型列表）。"""

    __tablename__ = "schedules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), default="")
    task = Column(String(40), default="refresh_balances")  # refresh_balances | sync_models | daily_checkin
    interval_minutes = Column(Integer, default=60)
    enabled = Column(Integer, default=1)  # 0/1
    last_run_at = Column(DateTime, nullable=True)
    next_run_at = Column(DateTime, nullable=True)
    last_result = Column(Text, default="")  # 上次运行结果摘要
    # 停止领取时间（仅 daily_checkin 任务使用）：到达该时间后不再执行领取请求，
    # 避免活动下线后继续请求触发上游风控。可由活动 end_time 预填或运行中发现 EventEnded 自动写入。
    stop_after = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BenefitClaim(Base):
    """限时权益 / 补偿的领取记录（**每账号每期一条**）。

    ## 为什么要有这张表

    上游的权益活动是「每账号只能领一次」，而且**多领会被判异常**
    （用户明确要求：「有些是只能跑一遍的，多遍跑会检测的」）。
    光靠内存状态挡不住：重启进程、多进程、连点两下都能绕过。

    `(account_id, campaign_key)` 上的唯一约束才是真正的互斥 ——
    并发插入时只有一个能成功，其余拿到 `IntegrityError`。

    ## campaign_key 的含义

    活动期标识。**换期必须换 key**，否则上一期的「已领取」会挡住新一期。
    当前值见 `admin/benefits.CAMPAIGN_KEY`。

    ## 字段语义（`claimed` 与 `credit` 的配合）

    | 场景 | claimed | credit | 说明 |
    |---|---|---|---|
    | 只查过，不可领 | 0 | 0 | `last_reason` 记原因 |
    | 领取请求发出但失败 | 0 | 0 | **不置 claimed**，允许用户重试 |
    | 领取成功 | 1 | N | `claimed_at` 记录时间，永久挡住重复领取 |

    注意「领取失败不置 claimed」是有意为之：否则一次网络抖动会让这个账号
    永久失去领取机会。
    """

    __tablename__ = "benefit_claims"
    __table_args__ = (
        # 真正的互斥来源：同一账号同一期只能有一行
        UniqueConstraint("account_id", "campaign_key",
                         name="uq_benefit_claims_account_campaign"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, nullable=False, index=True)
    campaign_key = Column(String(64), nullable=False, default="")
    claimed = Column(Integer, default=0)          # 0/1
    credit = Column(Integer, default=0)           # 成功领取的积分数
    claimed_at = Column(DateTime, nullable=True)
    checked_at = Column(DateTime, nullable=True)  # 上次查询时间
    last_reason = Column(String(500), default="")
    last_http = Column(Integer, default=0)
    last_code = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow,
                        onupdate=datetime.utcnow)


class GrowthRunRecord(Base):
    """成长任务**单次执行**的审计记录（一次执行一条）。

    ## 为什么需要

    成长任务分散在三条路径上跑（同步 `/run`、异步 `/run-async`、定时
    `run_growth_tasks`），执行完结果只回给当次调用方 —— 面板刷新后就没了。

    出问题时的典型疑问全都答不上来：

      * 「这个号的 chat_5 昨天到底做了没有？」
      * 「这 500 积分是哪次跑出来的？」
      * 「定时任务和我手动点的，是不是重复跑了？」

    所以每次**真实执行**（不加锁跳过的不算）留一条记录。

    ## 为什么不做成「每账号每任务一行」

    那样会丢历史（上周做了什么查不到），而且要处理「同一任务一天跑多次」
    的去重语义 —— 记录本身就不该有去重需求，它记录的是**事实**。

    ## 与 `GrowthPlanRecord`（上游）的关系

    上游对应的表叫 `growth_plan_records`，字段语义接近但那是给
    「逐账号巡检」用的。我们的执行模型不同（任务策略表 + 批量），
    所以字段也不同：这里有 `task_code`（具体任务）、`skipped`（是否跳过），
    上游只有笼统的 `action`。
    """

    __tablename__ = "growth_run_records"
    __table_args__ = (
        # 按账号+时间查「最近一次」是最高频的查询
        Index("ix_growth_run_records_account_time", "account_id", "created_at"),
        Index("ix_growth_run_records_created", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, nullable=False)
    #: 执行来源：sync / async / schedule / manual
    source = Column(String(24), default="")
    #: 具体任务码（如 chat_5 / skill_1）；批量汇总时为空
    task_code = Column(String(64), default="")
    #: 0/1。`ok=0` 且 `skipped=1` 表示「按规则跳过」，不是失败
    ok = Column(Integer, default=0)
    skipped = Column(Integer, default=0)
    #: 本次获得的积分 / 体力
    credit = Column(Integer, default=0)
    energy = Column(Integer, default=0)
    #: 跳过或失败的原因（成功时为空）
    reason = Column(String(500), default="")
    created_at = Column(DateTime, default=datetime.utcnow)
