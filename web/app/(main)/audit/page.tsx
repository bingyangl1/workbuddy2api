'use client';

/**
 * 操作审计（Wave B8，P2 T2 欠账入面板）。
 *
 * 管理端敏感操作的留痕（audit_logs：谁/何时/做了什么/对象/细节/来源IP）。
 * 对齐 routers/audit.py：id 倒序 + action 过滤 + offset 翻页（无 keyset，
 * 审计是排障入口不是高频流）+ 清空（清空本身会留痕一条 clear_audit）。
 */
import {useCallback, useEffect, useMemo, useState} from 'react';
import {auditApi, errText} from '@/lib/api';
import type {AuditLog} from '@/lib/types';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table';
import {
  Tooltip, TooltipContent, TooltipProvider, TooltipTrigger,
} from '@/components/ui/tooltip';
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import {ChevronLeft, ChevronRight, Trash2} from 'lucide-react';

const LIMIT = 50;

function fmtIso(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export default function AuditPage() {
  const t = useT();
  const [items, setItems] = useState<AuditLog[] | null>(null);
  const [error, setError] = useState('');
  const [action, setAction] = useState('all');
  const [offset, setOffset] = useState(0);
  const [clearConfirm, setClearConfirm] = useState(false);

  const load = useCallback(async (off: number) => {
    setError('');
    try {
      const p: Record<string, unknown> = {limit: LIMIT, offset: off};
      if (action !== 'all') p.action = action;
      const r = await auditApi.list(p as {limit?: number; offset?: number; action?: string});
      setItems(r.items);
    } catch (e) { setError(errText(e)); }
  }, [action]);

  useEffect(() => { setOffset(0); load(0); }, [load]);

  // action 下拉的可选项：从已加载的条目里收集（避免硬编码动作名清单）
  const actionOptions = useMemo(() => {
    const s = new Set<string>();
    (items || []).forEach((i) => i.action && s.add(i.action));
    return Array.from(s).sort();
  }, [items]);

  function goNext() { const n = offset + LIMIT; setOffset(n); load(n); }
  function goPrev() { const n = Math.max(0, offset - LIMIT); setOffset(n); load(n); }

  async function doClear() {
    try {
      await auditApi.clear();
      setItems([]);
      setClearConfirm(false);
      notify.ok(t('audit.cleared'));
      load(0);
    } catch (e) { notify.err(errText(e)); }
  }

  if (error) return <LoadError message={error} onRetry={() => load(offset)} />;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader title={t('audit.title')} description={t('audit.subtitle')} />

      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">{t('audit.fAction')}</span>
          <Select value={action} onValueChange={setAction}>
            <SelectTrigger className="w-44"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t('audit.allActions')}</SelectItem>
              {actionOptions.map((a) => <SelectItem key={a} value={a}>{a}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
        <Button size="sm" variant="outline" onClick={() => setClearConfirm(true)}
                disabled={!items || items.length === 0}>
          <Trash2 className="h-4 w-4" />{t('audit.clear')}
        </Button>
      </div>

      {!items ? (
        <SkeletonBar className="h-96" />
      ) : items.length === 0 ? (
        <EmptyState title={t('audit.empty')} />
      ) : (
        <div className="rounded-xl border bg-card overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t('audit.colTime')}</TableHead>
                <TableHead>{t('audit.colActor')}</TableHead>
                <TableHead>{t('audit.colAction')}</TableHead>
                <TableHead>{t('audit.colTarget')}</TableHead>
                <TableHead>{t('audit.colDetail')}</TableHead>
                <TableHead>{t('audit.colIp')}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((r) => (
                <TableRow key={r.id}>
                  <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{fmtIso(r.created_at)}</TableCell>
                  <TableCell className="text-xs">{r.actor}</TableCell>
                  <TableCell>
                    <Badge variant="secondary" className="font-mono text-[10px]">{r.action}</Badge>
                  </TableCell>
                  <TableCell className="max-w-[140px] truncate text-xs">{r.target || '—'}</TableCell>
                  <TableCell className="max-w-[280px]">
                    {r.detail ? (
                      <TooltipProvider delayDuration={200}>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <span className="block truncate text-xs text-muted-foreground">{r.detail}</span>
                          </TooltipTrigger>
                          <TooltipContent className="max-w-md whitespace-pre-wrap text-xs">{r.detail}</TooltipContent>
                        </Tooltip>
                      </TooltipProvider>
                    ) : <span className="text-xs text-muted-foreground">—</span>}
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">{r.ip || '—'}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {items && items.length > 0 && (
        <div className="flex items-center justify-between">
          <span className="text-xs text-muted-foreground">
            {t('audit.pageInfo', {from: offset + 1, to: offset + items.length})}
          </span>
          <div className="flex gap-1">
            <Button variant="outline" size="sm" onClick={goPrev} disabled={offset === 0}>
              <ChevronLeft className="h-4 w-4" />{t('audit.prev')}
            </Button>
            <Button variant="outline" size="sm" onClick={goNext} disabled={items.length < LIMIT}>
              {t('audit.next')}<ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}

      <AlertDialog open={clearConfirm} onOpenChange={(v: boolean) => !v && setClearConfirm(false)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('audit.clearTitle')}</AlertDialogTitle>
            <AlertDialogDescription>{t('audit.clearConfirm')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction className="bg-destructive text-white hover:bg-destructive/90"
                               onClick={(e) => { e.preventDefault(); doClear(); }}>
              {t('audit.clear')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
