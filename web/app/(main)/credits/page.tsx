'use client';

/**
 * 积分统计（Wave B10）。
 *
 * 两个视角（对齐 routers/credits.py，数据源 = refresh_credits 定时任务写的快照，
 * 统计接口不打上游）：
 * 1. 到期：谁在烧、还剩多少、还剩多久——按紧迫度升序（复用调度 urgency_rank，
 *    界面排第一的就是下一个被选中的号），概览卡用余额口径 + 到期分档条用快照口径；
 * 2. 消耗构成：按账号/按模型的消耗排行（usage_logs 聚合，只计成功请求）。
 */
import {useCallback, useEffect, useState} from 'react';
import {creditsApi, errText} from '@/lib/api';
import type {CreditExpiryStats, CreditUsageStats, CreditWindow} from '@/lib/types';
import {useRealm} from '@/lib/realm-context';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {StatCard} from '@/components/common/layout/StatCard';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';
import {fmtNumber} from '@/lib/format';
import {Badge} from '@/components/ui/badge';
import {Label} from '@/components/ui/label';
import {Progress} from '@/components/ui/progress';
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
import {CalendarClock, Flame} from 'lucide-react';

function fmtDate(ms: number | null): string {
  if (!ms) return '—';
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export default function CreditsPage() {
  const t = useT();
  const {realm, label: realmLabel} = useRealm();
  const [expiry, setExpiry] = useState<CreditExpiryStats | null>(null);
  const [usage, setUsage] = useState<CreditUsageStats | null>(null);
  const [error, setError] = useState('');
  const [days, setDays] = useState('7');
  /** true = 看两版合计（不对 realm 过滤）；false = 只看当前版本。 */
  const [watchAll, setWatchAll] = useState(false);

  // 积分按版本过滤：两版积分体系不通用（不同上游域、不同计费规则），
  // 混在一起看不出「是哪一版的账号缺积分」。`watchAll` 用于跨版总览。
  const load = useCallback(async () => {
    setError('');
    try {
      const scope = watchAll ? undefined : realm;
      const [eRes, uRes] = await Promise.all([
        creditsApi.expiry({days: 7, realm: scope}),
        creditsApi.usage({days: Number(days) || 7, realm: scope}),
      ]);
      setExpiry(eRes);
      setUsage(uRes);
    } catch (e) { setError(errText(e)); }
  }, [days, realm, watchAll]);

  useEffect(() => { load(); }, [load]);

  if (error) return <LoadError message={error} onRetry={load} />;
  if (!expiry || !usage) return <SkeletonBar className="h-96" />;

  const s = expiry.summary;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader title={t('credits.title')} description={t('credits.subtitle')} />

      {/*
        版本范围提示条。
        两版积分体系不通用（不同上游域、不同计费规则），默认跟随右上角的版本切换；
        这里额外给一个「看两版合计」的开关 —— 想总览时不必来回切版本。
        无论哪种视图，都明确标出「当前看的是哪一版」，避免看着国内版数字
        去判断国际版的情况（这正是用户报过的那类困惑）。
      */}
      <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-card/40 px-3 py-2 text-xs">
        <span className="text-muted-foreground">
          {t('credits.scope')}
        </span>
        <Badge variant={watchAll ? 'outline' : 'default'}>
          {watchAll ? t('credits.scopeAll') : realmLabel}
        </Badge>
        {!watchAll && (
          <span className="text-muted-foreground">
            {expiry.accounts_total != null
              ? t('credits.scopeAccounts', {n: String(expiry.accounts_total)})
              : ''}
          </span>
        )}
        <button
          type="button"
          onClick={() => setWatchAll((v) => !v)}
          className="ml-auto rounded-md border px-2 py-1 text-xs hover:bg-accent"
        >
          {watchAll ? t('credits.scopeOnlyThis') : t('credits.scopeAllBtn')}
        </button>
      </div>

      <Tabs defaultValue="expiry">
        <TabsList>
          <TabsTrigger value="expiry"><CalendarClock className="mr-1.5 h-4 w-4" />{t('credits.tabExpiry')}</TabsTrigger>
          <TabsTrigger value="usage"><Flame className="mr-1.5 h-4 w-4" />{t('credits.tabUsage')}</TabsTrigger>
        </TabsList>

        {/* ── 到期 ─────────────────────────────────────────── */}
        <TabsContent value="expiry" className="mt-4 flex flex-col gap-4">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <StatCard label={t('credits.available')} value={fmtNumber(Math.round(s.available_remaining))} />
            <StatCard label={t('credits.expiringSoon')} value={fmtNumber(Math.round(s.expiring_soon_remaining))} />
            <StatCard label={t('credits.evergreen')} value={fmtNumber(Math.round(s.evergreen_remaining))} />
            <StatCard label={t('credits.atRisk')} value={`${(s.at_risk_ratio * 100).toFixed(1)}%`} />
          </div>
          {s.soonest_account_name && (
            <p className="text-xs text-muted-foreground">
              {t('credits.soonest', {
                name: s.soonest_account_name,
                days: s.soonest_days_left != null ? String(s.soonest_days_left) : '—',
                date: fmtDate(s.soonest_expire_at),
              })}
            </p>
          )}

          {/* 到期分档条（快照口径，≤1/3/7/14/30 天） */}
          <section className="rounded-xl border bg-card p-4">
            <h2 className="mb-3 text-sm font-semibold">{t('credits.windows')}</h2>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
              {expiry.windows.map((w: CreditWindow) => {
                const pct = s.total_remaining > 0 ? (w.amount / s.total_remaining) * 100 : 0;
                const tone = w.days <= 3 ? 'bg-red-500' : w.days <= 7 ? 'bg-amber-500' : 'bg-sky-500';
                return (
                  <div key={w.days} className="rounded-md border p-2">
                    <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                      {t('credits.windowDays', {d: w.days})}
                    </div>
                    <div className="mt-0.5 text-sm font-semibold">{fmtNumber(Math.round(w.amount))}</div>
                    <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted">
                      <div className={cn('h-full', tone)} style={{width: `${Math.min(100, pct)}%`}} />
                    </div>
                    <div className="mt-0.5 text-[10px] text-muted-foreground">{t('credits.pkgs', {n: w.packages})}</div>
                  </div>
                );
              })}
            </div>
          </section>

          {/* 账号紧迫度排行 */}
          <section className="rounded-xl border bg-card">
            <div className="border-b p-3 text-sm font-semibold">{t('credits.accountsTitle')}</div>
            {expiry.accounts.length === 0 ? (
              <EmptyState title={t('credits.noAccounts')} />
            ) : (
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>{t('credits.colAccount')}</TableHead>
                      <TableHead className="text-right">{t('credits.colBalance')}</TableHead>
                      <TableHead className="text-right">{t('credits.colExpiring')}</TableHead>
                      <TableHead>{t('credits.colSoonest')}</TableHead>
                      <TableHead>{t('credits.colNextPkgs')}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {expiry.accounts.map((a) => {
                      const urgent = a.expiring_soon_remaining > 0;
                      return (
                        <TableRow key={a.account_id}>
                          <TableCell className="font-medium">
                            {a.account_name}
                            {a.status !== 'active' && (
                              <Badge variant="outline" className="ml-1.5 text-[9px]">{a.status}</Badge>
                            )}
                          </TableCell>
                          <TableCell className="text-right">{fmtNumber(a.balance_remain)}</TableCell>
                          <TableCell className="text-right">
                            {urgent ? (
                              <span className="font-medium text-red-500">{fmtNumber(a.expiring_soon_remaining)}</span>
                            ) : <span className="text-muted-foreground">0</span>}
                          </TableCell>
                          <TableCell>
                            {a.soonest_expire_at ? (
                              <TooltipProvider delayDuration={200}>
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <Badge variant="outline" className={cn('text-[10px]',
                                      (a.soonest_days_left ?? 99) <= 3 ? 'border-red-300 text-red-600'
                                      : (a.soonest_days_left ?? 99) <= 7 ? 'border-amber-300 text-amber-600'
                                      : 'border-sky-300 text-sky-600')}>
                                      {a.soonest_label}
                                    </Badge>
                                  </TooltipTrigger>
                                  <TooltipContent className="text-xs">{fmtDate(a.soonest_expire_at)}</TooltipContent>
                                </Tooltip>
                              </TooltipProvider>
                            ) : <span className="text-xs text-muted-foreground">—</span>}
                          </TableCell>
                          <TableCell className="max-w-[220px]">
                            <div className="flex flex-wrap gap-1">
                              {a.next_expiring.slice(0, 2).map((p, i) => (
                                <Badge key={i} variant="secondary" className="text-[9px]">
                                  {p.display_name}·{fmtNumber(Math.round(p.remaining))}
                                </Badge>
                              ))}
                              {a.active_package_count > 2 && (
                                <span className="text-[10px] text-muted-foreground">+{a.active_package_count - 2}</span>
                              )}
                            </div>
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>
            )}
          </section>
        </TabsContent>

        {/* ── 消耗构成 ─────────────────────────────────────── */}
        <TabsContent value="usage" className="mt-4 flex flex-col gap-4">
          <div className="flex items-center gap-2">
            <Label className="text-xs text-muted-foreground">{t('credits.range')}</Label>
            <Select value={days} onValueChange={setDays}>
              <SelectTrigger className="w-28"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="1">{t('credits.d1')}</SelectItem>
                <SelectItem value="7">{t('credits.d7')}</SelectItem>
                <SelectItem value="30">{t('credits.d30')}</SelectItem>
              </SelectContent>
            </Select>
            <span className="text-xs text-muted-foreground">
              {t('credits.totalBurned', {credits: usage.total_credits.toFixed(1)})}
            </span>
          </div>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {/* 按账号消耗 */}
            <section className="rounded-xl border bg-card">
              <div className="border-b p-3 text-sm font-semibold">{t('credits.byAccount')}</div>
              {usage.accounts.length === 0 ? (
                <EmptyState title={t('credits.noUsage')} />
              ) : (
                <div className="overflow-x-auto">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>{t('credits.colAccount')}</TableHead>
                        <TableHead className="text-right">{t('credits.colBurned')}</TableHead>
                        <TableHead className="text-right">{t('credits.colRemain')}</TableHead>
                        <TableHead>{t('credits.colShare')}</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {usage.accounts.map((a) => {
                        const share = usage.total_credits > 0 ? (a.credits / usage.total_credits) * 100 : 0;
                        return (
                          <TableRow key={a.account_id}>
                            <TableCell className="text-xs font-medium">{a.account_name}</TableCell>
                            <TableCell className="text-right text-xs">{a.credits.toFixed(1)}</TableCell>
                            <TableCell className="text-right text-xs">{fmtNumber(a.remaining)}</TableCell>
                            <TableCell>
                              <div className="flex items-center gap-1.5">
                                <Progress value={share} className="h-1.5 w-16" />
                                <span className="text-[10px] text-muted-foreground">{share.toFixed(0)}%</span>
                              </div>
                            </TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                </div>
              )}
            </section>

            {/* 按模型消耗 */}
            <section className="rounded-xl border bg-card">
              <div className="border-b p-3 text-sm font-semibold">{t('credits.byModel')}</div>
              {usage.models.length === 0 ? (
                <EmptyState title={t('credits.noUsage')} />
              ) : (
                <div className="overflow-x-auto">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>{t('credits.colModel')}</TableHead>
                        <TableHead className="text-right">{t('credits.colRequests')}</TableHead>
                        <TableHead className="text-right">{t('credits.colBurned')}</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {usage.models.map((m) => (
                        <TableRow key={m.model}>
                          <TableCell className="font-mono text-[11px]">{m.model}</TableCell>
                          <TableCell className="text-right text-xs">{fmtNumber(m.requests)}</TableCell>
                          <TableCell className="text-right text-xs">{m.credits.toFixed(1)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )}
            </section>
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}
