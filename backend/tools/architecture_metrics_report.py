# -*- coding: utf-8 -*-
"""Print structural architecture metrics for PR review; this is not a CI gate."""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from tools import coupling_metrics  # noqa: E402


def main() -> int:
    measured = coupling_metrics.measure_all()
    scalar_metrics = {
        key: value
        for key, value in measured.items()
        if isinstance(value, (int, float))
    }
    print(json.dumps(scalar_metrics, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
