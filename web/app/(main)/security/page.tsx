'use client';

/**
 * 安全 / 入站治理（Wave B7，P2 新能力入面板的欠账）。
 *
 * 三块能力（对齐 routers/security.py）：
 * 1. 入站 IP 管控：全局开关 + 白/黑名单模式 + CIDR 规则（/v1/* 在密钥校验之前过这道闸）；
 * 2. 访问审计：IpAccessLog（谁被放行/拦截 + 原因）；
 * 3. 模型别名映射：model_map（别名 → 真名），网关 proxy._map_model 在白名单校验之前应用。
 */
import {useCallback, useEffect, useState} from 'react';
import {securityApi, errText} from '@/lib/api';
import type {IpRule, IpAccessLog, SecurityConfig} from '@/lib/types';
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
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table';
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import {ArrowRightLeft, Plus, ScrollText, ShieldCheck, Trash2} from 'lucide-react';

function fmtIso(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export default function SecurityPage() {
  const t = useT();
  const [config, setConfig] = useState<SecurityConfig | null>(null);
  const [rules, setRules] = useState<IpRule[]>([]);
  const [logs, setLogs] = useState<IpAccessLog[]>([]);
  const [modelMap, setModelMap] = useState<Record<string, string>>({});
  const [error, setError] = useState('');
  const [newKind, setNewKind] = useState<'allow' | 'deny'>('deny');
  const [newCidr, setNewCidr] = useState('');
  const [newNote, setNewNote] = useState('');
  const [mapAlias, setMapAlias] = useState('');
  const [mapReal, setMapReal] = useState('');
  const [clearConfirm, setClearConfirm] = useState(false);

  const load = useCallback(async () => {
    setError('');
    try {
      const [cRes, rRes, lRes, mRes] = await Promise.all([
        securityApi.config(), securityApi.rules(), securityApi.logs(200), securityApi.modelMap(),
      ]);
      setConfig(cRes);
      setRules(rRes.items);
      setLogs(lRes.items);
      setModelMap(mRes.map);
    } catch (e) { setError(errText(e)); }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function saveConfig(next: SecurityConfig) {
    try {
      const r = await securityApi.saveConfig(next);
      setConfig(r);
      notify.ok(t('security.configSaved'));
    } catch (e) { notify.err(errText(e)); }
  }

  async function addRule() {
    if (!newCidr.trim()) return;
    try {
      await securityApi.addRule({kind: newKind, cidr: newCidr.trim(), note: newNote.trim()});
      notify.ok(t('security.ruleAdded'));
      setNewCidr(''); setNewNote('');
      const r = await securityApi.rules();
      setRules(r.items);
    } catch (e) { notify.err(errText(e)); }
  }

  async function removeRule(id: number) {
    try {
      await securityApi.removeRule(id);
      setRules((prev) => prev.filter((r) => r.id !== id));
      notify.ok(t('security.ruleDeleted'));
    } catch (e) { notify.err(errText(e)); }
  }

  async function addMapEntry() {
    const a = mapAlias.trim(), r = mapReal.trim();
    if (!a || !r || a === r) return;
    const next = {...modelMap, [a]: r};
    try {
      const res = await securityApi.saveModelMap(next);
      setModelMap(res.map);
      setMapAlias(''); setMapReal('');
      notify.ok(t('security.mapSaved'));
    } catch (e) { notify.err(errText(e)); }
  }

  async function removeMapEntry(alias: string) {
    const next = {...modelMap};
    delete next[alias];
    try {
      const res = await securityApi.saveModelMap(next);
      setModelMap(res.map);
      notify.ok(t('security.mapSaved'));
    } catch (e) { notify.err(errText(e)); }
  }

  async function doClearLogs() {
    try {
      await securityApi.clearLogs();
      setLogs([]);
      setClearConfirm(false);
      notify.ok(t('security.logsCleared'));
    } catch (e) { notify.err(errText(e)); }
  }

  if (error) return <LoadError message={error} onRetry={load} />;
  if (!config) return <SkeletonBar className="h-96" />;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader title={t('security.title')} description={t('security.subtitle')} />

      {/* 全局开关 + 模式 */}
      <section className="flex flex-wrap items-center gap-6 rounded-xl border bg-card p-4">
        <div className="flex items-center gap-2">
          <Switch checked={config.enabled}
                  onCheckedChange={(v) => saveConfig({...config, enabled: v})} />
          <Label className="cursor-pointer">{t('security.enabled')}</Label>
        </div>
        <div className="flex items-center gap-2">
          <Label className="text-xs text-muted-foreground">{t('security.mode')}</Label>
          <Select value={config.mode}
                  onValueChange={(v) => saveConfig({...config, mode: v as 'whitelist' | 'blacklist'})}>
            <SelectTrigger className="w-36"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="blacklist">{t('security.modeBlacklist')}</SelectItem>
              <SelectItem value="whitelist">{t('security.modeWhitelist')}</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <p className="text-xs text-muted-foreground">
          {config.enabled
            ? config.mode === 'whitelist' ? t('security.hintWhitelist') : t('security.hintBlacklist')
            : t('security.hintDisabled')}
        </p>
      </section>

      <Tabs defaultValue="rules">
        <TabsList>
          <TabsTrigger value="rules"><ShieldCheck className="mr-1.5 h-4 w-4" />{t('security.tabRules', {n: rules.length})}</TabsTrigger>
          <TabsTrigger value="logs"><ScrollText className="mr-1.5 h-4 w-4" />{t('security.tabLogs')}</TabsTrigger>
          <TabsTrigger value="map"><ArrowRightLeft className="mr-1.5 h-4 w-4" />{t('security.tabMap', {n: Object.keys(modelMap).length})}</TabsTrigger>
        </TabsList>

        {/* ── IP 规则 ─────────────────────────────────────── */}
        <TabsContent value="rules" className="mt-4 flex flex-col gap-3">
          <div className="flex flex-wrap items-end gap-2 rounded-xl border bg-card p-3">
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">{t('security.fKind')}</Label>
              <Select value={newKind} onValueChange={(v) => setNewKind(v as 'allow' | 'deny')}>
                <SelectTrigger className="w-28"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="deny">{t('security.kindDeny')}</SelectItem>
                  <SelectItem value="allow">{t('security.kindAllow')}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">{t('security.fCidr')}</Label>
              <Input value={newCidr} onChange={(e) => setNewCidr(e.target.value)}
                     placeholder="1.2.3.4 或 10.0.0.0/8" className="w-48 font-mono text-xs" />
            </div>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">{t('security.fNote')}</Label>
              <Input value={newNote} onChange={(e) => setNewNote(e.target.value)} className="w-40" />
            </div>
            <Button size="sm" onClick={addRule} disabled={!newCidr.trim()}>
              <Plus className="h-4 w-4" />{t('security.addRule')}
            </Button>
          </div>
          {rules.length === 0 ? (
            <EmptyState title={t('security.noRules')} />
          ) : (
            <div className="rounded-xl border bg-card overflow-x-auto">
          <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t('security.colKind')}</TableHead>
                    <TableHead>{t('security.colCidr')}</TableHead>
                    <TableHead>{t('security.colNote')}</TableHead>
                    <TableHead>{t('security.colCreated')}</TableHead>
                    <TableHead className="text-right">{t('security.colActions')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rules.map((r) => (
                    <TableRow key={r.id}>
                      <TableCell>
                        <Badge variant="outline" className={cn('text-[10px]',
                          r.kind === 'allow' ? 'border-emerald-300 text-emerald-600' : 'border-red-300 text-red-600')}>
                          {r.kind === 'allow' ? t('security.kindAllow') : t('security.kindDeny')}
                        </Badge>
                      </TableCell>
                      <TableCell className="font-mono text-xs">{r.cidr}</TableCell>
                      <TableCell className="text-xs text-muted-foreground">{r.note || '—'}</TableCell>
                      <TableCell className="text-xs text-muted-foreground">{fmtIso(r.created_at)}</TableCell>
                      <TableCell className="text-right">
                        <Button variant="ghost" size="sm" onClick={() => removeRule(r.id)}>
                          <Trash2 className="h-4 w-4 text-red-500" />
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </TabsContent>

        {/* ── 访问日志 ─────────────────────────────────────── */}
        <TabsContent value="logs" className="mt-4 flex flex-col gap-3">
          <div className="flex justify-end">
            <Button size="sm" variant="outline" onClick={() => setClearConfirm(true)}
                    disabled={logs.length === 0}>
              <Trash2 className="h-4 w-4" />{t('security.clearLogs')}
            </Button>
          </div>
          {logs.length === 0 ? (
            <EmptyState title={t('security.noLogs')} />
          ) : (
            <div className="rounded-xl border bg-card overflow-x-auto">
          <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t('security.colTime')}</TableHead>
                    <TableHead>{t('security.colIp')}</TableHead>
                    <TableHead>{t('security.colPath')}</TableHead>
                    <TableHead>{t('security.colResult')}</TableHead>
                    <TableHead>{t('security.colReason')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {logs.map((l) => (
                    <TableRow key={l.id}>
                      <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{fmtIso(l.created_at)}</TableCell>
                      <TableCell className="font-mono text-xs">{l.ip}</TableCell>
                      <TableCell className="max-w-[220px] truncate font-mono text-xs">{l.path}</TableCell>
                      <TableCell>
                        {l.blocked ? (
                          <Badge variant="outline" className="border-red-300 text-[10px] text-red-600">{t('security.blocked')}</Badge>
                        ) : (
                          <Badge variant="outline" className="border-emerald-300 text-[10px] text-emerald-600">{t('security.allowed')}</Badge>
                        )}
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">{l.reason || '—'}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </TabsContent>

        {/* ── 模型别名映射 ─────────────────────────────────── */}
        <TabsContent value="map" className="mt-4 flex flex-col gap-3">
          <p className="text-xs text-muted-foreground">{t('security.mapHint')}</p>
          <div className="flex flex-wrap items-end gap-2 rounded-xl border bg-card p-3">
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">{t('security.fAlias')}</Label>
              <Input value={mapAlias} onChange={(e) => setMapAlias(e.target.value)}
                     placeholder="gpt-4o-mini" className="w-44 font-mono text-xs" />
            </div>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">{t('security.fReal')}</Label>
              <Input value={mapReal} onChange={(e) => setMapReal(e.target.value)}
                     placeholder="deepseek-v4-pro" className="w-44 font-mono text-xs" />
            </div>
            <Button size="sm" onClick={addMapEntry}
                    disabled={!mapAlias.trim() || !mapReal.trim() || mapAlias.trim() === mapReal.trim()}>
              <Plus className="h-4 w-4" />{t('security.addMap')}
            </Button>
          </div>
          {Object.keys(modelMap).length === 0 ? (
            <EmptyState title={t('security.noMap')} />
          ) : (
            <div className="rounded-xl border bg-card overflow-x-auto">
          <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t('security.colAlias')}</TableHead>
                    <TableHead />
                    <TableHead>{t('security.colReal')}</TableHead>
                    <TableHead className="text-right">{t('security.colActions')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {Object.entries(modelMap).map(([alias, real]) => (
                    <TableRow key={alias}>
                      <TableCell className="font-mono text-xs">{alias}</TableCell>
                      <TableCell className="text-muted-foreground">→</TableCell>
                      <TableCell className="font-mono text-xs">{real}</TableCell>
                      <TableCell className="text-right">
                        <Button variant="ghost" size="sm" onClick={() => removeMapEntry(alias)}>
                          <Trash2 className="h-4 w-4 text-red-500" />
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </TabsContent>
      </Tabs>

      <AlertDialog open={clearConfirm} onOpenChange={(v: boolean) => !v && setClearConfirm(false)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('security.clearLogsTitle')}</AlertDialogTitle>
            <AlertDialogDescription>{t('security.clearLogsConfirm')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction className="bg-destructive text-white hover:bg-destructive/90"
                               onClick={(e) => { e.preventDefault(); doClearLogs(); }}>
              {t('security.clearLogs')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
