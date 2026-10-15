'use client';

/**
 * Changelog 查看器（P5）：读部署目录 CHANGELOG.md（经 /api/app/changelog），
 * 离线可用，按版本折叠（<details>），标记当前版本。
 *
 * Keep a Changelog 结构：版本段 → 小节（新增/修复/改进/安全）→ 条目。
 * 对齐 routers/releases.py::changelog。
 */
import {useEffect, useState} from 'react';
import {systemApi, errText} from '@/lib/api';
import type {ChangelogResponse, ChangelogSection} from '@/lib/types';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';
import {Badge} from '@/components/ui/badge';
import {ChevronRight, Tag} from 'lucide-react';

/** 小节标题 → 配色（新增/修复/改进/安全 各有语义色）。 */
function sectionTone(title: string): string {
  const s = title.toLowerCase();
  if (/(安全|security)/.test(s)) return 'border-red-500/40 bg-red-500/10 text-red-600 dark:text-red-400';
  if (/(修复|fix|bug)/.test(s)) return 'border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400';
  if (/(改进|improve|perf|优化)/.test(s)) return 'border-blue-500/40 bg-blue-500/10 text-blue-600 dark:text-blue-400';
  return 'border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'; // 新增/默认
}

export default function ChangelogPage() {
  const t = useT();
  const [data, setData] = useState<ChangelogResponse | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    systemApi.changelog().then(setData).catch((e) => setError(errText(e)));
  }, []);

  if (error) return <LoadError message={error} onRetry={() => systemApi.changelog().then(setData).catch((e) => setError(errText(e)))} />;
  if (!data) return <SkeletonBar className="h-96" />;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t('changelog.title')}
        description={t('changelog.description', {version: data.current_version})}
      />

      {data.versions.length === 0 ? (
        <EmptyState title={t('changelog.empty')} />
      ) : (
        <div className="flex flex-col gap-3">
          {data.versions.map((v, i) => {
            const isCurrent = v.version === data.current_version;
            const isUnreleased = v.version.toLowerCase() === 'unreleased';
            return (
              <details key={v.version + i} open={i === 0}
                       className="group rounded-lg border bg-card">
                <summary className="flex cursor-pointer list-none flex-wrap items-center gap-x-2 gap-y-1 px-4 py-3 [&::-webkit-details-marker]:hidden">
                  <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" />
                  <Tag className="h-4 w-4 shrink-0 text-muted-foreground" />
                  <span className="font-semibold">
                    {isUnreleased ? t('changelog.unreleased') : `v${v.version}`}
                  </span>
                  {v.date && <span className="text-xs text-muted-foreground">{v.date}</span>}
                  {isCurrent && (
                    <Badge className="ml-1 text-[10px]">{t('changelog.current')}</Badge>
                  )}
                  {isUnreleased && (
                    <Badge variant="outline" className="ml-1 text-[10px]">{t('changelog.unreleasedBadge')}</Badge>
                  )}
                  <span className="ml-auto text-xs text-muted-foreground">
                    {t('changelog.itemCount', {n: v.sections.reduce((a, s) => a + s.items.length, 0)})}
                  </span>
                </summary>
                <div className="flex flex-col gap-4 border-t px-4 py-3">
                  {v.sections.map((s: ChangelogSection, j) => (
                    <div key={j}>
                      <span className={cn('inline-block rounded border px-1.5 py-0.5 text-[11px] font-medium', sectionTone(s.title))}>
                        {s.title}
                      </span>
                      <ul className="mt-2 flex list-disc flex-col gap-1.5 pl-5 text-sm text-foreground/90">
                        {s.items.map((item, k) => (
                          <li key={k} className="leading-relaxed [overflow-wrap:anywhere]">
                            <InlineMd text={item} />
                          </li>
                        ))}
                      </ul>
                    </div>
                  ))}
                </div>
              </details>
            );
          })}
        </div>
      )}
    </div>
  );
}

/**
 * 轻量行内 Markdown：只支持 CHANGELOG 实际用到的两种——
 * `**加粗**` 与 `` `代码` ``。不引第三方库（离线优先、体积可控），
 * 其余原样输出（不做 HTML 注入，全部走 React 文本节点）。
 */
function InlineMd({text}: {text: string}) {
  // 先按 `代码` 切，再在每段里按 **加粗** 切
  const parts = text.split(/(`[^`]+`)/g);
  return (
    <>
      {parts.map((part, i) => {
        if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
          return (
            <code key={i} className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em] [overflow-wrap:anywhere]">
              {part.slice(1, -1)}
            </code>
          );
        }
        const chunks = part.split(/(\*\*[^*]+\*\*)/g);
        return chunks.map((chunk, j) => {
          if (chunk.startsWith('**') && chunk.endsWith('**') && chunk.length > 4) {
            return <strong key={`${i}-${j}`} className="font-semibold">{chunk.slice(2, -2)}</strong>;
          }
          return chunk ? <span key={`${i}-${j}`}>{chunk}</span> : null;
        });
      })}
    </>
  );
}
