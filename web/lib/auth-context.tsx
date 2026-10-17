'use client';

/**
 * 本引擎鉴权上下文：单管理员 JWT（X-Admin-Token 头）。
 *
 * 与 workbuddy-manager 的差异：manager 是 cookie 会话 + /api/me + 多用户角色；
 * 本引擎只有一个管理员（.env 的 ADMIN_USERNAME/PASSWORD），无「用户」「角色」
 * 概念，登录后拿 JWT，有效性由每个请求的 401 判定（api.ts 拦截器统一跳登录）。
 *
 * 「已登录」= sessionStorage 里有 token。token 本身不在这里校验——服务端
 * 验签才算数，前端只做「有没有」的乐观判断，401 时拦截器会清掉它。
 */
import {createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode} from 'react';
import {authApi, readToken, writeToken} from '@/lib/api';
import {BASE_PATH} from '@/lib/base-path';
import {ensureDesktopToken} from '@/lib/desktop-bridge';

interface AuthContextValue {
  /** 是否持有 token（乐观；服务端验签失败时 401 拦截器会清掉并跳登录） */
  authed: boolean;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({children}: {children: ReactNode}) {
  const [authed, setAuthed] = useState(false);
  const [loading, setLoading] = useState(true);
  const hydrated = useRef(false);

  useEffect(() => {
    if (hydrated.current) return;
    hydrated.current = true;

    // 已有 token：直接进入（这是绝大多数刷新的路径，必须同步完成，
    // 否则每次刷新都会先闪一下登录页）。
    if (readToken()) {
      setAuthed(true);
      setLoading(false);
      return;
    }

    // 桌面端（PyWebView）免密引导：本地所有权即鉴权，不该让用户再输密码。
    //
    // 注意三点（都是实测踩过的）：
    //   1. **不能靠 `?desktop=1` 判断** —— 未登录时面板会重定向到
    //      `/panel/login/`，静态导出的重定向不保留 query，标记就丢了。
    //   2. **必须真正等桥注入**（默认最多 15s + 100ms 轮询）——
    //      PyWebView 的注入可能晚于 React 首帧好几秒。
    //   3. **失败要能重试**：用 `ensureDesktopToken()`（内部多轮），
    //      而不是「试一次、失败就永远停登录页」。
    //
    // 失败一律退化成「显示登录框」——桌面引导只是便利，不是必需路径，
    // 绝不能因为它失败就把面板打成错误页。
    let cancelled = false;
    void (async () => {
      const token = await ensureDesktopToken();
      if (cancelled) return;
      if (token) {
        writeToken(token);
        setAuthed(true);
      }
      setLoading(false);
    })();
    return () => { cancelled = true; };
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const res = await authApi.login(username, password);
    writeToken(res.token);
    setAuthed(true);
  }, []);

  const logout = useCallback(() => {
    writeToken(null);
    setAuthed(false);
    if (typeof window !== 'undefined') window.location.href = `${BASE_PATH}/login`;
  }, []);

  return (
    <AuthContext.Provider value={{authed, loading, login, logout}}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth 必须在 AuthProvider 内使用');
  return ctx;
}
