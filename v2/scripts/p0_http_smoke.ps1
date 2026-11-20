# P0 isolated HTTP smoke: access stays open on loopback and non-loopback.
# No active database or running product process is changed.
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$root = Split-Path -Parent $PSScriptRoot
$work = Join-Path $root ("work\p0-open-http-" + [guid]::NewGuid().ToString("N"))
$null = New-Item -ItemType Directory -Force -Path $work
$bin = $env:ASTOCK_V2_BIN
if (-not $bin) { $bin = Join-Path $root "target\debug\astock-v2.exe" }
if (-not (Test-Path $bin)) { throw "build first; missing executable" }
$names = @("ASTOCK_V2_DB", "ASTOCK_V2_ADDR", "ASTOCK_PRICE_SETS_DIR", "ASTOCK_V2_TOKEN", "ASTOCK_ALLOW_SYNTHETIC")
$previous = @{}
foreach ($name in $names) {
    if (Test-Path "Env:$name") { $previous[$name] = (Get-Item "Env:$name").Value }
}
$cases = @(
    @{ Name = "loopback"; Host = "127.0.0.1"; Legacy = $false },
    @{ Name = "loopback-legacy-env"; Host = "127.0.0.1"; Legacy = $true },
    @{ Name = "nonloopback"; Host = "0.0.0.0"; Legacy = $false },
    @{ Name = "nonloopback-legacy-env"; Host = "0.0.0.0"; Legacy = $true }
)
try {
    foreach ($case in $cases) {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
        $listener.Start()
        $port = $listener.LocalEndpoint.Port
        $listener.Stop()
        $base = "http://127.0.0.1:$port"
        $caseDir = Join-Path $work $case.Name
        $null = New-Item -ItemType Directory -Force -Path $caseDir
        $env:ASTOCK_V2_DB = Join-Path $caseDir "account.sqlite3"
        $env:ASTOCK_V2_ADDR = "$($case.Host):$port"
        $env:ASTOCK_PRICE_SETS_DIR = Join-Path $caseDir "prices"
        Remove-Item Env:ASTOCK_ALLOW_SYNTHETIC -ErrorAction SilentlyContinue
        Remove-Item Env:ASTOCK_V2_TOKEN -ErrorAction SilentlyContinue
        $ignored = [guid]::NewGuid().ToString("N")
        if ($case.Legacy) { $env:ASTOCK_V2_TOKEN = $ignored }
        $out = Join-Path $caseDir "server.stdout"
        $err = Join-Path $caseDir "server.stderr"
        $server = Start-Process -FilePath $bin -PassThru -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err
        try {
            $ready = $false
            for ($i = 0; $i -lt 80; $i++) {
                if ($server.HasExited) { throw "isolated server exited before readiness: $($case.Name)" }
                try {
                    $health = Invoke-WebRequest -Uri "$base/v2/health" -UseBasicParsing -TimeoutSec 2
                    if ($health.StatusCode -eq 200) { $ready = $true; break }
                } catch { Start-Sleep -Milliseconds 100 }
            }
            if (-not $ready) { throw "server not ready: $($case.Name)" }
            $created = Invoke-WebRequest -Uri "$base/v2/account" -Method Post -ContentType "application/json" -Body '{"initial_cash_cents":10000000}' -UseBasicParsing
            if ($created.StatusCode -ne 200) { throw "account creation was blocked: $($case.Name)" }
            $account = Invoke-WebRequest -Uri "$base/v2/account" -UseBasicParsing
            if (($account.Content | ConvertFrom-Json).initial_cash_cents -ne 10000000) { throw "account principal mismatch" }
            $duplicate = Invoke-WebRequest -Uri "$base/v2/account" -Method Post -ContentType "application/json" -Body '{"initial_cash_cents":10000000}' -UseBasicParsing -SkipHttpErrorCheck
            if ($duplicate.StatusCode -ne 409) { throw "duplicate account protection changed" }
            $duplicateBody = $duplicate.Content | ConvertFrom-Json
            if ($duplicateBody.error.code -ne "account_already_exists") { throw "structured error missing" }
            $run = Invoke-WebRequest -Uri "$base/v2/account/run" -Method Post -ContentType "application/json" -Body '{}' -UseBasicParsing -SkipHttpErrorCheck
            if ($run.StatusCode -ne 422) { throw "missing data did not reject settlement" }
            if (($run.Content | ConvertFrom-Json).error.code -ne "no_price_data") { throw "missing-price error changed" }
            foreach ($logPath in @($out, $err)) {
                if ((Test-Path $logPath) -and (Get-Content -LiteralPath $logPath -Raw).Contains($ignored)) { throw "ignored environment value appeared in logs" }
            }
            Write-Output "PASS $($case.Name): health=200 create=200 read=200 duplicate=409 missing-data=422"
        } finally {
            Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Output "p0 open HTTP smoke: 4 cases passed; no Authorization header supplied"
} finally {
    foreach ($name in $names) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
        if ($previous.ContainsKey($name)) { Set-Item "Env:$name" $previous[$name] }
    }
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
