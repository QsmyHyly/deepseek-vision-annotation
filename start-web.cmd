@echo off
chcp 65001 >nul
rem ===========================================================================
rem  Start the DeepSeek object-location demo web server.
rem  Double-click this file. Real logic lives in start-web.ps1 next to it.
rem  Modelled on ~/.dsh/desktop-launcher/start-dsh-web.cmd (thin cmd -> ps1).
rem  NOTE: keep this file ASCII-only; cmd.exe mangles UTF-8 batch files even
rem        with chcp 65001. All the Chinese explanation is in the .ps1.
rem ===========================================================================
setlocal
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=powershell.exe"
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-web.ps1"
endlocal
