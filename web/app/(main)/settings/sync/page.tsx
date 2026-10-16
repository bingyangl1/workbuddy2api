'use client';

/**
 * 线上同步（旧面板 panel-sync 的等价实现）。
 *
 * 语义：本地数据视为最新（source of truth），把本地账号 / 模型配置 / 客户端参数
 * 推送到线上实例；线上需配置相同密钥（HMAC-SHA256 签名）并通过 /api/sync/receive 接收。
 *
 * 为什么客户端参数也要同步：线上服务器没装 WorkBuddy 桌面端，探测不到 UA/版本号/风控头，
 * 只能退化成内置兜底值（线上 UA 会报一个「谁也不认识的版本」）。同步时一并推送，
 * 接收端会自动把 source 钉成 saved。对齐 routers/sync.py。
 */
import {useCallback, useEffect, useState} from 'react';
import {syncApi, errText} from '@/lib/api';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {ArrowUpRight, Eye, EyeOff, Loader2, Save} from 'lucide-react';

interface SyncConfig {
  remote_url: string;
  secret: string;
  secret_masked: string;
  is_auto_generated: boolean;
}

export default function SyncPage() {
  const t = useT();
  const [cfg, setCfg] = useState<SyncConfig | null>(null);
  const [error, setError] = useState('');
  const [url, setUrl] = useState('');
  const [secret, setSecret] = useState('');
  const [showSecret, setShowSecret] = useState(false);
  const [busy, setBusy] = useState('');
  const [result, setResult] = useState<string>('');

  const load = useCallback(async () => {
    setError('');
    try {
      const res = await syncApi.config() as unknown as SyncConfig;
      setCfg(res);
      setUrl(res.remote_url || '');
      setSecret(res.secret || '');
      if (res.is_auto_generated) {
        notify.info(t('sync.secretGenerated'));
      }
    } catch (e) {
      setError(errText(e));
    }
  }, [t]);

  useEffect(() => { load(); }, [load]);

  const save = useCallback(async () => {
    setBusy('save');
    try {
      await syncApi.saveConfig({remote_url: url.trim(), secret: secret.trim()});
      notify.ok(t('sync.saved'));
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }, [url, secret, load, t]);

  const push = useCallback(async () => {
    setBusy('push');
    setResult('');
    try {
      // 带上当前输入框的值：服务端会先落库再推送，用户不必先点保存
      const res = await syncApi.push({remote_url: url.trim(), secret: secret.trim()});
      const detail = t('sync.pushOk', {
        url: String(res.remote || ''),
        a: String(res.accounts ?? 0),
        m: String(res.models ?? 0),
      });
      notify.ok(detail);
      setResult(detail);
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }, [url, secret, t]);

  if (error) return <LoadError message={error} onRetry={load} />;
  if (!cfg) return <SkeletonBar className="h-96" />;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t('sync.title')}
        description={t('sync.description')}
        actions={
          <Button size="sm" onClick={push} disabled={!!busy || !url.trim()}>
            {busy === 'push' ? <Loader2 className="h-4 w-4 animate-spin" /> : <ArrowUpRight className="h-4 w-4" />}
            {t('sync.push')}
          </Button>
        }
      />

      <section className="flex flex-col gap-4 rounded-xl border bg-card p-4">
        <div className="space-y-1.5">
          <Label htmlFor="sync-url">{t('sync.remoteUrl')}</Label>
          <Input id="sync-url" value={url} onChange={(e) => setUrl(e.target.value)}
                 placeholder="https://online.example.com" className="font-mono" />
          <p className="text-xs text-muted-foreground">{t('sync.remoteUrlHint')}</p>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="sync-secret">{t('sync.secret')}</Label>
          <div className="flex gap-2">
            <Input id="sync-secret" value={secret} type={showSecret ? 'text' : 'password'}
                   onChange={(e) => setSecret(e.target.value)} className="font-mono" />
            <Button variant="outline" size="icon" onClick={() => setShowSecret((v) => !v)}
                    aria-label={t('sync.toggleSecret')} type="button">
              {showSecret ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            {t('sync.secretHint')}
            {cfg.secret_masked && (
              <span className="ml-2 font-mono">{t('sync.masked')}: {cfg.secret_masked}</span>
            )}
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={save} disabled={!!busy}>
            {busy === 'save' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            {t('sync.saveConfig')}
          </Button>
          {cfg.is_auto_generated && (
            <Badge variant="outline" className="text-[10px]">{t('sync.autoGenerated')}</Badge>
          )}
        </div>
      </section>

      {result && (
        <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-xs">
          {result}
        </div>
      )}

      <section className="rounded-xl border bg-card p-4 text-xs text-muted-foreground">
        <h2 className="mb-2 text-sm font-semibold text-foreground">{t('sync.howItWorks')}</h2>
        <ul className="list-disc space-y-1 pl-4">
          <li>{t('sync.how1')}</li>
          <li>{t('sync.how2')}</li>
          <li>{t('sync.how3')}</li>
        </ul>
      </section>
    </div>
  );
}
