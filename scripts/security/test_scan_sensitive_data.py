"""Behaviour tests for the leak scanner itself, not just its exclusions.

Two classes of defect are covered here, both about a gate that *looks* like it
passed:

* an incomplete scan used to exit 0 (git enumeration failed, or the batch blob
  reader stopped early), so a range that was never read looked clean;
* the pull-request pass had no way to scan only the added lines of a diff.

The exit code is the contract (0 clean / 1 violations / 2 incomplete), so every
test below runs the real CLI in a subprocess and asserts the real status. Where
a failure is needed, it is caused by real repository damage (a truncated object,
a broken git environment) rather than by stubbing out the function under test.
"""

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib

MODULE_PATH = pathlib.Path(__file__).with_name("scan-sensitive-data.py")

# A value that matches the scanner's SECRET heuristic (opaque ASCII, has a digit
# and mixed case) but does not trip its fake-sample markers.
SECRET_NEW = 'Q7xLm2Tp9Rx'
SECRET_OLD = 'Z4vB8nM2kLp9'


def load_module():
    spec = importlib.util.spec_from_file_location("scan_sensitive_data", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run_git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


class RepoCase(unittest.TestCase):
    """A throwaway repository with one safe commit; nothing touches the network."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = pathlib.Path(self._tmp.name)
        init = subprocess.run(["git", "init", "-q", str(self.repo)], capture_output=True, text=True)
        self.assertEqual(init.returncode, 0, init.stderr)
        self.commit({"a.txt": "plain line\n"}, "init")

    def commit(self, files, message):
        for name, body in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body.encode("utf-8"))  # bytes: no CRLF translation
        self.assertEqual(run_git(self.repo, "add", "-A").returncode, 0)
        done = run_git(self.repo, "-c", "user.email=author@example.com",
                       "-c", "user.name=author", "commit", "-q", "-m", message)
        self.assertEqual(done.returncode, 0, done.stderr)

    def scan(self, *args, env=None):
        environment = dict(os.environ)
        environment.pop("ASTOCK_SCAN_REMOTE_REFS", None)
        environment.pop("GIT_DIR", None)
        environment.pop("GIT_WORK_TREE", None)
        environment.update(env or {})
        return subprocess.run([sys.executable, str(MODULE_PATH), *args],
                              capture_output=True, text=True, env=environment, cwd=str(self.repo))


class GitEnumerationFailureTest(RepoCase):
    """A `git` failure must never be reported as an empty (clean) result."""

    def test_non_repository_history_scan_exits_2_and_names_the_gap(self):
        # Must sit outside any repository: a subdirectory of the fixture repo
        # still resolves `git -C` to the enclosing repo and scans it happily.
        nowhere = pathlib.Path(tempfile.mkdtemp(prefix="scan-nowhere-"))
        self.addCleanup(shutil.rmtree, nowhere, True)
        done = self.scan("--repo", str(nowhere), "--scope", "history")
        self.assertEqual(done.returncode, 2,
                         "an unscannable history must not exit 0/1\n" + done.stdout + done.stderr)
        self.assertIn("UNVERIFIED", done.stdout)
        self.assertIn("history:", done.stdout)
        self.assertIn("no history was scanned", done.stdout)

    def test_ref_enumeration_failure_inside_a_repository_exits_2(self):
        # A healthy checkout whose git environment is broken: ref enumeration
        # fails while the working tree is perfectly readable.
        done = self.scan("--repo", str(self.repo), "--scope", "history",
                         env={"GIT_DIR": str(self.repo / "missing-git-dir")})
        self.assertEqual(done.returncode, 2,
                         done.stdout + done.stderr)
        self.assertIn("UNVERIFIED", done.stdout)
        self.assertIn("ref enumeration failed", done.stdout)

    def test_the_exit_code_wins_over_findings(self):
        # A secret in the working tree (a violation, exit 1 on its own) must not
        # mask an unscannable history: incomplete wins.
        self.commit({"leak.txt": f"secret = \"{SECRET_NEW}\"\n"}, "leak")
        done = self.scan("--repo", str(self.repo), "--scope", "all",
                         env={"GIT_DIR": str(self.repo / "missing-git-dir")})
        self.assertEqual(done.returncode, 2)

    def test_a_complete_clean_scan_still_exits_0(self):
        done = self.scan("--repo", str(self.repo), "--scope", "history")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn("UNVERIFIED", done.stdout)

    def test_a_complete_scan_with_a_violation_exits_1(self):
        self.commit({"leak.txt": f"secret = \"{SECRET_NEW}\"\n"}, "leak")
        done = self.scan("--repo", str(self.repo), "--scope", "history")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertNotIn("UNVERIFIED", done.stdout)


class HistoryReadInterruptionTest(RepoCase):
    """The `cat-file --batch` reader can stop early; the rest are unread, not clean."""

    def _corrupt_blob(self, name):
        """Truncate one blob's zlib stream, so reading it fails mid-stream."""
        blob = run_git(self.repo, "rev-parse", f"HEAD:{name}")
        self.assertEqual(blob.returncode, 0, blob.stderr)
        sha = blob.stdout.strip()
        path = self.repo / ".git" / "objects" / sha[:2] / sha[2:]
        self.assertTrue(path.is_file(), f"loose object expected at {path}")
        os.chmod(path, 0o644)
        full = zlib.compress(f"blob {len(name)} {name}".encode())
        path.write_bytes(full[: max(4, len(full) // 2)])
        return sha

    def test_truncated_blob_mid_batch_exits_2_and_reports_how_far_it_got(self):
        self.commit({"b1.txt": "first\n", "b2.txt": "second\n", "b3.txt": "third\n"}, "three")
        sha = self._corrupt_blob("b2.txt")
        done = self.scan("--repo", str(self.repo), "--scope", "history")
        self.assertEqual(done.returncode, 2,
                         "an interrupted blob read must not look clean\n" + done.stdout + done.stderr)
        self.assertIn("UNVERIFIED", done.stdout)
        self.assertIn(sha[:10], done.stdout)
        self.assertRegex(done.stdout, r"(\d+ of \d+ blob\(s\) were read|is missing)")

    def test_a_missing_object_is_reported_as_a_gap(self):
        # Deleting an object makes git answer "missing": the run may continue,
        # but it must not call the unscanned blob clean.
        self.commit({"b1.txt": "first\n"}, "one")
        blob = run_git(self.repo, "rev-parse", "HEAD:b1.txt").stdout.strip()
        path = self.repo / ".git" / "objects" / blob[:2] / blob[2:]
        os.chmod(path, 0o644)
        path.write_bytes(b"")
        done = self.scan("--repo", str(self.repo), "--scope", "history")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("UNVERIFIED", done.stdout)

    def test_the_reader_is_not_left_running(self):
        # A leaked `cat-file --batch` child would keep the repository locked on
        # Windows and leak one process per run.
        done = self.scan("--repo", str(self.repo), "--scope", "history")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        still = subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"],
                               capture_output=True, text=True)
        self.assertEqual(still.returncode, 0, "the repository must not stay locked")


class DiffScopeTest(RepoCase):
    """`--scope diff --diff FILE` covers exactly the added lines of a change."""

    def _pr_diff(self):
        self.commit({"cfg.yaml": f"secret = \"{SECRET_OLD}\"\nkeep = 1\n"}, "base with old secret")
        (self.repo / "cfg.yaml").write_bytes(
            f"keep = 1\nsecret = \"{SECRET_NEW}\"\n".encode("utf-8"))
        diff = self.repo / "change.diff"
        produced = subprocess.run(
            ["git", "-C", str(self.repo), "diff", "--unified=3"],
            capture_output=True, text=True)
        self.assertEqual(produced.returncode, 0, produced.stderr)
        diff.write_bytes(produced.stdout.encode("utf-8"))
        self.assertIn("+" + 'secret = "' + SECRET_NEW, produced.stdout)
        return diff

    def test_added_secret_is_found_and_only_the_added_lines_count(self):
        diff = self._pr_diff()
        report = self.repo / "report.json"
        done = self.scan("--repo", str(self.repo), "--scope", "diff",
                         "--diff", str(diff), "--json", str(report))
        self.assertEqual(done.returncode, 1,
                         "an added secret is a violation\n" + done.stdout + done.stderr)
        summary = json.loads(report.read_text(encoding="utf-8"))
        self.assertTrue(summary["coverage"]["complete"], summary["coverage"])
        self.assertEqual(summary["distinct_values"], 1,
                         "the pre-existing secret is context, not an addition")
        self.assertEqual([f["kind"] for f in summary["findings"]], ["SECRET"])
        self.assertTrue(all(loc.startswith("diff:cfg.yaml")
                            for loc in summary["findings"][0]["locations"]),
                        summary["findings"][0]["locations"])

    def test_a_diff_that_only_removes_a_secret_is_clean(self):
        self.commit({"cfg.yaml": f"secret = \"{SECRET_NEW}\"\nkeep = 1\n"}, "base")
        (self.repo / "cfg.yaml").write_bytes(b"keep = 1\n")
        diff = self.repo / "removal.diff"
        diff.write_bytes(subprocess.run(["git", "-C", str(self.repo), "diff"],
                                        capture_output=True, text=True).stdout.encode("utf-8"))
        done = self.scan("--repo", str(self.repo), "--scope", "diff", "--diff", str(diff))
        self.assertEqual(done.returncode, 0,
                         "a removal introduces nothing to the repository\n" + done.stdout)

    def test_scope_diff_without_a_diff_file_is_incomplete(self):
        done = self.scan("--repo", str(self.repo), "--scope", "diff")
        self.assertEqual(done.returncode, 2)
        self.assertIn("UNVERIFIED", done.stdout)
        self.assertIn("--diff", done.stdout)

    def test_an_empty_diff_is_incomplete_not_clean(self):
        empty = self.repo / "empty.diff"
        empty.write_bytes(b"")
        done = self.scan("--repo", str(self.repo), "--scope", "diff", "--diff", str(empty))
        self.assertEqual(done.returncode, 2, done.stdout)
        self.assertIn("empty", done.stdout)


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
