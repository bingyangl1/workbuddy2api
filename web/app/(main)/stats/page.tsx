'use client';

/**
 * 使用统计（Wave B6，recharts 图表核心页）。
 *
 * 本引擎 stats/usage 一次返回「概览卡片 + 模型分布 + 端点分布 + 按Key分布 + 趋势」，
 * 全部 SQL 侧聚合、同一套 where 条件（筛选后所有图与卡片跟着变）。
 * 口径：token/积分只统计成功请求（error_kind 空/success），请求数含失败（反映真实负载）。
 * 对齐 routers/stats.py::usage_stats。
 */
import {useCallback, useEffect, useMemo, useState} from 'react';
import {statsApi, errText} from '@/lib/api';
import type {UsageStats, UsageOptions} from '@/lib/types';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {StatCard} from '@/components/common/layout/StatCard';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {useT} from '@/lib/i18n/provider';
import {fmtNumber} from '@/lib/format';
import {Label} from '@/components/ui/label';
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import {
  ResponsiveContainer, LineChart, Line, BarChart, Bar, PieChart, Pie, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip as RTooltip, Legend,
} from 'recharts';

const COLORS = ['#6366f1', '#22d3ee', '#a78bfa', '#34d399', '#fbbf24', '#fb7185', '#94a3b8'];

export default function StatsPage() {
  const t = useT();
  const [stats, setStats] = useState<UsageStats | null>(null);
  const [options, setOptions] = useState<UsageOptions | null>(null);
  const [error, setError] = useState('');
  const [days, setDays] = useState('1');
  const [granularity, setGranularity] = useState('hour');
  const [keyId, setKeyId] = useState('0');
  const [model, setModel] = useState('');
  const [useCase, setUseCase] = useState('');

  const load = useCallback(async () => {
    setError('');
    try {
      const p: Record<string, unknown> = {days: Number(days) || 1, granularity};
      if (keyId !== '0') p.key_id = Number(keyId);
      if (model) p.model = model;
      if (useCase) p.use_case = useCase;
      const r = await statsApi.usage(p);
      setStats(r);
    } catch (e) { setError(errText(e)); }
  }, [days, granularity, keyId, model, useCase]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { statsApi.options(30).then(setOptions).catch(() => {}); }, []);

  const trendData = useMemo(() => (stats?.trend || []).map((p) => ({
    bucket: p.bucket.slice(granularity === 'hour' ? 5 : 5), // 去掉年份前缀
    requests: p.requests,
    credits: p.credits,
    tokens: p.prompt_tokens + p.completion_tokens,
  })), [stats, granularity]);

  if (error) return <LoadError message={error} onRetry={load} />;
  if (!stats) return <SkeletonBar className="h-96" />;

  const ov = stats.overview;
  const okRate = ov.requests > 0 ? (ov.ok_requests / ov.requests) * 100 : 100;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader title={t('stats.title')} description={t('stats.subtitle', {days})} />

      {/* 筛选（所有图与卡片联动） */}
      <div className="flex flex-wrap items-end gap-2">
        <div className="space-y-1">
          <Label className="text-xs text-muted-foreground">{t('stats.range')}</Label>
          <Select value={days} onValueChange={setDays}>
            <SelectTrigger className="w-28"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="1">{t('stats.d1')}</SelectItem>
              <SelectItem value="7">{t('stats.d7')}</SelectItem>
              <SelectItem value="14">{t('stats.d14')}</SelectItem>
              <SelectItem value="30">{t('stats.d30')}</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-muted-foreground">{t('stats.granularity')}</Label>
          <Select value={granularity} onValueChange={setGranularity}>
            <SelectTrigger className="w-24"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="hour">{t('stats.byHour')}</SelectItem>
              <SelectItem value="day">{t('stats.byDay')}</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-muted-foreground">{t('stats.fKey')}</Label>
          <Select value={keyId} onValueChange={setKeyId}>
            <SelectTrigger className="w-36"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="0">{t('stats.allKeys')}</SelectItem>
              {(options?.keys || []).map((k) => (
                <SelectItem key={k.id} value={String(k.id)}>{k.name || `#${k.id}`}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-muted-foreground">{t('stats.fModel')}</Label>
          <Select value={model || 'all'} onValueChange={(v) => setModel(v === 'all' ? '' : v)}>
            <SelectTrigger className="w-40"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t('stats.allModels')}</SelectItem>
              {(options?.models || []).map((m) => <SelectItem key={m} value={m}>{m}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-muted-foreground">{t('stats.fEndpoint')}</Label>
          <Select value={useCase || 'all'} onValueChange={(v) => setUseCase(v === 'all' ? '' : v)}>
            <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t('stats.allEndpoints')}</SelectItem>
              {(options?.use_cases || []).map((u) => <SelectItem key={u} value={u}>{u}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
      </div>

      {/* 概览卡片 */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard label={t('stats.requests')} value={fmtNumber(ov.requests)} />
        <StatCard label={t('stats.credits')} value={ov.credits.toFixed(1)} />
        <StatCard label={t('stats.tokens')} value={fmtNumber(ov.total_tokens)} />
        <StatCard label={t('stats.okRate')} value={`${okRate.toFixed(1)}%`} />
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard label={t('stats.okRequests')} value={fmtNumber(ov.ok_requests)} />
        <StatCard label={t('stats.failedRequests')} value={fmtNumber(ov.failed_requests)} />
        <StatCard label={t('stats.cachedTokens')} value={fmtNumber(ov.cached_tokens)} />
        <StatCard label={t('stats.avgLatency')} value={`${ov.avg_latency_ms}ms`} />
      </div>

      {/* 趋势 */}
      <section className="rounded-xl border bg-card p-4">
        <h2 className="mb-3 text-sm font-semibold">{t('stats.trend')}</h2>
        {trendData.length === 0 ? (
          <EmptyState title={t('stats.noData')} />
        ) : (
          <ResponsiveContainer width="100%" height={240}>
            <LineChart data={trendData} margin={{top: 4, right: 8, left: -8, bottom: 0}}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
              <XAxis dataKey="bucket" tick={{fontSize: 10}} stroke="hsl(var(--muted-foreground))" />
              <YAxis tick={{fontSize: 10}} stroke="hsl(var(--muted-foreground))" />
              <RTooltip contentStyle={{fontSize: 12, borderRadius: 8}} />
              <Legend wrapperStyle={{fontSize: 12}} />
              <Line type="monotone" dataKey="requests" name={t('stats.requests')} stroke="#6366f1" strokeWidth={2} dot={false} />
              <Line type="monotone" dataKey="credits" name={t('stats.credits')} stroke="#22d3ee" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        )}
      </section>

      {/* 三个分布 */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <DistBar title={t('stats.byModel')} data={stats.by_model} metric="credits" metricLabel={t('stats.credits')} />
        <DistBar title={t('stats.byEndpoint')} data={stats.by_endpoint} metric="credits" metricLabel={t('stats.credits')} />
        <DistPie title={t('stats.byKey')} data={stats.by_key} />
      </div>
    </div>
  );
}

/* ── 分布条形图 ─────────────────────────────────────────────────── */
function DistBar({title, data, metric, metricLabel}: {
  title: string; data: {label: string; requests: number; tokens: number; credits: number}[];
  metric: 'credits' | 'requests' | 'tokens'; metricLabel: string;
}) {
  const t = useT();
  const top = data.slice(0, 8);
  return (
    <section className="rounded-xl border bg-card p-4">
      <h2 className="mb-3 text-sm font-semibold">{title}</h2>
      {top.length === 0 ? (
        <EmptyState title={t('stats.noData')} />
      ) : (
        <ResponsiveContainer width="100%" height={220}>
          <BarChart data={top} layout="vertical" margin={{top: 0, right: 12, left: 8, bottom: 0}}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" horizontal={false} />
            <XAxis type="number" tick={{fontSize: 10}} stroke="hsl(var(--muted-foreground))" />
            <YAxis type="category" dataKey="label" width={90} tick={{fontSize: 10}}
                   stroke="hsl(var(--muted-foreground))" />
            <RTooltip contentStyle={{fontSize: 12, borderRadius: 8}} />
            <Bar dataKey={metric} name={metricLabel} fill="#6366f1" radius={[0, 4, 4, 0]} />
          </BarChart>
        </ResponsiveContainer>
      )}
    </section>
  );
}

/* ── 分布饼图 ───────────────────────────────────────────────────── */
function DistPie({title, data}: {
  title: string; data: {label: string; requests: number; tokens: number; credits: number}[];
}) {
  const t = useT();
  const top = data.slice(0, 7);
  return (
    <section className="rounded-xl border bg-card p-4">
      <h2 className="mb-3 text-sm font-semibold">{title}</h2>
      {top.length === 0 ? (
        <EmptyState title={t('stats.noData')} />
      ) : (
        <ResponsiveContainer width="100%" height={220}>
          <PieChart>
            <Pie data={top} dataKey="credits" nameKey="label" cx="50%" cy="50%"
                 outerRadius={78} label={(p) => p.label} fontSize={10}>
              {top.map((_, i) => <Cell key={i} fill={COLORS[i % COLORS.length]} />)}
            </Pie>
            <RTooltip contentStyle={{fontSize: 12, borderRadius: 8}}
                      formatter={(v: number) => [`${Number(v).toFixed(2)} ${t('stats.credits')}`, '']} />
          </PieChart>
        </ResponsiveContainer>
      )}
    </section>
  );
}
