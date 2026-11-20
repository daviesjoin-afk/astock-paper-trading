#!/usr/bin/env python3
"""Refresh the pinned upstream static snapshot used by ``astock_public``.

This is an explicit maintainer tool. It copies the trusted upstream checkout
into ``v2/data_sources/astock_public/_upstream`` without executing upstream code
or using the network. ``SKILL.md`` is converted to a documented publication
derivative: personal contact addresses are replaced, hard-coded TCP peers are
removed and an explicit runtime peer override is installed. The tool prints
both the original upstream digest and the shipped local digest. Other files
are byte copies with normalized text line endings.

After a refresh, review the diff, update the checksum and shape constants in
``astock_public/source_snapshot.py`` and run the offline regressions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "v2" / "data_sources" / "astock_public" / "_upstream"
TESTS = DEST / "tests"
FIXTURES = TESTS / "fixtures" / "sina_adjust"
CONTACT_PLACEHOLDER = "<UPSTREAM_CONTACT_REDACTED>"
ORIGINAL_SKILL_SHA256 = "304e7cc270b595c3031b3c9bd5431d6617ae684b66396092bebbc9f8622f60a6"
EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
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
    if EMAIL.search(result) or re.search(r"\(\s*['\"]\d{1,3}(?:\.\d{1,3}){3}['\"]\s*,\s*7709\s*\)", result):
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


def copy_snapshot(source: Path) -> dict[str, object]:
    if not (source / "SKILL.md").is_file() or not (source / "LICENSE").is_file():
        raise SystemExit(f"not an upstream snapshot: {source}")
    original_skill = (source / "SKILL.md").read_bytes()
    original_digest = sha256(original_skill)
    if original_digest != ORIGINAL_SKILL_SHA256:
        raise SystemExit("original_upstream_checksum_mismatch")
    for directory in (DEST, TESTS, FIXTURES, DEST / "docs"):
        directory.mkdir(parents=True, exist_ok=True)
    for name in ("SKILL.md", "README.md", "README_en.md", "LICENSE", "CHANGELOG.md",
                 "THIRD_PARTY_NOTICES.md"):
        path = source / name
        if not path.is_file():
            continue
        shutil.copy2(path, DEST / name)
    for path in (source / "tests").glob("*.py"):
        shutil.copy2(path, TESTS / path.name)
    for path in (source / "tests" / "fixtures" / "sina_adjust").glob("*"):
        if path.is_file():
            shutil.copy2(path, FIXTURES / path.name)
    for path in (source / "docs").glob("*.md"):
        shutil.copy2(path, DEST / "docs" / path.name)
    publications: dict[str, str] = {}
    for name in ("SKILL.md", "README.md", "README_en.md"):
        text = normalized_text((DEST / name).read_bytes())
        publications[name] = EMAIL.sub(CONTACT_PLACEHOLDER, text)
    publications["SKILL.md"] = derive_skill(publications["SKILL.md"])
    for name, text in publications.items():
        (DEST / name).write_bytes(text.encode("utf-8"))
    test_file = TESTS / "test_v39_sources.py"
    test_file.write_bytes(fixture_hosts(normalized_text(test_file.read_bytes())).encode("utf-8"))
    skill = (DEST / "SKILL.md").read_bytes()
    skill_text = normalized_text(skill)
    return dict(upstream_skill_sha256=original_digest,
                derived_skill_sha256=sha256(skill),
                skill_lines=len(skill_text.splitlines()),
                python_blocks=len(BLOCKS.findall(skill_text)),
                files=sum(1 for path in DEST.rglob("*") if path.is_file()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="path to a trusted upstream checkout")
    args = parser.parse_args()
    print(json.dumps(copy_snapshot(args.source.resolve()), sort_keys=True))
    print("Review the diff, update source_snapshot constants, then run offline regressions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
