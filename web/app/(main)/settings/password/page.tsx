'use client';

/**
 * 修改登录密码（旧面板 panel-settings 的等价实现）。
 *
 * 契约（web/lib/api.ts::authApi.changePassword → PATCH /api/admin/password）：
 * 提交 {old_password, new_password}，服务端校验旧密码后更新。
 * 改完**当前 JWT 仍然有效**（服务端只换密码哈希，不吊销已签发 token）——
 * 这点在页面上如实说明，避免用户以为需要重新登录。
 */
import {useCallback, useState} from 'react';
import {authApi, errText} from '@/lib/api';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {KeyRound, Loader2} from 'lucide-react';

export default function PasswordPage() {
  const t = useT();
  const [oldPwd, setOldPwd] = useState('');
  const [newPwd, setNewPwd] = useState('');
  const [confirmPwd, setConfirmPwd] = useState('');
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState('');

  const mismatch = confirmPwd.length > 0 && newPwd !== confirmPwd;
  const tooShort = newPwd.length > 0 && newPwd.length < 8;
  const canSubmit = oldPwd && newPwd && confirmPwd && !mismatch && !tooShort;

  const submit = useCallback(async () => {
    if (!canSubmit) return;
    setBusy(true);
    setHint('');
    try {
      await authApi.changePassword(oldPwd, newPwd);
      notify.ok(t('password.changed'));
      setHint(t('password.changedHint'));
      setOldPwd(''); setNewPwd(''); setConfirmPwd('');
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }, [oldPwd, newPwd, canSubmit, t]);

  return (
    <div className="flex flex-col gap-4">
      <PageHeader title={t('password.title')} description={t('password.description')} />

      <section className="max-w-md rounded-xl border bg-card p-4">
        <div className="flex flex-col gap-3">
          <div className="space-y-1.5">
            <Label htmlFor="old-pwd">{t('password.old')}</Label>
            <Input id="old-pwd" type="password" value={oldPwd} autoComplete="current-password"
                   onChange={(e) => setOldPwd(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="new-pwd">{t('password.new')}</Label>
            <Input id="new-pwd" type="password" value={newPwd} autoComplete="new-password"
                   onChange={(e) => setNewPwd(e.target.value)} />
            <p className="text-xs text-muted-foreground">{t('password.newHint')}</p>
            {tooShort && <p className="text-xs text-destructive">{t('password.tooShort')}</p>}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="confirm-pwd">{t('password.confirm')}</Label>
            <Input id="confirm-pwd" type="password" value={confirmPwd} autoComplete="new-password"
                   onChange={(e) => setConfirmPwd(e.target.value)} />
            {mismatch && <p className="text-xs text-destructive">{t('password.mismatch')}</p>}
          </div>
          <Button onClick={submit} disabled={!canSubmit || busy} className="self-start">
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <KeyRound className="h-4 w-4" />}
            {t('password.submit')}
          </Button>
          {hint && <p className="text-xs text-emerald-600 dark:text-emerald-400">{hint}</p>}
        </div>
      </section>
    </div>
  );
}
