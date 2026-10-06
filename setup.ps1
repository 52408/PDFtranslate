# PDFtrans setup script
# ---------------------------------------------------------------
# Installs PyMuPDF into the project-local deps\ folder.
# Usage (PowerShell):
#   powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1
#
# Pure ASCII on purpose: Windows PowerShell 5.1 reads BOM-less
# .ps1 files as ANSI, so non-ASCII would be mangled.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$deps = Join-Path $root "deps"

Write-Host ""
Write-Host "== PDFtrans setup ==" -ForegroundColor Cyan
Write-Host "project root : $root"

# --- 1) locate a Python 3 interpreter --------------------------------------
$pyExe = ""
$pyArg = ""
$candidates = @()
if ($env:PDFTRANS_PYTHON) { $candidates += $env:PDFTRANS_PYTHON }
$candidates += @(
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
    "C:\Python312\python.exe",
    "C:\Python311\python.exe",
    "python"
)
foreach ($c in $candidates) {
    if ($c -eq "python") {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        $exe = $cmd.Source
    } else {
        if (-not (Test-Path $c)) { continue }
        $exe = $c
    }
    $ver = & $exe -c "import sys;print(sys.version_info[0])" 2>$null
    if ($LASTEXITCODE -eq 0 -and "$ver".Trim() -eq "3") {
        $pyExe = $exe
        break
    }
}
if (-not $pyExe) {
    Write-Host "ERROR: no Python 3 found." -ForegroundColor Red
    Write-Host "Install Python 3.10+ from https://www.python.org/downloads/"
    Write-Host "or set PDFTRANS_PYTHON to your python.exe path."
    exit 1
}
Write-Host "python       : $pyExe"

# --- 2) install PyMuPDF into deps\ ----------------------------------------
New-Item -ItemType Directory -Force -Path $deps | Out-Null
& $pyExe -m pip install --upgrade --target $deps pymupdf
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pip install failed." -ForegroundColor Red
    exit 1
}

# --- 2b) create working folders -------------------------------------------
# These are gitignored (they hold papers / translations), so a fresh clone
# needs them created before the first run.
foreach ($d in @("input", "output", "cache", "inbox")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $root $d) | Out-Null
}
Write-Host "folders      : input\ output\ cache\ inbox\"

# settings.json without any API key (the key comes from the environment)
$sp = Join-Path $root "settings.json"
if (-not (Test-Path $sp)) {
    $default = '{"active_provider":"deepseek","providers":{"deepseek":{"model":"deepseek-chat","base_url":"https://api.deepseek.com"}},"target_language":"Chinese (Simplified)","pdf":{"font_size":9.5,"workers":4,"raster":false,"figures":true,"translate_figures":true,"layout":"region"}}'
    [System.IO.File]::WriteAllText($sp, $default, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "settings.json: created (no API key stored)"
}

# --- 3) verify ------------------------------------------------------------
$env:PYTHONPATH = $deps
& $pyExe -c "import fitz; print('PyMuPDF OK ->', getattr(fitz, '__file__', ''))"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: PyMuPDF import failed after install." -ForegroundColor Red
    exit 1
}

# --- 4) API key reminder (never stored in settings.json) ------------------
$hasKey = $false
if ($env:PDFTRANS_API_KEY) { $hasKey = $true }
if ($env:DEEPSEEK_API_KEY) { $hasKey = $true }
if (-not $hasKey) {
    Write-Host ""
    Write-Host "NOTE: no API key in environment variables." -ForegroundColor Yellow
    Write-Host "      Set one (do NOT put it into settings.json):"
    Write-Host '        setx DEEPSEEK_API_KEY sk-your-key-here'
    Write-Host "      Then open a new terminal."
}

Write-Host ""
Write-Host "Done. Try:" -ForegroundColor Green
Write-Host "  tool\run.cmd input\yourpaper.pdf --pages 1-5 --layout region --translate-figures"
Write-Host ""
exit 0
