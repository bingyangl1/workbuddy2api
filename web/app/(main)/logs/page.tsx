'use client';

/**
 * 用量日志（Wave B5）。
 *
 * 本引擎日志是「网关真实记账」：每个代理请求写一行（usage_logs），含
 * 积分/token/缓存token/TTFB/总耗时/error_kind/客户端IP/requested_model（别名原始名）。
 * 性能设计对齐 routers/logs.py：服务端分页 + keyset 深翻页游标（before_id）+
 * 按账号汇总 SQL 侧聚合。导出 CSV（UTF-8 BOM，北京时间）。
 */
import {useCallback, useEffect, useMemo, useState} from 'react';
import {logApi, errText} from '@/lib/api';
import type {UsageLog, UsageLogPage, LogSummary, LogAccountOption} from '@/lib/types';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';
import {fmtNumber} from '@/lib/format';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Tabs, TabsList, TabsTrigger, TabsContent} from '@/components/ui/tabs';
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table';
import {
  Tooltip, TooltipContent, TooltipProvider, TooltipTrigger,
} from '@/components/ui/tooltip';
import {ChevronLeft, ChevronRight, Download, ListFilter, ScrollText, Users} from 'lucide-react';

function fmtIso(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

const PAGE_SIZE = 20;

export default function LogsPage() {
  const t = useT();
  const [error, setError] = useState('');
  // 明细
  const [page, setPage] = useState<UsageLogPage | null>(null);
  const [pageNo, setPageNo] = useState(1);
  const [stack, setStack] = useState<(number | null)[]>([null]); // keyset 游标栈
  // 过滤
  const [model, setModel] = useState('');
  const [keyName, setKeyName] = useState('');
  const [accName, setAccName] = useState('');
  const [clientIp, setClientIp] = useState('');
  const [accountId, setAccountId] = useState('0');
  const [accOptions, setAccOptions] = useState<LogAccountOption[]>([]);
  // 汇总
  const [days, setDays] = useState('1');
  const [summary, setSummary] = useState<LogSummary | null>(null);

  const params = useMemo(() => {
    const p: Record<string, unknown> = {page_size: PAGE_SIZE};
    if (model.trim()) p.model = model.trim();
    if (keyName.trim()) p.key = keyName.trim();
    if (accName.trim() && accountId === '0') p.account = accName.trim();
    if (clientIp.trim()) p.client_ip = clientIp.trim();
    if (accountId !== '0') p.account_id = Number(accountId);
    return p;
  }, [model, keyName, accName, clientIp, accountId]);

  const loadLogs = useCallback(async (pg: number, beforeId: number | null) => {
    setError('');
    try {
      const p: Record<string, unknown> = {...params};
      if (beforeId != null) p.before_id = beforeId; else p.page = pg;
      const r = await logApi.list(p);
      setPage(r);
    } catch (e) { setError(errText(e)); }
  }, [params]);

  const loadSummary = useCallback(async () => {
    try {
      const r = await logApi.summary({days: Number(days) || 1});
      setSummary(r);
    } catch (e) { notify.err(errText(e)); }
  }, [days]);

  useEffect(() => { loadLogs(1, null); setPageNo(1); setStack([null]); }, [loadLogs]);
  useEffect(() => { loadSummary(); }, [loadSummary]);
  useEffect(() => {
    logApi.accounts(30).then((r) => setAccOptions(r.items)).catch(() => {});
  }, []);

  function goNext() {
    if (!page?.has_more) return;
    const bid = page.next_before_id;
    if (bid == null) { setPageNo((n) => n + 1); loadLogs(pageNo + 1, null); return; }
    setStack((s) => [...s, bid]);
    setPageNo((n) => n + 1);
    loadLogs(pageNo + 1, bid);
  }
  function goPrev() {
    if (stack.length <= 1) return;
    const s = [...stack];
    s.pop();
    const bid = s[s.length - 1];
    setStack(s);
    setPageNo((n) => Math.max(1, n - 1));
    loadLogs(pageNo - 1, bid);
  }

  function exportCsv() {
    const url = logApi.exportUrl({...params, limit: 50000});
    const a = document.createElement('a');
    a.href = url;
    a.download = 'usage_logs.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
    notify.ok(t('logs.exportStarted'));
  }

  if (error) return <LoadError message={error} onRetry={() => loadLogs(pageNo, stack[stack.length - 1])} />;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t('logs.title')}
        description={t('logs.subtitle', {n: page?.total ?? '—'})}
        actions={
          <Button size="sm" variant="outline" onClick={exportCsv}>
            <Download className="h-4 w-4" />{t('logs.export')}
          </Button>
        }
      />

      <Tabs defaultValue="detail">
        <TabsList>
          <TabsTrigger value="detail"><ScrollText className="mr-1.5 h-4 w-4" />{t('logs.tabDetail')}</TabsTrigger>
          <TabsTrigger value="summary"><Users className="mr-1.5 h-4 w-4" />{t('logs.tabSummary')}</TabsTrigger>
        </TabsList>

        {/* ── 明细 ──────────────────────────────────────────── */}
        <TabsContent value="detail" className="mt-4 flex flex-col gap-3">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
            <Input value={model} onChange={(e) => setModel(e.target.value)} placeholder={t('logs.fModel')} />
            <Input value={keyName} onChange={(e) => setKeyName(e.target.value)} placeholder={t('logs.fKey')} />
            <Select value={accountId} onValueChange={setAccountId}>
              <SelectTrigger><SelectValue placeholder={t('logs.fAccount')} /></SelectTrigger>
              <SelectContent>
                <SelectItem value="0">{t('logs.fAccountAll')}</SelectItem>
                {accOptions.map((a) => (
                  <SelectItem key={a.id} value={String(a.id)}>{a.name}（{a.requests}）</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Input value={accName} onChange={(e) => setAccName(e.target.value)}
                   placeholder={t('logs.fAccountName')} disabled={accountId !== '0'} />
            <Input value={clientIp} onChange={(e) => setClientIp(e.target.value)} placeholder={t('logs.fIp')} />
          </div>

          {!page ? (
            <SkeletonBar className="h-96" />
          ) : page.items.length === 0 ? (
            <EmptyState title={t('logs.empty')} />
          ) : (
            <div className="rounded-xl border bg-card overflow-x-auto">
          <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t('logs.colTime')}</TableHead>
                    <TableHead>{t('logs.colKey')}</TableHead>
                    <TableHead>{t('logs.colAccount')}</TableHead>
                    <TableHead>{t('logs.colModel')}</TableHead>
                    <TableHead className="text-right">{t('logs.colTokens')}</TableHead>
                    <TableHead className="text-right">{t('logs.colCredits')}</TableHead>
                    <TableHead className="text-right">{t('logs.colLatency')}</TableHead>
                    <TableHead>{t('logs.colResult')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {page.items.map((r) => <LogRow key={r.id} r={r} />)}
                </TableBody>
              </Table>
            </div>
          )}

          {page && page.items.length > 0 && (
            <div className="flex items-center justify-between">
              <span className="text-xs text-muted-foreground">
                {t('logs.pageInfo', {page: pageNo, total: page.total ?? '—'})}
              </span>
              <div className="flex gap-1">
                <Button variant="outline" size="sm" onClick={goPrev} disabled={stack.length <= 1}>
                  <ChevronLeft className="h-4 w-4" />{t('logs.prev')}
                </Button>
                <Button variant="outline" size="sm" onClick={goNext} disabled={!page.has_more}>
                  {t('logs.next')}<ChevronRight className="h-4 w-4" />
                </Button>
              </div>
            </div>
          )}
        </TabsContent>

        {/* ── 按账号汇总 ─────────────────────────────────────── */}
        <TabsContent value="summary" className="mt-4 flex flex-col gap-3">
          <div className="flex items-center gap-2">
            <Label className="text-xs text-muted-foreground">{t('logs.range')}</Label>
            <Select value={days} onValueChange={setDays}>
              <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="1">{t('logs.d1')}</SelectItem>
                <SelectItem value="7">{t('logs.d7')}</SelectItem>
                <SelectItem value="30">{t('logs.d30')}</SelectItem>
              </SelectContent>
            </Select>
            {summary && (
              <span className="text-xs text-muted-foreground">
                {t('logs.summaryTotal', {req: fmtNumber(summary.total_requests), credits: summary.total_credits.toFixed(2)})}
              </span>
            )}
          </div>
          {!summary ? (
            <SkeletonBar className="h-72" />
          ) : summary.items.length === 0 ? (
            <EmptyState title={t('logs.emptySummary')} />
          ) : (
            <div className="rounded-xl border bg-card overflow-x-auto">
          <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t('logs.colAccount')}</TableHead>
                    <TableHead className="text-right">{t('logs.colRequests')}</TableHead>
                    <TableHead className="text-right">{t('logs.colSuccess')}</TableHead>
                    <TableHead className="text-right">{t('logs.colCredits')}</TableHead>
                    <TableHead className="text-right">{t('logs.colTokens')}</TableHead>
                    <TableHead className="text-right">{t('logs.colAvgLatency')}</TableHead>
                    <TableHead>{t('logs.colLastUsed')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {summary.items.map((it) => {
                    const okRate = it.requests > 0 ? (it.ok_requests / it.requests) * 100 : 100;
                    return (
                      <TableRow key={it.account_id}>
                        <TableCell className="font-medium">{it.account_name}</TableCell>
                        <TableCell className="text-right">{fmtNumber(it.requests)}</TableCell>
                        <TableCell className="text-right">
                          <span className={cn(okRate < 90 ? 'text-red-500' : okRate < 98 ? 'text-amber-500' : 'text-emerald-600')}>
                            {okRate.toFixed(1)}%
                          </span>
                          {it.failed_requests > 0 && (
                            <span className="ml-1 text-[10px] text-muted-foreground">({it.failed_requests}✗)</span>
                          )}
                        </TableCell>
                        <TableCell className="text-right">{it.credits.toFixed(2)}</TableCell>
                        <TableCell className="text-right">{fmtNumber(it.total_tokens)}</TableCell>
                        <TableCell className="text-right">{it.avg_latency_ms}ms</TableCell>
                        <TableCell className="text-xs text-muted-foreground">{fmtIso(it.last_used_at)}</TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}

/* ── 明细行 ──────────────────────────────────────────────────────── */
function LogRow({r}: {r: UsageLog}) {
  const t = useT();
  const failed = r.error_kind && r.error_kind !== 'success';
  const modelCell = (
    <div className="flex flex-col">
      <span className="font-mono text-[12px]">{r.model}</span>
      {r.requested_model && r.requested_model !== r.model && (
        <span className="text-[10px] text-violet-500">↦ {r.requested_model}</span>
      )}
    </div>
  );
  return (
    <TableRow>
      <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{fmtIso(r.created_at)}</TableCell>
      <TableCell className="text-xs">{r.key_name}</TableCell>
      <TableCell className="text-xs">{r.account_name}</TableCell>
      <TableCell>
        {r.requested_model && r.requested_model !== r.model ? (
          <TooltipProvider delayDuration={200}>
            <Tooltip>
              <TooltipTrigger asChild>{modelCell}</TooltipTrigger>
              <TooltipContent className="text-xs">{t('logs.aliasHit', {from: r.requested_model, to: r.model})}</TooltipContent>
            </Tooltip>
          </TooltipProvider>
        ) : modelCell}
      </TableCell>
      <TableCell className="text-right text-xs">
        <TooltipProvider delayDuration={200}>
          <Tooltip>
            <TooltipTrigger asChild><span>{fmtNumber(r.total_tokens)}</span></TooltipTrigger>
            <TooltipContent className="text-xs">
              {t('logs.tokenBreak', {p: fmtNumber(r.prompt_tokens), c: fmtNumber(r.completion_tokens), cached: fmtNumber(r.cached_tokens)})}
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
      </TableCell>
      <TableCell className="text-right text-xs">{r.credits.toFixed(3)}</TableCell>
      <TableCell className="text-right text-xs">
        {r.latency_ms != null ? `${(r.latency_ms / 1000).toFixed(1)}s` : '—'}
      </TableCell>
      <TableCell>
        {failed ? (
          <Badge variant="outline" className="border-red-300 text-[10px] text-red-600">{r.error_kind}</Badge>
        ) : (
          <Badge variant="outline" className="border-emerald-300 text-[10px] text-emerald-600">{t('logs.ok')}</Badge>
        )}
      </TableCell>
    </TableRow>
  );
}
