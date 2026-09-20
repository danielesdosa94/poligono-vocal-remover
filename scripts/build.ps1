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

<#
.SYNOPSIS
    Make electron-builder's winCodeSign package available without needing the
    symlink privilege.

.DESCRIPTION
    electron-builder signs every .exe that goes into extraResources, so before
    packaging it fetches its winCodeSign bundle - even with no certificate
    configured, because the vendor path is resolved before the "no signing
    info, skipping" check.

    That bundle is a .7z containing macOS symlinks (libcrypto.dylib,
    libssl.dylib). Creating a symlink on Windows needs
    SeCreateSymbolicLinkPrivilege, which a normal user does not have unless
    Developer Mode is on, so the extraction fails with

        ERROR: Cannot create symbolic link : A required privilege is not held

    and the whole build dies. The two dylibs are macOS-only and useless here.

    So we extract the bundle ourselves, skipping the darwin tree, straight
    into the directory electron-builder looks in. It then finds it cached and
    never downloads or extracts anything.

    Failure here is not fatal: we warn and let electron-builder try its own
    way, which is exactly the behaviour without this function.
#>
function Initialize-WinCodeSignCache {
    # Pinned to what electron-builder 26.4.0 asks for. If it ever moves to a
    # newer bundle this simply stops matching, electron-builder downloads its
    # own, and we are back to needing Developer Mode - with a clear error.
    $version = 'winCodeSign-2.6.0'
    $cacheRoot = Join-Path $env:LOCALAPPDATA 'electron-builder\Cache\winCodeSign'
    $target = Join-Path $cacheRoot $version

    if (Test-Path (Join-Path $target 'windows-10\x64\signtool.exe')) {
        Write-Host '  winCodeSign OK (cached)'
        return
    }

    $sevenZip = Join-Path $RepoRoot 'node_modules\7zip-bin\win\x64\7za.exe'
    if (-not (Test-Path $sevenZip)) {
        Write-Warning '  7za.exe not found; skipping the winCodeSign cache warm-up'
        return
    }

    $url = "https://github.com/electron-userland/electron-builder-binaries/releases/download/$version/$version.7z"
    $archive = Join-Path ([System.IO.Path]::GetTempPath()) "$version.7z"

    try {
        Write-Host '  winCodeSign not cached; fetching it without the macOS symlinks'
        New-Item -ItemType Directory -Force -Path $cacheRoot | Out-Null
        Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing

        # -xr!darwin drops the two symlinked dylibs, which is the whole point.
        & $sevenZip x -bso0 -bsp0 -y $archive "-o$target" '-xr!darwin' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "7za exited with $LASTEXITCODE" }

        if (Test-Path (Join-Path $target 'windows-10\x64\signtool.exe')) {
            Write-Host '  winCodeSign OK (extracted)'
        } else {
            throw 'signtool.exe missing after extraction'
        }
    } catch {
        Write-Warning "  Could not pre-populate the winCodeSign cache: $_"
        Write-Warning '  If the build fails on "Cannot create symbolic link", turn on Windows Developer Mode.'
        if (Test-Path $target) { Remove-Item -Recurse -Force $target -ErrorAction SilentlyContinue }
    } finally {
        Remove-Item $archive -Force -ErrorAction SilentlyContinue
    }
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

# The nsis-web installer downloads the app package at install time, so the URL
# is baked into the .exe. Get it wrong and every customer's install fails with
# nothing to fall back on, long after the build looked fine. Fail here instead.
$packageUrl = (Get-Content (Join-Path $RepoRoot 'package.json') -Raw | ConvertFrom-Json).build.nsisWeb.appPackageUrl
if ([string]::IsNullOrWhiteSpace($packageUrl) -or $packageUrl -match 'REPLACE-ME') {
    throw @"
build.nsisWeb.appPackageUrl in package.json is still a placeholder:
    $packageUrl
This is the URL the installer downloads the app package from. Set it to the
folder the .nsis.7z will be uploaded to (with a trailing slash) before building
anything you intend to ship. See docs/DISTRIBUTION.md section 1.
"@
}
Write-Host "  package URL $packageUrl"

Initialize-WinCodeSignCache

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

# Wipe stale artifacts first. Changing target (or a build failing halfway)
# leaves older .exe / .7z files behind, and the report below would happily
# point at one of them: an orphaned 0.25 MB installer from a failed run looks
# exactly like a real nsis-web installer. win-unpacked is left alone, since
# it is rebuilt in place and re-copying 4.8 GB for nothing is not free.
Get-ChildItem -Path $ReleaseDir -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -in '.exe', '.7z', '.blockmap', '.yml', '.yaml' } |
    ForEach-Object { Remove-Item $_.FullName -Force }
Remove-Item (Join-Path $ReleaseDir 'nsis-web') -Recurse -Force -ErrorAction SilentlyContinue

$started = Get-Date
npm run dist:win
if ($LASTEXITCODE -ne 0) { throw "electron-builder failed with exit code $LASTEXITCODE" }
Write-Host ('  built in {0:N0}s' -f ((Get-Date) - $started).TotalSeconds)

# -----------------------------------------------------------------------------
# 4. Report
# -----------------------------------------------------------------------------

Write-Step 'Result'

# -Recurse: nsis-web writes into release\nsis-web\, not release\ itself.
$installers = @(Get-ChildItem -Path $ReleaseDir -Filter '*Setup.exe' -File -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notlike '*__uninstaller*' })
$packages = @(Get-ChildItem -Path $ReleaseDir -Filter '*.nsis.7z' -File -Recurse -ErrorAction SilentlyContinue)

if ($installers.Count -ne 1 -or $packages.Count -ne 1) {
    Write-Warning "Expected exactly one installer and one app package under $ReleaseDir"
    Write-Warning ("  installers found: {0}" -f $installers.Count)
    Write-Warning ("  packages found:   {0}" -f $packages.Count)
    $installers + $packages | ForEach-Object { Write-Warning ("    " + $_.FullName) }
    Write-Warning '  Refusing to name one: shipping the wrong file here breaks every install.'
    exit 1
}
$installer = $installers[0]
$package = $packages[0]

Write-Host ''
Write-Host ('  {0,-14} {1}' -f 'dist\motor', (Format-Size (Get-DirectorySize $MotorDist)))
Write-Host ('  {0,-14} {1}' -f 'ffmpeg', (Format-Size (Get-DirectorySize $FfmpegDir)))
Write-Host ''
Write-Host '  UPLOAD BOTH OF THESE. The installer is useless without the package:' -ForegroundColor Yellow
Write-Host ('    {0,10}  {1}' -f (Format-Size $installer.Length), $installer.Name) -ForegroundColor Green
Write-Host ('    {0,10}  {1}' -f (Format-Size $package.Length), $package.Name) -ForegroundColor Green
Write-Host ''
Write-Host "  The installer will fetch the package from:"
Write-Host ("    $packageUrl" + $package.Name)
Write-Host '  That exact URL must serve that exact file, or every install fails.'
Write-Host ''
Write-Host "  Release folder: $ReleaseDir"
Write-Host '  This build is UNSIGNED. See docs/DISTRIBUTION.md for the SmartScreen notice.'
Write-Host ''
Write-Host '  SHA-256 to publish next to the download link:'
Write-Host ('    ' + (Get-FileHash $installer.FullName -Algorithm SHA256).Hash)
