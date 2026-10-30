"""Process one durable Dashboard snapshot job, independently of the trading runner."""
import json

import paper_trading as paper


def main():
    paper.init_db()
    result = paper.run_risk_snapshot_refresh_job()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] in {"completed", "idle"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
