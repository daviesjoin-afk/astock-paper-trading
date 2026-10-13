"""Free official-exchange security-master ingestion for point-in-time universe.

The adapters use the same public exchange endpoints wrapped by AKShare, while
keeping the archive independent of optional dataframe/Excel packages.
"""
from __future__ import annotations

from datetime import date
from html.parser import HTMLParser
import hashlib
import json
import re
import sqlite3
import zipfile
from io import BytesIO
from typing import Any
import xml.etree.ElementTree as ET

import requests

try:
    import historical_universe_archive as HUA
except ImportError:  # pragma: no cover
    from . import historical_universe_archive as HUA


SSE_BASE = "https://query.sse.com.cn"
SZSE_REPORT = "https://www.szse.cn/api/report/ShowReport"
BSE_STOCKS = "https://www.bse.cn/nqxxController/nqxxCnzq.do"
BSE_CODE_MAPPING = "https://www.bse.cn/service/code_mapping.html"
BSE_LISTING_START = "2021-11-15"
BSE_CODE_SWITCH = "2025-05-06"
SOURCE_NAME = "official-free-exchange-stock-master-v1"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "Chrome/131.0 Safari/537.36")
_DATE8 = re.compile(r"^\d{8}$")
_CODE6 = re.compile(r"^\d{6}$")

# Each entry is supported by the exchange's published "终止上市暨摘牌" notice.
# An unrecognized missing BSE code blocks ingestion instead of being guessed.
_BSE_DELISTS = {
    "920680": {"name": "广道退", "listed_from": "2021-11-15", "delisted_at": "2026-01-05",
               "notice": "https://www.bse.cn/disclosure/2025/2025-12-31/8690dbac1d5c42db98e9c7388e562129.pdf"},
    "920305": {"name": "云创退", "listed_from": "2021-11-15", "delisted_at": "2026-07-30",
               "notice": "https://www.bse.cn/disclosure/2026/2026-07-29/8a64d7d9e97f4678af04313cdb66069e.pdf"},
}


class PublicUniverseSourceError(RuntimeError):
    """A source was unavailable, incomplete, or internally inconsistent."""


def _as_day(value: Any) -> str:
    raw = str(value or "").strip()
    if _DATE8.fullmatch(raw):
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    elif re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", raw):
        year, month, day = raw.split("/")
        raw = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise PublicUniverseSourceError("public_universe_date_invalid") from exc
    if parsed.isoformat() != raw:
        raise PublicUniverseSourceError("public_universe_date_invalid")
    return raw


def _get(session: requests.Session, url: str, *, params=None, headers=None) -> bytes:
    try:
        response = session.get(url, params=params, headers=headers, timeout=(5, 20))
        response.raise_for_status()
        return response.content
    except requests.RequestException as exc:
        raise PublicUniverseSourceError("public_universe_source_unavailable") from exc


def _post(session: requests.Session, url: str, *, data, headers=None) -> bytes:
    try:
        response = session.post(url, data=data, headers=headers, timeout=(5, 20))
        response.raise_for_status()
        return response.content
    except requests.RequestException as exc:
        raise PublicUniverseSourceError("public_universe_source_unavailable") from exc


def _sse_rows(session: requests.Session, *, stock_type: str, active: bool) -> list[dict[str, Any]]:
    url = f"{SSE_BASE}/sseQuery/commonQuery.do" if active else f"{SSE_BASE}/commonQuery.do"
    params = {
        "STOCK_TYPE": stock_type,
        "REG_PROVINCE": "",
        "CSRC_CODE": "",
        "STOCK_CODE": "",
        "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
        "COMPANY_STATUS": "2,4,5,7,8" if active else "3",
        "type": "inParams",
        "isPagination": "true",
        "pageHelp.cacheSize": "1",
        "pageHelp.beginPage": "1",
        # SSE clamps page size to 2,000. Every A-share type currently fits in
        # one response; requesting 1,000 returns page 1 again even when pageNo
        # is changed, so fail closed if the total ever exceeds this bound.
        "pageHelp.pageSize": "2000",
        "pageHelp.pageNo": "1",
        "pageHelp.endPage": "100",
    }
    headers = {"Host": "query.sse.com.cn", "Referer": "https://www.sse.com.cn/assortment/stock/list/share/",
               "User-Agent": _UA}
    first = _get(session, url, params=params, headers=headers)
    try:
        payload = json.loads(first.decode("utf-8"))
        page = payload["pageHelp"]
        total_pages = int(page["pageCount"])
        total = int(page["total"])
        # SSE currently puts rows in pageHelp.data; older responses used result.
        rows = list(page.get("data") or payload.get("result") or [])
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise PublicUniverseSourceError("sse_stock_master_response_invalid") from exc
    if total_pages != 1 or total < 1 or len(rows) != total:
        raise PublicUniverseSourceError("sse_stock_master_page_incomplete")
    output = []
    seen = set()
    for row in rows:
        code = str(row.get("A_STOCK_CODE") or "").strip().zfill(6)
        if not _CODE6.fullmatch(code) or code in seen:
            raise PublicUniverseSourceError("sse_stock_master_code_invalid_or_duplicate")
        seen.add(code)
        listed = _as_day(row.get("LIST_DATE"))
        delisted = None
        if not active:
            delisted = _as_day(row.get("DELIST_DATE"))
        output.append({"code": f"{code}.SH", "listed_from": listed,
                       "delisted_at": delisted, "security_type": "ordinary_share",
                       "exchange": "SSE", "name": str(row.get("COMPANY_ABBR") or "").strip()})
    return output


_XLSX_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def _xlsx_first_sheet_rows(payload: bytes) -> list[list[str]]:
    try:
        book = zipfile.ZipFile(BytesIO(payload))
        workbook = ET.fromstring(book.read("xl/workbook.xml"))
        relationships = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
        targets = {node.attrib["Id"]: node.attrib["Target"] for node in relationships}
        sheet = workbook.find("m:sheets/m:sheet", _XLSX_NS)
        if sheet is None:
            raise ValueError("missing_sheet")
        target = "xl/" + targets[sheet.attrib[f"{{{_XLSX_NS['r']}}}id"]]
        target = target.replace("xl/xl/", "xl/")
        xml = book.read(target).decode("gb18030", errors="replace")
        root = ET.fromstring(xml)
    except (KeyError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise PublicUniverseSourceError("szse_stock_master_workbook_invalid") from exc
    output = []
    for row in root.findall(".//m:sheetData/m:row", _XLSX_NS):
        values = []
        for cell in row.findall("m:c", _XLSX_NS):
            value = cell.find("m:v", _XLSX_NS)
            inline = cell.find("m:is/m:t", _XLSX_NS)
            values.append((value.text if value is not None else
                           inline.text if inline is not None else "") or "")
        output.append(values)
    return output


def _szse_rows(session: requests.Session, *, active: bool) -> list[dict[str, Any]]:
    params = {"SHOWTYPE": "xlsx", "CATALOGID": "1110" if active else "1793_ssgs",
              "TABKEY": "tab1" if active else "tab2", "random": "0.6935816432433362"}
    payload = _get(session, SZSE_REPORT, params=params,
                   headers={"Referer": "https://www.szse.cn/market/product/stock/list/index.html", "User-Agent": _UA})
    rows = _xlsx_first_sheet_rows(payload)
    if len(rows) < 100:
        raise PublicUniverseSourceError("szse_stock_master_truncated")
    output, seen = [], set()
    for row in rows[1:]:
        try:
            code = str(row[4] if active else row[0]).strip().zfill(6)
            listed = _as_day(row[6] if active else row[2])
            delisted = None if active else _as_day(row[3])
            name = str(row[5] if active else row[1]).strip()
        except (IndexError, PublicUniverseSourceError) as exc:
            raise PublicUniverseSourceError("szse_stock_master_row_invalid") from exc
        if not _CODE6.fullmatch(code) or code in seen:
            raise PublicUniverseSourceError("szse_stock_master_code_invalid_or_duplicate")
        seen.add(code)
        output.append({"code": f"{code}.SZ", "listed_from": listed,
                       "delisted_at": delisted, "security_type": "ordinary_share",
                       "exchange": "SZSE", "name": name})
    return output


class _MappingRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell = False
        self._text = ""

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell, self._text = True, ""

    def handle_data(self, data):
        if self._cell:
            self._text += data

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell and self._row is not None:
            self._row.append(" ".join(self._text.split()))
            self._cell = False
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _bse_page(session: requests.Session, page_no: int) -> tuple[dict[str, Any], bytes]:
    raw = _post(session, BSE_STOCKS,
                data={"page": str(page_no), "typejb": "T", "xxfcbj[]": "2", "xxzqdm": "",
                      "sortfield": "xxzqdm", "sorttype": "asc"},
                headers={"Referer": "https://www.bse.cn/nq/listedcompany.html", "User-Agent": _UA})
    try:
        text = raw.decode("utf-8")
        payload = json.loads(text[text.find("["):text.rfind("]") + 1])[0]
        return payload, raw
    except (ValueError, IndexError) as exc:
        raise PublicUniverseSourceError("bse_stock_master_response_invalid") from exc


def _bse_data(session: requests.Session) -> tuple[list[dict[str, Any]], bytes]:
    first, raw_first = _bse_page(session, 0)
    pages, total = int(first.get("totalPages") or 0), int(first.get("totalElements") or 0)
    if pages < 1 or total < 100 or len(first.get("content") or []) < 1:
        raise PublicUniverseSourceError("bse_stock_master_truncated")
    raw_parts = [raw_first]
    rows = list(first.get("content") or [])
    for page_no in range(1, pages):
        page, raw = _bse_page(session, page_no)
        if int(page.get("totalElements") or 0) != total:
            raise PublicUniverseSourceError("bse_stock_master_total_changed")
        rows.extend(page.get("content") or [])
        raw_parts.append(raw)
    if len(rows) != total:
        raise PublicUniverseSourceError("bse_stock_master_page_incomplete")
    output, seen = [], set()
    for row in rows:
        code = str(row.get("xxzqdm") or "").strip().zfill(6)
        if not _CODE6.fullmatch(code) or code in seen:
            raise PublicUniverseSourceError("bse_stock_master_code_invalid_or_duplicate")
        seen.add(code)
        listed = max(_as_day(row.get("xxgprq") or row.get("fxssrq")), BSE_LISTING_START)
        output.append({"code": f"{code}.BJ", "listed_from": listed,
                       "delisted_at": None, "security_type": "ordinary_share",
                       "exchange": "BSE", "name": str(row.get("xxzqjc") or "").strip()})
    return output, b"".join(raw_parts)


def _bse_code_map(session: requests.Session) -> tuple[dict[str, dict[str, str]], bytes]:
    raw = _get(session, BSE_CODE_MAPPING,
               headers={"Referer": "https://www.bse.cn/nq/listedcompany.html", "User-Agent": _UA})
    parser = _MappingRows()
    try:
        parser.feed(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise PublicUniverseSourceError("bse_code_mapping_encoding_invalid") from exc
    output = {}
    for row in parser.rows:
        if len(row) < 5 or not row[0].isdigit():
            continue
        old_code, new_code = row[3].strip(), row[4].strip()
        if not _CODE6.fullmatch(old_code) or not _CODE6.fullmatch(new_code) or new_code in output:
            raise PublicUniverseSourceError("bse_code_mapping_row_invalid")
        output[new_code] = {"old_code": old_code, "listed_from": max(_as_day(row[2]), BSE_LISTING_START)}
    if len(output) < 100:
        raise PublicUniverseSourceError("bse_code_mapping_truncated")
    return output, raw


def fetch_public_security_master(*, session: requests.Session | None = None) -> dict[str, Any]:
    """Fetch current+delisted A shares from official free exchange interfaces."""
    own_session = session is None
    session = session or requests.Session()
    session.headers.update({"User-Agent": _UA})
    raw_parts: list[bytes] = []
    try:
        sse_active = []
        for stock_type in ("1", "8"):
            sse_active.extend(_sse_rows(session, stock_type=stock_type, active=True))
        sse_delisted = _sse_rows(session, stock_type="1,8", active=False)
        raw_parts.extend([json.dumps(sse_active, sort_keys=True).encode(),
                          json.dumps(sse_delisted, sort_keys=True).encode()])

        szse_active = _szse_rows(session, active=True)
        szse_delisted = _szse_rows(session, active=False)
        raw_parts.extend([json.dumps(szse_active, sort_keys=True).encode(),
                          json.dumps(szse_delisted, sort_keys=True).encode()])

        bse_active, bse_response = _bse_data(session)
        bse_map, mapping_response = _bse_code_map(session)
        raw_parts.extend([bse_response, mapping_response])
        active_bse_codes = {row["code"].split(".")[0] for row in bse_active}
        missing_mapped_codes = set(bse_map) - active_bse_codes
        if missing_mapped_codes != set(_BSE_DELISTS):
            raise PublicUniverseSourceError("bse_delisting_history_unreconciled")
        bse_rows = []
        for current in bse_active:
            code = current["code"].split(".")[0]
            mapping = bse_map.get(code)
            if mapping:
                bse_rows.append({**current, "code": f"{mapping['old_code']}.BJ",
                                 "listed_from": mapping["listed_from"],
                                 "delisted_at": BSE_CODE_SWITCH})
                bse_rows.append({**current, "listed_from": BSE_CODE_SWITCH})
            else:
                bse_rows.append(current)
        for new_code, event in _BSE_DELISTS.items():
            mapping = bse_map[new_code]
            bse_rows.append({"code": f"{mapping['old_code']}.BJ", "listed_from": mapping["listed_from"],
                             "delisted_at": BSE_CODE_SWITCH, "security_type": "ordinary_share",
                             "exchange": "BSE", "name": event["name"]})
            bse_rows.append({"code": f"{new_code}.BJ", "listed_from": BSE_CODE_SWITCH,
                             "delisted_at": event["delisted_at"], "security_type": "ordinary_share",
                             "exchange": "BSE", "name": event["name"]})
            raw_parts.append(event["notice"].encode())

        active = sse_active + szse_active + bse_active
        delisted = sse_delisted + szse_delisted + [
            row for row in bse_rows if row.get("delisted_at") not in (None, BSE_CODE_SWITCH)
        ]
        all_rows = sse_active + sse_delisted + szse_active + szse_delisted + bse_rows
        intervals = {(row["code"], row["listed_from"], row["delisted_at"]) for row in all_rows}
        if len(all_rows) != len(intervals):
            raise PublicUniverseSourceError("public_stock_master_duplicate_interval")
        return {"rows": all_rows, "active": active, "delisted": delisted,
                "source_revision": hashlib.sha256(b"\n".join(raw_parts)).hexdigest(),
                "coverage_start": min(row["listed_from"] for row in all_rows),
                "counts": {"sse_active": len(sse_active), "sse_delisted": len(sse_delisted),
                           "szse_active": len(szse_active), "szse_delisted": len(szse_delisted),
                           "bse_active": len(bse_active), "bse_delisted": len(_BSE_DELISTS)}}
    finally:
        if own_session:
            session.close()


def _archive_rows(rows: list[dict[str, Any]], source_revision: str) -> list[dict[str, Any]]:
    """Model listing and delisting as dated source revisions for strict PIT reads."""
    output = []
    seen = set()
    for row in rows:
        code = row["code"]
        listed = _as_day(row["listed_from"])
        delisted = _as_day(row["delisted_at"]) if row.get("delisted_at") else None
        if delisted and delisted <= listed:
            raise PublicUniverseSourceError("public_stock_master_lifecycle_invalid")
        initial = {**row, "listed_from": listed, "delisted_at": None, "observed_at": listed}
        key = (code, listed)
        if key in seen:
            raise PublicUniverseSourceError("public_stock_master_duplicate_revision")
        seen.add(key)
        output.append(initial)
        if delisted:
            output.append({**row, "listed_from": listed, "delisted_at": delisted,
                           "observed_at": delisted})
    return output


def refresh_archive(db_path: str, *, asof: str) -> dict[str, Any]:
    """Import a verified official snapshot, returning its source and live roster."""
    cutoff = _as_day(asof)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        repository = HUA.HistoricalUniverseArchiveRepository(conn)
        latest = conn.execute(
            "SELECT universe_archive_fingerprint,coverage_end FROM historical_universe_manifests "
            "ORDER BY coverage_end DESC LIMIT 1"
        ).fetchone()
        if latest and str(latest["coverage_end"]) >= cutoff:
            fingerprint = str(latest["universe_archive_fingerprint"])
            manifest = repository.manifest(fingerprint)
        else:
            data = fetch_public_security_master()
            records = _archive_rows(data["rows"], data["source_revision"])
            manifest = repository.import_historical_security_master(
                records,
                coverage_start=data["coverage_start"],
                coverage_end=cutoff,
                source=SOURCE_NAME,
                source_revision=data["source_revision"],
            )
            fingerprint = manifest.universe_archive_fingerprint
        projection = repository.source_projection(fingerprint)
        all_members = repository.membership_rows(fingerprint, asof=cutoff)
        # repository returns the latest dated revision for every known symbol;
        # expose only securities that were actually members on the requested day.
        members = [row for row in all_members
                   if row["list_date"] <= cutoff and
                   (not row.get("delist_date") or row["delist_date"] > cutoff)]
        if not projection or not projection.get("historical_membership_complete") or not members:
            raise PublicUniverseSourceError("public_stock_master_archive_unproven")
        return {"manifest": manifest.projection(), "source": projection,
                "members": members, "counts": fetch_public_security_master_counts_from_members(members)}
    finally:
        conn.close()


def fetch_public_security_master_counts_from_members(members: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"SSE": 0, "SZSE": 0, "BSE": 0}
    for row in members:
        exchange = str(row.get("exchange") or "")
        if exchange in counts:
            counts[exchange] += 1
    return counts

