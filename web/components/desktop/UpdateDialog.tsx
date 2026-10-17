'use client';

/**
 * 版本与更新对话框（桌面端 + 浏览器双形态）。
 *
 * ## 来源
 *
 * 旧面板的版本入口在 `admin/static/desktop.js`（原生 DOM 拼装），只服务
 * legacy 面板。新面板（`/panel`）此前**完全没有版本显示** —— 桌面用户看不到
 * 当前版本，也无法发现/安装更新。本组件把这套能力搬进 React。
 *
 * ## 两种形态
 *
 * | 环境 | 数据来源 | 可下载/安装 |
 * |---|---|---|
 * | 桌面端（PyWebView） | `pywebview.api.check_updates()` | ✅ |
 * | 浏览器 / 服务端部署 | `GET /api/app/releases` | ❌（只读） |
 *
 * 两边返回**同一份结构**（`ReleasesPayload`），所以列表、历史版本、
 * 「打开 Releases」等 UI 完全共用；只有「下载更新 / 重启并安装」两个按钮
 * 在非桌面端隐藏 —— 浏览器没有权限替换 EXE，显示出来只会点了报错。
 *
 * ## 安全
 *
 * * Release 正文（`body`）来自 GitHub，**一律当纯文本渲染**（`white-space: pre-wrap`），
 *   绝不 `dangerouslySetInnerHTML`。旧面板也遵循同一条（见其源码首行注释）。
 * * 「打开 Releases」只对 `/api/app/version` 下发的 `releases_url` 前缀放行，
 *   避免把远端可控的 URL 变成任意跳转（`release_url()` 在服务端已校验过一次，
 *   这里再挡一层）。
 */

import {useCallback, useEffect, useRef, useState} from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  ExternalLink,
  Loader2,
  RefreshCw,
  RotateCcw,
} from 'lucide-react';

import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {
  applyUpdate,
  checkUpdates,
  downloadUpdate,
  isNativeShell,
  pollUpdateStatus,
  retryRuntime,
  type ReleaseRow,
  type ReleasesPayload,
  type UpdateState,
} from '@/lib/desktop-bridge';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';

interface Props {
  open: boolean;
  onClose: () => void;
}

/** 时间戳 → 本地可读（拿不到就原样显示）。 */
function fmtDate(iso: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function UpdateDialog({open, onClose}: Props) {
  const t = useT();
  const [data, setData] = useState<ReleasesPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [status, setStatus] = useState<UpdateState>({
    busy: false,
    message: '',
    ready: false,
  });
  const [runtime, setRuntime] = useState('');
  const [native, setNative] = useState(false);
  const pollRef = useRef<number>(0);

  /** 检查更新（幂等，可重入）。 */
  const refresh = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const payload = await checkUpdates();
      setData(payload);
      if (payload.error) setError(payload.error);
      if (payload.update) setStatus(payload.update);
      if (payload.runtime !== undefined) setRuntime(payload.runtime);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  // 打开时检查一次；原生判断放在 effect 里（SSR 期间没有 window）。
  useEffect(() => {
    if (!open) return;
    setNative(isNativeShell());
    void refresh();
  }, [open, refresh]);

  // 轮询下载/安装进度（仅桌面端）。用 ref 存 timer，关闭时清掉，
  // 否则对话框反复开关会堆叠多个 interval —— 旧实现就有这个隐患。
  useEffect(() => {
    if (!open || !native) return;
    const tick = async () => {
      const st = await pollUpdateStatus();
      if (!st) return;
      setStatus({busy: !!st.busy, message: st.message || '', ready: !!st.ready});
      if (st.runtime !== undefined) setRuntime(st.runtime);
    };
    pollRef.current = window.setInterval(tick, 1200);
    return () => {
      window.clearInterval(pollRef.current);
      pollRef.current = 0;
    };
  }, [open, native]);

  // Esc 关闭（无障碍与习惯一致）
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  const onDownload = async () => {
    const latest = data?.latest;
    if (!latest) return;
    setStatus((s) => ({...s, busy: true, message: t('updatePanel.downloading')}));
    const r = await downloadUpdate(latest.id);
    if (r.error) setStatus((s) => ({...s, busy: false, message: r.error!}));
  };

  const onInstall = async () => {
    setStatus((s) => ({...s, busy: true, message: t('updatePanel.applying')}));
    const r = await applyUpdate();
    if (r.error) setStatus((s) => ({...s, busy: false, message: r.error!}));
  };

  if (!open) return null;

  const latest = data?.latest;
  const releasesUrl = data?.releases_url || '';
  const canInstall = !!latest?.installable;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={t('updatePanel.dialogTitle')}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="flex max-h-[85vh] w-full max-w-2xl flex-col overflow-hidden rounded-lg border bg-background shadow-xl">
        {/* 头部 */}
        <div className="flex items-center justify-between gap-2 border-b px-4 py-3">
          <div className="min-w-0">
            <h2 className="truncate text-base font-semibold">
              {t('updatePanel.dialogTitle')}
            </h2>
            <p className="truncate text-xs text-muted-foreground">
              {t('updatePanel.currentIs', {v: data?.version || '…'})}
              {' · '}
              {native ? t('updatePanel.modeDesktop') : t('updatePanel.modeReadonly')}
            </p>
          </div>
          <Button variant="ghost" size="sm" onClick={onClose}>
            {t('updatePanel.close')}
          </Button>
        </div>

        {/* 操作区 */}
        <div className="flex flex-wrap items-center gap-2 border-b px-4 py-2">
          <Button variant="outline" size="sm" onClick={() => void refresh()} disabled={loading}>
            {loading ? (
              <Loader2 className="mr-1 size-3.5 animate-spin" />
            ) : (
              <RefreshCw className="mr-1 size-3.5" />
            )}
            {t('updatePanel.recheck')}
          </Button>
          {releasesUrl && (
            <Button variant="outline" size="sm" asChild>
              <a href={releasesUrl} target="_blank" rel="noopener noreferrer">
                <ExternalLink className="mr-1 size-3.5" />
                {t('updatePanel.openReleases')}
              </a>
            </Button>
          )}
          {latest && canInstall && native && (
            <Button size="sm" onClick={onDownload} disabled={status.busy}>
              <Download className="mr-1 size-3.5" />
              {t('updatePanel.download')}
            </Button>
          )}
          {status.ready && native && (
            <Button size="sm" onClick={onInstall} disabled={status.busy}>
              <RotateCcw className="mr-1 size-3.5" />
              {t('updatePanel.install')}
            </Button>
          )}
          {latest && !canInstall && (
            <span className="text-xs text-muted-foreground">
              {t('updatePanel.installHint')}
            </span>
          )}
        </div>

        {/* 状态/错误 */}
        {(error || status.message) && (
          <div
            className={cn(
              'flex items-start gap-2 border-b px-4 py-2 text-xs',
              error
                ? 'bg-amber-500/10 text-amber-700 dark:text-amber-400'
                : 'bg-muted/40 text-muted-foreground',
            )}
          >
            {error ? (
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
            ) : (
              <Loader2
                className={cn('mt-0.5 size-3.5 shrink-0', status.busy && 'animate-spin')}
              />
            )}
            <span className="break-all">{error || status.message}</span>
          </div>
        )}

        {/* 新版本提示 */}
        {latest && !error && (
          <div className="flex items-center gap-2 border-b bg-emerald-500/10 px-4 py-2 text-xs text-emerald-700 dark:text-emerald-400">
            <CheckCircle2 className="size-3.5 shrink-0" />
            {t('updatePanel.foundNew')}{' '}
            <strong className="font-semibold">{latest.tag}</strong>
          </div>
        )}

        {/* 历史版本 */}
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          <h3 className="mb-2 text-sm font-semibold">{t('updatePanel.history')}</h3>
          {(data?.releases || []).length === 0 ? (
            <p className="text-xs text-muted-foreground">
              {loading ? t('updatePanel.loading') : t('updatePanel.emptyReleases')}
            </p>
          ) : (
            <div className="space-y-3">
              {(data?.releases || []).map((row: ReleaseRow) => (
                <section key={row.id} className="rounded-md border p-3">
                  <div className="mb-1 flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium">{row.name}</span>
                    <Badge variant="secondary" className="shrink-0">
                      {row.tag}
                    </Badge>
                    {data?.latest?.id === row.id && (
                      <Badge className="shrink-0">{t('updatePanel.latest')}</Badge>
                    )}
                    <span className="text-xs text-muted-foreground">{fmtDate(row.date)}</span>
                  </div>
                  {/* Release 正文来自 GitHub —— **纯文本**渲染，绝不解析 HTML */}
                  <pre className="max-h-56 overflow-y-auto whitespace-pre-wrap break-words font-sans text-xs text-muted-foreground">
                    {row.body}
                  </pre>
                  {releasesUrl && row.url?.startsWith(releasesUrl) && (
                    <a
                      href={row.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="mt-2 inline-flex items-center gap-1 text-xs text-primary hover:underline"
                    >
                      {t('updatePanel.viewRelease')}
                      <ExternalLink className="size-3" />
                    </a>
                  )}
                </section>
              ))}
            </div>
          )}
        </div>

        {/* 运行环境（仅桌面端） */}
        {native && (
          <div className="flex flex-wrap items-center gap-2 border-t px-4 py-2">
            <span className="text-xs text-muted-foreground">
              {runtime || t('updatePanel.runtimePreparing')}
            </span>
            <Button variant="ghost" size="sm" onClick={() => void retryRuntime()}>
              {t('updatePanel.retryRuntime')}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
