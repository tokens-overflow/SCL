# ============================================================
#  Maps Deep Research Agent — 初次安装脚本
#
#  动作：
#    1. 检查 Python / Node / npm 在场
#    2. 检查 backend/config.yaml 存在；frontend/.env.local 不存在则从 example 拷贝
#    3. pip install -e .  （默认 deps）
#    4. 解析 config.yaml 找 active provider type，必要时追加 [bedrock] / [anthropic] extra
#    5. npm install
#    6. 配置加载冒烟
#    7. 提示必填字段是否填了，告知下一步用 ./start.ps1
#
#  幂等：重复跑只会增量补装，不会破坏现有 venv / node_modules
#
#  用法：  .\setup.ps1
# ============================================================

$ErrorActionPreference = "Stop"
$root     = Split-Path -Parent $MyInvocation.MyCommand.Definition
$backend  = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$cfgPath  = Join-Path $backend "config.yaml"
$cfgEx    = Join-Path $backend "config.example.yaml"
$envLocal = Join-Path $frontend ".env.local"
$envEx    = Join-Path $frontend ".env.local.example"

function Need-Cmd {
    param([string]$Name, [string]$HintUrl)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $cmd) {
        Write-Host "  ✗ $Name 没装" -ForegroundColor Red
        if ($HintUrl) { Write-Host "    安装：$HintUrl" -ForegroundColor Yellow }
        return $false
    }
    $ver = (& $Name --version 2>&1 | Select-Object -First 1) -replace "`r", ""
    Write-Host "  ✓ $Name : $ver" -ForegroundColor Green
    return $true
}

# ------------------------------------------------------------
Write-Host ""
Write-Host "[1/6] 环境检查" -ForegroundColor Cyan
$ok = $true
$ok = (Need-Cmd "python" "https://www.python.org/downloads/  (需要 3.10+)") -and $ok
$ok = (Need-Cmd "node"   "https://nodejs.org/  (需要 18+)")                  -and $ok
$ok = (Need-Cmd "npm"    $null)                                              -and $ok
if (-not $ok) { Write-Host "请先装好上面缺的工具再跑本脚本。" -ForegroundColor Red; exit 1 }

# ------------------------------------------------------------
Write-Host ""
Write-Host "[2/6] 检查配置文件" -ForegroundColor Cyan
if (-not (Test-Path $cfgPath)) {
    if (Test-Path $cfgEx) {
        Copy-Item $cfgEx $cfgPath
        Write-Host "  ✓ 已从 config.example.yaml 创建 backend/config.yaml" -ForegroundColor Yellow
        Write-Host "    ★ 记得编辑里面的 secret 字段（api_key / aws_*），或设环境变量" -ForegroundColor Yellow
    } else {
        Write-Host "  ✗ backend/config.yaml 和 config.example.yaml 都不存在" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "  ✓ backend/config.yaml 存在"
}

if (-not (Test-Path $envLocal)) {
    if (Test-Path $envEx) {
        Copy-Item $envEx $envLocal
        Write-Host "  ✓ 已从 .env.local.example 创建 frontend/.env.local" -ForegroundColor Yellow
        Write-Host "    ★ 记得编辑里面的 VITE_GOOGLE_MAPS_JS_KEY" -ForegroundColor Yellow
    } else {
        Write-Host "  ✗ frontend/.env.local.example 也不存在，无法兜底" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "  ✓ frontend/.env.local 存在"
}

# ------------------------------------------------------------
Write-Host ""
Write-Host "[3/6] 安装 backend 默认依赖" -ForegroundColor Cyan
Push-Location $backend
try {
    python -m pip install -e . --quiet --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { throw "pip install -e . 失败" }
    Write-Host "  ✓ 默认 deps 装好"
} finally { Pop-Location }

# ------------------------------------------------------------
Write-Host ""
Write-Host "[4/6] 解析 active provider，按需追加 extra" -ForegroundColor Cyan
$probeCode = @"
import yaml, sys
c = yaml.safe_load(open(r'$cfgPath', encoding='utf-8'))
act = c['llm']['active']
prov = c['llm']['providers'].get(act, {})
print(f"{act}|{prov.get('type', act)}")
"@
$probe = python -c $probeCode 2>$null
if ($LASTEXITCODE -ne 0 -or -not $probe) {
    Write-Host "  ⚠ 解析 config.yaml 失败，跳过 extra 安装" -ForegroundColor Yellow
} else {
    $parts = $probe.Trim() -split '\|'
    $active = $parts[0]; $ptype = $parts[1]
    Write-Host "  active=$active type=$ptype"
    $extra = $null
    if ($ptype -eq "bedrock")   { $extra = "[bedrock]" }
    elseif ($ptype -eq "anthropic") { $extra = "[anthropic]" }
    if ($extra) {
        Push-Location $backend
        try {
            python -m pip install -e ".$extra" --quiet --disable-pip-version-check
            if ($LASTEXITCODE -ne 0) { throw "pip install -e .$extra 失败" }
            Write-Host "  ✓ extra $extra 装好"
        } finally { Pop-Location }
    } else {
        Write-Host "  ✓ provider 不需要 extra"
    }
}

# ------------------------------------------------------------
Write-Host ""
Write-Host "[5/6] 安装 frontend 依赖" -ForegroundColor Cyan
Push-Location $frontend
try {
    npm install --silent
    if ($LASTEXITCODE -ne 0) { throw "npm install 失败" }
    Write-Host "  ✓ frontend node_modules 装好"
} finally { Pop-Location }

# ------------------------------------------------------------
Write-Host ""
Write-Host "[6/6] 配置加载冒烟 + 必填字段检查" -ForegroundColor Cyan
Push-Location $backend
try {
    $smoke = python -c "from src.config import get_configuration; c = get_configuration(); print(c.llm.active + '|' + str(c.app.server.port) + '|' + c.app.maps.api_key)" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ✗ config 加载失败：" -ForegroundColor Red
        Write-Host $smoke
        exit 1
    }
    $sm = $smoke.Trim() -split '\|'
    Write-Host "  ✓ config 加载 OK：provider=$($sm[0]) port=$($sm[1])"

    if (-not $sm[2]) {
        Write-Host ""
        Write-Host "  ⚠ app.maps.api_key 为空。" -ForegroundColor Yellow
        Write-Host "    打开 backend/config.yaml，把 app.maps.api_key 填上你的 Server key" -ForegroundColor Yellow
    }
} finally { Pop-Location }

$envContent = Get-Content $envLocal -Raw
if ($envContent -match "your-browser-maps-key" -or $envContent -notmatch "VITE_GOOGLE_MAPS_JS_KEY=\s*AIza") {
    Write-Host "  ⚠ frontend/.env.local 里 VITE_GOOGLE_MAPS_JS_KEY 还是占位值。" -ForegroundColor Yellow
    Write-Host "    打开 frontend/.env.local，把 key 填上你的 Browser key" -ForegroundColor Yellow
}

# ------------------------------------------------------------
Write-Host ""
Write-Host "★ 安装完成。下一步：" -ForegroundColor Green
Write-Host "  cd `"$root`""
Write-Host "  .\start.ps1"
Write-Host ""
