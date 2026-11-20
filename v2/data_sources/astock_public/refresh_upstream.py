"""Derive a publishable, pinned upstream overlay without executing examples.

Default: verify an existing component against a supplied original checkout.
--output-root: write a reviewable overlay to a NEW directory; never auto-fetch
or overwrite the installed component. The caller reviews/applies the overlay.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import ipaddress
import json
import re
from pathlib import Path

ORIGINAL_SKILL_SHA256 = "304e7cc270b595c3031b3c9bd5431d6617ae684b66396092bebbc9f8622f60a6"
_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_BLOCKS = re.compile(r"^```python[^\n]*\n(.*?)^```", re.M | re.S)
_PEER_CONFIG = '''    import json, os
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


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def shape(text: str) -> tuple[int, list[str]]:
    blocks = _BLOCKS.findall(text)
    names = [node.name for block in blocks for node in ast.parse(block).body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return len(blocks), names


def derive_skill(original: str) -> str:
    """Remove publication-only contact/peer details; retain every definition."""
    result = _EMAIL.sub("<UPSTREAM_CONTACT_REDACTED>", original)
    result, count = re.subn(
        r"_TDX_SERVERS = \[.*?\n\]",
        "# TCP peer selection belongs to mootdx or explicit runtime configuration.\n_TDX_SERVERS = []",
        result, count=1, flags=re.S)
    if count != 1 or result.count("    for ip, port in _TDX_SERVERS:\n") != 1:
        raise ValueError("upstream_peer_shape_changed")
    result = result.replace("    for ip, port in _TDX_SERVERS:\n", _PEER_CONFIG, 1)
    result = result.replace("# 实测可用的备选服务器（按延迟排序，2026-06 验证）",
                            "# 发布副本不含硬编码公网地址；默认使用下方 mootdx bestip/factory 回退。", 1)
    result = result.replace("顺序探测 _TDX_SERVERS", "顺序探测显式 ASTOCK_TDX_SERVERS 配置（默认空）", 1)
    if shape(original) != shape(result):
        raise ValueError("upstream_definitions_changed")
    for match in re.finditer(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", result):
        # A browser UA's four-part Chrome version is not a network endpoint.
        if result[:match.start()].endswith("Chrome/"):
            continue
        address = match.group()
        try:
            public = ipaddress.ip_address(address).is_global
        except ValueError:
            continue
        if public:
            raise ValueError("publication_contains_public_address")
    if _EMAIL.search(result):
        raise ValueError("publication_contains_contact_address")
    return result


def fixture_hosts(text: str) -> str:
    """Keep source-URL distinctions, using reserved fixture URLs only."""
    return re.sub(r"https?://([abx])(?=[/\s\"'|]|$)",
                  lambda match: "https://example.com/fixture-" + match.group(1), text)


def loader_for(template: str, digest: str, lines: int) -> str:
    template = template.replace("We\npreserve that source verbatim and load only imports, constants and definitions",
                                "We\nretain a documented publication derivative and load only imports, constants and definitions", 1)
    if "UPSTREAM_SKILL_SHA256 =" not in template:
        template = re.sub(r'^SKILL_SHA256 = "[0-9a-f]{64}"',
                          'UPSTREAM_SKILL_SHA256 = "' + ORIGINAL_SKILL_SHA256 + '"\nSKILL_SHA256 = "' + digest + '"',
                          template, count=1, flags=re.M)
    else:
        template = re.sub(r'^SKILL_SHA256 = "[0-9a-f]{64}"',
                          'SKILL_SHA256 = "' + digest + '"', template, count=1, flags=re.M)
    template = re.sub(r"^SKILL_LINES = \d+", "SKILL_LINES = " + str(lines), template, count=1, flags=re.M)
    if "upstream_skill_sha256=UPSTREAM_SKILL_SHA256" not in template:
        template = template.replace("license=UPSTREAM_LICENSE, url=UPSTREAM_URL, skill_sha256=digest,",
                                    "license=UPSTREAM_LICENSE, url=UPSTREAM_URL, skill_sha256=digest,\n"
                                    "                upstream_skill_sha256=UPSTREAM_SKILL_SHA256,\n"
                                    "                mirror_kind=\"publication_derivative\",", 1)
    if "publication contacts removed;" not in template:
        template = template.replace("old stock-flow", "publication contacts removed; TCP peers use mootdx or ASTOCK_TDX_SERVERS; old stock-flow", 1)
    ast.parse(template)
    return template


def overlay(source_root: Path, component: Path) -> tuple[dict[str, bytes], dict]:
    raw = (source_root / "SKILL.md").read_bytes()
    if sha256(raw) != ORIGINAL_SKILL_SHA256:
        raise ValueError("original_upstream_checksum_mismatch")
    original = raw.decode("utf-8").replace("\r\n", "\n")
    cleaned = derive_skill(original)
    count, names = shape(cleaned)
    if (count, len(names), len(set(names))) != (91, 182, 181):
        raise ValueError("original_upstream_shape_changed")
    files = {"_upstream/SKILL.md": cleaned.encode("utf-8")}
    for name in ("README.md", "README_en.md"):
        files["_upstream/" + name] = _EMAIL.sub("<UPSTREAM_CONTACT_REDACTED>",
            (source_root / name).read_text(encoding="utf-8")).encode("utf-8")
    files["_upstream/tests/test_v39_sources.py"] = fixture_hosts(
        (source_root / "tests/test_v39_sources.py").read_text(encoding="utf-8")).encode("utf-8")
    digest = sha256(files["_upstream/SKILL.md"])
    files["source_snapshot.py"] = loader_for(
        (component / "source_snapshot.py").read_text(encoding="utf-8"), digest,
        len(cleaned.splitlines())).encode("utf-8")
    report = dict(status="ok", upstream_commit="f21534946c42be3ad048ac938eecb1bad84cb8ab",
                  original_sha256=ORIGINAL_SKILL_SHA256, derived_sha256=digest,
                  python_blocks=count, function_definitions=len(names), unique_functions=len(set(names)),
                  derived_lines=len(cleaned.splitlines()),
                  matches_installed={name: (component / name).is_file() and
                                     (component / name).read_bytes() == body for name, body in files.items()})
    return files, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args(argv)
    files, report = overlay(args.source_root, Path(__file__).resolve().parent)
    if args.output_root is not None:
        if args.output_root.exists():
            parser.error("output root already exists; use a new directory")
        for name, body in files.items():
            target = args.output_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        (args.output_root / "manifest.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["mode"] = "reviewable_overlay"
        result = 0
    else:
        report["mode"] = "read_only_check"
        result = 0 if all(report["matches_installed"].values()) else 1
    print(json.dumps(report, ensure_ascii=False))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
