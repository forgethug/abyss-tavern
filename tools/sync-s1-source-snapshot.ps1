param(
    [string]$SourceRepo = (Join-Path $PSScriptRoot "..\..\gupiao"),
    [switch]$Check
)
$ErrorActionPreference = "Stop"
$sourcePath = (Resolve-Path -LiteralPath $SourceRepo).Path
$targetPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$scriptPath = Join-Path $PSScriptRoot "sync_s1_portable_source.py"
$syncArgs = @($scriptPath, "--source", $sourcePath, "--target", $targetPath)
if ($Check) { $syncArgs += "--check" }
& python @syncArgs
if ($LASTEXITCODE -ne 0) { throw "S1 portable source export failed ($LASTEXITCODE)" }
