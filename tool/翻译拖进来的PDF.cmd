@echo off
rem Drag a PDF onto this file, or drop PDFs into ..\inbox and double-click this file.
rem Engine / API key come from settings.json - run 设置翻译引擎.cmd first if needed.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
if errorlevel 1 pause
