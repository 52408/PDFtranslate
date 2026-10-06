@echo off
rem run.cmd - launcher that bypasses the PowerShell execution policy.
rem Usage: run.cmd ..\input\mypaper.pdf [extra options]
rem   e.g. run.cmd ..\input\mypaper.pdf -Pages 1-5
rem        run.cmd ..\input\mypaper.pdf -DryRun
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
