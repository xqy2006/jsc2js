param(
    [Parameter(Mandatory = $true)]
    [string]$VersionsJson
)

$ErrorActionPreference = "Stop"
$requiredVersion = [version]"15.3.25"
$sdkVersion = "10.0.28000.0"
$installerUrl = "https://go.microsoft.com/fwlink/?linkid=2372508"
$sdkRoot = "${env:ProgramFiles(x86)}\Windows Kits\10"
$header = Join-Path $sdkRoot "Include\$sdkVersion\um\Windows.h"
$library = Join-Path $sdkRoot "Lib\$sdkVersion\um\x64\User32.Lib"

try {
    $versions = @($VersionsJson | ConvertFrom-Json -ErrorAction Stop)
} catch {
    throw "VersionsJson is not a valid JSON array: $VersionsJson"
}

$needsSdk = $false
foreach ($item in $versions) {
    try {
        if ([version]([string]$item) -ge $requiredVersion) {
            $needsSdk = $true
            break
        }
    } catch {
        throw "Unsupported V8 version in VersionsJson: $item"
    }
}

if (-not $needsSdk) {
    Write-Output "The assigned V8 tags do not require Windows SDK $sdkVersion"
    exit 0
}
if ((Test-Path $header) -and (Test-Path $library)) {
    Write-Output "Windows SDK $sdkVersion is already installed"
    exit 0
}

$installer = Join-Path $env:RUNNER_TEMP "winsdksetup-28000.exe"
Write-Output "Downloading the pinned Microsoft Windows SDK installer"
Invoke-WebRequest -Uri $installerUrl -OutFile $installer
$signature = Get-AuthenticodeSignature -FilePath $installer
if (
    $signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
    $signature.SignerCertificate.Subject -notmatch "O=Microsoft Corporation"
) {
    throw "Windows SDK installer signature is not valid Microsoft code: $($signature.Status)"
}

$arguments = @(
    "/features",
    "OptionId.DesktopCPPx64",
    "OptionId.DesktopCPPx86",
    "/quiet",
    "/norestart"
)
$process = Start-Process `
    -FilePath $installer `
    -ArgumentList $arguments `
    -Wait `
    -PassThru `
    -WindowStyle Hidden
if ($process.ExitCode -notin @(0, 3010)) {
    throw "Windows SDK installation failed: $($process.ExitCode)"
}
if (-not (Test-Path $header)) {
    throw "Windows SDK installation did not provide $header"
}
if (-not (Test-Path $library)) {
    throw "Windows SDK installation did not provide $library"
}
Write-Output "Installed the exact Windows SDK family $sdkVersion"
