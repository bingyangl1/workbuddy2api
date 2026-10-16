'use client';

/**
 * 成长计划（Wave B13）。
 *
 * 引擎自动做成长任务领积分（对齐 routers/growth.py）：任务列表分级（可自动/
 * 需人工）+ 单账号任务完成情况弹窗 + 批量做任务（run-async 异步 + job 轮询进度，
 * 单账号约 27s/账号×N，同步会撞 nginx 60s）+ 领奖。
 * 安全口径：整组 require_admin（历史曾无鉴权泄露手机号，已修）。
 */
import {useCallback, useEffect, useRef, useState} from 'react';
import {growthApi, accountApi, errText} from '@/lib/api';
import type {Account, GrowthTask, GrowthAccountTasks, GrowthJobSnapshot} from '@/lib/types';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Progress} from '@/components/ui/progress';
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {
  Tooltip, TooltipContent, TooltipProvider, TooltipTrigger,
} from '@/components/ui/tooltip';
import {Gift, Loader2, Play, RefreshCw, Sparkles} from 'lucide-react';

function statusBadge(s: string | undefined): {label: string; cls: string} {
  switch (s) {
    case 'claimed': return {label: 'claimed', cls: 'border-emerald-300 text-emerald-600'};
    case 'completed': return {label: 'completed', cls: 'border-sky-300 text-sky-600'};
    case 'in_progress': return {label: 'in_progress', cls: 'border-amber-300 text-amber-600'};
    default: return {label: s || '—', cls: 'border-muted text-muted-foreground'};
  }
}

export default function GrowthPage() {
  const t = useT();
  const [tasks, setTasks] = useState<GrowthTask[] | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [error, setError] = useState('');
  const [syncing, setSyncing] = useState(false);
  const [acctView, setAcctView] = useState<number | null>(null);
  const [job, setJob] = useState<GrowthJobSnapshot | null>(null);
  const [claiming, setClaiming] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    setError('');
    try {
      const [tRes, aRes] = await Promise.all([growthApi.tasks(), accountApi.list()]);
      setTasks(tRes.tasks);
      setAccounts(aRes.items.filter((a) => a.status === 'active'));
    } catch (e) { setError(errText(e)); }
  }, []);

  useEffect(() => { load(); return stopPoll; }, [load]);

  function stopPoll() {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }

  async function refreshTasks() {
    setSyncing(true);
    try {
      const r = await growthApi.tasks(true);
      setTasks(r.tasks);
      notify.ok(t('growth.synced'));
    } catch (e) { notify.err(errText(e)); }
    finally { setSyncing(false); }
  }

  // 批量做任务（异步 + 轮询）
  async function runAll() {
    try {
      const j = await growthApi.runAsync([]);
      setJob(j);
      if (j.status === 'running' || j.reused) startPoll(j.job_id);
      else { notify.ok(t('growth.runDone')); load(); }
    } catch (e) { notify.err(errText(e)); }
  }

  function startPoll(jobId: string) {
    stopPoll();
    pollRef.current = setInterval(async () => {
      try {
        const j = await growthApi.job(jobId);
        setJob(j);
        if (j.status !== 'running') {
          stopPoll();
          notify.ok(t('growth.runDone'));
          load();
        }
      } catch { stopPoll(); }
    }, 2500);
  }

  // 领奖（全部可领账号）
  async function claimAll() {
    setClaiming(true);
    try {
      const ids = accounts.map((a) => a.id);
      const r = await growthApi.claim(ids, null);
      const totalCredit = r.results.reduce((s, x) => s + (x.credit || 0), 0);
      notify.ok(t('growth.claimed', {credit: totalCredit}));
      load();
    } catch (e) { notify.err(errText(e)); }
    finally { setClaiming(false); }
  }

  if (error) return <LoadError message={error} onRetry={load} />;
  if (!tasks) return <SkeletonBar className="h-96" />;

  const running = job?.status === 'running';
  const autoTasks = tasks.filter((x) => x.actionable);
  const progress = job && job.total ? ((job.done || 0) / job.total) * 100 : 0;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t('growth.title')}
        description={t('growth.subtitle', {n: tasks.length, auto: autoTasks.length})}
        actions={
          <div className="flex gap-2">
            <Button size="sm" variant="outline" onClick={refreshTasks} disabled={syncing}>
              {syncing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              {t('growth.refresh')}
            </Button>
            <Button size="sm" variant="outline" onClick={claimAll} disabled={claiming || running}>
              {claiming ? <Loader2 className="h-4 w-4 animate-spin" /> : <Gift className="h-4 w-4" />}
              {t('growth.claimAll')}
            </Button>
            <Button size="sm" onClick={runAll} disabled={running}>
              {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              {t('growth.runAll')}
            </Button>
          </div>
        }
      />

      {/* 批量进度 */}
      {running && job && (
        <div className="rounded-xl border bg-card p-4">
          <div className="flex items-center justify-between text-xs">
            <span className="font-medium">{t('growth.running', {phase: job.phase || ''})}</span>
            <span className="text-muted-foreground">{job.done || 0}/{job.total || 0}</span>
          </div>
          <Progress value={progress} className="mt-2 h-2" />
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* 任务定义（分级） */}
        <section className="rounded-xl border bg-card">
          <div className="border-b p-3 text-sm font-semibold">{t('growth.tasksTitle')}</div>
          {tasks.length === 0 ? (
            <EmptyState title={t('growth.noTasks')} />
          ) : (
            <div className="max-h-[480px] overflow-y-auto overflow-x-auto">
          <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t('growth.colTask')}</TableHead>
                    <TableHead>{t('growth.colLevel')}</TableHead>
                    <TableHead className="text-right">{t('growth.colReward')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {tasks.map((x) => (
                    <TableRow key={x.task_code}>
                      <TableCell>
                        <div className="flex flex-col">
                          <span className="text-xs font-medium">{x.title || x.task_code}</span>
                          <span className="font-mono text-[10px] text-muted-foreground">{x.task_code}</span>
                        </div>
                      </TableCell>
                      <TableCell>
                        {x.actionable ? (
                          <Badge variant="outline" className="border-emerald-300 text-[10px] text-emerald-600">
                            <Sparkles className="mr-1 h-3 w-3" />{t('growth.auto')}
                          </Badge>
                        ) : (
                          <TooltipProvider delayDuration={200}>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Badge variant="outline" className="text-[10px] text-muted-foreground">
                                  {t('growth.manual')}
                                </Badge>
                              </TooltipTrigger>
                              <TooltipContent className="max-w-xs text-xs">{x.reason || t('growth.manualReason')}</TooltipContent>
                            </Tooltip>
                          </TooltipProvider>
                        )}
                      </TableCell>
                      <TableCell className="text-right text-xs">
                        {x.reward_credit ? `+${x.reward_credit}` : '—'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </section>

        {/* 账号（点开看单账号完成情况） */}
        <section className="rounded-xl border bg-card">
          <div className="border-b p-3 text-sm font-semibold">{t('growth.accountsTitle')}</div>
          {accounts.length === 0 ? (
            <EmptyState title={t('growth.noAccounts')} />
          ) : (
            <div className="max-h-[480px] divide-y overflow-y-auto">
              {accounts.map((a) => (
                <button key={a.id} onClick={() => setAcctView(a.id)}
                        className="flex w-full items-center justify-between px-3 py-2.5 text-left hover:bg-muted/50">
                  <div>
                    <div className="text-xs font-medium">{a.name}</div>
                    <div className="text-[10px] text-muted-foreground">{a.realm} · {a.uid || `#${a.id}`}</div>
                  </div>
                  <span className="text-[11px] text-muted-foreground">{t('growth.viewTasks')} →</span>
                </button>
              ))}
            </div>
          )}
        </section>
      </div>

      <AccountTasksDialog accountId={acctView} onClose={() => setAcctView(null)}
                          onChanged={load} />
    </div>
  );
}

/* ── 单账号任务弹窗（完成情况 + 可领奖励）────────────────────────────── */
function AccountTasksDialog({accountId, onClose, onChanged}: {
  accountId: number | null; onClose: () => void; onChanged: () => void;
}) {
  const t = useT();
  const [data, setData] = useState<GrowthAccountTasks | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (refresh = false) => {
    if (accountId == null) return;
    try {
      const r = await growthApi.accountTasks(accountId, refresh);
      setData(r);
    } catch (e) { notify.err(errText(e)); }
  }, [accountId]);

  useEffect(() => { setData(null); load(); }, [load]);

  async function runOne() {
    if (accountId == null) return;
    setBusy(true);
    try {
      await growthApi.run([accountId], null);
      await load(true);
      onChanged();
      notify.ok(t('growth.runDone'));
    } catch (e) { notify.err(errText(e)); }
    finally { setBusy(false); }
  }

  async function claimOne() {
    if (accountId == null) return;
    setBusy(true);
    try {
      const r = await growthApi.claim([accountId], null);
      const credit = r.results.reduce((s, x) => s + (x.credit || 0), 0);
      notify.ok(t('growth.claimed', {credit}));
      await load(true);
      onChanged();
    } catch (e) { notify.err(errText(e)); }
    finally { setBusy(false); }
  }

  return (
    <Dialog open={accountId != null} onOpenChange={(v: boolean) => !v && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t('growth.acctTitle', {name: data?.name || `#${accountId}`})}</DialogTitle>
        </DialogHeader>
        {!data ? (
          <SkeletonBar className="h-64" />
        ) : (
          <div className="flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <div className="flex gap-3 text-xs text-muted-foreground">
                <span>{t('growth.sumDone', {done: data.summary.done, total: data.summary.total})}</span>
                {data.summary.claimable > 0 && (
                  <span className="text-emerald-600">
                    {t('growth.sumClaimable', {n: data.summary.claimable, credit: data.summary.claimable_credit})}
                  </span>
                )}
              </div>
              <div className="flex gap-2">
                <Button size="sm" variant="outline" onClick={claimOne}
                        disabled={busy || data.summary.claimable === 0}>
                  <Gift className="h-4 w-4" />{t('growth.claim')}
                </Button>
                <Button size="sm" onClick={runOne} disabled={busy}>
                  {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                  {t('growth.run')}
                </Button>
              </div>
            </div>
            <div className="max-h-[50vh] overflow-y-auto rounded-md border overflow-x-auto">
          <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t('growth.colTask')}</TableHead>
                    <TableHead>{t('growth.colStatus')}</TableHead>
                    <TableHead>{t('growth.colProgress')}</TableHead>
                    <TableHead className="text-right">{t('growth.colReward')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.tasks.map((x) => {
                    const sb = statusBadge(x.accept_status);
                    return (
                      <TableRow key={x.task_code} className={cn(!x.actionable && 'opacity-55')}>
                        <TableCell>
                          <div className="flex flex-col">
                            <span className="text-xs">{x.title || x.task_code}</span>
                            <span className="font-mono text-[9px] text-muted-foreground">{x.task_code}</span>
                          </div>
                        </TableCell>
                        <TableCell>
                          <Badge variant="outline" className={cn('text-[10px]', sb.cls)}>{sb.label}</Badge>
                        </TableCell>
                        <TableCell className="text-[11px] text-muted-foreground">
                          {x.progress?.target ? `${x.progress.current || 0}/${x.progress.target}` : '—'}
                        </TableCell>
                        <TableCell className="text-right text-xs">
                          {x.reward_credit ? `+${x.reward_credit}` : '—'}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
