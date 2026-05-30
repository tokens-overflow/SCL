# ============================================================
#  Maps Deep Research Agent — 一键启动脚本
#
#  动作：
#    1. 关掉占用 8000 / 5173 的旧进程（如有）
#    2. 在新窗口里启动 backend（python -m src.main）
#    3. 等 /healthz 返回 200
#    4. 在新窗口里启动 frontend（npm run dev）
#    5. 等 http://localhost:5173 可达
#    6. 用默认浏览器打开 http://localhost:5173
#
#  用法：  PowerShell 里 cd 到本目录，运行 ./start.ps1
#         或右键 "Run with PowerShell"
# ============================================================

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$backend  = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"

function Stop-Port {
    param([int]$Port)
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) { return }
    $pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($targetPid in $pids) {
        $p = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
        if ($p) {
            Write-Host "  ↳ stop PID $targetPid ($($p.ProcessName)) on :$Port" -ForegroundColor DarkYellow
            Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Milliseconds 500
}

function Wait-Url {
    param([string]$Url, [int]$TimeoutSec = 60, [string]$Label = "")
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
            if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) {
                Write-Host "  ↳ $Label OK ($Url)" -ForegroundColor Green
                return $true
            }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    Write-Host "  ↳ $Label TIMEOUT after ${TimeoutSec}s" -ForegroundColor Red
    return $false
}

# 选 PowerShell 可执行程序：优先 pwsh (7+)，回退到 powershell (5)
$pwshCmd = Get-Command pwsh -ErrorAction SilentlyContinue
if ($pwshCmd) {
    $ps = $pwshCmd.Source
} else {
    $ps = (Get-Command powershell -ErrorAction SilentlyContinue).Source
}

Write-Host ""
Write-Host "[1/5] 关掉旧实例 ..." -ForegroundColor Cyan
Stop-Port 8000
Stop-Port 5173

Write-Host ""
Write-Host "[2/5] 启动 backend (端口 8000) ..." -ForegroundColor Cyan
Start-Process -FilePath $ps `
    -ArgumentList "-NoExit","-Command","Set-Location '$backend'; python -m src.main" `
    -WindowStyle Normal | Out-Null

Write-Host ""
Write-Host "[3/5] 等 backend /healthz ..." -ForegroundColor Cyan
if (-not (Wait-Url -Url "http://localhost:8000/healthz" -TimeoutSec 60 -Label "backend")) {
    Write-Host "backend 没起来，看新弹出的窗口日志。" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "[4/5] 启动 frontend (端口 5173) ..." -ForegroundColor Cyan
Start-Process -FilePath $ps `
    -ArgumentList "-NoExit","-Command","Set-Location '$frontend'; npm run dev" `
    -WindowStyle Normal | Out-Null

Write-Host ""
Write-Host "[5/5] 等 frontend dev server ..." -ForegroundColor Cyan
if (-not (Wait-Url -Url "http://localhost:5173/" -TimeoutSec 60 -Label "frontend")) {
    Write-Host "frontend 没起来，看新弹出的窗口日志。" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "★ 双端就绪，打开浏览器..." -ForegroundColor Green
Start-Process "http://localhost:5173"
Write-Host ""
Write-Host "  backend:  http://localhost:8000   (新弹窗 A)"
Write-Host "  frontend: http://localhost:5173   (新弹窗 B)"
Write-Host ""
Write-Host "  停止：直接关掉那两个弹窗，或再跑一次 ./start.ps1（会先杀旧的）"
Write-Host ""
