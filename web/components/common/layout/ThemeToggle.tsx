'use client';

/**
 * 深浅色手动切换（页面右上角，与语言/版本切换并列）。
 *
 * 机制在 ThemeProvider（attribute=class + defaultTheme=system + enableSystem）：
 * 默认跟随系统；点这里在 深↔浅 之间手动切换（useThemeUtils.toggle 处理
 * system→实际系统色的解析）。图标与 tooltip 文案随当前有效主题变化。
 *
 * next-themes 在 SSR 时 theme 是 undefined（服务端不知道客户端偏好），
 * 直接渲染图标会造成水合不一致——先 mount 再出图标。
 */
import {useEffect, useState} from 'react';
import {useThemeUtils} from '@/hooks/use-theme-utils';
import {Button} from '@/components/ui/button';

export function ThemeToggle({className}: {className?: string}) {
  const {toggle, getIcon, getAction} = useThemeUtils();
  const [mounted, setMounted] = useState(false);

  useEffect(() => setMounted(true), []);

  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={toggle}
      title={mounted ? getAction() : ''}
      aria-label={mounted ? getAction() : 'toggle theme'}
      className={className}
    >
      {/* 未 mount 时渲染占位（与 Sun/Moon 同尺寸），避免布局抖动 */}
      {mounted ? getIcon('h-4 w-4') : <span className="h-4 w-4" />}
    </Button>
  );
}
