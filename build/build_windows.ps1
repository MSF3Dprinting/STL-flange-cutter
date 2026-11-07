<#
.SYNOPSIS
    Builds the Windows package of Flange Cutter: the portable ZIP (no installer).

.DESCRIPTION
    1. PyInstaller one-folder build:  build\out\dist\FlangeCutter\FlangeCutter.exe (+ _internal\)
    2. Code signing of FlangeCutter.exe (only with a certificate)
    3. Portable ZIP:  build\out\FlangeCutter-<version>-portable.zip  (a FlangeCutter\ folder inside)
    4. Smoke test of the ZIP: unzips it into build\out\zip-test, starts that FlangeCutter.exe on
       127.0.0.1, checks /api/ping and the page, quits it. So the test runs exactly what users unzip.

    Users unzip the ZIP by hand and double-click FlangeCutter.exe; nothing is installed.
    The version comes from flangecutter\__init__.py. Runs in Windows PowerShell 5.1 and PowerShell 7,
    needs no admin rights, and stops with a message and exit code 1 at the first failure.

.PARAMETER Python
    python.exe with requirements.txt and requirements-dev.txt installed (Python 3.12).
    Default: .venv\Scripts\python.exe in the project folder, else python.exe from PATH.

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
$ZipTest = Join-Path $Out "zip-test"
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
if ($Version -notmatch '^\d+(\.\d+){0,3}') { throw "Version '$Version' must start with numbers, like 1.2.0." }
Write-Host "    Flange Cutter $Version"
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

# --------------------------------------------------------------------------------------------
Write-Step "Cleaning build\out"

$Zip = Join-Path $Out "FlangeCutter-$Version-portable.zip"
foreach ($old in @($Dist, (Join-Path $Out "work"), (Join-Path $Out "smoke-home"), $ZipTest, $Zip)) {
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
# Signed before zipping, so the ZIP carries the signed EXE that the smoke test then starts.
if ($Signing) {
    Write-Step "Signing FlangeCutter.exe"
    Invoke-Program "Signing $Exe" $SignTool ($SignArguments + @($Exe))
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
if ($SkipSmokeTest) {
    Write-Step "Smoke test skipped (-SkipSmokeTest)"
} else {
    Write-Step "Smoke test: unzip the ZIP, start that FlangeCutter.exe, check it, quit it"

    [System.IO.Compression.ZipFile]::ExtractToDirectory($Zip, $ZipTest)
    $testExe = Join-Path $ZipTest "FlangeCutter\FlangeCutter.exe"
    if (-not (Test-Path -LiteralPath $testExe)) { throw "The ZIP has no FlangeCutter\FlangeCutter.exe." }

    # The app keeps its lock file and log in a scratch folder here, never in the user's own folder.
    $smokeHome = Join-Path $Out "smoke-home"
    New-Item -ItemType Directory -Force -Path $smokeHome | Out-Null
    $savedHome = $env:FLANGECUTTER_HOME
    $env:FLANGECUTTER_HOME = $smokeHome
    $process = $null
    try {
        $started = [Diagnostics.Stopwatch]::StartNew()
        $process = Start-Process -FilePath $testExe -ArgumentList "--no-browser" -WindowStyle Hidden -PassThru
        $null = $process.Handle          # keeps the exit code readable in Windows PowerShell 5.1
        $lock = Join-Path $smokeHome "flangecutter.lock"
        $port = 0
        while (-not $port) {
            if ($process.HasExited) { throw "The unzipped app stopped at start (exit code $($process.ExitCode)). Log: $smokeHome\logs" }
            if ($started.Elapsed.TotalSeconds -gt 120) { throw "The unzipped app did not start within 120 s." }
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
        if ($web.DownloadString("$url/") -notmatch "<html") { throw "The unzipped app does not serve its page at /." }
        $bolts = $web.DownloadString("$url/api/bolts") | ConvertFrom-Json
        Write-Host "    page OK, bolt sizes: $(($bolts.bolts | ForEach-Object { $_.name }) -join ', ')"
        $web.Headers.Add("Content-Type", "application/json")
        $null = $web.UploadString("$url/api/shutdown", "POST", "{}")
        if (-not $process.WaitForExit(20000)) { throw "The unzipped app did not quit after /api/shutdown." }
        if ($process.ExitCode -ne 0) { throw "The unzipped app quit with exit code $($process.ExitCode). Log: $smokeHome\logs" }
        if (Test-Path -LiteralPath $lock) { throw "The unzipped app left its lock file behind." }
        Write-Host "    quit OK"
    } finally {
        if ($process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
        $env:FLANGECUTTER_HOME = $savedHome
    }
    Remove-Item -LiteralPath $smokeHome -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $ZipTest -Recurse -Force -ErrorAction SilentlyContinue
}

# --------------------------------------------------------------------------------------------
Write-Step "Done"

if (-not (Test-Path -LiteralPath $Zip)) { throw "Missing package: $Zip" }
if ($Signing -and $null -eq (Get-ProgramOutput $SignTool @("verify", "/pa", $Exe))) {
    Write-Warning "signtool cannot verify the signature of $Exe on this computer (is the certificate's root trusted here?)."
}
$item = Get-Item -LiteralPath $Zip
Write-Host ("    {0}  {1,6:N1} MB  SHA-256 {2}" -f $item.FullName, ($item.Length / 1MB),
            (Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash)
Write-Host "    Users unzip it (right-click > Extract All) and double-click FlangeCutter\FlangeCutter.exe."
if (-not $Signing) {
    Write-Host "    Not signed: Windows SmartScreen may warn users (More info > Run anyway)." -ForegroundColor Yellow
}
Write-Host ("    Total time {0:mm\:ss}" -f $Clock.Elapsed)
exit 0
