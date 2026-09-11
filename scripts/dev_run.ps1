# ============================================================
# 本地开发启动（Windows PowerShell）
#
# 用途：在你自己的电脑上直接跑工作台，先在本地把业务逻辑调通，
#       再去走"构建 → 传输 → 导入极空间"那条慢循环。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\dev_run.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\dev_run.ps1 -Port 8080
#
# 注意：
#   - 本地直跑（不用容器）时，127.0.0.1 就是这台电脑本身，
#     所以如果 OpenClaw 也跑在这台电脑上，OPENCLAW_BASE_URL 可以用回环地址；
#     如果 OpenClaw 在 NAS 上，必须改成 NAS 的局域网 IP。
#   - 开发模式下用明文口令（WORKBENCH_PASSWORD），省去反复生成哈希。
#     **绝不能把 WORKBENCH_DEV=1 带到 NAS 上。**
# ============================================================
param(
    [int]$Port = 8080,
    [string]$OpenClawBaseUrl = "http://127.0.0.1:18789/v1",
    [string]$OpenClawToken = "",
    [string]$Model = "openclaw/default"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

# 本地数据目录（不会碰 NAS 上的任何数据）
$dataRoot = Join-Path $Root "var\dev-data"
New-Item -ItemType Directory -Force -Path (Join-Path $dataRoot "literature") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $dataRoot "life_notes") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Root "var\jobs") | Out-Null

$env:WORKBENCH_APP_ROOT      = $Root
$env:WORKBENCH_DEV           = "1"

# 开发口令：每次启动随机生成（除非你用 WORKBENCH_PASSWORD 显式指定）。
# 刻意【不】留固定默认值——固定口令一旦随代码进了仓库，
# 就等于把一串"可以直接拿来用的弱口令"一起发布了。
# uvicorn 的 --reload 只重启子进程，不会重跑本脚本，所以调试期间口令是稳定的。
if (-not $env:WORKBENCH_PASSWORD) {
    $alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    $env:WORKBENCH_PASSWORD = -join ($bytes | ForEach-Object { $alphabet[$_ % $alphabet.Length] })
}
$devPassword = $env:WORKBENCH_PASSWORD
$env:LITERATURE_DIR          = (Join-Path $dataRoot "literature")
$env:LIFE_NOTES_DIR          = (Join-Path $dataRoot "life_notes")
$env:WORKBENCH_WORK_DIR      = (Join-Path $Root "var\jobs")
$env:OPENCLAW_BASE_URL       = $OpenClawBaseUrl
$env:OPENCLAW_MODEL          = $Model
$env:PYTHONPATH              = $Root
$env:PYTHONUTF8              = "1"
$env:TZ                      = "Asia/Shanghai"
$env:WORKBENCH_LOG_LEVEL     = "INFO"
if ($OpenClawToken -ne "") { $env:OPENCLAW_TOKEN = $OpenClawToken }

Write-Host ""
Write-Host "本地开发模式" -ForegroundColor Cyan
Write-Host "  访问地址     : http://127.0.0.1:$Port"
Write-Host "  登录口令     : $devPassword"
Write-Host "  文献目录     : $($env:LITERATURE_DIR)"
Write-Host "  记录目录     : $($env:LIFE_NOTES_DIR)"
Write-Host "  AI 服务地址  : $($env:OPENCLAW_BASE_URL)"
if ($OpenClawToken -eq "") {
    Write-Host "  AI 令牌      : 未设置 —— 文献分析会报「AI 服务未配置」，生活记录的分类兜底仍可用" -ForegroundColor Yellow
}
Write-Host "  接口文档     : http://127.0.0.1:$Port/docs （仅开发模式可用）"
Write-Host ""

& python -m uvicorn app.main:app --host 127.0.0.1 --port $Port --reload --no-server-header
