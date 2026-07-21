param(
    [string]$ReleaseBaseUrl = "https://github.com/jaiprototypes/plan_commission_workbench/releases/download/pcw-windows-stable",
    [string]$WorkDir
)

$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Restart-AsAdministrator {
    $scriptPath = $PSCommandPath
    if ([string]::IsNullOrWhiteSpace($scriptPath)) {
        throw "Cannot locate this installer script for elevation."
    }

    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -ReleaseBaseUrl `"$ReleaseBaseUrl`""
    if (-not [string]::IsNullOrWhiteSpace($WorkDir)) {
        $arguments += " -WorkDir `"$WorkDir`""
    }
    $process = Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -Verb RunAs -Wait -PassThru
    exit $process.ExitCode
}

function Save-ReleaseAsset {
    param(
        [string]$Name,
        [string]$Destination
    )

    $source = "$ReleaseBaseUrl/$Name"
    Write-Host "Downloading $source"
    Invoke-WebRequest -Uri $source -OutFile $Destination -UseBasicParsing
}

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "Plan Commission Workbench MSIX installation is only supported on Windows."
}

if (-not (Test-IsAdministrator)) {
    Restart-AsAdministrator
}

if ([string]::IsNullOrWhiteSpace($WorkDir)) {
    $WorkDir = Join-Path $env:TEMP "PlanCommissionWorkbenchInstall"
}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

$certificatePath = Join-Path $WorkDir "PlanCommissionWorkbench-signing.cer"
$appInstallerPath = Join-Path $WorkDir "PlanCommissionWorkbench.appinstaller"

Save-ReleaseAsset "PlanCommissionWorkbench-signing.cer" $certificatePath
Save-ReleaseAsset "PlanCommissionWorkbench.appinstaller" $appInstallerPath

Write-Host "Trusting Plan Commission Workbench package certificate..."
Import-Certificate -FilePath $certificatePath -CertStoreLocation "Cert:\LocalMachine\TrustedPeople" | Out-Null

Write-Host "Installing Plan Commission Workbench from App Installer feed..."
Add-AppxPackage -AppInstallerFile $appInstallerPath

Write-Host "Plan Commission Workbench installation completed."
