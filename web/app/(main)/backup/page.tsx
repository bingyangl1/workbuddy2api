'use client';

/**
 * 数据备份（P5）：本地快照导出 / 下载 / 恢复。
 *
 * 后端 routers/backup.py：导出全 15 张业务表为 JSON 快照；恢复是**覆盖式**高危操作，
 * 后端会自动留底（pre-restore-*.json）并在审计里记 backup.restore。
 *
 * 安全提示（页面上明说，不给用户惊喜）：
 *  - 含密快照包含账号凭据（auth_json），文件本身即等价于账号池——务必妥善保管。
 *  - 脱敏快照用于结构核对/评审，不能用于恢复完整功能。
 */
import {useCallback, useEffect, useState} from 'react';
import {backupApi, errText, readToken} from '@/lib/api';
import type {BackupFile, BackupPreview, BackupRestoreResult} from '@/lib/types';
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
import {Switch} from '@/components/ui/switch';
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table';
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import {Download, HardDriveDownload, Loader2, RefreshCw, RotateCcw, ShieldAlert} from 'lucide-react';

export default function BackupPage() {
  const t = useT();
  const [files, setFiles] = useState<BackupFile[] | null>(null);
  const [error, setError] = useState('');
  const [name, setName] = useState('');
  const [includeSecrets, setIncludeSecrets] = useState(true);
  const [busy, setBusy] = useState('');
  const [preview, setPreview] = useState<BackupPreview | null>(null);
  const [restoreTarget, setRestoreTarget] = useState<string | null>(null);
  const [lastRestore, setLastRestore] = useState<BackupRestoreResult | null>(null);

  const load = useCallback(async () => {
    setError('');
    try {
      const res = await backupApi.list();
      setFiles(res.items);
    } catch (e) {
      setError(errText(e));
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const doExport = useCallback(async () => {
    setBusy('export');
    try {
      const res = await backupApi.export(name.trim(), includeSecrets);
      notify.ok(t('backup.exportOk', {name: res.name, n: res.size}));
      setName('');
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }, [name, includeSecrets, load, t]);

  const doPreview = useCallback(async (n: string) => {
    setBusy('preview');
    try {
      setPreview(await backupApi.preview(n));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }, []);

  const doRestore = useCallback(async () => {
    const target = restoreTarget;
    if (!target) return;
    setBusy('restore');
    try {
      const res = await backupApi.restore(target);
      setLastRestore(res);
      notify.ok(t('backup.restoreOk', {name: res.safeguard}));
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy('');
      setRestoreTarget(null);
    }
  }, [restoreTarget, load, t]);

  const download = useCallback((n: string) => {
    // 下载走原生导航（带鉴权头需要 fetch+blob）
    fetch(backupApi.downloadUrl(n), {
      headers: {'X-Admin-Token': readToken() || ''},
    }).then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = n;
      a.click();
      URL.revokeObjectURL(url);
    }).catch((e) => notify.err(errText(e)));
  }, []);

  const fmtSize = (n: number) => n > 1048576 ? `${(n / 1048576).toFixed(2)} MB` : `${(n / 1024).toFixed(1)} KB`;

  if (error) return <LoadError message={error} onRetry={load} />;
  if (!files) return <SkeletonBar className="h-96" />;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t('backup.title')}
        description={t('backup.description')}
        actions={
          <Button size="sm" variant="outline" onClick={load} disabled={!!busy}>
            <RefreshCw className="h-4 w-4" />{t('backup.refresh')}
          </Button>
        }
      />

      {/* 导出表单 */}
      <section className="rounded-xl border bg-card p-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-[180px] flex-1">
            <Label htmlFor="bk-name">{t('backup.fileName')}</Label>
            <Input id="bk-name" value={name} onChange={(e) => setName(e.target.value)}
                   placeholder={t('backup.fileNamePlaceholder')} className="mt-1.5" />
          </div>
          <div className="flex items-center gap-2 pb-2">
            <Switch checked={includeSecrets} onCheckedChange={setIncludeSecrets} id="bk-secrets" />
            <Label htmlFor="bk-secrets" className="cursor-pointer">
              {t('backup.includeSecrets')}
            </Label>
          </div>
          <Button onClick={doExport} disabled={!!busy}>
            {busy === 'export' ? <Loader2 className="h-4 w-4 animate-spin" /> : <HardDriveDownload className="h-4 w-4" />}
            {t('backup.export')}
          </Button>
        </div>
        <p className={`mt-2 flex items-start gap-1.5 text-xs ${includeSecrets ? 'text-amber-600 dark:text-amber-400' : 'text-muted-foreground'}`}>
          <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          {includeSecrets ? t('backup.secretsWarn') : t('backup.redactedHint')}
        </p>
      </section>

      {lastRestore && (
        <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-xs">
          {t('backup.restoreBanner', {
            n: Object.values(lastRestore.restored).reduce((a, b) => a + b, 0),
            safeguard: lastRestore.safeguard,
          })}
        </div>
      )}

      {/* 文件列表 */}
      {files.length === 0 ? (
        <EmptyState title={t('backup.noFiles')} />
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t('backup.colName')}</TableHead>
                <TableHead className="text-right">{t('backup.colSize')}</TableHead>
                <TableHead>{t('backup.colTime')}</TableHead>
                <TableHead>{t('backup.colKind')}</TableHead>
                <TableHead className="text-right">{t('backup.colActions')}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {files.map((f) => (
                <TableRow key={f.name}>
                  <TableCell className="font-mono text-xs">{f.name}</TableCell>
                  <TableCell className="text-right tabular-nums text-xs">{fmtSize(f.size)}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">{f.mtime.replace('T', ' ').slice(0, 19)}</TableCell>
                  <TableCell>
                    {f.name.startsWith('pre-restore-')
                      ? <Badge variant="outline" className="text-[10px]">{t('backup.kindAuto')}</Badge>
                      : <Badge variant="secondary" className="text-[10px]">{t('backup.kindManual')}</Badge>}
                  </TableCell>
                  <TableCell>
                    <div className="flex justify-end gap-1.5">
                      <Button size="sm" variant="ghost" onClick={() => doPreview(f.name)} disabled={!!busy}>
                        {t('backup.preview')}
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => download(f.name)}>
                        <Download className="h-3.5 w-3.5" />
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => setRestoreTarget(f.name)} disabled={!!busy}>
                        <RotateCcw className="h-3.5 w-3.5" />{t('backup.restore')}
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {/* 预览弹窗（元信息 + 每表行数） */}
      <AlertDialog open={!!preview} onOpenChange={(o) => !o && setPreview(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{preview?.name}</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="flex flex-col gap-2 text-sm">
                <div className="text-xs text-muted-foreground">
                  {preview?.generated_at} · {t('backup.format')} v{preview?.format} ·{' '}
                  {preview?.include_secrets ? t('backup.withSecrets') : t('backup.redacted')}
                </div>
                <div className="max-h-64 overflow-y-auto rounded border p-2 font-mono text-xs">
                  {preview && Object.entries(preview.tables).map(([k, v]) => (
                    <div key={k} className="flex justify-between">
                      <span>{k}</span><span className="tabular-nums">{v}</span>
                    </div>
                  ))}
                </div>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('backup.close')}</AlertDialogCancel>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* 恢复二次确认 */}
      <AlertDialog open={!!restoreTarget} onOpenChange={(o) => !o && setRestoreTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('backup.restoreTitle')}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('backup.restoreDesc', {name: restoreTarget || ''})}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('backup.cancel')}</AlertDialogCancel>
            <AlertDialogAction onClick={doRestore} disabled={busy === 'restore'}>
              {busy === 'restore' ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
              {t('backup.restoreConfirm')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
