@echo off
chcp 65001 >nul
rem ===========================================================================
rem  Start the DeepSeek object-location demo web server.
rem  Double-click this file. Real logic lives in start-web.ps1 next to it.
rem  Modelled on ~/.dsh/desktop-launcher/start-dsh-web.cmd (thin cmd -> ps1).
rem  NOTE: keep this file ASCII-only; cmd.exe mangles UTF-8 batch files even
rem        with chcp 65001. All the Chinese explanation is in the .ps1.
rem        The .ps1 itself MUST keep its UTF-8 BOM for the same reason in reverse:
rem        Windows PowerShell 5.1 (what this file launches) reads a BOM-less .ps1
rem        as ANSI and chokes on the Chinese comments. tests/test_launcher.py
rem        pins both rules down, with negative controls.
rem ===========================================================================
rem  Arguments are forwarded, so "start-web.cmd -NoBrowser" works from a console
rem  (the .lnk shortcut created by install-shortcut.ps1 double-clicks with none).
setlocal
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=powershell.exe"
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-web.ps1" %*
endlocal
