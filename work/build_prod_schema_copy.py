# -*- coding: utf-8 -*-
"""用真实 production DB 的 schema 构造一个**副本**，供 issue #161 probe 使用。

真实库（/app/data_cache/paper_trading.sqlite3）当前 ``historical_tradability_archive``
为 0 行、且停在 schema v16，因此"原来的 24 行场景"在今天的真实数据里已不存在。本脚本
只取真实库的 **schema**（105 条 CREATE 语句）与真实版本号，灌进一个临时库，使 probe
面对的是**生产同款 schema**，而不是测试自建的精简表。

用法::

    python work/build_prod_schema_copy.py <schema.sql> <out.db>
"""

import sqlite3
import sys
from pathlib import Path


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("usage: build_prod_schema_copy.py <schema.sql> <out.db>")
        return 2
    schema_path, out_path = Path(argv[0]), Path(argv[1])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    conn = sqlite3.connect(str(out_path))
    try:
        sql = schema_path.read_text(encoding="utf-8")
        # ``sqlite_sequence`` 是 SQLite 内部表（由 AUTOINCREMENT 自动创建），不能显式建；
        # 真实库的 schema dump 会带上它，复制时必须跳过，否则建库直接语法错误。
        statements = [
            part.strip()
            for part in sql.split("\n\n")
            if part.strip()
            and "CREATE TABLE sqlite_sequence" not in part
        ]
        for statement in statements:
            conn.execute(statement)
        conn.commit()
        # 真实库停在 v16：保留这个版本号，让 probe 真的走一次 v17 迁移。
        conn.execute(
            "INSERT OR REPLACE INTO schema_version(db_name, version, applied_at, description)"
            " VALUES(?,?,?,?)",
            ("paper_trading", 16, "2026-09-17T00:00:00", "probe baseline (production schema)"),
        )
        conn.commit()
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        print(f"built {out_path} with {len(tables)} tables, schema_version=16")
        for name in ("historical_tradability_archive",
                     "tradability_observation_ledger",
                     "tradability_ingestion_runs"):
            present = name in tables
            count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] if present else "n/a"
            print(f"  {name}: present={present} rows={count}")
        link_present = "tradability_archive_observation_links" in tables
        print(f"  tradability_archive_observation_links: present={link_present} (expected False at v16)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
