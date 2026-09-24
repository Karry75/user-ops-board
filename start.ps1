# user_ops_board 启动脚本（Windows PowerShell）
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$port = if ($env:BOARD_PORT) { $env:BOARD_PORT } else { "8095" }
$busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($busy) { Write-Host "端口 $port 已被占用，请设置环境变量 BOARD_PORT 指定其他端口。" -ForegroundColor Yellow; exit 1 }

Write-Host "启动 user_ops_board，端口 $port ..." -ForegroundColor Cyan
Start-Process -FilePath "python" -ArgumentList "app.py" -WorkingDirectory $root `
  -RedirectStandardOutput (Join-Path $logDir "server.log") `
  -RedirectStandardError (Join-Path $logDir "server.err.log") -WindowStyle Hidden
Start-Sleep -Seconds 4
try {
  $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/health" -UseBasicParsing -TimeoutSec 10
  Write-Host "服务已就绪：http://127.0.0.1:$port/" -ForegroundColor Green
  Write-Host $r.Content
} catch {
  Write-Host "启动失败，请查看 logs\server.err.log" -ForegroundColor Red
}
