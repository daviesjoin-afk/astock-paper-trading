#!/usr/bin/env python3
"""Refresh the pinned upstream static snapshot used by ``astock_public``.

This is an explicit maintainer tool. It copies the trusted upstream checkout
into ``v2/data_sources/astock_public/_upstream`` without executing upstream code
or using the network. ``SKILL.md`` is converted to a documented publication
derivative: personal contact addresses are replaced, hard-coded TCP peers are
removed and an explicit runtime peer override is installed. The tool prints
both the original upstream digest and the shipped local digest. Other files
are byte copies with normalized text line endings.

The published package is replaced as a whole, never file by file. The snapshot
is first built and checked in a scratch directory next to the destination and
only then swapped in, so an interrupted run cannot leave a half-updated or
half-redacted package behind: either the new snapshot is complete, or the old
one is still exactly as it was.

After a refresh, review the diff, update the checksum and shape constants in
``astock_public/source_snapshot.py`` and run the offline regressions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "v2" / "data_sources" / "astock_public" / "_upstream"
CONTACT_PLACEHOLDER = "<UPSTREAM_CONTACT_REDACTED>"
ORIGINAL_SKILL_SHA256 = "304e7cc270b595c3031b3c9bd5431d6617ae684b66396092bebbc9f8622f60a6"
EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
HARDCODED_PEER = re.compile(r"\(\s*['\"]\d{1,3}(?:\.\d{1,3}){3}['\"]\s*,\s*7709\s*\)")
BLOCKS = re.compile(r"^```python[^\n]*\n(.*?)^```", re.M | re.S)
PEER_CONFIG = '''    import json, os
    servers = _TDX_SERVERS
    configured = os.environ.get("ASTOCK_TDX_SERVERS", "").strip()
    if configured:
        try:
            servers = json.loads(configured)
        except (TypeError, ValueError) as exc:
            raise ValueError("ASTOCK_TDX_SERVERS must be a JSON list of [host, port]") from exc
        if not isinstance(servers, list) or not all(
                isinstance(peer, (list, tuple)) and len(peer) == 2
                and isinstance(peer[0], str) and bool(peer[0].strip())
                and type(peer[1]) is int and 1 <= peer[1] <= 65535 for peer in servers):
            raise ValueError("ASTOCK_TDX_SERVERS must contain [host, integer port] pairs")
    for ip, port in servers:
'''

TEXT_FILES = ("SKILL.md", "README.md", "README_en.md", "LICENSE", "CHANGELOG.md",
              "THIRD_PARTY_NOTICES.md")
PUBLISHED_TEXTS = ("SKILL.md", "README.md", "README_en.md")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalized_text(data: bytes) -> str:
    """Normalize line endings; .gitattributes requires LF in the working tree."""
    return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def derive_skill(text: str) -> str:
    result = EMAIL.sub(CONTACT_PLACEHOLDER, text)
    result, count = re.subn(
        r"_TDX_SERVERS = \[.*?\n\]",
        "# TCP peer selection belongs to mootdx or explicit runtime configuration.\n_TDX_SERVERS = []",
        result, count=1, flags=re.S)
    if count != 1 or result.count("    for ip, port in _TDX_SERVERS:\n") != 1:
        raise SystemExit("upstream TCP peer shape changed")
    result = result.replace("    for ip, port in _TDX_SERVERS:\n", PEER_CONFIG, 1)
    result = result.replace("# 实测可用的备选服务器（按延迟排序，2026-06 验证）",
                            "# 发布副本不含硬编码公网地址；默认使用下方 mootdx bestip/factory 回退。", 1)
    result = result.replace("顺序探测 _TDX_SERVERS",
                            "顺序探测显式 ASTOCK_TDX_SERVERS 配置（默认空）", 1)
    original_blocks = BLOCKS.findall(text)
    derived_blocks = BLOCKS.findall(result)
    if [ast_names(block) for block in original_blocks] != [ast_names(block) for block in derived_blocks]:
        raise SystemExit("upstream Python definitions changed during publication")
    if EMAIL.search(result) or HARDCODED_PEER.search(result):
        raise SystemExit("publication contact or hard-coded TCP peer remains")
    return result


def ast_names(block: str) -> list[str]:
    import ast
    return [node.name for node in ast.parse(block).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def fixture_hosts(text: str) -> str:
    """Keep source-URL distinctions using reserved fixture URLs only."""
    return re.sub(r"https?://([abx])(?=[/\s\"'|]|$)",
                  lambda match: "https://example.com/fixture-" + match.group(1), text)


def layout(root: Path) -> tuple[Path, Path]:
    """The (tests, fixtures) directories that live under a snapshot root."""
    tests = root / "tests"
    return tests, tests / "fixtures" / "sina_adjust"


def build_snapshot(source: Path, root: Path) -> dict[str, object]:
    """Write a complete, checked snapshot into ``root`` (which must be new).

    Nothing outside ``root`` is read-modified: the caller owns placement, so a
    failure here can leave ``root`` unusable without touching the published copy.
    """
    original_skill = (source / "SKILL.md").read_bytes()
    original_digest = sha256(original_skill)
    if original_digest != ORIGINAL_SKILL_SHA256:
        raise SystemExit("original_upstream_checksum_mismatch")
    tests, fixtures = layout(root)
    for directory in (root, tests, fixtures, root / "docs"):
        directory.mkdir(parents=True, exist_ok=True)
    for name in TEXT_FILES:
        path = source / name
        if not path.is_file():
            continue
        shutil.copy2(path, root / name)
    for path in (source / "tests").glob("*.py"):
        shutil.copy2(path, tests / path.name)
    for path in (source / "tests" / "fixtures" / "sina_adjust").glob("*"):
        if path.is_file():
            shutil.copy2(path, fixtures / path.name)
    for path in (source / "docs").glob("*.md"):
        shutil.copy2(path, root / "docs" / path.name)
    publications: dict[str, str] = {}
    for name in PUBLISHED_TEXTS:
        if not (root / name).is_file():
            continue
        text = normalized_text((root / name).read_bytes())
        publications[name] = EMAIL.sub(CONTACT_PLACEHOLDER, text)
    if "SKILL.md" not in publications:
        raise SystemExit("staged snapshot is missing SKILL.md")
    publications["SKILL.md"] = derive_skill(publications["SKILL.md"])
    for name, text in publications.items():
        (root / name).write_bytes(text.encode("utf-8"))
    test_file = tests / "test_v39_sources.py"
    test_file.write_bytes(fixture_hosts(normalized_text(test_file.read_bytes())).encode("utf-8"))
    derived = (root / "SKILL.md").read_bytes()
    return dict(upstream_skill_sha256=original_digest,
                derived_skill_sha256=sha256(derived),
                skill_lines=len(normalized_text(derived).splitlines()),
                python_blocks=len(BLOCKS.findall(normalized_text(derived))))


def verify_snapshot(source: Path, root: Path, stats: dict) -> None:
    """Read the bytes back and assert the publication invariants on disk.

    This is the check that makes the swap safe: a truncated write, a lost
    redaction or a dropped file is caught here, while the published copy is
    still untouched.
    """
    for name in ("SKILL.md", "LICENSE"):
        if not (root / name).is_file():
            raise SystemExit(f"staged snapshot is missing {name}")
    skill = normalized_text((root / "SKILL.md").read_bytes())
    if EMAIL.search(skill):
        raise SystemExit("staged snapshot still contains a contact address")
    if HARDCODED_PEER.search(skill):
        raise SystemExit("staged snapshot still contains a hard-coded TCP peer")
    if "_TDX_SERVERS = []" not in skill or "ASTOCK_TDX_SERVERS" not in skill:
        raise SystemExit("staged snapshot lost the runtime peer override")
    if sha256((root / "SKILL.md").read_bytes()) != stats["derived_skill_sha256"]:
        raise SystemExit("staged SKILL.md does not match the reported digest")
    staged_names = [ast_names(block) for block in BLOCKS.findall(skill)]
    source_names = [ast_names(block)
                    for block in BLOCKS.findall(normalized_text((source / "SKILL.md").read_bytes()))]
    if staged_names != source_names:
        raise SystemExit("staged Python definitions differ from upstream")
    for name in PUBLISHED_TEXTS:
        if (root / name).is_file() and EMAIL.search(normalized_text((root / name).read_bytes())):
            raise SystemExit(f"staged {name} still contains a contact address")


def remove_tree(path: Path) -> None:
    """Delete a directory, tolerating the read-only bits git/dirs may leave behind."""
    if not path.exists():
        return

    def make_writable(function, failed, _exc=None):
        try:
            os.chmod(failed, 0o700)
            function(failed)
        except OSError:
            pass

    try:
        shutil.rmtree(path, onexc=make_writable)
    except TypeError:  # Python < 3.12 spelling of the same hook
        shutil.rmtree(path, onerror=make_writable)


def swap_in(staging: Path, dest: Path) -> None:
    """Replace ``dest`` with ``staging`` atomically enough for Windows.

    ``os.replace`` cannot overwrite an existing directory on Windows, so the
    current snapshot is first renamed to a sibling ``.bak``: if the second rename
    fails the backup is renamed back and the published copy is unchanged again.
    """
    backup = None
    if dest.exists():
        backup = dest.with_name(f"{dest.name}.bak-{uuid.uuid4().hex[:8]}")
        os.rename(dest, backup)
    try:
        os.rename(staging, dest)
    except OSError:
        if backup is not None and not dest.exists():
            os.rename(backup, dest)
        raise
    if backup is not None:
        remove_tree(backup)


def copy_snapshot(source: Path, dest: Path | None = None) -> dict[str, object]:
    """Build the snapshot beside ``dest``, verify it, then publish it in one move."""
    dest = Path(dest) if dest is not None else DEST
    if not (source / "SKILL.md").is_file() or not (source / "LICENSE").is_file():
        raise SystemExit(f"not an upstream snapshot: {source}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Same directory, so the final rename stays on one filesystem and is cheap.
    staging = Path(tempfile.mkdtemp(prefix=f".{dest.name}.staging-", dir=dest.parent))
    try:
        stats = build_snapshot(source, staging)
        verify_snapshot(source, staging, stats)
        swap_in(staging, dest)
    except BaseException:
        remove_tree(staging)
        raise
    stats["files"] = sum(1 for path in dest.rglob("*") if path.is_file())
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="path to a trusted upstream checkout")
    args = parser.parse_args()
    print(json.dumps(copy_snapshot(args.source.resolve()), sort_keys=True))
    print("Review the diff, update source_snapshot constants, then run offline regressions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
