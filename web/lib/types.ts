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
  /** 网关态（accounts 列表 _gateway_fields）：disabled/cooling/ready */
  gateway_state?: 'disabled' | 'cooling' | 'ready';
  gateway_until?: string | null;
  /** 冷却/熔断/降权原因码（cool_kind/breaker/degraded） */
  gateway_reason?: string;
  /** 最近一次错误信息（停用/冷却的真实原因：401/403 被拒、限流等），人工判读封号风险用 */
  last_err_msg?: string | null;
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
/** routers/accounts.py::scan_local —— 本机客户端登录态（不暴露 token，仅元信息） */
export interface ScanLocalItem {
  file: string;
  uid: string;
  nickname: string;
  domain: string;
  is_active: boolean;         // 是否当前活动登录态
  expires_at: string;         // token 到期（格式化串，空=未知）
  expires_ts: number;         // epoch 毫秒
}
export interface ScanLocalResult {
  exists: boolean;
  dir: string;
  active_uid: string | null;
  items: ScanLocalItem[];
}

/** routers/accounts.py::model_limits —— 全池模型级限流行 */
export interface ModelLimitRow {
  account_id: number;
  account_name?: string;
  model: string;
  kind?: string;              // model_rate/model_queue/model_daily/model_block
  until?: string | null;
  reason?: string;
}

/** routers/accounts.py::duplicates —— keep/remove 引用 */
export interface DupRef {
  id: number;
  name: string;
  created_at: string;
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
  status: string;             // 'active' | 'revoked'
  note: string;
  group_id: number;           // 模型分组 id；0 = 不绑定
  group_name: string;
  account_group_id: number;   // 账号池分组 id（P2 T8）；0 = 全池选号
  account_group_name: string;
  realm: string;              // '' | 'cn' | 'global'
  // P2 T3 全维度
  expires_at: string | null;  // ISO；null = 永不过期
  expired: boolean;           // 服务端算好（expires_at < now）
  max_ips: number;            // 0 = 不限
  ip_allowlist: string[];     // CIDR/单 IP 数组（list 返回已解析）；空 = 不限
  bound_ips: number;          // 已绑定 IP 数（max_ips 生效中）
  token_quota: number;        // 0 = 不限
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
  credits_raw: string;          // 引擎返回 "" 或 "x1.5" 等原始字符串
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

/** routers/models.py::speed_test（测速：账号池直发上游一次流式请求） */
export interface SpeedTestResult {
  model: string;
  account: string;              // nickname 或 uid（脱敏显示用）
  ttft_ms: number | null;       // 首字延迟
  completion_tokens: number | null;
  total_tokens: number | null;
  gen_duration_ms: number;
  total_duration_ms: number;
  speed_tps: number | null;     // 生成速度 t/s
  overall_tps: number | null;   // 端到端 t/s
  sample: string;               // 输出样例（≤400 字）
}

/** routers/groups.py::list_groups（模型分组） */
export interface ModelGroup {
  id: number;
  name: string;
  note: string;
  models: string[];
  model_count: number;
  effective_count: number;      // 过滤掉已删除模型后的有效数
  key_count: number;            // 绑定该组的 Key 数
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
  total: number | null;         // with_total=false 或 keyset 翻页时为 null（未统计）
  page: number;
  page_size: number;
  has_more: boolean;
  next_before_id: number | null;
}

/** routers/logs.py::log_summary（按账号用量汇总） */
export interface LogSummaryItem {
  account_id: number;
  account_name: string;
  requests: number;
  failed_requests: number;
  ok_requests: number;
  credits: number;
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  avg_latency_ms: number;
  last_used_at: string | null;
}
export interface LogSummary {
  range_days: number;
  total_requests: number;
  total_credits: number;
  items: LogSummaryItem[];
}

/** routers/logs.py::log_accounts（按账号查询下拉：窗口内有记录的账号） */
export interface LogAccountOption {
  id: number;
  name: string;
  uid: string;
  status: string;
  balance_remain: number;
  requests: number;
  credits: number;
  last_used_at: string | null;
}

/* ── 统计（routers/stats.py）────────────────────────────────────── */
/** stats/usage 分布项（by_model/by_endpoint/by_key 共用） */
export interface UsageBreakdown {
  label: string;
  requests: number;
  tokens: number;
  credits: number;
}
/** stats/usage 趋势桶（按 granularity：hour/day） */
export interface UsageTrendPoint {
  bucket: string;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  credits: number;
  requests: number;
}
/** stats/usage 概览卡片（成功口径计 token/积分，请求数含失败） */
export interface UsageOverview {
  requests: number;         // 全部调用（含失败）
  ok_requests: number;
  failed_requests: number;
  credits: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cached_tokens: number;
  avg_latency_ms: number;
}
/** routers/stats.py::usage_stats 完整响应（与「使用记录」页一一对应） */
export interface UsageStats {
  range_days: number;
  granularity: string;
  filters: {key_id: number | null; model: string | null;
            account_id: number | null; use_case: string | null};
  overview: UsageOverview;
  by_model: UsageBreakdown[];
  by_endpoint: UsageBreakdown[];
  by_key: UsageBreakdown[];
  trend: UsageTrendPoint[];
}
/** stats/usage/options 筛选下拉（窗口内真实出现过的值） */
export interface UsageOptions {
  models: string[];
  use_cases: string[];
  keys: {id: number; name: string}[];
}
export interface ModelHealth {
  model: string;
  total: number;
  errors: number;
  error_rate: number;
  avg_latency_ms: number;
}

/* ── 积分（routers/credits.py）──────────────────────────────────── */
/** credits/expiry 单个积分包（精简字段） */
export interface CreditPackage {
  package_code: string;
  display_name: string;
  remaining: number;
  total: number;
  expire_at: number | null;        // 毫秒时间戳
  expire_label: string;
  expire_date: string;
  expired: boolean;
  expiring_soon: boolean;
}
/** credits/expiry 账号行（按紧迫度升序） */
export interface CreditExpiryAccount {
  account_id: number;
  account_name: string;
  uid: string;
  status: string;
  balance_remain: number;
  total_remaining: number;
  total_capacity: number;
  expiring_soon_remaining: number;
  evergreen_remaining: number;
  expired_remaining: number;
  soonest_expire_at: number | null;
  soonest_days_left: number | null;
  soonest_label: string;
  expiring_soon: boolean;
  expiring_package_count: number;
  package_count: number;
  active_package_count: number;
  synced_at: number | null;
  next_expiring: CreditPackage[];   // 最近到期前 4 个
}
/** credits/expiry 汇总 */
export interface CreditExpirySummary {
  accounts: number;
  available_remaining: number;      // 余额口径
  total_remaining: number;          // 快照口径
  snapshot_accounts: number;
  expiring_soon_remaining: number;
  evergreen_remaining: number;
  expired_remaining: number;
  expiring_accounts: number;
  expiring_packages: number;
  soonest_expire_at: number | null;
  soonest_days_left: number | null;
  soonest_account_id: number | null;
  soonest_account_name: string | null;
  at_risk_ratio: number;
  packages_total: number;
}
export interface CreditWindow { days: number; amount: number; packages: number; }
export interface CreditPackageRow extends CreditPackage { account_id: number; account_name: string; }
export interface CreditExpiryStats {
  generated_at: number;
  expiring_soon_days: number;
  suggested_days: number;
  summary: CreditExpirySummary;
  windows: CreditWindow[];
  accounts: CreditExpiryAccount[];
  packages: CreditPackageRow[];
  packages_total: number;
  package_offset: number;
  package_limit: number;
  has_more: boolean;
}

/** credits/usage 账号消耗行 */
export interface CreditUsageAccount {
  account_id: number;
  account_name: string;
  requests: number;
  credits: number;
  tokens: number;
  remaining: number;
  expiring_soon_remaining: number;
  soonest_days_left: number | null;
}
export interface CreditUsageModel { model: string; requests: number; credits: number; }
export interface CreditUsageStats {
  range_days: number;
  total_credits: number;
  accounts: CreditUsageAccount[];
  models: CreditUsageModel[];
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

/* ── 成长计划（routers/growth.py）──────────────────────────────── */
/** 任务定义（/tasks，分类后带 level/actionable） */
export interface GrowthTask {
  task_code: string;
  title?: string;
  accept_status?: string;         // not_accepted/in_progress/completed/claimed
  progress?: {current?: number; target?: number};
  reward_credit?: number;
  reward_energy?: number;
  level?: string;                 // 分级（auto/manual...）
  level_label?: string;
  actionable?: boolean;
  reason?: string;
}
/** /tasks 响应（不绑账号，任务定义） */
export interface GrowthTasksResponse {
  tasks: GrowthTask[];
  synced_at?: string;
  source_account_id?: number;
  cached?: boolean;
  stale?: boolean;
  error?: string;
}
/** /accounts/{id}/tasks 单账号任务完成情况（面板弹窗） */
export interface GrowthAccountTasks {
  account_id: number;
  name: string;
  tasks: GrowthTask[];
  profile?: Record<string, unknown>;
  summary: {
    total: number;
    done: number;
    claimable: number;
    claimable_credit: number;
    actionable: number;
  };
  cached?: boolean;
  stale?: boolean;
  error?: string;
}
/** 策略表（/plans，分级依据） */
export interface GrowthPlan {
  task_code: string;
  level: string;
  level_label: string;
  event_codes: string[];
  reason: string;
  actionable: boolean;
}
/** /run /run-async 批量结果（每账号） */
export interface GrowthRunAccountResult {
  account_id: number;
  name?: string;
  model?: string;
  ok: boolean;
  msg?: string;
  tasks?: {task_code?: string; title?: string; level?: string; ok: boolean;
           skipped?: string; fired?: number; times?: number; status?: string}[];
  claimed?: {task_code: string; ok: boolean; credit?: number; energy?: number; msg?: string}[];
  credit?: number;
  energy?: number;
  balance_total?: number;
  balance_remain?: number;
  noop?: boolean;
  noop_reason?: string;
}
/** jobrunner 进度快照（/job/{id} 与 run-async 共用） */
export interface GrowthJobSnapshot {
  job_id: string;
  status: string;                 // running/done/failed
  title?: string;
  phase?: string;
  total?: number;
  done?: number;
  reused?: boolean;
  result?: Record<string, unknown>;
  error?: string;
}
