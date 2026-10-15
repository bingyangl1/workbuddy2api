'use client';

import {useEffect} from 'react';
import {useRouter} from 'next/navigation';
import {LanguageToggle} from '@/components/common/layout/LanguageToggle';
import {RealmToggle} from '@/components/common/layout/RealmToggle';
import {ThemeToggle} from '@/components/common/layout/ThemeToggle';
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
        {/* 顶栏（Wave A 简化版）：品牌 + 语言/版本切换 + 登出。
            浮动底栏（ManagementBar）与命令面板在 Wave B 逐页接入时一并恢复，
            它们引用了 manager 专有的「添加账号/版本检查」等逻辑，需先适配本引擎。 */}
        <header className="sticky top-0 z-10 flex items-center justify-between gap-2 border-b bg-background/80 px-3 py-2 backdrop-blur sm:px-6">
          <div className="text-sm font-semibold tracking-tight">WorkBuddy2API</div>
          <div className="flex items-center gap-1.5 sm:gap-2">
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
        <div className="flex flex-1 flex-col">
          <div className="@container/main flex flex-1 flex-col gap-2">
            <div className="flex min-h-0 flex-1 flex-col px-4 py-6 sm:px-6 md:px-8 lg:px-12">
              <div className="mx-auto flex min-h-0 w-full max-w-7xl flex-1 flex-col gap-4 md:gap-6">
                {children}
              </div>
            </div>
          </div>
        </div>
      </div>
    </RealmProvider>
  );
}
