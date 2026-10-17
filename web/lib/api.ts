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
  BenefitAccountState,
  BenefitCampaign,
  BenefitClaimResponse,
  BenefitQueryResponse,
  BenefitStatusResponse,
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
  ReleasesPayload,
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
  BackupFile,
  BackupExportResult,
  BackupPreview,
  BackupRestoreResult,
  LayoutPayload,
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
  /**
   * 一键解除该账号的**本地**冷却 / 熔断 / 降权。
   *
   * 只清本地推断的状态（`cool_until` / `breaker_until` / `breaker_fails` /
   * `degrade_until`）。**不动** `session_dead_fails`（上游 401/403 的权威信号，
   * 清了会让死号反复被选中）与 `status`（停用是人工决策）。
   *
   * 返回的 `cleared` 是**实际清掉的项**；没清到时为空数组 —— 前端据此如实提示，
   * 不要一律弹「已重置」。
   */
  clearCooldown: (id: number) =>
    post<{id: number; ok: boolean; cleared: string[];
          kept: {session_dead_fails: number; status: string}; message: string}>(
      `/api/accounts/${id}/cooldown/clear`),
  /** uid 重复分组（每组 keep=最早创建保留，remove=可清理冗余）。 */
  duplicates: () => get<{total: number; unique: number; redundant: number;
                        groups: {uid: string; keep: DupRef; remove: DupRef[]}[]}>(
    '/api/accounts/duplicates'),
  /** 去重：同 uid 只留 id 最小（历史记录最全）那条。 */
  dedupe: () => post<{removed: number; kept: number; items: {id: number; name: string; uid: string}[]}>(
    '/api/accounts/dedupe'),
  /**
   * 立即重采积分到期快照。
   *
   * ⚠️ **会真实请求上游**（逐号拉账单）。账号多时务必用 `realm` 收窄范围，
   * 或改用定时任务 —— 频繁全量触发对上游是很明显的机器特征。
   * `realm` 留空 = 全部活跃账号（既有行为）。
   */
  syncCredits: (realm?: string) => post<{
    ok: boolean; total: number; refreshed: number; failed: number;
    credits_expiring_soon: number; realm: string;
    detail: {id: number; account: string; ok: boolean; error?: string}[];
  }>('/api/accounts/credits/sync', realm ? {realm} : {}),
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
  /**
   * 每密钥并发上限：`null`/省略 = 跟随全局；`0` = 不限；`N>0` = 最多 N 路。
   * 显式传 `null` 会把已有值清回「跟随全局」。
   */
  max_concurrent?: number | null;
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
  /**
   * 模型配置列表。
   *
   * ⚠️ **必须带 realm**：后端按 realm 过滤；不传会返回两版混合的清单。
   * 两版模型交集只有 5 个 —— 混在一起会让人以为「同步了但没生效」。
   */
  configs: (level?: string, realm?: string) =>
    get<{items: ModelConfig[]; realm?: string}>(
      '/api/models/configs',
      { ...(level ? {level} : {}), ...(realm ? {realm} : {}) }),
  toggle: (id: number, enabled: boolean) =>
    patch<{id: number; model_id: string; enabled: boolean}>(`/api/models/${id}`, {enabled}),
  // 契约：{model_ids: string[], enabled, level}（不是 ids:number[]）
  batchToggle: (model_ids: string[], enabled: boolean, level = 'system') =>
    post<{updated: number; enabled: boolean}>('/api/models/batch-toggle', {model_ids, enabled, level}),
  remove: (id: number) => del<{id: number; model_id: string; ok: boolean}>(`/api/models/${id}`),
  /**
   * 从上游拉取模型清单并入库。
   *
   * `realm` **必须传对**：不同版本要从**对应版本的上游**拉清单
   * （国内版 `copilot.tencent.com` / 国际版 `www.workbuddy.ai`）。
   * 不传的话后端只能按 id 挑第一个活跃账号，切到国际版时依然会拿国内版账号
   * 去拉 —— 用户实测报过这个现象。
   */
  sync: (realm: string = 'cn') =>
    post<{realm: string; source_account_id: number; total_fetched: number;
          added: number; updated: number; total_in_db: number; enabled: number}>(
      '/api/models/sync', {realm}),
  /**
   * 上游模型繁忙程度。
   *
   * `realm` 限定用哪一版账号去问：**两版的上游是两个域**，繁忙度是各自独立的
   * 事实 —— 拿国内版的值去标注国际版模型，数字本身就是错的。
   */
  busy: (realm: string = '') =>
    get<ModelBusyResponse>('/api/models/busy', realm ? {realm} : undefined),
  // 模型中心目录（P4）：按 realm 直连腾讯，含 source 来源标注
  catalog: (realm: string = 'cn', force = false) =>
    get<ModelCatalogResponse>('/api/models/catalog', {realm, ...(force ? {force: true} : {})}),
  /**
   * 用账号池直调上游测一次速（真实请求，约 2-10s）。
   *
   * `realm` 限定用哪一版账号：后端会**按账号 realm 决定出站基址**
   * （原实现写死国内版基址 → 国际版账号测速会打国内版端点，直接判非法）。
   */
  speedTest: (model: string, max_tokens = 256, realm = '') =>
    post<SpeedTestResult>('/api/models/speed-test',
                          {model, max_tokens, ...(realm ? {realm} : {})}),
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
  /**
   * 积分到期总览。
   *
   * `realm` 按版本过滤（`cn` / `global` / 空=两版合计）。
   * 两版积分体系不通用，混在一起看不出是哪一版缺积分。
   */
  expiry: (params?: {days?: number; include_disabled?: boolean; realm?: string}) =>
    get<CreditExpiryStats>('/api/credits/expiry', params as Record<string, unknown>),
  /** 积分消耗构成（`realm` 同上）。 */
  usage: (params?: {days?: number; account_id?: number; top?: number; realm?: string}) =>
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
/**
 * 出站伪装的客户端参数档案。
 *
 * **必须按 realm 分开操作**：两版客户端身份本质不同（解包实测）——
 *
 *     | 字段            | cn           | global          |
 *     |-----------------|--------------|-----------------|
 *     | applicationName | `WorkBuddy`  | `workbuddy-ai`  |
 *     | desktop_version | 5.8.3        | 5.8.4           |
 *     | turingChannelId | 109144       | 400111          |
 *
 * 只存一份的话，国际版账号会带国内版身份出站 —— 上游一比对就知道不是官方客户端。
 */
export const clientProfileApi = {
  /** 读取某一版的档案（响应另含 `realms`：两版并排摘要）。 */
  get: (realm: string = 'cn') =>
    get<ClientProfile>('/api/client-profile', {realm}),
  /** 保存某一版（`realm` 也可放在 body 里）。 */
  save: (body: Record<string, unknown>, realm: string = 'cn') =>
    put<ClientProfile>('/api/client-profile', {...body, realm}),
  /** 现场探测**对应版本**的安装目录（只读，不写库）。 */
  detect: (realm: string = 'cn') =>
    post<ClientProfile & {realm?: string; message?: string}>(
      '/api/client-profile/detect', {realm}),
  /** 重置（**只重置指定版本** —— 不会动另一版）。 */
  reset: (realm: string = 'cn') =>
    post<{ok: boolean; realm: string}>('/api/client-profile/reset', {realm}),
};

/* ── 线上同步（routers/sync.py）────────────────────────────────── */
export const syncApi = {
  config: () => get<Record<string, unknown>>('/api/sync/config'),
  saveConfig: (body: Record<string, unknown>) =>
    post<Record<string, unknown>>('/api/sync/config', body),
  // 带上当前输入框的 remote_url/secret：服务端会先落库再推送，用户不必先点保存
  // （契约见 routers/sync.py::push —— body 可空，优先用请求体里的值）
  push: (body?: {remote_url?: string; secret?: string}) =>
    post<{ok: boolean; remote: string; accounts: number; models: number;
          client_profile: boolean; remote_result: Record<string, unknown>}>(
      '/api/sync/push', body ?? {}),
};

/* ── 逆向产物（routers/app_source.py，需 ADMIN_DEV_TOOLS=1）──────── */
export const appSourceApi = {
  status: () => get<Record<string, unknown>>('/api/app-source/status'),
  detect: () => post<Record<string, unknown>>('/api/app-source/detect'),
  locate: (path: string) => post<Record<string, unknown>>('/api/app-source/locate', {path}),
  resetPaths: () => post<Record<string, unknown>>('/api/app-source/reset-paths'),
  unpackPlan: () => post<Record<string, unknown>>('/api/app-source/unpack/plan'),
  unpack: (dest: string, force = false) =>
    post<Record<string, unknown>>('/api/app-source/unpack', {dest, force}),
  unpackProgress: () => get<Record<string, unknown>>('/api/app-source/unpack/progress'),
  remove: () => post<Record<string, unknown>>('/api/app-source/remove'),
  search: (q: string, limit = 20) =>
    get<{items: {file: string; line: number; text: string}[]}>(
      '/api/app-source/search', {q, limit}),
};

/* ── 界面布局（routers/layout.py，用户自定义菜单）──────────────── */
export const layoutApi = {  get: () => get<{layout: LayoutPayload; items: string[]; groups: string[]}>('/api/layout'),
  save: (body: LayoutPayload) =>
    put<{ok: boolean; layout: LayoutPayload}>('/api/layout', body),
  reset: () => post<{ok: boolean; layout: LayoutPayload}>('/api/layout/reset'),
};

/* ── 系统（server.py + routers/releases.py）────────────────────── */
export const systemApi = {
  version: () => get<VersionInfo>('/api/app/version'),
  changelog: () => get<ChangelogResponse>('/api/app/changelog'),
  /**
   * 版本更新信息（**只读**：浏览器里不能下载/安装）。
   *
   * 与桌面端的 `pywebview.api.check_updates()` 返回同一份结构，
   * 所以 UI 不需要分叉。下载/安装那两个动作只能由桌面端桥执行。
   */
  releases: () => get<ReleasesPayload>('/api/app/releases'),
};

/* ── 本地快照备份（routers/backup.py，P5）───────────────────────── */
export const backupApi = {
  list: () => get<{items: BackupFile[]}>('/api/backup/list'),
  export: (name: string, includeSecrets: boolean) =>
    post<BackupExportResult>('/api/backup/export', {name, include_secrets: includeSecrets}),
  preview: (name: string) =>
    get<BackupPreview>(`/api/backup/download/${encodeURIComponent(name)}/preview`),
  // 恢复是覆盖式高危操作：后端会自动留底
  restore: (name: string) =>
    post<BackupRestoreResult>('/api/backup/restore', {name}),
  downloadUrl: (name: string) => `/api/backup/download/${encodeURIComponent(name)}`,
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

/* ── 限时权益 / 补偿（routers/benefits.py）─────────────────────
 *
 * **本功能不参与任何定时任务**：没有调度入口，只能由用户手动点。
 * 领取是写操作且不可回滚（上游每账号每期只能领一次），所以分成三步：
 *   campaign/status（读本地） → query（读上游） → claim（写上游）
 * 前端必须让用户看到 query 结果后再决定是否 claim。
 */
export const benefitsApi = {
  campaign: () => get<BenefitCampaign>('/api/benefits/campaign'),
  status: () => get<BenefitStatusResponse>('/api/benefits/status'),
  query: (account_ids: number[]) =>
    post<BenefitQueryResponse>('/api/benefits/query', {account_ids}),
  claim: (account_ids: number[]) =>
    post<BenefitClaimResponse>('/api/benefits/claim', {account_ids}),
};

export type {Account};
