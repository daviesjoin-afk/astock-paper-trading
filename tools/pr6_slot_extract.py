from pathlib import Path

path = Path("backend/paper_trading.py")
text = path.read_text(encoding="utf-8")
legacy_import = "import entry_lifecycle as ELC"
dispatch_import = "import execution_dispatch as EPD"
assert text.count(legacy_import) == 1, "expected exactly one entry_lifecycle import"
assert text.count(dispatch_import) == 1, "expected exactly one execution_dispatch import"
text = text.replace(legacy_import, "import paper_slot_service as PSS", 1)
text = text.replace(dispatch_import, "", 1)

start_marker = "def run_slot(slot, asof_date=None, force=False):\n"
start = text.index(start_marker)
day_marker = "    day = _date(asof_date)\n"
end = text.index(day_marker, start) + len(day_marker)
legacy = text[start:end]
for required in (
    "ELC.expire_stale_signals",
    "ELC.expire_stale_orders",
    "EPD.run_execution_dispatch",
    '"entry_lifecycle_error"',
    '"execution_dispatch_error"',
):
    assert required in legacy, f"run_slot preflight shape drifted: {required} missing"

replacement = '''def run_slot(slot, asof_date=None, force=False):
    """统一幂等入口；计划任务和页面的“立即检查”都使用同一事务键。"""
    PSS.validate_slot(slot)
    init_db()
    day = _date(asof_date)
    PSS.run_preflight(db_factory=_db, audit=_audit, asof_day=day)
'''
text = text[:start] + replacement + text[end:]
path.write_text(text, encoding="utf-8")
