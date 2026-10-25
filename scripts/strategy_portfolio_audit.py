"""Read-only ledger inventory for the three-strategy redesign (no credentials)."""
import argparse
import json
from pathlib import Path
import sqlite3


def inspect(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    result = {"source": str(path), "tables": {}, "queries": {}}
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("paper_accounts", "paper_nav", "paper_fills", "paper_orders",
                  "paper_orders_archive", "paper_signals", "paper_signals_archive",
                  "paper_archives", "paper_cycles"):
        if table in tables:
            result["tables"][table] = {
                "rows": conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                "columns": [r[1] for r in conn.execute(f"PRAGMA table_info({table})")],
            }
    queries = {
        "accounts": "SELECT id,name,initial_cash,cash,cycle_id,status FROM paper_accounts",
        "cycles": "SELECT id,status,capital,started_at,ended_at FROM paper_cycles ORDER BY id DESC LIMIT 10",
        "nav_coverage": "SELECT account_id,MIN(nav_date) first,MAX(nav_date) last,COUNT(*) n FROM paper_nav GROUP BY account_id",
        "latest_nav": "SELECT * FROM paper_nav WHERE nav_date=(SELECT MAX(nav_date) FROM paper_nav)",
        "filled_orders": "SELECT account_id,side,COUNT(*) n,SUM(realized_pnl) pnl,SUM(fees) fees,MIN(executed_at) first,MAX(executed_at) last FROM paper_orders WHERE status='filled' GROUP BY account_id,side",
        "archives": "SELECT id,cycle_id,reason,LENGTH(snapshot) size,created_at FROM paper_archives ORDER BY id DESC LIMIT 10",
        "nav_history": "SELECT account_id,nav_date,nav,quote_status FROM paper_nav ORDER BY account_id,nav_date",
        "execution_quality": "SELECT account_id,side,execution_status,execution_verified,COUNT(*) n,SUM(realized_pnl) pnl FROM paper_orders WHERE status='filled' GROUP BY account_id,side,execution_status,execution_verified",
        "symbol_results": "SELECT account_id,code,side,COUNT(*) n,SUM(realized_pnl) pnl,SUM(fees) fees FROM paper_orders WHERE status='filled' GROUP BY account_id,code,side",
        "sell_reasons": "SELECT account_id,reason,COUNT(*) n,SUM(realized_pnl) pnl FROM paper_orders WHERE status='filled' AND side='sell' GROUP BY account_id,reason",
        "intraday_actions": "SELECT account_id,action,COUNT(*) n,MIN(observed_at) first,MAX(observed_at) last FROM paper_intraday_observations WHERE action IN ('t_sell','t_rebuy') GROUP BY account_id,action",
    }
    for key, sql in queries.items():
        try:
            result["queries"][key] = [dict(row) for row in conn.execute(sql)]
        except sqlite3.Error as exc:
            result["queries"][key] = {"unavailable": str(exc)}
    conn.close()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-db", required=True)
    args = parser.parse_args()
    print(json.dumps(inspect(args.paper_db), ensure_ascii=True, indent=2))
