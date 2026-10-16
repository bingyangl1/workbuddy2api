'use client';

/**
 * 定时任务（Wave B12）。
 *
 * 引擎内置任务（refresh_balances/refresh_credits/sync_models/daily_checkin/
 * refresh_growth_tasks/run_growth_tasks/keepalive_tokens）的调度管理。
 * 对齐 routers/schedules.py：列表含 task_label/due_soon；last_result 是
 * _dump_result 写的 JSON 串（截断安全），前端解析展示；立即运行复用同一
 * last_result 口径。
 */
import {useCallback, useEffect, useState} from 'react';
import {scheduleApi, errText} from '@/lib/api';
import type {Schedule} from '@/lib/types';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from '@/components/animate-ui/radix/dialog';
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import {
  Tooltip, TooltipContent, TooltipProvider, TooltipTrigger,
} from '@/components/ui/tooltip';
import {Loader2, Play, Plus, Trash2} from 'lucide-react';

const TASK_KEYS = [
  'refresh_balances', 'refresh_credits', 'sync_models', 'daily_checkin',
  'refresh_growth_tasks', 'run_growth_tasks', 'keepalive_tokens',
] as const;

function fmtIso(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** last_result 是 _dump_result 的 JSON 串；解析出 ok/error/摘要 */
function parseResult(raw: string): {ok: boolean; text: string} {
  if (!raw) return {ok: true, text: ''};
  try {
    const o = JSON.parse(raw);
    if (o && typeof o === 'object') {
      const ok = o.ok !== false;
      let text = '';
      if (o.error) text = String(o.error);
      else {
        const parts: string[] = [];
        for (const k of ['updated', 'synced', 'added', 'count', 'refreshed', 'done', 'total']) {
          if (o[k] != null) parts.push(`${k}=${o[k]}`);
        }
        text = parts.length ? parts.join(' ') : 'ok';
      }
      return {ok, text};
    }
  } catch { /* 半截 JSON 兜底 */ }
  return {ok: true, text: raw.slice(0, 40)};
}

export default function SchedulesPage() {
  const t = useT();
  const [items, setItems] = useState<Schedule[] | null>(null);
  const [error, setError] = useState('');
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Schedule | null>(null);
  const [runningId, setRunningId] = useState<number | null>(null);

  const load = useCallback(async () => {
    setError('');
    try {
      const r = await scheduleApi.list();
      setItems(r.items);
    } catch (e) { setError(errText(e)); }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function toggle(s: Schedule) {
    try {
      await scheduleApi.toggle(s.id);
      load();
    } catch (e) { notify.err(errText(e)); }
  }

  async function runNow(s: Schedule) {
    setRunningId(s.id);
    try {
      const r = await scheduleApi.runNow(s.id);
      if (r.result?.ok === false) notify.err(r.result.error || t('schedules.runFailed'));
      else notify.ok(t('schedules.runOk'));
      load();
    } catch (e) { notify.err(errText(e)); }
    finally { setRunningId(null); }
  }

  async function doDelete() {
    if (!deleteTarget) return;
    try {
      await scheduleApi.remove(deleteTarget.id);
      setItems((prev) => prev && prev.filter((x) => x.id !== deleteTarget.id));
      setDeleteTarget(null);
      notify.ok(t('schedules.deleted'));
    } catch (e) { notify.err(errText(e)); }
  }

  if (error) return <LoadError message={error} onRetry={load} />;
  if (!items) return <SkeletonBar className="h-96" />;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t('schedules.title')}
        description={t('schedules.subtitle', {n: items.length})}
        actions={
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4" />{t('schedules.add')}
          </Button>
        }
      />

      {items.length === 0 ? (
        <EmptyState title={t('schedules.empty')} />
      ) : (
        <div className="rounded-xl border bg-card overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t('schedules.colName')}</TableHead>
                <TableHead>{t('schedules.colInterval')}</TableHead>
                <TableHead>{t('schedules.colLastRun')}</TableHead>
                <TableHead>{t('schedules.colNextRun')}</TableHead>
                <TableHead>{t('schedules.colResult')}</TableHead>
                <TableHead>{t('schedules.colEnabled')}</TableHead>
                <TableHead className="text-right">{t('schedules.colActions')}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((s) => {
                const res = parseResult(s.last_result);
                return (
                  <TableRow key={s.id} className={cn(!s.enabled && 'opacity-55')}>
                    <TableCell>
                      <div className="flex flex-col">
                        <span className="text-xs font-medium">{s.name || s.task_label}</span>
                        <span className="font-mono text-[10px] text-muted-foreground">{s.task}</span>
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge variant="secondary" className="text-[10px]">
                        {t('schedules.everyMin', {n: s.interval_minutes})}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">{fmtIso(s.last_run_at)}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {s.enabled ? (
                        <span className={cn(s.due_soon && 'font-medium text-sky-600')}>
                          {fmtIso(s.next_run_at)}
                          {s.due_soon && <span className="ml-1 text-[9px]">●</span>}
                        </span>
                      ) : '—'}
                    </TableCell>
                    <TableCell className="max-w-[180px]">
                      {res.text ? (
                        <TooltipProvider delayDuration={200}>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span className={cn('block truncate text-[11px]',
                                res.ok ? 'text-muted-foreground' : 'text-red-500')}>
                                {res.text}
                              </span>
                            </TooltipTrigger>
                            <TooltipContent className="max-w-sm whitespace-pre-wrap text-xs">{res.text}</TooltipContent>
                          </Tooltip>
                        </TooltipProvider>
                      ) : <span className="text-[11px] text-muted-foreground">—</span>}
                    </TableCell>
                    <TableCell>
                      <Switch checked={s.enabled} onCheckedChange={() => toggle(s)} />
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-1">
                        <Button variant="ghost" size="sm" title={t('schedules.runNow')}
                                onClick={() => runNow(s)} disabled={runningId === s.id}>
                          {runningId === s.id
                            ? <Loader2 className="h-4 w-4 animate-spin" />
                            : <Play className="h-4 w-4" />}
                        </Button>
                        <Button variant="ghost" size="sm" onClick={() => setDeleteTarget(s)}>
                          <Trash2 className="h-4 w-4 text-red-500" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}

      <CreateScheduleDialog open={createOpen} onClose={() => setCreateOpen(false)}
                            onCreated={() => { setCreateOpen(false); load(); }} />

      <AlertDialog open={!!deleteTarget} onOpenChange={(v: boolean) => !v && setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('schedules.deleteTitle')}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('schedules.deleteConfirm', {name: deleteTarget?.name || deleteTarget?.task_label || ''})}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction className="bg-destructive text-white hover:bg-destructive/90"
                               onClick={(e) => { e.preventDefault(); doDelete(); }}>
              {t('schedules.delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

/* ── 新建任务 ─────────────────────────────────────────────────────── */
function CreateScheduleDialog({open, onClose, onCreated}: {
  open: boolean; onClose: () => void; onCreated: () => void;
}) {
  const t = useT();
  const [task, setTask] = useState<string>('refresh_balances');
  const [name, setName] = useState('');
  const [interval, setIntervalMin] = useState('60');
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) { setTask('refresh_balances'); setName(''); setIntervalMin('60'); } }, [open]);

  async function submit() {
    setBusy(true);
    try {
      await scheduleApi.create({
        task, name: name.trim(), interval_minutes: Math.max(1, Number(interval) || 60),
        enabled: 1 as unknown as boolean, // 引擎 ScheduleIn.enabled 是 int
      });
      notify.ok(t('schedules.created'));
      onCreated();
    } catch (e) { notify.err(errText(e)); }
    finally { setBusy(false); }
  }

  return (
    <Dialog open={open} onOpenChange={(v: boolean) => !v && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>{t('schedules.addTitle')}</DialogTitle></DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="space-y-1.5">
            <Label>{t('schedules.fTask')}</Label>
            <Select value={task} onValueChange={setTask}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                {TASK_KEYS.map((k) => (
                  <SelectItem key={k} value={k}>{t(`schedules.task.${k}`)}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>{t('schedules.fName')}</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)}
                   placeholder={t('schedules.fNamePh')} />
          </div>
          <div className="space-y-1.5">
            <Label>{t('schedules.fInterval')}</Label>
            <Input type="number" min={1} value={interval} onChange={(e) => setIntervalMin(e.target.value)} />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel')}</Button>
          <Button onClick={submit} disabled={busy}>
            {busy ? t('common.submitting') : t('schedules.createSubmit')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
