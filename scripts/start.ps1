$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '缺少 .venv。请按 README 安装依赖。' }
$data = Join-Path $root 'data'
New-Item -ItemType Directory -Force -Path $data | Out-Null
$pidFile = Join-Path $data 'server.pid'
if (Test-Path -LiteralPath $pidFile) {
    $oldPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    $old = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
    if ($old -and $old.Path -eq $python) { throw "Study Scout 已在运行，PID $oldPid" }
    Remove-Item -LiteralPath $pidFile
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
icacls $data /inheritance:r /grant:r "${identity}:(OI)(CI)F" "${env:USERNAME}:(OI)(CI)F" | Out-Null
$env:PYTHONPATH = Join-Path $root 'src'
$process = Start-Process -FilePath $python -ArgumentList '-m','uvicorn','study_scout.main:app','--host','127.0.0.1','--port','8765' -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $data 'server.log') -RedirectStandardError (Join-Path $data 'server.err.log')
Set-Content -LiteralPath $pidFile -Value $process.Id
for ($i=0; $i -lt 30; $i++) {
    try { $null = Invoke-RestMethod 'http://127.0.0.1:8765/status' -TimeoutSec 2; break } catch { Start-Sleep -Milliseconds 500 }
}
if ($i -ge 30) { throw '服务启动失败，请检查 data/server.err.log' }
Start-Process 'http://127.0.0.1:8765' | Out-Null
Write-Host "Study Scout 已启动，PID $($process.Id)。浏览器扫码后自动运行一轮。"
