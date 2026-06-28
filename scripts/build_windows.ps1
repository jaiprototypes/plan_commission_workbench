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
$MsixBuildRoot = if (-not [string]::IsNullOrWhiteSpace($env:PCW_MSIX_STAGING_ROOT)) {
    $env:PCW_MSIX_STAGING_ROOT
}
elseif (-not [string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
    Join-Path $env:RUNNER_TEMP "pcw-msix"
}
elseif (-not [string]::IsNullOrWhiteSpace($env:TEMP)) {
    Join-Path $env:TEMP "pcw-msix"
}
else {
    Join-Path $Root "build\msix"
}
$MsixStagingDir = Join-Path $MsixBuildRoot "PlanCommissionWorkbench"
$MsixMappingPath = Join-Path $MsixBuildRoot "package.map.txt"
$MsixManifestTemplate = Join-Path $Root "packaging\windows\AppxManifest.xml.in"
$AppInstallerTemplate = Join-Path $Root "packaging\windows\PlanCommissionWorkbench.appinstaller.in"
$MsixPath = Join-Path $ArtifactDir "PlanCommissionWorkbench.msix"
$AppInstallerPath = Join-Path $ArtifactDir "PlanCommissionWorkbench.appinstaller"
$TorchSourceDir = ""

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

function Get-PythonModuleDirectory {
    param([string]$ModuleName)

    $script = "import importlib.util, pathlib; spec = importlib.util.find_spec('$ModuleName'); print(pathlib.Path(spec.origin).parent if spec and spec.origin else '')"
    $moduleDirectory = (& $Python -c $script).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($moduleDirectory)) {
        throw "Could not locate Python module directory for $ModuleName"
    }
    return $moduleDirectory
}

function Assert-LastExitCode {
    param([string]$CommandName)

    if ($LASTEXITCODE -ne 0) {
        throw "$CommandName failed with exit code $LASTEXITCODE"
    }
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
        $token = "{{" + $key + "}}"
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

function Get-StableLogoHash {
    param([string]$Value)

    # Purpose: produce repeatable logo variation from the package version.
    $hash = 17
    foreach ($character in ([string]$Value).ToCharArray()) {
        $hash = (($hash * 31) + [int][char]$character) % 100000
    }
    return $hash
}

function New-MsixLogo {
    param(
        [string]$Path,
        [int]$Size,
        [string]$VariationKey
    )

    Add-Type -AssemblyName System.Drawing
    $hash = Get-StableLogoHash $VariationKey
    $red = 238 + ($hash % 15)
    $green = 190 + [int]([Math]::Floor($hash / 7) % 31)
    $blue = 20 + [int]([Math]::Floor($hash / 13) % 30)
    $bitmap = New-Object System.Drawing.Bitmap($Size, $Size)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $background = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(31, 41, 55))
    $shadow = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(70, 0, 0, 0))
    $accent = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb($red, $green, $blue))
    try {
        $graphics.FillRectangle($background, 0, 0, $Size, $Size)
        $center = [double]$Size / 2
        $outerRadius = [double]$Size * 0.36
        $innerRadius = $outerRadius * (0.42 + (($hash % 9) * 0.01))
        $rotationRadians = (($hash % 13) - 6) * [Math]::PI / 180
        $points = @()
        for ($index = 0; $index -lt 10; $index++) {
            $radius = if ($index % 2 -eq 0) { $outerRadius } else { $innerRadius }
            $angle = -[Math]::PI / 2 + $rotationRadians + $index * [Math]::PI / 5
            $points += [System.Drawing.PointF]::new(
                [single]($center + [Math]::Cos($angle) * $radius),
                [single]($center + [Math]::Sin($angle) * $radius)
            )
        }
        $shadowOffset = [Math]::Max(1, [int]($Size * 0.035))
        $shadowPoints = foreach ($point in $points) {
            [System.Drawing.PointF]::new($point.X + $shadowOffset, $point.Y + $shadowOffset)
        }
        $graphics.FillPolygon($shadow, [System.Drawing.PointF[]]$shadowPoints)
        $graphics.FillPolygon($accent, [System.Drawing.PointF[]]$points)
        $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally {
        $accent.Dispose()
        $shadow.Dispose()
        $background.Dispose()
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

function Assert-MsixPayloadPath {
    param([string]$RelativePath)

    # Purpose: fail before MakeAppx when a frozen dependency creates an invalid MSIX payload path.
    if ($RelativePath -match '[<>:"|?*\[\]]') {
        throw "MSIX payload path contains an invalid character: $RelativePath"
    }
    foreach ($segment in $RelativePath.Split("\")) {
        if ([string]::IsNullOrWhiteSpace($segment)) {
            throw "MSIX payload path contains an empty segment: $RelativePath"
        }
        if ($segment.EndsWith(".") -or $segment.EndsWith(" ")) {
            throw "MSIX payload path segment ends with a dot or space: $RelativePath"
        }
    }
    if ($RelativePath.Length -gt 240) {
        throw "MSIX payload path is too long for reliable MakeAppx packaging: $RelativePath"
    }
}

function New-MsixMappingFile {
    param(
        [string]$SourceDirectory,
        [string]$MappingPath
    )

    $sourceRoot = [System.IO.Path]::GetFullPath($SourceDirectory).TrimEnd("\")
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("[Files]")
    foreach ($file in Get-ChildItem -Path $sourceRoot -File -Recurse | Sort-Object FullName) {
        $sourcePath = [System.IO.Path]::GetFullPath($file.FullName)
        $relativePath = $sourcePath.Substring($sourceRoot.Length + 1)
        Assert-MsixPayloadPath $relativePath
        $mappingLine = '"{0}" "{1}"' -f $sourcePath, $relativePath
        $lines.Add($mappingLine)
    }
    Set-Content -Path $MappingPath -Value $lines -Encoding UTF8
}

function Get-PayloadFileCount {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return 0
    }
    return @(Get-ChildItem -Path $Path -File -Recurse -Force).Count
}

function Remove-MsixPayloadPath {
    param(
        [string]$RootDirectory,
        [string]$RelativePath
    )

    $target = Join-Path $RootDirectory $RelativePath
    if (-not (Test-Path $target)) {
        return
    }
    $removedFileCount = Get-PayloadFileCount $target
    Remove-Item -Recurse -Force $target
    Write-Host "Removed $removedFileCount MSIX staging files from $RelativePath"
}

function Optimize-MsixPayload {
    param([string]$SourceDirectory)

    $internalDir = Join-Path $SourceDirectory "_internal"
    if (-not (Test-Path $internalDir)) {
        return
    }

    # Purpose: keep ML development/codegen payload out of MSIX while preserving runtime DLLs.
    foreach ($relativePath in @(
        "docx\templates\default-docx-template",
        "functorch",
        "torchgen",
        "triton",
        "torch\_dynamo",
        "torch\_functorch",
        "torch\_inductor",
        "torch\ao",
        "torch\distributed",
        "torch\include",
        "torch\onnx",
        "torch\profiler",
        "torch\share",
        "torch\testing",
        "torch\test",
        "torch\utils\benchmark",
        "torch\utils\bottleneck",
        "torch\utils\tensorboard"
    )) {
        Remove-MsixPayloadPath $internalDir $relativePath
    }
    Remove-MsixTorchSourcePayload $internalDir
    Restore-TorchConfigSources $internalDir
    Remove-MsixPurePythonSourcePayload $internalDir @("openai")

    $fileCount = Get-PayloadFileCount $SourceDirectory
    Write-Host "MSIX staging payload contains $fileCount files after pruning"
}

function Remove-MsixTorchSourcePayload {
    param([string]$InternalDirectory)

    $torchDir = Join-Path $InternalDirectory "torch"
    if (-not (Test-Path $torchDir)) {
        return
    }

    $sourceExtensions = @(
        ".c",
        ".cc",
        ".cpp",
        ".cu",
        ".cuh",
        ".h",
        ".hpp",
        ".jinja",
        ".md",
        ".py",
        ".pyi",
        ".pyx",
        ".pxd",
        ".rst"
    )
    $removedCount = 0
    foreach ($file in Get-ChildItem -Path $torchDir -File -Recurse -Force) {
        if ($sourceExtensions -notcontains $file.Extension.ToLowerInvariant()) {
            continue
        }
        Remove-Item -Force $file.FullName
        $removedCount += 1
    }
    Write-Host "Removed $removedCount MSIX staging source files from torch"
}

function Restore-TorchConfigSources {
    param([string]$InternalDirectory)

    if ([string]::IsNullOrWhiteSpace($TorchSourceDir) -or -not (Test-Path $TorchSourceDir)) {
        throw "Torch source directory was not resolved before MSIX staging"
    }
    foreach ($relativePath in Get-TorchConfigSourcePaths) {
        $sourcePath = Join-Path $TorchSourceDir $relativePath
        if (-not (Test-Path $sourcePath)) {
            continue
        }
        $destinationPath = Join-Path (Join-Path $InternalDirectory "torch") $relativePath
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destinationPath) | Out-Null
        Copy-Item -Force $sourcePath $destinationPath
    }
}

function Get-TorchConfigSourcePaths {
    return @(
        "utils\_config_module.py",
        "_dynamo\config.py",
        "_export\config.py",
        "_functorch\config.py",
        "_inductor\config.py",
        "_inductor\config_comms.py",
        "compiler\config.py",
        "distributed\config.py",
        "fx\experimental\_config.py",
        "utils\serialization\config.py"
    )
}

function Remove-MsixPurePythonSourcePayload {
    param(
        [string]$InternalDirectory,
        [string[]]$PackageNames
    )

    $sourceExtensions = @(".md", ".py", ".pyi", ".rst")
    foreach ($packageName in $PackageNames) {
        $packageDir = Join-Path $InternalDirectory $packageName
        if (-not (Test-Path $packageDir)) {
            continue
        }
        $removedCount = 0
        foreach ($file in Get-ChildItem -Path $packageDir -File -Recurse -Force) {
            if ($sourceExtensions -notcontains $file.Extension.ToLowerInvariant()) {
                continue
            }
            Remove-Item -Force $file.FullName
            $removedCount += 1
        }
        Write-Host "Removed $removedCount MSIX staging pure-Python source files from $packageName"
    }
}

function Invoke-ExecutableSelfTest {
    param(
        [string]$ExecutablePath,
        [string]$Argument,
        [string]$Name
    )

    # Purpose: wait on the windowed PyInstaller executable and read its real exit code.
    $process = Start-Process -FilePath $ExecutablePath -ArgumentList $Argument -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "$Name failed with exit code $($process.ExitCode)"
    }
}

function Test-StagedExecutable {
    param([string]$SourceDirectory)

    $stagedExe = Join-Path $SourceDirectory "PlanCommissionWorkbench.exe"
    if (-not (Test-Path $stagedExe)) {
        throw "Expected staged executable was not created: $stagedExe"
    }
    Invoke-ExecutableSelfTest $stagedExe "--self-test-docling" "Staged MSIX executable self-test"
    Invoke-ExecutableSelfTest $stagedExe "--self-test-runtime-imports" "Staged MSIX runtime import self-test"
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
    Remove-Item -Force $MsixMappingPath -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $MsixStagingDir | Out-Null
    Copy-Item -Path (Join-Path $AppDir "*") -Destination $MsixStagingDir -Recurse -Force
    $assetDir = Join-Path $MsixStagingDir "Assets"
    New-Item -ItemType Directory -Force -Path $assetDir | Out-Null
    New-MsixLogo (Join-Path $assetDir "Square44x44Logo.png") 44 $resolvedVersion
    New-MsixLogo (Join-Path $assetDir "Square150x150Logo.png") 150 $resolvedVersion
    Optimize-MsixPayload $MsixStagingDir
    Test-StagedExecutable $MsixStagingDir

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
    New-MsixMappingFile $MsixStagingDir $MsixMappingPath
    & $makeAppx pack /f $MsixMappingPath /p $MsixPath /o
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
Assert-LastExitCode "pip bootstrap install"
& $Python -m pip install -r requirements.txt
Assert-LastExitCode "requirements install"
& $Python -m pip install -e ".[test]"
Assert-LastExitCode "editable test install"
& $Python -m pip install "pyinstaller>=6.0"
Assert-LastExitCode "PyInstaller install"
$TorchSourceDir = Get-PythonModuleDirectory "torch"

if (-not $SkipTests) {
    & $Python -m pytest
    Assert-LastExitCode "pytest"
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
    --add-data "$TorchSourceDir\utils\_config_module.py;torch\utils" `
    --add-data "$TorchSourceDir\_dynamo\config.py;torch\_dynamo" `
    --add-data "$TorchSourceDir\_export\config.py;torch\_export" `
    --add-data "$TorchSourceDir\_functorch\config.py;torch\_functorch" `
    --add-data "$TorchSourceDir\_inductor\config.py;torch\_inductor" `
    --add-data "$TorchSourceDir\_inductor\config_comms.py;torch\_inductor" `
    --add-data "$TorchSourceDir\compiler\config.py;torch\compiler" `
    --add-data "$TorchSourceDir\distributed\config.py;torch\distributed" `
    --add-data "$TorchSourceDir\fx\experimental\_config.py;torch\fx\experimental" `
    --add-data "$TorchSourceDir\utils\serialization\config.py;torch\utils\serialization" `
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
Assert-LastExitCode "PyInstaller"

if (-not (Test-Path $ExePath)) {
    throw "Expected executable was not created: $ExePath"
}

Invoke-ExecutableSelfTest $ExePath "--self-test-docling" "Windows executable Docling self-test"
Invoke-ExecutableSelfTest $ExePath "--self-test-runtime-imports" "Windows executable runtime import self-test"

Copy-Item -Force (Join-Path $Root "README.md") (Join-Path $AppDir "README.md")
Remove-Item -Force $ZipPath -ErrorAction SilentlyContinue
Compress-Archive -Path $AppDir -DestinationPath $ZipPath
Write-Host "Built $ZipPath"

if (-not $SkipMsix) {
    Build-MsixArtifacts
}
