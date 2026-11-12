# End-to-end smoke for the rewritten single-account/two-strategy kernel.
# Uses an isolated database and a deterministic demo price set; never touches
# a production path, never starts a broker connection. The synthetic opt-in is
# set only inside this isolated process environment.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$work = Join-Path $env:TEMP ("astock-v2-rewrite-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path (Join-Path $work "prices") | Out-Null

try {
    $csv = Join-Path $work "prices.csv"
    & python (Join-Path $PSScriptRoot "make_demo_prices.py") --out $csv | Out-Null
    $days = (Import-Csv $csv | Select-Object -ExpandProperty day | Sort-Object -Unique)
    $start = $days[0]
    $end = $days[-1]
    $dayArg = $days -join ","
    & python (Join-Path $root "research\make_price_set.py") --input $csv `
        --out (Join-Path $work "prices\demo.json") --set-id demo-synthetic `
        --pack demo-pack --trust synthetic --coverage-start $start --coverage-end $end `
        --symbols "600000.SH,000001.SZ,600519.SH" --days $dayArg | Out-Null

    $env:ASTOCK_V2_DB = Join-Path $work "v2.sqlite3"
    $env:ASTOCK_PRICE_SETS_DIR = Join-Path $work "prices"
    $env:ASTOCK_V2_ADDR = "127.0.0.1:18781"
    $env:ASTOCK_ALLOW_SYNTHETIC = "1"
    $binary = Join-Path $root "target\debug\astock-v2.exe"
    if (-not (Test-Path $binary)) {
        throw "build first: cargo build --locked"
    }
    $server = Start-Process -FilePath $binary -PassThru -WindowStyle Hidden
    try {
        $base = "http://127.0.0.1:18781"
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            try {
                Invoke-RestMethod "$base/v2/health" | Out-Null
                break
            } catch {
                Start-Sleep -Milliseconds 250
            }
        }
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
        Write-Output ("rewrite smoke ok trades={0} positions={1} sellable={2} equity={3}" -f $ran, $held, $sellable, $account.equity_cents)
    } finally {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    }
} finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item Env:ASTOCK_V2_DB, Env:ASTOCK_PRICE_SETS_DIR, Env:ASTOCK_V2_ADDR, Env:ASTOCK_ALLOW_SYNTHETIC -ErrorAction SilentlyContinue
}
