"""Compare isolated startup and first HTTP reads against the current Git HEAD.

Run from the repository root: python work/benchmark_architecture_startup.py
Uses temporary ledgers, disables schedulers/demo mode, and rejects all networking.
"""
import io
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
CHILD = r'''
import asyncio, json, socket, sys, time
def denied(*args, **kwargs):
    raise RuntimeError("external network disabled for startup benchmark")
socket.create_connection = denied
socket.getaddrinfo = denied
original_connect = socket.socket.connect
def connect(self, address):
    # Windows asyncio creates a loopback socket pair for its event loop.
    if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
        return original_connect(self, address)
    return denied()
socket.socket.connect = connect
t = time.perf_counter()
import main
import_seconds = time.perf_counter() - t
import httpx
async def measure():
    t = time.perf_counter()
    async with main._lifespan(main.app):
        startup_seconds = time.perf_counter() - t
        responses = {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://localhost") as client:
            for path in ("/api/health", "/api/adaptive/ai/settings"):
                t = time.perf_counter()
                response = await client.get(path)
                responses[path] = {"seconds": time.perf_counter()-t, "status": response.status_code}
                assert response.status_code == 200, (path, response.text)
    return {"import_seconds": import_seconds, "lifespan_seconds": startup_seconds, "first_requests": responses}
print("BENCHMARK_RESULT=" + json.dumps(asyncio.run(measure())))
'''


def main():
    samples = {"baseline": [], "optimized": []}
    with tempfile.TemporaryDirectory(prefix="astock-startup-") as temp:
        temp_root = Path(temp)
        baseline = temp_root / "baseline"
        baseline.mkdir()
        archive = subprocess.run(
            ["git", "archive", "HEAD"], cwd=ROOT, capture_output=True, check=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            source.extractall(baseline, filter="data")
        for iteration in range(4):
            for label, root in (("baseline", baseline), ("optimized", ROOT)):
                isolated = temp_root / f"{label}-{iteration}"
                (isolated / "data").mkdir(parents=True)
                env = dict(os.environ, ASTOCK_DATA_DIR=str(isolated / "data"),
                           ASTOCK_REPORT_DIR=str(isolated / "reports"),
                           PYTHONPYCACHEPREFIX=str(isolated / "pycache"),
                           ASTOCK_ENABLE_FALLBACK_THREADS="0", ASTOCK_DEMO="0")
                child = subprocess.run(
                    [sys.executable, "-B", "-c", CHILD], cwd=root / "backend",
                    env=env, capture_output=True, text=True, encoding="utf-8", timeout=90,
                )
                if child.returncode:
                    raise RuntimeError(child.stderr)
                marker = next(line for line in child.stdout.splitlines() if line.startswith("BENCHMARK_RESULT="))
                value = json.loads(marker.split("=", 1)[1])
                if iteration:
                    samples[label].append(value)
    medians = {}
    for label, values in samples.items():
        medians[label] = {
            "import_seconds": statistics.median(v["import_seconds"] for v in values),
            "lifespan_seconds": statistics.median(v["lifespan_seconds"] for v in values),
            "ready_seconds": statistics.median(v["import_seconds"] + v["lifespan_seconds"] for v in values),
            "first_requests": {path: statistics.median(v["first_requests"][path]["seconds"] for v in values)
                               for path in values[0]["first_requests"]},
        }
    result = {"method": "Alternating 4 runs per variant; first discarded. Empty temporary ledgers/cache, no networking, schedulers disabled; HTTP via ASGI transport.",
              "baseline_ref": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip(),
              "samples": samples, "medians": medians}
    (ROOT / "work/architecture_optimization_startup_benchmark.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(medians, indent=2))


if __name__ == "__main__":
    main()
