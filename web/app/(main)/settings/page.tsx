'use client';

/**
 * 系统设置（Wave B14，P3 收尾页）。
 *
 * 客户端参数档案（对齐 routers/client_profile.py + admin/client_profile.py）：
 * 出站请求要伪装成官方桌面端，涉及 UA/版本号/风控头/桌面指纹一批参数。
 * 视图三态：effective（生效值）/ saved（钉住值）/ detected（本机探测）。
 * source：auto=优先现场探测 / saved=钉住已保存值（线上实例应选 saved）。
 * 本地探测 → 后台保存 → 线上同步 这条链路。
 */
import {useCallback, useEffect, useState} from 'react';
import {clientProfileApi, errText} from '@/lib/api';
import type {ClientProfile} from '@/lib/types';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import {Loader2, Radar, RotateCcw, Save} from 'lucide-react';

/** 标量字段（headers 单独成组）。键 = 引擎 DEFAULTS 的键。 */
const SCALAR_FIELDS: {key: string; num?: boolean}[] = [
  {key: 'desktop_version'}, {key: 'cli_version'},
  {key: 'application_name'}, {key: 'turing_product_name'},
  {key: 'ide_name'}, {key: 'ide_type'}, {key: 'product'}, {key: 'fp_product'},
  {key: 'ext_name'}, {key: 'os'}, {key: 'arch'}, {key: 'os_version'},
  {key: 'cpu_cores', num: true}, {key: 'memory_size', num: true},
  {key: 'timezone'}, {key: 'report_delay', num: true},
];

export default function SettingsPage() {
  const t = useT();
  const [data, setData] = useState<ClientProfile | null>(null);
  const [error, setError] = useState('');
  const [form, setForm] = useState<Record<string, string>>({});
  const [headers, setHeaders] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [detecting, setDetecting] = useState(false);

  const load = useCallback(async () => {
    setError('');
    try {
      const r = await clientProfileApi.get();
      setData(r);
      const eff = (r.effective || {}) as Record<string, unknown>;
      const f: Record<string, string> = {};
      SCALAR_FIELDS.forEach(({key}) => { f[key] = eff[key] != null ? String(eff[key]) : ''; });
      setForm(f);
      const h = (eff.headers || {}) as Record<string, unknown>;
      const hh: Record<string, string> = {};
      Object.entries(h).forEach(([k, v]) => { hh[k] = String(v); });
      setHeaders(hh);
    } catch (e) { setError(errText(e)); }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function detect() {
    setDetecting(true);
    try {
      const r = await clientProfileApi.detect();
      notify.ok(r.has_client ? t('settings.detectOk') : t('settings.detectNone'));
      await load();
    } catch (e) { notify.err(errText(e)); }
    finally { setDetecting(false); }
  }

  async function save() {
    setBusy(true);
    try {
      const profile: Record<string, unknown> = {};
      SCALAR_FIELDS.forEach(({key, num}) => {
        const v = form[key];
        if (v === '') return;
        profile[key] = num ? Number(v) : v;
      });
      profile.headers = {...headers};
      await clientProfileApi.save({profile});
      notify.ok(t('settings.saved'));
      await load();
    } catch (e) { notify.err(errText(e)); }
    finally { setBusy(false); }
  }

  async function setSource(source: 'auto' | 'saved') {
    try {
      await clientProfileApi.save({profile: {}, source});
      notify.ok(t('settings.saved'));
      await load();
    } catch (e) { notify.err(errText(e)); }
  }

  async function reset() {
    setBusy(true);
    try {
      await clientProfileApi.reset();
      notify.ok(t('settings.resetOk'));
      await load();
    } catch (e) { notify.err(errText(e)); }
    finally { setBusy(false); }
  }

  if (error) return <LoadError message={error} onRetry={load} />;
  if (!data) return <SkeletonBar className="h-96" />;

  const sources = (data.sources || {}) as Record<string, string>;
  const srcBadge = (key: string) => {
    const s = sources[key];
    if (s === 'saved') return <Badge variant="outline" className="border-violet-300 text-[9px] text-violet-600">{t('settings.srcSaved')}</Badge>;
    if (s === 'detected') return <Badge variant="outline" className="border-emerald-300 text-[9px] text-emerald-600">{t('settings.srcDetected')}</Badge>;
    return <Badge variant="outline" className="text-[9px] text-muted-foreground">{t('settings.srcDefault')}</Badge>;
  };

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t('settings.title')}
        description={t('settings.subtitle')}
        actions={
          <div className="flex gap-2">
            <Button size="sm" variant="outline" onClick={detect} disabled={detecting}>
              {detecting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Radar className="h-4 w-4" />}
              {t('settings.detect')}
            </Button>
            <Button size="sm" variant="outline" onClick={reset} disabled={busy}>
              <RotateCcw className="h-4 w-4" />{t('settings.reset')}
            </Button>
            <Button size="sm" onClick={save} disabled={busy}>
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              {t('settings.save')}
            </Button>
          </div>
        }
      />

      {/* 取值策略 */}
      <section className="flex flex-wrap items-center gap-4 rounded-xl border bg-card p-4">
        <div className="flex items-center gap-2">
          <Label className="text-xs text-muted-foreground">{t('settings.source')}</Label>
          <Select value={data.source || 'auto'} onValueChange={(v: string) => setSource(v as 'auto' | 'saved')}>
            <SelectTrigger className="w-48"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="auto">{t('settings.srcAuto')}</SelectItem>
              <SelectItem value="saved">{t('settings.srcPinned')}</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <p className="text-xs text-muted-foreground">
          {data.has_client ? t('settings.hasClient') : t('settings.noClient')}
        </p>
      </section>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* 标量参数 */}
        <section className="rounded-xl border bg-card p-4">
          <h2 className="mb-3 text-sm font-semibold">{t('settings.scalars')}</h2>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {SCALAR_FIELDS.map(({key, num}) => (
              <div key={key} className="space-y-1">
                <div className="flex items-center gap-1.5">
                  <Label className="font-mono text-[11px]">{key}</Label>
                  {srcBadge(key)}
                </div>
                <Input value={form[key] || ''} type={num ? 'number' : 'text'}
                       onChange={(e) => setForm((f) => ({...f, [key]: e.target.value}))}
                       className="h-8 font-mono text-xs" />
              </div>
            ))}
          </div>
        </section>

        {/* 风控头 */}
        <section className="rounded-xl border bg-card p-4">
          <h2 className="mb-1 text-sm font-semibold">{t('settings.headers')}</h2>
          <p className="mb-3 text-[11px] text-muted-foreground">{t('settings.headersHint')}</p>
          <div className="flex flex-col gap-3">
            {Object.keys(headers).map((hk) => (
              <div key={hk} className="space-y-1">
                <Label className="font-mono text-[11px]">{hk}</Label>
                <Input value={headers[hk] || ''}
                       onChange={(e) => setHeaders((h) => ({...h, [hk]: e.target.value}))}
                       className="h-8 font-mono text-xs" />
              </div>
            ))}
          </div>
        </section>
      </div>

      <p className="text-[11px] text-muted-foreground">{t('settings.footHint')}</p>
    </div>
  );
}
