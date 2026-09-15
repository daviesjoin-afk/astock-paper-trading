# -*- coding: utf-8 -*-
"""PIT 历史**证券状态**源（``name`` / ``risk_flag``）。

可成交性判定需要一个函数::

    security_state_at(code, session) -> {"name": ..., "risk_flag": ...}

它必须回答"**那个 session 当时**这只股票叫什么、是不是 ST/风险警示"。这是
账户权限（板块 / ST / 证券类型）判定的输入，因此**必须**是历史证据，不能是
今天的快照。

──────────────────────── 为什么需要单独一个模块 ────────────────────────

``selection_picks.name`` / ``paper_signals.name`` 记录的是**决策当时**的名称。
决策与动作（entry = 决策后首个交易日，exit 更晚）之间 ST 状态可能变化，所以
决策日名称不能证明动作时的资格。而 ``universe.json`` /
``market_snapshot_full.json`` 是**当前快照**——用它们回填历史就是 PIT 违规。

本模块把"历史状态从哪来"收敛成**一个**入口，并且：

* 只承认带 **per-session 维度** 且显式声明为历史归档的源；
* 逐行要求 ``available_at <= action_at``（委托 :mod:`point_in_time`）；
* **绝不**回退到当前快照 / 决策日名称 / 未来状态。

──────────────────────── 归档格式 ────────────────────────

``data_cache/security_state_history.json``（路径可用环境变量
``ASTOCK_SECURITY_STATE_ARCHIVE`` 覆盖）::

    {
      "kind": "historical_archive",
      "historical_membership_complete": true,
      "archive_source": "<描述>",
      "availability_basis": "session_close",
      "rows": [
        {
          "code": "600001",
          "effective_from": "2024-06-14",
          "effective_to": "2024-06-20",
          "name": "某某股份",
          "risk_flag": false,
          "available_at": "2024-06-14T15:00:00+08:00"
        }
      ]
    }

判定规则（先到先判，全部 fail closed）：

1. ``kind`` 必须是 :data:`point_in_time.UNIVERSE_SOURCE_KIND_HISTORICAL`
   （``"historical_archive"``）。当前快照无权自称历史归档 → 整个源不可用。
2. 逐行：``code`` 必须匹配；``effective_from``（缺省取 ``available_at`` 的日期）
   ``<= session < effective_to``（``effective_to`` 缺省为 ``effective_from`` 当日，
   即只覆盖当天）。
3. 可用时点：显式 ``available_at`` 优先；缺失时**只有**归档声明
   ``"availability_basis": "session_close"`` 才允许用该 session 的收盘时点兜底
   （"这条状态是收盘时记下的"是显式声明，不是默认假设）。两者都没有 →
   ``availability_unknown`` → 该行不可用。
4. 没有任何可用行 → 返回 ``None``。**这不是"非 ST"**，而是"ST 未知"，
   由 :mod:`selection_tradability` 判 ``unproven / unknown_st_status``。

本模块**只读**，不抓取、不写盘、不改任何生产数据。
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Mapping, Optional, Sequence

try:  # ``backend`` on sys.path（生产与 ``cd backend`` 测试）
    import point_in_time as PIT
except ImportError:  # pragma: no cover - package-style import
    from . import point_in_time as PIT


#: 本仓库归档文件的默认位置（相对 backend 的上一级 data_cache）。
DEFAULT_ARCHIVE_NAME = "security_state_history.json"
#: 覆盖归档路径的环境变量。
ARCHIVE_ENV_VAR = "ASTOCK_SECURITY_STATE_ARCHIVE"

#: 可用时点候选键（按可信度排序）。
ROW_AVAILABLE_AT_KEYS = ("available_at", "observed_at", "as_of", "asof", "snapshot_at")
#: 状态生效区间候选键。
ROW_EFFECTIVE_FROM_KEYS = ("effective_from", "session", "date", "trade_date", "as_of")
ROW_EFFECTIVE_TO_KEYS = ("effective_to", "effective_until", "valid_to")
#: 名称 / 风险标记候选键。
ROW_NAME_KEYS = ("name", "security_name", "short_name")
ROW_RISK_FLAG_KEYS = ("risk_flag", "is_st", "st_flag", "risk_warning")

#: 归档声明的可用性基准：允许"状态在某 session 收盘时记下"。
AVAILABILITY_BASIS_SESSION_CLOSE = "session_close"

#: 源可用性状态码（机器只读 ``status``）。
SOURCE_OK = "historical_state_archive_complete"
SOURCE_MISSING = "archive_missing"
SOURCE_UNREADABLE = "archive_unreadable"
SOURCE_NOT_HISTORICAL = "not_a_historical_archive"
SOURCE_INCOMPLETE = "archive_incomplete"

SOURCE_STATUSES = (
    SOURCE_OK,
    SOURCE_MISSING,
    SOURCE_UNREADABLE,
    SOURCE_NOT_HISTORICAL,
    SOURCE_INCOMPLETE,
)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float):
        return value != value
    if isinstance(value, str):
        return value.strip().lower() in {"", "nan", "nat", "none", "null", "-", "--"}
    return False


def _session_of(value: Any) -> Optional[str]:
    """把任意时点/日期归一成 ``YYYY-MM-DD``；无法解析 → ``None``。"""
    moment = PIT.parse_available_at(value)
    if moment is None:
        return None
    return moment.date().isoformat()


def default_archive_path() -> str:
    """归档文件的默认绝对路径（可用环境变量覆盖）。"""
    override = os.environ.get(ARCHIVE_ENV_VAR)
    if override and override.strip():
        return override.strip()
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    base = os.path.dirname(backend_dir)
    return os.path.join(base, "data_cache", DEFAULT_ARCHIVE_NAME)


def load_archive(path: Optional[str] = None) -> Optional[dict]:
    """读取归档 JSON。文件不存在/不可解析 → ``None``（**不是**空归档）。"""
    target = path or default_archive_path()
    try:
        with open(target, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def archive_provenance(
    payload: Optional[Mapping[str, Any]], *, asof: Any = None
) -> dict:
    """归档**源级**完整性判定（机器只读 ``status``）。

    只有显式 ``kind == "historical_archive"`` 且 ``rows`` 非空、并声明了完整性
    标志的归档才算可用。当前快照（``current_snapshot``）永远不合格。
    """
    if payload is None:
        return {"status": SOURCE_MISSING, "source": None, "rows": 0}
    kind = None
    for key in PIT.UNIVERSE_SOURCE_KIND_KEYS:
        value = payload.get(key)
        if not _is_missing(value):
            kind = str(value).strip()
            break
    source_name = None
    for key in PIT.UNIVERSE_SOURCE_NAME_KEYS:
        value = payload.get(key)
        if not _is_missing(value):
            source_name = str(value)
            break
    rows = payload.get("rows")
    row_count = len(rows) if isinstance(rows, list) else 0
    if kind != PIT.UNIVERSE_SOURCE_KIND_HISTORICAL:
        return {
            "status": SOURCE_NOT_HISTORICAL,
            "kind": kind,
            "source": source_name,
            "rows": row_count,
        }
    complete = None
    for key in PIT.UNIVERSE_SOURCE_COMPLETE_KEYS:
        if key in payload:
            # 严格归一（委托 :func:`point_in_time.as_strict_bool`）：``"false"`` /
            # ``"0"`` 必须读成"未声明完整"，绝不能靠 ``bool("false") == True``
            # 把一份显式否定的归档当成完整历史源。
            complete = PIT.as_strict_bool(payload.get(key))
            break
    if complete is not True:
        return {
            "status": SOURCE_INCOMPLETE,
            "kind": kind,
            "source": source_name,
            "rows": row_count,
        }
    if row_count == 0:
        return {
            "status": SOURCE_INCOMPLETE,
            "kind": kind,
            "source": source_name,
            "rows": 0,
        }
    return {
        "status": SOURCE_OK,
        "kind": kind,
        "source": source_name,
        "rows": row_count,
        "availability_basis": payload.get("availability_basis"),
    }


class SecurityStateArchive:
    """一个**已校验**的 PIT 历史状态源。

    只在 :meth:`from_payload` 校验通过后构造；构造失败返回 ``None``，
    调用方据此走"诚实降级"路径。
    """

    def __init__(self, rows: Sequence[Mapping[str, Any]], *, availability_basis: Any = None):
        self._by_code: dict = {}
        self._session_close_fallback = (
            str(availability_basis or "").strip() == AVAILABILITY_BASIS_SESSION_CLOSE
        )
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            code = str(row.get("code") or "").strip()
            if not code:
                continue
            self._by_code.setdefault(code, []).append(row)

    @classmethod
    def from_payload(cls, payload: Optional[Mapping[str, Any]]) -> "SecurityStateArchive | None":
        provenance = archive_provenance(payload)
        if provenance.get("status") != SOURCE_OK:
            return None
        return cls(payload.get("rows") or (), availability_basis=payload.get("availability_basis"))

    # ── 逐行取值 ──────────────────────────────────────────────
    @staticmethod
    def _row_value(row: Mapping[str, Any], keys: Sequence[str]) -> Any:
        for key in keys:
            value = row.get(key)
            if not _is_missing(value):
                return value
        return None

    def _row_window(self, row: Mapping[str, Any]) -> tuple:
        raw_from = self._row_value(row, ROW_EFFECTIVE_FROM_KEYS)
        raw_available = self._row_value(row, ROW_AVAILABLE_AT_KEYS)
        start = _session_of(raw_from if raw_from is not None else raw_available)
        if start is None:
            return None, None
        raw_to = self._row_value(row, ROW_EFFECTIVE_TO_KEYS)
        if raw_to is None:
            # 缺省：这条状态只覆盖 ``start`` 当天。
            return start, start
        end = _session_of(raw_to)
        if end is None:
            return None, None
        return start, end

    def _row_available_at(self, row: Mapping[str, Any], start: str) -> Optional[str]:
        raw_available = self._row_value(row, ROW_AVAILABLE_AT_KEYS)
        if raw_available is not None:
            moment = PIT.parse_available_at(raw_available)
            if moment is not None:
                return moment.isoformat(timespec="seconds")
        if self._session_close_fallback:
            # 显式声明"状态在 session 收盘时记下"，才允许用收盘时点兜底。
            # 委托 point_in_time（与 #147 的 label 成熟口径同一条规则）。
            moment = PIT.bar_available_at(start)
            return None if moment is None else moment.isoformat(timespec="seconds")
        return None

    def state_at(self, code: Any, session: Any) -> Optional[dict]:
        """``code`` 在 ``session`` 当时的状态；证据不足 → ``None``（fail closed）。"""
        code_text = str(code or "").strip()
        target = _session_of(session)
        if not code_text or target is None:
            return None
        candidates = []
        for row in self._by_code.get(code_text, ()):
            start, end = self._row_window(row)
            if start is None:
                continue
            if not (start <= target <= end):
                continue
            available_at = self._row_available_at(row, start)
            if available_at is None:
                # 没有可信可用时点 → 这一行不是历史证据，跳过（不是"默认可见"）。
                continue
            candidates.append((start, available_at, row))
        if not candidates:
            return None
        # 取生效起点最晚的一行（最近一次观测），再交由调用方做 PIT 可见性判定。
        candidates.sort(key=lambda item: (item[0], item[1]))
        start, available_at, row = candidates[-1]
        return {
            "name": self._row_value(row, ROW_NAME_KEYS),
            "risk_flag": self._row_value(row, ROW_RISK_FLAG_KEYS),
            "available_at": available_at,
            "session": target,
            "effective_from": start,
        }


def make_state_provider(
    archive: SecurityStateArchive,
) -> Callable[[str, str], Optional[dict]]:
    """把归档包装成 ``security_state_fn(code, session)``。"""

    def provider(code, session):
        return archive.state_at(code, session)

    return provider


def resolve_security_state_fn(
    *, explicit: Optional[Callable] = None, archive_path: Optional[str] = None
) -> tuple:
    """生产入口：解析出 ``(security_state_fn | None, provenance)``。

    ``explicit`` 优先（测试/调用方注入）。否则尝试加载历史归档；归档不存在、
    格式不对或未声明完整 → 返回 ``(None, provenance)``，调用方必须**诚实降级**，
    绝不用当前快照补位。
    """
    if explicit is not None:
        return explicit, {
            "status": SOURCE_OK,
            "kind": "injected_provider",
            "source": "explicit",
            "rows": None,
        }
    payload = load_archive(archive_path)
    provenance = archive_provenance(payload)
    if provenance.get("status") != SOURCE_OK:
        return None, provenance
    archive = SecurityStateArchive.from_payload(payload)
    if archive is None:  # pragma: no cover - provenance OK implies constructible
        return None, dict(provenance, status=SOURCE_UNREADABLE)
    return make_state_provider(archive), provenance


# ───────────────────────────── self-check ─────────────────────────────


def _self_check() -> None:
    payload = {
        "kind": PIT.UNIVERSE_SOURCE_KIND_HISTORICAL,
        "historical_membership_complete": True,
        "archive_source": "unit-test",
        "availability_basis": AVAILABILITY_BASIS_SESSION_CLOSE,
        "rows": [
            {
                "code": "600001",
                "effective_from": "2024-06-14",
                "name": "某某股份",
                "risk_flag": False,
            }
        ],
    }
    assert archive_provenance(payload)["status"] == SOURCE_OK
    archive = SecurityStateArchive.from_payload(payload)
    assert archive is not None
    state = archive.state_at("600001", "2024-06-14")
    assert state is not None and state["name"] == "某某股份", state
    assert state["available_at"].startswith("2024-06-14T15:00"), state
    # 生效区间之外 → 未知，而不是沿用旧值。
    assert archive.state_at("600001", "2024-06-17") is None
    # 当前快照无权自称历史归档。
    assert archive_provenance({"kind": "current_snapshot", "rows": [{}]})["status"] == (
        SOURCE_NOT_HISTORICAL
    )
    print("security_state_point_in_time self-check: ok")


if __name__ == "__main__":
    _self_check()
