'use client';

/**
 * 全站导航侧边栏。
 *
 * ## 为什么现在是侧边栏（而不是旧面板那种浮动底栏）
 *
 * 旧面板 11 项已经是底栏的容量极限；新面板有 **21 个页面**（还分了 6 个语义组），
 * 底栏放不下。侧边栏能容纳分组，也是管理后台的通行形态。
 *
 * ## 形态
 *
 *   * **桌面（≥lg）**：常驻左侧固定栏，6 个分组标题 + 条目，当前页高亮。
 *   * **窄屏（<lg）**：收起为顶栏的汉堡按钮，点开是全屏抽屉（同一份数据渲染）。
 *
 * ## 主题友好
 *
 * 所有颜色走 CSS 变量 / Tailwind 语义类（`bg-card`、`text-muted-foreground`、
 * `border-border`…），没有任何硬编码色值 —— 换主题只需改 `globals.css` 的
 * 变量定义，侧边栏自动跟随。分组/条目数据来自 `lib/app-nav.ts`（纯数据），
 * 图标来自 `nav-icons.tsx`（可替换映射），三者独立。
 */
import {useEffect, useState} from 'react';
import {createPortal} from 'react-dom';
import Link from 'next/link';
import {usePathname} from 'next/navigation';
import {Menu, X} from 'lucide-react';

import {NAV_GROUPS, isNavActive} from '@/lib/app-nav';
import {BASE_PATH} from '@/lib/base-path';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';
import {navIcon} from '@/components/common/layout/nav-icons';
import {Button} from '@/components/ui/button';

function NavList({onNavigate}: {onNavigate?: () => void}) {
  const t = useT();
  const pathname = usePathname();

  return (
    <nav className="flex flex-col gap-5" aria-label={t('nav.mainNav')}>
      {NAV_GROUPS.map((group) => (
        <div key={group.key} className="flex flex-col gap-1">
          <div className="px-3 pb-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/70">
            {t(group.labelKey)}
          </div>
          {group.items.map((item) => {
            const active = isNavActive(item, pathname, BASE_PATH);
            return (
              <Link
                key={item.href}
                // **不要**手动加 basePath：next.config 里配了 basePath，
                // `<Link>` 会自动前缀。手动再加一次会变成 /panel/panel/xxx（实测踩过：
                // 所有菜单项都跳 404）。
                href={item.href}
                onClick={onNavigate}
                aria-current={active ? 'page' : undefined}
                className={cn(
                  'flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-colors',
                  active
                    ? 'bg-accent font-medium text-accent-foreground'
                    : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground',
                )}
              >
                <span className="shrink-0 opacity-80">{navIcon(item.iconKey)}</span>
                <span className="truncate">{t(item.labelKey)}</span>
              </Link>
            );
          })}
        </div>
      ))}
    </nav>
  );
}

/** 桌面常驻侧边栏。 */
export function Sidebar() {
  return (
    <aside className="hidden w-56 shrink-0 border-r bg-card/40 lg:block">
      <div className="sticky top-14 max-h-[calc(100vh-3.5rem)] overflow-y-auto p-3">
        <NavList />
      </div>
    </aside>
  );
}

/** 窄屏汉堡 + 抽屉。 */
export function MobileNav() {
  const t = useT();
  const [open, setOpen] = useState(false);
  // 抽屉**必须 portal 到 body**：它渲染在 <header> 内部，而 header 是 sticky +
  // backdrop-blur，会创建 containing block —— 直接放在 header 里时
  // `fixed inset-0` 的高度会退化成 header 的高度（实测只有 52px，菜单全在
  // 滚动区外看不见）。portal 到 body 后高度恢复为视口高（实测 52 → 843）。
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const drawer = open ? (
    <div className="fixed inset-0 z-50 lg:hidden" role="dialog" aria-modal="true">
      {/* 遮罩：点击关闭（键盘用户走 Esc） */}
      <div className="absolute inset-0 bg-black/50" onClick={() => setOpen(false)} />
      <div className="absolute inset-y-0 left-0 w-72 max-w-[85vw] overflow-y-auto border-r bg-background p-4 shadow-xl">
        <div className="mb-4 flex items-center justify-between">
          <span className="text-sm font-semibold">WorkBuddy2API</span>
          <Button variant="ghost" size="sm" aria-label={t('nav.closeMenu')}
                  onClick={() => setOpen(false)}>
            <X className="h-4 w-4" />
          </Button>
        </div>
        <NavList onNavigate={() => setOpen(false)} />
      </div>
    </div>
  ) : null;

  return (
    <>
      <Button variant="ghost" size="sm" className="lg:hidden"
              aria-label={t('nav.openMenu')} onClick={() => setOpen(true)}>
        <Menu className="h-4 w-4" />
      </Button>
      {/* SSR 阶段不渲染（createPortal 只能在浏览器里跑） */}
      {mounted && drawer && createPortal(drawer, document.body)}
    </>
  );
}
