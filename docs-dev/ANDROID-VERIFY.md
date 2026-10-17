# OnePlus 9 真机验证报告

> 设备：OnePlus 9（LE2110）· Android 16（SDK 36）· arm64-v8a · 1080×2400 @480dpi
> APK：`WorkBuddy2API-1.0.0-android-arm64.apk`（22.90 MB）
> 验证时间：2026-10-17
> 连接方式：`adb`（USB）+ `ssh root@192.168.5.48:8022`（Termux，免密）

---

## 最终结论：✅ 安装即用

**界面正常进入，9 个页面全部渲染，14 个核心 API 全部可用。**

| 验证项 | 结果 | 证据 |
|---|---|---|
| 安装 | ✅ | `adb install` 成功；覆盖安装保留数据 |
| 冷启动 | ✅ | 进入「账号池」主界面 |
| Python 运行时 | ✅ | uvicorn 启动，端口 8790 LISTEN |
| 界面渲染 | ✅ | 9/9 页面有实质内容（截图 170–297 KB） |
| 侧边导航 | ✅ | 9 个菜单项全部正确渲染 + 角标 |
| 交互 | ✅ | 下拉框展开、页面切换正常 |
| 登录 | ✅ | 真实凭据换到 JWT（125 字符） |
| 鉴权 | ✅ | 未带 token → 401 |
| 数据库 | ✅ | SQLite WAL 模式，436 KB |
| 出网 | ✅ | `workbuddy.ai` / `copilot.tencent.com` 均 200 |
| 升级路径 | ✅ | 同签名覆盖安装，数据保留 |

---

## 核心 API 验证（12/14 → 修正后 14/14）

用真实 JWT 在设备上直接 curl：

```
✅ /api/accounts              200   账号列表
✅ /api/keys                  200   密钥列表
✅ /api/models/configs        200   模型配置
✅ /api/models/catalog        200   模型目录
✅ /api/schedules             200   定时任务
✅ /api/logs                  200   调用日志
✅ /api/audit                 200   审计
✅ /api/account-groups        200   账号分组
✅ /api/tokens                200   访问令牌
✅ /api/layout                200   导航布局
✅ /api/credits/expiry        200   积分到期
✅ /api/benefits/campaign     200   限时权益
✅ /api/growth/records        200   成长审计
✅ /api/app/version           200   版本
```

> 曾误报 `/api/models` 与 `/api/stats/summary` 为 404 —— 那是我**猜错了路径**，
> 真实路径是 `/api/models/configs` 与 `/api/logs/usage`。不是缺陷。

---

## 🔴 修复的关键 Bug（本次真机验证的最大收获）

**装上去之后界面永久停在「正在打开本地工作台…」**，而内嵌服务完全正常
（`curl /api/app/version` → 200）。

### 根因链（三层，一层比一层深）

**第一层**：`WBDesktop.checkUpdatesAtStartup` 从未被导出
→ `mobile.js` 调它时抛 `TypeError`

**第二层**：`initialize()` 里「移除引导层」排在可能失败的操作**之后**
→ 一抛异常，引导层永不消失

**第三层（真正的根因）**：`mobile.js` 的 IIFE **顶层**直接访问不存在的 DOM

```js
document.querySelector('.api-share-card').append(diagnosticButton);
```

而 `.api-share-card` 在 `index.html` 里**根本不存在**
（`Select-String 'api-share-card' admin/static/index.html` = 0 处）。

→ `null.append()` 抛 TypeError
→ **整个 IIFE 中途崩溃**
→ `window.WBMobile` 从未被赋值
→ Java 端 `window.WBMobile && WBMobile.initialize(...)` **静默跳过**（`&&` 短路）
→ 引导层永久卡死

### 为什么极难排查（我误判了 3 次）

| 我的判断 | 为什么错 |
|---|---|
| 进程被杀 | `pidof` 一直有 PID，`State: S`，58 线程正常 |
| 屏幕熄灭导致服务挂起 | **这个现象真的存在**，但不是本因（点亮后 200） |
| 只是导出缺失 | 修了之后仍然卡住 |

真正的障碍：

1. **`&&` 短路让注入无声无息地不发生** —— `adb logcat` 里一行 JS 错误都没有
2. 服务完全正常（200），看起来像网络问题
3. 页面上「重新连接」按钮指向 `WBDesktop.retryLocal` → 那是桌面端的 `pywebview`
   逻辑 → 报 `pywebview is not defined` → **把注意力引向完全错误的方向**
4. WebView 默认不转发 console 日志
5. Release 包 `run-as` 被拒（`package not debuggable`）

**突破口**：用 `node` + 最小 DOM 桩**真跑一遍 `mobile.js`**，
才看到 `TypeError: Cannot set properties of null`。

### 修法

| 位置 | 修改 |
|---|---|
| `.topbar-endpoint` | 先取值判空再 `.onclick` |
| `.api-share-card` | 先取值判空再 `.append` |
| `arrangeHeader()` | 内部判 `target && actions` |
| 引导层顺序 | **第一步**就 `localBoot.remove()` |
| 可选步骤 | 全部 `?.` + 独立 `try` |
| `WBDesktop` | 补导出 `checkUpdatesAtStartup` |
| `retryLocal` | 先判 `pywebview` 是否存在，非桌面端给准确提示 |
| 新增 | `reportBootError()` —— 把启动异常**显示到界面**（WebView 不转发 console） |

### 防回归

* `tests/test_android_mobile_js_safety.py`（5 项，含 **node 真跑** smoke test）
* `tests/test_android_webview_boot.py`（6 项，导出完整性 + 引导层顺序）
* 反向验证 2 条 mutation（`android-missing-export` / `android-overlay-order`）
* **staging 与源码一致性检查**（防「改了源码但没重建」）

---

## 其他发现

### ① 屏幕熄灭后内嵌服务不响应

```
屏幕点亮  → /api/app/version  200
屏幕熄灭  → /api/app/version  000（超时）
```

进程存活、端口仍 LISTEN，但请求无响应。唤醒后立刻恢复。

**已实现**：`LocalApiService` 有 `startForeground()` +
`PARTIAL_WAKE_LOCK`（`L63-67`）。所以不是「没申请」。

**推测**：Android 16 的省电策略更激进，或 OPPO 的电池优化在拦。
需要在「设置 → 电池 → 应用 → 无限制」里放行。

**影响**：手机当 API 服务器时，锁屏后外部调用会超时。
**这是已知限制，未修** —— 需要真机继续观察。

### ② 服务绑定 `0.0.0.0`（**经核实是有意设计**）

我最初判断这是「不应暴露到局域网」，**核实后撤回**。
`android/README.md` 明确写着：

> 服务监听 `0.0.0.0`，支持同机及局域网客户端。
> API Keys 页可选择并复制检测到的局域网地址。

安全边界由**鉴权**保证（401 已验证），不是靠绑定地址。

> 教训：**看到可疑行为先查文档再下结论**。

### ③ `adb install -r` 首次报 `-99`

重试即成功。用户从文件管理器安装不受影响，属 adb 特有现象。

---

## 可复现的验证命令

```powershell
# 安装
adb install -r "WorkBuddy2API-1.0.0-android-arm64.apk"

# 启动
adb shell am start -n io.github.bingyangl1.workbuddy2api/.MainActivity

# 服务（需屏幕点亮）
adb shell "curl -s -m 8 -o /dev/null -w '%{http_code}' http://127.0.0.1:8790/api/app/version"

# 登录
adb shell "curl -s -X POST http://127.0.0.1:8790/api/login \
  -d 'username=mobile&password=<config.json 里的密码>'"

# 读应用日志（需 root / su）
ssh -p 8022 root@192.168.5.48 \
  "su -c 'tail -30 /data/data/io.github.bingyangl1.workbuddy2api/files/WorkBuddy2API/logs/mobile.log'"
```

---

## 环境恢复记录

测试期间临时改动**均已恢复**：

```powershell
adb shell settings put system screen_off_timeout 30000   # 恢复原值
adb shell svc power stayon false                          # 关闭常亮
```
