param(
    [Parameter(Mandatory=$true)][string]$ImagePath,
    [Parameter(Mandatory=$true)][string]$OutputJson,
    [string]$Language = "zh-Hans-CN"
)

# 注意：这一段故意保持纯 ASCII 之外的最小化——PowerShell 5.1 按 GBK 读无 BOM 的 .ps1，
# 中文注释/字符串会破坏解析。所有诊断信息用英文。

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq "IAsyncOperation``1"
})[0]

$script:AccessDenied = $false

function Await-WinRt($op, $type) {
    $task = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
    try {
        $task.Wait(-1) | Out-Null
    } catch {
        # Record the flag here while the real exception is in hand, then rethrow
        # the original object (throwing a string would truncate the chain).
        $cur = $_.Exception
        $d = 0
        while ($cur -and $d -lt 10) {
            if ($cur.HResult -eq -2147024891 -or $cur -is [System.UnauthorizedAccessException]) {
                $script:AccessDenied = $true
            }
            if ($cur -is [System.AggregateException] -and $cur.InnerExceptions.Count -ge 1) {
                $cur = $cur.InnerExceptions[0]
            } else {
                $cur = $cur.InnerException
            }
            $d++
        }
        throw
    }
    return $task.Result
}

[Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Media, ContentType = WindowsRuntime] | Out-Null

function Fail($stage, $err) {
    # No Write-Error (it throws under ErrorActionPreference=Stop and hides the cause).
    # No -f formatting (it swallows pipeline args in PowerShell) -- plain concat.
    $detail = "unknown"
    $cur = $err
    $depth = 0
    while ($cur -and $depth -lt 10) {
        if ($cur.Message) { $detail = $cur.GetType().Name + ": " + $cur.Message }
        if ($cur -is [System.AggregateException] -and $cur.InnerExceptions.Count -ge 1) {
            $cur = $cur.InnerExceptions[0]
        } else {
            $cur = $cur.InnerException
        }
        $depth++
    }
    if ($cur -and $cur.Message) { $detail = $cur.GetType().Name + ": " + $cur.Message }
    $tag = ""
    if ($script:AccessDenied) { $tag = " access-denied" }
    [Console]::Error.WriteLine("[ocr_win] stage=" + $stage + $tag + " detail=" + $detail)
    exit 3
}

$file = $null
try {
    $file = Await-WinRt ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
} catch { Fail "GetFileFromPathAsync" $_.Exception }

$stream = $null
try {
    $stream = Await-WinRt ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
} catch { Fail "OpenAsync" $_.Exception }

$decoder = $null
try {
    $decoder = Await-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
} catch { Fail "BitmapDecoder" $_.Exception }

$bitmap = $null
try {
    $bitmap = Await-WinRt ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
} catch { Fail "GetSoftwareBitmap" $_.Exception }

$lang = New-Object Windows.Globalization.Language($Language)
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
if ($null -eq $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
}
if ($null -eq $engine) {
    [Console]::Error.WriteLine("[ocr_win] stage=engine detail=no OCR engine for language $Language")
    exit 4
}

$result = $null
try {
    $result = Await-WinRt ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
} catch { Fail "RecognizeAsync" $_.Exception }

$lines = New-Object System.Collections.ArrayList
foreach ($line in $result.Lines) {
    if (-not $line.Text.Trim()) { continue }
    $minX = [double]::MaxValue
    $minY = [double]::MaxValue
    $maxX = [double]::MinValue
    $maxY = [double]::MinValue
    foreach ($word in $line.Words) {
        $r = $word.BoundingRect
        if ($r.X -lt $minX) { $minX = $r.X }
        if ($r.Y -lt $minY) { $minY = $r.Y }
        if (($r.X + $r.Width) -gt $maxX) { $maxX = $r.X + $r.Width }
        if (($r.Y + $r.Height) -gt $maxY) { $maxY = $r.Y + $r.Height }
    }
    if ($maxX -le $minX -or $maxY -le $minY) { continue }
    [void]$lines.Add([ordered]@{
        text = $line.Text
        x = [math]::Round($minX, 2)
        y = [math]::Round($minY, 2)
        w = [math]::Round($maxX - $minX, 2)
        h = [math]::Round($maxY - $minY, 2)
    })
}
$lines | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $OutputJson -Encoding UTF8
Write-Output ("OCR_LINES=" + $lines.Count)
