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
import type {Account, AccountGroup, CreditDetails, ModelLimitRow, ScanLocalResult} from '@/lib/types';
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
import {AlertTriangle, Cat, Coins, Download, Flame, Plus, RefreshCw, Search, ShieldCheck, Timer, Trash2, Wrench} from 'lucide-react';

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
  const [importOpen, setImportOpen] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [creditOf, setCreditOf] = useState<Account | null>(null);
  const [deleteOf, setDeleteOf] = useState<Account | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [catId, setCatId] = useState<number | null>(null);
  const [catBatchOpen, setCatBatchOpen] = useState(false);
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

  /** 猫猫旅行：同意协议 → 首次领养(+300) → 派出 → 领奖（会真实触达上游）。
   *  返回结构化分步结果，直接拼成人话摘要，用户不必去积分明细核对。 */
  async function catTravel(a: Account) {
    setCatId(a.id);
    try {
      const r = await accountApi.catTravel(a.id) as {
        adopted?: boolean; credits?: number; detail?: string;
        steps?: {step?: string; ok?: boolean; credits?: number}[];
      };
      const parts: string[] = [];
      if (r.adopted) parts.push(t('accounts.catTravelAdopted', {n: 300}));
      if (r.credits) parts.push(t('accounts.catTravelCredits', {n: r.credits}));
      if (r.detail) parts.push(String(r.detail));
      if (!parts.length && Array.isArray(r.steps)) {
        for (const s of r.steps) {
          if (s?.step) parts.push(`${s.step}${s.ok === false ? '✗' : '✓'}`);
        }
      }
      notify.ok(t('accounts.catTravelOk', {detail: parts.join(' · ') || '—'}));
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setCatId(null);
    }
  }

  /** 批量猫猫旅行：对当前筛选出的启用账号逐个执行（顺序执行，避免上游风控）。 */
  async function catTravelBatch() {
    const targets = filtered.filter((a) => a.status === 'active');
    setCatBatchOpen(false);
    if (!targets.length) return;
    let ok = 0, fail = 0;
    for (const a of targets) {
      setCatId(a.id);
      try {
        await accountApi.catTravel(a.id);
        ok += 1;
      } catch {
        fail += 1;
      }
    }
    setCatId(null);
    notify.ok(t('accounts.catTravelBatchStarted', {n: targets.length}),
              `${ok} OK / ${fail} failed`);
    await load();
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
            <Button variant="outline" size="sm" onClick={() => setImportOpen(true)}>
              <Download className="h-4 w-4" />
              {t('accounts.importLocal')}
            </Button>
            <Button variant="outline" size="sm" onClick={() => setToolsOpen(true)}>
              <Wrench className="h-4 w-4" />
              {t('accounts.tools')}
            </Button>
            <Button variant="outline" size="sm" onClick={() => setCatBatchOpen(true)}
                    disabled={!!catId || !filtered.some((a) => a.status === 'active')}>
              <Cat className={cn('h-4 w-4', catId !== null && 'animate-bounce')} />
              {t('accounts.catTravelBatch')}
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
        <div className="rounded-xl border bg-card overflow-x-auto">
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
                      <Button variant="ghost" size="sm" title={t('accounts.catTravelHint')}
                              disabled={catId === a.id || a.status !== 'active'}
                              onClick={() => catTravel(a)}>
                        <Cat className={cn('h-4 w-4', catId === a.id && 'animate-bounce')} />
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
      <ImportLocalDialog open={importOpen} onClose={() => setImportOpen(false)}
                         onDone={() => { setImportOpen(false); load(); }} />
      <ToolsDialog open={toolsOpen} onClose={() => setToolsOpen(false)}
                   onChanged={load} />

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

      {/* 批量猫猫旅行确认（会真实触达上游，必须显式确认） */}
      <AlertDialog open={catBatchOpen} onOpenChange={setCatBatchOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('accounts.catTravelBatch')}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('accounts.catTravelBatchConfirm',
                 {n: filtered.filter((a) => a.status === 'active').length})}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction onClick={(e) => { e.preventDefault(); catTravelBatch(); }}>
              {t('accounts.catTravel')}
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
          <div className="max-h-[50vh] overflow-x-auto overflow-y-auto">
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

/* ── 本机扫描导入（scan-local + import-local，服务端读不暴露 token）────── */
function ImportLocalDialog({open, onClose, onDone}: {
  open: boolean; onClose: () => void; onDone: () => void;
}) {
  const t = useT();
  const [data, setData] = useState<ScanLocalResult | null>(null);
  const [err, setErr] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [scanning, setScanning] = useState(false);

  const scan = useCallback(async () => {
    setErr(''); setScanning(true);
    try {
      const r = await accountApi.scanLocal();
      setData(r);
      setSelected(new Set(r.items.map((i) => i.file)));   // 默认全选
    } catch (e) {
      setErr(errText(e));
    } finally {
      setScanning(false);
    }
  }, []);

  useEffect(() => {
    if (open) { setData(null); setSelected(new Set()); scan(); }
  }, [open, scan]);

  function toggle(file: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(file)) next.delete(file); else next.add(file);
      return next;
    });
  }

  async function doImport() {
    if (selected.size === 0) { notify.warn(t('accounts.importNone')); return; }
    setBusy(true);
    try {
      const all = data ? selected.size === data.items.length : false;
      const r = await accountApi.importLocal(Array.from(selected), all);
      if (r.errors.length > 0) {
        notify.warn(t('accounts.importPartial', {added: r.added, skipped: r.skipped}),
                    r.errors.slice(0, 3).join('；'));
      } else {
        notify.ok(t('accounts.importOk', {added: r.added, skipped: r.skipped}));
      }
      onDone();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v: boolean) => !v && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader><DialogTitle>{t('accounts.importTitle')}</DialogTitle></DialogHeader>
        {err ? (
          <p className="text-sm text-red-500">{err}</p>
        ) : !data || scanning ? (
          <SkeletonBar className="h-48" />
        ) : !data.exists ? (
          <EmptyState title={t('accounts.importNoDir')} />
        ) : data.items.length === 0 ? (
          <EmptyState title={t('accounts.importEmpty')} />
        ) : (
          <>
            <p className="text-xs text-muted-foreground">
              {t('accounts.importDir', {dir: data.dir, n: data.items.length})}
            </p>
            <div className="max-h-[46vh] overflow-x-auto overflow-y-auto rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-8" />
                    <TableHead>{t('accounts.colName')}</TableHead>
                    <TableHead>{t('accounts.importColUid')}</TableHead>
                    <TableHead>{t('accounts.importColExpire')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.items.map((it) => (
                    <TableRow key={it.file} className="cursor-pointer"
                              onClick={() => toggle(it.file)}>
                      <TableCell>
                        <input type="checkbox" checked={selected.has(it.file)}
                               onChange={() => toggle(it.file)}
                               onClick={(e) => e.stopPropagation()}
                               className="h-4 w-4 accent-primary" />
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-1.5">
                          <span className="text-sm">{it.nickname || it.uid || it.file}</span>
                          {it.is_active && (
                            <Badge variant="secondary" className="text-[10px]">
                              {t('accounts.importActive')}
                            </Badge>
                          )}
                        </div>
                      </TableCell>
                      <TableCell className="font-mono text-[11px] text-muted-foreground">{it.uid}</TableCell>
                      <TableCell className="text-xs text-muted-foreground">{it.expires_at || '—'}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel')}</Button>
          <Button onClick={doImport} disabled={busy || !data || data.items.length === 0}>
            {busy ? t('common.submitting') : t('accounts.importSubmit', {n: selected.size})}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ── 工具（去重 + 模型限流台账 + 导出全池）────────────────────────── */
function ToolsDialog({open, onClose, onChanged}: {
  open: boolean; onClose: () => void; onChanged: () => void;
}) {
  const t = useT();
  const [limits, setLimits] = useState<ModelLimitRow[] | null>(null);
  const [dup, setDup] = useState<{redundant: number; unique: number} | null>(null);
  const [busy, setBusy] = useState('');

  const load = useCallback(async () => {
    try {
      const [limRes, dupRes] = await Promise.all([
        accountApi.modelLimits(),
        accountApi.duplicates(),
      ]);
      setLimits(limRes.items);
      setDup({redundant: dupRes.redundant, unique: dupRes.unique});
    } catch (e) {
      notify.err(errText(e));
    }
  }, []);

  useEffect(() => {
    if (open) { setLimits(null); setDup(null); load(); }
  }, [open, load]);

  async function doDedupe() {
    setBusy('dedupe');
    try {
      const r = await accountApi.dedupe();
      notify.ok(t('accounts.dedupeOk', {removed: r.removed, kept: r.kept}));
      await load();
      onChanged();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }

  async function clearLimit(accountId: number) {
    setBusy(`clear-${accountId}`);
    try {
      await accountApi.clearModelLimit(accountId);
      notify.ok(t('accounts.limitCleared'));
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }

  /**
   * 一键解除该账号的本地冷却/熔断/降权。
   *
   * 只清本地推断的状态；`session_dead_fails`（上游 401/403 的权威信号）与
   * `status`（人工停用决策）**不会**被动到 —— 后端已保证这点。
   *
   * 提示语按 `cleared` 如实反馈：没清到东西时明确说「无需解除」，
   * 不弹「已重置」（那会让用户误以为发生了什么）。
   */
  async function clearCooldown(accountId: number) {
    setBusy(`cool-${accountId}`);
    try {
      const r = await accountApi.clearCooldown(accountId);
      if (r.cleared.length) {
        notify.ok(t('accounts.cooldownCleared', {items: r.cleared.join(', ')}));
      } else {
        notify.ok(t('accounts.cooldownNone'));
      }
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }

  async function doExport() {
    setBusy('export');
    try {
      const blob = await accountApi.exportAll();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `workbuddy-accounts-${Date.now()}.json`;
      a.click();
      URL.revokeObjectURL(url);
      notify.ok(t('accounts.exportOk'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v: boolean) => !v && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader><DialogTitle>{t('accounts.toolsTitle')}</DialogTitle></DialogHeader>
        <div className="flex flex-col gap-5">
          {/* 去重 */}
          <section className="flex items-center justify-between gap-3 rounded-lg border p-3">
            <div>
              <p className="text-sm font-medium">{t('accounts.dedupeTitle')}</p>
              <p className="text-xs text-muted-foreground">
                {dup == null
                  ? t('common.loading')
                  : dup.redundant > 0
                    ? t('accounts.dedupeFound', {redundant: dup.redundant, unique: dup.unique})
                    : t('accounts.dedupeClean')}
              </p>
            </div>
            <Button variant="outline" size="sm" onClick={doDedupe}
                    disabled={busy === 'dedupe' || !dup || dup.redundant === 0}>
              {t('accounts.dedupeRun')}
            </Button>
          </section>

          {/* 模型限流台账 */}
          <section className="rounded-lg border p-3">
            <div className="mb-2 flex items-center justify-between">
              <p className="text-sm font-medium">{t('accounts.limitsTitle')}</p>
              <Badge variant="secondary">{limits?.length ?? 0}</Badge>
            </div>
            {limits == null ? (
              <SkeletonBar className="h-24" />
            ) : limits.length === 0 ? (
              <p className="py-2 text-xs text-muted-foreground">{t('accounts.limitsEmpty')}</p>
            ) : (
              <div className="max-h-[32vh] overflow-x-auto overflow-y-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>{t('accounts.colName')}</TableHead>
                      <TableHead>{t('accounts.colModel')}</TableHead>
                      <TableHead className="text-right">{t('accounts.colActions')}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {limits.map((l, i) => (
                      <TableRow key={`${l.account_id}-${l.model}-${i}`}>
                        <TableCell className="text-sm">{l.account_name || `#${l.account_id}`}</TableCell>
                        <TableCell className="font-mono text-xs">{l.model}</TableCell>
                        <TableCell className="text-right">
                          <div className="flex flex-wrap justify-end gap-1">
                            <Button variant="ghost" size="sm"
                                    disabled={busy === `clear-${l.account_id}`}
                                    onClick={() => clearLimit(l.account_id)}>
                              {t('accounts.limitClear')}
                            </Button>
                            {/* 本地冷却/熔断/降权的一键解除。
                                与「模型级限流」是两个维度：那个清的是
                                AccountModelCooldown 行，这个清的是账号上的
                                cool_until/breaker/degrade。两者都可能让账号
                                看起来「不可用」，所以放在一起便于判断。 */}
                            <Button variant="ghost" size="sm"
                                    disabled={busy === `cool-${l.account_id}`}
                                    onClick={() => clearCooldown(l.account_id)}>
                              {t('accounts.cooldownClear')}
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </section>

          {/* 导出全池 */}
          <section className="flex items-center justify-between gap-3 rounded-lg border p-3">
            <div>
              <p className="text-sm font-medium">{t('accounts.exportTitle')}</p>
              <p className="text-xs text-muted-foreground">{t('accounts.exportHint')}</p>
            </div>
            <Button variant="outline" size="sm" onClick={doExport} disabled={busy === 'export'}>
              <Download className="h-4 w-4" />
              {t('accounts.exportRun')}
            </Button>
          </section>
        </div>
        <DialogFooter>
          <Button onClick={onClose}>{t('common.close')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
