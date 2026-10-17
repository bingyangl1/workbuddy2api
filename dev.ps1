<#
.SYNOPSIS
  workbuddy2api 本地开发启动（源码方式）。

.DESCRIPTION
  从源码把服务跑起来，用于日常开发与功能体验。前台运行、日志直接可见（Ctrl+C 停止）。
  对比：`start_daemon.bat` 是后台常驻 + 自动拉起（部署用），开发时不便看日志。

  脚本做四件事：
    1. 环境自检（Python / 依赖 / 前端产物 / 数据库连通性 / 端口占用）
    2. 注入开发用环境变量
    3. 提示可访问的入口
    4. 启动服务

.PARAMETER Live
  关闭上游只读护栏。默认开启（测试时绝不打上游写操作）。
  开启后「成长任务 / 猫猫旅行 / 测速 / 模型同步 / 测试台直调」会被拦为 403。
  关了之后这些操作会**真实触达上游**，请勿高频连点（有被限流/封禁风险）。

.PARAMETER Check
  只做环境自检，不启动服务。

.PARAMETER NextPanel
  让 / 和 /admin 也指向新面板（等价 ADMIN_DEFAULT_PANEL=next）。

.PARAMETER Docs
  打开 `/docs`（OpenAPI 接口文档）。**默认关闭** —— 与生产口径一致
  （`config.py` 里 `ADMIN_ENABLE_DOCS` 默认 0）。接口文档会暴露全部路由与
  请求结构，非测试环境不该默认开着。

.PARAMETER Tunnel
  数据库改走 SSH 隧道（127.0.0.1:13306）而不是默认的局域网直连。
  什么时候用：不在同一网段（出差 / 公司 VPN 隔离 / 只允许 SSH 出网）。

.EXAMPLE
  .\dev.ps1                 # 启动（带上游只读护栏；/docs 关闭；数据库直连）
  .\dev.ps1 -Check          # 只自检
  .\dev.ps1 -Docs           # 启动并打开接口文档
  .\dev.ps1 -Live           # 启动并允许真实触达上游
  .\dev.ps1 -NextPanel      # 启动且默认进新面板
  .\dev.ps1 -NextPanel -Tunnel   # 不在同一网段时，改走 SSH 隧道

.NOTES
  如果提示「无法加载文件，因为在此系统上禁止运行脚本」，用：
    powershell -ExecutionPolicy Bypass -File .\dev.ps1
  或者直接看下面注释里的手动命令（不用脚本也能起）。
#>
[CmdletBinding()]
param(
    [switch]$Live,
    [switch]$Check,
    [switch]$NextPanel,
    [switch]$Docs,
    [switch]$Tunnel
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# ──────────────────────────────────────────────────────────────────────────
# 开发环境配置（按需修改；也可以改用项目根的 .env 文件，两者等价）
# ──────────────────────────────────────────────────────────────────────────
$DevConfig = [ordered]@{
    # 数据库：dev 库在局域网机器 192.168.5.47 上，**直连 3306**。
    #
    # 为什么不再走 SSH 隧道（127.0.0.1:13306）：
    #
    # 那里跑的是 Docker 里的 MySQL，docker-proxy 默认把 3306 映射到
    # **所有网卡**（实测 `LISTEN 0 4096 0.0.0.0:3306`），所以局域网内
    # 直接能连。实测对比：
    #
    #     直连 192.168.5.47:3306   →   31 ms
    #     隧道 127.0.0.1:13306     →  278 ms（多绕一跳 SSH，慢 8 倍）
    #
    # 原注释「本机没有 MySQL，需要 SSH 隧道」是建脚本时的**假设，没验证过**，
    # 一直被当事实沿用。直连少一个失败点，也快得多。
    #
    # 如果你在**不同网段**（出差 / 公司 VPN 隔离），加 `-Tunnel` 改走隧道。
    ADMIN_DATABASE_URL   = 'mysql+pymysql://root:wb2api_dev_root@192.168.5.47:3306/workbuddy_admin'
    ADMIN_USERNAME       = 'admin'
    ADMIN_PASSWORD       = 'Wb2!LocalTest8790'
    ADMIN_JWT_SECRET     = 'wb2api_local_jwt_secret_dev_only'
    ADMIN_PORT           = '8790'
    # 新面板托管（需要先构建 web/out）
    ADMIN_PANEL_STATIC   = '1'
    ADMIN_PANEL_PATH     = '/panel'
    # 开发便利
    # 注意：`/docs`（OpenAPI 文档）**默认关闭**，与生产口径一致
    # （config.py: ENABLE_DOCS = os.getenv("ADMIN_ENABLE_DOCS","0") == "1"）。
    # 要看接口文档时显式加 `-Docs` 参数，不要在默认路径上打开它。
    ADMIN_ENABLE_DOCS    = '0'
    ADMIN_DEV_TOOLS      = '1'
    ADMIN_BACKUP_DIR     = (Join-Path $PSScriptRoot '_bkptest')
    # 安全护栏：默认开启（测试时绝不打上游写操作）
    ADMIN_UPSTREAM_READONLY = '1'
}
if ($NextPanel) { $DevConfig['ADMIN_DEFAULT_PANEL'] = 'next' }
if ($Live)      { $DevConfig['ADMIN_UPSTREAM_READONLY'] = '0' }
# `/docs` 必须**显式**打开（用户要求：非测试环境默认关闭）
if ($Docs)      { $DevConfig['ADMIN_ENABLE_DOCS'] = '1' }
# -Tunnel：改走 SSH 隧道（默认直连局域网 3306）
#
# 什么时候需要：不在同一网段时（出差 / 公司 VPN 隔离 / 只允许 SSH 出网）。
if ($Tunnel) {
    $DevConfig['ADMIN_DATABASE_URL'] =
        'mysql+pymysql://root:wb2api_dev_root@127.0.0.1:13306/workbuddy_admin'
}

$port = $DevConfig['ADMIN_PORT']

function Write-Head($text) {
    Write-Host ''
    Write-Host ('=' * 62) -ForegroundColor DarkGray
    Write-Host " $text" -ForegroundColor Cyan
    Write-Host ('=' * 62) -ForegroundColor DarkGray
}
function Write-Ok($text)   { Write-Host "  [OK] $text" -ForegroundColor Green }
function Write-Warn2($text){ Write-Host "  [!]  $text" -ForegroundColor Yellow }
function Write-Bad($text)  { Write-Host "  [X]  $text" -ForegroundColor Red }

# ──────────────────────────────────────────────────────────────────────────
# 自检
# ──────────────────────────────────────────────────────────────────────────
$fatal = $false

Write-Head '1/5  Python 与依赖'
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Bad '找不到 python，请确认已加入 PATH'
    $fatal = $true
} else {
    $ver = (& python --version 2>&1) -join ' '
    Write-Ok $ver
    & python -c 'import fastapi, uvicorn, sqlalchemy, pymysql, httpx, jwt' 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Bad '关键依赖缺失，请先执行： python -m pip install -r requirements.txt'
        $fatal = $true
    } else {
        Write-Ok '关键依赖齐全'
    }
}

Write-Head '2/5  前端产物（web/out）'
$outIndex = Join-Path $PSScriptRoot 'web\out\index.html'
if (Test-Path $outIndex) {
    Write-Ok 'web\out 已存在'
    # 检查新面板页面是否齐全 —— 踩过的坑：只删 .next 不删 out，增量导出会漏掉新页面
    if (Test-Path (Join-Path $PSScriptRoot 'web\out\settings\layout\index.html')) {
        Write-Ok '面板页面完整（含 /settings/layout）'
    } else {
        Write-Warn2 'web\out 缺少部分页面（可能是增量导出漏页）。若刚加过新页面，请重建：'
        Write-Host '        cd web; Remove-Item .next,out -Recurse -Force' -ForegroundColor DarkGray
        Write-Host '        $env:NEXT_OUTPUT_EXPORT=1; $env:NEXT_PUBLIC_BASE_PATH=''/panel''; npm run build' -ForegroundColor DarkGray
        Write-Host '        注意：**.next 和 out 都要删**（只删 .next 会漏掉新目录）' -ForegroundColor DarkGray
    }
} else {
    Write-Warn2 'web\out 不存在 —— /panel 会 404。构建方法：'
    Write-Host '        cd web' -ForegroundColor DarkGray
    Write-Host '        $env:NEXT_OUTPUT_EXPORT=1; $env:NEXT_PUBLIC_BASE_PATH=''/panel''' -ForegroundColor DarkGray
    Write-Host '        npm run build' -ForegroundColor DarkGray
}

# ── 3/5 数据库连通性 ──────────────────────────────────────────────
#
# 从 ADMIN_DATABASE_URL 里解析出实际要连的 host:port 再测 ——
# 原来是硬编码测 127.0.0.1:13306，改成直连后那条检查就永远失败（自检骗人）。
$dbUrl = [string]$DevConfig['ADMIN_DATABASE_URL']
$dbHost = ''
$dbPort = 3306
if ($dbUrl -match '@([^/:]+):(\d+)/') {
    $dbHost = $Matches[1]
    $dbPort = [int]$Matches[2]
}
Write-Head "3/5  数据库（${dbHost}:${dbPort}）"
$dbOk = $false
if ($dbHost) {
    $dbOk = (Test-NetConnection -ComputerName $dbHost -Port $dbPort -WarningAction SilentlyContinue).TcpTestSucceeded
}
if (-not $dbOk) {
    Write-Bad "数据库连不上（${dbHost}:${dbPort}）！服务能启动，但**登录会返回 500**（不是密码错）。"
    if ($Tunnel) {
        Write-Host '        你用了 -Tunnel，需要先另开窗口建隧道并保持开着：' -ForegroundColor DarkGray
        Write-Host '          ssh -N -L 13306:127.0.0.1:3306 root@192.168.5.47' -ForegroundColor White
    } else {
        Write-Host '        默认走局域网直连。排查方向：' -ForegroundColor DarkGray
        Write-Host '          1) 是否和 192.168.5.47 在同一网段（ping / ipconfig）' -ForegroundColor DarkGray
        Write-Host '          2) 对方 MySQL 是否在跑（docker ps | grep mysql）' -ForegroundColor DarkGray
        Write-Host '          3) 不在同一网段时改用隧道：.\dev.ps1 -Tunnel' -ForegroundColor White
    }
    $fatal = $true
} else {
    Write-Ok "数据库已通（${dbHost}:${dbPort}）"
}

Write-Head "4/5  端口 $port"
$used = (Test-NetConnection -ComputerName 127.0.0.1 -Port ([int]$port) -WarningAction SilentlyContinue).TcpTestSucceeded
if ($used) {
    Write-Warn2 "端口 $port 已被占用（可能已有实例在跑）"
    Write-Host '        要重启先清掉旧进程（**两个都要杀**）：' -ForegroundColor DarkGray
    Write-Host '          Get-Process python | Stop-Process -Force' -ForegroundColor White
    Write-Host '        main.py 会 fork 一个 uvicorn 子进程，只杀父进程的话子进程' -ForegroundColor DarkGray
    Write-Host '        会继续用旧代码响应 HTTP —— 这是「改了代码不生效」的经典原因。' -ForegroundColor DarkGray
} else {
    Write-Ok "端口 $port 空闲"
}

Write-Head '5/5  配置摘要'
foreach ($k in $DevConfig.Keys) {
    $v = $DevConfig[$k]
    # 密码类不明文打印
    if ($k -match 'PASSWORD|SECRET') { $v = ($v -replace '^(.{3}).*$', '$1***') }
    Write-Host ("  {0,-24} {1}" -f $k, $v) -ForegroundColor DarkGray
}

if ($Check) {
    Write-Head '自检完成（-Check 模式，未启动服务）'
    if ($fatal) { exit 1 } else { exit 0 }
}
if ($fatal) {
    Write-Head '自检未通过，已中止'
    exit 1
}

# ──────────────────────────────────────────────────────────────────────────
# 启动
# ──────────────────────────────────────────────────────────────────────────
foreach ($k in $DevConfig.Keys) {
    Set-Item -Path "Env:$k" -Value $DevConfig[$k]
}

if ($Live) {
    Write-Host ''
    Write-Host '  !!' -ForegroundColor Red -NoNewline
    Write-Host ' 已关闭上游只读护栏（-Live）' -ForegroundColor Red
    Write-Host '  !! 成长任务 / 猫猫旅行 / 测速 / 模型同步 / 测试台会**真实触达上游**' -ForegroundColor Yellow
    Write-Host '  !! 不要高频连点 —— 这是账号被限流/封禁的直接原因' -ForegroundColor Yellow
    Start-Sleep -Seconds 3
}

Write-Head '启动服务（前台运行，Ctrl+C 停止）'
Write-Host "  新面板    http://127.0.0.1:$port/panel/" -ForegroundColor White
Write-Host "  旧面板    http://127.0.0.1:$port/admin-legacy/" -ForegroundColor White
Write-Host "  注册页    http://127.0.0.1:$port/join" -ForegroundColor White
if ($DevConfig['ADMIN_ENABLE_DOCS'] -eq '1') {
    Write-Host "  接口文档  http://127.0.0.1:$port/docs" -ForegroundColor White
} else {
    Write-Host "  接口文档  已关闭（要用加 -Docs 参数）" -ForegroundColor DarkGray
}
Write-Host "  登录      $($DevConfig['ADMIN_USERNAME']) / $($DevConfig['ADMIN_PASSWORD'])" -ForegroundColor White
$ro = $DevConfig['ADMIN_UPSTREAM_READONLY']
$roText = if ($ro -eq '1') { '1（只读：所有上游写操作返回 403）' } else { '0（开放：会真实触达上游）' }
Write-Host "  上游护栏  $roText" -ForegroundColor White
Write-Host ''

& python main.py
