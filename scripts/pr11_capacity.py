"""Read-only admission check for the server profile; never print env-file data."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import subprocess

MIB = 1024 * 1024
HEADROOM_BYTES = 1024 * MIB
THREAD_KEYS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")


def memory_bytes(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text[-1:] in ("k", "m", "g"):
        return int(float(text[:-1]) * {"k": 1024, "m": MIB, "g": 1024 * MIB}[text[-1]])
    return int(text)


def read_compose(path):
    result = subprocess.run(
        ["docker", "compose", "-f", str(path), "config", "--format", "json"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError("docker compose config failed")
    return json.loads(result.stdout)


def profile_limits(config):
    """Only non-secret resource fields leave the compose parser."""
    limits = {}
    for name, service in config["services"].items():
        cpu = float(service["cpus"])
        memory = memory_bytes(service["mem_limit"])
        if not math.isfinite(cpu) or cpu <= 0 or memory <= 0:
            raise ValueError("invalid worker resource limit")
        limits[name] = {"cpus": cpu, "memory_bytes": memory,
                        "swap_bytes": memory_bytes(service.get("memswap_limit", memory)),
                        "pids": int(service["pids_limit"]),
                        "cpu_shares": int(service.get("cpu_shares", 1024))}
    return limits


def evaluate(config, *, cpu_count, total_memory, available_memory):
    limits = profile_limits(config)
    paper = limits["astock-paper-worker"]
    candidate = limits["astock-candidate-worker"]
    required = sum(item["memory_bytes"] for item in limits.values()) + HEADROOM_BYTES
    errors = []
    if cpu_count < 2 or paper["cpus"] + candidate["cpus"] > cpu_count:
        errors.append("insufficient_cpu_capacity")
    if total_memory < required:
        errors.append("insufficient_total_memory")
    if available_memory < HEADROOM_BYTES:
        errors.append("insufficient_available_memory")
    if candidate["swap_bytes"] != candidate["memory_bytes"]:
        errors.append("candidate_swap_not_bounded_to_memory")
    if paper["cpu_shares"] <= candidate["cpu_shares"]:
        errors.append("risk_cpu_priority_missing")
    for name in ("astock-paper-worker", "astock-candidate-worker"):
        service = config["services"][name]
        if any(str(service.get("environment", {}).get(key)) != "1" for key in THREAD_KEYS):
            errors.append(f"native_thread_limit_missing:{name}")
        if limits[name]["pids"] <= 0:
            errors.append(f"pid_limit_missing:{name}")
    return {"ok": not errors, "errors": errors, "limits": limits,
            "host_cpu_count": cpu_count, "host_total_bytes": total_memory,
            "host_available_bytes": available_memory, "required_total_bytes": required,
            "headroom_bytes": HEADROOM_BYTES,
            "note": "CPU quotas are ceilings; shares are weights, not reservations."}


def host_capacity():
    values = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value.strip().split()[0]) * 1024
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 0)
    return {"cpu_count": cpus, "total_memory": values["MemTotal"],
            "available_memory": values["MemAvailable"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path,
                        default=Path(__file__).resolve().parents[1] / "docker-compose.server.yml")
    args = parser.parse_args()
    try:
        report = evaluate(read_compose(args.compose), **host_capacity())
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        report = {"ok": False, "errors": ["capacity_facts_unavailable"]}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
