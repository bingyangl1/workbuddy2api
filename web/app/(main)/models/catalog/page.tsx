'use client';

/**
 * 模型中心（P4-2）：按 realm 直连腾讯模型目录的只读浏览页。
 *
 * 与「模型」页（白名单 + 倍率管理）分工：那页管「哪些模型能用、多贵」，
 * 本页管「这个模型是什么」——显示名 / 描述 / 上下文 / 最大输出 / 推理档位 /
 * 能力标记 / 积分倍率，并按系列分组、搜索、能力筛选、排序。
 *
 * 数据来自 GET /api/models/catalog（按 realm 分键缓存，来源 source 如实标注）。
 * 倍率/启停是本地 ModelConfig 合并进来的——本页只读，启停管理仍在「模型」页。
 * 对齐 routers/models.py::model_catalog。
 */
import {useCallback, useEffect, useMemo, useState} from 'react';
import {modelApi, errText} from '@/lib/api';
import type {CatalogModel, ModelCatalogResponse} from '@/lib/types';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {useT} from '@/lib/i18n/provider';
import {useRealm} from '@/lib/realm-context';
import {cn} from '@/lib/utils';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
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
  Brain, Image as ImageIcon, Info, Loader2, RefreshCw, Search, Wrench,
} from 'lucide-react';

type CapFilter = 'all' | 'reasoning' | 'images' | 'toolCall' | 'free';
type SortKey = 'series' | 'multiplier' | 'context';

export default function ModelCatalogPage() {
  const t = useT();
  const {realm} = useRealm();
  const [data, setData] = useState<ModelCatalogResponse | null>(null);
  const [error, setError] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [query, setQuery] = useState('');
  const [cap, setCap] = useState<CapFilter>('all');
  const [sort, setSort] = useState<SortKey>('series');

  const load = useCallback(async (force = false) => {
    setError('');
    setRefreshing(true);
    try {
      const res = await modelApi.catalog(realm, force);
      setData(res);
    } catch (e) {
      setError(errText(e));
    } finally {
      setRefreshing(false);
    }
  }, [realm]);

  useEffect(() => { setData(null); load(); }, [load]);

  const filtered = useMemo(() => {
    let items = data?.models ?? [];
    const q = query.trim().toLowerCase();
    if (q) {
      items = items.filter((m) =>
        m.id.toLowerCase().includes(q) ||
        (m.name || '').toLowerCase().includes(q) ||
        (m.description || '').toLowerCase().includes(q) ||
        (m.tags || []).some((tg) => tg.toLowerCase().includes(q)));
    }
    if (cap === 'reasoning') items = items.filter((m) => m.supports_reasoning);
    else if (cap === 'images') items = items.filter((m) => m.supports_images);
    else if (cap === 'toolCall') items = items.filter((m) => m.supports_tool_call);
    else if (cap === 'free') items = items.filter((m) => m.is_free);

    const sorted = [...items];
    if (sort === 'multiplier') {
      sorted.sort((a, b) => a.credit_multiplier - b.credit_multiplier);
    } else if (sort === 'context') {
      sorted.sort((a, b) => b.context_length - a.context_length);
    } else {
      // 系列分组：系列名 → 倍率 → id
      sorted.sort((a, b) =>
        a.series.localeCompare(b.series) ||
        a.credit_multiplier - b.credit_multiplier ||
        a.id.localeCompare(b.id));
    }
    return sorted;
  }, [data, query, cap, sort]);

  // 系列分组（sort=series 时分组渲染标题行）
  const grouped = useMemo(() => {
    if (sort !== 'series') return null;
    const map = new Map<string, CatalogModel[]>();
    for (const m of filtered) {
      const arr = map.get(m.series) || [];
      arr.push(m);
      map.set(m.series, arr);
    }
    return [...map.entries()];
  }, [filtered, sort]);

  if (error) return <LoadError message={error} onRetry={() => load()} />;
  if (!data) return <SkeletonBar className="h-96" />;

  const fmtCtx = (n: number) => (n > 0 ? t('catalog.ctxCompact', {k: Math.round(n / 1000)}) : '—');

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t('catalog.title')}
        description={t('catalog.description')}
        actions={
          <Button size="sm" variant="outline" onClick={() => load(true)} disabled={refreshing}>
            {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            {refreshing ? t('catalog.refreshing') : t('catalog.refresh')}
          </Button>
        }
      />

      {/* 来源标注 + 缓存提示（如实标注，含推导值声明） */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1">
          <Info className="h-3 w-3" />
          {t('catalog.source')}: {data.source_label}
        </span>
        {data.cached && (
          <Badge variant="outline" className="text-[10px]">
            {t('catalog.cachedHint')} · {t('catalog.secondsAgo', {n: data.cache_age})}
          </Badge>
        )}
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="cursor-help underline decoration-dotted">{t('catalog.seriesDerived')}</span>
            </TooltipTrigger>
            <TooltipContent>{t('catalog.seriesDerived')}</TooltipContent>
          </Tooltip>
        </TooltipProvider>
      </div>

      {/* 工具条：搜索 + 能力筛选 + 排序 */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-0 flex-1 sm:max-w-sm">
          <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input value={query} onChange={(e) => setQuery(e.target.value)}
                 placeholder={t('catalog.search')} className="pl-8" />
        </div>
        <Select value={cap} onValueChange={(v) => setCap(v as CapFilter)}>
          <SelectTrigger className="w-[130px]" aria-label={t('catalog.filterCapability')}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{t('catalog.capAll')}</SelectItem>
            <SelectItem value="reasoning">{t('catalog.capReasoning')}</SelectItem>
            <SelectItem value="images">{t('catalog.capImages')}</SelectItem>
            <SelectItem value="toolCall">{t('catalog.capToolCall')}</SelectItem>
            <SelectItem value="free">{t('catalog.capFree')}</SelectItem>
          </SelectContent>
        </Select>
        <Select value={sort} onValueChange={(v) => setSort(v as SortKey)}>
          <SelectTrigger className="w-[130px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="series">{t('catalog.sortSeries')}</SelectItem>
            <SelectItem value="multiplier">{t('catalog.sortMultiplier')}</SelectItem>
            <SelectItem value="context">{t('catalog.sortContext')}</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {filtered.length === 0 ? (
        <EmptyState title={t('catalog.noModels')} />
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t('catalog.colModel')}</TableHead>
                <TableHead className="text-right">{t('catalog.colContext')}</TableHead>
                <TableHead className="text-right">{t('catalog.colMaxOutput')}</TableHead>
                <TableHead>{t('catalog.colEfforts')}</TableHead>
                <TableHead className="text-right">{t('catalog.colMultiplier')}</TableHead>
                <TableHead>{t('catalog.colCapability')}</TableHead>
                <TableHead>{t('catalog.colStatus')}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {grouped
                ? grouped.map(([series, items]) => (
                    <SeriesSection key={series} series={series} items={items} t={t} fmtCtx={fmtCtx} />
                  ))
                : filtered.map((m) => <ModelRow key={m.id} m={m} t={t} fmtCtx={fmtCtx} />)}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}

function SeriesSection({series, items, t, fmtCtx}: {
  series: string; items: CatalogModel[];
  t: ReturnType<typeof useT>; fmtCtx: (n: number) => string;
}) {
  return (
    <>
      <TableRow className="bg-muted/40 hover:bg-muted/40">
        <TableCell colSpan={7} className="py-1.5 text-xs font-semibold text-muted-foreground">
          {series} · {items.length}
        </TableCell>
      </TableRow>
      {items.map((m) => <ModelRow key={m.id} m={m} t={t} fmtCtx={fmtCtx} />)}
    </>
  );
}

function ModelRow({m, t, fmtCtx}: {
  m: CatalogModel; t: ReturnType<typeof useT>; fmtCtx: (n: number) => string;
}) {
  return (
    <TableRow>
      <TableCell>
        <div className="flex flex-col">
          <span className="font-medium">{m.name || m.id}
            {m.is_default && <Badge variant="secondary" className="ml-1.5 text-[10px]">{t('catalog.effortDefault')}</Badge>}
          </span>
          <span className="font-mono text-[11px] text-muted-foreground">{m.id}</span>
          {m.description && (
            <span className="mt-0.5 line-clamp-1 max-w-md text-[11px] text-muted-foreground">{m.description}</span>
          )}
        </div>
      </TableCell>
      <TableCell className="text-right tabular-nums">{fmtCtx(m.context_length)}</TableCell>
      <TableCell className="text-right tabular-nums">{fmtCtx(m.max_output_tokens)}</TableCell>
      <TableCell>
        {m.efforts.length > 0 ? (
          <div className="flex flex-wrap gap-1">
            {m.efforts.map((e) => (
              <Badge key={e} variant={e === m.default_effort ? 'default' : 'outline'}
                     className="text-[10px]">{e}</Badge>
            ))}
          </div>
        ) : '—'}
      </TableCell>
      <TableCell className="text-right">
        {m.is_free
          ? <Badge variant="secondary" className="text-[10px]">{t('catalog.free')}</Badge>
          : <span className="tabular-nums text-xs">{m.credits_raw || `x${m.credit_multiplier}`}</span>}
      </TableCell>
      <TableCell>
        <div className="flex items-center gap-1.5 text-muted-foreground">
          {m.supports_reasoning && (
            <span title={t('catalog.capReasoning')}><Brain className="h-3.5 w-3.5" /></span>)}
          {m.supports_images && (
            <span title={t('catalog.capImages')}><ImageIcon className="h-3.5 w-3.5" /></span>)}
          {m.supports_tool_call && (
            <span title={t('catalog.capToolCall')}><Wrench className="h-3.5 w-3.5" /></span>)}
          {!m.supports_reasoning && !m.supports_images && !m.supports_tool_call && '—'}
        </div>
      </TableCell>
      <TableCell>
        {!m.in_local_config
          ? <Badge variant="outline" className="text-[10px]">{t('catalog.notInConfig')}</Badge>
          : m.enabled
            ? <Badge className="text-[10px]">{t('catalog.enabled')}</Badge>
            : <Badge variant="outline" className="text-[10px]">{t('catalog.disabled')}</Badge>}
      </TableCell>
    </TableRow>
  );
}
