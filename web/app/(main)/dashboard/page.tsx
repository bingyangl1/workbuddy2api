'use client';

/**
 * 仪表盘（Wave A 验证页）：调通本引擎 /api/stats/usage + /api/accounts +
 * /api/stats/model-health，验证 typed client + JWT 鉴权 + i18n 全链路。
 *
 * 这是 P3 第一个能跑的页面——刻意保持简单，只证明「前端 → 本引擎契约」
 * 这条链路通了。丰富的可视化在 Wave B 按本引擎数据形状逐步补。
 */
import {useCallback, useEffect, useState} from 'react';
import {accountApi, statsApi, errText} from '@/lib/api';
import type {Account, ModelHealth, UsageStats} from '@/lib/types';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {StatCard} from '@/components/common/layout/StatCard';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {useT} from '@/lib/i18n/provider';

export default function DashboardPage() {
  const t = useT();
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [usage, setUsage] = useState<UsageStats | null>(null);
  const [health, setHealth] = useState<ModelHealth[] | null>(null);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setError('');
    try {
      const [accRes, usageRes, healthRes] = await Promise.all([
        accountApi.list(),
        statsApi.usage({days: 1, granularity: 'hour'}),
        statsApi.modelHealth(24),
      ]);
      setAccounts(accRes.items);
      setUsage(usageRes);
      setHealth(healthRes.items);
    } catch (e) {
      setError(errText(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (error) return <LoadError message={error} onRetry={load} />;
  if (!accounts || !usage || !health) return <SkeletonBar className="h-64" />;

  const active = accounts.filter((a) => a.status === 'active').length;
  const totalBalance = accounts.reduce((s, a) => s + (a.balance_remain ?? 0), 0);

  return (
    <div className="flex flex-col gap-4">
      <PageHeader title={t('dashboard.title')} description={t('dashboard.subtitle')} />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard label={t('dashboard.accounts')} value={String(accounts.length)} />
        <StatCard label={t('dashboard.activeAccounts')} value={String(active)} />
        <StatCard label={t('dashboard.todayRequestsLabel')} value={String(usage.overview.requests)} />
        <StatCard label={t('dashboard.todayCredits')} value={String(Math.round(usage.overview.credits))} />
      </div>

      <section className="rounded-xl border bg-card p-4">
        <h2 className="mb-3 text-sm font-semibold">{t('dashboard.modelHealth')}</h2>
        {health.length === 0 ? (
          <EmptyState title={t('dashboard.noData')} />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b text-left text-xs text-muted-foreground">
                  <th className="py-2 pr-4 font-medium">{t('dashboard.colModel')}</th>
                  <th className="py-2 pr-4 font-medium">{t('dashboard.colRequests')}</th>
                  <th className="py-2 pr-4 font-medium">{t('dashboard.colSuccessRate')}</th>
                  <th className="py-2 font-medium">{t('dashboard.colLatency')}</th>
                </tr>
              </thead>
              <tbody>
                {health.map((h) => (
                  <tr key={h.model} className="border-b last:border-0">
                    <td className="py-2 pr-4 font-mono text-xs">{h.model}</td>
                    <td className="py-2 pr-4">{h.requests}</td>
                    <td className="py-2 pr-4">
                      {h.success_rate == null ? '—' : `${(h.success_rate * 100).toFixed(1)}%`}
                    </td>
                    <td className="py-2">
                      {h.latency_ms?.p50 == null ? '—' : `${Math.round(h.latency_ms.p50)} ms`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <p className="text-xs text-muted-foreground">
        {t('dashboard.balanceNote', {total: Math.round(totalBalance)})}
      </p>
    </div>
  );
}
