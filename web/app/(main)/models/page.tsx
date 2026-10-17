'use client';

/**
 * 模型管理（Wave B4）。
 *
 * 本引擎模型是「网关白名单 + 积分倍率」双轴：每个模型可启用/禁用、记录
 * credit_multiplier（倍率，0=免费）；模型分组（ModelGroup）绑定到 API 密钥
 * 限定该 key 可用哪些模型（keys 页已引用）。测速用账号池直发上游一次流式
 * 请求（免 API Key，真实但只占用一个账号、不改额度）。
 * 对齐 routers/models.py + routers/groups.py。
 */
import {useCallback, useEffect, useMemo, useState} from 'react';
import {modelApi, errText} from '@/lib/api';
import type {ModelConfig, ModelGroup, ModelBusy, SpeedTestResult} from '@/lib/types';
import {useRealm} from '@/lib/realm-context';
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
import {Tabs, TabsList, TabsTrigger, TabsContent} from '@/components/ui/tabs';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from '@/components/animate-ui/radix/dialog';
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table';
import {
  Tooltip, TooltipContent, TooltipProvider, TooltipTrigger,
} from '@/components/ui/tooltip';
import {
  Activity, Gauge, Layers, Loader2, Pencil, Plus, RefreshCw, Search, Trash2,
} from 'lucide-react';

export default function ModelsPage() {
  const t = useT();
  // 模型清单按版本隔离（国际版条目带 `global:` 前缀）。同步时必须把当前版本
  // 传给后端 —— 否则会拿国内版账号去拉国际版的清单。
  const {realm, label: realmLabel} = useRealm();
  const [configs, setConfigs] = useState<ModelConfig[] | null>(null);
  const [groups, setGroups] = useState<ModelGroup[]>([]);
  const [busy, setBusy] = useState<Record<string, ModelBusy>>({});
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [syncing, setSyncing] = useState(false);
  // 排序方式：默认按模型名（原行为）；可切按倍率（升序，免费在前）。
  // 按价格筛模型/建分组是高频场景，没有排序只能肉眼扫全表。
  const [sortBy, setSortBy] = useState<'model' | 'mult'>('model');
  const [speedOf, setSpeedOf] = useState<ModelConfig | null>(null);
  const [groupEdit, setGroupEdit] = useState<ModelGroup | 'new' | null>(null);
  const [groupDelete, setGroupDelete] = useState<ModelGroup | null>(null);

  const load = useCallback(async () => {
    setError('');
    try {
      const [cRes, gRes, bRes] = await Promise.all([
        // **必须传 realm**：后端按 realm 过滤模型清单。
        // 不传会拿到两版混合的大杂烩（交集只有 5 个），
        // 切到国际版后仍看到国内版模型 —— 用户实测报告过。
        modelApi.configs(undefined, realm),
        modelApi.groups(),
        modelApi.busy(realm).catch(() => null),
      ]);
      setConfigs(cRes.items);
      setGroups(gRes.items);
      if (bRes) {
        const m: Record<string, ModelBusy> = {};
        bRes.items.forEach((it) => { m[it.model_id] = it; });
        setBusy(m);
      }
    } catch (e) {
      setError(errText(e));
    }
    // realm 变化时必须重新加载（切换版本后看到对应版本的模型）
  }, [realm]);

  useEffect(() => { load(); }, [load]);

  const filtered = useMemo(() => {
    if (!configs) return [];
    const q = query.trim().toLowerCase();
    let arr = configs;
    if (q) {
      arr = arr.filter((c) =>
        c.model_id.toLowerCase().includes(q) || (c.note || '').toLowerCase().includes(q));
    }
    if (sortBy === 'mult') {
      arr = [...arr].sort((a, b) => (a.credit_multiplier || 0) - (b.credit_multiplier || 0)
                                   || a.model_id.localeCompare(b.model_id));
    }
    return arr;
  }, [configs, query, sortBy]);

  const enabledCount = useMemo(() => (configs || []).filter((c) => c.enabled).length, [configs]);

  async function toggle(c: ModelConfig) {
    try {
      await modelApi.toggle(c.id, !c.enabled);
      setConfigs((prev) => prev && prev.map((x) => x.id === c.id ? {...x, enabled: !c.enabled} : x));
    } catch (e) { notify.err(errText(e)); }
  }

  async function batchSet(enabled: boolean) {
    if (selected.size === 0) return;
    try {
      const r = await modelApi.batchToggle(Array.from(selected), enabled);
      notify.ok(t(enabled ? 'models.batchEnabled' : 'models.batchDisabled', {n: r.updated}));
      setSelected(new Set());
      await load();
    } catch (e) { notify.err(errText(e)); }
  }

  async function doSync() {
    setSyncing(true);
    try {
      // **必须带上 realm**：不同版本要从对应版本的上游拉清单
      // （国内版 copilot.tencent.com / 国际版 www.workbuddy.ai）。
      // 不传的话后端只能按 id 挑第一个活跃账号 —— 切到国际版时依然会拿国内版
      // 账号去拉、把国内版清单写进库（用户实测报过这个现象）。
      const r = await modelApi.sync(realm);
      // 提示里带版本 + 源账号：同步的正确性过去出过好几次「看起来同步了
      // 但拿到的是另一版清单」的问题，用户无从分辨 —— 提示必须能自证清白。
      notify.ok(t('models.syncedWithRealm', {
        realm: r.realm || realm, added: r.added, updated: r.updated,
        acc: r.source_account_id ?? '?'}));
      await load();
    } catch (e) { notify.err(errText(e)); }
    finally { setSyncing(false); }
  }

  function toggleSelect(mid: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(mid)) next.delete(mid); else next.add(mid);
      return next;
    });
  }

  if (error) return <LoadError message={error} onRetry={load} />;
  if (!configs) return <SkeletonBar className="h-96" />;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t('models.title')}
        description={t('models.subtitle', {n: configs.length, enabled: enabledCount})}
        actions={
          <div className="flex items-center gap-2">
            {/* 倍率排序切换：按价格找模型是高频场景 */}
            <Button size="sm" variant="outline" onClick={() => setSortBy((s) => s === 'mult' ? 'model' : 'mult')}
                    title={t('models.sortByMultiplier')}>
              {sortBy === 'mult' ? t('models.sortByMultiplier') : t('models.sortByModel')}
            </Button>
            <Button size="sm" onClick={doSync} disabled={syncing}>
              {syncing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              {/* 按钮上标明当前版本：两版上游不同，同步结果只对当前版本生效，
                  不标的话用户点了国际版却不知道拉的是哪版（用户实测反馈过）。 */}
              {t('models.sync')} · {realmLabel}
            </Button>
          </div>
        }
      />

      <Tabs defaultValue="models">
        <TabsList>
          <TabsTrigger value="models"><Activity className="mr-1.5 h-4 w-4" />{t('models.tabModels')}</TabsTrigger>
          <TabsTrigger value="groups"><Layers className="mr-1.5 h-4 w-4" />{t('models.tabGroups', {n: groups.length})}</TabsTrigger>
        </TabsList>

        {/* ── 模型列表 ─────────────────────────────────────────── */}
        <TabsContent value="models" className="mt-4 flex flex-col gap-3">
          <div className="flex items-center gap-2">
            <div className="relative max-w-sm flex-1">
              <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input value={query} onChange={(e) => setQuery(e.target.value)}
                     placeholder={t('models.searchPlaceholder')} className="pl-8" />
            </div>
            {selected.size > 0 && (
              <>
                <span className="text-xs text-muted-foreground">{t('models.selected', {n: selected.size})}</span>
                <Button size="sm" variant="outline" onClick={() => batchSet(true)}>{t('models.enableSelected')}</Button>
                <Button size="sm" variant="outline" onClick={() => batchSet(false)}>{t('models.disableSelected')}</Button>
              </>
            )}
          </div>

          {filtered.length === 0 ? (
            <EmptyState title={t('models.empty')} />
          ) : (
            <div className="rounded-xl border bg-card overflow-x-auto">
          <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-8" />
                    <TableHead>{t('models.colModel')}</TableHead>
                    <TableHead>{t('models.colMultiplier')}</TableHead>
                    <TableHead>{t('models.colBusy')}</TableHead>
                    <TableHead>{t('models.colEnabled')}</TableHead>
                    <TableHead className="text-right">{t('models.colActions')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filtered.map((c) => (
                    <ModelRow key={c.id} c={c} busy={busy[c.model_id]}
                              selected={selected.has(c.model_id)}
                              onSelect={() => toggleSelect(c.model_id)}
                              onToggle={() => toggle(c)}
                              onSpeed={() => setSpeedOf(c)} />
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </TabsContent>

        {/* ── 模型分组 ─────────────────────────────────────────── */}
        <TabsContent value="groups" className="mt-4 flex flex-col gap-3">
          <div className="flex justify-end">
            <Button size="sm" onClick={() => setGroupEdit('new')}>
              <Plus className="h-4 w-4" />{t('models.addGroup')}
            </Button>
          </div>
          {groups.length === 0 ? (
            <EmptyState title={t('models.noGroups')} />
          ) : (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              {groups.map((g) => (
                <div key={g.id} className="rounded-xl border bg-card p-4">
                  <div className="flex items-start justify-between">
                    <div>
                      <div className="flex items-center gap-2">
                        <span className="font-medium">{g.name}</span>
                        <Badge variant="secondary" className="text-[10px]">{t('models.groupModels', {n: g.model_count})}</Badge>
                        <Badge variant="outline" className="text-[10px]">{t('models.groupKeys', {n: g.key_count})}</Badge>
                      </div>
                      {g.note && <p className="mt-1 text-xs text-muted-foreground">{g.note}</p>}
                    </div>
                    <div className="flex gap-1">
                      <Button variant="ghost" size="sm" onClick={() => setGroupEdit(g)}><Pencil className="h-4 w-4" /></Button>
                      <Button variant="ghost" size="sm" onClick={() => setGroupDelete(g)}><Trash2 className="h-4 w-4 text-red-500" /></Button>
                    </div>
                  </div>
                  <div className="mt-3 flex max-h-20 flex-wrap gap-1 overflow-y-auto">
                    {g.models.map((m) => <Badge key={m} variant="outline" className="font-mono text-[10px]">{m}</Badge>)}
                  </div>
                </div>
              ))}
            </div>
          )}
        </TabsContent>
      </Tabs>

      <SpeedTestDialog target={speedOf} onClose={() => setSpeedOf(null)} />
      <GroupEditDialog target={groupEdit} allModels={configs}
                       onClose={() => setGroupEdit(null)} onSaved={() => { setGroupEdit(null); load(); }} />
      <GroupDeleteDialog target={groupDelete} onClose={() => setGroupDelete(null)}
                         onDeleted={() => { setGroupDelete(null); load(); }} />
    </div>
  );
}

/* ── 模型行 ──────────────────────────────────────────────────────── */
function ModelRow({c, busy, selected, onSelect, onToggle, onSpeed}: {
  c: ModelConfig; busy?: ModelBusy; selected: boolean;
  onSelect: () => void; onToggle: () => void; onSpeed: () => void;
}) {
  const t = useT();
  const busyTone = !busy ? '' :
    busy.busy_level >= 2 ? 'border-red-300 text-red-600' :
    busy.busy_level === 1 ? 'border-amber-300 text-amber-600' :
    'border-emerald-300 text-emerald-600';
  return (
    <TableRow className={cn(!c.enabled && 'opacity-55')}>
      <TableCell>
        <input type="checkbox" checked={selected} onChange={onSelect}
               className="h-4 w-4 cursor-pointer accent-primary" />
      </TableCell>
      <TableCell>
        <div className="flex flex-col">
          <span className="font-mono text-[13px]">{c.model_id}</span>
          {c.note && c.note !== c.model_id && (
            <span className="text-[11px] text-muted-foreground">{c.note}</span>
          )}
        </div>
      </TableCell>
      <TableCell>
        {c.is_free ? (
          <Badge variant="outline" className="border-emerald-300 text-emerald-600">{t('models.free')}</Badge>
        ) : (
          <Badge variant="outline" className="border-violet-300 text-violet-600">x{c.credit_multiplier}</Badge>
        )}
      </TableCell>
      <TableCell>
        {busy ? (
          <Badge variant="outline" className={cn('text-[10px]', busyTone)}>{busy.busy_label}</Badge>
        ) : <span className="text-xs text-muted-foreground">—</span>}
      </TableCell>
      <TableCell>
        <Switch checked={c.enabled} onCheckedChange={onToggle} />
      </TableCell>
      <TableCell className="text-right">
        <Button variant="ghost" size="sm" title={t('models.speedTest')} onClick={onSpeed}>
          <Gauge className="h-4 w-4" />
        </Button>
      </TableCell>
    </TableRow>
  );
}

/* ── 测速弹层（真实上游请求，需等待）────────────────────────────────── */
function SpeedTestDialog({target, onClose}: {target: ModelConfig | null; onClose: () => void}) {
  const t = useT();
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<SpeedTestResult | null>(null);
  const [err, setErr] = useState('');

  useEffect(() => {
    if (!target) { setResult(null); setErr(''); return; }
  }, [target]);

  async function run() {
    if (!target) return;
    setRunning(true); setErr(''); setResult(null);
    try {
      const r = await modelApi.speedTest(target.model_id);
      setResult(r);
    } catch (e) { setErr(errText(e)); }
    finally { setRunning(false); }
  }

  return (
    <Dialog open={!!target} onOpenChange={(v: boolean) => !v && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>{t('models.speedTitle', {model: target?.model_id || ''})}</DialogTitle></DialogHeader>
        {!result && !running && !err && (
          <p className="text-sm text-muted-foreground">{t('models.speedHint')}</p>
        )}
        {running && (
          <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />{t('models.speedRunning')}
          </div>
        )}
        {err && <p className="text-sm text-red-500">{err}</p>}
        {result && (
          <div className="flex flex-col gap-3">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <StatBox label={t('models.ttft')} value={result.ttft_ms != null ? `${result.ttft_ms}ms` : '—'} />
              <StatBox label={t('models.genSpeed')} value={result.speed_tps != null ? `${result.speed_tps} t/s` : '—'} />
              <StatBox label={t('models.tokens')} value={result.completion_tokens != null ? String(result.completion_tokens) : '—'} />
              <StatBox label={t('models.totalDur')} value={`${(result.total_duration_ms / 1000).toFixed(1)}s`} />
            </div>
            {result.sample && (
              <div className="rounded-md border bg-muted p-3">
                <p className="text-[10px] uppercase tracking-wide text-muted-foreground">{t('models.sample')}</p>
                <p className="mt-1 whitespace-pre-wrap text-xs">{result.sample}</p>
              </div>
            )}
            <p className="text-[11px] text-muted-foreground">{t('models.speedAccount', {acc: result.account})}</p>
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.close')}</Button>
          <Button onClick={run} disabled={running}>
            {running ? t('models.speedRunning') : result ? t('models.speedAgain') : t('models.speedStart')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function StatBox({label, value}: {label: string; value: string}) {
  return (
    <div className="rounded-md border bg-card p-2 text-center">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-sm font-semibold">{value}</div>
    </div>
  );
}

/* ── 分组编辑（模型多选，全量覆盖语义）────────────────────────────────── */
function GroupEditDialog({target, allModels, onClose, onSaved}: {
  target: ModelGroup | 'new' | null; allModels: ModelConfig[];
  onClose: () => void; onSaved: () => void;
}) {
  const t = useT();
  const isNew = target === 'new';
  const editing: ModelGroup | null = target && target !== 'new' ? target : null;
  const [name, setName] = useState('');
  const [note, setNote] = useState('');
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [q, setQ] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (editing) {
      setName(editing.name); setNote(editing.note || '');
      setPicked(new Set(editing.models));
    } else {
      setName(''); setNote(''); setPicked(new Set());
    }
    setQ('');
  }, [target]);   // eslint-disable-line react-hooks/exhaustive-deps

  if (target === null) return null;

  // 显示积分倍率：按价格建分组是分组的主要场景之一，只给名字没法判断贵贱。
  // 搜索仍按名字匹配；列表按「免费 → 便宜 → 贵」排，免费模型天然排前面。
  const shown = allModels
    .filter((c) => !q || c.model_id.toLowerCase().includes(q.toLowerCase()))
    .sort((a, b) => (a.credit_multiplier || 0) - (b.credit_multiplier || 0)
                    || a.model_id.localeCompare(b.model_id));

  function togglePick(m: string) {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(m)) next.delete(m); else next.add(m);
      return next;
    });
  }

  async function submit() {
    if (!name.trim() || picked.size === 0) return;
    setBusy(true);
    try {
      if (isNew) {
        await modelApi.createGroup({name: name.trim(), note: note.trim(), models: Array.from(picked)});
        notify.ok(t('models.groupCreated'));
      } else if (editing) {
        await modelApi.updateGroup(editing.id, {name: name.trim(), note: note.trim(), models: Array.from(picked)});
        notify.ok(t('models.groupSaved'));
      }
      onSaved();
    } catch (e) { notify.err(errText(e)); }
    finally { setBusy(false); }
  }

  return (
    <Dialog open={target !== null} onOpenChange={(v: boolean) => !v && onClose()}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{isNew ? t('models.addGroupTitle') : t('models.editGroupTitle', {name: editing?.name || ''})}</DialogTitle>
        </DialogHeader>
        <div className="flex max-h-[62vh] flex-col gap-3 overflow-y-auto pr-1">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>{t('models.fGroupName')}</Label>
              <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={t('models.fGroupNamePh')} />
            </div>
            <div className="space-y-1.5">
              <Label>{t('models.fGroupNote')}</Label>
              <Input value={note} onChange={(e) => setNote(e.target.value)} />
            </div>
          </div>
          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <Label>{t('models.fGroupModels', {n: picked.size})}</Label>
              <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t('models.searchPlaceholder')}
                     className="h-7 w-44 text-xs" />
            </div>
            <div className="max-h-56 overflow-y-auto rounded-md border p-2">
              <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
                {shown.map((c) => (
                  <label key={c.model_id} className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 hover:bg-muted">
                    <input type="checkbox" checked={picked.has(c.model_id)} onChange={() => togglePick(c.model_id)}
                           className="h-3.5 w-3.5 accent-primary" />
                    <span className="font-mono text-xs">{c.model_id}</span>
                    {/* 积分倍率：0 = 免费（绿色「免费」角标），其余显示倍数 */}
                    <span className={'ml-auto shrink-0 rounded px-1.5 py-0.5 text-[10px] leading-none '
                      + ((c.credit_multiplier || 0) === 0
                         ? 'bg-emerald-50 text-emerald-700'
                         : 'bg-slate-100 text-slate-600')}>
                      {(c.credit_multiplier || 0) === 0 ? t('models.free') : `x${c.credit_multiplier}`}
                    </span>
                  </label>
                ))}
              </div>
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel')}</Button>
          <Button onClick={submit} disabled={busy || !name.trim() || picked.size === 0}>
            {busy ? t('common.submitting') : isNew ? t('models.createSubmit') : t('models.saveSubmit')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ── 分组删除（会把绑定的 Key group_id 置 0）─────────────────────────── */
function GroupDeleteDialog({target, onClose, onDeleted}: {
  target: ModelGroup | null; onClose: () => void; onDeleted: () => void;
}) {
  const t = useT();
  async function doDelete() {
    if (!target) return;
    try {
      const r = await modelApi.deleteGroup(target.id);
      notify.ok(t('models.groupDeleted', {n: r.unbound_keys}));
      onDeleted();
    } catch (e) { notify.err(errText(e)); }
  }
  return (
    <AlertDialog open={!!target} onOpenChange={(v: boolean) => !v && onClose()}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{t('models.deleteGroupTitle')}</AlertDialogTitle>
          <AlertDialogDescription>
            {t('models.deleteGroupConfirm', {name: target?.name || '', n: target?.key_count || 0})}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
          <AlertDialogAction className="bg-destructive text-white hover:bg-destructive/90"
                             onClick={(e) => { e.preventDefault(); doDelete(); }}>
            {t('models.delete')}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
