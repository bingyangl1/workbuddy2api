'use client';

/**
 * 逆向产物（旧面板 panel-settings 的「逆向产物」区块）。
 *
 * 用途：从本机安装的 WorkBuddy 客户端 `app.asar` 里解出源码，便于检索/排障。
 * 纯标准库解包（`wb_asar.py`），**不需要 Node/npm**。
 *
 * 生产环境不需要这功能——`ADMIN_DEV_TOOLS=0` 时后端 `/api/app-source/*` 全部 404，
 * 页面会显示为不可用（如实提示，不假装能用）。
 *
 * 对齐 routers/app_source.py：status / detect / locate / unpack(plan|progress) / remove / search。
 */
import {useCallback, useEffect, useState} from 'react';
import {appSourceApi, errText} from '@/lib/api';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Boxes, FolderSearch, Loader2, PackageOpen, Trash2} from 'lucide-react';

interface SourceStatus {
  available?: boolean;
  installed?: boolean;
  source_dir?: string;
  unpacked?: boolean;
  astro?: string;
  files?: number;
  [k: string]: unknown;
}

export default function AppSourcePage() {
  const t = useT();
  const [st, setSt] = useState<SourceStatus | null>(null);
  const [error, setError] = useState('');
  const [installDir, setInstallDir] = useState('');
  const [outDir, setOutDir] = useState('');
  const [busy, setBusy] = useState('');
  const [query, setQuery] = useState('');
  const [hits, setHits] = useState<{file: string; line: number; text: string}[] | null>(null);

  const load = useCallback(async () => {
    setError('');
    try {
      const res = await appSourceApi.status() as unknown as SourceStatus;
      setSt(res);
      setInstallDir(String(res.install_dir || ''));
      setOutDir(String(res.source_dir || ''));
    } catch (e) {
      setError(errText(e));
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const run = useCallback(async (tag: string, fn: () => Promise<unknown>, okMsg: string) => {
    setBusy(tag);
    try {
      const res = await fn();
      notify.ok(okMsg + (res ? '' : ''));
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }, [load]);

  const doSearch = useCallback(async () => {
    if (!query.trim()) return;
    setBusy('search');
    try {
      const res = await appSourceApi.search(query.trim()) as unknown as {items?: typeof hits};
      setHits((res.items as typeof hits) || []);
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }, [query]);

  if (error) return <LoadError message={error} onRetry={load} />;
  if (!st) return <SkeletonBar className="h-96" />;

  // 后端在 ADMIN_DEV_TOOLS=0 时整组 404 → 页面明确告知，而不是给一堆点不动的按钮
  const disabled = st.available === false;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t('appSource.title')}
        description={t('appSource.description')}
        actions={
          <Button size="sm" variant="outline" onClick={load} disabled={!!busy || disabled}>
            {busy === 'status' ? <Loader2 className="h-4 w-4 animate-spin" /> : <FolderSearch className="h-4 w-4" />}
            {t('appSource.rescan')}
          </Button>
        }
      />

      {disabled ? (
        <EmptyState title={t('appSource.disabled')} />
      ) : (
        <>
          <section className="flex flex-wrap items-center gap-3 rounded-xl border bg-card p-4">
            <Boxes className="h-5 w-5 text-muted-foreground" />
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="text-muted-foreground">{t('appSource.clientInstalled')}</span>
              <Badge variant={st.installed ? 'default' : 'outline'} className="text-[10px]">
                {st.installed ? t('appSource.yes') : t('appSource.no')}
              </Badge>
              <span className="ml-2 text-muted-foreground">{t('appSource.unpacked')}</span>
              <Badge variant={st.unpacked ? 'default' : 'outline'} className="text-[10px]">
                {st.unpacked ? t('appSource.yes') : t('appSource.no')}
              </Badge>
              {st.source_dir && (
                // break-all：Windows 路径是长且不含空格的串，不强制断行会撑破 375px 视口
                <span className="ml-2 break-all font-mono text-xs text-muted-foreground">{String(st.source_dir)}</span>
              )}
            </div>
          </section>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <section className="flex flex-col gap-3 rounded-xl border bg-card p-4">
              <h2 className="text-sm font-semibold">{t('appSource.installDir')}</h2>
              <Input value={installDir} onChange={(e) => setInstallDir(e.target.value)}
                     placeholder={t('appSource.installDirPlaceholder')} className="font-mono text-xs" />
              <p className="text-xs text-muted-foreground">{t('appSource.installDirHint')}</p>
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="outline" disabled={!!busy || !installDir.trim()}
                        onClick={() => run('locate', () => appSourceApi.locate(installDir.trim()),
                                           t('appSource.locateOk'))}>
                  {busy === 'locate' ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                  {t('appSource.locate')}
                </Button>
                <Button size="sm" variant="outline" disabled={!!busy}
                        onClick={() => run('detect', () => appSourceApi.detect(), t('appSource.detectOk'))}>
                  {busy === 'detect' ? <Loader2 className="h-4 w-4 animate-spin" /> : <FolderSearch className="h-4 w-4" />}
                  {t('appSource.detect')}
                </Button>
              </div>
            </section>

            <section className="flex flex-col gap-3 rounded-xl border bg-card p-4">
              <h2 className="text-sm font-semibold">{t('appSource.outDir')}</h2>
              <Input value={outDir} onChange={(e) => setOutDir(e.target.value)}
                     placeholder={t('appSource.outDirPlaceholder')} className="font-mono text-xs" />
              <p className="text-xs text-muted-foreground">{t('appSource.outDirHint')}</p>
              <div className="flex flex-wrap gap-2">
                <Button size="sm" disabled={!!busy}
                        onClick={() => run('plan', () => appSourceApi.unpackPlan(), t('appSource.planOk'))}>
                  {busy === 'plan' ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                  {t('appSource.preflight')}
                </Button>
                <Button size="sm" disabled={!!busy}
                        onClick={() => run('unpack', () => appSourceApi.unpack(outDir.trim()),
                                           t('appSource.unpackOk'))}>
                  {busy === 'unpack' ? <Loader2 className="h-4 w-4 animate-spin" /> : <PackageOpen className="h-4 w-4" />}
                  {t('appSource.unpack')}
                </Button>
                <Button size="sm" variant="outline" disabled={!!busy}
                        onClick={() => run('remove', () => appSourceApi.remove(), t('appSource.removeOk'))}>
                  {busy === 'remove' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                  {t('appSource.remove')}
                </Button>
              </div>
            </section>
          </div>

          <section className="flex flex-col gap-3 rounded-xl border bg-card p-4">
            <h2 className="text-sm font-semibold">{t('appSource.search')}</h2>
            <div className="flex gap-2">
              <Input value={query} onChange={(e) => setQuery(e.target.value)}
                     placeholder={t('appSource.searchPlaceholder')} className="font-mono text-xs"
                     onKeyDown={(e) => { if (e.key === 'Enter') doSearch(); }} />
              <Button size="sm" onClick={doSearch} disabled={!!busy || !query.trim()}>
                {busy === 'search' ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                {t('appSource.searchBtn')}
              </Button>
            </div>
            {hits && (
              hits.length === 0 ? (
                <p className="text-xs text-muted-foreground">{t('appSource.noHits')}</p>
              ) : (
                <div className="max-h-72 overflow-auto rounded border p-2 font-mono text-xs">
                  {hits.map((h, i) => (
                    <div key={i} className="flex gap-2 border-b py-1 last:border-0">
                      <span className="shrink-0 text-muted-foreground">{h.file}:{h.line}</span>
                      <span className="truncate">{h.text}</span>
                    </div>
                  ))}
                </div>
              )
            )}
          </section>
        </>
      )}
    </div>
  );
}
