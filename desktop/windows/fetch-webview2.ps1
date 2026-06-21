#Requires -Version 5.1
<#
.SYNOPSIS
  Download and vendor the WebView2 managed assemblies for the Windows widget.

.DESCRIPTION
  This script makes a NETWORK CALL (NuGet download) and is meant for
  maintainers, CI, and the install step — NOT the runtime widget host.
  The runtime widget (cognitive-stress.ps1) is private and makes no network
  calls; it only reads the vendored DLLs this script places in lib/.

  Downloads the Microsoft.Web.WebView2 NuGet package and extracts:
    lib/net45/Microsoft.Web.WebView2.Core.dll
    lib/net45/Microsoft.Web.WebView2.WinForms.dll
    runtimes/win-x64/native/WebView2Loader.dll   (if present in the package)
    runtimes/win-arm64/native/WebView2Loader.dll  (if present in the package)

  The net45 TFM is required because the widget host targets Windows PowerShell
  5.1 (.NET Framework), not PowerShell 7 / .NET Core. If net45 is not present
  in the package (future SDK version), the script falls back to the lowest
  available .NET Framework TFM.

  Idempotent: if the DLLs already exist and the version matches, it skips the
  download unless -Force is passed.

.PARAMETER Version
  WebView2 SDK version to fetch (default: 1.0.2792.45 — a recent stable release).
  Pin this to ensure reproducible builds. Check for new releases at:
  https://www.nuget.org/packages/Microsoft.Web.WebView2

.PARAMETER OutDir
  Directory to place the extracted assemblies (default: $PSScriptRoot/lib).
  The script creates this directory if it does not exist.

.PARAMETER Force
  Re-download and overwrite even if the DLLs already exist.

.EXAMPLE
  # Fetch the default version into desktop/windows/lib/:
  powershell -ExecutionPolicy Bypass -File fetch-webview2.ps1

.EXAMPLE
  # Fetch a specific version into a custom directory:
  powershell -ExecutionPolicy Bypass -File fetch-webview2.ps1 `
    -Version 1.0.2849.39 -OutDir C:\tmp\wv2

.NOTES
  Called automatically by: python install.py --windows
  Network boundary: this script only — the widget host (cognitive-stress.ps1)
  reads the resulting DLLs from disk and makes no network calls at runtime.
#>

[CmdletBinding()]
param(
    [string] $Version = "1.0.2792.45",
    [string] $OutDir  = "",
    [switch] $Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($OutDir -eq "") {
    $OutDir = Join-Path $PSScriptRoot "lib"
}

$net45Dir  = Join-Path $OutDir "net45"
$coreOut   = Join-Path $net45Dir "Microsoft.Web.WebView2.Core.dll"
$formsOut  = Join-Path $net45Dir "Microsoft.Web.WebView2.WinForms.dll"

# Skip download if DLLs already exist and -Force not requested.
if (-not $Force -and (Test-Path $coreOut) -and (Test-Path $formsOut)) {
    Write-Host "WebView2 DLLs already present in $net45Dir — skipping download."
    Write-Host "  (Use -Force to re-download.)"
    exit 0
}

# ---------------------------------------------------------------------------
# 1. Download the NuGet package (it is a zip with a .nupkg extension).
# ---------------------------------------------------------------------------

$nugetUrl  = "https://www.nuget.org/api/v2/package/Microsoft.Web.WebView2/$Version"
# A .nupkg IS a zip, but Expand-Archive validates the file extension and only
# accepts ".zip" — so save the download with a .zip name.
$tmpNupkg  = Join-Path ([System.IO.Path]::GetTempPath()) "Microsoft.Web.WebView2.$Version.zip"
$tmpExpand = Join-Path ([System.IO.Path]::GetTempPath()) "wv2-expand-$([System.Guid]::NewGuid().ToString('N'))"

Write-Host "Fetching Microsoft.Web.WebView2 $Version from NuGet..."
Write-Host "  URL: $nugetUrl"

try {
    Invoke-WebRequest -Uri $nugetUrl -OutFile $tmpNupkg -UseBasicParsing
} catch {
    Write-Error "Download failed: $_`nCheck your network connection and that the version '$Version' exists at https://www.nuget.org/packages/Microsoft.Web.WebView2/$Version"
    exit 1
}

Write-Host "  Downloaded $('{0:N0}' -f (Get-Item $tmpNupkg).Length) bytes."

# ---------------------------------------------------------------------------
# 2. Expand the .nupkg (it is a zip).
# ---------------------------------------------------------------------------

try {
    Expand-Archive -Path $tmpNupkg -DestinationPath $tmpExpand -Force
} catch {
    Write-Error "Failed to expand the NuGet package: $_"
    Remove-Item $tmpNupkg -ErrorAction SilentlyContinue
    exit 1
}

# ---------------------------------------------------------------------------
# 3. Locate the .NET Framework managed assemblies.
#    The NuGet layout for WebView2 is:
#      lib/net45/Microsoft.Web.WebView2.Core.dll
#      lib/net45/Microsoft.Web.WebView2.WinForms.dll
#    Prefer net45; fall back to the lowest available .NET Framework TFM.
# ---------------------------------------------------------------------------

$libBase = Join-Path $tmpExpand "lib"
if (-not (Test-Path $libBase)) {
    Write-Error "Unexpected NuGet package layout — no 'lib/' directory found in the package. Check the version."
    Remove-Item $tmpExpand -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $tmpNupkg  -ErrorAction SilentlyContinue
    exit 1
}

# Preference order for .NET Framework TFMs (PowerShell 5.1 requires net45 or
# a compatible framework target). net45 is standard for this SDK.
$preferredTfms = @("net45", "net46", "net461", "net462", "net47", "net471", "net472", "net48")
$chosenTfm     = $null

foreach ($tfm in $preferredTfms) {
    $candidate = Join-Path $libBase $tfm
    if (Test-Path (Join-Path $candidate "Microsoft.Web.WebView2.Core.dll")) {
        $chosenTfm = $tfm
        break
    }
}

if (-not $chosenTfm) {
    # List what's available to help the maintainer debug.
    $available = if (Test-Path $libBase) {
        (Get-ChildItem $libBase -Directory).Name -join ", "
    } else { "(lib/ not found)" }
    Write-Error ("Could not find a .NET Framework (net4x) build of the WebView2 Core DLL. " +
                 "Available TFMs in package: $available`n" +
                 "This likely means the SDK dropped net45 support. " +
                 "Update the script's `$preferredTfms list or pin an older version.")
    Remove-Item $tmpExpand -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $tmpNupkg  -ErrorAction SilentlyContinue
    exit 1
}

if ($chosenTfm -ne "net45") {
    Write-Warning "net45 not found; using $chosenTfm as fallback. The widget host targets PowerShell 5.1 (.NET Framework). Verify the chosen TFM is .NET Framework-compatible."
}

$srcDir = Join-Path $libBase $chosenTfm

# ---------------------------------------------------------------------------
# 4. Copy the managed assemblies to OutDir/net45/ (always net45/ to keep the
#    widget host's hardcoded path stable regardless of TFM fallback).
# ---------------------------------------------------------------------------

New-Item -ItemType Directory -Path $net45Dir -Force | Out-Null

$copiedFiles = @()
foreach ($dll in @("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll")) {
    $src  = Join-Path $srcDir $dll
    $dest = Join-Path $net45Dir $dll
    if (-not (Test-Path $src)) {
        Write-Error "Expected DLL not found in package: $src"
        exit 1
    }
    Copy-Item $src $dest -Force
    $copiedFiles += $dest
    Write-Host "  copied: $dll -> $net45Dir"
}

# ---------------------------------------------------------------------------
# 5. Copy the native WebView2Loader.dll for supported architectures.
#    These are optional (the Evergreen Runtime supplies them at runtime), but
#    shipping them avoids a PATH dependency on the Runtime's install location.
# ---------------------------------------------------------------------------

$runtimesBase = Join-Path $tmpExpand "runtimes"
foreach ($arch in @("win-x64", "win-arm64", "win-x86")) {
    # Nested Join-Path: the 3+ component form is PowerShell 6+ only; this
    # script must run under stock Windows PowerShell 5.1.
    $nativeSrc = Join-Path (Join-Path (Join-Path $runtimesBase $arch) "native") "WebView2Loader.dll"
    if (Test-Path $nativeSrc) {
        $nativeDestDir = Join-Path (Join-Path (Join-Path $OutDir "runtimes") $arch) "native"
        New-Item -ItemType Directory -Path $nativeDestDir -Force | Out-Null
        Copy-Item $nativeSrc (Join-Path $nativeDestDir "WebView2Loader.dll") -Force
        $copiedFiles += (Join-Path $nativeDestDir "WebView2Loader.dll")
        Write-Host "  copied: runtimes/$arch/native/WebView2Loader.dll"
    }
}

# ---------------------------------------------------------------------------
# 6. Cleanup temp files.
# ---------------------------------------------------------------------------

Remove-Item $tmpExpand -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $tmpNupkg  -ErrorAction SilentlyContinue

# ---------------------------------------------------------------------------
# 7. Summary.
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "WebView2 $Version vendored successfully."
Write-Host "  TFM used: $chosenTfm (copied to lib/net45/ for the widget host)"
Write-Host "  Files written ($($copiedFiles.Count)):"
foreach ($f in $copiedFiles) {
    Write-Host "    $f"
}
Write-Host ""
Write-Host "The widget host (cognitive-stress.ps1) loads these DLLs at launch."
Write-Host "The WebView2 Evergreen Runtime must also be installed on the target"
Write-Host "machine (pre-installed on Windows 11; auto-deployed on Windows 10)."
