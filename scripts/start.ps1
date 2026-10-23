$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '缺少 .venv。请按 README 安装依赖。' }
$config = Join-Path $root '.env'
if (-not (Test-Path -LiteralPath $config)) { throw '缺少 .env。请复制 .env.example 并填写配置。' }
$data = Join-Path $root 'data'
New-Item -ItemType Directory -Force -Path $data | Out-Null
$legacyPidFile = Join-Path $data 'server.pid'
if (Test-Path -LiteralPath $legacyPidFile) {
    $legacyPid = 0
    $legacyText = (Get-Content -LiteralPath $legacyPidFile -Raw).Trim()
    if (-not [int]::TryParse($legacyText, [ref]$legacyPid)) { throw '旧版 server.pid 无法读取，请检查 data/server.pid 后再启动。' }
    $legacyProcess = Get-Process -Id $legacyPid -ErrorAction SilentlyContinue
    if ($legacyProcess -and $legacyProcess.Path -eq $python) {
        throw "旧版 Web 服务可能仍在运行（PID $legacyPid）。请核对进程后执行 Stop-Process -Id $legacyPid，再启动终端版。"
    }
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
icacls $data /inheritance:r /grant:r ($identity + ':(OI)(CI)F') ($env:USERNAME + ':(OI)(CI)F') | Out-Null
$env:PYTHONPATH = Join-Path $root 'src'
Push-Location $root
try {
    & $python -m study_scout.main @args
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $exitCode
