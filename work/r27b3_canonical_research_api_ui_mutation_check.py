#!/usr/bin/env python3
"""Source-level mutation matrix for R27-B3 API, UI, and compatibility boundaries."""
from __future__ import annotations

import hashlib
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = (
    "backend/api_adaptive.py",
    "backend/ai_analysis.py",
    "backend/ai_research_repository.py",
    "backend/deepseek_advisor.py",
    "frontend/src/features/adaptive.js",
)


def _load_sources() -> dict[str, str]:
    return {
        name: open(os.path.join(ROOT, name), encoding="utf-8").read()
        for name in FILES
    }


def _replace_once(source: str, old: str, new: str) -> str:
    if old not in source:
        raise AssertionError(f"mutation anchor not found: {old!r}")
    return source.replace(old, new, 1)


def _function(source: str, name: str) -> str:
    marker = f"def {name}("
    start = source.find(marker)
    if start < 0:
        return ""
    next_function = source.find("\ndef ", start + len(marker))
    next_async = source.find("\nasync def ", start + len(marker))
    ends = [position for position in (next_function, next_async) if position >= 0]
    return source[start:min(ends)] if ends else source[start:]


def _js_function(source: str, signature: str) -> str:
    start = source.find(signature)
    if start < 0:
        return ""
    next_export = source.find("\nexport ", start + len(signature))
    return source[start:next_export] if next_export >= 0 else source[start:]


def architecture_violations(sources: dict[str, str]) -> list[str]:
    api = sources["backend/api_adaptive.py"]
    timeline = _function(sources["backend/ai_analysis.py"], "timeline")
    repository = sources["backend/ai_research_repository.py"]
    advisor = sources["backend/deepseek_advisor.py"]
    frontend = sources["frontend/src/features/adaptive.js"]
    list_api = _function(api, "canonical_research_runs")
    detail_api = _function(api, "canonical_research_run")
    read_connection = _function(api, "_canonical_research_connection")
    ui_status = _js_function(frontend, "export function adaptiveResearchStatusLabel(")
    ui_evidence = _js_function(frontend, "export function adaptiveResearchEvidenceHtml(")
    ui_confidence = _js_function(frontend, "export function adaptiveResearchConfidencePercent(")
    ui_render = _js_function(frontend, "export function renderAdaptive(")

    problems = []
    if "repository.recent_runs" not in list_api or re_search_sql(list_api):
        problems.append("B3-G01 list must use repository only")
    if "repository.get_run" not in detail_api or re_search_sql(detail_api):
        problems.append("B3-G02 detail must use repository only")
    if any(token in list_api + detail_api for token in (
        "run_research", "run_review", "run_suite", "provider", "urlopen", "requests",
    )):
        problems.append("B3-G03 GET triggered a provider or research execution path")
    if "PRAGMA query_only=ON" not in read_connection or re_search_sql(read_connection):
        problems.append("B3-G04 GET connection must be query-only")
    if "_canonical_research_display" in advisor:
        problems.append("B3-G05 legacy canonical projection must be absent")
    if sql_mentions_legacy_advisor(advisor):
        problems.append("B3-G06 adaptive_advisor_runs production reader/writer remains")
    if "latest_by_purpose" in frontend or "deepseek.latest" in frontend:
        problems.append("B3-G07 frontend still depends on advisor research projection")
    if "repository.get_run" in timeline or "canonical_research" in timeline:
        problems.append("B3-G08 operational timeline embeds canonical conclusion")
    if 'item["canonical_run_id"] = run_id' not in timeline or 'item["result"] = None' not in timeline:
        problems.append("B3-G09 legacy result is not stripped into an unavailable operational reference")
    if "supported:'支持（研究假设）'" not in ui_status or any(
        token in ui_status.lower() for token in ("approved", "actionable", "should_trade", "可以执行")
    ):
        problems.append("B3-G10 supported status was presented as authority or approval")
    flat_fields = (
        "item.relation", "item.source_type", "item.source_id", "item.as_of",
        "item.verification", "item.verification_method", "item.cross_source_verified",
    )
    if "item.evidence" in ui_evidence or any(field not in ui_evidence for field in flat_fields):
        problems.append("B3-G19 evidence renderer does not consume the flat canonical projection")
    if (
        "item.cross_source_verified===true" not in ui_evidence
        or "verification==='verified'" in ui_evidence
    ):
        problems.append("B3-G11 verification status was treated as cross-source verification")
    if "item.source_type!=='market_data'" not in ui_evidence or "cross='不适用'" not in ui_evidence:
        problems.append("B3-G20 non-market evidence treats market-only cross-source status as applicable")
    if "number*100" not in ui_confidence or re_search_confidence_conversion(list_api + detail_api):
        problems.append("B3-G12 confidence contract is not raw API and presentation-only percent")
    if "le=200" not in list_api or "ge=1" not in list_api:
        problems.append("B3-G13 canonical list limit is not bounded")
    if (
        "except repository.ResearchPersistenceError as exc" not in list_api + detail_api
        or "detail=exc.reason" not in list_api + detail_api
    ):
        problems.append("B3-G14 corrupt rows do not fail closed")
    if 'raise HTTPException(status_code=404, detail="research_run_not_found")' not in detail_api:
        problems.append("B3-G15 missing run does not return 404")
    if "ORDER BY id DESC LIMIT ?" not in repository or "ORDER BY created_at DESC" in repository:
        problems.append("B3-G16 canonical list no longer follows append order")
    if "legacy_adaptive_advisor_runs" in frontend or "latest_by_purpose" in frontend:
        problems.append("B3-G17 old advisor history remains a frontend fallback")
    if "\n  refreshAdaptiveResearchHistory();\n}" not in ui_render:
        problems.append("B3-G18 adaptive rerender leaves canonical research history stale")
    return problems


def re_search_sql(source: str) -> bool:
    import re

    return bool(re.search(r"\b(?:SELECT|INSERT|UPDATE|DELETE|REPLACE|UPSERT)\b", source, re.I))


def re_search_confidence_conversion(source: str) -> bool:
    import re

    return bool(re.search(r"\bconfidence\w*\s*=.*\*\s*100", source, re.I))


def sql_mentions_legacy_advisor(source: str) -> bool:
    import re

    return bool(re.search(
        r"(?is)\b(?:SELECT|INSERT\s+INTO|CREATE\s+TABLE|UPDATE|DELETE\s+FROM)\b"
        r"[^;\n]{0,180}adaptive_advisor_runs",
        source,
    ))


def _sha(sources: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name, value in sorted(sources.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("utf-8"))
    return digest.hexdigest()


def main() -> int:
    originals = _load_sources()
    before = _sha(originals)
    baseline = architecture_violations(originals)
    if baseline:
        print(f"baseline: RED ({'; '.join(baseline)})")
        return 1
    print("baseline: GREEN")

    cases = [
        ("M-B3-01", "list API reads legacy table", "backend/api_adaptive.py",
         lambda s: _replace_once(s, "repository.recent_runs(", 'conn.execute("SELECT * FROM adaptive_advisor_runs"); repository.recent_runs(')),
        ("M-B3-02", "detail API reads legacy table", "backend/api_adaptive.py",
         lambda s: _replace_once(s, "run = repository.get_run(conn, run_id)", 'run = conn.execute("SELECT * FROM adaptive_advisor_runs WHERE id=?", (run_id,)).fetchone()')),
        ("M-B3-03", "compatibility projection restored", "backend/deepseek_advisor.py",
         lambda s: s + "\ndef _canonical_research_display(row):\n    return dict(row)\n"),
        ("M-B3-04", "frontend reads latest_by_purpose", "frontend/src/features/adaptive.js",
         lambda s: _replace_once(s, "export function adaptiveResearchHistoryHtml(payload){", "var advisorRuns=deepseek.latest_by_purpose||{};\nexport function adaptiveResearchHistoryHtml(payload){")),
        ("M-B3-05", "timeline embeds canonical conclusion", "backend/ai_analysis.py",
         lambda s: _replace_once(s, 'item["canonical_run_id"] = run_id', 'item["canonical_research"] = repository.get_run(conn, run_id)\n            item["canonical_run_id"] = run_id')),
        ("M-B3-06", "legacy result is promoted", "backend/ai_analysis.py",
         lambda s: _replace_once(s, 'item["result"] = None', 'item["canonical_research"] = item["result"]\n            item["result"] = None')),
        ("M-B3-07", "supported becomes approved", "frontend/src/features/adaptive.js",
         lambda s: _replace_once(s, "supported:'支持（研究假设）'", "supported:'已批准'")),
        ("M-B3-08", "supported becomes actionable", "frontend/src/features/adaptive.js",
         lambda s: _replace_once(s, "supported:'支持（研究假设）'", "supported:'可以执行（actionable）'")),
        ("M-B3-09", "verified implies cross-source", "frontend/src/features/adaptive.js",
         lambda s: _replace_once(s, "item.cross_source_verified===true", "item.verification==='verified'")),
        ("M-B3-10", "API multiplies confidence", "backend/api_adaptive.py",
         lambda s: _replace_once(s, "runs = repository.recent_runs(", "confidence_percent = 0.8 * 100\n            runs = repository.recent_runs(")),
        ("M-B3-11", "corrupt row is stringified", "backend/api_adaptive.py",
         lambda s: s.replace("detail=exc.reason", "detail=str(exc)")),
        ("M-B3-12", "missing run becomes empty object", "backend/api_adaptive.py",
         lambda s: _replace_once(s, 'raise HTTPException(status_code=404, detail="research_run_not_found")', 'return {"status": "ok", "run": {}}')),
        ("M-B3-13", "list limit becomes unbounded", "backend/api_adaptive.py",
         lambda s: _replace_once(s, "Query(50, ge=1, le=200)", "Query(50)")),
        ("M-B3-14", "GET starts research", "backend/api_adaptive.py",
         lambda s: _replace_once(s, '"""Return persisted canonical research artifacts in append order, never current truth."""', '"""Return persisted canonical research artifacts in append order, never current truth."""\n    run_review()')),
        ("M-B3-15", "GET connection permits writes", "backend/api_adaptive.py",
         lambda s: _replace_once(s, 'conn.execute("PRAGMA query_only=ON")', 'conn.execute("DELETE FROM ai_research_runs")')),
        ("M-B3-16", "list sorts by created_at", "backend/ai_research_repository.py",
         lambda s: _replace_once(s, "ORDER BY id DESC LIMIT ?", "ORDER BY created_at DESC LIMIT ?")),
        ("M-B3-17", "old advisor is frontend fallback", "frontend/src/features/adaptive.js",
         lambda s: _replace_once(s, "export function adaptiveResearchHistoryHtml(payload){", "var legacyHistory=deepseek.latest;\nexport function adaptiveResearchHistoryHtml(payload){")),
        ("M-B3-18", "adaptive rerender drops research history refresh", "frontend/src/features/adaptive.js",
         lambda s: _replace_once(s, "  refreshAdaptiveResearchHistory();\n}", "  // research history refresh removed\n}")),
        ("M-B3-19", "renderer restores nested evidence shape", "frontend/src/features/adaptive.js",
         lambda s: _replace_once(
             _replace_once(s, "var sourceType=item.source_type||", "var sourceType=(item.evidence||{}).source_type||"),
             "var sourceId=item.source_id||", "var sourceId=(item.evidence||{}).source_id||",
         )),
        ("M-B3-20", "non-market cross-source false appears unconfirmed", "frontend/src/features/adaptive.js",
         lambda s: _replace_once(s, "if(item.source_type!=='market_data') cross='不适用';", "if(item.source_type!=='market_data') cross='未确认';")),
    ]

    detected = 0
    for mutation_id, label, target, mutate in cases:
        changed = dict(originals)
        changed[target] = mutate(changed[target])
        violations = architecture_violations(changed)
        if violations:
            detected += 1
            print(f"{mutation_id} {label}: DETECTED")
        else:
            print(f"{mutation_id} {label}: SURVIVED")

    after = _sha(_load_sources())
    restore = "PASS" if before == after else "FAIL"
    survived = len(cases) - detected
    print(f"detected={detected}/{len(cases)} survived={survived} fake=0 timeout=0 restore sha256={restore}")
    return 0 if survived == 0 and restore == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
