$ErrorActionPreference = "SilentlyContinue"

$LogDir = "C:\Users\wirya\Documents\Clips_Store\logs"
$PidFile = Join-Path $LogDir "pids.json"
$killed = New-Object System.Collections.Generic.HashSet[int]

function Stop-Tree([int]$ProcId, [string]$Label) {
    if ($ProcId -le 0 -or $killed.Contains($ProcId)) { return }
    $proc = Get-Process -Id $ProcId -ErrorAction SilentlyContinue
    if ($proc) {
        & taskkill /PID $ProcId /T /F | Out-Null
        [void]$killed.Add($ProcId)
        Write-Host "[stop] $Label (PID $ProcId)"
    }
}

if (Test-Path $PidFile) {
    try {
        $saved = Get-Content $PidFile -Raw | ConvertFrom-Json
        foreach ($prop in $saved.PSObject.Properties) {
            if ($prop.Value) {
                Stop-Tree ([int]$prop.Value) $prop.Name
            }
        }
    } catch {}
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

foreach ($entry in @(
    @{ Port = 3000; Name = "frontend :3000" },
    @{ Port = 8000; Name = "backend :8000" },
    @{ Port = 8770; Name = "neonclipping :8770" }
)) {
    $conns = Get-NetTCPConnection -LocalPort $entry.Port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        Stop-Tree ([int]$c.OwningProcess) $entry.Name
    }
}

if ($killed.Count -eq 0) {
    Write-Host "No pipeline processes were running."
} else {
    Write-Host "Pipeline stopped cleanly."
}

