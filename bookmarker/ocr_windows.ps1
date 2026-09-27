param(
    [Parameter(Mandatory = $true)][string]$ImagePath,
    [ValidateSet('zh-Hans-CN', 'en-US')][string]$Language = 'zh-Hans-CN'
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
Add-Type -AssemblyName System.Runtime.WindowsRuntime

$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]

$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetParameters().Count -eq 1 } |
    Select-Object -First 1

function Await-WinRT($operation, [Type]$resultType) {
    $task = $asTask.MakeGenericMethod(@($resultType)).Invoke($null, @($operation))
    return $task.GetAwaiter().GetResult()
}

$file = Await-WinRT ([Windows.Storage.StorageFile]::GetFileFromPathAsync((Resolve-Path -LiteralPath $ImagePath).Path)) ([Windows.Storage.StorageFile])
$stream = Await-WinRT ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
try {
    $decoder = Await-WinRT ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await-WinRT ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    try {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($Language))
        if ($null -eq $engine) { throw "Windows OCR language unavailable: $Language" }
        $result = Await-WinRT ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $lines = @(
            foreach ($line in $result.Lines) {
                $words = @(
                    foreach ($word in $line.Words) {
                        [pscustomobject]@{
                            text = $word.Text
                            x = [double]$word.BoundingRect.X
                            y = [double]$word.BoundingRect.Y
                            width = [double]$word.BoundingRect.Width
                            height = [double]$word.BoundingRect.Height
                        }
                    }
                )
                [pscustomobject]@{ text = $line.Text; words = $words }
            }
        )
        [pscustomobject]@{ width = $bitmap.PixelWidth; height = $bitmap.PixelHeight; lines = $lines } |
            ConvertTo-Json -Depth 8 -Compress
    } finally {
        if ($null -ne $bitmap) { $bitmap.Dispose() }
    }
} finally {
    if ($null -ne $stream) { $stream.Dispose() }
}
