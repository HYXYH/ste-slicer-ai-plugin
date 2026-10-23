@echo off
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install-plugin.ps1" %*
set "installExitCode=%ERRORLEVEL%"
echo.
if "%installExitCode%"=="0" (
    echo Installation finished. Restart STE Slicer to load the plugin.
) else (
    echo Installation failed. See the error above.
)
echo.
pause
exit /b %installExitCode%
