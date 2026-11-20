# End-to-end smoke for the rewritten single-account/two-strategy kernel.
# Uses an isolated database and a deterministic demo price set; never touches
# a production path, never starts a broker connection. The synthetic opt-in is
# set only inside this isolated process environment.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$binary = $env:ASTOCK_V2_BIN
if (-not $binary) { $binary = Join-Path $root "target\debug\astock-v2.exe" }
if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) {
    throw "build first: cargo build --locked"
}
$work = Join-Path $root ("work\p0-rewrite-smoke-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path (Join-Path $work "prices") | Out-Null

$names = @("ASTOCK_V2_DB", "ASTOCK_PRICE_SETS_DIR", "ASTOCK_V2_ADDR", "ASTOCK_ALLOW_SYNTHETIC")
$previous = @{}
foreach ($name in $names) {
    if (Test-Path "Env:$name") { $previous[$name] = (Get-Item "Env:$name").Value }
}

try {
    $csv = Join-Path $work "prices.csv"
    & python (Join-Path $PSScriptRoot "make_demo_prices.py") --out $csv | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "demo CSV generation failed (exit $LASTEXITCODE)" }
    $days = (Import-Csv $csv | Select-Object -ExpandProperty day | Sort-Object -Unique)
    $start = $days[0]
    $end = $days[-1]
    $dayArg = $days -join ","
    & $binary price-set --input $csv `
        --out (Join-Path $work "prices\demo.json") --set-id demo-synthetic `
        --pack demo-pack --trust synthetic --coverage-start $start --coverage-end $end `
        --symbols "600000.SH,000001.SZ,600519.SH" --days $dayArg | Out-Null

    if ($LASTEXITCODE -ne 0) { throw "price-set generation failed (exit $LASTEXITCODE)" }

    $env:ASTOCK_V2_DB = Join-Path $work "v2.sqlite3"
    $env:ASTOCK_PRICE_SETS_DIR = Join-Path $work "prices"
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    $env:ASTOCK_V2_ADDR = "127.0.0.1:$port"
    $env:ASTOCK_ALLOW_SYNTHETIC = "1"
    $server = Start-Process -FilePath $binary -PassThru -WindowStyle Hidden
    try {
        $base = "http://$env:ASTOCK_V2_ADDR"
        $ready = $false
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            if ($server.HasExited) { throw "isolated server exited before readiness" }
            try {
                Invoke-RestMethod "$base/v2/health" -TimeoutSec 2 | Out-Null
                $ready = $true
                break
            } catch {
                Start-Sleep -Milliseconds 250
            }
        }
        if (-not $ready) { throw "isolated server not ready" }
        $created = Invoke-RestMethod -Method Post "$base/v2/account" `
            -ContentType "application/json" -Body '{"initial_cash_cents":10000000}'
        if ($created.slots[0].name -ne "稳健" -or $created.slots[1].name -ne "激进") {
            throw "strategy names are wrong"
        }
        if ($created.slots[0].weight_bps -ne 5000 -or $created.slots[1].weight_bps -ne 5000) {
            throw "initial split is not 50/50"
        }
        $ran = 0
        foreach ($day in $days) {
            $result = Invoke-RestMethod -Method Post "$base/v2/account/run" `
                -ContentType "application/json" -Body (@{ day = $day } | ConvertTo-Json)
            $ran += $result.trades.Count
        }
        $account = Invoke-RestMethod "$base/v2/account"
        $sellable = ($account.positions | Measure-Object -Property sellable_quantity -Sum).Sum
        $held = ($account.positions | Measure-Object -Property quantity -Sum).Sum
        if ($ran -lt 1) { throw "no trade was executed" }
        if ($null -eq $account.equity_cents) { throw "valuation is unavailable" }
        $backup = Join-Path $work "backup.sqlite3"
        $restored = Join-Path $work "restored.sqlite3"
        & $binary db backup --db $env:ASTOCK_V2_DB --out $backup | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "online database backup failed (exit $LASTEXITCODE)" }
        & $binary db restore --backup $backup --db $restored | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "database restore failed (exit $LASTEXITCODE)" }
        $verified = & $binary db verify --db $restored
        if ($LASTEXITCODE -ne 0 -or $verified -notmatch "^verify ok schema=\d+ accounts=1 settled_days=$($days.Count)$") {
            throw "restored database verification failed ($verified)"
        }
        Write-Output ("rewrite smoke ok trades={0} positions={1} sellable={2} equity={3}" -f $ran, $held, $sellable, $account.equity_cents)
    } finally {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    }
} finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    foreach ($name in $names) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
        if ($previous.ContainsKey($name)) { Set-Item "Env:$name" $previous[$name] }
    }
}
