"""Build an executable stand-in for a probe binary, on any platform.

A `.bat` shim only runs on Windows; on POSIX the same idea is a `#!/bin/sh`
wrapper that `chmod +x` makes executable. Both swallow the CLI arguments the
caller passes, so the stub can print a canned report and choose an exit code.
"""
from __future__ import annotations

import os
from pathlib import Path

NEWLINE = chr(10)


def make_probe_stub(root: Path, script: Path) -> Path:
    if os.name == "nt":
        stub = root / "stub.bat"
        stub.write_text(
            "@echo off" + NEWLINE + f'"{os.sys.executable}" "{script}"' + NEWLINE,
            encoding="utf-8",
        )
        return stub
    stub = root / "stub.sh"
    stub.write_text(
        "#!/bin/sh" + NEWLINE + f'exec "{os.sys.executable}" "{script}" "$@"' + NEWLINE,
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub
