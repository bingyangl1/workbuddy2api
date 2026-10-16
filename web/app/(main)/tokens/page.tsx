'use client';

/**
 * 访问令牌（Wave B11，P2 T7 欠账入面板）。
 *
 * 管理面作用域 API Token（wbt_ 前缀）：给脚本/看板/CI 的只读凭据。
 * SHA-256 存储、明文仅创建时返回一次；scope 恒 readonly（本引擎无 admin scope）。
 * 对齐 routers/tokens.py + admin/tokensvc.py。
 */
import {useCallback, useEffect, useState} from 'react';
import {tokenApi, errText} from '@/lib/api';
import type {ApiToken, CreatedApiToken} from '@/lib/types';
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
import {CopyButton} from '@/components/ui/copy-button';
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from '@/components/animate-ui/radix/dialog';
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import {KeyRound, Plus, Trash2} from 'lucide-react';

function fmtIso(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export default function TokensPage() {
  const t = useT();
  const [items, setItems] = useState<ApiToken[] | null>(null);
  const [error, setError] = useState('');
  const [createOpen, setCreateOpen] = useState(false);
  const [created, setCreated] = useState<CreatedApiToken | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ApiToken | null>(null);

  const load = useCallback(async () => {
    setError('');
    try {
      const r = await tokenApi.list();
      setItems(r.items);
    } catch (e) { setError(errText(e)); }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function toggle(tok: ApiToken) {
    try {
      await tokenApi.update(tok.id, {enabled: !tok.enabled});
      setItems((prev) => prev && prev.map((x) => x.id === tok.id ? {...x, enabled: !tok.enabled} : x));
      notify.ok(tok.enabled ? t('tokens.disabledOk') : t('tokens.enabledOk'));
    } catch (e) { notify.err(errText(e)); }
  }

  async function doDelete() {
    if (!deleteTarget) return;
    try {
      await tokenApi.remove(deleteTarget.id);
      setItems((prev) => prev && prev.filter((x) => x.id !== deleteTarget.id));
      setDeleteTarget(null);
      notify.ok(t('tokens.deleted'));
    } catch (e) { notify.err(errText(e)); }
  }

  if (error) return <LoadError message={error} onRetry={load} />;
  if (!items) return <SkeletonBar className="h-96" />;

  const expired = (tok: ApiToken) => tok.expires_at != null && new Date(tok.expires_at) < new Date();

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t('tokens.title')}
        description={t('tokens.subtitle', {n: items.length})}
        actions={
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4" />{t('tokens.add')}
          </Button>
        }
      />

      <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-900/40 dark:bg-amber-950/30 dark:text-amber-300">
        {t('tokens.hint')}
      </p>

      {items.length === 0 ? (
        <EmptyState title={t('tokens.empty')} />
      ) : (
        <div className="rounded-xl border bg-card overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t('tokens.colName')}</TableHead>
                <TableHead>{t('tokens.colPrefix')}</TableHead>
                <TableHead>{t('tokens.colScope')}</TableHead>
                <TableHead>{t('tokens.colExpires')}</TableHead>
                <TableHead>{t('tokens.colLastUsed')}</TableHead>
                <TableHead>{t('tokens.colEnabled')}</TableHead>
                <TableHead className="text-right">{t('tokens.colActions')}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((tok) => (
                <TableRow key={tok.id} className={cn(!tok.enabled && 'opacity-55')}>
                  <TableCell className="text-xs font-medium">{tok.name || `#${tok.id}`}</TableCell>
                  <TableCell className="font-mono text-[11px] text-muted-foreground">{tok.prefix}…</TableCell>
                  <TableCell>
                    <Badge variant="secondary" className="text-[10px]">{tok.scope}</Badge>
                  </TableCell>
                  <TableCell>
                    {tok.expires_at ? (
                      <span className={cn('text-xs', expired(tok) && 'text-red-500')}>
                        {expired(tok) ? t('tokens.expired') : fmtIso(tok.expires_at)}
                      </span>
                    ) : <span className="text-xs text-muted-foreground">{t('tokens.never')}</span>}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">{fmtIso(tok.last_used_at)}</TableCell>
                  <TableCell>
                    <Switch checked={tok.enabled} onCheckedChange={() => toggle(tok)} />
                  </TableCell>
                  <TableCell className="text-right">
                    <Button variant="ghost" size="sm" onClick={() => setDeleteTarget(tok)}>
                      <Trash2 className="h-4 w-4 text-red-500" />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <CreateTokenDialog open={createOpen} onClose={() => setCreateOpen(false)}
                         onCreated={(ct) => { setCreateOpen(false); setCreated(ct); load(); }} />
      <CreatedTokenDialog created={created} onClose={() => setCreated(null)} />

      <AlertDialog open={!!deleteTarget} onOpenChange={(v: boolean) => !v && setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('tokens.deleteTitle')}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('tokens.deleteConfirm', {name: deleteTarget?.name || `#${deleteTarget?.id}`})}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction className="bg-destructive text-white hover:bg-destructive/90"
                               onClick={(e) => { e.preventDefault(); doDelete(); }}>
              {t('tokens.delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

/* ── 新建令牌（名称 + 可选过期）────────────────────────────────────── */
function CreateTokenDialog({open, onClose, onCreated}: {
  open: boolean; onClose: () => void; onCreated: (t: CreatedApiToken) => void;
}) {
  const t = useT();
  const [name, setName] = useState('');
  const [expires, setExpires] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) { setName(''); setExpires(''); } }, [open]);

  async function submit() {
    setBusy(true);
    try {
      const body: {name: string; expires_at?: string | null} = {name: name.trim()};
      if (expires) body.expires_at = new Date(expires).toISOString();
      const ct = await tokenApi.create(body);
      onCreated(ct);
    } catch (e) { notify.err(errText(e)); }
    finally { setBusy(false); }
  }

  return (
    <Dialog open={open} onOpenChange={(v: boolean) => !v && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>{t('tokens.addTitle')}</DialogTitle></DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="space-y-1.5">
            <Label>{t('tokens.fName')}</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)}
                   placeholder={t('tokens.fNamePh')} />
          </div>
          <div className="space-y-1.5">
            <Label>{t('tokens.fExpires')}</Label>
            <Input type="datetime-local" value={expires} onChange={(e) => setExpires(e.target.value)} />
            <p className="text-[11px] text-muted-foreground">{t('tokens.fExpiresHint')}</p>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel')}</Button>
          <Button onClick={submit} disabled={busy}>
            {busy ? t('common.submitting') : t('tokens.createSubmit')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ── 创建成功：明文仅此一次 ─────────────────────────────────────────── */
function CreatedTokenDialog({created, onClose}: {created: CreatedApiToken | null; onClose: () => void}) {
  const t = useT();
  if (!created) return null;
  return (
    <Dialog open={!!created} onOpenChange={(v: boolean) => !v && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>{t('tokens.createdTitle')}</DialogTitle></DialogHeader>
        <div className="flex flex-col gap-2">
          <p className="text-xs text-muted-foreground">{t('tokens.createdHint')}</p>
          <div className="flex items-center gap-2 rounded-md border bg-muted p-2">
            <KeyRound className="h-4 w-4 shrink-0 text-muted-foreground" />
            <code className="flex-1 break-all font-mono text-xs">{created.token}</code>
            <CopyButton value={created.token} />
          </div>
        </div>
        <DialogFooter>
          <Button onClick={onClose}>{t('tokens.createdDone')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
