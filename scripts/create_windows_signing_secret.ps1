param(
    [string]$Publisher = "CN=GECG",
    [string]$OutputDir,
    [string]$Password
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$resolvedOutputDir = if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    Join-Path $Root "artifacts\signing"
}
else {
    $OutputDir
}

if ([string]::IsNullOrWhiteSpace($Password)) {
    throw "Provide -Password for the exported PFX. Store it in GitHub secret PCW_SIGNING_CERTIFICATE_PASSWORD."
}
if (-not (Get-Command New-SelfSignedCertificate -ErrorAction SilentlyContinue)) {
    throw "New-SelfSignedCertificate is unavailable. Run this on Windows PowerShell."
}

New-Item -ItemType Directory -Force -Path $resolvedOutputDir | Out-Null
$certificate = New-SelfSignedCertificate `
    -Type Custom `
    -Subject $Publisher `
    -CertStoreLocation "Cert:\CurrentUser\My" `
    -KeyExportPolicy Exportable `
    -KeyUsage DigitalSignature `
    -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3", "2.5.29.19={text}") `
    -FriendlyName "Plan Commission Workbench persistent MSIX signing certificate"

$pfxPath = Join-Path $resolvedOutputDir "PlanCommissionWorkbench-signing.pfx"
$cerPath = Join-Path $resolvedOutputDir "PlanCommissionWorkbench-signing.cer"
$base64Path = Join-Path $resolvedOutputDir "PlanCommissionWorkbench-signing.pfx.base64.txt"
$securePassword = ConvertTo-SecureString $Password -AsPlainText -Force

Export-PfxCertificate -Cert $certificate -FilePath $pfxPath -Password $securePassword | Out-Null
Export-Certificate -Cert $certificate -FilePath $cerPath | Out-Null
[Convert]::ToBase64String([IO.File]::ReadAllBytes($pfxPath)) | Set-Content -Path $base64Path -Encoding ascii

Write-Host "Created persistent MSIX signing files:"
Write-Host "  Public certificate: $cerPath"
Write-Host "  Private PFX: $pfxPath"
Write-Host "  GitHub secret payload: $base64Path"
Write-Host ""
Write-Host "Trust the public certificate once on the production PC:"
Write-Host "  Import-Certificate -FilePath `"$cerPath`" -CertStoreLocation Cert:\LocalMachine\TrustedPeople"
Write-Host ""
Write-Host "Or download and run Install-PlanCommissionWorkbench.cmd from the stable release."
Write-Host ""
Write-Host "Configure GitHub with the PFX payload and password:"
Write-Host "  gh secret set PCW_SIGNING_CERTIFICATE_BASE64 --body (Get-Content -Raw `"$base64Path`")"
Write-Host "  gh secret set PCW_SIGNING_CERTIFICATE_PASSWORD --body <the same password>"
Write-Host "  gh variable set PCW_MSIX_PUBLISHER --body `"$Publisher`""
