/**
 * 本引擎（workbuddy2api）typed API client。唯一契约真源是 admin/routers/*.py
 * （125 条路由），**不是** workbuddy-manager 的契约。
 *
 * 与 manager 的根本差异（鉴权模型）：
 *   manager = cookie 会话（withCredentials + /api/me + 多用户角色）；
 *   本引擎 = JWT 头（POST /api/login 返回 {token}，后续请求带 X-Admin-Token 头）。
 * token 存 sessionStorage（关标签页即失效，沿用 manager 的 wb-me 缓存键思路）。
 */
import axios, {AxiosError} from 'axios';
import {BASE_PATH} from './base-path';
import {t, tp} from './i18n';
import type {
  Account,
  AccountGroup,
  ApiKey,
  ApiToken,
  AuditLog,
  ClientProfile,
  GrowthAccountTasks,
  GrowthJobSnapshot,
  GrowthPlan,
  GrowthRunAccountResult,
  GrowthTasksResponse,
  CreatedApiKey,
  CreatedApiToken,
  CreditDetails,
  CreditExpiryStats,
  CreditUsageStats,
  DupRef,
  IpAccessLog,
  IpRule,
  KeyUsage,
  LogAccountOption,
  LoginResponse,
  LogSummary,
  ModelBusyResponse,
  ModelCatalogResponse,
  ModelConfig,
  ModelGroup,
  ModelHealth,
  ModelLimitRow,
  ScanLocalResult,
  SpeedTestResult,
  Schedule,
  SecurityConfig,
  UsageLogPage,
  UsageOptions,
  UsageStats,
  VersionInfo,
  ChangelogResponse,
} from './types';

// API 走同源根路径 /api（FastAPI 单端口把 /api /v1 /panel 挂在一起）。
// 不能用 BASE_PATH 作 baseURL：BASE_PATH 是面板页面前缀（/panel），
// API 不在它下面——否则 /api/accounts 会被拼成 /panel/api/accounts 404。
export const http = axios.create({
  baseURL: '',
  timeout: 60000,
});

const TOKEN_KEY = 'wb-admin-token';

export function readToken(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}
export function writeToken(token: string | null): void {
  if (typeof window === 'undefined') return;
  try {
    if (token) window.sessionStorage.setItem(TOKEN_KEY, token);
    else window.sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* 隐私模式忽略 */
  }
}

// 每个请求注入 JWT（本引擎 require_admin 读 X-Admin-Token 头）。
http.interceptors.request.use((config) => {
  const token = readToken();
  if (token) config.headers['X-Admin-Token'] = token;
  return config;
});

// 401 → 清登录态跳登录页（公开页 /login、/join 不跳）。
http.interceptors.response.use(
  (r) => r,
  (error: AxiosError) => {
    if (error.response?.status === 401 && typeof window !== 'undefined') {
      writeToken(null);
      const path = window.location.pathname;
      const loginPath = `${BASE_PATH}/login`;
      const publicPaths = [loginPath, `${BASE_PATH}/join`];
      if (!publicPaths.some((p) => path.startsWith(p))) {
        window.location.href = loginPath;
      }
    }
    return Promise.reject(error);
  },
);

/** 统一抽取后端错误信息（detail 优先；本引擎报错是中文，过短语表翻译）。 */
export function errText(e: unknown): string {
  const ax = e as AxiosError<{detail?: string; error?: string}>;
  const d = ax?.response?.data;
  const raw = (typeof d === 'string' ? d : d?.detail || d?.error) || '';
  if (raw) return tp(raw);
  if (ax?.response?.status === 403) return t('error.forbidden');
  const fallback = ax?.message || '';
  return fallback ? tp(fallback) : tp('请求失败');
}

const get = async <T>(url: string, params?: Record<string, unknown>): Promise<T> =>
  (await http.get<T>(url, {params})).data;
const post = async <T>(url: string, body?: unknown): Promise<T> =>
  (await http.post<T>(url, body)).data;
const patch = async <T>(url: string, body?: unknown): Promise<T> =>
  (await http.patch<T>(url, body)).data;
const put = async <T>(url: string, body?: unknown): Promise<T> =>
  (await http.put<T>(url, body)).data;
const del = async <T>(url: string): Promise<T> => (await http.delete<T>(url)).data;

/* ── 鉴权（server.py）─────────────────────────────────────────────
 * POST /api/login 是 **Form 表单**（username/password），返回 {token}。
 * 无 /api/me——JWT 有效性由每个请求的 401 判定（拦截器已处理）。 */
export const authApi = {
  login: async (username: string, password: string): Promise<LoginResponse> => {
    const form = new URLSearchParams();
    form.set('username', username);
    form.set('password', password);
    const res = await http.post<LoginResponse>('/api/login', form, {
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    });
    return res.data;
  },
  changePassword: (oldPassword: string, newPassword: string) =>
    patch<{ok: boolean}>('/api/admin/password', {
      old_password: oldPassword,
      new_password: newPassword,
    }),
};

/* ── 账号池（routers/accounts.py）───────────────────────────────── */
export const accountApi = {
  list: () => get<{items: Account[]}>('/api/accounts'),
  /** 添加账号：核心入参是 auth_json（腾讯授权凭证 JSON），服务端真调上游验证
   *  并按 uid 去重（重复=新凭证覆盖）。name 留空则自动取真实昵称/uid。 */
  create: (body: {auth_json: string; name?: string}) =>
    post<{id: number; name: string; ok: boolean; duplicated: boolean; message?: string;
          verified?: boolean; balance_remain?: number | null}>('/api/accounts', body),
  /** 批量添加：verify=false 只做结构校验（导入自己刚导出的数据时用，快但不验伪造）。 */
  createBatch: (items: {auth_json: string; name?: string}[], verify = true) =>
    post<{ok: boolean; results: Record<string, unknown>[]}>('/api/accounts/batch', {items, verify}),
  update: (id: number, body: {name?: string; status?: 'active' | 'disabled'}) =>
    patch<{id: number; ok: boolean}>(`/api/accounts/${id}`, body),
  remove: (id: number) => del<{ok: boolean}>(`/api/accounts/${id}`),
  refresh: (id: number) =>
    post<{id: number; balance_total: number | null; balance_remain: number | null}>(
      `/api/accounts/${id}/refresh`),
  creditDetails: (id: number, refresh = false) =>
    get<CreditDetails>(`/api/accounts/${id}/credit-details`, refresh ? {refresh: 1} : undefined),
  /** 全池模型级限流台账（routers/accounts.py::model_limits）。 */
  modelLimits: () => get<{items: ModelLimitRow[]}>('/api/accounts/model-limits'),
  clearModelLimit: (id: number) =>
    post<{ok: boolean; cleared: number}>(`/api/accounts/${id}/model-limits/clear`),
  /** uid 重复分组（每组 keep=最早创建保留，remove=可清理冗余）。 */
  duplicates: () => get<{total: number; unique: number; redundant: number;
                        groups: {uid: string; keep: DupRef; remove: DupRef[]}[]}>(
    '/api/accounts/duplicates'),
  /** 去重：同 uid 只留 id 最小（历史记录最全）那条。 */
  dedupe: () => post<{removed: number; kept: number; items: {id: number; name: string; uid: string}[]}>(
    '/api/accounts/dedupe'),
  syncCredits: () => post<{ok: boolean; synced: number}>('/api/accounts/credits/sync'),
  /** 扫描本机客户端登录目录（服务端读，不向前端暴露 token）。 */
  scanLocal: () => get<ScanLocalResult>('/api/accounts/scan-local'),
  /** 导入本机 .info：files 为空 + all=true 导入全部。 */
  importLocal: (files: string[], all = false) =>
    post<{added: number; skipped: number; errors: string[]}>(
      '/api/accounts/import-local', {files, all}),
  /** 导出全池（含 auth_json 明文凭证——敏感，仅管理员）。 */
  exportAll: async () =>
    (await http.get<Blob>('/api/accounts/export', {responseType: 'blob'})).data,
  exportOne: (id: number) => get<{id: number; uid: string; name: string; auth_json: string}>(
    `/api/accounts/${id}/export`),
  /** 注入客户端活动登录态（先备份当前登录态）。confirm=true 才执行。 */
  inject: (id: number) =>
    post<{ok: boolean; target: string; backup: string | null; account: string; uid: string}>(
      `/api/accounts/${id}/inject`, {confirm: true}),
  /** 猫猫旅行：单账号领养+派出+领奖（结构化分步结果）。 */
  catTravel: (id: number) => post<Record<string, unknown>>(`/api/accounts/${id}/cat-travel`),
  requestUsage: (id: number, params?: Record<string, unknown>) =>
    get<Record<string, unknown>>(`/api/accounts/${id}/request-usage`, params),
};

/* ── API 密钥（routers/keys.py，含 P2 T3 全维度）────────────────── */
/** 创建/编辑密钥的入参（KeyIn + patch_key 可改字段；显式空值 = 改回「不限」）。 */
export interface KeyPayload {
  name?: string;
  credit_limit?: number;
  unlimited?: boolean;
  note?: string;
  status?: 'active' | 'revoked';
  group_id?: number;            // 模型分组
  account_group_id?: number;    // 账号池分组
  realm?: '' | 'cn' | 'global';
  expires_at?: string | null;   // ISO；null/'' = 永不过期
  max_ips?: number;             // 0 = 不限
  ip_allowlist?: string[];      // CIDR/单 IP；空 = 不限
  token_quota?: number;         // 0 = 不限
  reset_used?: boolean;         // true = credit_used + used_tokens 一起归零
}
export const keyApi = {
  list: () => get<{items: ApiKey[]}>('/api/keys'),
  create: (body: KeyPayload) => post<CreatedApiKey>('/api/keys', body),
  update: (id: number, body: KeyPayload) => patch<{id: number; ok: boolean}>(`/api/keys/${id}`, body),
  remove: (id: number) => del<{ok: boolean}>(`/api/keys/${id}`),
  usage: (id: number) => get<KeyUsage>(`/api/keys/${id}/usage`),
  /** 完整明文——JWT-only，wbt_ 只读 token 摸不到（T9 刻意保留 require_admin）。 */
  view: (id: number) => get<{id: number; key: string}>(`/api/keys/${id}/view`),
  resetUsage: (id: number) => post<{ok: boolean}>(`/api/keys/${id}/reset-usage`),
};

/* ── 模型（routers/models.py + groups.py）───────────────────────── */
export const modelApi = {
  configs: (level?: string) =>
    get<{items: ModelConfig[]}>('/api/models/configs', level ? {level} : undefined),
  toggle: (id: number, enabled: boolean) =>
    patch<{id: number; model_id: string; enabled: boolean}>(`/api/models/${id}`, {enabled}),
  // 契约：{model_ids: string[], enabled, level}（不是 ids:number[]）
  batchToggle: (model_ids: string[], enabled: boolean, level = 'system') =>
    post<{updated: number; enabled: boolean}>('/api/models/batch-toggle', {model_ids, enabled, level}),
  remove: (id: number) => del<{id: number; model_id: string; ok: boolean}>(`/api/models/${id}`),
  // 契约返回 {total_fetched, added, updated, total_in_db, enabled}
  sync: () => post<{total_fetched: number; added: number; updated: number; total_in_db: number; enabled: number}>('/api/models/sync'),
  busy: () => get<ModelBusyResponse>('/api/models/busy'),
  // 模型中心目录（P4）：按 realm 直连腾讯，含 source 来源标注
  catalog: (realm: string = 'cn', force = false) =>
    get<ModelCatalogResponse>('/api/models/catalog', {realm, ...(force ? {force: true} : {})}),
  // 契约：{model, max_tokens?, messages?} → SpeedTestResult（真实上游请求，约 2-10s）
  speedTest: (model: string, max_tokens = 256) =>
    post<SpeedTestResult>('/api/models/speed-test', {model, max_tokens}),
  // 模型分组（/api/groups，GroupIn = {name, note, models[]} 全量覆盖语义）
  groups: () => get<{items: ModelGroup[]}>('/api/groups'),
  createGroup: (body: {name: string; note?: string; models: string[]}) =>
    post<{id: number; name: string; model_count: number; ok: boolean}>('/api/groups', body),
  updateGroup: (id: number, body: {name: string; note?: string; models: string[]}) =>
    put<{id: number; ok: boolean}>(`/api/groups/${id}`, body),
  // 删除分组会把绑定的 Key group_id 置 0，返回 unbound_keys
  deleteGroup: (id: number) => del<{id: number; ok: boolean; unbound_keys: number}>(`/api/groups/${id}`),
};

/* ── 账号池分组（routers/account_groups.py，P2 T8）──────────────── */
export const accountGroupApi = {
  list: () => get<{items: AccountGroup[]}>('/api/account-groups'),
  create: (body: {name: string; note?: string}) => post<AccountGroup>('/api/account-groups', body),
  update: (id: number, body: {name?: string; note?: string}) =>
    patch<AccountGroup>(`/api/account-groups/${id}`, body),
  remove: (id: number) => del<{ok: boolean}>(`/api/account-groups/${id}`),
  assign: (groupId: number, accountId: number) =>
    post<{ok: boolean}>(`/api/account-groups/${groupId}/accounts/${accountId}`),
  unassign: (accountId: number) =>
    post<{ok: boolean}>(`/api/account-groups/accounts/${accountId}/unassign`),
};

/* ── 用量日志（routers/logs.py）─────────────────────────────────── */
export const logApi = {
  list: (params?: Record<string, unknown>) => get<UsageLogPage>('/api/logs', params),
  summary: (params?: Record<string, unknown>) => get<LogSummary>('/api/logs/summary', params),
  accounts: (days = 30) => get<{range_days: number; items: LogAccountOption[]}>('/api/logs/accounts', {days}),
  exportUrl: (params?: Record<string, unknown>) => {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v != null && v !== '') qs.set(k, String(v));
    });
    // 同源 API（/api），不加面板前缀——与 baseURL 口径一致
    return `/api/logs/export${qs.toString() ? `?${qs}` : ''}`;
  },
};

/* ── 统计（routers/stats.py）────────────────────────────────────── */
export const statsApi = {
  usage: (params?: {days?: number; granularity?: string; key_id?: number; model?: string;
                    account_id?: number; use_case?: string}) =>
    get<UsageStats>('/api/stats/usage', params as Record<string, unknown>),
  options: (days = 30) =>
    get<UsageOptions>('/api/stats/usage/options', {days}),
  modelHealth: (hours = 24) => get<{items: ModelHealth[]}>('/api/stats/model-health', {hours}),
};

/* ── 积分（routers/credits.py）──────────────────────────────────── */
export const creditsApi = {
  expiry: (params?: {days?: number; include_disabled?: boolean}) =>
    get<CreditExpiryStats>('/api/credits/expiry', params as Record<string, unknown>),
  usage: (params?: {days?: number; account_id?: number; top?: number}) =>
    get<CreditUsageStats>('/api/credits/usage', params as Record<string, unknown>),
};

/* ── 安全 / IP（routers/security.py，P2 T5/T6）──────────────────── */
export const securityApi = {
  config: () => get<SecurityConfig>('/api/security/config'),
  saveConfig: (body: SecurityConfig) => post<SecurityConfig>('/api/security/config', body),
  rules: () => get<{items: IpRule[]}>('/api/security/rules'),
  addRule: (body: {kind: 'allow' | 'deny'; cidr: string; note?: string}) =>
    post<IpRule>('/api/security/rules', body),
  removeRule: (id: number) => del<{ok: boolean}>(`/api/security/rules/${id}`),
  logs: (limit = 200) => get<{items: IpAccessLog[]}>('/api/security/logs', {limit}),
  clearLogs: () => post<{ok: boolean}>('/api/security/logs/clear'),
  modelMap: () => get<{map: Record<string, string>}>('/api/security/model-map'),
  saveModelMap: (map: Record<string, string>) =>
    post<{map: Record<string, string>}>('/api/security/model-map', {map}),
};

/* ── 审计（routers/audit.py，P2 T2）────────────────────────────── */
export const auditApi = {
  list: (params?: {limit?: number; offset?: number; action?: string}) =>
    get<{items: AuditLog[]}>('/api/audit', params as Record<string, unknown>),
  clear: () => post<{ok: boolean; cleared: number}>('/api/audit/clear'),
};

/* ── 访问令牌（routers/tokens.py，P2 T7，wbt_ 只读）────────────── */
export const tokenApi = {
  list: () => get<{items: ApiToken[]}>('/api/tokens'),
  create: (body: {name: string; expires_at?: string | null}) =>
    post<CreatedApiToken>('/api/tokens', body),
  update: (id: number, body: {name?: string; enabled?: boolean; expires_at?: string | null}) =>
    patch<ApiToken>(`/api/tokens/${id}`, body),
  remove: (id: number) => del<{ok: boolean}>(`/api/tokens/${id}`),
};

/* ── 定时任务（routers/schedules.py）────────────────────────────── */
export const scheduleApi = {
  list: () => get<{items: Schedule[]}>('/api/schedules'),
  create: (body: Partial<Schedule>) => post<{id: number; ok: boolean}>('/api/schedules', body),
  update: (id: number, body: Partial<Schedule>) =>
    patch<{id: number; ok: boolean}>(`/api/schedules/${id}`, body),
  toggle: (id: number) => post<{id: number; enabled: boolean}>(`/api/schedules/${id}/toggle`),
  runNow: (id: number) => post<{id: number; result: {ok: boolean; error?: string; [k: string]: unknown}}>(`/api/schedules/${id}/run`),
  remove: (id: number) => del<{id: number; ok: boolean}>(`/api/schedules/${id}`),
};

/* ── 客户端参数（routers/client_profile.py）────────────────────── */
export const clientProfileApi = {
  get: () => get<ClientProfile>('/api/client-profile'),
  save: (body: Record<string, unknown>) => put<ClientProfile>('/api/client-profile', body),
  detect: () => post<ClientProfile>('/api/client-profile/detect'),
  reset: () => post<{ok: boolean}>('/api/client-profile/reset'),
};

/* ── 线上同步（routers/sync.py）────────────────────────────────── */
export const syncApi = {
  config: () => get<Record<string, unknown>>('/api/sync/config'),
  saveConfig: (body: Record<string, unknown>) =>
    post<Record<string, unknown>>('/api/sync/config', body),
  push: () => post<{ok: boolean; pushed: number}>('/api/sync/push'),
};

/* ── 系统（server.py + routers/releases.py）────────────────────── */
export const systemApi = {
  version: () => get<VersionInfo>('/api/app/version'),
  changelog: () => get<ChangelogResponse>('/api/app/changelog'),
};

/* ── 成长计划（routers/growth.py）──────────────────────────────── */
export const growthApi = {
  tasks: (refresh = false) => get<GrowthTasksResponse>('/api/growth/tasks', {refresh}),
  plans: () => get<{levels: Record<string, string>; plans: GrowthPlan[]}>('/api/growth/plans'),
  accountTasks: (accountId: number, refresh = false) =>
    get<GrowthAccountTasks>(`/api/growth/accounts/${accountId}/tasks`, {refresh}),
  accept: (account_ids: number[], task_codes: string[]) =>
    post<{results: unknown[]}>('/api/growth/accept', {account_ids, task_codes}),
  run: (account_ids: number[] = [], task_codes: string[] | null = null) =>
    post<{results: GrowthRunAccountResult[]; model: string}>('/api/growth/run', {account_ids, task_codes}),
  runAsync: (account_ids: number[] = [], task_codes: string[] | null = null) =>
    post<GrowthJobSnapshot>('/api/growth/run-async', {account_ids, task_codes}),
  job: (jobId: string) => get<GrowthJobSnapshot>(`/api/growth/job/${jobId}`),
  claim: (account_ids: number[], task_codes: string[] | null = null) =>
    post<{results: GrowthRunAccountResult[]}>('/api/growth/claim', {account_ids, task_codes}),
};

export type {Account};
