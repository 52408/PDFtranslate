<#
  run.ps1 - launcher for pdf_bilingual (PDF library bundled in ..\deps).

  NOTE: keep this file pure ASCII. Windows PowerShell 5.1 reads .ps1 files
  as GBK unless they have a BOM, so non-ASCII comments break the parser.

  Both flag styles work:  -Pages 1-5   or   --pages 1-5

  Examples (run from the tool folder):
    run.cmd --settings                       open the engine settings GUI
    run.cmd --list-providers                 list engines and key status
    run.cmd ..\input\mypaper.pdf             translate whole file -> ..\output
    run.cmd ..\input\mypaper.pdf --pages 1-5 only the first 5 pages
    run.cmd ..\input\mypaper.pdf --dry-run   layout check, no API calls
    run.cmd ..\input\mypaper.pdf --provider google
    run.cmd ..\input\mypaper.pdf --provider ollama --model qwen2.5:7b

  Engine / key / model defaults come from settings.json (written by the GUI).
#>

$ErrorActionPreference = "Stop"

$toolDir = Split-Path -Parent $MyInvocation.MyCommand.Path   # ...\PDFtrans\tool
$root    = Split-Path -Parent $toolDir                       # ...\PDFtrans

# ---------------------------------------------------------------- locate Python
$candidates = @(
    (Join-Path $root "deps\python\python.exe"),
    (Join-Path $env:USERPROFILE ".dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe")
)
$py = $null
foreach ($c in $candidates) {
    if ($c -and (Test-Path $c)) { $py = $c; break }
}
if (-not $py) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source }
}
if (-not $py) {
    throw "Python not found. Install Python 3.10+ or put a portable Python in deps\python\."
}

# ---------------------------------------------------------------- environment
$deps = Join-Path $root "deps"
if (-not (Test-Path (Join-Path $deps "pymupdf"))) {
    throw ("Missing PDF library: {0}\pymupdf does not exist. " +
           "Run setup.ps1 in the project root first." -f $deps)
}
$env:PYTHONPATH = $deps
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$script = Join-Path $toolDir "pdf_bilingual.py"

# ---------------------------------------------------------------- parse args
# Only tokens that start with "-" are treated as options; anything else is the
# PDF path. This avoids the trap where a positional argument swallows the next
# token as if it were an option value.
$flagNames = @{
    "dry-run"        = "dry_run"
    "dryrun"         = "dry_run"
    "raster"         = "raster"
    "no-figures"     = "no_figures"
    "nofigures"      = "no_figures"
    "settings"       = "settings"
    "list-providers" = "list_providers"
    "listproviders"  = "list_providers"
    "make-commands"  = "make_commands"
    "makecommands"   = "make_commands"
    "wizard"         = "wizard"
    "debug"          = "debug"
    "legacy-layout"  = "legacy_layout"
    "legacylayout"   = "legacy_layout"
    "no-notes"       = "no_notes"
    "nonotes"        = "no_notes"
    "no-captions"    = "no_captions"
    "nocaptions"     = "no_captions"
    "render-only"    = "render_only"
    "renderonly"     = "render_only"
    "translate-figures" = "translate_figures"
    "translatefigures"  = "translate_figures"
    "no-figure-captions" = "no_figure_captions"
    "nofigurecaptions"   = "no_figure_captions"
}
$valueNames = @{
    "pages" = "--pages"; "provider" = "--provider"; "api-key" = "--api-key"
    "apikey" = "--api-key"; "base-url" = "--base-url"; "baseurl" = "--base-url"
    "model" = "--model"; "lang" = "--lang"; "font" = "--font"
    "font-size" = "--font-size"; "fontsize" = "--font-size"
    "workers" = "--workers"; "zoom" = "--zoom"; "output" = "--output"
}

$raw = @($args)
if ($raw.Count -eq 0) { $raw = @("--settings") }

$opt = @{}
$rest = @()
$pos = 0
while ($pos -lt $raw.Count) {
    $tok = [string]$raw[$pos]
    $isOption = $tok.StartsWith("-")
    $key = $tok.TrimStart("-").ToLower()

    if ($isOption -and $flagNames.ContainsKey($key)) {
        $opt[$flagNames[$key]] = $true
        $pos++
        continue
    }
    if ($isOption -and $valueNames.ContainsKey($key)) {
        if ($pos + 1 -lt $raw.Count) {
            $opt[$key] = [string]$raw[$pos + 1]
            $pos += 2
            continue
        }
        $pos++
        continue
    }
    if ($isOption) {
        # unknown option: keep it so it is visible rather than silently dropped
        $rest += $tok
        $pos++
        continue
    }
    $rest += $tok
    $pos++
}

if ($opt.ContainsKey("debug")) {
    $lines = @()
    $lines += "raw   = " + ($raw -join " | ")
    $lines += "opt   = " + (($opt.Keys | ForEach-Object { "$_=$($opt[$_])" }) -join " | ")
    $lines += "rest  = " + ($rest -join " | ")
    $dbgPath = Join-Path $env:TEMP "pdftrans_run_debug.txt"
    $lines | Set-Content -Path $dbgPath -Encoding UTF8
    Write-Host ("debug written to " + $dbgPath)
}

if ($opt.ContainsKey("list_providers")) {
    & $py $script --list-providers
    exit $LASTEXITCODE
}
if ($opt.ContainsKey("make_commands")) {
    & $py (Join-Path $toolDir "make_commands.py")
    exit $LASTEXITCODE
}
if ($opt.ContainsKey("settings")) {
    & $py $script --settings
    exit $LASTEXITCODE
}

$InputPdf = $null
if ($rest.Count -gt 0) { $InputPdf = $rest[0] }
if (-not $InputPdf) {
    # No input file given -> open the settings GUI instead of failing.
    & $py $script --settings
    exit $LASTEXITCODE
}

if (-not (Test-Path $InputPdf)) {
    $alt = Join-Path (Join-Path $root "input") $InputPdf
    if (Test-Path $alt) { $InputPdf = $alt } else { throw "PDF not found: $InputPdf" }
}
$InputPdf = (Resolve-Path $InputPdf).Path

$Output = $opt["output"]
if (-not $Output) {
    $outDir = Join-Path $root "output"
    if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }
    $stem = [System.IO.Path]::GetFileNameWithoutExtension($InputPdf)
    $Output = Join-Path $outDir ($stem + ".bilingual.pdf")
}
$cacheDir = Join-Path $root "cache"

Write-Host "== Python : $py"
Write-Host "== input  : $InputPdf"
Write-Host "== output : $Output"
Write-Host "== cache  : $cacheDir"
Write-Host ""

$argList = @($script, $InputPdf, "-o", $Output, "--cache-dir", $cacheDir)
$map = @{
    "pages" = "--pages"; "provider" = "--provider"; "api-key" = "--api-key";
    "base-url" = "--base-url"; "model" = "--model"; "lang" = "--lang"; "font" = "--font";
    "font-size" = "--font-size"; "workers" = "--workers"; "zoom" = "--zoom"
}
foreach ($k in $map.Keys) {
    if ($opt.ContainsKey($k) -and $opt[$k]) { $argList += @($map[$k], $opt[$k]) }
}
if ($opt.ContainsKey("dry_run")) { $argList += "--dry-run" }
if ($opt.ContainsKey("raster")) { $argList += "--raster" }
if ($opt.ContainsKey("no_figures")) { $argList += "--no-figures" }
if ($opt.ContainsKey("wizard")) { $argList += "--wizard" }
if ($opt.ContainsKey("legacy_layout")) { $argList += "--legacy-layout" }
if ($opt.ContainsKey("no_notes")) { $argList += "--no-notes" }
if ($opt.ContainsKey("no_captions")) { $argList += "--no-captions" }
if ($opt.ContainsKey("render_only")) { $argList += "--render-only" }
if ($opt.ContainsKey("translate_figures")) { $argList += "--translate-figures" }
if ($opt.ContainsKey("no_figure_captions")) { $argList += "--no-figure-captions" }

& $py @argList
exit $LASTEXITCODE
