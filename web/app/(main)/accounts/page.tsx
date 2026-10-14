'use client';

/**
 * 账号池（Wave B 第一页，本引擎契约新写）。
 *
 * 本引擎账号是 **DB 模型**（id/name/uid/realm/group_id/status/balance），
 * 与 manager 的「文件模型账号」完全不同——本页不移植 manager 逻辑，
 * 只借其 UI 组件。功能对齐本引擎 routers/accounts.py（B1 核心子集）：
 *   列表 + realm/分组/状态过滤、添加（auth_json 真验证）、启停、删除、
 *   积分明细弹层（credit-details）、刷新余额、全量同步（credits/sync）。
 * 模型限流 / 去重 / 本机扫描导入 / 导出注入 / cat-travel 归 B2。
 */
import {useCallback, useEffect, useMemo, useState} from 'react';
import {accountApi, accountGroupApi, errText} from '@/lib/api';
import type {Account, AccountGroup, CreditDetails} from '@/lib/types';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {useRealm} from '@/lib/realm-context';
import {fmtNumber} from '@/lib/format';
import {cn} from '@/lib/utils';

/** 本引擎的时间字段是 ISO 字符串（last_used_at/created_at 等），
 *  与 manager 的 epoch 秒不同——本地格式化，无法解析时原样返回。 */
function fmtIso(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Textarea} from '@/components/ui/textarea';
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
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table';
import {
  Tooltip, TooltipContent, TooltipProvider, TooltipTrigger,
} from '@/components/ui/tooltip';
import {AlertTriangle, Coins, Flame, Plus, RefreshCw, Search, ShieldCheck, Timer, Trash2} from 'lucide-react';

export default function AccountsPage() {
  const t = useT();
  const {realm} = useRealm();
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [groups, setGroups] = useState<AccountGroup[]>([]);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [groupFilter, setGroupFilter] = useState<string>('all');
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [addOpen, setAddOpen] = useState(false);
  const [creditOf, setCreditOf] = useState<Account | null>(null);
  const [deleteOf, setDeleteOf] = useState<Account | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [syncing, setSyncing] = useState(false);

  const load = useCallback(async () => {
    setError('');
    try {
      const [accRes, grpRes] = await Promise.all([
        accountApi.list(),
        accountGroupApi.list(),
      ]);
      setAccounts(accRes.items);
      setGroups(grpRes.items);
    } catch (e) {
      setError(errText(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const filtered = useMemo(() => {
    if (!accounts) return [];
    const q = query.trim().toLowerCase();
    return accounts.filter((a) => {
      if ((a.realm || 'cn') !== realm) return false;
      if (groupFilter !== 'all' && String(a.group_id || 0) !== groupFilter) return false;
      if (statusFilter !== 'all' && a.status !== statusFilter) return false;
      if (q && !(a.name.toLowerCase().includes(q) || a.uid.toLowerCase().includes(q))) return false;
      return true;
    });
  }, [accounts, realm, groupFilter, statusFilter, query]);

  async function toggleStatus(a: Account) {
    setBusyId(a.id);
    try {
      const next = a.status === 'active' ? 'disabled' : 'active';
      await accountApi.update(a.id, {status: next});
      notify.ok(t(next === 'active' ? 'accounts.enabled' : 'accounts.disabled', {name: a.name}));
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusyId(null);
    }
  }

  async function refreshBalance(a: Account) {
    setBusyId(a.id);
    try {
      const r = await accountApi.refresh(a.id);
      notify.ok(t('accounts.refreshed', {name: a.name}),
                t('accounts.balanceNow', {n: fmtNumber(r.balance_remain)}));
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusyId(null);
    }
  }

  async function syncAllCredits() {
    setSyncing(true);
    try {
      await accountApi.syncCredits();
      notify.ok(t('accounts.syncStarted'));
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setSyncing(false);
    }
  }

  async function doDelete() {
    if (!deleteOf) return;
    try {
      await accountApi.remove(deleteOf.id);
      notify.ok(t('accounts.deleted', {name: deleteOf.name}));
      setDeleteOf(null);
      await load();
    } catch (e) {
      notify.err(errText(e));
    }
  }

  if (error) return <LoadError message={error} onRetry={load} />;
  if (!accounts) return <SkeletonBar className="h-96" />;

  const groupName = (gid: number) => groups.find((g) => g.id === gid)?.name || '';

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t('accounts.title')}
        description={t('accounts.subtitle', {n: filtered.length, realm})}
        actions={
          <>
            <Button variant="outline" size="sm" onClick={syncAllCredits} disabled={syncing}>
              <RefreshCw className={cn('h-4 w-4', syncing && 'animate-spin')} />
              {t('accounts.syncCredits')}
            </Button>
            <Button size="sm" onClick={() => setAddOpen(true)}>
              <Plus className="h-4 w-4" />
              {t('accounts.add')}
            </Button>
          </>
        }
      />

      {/* 过滤行 */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-[200px] flex-1">
          <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('accounts.searchPlaceholder')}
            className="pl-8"
          />
        </div>
        <Select value={groupFilter} onValueChange={setGroupFilter}>
          <SelectTrigger className="w-[150px]"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{t('accounts.allGroups')}</SelectItem>
            <SelectItem value="0">{t('accounts.ungrouped')}</SelectItem>
            {groups.map((g) => (
              <SelectItem key={g.id} value={String(g.id)}>{g.name}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={statusFilter} onValueChange={setStatusFilter}>
          <SelectTrigger className="w-[130px]"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{t('accounts.allStatus')}</SelectItem>
            <SelectItem value="active">{t('accounts.statusActive')}</SelectItem>
            <SelectItem value="disabled">{t('accounts.statusDisabled')}</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {filtered.length === 0 ? (
        <EmptyState title={t('accounts.empty')} />
      ) : (
        <div className="rounded-xl border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t('accounts.colName')}</TableHead>
                <TableHead>{t('accounts.colGroup')}</TableHead>
                <TableHead className="text-right">{t('accounts.colBalance')}</TableHead>
                <TableHead>{t('accounts.colStatus')}</TableHead>
                <TableHead>{t('accounts.colLastUsed')}</TableHead>
                <TableHead className="text-right">{t('accounts.colActions')}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filtered.map((a) => (
                <TableRow key={a.id}>
                  <TableCell>
                    <div className="flex flex-col">
                      <span className="font-medium">{a.name || a.uid}</span>
                      <span className="font-mono text-[11px] text-muted-foreground">{a.uid}</span>
                    </div>
                  </TableCell>
                  <TableCell>
                    {a.group_id ? (
                      <Badge variant="secondary">{groupName(a.group_id)}</Badge>
                    ) : (
                      <span className="text-xs text-muted-foreground">{t('accounts.ungrouped')}</span>
                    )}
                  </TableCell>
                  <TableCell className="text-right font-mono">
                    {a.balance_remain == null ? '—' : fmtNumber(a.balance_remain)}
                  </TableCell>
                  <TableCell>
                    <AccountStateBadges a={a} />
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {fmtIso(a.last_used_at)}
                  </TableCell>
                  <TableCell className="text-right">
                    <div className="flex items-center justify-end gap-1">
                      <Button variant="ghost" size="sm" title={t('accounts.creditDetails')}
                              onClick={() => setCreditOf(a)}>
                        <Coins className="h-4 w-4" />
                      </Button>
                      <Button variant="ghost" size="sm" title={t('accounts.refreshBalance')}
                              disabled={busyId === a.id} onClick={() => refreshBalance(a)}>
                        <RefreshCw className={cn('h-4 w-4', busyId === a.id && 'animate-spin')} />
                      </Button>
                      <Button variant="ghost" size="sm"
                              title={a.status === 'active' ? t('accounts.disable') : t('accounts.enable')}
                              disabled={busyId === a.id} onClick={() => toggleStatus(a)}>
                        <ShieldCheck className="h-4 w-4" />
                      </Button>
                      <Button variant="ghost" size="sm" title={t('accounts.delete')}
                              onClick={() => setDeleteOf(a)}>
                        <Trash2 className="h-4 w-4 text-red-500" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <AddAccountDialog open={addOpen} onClose={() => setAddOpen(false)}
                        onDone={() => { setAddOpen(false); load(); }} />
      <CreditDetailsDialog account={creditOf} onClose={() => setCreditOf(null)} />

      {/* 删除确认（行内按钮触发，受控 open） */}
      <AlertDialog open={!!deleteOf} onOpenChange={(v: boolean) => !v && setDeleteOf(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('accounts.deleteTitle')}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('accounts.deleteConfirm', {name: deleteOf?.name || ''})}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction className="bg-destructive text-white hover:bg-destructive/90"
                               onClick={(e) => { e.preventDefault(); doDelete(); }}>
              {t('accounts.delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

/* ── 账号状态徽章：把 cooling/breaker/degraded/限流显著标出，人工判读封号风险 ──
 *
 * 你刚被封过一个号——所以「账号当前是不是在异常状态」必须一眼可见，
 * 而不是藏在 status=active 后面。本引擎网关的分层状态（_update_account_state）：
 *   ready    → 正常（绿）
 *   cooling  → 限流/瞬时故障冷却中（黄，多数可自愈，悬停看真实原因）
 *   breaker  → 连败熔断（橙，持续坏，建议人工看一眼）
 *   degraded → 连败降权（橙）
 *   disabled → 已停用（红；401/403 被上游拒会立即到这，需重新登录）
 * 另有模型级限流（model_limits）单独标。悬停统一显示 last_err_msg（真实原因）。 */
function AccountStateBadges({a}: {a: Account}) {
  const t = useT();
  const reason = a.last_err_msg || '';
  const tip = reason ? `${a.gateway_reason || a.gateway_state || ''}：${reason}` : undefined;

  const badges: {key: string; label: string; cls: string; icon?: React.ReactNode}[] = [];
  if (a.status !== 'active') {
    badges.push({key: 'disabled', label: t('accounts.statusDisabled'),
                 cls: 'border-red-300 bg-red-50 text-red-600 dark:bg-red-950/40 dark:text-red-400'});
  } else {
    switch (a.gateway_state) {
      case 'cooling':
        badges.push({key: 'cooling', label: t('accounts.stateCooling'),
                     cls: 'border-amber-300 bg-amber-50 text-amber-600 dark:bg-amber-950/40 dark:text-amber-400',
                     icon: <Timer className="h-3 w-3" />});
        break;
      case 'ready':
      default:
        badges.push({key: 'ready', label: t('accounts.statusActive'),
                     cls: 'border-emerald-300 bg-emerald-50 text-emerald-600 dark:bg-emerald-950/40 dark:text-emerald-400'});
        break;
    }
    // breaker/degraded 与 cooling 可叠加（gateway_state 取最长那个），单独补标
    if (a.gateway_reason === 'breaker' || a.gateway_reason === 'degraded') {
      badges.push({key: 'risk', label: t('accounts.stateRisk'),
                   cls: 'border-orange-300 bg-orange-50 text-orange-600 dark:bg-orange-950/40 dark:text-orange-400',
                   icon: <Flame className="h-3 w-3" />});
    }
  }
  if ((a.model_limits?.length ?? 0) > 0) {
    badges.push({key: 'model', label: t('accounts.stateModelLimited', {n: a.model_limits!.length}),
                 cls: 'border-violet-300 bg-violet-50 text-violet-600 dark:bg-violet-950/40 dark:text-violet-400',
                 icon: <AlertTriangle className="h-3 w-3" />});
  }

  const body = (
    <div className="flex flex-wrap items-center gap-1">
      {badges.map((b) => (
        <Badge key={b.key} variant="outline"
               className={cn('gap-1 border font-normal', b.cls)}>
          {b.icon}{b.label}
        </Badge>
      ))}
    </div>
  );

  if (!tip) return body;
  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>{body}</TooltipTrigger>
        <TooltipContent className="max-w-xs text-xs">{tip}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

/* ── 添加账号（auth_json 凭证，服务端真验证）───────────────────── */
function AddAccountDialog({open, onClose, onDone}: {
  open: boolean; onClose: () => void; onDone: () => void;
}) {
  const t = useT();
  const [name, setName] = useState('');
  const [authJson, setAuthJson] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (!authJson.trim()) {
      notify.warn(t('accounts.authJsonRequired'));
      return;
    }
    setBusy(true);
    try {
      const r = await accountApi.create({auth_json: authJson.trim(), name: name.trim() || undefined});
      if (r.duplicated) notify.info(t('accounts.addDuplicated'), r.message);
      else notify.ok(t('accounts.addOk', {name: r.name}),
                     r.balance_remain != null ? t('accounts.balanceNow', {n: fmtNumber(r.balance_remain)}) : undefined);
      setName(''); setAuthJson('');
      onDone();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v: boolean) => !v && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>{t('accounts.addTitle')}</DialogTitle></DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="space-y-1.5">
            <Label>{t('accounts.nameOptional')}</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)}
                   placeholder={t('accounts.namePlaceholder')} />
          </div>
          <div className="space-y-1.5">
            <Label>{t('accounts.authJson')}</Label>
            <Textarea value={authJson} onChange={(e) => setAuthJson(e.target.value)}
                      placeholder='{"accessToken":"...","refreshToken":"...",...}'
                      rows={7} className="font-mono text-xs" />
            <p className="text-[11px] text-muted-foreground">{t('accounts.authJsonHint')}</p>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel')}</Button>
          <Button onClick={submit} disabled={busy}>
            {busy ? t('common.submitting') : t('accounts.addSubmit')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ── 积分明细弹层（credit-details，库内快照优先，可重新采集）────── */
function CreditDetailsDialog({account, onClose}: {
  account: Account | null; onClose: () => void;
}) {
  const t = useT();
  const [data, setData] = useState<CreditDetails | null>(null);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (refresh: boolean) => {
    if (!account) return;
    setErr(''); setBusy(true);
    try {
      setData(await accountApi.creditDetails(account.id, refresh));
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy(false);
    }
  }, [account]);

  useEffect(() => {
    if (account) { setData(null); load(false); }
  }, [account, load]);

  const resources = data?.credits?.all_resources ?? [];

  return (
    <Dialog open={!!account} onOpenChange={(v: boolean) => !v && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t('accounts.creditTitle', {name: account?.name || ''})}</DialogTitle>
        </DialogHeader>
        {err ? (
          <p className="text-sm text-red-500">{err}</p>
        ) : !data ? (
          <SkeletonBar className="h-48" />
        ) : resources.length === 0 ? (
          <EmptyState title={t('accounts.creditEmpty')} />
        ) : (
          <div className="max-h-[50vh] overflow-y-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t('accounts.colPackage')}</TableHead>
                  <TableHead className="text-right">{t('accounts.colRemaining')}</TableHead>
                  <TableHead className="text-right">{t('accounts.colTotal')}</TableHead>
                  <TableHead>{t('accounts.colExpire')}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {resources.map((r) => (
                  <TableRow key={r.packageCode}>
                    <TableCell className="text-sm">{r.displayName || r.packageName}</TableCell>
                    <TableCell className="text-right font-mono">{fmtNumber(r.remaining)}</TableCell>
                    <TableCell className="text-right font-mono text-muted-foreground">{fmtNumber(r.total)}</TableCell>
                    <TableCell>
                      <span className={cn('text-xs',
                        r.expired ? 'text-red-500' : r.expiringSoon ? 'text-amber-500' : 'text-muted-foreground')}>
                        {r.expireLabel || r.expireDate || '—'}
                      </span>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => load(true)} disabled={busy}>
            <RefreshCw className={cn('h-4 w-4', busy && 'animate-spin')} />
            {t('accounts.creditRefresh')}
          </Button>
          <Button onClick={onClose}>{t('common.close')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
