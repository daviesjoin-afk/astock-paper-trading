# -*- coding: utf-8 -*-
"""创建 PR #160（base=master, head=codex/tradability-shadow-validation）。

用法::

    PY=<仓库 venv 的 python>
    $PY work/pr160_open_pr.py <body-file>
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = "daviesjoin-afk/astock-paper-trading"
HEAD = "codex/tradability-shadow-validation"
BASE = "master"
TITLE = "feat(data): harden tradability replay and add shadow validation"


def gh(*args) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
        raise SystemExit(proc.returncode)
    return proc.stdout


def main() -> int:
    body_file = Path(sys.argv[1]).resolve()
    base_sha = gh("api", f"repos/{REPO}/git/ref/heads/{BASE}", "--jq", ".object.sha").strip()
    head_sha = gh(
        "api", f"repos/{REPO}/git/ref/heads/{HEAD}", "--jq", ".object.sha"
    ).strip()
    print(f"base {BASE} = {base_sha}")
    print(f"head {HEAD} = {head_sha}")

    created = gh(
        "pr", "create",
        "--repo", REPO,
        "--base", BASE,
        "--head", HEAD,
        "--title", TITLE,
        "--body-file", str(body_file),
    ).strip()
    print("created:", created)

    number = json.loads(
        gh("pr", "view", created, "--repo", REPO, "--json", "number")
    )["number"]
    print("pr number:", number)

    view = json.loads(
        gh(
            "pr", "view", str(number), "--repo", REPO, "--json",
            "number,state,isDraft,headRefOid,baseRefOid,mergeable,mergeStateStatus,url",
        )
    )
    print(json.dumps(view, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
