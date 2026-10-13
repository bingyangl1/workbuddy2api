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
    setAuthed(!!readToken());
    setLoading(false);
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
