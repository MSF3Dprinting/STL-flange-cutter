<#
.SYNOPSIS
    Builds the Windows packages of Flange Cutter: the per-user installer and the portable ZIP.

.DESCRIPTION
    1. PyInstaller one-folder build:  build\out\dist\FlangeCutter\FlangeCutter.exe (+ _internal\)
    2. Code signing of FlangeCutter.exe (only with a certificate)
    3. Smoke test: starts the built app on 127.0.0.1, checks /api/ping and the page, quits it
    4. Portable ZIP:  build\out\FlangeCutter-<version>-portable.zip  (a FlangeCutter\ folder inside)
    5. Installer:     build\out\FlangeCutter-Setup-<version>.exe     (Inno Setup; signed too)

    The version comes from flangecutter\__init__.py. Runs in Windows PowerShell 5.1 and PowerShell 7,
    needs no admin rights, and stops with a message and exit code 1 at the first failure.

.PARAMETER Python
    python.exe with requirements.txt and requirements-dev.txt installed (Python 3.12).
    Default: .venv\Scripts\python.exe in the project folder, else python.exe from PATH.

.PARAMETER InnoSetupCompiler
    Path of ISCC.exe. Default: PATH, then the usual Inno Setup 6 folders (per user and per machine).

.PARAMETER CertificateFile
    Code signing certificate as a .pfx file. Without -CertificateFile or -CertificateThumbprint
    nothing is signed.

.PARAMETER CertificatePassword
    Password of the .pfx file, as a string or a SecureString.

.PARAMETER CertificateThumbprint
    SHA-1 thumbprint of a code signing certificate in the current user's store (Cert:\CurrentUser\My),
    for example on a USB token. Use instead of -CertificateFile.

.PARAMETER TimestampUrl
    RFC 3161 timestamp server, so signatures stay valid after the certificate expires.
    Pass "" to sign without a timestamp (not recommended).

.PARAMETER SignTool
    Path of signtool.exe. Default: PATH, then the newest Windows 10/11 SDK.

.PARAMETER SkipSmokeTest
    Do not start the built app (only for build machines that cannot run it).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File build\build_windows.ps1

.EXAMPLE
    .\build\build_windows.ps1 -CertificateFile D:\keys\codesign.pfx -CertificatePassword (Read-Host -AsSecureString "PFX password")
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$InnoSetupCompiler = "",
    [string]$CertificateFile = "",
    [object]$CertificatePassword = $null,
    [string]$CertificateThumbprint = "",
    [string]$TimestampUrl = "http://timestamp.digicert.com",
    [string]$SignTool = "",
    [switch]$SkipSmokeTest
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 3.0

# Any error ends the build with a clear message and exit code 1.
trap {
    Write-Host ""
    Write-Host "BUILD FAILED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "(build_windows.ps1, line $($_.InvocationInfo.ScriptLineNumber))" -ForegroundColor DarkGray
    exit 1
}

$Root = Split-Path -Parent $PSScriptRoot
$Out = Join-Path $Root "build\out"
$Dist = Join-Path $Out "dist"
$AppDir = Join-Path $Dist "FlangeCutter"
$Exe = Join-Path $AppDir "FlangeCutter.exe"
$Clock = [Diagnostics.Stopwatch]::StartNew()

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host ("==> {0}   [{1:mm\:ss}]" -f $Text, $Clock.Elapsed) -ForegroundColor Cyan
}

# Runs a program and shows its output as it comes; throws when it returns an error code.
# Windows PowerShell 5.1 would stop at the first line a program writes to stderr, so that is
# switched off here and only the exit code counts.
function Invoke-Program([string]$What, [string]$File, [string[]]$Arguments) {
    $eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $File @Arguments 2>&1 | ForEach-Object { Write-Host "    $_" } }
    finally { $ErrorActionPreference = $eap }
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE). See the messages above." }
}

# Runs a program quietly and returns its output, or $null when it cannot run or fails.
function Get-ProgramOutput([string]$File, [string[]]$Arguments) {
    $eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $lines = & $File @Arguments 2>$null } catch { return $null } finally { $ErrorActionPreference = $eap }
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($lines -join "`n").Trim()
}

# The first file that exists: an explicit path, a program on PATH, or one of the usual places.
function Find-Tool([string]$Given, [string]$Name, [string[]]$Places, [string]$Hint) {
    if ($Given) {
        if (Test-Path -LiteralPath $Given -PathType Leaf) { return (Resolve-Path -LiteralPath $Given).Path }
        $command = Get-Command $Given -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) { return $command.Path }
        throw "$Name not found: $Given. $Hint"
    }
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Path }
    foreach ($place in $Places) { if ($place -and (Test-Path -LiteralPath $place -PathType Leaf)) { return $place } }
    throw "$Name not found. $Hint"
}

# --------------------------------------------------------------------------------------------
Write-Step "Checking the tools"

$init = Get-Content -LiteralPath (Join-Path $Root "flangecutter\__init__.py") -Raw -Encoding UTF8
if ($init -notmatch '(?m)^__version__\s*=\s*"([^"]+)"') { throw "No __version__ found in flangecutter\__init__.py." }
$Version = $Matches[1]
if ($Version -notmatch '^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?') { throw "Version '$Version' must start with numbers, like 1.2.0." }
$NumericVersion = (1..4 | ForEach-Object { if ($Matches[$_]) { [int]$Matches[$_] } else { 0 } }) -join "."
Write-Host "    Flange Cutter $Version (file version $NumericVersion)"
# Windows limits most paths to 260 characters, and the build writes files up to ~120 characters deep.
if ($Root.Length -gt 135) {
    Write-Warning ("The project folder path is $($Root.Length) characters long. If the build fails with " +
                   "'filename too long', move the project to a shorter folder, such as C:\src\STL-flange-cut.")
}

if (-not $Python) {
    $venv = Join-Path $Root ".venv\Scripts\python.exe"
    $Python = if (Test-Path -LiteralPath $venv) { $venv } else { "python.exe" }
}
$Python = Find-Tool $Python "python.exe" @() ("Create .venv in the project folder (py -3.12 -m venv .venv, then install " +
          "requirements.txt and requirements-dev.txt into it), or pass -Python <path of python.exe>.")
$pythonVersion = Get-ProgramOutput $Python @("-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])")
if (-not $pythonVersion) { throw "This Python does not run: $Python" }
Write-Host "    Python $pythonVersion  $Python"
if (-not $pythonVersion.StartsWith("3.12.")) { Write-Warning "Flange Cutter is built and tested with Python 3.12." }
$check = "import PyInstaller, flask, manifold3d, shapely, trimesh, numpy, networkx; print('ok')"
if ((Get-ProgramOutput $Python @("-c", $check)) -ne "ok") {
    throw ("Python packages are missing. Install them with:`n    `"$Python`" -m pip install " +
           "-r `"$(Join-Path $Root 'requirements.txt')`" -r `"$(Join-Path $Root 'requirements-dev.txt')`"")
}

$innoPlaces = @()
foreach ($base in @((Join-Path $env:LOCALAPPDATA "Programs"), ${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
    if ($base) { $innoPlaces += Join-Path $base "Inno Setup 6\ISCC.exe" }
}
$Iscc = Find-Tool $InnoSetupCompiler "ISCC.exe" $innoPlaces ("Install Inno Setup 6 (no admin rights needed: " +
        "winget install JRSoftware.InnoSetup --scope user) or pass -InnoSetupCompiler <path of ISCC.exe>.")
Write-Host "    Inno Setup  $Iscc"

# Code signing: optional. $SignArguments are the signtool.exe arguments without the file name.
$Signing = [bool]($CertificateFile -or $CertificateThumbprint)
$SignArguments = @()
if ($Signing) {
    if ($CertificateFile -and $CertificateThumbprint) { throw "Give -CertificateFile or -CertificateThumbprint, not both." }
    $sdkPlaces = @()
    foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        $bin = if ($base) { Join-Path $base "Windows Kits\10\bin" } else { "" }
        if ($bin -and (Test-Path -LiteralPath $bin)) {
            $sdkPlaces += Get-ChildItem -LiteralPath $bin -Directory | Where-Object { $_.Name -match '^\d+(\.\d+)+$' } |
                Sort-Object { [version]$_.Name } -Descending | ForEach-Object { Join-Path $_.FullName "x64\signtool.exe" }
            $sdkPlaces += Join-Path $bin "x64\signtool.exe"
            $sdkPlaces += Join-Path $base "Windows Kits\10\App Certification Kit\signtool.exe"
        }
    }
    $SignTool = Find-Tool $SignTool "signtool.exe" $sdkPlaces ("Install the Windows SDK signing tools " +
                "(https://developer.microsoft.com/windows/downloads/windows-sdk/) or pass -SignTool <path of signtool.exe>.")
    $SignArguments = @("sign", "/fd", "sha256", "/d", "Flange Cutter")
    if ($CertificateFile) {
        if (-not (Test-Path -LiteralPath $CertificateFile -PathType Leaf)) { throw "Certificate file not found: $CertificateFile" }
        $SignArguments += @("/f", (Resolve-Path -LiteralPath $CertificateFile).Path)
        if ($CertificatePassword -is [System.Security.SecureString]) {
            $CertificatePassword = (New-Object System.Management.Automation.PSCredential("pfx", $CertificatePassword)).GetNetworkCredential().Password
        }
        if ($CertificatePassword) { $SignArguments += @("/p", [string]$CertificatePassword) }
    } else {
        $SignArguments += @("/sha1", $CertificateThumbprint.Replace(" ", ""), "/s", "My")
    }
    if ($TimestampUrl) { $SignArguments += @("/tr", $TimestampUrl, "/td", "sha256") }
    else { Write-Warning "Signing without a timestamp: the signatures end when the certificate expires." }
    if ($SignArguments -match '"') { throw "The certificate password and the other signing values must not contain double quotes." }
    Write-Host "    Signing     $SignTool"
} else {
    Write-Host "    Signing     skipped: no certificate given (-CertificateFile or -CertificateThumbprint)." -ForegroundColor Yellow
}

function Invoke-Signing([string]$File) {
    Invoke-Program "Signing $File" $SignTool ($SignArguments + @($File))
}

# --------------------------------------------------------------------------------------------
Write-Step "Cleaning build\out"

$Zip = Join-Path $Out "FlangeCutter-$Version-portable.zip"
$Setup = Join-Path $Out "FlangeCutter-Setup-$Version.exe"
foreach ($old in @($Dist, (Join-Path $Out "work"), (Join-Path $Out "smoke-home"), $Zip, $Setup)) {
    if (Test-Path -LiteralPath $old) {
        try { Remove-Item -LiteralPath $old -Recurse -Force }
        catch { throw "Cannot delete $old. Is Flange Cutter from an earlier build still running? ($($_.Exception.Message))" }
    }
}
New-Item -ItemType Directory -Force -Path $Out | Out-Null

# --------------------------------------------------------------------------------------------
Write-Step "PyInstaller: one-folder build"

Invoke-Program "PyInstaller" $Python @("-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
    "--distpath", $Dist, "--workpath", (Join-Path $Out "work"), (Join-Path $PSScriptRoot "flangecutter.spec"))
foreach ($needed in @($Exe, (Join-Path $AppDir "_internal\flangecutter\static\index.html"),
                      (Join-Path $AppDir "_internal\flangecutter\static\vendor\three\build\three.module.js"))) {
    if (-not (Test-Path -LiteralPath $needed)) { throw "The build is missing $needed" }
}
$files = Get-ChildItem -LiteralPath $AppDir -Recurse -File
Write-Host ("    {0} files, {1:N1} MB" -f $files.Count, (($files | Measure-Object Length -Sum).Sum / 1MB))

# --------------------------------------------------------------------------------------------
# Signed before the smoke test, so the test starts exactly the EXE that ships.
if ($Signing) {
    Write-Step "Signing FlangeCutter.exe"
    Invoke-Signing $Exe
}

# --------------------------------------------------------------------------------------------
if ($SkipSmokeTest) {
    Write-Step "Smoke test skipped (-SkipSmokeTest)"
} else {
    Write-Step "Smoke test: start the built app, check it, quit it"

    # The app keeps its lock file and log in a scratch folder here, never in the user's own folder.
    $smokeHome = Join-Path $Out "smoke-home"
    New-Item -ItemType Directory -Force -Path $smokeHome | Out-Null
    $savedHome = $env:FLANGECUTTER_HOME
    $env:FLANGECUTTER_HOME = $smokeHome
    $process = $null
    try {
        $started = [Diagnostics.Stopwatch]::StartNew()
        $process = Start-Process -FilePath $Exe -ArgumentList "--no-browser" -WindowStyle Hidden -PassThru
        $null = $process.Handle          # keeps the exit code readable in Windows PowerShell 5.1
        $lock = Join-Path $smokeHome "flangecutter.lock"
        $port = 0
        while (-not $port) {
            if ($process.HasExited) { throw "The built app stopped at start (exit code $($process.ExitCode)). Log: $smokeHome\logs" }
            if ($started.Elapsed.TotalSeconds -gt 120) { throw "The built app did not start within 120 s." }
            Start-Sleep -Milliseconds 200
            try { $port = [int](Get-Content -LiteralPath $lock -Raw -ErrorAction Stop | ConvertFrom-Json).port } catch { $port = 0 }
        }
        $web = New-Object System.Net.WebClient
        $web.Proxy = $null               # straight to 127.0.0.1, never through a proxy
        $web.Encoding = [System.Text.Encoding]::UTF8
        $url = "http://127.0.0.1:$port"
        $ping = $web.DownloadString("$url/api/ping") | ConvertFrom-Json
        if ($ping.app -ne "flangecutter" -or $ping.version -ne $Version) { throw "Unexpected /api/ping answer: $($ping | ConvertTo-Json -Compress)" }
        Write-Host ("    started in {0:N1} s on port {1}" -f $started.Elapsed.TotalSeconds, $port)
        if ($web.DownloadString("$url/") -notmatch "<html") { throw "The built app does not serve its page at /." }
        $bolts = $web.DownloadString("$url/api/bolts") | ConvertFrom-Json
        Write-Host "    page OK, bolt sizes: $(($bolts.bolts | ForEach-Object { $_.name }) -join ', ')"
        $web.Headers.Add("Content-Type", "application/json")
        $null = $web.UploadString("$url/api/shutdown", "POST", "{}")
        if (-not $process.WaitForExit(20000)) { throw "The built app did not quit after /api/shutdown." }
        if ($process.ExitCode -ne 0) { throw "The built app quit with exit code $($process.ExitCode). Log: $smokeHome\logs" }
        if (Test-Path -LiteralPath $lock) { throw "The built app left its lock file behind." }
        Write-Host "    quit OK"
    } finally {
        if ($process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
        $env:FLANGECUTTER_HOME = $savedHome
    }
    Remove-Item -LiteralPath $smokeHome -Recurse -Force -ErrorAction SilentlyContinue
}

# --------------------------------------------------------------------------------------------
Write-Step "Portable ZIP"

# .NET ZipFile with our own entry names: forward slashes on every PowerShell version.
Add-Type -AssemblyName System.IO.Compression
try { Add-Type -AssemblyName System.IO.Compression.FileSystem }        # .NET Framework (PowerShell 5.1)
catch { Add-Type -AssemblyName System.IO.Compression.ZipFile }         # .NET (PowerShell 7), if needed
$base = (Get-Item -LiteralPath $AppDir).FullName.TrimEnd("\").Length + 1
$archive = [System.IO.Compression.ZipFile]::Open($Zip, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($file in (Get-ChildItem -LiteralPath $AppDir -Recurse -File)) {
        $name = "FlangeCutter/" + $file.FullName.Substring($base).Replace("\", "/")
        $null = [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $file.FullName, $name,
                    [System.IO.Compression.CompressionLevel]::Optimal)
    }
} finally {
    $archive.Dispose()
}

# --------------------------------------------------------------------------------------------
Write-Step "Inno Setup: per-user installer"

$isccArguments = @("/Q", "/DAppVersion=$Version", "/DAppNumericVersion=$NumericVersion",
                   "/DDistDir=$AppDir", "/DOutputDir=$Out")
if ($Signing) {
    # Inno Setup runs signtool itself, for the uninstaller and for Setup. In this command $q stands
    # for a double quote, $$ for $, and $f for the file to sign.
    $command = (@($SignTool) + $SignArguments | ForEach-Object { '$q' + $_.Replace('$', '$$') + '$q' }) -join " "
    $isccArguments += @("/DSign", ("/Sflangesign=" + $command + ' $f'))
}
Invoke-Program "Inno Setup" $Iscc ($isccArguments + @((Join-Path $PSScriptRoot "installer.iss")))

# --------------------------------------------------------------------------------------------
Write-Step "Done"

foreach ($package in @($Setup, $Zip)) {
    if (-not (Test-Path -LiteralPath $package)) { throw "Missing package: $package" }
    if ($Signing -and $package.EndsWith(".exe")) {
        foreach ($signed in @($package, $Exe)) {
            if ($null -eq (Get-ProgramOutput $SignTool @("verify", "/pa", $signed))) {
                Write-Warning "signtool cannot verify the signature of $signed on this computer (is the certificate's root trusted here?)."
            }
        }
    }
    $item = Get-Item -LiteralPath $package
    Write-Host ("    {0}  {1,6:N1} MB  SHA-256 {2}" -f $item.FullName, ($item.Length / 1MB),
                (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash)
}
if (-not $Signing) {
    Write-Host "    Not signed: Windows SmartScreen may warn users (More info > Run anyway)." -ForegroundColor Yellow
}
Write-Host ("    Total time {0:mm\:ss}" -f $Clock.Elapsed)
exit 0
