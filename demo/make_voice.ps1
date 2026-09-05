# Render each narration segment to its own WAV with the Windows speech engine,
# then report the duration of each. The video is paced to these numbers, so the
# audio has to exist before the footage is recorded.
param(
  [string]$Voice = "Microsoft Zira Desktop",
  [int]$Rate = -1,          # slightly under default; the default gabbles numbers
  [string]$OutDir = "demo/audio"
)

Add-Type -AssemblyName System.Speech
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$segments = Get-Content "demo/narration.json" -Raw | ConvertFrom-Json
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SelectVoice($Voice)
$synth.Rate = $Rate

$manifest = @()
foreach ($seg in $segments) {
  $path = Join-Path $OutDir "$($seg.id).wav"
  $synth.SetOutputToWaveFile($path)
  # A short lead-in and tail keeps the joins from clipping the first syllable.
  $prompt = New-Object System.Speech.Synthesis.PromptBuilder
  $prompt.AppendBreak([System.Speech.Synthesis.PromptBreak]::Small)
  $prompt.AppendText($seg.text)
  $prompt.AppendBreak([System.Speech.Synthesis.PromptBreak]::Medium)
  $synth.Speak($prompt)
  $synth.SetOutputToNull()

  $reader = New-Object System.Media.SoundPlayer $path
  $reader.Load()
  # WAV header: bytes 40-43 hold the data size; divide by the byte rate at 24-27.
  $bytes = [System.IO.File]::ReadAllBytes($path)
  $byteRate = [BitConverter]::ToUInt32($bytes, 28)
  $dataSize = [BitConverter]::ToUInt32($bytes, 40)
  $seconds = [math]::Round($dataSize / $byteRate, 2)

  $manifest += [pscustomobject]@{ id = $seg.id; scene = $seg.scene;
                                  file = $path; seconds = $seconds }
  "{0,-16} {1,6:N2}s  {2}" -f $seg.id, $seconds, $seg.scene
}
$synth.Dispose()

$manifest | ConvertTo-Json -Depth 3 | Set-Content "demo/audio/manifest.json" -Encoding utf8
$total = ($manifest | Measure-Object -Property seconds -Sum).Sum
""
"segments : {0}" -f $manifest.Count
"total    : {0:N1}s  ({1:N0}m {2:N0}s)" -f $total, [math]::Floor($total / 60), ($total % 60)
