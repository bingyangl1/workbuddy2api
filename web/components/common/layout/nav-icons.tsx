'use client';

/**
 * 导航图标映射（数据与表现分离）。
 *
 * `lib/app-nav.ts` 只存 `iconKey` 字符串 —— 它是**纯数据**（可序列化、
 * 将来可被用户自定义配置覆盖）。真正的图标组件映射在这里。
 *
 * ## 为什么这样分
 *
 * 用户要求「给后续做主题或者自定义主题留条路」。若把图标组件直接写进
 * 导航清单，那份清单就变成了「代码」：用户想换一套图标风格、或想隐藏/重排菜单项，
 * 都得改源码。分离之后：
 *   * **换肤**：替换本文件的映射表即可（或按主题提供多套映射）。
 *   * **自定义菜单**：只覆盖 `app-nav.ts` 的数据（本地存储/后端下发都行），
 *     图标仍由 key 查表得到。
 */
import type {ReactNode} from 'react';
import {
  Activity,
  Bot,
  Boxes,
  CalendarClock,
  Coins,
  FileClock,
  Gauge,
  Gift,
  KeyRound,
  LayoutPanelLeft,
  MessageSquare,
  PackageOpen,
  ScrollText,
  Settings,
  ShieldCheck,
  Sprout,
  Users,
  DatabaseBackup,
  RefreshCw,
  KeySquare,
  LayoutDashboard,
  ListTree,
} from 'lucide-react';

import type {NavIconKey} from '@/lib/app-nav';

const ICONS: Record<NavIconKey, ReactNode> = {
  dashboard: <LayoutDashboard className="h-4 w-4" />,
  accounts: <Users className="h-4 w-4" />,
  keys: <KeyRound className="h-4 w-4" />,
  tokens: <KeySquare className="h-4 w-4" />,
  models: <Boxes className="h-4 w-4" />,
  catalog: <ListTree className="h-4 w-4" />,
  playground: <MessageSquare className="h-4 w-4" />,
  stats: <Activity className="h-4 w-4" />,
  logs: <FileClock className="h-4 w-4" />,
  credits: <Coins className="h-4 w-4" />,
  schedules: <CalendarClock className="h-4 w-4" />,
  growth: <Sprout className="h-4 w-4" />,
  benefits: <Gift className="h-4 w-4" />,
  security: <ShieldCheck className="h-4 w-4" />,
  audit: <ScrollText className="h-4 w-4" />,
  accountGroups: <Gauge className="h-4 w-4" />,
  settings: <Settings className="h-4 w-4" />,
  sync: <RefreshCw className="h-4 w-4" />,
  password: <KeySquare className="h-4 w-4" />,
  appSource: <PackageOpen className="h-4 w-4" />,
  backup: <DatabaseBackup className="h-4 w-4" />,
  changelog: <Bot className="h-4 w-4" />,
  layout: <LayoutPanelLeft className="h-4 w-4" />,
};

export function navIcon(key: NavIconKey): ReactNode {
  return ICONS[key] ?? <Boxes className="h-4 w-4" />;
}
