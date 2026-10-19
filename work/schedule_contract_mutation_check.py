"""变异验证：确认 guard 能抓住真实漂移，不是恒真断言。"""
import shutil, subprocess, sys, tempfile
from pathlib import Path

BACKEND = Path("backend")
TARGETS = {
    "paper_schedule_contract.py": [
        ("RUNNER_ONLY_SLOTS: tuple[str, ...] = (\"fast-entry\",)",
         "RUNNER_ONLY_SLOTS: tuple[str, ...] = (\"fast-entry\", \"risk\")",
         "M1: 把 risk 误标为 runner-only"),
        ("INTRADAY_CRON_MINUTES: dict[int, str] = {\n    9: \"33-59/3\",",
         "INTRADAY_CRON_MINUTES: dict[int, str] = {\n    9: \"30-59/3\",",
         "M2: 首小时起点改回 30（与 open 抢锁）"),
        ("    11: \"0-25/3\",", "    11: \"0-30/3\",",
         "M3: 上午末小时越过 11:25 窗口"),
        ("    13: \"0-55/3\",", "    13: \"*/3\",",
         "M4: 下午首小时改成整点步进（多出 13:57）"),
    ],
    "paper_runner.py": [
        ("choices=list(RUNNER_SLOTS)", "choices=[\"auction\", \"open\", \"risk\", \"close\", \"weekly-review\", \"intraday\"]",
         "M5: runner 丢掉 fast-entry"),
    ],
}

results = []
for filename, mutations in TARGETS.items():
    path = BACKEND / filename
    original = path.read_text(encoding="utf-8")
    for old, new, label in mutations:
        if old not in original:
            results.append((label, "ANCHOR_MISSING", ""))
            continue
        path.write_text(original.replace(old, new, 1), encoding="utf-8", newline="\n")
        try:
            proc = subprocess.run(
                [sys.executable, "-B", "-m", "unittest", "discover",
                 "-s", "backend", "-p", "test_paper_schedule_contract.py"],
                capture_output=True, text=True, timeout=180)
            verdict = "DETECTED" if proc.returncode != 0 else "SURVIVED"
            tail = [l for l in proc.stderr.splitlines() if "FAILED" in l or "AssertionError" in l]
            results.append((label, verdict, tail[0][:110] if tail else ""))
        finally:
            path.write_text(original, encoding="utf-8", newline="\n")

print(f"{'变异':45s} {'结果':10s} 证据")
for label, verdict, evidence in results:
    print(f"{label:45s} {verdict:10s} {evidence}")
survived = [r for r in results if r[1] == "SURVIVED"]
print()
print(f"检出 {len(results) - len(survived)}/{len(results)}，存活 {len(survived)}")
