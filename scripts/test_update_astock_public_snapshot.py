#!/usr/bin/env python3
"""Behaviour tests for the pinned-snapshot refresh.

The refresh used to overwrite the published package file by file and derive the
publication copy afterwards. An interruption therefore left a half-updated (and
possibly half-redacted) package that later runs and audits would trust. The rule
these tests pin down: the destination is either the complete new snapshot or
exactly what it was before -- never a mixture.

Everything runs offline on temporary directories; the real package is never
touched.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

MODULE_PATH = Path(__file__).resolve().with_name("update_astock_public_snapshot.py")


def load_module():
    spec = importlib.util.spec_from_file_location("update_astock_public_snapshot", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


SKILL = """---
name: sample-skill
version: 1.0.0
---

# Sample

> Maintainer contact: an.author@example.net is reachable.

```python
_TDX_SERVERS = [
    ("tdx-primary.example.net", 7709),
    ("tdx-backup.example.net", 7709),
]

def tdx_client(market="std"):
    \"\"\"Create a client.\"\"\"
    for ip, port in _TDX_SERVERS:
        print(ip, port)
```

```python
def second_entry():
    return 2
```
"""

README = "# Sample\n\nIssues: helper@example.net\n"
TESTS = '''"""fixture test file"""

URL_A = "http://upstream-a.example.net/history"
URL_B = "http://upstream-b.example.net/history"


def test_placeholder():
    assert URL_A and URL_B
'''

OLD_SKILL = "# old published skill\nold-contact@example.net\n"


def tree(root: Path) -> dict[str, bytes]:
    """Byte-level picture of a directory, so "unchanged" can be asserted exactly."""
    return {str(path.relative_to(root)).replace(os.sep, "/"): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


class SnapshotFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.source = self.base / "upstream"
        self.write_source()
        # The digest guard compares against one pinned upstream revision; the
        # fixture is its own revision, so pin the fixture instead of editing it.
        patcher = mock.patch.object(
            self.module, "ORIGINAL_SKILL_SHA256",
            hashlib.sha256((self.source / "SKILL.md").read_bytes()).hexdigest())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dest = self.base / "pkg" / "_upstream"
        self.write_existing_dest()

    def write_source(self) -> None:
        # Bytes, not text: text mode would translate LF to CRLF on Windows and
        # change the digest the guard checks.
        for name, body in (
            ("SKILL.md", SKILL),
            ("README.md", README),
            ("README_en.md", README),
            ("LICENSE", "MIT\n"),
            ("CHANGELOG.md", "v1\n"),
            ("THIRD_PARTY_NOTICES.md", "notices\n"),
            ("tests/__init__.py", ""),
            ("tests/test_v39_sources.py", TESTS),
            ("tests/fixtures/sina_adjust/sample.csv", "day,factor\n2026-01-05,1.0\n"),
            ("docs/guide.md", "# guide\n"),
        ):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body.encode("utf-8"))

    def write_existing_dest(self) -> None:
        """A published snapshot that predates this run."""
        self.dest.mkdir(parents=True, exist_ok=True)
        for name, body in (
            ("SKILL.md", OLD_SKILL),
            ("LICENSE", "MIT\n"),
            ("obsolete.md", "only in the old package\n"),
            ("tests/test_v39_sources.py", TESTS),
            ("tests/fixtures/sina_adjust/sample.csv", "day,factor\n"),
        ):
            path = self.dest / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body.encode("utf-8"))

    def siblings(self, marker: str) -> list[str]:
        return [path.name for path in self.dest.parent.iterdir()
                if path.is_dir() and marker in path.name]

    def staging_leftovers(self) -> list[str]:
        return self.siblings(".staging-")

    def backup_leftovers(self) -> list[str]:
        return self.siblings(".bak")


class FailureLeavesTargetUntouched(SnapshotFixture):
    """The audit finding: a failure mid-refresh must not publish a partial package."""

    def test_derivation_failure_leaves_the_target_byte_identical(self):
        before = tree(self.dest)
        with mock.patch.object(self.module, "derive_skill",
                               side_effect=SystemExit("upstream TCP peer shape changed")):
            with self.assertRaises(SystemExit):
                self.module.copy_snapshot(self.source, self.dest)
        self.assertEqual(tree(self.dest), before,
                         "a failed derivation must not touch the published snapshot")
        self.assertEqual(self.staging_leftovers(), [],
                         "the scratch directory must be cleaned up")

    def test_copy_failure_leaves_the_target_byte_identical(self):
        before = tree(self.dest)
        real_copy = shutil.copy2
        calls = {"n": 0}

        def flaky_copy(src, dst, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError(28, "No space left on device")
            return real_copy(src, dst, *args, **kwargs)

        with mock.patch.object(self.module.shutil, "copy2", side_effect=flaky_copy):
            with self.assertRaises(OSError):
                self.module.copy_snapshot(self.source, self.dest)
        self.assertEqual(tree(self.dest), before)
        self.assertEqual(self.staging_leftovers(), [])

    def test_verification_failure_leaves_the_target_byte_identical(self):
        # A staging tree that fails its own check must never be published: here
        # the contact redaction is sabotaged, which verification has to notice.
        before = tree(self.dest)
        real_derive = self.module.derive_skill

        def leaky_derive(text: str) -> str:
            return real_derive(text).replace("<UPSTREAM_CONTACT_REDACTED>",
                                             "leaked@example.net")

        with mock.patch.object(self.module, "derive_skill", side_effect=leaky_derive):
            with self.assertRaises(SystemExit):
                self.module.copy_snapshot(self.source, self.dest)
        self.assertEqual(tree(self.dest), before)
        self.assertEqual(self.staging_leftovers(), [])

    def test_a_failed_swap_restores_the_previous_snapshot(self):
        before = tree(self.dest)
        real_rename = os.rename
        state = {"calls": 0}

        def flaky_rename(src, dst):
            state["calls"] += 1
            if state["calls"] == 2:  # staging -> dest
                raise PermissionError(13, "Access is denied")
            return real_rename(src, dst)

        with mock.patch.object(self.module.os, "rename", side_effect=flaky_rename):
            with self.assertRaises(PermissionError):
                self.module.copy_snapshot(self.source, self.dest)
        self.assertEqual(tree(self.dest), before,
                         "the renamed-away snapshot must be moved back")
        self.assertEqual(self.backup_leftovers(), [],
                         "a restored .bak must not be left lying around")

    def test_a_fresh_install_failure_creates_nothing(self):
        fresh = self.base / "fresh" / "_upstream"
        self.assertFalse(fresh.exists())
        with mock.patch.object(self.module, "derive_skill",
                               side_effect=SystemExit("upstream TCP peer shape changed")):
            with self.assertRaises(SystemExit):
                self.module.copy_snapshot(self.source, fresh)
        self.assertFalse(fresh.exists(), "a failed first install must not appear half-built")
        self.assertEqual([p.name for p in fresh.parent.iterdir() if ".staging-" in p.name], [])


class SuccessfulRefresh(SnapshotFixture):
    def test_the_whole_package_is_replaced_and_redacted(self):
        stats = self.module.copy_snapshot(self.source, self.dest)
        files = tree(self.dest)
        self.assertNotIn("obsolete.md", files,
                         "the new snapshot replaces the package instead of merging into it")
        self.assertIn("docs/guide.md", files)
        self.assertIn("tests/fixtures/sina_adjust/sample.csv", files)
        skill = files["SKILL.md"].decode("utf-8")
        self.assertNotIn("an.author@example.net", skill)
        self.assertNotIn("tdx-primary.example.net", skill, "hard-coded peers must not be published")
        self.assertIn("ASTOCK_TDX_SERVERS", skill)
        self.assertNotIn("helper@example.net",
                         files["README.md"].decode("utf-8"))
        self.assertIn("upstream-a.example.net/history", files["tests/test_v39_sources.py"].decode("utf-8"))
        self.assertEqual(stats["upstream_skill_sha256"],
                         hashlib.sha256((self.source / "SKILL.md").read_bytes()).hexdigest())
        self.assertEqual(stats["derived_skill_sha256"],
                         hashlib.sha256(files["SKILL.md"]).hexdigest())
        self.assertEqual(stats["files"], len(files))
        self.assertEqual(self.staging_leftovers(), [])
        self.assertEqual(self.backup_leftovers(), [],
                         "the .bak is removed once the swap succeeded")

    def test_a_fresh_install_publishes_a_complete_package(self):
        fresh = self.base / "fresh" / "_upstream"
        self.module.copy_snapshot(self.source, fresh)
        files = tree(fresh)
        self.assertIn("SKILL.md", files)
        self.assertIn("LICENSE", files)
        self.assertIn("tests/test_v39_sources.py", files)

    def test_repeated_refreshes_are_stable(self):
        self.module.copy_snapshot(self.source, self.dest)
        first = tree(self.dest)
        self.module.copy_snapshot(self.source, self.dest)
        self.assertEqual(tree(self.dest), first,
                         "re-running the refresh must not depend on the previous package")
        self.assertEqual(self.backup_leftovers(), [])

    def test_an_unexpected_upstream_revision_is_refused(self):
        before = tree(self.dest)
        with mock.patch.object(self.module, "ORIGINAL_SKILL_SHA256", "0" * 64):
            with self.assertRaises(SystemExit):
                self.module.copy_snapshot(self.source, self.dest)
        self.assertEqual(tree(self.dest), before)


class CliContract(SnapshotFixture):
    def test_main_refuses_a_directory_that_is_not_an_upstream_checkout(self):
        empty = self.base / "empty"
        empty.mkdir()
        before = tree(self.dest)
        argv = sys.argv
        sys.argv = ["update_astock_public_snapshot.py", str(empty)]
        try:
            with mock.patch.object(self.module, "DEST", self.dest):
                with self.assertRaises(SystemExit):
                    self.module.main()
        finally:
            sys.argv = argv
        self.assertEqual(tree(self.dest), before,
                         "a refused source must not change the published package")


if __name__ == "__main__":
    unittest.main()
