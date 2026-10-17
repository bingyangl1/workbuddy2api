'use client';

/**
 * 版本号 + 更新入口（侧边栏底部）。
 *
 * ## 为什么需要
 *
 * 旧面板在左下角常驻一个版本按钮（`#appVersion`），点开是「版本与更新」。
 * 新面板完全没有版本显示 —— 桌面用户既看不到版本，也不知道有新版本可装。
 *
 * ## 行为
 *
 * * 挂载时静默检查一次（失败**静默**：版本号不该因为网络问题变成错误提示；
 *   只在点开对话框时才显示具体错误）。
 * * 发现新版本 → 按钮变强调色 + 「发现新版本」副标题。
 * * 点击 → 打开 `UpdateDialog`。
 *
 * 检查是「自动 + 静默」的，与旧面板一致：启动时只提示、不弹模态、不强制。
 */

import {useCallback, useEffect, useState} from 'react';
import {ArrowUpCircle, Info} from 'lucide-react';

import {UpdateDialog} from '@/components/desktop/UpdateDialog';
import {checkUpdates, type ReleasesPayload} from '@/lib/desktop-bridge';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';

export function VersionButton({compact = false}: {compact?: boolean}) {
  const t = useT();
  const [data, setData] = useState<ReleasesPayload | null>(null);
  const [open, setOpen] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const payload = await checkUpdates();
      // 检查失败但拿到了缓存版本号时仍显示版本（只是不提示更新）
      setData(payload);
    } catch {
      /* 静默：版本号不该因为网络问题变成错误 */
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      // 稍等再查：避免和首屏其它请求抢带宽、也避开桥尚未就绪的窗口
      await new Promise((r) => window.setTimeout(r, 1200));
      if (cancelled) return;
      await refresh();
    })();
    return () => {
      cancelled = true;
    };
  }, [refresh]);

  const version = data?.version || '';
  const hasUpdate = !!data?.available;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        title={hasUpdate ? t('updatePanel.hasUpdateTip') : t('updatePanel.versionEntry')}
        className={cn(
          'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs transition-colors',
          'hover:bg-accent hover:text-accent-foreground',
          hasUpdate && 'text-primary',
        )}
      >
        {hasUpdate ? (
          <ArrowUpCircle className="size-3.5 shrink-0" />
        ) : (
          <Info className="size-3.5 shrink-0 opacity-60" />
        )}
        {!compact && (
          <span className="min-w-0 flex-1 truncate">
            {version ? `v${version}` : t('updatePanel.versionEntry')}
            {hasUpdate && (
              <span className="ml-1 opacity-80">{t('updatePanel.newVersionSuffix')}</span>
            )}
          </span>
        )}
      </button>
      <UpdateDialog open={open} onClose={() => setOpen(false)} />
    </>
  );
}
