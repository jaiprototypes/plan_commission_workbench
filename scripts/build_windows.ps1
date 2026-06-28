param(
    [switch]$SkipTests,
    [switch]$SkipMsix,
    [string]$Version,
    [string]$Architecture,
    [string]$PackageName,
    [string]$Publisher,
    [string]$PublisherDisplayName,
    [string]$AppInstallerUri,
    [string]$PackageUri,
    [string]$SigningCertificatePath,
    [string]$SigningCertificatePassword,
    [string]$SigningCertificateThumbprint,
    [string]$TimestampUrl,
    [switch]$CreateTestCertificate
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Venv = Join-Path $Root ".venv-win"
$Python = Join-Path $Venv "Scripts\python.exe"
$ArtifactDir = Join-Path $Root "artifacts"
$AppDir = Join-Path $Root "dist\PlanCommissionWorkbench"
$ExePath = Join-Path $AppDir "PlanCommissionWorkbench.exe"
$ZipPath = Join-Path $ArtifactDir "PlanCommissionWorkbench-windows.zip"
$MsixStagingDir = Join-Path $Root "build\msix\PlanCommissionWorkbench"
$MsixManifestTemplate = Join-Path $Root "packaging\windows\AppxManifest.xml.in"
$AppInstallerTemplate = Join-Path $Root "packaging\windows\PlanCommissionWorkbench.appinstaller.in"
$MsixPath = Join-Path $ArtifactDir "PlanCommissionWorkbench.msix"
$AppInstallerPath = Join-Path $ArtifactDir "PlanCommissionWorkbench.appinstaller"

function New-Venv {
    if (Test-Path $Python) {
        return
    }
    try {
        py -3.11 -m venv $Venv
    }
    catch {
        py -3 -m venv $Venv
    }
}

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
        if ([int]$part -gt 65535) {
            throw "MSIX version parts must be 65535 or lower: $InputVersion"
        }
    }
    while ($parts.Count -lt 4) {
        $parts += "0"
    }
    return $parts -join "."
}

function ConvertTo-AbsoluteUri {
    param(
        [string]$Value,
        [string]$FallbackPath
    )

    if (-not [string]::IsNullOrWhiteSpace($Value)) {
        return $Value
    }
    $fullPath = [System.IO.Path]::GetFullPath($FallbackPath)
    return ([System.Uri]$fullPath).AbsoluteUri
}

function Expand-Template {
    param(
        [string]$TemplatePath,
        [string]$OutputPath,
        [hashtable]$Values
    )

    $content = Get-Content -Raw -Path $TemplatePath
    foreach ($key in $Values.Keys) {
        $token = "{{{0}}}" -f $key
        $replacement = [System.Security.SecurityElement]::Escape([string]$Values[$key])
        $content = $content.Replace($token, $replacement)
    }
    if ($content -match '\{\{[A-Z0-9_]+\}\}') {
        throw "Template still contains unresolved token: $($Matches[0])"
    }
    Set-Content -Path $OutputPath -Value $content -Encoding UTF8
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

function New-MsixLogo {
    param(
        [string]$Path,
        [int]$Size
    )

    Add-Type -AssemblyName System.Drawing
    $bitmap = New-Object System.Drawing.Bitmap($Size, $Size)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $background = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(31, 41, 55))
    $accent = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(14, 165, 233))
    try {
        $graphics.FillRectangle($background, 0, 0, $Size, $Size)
        $barHeight = [Math]::Max(4, [int]($Size * 0.18))
        $graphics.FillRectangle($accent, 0, $Size - $barHeight, $Size, $barHeight)
        $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally {
        $accent.Dispose()
        $background.Dispose()
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

function Invoke-MsixSigning {
    param(
        [string]$PackagePath,
        [string]$CertificateSubject
    )

    $certPath = Use-DefaultString $SigningCertificatePath "PCW_SIGNING_CERTIFICATE_PATH" ""
    $certPassword = Use-DefaultString $SigningCertificatePassword "PCW_SIGNING_CERTIFICATE_PASSWORD" ""
    $certThumbprint = Use-DefaultString $SigningCertificateThumbprint "PCW_SIGNING_CERTIFICATE_THUMBPRINT" ""
    $timestamp = Use-DefaultString $TimestampUrl "PCW_TIMESTAMP_URL" ""
    if ([string]::IsNullOrWhiteSpace($certPath) -and [string]::IsNullOrWhiteSpace($certThumbprint) -and -not $CreateTestCertificate) {
        Write-Warning "MSIX package was created but not signed. Provide PCW_SIGNING_CERTIFICATE_PATH, PCW_SIGNING_CERTIFICATE_THUMBPRINT, or -CreateTestCertificate before installing through App Installer."
        return
    }

    $signTool = Find-WindowsSdkTool "SignTool.exe"
    if (-not $signTool) {
        throw "SignTool.exe was not found. Install the Windows SDK or run from a Developer PowerShell prompt."
    }

    $args = @("sign", "/fd", "SHA256")
    if (-not [string]::IsNullOrWhiteSpace($timestamp)) {
        $args += @("/tr", $timestamp, "/td", "SHA256")
    }
    if (-not [string]::IsNullOrWhiteSpace($certPath)) {
        $args += @("/f", $certPath)
        if (-not [string]::IsNullOrWhiteSpace($certPassword)) {
            $args += @("/p", $certPassword)
        }
    }
    elseif (-not [string]::IsNullOrWhiteSpace($certThumbprint)) {
        $args += @("/sha1", $certThumbprint, "/s", "My")
    }
    else {
        if (-not (Get-Command New-SelfSignedCertificate -ErrorAction SilentlyContinue)) {
            throw "New-SelfSignedCertificate is unavailable; provide a PFX or certificate thumbprint instead."
        }
        $cert = New-SelfSignedCertificate `
            -Type CodeSigningCert `
            -Subject $CertificateSubject `
            -CertStoreLocation "Cert:\CurrentUser\My" `
            -KeyExportPolicy Exportable `
            -KeyUsage DigitalSignature `
            -FriendlyName "Plan Commission Workbench MSIX Test Certificate"
        $testCertificatePath = Join-Path $ArtifactDir "PlanCommissionWorkbench-test.cer"
        Export-Certificate -Cert $cert -FilePath $testCertificatePath | Out-Null
        Write-Warning "Created a test signing certificate. Trust $testCertificatePath on the target Windows machine before installing."
        $args += @("/sha1", $cert.Thumbprint, "/s", "My")
    }
    $args += $PackagePath
    & $signTool @args
    if ($LASTEXITCODE -ne 0) {
        throw "SignTool failed with exit code $LASTEXITCODE"
    }
}

function Build-MsixArtifacts {
    $makeAppx = Find-WindowsSdkTool "MakeAppx.exe"
    if (-not $makeAppx) {
        throw "MakeAppx.exe was not found. Install the Windows SDK or run from a Developer PowerShell prompt."
    }

    $resolvedVersion = ConvertTo-MsixVersion (Use-DefaultString $Version "PCW_MSIX_VERSION" (Get-ProjectVersion))
    $resolvedArchitecture = Use-DefaultString $Architecture "PCW_MSIX_ARCHITECTURE" "x64"
    $resolvedPackageName = Use-DefaultString $PackageName "PCW_MSIX_PACKAGE_NAME" "GECG.PlanCommissionWorkbench"
    $resolvedPublisher = Use-DefaultString $Publisher "PCW_MSIX_PUBLISHER" "CN=GECG"
    $resolvedPublisherDisplayName = Use-DefaultString $PublisherDisplayName "PCW_MSIX_PUBLISHER_DISPLAY_NAME" "GECG"
    $resolvedAppInstallerUri = ConvertTo-AbsoluteUri (Use-DefaultString $AppInstallerUri "PCW_APPINSTALLER_URI" "") $AppInstallerPath
    $resolvedPackageUri = ConvertTo-AbsoluteUri (Use-DefaultString $PackageUri "PCW_MSIX_PACKAGE_URI" "") $MsixPath

    Remove-Item -Recurse -Force $MsixStagingDir -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $MsixStagingDir | Out-Null
    Copy-Item -Path (Join-Path $AppDir "*") -Destination $MsixStagingDir -Recurse -Force
    $assetDir = Join-Path $MsixStagingDir "Assets"
    New-Item -ItemType Directory -Force -Path $assetDir | Out-Null
    New-MsixLogo (Join-Path $assetDir "Square44x44Logo.png") 44
    New-MsixLogo (Join-Path $assetDir "Square150x150Logo.png") 150

    Expand-Template $MsixManifestTemplate (Join-Path $MsixStagingDir "AppxManifest.xml") @{
        PACKAGE_NAME = $resolvedPackageName
        PUBLISHER = $resolvedPublisher
        VERSION = $resolvedVersion
        ARCHITECTURE = $resolvedArchitecture
        DISPLAY_NAME = "Plan Commission Workbench"
        PUBLISHER_DISPLAY_NAME = $resolvedPublisherDisplayName
        DESCRIPTION = "Standalone Madison Plan Commission review and export workbench"
    }
    Expand-Template $AppInstallerTemplate $AppInstallerPath @{
        VERSION = $resolvedVersion
        APPINSTALLER_URI = $resolvedAppInstallerUri
        PACKAGE_NAME = $resolvedPackageName
        PUBLISHER = $resolvedPublisher
        ARCHITECTURE = $resolvedArchitecture
        PACKAGE_URI = $resolvedPackageUri
        HOURS_BETWEEN_UPDATE_CHECKS = "0"
        SHOW_UPDATE_PROMPT = "true"
        UPDATE_BLOCKS_ACTIVATION = "false"
        FORCE_UPDATE_FROM_ANY_VERSION = "true"
    }

    Remove-Item -Force $MsixPath -ErrorAction SilentlyContinue
    & $makeAppx pack /d $MsixStagingDir /p $MsixPath /o
    if ($LASTEXITCODE -ne 0) {
        throw "MakeAppx failed with exit code $LASTEXITCODE"
    }
    Invoke-MsixSigning -PackagePath $MsixPath -CertificateSubject $resolvedPublisher
    Write-Host "Built $MsixPath"
    Write-Host "Built $AppInstallerPath"
}

Set-Location $Root
New-Venv

& $Python -m pip install --upgrade pip setuptools wheel
& $Python -m pip install -r requirements.txt
& $Python -m pip install -e ".[test]"
& $Python -m pip install "pyinstaller>=6.0"

if (-not $SkipTests) {
    & $Python -m pytest
}

Remove-Item -Recurse -Force (Join-Path $Root "build") -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $Root "dist") -ErrorAction SilentlyContinue
Remove-Item -Force (Join-Path $Root "PlanCommissionWorkbench.spec") -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $ArtifactDir | Out-Null

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --noconsole `
    --name "PlanCommissionWorkbench" `
    --add-data "plan_commission_workbench\templates;plan_commission_workbench\templates" `
    --add-data "plan_commission_workbench\static;plan_commission_workbench\static" `
    --collect-all "docling" `
    --collect-all "docling_core" `
    --collect-all "docling_parse" `
    --collect-all "pypdfium2" `
    --collect-all "pypdfium2_raw" `
    --collect-all "rapidocr" `
    --collect-all "openai" `
    --hidden-import "plan_commission_workbench.docling_worker" `
    --hidden-import "plan_commission_workbench.run_worker" `
    --hidden-import "plan_commission_workbench.server" `
    --hidden-import "uvicorn.logging" `
    --hidden-import "uvicorn.loops.auto" `
    --hidden-import "uvicorn.protocols.http.auto" `
    --hidden-import "uvicorn.protocols.websockets.auto" `
    --hidden-import "uvicorn.lifespan.on" `
    "plan_commission_workbench\desktop_launcher.py"

if (-not (Test-Path $ExePath)) {
    throw "Expected executable was not created: $ExePath"
}

& $ExePath --self-test-docling

Copy-Item -Force (Join-Path $Root "README.md") (Join-Path $AppDir "README.md")
Remove-Item -Force $ZipPath -ErrorAction SilentlyContinue
Compress-Archive -Path $AppDir -DestinationPath $ZipPath
Write-Host "Built $ZipPath"

if (-not $SkipMsix) {
    Build-MsixArtifacts
}
