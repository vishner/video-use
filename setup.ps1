# video-use setup script for Windows (PowerShell 5+)
# Run from PowerShell:  .\setup.ps1
# (If blocked: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "[ok]  $msg"  -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "[warn] $msg" -ForegroundColor Yellow }
function Write-Err2($msg) { Write-Host "[err] $msg"  -ForegroundColor Red }

# ----------------------------------------------------------------- Python
Write-Step "Checking Python (>= 3.10)"
$pyCmd = $null
foreach ($c in @("py -3", "python", "python3")) {
    try {
        $v = & cmd /c "$c --version 2>&1"
        if ($LASTEXITCODE -eq 0) { $pyCmd = $c; Write-OK "$c -> $v"; break }
    } catch {}
}
if (-not $pyCmd) {
    Write-Err2 "Python not found on PATH. Install Python 3.10+ from https://python.org/ or via 'winget install Python.Python.3.12'."
    exit 1
}

# Determine actual interpreter for pip invocations
$pyExe = (& cmd /c "$pyCmd -c `"import sys;print(sys.executable)`"").Trim()
Write-OK "Using $pyExe"

# Verify >= 3.10
$verOut = & cmd /c "$pyCmd -c `"import sys;print(sys.version_info.major, sys.version_info.minor)`""
$parts = $verOut.Trim().Split(" ")
$major = [int]$parts[0]; $minor = [int]$parts[1]
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
    Write-Err2 "Python $major.$minor found, but >= 3.10 is required."
    exit 1
}
if ($major -eq 3 -and $minor -ge 14) {
    Write-Warn2 "Python $major.$minor detected. Some scientific wheels (e.g. librosa) may lag on very new Python releases. Recommended: 3.11 or 3.12."
}

# ----------------------------------------------------------------- pip install
Write-Step "Installing Python dependencies (this can take a few minutes)"
& $pyExe -m pip install --upgrade pip
& $pyExe -m pip install -e .
if ($LASTEXITCODE -ne 0) {
    Write-Warn2 "Editable install failed; trying plain dependency install."
    & $pyExe -m pip install requests librosa matplotlib pillow numpy
    if ($LASTEXITCODE -ne 0) { Write-Err2 "pip install failed."; exit 1 }
}
Write-OK "Python deps installed."

# Optional: opencv for detect_approach.py
Write-Step "Installing opencv (for helpers/detect_approach.py)"
& $pyExe -m pip install "opencv-python-headless>=4.8"
if ($LASTEXITCODE -eq 0) {
    Write-OK "opencv-python-headless installed."
} else {
    Write-Warn2 "opencv install failed -- detect_approach.py will not run until 'pip install opencv-python-headless' succeeds."
}

# ----------------------------------------------------------------- ffmpeg
Write-Step "Checking ffmpeg + ffprobe"
$haveFfmpeg  = (Get-Command ffmpeg  -ErrorAction SilentlyContinue) -ne $null
$haveFfprobe = (Get-Command ffprobe -ErrorAction SilentlyContinue) -ne $null
if ($haveFfmpeg -and $haveFfprobe) {
    Write-OK ((& ffmpeg -version | Select-Object -First 1))
} else {
    Write-Warn2 "ffmpeg/ffprobe missing. Attempting winget install..."
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
        Write-Warn2 "If ffmpeg was just installed, you may need to open a NEW terminal for PATH to refresh, then re-run this script."
    } else {
        Write-Err2 "winget unavailable. Install ffmpeg manually:"
        Write-Host "  1) Download a static build from https://www.gyan.dev/ffmpeg/builds/  (release essentials)"
        Write-Host "  2) Unzip to e.g. C:\ffmpeg, add C:\ffmpeg\bin to PATH"
        Write-Host "  3) Open a new terminal and verify: ffmpeg -version"
    }
}

# ----------------------------------------------------------------- yt-dlp (optional)
Write-Step "Checking yt-dlp (optional, for URL sources)"
if (Get-Command yt-dlp -ErrorAction SilentlyContinue) {
    Write-OK "yt-dlp present."
} else {
    Write-Warn2 "yt-dlp missing. Installing via pip (optional -- only needed for URL sources)."
    & $pyExe -m pip install yt-dlp
}

# ----------------------------------------------------------------- ElevenLabs key validation
Write-Step "Validating ElevenLabs API key"
$envPath = Join-Path $RepoRoot ".env"
if (-not (Test-Path $envPath)) {
    Write-Warn2 ".env file missing at $envPath. Creating empty stub from .env.example."
    $examplePath = Join-Path $RepoRoot ".env.example"
    if (Test-Path $examplePath) {
        Copy-Item $examplePath $envPath
    } else {
        "ELEVENLABS_API_KEY=" | Out-File -Encoding ascii $envPath
    }
    Write-Warn2 "Edit $envPath and paste your key, then re-run this script."
    exit 1
}
$keyLine = Get-Content $envPath | Where-Object { $_ -match '^ELEVENLABS_API_KEY=' } | Select-Object -First 1
$key = ""
if ($keyLine) {
    $key = ($keyLine -replace '^ELEVENLABS_API_KEY=', '').Trim().Trim('"').Trim("'")
}
if (-not $key) {
    Write-Err2 "ELEVENLABS_API_KEY is empty in .env. Paste your key and re-run."
    exit 1
}
try {
    $resp = Invoke-WebRequest -Uri "https://api.elevenlabs.io/v1/user" -Headers @{ "xi-api-key" = $key } -UseBasicParsing -TimeoutSec 15
    if ($resp.StatusCode -eq 200) { Write-OK "ElevenLabs key is valid (HTTP 200)." }
    else { Write-Warn2 "Unexpected HTTP $($resp.StatusCode) -- check your key." }
} catch {
    $code = $null
    if ($_.Exception.Response -ne $null) {
        try { $code = $_.Exception.Response.StatusCode.value__ } catch {}
    }
    if ($code -eq 401) { Write-Err2 "ElevenLabs returned 401 Unauthorized -- the key is wrong or expired." }
    else { Write-Warn2 "Could not reach ElevenLabs: $($_.Exception.Message)" }
}

# ----------------------------------------------------------------- Skill registration
Write-Step "Registering skill with Claude Code"
$claudeSkills = Join-Path $env:USERPROFILE ".claude\skills"
New-Item -ItemType Directory -Force -Path $claudeSkills | Out-Null
$linkPath = Join-Path $claudeSkills "video-use"
if (Test-Path $linkPath) {
    Write-OK "Skill already registered at $linkPath (overwriting junction)."
    cmd /c rmdir "$linkPath" 2>$null
}
cmd /c mklink /J "$linkPath" "$RepoRoot" | Out-Null
if (Test-Path $linkPath) {
    Write-OK "Junction created: $linkPath -> $RepoRoot"
} else {
    Write-Warn2 "Could not create junction. You can still use the skill with full paths."
}

# ----------------------------------------------------------------- Smoke test
Write-Step "Smoke test"
$smokeFailed = $false
foreach ($helper in @("timeline_view.py", "render.py", "transcribe.py", "pack_transcripts.py", "grade.py", "analyze_audio.py", "sparkle_overlay.py")) {
    $script = Join-Path $RepoRoot "helpers\$helper"
    if (-not (Test-Path $script)) { Write-Warn2 "missing helper: $helper"; continue }
    & $pyExe $script --help *>$null
    if ($LASTEXITCODE -eq 0) { Write-OK "$helper --help OK" }
    else { Write-Err2 "$helper --help FAILED"; $smokeFailed = $true }
}

if (Get-Command ffprobe -ErrorAction SilentlyContinue) {
    & ffprobe -version | Select-Object -First 1 | Out-Null
    Write-OK "ffprobe runs."
}

if ($smokeFailed) {
    Write-Warn2 "One or more helpers failed --help. Check Python deps."
} else {
    Write-Host "`n=== Setup complete ===" -ForegroundColor Cyan
    Write-Host "Next: cd into a folder of raw video, run 'claude' there, and say:"
    Write-Host '   "edit these into a launch video"' -ForegroundColor Yellow
}
