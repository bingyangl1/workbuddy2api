'use client';

/**
 * 聊天测试台（P4-4）：管理员免密钥直调真实模型。
 *
 * 走 POST /api/playground/chat（号池状态机选号，真实消耗积分，记账 key_id=0 +
 * use_case=playground）。模型下拉用模型中心目录（含每个模型支持的推理档位——
 * 只列支持项，未声明档位的模型不显示档位选择）。流式渲染 + 可中断（AbortController
 * 断开即让上游流关闭）；右下角实时显示本次消耗积分（usage.credit，来自 SSE 尾帧
 * playground_usage；未返回显示「—」与「扣了 0」区分）。
 *
 * 护栏：UPSTREAM_READONLY=1 时后端拦截本调用（403），前端如实显示。
 * 对齐 routers/playground.py。
 */
import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {modelApi, errText, readToken} from '@/lib/api';
import type {CatalogModel} from '@/lib/types';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {useT} from '@/lib/i18n/provider';
import {useRealm} from '@/lib/realm-context';
import {cn} from '@/lib/utils';
import {Button} from '@/components/ui/button';
import {Textarea} from '@/components/ui/textarea';
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import {Brain, Loader2, Send, Square, Trash2} from 'lucide-react';

interface Msg {role: 'user' | 'assistant'; content: string}

interface Usage {
  credits: number | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  latency_ms: number | null;
}

export default function PlaygroundPage() {
  const t = useT();
  const {realm} = useRealm();
  const [models, setModels] = useState<CatalogModel[] | null>(null);
  const [modelId, setModelId] = useState('');
  const [effort, setEffort] = useState('');
  const [input, setInput] = useState('');
  const [messages, setMessages] = useState<Msg[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [error, setError] = useState('');
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // 加载模型目录（当前 realm，含每模型支持的推理档位）
  useEffect(() => {
    setModels(null);
    modelApi.catalog(realm).then((res) => {
      // 只列本地已启用的模型（测试台调的是网关白名单内的）
      const enabled = res.models.filter((m) => m.enabled);
      setModels(enabled);
      if (enabled.length && !enabled.some((m) => m.id === modelId)) {
        setModelId(enabled[0].id);
      }
    }).catch((e) => setError(errText(e)));
  }, [realm]);

  const current = useMemo(() => models?.find((m) => m.id === modelId) || null, [models, modelId]);
  // 该模型支持的档位（只列支持项；空 = 该模型不推理/未声明 → 不显示档位选择）
  const efforts = useMemo(() => current?.efforts ?? [], [current]);

  useEffect(() => {
    if (current) setEffort(current.default_effort || (current.efforts[0] ?? ''));
  }, [current]);

  useEffect(() => {
    scrollRef.current?.scrollTo({top: scrollRef.current.scrollHeight, behavior: 'smooth'});
  }, [messages, streaming]);

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || !modelId || streaming) return;
    setError('');
    setUsage(null);
    const nextMsgs: Msg[] = [...messages, {role: 'user', content: text}];
    setMessages(nextMsgs);
    setInput('');
    setStreaming(true);
    // 占位 assistant 消息，流式追加
    setMessages((m) => [...m, {role: 'assistant', content: ''}]);

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const resp = await fetch('/api/playground/chat', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Admin-Token': readToken() || '',
        },
        body: JSON.stringify({
          model: modelId,
          messages: nextMsgs.map((m) => ({role: m.role, content: m.content})),
          stream: true,
          ...(effort ? {reasoning_effort: effort} : {}),
        }),
        signal: ctrl.signal,
      });
      if (!resp.ok || !resp.body) {
        const txt = await resp.text().catch(() => '');
        let msg = txt;
        try { msg = JSON.parse(txt)?.detail || JSON.parse(txt)?.error?.message || txt; } catch { /* 保留原文 */ }
        throw new Error(msg || `HTTP ${resp.status}`);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      for (;;) {
        const {done, value} = await reader.read();
        if (done) break;
        buf += decoder.decode(value, {stream: true});
        // 按 SSE 帧（\n\n 分隔）切
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          for (const line of frame.split('\n')) {
            if (!line.startsWith('data:')) continue;
            const data = line.slice(5).trim();
            if (!data || data === '[DONE]') continue;
            let obj: {
              error?: {message?: string} | string;
              playground_usage?: Usage;
              choices?: {delta?: {content?: string; reasoning_content?: string}}[];
            } | null = null;
            try { obj = JSON.parse(data); } catch { continue; }
            if (!obj) continue;
            // 错误帧
            if (obj.error) {
              setError(typeof obj.error === 'string' ? obj.error : (obj.error.message || 'upstream error'));
              continue;
            }
            // 实时积分尾帧
            if (obj.playground_usage) {
              setUsage(obj.playground_usage);
              continue;
            }
            // 正文增量（content 优先，推理模型回退 reasoning_content）
            const delta = obj.choices?.[0]?.delta;
            const piece = (delta?.content || '') || (delta?.reasoning_content || '');
            if (piece) {
              setMessages((m) => {
                const copy = [...m];
                const last = copy[copy.length - 1];
                if (last && last.role === 'assistant') {
                  copy[copy.length - 1] = {...last, content: last.content + piece};
                }
                return copy;
              });
            }
          }
        }
      }
    } catch (e) {
      if ((e as Error).name === 'AbortError') {
        // 用户中断：不算错误，只标记
        setError(t('playground.aborted'));
      } else {
        setError(errText(e));
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [input, modelId, effort, messages, streaming, t]);

  const clear = useCallback(() => {
    if (streaming) return;
    setMessages([]);
    setUsage(null);
    setError('');
  }, [streaming]);

  if (!models) return <SkeletonBar className="h-96" />;

  return (
    <div className="flex h-full flex-col gap-4">
      <PageHeader title={t('playground.title')} description={t('playground.description')} />

      {/* 控制条：模型 + 推理档位（仅该模型支持时显示）+ 清空 */}
      <div className="flex flex-wrap items-center gap-2">
        <Select value={modelId} onValueChange={setModelId} disabled={streaming}>
          <SelectTrigger className="w-[220px]" aria-label={t('playground.modelLabel')}>
            <SelectValue placeholder={t('playground.modelLabel')} />
          </SelectTrigger>
          <SelectContent>
            {models.map((m) => (
              <SelectItem key={m.id} value={m.id}>
                {m.name || m.id}{m.is_free ? ` · ${t('catalog.free')}` : ''}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {efforts.length > 0 && (
          <Select value={effort} onValueChange={setEffort} disabled={streaming}>
            <SelectTrigger className="w-[150px]" aria-label={t('playground.effortLabel')}>
              <Brain className="mr-1.5 h-3.5 w-3.5 opacity-70" />
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {efforts.map((e) => (
                <SelectItem key={e} value={e}>
                  {e}{e === current?.default_effort ? ` · ${t('catalog.effortDefault')}` : ''}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        <Button size="sm" variant="outline" onClick={clear} disabled={streaming || !messages.length}>
          <Trash2 className="h-4 w-4" />{t('playground.clear')}
        </Button>
      </div>

      {/* 消息区 */}
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto rounded-lg border bg-muted/20 p-4">
        {messages.length === 0 ? (
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            {t('playground.empty')}
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {messages.map((m, i) => (
              <div key={i} className={cn('flex', m.role === 'user' ? 'justify-end' : 'justify-start')}>
                <div className={cn(
                  'max-w-[85%] whitespace-pre-wrap break-words rounded-lg px-3 py-2 text-sm',
                  m.role === 'user'
                    ? 'bg-primary text-primary-foreground'
                    : 'bg-background border',
                )}>
                  {m.content || (m.role === 'assistant' && streaming && i === messages.length - 1
                    ? <Loader2 className="h-4 w-4 animate-spin opacity-50" /> : '')}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          {error}
        </div>
      )}

      {/* 输入区 */}
      <div className="flex items-end gap-2">
        <Textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={t('playground.inputPlaceholder')}
          className="min-h-[52px] flex-1 resize-none"
          disabled={streaming}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        {streaming ? (
          <Button variant="destructive" onClick={stop} className="shrink-0">
            <Square className="h-4 w-4" />{t('playground.stop')}
          </Button>
        ) : (
          <Button onClick={send} disabled={!input.trim() || !modelId} className="shrink-0">
            <Send className="h-4 w-4" />{t('playground.send')}
          </Button>
        )}
      </div>

      {/* 右下角实时积分消耗（usage.credit；未返回显「—」与扣 0 区分） */}
      {usage && (
        <div className="fixed bottom-4 right-4 z-20 rounded-lg border bg-background/95 px-3 py-2 text-xs shadow-lg backdrop-blur">
          <div className="font-medium">{t('playground.usageTitle')}</div>
          <div className="mt-0.5 tabular-nums text-muted-foreground">
            {usage.credits == null
              ? t('playground.creditsUnknown')
              : t('playground.credits', {n: usage.credits})}
            {usage.total_tokens != null && ` · ${usage.total_tokens} tok`}
            {usage.latency_ms != null && ` · ${(usage.latency_ms / 1000).toFixed(1)}s`}
          </div>
        </div>
      )}
    </div>
  );
}
