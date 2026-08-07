param(
    [Parameter(Position = 0)]
    [string]$InputPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$script:CurrentInputFile = $null
$script:CurrentInputBase = "run"
$script:ResultsDir = $null
$script:WorkDir = $null
$script:EnableNvencArgDebug = $true

function Write-Info {
    param([string]$Message)
    Write-Host "[INFO] $Message" -ForegroundColor Cyan
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Write-Fail {
    param([string]$Message)
    Write-Host "[FEHLER] $Message" -ForegroundColor Red
}

function Read-Choice {
    param(
        [string]$Prompt,
        [string]$Default,
        [string[]]$Allowed
    )

    while ($true) {
        $value = Read-Host "$Prompt [Standard: $Default]"
        if ([string]::IsNullOrWhiteSpace($value)) {
            $value = $Default
        }

        if ($Allowed -contains $value) {
            return $value
        }

        Write-Warn "Ungueltige Eingabe: '$value'. Erlaubt: $($Allowed -join ', ')."
    }
}

function Assert-File {
    param([string]$Path, [string]$Label)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label wurde nicht gefunden: $Path"
    }
}

function Get-ToolPaths {
    $scriptRoot = Split-Path -Parent $PSCommandPath

    $ffmpeg = Join-Path $scriptRoot "FFMPeg\ffmpeg.exe"
    $ffprobe = Join-Path $scriptRoot "FFMPeg\ffprobe.exe"
    $nvencc = Join-Path $scriptRoot "NVEncC\nvencc64.exe"
    $results = Join-Path $scriptRoot "Results"

    Assert-File -Path $ffmpeg -Label "ffmpeg"
    Assert-File -Path $ffprobe -Label "ffprobe"

    if (-not (Test-Path -LiteralPath $results -PathType Container)) {
        New-Item -ItemType Directory -Path $results | Out-Null
    }

    return [pscustomobject]@{
        ScriptRoot = $scriptRoot
        FFmpeg = $ffmpeg
        FFprobe = $ffprobe
        NVEncC = $nvencc
        Results = $results
    }
}

function Get-InputFile {
    param([string]$InputPath)

    if ([string]::IsNullOrWhiteSpace($InputPath)) {
        $InputPath = Read-Host "Videodatei Pfad eingeben (oder Datei per Drag and Drop auf Script ziehen)"
    }

    if ([string]::IsNullOrWhiteSpace($InputPath)) {
        throw "Es wurde keine Eingabedatei angegeben."
    }

    $clean = $InputPath.Trim('"')
    $resolved = Resolve-Path -LiteralPath $clean -ErrorAction Stop
    $item = Get-Item -LiteralPath $resolved

    if ($item.PSIsContainer) {
        throw "Eingabe ist ein Ordner, erwartet wird eine Videodatei: $($item.FullName)"
    }

    return $item.FullName
}

function Test-NvidiaGpu {
    param([string]$NvencPath)

    if (-not (Test-Path -LiteralPath $NvencPath -PathType Leaf)) {
        return $false
    }

    $hasNvidiaSmi = $false
    $smiOutput = ""
    try {
        $smiOutput = & nvidia-smi -L 2>&1
        if ($LASTEXITCODE -eq 0 -and ($smiOutput | Out-String) -match "GPU") {
            $hasNvidiaSmi = $true
        }
    } catch {
        $hasNvidiaSmi = $false
    }

    if (-not $hasNvidiaSmi) {
        return $false
    }

    try {
        $check = Invoke-NvencSync -NvencPath $NvencPath -Arguments @("--version")
        return $check.ExitCode -eq 0 -and $check.OutputText -match "NVEncC"
    } catch {
        return $false
    }
}

function Get-SubtitleTrackIndexZeroExists {
    param(
        [string]$FFprobe,
        [string]$InputFile
    )

    $args = @(
        "-v", "error",
        "-select_streams", "s:0",
        "-show_entries", "stream=index",
        "-of", "default=noprint_wrappers=1:nokey=1",
        $InputFile
    )

    $output = & $FFprobe @args 2>&1
    return $LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace(($output | Out-String).Trim())
}

function Get-VideoDurationSeconds {
    param(
        [string]$FFprobe,
        [string]$InputFile
    )

    $args = @(
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        $InputFile
    )

    $output = & $FFprobe @args 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Konnte Gesamtdauer mit ffprobe nicht lesen."
    }

    $raw = ($output | Out-String).Trim()
    if ([string]::IsNullOrWhiteSpace($raw)) {
        throw "ffprobe lieferte keine Dauer fuer das Quellvideo."
    }

    $duration = 0.0
    if (-not [double]::TryParse($raw, [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$duration)) {
        throw "Konnte ffprobe-Dauer nicht parsen: $raw"
    }

    return [int][Math]::Round($duration)
}

function Get-PeakWindow {
    param(
        [string]$FFprobe,
        [string]$InputFile,
        [int]$WindowSeconds = 180
    )

    Write-Info "Analysiere Paket-Bitraten mit ffprobe..."
    $tempCsv = Join-Path $env:TEMP ("packets_" + [guid]::NewGuid().ToString("N") + ".csv")

    try {
        $args = @(
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "packet=pts_time,size",
            "-of", "csv=p=0:nk=1",
            $InputFile
        )

        & $FFprobe @args | Set-Content -LiteralPath $tempCsv
        if ($LASTEXITCODE -ne 0) {
            throw "ffprobe Paketanalyse fehlgeschlagen."
        }

        $bytesPerSecond = @{}
        foreach ($line in Get-Content -LiteralPath $tempCsv) {
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            $parts = $line.Split(',')
            if ($parts.Count -lt 2) { continue }

            $pts = 0.0
            $size = 0L

            $parsed = [double]::TryParse($parts[0], [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$pts) -and
                      [long]::TryParse($parts[1], [System.Globalization.NumberStyles]::Integer, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$size)

            if (-not $parsed) {
                $ptsSwap = 0.0
                $sizeSwap = 0L
                $parsedSwap = [double]::TryParse($parts[1], [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$ptsSwap) -and
                              [long]::TryParse($parts[0], [System.Globalization.NumberStyles]::Integer, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$sizeSwap)

                if (-not $parsedSwap) { continue }

                $pts = $ptsSwap
                $size = $sizeSwap
            }

            $sec = [long][Math]::Floor($pts)
            if ($bytesPerSecond.ContainsKey($sec)) {
                $bytesPerSecond[$sec] = [long]$bytesPerSecond[$sec] + [long]$size
            } else {
                $bytesPerSecond[$sec] = [long]$size
            }
        }

        if ($bytesPerSecond.Count -eq 0) {
            throw "Keine Paketdaten gefunden."
        }

        $maxSecond = [long](($bytesPerSecond.Keys | Measure-Object -Maximum).Maximum)
        if ($maxSecond -gt ([int]::MaxValue - 1)) {
            throw "Unplausibler Zeitstempel in ffprobe-Ausgabe erkannt (>$([int]::MaxValue - 1))."
        }
        $durationSeconds = [int]$maxSecond + 1
        $effectiveWindow = [Math]::Min($WindowSeconds, [Math]::Max(1, $durationSeconds))

        $perSec = New-Object 'long[]' $durationSeconds
        for ($i = 0; $i -lt $durationSeconds; $i++) {
            $secKey = [long]$i
            if ($bytesPerSecond.ContainsKey($secKey)) {
                $perSec[$i] = $bytesPerSecond[$secKey]
            } else {
                $perSec[$i] = 0
            }
        }

        $running = 0L
        for ($i = 0; $i -lt $effectiveWindow; $i++) {
            $running += $perSec[$i]
        }

        $bestStart = 0
        $bestBytes = $running

        for ($start = 1; $start -le ($durationSeconds - $effectiveWindow); $start++) {
            $running += $perSec[$start + $effectiveWindow - 1]
            $running -= $perSec[$start - 1]
            if ($running -gt $bestBytes) {
                $bestBytes = $running
                $bestStart = $start
            }
        }

        $avgMbps = [Math]::Round((($bestBytes * 8.0) / $effectiveWindow) / 1000000.0, 3)

        return [pscustomobject]@{
            StartSeconds = $bestStart
            WindowSeconds = $effectiveWindow
            AvgMbps = $avgMbps
        }
    }
    finally {
        if (Test-Path -LiteralPath $tempCsv) {
            Remove-Item -LiteralPath $tempCsv -Force -ErrorAction SilentlyContinue
        }
    }
}

function Cut-TestSample {
    param(
        [string]$FFmpeg,
        [string]$InputFile,
        [int]$StartSeconds,
        [int]$DurationSeconds,
        [string]$OutputPath
    )

    Write-Info "Schneide Test-Clip ($DurationSeconds s) ab Sekunde $StartSeconds..."

    $copyArgs = @(
        "-hide_banner", "-loglevel", "error", "-y",
        "-ss", $StartSeconds,
        "-i", $InputFile,
        "-t", $DurationSeconds,
        "-map", "0:v:0",
        "-an",
        "-sn",
        "-c", "copy",
        $OutputPath
    )

    & $FFmpeg @copyArgs
    if ($LASTEXITCODE -eq 0) {
        return
    }

    Write-Warn "Copy-Cut fehlgeschlagen, nutze re-encode fallback fuer den Test-Clip."
    $fallbackArgs = @(
        "-hide_banner", "-loglevel", "error", "-y",
        "-ss", $StartSeconds,
        "-i", $InputFile,
        "-t", $DurationSeconds,
        "-map", "0:v:0",
        "-an",
        "-sn",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "0",
        $OutputPath
    )
    & $FFmpeg @fallbackArgs

    if ($LASTEXITCODE -ne 0) {
        throw "Konnte Test-Clip nicht erstellen."
    }
}

function Cut-NoiseSample {
    param(
        [string]$FFmpeg,
        [string]$InputFile,
        [int]$PeakStartSeconds,
        [int]$PeakWindowSeconds,
        [string]$OutputPath
    )

    $noiseStart = [int][Math]::Floor($PeakStartSeconds + ($PeakWindowSeconds / 2.0) - 10.0)
    if ($noiseStart -lt 0) {
        $noiseStart = 0
    }

    Write-Info "Schneide Rausch-Sample (20 s) ab Sekunde $noiseStart..."

    $copyArgs = @(
        "-hide_banner", "-loglevel", "error", "-y",
        "-ss", $noiseStart,
        "-i", $InputFile,
        "-t", "20",
        "-map", "0:v:0",
        "-an",
        "-sn",
        "-c", "copy",
        $OutputPath
    )

    & $FFmpeg @copyArgs
    if ($LASTEXITCODE -eq 0) {
        return $noiseStart
    }

    Write-Warn "Copy-Cut fuer Rausch-Sample fehlgeschlagen, nutze re-encode fallback."
    $fallbackArgs = @(
        "-hide_banner", "-loglevel", "error", "-y",
        "-ss", $noiseStart,
        "-i", $InputFile,
        "-t", "20",
        "-map", "0:v:0",
        "-an",
        "-sn",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "0",
        $OutputPath
    )

    & $FFmpeg @fallbackArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Konnte Rausch-Sample nicht erstellen."
    }

    return $noiseStart
}

function Get-NvencBaseArgs {
    param(
        [ValidateSet("hevc", "av1")]
        [string]$Codec,
        [int]$Qvbr
    )

    if ($Codec -eq "hevc") {
        return @(
            "--avhw",
            "--codec", "hevc",
            "--profile", "main10",
            "--tier", "high",
            "--level", "5.1",
            "--qvbr", $Qvbr,
            "--output-depth", "10",
            "--preset", "P7",
            "--multipass", "2pass-full",
            "--lookahead", "32",
            "--lookahead-level", "3",
            "--aq",
            "--aq-temporal",
            "--ref", "4",
            "--bframes", "4",
            "--bref-mode", "middle",
            "--pic-struct"
        )
    }

    return @(
        "--avhw",
        "--codec", "av1",
        "--profile", "main",
        "--qvbr", $Qvbr,
        "--output-depth", "10",
        "--preset", "P7",
        "--multipass", "2pass-full",
        "--lookahead", "32",
        "--lookahead-level", "3",
        "--aq",
        "--aq-temporal",
        "--ref", "4",
        "--bframes", "4",
        "--bref-mode", "middle",
        "--pic-struct"
    )
}

function Get-AiModeArgs {
    param([ValidateSet("1", "2", "3", "4")][string]$AiChoice)

    switch ($AiChoice) {
        "2" {
            return @(
                "--colormatrix", "bt2020nc",
                "--colorprim", "bt2020",
                "--transfer", "smpte2084",
                "--max-cll", "1000,300",
                "--master-display", "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)",
                "--atc-sei", "auto",
                "--vpp-ngx-truehdr", "contrast=80,saturation=90,middlegray=50,maxluminance=1000"
            )
        }
        "3" {
            return @(
                "--vpp-ngx-vrs", "quality=ultra-high,height=1080"
            )
        }
        "4" {
            return @(
                "--vpp-ngx-vrs", "quality=ultra-high,height=1080",
                "--colormatrix", "bt2020nc",
                "--colorprim", "bt2020",
                "--transfer", "smpte2084",
                "--max-cll", "1000,300",
                "--master-display", "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)",
                "--atc-sei", "auto",
                "--vpp-ngx-truehdr", "contrast=80,saturation=90,middlegray=50,maxluminance=1000"
            )
        }
        default {
            return @()
        }
    }
}

function Get-AiModeName {
    param([ValidateSet("1", "2", "3", "4")][string]$AiChoice)

    switch ($AiChoice) {
        "2" { return "TrueHDR" }
        "3" { return "DVD2HD" }
        "4" { return "DVD2HD_TrueHDR" }
        default { return "Native" }
    }
}

function Invoke-NvencSync {
    param(
        [string]$NvencPath,
        [string[]]$Arguments,
        [int]$MaxOutputLines = 2000,
        [switch]$PersistFullOutputOnTrim
    )

    $Arguments = @($Arguments | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })

    # Native Pipeline mit zusammengefuehrten Streams vermeidet StdOut/StdErr Deadlocks.
    # Bei globalem ErrorActionPreference=Stop darf nativer stderr nicht als terminierender Fehler abbrechen.
    $previousEap = $ErrorActionPreference
    $output = @()
    $exitCode = -1

    try {
        $ErrorActionPreference = "Continue"
        $output = & $NvencPath @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousEap
    }

    $lines = @()
    if ($null -ne $output) {
        $lines = @(
            $output | ForEach-Object {
                if ($_ -is [System.Management.Automation.ErrorRecord]) {
                    [string]$_.Exception.Message
                }
                else {
                    [string]$_
                }
            }
        )
    }

    $fullLineCount = $lines.Count
    $fullOutputLogPath = $null

    if ($MaxOutputLines -gt 0 -and $fullLineCount -gt $MaxOutputLines) {
        if ($PersistFullOutputOnTrim) {
            $logBaseDir = if (-not [string]::IsNullOrWhiteSpace($script:WorkDir) -and (Test-Path -LiteralPath $script:WorkDir -PathType Container)) { $script:WorkDir } else { $env:TEMP }
            $fullOutputLogPath = Join-Path $logBaseDir ("nvencc_output_" + [guid]::NewGuid().ToString("N") + ".log")
            Set-Content -LiteralPath $fullOutputLogPath -Value $lines -Encoding UTF8
        }

        $start = $fullLineCount - $MaxOutputLines
        $lines = $lines[$start..($fullLineCount - 1)]
    }

    return [pscustomobject]@{
        ExitCode = $exitCode
        OutputLines = $lines
        OutputLineCount = $fullLineCount
        FullOutputLogPath = $fullOutputLogPath
        OutputText = ($lines -join [Environment]::NewLine).Trim()
    }
}

function Write-NvencArgsDebugLog {
    param(
        [string]$Stage,
        [string[]]$Arguments
    )

    if (-not $script:EnableNvencArgDebug) {
        return
    }

    $logDir = $null
    if (-not [string]::IsNullOrWhiteSpace($script:WorkDir) -and (Test-Path -LiteralPath $script:WorkDir -PathType Container)) {
        $logDir = $script:WorkDir
    }
    elseif (-not [string]::IsNullOrWhiteSpace($script:ResultsDir) -and (Test-Path -LiteralPath $script:ResultsDir -PathType Container)) {
        $logDir = $script:ResultsDir
    }

    if ([string]::IsNullOrWhiteSpace($logDir)) {
        return
    }

    $baseName = if ([string]::IsNullOrWhiteSpace($script:CurrentInputBase)) { "run" } else { $script:CurrentInputBase }
    $logPath = Join-Path $logDir ("{0}_nvencc_args.log" -f $baseName)

    $cleanArgs = @($Arguments | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
    $argLine = ($cleanArgs | ForEach-Object {
        $text = [string]$_
        if ($text -match '\s') {
            '"' + $text.Replace('"', '\"') + '"'
        }
        else {
            $text
        }
    }) -join ' '

    $lines = @(
        "Timestamp: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
        "Stage: $Stage",
        "Args: $argLine",
        ""
    )

    Add-Content -LiteralPath $logPath -Value $lines -Encoding UTF8
}

function Get-NvencVmafFromOutput {
    param(
        [object[]]$OutputLines,
        [int]$Qvbr
    )

    $values = @()
    $patternPrimary = '(?i)VMAF(?:\s+score)?\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)'
    $patternFallback = '(?i)VMAF[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)'

    foreach ($line in $OutputLines) {
        $text = [string]$line
        $match = [regex]::Match($text, $patternPrimary)
        if (-not $match.Success) {
            $match = [regex]::Match($text, $patternFallback)
        }

        if ($match.Success) {
            $score = 0.0
            if ([double]::TryParse($match.Groups[1].Value, [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$score)) {
                if ($score -ge 0.0 -and $score -le 100.0) {
                    $values += $score
                }
            }
        }
    }

    if ($values.Count -eq 0) {
        throw "Konnte VMAF-Score nicht aus NVEncC-Output lesen (QVBR=$Qvbr)."
    }

    return [Math]::Round([double]$values[-1], 3)
}

function Test-NoiseLevel {
    param(
        [string]$Nvenc,
        [string]$NoiseSample,
        [string]$WorkDir
    )

    $noiseRaw = Join-Path $WorkDir "noise_raw.mkv"
    $noiseDenoised = Join-Path $WorkDir "noise_denoised.mkv"

    foreach ($f in @($noiseRaw, $noiseDenoised)) {
        if (Test-Path -LiteralPath $f) {
            Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
        }
    }

    $baseArgs = @()
    $baseArgs += Get-NvencBaseArgs -Codec "hevc" -Qvbr 22

    $rawArgs = @()
    $rawArgs += $baseArgs
    $rawArgs += @(
        "-i", $NoiseSample,
        "-o", $noiseRaw
    )
    Write-NvencArgsDebugLog -Stage "NoiseCheck-Raw" -Arguments $rawArgs

    $denoiseArgs = @()
    $denoiseArgs += $baseArgs
    $denoiseArgs += @(
        "--vpp-knn",
        "-i", $NoiseSample,
        "-o", $noiseDenoised
    )
    Write-NvencArgsDebugLog -Stage "NoiseCheck-Denoise" -Arguments $denoiseArgs

    $rawResult = Invoke-NvencSync -NvencPath $Nvenc -Arguments $rawArgs -PersistFullOutputOnTrim
    if ($rawResult.ExitCode -ne 0) {
        $tail = ($rawResult.OutputLines | Select-Object -Last 12) -join [Environment]::NewLine
        $fullLogHint = if ($rawResult.FullOutputLogPath) { "`nVollstaendiges NVEncC-Log: $($rawResult.FullOutputLogPath)" } else { "" }
        throw "Rauschcheck Lauf A fehlgeschlagen.`n$tail$fullLogHint"
    }

    $denoiseResult = Invoke-NvencSync -NvencPath $Nvenc -Arguments $denoiseArgs -PersistFullOutputOnTrim
    if ($denoiseResult.ExitCode -ne 0) {
        $tail = ($denoiseResult.OutputLines | Select-Object -Last 12) -join [Environment]::NewLine
        $fullLogHint = if ($denoiseResult.FullOutputLogPath) { "`nVollstaendiges NVEncC-Log: $($denoiseResult.FullOutputLogPath)" } else { "" }
        throw "Rauschcheck Lauf B (Denoise) fehlgeschlagen.`n$tail$fullLogHint"
    }

    if (-not (Test-Path -LiteralPath $noiseRaw) -or -not (Test-Path -LiteralPath $noiseDenoised)) {
        throw "Rauschcheck Dateien wurden nicht erzeugt."
    }

    $sizeRaw = [double](Get-Item -LiteralPath $noiseRaw).Length
    $sizeDenoised = [double](Get-Item -LiteralPath $noiseDenoised).Length

    if ($sizeRaw -le 0) {
        throw "Rauschcheck ungueltig: noise_raw.mkv ist leer."
    }

    $delta = ($sizeRaw - $sizeDenoised) / $sizeRaw
    if ($delta -lt 0) {
        $delta = 0
    }

    return [pscustomobject]@{
        NoiseDetected = ($delta -ge 0.25)
        Delta = $delta
        RawSize = [int64]$sizeRaw
        DenoisedSize = [int64]$sizeDenoised
    }
}

function Encode-TestNvenc {
    param(
        [string]$Nvenc,
        [string]$InputClip,
        [string]$OutputClip,
        [string]$Codec,
        [int]$Qvbr,
        [int]$SampleDurationSeconds = 180
    )

    if (Test-Path -LiteralPath $OutputClip) {
        Remove-Item -LiteralPath $OutputClip -Force
    }

    $baseArgs = @()
    $baseArgs += Get-NvencBaseArgs -Codec $Codec -Qvbr $Qvbr
    $baseArgs += @(
        "-i", $InputClip,
        "-o", $OutputClip
    )

    $sampleEncodeTime = 0.0
    $result = $null
    $usedNativeVmaf = $true

    $argsNative = @("--vmaf") + $baseArgs
    Write-NvencArgsDebugLog -Stage ("Tune-QVBR-{0}-NativeVMAF" -f $Qvbr) -Arguments $argsNative

    $measureNative = Measure-Command {
        $result = Invoke-NvencSync -NvencPath $Nvenc -Arguments $argsNative -PersistFullOutputOnTrim
    }
    $sampleEncodeTime = [double]$measureNative.TotalSeconds

    if ($result.ExitCode -ne 0) {
        Write-Warn "NVEncC Testencode mit --vmaf fehlgeschlagen (QVBR=$Qvbr). Versuche Testencode ohne --vmaf und nutze FFmpeg-VMAF-Fallback."
        $usedNativeVmaf = $false

        $argsFallback = @($baseArgs)
        Write-NvencArgsDebugLog -Stage ("Tune-QVBR-{0}-FallbackNoVMAF" -f $Qvbr) -Arguments $argsFallback

        $measureFallback = Measure-Command {
            $result = Invoke-NvencSync -NvencPath $Nvenc -Arguments $argsFallback -PersistFullOutputOnTrim
        }
        $sampleEncodeTime = [double]$measureFallback.TotalSeconds

        if ($result.ExitCode -ne 0) {
            $tail = ($result.OutputLines | Select-Object -Last 12) -join [Environment]::NewLine
            $fullLogHint = if ($result.FullOutputLogPath) { "`nVollstaendiges NVEncC-Log: $($result.FullOutputLogPath)" } else { "" }
            throw "NVEncC Testencode fehlgeschlagen (QVBR=$Qvbr).`n$tail$fullLogHint"
        }
    }

    $speedFactor = 0.0
    if ($sampleEncodeTime -gt 0) {
        $speedFactor = [double]$SampleDurationSeconds / $sampleEncodeTime
    }

    try {
        if (-not $usedNativeVmaf) {
            return [pscustomobject]@{
                Vmaf = $null
                UsedFallback = $true
                SampleEncodeTime = $sampleEncodeTime
                SpeedFactor = $speedFactor
            }
        }

        $vmaf = Get-NvencVmafFromOutput -OutputLines $result.OutputLines -Qvbr $Qvbr
        return [pscustomobject]@{
            Vmaf = $vmaf
            UsedFallback = $false
            SampleEncodeTime = $sampleEncodeTime
            SpeedFactor = $speedFactor
        }
    }
    catch {
        Write-Warn "NVEncC-VMAF konnte nicht gelesen werden (QVBR=$Qvbr). Nutze FFmpeg libvmaf Fallback fuer diese Iteration."
        return [pscustomobject]@{
            Vmaf = $null
            UsedFallback = $true
            SampleEncodeTime = $sampleEncodeTime
            SpeedFactor = $speedFactor
        }
    }
}

function Get-VmafScore {
    param(
        [string]$FFmpeg,
        [string]$EncodedClip,
        [string]$ReferenceClip,
        [string]$LogJson
    )

    if (Test-Path -LiteralPath $LogJson) {
        Remove-Item -LiteralPath $LogJson -Force
    }

    $logDir = Split-Path -Parent $LogJson
    $logName = Split-Path -Leaf $LogJson
    $filter = "libvmaf=log_fmt=json:log_path=$logName"
    $args = @(
        "-hide_banner",
        "-y",
        "-i", $EncodedClip,
        "-i", $ReferenceClip,
        "-lavfi", $filter,
        "-f", "null",
        "NUL"
    )

    Push-Location $logDir
    try {
        & $FFmpeg @args
        if ($LASTEXITCODE -ne 0) {
            throw "VMAF-Berechnung fehlgeschlagen."
        }
    }
    finally {
        Pop-Location
    }

    if (-not (Test-Path -LiteralPath $LogJson)) {
        throw "VMAF-Log wurde nicht erstellt: $LogJson"
    }

    $json = Get-Content -LiteralPath $LogJson -Raw | ConvertFrom-Json
    $mean = [double]$json.pooled_metrics.vmaf.mean
    return [Math]::Round($mean, 3)
}

function Find-QualityValueNvenc {
    param(
        [string]$Nvenc,
        [string]$FFmpeg,
        [string]$SampleClip,
        [string]$Codec,
        [string]$WorkDir,
        [double]$TargetVmaf = 97.0,
        [double]$LowerBound = 96.5,
        [double]$UpperBound = 97.5,
        [int]$SampleDurationSeconds = 180
    )

    $maxQvbr = if ($Codec -eq "av1") { 34 } else { 30 }
    $qvbr = if ($Codec -eq "av1") { 26 } else { 22 }
    $steps = @(4, 2, 1)
    $attempts = @()
    $speedFactors = @()
    $lastVmaf = $null

    foreach ($step in $steps) {
        $encoded = Join-Path $WorkDir ("test_qvbr_" + $qvbr + ".mkv")
        $vmafLog = Join-Path $WorkDir ("vmaf_qvbr_" + $qvbr + ".json")

        Write-Info "Teste QVBR=$qvbr (Step=$step)..."
        $nvencResult = Encode-TestNvenc -Nvenc $Nvenc -InputClip $SampleClip -OutputClip $encoded -Codec $Codec -Qvbr $qvbr -SampleDurationSeconds $SampleDurationSeconds

        if ($nvencResult.SpeedFactor -gt 0) {
            $speedFactors += [double]$nvencResult.SpeedFactor
        }

        if ($nvencResult.UsedFallback) {
            $vmaf = Get-VmafScore -FFmpeg $FFmpeg -EncodedClip $encoded -ReferenceClip $SampleClip -LogJson $vmafLog
        }
        else {
            $vmaf = [double]$nvencResult.Vmaf
        }

        $attempts += [pscustomobject]@{
            Qvbr = $qvbr
            Vmaf = $vmaf
            SpeedFactor = if ($nvencResult.SpeedFactor -gt 0) { [Math]::Round([double]$nvencResult.SpeedFactor, 3) } else { $null }
        }
        $lastVmaf = $vmaf

        Write-Info "VMAF Ergebnis: $vmaf"

        if ($vmaf -ge $LowerBound -and $vmaf -le $UpperBound) {
            Write-Info "Treffer im Zielfenster ($LowerBound - $UpperBound)."
            $avgSpeed = if ($speedFactors.Count -gt 0) { [double](($speedFactors | Measure-Object -Average).Average) } else { 0.0 }
            return [pscustomobject]@{
                Qvbr = $qvbr
                Attempts = $attempts
                SpeedFactor = $avgSpeed
            }
        }

        if ($vmaf -gt $UpperBound) {
            $qvbr += $step
        } elseif ($vmaf -lt $LowerBound) {
            $qvbr -= $step
        }

        $qvbr = [Math]::Max(1, [Math]::Min($maxQvbr, $qvbr))
    }

    # Falls nach 4-2-1 noch ueber dem Zielkorridor: in 2er-Schritten weiter erhoehen bis Treffer oder Safety-Cap.
    if ($null -ne $lastVmaf -and $lastVmaf -gt $UpperBound -and $qvbr -lt $maxQvbr) {
        Write-Warn "VMAF nach 4-2-1 weiterhin ueber Zielkorridor. Erhoehe QVBR dynamisch in 2er-Schritten bis max $maxQvbr."

        while ($qvbr -lt $maxQvbr) {
            $qvbr = [Math]::Min($maxQvbr, $qvbr + 2)

            $encoded = Join-Path $WorkDir ("test_qvbr_" + $qvbr + ".mkv")
            $vmafLog = Join-Path $WorkDir ("vmaf_qvbr_" + $qvbr + ".json")

            Write-Info "Dynamischer Nachlauf: Teste QVBR=$qvbr (Step=2)..."
            $nvencResult = Encode-TestNvenc -Nvenc $Nvenc -InputClip $SampleClip -OutputClip $encoded -Codec $Codec -Qvbr $qvbr -SampleDurationSeconds $SampleDurationSeconds

            if ($nvencResult.SpeedFactor -gt 0) {
                $speedFactors += [double]$nvencResult.SpeedFactor
            }

            if ($nvencResult.UsedFallback) {
                $vmaf = Get-VmafScore -FFmpeg $FFmpeg -EncodedClip $encoded -ReferenceClip $SampleClip -LogJson $vmafLog
            }
            else {
                $vmaf = [double]$nvencResult.Vmaf
            }

            $attempts += [pscustomobject]@{
                Qvbr = $qvbr
                Vmaf = $vmaf
                SpeedFactor = if ($nvencResult.SpeedFactor -gt 0) { [Math]::Round([double]$nvencResult.SpeedFactor, 3) } else { $null }
            }
            $lastVmaf = $vmaf

            Write-Info "VMAF Ergebnis: $vmaf"

            if ($vmaf -ge $LowerBound -and $vmaf -le $UpperBound) {
                Write-Info "Treffer im Zielfenster ($LowerBound - $UpperBound)."
                $avgSpeed = if ($speedFactors.Count -gt 0) { [double](($speedFactors | Measure-Object -Average).Average) } else { 0.0 }
                return [pscustomobject]@{
                    Qvbr = $qvbr
                    Attempts = $attempts
                    SpeedFactor = $avgSpeed
                }
            }
        }

        if ($qvbr -ge $maxQvbr) {
            Write-Warn "Safety-Cap erreicht (QVBR=$maxQvbr). Verwende Max-Wert als Sicherheitsgrenze."
            $avgSpeed = if ($speedFactors.Count -gt 0) { [double](($speedFactors | Measure-Object -Average).Average) } else { 0.0 }
            return [pscustomobject]@{
                Qvbr = $maxQvbr
                Attempts = $attempts
                SpeedFactor = $avgSpeed
            }
        }
    }

    $closest = $attempts | Sort-Object { [Math]::Abs($_.Vmaf - $TargetVmaf) } | Select-Object -First 1
    Write-Warn "Kein exakter Treffer im Zielfenster. Nutze naechsten Wert (QVBR=$($closest.Qvbr), VMAF=$($closest.Vmaf))."

    return [pscustomobject]@{
        Qvbr = [int]$closest.Qvbr
        Attempts = $attempts
        SpeedFactor = if ($speedFactors.Count -gt 0) { [double](($speedFactors | Measure-Object -Average).Average) } else { 0.0 }
    }
}

function Find-QualityValueCpu {
    param(
        [string]$FFmpeg,
        [string]$SampleClip,
        [string]$Codec,
        [string]$WorkDir,
        [int]$SampleDurationSeconds = 180
    )

    $target = 97.0
    $lower = 96.5
    $upper = 97.5

    $value = if ($Codec -eq "av1") { 27 } else { 22 }
    $steps = @(4, 2, 1)
    $attempts = @()
    $speedFactors = @()

    foreach ($step in $steps) {
        $encoded = Join-Path $WorkDir ("test_cpu_" + $value + ".mkv")
        $vmafLog = Join-Path $WorkDir ("vmaf_cpu_" + $value + ".json")

        if (Test-Path -LiteralPath $encoded) {
            Remove-Item -LiteralPath $encoded -Force
        }

        Write-Info "CPU-Testwert=$value (Step=$step)..."

        if ($Codec -eq "av1") {
            $args = @(
                "-hide_banner", "-y",
                "-i", $SampleClip,
                "-map", "0:v:0",
                "-c:v", "libsvtav1",
                "-pix_fmt", "yuv420p10le",
                "-preset", "4",
                "-crf", $value,
                "-an",
                $encoded
            )
        } else {
            $args = @(
                "-hide_banner", "-y",
                "-i", $SampleClip,
                "-map", "0:v:0",
                "-c:v", "libx265",
                "-preset", "slow",
                "-pix_fmt", "yuv420p10le",
                "-crf", $value,
                "-an",
                $encoded
            )
        }

        $measure = Measure-Command {
            & $FFmpeg @args
        }
        if ($LASTEXITCODE -ne 0) {
            throw "CPU-Testencode fehlgeschlagen (Wert=$value)."
        }

        $sampleEncodeTime = [double]$measure.TotalSeconds
        $speedFactor = 0.0
        if ($sampleEncodeTime -gt 0) {
            $speedFactor = [double]$SampleDurationSeconds / $sampleEncodeTime
            $speedFactors += $speedFactor
        }

        $vmaf = Get-VmafScore -FFmpeg $FFmpeg -EncodedClip $encoded -ReferenceClip $SampleClip -LogJson $vmafLog
        $attempts += [pscustomobject]@{
            Value = $value
            Vmaf = $vmaf
            SpeedFactor = if ($speedFactor -gt 0) { [Math]::Round($speedFactor, 3) } else { $null }
        }

        Write-Info "VMAF Ergebnis: $vmaf"

        if ($vmaf -ge $lower -and $vmaf -le $upper) {
            $avgSpeed = if ($speedFactors.Count -gt 0) { [double](($speedFactors | Measure-Object -Average).Average) } else { 0.0 }
            return [pscustomobject]@{
                Value = $value
                Attempts = $attempts
                SpeedFactor = $avgSpeed
            }
        }

        if ($vmaf -gt $upper) {
            $value += $step
        } elseif ($vmaf -lt $lower) {
            $value -= $step
        }

        $value = [Math]::Max(1, [Math]::Min(51, $value))
    }

    $closest = $attempts | Sort-Object { [Math]::Abs($_.Vmaf - $target) } | Select-Object -First 1
    return [pscustomobject]@{
        Value = [int]$closest.Value
        Attempts = $attempts
        SpeedFactor = if ($speedFactors.Count -gt 0) { [double](($speedFactors | Measure-Object -Average).Average) } else { 0.0 }
    }
}

function Encode-FinalNvenc {
    param(
        [string]$Nvenc,
        [string]$InputFile,
        [string]$OutputFile,
        [string]$Codec,
        [int]$Qvbr,
        [string]$AiChoice
    )

    $args = @()
    $args += Get-NvencBaseArgs -Codec $Codec -Qvbr $Qvbr
    $args += Get-AiModeArgs -AiChoice $AiChoice
    $args += @(
        "--vpp-subburn", "track=1,forced_subs_only=on",
        "--chapter-copy",
        "--audio-copy",
        "-i", $InputFile,
        "-o", $OutputFile
    )

    $result = Invoke-NvencSync -NvencPath $Nvenc -Arguments $args -PersistFullOutputOnTrim
    if ($result.ExitCode -ne 0) {
        $tail = ($result.OutputLines | Select-Object -Last 12) -join [Environment]::NewLine
        $fullLogHint = if ($result.FullOutputLogPath) { "`nVollstaendiges NVEncC-Log: $($result.FullOutputLogPath)" } else { "" }
        throw "Finaler NVEncC Encode fehlgeschlagen.`n$tail$fullLogHint"
    }
}

function Encode-FinalCpu {
    param(
        [string]$FFmpeg,
        [string]$InputFile,
        [string]$OutputFile,
        [string]$Codec,
        [int]$QualityValue
    )

    Write-Warn "CPU-Fallback aktiv: Forced-Only Subburn wie in NVEncC ist in ffmpeg nicht 1:1 verfuegbar. Es wird Spur 1 direkt eingebrannt."

    $subtitlePath = $InputFile.Replace("\", "\\").Replace(":", "\:").Replace("'", "\'")
    $subtitleFilter = "subtitles='$subtitlePath':si=0"

    if ($Codec -eq "av1") {
        $args = @(
            "-hide_banner", "-y",
            "-i", $InputFile,
            "-map", "0",
            "-vf", $subtitleFilter,
            "-c:v", "libsvtav1",
            "-pix_fmt", "yuv420p10le",
            "-preset", "4",
            "-crf", $QualityValue,
            "-c:a", "copy",
            "-c:s", "copy",
            $OutputFile
        )
    } else {
        $args = @(
            "-hide_banner", "-y",
            "-i", $InputFile,
            "-map", "0",
            "-vf", $subtitleFilter,
            "-c:v", "libx265",
            "-preset", "slow",
            "-pix_fmt", "yuv420p10le",
            "-crf", $QualityValue,
            "-c:a", "copy",
            "-c:s", "copy",
            $OutputFile
        )
    }

    & $FFmpeg @args
    if ($LASTEXITCODE -ne 0) {
        throw "Finaler CPU-Encode fehlgeschlagen."
    }
}

function Format-TimeCode {
    param([double]$TotalSeconds)

    if ($TotalSeconds -lt 0) {
        $TotalSeconds = 0
    }

    $ts = [TimeSpan]::FromSeconds([int][Math]::Round($TotalSeconds))
    return "{0:D2}:{1:D2}:{2:D2}" -f [int]$ts.Hours, [int]$ts.Minutes, [int]$ts.Seconds
}

function Write-RunSummaryLog {
    param(
        [string]$SummaryPath,
        [string]$InputFile,
        [string]$CodecTag,
        [string]$Engine,
        [string]$ModeName,
        [pscustomobject]$Peak,
        [object[]]$Iterations,
        [string]$IterationLabel,
        [string]$FinalValueLabel,
        [int]$FinalValue,
        [string]$OutputFile,
        [string]$FinalParams,
        [double]$NoiseDeltaPercent = [double]::NaN,
        [bool]$NoiseDetected = $false,
        [long]$NoiseRawBytes = -1,
        [long]$NoiseDenoisedBytes = -1,
        [double]$TargetVmaf = [double]::NaN,
        [double]$LowerBound = [double]::NaN,
        [double]$UpperBound = [double]::NaN
    )

    $lines = @()
    $lines += "Run Timestamp: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    $lines += "Input: $InputFile"
    $lines += "Engine: $Engine"
    $lines += "Codec: $CodecTag"
    $lines += "Mode: $ModeName"
    $lines += "Peak Window: start=$(Format-TimeCode -TotalSeconds $Peak.StartSeconds) ($($Peak.StartSeconds)s), duration=$($Peak.WindowSeconds)s, avg_mbps=$($Peak.AvgMbps)"
    $lines += ""
    $lines += "Iterations ($IterationLabel -> VMAF):"

    foreach ($entry in $Iterations) {
        $value = $null
        if ($entry.PSObject.Properties.Match("Qvbr").Count -gt 0) {
            $value = $entry.Qvbr
        }
        elseif ($entry.PSObject.Properties.Match("Value").Count -gt 0) {
            $value = $entry.Value
        }

        $vmaf = if ($entry.PSObject.Properties.Match("Vmaf").Count -gt 0) { $entry.Vmaf } else { "n/a" }
        $lines += "  $value -> $vmaf"
    }

    if (-not [double]::IsNaN($NoiseDeltaPercent)) {
        $noiseState = if ($NoiseDetected) { "yes" } else { "no" }
        $lines += ""
        $lines += "Pre-Flight Noise Analysis:"
        $lines += "  Delta Percent: $NoiseDeltaPercent%"
        $lines += "  Noise Detected: $noiseState"
        if ($NoiseRawBytes -ge 0) {
            $lines += "  noise_raw.mkv bytes: $NoiseRawBytes"
        }
        if ($NoiseDenoisedBytes -ge 0) {
            $lines += "  noise_denoised.mkv bytes: $NoiseDenoisedBytes"
        }
        if ((-not [double]::IsNaN($TargetVmaf)) -and (-not [double]::IsNaN($LowerBound)) -and (-not [double]::IsNaN($UpperBound))) {
            $lines += "  VMAF Target Window: $LowerBound - $UpperBound (target $TargetVmaf)"
        }
    }

    $lines += ""
    $lines += "Final Selection: $FinalValueLabel=$FinalValue"
    $lines += "Output: $OutputFile"
    $lines += "Final Parameters: $FinalParams"

    Set-Content -LiteralPath $SummaryPath -Value $lines -Encoding UTF8
}

function Write-GlobalErrorLog {
    param(
        [string]$InputFile,
        [string]$InputBase,
        [System.Management.Automation.ErrorRecord]$ErrorRecord
    )

    $scriptRoot = Split-Path -Parent $PSCommandPath
    $resultsDir = Join-Path $scriptRoot "Results"
    if (-not (Test-Path -LiteralPath $resultsDir -PathType Container)) {
        New-Item -ItemType Directory -Path $resultsDir | Out-Null
    }

    $baseName = if ([string]::IsNullOrWhiteSpace($InputBase)) { "run" } else { $InputBase }
    $errorLogPath = Join-Path $resultsDir ("{0}_error.log" -f $baseName)

    $safeInput = if ([string]::IsNullOrWhiteSpace($InputFile)) { "n/a" } else { $InputFile }
    $stack = if ([string]::IsNullOrWhiteSpace($ErrorRecord.ScriptStackTrace)) { "n/a" } else { $ErrorRecord.ScriptStackTrace }

    $lines = @(
        "Timestamp: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
        "Input: $safeInput",
        "Message: $($ErrorRecord.Exception.Message)",
        "StackTrace:",
        $stack,
        ""
    )

    Add-Content -LiteralPath $errorLogPath -Value $lines -Encoding UTF8
    return $errorLogPath
}

function Get-MeasuredVmafForSelection {
    param(
        [object[]]$Attempts,
        [string]$ValueProperty,
        [int]$SelectedValue
    )

    if ($null -eq $Attempts -or $Attempts.Count -eq 0) {
        return [double]::NaN
    }

    $match = $Attempts |
        Where-Object {
            $_.PSObject.Properties.Match($ValueProperty).Count -gt 0 -and
            [int]($_.$ValueProperty) -eq $SelectedValue
        } |
        Select-Object -Last 1

    if ($null -eq $match -or $match.PSObject.Properties.Match("Vmaf").Count -eq 0) {
        return [double]::NaN
    }

    return [double]$match.Vmaf
}

function Write-FinalSummaryText {
    param(
        [string]$SummaryPath,
        [string]$InputFile,
        [string]$OutputFile,
        [double]$MeasuredVmaf,
        [string]$QualityLabel,
        [int]$QualityValue,
        [double]$EncodingSpeedFactor,
        [double]$TotalRuntimeSeconds
    )

    $sourceSizeBytes = [double](Get-Item -LiteralPath $InputFile).Length
    $finalSizeBytes = [double](Get-Item -LiteralPath $OutputFile).Length

    $savingPercent = 0.0
    if ($sourceSizeBytes -gt 0) {
        $savingPercent = (($sourceSizeBytes - $finalSizeBytes) / $sourceSizeBytes) * 100.0
    }

    $vmafText = if ([double]::IsNaN($MeasuredVmaf)) { "n/a" } else { $MeasuredVmaf.ToString("F3", [System.Globalization.CultureInfo]::InvariantCulture) }
    $speedText = if ($EncodingSpeedFactor -gt 0) { $EncodingSpeedFactor.ToString("F2", [System.Globalization.CultureInfo]::InvariantCulture) + "x" } else { "n/a" }

    $lines = @(
        "Summary Timestamp: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
        "Input: $InputFile",
        "Output: $OutputFile",
        "Source Size Bytes: $([int64]$sourceSizeBytes)",
        "Final Size Bytes: $([int64]$finalSizeBytes)",
        "Savings Percent: $([Math]::Round($savingPercent, 2))%",
        "Measured VMAF: $vmafText",
        "Selected ${QualityLabel}: $QualityValue",
        "Encoding Speed: $speedText",
        "Total Runtime: $(Format-TimeCode -TotalSeconds $TotalRuntimeSeconds)"
    )

    Set-Content -LiteralPath $SummaryPath -Value $lines -Encoding UTF8
}

try {
    $runStart = Get-Date
    $encodeSucceeded = $false
    $tools = Get-ToolPaths

    $inputFile = Get-InputFile -InputPath $InputPath
    $inputBase = [System.IO.Path]::GetFileNameWithoutExtension($inputFile)
    $script:CurrentInputFile = $inputFile
    $script:CurrentInputBase = $inputBase

    $workBaseDir = $tools.Results
    $workDir = Join-Path $workBaseDir ("{0}_Work" -f $inputBase)
    if (-not (Test-Path -LiteralPath $workDir -PathType Container)) {
        New-Item -ItemType Directory -Path $workDir | Out-Null
    }

    $script:ResultsDir = $tools.Results
    $script:WorkDir = $workDir

    $summaryPath = Join-Path $workDir ("{0}_summary.log" -f $inputBase)
    $finalSummaryPath = Join-Path $tools.Results ("{0}.summary.txt" -f $inputBase)

    Write-Info "Quelle: $inputFile"
    Write-Info "Work-Directory: $workDir"

    $totalDurationSeconds = Get-VideoDurationSeconds -FFprobe $tools.FFprobe -InputFile $inputFile
    Write-Info "Gesamtdauer Quelle: $(Format-TimeCode -TotalSeconds $totalDurationSeconds) ($totalDurationSeconds s)"

    $hasSubtitle = Get-SubtitleTrackIndexZeroExists -FFprobe $tools.FFprobe -InputFile $inputFile
    if (-not $hasSubtitle) {
        throw "Track 1 (Index 0) fuer Untertitel wurde nicht gefunden. Forced-Subburn setzt mindestens eine Untertitelspur voraus."
    }

    $hasNvidia = Test-NvidiaGpu -NvencPath $tools.NVEncC
    if ($hasNvidia) {
        Write-Info "NVIDIA GPU erkannt: NVEncC wird verwendet."
    } else {
        Write-Warn "Keine kompatible NVIDIA GPU erkannt oder nvencc64.exe fehlt. CPU-Fallback (ffmpeg) wird verwendet."
    }

    Write-Host ""
    Write-Host "========= ENCODER MENUE =========" -ForegroundColor Green
    Write-Host "1) HEVC"
    Write-Host "2) AV1"
    $codecChoice = Read-Choice -Prompt "Codec waehlen (1/2)" -Default "1" -Allowed @("1", "2")
    $codec = if ($codecChoice -eq "2") { "av1" } else { "hevc" }
    $codecTag = if ($codec -eq "av1") { "AV1" } else { "HEVC" }

    $aiChoice = "1"
    if ($hasNvidia) {
        Write-Host ""
        Write-Host "===== NVIDIA KI VIDEO OPTIMIERUNG =====" -ForegroundColor Green
        Write-Host "1) Keine KI-Filter (Standard)"
        Write-Host "2) SDR -> HDR10 (TrueHDR)"
        Write-Host "3) DVD2HD AI Upscaling (NGX VRS 1080p)"
        Write-Host "4) DVD2HD + TrueHDR"
        $aiChoice = Read-Choice -Prompt "Modus waehlen (1/2/3/4)" -Default "1" -Allowed @("1", "2", "3", "4")
    }

    $modeName = if ($hasNvidia) { Get-AiModeName -AiChoice $aiChoice } else { "CPU" }

    $peak = Get-PeakWindow -FFprobe $tools.FFprobe -InputFile $inputFile -WindowSeconds 180
    Write-Info "Peak-Fenster: Start=$($peak.StartSeconds)s, Dauer=$($peak.WindowSeconds)s, Avg=$($peak.AvgMbps) Mbps"

    $sampleClip = Join-Path $workDir "test_sample.mkv"
    Cut-TestSample -FFmpeg $tools.FFmpeg -InputFile $inputFile -StartSeconds $peak.StartSeconds -DurationSeconds $peak.WindowSeconds -OutputPath $sampleClip

    if ($hasNvidia) {
        $noiseSample = Join-Path $workDir "noise_sample.mkv"
        Cut-NoiseSample -FFmpeg $tools.FFmpeg -InputFile $inputFile -PeakStartSeconds $peak.StartSeconds -PeakWindowSeconds $peak.WindowSeconds -OutputPath $noiseSample | Out-Null

        $noiseProbe = Test-NoiseLevel -Nvenc $tools.NVEncC -NoiseSample $noiseSample -WorkDir $workDir
        $deltaPercent = [Math]::Round($noiseProbe.Delta * 100.0, 1)

        $targetVmaf = 97.0
        $lowerBound = 96.5
        $upperBound = 97.5

        if ($noiseProbe.NoiseDetected) {
            $targetVmaf = 95.5
            $lowerBound = 95.0
            $upperBound = 96.0
            Write-Info "Delta-Bitraten-Analyse: Delta = $deltaPercent% -> Starkes Rauschen erkannt. VMAF-Zielwert auf 95.5 gesenkt."
        }
        else {
            Write-Info "Delta-Bitraten-Analyse: Delta = $deltaPercent% -> Kein starkes Rauschen erkannt. VMAF-Zielbereich bleibt 96.5-97.5."
        }

        Write-Info "Starte 4-2-1 QVBR Einmessung (ohne KI-Filter)..."
        $sampleDurationSeconds = 180
        $fit = Find-QualityValueNvenc -Nvenc $tools.NVEncC -FFmpeg $tools.FFmpeg -SampleClip $sampleClip -Codec $codec -WorkDir $workDir -TargetVmaf $targetVmaf -LowerBound $lowerBound -UpperBound $upperBound -SampleDurationSeconds $sampleDurationSeconds
        $qvbr = [int]$fit.Qvbr

        Write-Host ""
        Write-Host "QVBR Testhistorie:" -ForegroundColor Green
        foreach ($entry in $fit.Attempts) {
            Write-Host ("  QVBR={0} -> VMAF={1}" -f $entry.Qvbr, $entry.Vmaf)
        }

        $outputName = "{0}_{1}_QVBR{2}_{3}.mkv" -f $inputBase, $codecTag, $qvbr, $modeName
        $outputFileWork = Join-Path $workDir $outputName
        $outputFile = Join-Path $tools.Results $outputName

        $finalArgsPreview = @()
        $finalArgsPreview += Get-NvencBaseArgs -Codec $codec -Qvbr $qvbr
        $finalArgsPreview += Get-AiModeArgs -AiChoice $aiChoice
        $finalArgsPreview += @("--vpp-subburn", "track=1,forced_subs_only=on", "--chapter-copy", "--audio-copy")

        $speedFactor = [double]$fit.SpeedFactor
        if ($speedFactor -gt 0 -and $totalDurationSeconds -gt 0) {
            $estimatedFinalSeconds = [double]$totalDurationSeconds / $speedFactor
            $etaTime = (Get-Date).AddSeconds($estimatedFinalSeconds)
            Write-Info "Geschaetzte Encoding-Geschwindigkeit: $($speedFactor.ToString("F1", [System.Globalization.CultureInfo]::InvariantCulture))x Echtzeit"
            Write-Info "Voraussichtliche Dauer: $(Format-TimeCode -TotalSeconds $estimatedFinalSeconds)"
            Write-Info "Voraussichtlich fertig um: $($etaTime.ToString("HH:mm")) Uhr"
        }
        else {
            Write-Warn "ETA konnte nicht berechnet werden (ungueltiger Speed-Faktor aus Test-Encodes)."
        }

        Write-Info "Finaler Encode startet (Work-Directory): $outputFileWork"
        Encode-FinalNvenc -Nvenc $tools.NVEncC -InputFile $inputFile -OutputFile $outputFileWork -Codec $codec -Qvbr $qvbr -AiChoice $aiChoice

        if (Test-Path -LiteralPath $outputFile) {
            Remove-Item -LiteralPath $outputFile -Force
        }
        Move-Item -LiteralPath $outputFileWork -Destination $outputFile -Force

        Write-RunSummaryLog -SummaryPath $summaryPath -InputFile $inputFile -CodecTag $codecTag -Engine "NVEncC" -ModeName $modeName -Peak $peak -Iterations $fit.Attempts -IterationLabel "QVBR" -FinalValueLabel "QVBR" -FinalValue $qvbr -OutputFile $outputFile -FinalParams ($finalArgsPreview -join ' ') -NoiseDeltaPercent $deltaPercent -NoiseDetected $noiseProbe.NoiseDetected -NoiseRawBytes $noiseProbe.RawSize -NoiseDenoisedBytes $noiseProbe.DenoisedSize -TargetVmaf $targetVmaf -LowerBound $lowerBound -UpperBound $upperBound

        $measuredVmaf = Get-MeasuredVmafForSelection -Attempts $fit.Attempts -ValueProperty "Qvbr" -SelectedValue $qvbr
        $runtimeSeconds = [double]((Get-Date) - $runStart).TotalSeconds
        Write-FinalSummaryText -SummaryPath $finalSummaryPath -InputFile $inputFile -OutputFile $outputFile -MeasuredVmaf $measuredVmaf -QualityLabel "QVBR" -QualityValue $qvbr -EncodingSpeedFactor ([double]$fit.SpeedFactor) -TotalRuntimeSeconds $runtimeSeconds

        $encodeSucceeded = $true

        Write-Info "Fertig. Ausgabe: $outputFile"
        Write-Info "Summary-Datei: $finalSummaryPath"
    }
    else {
        Write-Info "Starte CPU 4-2-1 Qualitaetseinmessung (CRF-basiert)..."
        $sampleDurationSeconds = 180
        $fitCpu = Find-QualityValueCpu -FFmpeg $tools.FFmpeg -SampleClip $sampleClip -Codec $codec -WorkDir $workDir -SampleDurationSeconds $sampleDurationSeconds
        $quality = [int]$fitCpu.Value

        Write-Host ""
        Write-Host "CPU Testhistorie:" -ForegroundColor Green
        foreach ($entry in $fitCpu.Attempts) {
            Write-Host ("  Wert={0} -> VMAF={1}" -f $entry.Value, $entry.Vmaf)
        }

        $outputName = "{0}_{1}_CRF{2}_{3}.mkv" -f $inputBase, $codecTag, $quality, $modeName
        $outputFileWork = Join-Path $workDir $outputName
        $outputFile = Join-Path $tools.Results $outputName

        $cpuParams = if ($codec -eq "av1") {
            "-vf subtitles=... -c:v libsvtav1 -pix_fmt yuv420p10le -preset 4 -crf $quality -c:a copy -c:s copy"
        }
        else {
            "-vf subtitles=... -c:v libx265 -pix_fmt yuv420p10le -preset slow -crf $quality -c:a copy -c:s copy"
        }

        $speedFactor = [double]$fitCpu.SpeedFactor
        if ($speedFactor -gt 0 -and $totalDurationSeconds -gt 0) {
            $estimatedFinalSeconds = [double]$totalDurationSeconds / $speedFactor
            $etaTime = (Get-Date).AddSeconds($estimatedFinalSeconds)
            Write-Info "Geschaetzte Encoding-Geschwindigkeit: $($speedFactor.ToString("F1", [System.Globalization.CultureInfo]::InvariantCulture))x Echtzeit"
            Write-Info "Voraussichtliche Dauer: $(Format-TimeCode -TotalSeconds $estimatedFinalSeconds)"
            Write-Info "Voraussichtlich fertig um: $($etaTime.ToString("HH:mm")) Uhr"
        }
        else {
            Write-Warn "ETA konnte nicht berechnet werden (ungueltiger Speed-Faktor aus Test-Encodes)."
        }

        Write-Info "Finaler CPU Encode startet (Work-Directory): $outputFileWork"
        Encode-FinalCpu -FFmpeg $tools.FFmpeg -InputFile $inputFile -OutputFile $outputFileWork -Codec $codec -QualityValue $quality

        if (Test-Path -LiteralPath $outputFile) {
            Remove-Item -LiteralPath $outputFile -Force
        }
        Move-Item -LiteralPath $outputFileWork -Destination $outputFile -Force

        Write-RunSummaryLog -SummaryPath $summaryPath -InputFile $inputFile -CodecTag $codecTag -Engine "FFmpeg CPU" -ModeName $modeName -Peak $peak -Iterations $fitCpu.Attempts -IterationLabel "CRF" -FinalValueLabel "CRF" -FinalValue $quality -OutputFile $outputFile -FinalParams $cpuParams

        $measuredVmaf = Get-MeasuredVmafForSelection -Attempts $fitCpu.Attempts -ValueProperty "Value" -SelectedValue $quality
        $runtimeSeconds = [double]((Get-Date) - $runStart).TotalSeconds
        Write-FinalSummaryText -SummaryPath $finalSummaryPath -InputFile $inputFile -OutputFile $outputFile -MeasuredVmaf $measuredVmaf -QualityLabel "CRF" -QualityValue $quality -EncodingSpeedFactor ([double]$fitCpu.SpeedFactor) -TotalRuntimeSeconds $runtimeSeconds

        $encodeSucceeded = $true

        Write-Info "Fertig. Ausgabe: $outputFile"
        Write-Info "Summary-Datei: $finalSummaryPath"
    }

    if ($encodeSucceeded -and (Test-Path -LiteralPath $workDir -PathType Container)) {
        Write-Info "Cleanup Work-Directory (temporare Artefakte)..."

        Get-ChildItem -LiteralPath $workDir -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Extension -in @('.mkv', '.nut', '.log', '.json', '.csv') } |
            Remove-Item -Force -ErrorAction SilentlyContinue

        $remaining = Get-ChildItem -LiteralPath $workDir -Force -ErrorAction SilentlyContinue
        if ($null -eq $remaining -or $remaining.Count -eq 0) {
            Remove-Item -LiteralPath $workDir -Force -ErrorAction SilentlyContinue
            Write-Info "Leeres Work-Directory entfernt: $workDir"
        }
    }
}
catch {
    $errorLog = Write-GlobalErrorLog -InputFile $script:CurrentInputFile -InputBase $script:CurrentInputBase -ErrorRecord $_
    Write-Fail $_.Exception.Message
    Write-Warn "Details in Fehler-Log: $errorLog"
    exit 1
}
finally {
    $script:WorkDir = $null
}
