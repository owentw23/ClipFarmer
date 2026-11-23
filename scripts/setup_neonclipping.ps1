# Reproducible setup for engines\neonclipping (.venv + Python 3.12 + CUDA Whisper runtime)
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$EngineDir = Join-Path $RepoRoot "engines\neonclipping"
$VenvDir = Join-Path $EngineDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPip = Join-Path $VenvDir "Scripts\pip.exe"

$Python312 = if ($env:PYTHON312_EXE) {
    $env:PYTHON312_EXE
} else {
    "C:\Users\wirya\AppData\Local\Programs\Python\Python312\python.exe"
}

if (-not (Test-Path $Python312)) {
    throw "Python 3.12 not found at '$Python312'. Set PYTHON312_EXE or install Python 3.12."
}

if (-not (Test-Path (Join-Path $EngineDir "app.py"))) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $EngineDir) | Out-Null
    Write-Host "[setup] Cloning iamajmeena/neonclipping into $EngineDir ..."
    git clone --depth 1 https://github.com/iamajmeena/neonclipping.git $EngineDir
    if ($LASTEXITCODE -ne 0) { throw "git clone failed with exit code $LASTEXITCODE" }
}

if (-not (Test-Path $VenvPython)) {
    Write-Host "[setup] Creating Python 3.12 venv at $VenvDir ..."
    & $Python312 -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed with exit code $LASTEXITCODE" }
}

Write-Host "[setup] Upgrading pip ..."
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed with exit code $LASTEXITCODE" }

Write-Host "[setup] Installing NeonClipping requirements + CUDA 12 cuBLAS/cuDNN ..."
& $VenvPip install -r (Join-Path $EngineDir "requirements.txt") nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
if ($LASTEXITCODE -ne 0) { throw "pip install failed with exit code $LASTEXITCODE" }

$DataDir = if ($env:NEONCLIPPING_DATA_DIR) {
    $env:NEONCLIPPING_DATA_DIR
} else {
    "C:\Users\wirya\Documents\Clips_Store\neon_tmp"
}
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

Write-Host "[setup] Ensuring static_ffmpeg binaries, CUDA device count, and streamlink import ..."
& $VenvPython -c "import static_ffmpeg, ctranslate2, streamlink, faster_whisper; static_ffmpeg.add_paths(); n = ctranslate2.get_cuda_device_count(); print('cuda_devices=', n); assert n > 0, 'No CUDA devices detected'"
if ($LASTEXITCODE -ne 0) { throw "Verification failed with exit code $LASTEXITCODE" }

Write-Host "[setup] NeonClipping runtime ready at $VenvDir"
