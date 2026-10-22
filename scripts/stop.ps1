$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$data = Join-Path $root 'data'
$pidFile = Join-Path $data 'server.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Host '未发现运行中的 Study Scout。'; exit 0 }
$serverPid = [int](Get-Content -LiteralPath $pidFile -Raw)
$process = Get-Process -Id $serverPid -ErrorAction SilentlyContinue
$expectedPython = Join-Path $root '.venv\Scripts\python.exe'
if (-not $process -or $process.Path -ne $expectedPython) { throw 'PID 与本项目 Python 进程不匹配，已拒绝停止。' }
$token = Get-Content -LiteralPath (Join-Path $data 'control.token') -Raw
try { $null = Invoke-RestMethod 'http://127.0.0.1:8765/stop' -Method Post -Body @{csrf_token=$token.Trim()} -TimeoutSec 5 } catch { Write-Warning '无法通知 runner 停止，准备结束进程。' }
for ($i=0; $i -lt 26; $i++) {
    try {
        $status = Invoke-RestMethod 'http://127.0.0.1:8765/status' -TimeoutSec 2
        if (-not $status.running) { break }
    } catch { break }
    Start-Sleep -Seconds 1
}
try { $status = Invoke-RestMethod 'http://127.0.0.1:8765/status' -TimeoutSec 2; Write-Host "本轮状态：$($status.runs[0].status)" } catch { Write-Host '服务已停止。' }
Stop-Process -Id $serverPid -Force
Remove-Item -LiteralPath $pidFile
Write-Host 'Study Scout 已停止。'
