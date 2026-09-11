from pathlib import Path

path = Path("backend/paper_trading.py")
text = path.read_text(encoding="utf-8")
old = '''    day = _date(asof_date)
    PSS.run_preflight(db_factory=_db, audit=_audit, asof_day=day)
'''
new = '''    PSS.run_preflight(
        db_factory=_db,
        audit=_audit,
        resolve_asof_day=lambda: _date(asof_date),
    )
    day = _date(asof_date)
'''
assert text.count(old) == 1, "expected exactly one extracted preflight call"
path.write_text(text.replace(old, new, 1), encoding="utf-8")
