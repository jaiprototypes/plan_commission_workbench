param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Venv = Join-Path $Root ".venv-win"
$Python = Join-Path $Venv "Scripts\python.exe"
$ArtifactDir = Join-Path $Root "artifacts"
$ExePath = Join-Path $Root "dist\PlanCommissionWorkbench.exe"
$ZipPath = Join-Path $ArtifactDir "PlanCommissionWorkbench-windows.zip"

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
    --onefile `
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

Remove-Item -Force $ZipPath -ErrorAction SilentlyContinue
Compress-Archive -Path $ExePath, (Join-Path $Root "README.md") -DestinationPath $ZipPath
Write-Host "Built $ZipPath"
