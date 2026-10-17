'use client';

/**
 * 限时权益 / 补偿领取（**手动运行，无定时任务**）。
 *
 * ## 为什么这个页面强调「手动」
 *
 * 用户明确要求：
 *
 * > 「这些拓展能力别默认启用，有些是只能跑一遍的，多遍跑会检测的，
 * >    让用户自己点击运行」
 *
 * 所以这个功能：
 *
 * * **不在任何调度任务里**（`TASK_CHOICES` 没有它，`scheduler.run_task()` 不认）
 * * 页面打开时**只读本地状态**（`/status`），不自动向上游发请求
 * * 想看「现在能不能领」必须点「查询」
 * * 领取必须点「领取」并二次确认
 *
 * 这个页面唯一的自动行为是读 `GET /status`（纯 DB 查询，秒回，不打上游）。
 *
 * ## 三步式交互
 *
 *     ① 读本地状态  →  列表显示「已领取 / 尚未查询」
 *     ② 点「查询」  →  打上游（只读），显示「可领 500 积分」
 *     ③ 点「领取」  →  写上游（每账号每期只成功一次），二次确认
 *
 * 第 ③ 步的二次确认是刻意的：领取不可回滚，误点代价是账号状态异常。
 */

import {useCallback, useEffect, useMemo, useState} from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Gift,
  Loader2,
  RefreshCw,
  Search,
  XCircle,
} from 'lucide-react';

import {benefitsApi, errText} from '@/lib/api';
import type {
  BenefitAccountState,
  BenefitCampaign,
  BenefitStatusResponse,
} from '@/lib/types';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';

/** 状态 → 配色与图标语义。 */
function tone(status: string): string {
  switch (status) {
    case 'available':
      return 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400';
    case 'claimed':
    case 'already':
      return 'border-blue-500/40 bg-blue-500/10 text-blue-700 dark:text-blue-400';
    case 'expired':
    case 'ineligible':
      return 'border-muted bg-muted text-muted-foreground';
    case 'error':
      return 'border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-400';
    default:
      return 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400';
  }
}

export default function BenefitsPage() {
  const t = useT();
  const [campaign, setCampaign] = useState<BenefitCampaign | null>(null);
  const [status, setStatus] = useState<BenefitStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState<'' | 'query' | 'claim'>('');
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [confirming, setConfirming] = useState(false);

  /** 只读本地状态（不打上游）。 */
  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [c, s] = await Promise.all([
        benefitsApi.campaign(),
        benefitsApi.status(),
      ]);
      setCampaign(c);
      setStatus(s);
    } catch (e) {
      setError(errText(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /** 把一批结果合并进列表（按 account_id 覆盖）。 */
  const merge = useCallback((rows: BenefitAccountState[]) => {
    setStatus((prev) => {
      if (!prev) return prev;
      const byId = new Map(prev.accounts.map((a) => [a.account_id, a]));
      for (const r of rows) {
        const old = byId.get(r.account_id);
        byId.set(r.account_id, {...(old || {}), ...r} as BenefitAccountState);
      }
      return {...prev, accounts: [...byId.values()]};
    });
  }, []);

  const onQuery = async () => {
    const ids = [...picked];
    if (!ids.length) return;
    setBusy('query');
    setError('');
    try {
      const res = await benefitsApi.query(ids);
      merge(res.results);
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy('');
    }
  };

  const onClaim = async () => {
    const ids = [...picked];
    if (!ids.length) return;
    setConfirming(false);
    setBusy('claim');
    setError('');
    try {
      const res = await benefitsApi.claim(ids);
      merge(res.results);
      await load();      // 重新拉活动汇总（已领取数/积分）
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy('');
    }
  };

  const accounts = status?.accounts || [];
  const selectable = useMemo(
    () => accounts.filter((a) => a.status !== 'already' && a.status !== 'claimed'),
    [accounts],
  );
  const allPicked = selectable.length > 0 && picked.size === selectable.length;

  const toggle = (id: number) => {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    setPicked(allPicked ? new Set() : new Set(selectable.map((a) => a.account_id)));
  };

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('benefits.title')}
        description={t('benefits.subtitle')}
      />

      {/* 手动运行的说明 —— 放在最显眼位置，避免用户误以为它会自动跑 */}
      <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-300">
        <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
        <span>
          <strong className="font-semibold">{t('benefits.manualOnly')}</strong>
          {t('benefits.manualOnlyHint')}
        </span>
      </div>

      {/* 活动汇总 */}
      {campaign && (
        <div className="grid gap-3 sm:grid-cols-3">
          <div className="rounded-md border p-3">
            <div className="text-xs text-muted-foreground">{t('benefits.accountsChecked')}</div>
            <div className="text-xl font-semibold">{campaign.accounts_checked}</div>
          </div>
          <div className="rounded-md border p-3">
            <div className="text-xs text-muted-foreground">{t('benefits.accountsClaimed')}</div>
            <div className="text-xl font-semibold">{campaign.accounts_claimed}</div>
          </div>
          <div className="rounded-md border p-3">
            <div className="text-xs text-muted-foreground">{t('benefits.creditsClaimed')}</div>
            <div className="text-xl font-semibold">{campaign.credits_claimed}</div>
          </div>
        </div>
      )}

      {/* 操作条 */}
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading || !!busy}>
          <RefreshCw className={cn('mr-1 size-3.5', loading && 'animate-spin')} />
          {t('benefits.refresh')}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => void onQuery()}
          disabled={!picked.size || !!busy}
          title={t('benefits.queryHint')}
        >
          <Search className="mr-1 size-3.5" />
          {busy === 'query' ? t('benefits.querying') : t('benefits.query')}
        </Button>
        <Button
          size="sm"
          onClick={() => setConfirming(true)}
          disabled={!picked.size || !!busy}
        >
          <Gift className="mr-1 size-3.5" />
          {busy === 'claim' ? t('benefits.claiming') : t('benefits.claim')}
        </Button>
        <span className="text-xs text-muted-foreground">
          {t('benefits.selected', {n: picked.size})}
        </span>
      </div>

      {error && <LoadError message={error} onRetry={() => void load()} />}

      {/* 列表 */}
      {loading && !status ? (
        <div className="space-y-2">
          <SkeletonBar /><SkeletonBar /><SkeletonBar />
        </div>
      ) : accounts.length === 0 ? (
        <EmptyState title={t('benefits.empty')} />
      ) : (
        <div className="overflow-hidden rounded-md border">
          <table className="w-full text-sm">
            <thead className="bg-muted/50 text-xs text-muted-foreground">
              <tr>
                <th className="w-10 px-2 py-2">
                  <input
                    type="checkbox"
                    aria-label={t('benefits.selectAll')}
                    checked={allPicked}
                    onChange={toggleAll}
                    disabled={!selectable.length}
                  />
                </th>
                <th className="px-2 py-2 text-left">{t('benefits.account')}</th>
                <th className="px-2 py-2 text-left">{t('benefits.realm')}</th>
                <th className="px-2 py-2 text-left">{t('benefits.status')}</th>
                <th className="px-2 py-2 text-left">{t('benefits.detail')}</th>
              </tr>
            </thead>
            <tbody>
              {accounts.map((a) => {
                const claimable = a.status === 'available';
                const done = a.status === 'already' || a.status === 'claimed';
                return (
                  <tr key={a.account_id} className="border-t">
                    <td className="px-2 py-2">
                      <input
                        type="checkbox"
                        aria-label={`选择账号 ${a.account_id}`}
                        checked={picked.has(a.account_id)}
                        onChange={() => toggle(a.account_id)}
                        disabled={done}
                      />
                    </td>
                    <td className="px-2 py-2">
                      <div className="font-medium">{a.nickname || `#${a.account_id}`}</div>
                      <div className="text-xs text-muted-foreground">{a.uid || '—'}</div>
                    </td>
                    <td className="px-2 py-2 text-xs">
                      {a.realm === 'global' ? t('benefits.realmGlobal') : t('benefits.realmCn')}
                    </td>
                    <td className="px-2 py-2">
                      <Badge variant="outline" className={cn('border', tone(a.status))}>
                        {claimable && <CheckCircle2 className="mr-1 size-3" />}
                        {(a.status === 'already' || a.status === 'claimed') && (
                          <CheckCircle2 className="mr-1 size-3" />
                        )}
                        {a.status === 'error' && <XCircle className="mr-1 size-3" />}
                        {t(`benefits.st.${a.status}`)}
                      </Badge>
                    </td>
                    <td className="px-2 py-2 text-xs text-muted-foreground">
                      {a.claimed_at && (
                        <div>
                          {t('benefits.claimedAt')} {a.claimed_at}
                          {a.claim_credit ? ` · ${a.claim_credit}` : ''}
                        </div>
                      )}
                      <div>{a.reason}</div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* 二次确认（领取不可回滚） */}
      {confirming && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
          role="dialog"
          aria-modal="true"
          aria-label={t('benefits.confirmTitle')}
        >
          <div className="w-full max-w-md rounded-lg border bg-background p-4 shadow-xl">
            <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold">
              <AlertTriangle className="size-4 text-amber-500" />
              {t('benefits.confirmTitle')}
            </h3>
            <p className="mb-1 text-xs text-muted-foreground">
              {t('benefits.confirmBody', {n: picked.size})}
            </p>
            <p className="mb-4 text-xs font-medium text-amber-700 dark:text-amber-400">
              {t('benefits.confirmIrreversible')}
            </p>
            <div className="flex justify-end gap-2">
              <Button variant="outline" size="sm" onClick={() => setConfirming(false)}>
                {t('benefits.cancel')}
              </Button>
              <Button size="sm" onClick={() => void onClaim()} disabled={!!busy}>
                {busy === 'claim' ? (
                  <Loader2 className="mr-1 size-3.5 animate-spin" />
                ) : (
                  <Gift className="mr-1 size-3.5" />
                )}
                {t('benefits.confirmYes')}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
