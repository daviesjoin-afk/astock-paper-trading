# -*- coding: utf-8 -*-
"""Shadow CLI 端到端冒烟：在临时数据目录里建库、灌事实、跑只读 CLI。

证明三件事：

1. CLI 真的能读真实 SQLite（不是只跑单元测试）；
2. 非法 operator scope 在**打开数据库之前**就被拒绝（exit 2，零 DB 副作用）；
3. 输出同时给出 comparable / not_comparable 与"缺口不算分歧"的口径。

用法::

    PY=<仓库 venv 的 python>   # 本地绝对路径不进仓库（敏感扫描）
    $PY work/pr160_cli_smoke.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
PY = sys.executable

sys.path.insert(0, str(BACKEND))
import tradability_archive as TA  # noqa: E402
import tradability_ingestion as TI  # noqa: E402

NORMAL = {
    "is_listed": True, "is_st": False, "is_suspended": False,
    "has_market_quote": True, "has_trade_volume": True,
}
SESSION = "2024-01-10"


def build_db(data_dir: Path) -> list:
    """建库 + 准备生产侧证据。返回**本脚本创建**的临时文件（结束时清理）。

    ``data_fetcher.KLINE_DIR`` 固定在仓库 ``data_cache/klines``（不受
    ``ASTOCK_DATA_DIR`` 影响），所以日线缓存只能写在那里——它是 gitignored 的运行时
    目录，脚本会精确删除自己创建的那几个文件。
    """
    created: list = []
    data_dir.mkdir(parents=True, exist_ok=True)
    db = data_dir / "paper_trading.sqlite3"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    TA.ensure_schema(conn)
    TI.ensure_ingestion_schema(conn)
    repo = TA.TradabilityArchiveRepository(conn)

    def add(code, **flags):
        payload = {
            "code": code, "session_date": SESSION, "source": "cli_smoke.json",
            # 证据必须在 decision_at（该 session 收盘）**之前**可见，否则归档会
            # 正确地判 unprovable——那是另一类用例，不该是这里的默认。
            "observed_at": f"{SESSION}T09:35:00",
            "effective_at": f"{SESSION}T09:35:00",
            **NORMAL,
        }
        payload.update(flags)
        repo.save(TA.normalize_record(payload))

    add("000001")                                   # 正常 → 两侧一致
    add("600000", is_suspended=True)                # 停牌 → 归档阻断
    add("000002", is_st=None)                       # ST 未知 → archive_unknown
    # 当时不可知（observed_at 在 decision_at 之后）→ archive_unprovable。
    add("600001", observed_at="2024-06-01T15:05:00", effective_at="2024-06-01T15:05:00")
    # 000003 完全不写入 → archive_missing
    conn.commit()
    conn.close()

    # 日线缓存：让**生产侧**真的拿到价格 / 参考价（否则生产判 unproven，
    # 整批都会落成 production_unknown，比不出任何东西）。
    klines = ROOT / "data_cache" / "klines"
    klines.mkdir(parents=True, exist_ok=True)
    for code, prior_close, close in (
        ("000001", 10.0, 10.2), ("600000", 10.0, 10.0),
        ("000002", 10.0, 10.1), ("600001", 10.0, 9.9), ("000003", 10.0, 10.0),
    ):
        path = klines / f"{code}.csv"
        if not path.exists():
            created.append(path)
        path.write_text(
            "date,open,close,high,low,volume,amount\n"
            f"2024-01-09,{prior_close},{prior_close},{prior_close},{prior_close},1000,10000\n"
            f"{SESSION},{close},{close},{close},{close},1200,12000\n",
            encoding="utf-8",
        )
    # 历史证券状态档：给生产侧一个 PIT 正确的 name / risk_flag。
    # 必须显式声明为 historical_archive + complete，否则生产入口会诚实降级成
    # "无历史状态源"（那正是它该做的），生产侧就全判 unproven，比不出任何东西。
    states = {
        "kind": "historical_archive",
        "historical_membership_complete": True,
        "archive_source": "cli-smoke",
        "availability_basis": "session_close",
        "rows": [
            # 生效区间是**半开** ``[effective_from, effective_to)``；缺 ``effective_to``
            # 只覆盖 from 当天，所以这里显式给一个覆盖到未来的上界。
            {"code": code, "name": code, "risk_flag": False,
             "effective_from": "2020-01-01", "effective_to": "2030-01-01"}
            for code in ("000001", "600000", "000002", "600001", "000003")
        ],
    }
    (data_dir / "security_state_history.json").write_text(
        json.dumps(states, ensure_ascii=False), encoding="utf-8"
    )
    return created


def run_cli(data_dir: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(ROOT / "work" / "tradability_shadow_validation.py"), *args],
        cwd=str(ROOT), capture_output=True, text=True,
        env={**os.environ, "ASTOCK_DATA_DIR": str(data_dir),
             "ASTOCK_SECURITY_STATE_ARCHIVE": str(data_dir / "security_state_history.json"),
             "PYTHONPATH": str(BACKEND), "PYTHONDONTWRITEBYTECODE": "1"},
    )


def main() -> int:
    created: list = []
    try:
        return _run(created)
    finally:
        for path in created:
            path.unlink(missing_ok=True)


def _run(created: list) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        created.extend(build_db(data_dir))
        codes = "000001,600000,000002,600001,000003"

        proc = run_cli(data_dir, "--session", SESSION, "--codes", codes, "--json")
        if proc.returncode != 0:
            print(proc.stdout)
            print(proc.stderr)
            return 1
        payload = json.loads(proc.stdout)
        summary = payload["summary"]
        print("=== shadow CLI JSON summary ===")
        for key in (
            "requested", "comparable", "not_comparable", "agree", "disagree",
            "agree_allow", "agree_block",
            "production_allow_archive_block", "production_block_archive_allow",
            "archive_unknown", "archive_unprovable", "archive_missing",
            "production_unknown", "comparison_invalid",
            "comparison_rate", "agreement_rate", "disagreement_rate",
        ):
            print(f"  {key}: {summary[key]}")
        statuses = sorted({c["status"] for c in payload["comparisons"]})
        print(f"  statuses observed: {statuses}")
        print()
        print("=== per (code, side) ===")
        for item in payload["comparisons"]:
            print(
                f"  {item['code']} {item['side']:4s} {item['status']:34s} "
                f"prod={item['production_allowed']} archive={item['archive_allowed']} "
                f"p_reason={item['production_reason']} a_reason={item['archive_reason']}"
            )
        print()

        # 契约断言
        assert summary["requested"] == 10, summary["requested"]  # 5 codes × 2 sides
        assert summary["comparable"] + summary["not_comparable"] == summary["requested"]
        assert summary["agree"] + summary["disagree"] == summary["comparable"]
        assert summary["comparable"] > 0, "整批都不可比 → 这个冒烟证明不了任何比对"
        # 每一类归档缺口都被真实走到，并且**都不进**分歧分母。
        assert summary["archive_unknown"] == 1, summary["archive_unknown"]
        # 归档_unprovable 现在是调用方声明（CLI 不声明，见文件内说明），因此这里
        # 期望 0：没有可见证据一律 archive_missing。
        assert summary["archive_unprovable"] == 0, summary["archive_unprovable"]
        assert summary["archive_missing"] == 4, summary["archive_missing"]
        assert summary["production_unknown"] == 0
        assert summary["agreement_rate"] is not None
        assert summary["disagreement_rate"] is not None
        assert (
            summary["disagree"]
            == summary["production_allow_archive_block"]
            + summary["production_block_archive_allow"]
        ), summary
        for gap in ("archive_unknown", "archive_missing"):
            assert gap in statuses, (gap, statuses)
        # 卖出方向不得出现 T+1 造成的假分歧：CLI 不声明 entry_session，因此
        # 任何 ``t1_not_sellable`` 都说明它又伪造了同日入场。
        sell_reasons = {
            item["production_reason"]
            for item in payload["comparisons"]
            if item["side"] == "sell"
        }
        assert "t1_not_sellable" not in sell_reasons, sell_reasons
        # 归档侧停牌在买卖两侧都是 block（停牌与方向无关）。
        suspended = [
            item for item in payload["comparisons"]
            if item["archive_reason"] == "suspended"
        ]
        assert len(suspended) == 2, suspended
        assert all(item["archive_allowed"] is False for item in suspended), suspended

        print()
        print("=== 非法 operator scope（必须在打开 DB 之前拒绝） ===")
        for label, args in (
            ('--codes ","', ["--session", SESSION, "--codes", ","]),
            ("zero-session range", ["--from", "2026-09-12", "--to", "2026-09-13"]),
            ("reversed range", ["--from", "2026-09-18", "--to", "2026-09-14"]),
            ("malformed date", ["--session", "2026-13-99"]),
        ):
            bad = run_cli(data_dir, *args)
            first_line = (bad.stdout or bad.stderr).strip().splitlines()[:1]
            print(f"  {label:22s} exit={bad.returncode}  {first_line}")
            assert bad.returncode == 2, (label, bad.returncode)
            assert "数据库不存在" not in (bad.stdout or ""), label

        # 零 DB 副作用：非法 scope 之后数据库内容不变。
        before = _db_digest(data_dir)
        run_cli(data_dir, "--session", SESSION, "--codes", ",")
        run_cli(data_dir, "--from", "2026-09-12", "--to", "2026-09-13")
        assert before == _db_digest(data_dir), "非法 scope 改变了数据库"

        # 只读证明：正常跑一次之后数据库内容同样不变。
        run_cli(data_dir, "--session", SESSION, "--codes", codes)
        assert before == _db_digest(data_dir), "shadow CLI 改写了数据库"
        print("  数据库内容在 CLI 运行前后完全一致（只读）")

    print()
    print("cli smoke: OK")
    return 0


def _db_digest(data_dir: Path) -> str:
    db = data_dir / "paper_trading.sqlite3"
    return f"{db.stat().st_size}:{hash(db.read_bytes())}"


if __name__ == "__main__":
    raise SystemExit(main())
