"""PR4 fixed-clock risk/ledger and real CSV-read comparison.

Run the same absolute script from each checkout root with an output JSON path.
Only temporary fixture ledgers/files are used. freezegun is a probe dependency,
not an application dependency; source invocation counts are not HTTP counts.
"""
from collections import Counter
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys
import traceback
from unittest import mock

import pandas as pd
from freezegun import freeze_time

clock = freeze_time("2026-10-31 09:30:00", tz_offset=8)
clock.start()
sys.path.insert(0, str(Path.cwd() / "backend"))
import data_fetcher as Data
import paper_trading as PT
from test_risk_application_service import _RiskServiceCase

TABLES = (
    "paper_orders", "paper_fills", "paper_accounts", "paper_positions",
    "paper_position_lots", "paper_capital_reservations", "paper_nav",
    "paper_audit", "paper_cycles", "paper_position_risk_state",
    "paper_risk_decisions", "paper_risk_scan_runs", "paper_position_reviews",
    "paper_position_limit_versions",
)
SCENARIOS = (
    "hold", "full_sell", "partial_sell", "stale_quote", "t1", "limit_down",
    "same_code_two_accounts", "missing", "empty", "corrupt",
    "transient_empty", "transient_error", "unused_audit_error",
)


def run(scenario):
    case = _RiskServiceCase()
    case.setUp()
    try:
        case.add_lot(1000, 10.0)
        case.set_sell_quote()
        if scenario == "hold":
            case.set_non_sell_quote()
        elif scenario == "partial_sell":
            case.set_quote(price=10.9, pct=9.0, high=11.0, low=10.8)
        elif scenario == "stale_quote":
            case._inner.quotes_map[case.code]["quote_at"] = str(case.day - dt.timedelta(days=1)) + " 14:50:00"
        elif scenario == "t1":
            case.conn.execute("UPDATE paper_position_lots SET available_date=?", (str(case.day + dt.timedelta(days=1)),))
            case.conn.commit()
        elif scenario == "limit_down":
            case.set_quote(price=9.0, pct=-10.0, high=9.0, low=9.0)
        elif scenario == "same_code_two_accounts":
            case.add_lot_for_account("trend_pullback", case.code, 100, 10.0)

        csv = Path(case._inner.tmp_dir) / "fixture.csv"
        index = pd.date_range(case.day - dt.timedelta(days=130), case.day + dt.timedelta(days=2))
        frame = pd.DataFrame({"open": 10.0, "close": 10.0, "high": 10.5,
                              "low": 9.5, "volume": 1000.0}, index=index)
        frame.to_csv(csv)
        if scenario == "empty":
            frame.iloc[:0].to_csv(csv)
        elif scenario == "corrupt":
            csv.write_text('"unterminated CSV', encoding="utf8")
        attempts, reads = Counter(), []
        csv_reads = 0
        csv_observations = []

        def source(code):
            nonlocal csv_reads
            if str(code) != case.code:
                return frame.copy(deep=True)
            csv_reads += 1
            probe = sqlite3.connect(PT.DB_PATH, timeout=0.01)
            try:
                probe.execute("BEGIN IMMEDIATE")
                outside_writer = True
                probe.rollback()
            except sqlite3.OperationalError:
                outside_writer = False
            finally:
                probe.close()
            csv_observations.append({
                "outside_writer": outside_writer,
                "callers": [entry.name for entry in traceback.extract_stack()[-8:-1]],
            })
            result = pd.read_csv(csv, index_col=0, parse_dates=True)
            result.attrs["source"] = "fixed_csv_fixture"
            return result

        def load(code, cutoff, *, inclusive=True):
            key = (str(code), str(cutoff), inclusive)
            attempts[key] += 1
            probe = sqlite3.connect(PT.DB_PATH, timeout=0.01)
            try:
                probe.execute("BEGIN IMMEDIATE")
                outside_writer = True
                probe.rollback()
            except sqlite3.OperationalError:
                outside_writer = False
            finally:
                probe.close()
            reads.append({"key": key, "outside_writer": outside_writer})
            if scenario == "missing":
                return None
            if not inclusive and attempts[key] == 1:
                if scenario == "transient_empty":
                    return None
                if scenario == "transient_error":
                    raise OSError("transient CSV error")
            if inclusive and scenario == "unused_audit_error":
                raise OSError("unused audit error")
            return Data.completed_kline(code, cutoff, inclusive=inclusive)

        result, error = None, None
        with mock.patch.object(PT, "_completed_kline", side_effect=load), \
             mock.patch.object(Data, "load_cached_kline", side_effect=source):
            try:
                result = case.run_risk()
            except Exception as exc:
                error = {"type": type(exc).__name__, "message": str(exc)}
        with sqlite3.connect(PT.DB_PATH) as conn:
            ledger = {
                table: [dict(zip([item[0] for item in cursor.description], row, strict=True)) for row in cursor]
                for table in TABLES
                for cursor in [conn.execute("SELECT * FROM " + table + " ORDER BY rowid")]
            }
        return {"result": result, "error": error, "ledger": ledger,
                "source": {"loader_calls": len(reads), "csv_reads": csv_reads,
                           "inside_writer": sum(not r["outside_writer"] for r in reads), "reads": reads,
                           "csv_inside_writer": sum(not r["outside_writer"] for r in csv_observations),
                           "csv_observations": csv_observations}}
    finally:
        case.doCleanups()


try:
    output = {scenario: run(scenario) for scenario in SCENARIOS}
    Path(sys.argv[1]).write_text(json.dumps(output, ensure_ascii=False, default=str), encoding="utf8")
    print(f"Captured {len(output)} scenarios, {len(TABLES)} tables per scenario")
finally:
    clock.stop()
