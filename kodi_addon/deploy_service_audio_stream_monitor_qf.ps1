param(
    [string]$KodiAddonsRoot = "D:\Program Files\Kodi21\portable_data\addons"
)

$sourceAddonDir = Join-Path $PSScriptRoot "service.audio.stream.monitor.qf"
$targetAddonDir = Join-Path $KodiAddonsRoot "service.audio.stream.monitor.qf"

if (-not (Test-Path -LiteralPath $sourceAddonDir)) {
    throw "Source addon directory not found: $sourceAddonDir"
}

if (-not (Test-Path -LiteralPath $KodiAddonsRoot)) {
    throw "Kodi addons root not found: $KodiAddonsRoot"
}

robocopy $sourceAddonDir $targetAddonDir /MIR /R:1 /W:1 /XD __pycache__ .git .idea .vscode /XF *.pyc
$rc = $LASTEXITCODE
if ($rc -ge 8) {
    throw "Robocopy failed with exit code $rc"
}

Write-Host "Deployed addon to: $targetAddonDir"
