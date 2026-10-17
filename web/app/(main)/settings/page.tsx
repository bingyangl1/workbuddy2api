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
import {useRealm} from '@/lib/realm-context';
import {cn} from '@/lib/utils';
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
import {Loader2, Radar, RotateCcw, Save, ScrollText} from 'lucide-react';
import {BASE_PATH} from '@/lib/base-path';

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
  /**
   * 档案**按版本编辑**。
   *
   * 两版客户端身份本质不同（解包实测）：
   *   cn     applicationName=WorkBuddy    channel=109144  ver=5.8.3
   *   global applicationName=workbuddy-ai channel=400111  ver=5.8.4
   *
   * 编辑对象跟随右上角的版本切换；下方「两版一览」始终显示两版并排摘要，
   * 这样用户不必来回切换就能发现「另一版还没配」（漏配会导致那一版的账号
   * 带着错的客户端身份出站 —— 上游一比对就知道不是官方客户端）。
   */
  const {realm, label: realmLabel} = useRealm();
  const [data, setData] = useState<ClientProfile | null>(null);
  const [error, setError] = useState('');
  const [form, setForm] = useState<Record<string, string>>({});
  const [headers, setHeaders] = useState<Record<string, string>>({});
  // #8 新增自定义风控头的键名输入
  const [newHeaderKey, setNewHeaderKey] = useState('');

  /** 添加一个新头（键名规范化：去空白；重名不覆盖已有值）。 */
  function addHeader() {
    const k = newHeaderKey.trim();
    if (!k) return;
    setHeaders((h) => (k in h ? h : {...h, [k]: ''}));
    setNewHeaderKey('');
  }
  const [busy, setBusy] = useState(false);
  const [detecting, setDetecting] = useState(false);

  const load = useCallback(async () => {
    setError('');
    try {
      const r = await clientProfileApi.get(realm);
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
  }, [realm]);

  useEffect(() => { load(); }, [load]);

  async function detect() {
    setDetecting(true);
    try {
      // 探测**对应版本**的安装目录（cn=WorkBuddy / global=WorkBuddyAI）
      const r = await clientProfileApi.detect(realm);
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
      await clientProfileApi.save({profile}, realm);
      notify.ok(t('settings.saved'));
      await load();
    } catch (e) { notify.err(errText(e)); }
    finally { setBusy(false); }
  }

  async function setSource(source: 'auto' | 'saved') {
    try {
      await clientProfileApi.save({profile: {}, source}, realm);
      notify.ok(t('settings.saved'));
      await load();
    } catch (e) { notify.err(errText(e)); }
  }

  async function reset() {
    setBusy(true);
    try {
      // 只重置当前版本 —— 不会动另一版
      await clientProfileApi.reset(realm);
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
          // flex-wrap：375px 下四个按钮排不下，必须换行（否则页面横向溢出）
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="ghost" asChild>
              <a href={`${BASE_PATH}/changelog/`}><ScrollText className="h-4 w-4" />{t('nav.changelog')}</a>
            </Button>
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

      {/*
        当前编辑的版本 + 两版一览。
        两版参数必须各存各用；界面上如果只能一版一版切换着看，
        很容易漏配另一版还不自知（那一版的账号会带错的客户端身份出站）。
        所以把两版并排放在最上方，一眼能看到「另一版配了没、配的对不对」。
      */}
      <section className="rounded-lg border bg-card/40 p-3">
        <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
          <span className="text-muted-foreground">{t('settings.editingRealm')}</span>
          <Badge>{realmLabel}</Badge>
          <span className="text-muted-foreground">
            {t('settings.realmSwitchHint')}
          </span>
        </div>
        <div className="grid gap-2 md:grid-cols-2">
          {(['cn', 'global'] as const).map((r) => {
            const brief = data?.realms?.[r];
            const isCurrent = r === realm;
            return (
              <div key={r}
                className={cn('rounded-md border p-2 text-xs',
                  isCurrent ? 'border-primary/60 bg-primary/5' : 'bg-background/40')}>
                <div className="mb-1 flex items-center gap-2">
                  <Badge variant={isCurrent ? 'default' : 'outline'}>
                    {r === 'cn' ? t('realm.cn') : t('realm.global')}
                  </Badge>
                  {brief?.has_detected
                    ? <Badge variant="secondary">{t('settings.detected')}</Badge>
                    : <Badge variant="outline">{t('settings.notDetected')}</Badge>}
                  {brief?.source && (
                    <span className="text-muted-foreground">
                      {brief.source === 'saved' ? t('settings.sourceSaved') : t('settings.sourceAuto')}
                    </span>
                  )}
                </div>
                {brief ? (
                  <dl className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5">
                    <dt className="text-muted-foreground">application_name</dt>
                    <dd className="truncate">{brief.application_name ?? '—'}</dd>
                    <dt className="text-muted-foreground">turing_channel_id</dt>
                    <dd>{brief.turing_channel_id ?? '—'}</dd>
                    <dt className="text-muted-foreground">desktop_version</dt>
                    <dd>{brief.desktop_version ?? '—'}</dd>
                    <dt className="text-muted-foreground">UA</dt>
                    <dd className="truncate" title={brief.ua}>{brief.ua ?? '—'}</dd>
                  </dl>
                ) : <p className="text-muted-foreground">—</p>}
              </div>
            );
          })}
        </div>
      </section>

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
                <div className="flex items-center gap-1">
                  <Label className="font-mono text-[11px]">{hk}</Label>
                  {/* 自定义头可删除（引擎的四个归属头 X-IDE-* / X-Product 不给删，
                      它们是官方形态的一部分，删了反而暴露非官方特征） */}
                  {!(hk.startsWith('X-IDE-') || hk === 'X-Product') && (
                    <Button size="sm" variant="ghost" className="ml-auto h-5 px-1.5 text-[10px] text-red-500"
                            title={t('settings.headerRemove')}
                            onClick={() => setHeaders((h) => {
                              const n = {...h};
                              delete n[hk];
                              return n;
                            })}>
                      ×
                    </Button>
                  )}
                </div>
                <Input value={headers[hk] || ''}
                       onChange={(e) => setHeaders((h) => ({...h, [hk]: e.target.value}))}
                       className="h-8 font-mono text-xs" />
              </div>
            ))}
            {/* #8 新增自定义头：上游出新风控头时能立即跟上，
                不用等发版。键名输入 + 添加按钮。 */}
            <div className="flex items-center gap-2 border-t pt-3">
              <Input value={newHeaderKey}
                     onChange={(e) => setNewHeaderKey(e.target.value)}
                     placeholder={t('settings.newHeaderKeyPh')}
                     className="h-8 flex-1 font-mono text-xs"
                     onKeyDown={(e) => {
                       if (e.key === 'Enter' && newHeaderKey.trim()) addHeader();
                     }} />
              <Button size="sm" variant="outline" disabled={!newHeaderKey.trim()}
                      onClick={addHeader}>
                {t('settings.addHeader')}
              </Button>
            </div>
          </div>
        </section>
      </div>

      <p className="text-[11px] text-muted-foreground">{t('settings.footHint')}</p>
    </div>
  );
}
