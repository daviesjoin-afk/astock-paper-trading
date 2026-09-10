# Pre-push leak scan (Windows). Exit 1 blocks the push.
$ErrorActionPreference = "Stop"
$repo = if ($args.Count -ge 1) { $args[0] } else { (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path }
python (Join-Path $PSScriptRoot "scan-sensitive-data.py") --repo $repo --scope all
exit $LASTEXITCODE
