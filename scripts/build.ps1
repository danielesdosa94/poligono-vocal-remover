<#
.SYNOPSIS
    Build Polígono AI Hub end to end: motor.exe with PyInstaller, then the
    Windows installer with electron-builder.

.DESCRIPTION
    Run from anywhere; the script works out the repo root from its own
    location and does everything from there. PyInstaller resolves --distpath
    and --workpath against the current directory, and package.json expects
    dist\motor, so the working directory is not a detail.

.PARAMETER SkipMotor
    Reuse the existing dist\motor and only rebuild the installer. The Python
    side is the slow half, so this is the one to use while iterating on the
    Electron packaging.

.PARAMETER DebugConsole
    Build a motor.exe that shows its console window, for when the frozen
    daemon misbehaves and the JSON traffic has to be watched directly.

.PARAMETER Clean
    Delete dist\motor, build\pyinstaller and release\ first.

.EXAMPLE
    .\scripts\build.ps1
    .\scripts\build.ps1 -SkipMotor
    .\scripts\build.ps1 -Clean -DebugConsole
#>

[CmdletBinding()]
param(
    [switch]$SkipMotor,
    [switch]$DebugConsole,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvActivate = Join-Path $RepoRoot 'python\venv\Scripts\Activate.ps1'
$MotorDist = Join-Path $RepoRoot 'dist\motor'
$MotorExe = Join-Path $MotorDist 'motor.exe'
$ReleaseDir = Join-Path $RepoRoot 'release'
$FfmpegDir = Join-Path $RepoRoot 'resources\bin\ffmpeg'

function Write-Step([string]$Message) {
    Write-Host ''
    Write-Host "=== $Message" -ForegroundColor Cyan
}

function Format-Size([long]$Bytes) {
    if ($Bytes -ge 1GB) { return '{0:N2} GB' -f ($Bytes / 1GB) }
    if ($Bytes -ge 1MB) { return '{0:N1} MB' -f ($Bytes / 1MB) }
    return '{0:N0} KB' -f ($Bytes / 1KB)
}

function Get-DirectorySize([string]$Path) {
    if (-not (Test-Path $Path)) { return 0 }
    $measured = Get-ChildItem -Path $Path -Recurse -File -ErrorAction SilentlyContinue |
        Measure-Object -Property Length -Sum
    if ($null -eq $measured.Sum) { return 0 }
    return [long]$measured.Sum
}

Set-Location $RepoRoot
Write-Host "Repo root: $RepoRoot"

# -----------------------------------------------------------------------------
# Preflight
# -----------------------------------------------------------------------------

Write-Step 'Checking prerequisites'

if (-not (Test-Path $VenvActivate)) {
    throw "Python venv not found at $VenvActivate. Create it with: python -m venv python\venv"
}
foreach ($binary in @('ffmpeg.exe', 'ffprobe.exe')) {
    $path = Join-Path $FfmpegDir $binary
    if (-not (Test-Path $path)) {
        throw "$binary not found in $FfmpegDir. It is not in git; copy it in before building."
    }
}
Write-Host "  venv        OK"
Write-Host "  ffmpeg      OK ($(Format-Size (Get-DirectorySize $FfmpegDir)))"

if ($Clean) {
    Write-Step 'Cleaning previous output'
    foreach ($path in @($MotorDist, (Join-Path $RepoRoot 'build\pyinstaller'), $ReleaseDir)) {
        if (Test-Path $path) {
            Remove-Item -Recurse -Force $path
            Write-Host "  removed $path"
        }
    }
}

# -----------------------------------------------------------------------------
# 1. motor.exe
# -----------------------------------------------------------------------------

if ($SkipMotor) {
    if (-not (Test-Path $MotorExe)) {
        throw "-SkipMotor was given but $MotorExe does not exist."
    }
    Write-Step 'Skipping PyInstaller (using the existing dist\motor)'
} else {
    Write-Step 'Building motor.exe (PyInstaller)'

    # Dot-sourced: the venv has to stay active for the pyinstaller call below.
    . $VenvActivate

    if ($DebugConsole) {
        $env:MOTOR_DEBUG_CONSOLE = '1'
        Write-Host '  console window ENABLED for this build'
    } else {
        Remove-Item Env:\MOTOR_DEBUG_CONSOLE -ErrorAction SilentlyContinue
    }

    $started = Get-Date
    # --workpath keeps PyInstaller's scratch out of build\, which
    # electron-builder reads as its buildResources directory.
    pyinstaller python\motor.spec --workpath build\pyinstaller --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

    if (-not (Test-Path $MotorExe)) { throw "PyInstaller reported success but $MotorExe is missing." }
    Write-Host ('  built in {0:N0}s' -f ((Get-Date) - $started).TotalSeconds)
    Write-Host "  dist\motor  $(Format-Size (Get-DirectorySize $MotorDist))"
}

# -----------------------------------------------------------------------------
# 2. Smoke test: does the frozen motor answer at all?
# -----------------------------------------------------------------------------

Write-Step 'Smoke test: ping -> pong'

# No --parent-pid: the watchdog would kill it the moment this shell moves on.
$pong = '{"type":"ping"}' | & $MotorExe 2>$null | Select-String -Pattern '"event"\s*:\s*"pong"' -Quiet
if ($pong) {
    Write-Host '  motor.exe answered pong' -ForegroundColor Green
} else {
    Write-Warning '  motor.exe did NOT answer pong. The installer will be built anyway, but test it before shipping.'
    Write-Warning "  Reproduce with:  '{\"type\":\"ping\"}' | & '$MotorExe'"
}

# -----------------------------------------------------------------------------
# 3. Installer
# -----------------------------------------------------------------------------

Write-Step 'Building the Windows installer (electron-builder)'

$started = Get-Date
npm run dist:win
if ($LASTEXITCODE -ne 0) { throw "electron-builder failed with exit code $LASTEXITCODE" }
Write-Host ('  built in {0:N0}s' -f ((Get-Date) - $started).TotalSeconds)

# -----------------------------------------------------------------------------
# 4. Report
# -----------------------------------------------------------------------------

Write-Step 'Result'

$installers = Get-ChildItem -Path $ReleaseDir -Filter '*.exe' -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending

if (-not $installers) {
    Write-Warning "No installer found in $ReleaseDir"
    exit 1
}

Write-Host ''
Write-Host ('  {0,-14} {1}' -f 'dist\motor', (Format-Size (Get-DirectorySize $MotorDist)))
Write-Host ('  {0,-14} {1}' -f 'ffmpeg', (Format-Size (Get-DirectorySize $FfmpegDir)))
Write-Host ''
foreach ($installer in $installers) {
    Write-Host ('  {0}  {1}' -f (Format-Size $installer.Length), $installer.Name) -ForegroundColor Green
}
Write-Host ''
Write-Host "  Installer folder: $ReleaseDir"
Write-Host '  This build is UNSIGNED. See docs/DISTRIBUTION.md for the SmartScreen notice.'
