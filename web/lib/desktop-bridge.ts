'use client';

/**
 * 桌面端（PyWebView）桥接：免密登录 + 版本更新。
 *
 * ## 为什么需要
 *
 * 桌面版 EXE 是本机单用户程序，「本地所有权」本身就是鉴权 —— 用户不该再输
 * 一遍用户名密码。旧面板靠 `admin/static/desktop.js` 做这件事：
 *
 *     const {token} = await pywebview.api.desktop_login()
 *     localStorage.setItem(TOKEN_KEY, token)
 *
 * 新面板（`/panel`）没有这段逻辑，于是桌面端打开新面板会停在登录页 ——
 * 表现是「桌面版打不开后台」，而同样代码在浏览器里输密码却正常，
 * 很容易误判成新面板本身的 bug。
 *
 * 同理，「版本与更新」的**下载/安装**动作也只能由桌面端执行（浏览器里没有
 * 权限替换 EXE），所以这部分也必须走桥。
 *
 * ## 安全边界（后端 `desktop_login` 也已校验，双保险）
 *
 * 免密只在**同时满足**下面三条时才发生：
 *
 *   1. 页面跑在 PyWebView 里（存在 `window.pywebview.api.desktop_login`）
 *   2. URL 带 `?desktop=1`（桌面端自己打开的标记，可能被重定向剥掉）
 *   3. 后端再次校验「当前窗口 URL 是本机回环 + 本进程端口 + 白名单路径」
 *
 * 只做第 1 条是不够的：网页里也能伪造 `window.pywebview`。所以后端那次
 * 校验才是真正的闸门；前两条只是「不要白跑一趟」的快速判断。
 */

/**
 * 类型来自 `lib/types.ts`（与 `/api/app/releases` 共用同一份契约）。
 * 这里**重新导出**，让调用方只 import 一个模块即可。
 */
import type {ReleaseRow, ReleasesPayload, UpdateState} from '@/lib/types';
// `writeToken` 来自 api.ts（那边负责 sessionStorage 的唯一写入点）。
// 已确认 api.ts **不**引用 desktop-bridge，所以这里不会有循环依赖。
import {writeToken} from '@/lib/api';

export type {ReleaseRow, ReleasesPayload, UpdateState};

/** PyWebView 暴露的桥（只声明我们用到的部分）。 */
interface PyWebViewApi {
  desktop_login?: () => Promise<{token?: string}>;
  check_updates?: () => Promise<ReleasesPayload>;
  update_status?: () => Promise<UpdateState & {runtime?: string}>;
  download_update?: (releaseId: number) => Promise<{error?: string}>;
  apply_update?: () => Promise<{error?: string}>;
  retry_runtime?: () => Promise<{ok?: boolean}>;
  /** 桌面端当前窗口 URL（PyWebView 自带，用于诊断） */
  [key: string]: unknown;
}

interface PyWebViewBridge {
  api?: PyWebViewApi;
}

declare global {
  interface Window {
    pywebview?: PyWebViewBridge;
    /** PyWebView 注入完成时会派发这个事件。 */
    __wbDesktopReady?: boolean;
  }
}

/** 是否由桌面端打开。
 *
 * **不能只靠 `?desktop=1`**（实测踩过）：新面板未登录时会把 `/panel?desktop=1`
 * 重定向到 `/panel/login/`，而**静态导出的重定向不保留 query** ——
 * 于是 `location.search` 变成空字符串，免密引导判断失败 →
 * 桌面端永远停在登录页。
 *
 * 所以判据改成「**桥存在** 或 URL 带 desktop=1」：
 *
 *   * 桥存在 = 这个页面确实跑在 PyWebView 里（网页无法凭空造出
 *     后端注入的 js_api，而真正的闸门在后端那次 URL 校验）
 *   * desktop=1 = 显式标记（兼容旧行为 / 便于调试）
 *
 * 两者取「或」：`/panel/login/` 这类丢了 query 的页面也能正确识别。
 */
export function isDesktopLaunch(): boolean {
  if (typeof window === 'undefined') return false;
  if (hasDesktopBridge() || hasAndroidBridge()) return true;
  try {
    return new URLSearchParams(window.location.search).get('desktop') === '1';
  } catch {
    return false;
  }
}

/** 桥是否已就绪（PyWebView 注入完成）。 */
export function hasDesktopBridge(): boolean {
  if (typeof window === 'undefined') return false;
  return typeof window.pywebview?.api?.desktop_login === 'function';
}

/**
 * 安卓壳（APK）的桥是否就绪。
 *
 * ## 与桌面端的区别
 *
 * | | 桌面端（PyWebView） | 安卓（WebView + Java） |
 * |---|---|---|
 * | 注入对象 | `window.pywebview.api` | `window.WBMobile` |
 * | 免密方式 | `pywebview.api.desktop_login()` | Java 主动调 `WBMobile.initialize(token)` |
 * | 谁发起 | JS 主动请求 | **Java 主动推送** |
 *
 * 所以安卓端**不需要**我们去「请求 token」—— Java 那边在
 * `onPageFinished` 里直接调 `WBMobile.initialize(token)`，
 * 由壳内脚本把 token 写进 `localStorage`。
 *
 * 新面板要做的只有一件事：**认出自己在安卓壳里**，
 * 然后等壳把 token 塞进来（见 `waitForAndroidToken`）。
 */
export function hasAndroidBridge(): boolean {
  if (typeof window === 'undefined') return false;
  const w = window as unknown as {
    WBMobile?: unknown;
    WBAndroid?: unknown;
  };
  // `WBAndroid` 是 `addJavascriptInterface` 注入的原生接口（Java 侧一定先注入），
  // `WBMobile` 是页面脚本包装。两者任一存在即认为在安卓壳里。
  return Boolean(w.WBAndroid) || Boolean(w.WBMobile);
}

/**
 * 等安卓壳把 token 写进 localStorage（最多 `timeoutMs`）。
 *
 * Java 的时序：`onPageFinished` → `evaluateJavascript("WBMobile.initialize(tok)")`
 * → 旧面板脚本写 `localStorage['wb_admin_token']`。
 *
 * 新面板用自己的一套：等待 `wb-admin-token` 出现即可（壳会写到这个 key，
 * 见 `android/web/mobile.js` 的 `TOKEN_KEY` 兼容处理）。
 */
export function waitForAndroidToken(timeoutMs = 15000): Promise<boolean> {
  if (typeof window === 'undefined') return Promise.resolve(false);
  const KEY = 'wb-admin-token';
  try {
    if (window.localStorage.getItem(KEY)) return Promise.resolve(true);
  } catch {
    return Promise.resolve(false);
  }
  return new Promise((resolve) => {
    let done = false;
    let poll = 0;
    let timer = 0;
    const finish = (ok: boolean) => {
      if (done) return;
      done = true;
      window.clearInterval(poll);
      window.clearTimeout(timer);
      resolve(ok);
    };
    poll = window.setInterval(() => {
      try {
        if (window.localStorage.getItem(KEY)) finish(true);
      } catch {
        finish(false);
      }
    }, 200);
    timer = window.setTimeout(() => finish(false), timeoutMs);
  });
}

/**
 * 等桥就绪（最多 `timeoutMs`，默认 15 秒）。
 *
 * PyWebView 的注入时机是「页面加载后」，**可能晚于 React 首次渲染**，而且
 * 在我们这个场景里还要等后端起完 uvicorn、窗口再导航到面板 —— 实测注入
 * 可能晚好几秒。所以这里**同时**用三种手段：
 *
 *   1. 监听 `pywebviewready`（官方事件，最快）
 *   2. **短间隔轮询** `window.pywebview`（事件可能在监听器挂上之前就派发过了，
 *      或该 WebView2 版本根本没派发 —— 只靠事件会永久卡住）
 *   3. 超时兜底
 *
 * 历史教训：最初只监听事件 + 3 秒超时，结果「事件早已错过 + 桥晚于 3 秒」
 * 两条同时成立 → 一直拿不到 token，桌面版停在登录页。
 */
export function waitForDesktopBridge(timeoutMs = 15000): Promise<boolean> {
  if (typeof window === 'undefined') return Promise.resolve(false);
  if (hasDesktopBridge()) return Promise.resolve(true);
  return new Promise((resolve) => {
    let done = false;
    let timer = 0;
    let poll = 0;
    const finish = (ok: boolean) => {
      if (done) return;
      done = true;
      window.removeEventListener('pywebviewready', onReady);
      window.clearTimeout(timer);
      window.clearInterval(poll);
      resolve(ok);
    };
    const onReady = () => finish(hasDesktopBridge());
    window.addEventListener('pywebviewready', onReady);
    // 轮询：即便事件没来，桥出现后也能立刻发现（100ms 足够细，开销可忽略）
    poll = window.setInterval(() => {
      if (hasDesktopBridge()) finish(true);
    }, 100);
    timer = window.setTimeout(() => finish(hasDesktopBridge()), timeoutMs);
  });
}

/**
 * 进行中的请求（**单例去重**）。
 *
 * 为什么必须去重：`AuthProvider` 与登录页都会尝试免密登录，两者几乎同时
 * 发起 → PyWebView 的 js_api 回调表出现两条同函数名的并发返回
 * → 报 `_returnValuesCallbacks.desktop_login.xxx is not a function`
 * → 两条都失败，token 反而拿不到。
 *
 * 去重后：第二个调用者直接复用第一次的 Promise。
 */
let inflight: Promise<string | null> | null = null;

/** 已拿到的 token（**成功后缓存**，避免重复走桥）。 */
let cachedToken: string | null = null;

/** 与 `lib/api.ts` 的 TOKEN_KEY 保持一致（那是唯一事实来源，这里只读不改）。 */
const TOKEN_STORAGE_KEY = 'wb-admin-token';

/**
 * 向桌面端索取免密 token；拿不到返回 null。
 *
 * **判据是「桥是否存在」，不是「URL 有没有 desktop=1」**：面板未登录时会
 * 重定向到 `/login/`，而静态导出的重定向**不保留 query** —— 靠 URL 判断会漏。
 * 桥本身只有 PyWebView 能注入，网页造不出来；真正的闸门还在后端那次
 * 「当前窗口 URL 是否本机回环 + 白名单路径」的校验。
 *
 * **绝不抛异常**：桌面引导失败不能把整个面板打成错误页 ——
 * 退化成「显示登录框、用户手输密码」仍然可用。
 */
export async function requestDesktopToken(): Promise<string | null> {
  if (cachedToken) return cachedToken;
  if (inflight) return inflight;
  inflight = (async () => {
    const ok = await waitForDesktopBridge();
    if (!ok) return null;
    if (typeof window === 'undefined' || !window.pywebview?.api?.desktop_login) {
      return null;
    }
    try {
      // ⚠️ **必须给桥调用加超时**（本项目实测踩过的坑）：
      // PyWebView 的 js_api 回调依赖「发起调用那个文档」的
      // `_returnValuesCallbacks` 表。面板加载过程会经历
      // `/panel?desktop=1` → `/panel/login/` 的导航，回调表随文档销毁，
      // 于是这个 Promise **永远不 settle** —— `await` 直接挂死，
      // 后续代码（写 token、关 loading）一行都不会执行。
      //
      // 表现极具迷惑性：桥是 function、手动调用能成功，但 React 那段
      // 代码「看起来没跑」（其实卡在 await 上）。
      const res = await Promise.race([
        window.pywebview.api.desktop_login(),
        new Promise<null>((resolve) => {
          window.setTimeout(() => resolve(null), 4000);
        }),
      ]);
      const token = res && typeof (res as {token?: string}).token === 'string'
        ? (res as {token: string}).token
        : '';
      if (token) cachedToken = token;
      return token || null;
    } catch {
      return null;
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}

/**
 * 在桌面端里**反复尝试**直到拿到 token（登录页用，最可靠）。
 *
 * 登录页是稳定页面（不再导航，PyWebView 的回调表不会被销毁），适合多试几次：
 * 桥晚到、第一次调用赶上导航时机，都能自愈。
 *
 * ⚠️ **不要加「几次失败就放弃」的提前返回**（曾经踩过）：
 * 早先这里是 `if (!hasDesktopBridge() && i >= 2) return null`，
 * 想「不是桌面端就别白等」。但 PyWebView 的桥**可能晚于 4.5 秒才注入**
 * （要等后端起 uvicorn + 窗口导航 + WebView2 注入），于是正常的桌面端
 * 也被判成「不是桌面端」→ 永久放弃 → 一直停在登录页。
 *
 * 现在改成：耐心等（每轮 `requestDesktopToken` 内部自己会等 15s），
 * 总时长上限由 `attempts × gapMs` 控制；真不是桌面端也只是白等几秒，
 * 而误判的代价是用户每次都要手输密码。
 */
export async function ensureDesktopToken(
  attempts = 10,
  gapMs = 1500,
): Promise<string | null> {
  for (let i = 0; i < attempts; i += 1) {
    if (typeof window === 'undefined') return null;
    // 已有 token（别的路径已经写成功）→ 直接用，不必再问桥。
    if (cachedToken) return cachedToken;
    if (readTokenSafe()) return cachedToken;

    // ── 安卓壳分支 ────────────────────────────────────────────────
    //
    // 安卓**不通过 pywebview 请求 token** —— 是 Java 在 `onPageFinished`
    // 里主动调 `WBMobile.initialize(token)`，由壳内脚本写进 localStorage。
    //
    // 所以这里只需要「等它写进来」。等到了就直接用（同时补写
    // sessionStorage，之后 `readTokenSafe()` 就能命中）。
    if (hasAndroidBridge()) {
      const ok = await waitForAndroidToken(gapMs * 2);
      if (ok) {
        try {
          const t = window.localStorage.getItem('wb-admin-token');
          if (t) {
            writeToken(t);
            return t;
          }
        } catch {
          /* localStorage 不可用 —— 落回下面的轮询 */
        }
      }
      // 安卓端没有「问桥」这一步，直接进下一轮等待
      await new Promise((r) => window.setTimeout(r, gapMs));
      continue;
    }

    const token = await requestDesktopToken();
    if (token) return token;
    await new Promise((r) => window.setTimeout(r, gapMs));
  }
  return null;
}

/** 读 sessionStorage 里是否已有 token（避免重复问桥）。 */
function readTokenSafe(): boolean {
  try {
    return !!window.sessionStorage.getItem(TOKEN_STORAGE_KEY);
  } catch {
    return false;
  }
}

// ─────────────────────────────────────────────────────────────────────
// 版本与更新
// ─────────────────────────────────────────────────────────────────────

/**
 * 调用桥上的某个方法，带超时保护。
 *
 * 与 `requestDesktopToken` 同一个原因：PyWebView 的 js_api 回调依赖
 * 「发起调用那个文档」的回调表，文档一旦导航，Promise 就永不 settle ——
 * 不加超时会让 UI 永久卡在「检查中…」。
 *
 * 返回 `undefined` 表示「桥不可用 / 超时 / 抛错」，调用方据此决定是否降级。
 */
async function callBridge<T>(
  method: keyof PyWebViewApi,
  timeoutMs = 20000,
  ...args: unknown[]
): Promise<T | undefined> {
  if (typeof window === 'undefined') return undefined;
  const api = window.pywebview?.api;
  const fn = api?.[method];
  if (typeof fn !== 'function') return undefined;
  try {
    const timeout = new Promise<undefined>((resolve) => {
      window.setTimeout(() => resolve(undefined), timeoutMs);
    });
    const call = (fn as (...a: unknown[]) => Promise<T>).apply(api, args);
    return await Promise.race([call, timeout]);
  } catch {
    return undefined;
  }
}

/** 当前是否跑在桌面端（能执行下载/安装）。 */
export function isNativeShell(): boolean {
  return typeof window !== 'undefined'
    && typeof window.pywebview?.api?.apply_update === 'function';
}

/**
 * 检查更新。
 *
 * 桌面端走桥（能拿到 `runtime` / `update` 这些本机状态）；
 * 浏览器/服务端部署退化为 `GET /api/app/releases`（同一份数据，只是没有本机动作）。
 *
 * 两者字段一致，所以上层 UI 不需要分叉。
 *
 * ⚠️ 降级分支**必须走 api 客户端**（`systemApi.releases()`），不能用裸
 * `fetch('/api/app/releases')` —— 那个端点要 `X-Admin-Token`，裸 fetch 不会带，
 * 结果是 401 且 UI 上表现为「检查更新失败」，很难看出是漏了鉴权。
 */
export async function checkUpdates(): Promise<ReleasesPayload> {
  if (isNativeShell()) {
    const native = await callBridge<ReleasesPayload>('check_updates', 30000);
    if (native && typeof native.version === 'string') return native;
    // 桥在但调用失败：不要静默退回服务端（那会少了本机状态）——
    // 继续走下面的只读接口，至少让用户看到版本与历史。
  }
  const {systemApi} = await import('@/lib/api');
  return systemApi.releases();
}

/** 轮询下载/安装状态（仅桌面端有意义；非桌面端返回 undefined）。 */
export async function pollUpdateStatus(): Promise<
  (UpdateState & {runtime?: string}) | undefined
> {
  return callBridge<UpdateState & {runtime?: string}>('update_status', 8000);
}

/** 下载更新（仅桌面端）。返回 `{error}` 或空对象。 */
export async function downloadUpdate(
  releaseId: number,
): Promise<{error?: string}> {
  const r = await callBridge<{error?: string}>('download_update', 60000, releaseId);
  return r ?? {error: '桌面端未响应下载请求'};
}

/** 重启并安装更新（仅桌面端）。 */
export async function applyUpdate(): Promise<{error?: string}> {
  const r = await callBridge<{error?: string}>('apply_update', 20000);
  return r ?? {error: '桌面端未响应安装请求'};
}

/** 重试准备运行环境（Node.js，仅桌面端）。 */
export async function retryRuntime(): Promise<void> {
  await callBridge<{ok?: boolean}>('retry_runtime', 10000);
}

