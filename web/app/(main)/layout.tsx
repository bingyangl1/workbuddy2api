'use client';

import {useEffect} from 'react';
import {useRouter} from 'next/navigation';
import {LanguageToggle} from '@/components/common/layout/LanguageToggle';
import {RealmToggle} from '@/components/common/layout/RealmToggle';
import {ThemeToggle} from '@/components/common/layout/ThemeToggle';
import {MobileNav, Sidebar} from '@/components/common/layout/Sidebar';
import {RealmProvider} from '@/lib/realm-context';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {Button} from '@/components/ui/button';
import {LogOut} from 'lucide-react';

export default function MainLayout({
  children,
}: {
  children: React.ReactNode
}) {
  const {authed, loading, logout} = useAuth();
  const t = useT();
  const router = useRouter();

  // 仅在确认未登录时跳转；不阻塞内容渲染，避免每次切页闪一下
  useEffect(() => {
    if (!loading && !authed) router.replace('/login');
  }, [loading, authed, router]);

  return (
    <RealmProvider>
      <div className="min-h-screen flex flex-col">
        {/* 顶栏：窄屏汉堡 + 品牌 + 主题/语言/版本切换 + 登出。
            全局导航在侧边栏（Sidebar / MobileNav）；菜单数据**单一来源**见
            lib/app-nav.ts —— 以前三处各写一份导致漂移（命令面板登记了 9 个
            不存在的页面），现已统一。 */}
        <header className="sticky top-0 z-30 flex items-center justify-between gap-1 border-b bg-background/80 px-2 py-2 backdrop-blur sm:gap-2 sm:px-6">
          {/* 左侧：汉堡（窄屏）+ 品牌。
              品牌文字在**最窄屏（<sm）隐藏**：加了汉堡按钮后，375px 顶栏装不下
              「汉堡 + 品牌 + 主题 + 语言 + 版本 + 登出」（实测右侧按钮组被推出视口，
              scrollWidth 389 > 375）。窄屏靠抽屉里的品牌名辨识即可。 */}
          <div className="flex min-w-0 items-center gap-1.5">
            <MobileNav />
            <div className="hidden truncate text-sm font-semibold tracking-tight sm:block">WorkBuddy2API</div>
          </div>
          <div className="flex shrink-0 items-center gap-0.5 sm:gap-2">
            <ThemeToggle />
            <LanguageToggle />
            {/* 窄屏（<sm）只显图标，避免 375px 横向溢出 */}
            <span className="hidden sm:inline-flex"><RealmToggle /></span>
            <span className="inline-flex sm:hidden"><RealmToggle compact /></span>
            <Button variant="ghost" size="sm" onClick={logout} title={t('profile.logout')} aria-label={t('profile.logout')}>
              <LogOut className="h-4 w-4" />
            </Button>
          </div>
        </header>
        {/* 侧边栏 + 内容区：桌面并排；窄屏侧边栏收进顶栏汉堡抽屉 */}
        <div className="flex flex-1 flex-col lg:flex-row">
          <Sidebar />
          <div className="flex min-h-0 flex-1 flex-col">
            <div className="@container/main flex flex-1 flex-col gap-2">
              <div className="flex min-h-0 flex-1 flex-col px-4 py-6 sm:px-6 md:px-8 lg:px-10">
                <div className="mx-auto flex min-h-0 w-full max-w-7xl flex-1 flex-col gap-4 md:gap-6">
                  {children}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </RealmProvider>
  );
}
