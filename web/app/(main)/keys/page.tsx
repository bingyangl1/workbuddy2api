'use client';

/**
 * API 密钥（Wave B3，P2 全维度的核心 UI——此前这些维度只能走 API 操作）。
 *
 * 本引擎密钥是「多租户网关凭证」：每把 key 可独立限制 积分上限 / 模型分组 /
 * 账号池分组 / realm / 有效期 / max_ips / IP白名单 / token 配额——这是
 * workbuddy2api 相对 manager 的独有能力，manager 完全没有对应物。
 * 对齐 routers/keys.py（KeyIn + patch_key）：显式空值 = 改回「不限」。
 */
import {useCallback, useEffect, useMemo, useState} from 'react';
import {accountGroupApi, keyApi, modelApi, errText} from '@/lib/api';
import type {AccountGroup, ApiKey, CreatedApiKey, ModelGroup} from '@/lib/types';
import type {KeyPayload} from '@/lib/api';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {useRealm} from '@/lib/realm-context';
import {fmtNumber} from '@/lib/format';
import {cn} from '@/lib/utils';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Textarea} from '@/components/ui/textarea';
import {Switch} from '@/components/ui/switch';
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
import {Copy, Eye, KeyRound, Pencil, Plus, RotateCcw, Search, Trash2} from 'lucide-react';

function fmtIso(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export default function KeysPage() {
  const t = useT();
  const {realm} = useRealm();
  const [keys, setKeys] = useState<ApiKey[] | null>(null);
  const [modelGroups, setModelGroups] = useState<ModelGroup[]>([]);
  const [accGroups, setAccGroups] = useState<AccountGroup[]>([]);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [editOf, setEditOf] = useState<ApiKey | 'new' | null>(null);
  const [viewOf, setViewOf] = useState<ApiKey | null>(null);
  const [created, setCreated] = useState<CreatedApiKey | null>(null);
  const [deleteOf, setDeleteOf] = useState<ApiKey | null>(null);

  const load = useCallback(async () => {
    setError('');
    try {
      const [kRes, mgRes, agRes] = await Promise.all([
        keyApi.list(),
        modelApi.groups(),
        accountGroupApi.list(),
      ]);
      setKeys(kRes.items);
      setModelGroups(mgRes.items);
      setAccGroups(agRes.items);
    } catch (e) {
      setError(errText(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const filtered = useMemo(() => {
    if (!keys) return [];
    const q = query.trim().toLowerCase();
    return keys.filter((k) => {
      // realm 过滤：key.realm 为空表示跟随全局（两版本都可见）
      if (k.realm && k.realm !== realm) return false;
      if (q && !(k.name.toLowerCase().includes(q) || k.key_prefix.toLowerCase().includes(q))) return false;
      return true;
    });
  }, [keys, realm, query]);

  async function toggleStatus(k: ApiKey) {
    try {
      const next = k.status === 'active' ? 'revoked' : 'active';
      await keyApi.update(k.id, {status: next});
      notify.ok(t(next === 'active' ? 'keys.enabled' : 'keys.revoked', {name: k.name || k.key_prefix}));
      await load();
    } catch (e) {
      notify.err(errText(e));
    }
  }

  async function resetUsage(k: ApiKey) {
    try {
      await keyApi.update(k.id, {reset_used: true});
      notify.ok(t('keys.usageReset', {name: k.name || k.key_prefix}));
      await load();
    } catch (e) {
      notify.err(errText(e));
    }
  }

  async function doDelete() {
    if (!deleteOf) return;
    try {
      await keyApi.remove(deleteOf.id);
      notify.ok(t('keys.deleted', {name: deleteOf.name || deleteOf.key_prefix}));
      setDeleteOf(null);
      await load();
    } catch (e) {
      notify.err(errText(e));
    }
  }

  if (error) return <LoadError message={error} onRetry={load} />;
  if (!keys) return <SkeletonBar className="h-96" />;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t('keys.title')}
        description={t('keys.subtitle', {n: filtered.length, realm})}
        actions={
          <Button size="sm" onClick={() => setEditOf('new')}>
            <Plus className="h-4 w-4" />
            {t('keys.add')}
          </Button>
        }
      />

      <div className="relative max-w-sm">
        <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input value={query} onChange={(e) => setQuery(e.target.value)}
               placeholder={t('keys.searchPlaceholder')} className="pl-8" />
      </div>

      {filtered.length === 0 ? (
        <EmptyState title={t('keys.empty')} />
      ) : (
        <div className="rounded-xl border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t('keys.colName')}</TableHead>
                <TableHead>{t('keys.colUsage')}</TableHead>
                <TableHead>{t('keys.colScope')}</TableHead>
                <TableHead>{t('keys.colStatus')}</TableHead>
                <TableHead className="text-right">{t('keys.colActions')}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filtered.map((k) => (
                <TableRow key={k.id}>
                  <TableCell>
                    <div className="flex flex-col">
                      <span className="font-medium">{k.name || t('keys.unnamed')}</span>
                      <span className="font-mono text-[11px] text-muted-foreground">{k.masked}</span>
                    </div>
                  </TableCell>
                  <TableCell>
                    <KeyUsageCell k={k} />
                  </TableCell>
                  <TableCell>
                    <KeyScopeCell k={k} />
                  </TableCell>
                  <TableCell>
                    <KeyStatusCell k={k} />
                  </TableCell>
                  <TableCell className="text-right">
                    <div className="flex items-center justify-end gap-1">
                      <Button variant="ghost" size="sm" title={t('keys.view')}
                              onClick={() => setViewOf(k)}>
                        <Eye className="h-4 w-4" />
                      </Button>
                      <Button variant="ghost" size="sm" title={t('keys.edit')}
                              onClick={() => setEditOf(k)}>
                        <Pencil className="h-4 w-4" />
                      </Button>
                      <Button variant="ghost" size="sm" title={t('keys.resetUsage')}
                              onClick={() => resetUsage(k)}>
                        <RotateCcw className="h-4 w-4" />
                      </Button>
                      <Button variant="ghost" size="sm"
                              title={k.status === 'active' ? t('keys.revoke') : t('keys.enable')}
                              onClick={() => toggleStatus(k)}>
                        <KeyRound className="h-4 w-4" />
                      </Button>
                      <Button variant="ghost" size="sm" title={t('keys.delete')}
                              onClick={() => setDeleteOf(k)}>
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

      <KeyEditDialog
        target={editOf}
        modelGroups={modelGroups}
        accGroups={accGroups}
        onClose={() => setEditOf(null)}
        onCreated={(ck) => { setEditOf(null); setCreated(ck); load(); }}
        onSaved={() => { setEditOf(null); load(); }}
      />
      <KeyViewDialog target={viewOf} onClose={() => setViewOf(null)} />
      <CreatedKeyDialog created={created} onClose={() => setCreated(null)} />

      <AlertDialog open={!!deleteOf} onOpenChange={(v: boolean) => !v && setDeleteOf(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('keys.deleteTitle')}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('keys.deleteConfirm', {name: deleteOf?.name || deleteOf?.key_prefix || ''})}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction className="bg-destructive text-white hover:bg-destructive/90"
                               onClick={(e) => { e.preventDefault(); doDelete(); }}>
              {t('keys.delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

/* ── 用量格：积分 + token 配额，超限标红 ─────────────────────────── */
function KeyUsageCell({k}: {k: ApiKey}) {
  const t = useT();
  const creditPct = k.unlimited || !k.credit_limit ? 0 : Math.min(100, (k.credit_used / k.credit_limit) * 100);
  const creditOver = !k.unlimited && k.credit_limit > 0 && k.credit_used >= k.credit_limit;
  const tokenOver = k.token_quota > 0 && k.used_tokens >= k.token_quota;
  return (
    <div className="flex min-w-[130px] flex-col gap-1">
      <div className="flex items-center gap-1.5 text-xs">
        <span className={cn(creditOver && 'font-medium text-red-500')}>
          {k.unlimited ? t('keys.unlimited') : `${fmtNumber(k.credit_used)}/${fmtNumber(k.credit_limit)}`}
        </span>
      </div>
      {!k.unlimited && k.credit_limit > 0 && (
        <div className="h-1 w-full overflow-hidden rounded-full bg-muted">
          <div className={cn('h-full', creditOver ? 'bg-red-500' : 'bg-primary')}
               style={{width: `${creditPct}%`}} />
        </div>
      )}
      {k.token_quota > 0 && (
        <span className={cn('text-[10px]', tokenOver ? 'text-red-500' : 'text-muted-foreground')}>
          {t('keys.tokenQuota', {used: fmtNumber(k.used_tokens), quota: fmtNumber(k.token_quota)})}
        </span>
      )}
    </div>
  );
}

/* ── 范围格：模型分组 + 账号池分组 + realm + IP 限制 ─────────────── */
function KeyScopeCell({k}: {k: ApiKey}) {
  const t = useT();
  const items: string[] = [];
  if (k.group_name) items.push(k.group_name);
  if (k.account_group_name) items.push(`@${k.account_group_name}`);
  if (k.realm) items.push(k.realm);
  if (k.max_ips > 0) items.push(t('keys.maxIps', {n: k.max_ips}));
  if (k.ip_allowlist.length > 0) items.push(t('keys.ipAllow', {n: k.ip_allowlist.length}));
  if (items.length === 0) return <span className="text-xs text-muted-foreground">{t('keys.scopeAll')}</span>;
  const tip = k.ip_allowlist.length > 0 ? k.ip_allowlist.join('\n') : undefined;
  const body = (
    <div className="flex max-w-[220px] flex-wrap gap-1">
      {items.map((s, i) => <Badge key={i} variant="secondary" className="text-[10px]">{s}</Badge>)}
    </div>
  );
  if (!tip) return body;
  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>{body}</TooltipTrigger>
        <TooltipContent className="max-w-xs whitespace-pre-line text-xs">{tip}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

/* ── 状态格：active / revoked / expired ──────────────────────────── */
function KeyStatusCell({k}: {k: ApiKey}) {
  const t = useT();
  if (k.status !== 'active') {
    return <Badge variant="outline" className="border-red-300 text-red-600">{t('keys.revoked2')}</Badge>;
  }
  if (k.expired) {
    return (
      <TooltipProvider delayDuration={200}>
        <Tooltip>
          <TooltipTrigger asChild>
            <Badge variant="outline" className="border-amber-300 text-amber-600">{t('keys.expired')}</Badge>
          </TooltipTrigger>
          <TooltipContent className="text-xs">{t('keys.expiredAt', {at: fmtIso(k.expires_at)})}</TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }
  return <Badge variant="outline" className="border-emerald-300 text-emerald-600">{t('keys.active')}</Badge>;
}

/* ── 创建/编辑（全维度表单）────────────────────────────────────────── */
function KeyEditDialog({target, modelGroups, accGroups, onClose, onCreated, onSaved}: {
  target: ApiKey | 'new' | null;
  modelGroups: ModelGroup[];
  accGroups: AccountGroup[];
  onClose: () => void;
  onCreated: (ck: CreatedApiKey) => void;
  onSaved: () => void;
}) {
  const t = useT();
  const isNew = target === 'new';
  const editing: ApiKey | null = target && target !== 'new' ? target : null;
  const [form, setForm] = useState({
    name: '', note: '', credit_limit: '0', unlimited: false,
    group_id: '0', account_group_id: '0', realm: '',
    expires_at: '', max_ips: '0', ip_allowlist: '', token_quota: '0',
  });
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (editing) {
      setForm({
        name: editing.name, note: editing.note,
        credit_limit: String(editing.credit_limit || 0), unlimited: editing.unlimited,
        group_id: String(editing.group_id || 0),
        account_group_id: String(editing.account_group_id || 0),
        realm: editing.realm || '',
        expires_at: editing.expires_at ? editing.expires_at.slice(0, 16) : '',
        max_ips: String(editing.max_ips || 0),
        ip_allowlist: editing.ip_allowlist.join('\n'),
        token_quota: String(editing.token_quota || 0),
      });
    } else {
      setForm({name: '', note: '', credit_limit: '0', unlimited: false,
               group_id: '0', account_group_id: '0', realm: '',
               expires_at: '', max_ips: '0', ip_allowlist: '', token_quota: '0'});
    }
  }, [target]);   // eslint-disable-line react-hooks/exhaustive-deps

  if (target === null) return null;

  async function submit() {
    setBusy(true);
    const payload: KeyPayload = {
      name: form.name.trim(),
      note: form.note.trim(),
      unlimited: form.unlimited,
      credit_limit: form.unlimited ? 0 : Number(form.credit_limit) || 0,
      group_id: Number(form.group_id) || 0,
      account_group_id: Number(form.account_group_id) || 0,
      realm: (form.realm || '') as '' | 'cn' | 'global',
      expires_at: form.expires_at ? new Date(form.expires_at).toISOString() : null,
      max_ips: Number(form.max_ips) || 0,
      ip_allowlist: form.ip_allowlist.split('\n').map((s) => s.trim()).filter(Boolean),
      token_quota: Number(form.token_quota) || 0,
    };
    try {
      if (isNew) {
        const ck = await keyApi.create(payload);
        onCreated(ck);
      } else if (editing) {
        await keyApi.update(editing.id, payload);
        notify.ok(t('keys.saved'));
        onSaved();
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  const set = (k: keyof typeof form) => (v: string | boolean) =>
    setForm((f) => ({...f, [k]: v}));

  return (
    <Dialog open={target !== null} onOpenChange={(v: boolean) => !v && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{isNew ? t('keys.addTitle') : t('keys.editTitle', {name: editing?.name || ''})}</DialogTitle>
        </DialogHeader>
        <div className="grid max-h-[62vh] grid-cols-1 gap-4 overflow-y-auto pr-1 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>{t('keys.fName')}</Label>
            <Input value={form.name} onChange={(e) => set('name')(e.target.value)}
                   placeholder={t('keys.fNamePh')} />
          </div>
          <div className="space-y-1.5">
            <Label>{t('keys.fRealm')}</Label>
            <Select value={form.realm || 'follow'} onValueChange={(v) => set('realm')(v === 'follow' ? '' : v)}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="follow">{t('keys.realmFollow')}</SelectItem>
                <SelectItem value="cn">cn</SelectItem>
                <SelectItem value="global">global</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5 sm:col-span-2">
            <Label>{t('keys.fNote')}</Label>
            <Input value={form.note} onChange={(e) => set('note')(e.target.value)} />
          </div>

          <div className="space-y-1.5">
            <Label>{t('keys.fModelGroup')}</Label>
            <Select value={form.group_id} onValueChange={(v) => set('group_id')(v)}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="0">{t('keys.groupNone')}</SelectItem>
                {modelGroups.map((g) => (
                  <SelectItem key={g.id} value={String(g.id)}>{g.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>{t('keys.fAccGroup')}</Label>
            <Select value={form.account_group_id} onValueChange={(v) => set('account_group_id')(v)}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="0">{t('keys.accGroupAll')}</SelectItem>
                {accGroups.map((g) => (
                  <SelectItem key={g.id} value={String(g.id)}>{g.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label>{t('keys.fCreditLimit')}</Label>
            <Input type="number" min={0} value={form.credit_limit} disabled={form.unlimited}
                   onChange={(e) => set('credit_limit')(e.target.value)} />
          </div>
          <div className="flex items-end gap-2 space-y-1.5 pb-1">
            <div className="flex items-center gap-2">
              <Switch checked={form.unlimited} onCheckedChange={(v) => set('unlimited')(v)} />
              <Label className="cursor-pointer">{t('keys.fUnlimited')}</Label>
            </div>
          </div>

          <div className="space-y-1.5">
            <Label>{t('keys.fExpires')}</Label>
            <Input type="datetime-local" value={form.expires_at}
                   onChange={(e) => set('expires_at')(e.target.value)} />
            <p className="text-[10px] text-muted-foreground">{t('keys.fExpiresHint')}</p>
          </div>
          <div className="space-y-1.5">
            <Label>{t('keys.fTokenQuota')}</Label>
            <Input type="number" min={0} value={form.token_quota}
                   onChange={(e) => set('token_quota')(e.target.value)} />
          </div>

          <div className="space-y-1.5">
            <Label>{t('keys.fMaxIps')}</Label>
            <Input type="number" min={0} value={form.max_ips}
                   onChange={(e) => set('max_ips')(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label>{t('keys.fIpAllow')}</Label>
            <Textarea rows={2} value={form.ip_allowlist} placeholder="1.2.3.4&#10;10.0.0.0/8"
                      onChange={(e) => set('ip_allowlist')(e.target.value)}
                      className="font-mono text-xs" />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel')}</Button>
          <Button onClick={submit} disabled={busy}>
            {busy ? t('common.submitting') : isNew ? t('keys.createSubmit') : t('keys.saveSubmit')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ── 查看完整明文（JWT-only）───────────────────────────────────────── */
function KeyViewDialog({target, onClose}: {target: ApiKey | null; onClose: () => void}) {
  const t = useT();
  const [full, setFull] = useState('');
  const [err, setErr] = useState('');

  useEffect(() => {
    if (!target) { setFull(''); setErr(''); return; }
    setFull(''); setErr('');
    keyApi.view(target.id)
      .then((r) => setFull(r.key))
      .catch((e) => setErr(errText(e)));
  }, [target]);

  return (
    <Dialog open={!!target} onOpenChange={(v: boolean) => !v && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>{t('keys.viewTitle')}</DialogTitle></DialogHeader>
        {err ? (
          <p className="text-sm text-red-500">{err}</p>
        ) : !full ? (
          <SkeletonBar className="h-16" />
        ) : (
          <div className="flex items-center gap-2 rounded-md border bg-muted p-3">
            <code className="flex-1 break-all font-mono text-xs">{full}</code>
            <Button variant="ghost" size="sm"
                    onClick={() => { navigator.clipboard.writeText(full); notify.ok(t('keys.copied')); }}>
              <Copy className="h-4 w-4" />
            </Button>
          </div>
        )}
        <DialogFooter><Button onClick={onClose}>{t('common.close')}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ── 创建成功：明文只显示这一次 ────────────────────────────────────── */
function CreatedKeyDialog({created, onClose}: {created: CreatedApiKey | null; onClose: () => void}) {
  const t = useT();
  if (!created) return null;
  return (
    <Dialog open={!!created} onOpenChange={(v: boolean) => !v && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>{t('keys.createdTitle')}</DialogTitle></DialogHeader>
        <p className="text-xs text-amber-600">{t('keys.createdHint')}</p>
        <div className="flex items-center gap-2 rounded-md border bg-muted p-3">
          <code className="flex-1 break-all font-mono text-xs">{created.key}</code>
          <Button variant="ghost" size="sm"
                  onClick={() => { navigator.clipboard.writeText(created.key); notify.ok(t('keys.copied')); }}>
            <Copy className="h-4 w-4" />
          </Button>
        </div>
        <DialogFooter><Button onClick={onClose}>{t('keys.createdDone')}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
