/**
 * 本引擎（workbuddy2api）API 响应类型。唯一真源是 admin/routers/*.py 的
 * 实际返回 dict——每个 interface 上方的注释标注它对应哪个路由函数。
 *
 * 与 workbuddy-manager 的 types 完全不同：manager 账号是「文件 + uid + nickname」
 * 模型，本引擎账号是「DB 行 + id + balance」模型；manager 密钥只有白名单，
 * 本引擎密钥有 P2 全维度（有效期/IP/配额/realm/账号池分组）。
 */

/* ── 账号（routers/accounts.py::list_accounts）──────────────────── */
export interface Account {
  id: number;
  name: string;
  uid: string;
  enterprise_id: string;
  domain: string;
  realm: string;              // 'cn' | 'global'（P1）
  group_id: number;           // 账号池分组 id（P2 T8）；0 = 未分组
  status: string;             // 'active' | 'disabled' | 'cooling' | ...
  balance_total: number | null;
  balance_remain: number | null;
  last_sync_at: string | null;
  last_used_at: string | null;
  created_at: string | null;
  /** 该账号的模型级限流台账（model_limits 端点同源） */
  model_limits?: AccountModelLimit[];
}

export interface AccountModelLimit {
  model: string;
  requests_today?: number;
  tokens_today?: number;
  until?: string;
  reset_at?: string;
  reason?: string;
}

/** routers/accounts.py::credit_details → credits.resources 元素（camelCase，真调上游归一化） */
export interface CreditResource {
  packageCode: string;
  packageName: string;
  displayName: string;
  total: number | null;
  remaining: number | null;
  used: number;
  status: string;
  expireAt: number | null;      // epoch 毫秒
  expireLabel: string;
  expireDate: string;
  expired: boolean;
  expiringSoon: boolean;
  daysLeft: number | null;
}

/** routers/accounts.py::credit_details 响应主体（credits 子树 + 兼容 packages） */
export interface CreditDetails {
  ok: boolean;
  id?: number;
  packages?: Record<string, unknown>[];   // 兼容层（旧结构），新代码别用
  credits: {
    ok: boolean;
    account_id: number;
    account_name: string;
    updated_at: number | null;
    total_capacity: number | null;
    total_remaining: number | null;
    expiring_soon_remaining: number | null;
    expired_remaining: number | null;
    soonest_expire_at: number | null;
    soonest_days_left: number | null;
    expiring_soon: boolean;
    expired: boolean;
    expiring_soon_days: number;
    resources: CreditResource[];          // 仍有剩余的包，到期升序
    all_resources: CreditResource[];      // 全量（含已用完）
  };
  collection?: Record<string, unknown>;
}

/** routers/accounts.py::scan_local */
export interface ScanLocalResult {
  exists: boolean;
  dir: string;
  active_uid: string;
  items: {uid: string; name?: string; imported?: boolean}[];
}

/* ── API 密钥（routers/keys.py::list_keys）──────────────────────── */
export interface ApiKey {
  id: number;
  name: string;
  key_prefix: string;
  masked: string;
  credit_limit: number;
  credit_used: number;
  unlimited: boolean;
  status: string;             // 'active' | 'disabled'
  note: string;
  group_id: number;           // 模型分组 id；0 = 不绑定
  group_name: string;
  account_group_id: number;   // 账号池分组 id（P2 T8）；0 = 全池选号
  account_group_name: string;
  realm: string;              // '' | 'cn' | 'global'
  // P2 T3 全维度
  expires_at: string | null;
  max_ips: number;
  ip_allowlist: string;       // JSON 数组字符串（CIDR）
  token_quota: number;
  used_tokens: number;
  last_used_at: string | null;
  created_at: string | null;
}

/** routers/keys.py::key_usage */
export interface KeyUsage {
  id: number;
  name: string;
  status: string;
  credit_limit: number;
  credit_used: number;
  credit_remaining: number | null;
  usage_percent: number | null;
  unlimited: boolean;
  created_at: string | null;
}

/** create_key 响应：明文仅此一次 */
export interface CreatedApiKey extends ApiKey {
  key: string;
}

/* ── 模型（routers/models.py::list_configs）─────────────────────── */
export interface ModelConfig {
  id: number;
  model_id: string;
  enabled: boolean;
  level: string;
  credit_multiplier: number;
  credits_raw: number | null;
  is_free: boolean;
  note: string;
  created_at: string | null;
}

/** routers/models.py::model_busy → items 元素 */
export interface ModelBusy {
  model_id: string;
  busy_level: number;
  busy_label: string;
}
export interface ModelBusyResponse {
  items: ModelBusy[];
  observed_at: string;
  stale: boolean;
}

/** routers/groups.py::list_groups（模型分组） */
export interface ModelGroup {
  id: number;
  name: string;
  models: string[];
  model_count?: number;
  bound_keys?: number;
  created_at: string | null;
}

/* ── 账号池分组（routers/account_groups.py::list_groups，P2 T8）─── */
export interface AccountGroup {
  id: number;
  name: string;
  note: string;
  accounts: number;           // 组内账号数
  bound_keys: number;         // 绑定该组的密钥数
  created_at: string | null;
}

/* ── 用量日志（routers/logs.py::_serialize）─────────────────────── */
export interface UsageLog {
  id: number;
  seq: number;
  key_name: string;
  account_name: string;
  model: string;
  requested_model: string;    // P2 T6：客户端原始请求模型名（别名）
  use_case: string;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  total_tokens: number;
  credits: number;
  client_ip: string;
  error_kind: string;
  latency_ms: number;
  ttfb_ms: number;
  created_at: string;
}
export interface UsageLogPage {
  items: UsageLog[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
  next_before_id: number | null;
}

/* ── 统计（routers/stats.py）────────────────────────────────────── */
export interface UsageSeriesPoint {
  bucket: string;             // 时间桶（按 granularity：minute/hour/day）
  requests: number;
  tokens: number;
  credits: number;
}
export interface UsageBreakdown {
  label: string;
  requests: number;
  tokens: number;
  credits: number;
}
export interface UsageStats {
  totals: {requests: number; tokens: number; credits: number};
  series: UsageSeriesPoint[];
  by_model?: UsageBreakdown[];
  by_key?: UsageBreakdown[];
  by_endpoint?: UsageBreakdown[];
  by_account?: UsageBreakdown[];
}
export interface ModelHealth {
  model: string;
  total: number;
  errors: number;
  error_rate: number;
  avg_latency_ms: number;
}

/* ── 积分（routers/credits.py）──────────────────────────────────── */
export interface CreditExpiryAccount {
  id: number;
  name: string;
  uid: string;
  balance_remain: number | null;
  soonest_expire_at: string | null;
  expiring_soon: number;
  urgency?: string;
}
export interface CreditExpiryStats {
  accounts: CreditExpiryAccount[];
  expiring_packages: CreditResource[];
}

/* ── 安全 / IP（routers/security.py，P2 T5/T6）──────────────────── */
export interface SecurityConfig {
  enabled: boolean;
  mode: 'blacklist' | 'whitelist';
}
export interface IpRule {
  id: number;
  kind: 'allow' | 'deny';
  cidr: string;
  note: string;
  created_at: string | null;
}
export interface IpAccessLog {
  id: number;
  ip: string;
  path: string;
  blocked: boolean;
  reason: string;
  ua: string;
  created_at: string | null;
}

/* ── 审计（routers/audit.py，P2 T2）────────────────────────────── */
export interface AuditLog {
  id: number;
  actor: string;
  action: string;
  target: string;
  detail: string;
  ip: string;
  created_at: string | null;
}

/* ── 访问令牌（routers/tokens.py，P2 T7，wbt_ 只读）────────────── */
export interface ApiToken {
  id: number;
  name: string;
  prefix: string;
  scope: string;              // 恒为 'readonly'（本引擎无 admin scope）
  enabled: boolean;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string | null;
}
export interface CreatedApiToken extends ApiToken {
  token: string;              // 明文仅此一次
}

/* ── 定时任务（routers/schedules.py）────────────────────────────── */
export interface Schedule {
  id: number;
  name: string;
  task: string;
  task_label: string;
  interval_minutes: number;
  enabled: boolean;
  stop_after: string | null;
  last_run_at: string | null;
  next_run_at: string | null;
  last_result: string;
  due_soon: boolean;
}

/* ── 客户端参数（routers/client_profile.py）────────────────────── */
export interface ClientProfile {
  ok: boolean;
  has_client: boolean;
  detected: Record<string, unknown>;
  saved?: Record<string, unknown>;
  effective?: Record<string, unknown>;
  source?: string;
  sources?: Record<string, string>;
  message?: string;
}

/* ── 系统（server.py + routers/releases.py）────────────────────── */
export interface VersionInfo {
  version: string;
  [k: string]: unknown;
}

/* ── 鉴权（server.py::login）───────────────────────────────────── */
export interface LoginResponse {
  token: string;
}
