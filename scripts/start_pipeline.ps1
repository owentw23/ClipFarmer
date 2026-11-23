param(
    [switch]$DevFrontend
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = "C:\Users\wirya\Documents\Clips_Store\logs"
$PidFile = Join-Path $LogDir "pids.json"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Get-ListeningPid([int]$Port) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) { return [int]$conn.OwningProcess }
    return $null
}

function Wait-HttpOk([string]$Url, [int]$TimeoutSec = 25) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
            if ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400) {
                return $true
            }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    return $false
}

$pids = @{}

# 1. NeonClipping (port 8770)
$neonPid = Get-ListeningPid 8770
if ($neonPid) {
    Write-Host "[skip] NeonClipping already listening on :8770 (PID $neonPid)"
    $pids["neonclipping"] = $neonPid
} else {
    $neonDir = Join-Path $RepoRoot "engines\neonclipping"
    $neonPy = Join-Path $neonDir ".venv\Scripts\python.exe"
    $env:NEONCLIPPING_DATA_DIR = "C:\Users\wirya\Documents\Clips_Store\neon_tmp"
    $env:NEONCLIPPING_NO_AUTOUPDATE = "1"
    $p = Start-Process -FilePath $neonPy `
        -ArgumentList "app.py", "--host", "127.0.0.1", "--port", "8770" `
        -WorkingDirectory $neonDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir "neonclipping.out.log") `
        -RedirectStandardError (Join-Path $LogDir "neonclipping.err.log") `
        -PassThru
    $pids["neonclipping"] = $p.Id
    Write-Host "[start] NeonClipping :8770 (PID $($p.Id))"
}

# 2. FastAPI Backend (port 8000)
$backendPid = Get-ListeningPid 8000
if ($backendPid) {
    Write-Host "[skip] Backend already listening on :8000 (PID $backendPid)"
    $pids["backend"] = $backendPid
} else {
    $backendDir = Join-Path $RepoRoot "backend"
    $uvicornExe = Join-Path $backendDir ".venv\Scripts\uvicorn.exe"
    $p = Start-Process -FilePath $uvicornExe `
        -ArgumentList "app.main:app", "--host", "127.0.0.1", "--port", "8000" `
        -WorkingDirectory $backendDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir "backend.out.log") `
        -RedirectStandardError (Join-Path $LogDir "backend.err.log") `
        -PassThru
    $pids["backend"] = $p.Id
    Write-Host "[start] Backend :8000 (PID $($p.Id))"
}

# 3. Next.js Frontend (port 3000)
$frontendPid = Get-ListeningPid 3000
if ($frontendPid) {
    Write-Host "[skip] Frontend already listening on :3000 (PID $frontendPid)"
    $pids["frontend"] = $frontendPid
} else {
    $frontendDir = Join-Path $RepoRoot "frontend"
    $hasBuild = Test-Path (Join-Path $frontendDir ".next\BUILD_ID")
    $npmScript = if ($DevFrontend -or -not $hasBuild) { "dev" } else { "start" }
    $p = Start-Process -FilePath "npm.cmd" `
        -ArgumentList "run", $npmScript `
        -WorkingDirectory $frontendDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir "frontend.out.log") `
        -RedirectStandardError (Join-Path $LogDir "frontend.err.log") `
        -PassThru
    $pids["frontend"] = $p.Id
    Write-Host "[start] Frontend ($npmScript) :3000 (PID $($p.Id))"
}

$pids | ConvertTo-Json | Set-Content -Path $PidFile -Encoding UTF8

$okNeon = Wait-HttpOk "http://127.0.0.1:8770/healthz" 25
$okBack = Wait-HttpOk "http://127.0.0.1:8000/api/health" 25
$okFront = Wait-HttpOk "http://127.0.0.1:3000" 25

Write-Host ""
Write-Host "NeonClipping :8770 -> $(if ($okNeon) { 'READY' } else { 'FAILED (check neonclipping.err.log)' })"
Write-Host "Backend      :8000 -> $(if ($okBack) { 'READY' } else { 'FAILED (check backend.err.log)' })"
Write-Host "Frontend     :3000 -> $(if ($okFront) { 'READY' } else { 'FAILED (check frontend.err.log)' })"
Write-Host "Dashboard: http://127.0.0.1:3000"
Write-Host "Logs:      $LogDir"

