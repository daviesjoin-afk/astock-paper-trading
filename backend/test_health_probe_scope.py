"""Successful probes may be reused briefly; failures never establish a window."""
import unittest
import datetime as dt
from unittest import mock

import data_fetcher as dfc


class HealthProbeScopeTests(unittest.TestCase):
    def setUp(self):
        self.valid_rows = [{"f12": str(i).zfill(6), "f2": 10., "f3": 0.,
                            "f124": dt.datetime.now().timestamp()} for i in range(20)]
        patches = [
            mock.patch.object(dfc, "load_source_health", return_value={}),
            mock.patch.object(dfc, "_save_source_health"),
            mock.patch.object(dfc, "reset_data_source"),
            mock.patch.object(dfc.time, "sleep"),
            mock.patch.object(dfc, "http_get", return_value=""),
            mock.patch.object(dfc, "fetch_independent_realtime_for_codes", return_value=[]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(dfc, "_fetch_clist", return_value=self.valid_rows)
        self.probe = patch.start()
        self.addCleanup(patch.stop)

    def test_successful_forced_probe_is_reused_only_in_scope(self):
        with dfc.health_probe_scope():
            first = dfc.check_data_source_health(force=True)
            second = dfc.check_data_source_health(force=True)
        self.assertTrue(first["healthy"])
        self.assertEqual(first, second)
        self.assertEqual(self.probe.call_count, 1)
        dfc.check_data_source_health(force=True)
        self.assertEqual(self.probe.call_count, 2)

    def test_forced_calls_without_scope_always_probe(self):
        dfc.check_data_source_health(force=True)
        dfc.check_data_source_health(force=True)
        self.assertEqual(self.probe.call_count, 2)

    def test_failure_never_creates_reuse_window(self):
        self.probe.return_value = []
        with dfc.health_probe_scope():
            self.assertFalse(dfc.check_data_source_health(force=True)["healthy"])
            self.assertFalse(dfc.check_data_source_health(force=True)["healthy"])
        self.assertEqual(self.probe.call_count, 4)

    def test_returned_payload_mutation_does_not_change_cached_evidence(self):
        with dfc.health_probe_scope():
            first = dfc.check_data_source_health(force=True)
            first["eastmoney"]["status"] = "tampered"
            second = dfc.check_data_source_health(force=True)
            second["healthy"] = False
            third = dfc.check_data_source_health(force=True)
        self.assertEqual(second["eastmoney"]["status"], "fresh")
        self.assertTrue(third["healthy"])

    def test_window_expires_and_a_later_failure_remains_retryable(self):
        with mock.patch.object(dfc.time, "monotonic", return_value=100.) as clock:
            with dfc.health_probe_scope():
                dfc.check_data_source_health(force=True)
                clock.return_value = 131.
                self.probe.return_value = []
                self.assertFalse(dfc.check_data_source_health(force=True)["healthy"])
                self.probe.return_value = self.valid_rows
                self.assertTrue(dfc.check_data_source_health(force=True)["healthy"])
        self.assertEqual(self.probe.call_count, 4)

    def test_nested_scope_is_independent_and_restores_parent(self):
        with dfc.health_probe_scope():
            dfc.check_data_source_health(force=True)
            with dfc.health_probe_scope():
                dfc.check_data_source_health(force=True)
            dfc.check_data_source_health(force=True)
        self.assertEqual(self.probe.call_count, 2)

    def test_exception_resets_scope(self):
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with dfc.health_probe_scope():
                dfc.check_data_source_health(force=True)
                raise RuntimeError("abort")
        dfc.check_data_source_health(force=True)
        self.assertEqual(self.probe.call_count, 2)


if __name__ == "__main__":
    unittest.main()
