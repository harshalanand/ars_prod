param(
    [int]$Port = 8080,
    [string]$AppModule = "main:app",
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$pythonExe = Join-Path $scriptDir "venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    Write-Host "venv python not found at $pythonExe" -ForegroundColor Red
    exit 1
}

$targets = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'uvicorn.*main:app' -or $_.CommandLine -match 'ars_prod' }

if ($targets) {
    Write-Host "Killing existing uvicorn / ars_prod python processes:" -ForegroundColor Yellow
    $targets | ForEach-Object {
        Write-Host ("  PID {0}  {1}" -f $_.ProcessId, $_.CommandLine)
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch { Write-Host "  (already gone)" }
    }
    Start-Sleep -Seconds 2
} else {
    Write-Host "No existing uvicorn processes found." -ForegroundColor Green
}

$stillOnPort = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($stillOnPort) {
    Write-Host "Port $Port still bound by PID(s): $($stillOnPort.OwningProcess -join ', '). Aborting." -ForegroundColor Red
    exit 1
}

if ($NoStart) {
    Write-Host "Cleanup done. -NoStart specified, not launching uvicorn." -ForegroundColor Cyan
    exit 0
}

Write-Host "Starting uvicorn on port $Port ..." -ForegroundColor Green
& $pythonExe -m uvicorn $AppModule --host 0.0.0.0 --port $Port
