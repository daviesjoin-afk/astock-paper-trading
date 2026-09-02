import datetime as dt
import json

import pandas as pd

import data_fetcher as dfc
import selection_runner


def test_fetch_clist_reports_missing_page(monkeypatch):
    monkeypatch.setattr(dfc, "CLIST_HOSTS", ["test-host"])

    def fake_get_json(_url, params, **_kwargs):
        page = int(params["pn"])
        if page == 1:
            return {"data": {"total": 6, "diff": [{"f12": "000001"}, {"f12": "000002"}]}}
        if page == 2:
            return {"data": {"total": 6, "diff": [{"f12": "000003"}, {"f12": "000004"}]}}
        return {"data": {"total": 6, "diff": []}}

    monkeypatch.setattr(dfc, "_get_json", fake_get_json)
    result = dfc._fetch_clist("f12", pages=None, pz=2, return_meta=True)
    assert result["pages_expected"] == 3
    assert result["pages_ok"] == 2
    assert result["failed_pages"] == [3]
    assert result["complete"] is False


def test_full_market_snapshot_fails_closed_on_partial_pages(monkeypatch):
    dfc._mem_cache.clear()
    monkeypatch.setattr(
        dfc,
        "_fetch_clist",
        lambda *args, **kwargs: {
            "rows": [{"f12": str(i), "f2": 1.0, "f3": 0.0} for i in range(4500)],
            "total": 5000,
            "pages_expected": 25,
            "pages_ok": 24,
            "failed_pages": [25],
            "complete": False,
        },
    )
    assert dfc.fetch_market_snapshot(pages=None, allow_disk_fallback=False) == []
    assert dfc._full_snapshot_payload_is_complete({"rows": ["legacy"] * 5000}) is False


def test_flow_map_fails_closed_and_exposes_coverage(monkeypatch):
    monkeypatch.setattr(
        dfc,
        "_fetch_clist",
        lambda *args, **kwargs: {
            "rows": [{"f12": str(i), "f66": 1.0} for i in range(5000)],
            "total": 5500,
            "pages_expected": 28,
            "pages_ok": 27,
            "failed_pages": [28],
            "complete": False,
        },
    )
    assert dfc._fetch_all_flow_map() == {}
    state = dfc.get_flow_fetch_state()
    assert state["complete"] is False
    assert state["coverage_ok"] is False
    assert state["coverage_pct"] < 100.0


def test_kline_save_uses_atomic_manifest_and_persists_entry(monkeypatch, tmp_path):
    kline_dir = tmp_path / "klines"
    kline_dir.mkdir()
    manifest_path = tmp_path / "kline_manifest.json"
    monkeypatch.setattr(dfc, "KLINE_DIR", str(kline_dir))
    monkeypatch.setattr(dfc, "KLINE_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setattr(dfc, "_manifest", None)
    monkeypatch.setattr(dfc, "_manifest_mtime", None)
    monkeypatch.setattr(dfc, "_manifest_dirty", 0)
    monkeypatch.setattr(dfc, "_manifest_pending", {})
    frame = pd.DataFrame(
        [{"open": 1, "close": 1.1, "high": 1.2, "low": 0.9, "volume": 10, "amount": 11}],
        index=pd.to_datetime(["2026-08-24"]),
    )
    frame.attrs.update({"source": "test", "adjustment": "qfq"})
    dfc.save_kline("000001", frame)
    dfc.flush_kline_manifest()
    with manifest_path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    assert payload["stocks"]["000001"]["last_date"] == "2026-08-24"
    assert not list(kline_dir.glob("*.tmp"))
    assert (kline_dir / "000001.csv").exists()


def test_selection_runner_marks_partial_when_one_strategy_fails(monkeypatch):
    monkeypatch.setattr(selection_runner.ST, "ensure_schema", lambda: None)
    monkeypatch.setattr(selection_runner.ST, "update_observations", lambda: {"updated": 0})
    monkeypatch.setattr(selection_runner.M, "_complete_daily_cutoff", lambda: dt.date(2026, 8, 20))
    monkeypatch.setattr(selection_runner.M.U, "refresh_history", lambda **_kwargs: {"status": "up_to_date"})
    monkeypatch.setattr(selection_runner.P, "_rebuild_selection_factor_cache", lambda _target: {"status": "ok"})
    monkeypatch.setattr(selection_runner.S, "STRATEGIES", ["one", "two"])
    monkeypatch.setattr(
        selection_runner.M,
        "_select_uncached",
        lambda strategy, topn: {"strategy": strategy} if strategy == "one" else {"error": "provider"},
    )
    monkeypatch.setattr(selection_runner.ST, "record_run", lambda result, **_kwargs: result)
    result = selection_runner._run_daily(
        now=dt.datetime(2026, 8, 24, 16, 0, 0),
        admission={"allowed": True},
    )
    assert result["status"] == "partial"
    assert [item["strategy"] for item in result["failures"]] == ["two"]
