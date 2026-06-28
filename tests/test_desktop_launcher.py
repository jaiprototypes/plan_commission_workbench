from __future__ import annotations

import runpy
from pathlib import Path
import xml.etree.ElementTree as ET

from plan_commission_workbench.desktop_launcher import (
    SMOKE_TEST_TEXT,
    default_data_dir,
    desktop_log_paths,
    recent_error_summary,
    run_runtime_import_self_test,
    smoke_test_pdf_bytes,
)


ROOT = Path(__file__).resolve().parents[1]


def render_template(path: Path, values: dict[str, str]) -> str:
    """Purpose: render MSIX templates enough for XML structure tests."""

    content = path.read_text(encoding="utf-8")
    for key, value in values.items():
        content = content.replace(f"{{{{{key}}}}}", value)
    return content


def msix_template_values() -> dict[str, str]:
    """Purpose: keep manifest and appinstaller test identities aligned."""

    return {
        "PACKAGE_NAME": "GECG.PlanCommissionWorkbench",
        "PUBLISHER": "CN=GECG",
        "VERSION": "1.2.3.0",
        "ARCHITECTURE": "x64",
        "DISPLAY_NAME": "Plan Commission Workbench",
        "PUBLISHER_DISPLAY_NAME": "GECG",
        "DESCRIPTION": "Standalone Madison Plan Commission review and export workbench",
        "APPINSTALLER_URI": "https://example.com/PlanCommissionWorkbench.appinstaller",
        "PACKAGE_URI": "https://example.com/PlanCommissionWorkbench.msix",
        "HOURS_BETWEEN_UPDATE_CHECKS": "0",
        "SHOW_UPDATE_PROMPT": "true",
        "UPDATE_BLOCKS_ACTIVATION": "false",
        "FORCE_UPDATE_FROM_ANY_VERSION": "true",
    }


def test_default_data_dir_uses_local_app_data(monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Tester\AppData\Local")

    assert default_data_dir() == Path(r"C:\Users\Tester\AppData\Local") / "PlanCommissionWorkbench" / "data"


def test_desktop_logs_use_local_app_data(monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Tester\AppData\Local")

    log_path, error_path = desktop_log_paths()

    data_dir = Path(r"C:\Users\Tester\AppData\Local") / "PlanCommissionWorkbench" / "data"
    assert log_path == data_dir / "server.log"
    assert error_path == data_dir / "server.err.log"


def test_recent_error_summary_tails_file(tmp_path) -> None:
    error_path = tmp_path / "server.err.log"
    error_path.write_text("\n".join(f"line {number}" for number in range(20)), encoding="utf-8")

    assert recent_error_summary(error_path, line_count=3) == "line 17\nline 18\nline 19"


def test_smoke_test_pdf_bytes_are_valid_pdf_shaped() -> None:
    payload = smoke_test_pdf_bytes()

    assert payload.startswith(b"%PDF-")
    assert b"%%EOF" in payload[-32:]
    assert SMOKE_TEST_TEXT.encode("ascii") in payload


def test_runtime_import_self_test_loads_lazy_production_dependencies() -> None:
    assert run_runtime_import_self_test() == 0


def test_launcher_file_imports_as_top_level_script() -> None:
    path = ROOT / "plan_commission_workbench" / "desktop_launcher.py"

    namespace = runpy.run_path(str(path))

    assert namespace["APP_NAME"] == "Plan Commission Workbench"


def test_windows_build_explicitly_bundles_server_module() -> None:
    script_path = ROOT / "scripts" / "build_windows.ps1"
    script = script_path.read_text(encoding="utf-8")

    assert "--onedir" in script
    assert "--onefile" not in script
    assert 'dist\\PlanCommissionWorkbench' in script
    assert '--hidden-import "plan_commission_workbench.docling_worker"' in script
    assert '--hidden-import "plan_commission_workbench.run_worker"' in script
    assert '--hidden-import "plan_commission_workbench.server"' in script
    assert '--collect-all "docling_parse"' in script
    assert '--collect-all "pypdfium2_raw"' in script
    assert "--self-test-docling" in script
    assert "--self-test-runtime-imports" in script


def test_windows_build_produces_msix_and_appinstaller_artifacts() -> None:
    script = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    verifier = (ROOT / "scripts" / "verify_windows_artifacts.ps1").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "windows-build.yml").read_text(encoding="utf-8")
    rollback_workflow = (ROOT / ".github" / "workflows" / "windows-rollback.yml").read_text(encoding="utf-8")
    signing_script = (ROOT / "scripts" / "create_windows_signing_secret.ps1").read_text(encoding="utf-8")
    installer_script = (ROOT / "packaging" / "windows" / "Install-PlanCommissionWorkbench.ps1").read_text(
        encoding="utf-8"
    )
    installer_cmd = (ROOT / "packaging" / "windows" / "Install-PlanCommissionWorkbench.cmd").read_text(
        encoding="utf-8"
    )

    assert "MakeAppx.exe" in script
    assert "SignTool.exe" in script
    assert "AppxManifest.xml.in" in script
    assert "PlanCommissionWorkbench.appinstaller.in" in script
    assert '$token = "{{" + $key + "}}"' in script
    assert '$token = "{{{0}}}"' not in script
    assert "PlanCommissionWorkbench.msix" in script
    assert "PlanCommissionWorkbench.appinstaller" in script
    assert "PCW_MSIX_STAGING_ROOT" in script
    assert "Assert-LastExitCode" in script
    assert "Windows executable Docling self-test" in script
    assert "Windows executable runtime import self-test" in script
    assert "Get-PythonModuleDirectory \"torch\"" in script
    assert "$TorchSourceDir\\utils\\_config_module.py;torch\\utils" in script
    assert "$TorchSourceDir\\_dynamo\\config.py;torch\\_dynamo" in script
    assert "package.map.txt" in script
    assert "Assert-MsixPayloadPath" in script
    assert "'[<>:\"|?*\\[\\]]'" in script
    assert '$mappingLine = \'"{0}" "{1}"\' -f $sourcePath, $relativePath' in script
    assert "Optimize-MsixPayload $MsixStagingDir" in script
    assert "$graphics.DrawArc($main" in script
    assert "$graphics.DrawLine($main" in script
    assert "$highlightGreen" in script
    assert "Get-StableLogoHash" in script
    assert "$tiltDegrees" in script
    assert 'New-MsixLogo (Join-Path $assetDir "Square150x150Logo.png") 150 $resolvedVersion' in script
    assert "Test-StagedExecutable $MsixStagingDir" in script
    assert "Restore-TorchConfigSources" in script
    assert "Remove-MsixTorchSourcePayload" in script
    assert "Remove-MsixPurePythonSourcePayload" in script
    assert 'Remove-MsixPurePythonSourcePayload $internalDir @("openai")' in script
    assert '"docx\\templates\\default-docx-template"' in script
    assert "Invoke-ExecutableSelfTest" in script
    assert "Start-Process -FilePath $ExecutablePath -ArgumentList $Argument -Wait -PassThru" in script
    assert "Staged MSIX runtime import self-test" in script
    assert '"torch\\_inductor"' in script
    assert '"torch\\include"' in script
    assert "/f $MsixMappingPath" in script
    assert "PCW_APPINSTALLER_URI" in script
    assert "PCW_MSIX_PACKAGE_URI" in script
    assert "CreateTestCertificate" in script
    assert "artifacts/PlanCommissionWorkbench.msix" in workflow
    assert "artifacts/PlanCommissionWorkbench.appinstaller" in workflow
    assert "verify_windows_artifacts.ps1" in workflow
    assert ".\\scripts\\build_windows.ps1 -CreateTestCertificate" in workflow
    assert ".\\scripts\\verify_windows_artifacts.ps1 -RequireTrustedSignature" in workflow
    assert '$arguments += "-CreateTestCertificate"' not in workflow
    assert "PCW_REQUIRE_TRUSTED_SIGNATURE" in workflow
    assert "Configure App Installer feed" in workflow
    assert "function ConvertTo-MsixVersion" in workflow
    assert "Bake diagnostic email defaults" in workflow
    assert "PCW_DIAGNOSTIC_SMTP_PASSWORD" in workflow
    assert "Validate diagnostic email release configuration" in workflow
    assert "pcw-windows-stable" in workflow
    assert "PCW_VERSION_RELEASE_TAG=pcw-windows-v$env:PCW_MSIX_VERSION" in workflow
    assert "Publish App Installer update feed" in workflow
    assert "gh release upload $env:PCW_UPDATE_RELEASE_TAG @files" in workflow
    assert "PCW_SIGNING_CERTIFICATE_BASE64 != ''" in workflow
    assert "Skipped App Installer feed publish" in workflow
    assert "unpack /p" in verifier
    assert "Get-AuthenticodeSignature" in verifier
    assert "RequireTrustedSignature" in verifier
    assert "Configured AppInstaller URI" in verifier
    assert "Configured MSIX package URI" in verifier
    assert "ForceUpdateFromAnyVersion" in verifier
    assert "rollback support requires ForceUpdateFromAnyVersion=true" in verifier
    assert "Roll Back Windows Desktop Update Feed" in rollback_workflow
    assert "pcw-windows-v$env:ROLLBACK_VERSION" in rollback_workflow
    assert "gh release download $versionTag" in rollback_workflow
    assert "gh release upload $stableTag @files" in rollback_workflow
    assert "New-SelfSignedCertificate" in signing_script
    assert "PCW_SIGNING_CERTIFICATE_BASE64" in signing_script
    assert "2.5.29.19={text}" in signing_script
    assert "Cert:\\LocalMachine\\TrustedPeople" in signing_script
    assert "Install-PlanCommissionWorkbench.ps1" in workflow
    assert "Install-PlanCommissionWorkbench.cmd" in workflow
    assert "Start-Process" in installer_script
    assert "Verb RunAs" in installer_script
    assert "PlanCommissionWorkbench-signing.cer" in installer_script
    assert "Cert:\\LocalMachine\\TrustedPeople" in installer_script
    assert "Add-AppxPackage -AppInstallerFile" in installer_script
    assert "ExecutionPolicy Bypass" in installer_cmd


def test_msix_manifest_template_declares_packaged_desktop_app() -> None:
    content = render_template(ROOT / "packaging" / "windows" / "AppxManifest.xml.in", msix_template_values())
    root = ET.fromstring(content)
    ns = {
        "pkg": "http://schemas.microsoft.com/appx/manifest/foundation/windows10",
        "uap10": "http://schemas.microsoft.com/appx/manifest/uap/windows10/10",
        "rescap": "http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities",
    }

    identity = root.find("pkg:Identity", ns)
    application = root.find("pkg:Applications/pkg:Application", ns)
    capability = root.find("pkg:Capabilities/rescap:Capability", ns)

    assert identity is not None
    assert identity.attrib["Name"] == "GECG.PlanCommissionWorkbench"
    assert identity.attrib["Publisher"] == "CN=GECG"
    assert identity.attrib["Version"] == "1.2.3.0"
    assert application is not None
    assert application.attrib["Executable"] == "PlanCommissionWorkbench.exe"
    assert application.attrib[f"{{{ns['uap10']}}}RuntimeBehavior"] == "packagedClassicApp"
    assert application.attrib[f"{{{ns['uap10']}}}TrustLevel"] == "mediumIL"
    assert capability is not None
    assert capability.attrib["Name"] == "runFullTrust"


def test_appinstaller_template_points_to_msix_and_launch_updates() -> None:
    content = render_template(
        ROOT / "packaging" / "windows" / "PlanCommissionWorkbench.appinstaller.in",
        msix_template_values(),
    )
    root = ET.fromstring(content)
    ns = {"appinstaller": "http://schemas.microsoft.com/appx/appinstaller/2021"}
    main_package = root.find("appinstaller:MainPackage", ns)
    on_launch = root.find("appinstaller:UpdateSettings/appinstaller:OnLaunch", ns)
    force_update = root.find("appinstaller:UpdateSettings/appinstaller:ForceUpdateFromAnyVersion", ns)

    assert root.attrib["Version"] == "1.2.3.0"
    assert root.attrib["Uri"] == "https://example.com/PlanCommissionWorkbench.appinstaller"
    assert main_package is not None
    assert main_package.attrib["Name"] == "GECG.PlanCommissionWorkbench"
    assert main_package.attrib["Publisher"] == "CN=GECG"
    assert main_package.attrib["Uri"] == "https://example.com/PlanCommissionWorkbench.msix"
    assert on_launch is not None
    assert on_launch.attrib["HoursBetweenUpdateChecks"] == "0"
    assert force_update is not None
    assert force_update.text == "true"
