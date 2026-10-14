'use client';

/**
 * 账号池分组（Wave B9，P2 T8 欠账入面板）。
 *
 * 与模型分组正交：那个管「密钥能用哪些模型」，这个管「密钥能用哪些账号」
 * （ApiKey.account_group_id 绑定，proxy._select_account 按 group_id 过滤选号）。
 * 删除守卫（409）：组内还有账号或还有密钥绑定时拒绝——静默删会让在途请求跨组串号。
 * 对齐 routers/account_groups.py。
 */
import {useCallback, useEffect, useState} from 'react';
import {accountGroupApi, accountApi, errText} from '@/lib/api';
import type {Account, AccountGroup} from '@/lib/types';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
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
import {Layers, Pencil, Plus, Trash2, UserPlus, X} from 'lucide-react';

export default function AccountGroupsPage() {
  const t = useT();
  const [groups, setGroups] = useState<AccountGroup[] | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [error, setError] = useState('');
  const [editTarget, setEditTarget] = useState<AccountGroup | 'new' | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<AccountGroup | null>(null);
  const [assignTarget, setAssignTarget] = useState<AccountGroup | null>(null);

  const load = useCallback(async () => {
    setError('');
    try {
      const [gRes, aRes] = await Promise.all([accountGroupApi.list(), accountApi.list()]);
      setGroups(gRes.items);
      setAccounts(aRes.items);
    } catch (e) { setError(errText(e)); }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function doDelete(g: AccountGroup) {
    try {
      await accountGroupApi.remove(g.id);
      notify.ok(t('agroups.deleted'));
      setDeleteTarget(null);
      load();
    } catch (e) {
      notify.err(errText(e));
      setDeleteTarget(null);
    }
  }

  if (error) return <LoadError message={error} onRetry={load} />;
  if (!groups) return <SkeletonBar className="h-96" />;

  const ungrouped = accounts.filter((a) => !a.group_id || a.group_id === 0).length;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t('agroups.title')}
        description={t('agroups.subtitle', {n: groups.length, ungrouped})}
        actions={
          <Button size="sm" onClick={() => setEditTarget('new')}>
            <Plus className="h-4 w-4" />{t('agroups.add')}
          </Button>
        }
      />

      {groups.length === 0 ? (
        <EmptyState title={t('agroups.empty')} />
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
          {groups.map((g) => {
            const members = accounts.filter((a) => a.group_id === g.id);
            return (
              <div key={g.id} className="flex flex-col gap-3 rounded-xl border bg-card p-4">
                <div className="flex items-start justify-between">
                  <div>
                    <div className="flex items-center gap-2">
                      <Layers className="h-4 w-4 text-muted-foreground" />
                      <span className="font-medium">{g.name}</span>
                    </div>
                    {g.note && <p className="mt-1 text-xs text-muted-foreground">{g.note}</p>}
                  </div>
                  <div className="flex gap-1">
                    <Button variant="ghost" size="sm" onClick={() => setEditTarget(g)}>
                      <Pencil className="h-4 w-4" />
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => setDeleteTarget(g)}>
                      <Trash2 className="h-4 w-4 text-red-500" />
                    </Button>
                  </div>
                </div>
                <div className="flex gap-2">
                  <Badge variant="secondary" className="text-[10px]">{t('agroups.accCount', {n: g.accounts})}</Badge>
                  <Badge variant="outline" className="text-[10px]">{t('agroups.keyCount', {n: g.bound_keys})}</Badge>
                </div>
                {/* 成员 */}
                <div className="flex max-h-24 flex-wrap gap-1 overflow-y-auto">
                  {members.length === 0 ? (
                    <span className="text-[11px] text-muted-foreground">{t('agroups.noMembers')}</span>
                  ) : members.map((a) => (
                    <Badge key={a.id} variant="outline" className="gap-1 text-[10px]">
                      {a.name}
                      <button
                        onClick={async () => {
                          try { await accountGroupApi.unassign(a.id); load(); }
                          catch (e) { notify.err(errText(e)); }
                        }}
                        className="rounded-full hover:bg-muted" title={t('agroups.remove')}>
                        <X className="h-3 w-3" />
                      </button>
                    </Badge>
                  ))}
                </div>
                <Button variant="outline" size="sm" onClick={() => setAssignTarget(g)}>
                  <UserPlus className="h-4 w-4" />{t('agroups.assign')}
                </Button>
              </div>
            );
          })}
        </div>
      )}

      <GroupEditDialog target={editTarget} onClose={() => setEditTarget(null)}
                       onSaved={() => { setEditTarget(null); load(); }} />
      <AssignDialog target={assignTarget} accounts={accounts}
                    onClose={() => setAssignTarget(null)}
                    onDone={() => { setAssignTarget(null); load(); }} />
      <AlertDialog open={!!deleteTarget} onOpenChange={(v: boolean) => !v && setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('agroups.deleteTitle')}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('agroups.deleteConfirm', {
                name: deleteTarget?.name || '',
                acc: deleteTarget?.accounts || 0,
                key: deleteTarget?.bound_keys || 0,
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction className="bg-destructive text-white hover:bg-destructive/90"
                               onClick={(e) => { e.preventDefault(); deleteTarget && doDelete(deleteTarget); }}>
              {t('agroups.delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

/* ── 分组编辑（名称 + 备注，名称唯一 409）────────────────────────────── */
function GroupEditDialog({target, onClose, onSaved}: {
  target: AccountGroup | 'new' | null; onClose: () => void; onSaved: () => void;
}) {
  const t = useT();
  const isNew = target === 'new';
  const editing: AccountGroup | null = target && target !== 'new' ? target : null;
  const [name, setName] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (editing) { setName(editing.name); setNote(editing.note || ''); }
    else { setName(''); setNote(''); }
  }, [target]); // eslint-disable-line react-hooks/exhaustive-deps

  if (target === null) return null;

  async function submit() {
    if (!name.trim()) return;
    setBusy(true);
    try {
      if (isNew) {
        await accountGroupApi.create({name: name.trim(), note: note.trim()});
        notify.ok(t('agroups.created'));
      } else if (editing) {
        await accountGroupApi.update(editing.id, {name: name.trim(), note: note.trim()});
        notify.ok(t('agroups.saved'));
      }
      onSaved();
    } catch (e) { notify.err(errText(e)); }
    finally { setBusy(false); }
  }

  return (
    <Dialog open={target !== null} onOpenChange={(v: boolean) => !v && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{isNew ? t('agroups.addTitle') : t('agroups.editTitle', {name: editing?.name || ''})}</DialogTitle>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="space-y-1.5">
            <Label>{t('agroups.fName')}</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)}
                   placeholder={t('agroups.fNamePh')} />
          </div>
          <div className="space-y-1.5">
            <Label>{t('agroups.fNote')}</Label>
            <Input value={note} onChange={(e) => setNote(e.target.value)} />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel')}</Button>
          <Button onClick={submit} disabled={busy || !name.trim()}>
            {busy ? t('common.submitting') : isNew ? t('agroups.createSubmit') : t('agroups.saveSubmit')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ── 分配账号（选未分组账号划入该组）────────────────────────────────────── */
function AssignDialog({target, accounts, onClose, onDone}: {
  target: AccountGroup | null; accounts: Account[]; onClose: () => void; onDone: () => void;
}) {
  const t = useT();
  const [accId, setAccId] = useState('0');
  const [busy, setBusy] = useState(false);
  useEffect(() => { setAccId('0'); }, [target]);
  if (!target) return null;
  const available = accounts.filter((a) => !a.group_id || a.group_id === 0);

  async function assign() {
    if (accId === '0' || !target) return;
    setBusy(true);
    try {
      await accountGroupApi.assign(target.id, Number(accId));
      notify.ok(t('agroups.assigned'));
      onDone();
    } catch (e) { notify.err(errText(e)); }
    finally { setBusy(false); }
  }

  return (
    <Dialog open={!!target} onOpenChange={(v: boolean) => !v && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('agroups.assignTitle', {name: target.name})}</DialogTitle>
        </DialogHeader>
        {available.length === 0 ? (
          <p className="py-2 text-sm text-muted-foreground">{t('agroups.noAvailable')}</p>
        ) : (
          <div className="space-y-1.5">
            <Label>{t('agroups.fAccount')}</Label>
            <Select value={accId} onValueChange={setAccId}>
              <SelectTrigger><SelectValue placeholder={t('agroups.pickAccount')} /></SelectTrigger>
              <SelectContent>
                {available.map((a) => (
                  <SelectItem key={a.id} value={String(a.id)}>
                    {a.name}（{a.realm}{a.status !== 'active' ? ` · ${a.status}` : ''}）
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel')}</Button>
          <Button onClick={assign} disabled={busy || accId === '0' || available.length === 0}>
            {busy ? t('common.submitting') : t('agroups.assignSubmit')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
