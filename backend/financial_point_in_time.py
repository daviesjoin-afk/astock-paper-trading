# -*- coding: utf-8 -*-
"""Conservative point-in-time metadata for financial factor rows.

The current Eastmoney financial endpoint supplies a report period (``REPORTDATE``)
but no disclosure/publication timestamp.  A report period is therefore never
treated as the date on which a report became visible.  These small standard
library helpers keep that distinction explicit and can be used by factor code
without coupling it to a network client or a dataframe implementation.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from typing import Any, Mapping, Optional, Sequence


REPORT_PERIOD_KEYS = (
    "report_period",
    "report_date",
    "REPORTDATE",
    "period",
)
REPORT_PUBLISHED_KEYS = (
    "report_published_at",
    "published_at",
    "publish_date",
    "announcement_date",
    "announcement_at",
    "disclosure_date",
    "disclosed_at",
    "published_date",
    "publish_time",
    "announcement_time",
    "report_ann_date",
    "report_notice_date",
    "report_disclosure_date",
    "notice_date",
    "ANN_DATE",
    "ANNDATE",
    "NOTICE_DATE",
)
PROFIT_VALUE_KEYS = (
    "roe",
    "rev_yoy",
    "profit_yoy",
    "net_profit",
    "annual_net_profit",
    "eps",
    "bps",
)

_DATE_ONLY = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$")


class _Temporal:
    """Parsed temporal value while retaining whether a time was supplied."""

    __slots__ = ("value", "has_time")

    def __init__(self, value: _dt.datetime, has_time: bool):
        self.value = value
        self.has_time = has_time


def _missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip().lower() in {"nan", "nat", "none", "null", "-"}
    if isinstance(value, float):
        return math.isnan(value)
    return False


def _parse_temporal(value: Any) -> Optional[_Temporal]:
    """Parse explicit date/datetime values; do not guess numeric epochs."""
    if _missing(value):
        return None
    if isinstance(value, _dt.datetime):
        return _Temporal(value, True)
    if isinstance(value, _dt.date):
        return _Temporal(_dt.datetime.combine(value, _dt.time()), False)
    text = str(value).strip()
    if _DATE_ONLY.match(text):
        try:
            parsed_date = _dt.date.fromisoformat(text.replace("/", "-"))
        except ValueError:
            return None
        return _Temporal(_dt.datetime.combine(parsed_date, _dt.time()), False)
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = _dt.datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = _dt.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    return _Temporal(parsed, True)


def _utc_naive(value: _dt.datetime) -> _dt.datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(_dt.timezone.utc).replace(tzinfo=None)


def _first(mapping: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if not _missing(value):
            return value
    return None


def _format_temporal(parsed: Optional[_Temporal], *, date_only: bool = False) -> Optional[str]:
    if parsed is None:
        return None
    value = parsed.value
    if date_only or not parsed.has_time:
        return value.date().isoformat()
    return value.isoformat(timespec="seconds")


def _has_profit_value(record: Mapping[str, Any], value_keys: Sequence[str]) -> bool:
    return any(not _missing(record.get(key)) for key in value_keys)


def financial_visibility(
    record: Optional[Mapping[str, Any]],
    asof: Any = None,
    *,
    period_keys: Sequence[str] = REPORT_PERIOD_KEYS,
    published_keys: Sequence[str] = REPORT_PUBLISHED_KEYS,
    value_keys: Sequence[str] = PROFIT_VALUE_KEYS,
) -> dict:
    """Return conservative visibility metadata for one financial report.

    ``asof=None`` is the live/compatibility mode: values are retained even if
    publication metadata is absent, but the source is explicitly ``shadow``.
    Supplying an ``asof`` makes the function point-in-time strict.  A report
    without a parseable publication timestamp is then hidden, and a report
    published after the cutoff is hidden as ``future``.  No report period is
    ever substituted for a publication timestamp.
    """
    row = record if isinstance(record, Mapping) else {}
    asof_requested = asof is not None
    asof_temporal = _parse_temporal(asof) if asof_requested else None
    period_raw = _first(row, period_keys)
    period_temporal = _parse_temporal(period_raw)
    # Keep an unparseable source value visible for diagnostics, but never use
    # it as a publication timestamp or for a visibility comparison.
    period = _format_temporal(period_temporal, date_only=True) if period_temporal else (
        str(period_raw).strip() if period_raw is not None else None
    )

    published_raw = _first(row, published_keys)
    published_temporal = _parse_temporal(published_raw)
    published_at = _format_temporal(published_temporal)
    asof_date = _format_temporal(asof_temporal, date_only=True)
    has_value = _has_profit_value(row, value_keys)

    visible = True
    source = (
        "reported"
        if published_temporal is not None and has_value
        else ("unknown" if published_temporal is not None else ("shadow" if has_value else "unknown"))
    )
    if asof_requested:
        # A malformed cutoff is not a safe basis for a historical replay.
        if asof_temporal is None:
            visible = False
            source = "shadow" if has_value else "unknown"
        elif published_temporal is None:
            visible = False
            source = "shadow" if has_value else "unknown"
            if period_temporal is not None and asof_temporal is not None:
                if period_temporal.value.date() > _utc_naive(asof_temporal.value).date():
                    source = "future"
        else:
            pub_cmp = _utc_naive(published_temporal.value)
            asof_cmp = _utc_naive(asof_temporal.value)
            if asof_temporal.has_time:
                visible = pub_cmp <= asof_cmp
                # A date-only disclosure is not a midnight timestamp.  When
                # replaying intraday on that same date, keep it hidden because
                # the endpoint does not prove that publication had happened.
                if (
                    visible
                    and not published_temporal.has_time
                    and pub_cmp.date() == asof_cmp.date()
                ):
                    visible = False
                    source = "shadow" if has_value else "unknown"
            else:
                # A date-only cutoff denotes the end of that calendar day.
                visible = pub_cmp.date() <= asof_cmp.date()
            # A period itself cannot be in the future, even if a malformed
            # upstream record claims an earlier publication timestamp.
            if period_temporal is not None and period_temporal.value.date() > asof_cmp.date():
                visible = False
            if source not in {"shadow", "unknown"}:
                source = "reported" if visible else "future"
            elif not visible and (
                pub_cmp.date() > asof_cmp.date()
                or (period_temporal is not None and period_temporal.value.date() > asof_cmp.date())
            ):
                source = "future"

    age_days = None
    if visible and published_temporal is not None and asof_temporal is not None:
        pub_cmp = _utc_naive(published_temporal.value)
        asof_cmp = _utc_naive(asof_temporal.value)
        age_days = (asof_cmp.date() - pub_cmp.date()).days
        if age_days < 0:
            age_days = None

    return {
        "report_period": period,
        "report_published_at": published_at,
        "asof_date": asof_date,
        "report_age_days": age_days,
        "profit_source": source,
        "visible": bool(visible),
    }


def _self_check() -> None:
    # Endpoint-style row: period is known, publication is not.  Live mode keeps
    # the value for compatibility but labels it shadow.
    live = financial_visibility({"report_date": "2024-03-31", "net_profit": 10})
    assert live["report_period"] == "2024-03-31"
    assert live["report_published_at"] is None
    assert live["profit_source"] == "shadow"
    assert live["visible"] is True

    # Historical mode must not let an unknown publication date leak a value.
    historical = financial_visibility(
        {"report_date": "2024-03-31", "net_profit": 10}, "2024-06-30"
    )
    assert historical["visible"] is False
    assert historical["profit_source"] == "shadow"

    known = financial_visibility(
        {
            "report_date": "2024-03-31",
            "report_published_at": "2024-05-15",
            "net_profit": 10,
        },
        "2024-06-30",
    )
    assert known["visible"] is True
    assert known["profit_source"] == "reported"
    assert known["report_age_days"] == 46

    future = financial_visibility(
        {"report_date": "2024-06-30", "report_published_at": "2024-08-31", "net_profit": 10},
        "2024-06-30",
    )
    assert future["visible"] is False
    assert future["profit_source"] == "future"

    inconsistent = financial_visibility(
        {"report_date": "2024-12-31", "report_published_at": "2024-06-30", "net_profit": 10},
        "2024-06-30",
    )
    assert inconsistent["visible"] is False
    assert inconsistent["profit_source"] == "future"

    unknown_period_future = financial_visibility(
        {"report_date": "2024-12-31", "net_profit": 10}, "2024-06-30"
    )
    assert unknown_period_future["visible"] is False
    assert unknown_period_future["profit_source"] == "future"

    same_day_intraday = financial_visibility(
        {"report_date": "2024-03-31", "report_published_at": "2024-05-15", "net_profit": 10},
        "2024-05-15T09:00:00",
    )
    assert same_day_intraday["visible"] is False
    assert same_day_intraday["profit_source"] == "shadow"

    no_value = financial_visibility(
        {"report_date": "2024-03-31", "report_published_at": "2024-05-15"},
        "2024-06-30",
    )
    assert no_value["visible"] is True
    assert no_value["profit_source"] == "unknown"


if __name__ == "__main__":
    _self_check()
    print("financial_point_in_time self-check: ok")
