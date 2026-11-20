"""Offline behavioral tests for the broad astock_public data layer.

No test in this file performs network I/O.  A blocking urlopen/requests hook is
installed during every test, and the pinned upstream source is loaded only
through the package's static AST filter.
"""
from __future__ import annotations

import io
import importlib.util
import json
import re
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import astock_public as public
from astock_public import (AStockPublicError, CapabilityError, CapabilityRegistry,
                           PublicClient, RegistryError, SourceLoadError,
                           build_source_http_transport, catalog,
                           source_manifest)
from astock_public.source_snapshot import (EXPECTED_FUNCTIONS, StaticSource,
                                           python_blocks,
                                           static_definition_names)
from source_http import RequestIdentity, SourceHttp, SourceHttpError

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


@contextmanager
def deny_network():
    """Fail a test immediately if any real socket/urlopen path is reached."""
    def blocked(*args, **kwargs):
        raise AssertionError("network egress attempted during an offline test")

    original_socket = socket.socket.connect
    original_urlopen = urllib.request.urlopen
    socket.socket.connect = blocked
    urllib.request.urlopen = blocked
    try:
        yield
    finally:
        socket.socket.connect = original_socket
        urllib.request.urlopen = original_urlopen


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class StaticSourceTests(unittest.TestCase):
    def test_import_and_catalog_are_offline_and_have_exact_counts(self):
        with deny_network():
            result = catalog()
            manifest = source_manifest()
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["network_accessed"])
        counts = result["counts"]
        self.assertEqual(counts["inventory_rows"], 88)
        self.assertEqual(counts["capability_endpoints"], 87)
        self.assertEqual(counts["primary"], 82)
        self.assertEqual(counts["backups"], 5)
        self.assertEqual(counts["layers"], 15)
        self.assertEqual(counts["sources"], 34)
        self.assertEqual(manifest["python_blocks"], 91)
        self.assertEqual(manifest["skill_lines"], 7711)
        self.assertEqual(manifest["skill_sha256"], public.SKILL_SHA256)
        self.assertEqual(manifest["license"], "Apache-2.0")

    def test_every_endpoint_group_is_registered_even_if_credentials_or_deps_absent(self):
        with deny_network():
            rows = catalog()["capabilities"]
            blockers = catalog()["blockers"]
            dependencies = public.dependency_report()
        self.assertEqual(len(rows), 88)
        self.assertTrue(all(row["functions"] for row in rows))
        self.assertTrue(any("IWENCAI_API_KEY" in row["credentials"] for row in rows))
        self.assertTrue(any("mootdx" in row["optional_dependencies"] for row in rows))
        self.assertTrue(any("baostock" in row["optional_dependencies"] for row in rows))
        self.assertIn("tencent_501_anti_scrape", blockers)
        self.assertFalse(blockers["tencent_501_anti_scrape"]["permanent"])
        self.assertTrue(dependencies["optional_dependencies_do_not_disable_package"])
        self.assertGreaterEqual(dependencies["absent"], 1)

    def test_temporal_and_dependency_restrictions_are_actual_catalog_data(self):
        with deny_network():
            valuation = PublicClient().describe("fundamentals.baostock_valuation")
            announcement = PublicClient().describe("fundamentals.cninfo_announcements")
            bse = PublicClient().describe("backup.bse_quote")
            tdx_tcp = PublicClient().describe("bars.mootdx_legacy")
        self.assertEqual(valuation["capability"]["temporal"], "historical_date_window")
        self.assertIn("baostock", valuation["capability"]["optional_dependencies"])
        self.assertNotIn("BJ", valuation["capability"]["markets"])
        self.assertIn("no_exact_publication_time",
                      [b["id"] for b in announcement["capability"]["blockers"]])
        self.assertEqual(bse["capability"]["credentials"], ["anonymous_cookie"])
        self.assertEqual(bse["capability"]["temporal"], "current_snapshot")
        self.assertIn("tdx_tcp_bars_empty", [b["id"] for b in tdx_tcp["capability"]["blockers"]])

    def test_upstream_snapshot_checksum_tests_fixtures_and_license_are_present(self):
        base = ROOT / "astock_public" / "_upstream"
        self.assertTrue((base / "LICENSE").is_file())
        self.assertIn("Apache License", (base / "LICENSE").read_text(encoding="utf-8"))
        self.assertEqual(len(list((base / "tests").glob("test_*.py"))), 5)
        self.assertEqual(len(list((base / "tests" / "fixtures" / "sina_adjust").glob("*"))), 12)
        self.assertEqual(len(list((base / "docs").glob("*.md"))), 2)
        self.assertTrue((base / "THIRD_PARTY_NOTICES.md").is_file())
        self.assertEqual(len(python_blocks()), 91)
        self.assertEqual(len(static_definition_names()), 181)  # unique names; one helper is defined twice

    def test_modified_snapshot_is_rejected(self):
        original = public.source_snapshot.SKILL_SHA256
        try:
            public.source_snapshot.SKILL_SHA256 = "0" * 64
            with self.assertRaises(SourceLoadError) as caught:
                source_manifest()
            self.assertEqual(caught.exception.code, "upstream_checksum_mismatch")
        finally:
            public.source_snapshot.SKILL_SHA256 = original

    def test_ast_filter_keeps_all_definitions_and_no_demo_side_effects(self):
        with deny_network():
            source = StaticSource()
        self.assertGreaterEqual(len(source.functions), EXPECTED_FUNCTIONS)
        for name in ("norm_ticker", "tencent_quote", "tencent_kline",
                     "tdx_daily_package", "tencent_ticks", "futures_kline",
                     "full_valuation", "margin_trading_backup",
                     "bse_quote_backup", "dragon_tiger_backup"):
            self.assertIn(name, source.functions)
        # Constants needed by callable semantics survived the filter.
        self.assertIn("_TICKER_RE", source.namespace)
        self.assertIn("DATACENTER_URL", source.namespace)
        self.assertIn("TDX_PACKAGE_URL", source.namespace)
        # Session objects exist but are inert shims, not real requests sessions.
        self.assertEqual(source.namespace["EM_SESSION"].__class__.__module__,
                         "astock_public.transport")

    def test_all_registry_endpoint_functions_are_actually_callable(self):
        with deny_network():
            source = StaticSource()
            missing = {}
            for capability in public.CAPABILITIES:
                absent = [name for name in capability.functions if name not in source.functions]
                if absent:
                    missing[capability.id] = absent
            # @_v39_contract wraps these functions; its functools.wraps keeps
            # __name__ but makes __code__ point at the wrapper.
            decorators_wrapped = [name for name in (
                "tencent_kline", "tdx_daily_package", "futures_kline",
                "convertible_bonds")
                if source.callable(name).__name__ == name
                and source.callable(name).__code__.co_name == "wrapper"]
            last_macro_get = source.callable("_macro_get")
        self.assertEqual(missing, {})
        self.assertEqual(len(decorators_wrapped), 4)
        self.assertTrue(callable(last_macro_get))
        # The duplicate helper is intentionally the last, behaviour-identical
        # definition from SKILL.md block 68.
        self.assertEqual(last_macro_get.__code__.co_filename, "SKILL.md:block68")

    def test_callable_semantics_of_upstream_helpers_are_preserved(self):
        with deny_network():
            client = PublicClient()
            self.assertEqual(client.call("norm_ticker", "600519.SH"), "600519")
            self.assertEqual(client.call("to_joinquant", "000001.SZ"), "000001.XSHE")
            self.assertEqual(client.call("get_prefix", "000016.SH"), "sh")
            self.assertEqual(client.call("em_secid", "600519.SH"), "1.600519")
            self.assertEqual(client.call("_v39_num", "1,234.50"), 1234.5)
            with self.assertRaises(ValueError):
                client.call("norm_ticker", "foo600519bar")

    def test_no_transport_network_endpoint_fails_closed(self):
        with deny_network():
            with self.assertRaises(CapabilityError) as caught:
                PublicClient().query("quote.tencen_snapshot", ["600519"])
            self.assertEqual(caught.exception.code, "transport_required")
            # The low-level inert shim also fails closed rather than reaching
            # the machine's real requests/urllib installation.
            source = StaticSource()
            with self.assertRaisesRegex(Exception, "transport_required"):
                source.callable("_v39_http")("https://example.invalid/x")

    def test_historical_as_of_rejects_current_snapshot_without_network(self):
        with deny_network():
            client = PublicClient(fetch=lambda *a, **k: b"{}", as_of="2026-11-15T12:00:00+00:00")
            with self.assertRaises(CapabilityError) as caught:
                client.query("quote.tencen_snapshot", ["600519"])
            self.assertEqual(caught.exception.code, "historical_snapshot_unavailable")

    def test_optional_dependency_absence_disables_one_capability_only(self):
        with deny_network():
            client = PublicClient(fetch=lambda *a, **k: b"{}")
            self.assertEqual(len(client.catalog()["capabilities"]), 88)
            with patch("astock_public.registry._dependency_state",
                       return_value={"name": "mootdx", "kind": "python_package",
                                     "available": False, "detail": "absent"}):
                with self.assertRaises(CapabilityError) as caught:
                    client.query("bars.mootdx_legacy")
            self.assertEqual(caught.exception.code, "optional_dependency_missing")
            self.assertEqual(client.call("norm_ticker", "600519.SH"), "600519")

    def test_actual_optional_import_absence_preserves_unrelated_calls(self):
        original = importlib.util.find_spec
        def absent(name, *args, **kwargs):
            return None if name in {"pandas", "numpy", "baostock", "mootdx"} else original(name, *args, **kwargs)
        with deny_network(), patch("importlib.util.find_spec", side_effect=absent):
            client = PublicClient()
            self.assertEqual(client.call("norm_ticker", "600519.SH"), "600519")
            self.assertEqual(len(client.catalog()["capabilities"]), 88)

    def test_iwencai_credential_absence_is_explicit_but_catalog_still_lists_it(self):
        with deny_network():
            client = PublicClient(fetch=lambda *a, **k: b"{}")
            with patch.dict("os.environ", {"IWENCAI_API_KEY": "", "IWENCAI_BASE_URL": ""}, clear=False):
                capability = client.describe("reports.iwencai")["capability"]
                self.assertIn("IWENCAI_API_KEY", capability["credentials"])
                with self.assertRaises(CapabilityError) as caught:
                    client.query("reports.iwencai", "贵州茅台")
            self.assertEqual(caught.exception.code, "credential_required")


class TransportInjectionTests(unittest.TestCase):
    def make_http(self, body: bytes):
        calls = []

        def opener(request, timeout):
            calls.append((request.full_url, getattr(request, "data", None), dict(request.header_items())))
            return FakeResponse(body)

        return SourceHttp(clock=lambda: NOW, opener=opener), calls

    def test_source_http_is_used_for_requests_shaped_endpoint(self):
        body = ('v_sh600519="1~贵州茅台~600519~1688.00~1680.00~1681.00~100~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~20261115150000~0~0~0~0~0~1688.00/100/168800~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0"').encode('gbk')
        http, calls = self.make_http(body)
        with deny_network():
            client = PublicClient(http=http)
            result = client.query("quote.tencen_snapshot", ["600519"], as_json=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["capability"], "quote.tencen_snapshot")
        self.assertEqual(result["result"]["600519"]["name"], "贵州茅台")
        self.assertEqual(result["result"]["600519"]["price"], 1688.0)
        self.assertTrue(result["provenance"]["transport_injected"])
        self.assertEqual(len(calls), 1)
        self.assertIn("qt.gtimg.cn", calls[0][0])
        self.assertEqual(calls[0][2].get("User-agent"), "Mozilla/5.0")

    def test_injected_source_http_identity_contains_method_url_and_body(self):
        body = b'[]'
        calls = []

        class RecordingHttp:
            def request(self, identity, *, headers=None, **kwargs):
                calls.append(identity.document())
                return type("Snap", (), {"raw": body})()

        with deny_network():
            client = PublicClient(http=RecordingHttp())
            response = client.call("_v39_http", "https://example.test/x",
                                   params={"a": 1}, data={"b": 2}, method="POST")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        self.assertEqual(calls[0]["method"], "POST")
        self.assertIn("a=1", calls[0]["url"])
        self.assertEqual(calls[0]["adjust"], "none")
        self.assertEqual(calls[0]["symbol"], "*")

    def test_project_public_data_client_still_uses_the_same_source_http(self):
        from public_data import PublicDataClient
        from test_public_data import payload as quote_payload
        body = ('v_sh600000="' + quote_payload() + '";').encode("gbk")
        http, calls = self.make_http(body)
        with deny_network():
            old = PublicDataClient(http).quotes(["600000.SH"])
            broad = PublicClient(http=http)
            self.assertEqual(broad.call("norm_ticker", "600000.SH"), "600000")
        self.assertEqual(old["status"], "ok")
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(next(iter(http.__dict__.values())), object)

    def test_404_is_preserved_and_missing_package_is_not_a_success(self):
        calls = []
        def opener(request, timeout):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, io.BytesIO(b"missing"))
        http = SourceHttp(opener=opener, clock=lambda: NOW)
        with deny_network():
            client = PublicClient(http=http)
            response = client.call("_v39_http", "https://example.test/missing", allow_status=(404,))
            self.assertEqual(response.status_code, 404)
            with self.assertRaises(ValueError):
                client.query("bars.tdx_daily_package", "2021-01-04")
        self.assertEqual(len(calls), 2)


class PublicationDerivativeTests(unittest.TestCase):
    def factory_scope(self):
        from types import SimpleNamespace
        source = StaticSource()
        fn = source.callable("tdx_client")
        calls = []
        class Tcp:
            def F10C(self, **kwargs):
                return [{"name": "最新提示"}]
            def F10(self, **kwargs):
                return "synthetic finance text"
            def finance(self, **kwargs):
                return SimpleNamespace(empty=False)
        tcp = Tcp()
        def factory(**kwargs):
            calls.append(kwargs)
            return tcp
        return fn, {"Quotes": SimpleNamespace(factory=factory), "_probe": lambda *a, **kw: True}, calls, tcp

    def test_publication_manifest_separates_original_and_local_integrity(self):
        with deny_network():
            manifest = source_manifest()
            metadata = public.upstream_metadata()
            catalog_snapshot = catalog()["snapshot"]
        self.assertEqual(manifest["upstream_skill_sha256"],
                         public.UPSTREAM_SKILL_SHA256)
        self.assertEqual(manifest["skill_sha256"], public.SKILL_SHA256)
        self.assertNotEqual(manifest["skill_sha256"], manifest["upstream_skill_sha256"])
        self.assertEqual(manifest["mirror_kind"], "publication_derivative")
        self.assertEqual(metadata["upstream_skill_sha256"], public.UPSTREAM_SKILL_SHA256)
        self.assertEqual(metadata["publication_skill_sha256"], public.SKILL_SHA256)
        self.assertNotEqual(metadata["publication_skill_sha256"],
                            metadata["upstream_skill_sha256"])
        self.assertEqual(catalog_snapshot["upstream_skill_sha256"],
                         public.UPSTREAM_SKILL_SHA256)
        self.assertEqual(catalog_snapshot["skill_sha256"], public.SKILL_SHA256)
        self.assertNotEqual(catalog_snapshot["skill_sha256"],
                            catalog_snapshot["upstream_skill_sha256"])

    def test_publication_copy_has_no_contact_or_hardcoded_peers(self):
        base = ROOT / "astock_public" / "_upstream"
        email = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
        peer = re.compile(r"\(\s*['\"]\d{1,3}(?:\.\d{1,3}){3}['\"]\s*,\s*7709\s*\)")
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix.lower() in (".png", ".jpg", ".gif"):
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(base)):
                self.assertIsNone(email.search(text))
                self.assertIsNone(peer.search(text))
        self.assertIn("_TDX_SERVERS = []", (base / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("ASTOCK_TDX_SERVERS", (base / "SKILL.md").read_text(encoding="utf-8"))

    def test_default_peer_choice_uses_library_discovery(self):
        with deny_network():
            fn, replacements, calls, tcp = self.factory_scope()
            with patch.dict(fn.__globals__, replacements), patch.dict("os.environ", {"ASTOCK_TDX_SERVERS": ""}):
                self.assertIs(fn(check="finance"), tcp)
        self.assertEqual(calls, [{"market": "std", "bestip": True}])

    def test_explicit_peer_and_bad_configuration_do_not_use_unintended_network(self):
        with deny_network():
            fn, replacements, calls, tcp = self.factory_scope()
            with patch.dict(fn.__globals__, replacements), patch.dict("os.environ", {
                    "ASTOCK_TDX_SERVERS": '[["example.com",7709]]'}):
                self.assertIs(fn(check="finance"), tcp)
            self.assertEqual(calls, [{"market": "std", "server": ("example.com", 7709)}])
            calls.clear()
            for invalid in ("not-json", "{}", '[["example.com",true]]', '[["example.com",0]]'):
                with self.subTest(invalid=invalid), patch.dict(fn.__globals__, replacements), \
                     patch.dict("os.environ", {"ASTOCK_TDX_SERVERS": invalid}), self.assertRaises(ValueError):
                    fn(check="finance")
            self.assertEqual(calls, [])

    def test_derivation_preserves_definitions_without_executing_demos(self):
        from astock_public.refresh_upstream import derive_skill, shape
        fixture = ('contact: user@example.com\n```python\n_TDX_SERVERS = [\n'
                   '    ("192.0.2.1", 7709),\n]\n'
                   'def tdx_client():\n    for ip, port in _TDX_SERVERS:\n        pass\n'
                   'raise RuntimeError("demo must never run")\n```\n')
        with deny_network():
            result = derive_skill(fixture)
        self.assertNotIn("user@example.com", result)
        self.assertNotIn("192.0.2.1", result)
        self.assertEqual(shape(fixture), shape(result))
        self.assertIn('raise RuntimeError("demo must never run")', result)

    def test_loader_regeneration_is_idempotent(self):
        from astock_public.refresh_upstream import loader_for
        template = (ROOT / "astock_public/source_snapshot.py").read_text(encoding="utf-8")
        first = loader_for(template, public.SKILL_SHA256, 7711)
        self.assertEqual(first, template)
        self.assertEqual(loader_for(first, public.SKILL_SHA256, 7711), first)


class TdxClientMethodTests(unittest.TestCase):
    class FakeTcp:
        def __init__(self):
            self.client = self
            self.closed = 0
            self.calls = []
        def finance(self, **kwargs):
            self.calls.append(("finance", kwargs))
            return [{"symbol": kwargs["symbol"], "eps": 1.5}]
        def F10C(self, **kwargs):
            self.calls.append(("F10C", kwargs))
            return [{"name": "latest"}, {"name": "risk"}]
        def F10(self, **kwargs):
            self.calls.append(("F10", kwargs))
            return "synthetic text " + kwargs["name"]
        def bars(self, **kwargs):
            self.calls.append(("bars", kwargs))
            raise RuntimeError("synthetic TCP method failure")
        def disconnect(self):
            self.closed += 1

    def run_query(self, capability, tcp, **params):
        registry = CapabilityRegistry(fetch=lambda *a, **kw: b"")
        original = registry.function
        factory_calls = []
        def factory(**kwargs):
            factory_calls.append(kwargs)
            return tcp
        def resolve(name):
            return factory if name == "tdx_client" else original(name)
        with deny_network(), patch.object(registry, "function", side_effect=resolve), \
             patch("astock_public.registry._dependency_state", return_value={"available": True}):
            result = registry.call(capability, **params)
        return result, factory_calls[0]

    def test_finance_query_executes_finance_and_disconnects(self):
        tcp = self.FakeTcp()
        result, factory = self.run_query("fundamentals.tdx_finance", tcp, code="688017.SH")
        self.assertEqual(factory, {"check": "finance"})
        self.assertEqual(result, [{"symbol": "688017", "eps": 1.5}])
        self.assertEqual(tcp.calls, [("finance", {"symbol": "688017"})])
        self.assertEqual(tcp.closed, 1)

    def test_f10_query_reads_actual_categories_and_returns_text(self):
        tcp = self.FakeTcp()
        result, factory = self.run_query("fundamentals.tdx_f10", tcp, code="688017.SH", category="risk")
        self.assertEqual(factory, {"check": "finance"})
        self.assertEqual(result, [{"symbol": "688017", "category": "risk", "text": "synthetic text risk"}])
        self.assertEqual(tcp.calls[-1], ("F10", {"symbol": "688017", "name": "risk"}))
        self.assertEqual(tcp.closed, 1)

    def test_method_failure_still_releases_the_tcp_connection(self):
        tcp = self.FakeTcp()
        with self.assertRaisesRegex(RuntimeError, "synthetic TCP method failure"):
            self.run_query("bars.mootdx_legacy", tcp, code="688017.SH", frequency=9)
        self.assertEqual(tcp.calls, [("bars", {"symbol": "688017", "frequency": 9})])
        self.assertEqual(tcp.closed, 1)


class CatalogAndCliTests(unittest.TestCase):
    def test_filters_and_uncounted_rows(self):
        with deny_network():
            all_rows = catalog()["capabilities"]
            counted = catalog(include_uncounted=False)["capabilities"]
            current = catalog(temporal="current_snapshot")["capabilities"]
            eastmoney = catalog(source="东财")["capabilities"]
            bse = catalog(market="BJ")["capabilities"]
        self.assertEqual(len(all_rows), 88)
        self.assertEqual(len(counted), 86)
        self.assertGreater(len(current), 5)
        self.assertGreater(len(eastmoney), 10)
        self.assertTrue(all("BJ" in row["markets"] for row in bse))
        self.assertTrue(all(row["counted"] for row in counted))

    def test_cli_catalog_has_no_import_time_or_demo_network(self):
        with deny_network():
            completed = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", str(ROOT / "astock_public" / "client.py"), "catalog"],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["counts"]["capability_endpoints"], 87)
        self.assertFalse(payload["network_accessed"])

    def test_cli_manifest_and_blockers_are_offline(self):
        with deny_network():
            for action in ("manifest", "blockers", "dependencies"):
                result = subprocess.run(
                    [sys.executable, "-B", "-X", "utf8", str(ROOT / "astock_public" / "client.py"), action],
                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
                self.assertEqual(json.loads(result.stdout)["status"], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
