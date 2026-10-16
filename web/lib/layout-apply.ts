/**
 * 布局叠加的**纯逻辑**（无 React 依赖）。
 *
 * ## 为什么单独成文件（不是放在 layout-context.tsx 里）
 *
 * 这里全是纯函数（输入菜单 + 用户配置 → 输出菜单），可以被 node 直接 import 做单测。
 * 而 React 组件所在的 `.tsx` 文件 Node **无法** type-strip（JSX 语法），
 * 放一起就没法单测了（实测：`node --experimental-strip-types` 报
 * `ERR_UNKNOWN_FILE_EXTENSION: Unknown file extension ".tsx"`）。
 *
 * 同类先例：`lib/app-nav.ts`（纯数据 + 纯函数，零 React 依赖）也是这个理由。
 */
import type {NavGroup, NavItem} from '@/lib/app-nav';

export interface LayoutConfig {
  /** 被隐藏的 href。 */
  hidden: string[];
  /** 自定义顺序（不在其中的条目排在后面，保持默认相对顺序）。 */
  order: string[];
  /** 分组显示名覆盖（groupKey → 自定义名字）。 */
  groupLabels: Record<string, string>;
}

export const EMPTY_LAYOUT: LayoutConfig = {hidden: [], order: [], groupLabels: {}};

/**
 * **不允许被隐藏**的条目。
 *
 * 布局配置页本身必须常驻菜单 —— 否则用户把它隐藏后，就再也进不去配置页
 * 把布局改回来（只能手改数据库）。这是本功能唯一的安全约束：
 * 其他条目随便隐藏，唯独这几个必须留着。
 */
export const MUST_KEEP_VISIBLE: readonly string[] = ['/settings/layout'];

/**
 * 把用户覆盖叠加到默认菜单上。
 *
 * 语义：
 *   * `hidden` 里的条目被移除（MUST_KEEP_VISIBLE 除外）
 *   * `order` 只影响**组内**次序；未列出的条目按默认相对顺序排在后面
 *   * `groupLabels` 覆盖分组标题（labelOverride）
 *   * 空组被剔除（不渲染只有标题的组）
 *   * 未知 href 静默忽略（不报错、不影响其他条目）
 */
export function applyLayout(base: readonly NavGroup[], cfg: LayoutConfig): NavGroup[] {
  // 必须常驻的条目即使被写进 hidden 也强制可见（防用户把自己锁在门外）
  const hidden = new Set(cfg.hidden.filter((h) => !MUST_KEEP_VISIBLE.includes(h)));

  const rank = new Map<string, number>();
  cfg.order.forEach((h, i) => rank.set(h, i));

  const seen = new Set<string>();
  const orderedAll: NavItem[] = [];
  const baseItems = base.flatMap((g) => g.items);
  // 先放 order 里指定的（且必须真实存在）
  for (const h of cfg.order) {
    const item = baseItems.find((i) => i.href === h);
    if (item && !seen.has(h)) {
      seen.add(h);
      orderedAll.push(item);
    }
  }
  // 再放剩下的，保持默认顺序
  for (const item of baseItems) {
    if (!seen.has(item.href)) {
      seen.add(item.href);
      orderedAll.push(item);
    }
  }

  const visible = orderedAll.filter((i) => !hidden.has(i.href));

  return base
    .map((g) => {
      const items = visible.filter((i) => g.items.some((gi) => gi.href === i.href));
      const label = cfg.groupLabels[g.key];
      return {
        ...g,
        ...(label ? {labelOverride: label} : {}),
        items,
      } as NavGroup;
    })
    .filter((g) => g.items.length > 0);
}
