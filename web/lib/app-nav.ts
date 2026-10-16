/**
 * 全站导航菜单的**唯一数据源**（P3 收尾：接线全局导航）。
 *
 * ## 为什么单独建这个文件
 *
 * 之前有三处各自维护导航清单：
 *   * `lib/section-nav.ts`       —— 页内二级 Tab（账号/密钥/模型/设置 四节）
 *   * `lib/command-palette.ts`   —— 命令面板（Ctrl+K）的页面索引
 *   * `components/.../Sidebar`   —— 之前**不存在**（这正是「没有菜单」的根因）
 *
 * 三处各写一份的结果是**漂移**：命令面板里登记了 `/tasks`、`/red-packets`、
 * `/settings/upstream` 等 9 条页面（从 manager 抄骨架时带过来的），
 * 而本引擎根本没有这些页面 —— 点进去 404。逐页做 parity 时只验了「直接访问
 * URL」，没有端到端走一遍「从菜单点进去」，所以没暴露。
 *
 * 所以现在只留**一份**页面清单（本文件）：侧边栏、命令面板、页内 Tab 都从这里取。
 * 加页面 = 只改这里（加一行），三处自动同步。
 *
 * ## 主题/换肤友好（用户要求：给后续做主题留条路）
 *
 * 分组与项目只存 `iconKey`（字符串）而不是图标组件本身 —— 这样本文件是**纯数据**，
 * 没有 React/图标库依赖，将来：
 *   * 换肤只需替换 `components/common/layout/nav-icons.tsx` 的映射表；
 *   * 想让用户自定义菜单顺序/隐藏项，只需把这个常量改成「默认值 + 本地覆盖」。
 * 图标映射单独放，是为了让「数据」与「表现」分开 —— 数据可以被持久化/序列化，
 * 组件不行。
 */

/** 图标标识。与 `components/common/layout/nav-icons.tsx` 的映射表一一对应。 */
export type NavIconKey =
  | 'dashboard'
  | 'accounts'
  | 'keys'
  | 'tokens'
  | 'models'
  | 'catalog'
  | 'playground'
  | 'stats'
  | 'logs'
  | 'credits'
  | 'schedules'
  | 'growth'
  | 'security'
  | 'audit'
  | 'accountGroups'
  | 'settings'
  | 'sync'
  | 'password'
  | 'appSource'
  | 'backup'
  | 'changelog'
  | 'layout';

/** 侧边栏分组。顺序即渲染顺序。 */
export type NavGroupKey =
  | 'overview'
  | 'ops'
  | 'data'
  | 'automation'
  | 'governance'
  | 'system';

export type NavItem = {
  /** 站内绝对路径，**不带** basePath（Link 自己会加）。 */
  href: string;
  /** 显示名的 i18n 键。 */
  labelKey: string;
  iconKey: NavIconKey;
  /** 命中该路径时同时点亮（用于子路由，如 /settings 点亮 settings 节）。 */
  matchPrefix?: string;
};

export type NavGroup = {
  key: NavGroupKey;
  labelKey: string;
  items: readonly NavItem[];
  /**
   * 用户自定义的分组名（覆盖 labelKey 的翻译结果）。
   *
   * 由 `lib/layout-context.tsx` 在叠加用户布局时注入；默认菜单里不设。
   * 为什么是「覆盖值」而不是「直接改 labelKey」：labelKey 指向 i18n 词条
   * （多语言），用户自定义名是**单一语言的用户输入** —— 语义不同，
   * 分开存才不会把用户输入混进翻译表。
   */
  labelOverride?: string;
};

/**
 * 侧边栏全部条目。
 *
 * ⚠️ **加页面只改这里**：`lib/sidebar-nav.ts`（侧边栏）、`lib/command-palette.ts`
 * （命令面板）都从这里派生；`tests/test_nav_single_source.py` 会校验
 * 「本清单里的 href 都真实存在页面」+「真实页面都在清单里」，
 * 防止再出现「登记了但不存在」的死链。
 */
export const NAV_GROUPS: readonly NavGroup[] = [
  {
    key: 'overview',
    labelKey: 'nav.groupOverview',
    items: [
      {href: '/dashboard', labelKey: 'nav.dashboard', iconKey: 'dashboard'},
    ],
  },
  {
    key: 'ops',
    labelKey: 'nav.groupOps',
    items: [
      {href: '/accounts', labelKey: 'nav.accounts', iconKey: 'accounts'},
      {href: '/keys', labelKey: 'nav.keys', iconKey: 'keys'},
      {href: '/tokens', labelKey: 'nav.tokens', iconKey: 'tokens'},
      {href: '/models', labelKey: 'nav.models', iconKey: 'models', matchPrefix: '/models'},
      {href: '/models/catalog', labelKey: 'nav.catalog', iconKey: 'catalog'},
      {href: '/playground', labelKey: 'nav.playground', iconKey: 'playground'},
    ],
  },
  {
    key: 'data',
    labelKey: 'nav.groupData',
    items: [
      {href: '/stats', labelKey: 'nav.stats', iconKey: 'stats'},
      {href: '/logs', labelKey: 'nav.logs', iconKey: 'logs'},
      {href: '/credits', labelKey: 'nav.credits', iconKey: 'credits'},
    ],
  },
  {
    key: 'automation',
    labelKey: 'nav.groupAutomation',
    items: [
      {href: '/schedules', labelKey: 'nav.schedules', iconKey: 'schedules'},
      {href: '/growth', labelKey: 'nav.growth', iconKey: 'growth'},
    ],
  },
  {
    key: 'governance',
    labelKey: 'nav.groupGovernance',
    items: [
      {href: '/security', labelKey: 'nav.security', iconKey: 'security'},
      {href: '/audit', labelKey: 'nav.audit', iconKey: 'audit'},
      {href: '/account-groups', labelKey: 'nav.accountGroups', iconKey: 'accountGroups'},
    ],
  },
  {
    key: 'system',
    labelKey: 'nav.groupSystem',
    items: [
      {href: '/settings', labelKey: 'nav.settings', iconKey: 'settings', matchPrefix: '/settings'},
      {href: '/settings/sync', labelKey: 'sync.title', iconKey: 'sync'},
      {href: '/settings/password', labelKey: 'password.title', iconKey: 'password'},
      {href: '/settings/app-source', labelKey: 'appSource.title', iconKey: 'appSource'},
      {href: '/backup', labelKey: 'nav.backup', iconKey: 'backup'},
      {href: '/changelog', labelKey: 'nav.changelog', iconKey: 'changelog'},
      {href: '/settings/layout', labelKey: 'layout.title', iconKey: 'layout'},
    ],
  },
];

/** 全部条目（扁平）。 */
export function allNavItems(): readonly NavItem[] {
  return NAV_GROUPS.flatMap((g) => g.items);
}

/**
 * 判断某个路径是否命中该条目（用于「当前页高亮」）。
 *
 * 规则：**精确匹配**该条目的 href，或（仅当整份清单里**没有任何条目精确匹配**时）
 * 该条目声明了 `matchPrefix` 且路径在它下面。
 *
 * 为什么要「精确优先」：`/models/catalog` 既精确命中「模型中心」，也落在
 * 「模型」（matchPrefix=/models）下面 —— 两个都亮是错的（实测踩过，侧边栏同时
 * 高亮「模型」与「模型中心」）。同理 `/settings/sync` 不该同时点亮「设置」。
 * 只有当路径**不属于任何具体条目**（例如将来加了 `/settings/foo` 但忘了登记）时，
 * 才回落到父条目的 matchPrefix 高亮 —— 这样新页面至少有个合理的归属提示。
 */
export function isNavActive(item: NavItem, pathname: string, basePath = ''): boolean {
  const prefix = (basePath || '').replace(/\/+$/, '');
  let p = (pathname || '').replace(/\/+$/, '');
  if (prefix && p.startsWith(prefix)) p = p.slice(prefix.length);
  if (!p) return false;
  if (p === item.href) return true;

  // 这份清单里有别的条目精确命中该路径 → 该路径属于它，本条目不抢高亮
  const exactHitElsewhere = allNavItems().some((other) => other.href === p);
  if (exactHitElsewhere) return false;

  if (item.matchPrefix) {
    return p === item.matchPrefix || p.startsWith(item.matchPrefix + '/');
  }
  return false;
}

/**
 * 由真实路径反查它属于哪一组（用于侧边栏自动展开当前分组）。
 * 认不出返回 null。
 */
export function activeGroupOf(pathname: string, basePath = ''): NavGroupKey | null {
  for (const g of NAV_GROUPS) {
    for (const item of g.items) {
      if (isNavActive(item, pathname, basePath)) return g.key;
    }
  }
  return null;
}
