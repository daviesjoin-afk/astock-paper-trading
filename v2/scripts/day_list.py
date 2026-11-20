"""Print the kernel's held trading days, one per line, starting at --from.

Two details that matter on Windows:

* Output is written as bytes, not text. Python translates "\\n" to "\\r\\n" on a
  text stdout, and the shell then carries a stray CR into every date it
  compares, so `2026-06-02` never equals `2026-06-02`.
* The day list comes from the kernel's ingested bars, not from a local
  calendar, so a replay is reproducible and does not depend on today's date.
"""
import argparse
import json
import sys
import urllib.request


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--from", dest="start", default="")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)

    with urllib.request.urlopen(args.api.rstrip("/") + "/v2/market/days",
                                timeout=30) as response:
        days = json.loads(response.read().decode("utf-8"))["days"]

    if args.start:
        days = [day for day in days if day >= args.start]
    if args.limit:
        days = days[: args.limit]

    out = sys.stdout.buffer
    for day in days:
        out.write(day.encode("ascii") + b"\n")
    out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
