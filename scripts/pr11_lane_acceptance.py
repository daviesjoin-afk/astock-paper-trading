"""Isolated offline Docker resource probe; no production containers or data.

Cold Python imports and real SQLite risk/recovery slots run on a scaled tick.
Candidate pressure uses the real 5,000-row fundamental-input computation.
The report is evidence for this workload, not live-provider/180-second proof.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


def cgroup_facts():
    root = Path("/sys/fs/cgroup")
    def read(name):
        return (root / name).read_text(encoding="utf-8").strip()
    events = dict(line.split() for line in read("memory.events").splitlines())
    return {"memory_max": int(read("memory.max")), "memory_peak": int(read("memory.peak")),
            "swap_max": int(read("memory.swap.max")), "oom": int(events.get("oom", 0)),
            "cpu_max": read("cpu.max"), "cpu_weight": int(read("cpu.weight")),
            "cpuset": read("cpuset.cpus.effective"),
            "pids_max": int(read("pids.max")), "oom_kill": int(events.get("oom_kill", 0)),
            "namespace": os.readlink("/proc/self/ns/cgroup"),
            "runtime_commit": os.environ.get("ASTOCK_GIT_COMMIT", "unknown")}


def fast_once(slot, index):
    started, cpu = time.monotonic(), time.process_time()
    import datetime as dt
    import resource
    from unittest import mock
    import paper_trading as paper
    from test_paper_risk_exit_production_path import PaperRiskExitProductionPathTestCase
    from test_paper_risk_lane import _fixed_clock, REAL_DATETIME
    fixture = PaperRiskExitProductionPathTestCase()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        fixture.setUp()
        try:
            moment = REAL_DATETIME(2026, 9, 10, 14) + dt.timedelta(minutes=3 * index)
            with _fixed_clock(moment), mock.patch.object(paper, "_quotes", return_value={}):
                result = paper.run_slot(slot, asof_date=fixture.day)
            assert result["status"] == "completed", result
            with paper._db() as conn:
                count = conn.execute("SELECT COUNT(*) FROM paper_job_runs WHERE status='completed'").fetchone()[0]
                assert count == 1
        finally:
            fixture.tearDown()
    return {"started": started, "wall_s": time.monotonic() - started,
            "cpu_s": time.process_time() - cpu,
            "rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            "status": result["status"]}


def candidate_pressure(duration=90):
    import datetime as dt
    import hashlib
    import paper_trading  # noqa: F401 — include the real scan stack's import cost
    from paper_candidate_inputs import CandidateScanInputs
    rows = [{"code": f"{600000+i:06}", "name": "synthetic", "industry": f"sector{i%20}",
             "board": "主板", "price": 10+i%50/10, "mktcap": 1e9+i,
             "float_cap": 8e8+i, "pe": 12+i%30, "pb": 1.2+i%5/10} for i in range(5000)]
    finance = {"data": {row["code"]: {"report_date": "2026-06-30", "annual_net_profit": 5e8,
                "net_profit": 2.5e8, "profit_yoy": 12.5, "revenue_yoy": 8.0, "roe": 11.0}
                for row in rows}, "report_date": "2026-06-30"}
    started, cpu, iterations, fingerprint = time.monotonic(), time.process_time(), 0, None
    print(json.dumps({"ready": True}), flush=True)
    while time.monotonic() - started < duration and not Path("/tmp/pr11-stop").exists():
        inputs = CandidateScanInputs(dt.date(2026, 10, 22))
        frame = inputs.fundamentals(inputs.universe(lambda: rows), inputs.finance(lambda: finance),
                                    asof=dt.date(2026, 10, 22))
        assert len(frame) == 5000
        digest = hashlib.sha256(frame.reset_index().to_json(orient="split").encode()).hexdigest()
        assert fingerprint is None or fingerprint == digest
        fingerprint, iterations = digest, iterations + 1
    return {"iterations": iterations, "fingerprint": fingerprint,
            "cpu_s": time.process_time() - cpu, "wall_s": time.monotonic() - started}


def sqlite_holder():
    import paper_storage as storage
    path = "/app/data_cache/pr11-coordination.sqlite3"
    with storage.db(path, hot_path=True) as conn:
        conn.execute("CREATE TABLE events(event_key TEXT PRIMARY KEY)")
    class RollbackProbe(Exception):
        pass
    try:
        with storage.db(path, immediate=True, hot_path=True) as conn:
            conn.execute("INSERT INTO events VALUES ('must-rollback')")
            print(json.dumps({"held": True}), flush=True)
            deadline = time.monotonic() + 15
            while not Path("/app/data_cache/pr11-release").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("SQLite coordination probe release missing")
                time.sleep(0.02)
            raise RollbackProbe()
    except RollbackProbe:
        return {"rolled_back": True}


def sqlite_contender():
    import paper_storage as storage
    import sqlite3
    busy = 0
    for attempt in range(3):
        try:
            with storage.db("/app/data_cache/pr11-coordination.sqlite3", immediate=True, hot_path=True) as conn:
                conn.execute("INSERT OR IGNORE INTO events VALUES ('committed-once')")
                conn.execute("INSERT OR IGNORE INTO events VALUES ('committed-once')")
                rows = [row[0] for row in conn.execute("SELECT event_key FROM events ORDER BY event_key")]
            assert rows == ["committed-once"], rows
            return {"busy_errors": busy, "attempts": attempt + 1, "rows": rows}
        except sqlite3.OperationalError as exc:
            if not storage.is_sqlite_busy_error(exc) or attempt == 2:
                raise
            busy += 1
            Path("/app/data_cache/pr11-release").touch()
            time.sleep(storage.sqlite_busy_backoff(attempt, hot_path=True))
    raise AssertionError("bounded SQLite probe exhausted")


def command(args, timeout=90):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"probe command failed: {result.stderr[-1500:]}")
    return result.stdout


def launch(image, name, limits, volume, cpuset, tmpfs):
    command(["docker", "run", "-d", "--name", name, "--network", "none", "--read-only",
             "--no-healthcheck", "--tmpfs", tmpfs,
             "--mount", f"type=volume,source={volume},target=/app/data_cache",
             "--memory", str(limits["memory_bytes"]), "--memory-swap", str(limits["swap_bytes"]),
             "--cpus", str(limits["cpus"]), "--cpu-shares", str(limits["cpu_shares"]),
             "--cpuset-cpus", cpuset,
             "--pids-limit", str(limits["pids"]), "--security-opt", "no-new-privileges:true",
             "--env", "ASTOCK_ENABLE_FALLBACK_THREADS=0", image, "sleep", "infinity"])


def invocation(name, mode, *extra):
    return ["docker", "exec", name, "python", "scripts/pr11_lane_acceptance.py", "--inside", mode, *extra]


def percentile(values, portion=0.95):
    return sorted(values)[max(0, int(len(values) * portion + 0.999) - 1)]


def sample_fast(name, *, samples=6, tick=6.0):
    rows, execution_rows, queue, roundtrips = [], [], [], []
    epoch = time.monotonic()
    for index in range(samples):
        due = epoch + index * tick
        time.sleep(max(0, due - time.monotonic()))
        round_started = time.monotonic()
        # Match both sides: candidate pressure is the only workload difference.
        peer = subprocess.Popen(invocation(name, "fast", "--slot", "execution-resume", "--index", str(index)),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            row = json.loads(command(invocation(name, "fast", "--slot", "risk", "--index", str(index))))
            rows.append(row)
            queue.append(max(0.0, row["started"] - due))
            stdout, stderr = peer.communicate(timeout=90)
            assert peer.returncode == 0, stderr[-1500:]
            execution_rows.append(json.loads(stdout))
            roundtrips.append(time.monotonic() - round_started)
        finally:
            if peer.poll() is None:
                peer.kill()
                peer.wait()
    intervals = [b["started"] - a["started"] for a, b in zip(rows, rows[1:], strict=False)]
    return {"samples": samples, "scaled_tick_s": tick, "interval_p95_s": percentile(intervals),
            "interval_max_s": max(intervals), "queue_max_s": max(queue),
            "rss_max_bytes": max(r["rss_bytes"] for r in rows + execution_rows),
            "work_wall_max_s": max(r["wall_s"] for r in rows + execution_rows),
            "roundtrip_max_s": max(roundtrips),
            "execution_samples": len(execution_rows),
            "risk_samples": rows, "recovery_samples": execution_rows}


def host_probe(image, evidence):
    from pr11_capacity import read_compose, evaluate, host_capacity, profile_limits
    root = Path(__file__).resolve().parents[1]
    config = read_compose(root / "docker-compose.server.yml")
    capacity = evaluate(config, **host_capacity())
    evidence.update(ok=False, capacity=capacity, image=image, phase="capacity")
    assert capacity["ok"], capacity
    limits = profile_limits(config)
    cpuset = ",".join(str(cpu) for cpu in sorted(os.sched_getaffinity(0))[:2])
    probe_prefix = "pr11-ci-" + uuid.uuid4().hex[:12]
    paper, candidate = probe_prefix + "-paper", probe_prefix + "-candidate"
    volume, created, volume_created = probe_prefix + "-data", [], False
    pressure = None
    try:
        command(["docker", "volume", "create", volume])
        volume_created = True
        # This newly created CI volume contains no production data.
        command(["docker", "run", "--rm", "--network", "none", "--user", "0",
                 "--mount", f"type=volume,source={volume},target=/app/data_cache", image,
                 "python", "-c", "import os; os.chown('/app/data_cache', 10001, 10001)"])
        launch(image, paper, limits["astock-paper-worker"], volume, cpuset,
               config["services"]["astock-paper-worker"]["tmpfs"][0])
        created.append(paper)
        launch(image, candidate, limits["astock-candidate-worker"], volume, cpuset,
               config["services"]["astock-candidate-worker"]["tmpfs"][0])
        created.append(candidate)
        # A fixed 6s tick can be shorter than two cold imports on a slower CI
        # host. Calibrate once WITHOUT candidate load, then freeze the SAME tick
        # and matched risk/recovery workload for baseline and loaded samples.
        calibration = sample_fast(paper, samples=2)
        tick = float(max(6, math.ceil(calibration["roundtrip_max_s"] * 1.5)))
        evidence.update(calibration=calibration, calibrated_tick_s=tick, phase="baseline")
        assert tick <= 30, "quiet host cannot sustain a bounded scaled probe"
        baseline = sample_fast(paper, tick=tick)
        evidence["baseline"] = baseline
        holder = subprocess.Popen(invocation(candidate, "hold"),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert select.select([holder.stdout], [], [], 30)[0], "SQLite holder readiness timeout"
            assert json.loads(holder.stdout.readline())["held"]
            coordination = json.loads(command(invocation(paper, "coordinate")))
            assert 1 <= coordination["busy_errors"] <= 2, coordination
            holder_out, holder_err = holder.communicate(timeout=30)
            assert holder.returncode == 0, holder_err[-1500:]
            assert json.loads(holder_out)["rolled_back"]
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait()
        evidence["sqlite_coordination"] = coordination
        pressure = subprocess.Popen(invocation(candidate, "candidate", "--duration", str(int(6 * tick + 20))),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert select.select([pressure.stdout], [], [], 60)[0], "candidate readiness timeout"
        assert json.loads(pressure.stdout.readline())["ready"]
        evidence["phase"] = "candidate_pressure"
        loaded = sample_fast(paper, tick=tick)
        evidence["contended"] = loaded
        assert pressure.poll() is None, "candidate pressure stopped before the contended samples finished"
        command(["docker", "exec", candidate, "python", "-c", "from pathlib import Path; Path('/tmp/pr11-stop').touch()"])
        stdout, stderr = pressure.communicate(timeout=60)
        assert pressure.returncode == 0, stderr[-1500:]
        load = json.loads(stdout)
        facts = {role: json.loads(command(invocation(name, "facts")))
                 for role, name in (("paper", paper), ("candidate", candidate))}
        evidence.update(candidate_pressure=load, cgroups=facts, probe_cpu_affinity=cpuset,
                        phase="assertions",
                        scope="offline synthetic 5000-row preparation + matched cold risk/recovery pairs; calibrated scaled tick, not live 180s proof")
        for role, service in (("paper", "astock-paper-worker"), ("candidate", "astock-candidate-worker")):
            actual, limit = facts[role], limits[service]
            assert actual["memory_max"] == limit["memory_bytes"]
            assert actual["swap_max"] == limit["swap_bytes"] - limit["memory_bytes"]
            assert actual["memory_peak"] < actual["memory_max"] * 0.8, actual
            assert actual["oom"] == actual["oom_kill"] == 0
            quota, period = map(int, actual["cpu_max"].split())
            assert abs(quota / period - limit["cpus"]) < 0.001
            assert actual["pids_max"] == limit["pids"]
        assert facts["paper"]["namespace"] != facts["candidate"]["namespace"]
        assert facts["paper"]["cpuset"] == facts["candidate"]["cpuset"]
        assert facts["paper"]["runtime_commit"] == facts["candidate"]["runtime_commit"]
        if os.environ.get("GITHUB_SHA"):
            assert facts["paper"]["runtime_commit"] == os.environ["GITHUB_SHA"]
        assert facts["paper"]["cpu_weight"] > facts["candidate"]["cpu_weight"]
        assert load["iterations"] >= 2 and load["cpu_s"] / load["wall_s"] > 0.2, load
        assert loaded["interval_p95_s"] <= baseline["interval_p95_s"] + 0.25, (baseline, loaded)
        assert loaded["interval_max_s"] < loaded["scaled_tick_s"] * 1.5, loaded
        assert loaded["queue_max_s"] < loaded["scaled_tick_s"] * 0.5, loaded
        evidence.update(ok=True, phase="completed")
        return evidence
    finally:
        # Only these unique, newly created CI containers may be removed.
        for name in created:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
        if volume_created:
            subprocess.run(["docker", "volume", "rm", volume], capture_output=True, timeout=30)
        if pressure and pressure.poll() is None:
            pressure.kill()
            pressure.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="astock-codex:ci")
    parser.add_argument("--inside", choices=("candidate", "fast", "facts", "hold", "coordinate"))
    parser.add_argument("--slot", default="risk")
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--duration", type=int, default=90)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.inside == "candidate":
        if not 1 <= args.duration <= 210:
            parser.error("duration must be between 1 and 210 seconds")
        result = candidate_pressure(args.duration)
    elif args.inside == "fast":
        result = fast_once(args.slot, args.index)
    elif args.inside == "facts":
        result = cgroup_facts()
    elif args.inside == "hold":
        result = sqlite_holder()
    elif args.inside == "coordinate":
        result = sqlite_contender()
    else:
        result = {"ok": False, "phase": "setup"}
        try:
            result = host_probe(args.image, result)
        finally:
            # Keep raw samples and failure phase even when a gate rejects it.
            if args.output:
                args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    if args.output:
        args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
