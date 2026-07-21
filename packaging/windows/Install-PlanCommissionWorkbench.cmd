@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_PATH=%SCRIPT_DIR%Install-PlanCommissionWorkbench.ps1"
set "FALLBACK_SCRIPT=%TEMP%\Install-PlanCommissionWorkbench.ps1"
set "SCRIPT_URL=https://github.com/jaiprototypes/plan_commission_workbench/releases/download/pcw-windows-stable/Install-PlanCommissionWorkbench.ps1"

if exist "%SCRIPT_PATH%" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_PATH%"
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -UseBasicParsing -Uri '%SCRIPT_URL%' -OutFile '%FALLBACK_SCRIPT%'; & '%FALLBACK_SCRIPT%'"
)

exit /b %ERRORLEVEL%
