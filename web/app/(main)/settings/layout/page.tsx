'use client';

/**
 * 界面布局配置（用户自定义菜单）。
 *
 * 能力：菜单项**显示/隐藏** + **上下移动排序** + **分组重命名** + 恢复默认 + 导入/导出。
 * 存在**后端**（`PUT /api/layout`，落在 system_settings.ui_layout），换设备/清缓存都不丢。
 *
 * ## 为什么是「差量」而不是整份菜单
 *
 * 存整份菜单的话，将来加页面时用户那份旧快照不认识新页面 —— 新页面永远不出现在
 * 他的菜单里，除非手动重置。存差量（隐藏了谁 / 顺序 / 分组名）则新页面默认可见。
 *
 * ## 交互取舍：用上下箭头而不是拖拽
 *
 * 拖拽要引 dnd 库（体积 + 移动端手势冲突 + 无障碍差）。上下箭头零依赖、
 * 键盘可用、行为确定 —— 对「调几个菜单顺序」这个频率的需求足够。
 *
 * ## 防护
 *
 * 后端会丢弃未知 href（白名单），前端这里也只操作真实条目。
 * 保存失败**不影响当前渲染**（布局是偏好，不该阻断使用）。
 */
import {useCallback, useEffect, useMemo, useState} from 'react';
import {errText} from '@/lib/api';
import {useLayout, MUST_KEEP_VISIBLE, type LayoutConfig} from '@/lib/layout-context';
import {NAV_GROUPS} from '@/lib/app-nav';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';
import {
  ArrowDown, ArrowUp, Download, Eye, EyeOff, Loader2, RotateCcw, Save, Upload,
} from 'lucide-react';

/** 默认顺序（扁平的 href 列表），用于「未自定义时」的展示。 */
const DEFAULT_ORDER: string[] = NAV_GROUPS.flatMap((g) => g.items.map((i) => i.href));

/** href → 显示名（渲染用；labelKey 走 i18n）。 */
const LABEL_OF: Record<string, string> = Object.fromEntries(
  NAV_GROUPS.flatMap((g) => g.items.map((i) => [i.href, i.labelKey])),
);
/** href → 所属分组 key。 */
const GROUP_OF: Record<string, string> = Object.fromEntries(
  NAV_GROUPS.flatMap((g) => g.items.map((i) => [i.href, g.key])),
);
const GROUP_LABEL: Record<string, string> = Object.fromEntries(
  NAV_GROUPS.map((g) => [g.key, g.labelKey]),
);

export default function LayoutPage() {
  const t = useT();
  const {layout, loaded, save, reset} = useLayout();
  const [draft, setDraft] = useState<LayoutConfig>(layout);
  const [busy, setBusy] = useState('');
  const [dirty, setDirty] = useState(false);

  // 后端布局加载完后同步到草稿（首次进入 / 保存后）
  useEffect(() => {
    if (loaded && !dirty) setDraft(layout);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded, layout]);

  /** 生效顺序：draft.order 里指定的在前（按序），其余按默认顺序。 */
  const ordered = useMemo(() => {
    const rank = new Map<string, number>();
    draft.order.forEach((h, i) => rank.set(h, i));
    const known = DEFAULT_ORDER.filter((h) => !draft.order.includes(h));
    return [
      ...draft.order.filter((h) => DEFAULT_ORDER.includes(h)),
      ...known,
    ];
  }, [draft.order]);

  const update = useCallback((next: Partial<LayoutConfig>) => {
    setDirty(true);
    setDraft((d) => ({...d, ...next}));
  }, []);

  const toggleVisible = useCallback((href: string, visible: boolean) => {
    const hidden = visible
      ? draft.hidden.filter((h) => h !== href)
      : [...draft.hidden, href];
    update({hidden});
  }, [draft.hidden, update]);

  const move = useCallback((href: string, delta: number) => {
    const cur = ordered.slice();
    const i = cur.indexOf(href);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= cur.length) return;
    [cur[i], cur[j]] = [cur[j], cur[i]];
    update({order: cur});
  }, [ordered, update]);

  const doSave = useCallback(async () => {
    setBusy('save');
    try {
      await save(draft);
      setDirty(false);
      notify.ok(t('layout.saved'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }, [draft, save, t]);

  const doReset = useCallback(async () => {
    setBusy('reset');
    try {
      await reset();
      setDirty(false);
      notify.ok(t('layout.resetOk'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }, [reset, t]);

  const doExport = useCallback(() => {
    const blob = new Blob([JSON.stringify(draft, null, 2)], {type: 'application/json'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'workbuddy2api-layout.json';
    a.click();
    URL.revokeObjectURL(url);
  }, [draft]);

  const doImport = useCallback((file: File) => {
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const data = JSON.parse(String(reader.result || '{}'));
        update({
          hidden: Array.isArray(data.hidden) ? data.hidden.filter((h: string) => DEFAULT_ORDER.includes(h)) : [],
          order: Array.isArray(data.order) ? data.order.filter((h: string) => DEFAULT_ORDER.includes(h)) : [],
          groupLabels: (data.groupLabels && typeof data.groupLabels === 'object') ? data.groupLabels : {},
        });
        notify.ok(t('layout.imported'));
      } catch {
        notify.err(t('layout.importBad'));
      }
    };
    reader.readAsText(file);
  }, [update, t]);

  if (!loaded) return <SkeletonBar className="h-96" />;

  const hiddenCount = draft.hidden.length;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t('layout.title')}
        description={t('layout.description')}
        actions={
          <div className="flex flex-wrap gap-2">
            <label className="inline-flex">
              <input type="file" accept="application/json" className="hidden"
                     onChange={(e) => { const f = e.target.files?.[0]; if (f) doImport(f); e.target.value = ''; }} />
              <Button size="sm" variant="outline" asChild>
                <span><Upload className="h-4 w-4" />{t('layout.import')}</span>
              </Button>
            </label>
            <Button size="sm" variant="outline" onClick={doExport}>
              <Download className="h-4 w-4" />{t('layout.export')}
            </Button>
            <Button size="sm" variant="outline" onClick={doReset} disabled={!!busy}>
              {busy === 'reset' ? <Loader2 className="h-4 w-4 animate-spin" /> : <RotateCcw className="h-4 w-4" />}
              {t('layout.reset')}
            </Button>
            <Button size="sm" onClick={doSave} disabled={!!busy || !dirty}>
              {busy === 'save' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              {t('layout.save')}
            </Button>
          </div>
        }
      />

      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <Badge variant="outline" className="text-[10px]">
          {t('layout.hiddenCount', {n: hiddenCount})}
        </Badge>
        <span>{t('layout.hint')}</span>
      </div>

      {/* 分组重命名 */}
      <section className="rounded-xl border bg-card p-4">
        <h2 className="mb-3 text-sm font-semibold">{t('layout.groupNames')}</h2>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {NAV_GROUPS.map((g) => (
            <div key={g.key} className="space-y-1.5">
              <Label htmlFor={`g-${g.key}`} className="text-xs text-muted-foreground">
                {t(g.labelKey)}
              </Label>
              <Input id={`g-${g.key}`} value={draft.groupLabels[g.key] ?? ''}
                     placeholder={t(g.labelKey)}
                     onChange={(e) => {
                       const v = e.target.value;
                       const next = {...draft.groupLabels};
                       if (v.trim()) next[g.key] = v; else delete next[g.key];
                       update({groupLabels: next});
                     }} />
            </div>
          ))}
        </div>
      </section>

      {/* 条目：显示/隐藏 + 排序 */}
      <section className="rounded-xl border bg-card p-4">
        <h2 className="mb-3 text-sm font-semibold">{t('layout.items')}</h2>
        <div className="flex flex-col gap-1.5">
          {ordered.map((href, idx) => {
            const visible = !draft.hidden.includes(href);
            const gkey = GROUP_OF[href] || '';
            // 配置页自己不允许隐藏（否则用户把自己锁在门外，再也进不来恢复）
            const locked = MUST_KEEP_VISIBLE.includes(href);
            return (
              <div key={href}
                   className={cn('flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2',
                                 !visible && 'opacity-55')}>
                <Switch checked={visible} disabled={locked}
                        onCheckedChange={(v) => toggleVisible(href, v)}
                        aria-label={t('layout.toggleVisible', {name: t(LABEL_OF[href] || href)})} />
                <span className="min-w-0 flex-1 truncate text-sm">
                  {t(LABEL_OF[href] || href)}
                  <span className="ml-2 font-mono text-[10px] text-muted-foreground">{href}</span>
                </span>
                {locked && (
                  <Badge variant="outline" className="text-[10px]">{t('layout.locked')}</Badge>
                )}
                <Badge variant="secondary" className="text-[10px]">{t(GROUP_LABEL[gkey] || gkey)}</Badge>
                <div className="flex gap-1">
                  <Button variant="ghost" size="sm" disabled={idx === 0}
                          aria-label={t('layout.moveUp')} onClick={() => move(href, -1)}>
                    <ArrowUp className="h-3.5 w-3.5" />
                  </Button>
                  <Button variant="ghost" size="sm" disabled={idx === ordered.length - 1}
                          aria-label={t('layout.moveDown')} onClick={() => move(href, 1)}>
                    <ArrowDown className="h-3.5 w-3.5" />
                  </Button>
                </div>
                {visible
                  ? <Eye className="h-3.5 w-3.5 text-muted-foreground" />
                  : <EyeOff className="h-3.5 w-3.5 text-muted-foreground" />}
              </div>
            );
          })}
        </div>
      </section>

      {dirty && (
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
          {t('layout.unsaved')}
        </div>
      )}
    </div>
  );
}
