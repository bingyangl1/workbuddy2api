'use client';

/**
 * 界面布局（用户自定义菜单）——React 上下文与读写。
 *
 * ## 数据流
 *
 *   AppNav（默认菜单，`lib/app-nav.ts`）        ← 编译期常量
 *            ↓ 叠加用户覆盖（`lib/layout-apply.ts` 纯函数）
 *   LayoutContext（本文件：加载/保存/状态）
 *            ↓
 *   Sidebar 渲染（已过滤 + 已重排 + 已改名）
 *
 * 纯逻辑放在 `layout-apply.ts`（可以用 node 单测）；本文件只管 React 状态与
 * 后端读写 —— `.tsx` 无法被 node type-strip（JSX），混在一起就没法测了。
 *
 * ## 为什么覆盖层是「差量」而不是「完整菜单」
 *
 * 存整份菜单的坏处：将来加页面时，用户那份旧快照 **不认识新页面**，新页面永远
 * 不会出现在他的菜单里（除非他手动重置）。存差量（隐藏了谁 / 顺序 / 分组名）
 * 的话，新页面默认可见、按默认位置出现 —— 对用户是「多了一个入口」而不是
 * 「重置才看得到」。
 *
 * ## 存储与失败处理
 *
 * 布局存**后端**（`GET/PUT /api/layout` → system_settings.ui_layout）。
 * 读写失败一律**回落到默认布局**并继续渲染：布局是锦上添花的偏好，
 * 绝不能因为它读不到就让整个面板没有导航（那是灾难性的）。
 */
import {
  createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode,
} from 'react';

import {NAV_GROUPS, type NavGroup} from '@/lib/app-nav';
import {applyLayout, EMPTY_LAYOUT, MUST_KEEP_VISIBLE, type LayoutConfig} from '@/lib/layout-apply';
import {layoutApi} from '@/lib/api';

// 纯逻辑与常量从这里透出，调用方只 import 本模块一处即可
export {applyLayout, EMPTY_LAYOUT, MUST_KEEP_VISIBLE};
export type {LayoutConfig};

interface LayoutContextValue {
  /** 当前生效的布局（后端返回 / 默认）。 */
  layout: LayoutConfig;
  /** 应用布局后的菜单（已过滤 + 已重排 + 已改名）。侧边栏直接用这个。 */
  groups: NavGroup[];
  /** 后端是否已加载完（避免首屏闪现默认布局再跳变）。 */
  loaded: boolean;
  /** 保存（后端）。失败抛错，由调用方决定怎么提示。 */
  save: (next: LayoutConfig) => Promise<void>;
  /** 恢复默认（后端删键）。 */
  reset: () => Promise<void>;
}

const LayoutContext = createContext<LayoutContextValue | null>(null);

function normalize(raw: unknown): LayoutConfig {
  const l = (raw || {}) as Partial<LayoutConfig>;
  return {
    hidden: Array.isArray(l.hidden) ? l.hidden : [],
    order: Array.isArray(l.order) ? l.order : [],
    groupLabels: (l.groupLabels && typeof l.groupLabels === 'object') ? l.groupLabels : {},
  };
}

export function LayoutProvider({children}: {children: ReactNode}) {
  const [layout, setLayout] = useState<LayoutConfig>(EMPTY_LAYOUT);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    layoutApi.get()
      .then((res) => { if (alive) setLayout(normalize(res.layout)); })
      // 读失败 → 用默认布局继续渲染（布局不该阻断导航）
      .catch(() => { if (alive) setLayout(EMPTY_LAYOUT); })
      .finally(() => { if (alive) setLoaded(true); });
    return () => { alive = false; };
  }, []);

  const save = useCallback(async (next: LayoutConfig) => {
    const res = await layoutApi.save(next);
    setLayout(normalize(res.layout));
  }, []);

  const reset = useCallback(async () => {
    await layoutApi.reset();
    setLayout(EMPTY_LAYOUT);
  }, []);

  const groups = useMemo(() => applyLayout(NAV_GROUPS, layout), [layout]);

  const value = useMemo<LayoutContextValue>(
    () => ({layout, groups, loaded, save, reset}),
    [layout, groups, loaded, save, reset],
  );

  return <LayoutContext.Provider value={value}>{children}</LayoutContext.Provider>;
}

export function useLayout(): LayoutContextValue {
  const ctx = useContext(LayoutContext);
  if (!ctx) throw new Error('useLayout 必须在 LayoutProvider 内使用');
  return ctx;
}
