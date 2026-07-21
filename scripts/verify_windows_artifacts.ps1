param(
    [string]$ArtifactDir,
    [string]$PackageName,
    [string]$Publisher,
    [string]$Version,
    [string]$Architecture,
    [switch]$RequireTrustedSignature
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$VerifyDir = Join-Path $Root "build\verify-msix"

function Use-DefaultString {
    param(
        [string]$Value,
        [string]$EnvironmentName,
        [string]$DefaultValue
    )

    if (-not [string]::IsNullOrWhiteSpace($Value)) {
        return $Value
    }
    $environmentValue = [Environment]::GetEnvironmentVariable($EnvironmentName)
    if (-not [string]::IsNullOrWhiteSpace($environmentValue)) {
        return $environmentValue
    }
    return $DefaultValue
}

function Get-ProjectVersion {
    $pyproject = Get-Content -Raw -Path (Join-Path $Root "pyproject.toml")
    if ($pyproject -notmatch '(?m)^version\s*=\s*"([^"]+)"') {
        throw "Could not read project version from pyproject.toml"
    }
    return $Matches[1]
}

function ConvertTo-MsixVersion {
    param([string]$InputVersion)

    $parts = @($InputVersion.Split("."))
    if ($parts.Count -gt 4) {
        throw "MSIX versions must use at most four numeric parts: $InputVersion"
    }
    foreach ($part in $parts) {
        if ($part -notmatch '^\d+$') {
            throw "MSIX version parts must be numeric: $InputVersion"
        }
    }
    while ($parts.Count -lt 4) {
        $parts += "0"
    }
    return $parts -join "."
}

function Find-WindowsSdkTool {
    param([string]$Name)

    $roots = @()
    if (${env:ProgramFiles(x86)}) {
        $roots += Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    }
    if ($env:ProgramFiles) {
        $roots += Join-Path $env:ProgramFiles "Windows Kits\10\bin"
    }
    foreach ($rootPath in $roots) {
        if (-not (Test-Path $rootPath)) {
            continue
        }
        $versions = Get-ChildItem -Path $rootPath -Directory | Sort-Object Name -Descending
        foreach ($versionDir in $versions) {
            foreach ($toolPath in @(
                (Join-Path $versionDir.FullName "x64\$Name"),
                (Join-Path $versionDir.FullName "x86\$Name")
            )) {
                if (Test-Path $toolPath) {
                    return $toolPath
                }
            }
        }
    }
    return $null
}

function Assert-FileExists {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        throw "Expected artifact is missing: $Path"
    }
}

function Assert-Equal {
    param(
        [string]$Name,
        [string]$Expected,
        [string]$Actual
    )

    if ($Expected -ne $Actual) {
        throw "$Name mismatch. Expected '$Expected'; got '$Actual'"
    }
}

function Assert-AbsoluteUri {
    param(
        [string]$Name,
        [string]$Value
    )

    $uri = $null
    if (-not [System.Uri]::TryCreate($Value, [System.UriKind]::Absolute, [ref]$uri)) {
        throw "$Name must be an absolute URI: $Value"
    }
}

function Read-XmlDocument {
    param([string]$Path)

    [xml]$document = Get-Content -Raw -Path $Path
    return $document
}

function Select-XmlNode {
    param(
        [xml]$Document,
        [string]$NamespacePrefix,
        [string]$NamespaceUri,
        [string]$XPath
    )

    $manager = New-Object System.Xml.XmlNamespaceManager($Document.NameTable)
    $manager.AddNamespace($NamespacePrefix, $NamespaceUri)
    $node = $Document.SelectSingleNode($XPath, $manager)
    if (-not $node) {
        throw "Missing XML node '$XPath' in $($Document.BaseURI)"
    }
    return $node
}

function Verify-MsixSignature {
    param([string]$PackagePath)

    $signature = Get-AuthenticodeSignature -FilePath $PackagePath
    if (-not $signature.SignerCertificate) {
        throw "MSIX is not signed: $PackagePath"
    }
    if ($signature.Status -eq "HashMismatch") {
        throw "MSIX signature hash mismatch: $($signature.StatusMessage)"
    }
    if ($signature.Status -eq "NotSigned") {
        throw "MSIX is not signed: $PackagePath"
    }
    if (-not $RequireTrustedSignature) {
        Write-Host "MSIX signature present: $($signature.Status)"
        return
    }
    $signTool = Find-WindowsSdkTool "SignTool.exe"
    if (-not $signTool) {
        throw "SignTool.exe was not found for trusted signature verification."
    }
    & $signTool verify /pa /v $PackagePath
    if ($LASTEXITCODE -ne 0) {
        throw "SignTool trusted verification failed with exit code $LASTEXITCODE"
    }
}

$resolvedArtifactDir = Use-DefaultString $ArtifactDir "PCW_ARTIFACT_DIR" (Join-Path $Root "artifacts")
$resolvedPackageName = Use-DefaultString $PackageName "PCW_MSIX_PACKAGE_NAME" "GECG.PlanCommissionWorkbench"
$resolvedPublisher = Use-DefaultString $Publisher "PCW_MSIX_PUBLISHER" "CN=GECG"
$resolvedVersion = ConvertTo-MsixVersion (Use-DefaultString $Version "PCW_MSIX_VERSION" (Get-ProjectVersion))
$resolvedArchitecture = Use-DefaultString $Architecture "PCW_MSIX_ARCHITECTURE" "x64"

$zipPath = Join-Path $resolvedArtifactDir "PlanCommissionWorkbench-windows.zip"
$msixPath = Join-Path $resolvedArtifactDir "PlanCommissionWorkbench.msix"
$appInstallerPath = Join-Path $resolvedArtifactDir "PlanCommissionWorkbench.appinstaller"

Assert-FileExists $zipPath
Assert-FileExists $msixPath
Assert-FileExists $appInstallerPath
Verify-MsixSignature $msixPath

$makeAppx = Find-WindowsSdkTool "MakeAppx.exe"
if (-not $makeAppx) {
    throw "MakeAppx.exe was not found. Install the Windows SDK or run from a Developer PowerShell prompt."
}

Remove-Item -Recurse -Force $VerifyDir -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $VerifyDir | Out-Null
& $makeAppx unpack /p $msixPath /d $VerifyDir /o
if ($LASTEXITCODE -ne 0) {
    throw "MakeAppx unpack failed with exit code $LASTEXITCODE"
}

$manifestPath = Join-Path $VerifyDir "AppxManifest.xml"
$exePath = Join-Path $VerifyDir "PlanCommissionWorkbench.exe"
Assert-FileExists $manifestPath
Assert-FileExists $exePath

$manifest = Read-XmlDocument $manifestPath
$identity = Select-XmlNode `
    -Document $manifest `
    -NamespacePrefix "pkg" `
    -NamespaceUri "http://schemas.microsoft.com/appx/manifest/foundation/windows10" `
    -XPath "/pkg:Package/pkg:Identity"
$application = Select-XmlNode `
    -Document $manifest `
    -NamespacePrefix "pkg" `
    -NamespaceUri "http://schemas.microsoft.com/appx/manifest/foundation/windows10" `
    -XPath "/pkg:Package/pkg:Applications/pkg:Application"

Assert-Equal "Manifest package name" $resolvedPackageName ($identity.GetAttribute("Name"))
Assert-Equal "Manifest publisher" $resolvedPublisher ($identity.GetAttribute("Publisher"))
Assert-Equal "Manifest version" $resolvedVersion ($identity.GetAttribute("Version"))
Assert-Equal "Manifest architecture" $resolvedArchitecture ($identity.GetAttribute("ProcessorArchitecture"))
Assert-Equal "Manifest executable" "PlanCommissionWorkbench.exe" ($application.GetAttribute("Executable"))

$appInstaller = Read-XmlDocument $appInstallerPath
$mainPackage = Select-XmlNode `
    -Document $appInstaller `
    -NamespacePrefix "ai" `
    -NamespaceUri "http://schemas.microsoft.com/appx/appinstaller/2021" `
    -XPath "/ai:AppInstaller/ai:MainPackage"
$onLaunch = Select-XmlNode `
    -Document $appInstaller `
    -NamespacePrefix "ai" `
    -NamespaceUri "http://schemas.microsoft.com/appx/appinstaller/2021" `
    -XPath "/ai:AppInstaller/ai:UpdateSettings/ai:OnLaunch"
$forceUpdate = Select-XmlNode `
    -Document $appInstaller `
    -NamespacePrefix "ai" `
    -NamespaceUri "http://schemas.microsoft.com/appx/appinstaller/2021" `
    -XPath "/ai:AppInstaller/ai:UpdateSettings/ai:ForceUpdateFromAnyVersion"

$appInstallerRoot = $appInstaller.DocumentElement
Assert-Equal "AppInstaller version" $resolvedVersion ($appInstallerRoot.GetAttribute("Version"))
Assert-Equal "AppInstaller package name" $resolvedPackageName ($mainPackage.GetAttribute("Name"))
Assert-Equal "AppInstaller publisher" $resolvedPublisher ($mainPackage.GetAttribute("Publisher"))
Assert-Equal "AppInstaller version" $resolvedVersion ($mainPackage.GetAttribute("Version"))
Assert-Equal "AppInstaller architecture" $resolvedArchitecture ($mainPackage.GetAttribute("ProcessorArchitecture"))
Assert-AbsoluteUri "AppInstaller URI" ($appInstallerRoot.GetAttribute("Uri"))
Assert-AbsoluteUri "MainPackage URI" ($mainPackage.GetAttribute("Uri"))

$expectedAppInstallerUri = [Environment]::GetEnvironmentVariable("PCW_APPINSTALLER_URI")
if (-not [string]::IsNullOrWhiteSpace($expectedAppInstallerUri)) {
    Assert-Equal "Configured AppInstaller URI" $expectedAppInstallerUri ($appInstallerRoot.GetAttribute("Uri"))
}
$expectedPackageUri = [Environment]::GetEnvironmentVariable("PCW_MSIX_PACKAGE_URI")
if (-not [string]::IsNullOrWhiteSpace($expectedPackageUri)) {
    Assert-Equal "Configured MSIX package URI" $expectedPackageUri ($mainPackage.GetAttribute("Uri"))
}

if (-not $onLaunch.GetAttribute("HoursBetweenUpdateChecks")) {
    throw "AppInstaller update settings must include OnLaunch HoursBetweenUpdateChecks."
}
if ($forceUpdate.InnerText -ne "true") {
    throw "AppInstaller rollback support requires ForceUpdateFromAnyVersion=true."
}

Write-Host "Verified Windows artifacts in $resolvedArtifactDir"
