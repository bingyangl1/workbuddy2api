# 新面板：架构 / i18n / 构建 / 部署

本项目的管理面板有**两套**，可切换：

| | 旧面板（legacy） | 新面板（next） |
|---|---|---|
| 来源 | `admin/static/index.html`（单文件） | `web/`（Next.js 应用） |
| 构建 | 无需构建 | 需要 `npm run build` |
| 输出 | 直接托管 | 静态导出到 `web/out/` |
| 默认 | ✅ 服务端部署的默认 | 需显式开启 |

切换方式：

```bash
ADMIN_PANEL_STATIC=1          # 托管新面板
ADMIN_PANEL_PATH=/panel       # 挂载路径
ADMIN_DEFAULT_PANEL=next      # 默认打开哪个（legacy / next）
```

---

## 1. 技术栈

| 层 | 选型 |
|---|---|
| 框架 | **Next.js 15（静态导出）** |
| 语言 | TypeScript |
| 样式 | Tailwind CSS |
| 组件 | Radix UI primitives（无样式、可访问性完备） |
| 图标 | lucide-react |
| 图表 | 自绘（不引重型图表库） |
| 状态 | 轻量自管（不引 Redux/Zustand） |
| i18n | 自建（5 语言，见 §4） |

### 为什么选静态导出

```
NEXT_OUTPUT_EXPORT=1
NEXT_PUBLIC_BASE_PATH=/panel
```

理由：

* **不需要 Node.js 运行时** —— 产物就是纯静态文件，Python 直接托管
* 部署只需「拷 `out/` 到服务器」
* 没有 SSR，就没有 SSR 带来的额外依赖与攻击面

代价：不能用 Server Components 的运行时能力、不能用 API Routes。
对本项目（后端是 Python FastAPI）完全不构成限制。

---

## 2. 页面结构（25 个页面源文件）

> 源文件是 25 个 `page.tsx`；构建产物是 26 个 `index.html`
> （多出的一个是根路径 `/panel/` 的入口）。有测试校验这个数字。

```
web/app/
├── page.tsx                 根入口
├── (auth)/login             登录
└── (main)/
    ├── dashboard            总览
    ├── accounts             账号列表
    ├── account-groups       账号分组
    ├── keys                 密钥
    ├── models               模型
    │   └── catalog          模型目录
    ├── credits              积分
    ├── growth               成长任务
    ├── benefits             限时权益（手动运行）
    ├── playground           测试台
    ├── schedules            定时任务
    ├── logs                 调用日志
    ├── stats                统计
    ├── tokens               令牌
    ├── audit                审计
    ├── backup               备份
    ├── changelog            更新日志
    ├── security             安全设置
    └── settings             设置
        ├── sync             同步
        ├── layout           导航布局
        ├── password         改密码
        └── app-source       客户端源码浏览
```

组件 64 个、`lib/` 30 个文件。

### 导航单一事实来源

页面的导航清单只有一处：**`web/lib/app-nav.ts`**。

侧边栏、移动端抽屉、⌘K 搜索都从它读。原因：

* 三处各写一份必然不同步（曾经就出现过「⌘K 能搜到但侧边栏没有」）
* 有测试守着：`test_nav_single_source.py` 断言
  **「导航里的每个 href 都有真实页面」** 且 **「每个页面都能从导航到达」**

> 这个测试抓到过一个极隐蔽的 bug：`.gitignore` 里写了裸 `logs/`，
> 把 `web/app/(main)/logs/` **整个目录**忽略了 —— 页面从来没进过仓库。
> 本地文件在磁盘上所以看不出问题，克隆下来点 `/logs` 就 404。

---

## 3. 移动端适配

**已内置，无需额外适配。** 从桌面端到手机共用同一套组件：

* 窄屏（`< lg`）自动收起常驻侧边栏
* 顶栏显示汉堡按钮 → 全屏抽屉导航
* 抽屉 **portal 到 `<body>`**

### 为什么必须 portal

抽屉渲染在 `sticky` + `backdrop-blur` 的 header 内部时，
`fixed inset-0` 的高度会退化成 **header 的高度**（实测只有 52px），
菜单全在视口外看不见。

```tsx
{mounted && drawer && createPortal(drawer, document.body)}
```

> 测试断言的是**实际调用** `createPortal(x, document.body)`，
> 而不是「源码里出现 createPortal 字样」——后者在
> `import {createPortal}` 那行就满足了，属于假绿。

---

## 4. i18n

5 种语言：`zh-CN` / `zh-TW` / `en` / `ja` / `ko`

位置：`web/lib/i18n/locales/*.json`

### 必须用嵌套结构，不能用带点的扁平 key

```json
// ✅ 正确
{ "benefits": { "st": { "unknown": "状态未知" } } }

// ❌ 错误
{ "benefits.st.unknown": "状态未知" }
```

原因：查表是按 `split('.')` 逐层下钻的。扁平 key 永远匹配不到，
界面上会**直接显示 key 名**（`benefits.st.unknown`）。

有测试守着：`test_panel_update_ui.py::I18nCase::test_no_dotted_flat_keys`
扫描所有 locale，任何含 `.` 的顶层 key 都报错（`phrases` 段除外）。

> 这个断言抓到过**既有的** `schedules.task.*` 问题 —— 定时任务下拉框
> 一直在显示原始 key 名，只是没人注意。

---

## 5. 构建（关键：必须删两个目录）

```bash
cd web

# ⚠️ 两个目录都要删
rm -rf .next out          # PowerShell: Remove-Item .next, out -Recurse -Force

NEXT_OUTPUT_EXPORT=1 NEXT_PUBLIC_BASE_PATH=/panel npm run build
```

### 为什么两个都要删

* `.next/` 是构建缓存 —— 留着会用旧的编译结果
* `out/` 是导出产物 —— 留着会与新的混在一起

**只删一个会出现「代码改了但界面没变」的幽灵问题**，而且没有任何报错。

产物：`web/out/`，26 个页面（每个目录一个 `index.html`）。

### 环境变量

| 变量 | 作用 |
|---|---|
| `NEXT_OUTPUT_EXPORT=1` | 启用静态导出 |
| `NEXT_PUBLIC_BASE_PATH=/panel` | 资源前缀（与 `ADMIN_PANEL_PATH` 一致） |

两者不一致会导致**资源 404**（JS/CSS 全部加载失败，页面白屏）。

---

## 6. 桌面端接入（P6）

桌面版（PyWebView）打开新面板时有两个特殊问题：

### ① 免密登录

由本地启动器签发 token，通过 bridge 传给前端。三道约束见
[security.md §5](security.md#桌面端免密登录)。

### ② 桥调用必须加超时

**PyWebView 的 `js_api` 回调表在文档导航时会被销毁**。后果：

* `evaluate_js` 可能抛异常
* 返回 Promise 的桥调用**永远不会 settle**

如果代码里直接 `await` 它，UI 会永久卡在 loading。

所以 `web/lib/desktop-bridge.ts` 里所有桥调用都包了
`Promise.race([call, timeout(4s)])`，并且**每次调用单独 try/catch**
（一次 JS 抖动不该让整个探针失效）。

### ③ 判断「是不是桌面端」不能靠 URL 参数

`?desktop=1` 会被静态导出的重定向**剥掉**。正确做法是判断
**bridge 注入的全局对象是否存在**：

```ts
export function hasDesktopBridge(): boolean
```

> 踩过的完整链路：`boot.html → /panel/login/`（`?desktop=1` 已丢失）
> → 判定失败 → 停在登录页。改成桥判定后正常跳到 `/panel/dashboard/`。

---

## 7. 缓存头

静态资源必须带正确的 `Cache-Control`，否则**升级后用户看到旧界面**：

* 带 hash 的资源（`_next/static/**`）：`public, max-age=31536000, immutable`
* HTML：`no-cache, must-revalidate`

有测试守着：`test_panel_cache_headers.py`。

---

## 8. 本地开发

```bash
# 后端
python main.py

# 前端（热更新）
cd web && npm run dev
```

开发时前端跑 `localhost:3000`，通过 `next.config` 的 rewrite 把
`/api/*` 转发到后端。生产则不需要（同一端口）。

---

## 9. 与旧面板的关系

**两套并存，不是替换关系。**

* 旧面板功能完整、零构建依赖 → 保留给「不想装 Node.js」的用户
* 新面板体验更好、可维护性更高 → 推荐但有前提
* `ADMIN_DEFAULT_PANEL` 决定默认打开哪个

桌面端与 Android 端**仍以旧面板为主**（`build_android.py` 只打包
`admin/static`）——新面板需要 Node 构建，暂不进移动端产物。

> 因此 `scripts/check_runtime.py --android` **不检查 `/panel/`**。
> 早先无条件检查它，导致 APK 构建必然失败。
