# 本地启动与全流程体验指南（DEV）

面向「想在本机把整个服务跑起来、亲手点一遍所有功能」的场景。
生产部署（systemd / 宝塔 / nginx）见 README 第四章；本文只讲**本机开发/体验**。

---

## 0. 太长不看（TL;DR）

```powershell
# 1) 一条命令起服务（数据库默认**局域网直连**）
cd F:\wb2api\workbuddy2api
.\dev.ps1 -NextPanel        # 新面板；不加 -NextPanel 则打开旧面板

# 2) 浏览器打开
#    新面板  http://127.0.0.1:8790/panel/      账号 admin / 密码 Wb2!LocalTest8790
#    旧面板  http://127.0.0.1:8790/admin-legacy/
```

> **不需要先建 SSH 隧道**。dev 库跑在 `192.168.5.47` 的 Docker 里，
> `docker-proxy` 默认把 3306 映射到所有网卡，同一网段直接能连。
> 实测直连 31ms、走隧道 278ms（多绕一跳 SSH，慢 8 倍）。
>
> 只有**不在同一网段**时（出差 / 公司 VPN 隔离）才需要 `-Tunnel`。

---

## 1. 前置：数据库

dev 库在 `192.168.5.47`（almalinux）的 **Docker** 里。默认**直连**：

```powershell
# 验证直连（这是默认配置）
Test-NetConnection -ComputerName 192.168.5.47 -Port 3306 -InformationLevel Quiet
# True 表示通
```

### 什么时候需要隧道

只有不在同一网段时（出差 / 公司 VPN 隔离 / 只允许 SSH 出网）：

```powershell
# 另开一个窗口，建立隧道并保持开着（-N 表示只做转发不执行命令）
ssh -N -L 13306:127.0.0.1:3306 root@192.168.5.47

# 启动时加 -Tunnel
.\dev.ps1 -NextPanel -Tunnel
```

> **数据库不通会怎样**：服务能启动，但**登录会返回 500**（不是密码错）。
> 症状辨识：`/api/login` 500 且日志里有 `Can't connect to MySQL server ...`。
> 直连不通先确认网段；隧道模式则重跑上面那条 ssh 命令。

---

## 2. 起服务

### 2.1 推荐：写 `.env` 文件（一次配置，长期有效）

```powershell
cd F:\wb2api\workbuddy2api
Copy-Item .env.example .env
```

然后编辑 `.env`，至少改这几项：

```ini
ADMIN_DATABASE_URL=mysql+pymysql://root:wb2api_dev_root@192.168.5.47:3306/workbuddy_admin
ADMIN_USERNAME=admin
ADMIN_PASSWORD=Wb2!LocalTest8790
ADMIN_JWT_SECRET=wb2api_local_jwt_secret_dev_only

# 新面板开关（P3）
ADMIN_PANEL_STATIC=1
ADMIN_PANEL_PATH=/panel
# 想让 / 和 /admin 直接进新面板（否则进旧面板）
# ADMIN_DEFAULT_PANEL=next

# 安全：本机体验可开，能省去大量提权/网络问题
ADMIN_ENABLE_DOCS=1          # 想看 /docs 时开（默认关）
# ADMIN_DEV_TOOLS=1          # 想玩「逆向产物」（拆 app.asar）时开
```

然后直接跑：

```powershell
python main.py
```

### 2.2 或者：临时环境变量（不想建文件）

```powershell
cd F:\wb2api\workbuddy2api
$env:ADMIN_DATABASE_URL='mysql+pymysql://root:wb2api_dev_root@192.168.5.47:3306/workbuddy_admin'
$env:ADMIN_USERNAME='admin'
$env:ADMIN_PASSWORD='Wb2!LocalTest8790'
$env:ADMIN_JWT_SECRET='wb2api_local_jwt_secret_dev_only'
$env:ADMIN_PANEL_STATIC='1'
$env:ADMIN_PANEL_PATH='/panel'
python main.py
```

启动成功会打印：

```
[main] 启动 admin (http://0.0.0.0:8790) : ...
[main] 单端口服务已启动：
       管理后台   : http://0.0.0.0:8790/admin
       托管网关   : http://0.0.0.0:8790/v1/chat/completions  (带 Key 配额)
       内嵌网关   : http://0.0.0.0:8790/gw/v1/...            (桌面登录态 / responses / messages)
```

> `main.py` 会 fork 一个 uvicorn 子进程。**改完代码要重启**：杀掉所有 python
> （`Get-Process python | Stop-Process -Force`）再重跑，否则旧子进程会继续用旧代码
> 响应 HTTP（一个很容易踩的坑：直接调函数是新行为、走 HTTP 却是旧行为）。

### 2.3 后台常驻（关掉终端也继续跑）

```powershell
.\start_daemon.bat          # 启动（脱离会话 + 守护循环，挂了自动拉起）
.\start_daemon.bat stop     # 停止
```

### 2.4 改前端后要重新构建（重要）

`/panel` 服务的是**静态导出产物** `web/out/`。改了 `web/` 下任何代码都要重建：

```powershell
cd F:\wb2api\workbuddy2api\web
$env:NEXT_OUTPUT_EXPORT='1'; $env:NEXT_PUBLIC_BASE_PATH='/panel'
npm run build            # 产物在 web/out/
```

**踩坑提醒**：如果改完代码构建后行为没变，先删缓存重建：

```powershell
Remove-Item .next -Recurse -Force
npm run build
```

（Next 15 的 `.next/cache` 会复用陈旧编译输出，实测会导致「源码已改、产物没变」。）

想边改边看（热重载）也可以走 dev 模式：

```powershell
cd web; npm run dev      # http://localhost:3000（需另配 API 代理）
```

---

## 3. 打开面板

| 地址 | 说明 |
|---|---|
| http://127.0.0.1:8790/panel/ | **新面板**（Next.js，14+4 页） |
| http://127.0.0.1:8790/panel/dashboard/ | 仪表盘 |
| http://127.0.0.1:8790/admin-legacy/ | **旧面板**（125 路由原始实现，功能最全） |
| http://127.0.0.1:8790/join | 公开手机号注册页（可直接发给别人） |
| http://127.0.0.1:8790/docs | OpenAPI 文档（需 `ADMIN_ENABLE_DOCS=1`） |

登录：`admin` / `Wb2!LocalTest8790`（JWT 存在 sessionStorage，键名 `wb-admin-token`）。

**新旧面板切换**（`.env` 改完重启）：

```ini
ADMIN_DEFAULT_PANEL=legacy   # / 和 /admin → 旧面板（默认）
ADMIN_DEFAULT_PANEL=next     # / 和 /admin → 新面板（需 ADMIN_PANEL_STATIC=1）
```

---

## 4. 全流程体验路线（建议按顺序，20 分钟）

### 4.1 仪表盘 — 看总体
`/panel/dashboard/`：账号总数 / 健康账号 / 今日请求 / 今日积分 + 模型健康表
（模型 / 请求数 / 成功率 / 平均时延）。

### 4.2 账号池 — 核心
`/panel/accounts/`：
- 看 10 个账号的状态徽章（异常状态会显著标色：冷却 / 熔断 / 降权 / 模型限流，悬停看真实原因）
- **刷新余额**（单账号 ⭮ 图标，会真实打上游）
- **模型限流台账**（工具菜单 Wrench）
- **导出 / 本机扫描导入**（工具菜单）
- **猫猫旅行**（Cat 图标 / 顶部批量按钮）：同意协议 → 领养 +300 → 派出 → 领奖
  ⚠️ 会**真实触达上游**并消耗账号行为额度

### 4.3 模型 — 管理 + 目录 + 测试台
- `/panel/models/`：白名单启停、积分倍率、模型分组（绑定密钥可限制可用模型）、测速
- `/panel/models/catalog/`：**模型中心** —— 系列分组、搜索、能力筛选、排序；
  显示名 / 描述 / 上下文 / 最大输出 / **推理档位** / 倍率 / 能力标记
- `/panel/playground/`：**聊天测试台** —— 免密钥直调真实模型，选模型 + 思考强度，
  流式渲染 + 可中断 + 右下角实时积分
  ⚠️ 会**真实消耗积分**（走同一号池状态机，记账口径与 /v1 一致）

### 4.4 密钥 — 对外分发
`/panel/keys/`：创建 API Key（可设积分上限 / 有效期 / IP 白名单 / Token 配额）；
明文只显示一次；用量进度条；**访问令牌**（`/panel/tokens/`，只读 wbt_ 令牌给监控用）。

### 4.5 日志 / 统计 — 观察流量
- `/panel/logs/`：keyset 深翻页 + 过滤 + **CSV 导出**
- `/panel/stats/`：趋势折线 + 模型/端点/密钥分布，筛选联动

### 4.6 安全 / 审计 / 分组
- `/panel/security/`：IP 白/黑名单 + 访问审计 + 模型别名映射
- `/panel/audit/`：操作审计（谁在什么时候删了账号/看了密钥）
- `/panel/account-groups/`：账号池分组（绑定密钥 → 密钥只能用某组账号）

### 4.7 定时 / 成长 — 自动化
- `/panel/schedules/`：7 个内置任务，可增删改查 + **立即运行**（结果是**人话摘要**）
- `/panel/growth/`：15 个成长任务，单账号弹窗 / 批量异步执行 + 领奖
  ⚠️ 会真实触达上游

### 4.8 设置（4 个子页）
- `/panel/settings/`：客户端参数档案（出站伪装 UA/版本号/风控头）—— 探测 / 保存 / 重置
- `/panel/settings/sync/`：**线上同步**（把本地账号+模型+客户端参数推到线上，HMAC 签名）
- `/panel/settings/password/`：修改登录密码
- `/panel/settings/app-source/`：**逆向产物**（拆 app.asar，需 `ADMIN_DEV_TOOLS=1`）

### 4.9 备份 / 更新日志
- `/panel/backup/`：本地快照导出（含密/脱敏）→ 列表 → 预览 → 下载 → **覆盖式恢复**
  （恢复前自动留底 `pre-restore-*.json`）
- `/panel/changelog/`：离线更新日志查看器

### 4.10 试一下网关（真正的 API）
用刚创建的 API Key 调一次：

```powershell
$key = "wb-你的密钥"
curl.exe -s http://127.0.0.1:8790/v1/chat/completions `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer $key" `
  -d '{"model":"auto","messages":[{"role":"user","content":"说一句话"}],"stream":false}'
```

其他协议端点：`/v1/messages`（Anthropic/Claude Code）、`/v1/responses`、`/v1/models`、`/v1/usage`。

---

## 5. 体验时的安全护栏（重要）

本机 dev 库里的账号是**真实账号**，误操作会打限流甚至封号。体验「会触达上游」的功能前，
建议先开**上游只读护栏**：

```powershell
$env:ADMIN_UPSTREAM_READONLY='1'
python main.py
```

开启后，所有**会真实触达上游的写操作**统一返回 403：

- 成长任务（做任务 / 领奖）、猫猫旅行、批量执行
- 强制刷新余额、测速、同步模型列表、token 注入、定时任务「立即运行」
- 聊天测试台直调

**只读操作与本地 CRUD 全部放行**：账号列表、日志、统计、模型白名单、密钥管理、
备份导出、模型中心浏览……所以「不碰账号」的体验完全不受影响。

想体验真实调用的功能（测试台 / 猫猫旅行 / 成长任务）时，再关掉护栏（`=0`）并**少量试**：
不要连点几十次，尤其是聊天测试台（高频调用是封号的直接原因）。

---

## 6. 跑测试（验证代码没坏）

```powershell
cd F:\wb2api\workbuddy2api
python -m pytest tests/ -q --no-header      # 引擎测试（当前 442 个）
```

面板端到端冒烟（需要引擎在跑 + CDP 浏览器）：

```powershell
# 1) 起一个带调试端口的浏览器（禁用代理，应用自己的网络环境）
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
  --remote-debugging-port=9223 --user-data-dir="$env:TEMP\wb-cdp" `
  --no-first-run --no-proxy-server --headless=new about:blank
# 2) 跑冒烟（21 页遍历 + 交互 + 移动端溢出）
python scripts/panel_smoke_test.py
```

---

## 7. 常见问题

| 症状 | 原因 / 解决 |
|---|---|
| 登录返回 **500**，日志 `Can't connect to MySQL` | 数据库不通。**默认直连**：确认同一网段、对方 MySQL 在跑；隧道模式：`ssh -N -L 13306:...` |
| 端口 8790 被占 / 服务起不来 | `Get-Process python \| Stop-Process -Force` 后重试（旧进程没死透） |
| 改了代码但 HTTP 行为没变 | 旧 uvicorn 子进程还在 → 杀干净所有 python 再起 |
| 改了前端但页面没变 | 没重建 `web/out`，或 `.next` 缓存陈旧 → 删 `.next` 重建 |
| `/panel/*` 404 | 没设 `ADMIN_PANEL_STATIC=1`，或没构建 `web/out` |
| 页面打开是**旧面板** | `ADMIN_DEFAULT_PANEL=legacy`（默认）；要新面板就设 `next` |
| 某些按钮点了一直转 / 403 | `ADMIN_UPSTREAM_READONLY=1` 开着（这是护栏，不是 bug） |
| 面板白屏 + 控制台 `ChunkLoadError` | 构建产物变了但浏览器还拿旧 HTML（缓存）→ 强刷 Ctrl+Shift+R |
| 想看接口文档 | `ADMIN_ENABLE_DOCS=1` 后访问 `/docs` |

---

## 8. 无账号环境体验（不想连真库）

想用假数据把面板每页都点一遍、完全不碰真账号：

```powershell
# 1) 灌一份确定性假数据到本地 SQLite（10 账号 / 30 模型 / 3 密钥 / 225 条用量）
$env:ADMIN_DATABASE_URL='sqlite:///./dev_fixtures.db'
python dev/seed_fixtures.py

# 2) 用同一个库起服务
$env:ADMIN_USERNAME='admin'; $env:ADMIN_PASSWORD='test1234'
$env:ADMIN_JWT_SECRET='dev_only_secret_at_least_32_bytes_long'
$env:ADMIN_PANEL_STATIC='1'; $env:ADMIN_PANEL_PATH='/panel'
$env:ADMIN_UPSTREAM_READONLY='1'   # 假账号当然打不了上游，开着更安全
python main.py
```

好处：**零上游触达**、随便点、数据固定（截图/回归可复现）。
`seed_fixtures.py` 强制要求 `sqlite://` 前缀，防止误灌真库。
