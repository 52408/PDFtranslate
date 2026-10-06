@echo off
rem Auto-detect PDFs in ..\input, generate the README route-A commands, and optionally run them.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" --make-commands
if errorlevel 1 pause
