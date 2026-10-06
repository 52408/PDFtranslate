@echo off
rem Open the translation-engine settings window (choose provider, paste API key, test connection).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" --settings
