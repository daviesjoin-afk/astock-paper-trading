"""Unit tests for the leak scanner's own exclusions.

These exist because a shallow CI clone made the scanner report historical merge
commit ids as leaked hex: the known-OID set is built from `git rev-list --all
--objects`, so anything derived from repository history must be excluded by
value, not by pattern.
"""

import importlib.util
import pathlib
import unittest

MODULE_PATH = pathlib.Path(__file__).with_name("scan-sensitive-data.py")


def load_module():
    spec = importlib.util.spec_from_file_location("scan_sensitive_data", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class KnownOidExclusionTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_commit_ids_are_excluded_by_value(self):
        findings = self.module.Findings()
        commit_id = "97c79d81e33adb606e1a19e2aeac184b1f6aae39"[:32]
        findings.known_oids = {commit_id}
        self.module.scan_text(f"| #346 | `{commit_id}` |", "worktree:doc.md", findings)
        self.assertEqual(findings.count(), 0)

    def test_unknown_hex_is_still_reported(self):
        findings = self.module.Findings()
        findings.known_oids = {"a" * 32}
        value = "9f3ac1d47b2e8c05aa61d4e77b093c2f"
        self.module.scan_text(f"x {value}", "worktree:doc.md", findings)
        kinds = {entry["kind"] for entry in findings.by_value.values()}
        self.assertIn("LONG_HEX", kinds)

    def test_allowlist_value_is_excluded(self):
        findings = self.module.Findings()
        allow = self.module.AllowList(str(MODULE_PATH.parent))
        value = "deadbeefdeadbeefdeadbeefdeadbeef"
        allow.values.add(value)
        self.module.scan_text(f"value {value}", "worktree:doc.md", findings, allow=allow)
        self.assertEqual(findings.count(), 0)


if __name__ == "__main__":
    unittest.main()
