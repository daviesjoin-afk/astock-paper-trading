#!/usr/bin/env python3
"""Sensitive-data scanner for Git repositories (worktree + full reachable history).

Exit codes:
    0 = clean (every requested scope was fully covered, nothing found)
    1 = violations found, and every requested scope was fully covered
    2 = the scan did NOT complete: some part of the requested scope could not be
        enumerated or read. Findings collected so far are still printed, but a
        clean result is never claimed for the uncovered range. This is a
        failure, not a pass -- a gate must treat it as blocking.

The three ways a scan used to look finished while it was not are now reported
explicitly: a failing `git` command (previously an empty string), a batch blob
read that stopped early (previously a silent `break`), and an empty ref/object
enumeration (previously an early `return` that exited 0).

Designed to run standalone (local pre-push) and as a CI gate. It never prints
raw sensitive values unless --write-raw-file is given, and that file is written
outside the repository by default.

Usage:
    python scan-sensitive-data.py [--repo PATH] [--scope all|worktree|history|diff]
                                  [--diff PATH]
                                  [--write-raw-file PATH] [--max-findings N]
                                  [--json PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import OrderedDict

# --------------------------------------------------------------------------
# Allow-lists: values that are documentation-safe and must not be reported.
# --------------------------------------------------------------------------
SAFE_EMAIL_RE = re.compile(
    r"@users\.noreply\.github\.com$|^noreply@github\.com$|@example\.(?:com|org|net|invalid)$|"
    r"@example$|@[\w.\-]*\.(?:invalid|test|local|localhost)$|@localhost$",
    re.I,
)

SAFE_IPV4 = {
    "127.0.0.1", "0.0.0.0", "255.255.255.255",
}
SAFE_IPV4_PREFIXES = (
    "192.0.2.",      # RFC5737 TEST-NET-1
    "198.51.100.",   # RFC5737 TEST-NET-2
    "203.0.113.",    # RFC5737 TEST-NET-3
)
PRIVATE_IPV4_RE = re.compile(r"^(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)")
SAFE_IPV6 = {"::1", "::", "fe80::", "fd00::", "2001:db8::"}

# Placeholders produced by earlier redaction passes are never violations.
PLACEHOLDER_RE = re.compile(r"\[(?:REDACTED|LOCAL_PATH|REMOTE_PATH|REMOTE_HOST|LOCAL_HOST|SSH_REMOTE|REDACTED_PORT)[^\]]*\]|<(?:LOCAL_PATH|PROJECT_ROOT|USER_HOME|SERVER_PATH|SERVER_HOST|REDACTED[^>]*)>")

SAFE_PATH_MARKERS = (
    "/usr/", "/etc/", "/var/log", "/tmp/", "/app/", "/workspace/", "/data/",
    "/opt/", "/srv/", "/mnt/", "/media/", "/proc/", "/sys/", "/dev/",
    "/root/.cache", "/home/runner", "/github/workspace",
)

SAFE_HOSTS = {
    "localhost", "example.com", "example.org", "example.net", "github.com",
    "api.github.com", "raw.githubusercontent.com", "codeload.github.com",
    "pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "npmjs.com",
    "schema.org", "www.w3.org", "w3.org", "json-schema.org", "python.org",
    "docs.python.org", "fastapi.tiangolo.com", "starlette.io", "uvicorn.org",
    "pydantic-docs.helpmanual.io", "docker.com", "docs.docker.com", "hub.docker.com",
    "openai.com", "platform.openai.com", "openrouter.ai", "finnhub.io",
    "alpaca.markets", "interactivebrokers.com", "ibkr.com", "eastmoney.com",
    "qt.gtimg.cn", "sinajs.cn", "sina.com.cn", "163.com", "tencent.com",
    "baidu.com", "aliyun.com", "csrc.gov.cn", "sse.com.cn", "szse.cn",
    "localhost.localdomain", "host.docker.internal",
    # market-data / vendor documentation and public data endpoints
    "nasdaq.com", "nasdaqtrader.com", "api.nasdaq.com", "sec.gov", "data.sec.gov",
    "finance.yahoo.com", "query1.finance.yahoo.com", "query2.finance.yahoo.com",
    "quantconnect.com", "tradingview.com", "backtrader.com", "nautilustrader.io",
    "interactivebrokers.github.io", "ibkrcampus.com", "cboe.com", "nyse.com",
    "alphavantage.co", "tiingo.com", "polygon.io", "stooq.com", "investing.com",
    "xueqiu.com", "jrj.com.cn", "hexun.com", "cninfo.com.cn", "hkex.com.hk",
    "stripe.com", "docs.stripe.com", "anthropic.com", "docs.anthropic.com",
    "deepseek.com", "api.deepseek.com", "huggingface.co", "kaggle.com",
    "shields.io", "img.shields.io", "badgen.net", "readthedocs.io", "sphinx-doc.org",
    "code.visualstudio.com", "learn.microsoft.com", "microsoft.com", "docs.microsoft.com",
    "nodejs.org", "esbuild.github.io", "echarts.apache.org", "apache.org",
    "gnu.org", "opensource.org", "creativecommons.org", "choosealicense.com",
}
SAFE_HOST_SUFFIXES = (
    ".example.com", ".example.org", ".example.net", ".local", ".test", ".invalid",
    ".internal", ".gov", ".edu", ".wikipedia.org", ".github.io", ".readthedocs.io",
)

# SHA/checksum context: a 32+ hex string next to these words is an artifact digest,
# not a secret. Only applies to the LONG_HEX heuristic.
DIGEST_CONTEXT_RE = re.compile(r"(?i)\b(?:sha[-_]?1|sha[-_]?256|sha[-_]?512|md5|checksum|digest|integrity|etag|hash|blake2|commit)\b")

# Browser/user-agent strings embed version numbers that look like IPv4 addresses.
USER_AGENT_RE = re.compile(r"(?i)(?:mozilla|chrome|safari|firefox|edg|applewebkit|gecko|trident|opera)/")

# IPV6 hits that are really git object ids / hashes, not addresses.
HEXLIKE_RE = re.compile(r"^[0-9a-fA-F:]+$")


def looks_like_hex_id(value: str) -> bool:
    if not HEXLIKE_RE.match(value):
        return False
    hexdigits = value.replace(":", "")
    if not hexdigits:
        return False
    hex_chars = sum(1 for ch in hexdigits.lower() if ch in "abcdef")
    # A real IPv6 address is short and mostly digits; long hex-letter runs are ids.
    return len(hexdigits) > 12 and hex_chars >= len(hexdigits) * 0.25

# Secrets that are obviously fake sample values.
FAKE_SECRET_MARKERS = (
    "example", "your_", "your-", "xxx", "yyy", "zzz", "dummy", "placeholder",
    "changeme", "change_me", "redacted", "fake", "sample", "test_key", "test-key",
    "sk-xxx", "abcdef", "1234567890", "todo", "none", "null", "local-dev",
)

SKIP_DIR_NAMES = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".ruff_cache",
    ".pytest_cache", "dist-packages", "site-packages", ".tox", "htmlcov", ".next", ".cache",
}
BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tgz",
    ".sqlite", ".sqlite3", ".db", ".woff", ".woff2", ".ttf", ".eot", ".so", ".dll",
    ".exe", ".dylib", ".pyc", ".class", ".jar", ".mp4", ".mov", ".bin",
}

# Paths whose opaque content we cannot scan reliably -> manual review.
IMAGE_REVIEW_HINT_RE = re.compile(
    r"(account|portfolio|invoice|statement|screenshot|dashboard|settings|profile|login|email|credential|console|terminal)",
    re.I,
)

# --------------------------------------------------------------------------
# Detection patterns
# --------------------------------------------------------------------------
PATTERNS = OrderedDict()

PATTERNS["PRIVATE_EMAIL"] = re.compile(r"[A-Za-z0-9._%+\-]+@(?:[A-Za-z0-9\-]+\.)+[A-Za-z]{2,}")
PATTERNS["PUBLIC_IPV4"] = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
PATTERNS["IPV6"] = re.compile(r"(?i)(?<![\w:.])(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*::[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*|(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4})(?![\w:.])")
PATTERNS["REMOTE_URL"] = re.compile(r"(?i)\b(?:https?|ssh|git|ftp|sftp|scp|rsync)://(?:[^\s/@]+@)?([A-Za-z0-9.\-]+)(?::\d{1,5})?")
PATTERNS["SSH_TARGET"] = re.compile(r"(?i)\b(?:ssh|scp|sftp|rsync)\b[^\r\n]{0,40}?(?:[a-z_][a-z0-9_.\-]*@[\w.\-]+|(?:\d{1,3}\.){3}\d{1,3})(?::\d{1,5})?")
PATTERNS["WIN_ABS_PATH"] = re.compile(r"(?i)(?<![0-9A-Za-z_:/])\?*[A-Za-z]:[\\/](?:[A-Za-z0-9._~+@%=\-]+[\\/])*[A-Za-z0-9._~+@%=\-]+")
PATTERNS["WIN_USER_PATH"] = re.compile(r"(?i)[A-Za-z]:[\\/]+Users[\\/]+([A-Za-z0-9._\-]+)")
PATTERNS["NIX_ABS_PATH"] = re.compile(r"(?<![A-Za-z0-9._\-])/(?:root|home|Users|mnt|opt|srv|var/www|etc/ssh)(?:/[A-Za-z0-9._~+@%=\-]+)*/?")

# The scanner's own sources contain the patterns and fixtures used to catch leaks;
# flagging them would be self-referential noise.
# The scanner's own sources and the policy docs quote the patterns they detect;
# flagging them would be self-referential noise. Real secrets are still caught
# everywhere else, including in new code added under scripts/.
SCANNER_SELF_RE = re.compile(
    r"(?:^|/)(?:scripts/security/|scan-sensitive-data\.py$|triage-findings\.py$|run-rewrite\.py$)"
    r"|(?:^|/)(?:SECURITY|security)\.md$|(?:^|/)docs?/security[-_/]"
)
# Regex-literal context: 'r"...C:\\Users..."' defines a pattern, it is not a path.
REGEX_LITERAL_RE = re.compile(r"""(?:\bre\.(?:compile|match|search|sub|fullmatch|finditer)\s*\(\s*r?["']|^[^"']*r["'][^"']*$)""")
# 第 2 组记录引号：带引号的右值即使长得像标识符也可能是**字面量口令**，
# 因此"标识符引用豁免"只对未加引号的值生效（评审 P1）。
PATTERNS["SECRET_ASSIGN"] = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key|private[_\-]?key|client[_\-]?secret|"
    r"refresh[_\-]?token|auth[_\-]?token|session[_\-]?id|cookie)\b\s*[:=]\s*([\"']?)([^\s\"',;]{6,})"
)
# Code-shaped right-hand sides are references/comprehensions, not literal secrets.
SECRET_VALUE_NOISE_RE = re.compile(
    r"(?:\w\s*\(|[\[\]{}()<>]|->|::|\.\w+\s*\(|\$|\bself\.|\brow\[|\brequest\.|\bconfig\.|\bpayload\.|\bdata\.|"
    r"\bexisting\.|\bintent\.|\bparams\.|\benv\b|\bkwargs\b|\bvar\b|,\s*$)"
)
# A bare identifier (variable/table-column name) is a reference, not a literal.
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,}$")
# Keyword-argument / dict-field context: the sensitive word is the *field name*.
KEY_CONTEXT_RE = re.compile(
    r"(?i)(?:^|[\s,{(])(?:[a-z_][a-z0-9_]*_)?(?:id|key|token|secret|session|password|pwd)\s*[:=]\s*$"
)
PATTERNS["SECRET_CN"] = re.compile(r"(密码|口令|账密|账号|账户|用户名|登录名)\s*(?:为|是|＝|：|:|=)\s*([^\s，。；、,;）)\]】\"']+)")
PATTERNS["KNOWN_TOKEN"] = re.compile(
    r"(?i)\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_\-]{16,}|"
    r"AIza[0-9A-Za-z_\-]{30,}|xox[baprs]-[0-9A-Za-z\-]{10,}|(?:AKIA|ASIA)[0-9A-Z]{16}|"
    r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,})\b"
)
PATTERNS["PRIVATE_KEY_BLOCK"] = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
PATTERNS["SSH_PUBKEY"] = re.compile(r"\bssh-(?:rsa|ed25519|dss)\s+AAAA[A-Za-z0-9+/=]{20,}")
PATTERNS["BROKER_ACCOUNT"] = re.compile(r"(?i)\b(DU\d{6,}|U\d{6,}|account[_\-]?id\s*[:=]\s*[\"']?[A-Z0-9]{6,})")
PATTERNS["AUTH_HEADER"] = re.compile(r"(?i)\b(?:authorization|proxy-authorization)\s*:\s*[^\r\n]{6,}")
PATTERNS["LONG_HEX"] = re.compile(r"\b[A-Fa-f0-9]{32,}\b")
PATTERNS["DATABASE_URL"] = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^/\s:@]+:[^/\s:@]+@[\w.\-]+")

# Files that are sensitive by nature. Migration/test fixtures and seed data are
# content-scanned like everything else but are not treated as real databases.
FIXTURE_PATH_RE = re.compile(r"(?i)(?:^|/)(?:tests?|fixtures?|migrations?|seeds?|samples?|examples?)/|(?:fixture|legacy|sample|seed|demo|synthetic)")
SENSITIVE_FILE_RE = re.compile(
    r"(?i)(?:^|/)(?:\.env(?:\..+)?|id_(?:rsa|ed25519|ecdsa)|[^/]*\.(?:pem|key|p12|pfx|keystore)|"
    r"(?<!\.env\.)[^/]*\.(?:sqlite3?|db|dump|bak|backup)|[^/]*\.(?:log|out)|"
    r"credentials(?:\.json)?|secrets?(?:\.json|\.ya?ml|\.txt)?|\.netrc|\.htpasswd|kdbx)$"
)
SENSITIVE_FILE_ALLOW = (".env.example", ".env.sample", ".env.template")

# Text extensions scanned in worktree mode. In history mode every blob counts.
TEXT_EXT = {
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".json", ".json5",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".md", ".markdown", ".rst",
    ".txt", ".text", ".html", ".htm", ".css", ".scss", ".sh", ".bash", ".zsh",
    ".ps1", ".psm1", ".psd1", ".bat", ".cmd", ".sql", ".env", ".example", ".dockerfile",
    ".gitignore", ".gitattributes", ".editorconfig", ".properties", ".xml", ".csv", ".tsv",
}
TEXT_BASENAMES = {
    "Dockerfile", "Makefile", "Procfile", "Gemfile", "Rakefile", "LICENSE", "NOTICE",
    "CODEOWNERS", ".gitignore", ".gitattributes", ".dockerignore", ".env.example",
}
# Generated/minified assets: their content mirrors source and produces noise.
GENERATED_PATH_RE = re.compile(
    r"(?i)(?:^|/)(?:dist|build|out|coverage|htmlcov|\.next|\.nuxt|vendor|third_party|"
    r"node_modules|site-packages|\.venv|venv)/"
)


def should_scan_file(rel: str, ext: str) -> bool:
    if ext in BINARY_EXT:
        return False
    if GENERATED_PATH_RE.search(rel):
        return False
    if ext in TEXT_EXT:
        return True
    return os.path.basename(rel) in TEXT_BASENAMES


class AllowList:
    """Repository-level allowlist: documented, reviewable exceptions.

    File format (`.security-allowlist` in the repo root):
        # comment
        host:<exact.hostname>
        path:<relative/path/or/prefix>
        value:<exact value>          # last resort; requires a comment on the line
        suffix:<domain.suffix>
    """

    def __init__(self, repo: str):
        self.hosts: set[str] = set()
        self.suffixes: set[str] = set()
        self.paths: list[str] = []
        self.values: set[str] = set()
        path = os.path.join(repo, ".security-allowlist")
        if not os.path.isfile(path):
            return
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                kind, _, item = line.partition(":")
                item = item.strip()
                if not item:
                    continue
                if kind == "host":
                    self.hosts.add(item.lower())
                elif kind == "suffix":
                    self.suffixes.add(item.lower())
                elif kind == "path":
                    self.paths.append(item.replace("\\", "/"))
                elif kind == "value":
                    self.values.add(item)

    def evaluate_host(self, host: str) -> bool:
        """True when the host is an approved public/infrastructure endpoint."""
        low = host.lower()
        if low in self.hosts or low in SAFE_HOSTS:
            return True
        if low.endswith(SAFE_HOST_SUFFIXES):
            return True
        return any(low.endswith(suffix) for suffix in self.suffixes)

    def evaluate_location(self, location: str) -> bool:
        rel = location.split(":", 1)[1] if location.startswith(("worktree:", "history:")) else location
        rel = rel.split("@", 1)[0].replace("\\", "/")
        return any(rel == p or rel.startswith(p) for p in self.paths)

    def evaluate_value(self, value: str) -> bool:
        return value in self.values


class GitCommandError(RuntimeError):
    """A git command needed for coverage failed. Never silently becomes ""."""

    def __init__(self, args, returncode: int, stderr: str = ""):
        self.argv = list(args)
        self.returncode = returncode
        # Only the masked first line: git echoes paths, and this scanner exists
        # to avoid publishing them.
        self.detail = mask_line(stderr.strip().splitlines()[0]) if stderr.strip() else ""
        super().__init__(f"{' '.join(self.argv[:4])} exited {returncode}: {self.detail}")


class Coverage:
    """Which parts of the requested scope were actually scanned.

    A gate that only counts accumulated findings cannot tell "clean" from
    "never looked". Every gap that a scan could not cover is recorded here, and
    any gap makes the exit code non-zero with the uncovered range spelled out.
    """

    def __init__(self) -> None:
        self.gaps: list = []
        self.scanned: dict = {}

    def gap(self, scope: str, detail: str) -> None:
        entry = f"{scope}: {detail}"
        if entry not in self.gaps:
            self.gaps.append(entry)

    def note(self, scope: str, count: int) -> None:
        self.scanned[scope] = self.scanned.get(scope, 0) + count

    def complete(self) -> bool:
        return not self.gaps


def run(args, cwd=None, *, allow_failure=False) -> str:
    """Run git, and raise instead of pretending the output was empty."""
    proc = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0 and not allow_failure:
        raise GitCommandError(args, proc.returncode, proc.stderr.decode("utf-8", "replace"))
    return proc.stdout.decode("utf-8", "replace")


def is_binary_bytes(data: bytes) -> bool:
    return b"\x00" in data[:8000]


class Findings:
    def __init__(self, keep_preview: bool = True):
        self.by_value: "OrderedDict[str, dict]" = OrderedDict()
        self.ids: "OrderedDict[str, str]" = OrderedDict()
        self.counters: "OrderedDict[str, int]" = OrderedDict()
        self.keep_preview = keep_preview
        # 本仓库自身可达的提交/对象 id：它们是标识符，不是密钥（例如 .gitleaksignore 里的指纹）。
        self.known_oids = set()

    def redaction_id(self, kind: str, value: str) -> str:
        key = f"{kind}\x00{value}"
        if key in self.ids:
            return self.ids[key]
        self.counters[kind] = self.counters.get(kind, 0) + 1
        rid = f"{kind}_{self.counters[kind]:03d}"
        self.ids[key] = rid
        return rid

    def add(self, kind: str, value: str, location: str, extra: str = "", context: str = ""):
        rid = self.redaction_id(kind, value)
        entry = self.by_value.setdefault(value, {"id": rid, "kind": kind, "locations": [], "extra": extra, "preview": ""})
        loc = f"{location} ({extra})" if extra else location
        if loc not in entry["locations"]:
            entry["locations"].append(loc)
        if self.keep_preview and not entry["preview"]:
            entry["preview"] = mask_line(context) if context else preview_value(kind, value)

    def count(self) -> int:
        return len(self.by_value)


# --------------------------------------------------------------------------
# Redacted previews: enough structure to classify a finding, never the value.
# --------------------------------------------------------------------------
def preview_value(kind: str, value: str) -> str:
    try:
        if kind in ("PRIVATE_EMAIL", "PERSONAL_NAME"):
            if "@" not in value:
                return value[:1] + "***"
            local, _, domain = value.partition("@")
            parts = domain.split(".")
            return f"{local[:1]}***@{parts[0][:1]}***.{parts[-1]}"
        if kind in ("PUBLIC_IP", "PRIVATE_IP"):
            parts = value.split(".")
            tag = "private" if kind == "PRIVATE_IP" else "public"
            return f"{parts[0]}.**.**.{parts[-1]} ({tag})"
        if kind == "IPV6":
            return value.split(":")[0][:2] + "::***"
        if kind in ("LOCAL_PATH", "SERVER_PATH"):
            cleaned = value.replace("\\", "/")
            parts = [p for p in cleaned.split("/") if p]
            return f"{cleaned[:3]}<...>/{parts[-1][:12]} (segs={len(parts)})"
        if kind in ("SECRET", "TOKEN", "AUTH_HEADER", "DB_CREDENTIAL_URL", "BROKER_ACCOUNT", "SSH_KEY", "SSH_TARGET"):
            if len(value) <= 4:
                return "*" * len(value)
            return f"{value[:2]}{'*' * min(len(value) - 4, 8)}{value[-2:]} (len={len(value)})"
        if kind == "REMOTE_HOST":
            head = value.split(".")[0]
            tail = value.split(".")[-1]
            return f"{head[:2]}***.{tail}"
        if kind == "LONG_HEX":
            return f"{value[:4]}…{value[-2:]} (len={len(value)})"
        return value[:6] + "…"
    except Exception:  # noqa: BLE001
        return "<preview failed>"


def mask_line(line: str) -> str:
    """Mask every sensitive-looking token in a line of text."""
    masked = SAFE_EMAIL_RE.sub("<SAFE_EMAIL>", line)
    masked = re.sub(r"[A-Za-z0-9._%+\-]+@(?:[A-Za-z0-9\-]+\.)+[A-Za-z]{2,}",
                    lambda m: preview_value("PRIVATE_EMAIL", m.group(0)), masked)
    masked = re.sub(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])",
                    lambda m: preview_value("PUBLIC_IP", m.group(0)), masked)
    masked = re.sub(r"(?i)(?<![0-9A-Za-z_:/])\?*[A-Za-z]:[\\/][^\s\"'`<>|)]*",
                    lambda m: preview_value("LOCAL_PATH", m.group(0)), masked)
    masked = re.sub(r"(?<![A-Za-z0-9._\-])/(?:root|home|Users|mnt|opt|srv|var/www|etc/ssh)[^\s\"'`<>|)]*",
                    lambda m: preview_value("SERVER_PATH", m.group(0)), masked)
    masked = re.sub(r"(?i)\b(?:gh[pousr]_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9_\-]{16,}|AIza[0-9A-Za-z_\-]{30,})",
                    lambda m: preview_value("TOKEN", m.group(0)), masked)
    masked = re.sub(r"(?i)(\b(?:password|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key|private[_\-]?key|"
                    r"client[_\-]?secret|refresh[_\-]?token|auth[_\-]?token|session[_\-]?id|cookie)\b\s*[:=]\s*)([^\s\"',;]{4,})",
                    lambda m: m.group(1) + preview_value("SECRET", m.group(2)), masked)
    masked = re.sub(r"(?i)\b(DU\d{6,})", lambda m: preview_value("BROKER_ACCOUNT", m.group(1)), masked)
    return masked[:220]


def classify_ip(value: str):
    if value in SAFE_IPV4 or value.startswith(SAFE_IPV4_PREFIXES):
        return None
    if PRIVATE_IPV4_RE.match(value):
        return "PRIVATE_IP"
    return "PUBLIC_IP"


def is_fake_secret(value: str) -> bool:
    low = value.lower()
    if PLACEHOLDER_RE.search(value):
        return True
    return any(marker in low for marker in FAKE_SECRET_MARKERS)


def line_of(text: str, index: int) -> str:
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    if end == -1:
        end = len(text)
    return text[start:end].strip()


def scan_text(text: str, location: str, findings: Findings, *, include_long_hex=True, allow: "AllowList | None" = None):
    """Scan one text blob; report findings with masked context. Never returns raw values."""

    def skip(value: str) -> bool:
        # 路径白名单只用于文件性质（SENSITIVE_FILE）判定，**不**豁免内容命中：
        # 否则 path:deploy/ 这类条目会让整个目录里的真实密钥不被扫描（评审 P1）。
        return bool(allow) and allow.evaluate_value(value)

    for kind, pattern in PATTERNS.items():
        if kind == "LONG_HEX" and not include_long_hex:
            continue
        for match in pattern.finditer(text):
            ctx = line_of(text, match.start())
            value = match.group(0)

            if kind == "PRIVATE_EMAIL":
                if SAFE_EMAIL_RE.search(value) or skip(value):
                    continue
                findings.add("PRIVATE_EMAIL", value, location, context=ctx)

            elif kind == "PUBLIC_IPV4":
                octets = value.split(".")
                if len(octets) != 4 or any(not o.isdigit() or int(o) > 255 for o in octets):
                    continue
                # Product/UA version strings (Chrome/117.0.0.0) are not addresses.
                if USER_AGENT_RE.search(ctx):
                    continue
                # Numbers embedded in slash/dot-delimited literals (SVG paths, regexes,
                # version tuples) are not addresses.
                before = text[match.start() - 1] if match.start() > 0 else ""
                after = text[match.end()] if match.end() < len(text) else ""
                if before in "/\\" or after in "/\\" or before.isdigit() or after.isdigit():
                    continue
                if skip(value):
                    continue
                bucket = classify_ip(value)
                if bucket:
                    findings.add(bucket, value, location, context=ctx)

            elif kind == "IPV6":
                low = value.lower()
                if low in SAFE_IPV6 or low.startswith(("2001:db8", "fe80:", "fd", "fc")):
                    continue
                if looks_like_hex_id(value) or skip(value):
                    continue
                findings.add("IPV6", value, location, context=ctx)

            elif kind == "REMOTE_URL":
                host = match.group(1)
                if (allow and allow.evaluate_host(host)) or re.match(r"^127\.|^0\.0\.0\.0$|^localhost$", host, re.I):
                    continue
                findings.add("REMOTE_HOST", host, location, context=ctx)

            elif kind == "SSH_TARGET":
                target = match.group(0).strip()
                if skip(target):
                    continue
                findings.add("SSH_TARGET", target, location, context=ctx)

            elif kind in ("WIN_ABS_PATH", "WIN_USER_PATH"):
                # CJK text glued to a path means the regex over-matched prose.
                if skip(value) or REGEX_LITERAL_RE.search(ctx) or any("\u3000" <= ch <= "\u9fff" for ch in value):
                    continue
                findings.add("LOCAL_PATH", value, location, context=ctx)

            elif kind == "NIX_ABS_PATH":
                # A bare mount point ("/opt", "/srv") is generic; only paths with a
                # real project/user segment are identifying.
                segments = [seg for seg in value.strip("/").split("/") if seg]
                if (
                    len(segments) < 2
                    or any(marker in value for marker in SAFE_PATH_MARKERS[:10])
                    or skip(value)
                    or REGEX_LITERAL_RE.search(ctx)
                    or any("\u3000" <= ch <= "\u9fff" for ch in value)
                ):
                    continue
                findings.add("SERVER_PATH", value, location, context=ctx)

            elif kind in ("SECRET_ASSIGN", "SECRET_CN"):
                candidate = match.group(match.lastindex)
                quoted = kind == "SECRET_ASSIGN" and bool(match.group(match.lastindex - 1))
                if len(candidate) < 8 or is_fake_secret(candidate):
                    continue
                # A real credential is opaque ASCII; prose/punctuation/identifiers are not.
                if not re.fullmatch(r"[A-Za-z0-9._~+/=\-]{8,}", candidate):
                    continue
                if not re.search(r"[0-9]", candidate) and not re.search(r"[a-z][A-Z]|[A-Z][a-z]", candidate):
                    continue
                if candidate.startswith(("[", "<")) and candidate.endswith(("]", ">")):
                    continue
                # 未加引号才按"代码引用"豁免：带引号的右值是字面量，必须照常判定。
                if kind == "SECRET_ASSIGN" and not quoted and (
                    SECRET_VALUE_NOISE_RE.search(candidate)
                    or candidate.endswith(("(", ")", "[", "]", "{", "}", ",", ":"))
                ):
                    continue
                # `api_key=api_key` / `session_id=session_id` is a reference, not a value
                if kind == "SECRET_ASSIGN" and not quoted and (
                    IDENTIFIER_RE.match(candidate) or KEY_CONTEXT_RE.search(ctx)
                ):
                    continue
                if skip(candidate):
                    continue
                findings.add("SECRET", candidate, location, context=ctx)

            elif kind == "KNOWN_TOKEN":
                if is_fake_secret(value) or skip(value):
                    continue
                findings.add("TOKEN", value, location, context=ctx)

            elif kind == "PRIVATE_KEY_BLOCK":
                findings.add("PRIVATE_KEY", "-----BEGIN PRIVATE KEY BLOCK-----", location, context=ctx)

            elif kind == "SSH_PUBKEY":
                findings.add("SSH_KEY", value[:60], location, context=ctx)

            elif kind == "BROKER_ACCOUNT":
                if is_fake_secret(value) or value.upper() in ("DU0", "U0"):
                    continue
                # A real account id is a bare identifier dominated by digits
                # ("DU1234567"). Column/constant names are not.
                token = re.sub(r"(?i)^account[_\-]?id\s*[:=]\s*[\"']?", "", value).strip("\"'")
                if not re.fullmatch(r"[A-Z]{0,3}[0-9]{5,}", token):
                    continue
                if skip(value):
                    continue
                findings.add("BROKER_ACCOUNT", value, location, context=ctx)

            elif kind == "AUTH_HEADER":
                if is_fake_secret(value) or skip(value):
                    continue
                findings.add("SECRET", value, location, context=ctx)

            elif kind == "LONG_HEX":
                if is_fake_secret(value):
                    continue
                # 本仓库自己的对象 id（提交/树/blob）不是密钥。
                if value.lower() in findings.known_oids:
                    continue
                # 32+ hex near sha/checksum words, or inside a tag's `object <sha>`, is a fingerprint.
                window = text[max(0, match.start() - 220): match.end() + 120]
                if DIGEST_CONTEXT_RE.search(window):
                    continue
                if re.search(r"\bobject\s+[0-9a-f]{40}\b", ctx, re.I):
                    continue
                if skip(value):
                    continue
                findings.add("LONG_HEX", value, location, context=ctx)

            elif kind == "DATABASE_URL":
                if is_fake_secret(value) or skip(value):
                    continue
                findings.add("DB_CREDENTIAL_URL", value, location, context=ctx)


def iter_worktree_files(repo: str, coverage: "Coverage | None" = None):
    """Yield (absolute_path, relative_path). Prefers git's own file list so that
    .gitignore'd runtime state (databases, caches, logs) never enters the scan.

    When git fails, the caller is told through `coverage` instead of receiving an
    empty file list that looks exactly like a clean worktree.
    """
    if os.path.isdir(os.path.join(repo, ".git")) or os.path.isfile(os.path.join(repo, ".git")):
        try:
            listed = run(["git", "-C", repo, "ls-files", "--cached", "--others",
                          "--exclude-standard"])
        except GitCommandError as error:
            if coverage is not None:
                coverage.gap("worktree", f"git ls-files failed, no file was scanned ({error})")
            listed = ""
        seen = set()
        for rel in listed.splitlines():
            rel = rel.strip()
            if not rel or rel in seen:
                continue
            seen.add(rel)
            full = os.path.join(repo, rel.replace("/", os.sep))
            if os.path.isfile(full):
                yield full, rel
        return
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
        for name in files:
            path = os.path.join(root, name)
            yield path, os.path.relpath(path, repo).replace("\\", "/")


def scan_worktree(repo: str, findings: Findings, manual_review: list,
                  allow: "AllowList", coverage: "Coverage | None" = None):
    coverage = coverage if coverage is not None else Coverage()
    for path, rel in iter_worktree_files(repo, coverage):
        name = os.path.basename(path)
        ext = os.path.splitext(name)[1].lower()
        if SCANNER_SELF_RE.search(rel):
            continue
        if (
            SENSITIVE_FILE_RE.search(rel)
            and not rel.endswith(SENSITIVE_FILE_ALLOW)
            and not FIXTURE_PATH_RE.search(rel)
            and not allow.evaluate_location(f"worktree:{rel}")
        ):
            findings.add("SENSITIVE_FILE", rel, "worktree")
        if ext in BINARY_EXT:
            if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf") and IMAGE_REVIEW_HINT_RE.search(rel):
                manual_review.append(f"IMAGE_REVIEW | {rel}")
            continue
        if not should_scan_file(rel, ext):
            continue
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as error:
            # A file we cannot read is not a file we cleared.
            coverage.gap("worktree", f"{rel} unreadable ({type(error).__name__})")
            continue
        coverage.note("worktree", 1)
        if is_binary_bytes(data):
            continue
        scan_text(data.decode("utf-8", "replace"), f"worktree:{rel}", findings, allow=allow)


def reachable_refs(repo: str) -> list:
    """Refs to scan.

    Default is the repository's own history: local branches, tags and the checked
    out revision. `refs/remotes/origin/*` is deliberately NOT used because on CI
    (actions/checkout with fetch-depth: 0) the remote-tracking refs can point at
    pull-request refs, which would make the gate report history that no branch
    exposes any more. Set ASTOCK_SCAN_REMOTE_REFS=1 to include every ref (useful
    for a local audit before deleting stale remote refs).
    """
    pattern = "refs" if os.environ.get("ASTOCK_SCAN_REMOTE_REFS") == "1" else "refs/heads"
    raw = run(["git", "-C", repo, "for-each-ref", "--format=%(refname)", pattern], cwd=repo)
    refs = [line.strip() for line in raw.splitlines() if line.strip()]
    if "refs/remotes" not in pattern:
        tags = run(["git", "-C", repo, "for-each-ref", "--format=%(refname)", "refs/tags"], cwd=repo)
        refs += [line.strip() for line in tags.splitlines() if line.strip()]
        # pull_request 场景是 detached HEAD：symbolic-ref 为空，必须回退到 HEAD 本身，
        # 否则当前检出的修订不会被纳入历史扫描（评审 P1）。--verify 在没有提交的空仓库里
        # 退出非零是合法状态，因此这里允许失败；其它 git 失败照常抛出。
        head = run(["git", "-C", repo, "symbolic-ref", "-q", "HEAD"], cwd=repo,
                   allow_failure=True).strip()
        if not head:
            head = run(["git", "-C", repo, "rev-parse", "--verify", "HEAD"], cwd=repo,
                       allow_failure=True).strip()
        if head and head not in refs:
            refs.append(head)
    return refs


def scan_history(repo: str, findings: Findings, manual_review: list, allow: "AllowList",
                 coverage: "Coverage | None" = None) -> None:
    """Scan commit identities/messages and every reachable blob.

    Any interruption -- a failing git command, an empty ref list, an empty
    object list, or a broken `cat-file --batch` pipe -- is recorded as a
    coverage gap, so the run cannot end "clean" over a range it never read.
    """
    coverage = coverage if coverage is not None else Coverage()

    def skip(value: str) -> bool:
        return allow.evaluate_value(value)

    try:
        refs = reachable_refs(repo)
    except GitCommandError as error:
        coverage.gap("history", f"ref enumeration failed, no history was scanned ({error})")
        return
    if not refs:
        coverage.gap("history", "no refs enumerated, no history was scanned")
        return

    # 1) metadata + messages
    fmt = "%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e"
    try:
        raw = run(["git", "log", "--format=" + fmt, *refs], cwd=repo)
    except GitCommandError as error:
        coverage.gap("history", f"git log failed, no commit was scanned ({error})")
        return
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split("\x1f")
        if len(parts) < 6:
            continue
        sha, an, ae, cn, ce, message = parts[0], parts[1], parts[2], parts[3], parts[4], "\x1f".join(parts[5:])
        for kind, value in (("AUTHOR_EMAIL", ae), ("COMMITTER_EMAIL", ce)):
            if value and not SAFE_EMAIL_RE.search(value):
                findings.add("PRIVATE_EMAIL", value, f"commit-identity:{sha[:10]}", kind)
        # 身份对判定：邮箱已是 GitHub noreply（隐私身份）时，显示名里的公开账号 handle
        # （如 daviesjoin-afk）不构成个人信息；只有"非 noreply 邮箱 + 个人化名字"才算命中。
        for name_field, name_value, email_value in (
            ("author-name", an, ae),
            ("committer-name", cn, ce),
        ):
            low = name_value.lower()
            if SAFE_EMAIL_RE.search(email_value or ""):
                continue
            if any(marker in low for marker in ("jiamianh", "daviesjoin")):
                findings.add("PERSONAL_NAME", name_value, f"commit-identity:{sha[:10]}", name_field)
        scan_text(message, f"commit-message:{sha[:10]}", findings, include_long_hex=False, allow=allow)

    # 2) every reachable blob (de-duplicated by object id: metadata-only rewrites keep
    #    the same blob ids, so identity-only rewrites stay cheap; content rewrites
    #    cascade and every rewritten blob is genuinely new).
    try:
        objects = run(["git", "rev-list", "--objects", *refs], cwd=repo)
    except GitCommandError as error:
        coverage.gap("history", f"object enumeration failed, no blob was scanned ({error})")
        return
    seen_blobs = {}
    for line in objects.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1]:
            seen_blobs.setdefault(parts[0], parts[1])
    blobs = list(seen_blobs.items())
    if not blobs:
        coverage.gap("history", f"{len(refs)} ref(s) exposed no reachable blob")
        return
    proc = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    scanned = 0
    stopped_at = None
    try:
        for sha, path in blobs:
            ext = os.path.splitext(path)[1].lower()
            if SCANNER_SELF_RE.search(path):
                continue
            if SENSITIVE_FILE_RE.search(path) and not path.endswith(SENSITIVE_FILE_ALLOW) and not FIXTURE_PATH_RE.search(path):
                if not skip(path):
                    findings.add("SENSITIVE_FILE", path, f"history:{sha[:10]}")
            if ext in BINARY_EXT:
                if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf") and IMAGE_REVIEW_HINT_RE.search(path):
                    manual_review.append(f"IMAGE_REVIEW | {path} | {sha[:10]}")
                continue
            try:
                proc.stdin.write(f"{sha}\n".encode())
                proc.stdin.flush()
                header = proc.stdout.readline().decode("utf-8", "replace").strip()
                if not header:
                    # The pipe closed mid-stream: the remaining blobs are unread.
                    stopped_at = (sha, path, "empty header")
                    break
                if header.endswith(("missing", "ambiguous")):
                    coverage.gap("history", f"blob {sha[:10]} ({path}) is {header.split()[-1]}")
                    continue
                size = int(header.split()[2])
                data = proc.stdout.read(size)
                if len(data) != size:
                    stopped_at = (sha, path, f"short read {len(data)}/{size}")
                    break
                proc.stdout.read(1)  # trailing newline
            except (BrokenPipeError, ValueError, IndexError, OSError) as error:
                stopped_at = (sha, path, type(error).__name__)
                break
            scanned += 1
            coverage.note("history-blobs", 1)
            if is_binary_bytes(data):
                continue
            scan_text(data.decode("utf-8", "replace"), f"history:{path}@{sha[:10]}", findings, allow=allow)
    finally:
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=30)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    if stopped_at is not None:
        sha, path, why = stopped_at
        coverage.gap(
            "history",
            f"blob batch read stopped at {sha[:10]} ({path}): {why}; "
            f"{scanned} of {len(blobs)} blob(s) were read and the rest are unchecked",
        )


def _diff_path(lines: list, fallback: str) -> str:
    """The target file of a unified-diff hunk header, if it names one."""
    for line in reversed(lines):
        match = re.match(r"^\+\+\+ (?:\w+/)?(.+?)(?:\t.*)?$", line)
        if match:
            path = match.group(1).strip()
            return "" if path == "/dev/null" else path
    return fallback


def scan_diff(path: str, findings: Findings, manual_review: list,
              allow: "AllowList", coverage: "Coverage | None" = None) -> None:
    """Scan the *added* lines of a unified diff (the PR fast-feedback pass).

    Only what the pull request introduces is scanned, which is the point: the
    full-history pass covers the rest. The diff file is a required input -- a
    missing or empty diff is a coverage gap, not "nothing to report".
    """
    coverage = coverage if coverage is not None else Coverage()
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as error:
        coverage.gap("diff", f"{path} unreadable ({type(error).__name__})")
        return
    if not text.strip():
        coverage.gap("diff", f"{path} is empty, no pull-request change was scanned")
        return

    current: list = []
    target = ""
    added: list = []
    files = 0
    for line in text.splitlines():
        if line.startswith("diff --git ") or line.startswith("--- "):
            if added:
                _scan_diff_block(target, added, findings, manual_review, allow)
                coverage.note("diff-lines", len(added))
                added = []
            if line.startswith("diff --git "):
                files += 1
                target = ""
                current = []
            continue
        if line.startswith("+++ "):
            target = _diff_path([line], target)
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            manual_review.append(f"IMAGE_REVIEW | diff-binary | {target or path}")
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
    if added:
        _scan_diff_block(target, added, findings, manual_review, allow)
        coverage.note("diff-lines", len(added))
    if files == 0 and not coverage.gaps:
        coverage.gap("diff", f"{path} contains no file section; nothing was scanned")


def _scan_diff_block(path: str, added: list, findings: Findings,
                     manual_review: list, allow: "AllowList") -> None:
    rel = path or "<unknown-path>"
    if (SENSITIVE_FILE_RE.search(rel) and not rel.endswith(SENSITIVE_FILE_ALLOW)
            and not FIXTURE_PATH_RE.search(rel) and not allow.evaluate_location(rel)):
        findings.add("SENSITIVE_FILE", rel, "diff")
    if SCANNER_SELF_RE.search(rel):
        return
    scan_text("\n".join(added), f"diff:{rel}", findings, allow=allow)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan a Git repository for sensitive data.")
    parser.add_argument("--repo", default=".", help="repository path (default: current dir)")
    parser.add_argument("--scope", choices=["all", "worktree", "history", "diff"], default="all")
    parser.add_argument("--diff", help="unified diff to scan (needed by --scope diff)")
    parser.add_argument("--write-raw-file", help="write RAW values here (LOCAL ONLY, never commit)")
    parser.add_argument("--json", help="write machine-readable summary here")
    parser.add_argument("--max-findings", type=int, default=60, help="max findings printed")
    parser.add_argument("--preview", action="store_true",
                        help="print a redacted preview + context for every distinct value (triage mode)")
    args = parser.parse_args()

    repo = os.path.abspath(args.repo)
    findings = Findings()
    manual_review: list = []
    coverage = Coverage()
    allowlist = AllowList(repo)
    # 只对本仓库做一次对象 id 收集，用于排除"自身提交 id"这类标识符。
    # 非仓库场景退化为空集合，但这本身不影响覆盖声明——覆盖由各 scope 自己记录。
    try:
        findings.known_oids = {
            line.strip().lower()
            for line in run(["git", "-C", repo, "rev-list", "--all", "--objects"]).splitlines()
            if line.strip()
        }
    except (GitCommandError, OSError):
        findings.known_oids = set()

    wants_diff = args.scope == "diff" or bool(args.diff)
    if args.scope == "diff" and not args.diff:
        coverage.gap("diff", "--scope diff requires --diff PATH; nothing was scanned")

    if args.scope in ("all", "worktree"):
        scan_worktree(repo, findings, manual_review, allowlist, coverage)
    if args.scope in ("all", "history"):
        scan_history(repo, findings, manual_review, allowlist, coverage)
    if wants_diff and args.diff:
        scan_diff(args.diff, findings, manual_review, allowlist, coverage)

    summary = {
        "repo": repo,
        "scope": args.scope,
        "diff": args.diff,
        "distinct_values": findings.count(),
        "by_kind": {},
        "manual_review": sorted(set(manual_review)),
        "coverage": {"complete": coverage.complete(),
                     "scanned": dict(sorted(coverage.scanned.items())),
                     "uncovered": list(coverage.gaps)},
        "findings": [],
    }
    for value, entry in findings.by_value.items():
        kind = entry["kind"]
        summary["by_kind"][kind] = summary["by_kind"].get(kind, 0) + 1
        summary["findings"].append({
            "id": entry["id"],
            "kind": kind,
            "locations": entry["locations"][:6],
            "location_count": len(entry["locations"]),
        })

    print(f"repo   : {repo}")
    print(f"scope  : {args.scope}" + (f" (diff={args.diff})" if args.diff else ""))
    print(f"kinds  : " + (", ".join(f"{k}={v}" for k, v in sorted(summary['by_kind'].items())) or "none"))
    print(f"values : {findings.count()}")
    if coverage.scanned:
        print("scanned: " + ", ".join(f"{scope}={count}" for scope, count in sorted(coverage.scanned.items())))
    if args.preview:
        print("")
        print("== 脱敏预览（ID / 类型 / 预览 / 上下文）==")
        for value, entry in findings.by_value.items():
            loc = entry["locations"][0] if entry["locations"] else "?"
            extra = f" x{len(entry['locations'])}" if len(entry["locations"]) > 1 else ""
            print(f"{entry['id']:<22} {entry['kind']:<16} {preview_value(entry['kind'], value):<32} {loc}{extra}")
            if entry.get("preview"):
                print(f"{'':<22} ctx: {entry['preview']}")
    else:
        for item in summary["findings"][: args.max_findings]:
            print(f"  {item['id']:<22} {item['kind']:<18} x{item['location_count']:<4} {item['locations'][0]}")
        if findings.count() > args.max_findings:
            print(f"  … 其余 {findings.count() - args.max_findings} 项省略")
    if summary["manual_review"]:
        print(f"manual review: {len(summary['manual_review'])} 项（截图/二进制）")
        for item in summary["manual_review"][:10]:
            print(f"  {item}")
    if not coverage.complete():
        print("")
        print(f"UNVERIFIED: {len(coverage.gaps)} range(s) of the requested scope were "
              f"NOT scanned; this run proves nothing about them")
        for gap in coverage.gaps:
            print(f"  {gap}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
    if args.write_raw_file:
        with open(args.write_raw_file, "w", encoding="utf-8") as handle:
            handle.write("# LOCAL ONLY — raw sensitive values for history rewrite. NEVER COMMIT.\n")
            for value, entry in findings.by_value.items():
                if entry["kind"] in ("SENSITIVE_FILE", "PRIVATE_KEY"):
                    handle.write(f"{entry['kind']}\t{value}\n")
                    continue
                handle.write(f"{entry['id']}\t{entry['kind']}\t{value}\n")

    # Exit codes are the contract: 0 clean, 1 violations, 2 incomplete scan.
    # An incomplete scan wins over a clean one -- it must never look like a pass.
    if not coverage.complete():
        return 2
    return 1 if findings.count() else 0


if __name__ == "__main__":
    sys.exit(main())











