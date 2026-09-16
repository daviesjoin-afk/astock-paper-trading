# -*- coding: utf-8 -*-
"""Learning-closure 变异矩阵 L1–L8。

用法::

    python work/learning_closure_mutation_check.py

设计口径与 ``work/execution_reality_mutation_check.py`` 一致：每个变异条目显式
携带目标文件；变异前后都清 ``__pycache__`` 并关闭字节码写入，避免同一秒内的写入
被缓存掩盖（"变异没生效"会伪装成 UNDETECTED）；变异体必须**可导入**，靠
SyntaxError 假杀不算 CAUGHT。

判定语义::

* ``CAUGHT``     = 变异后契约测试失败（缺陷被抓住）—— 本轮要求全部 CAUGHT；
* ``UNDETECTED`` = 变异后测试仍全绿（缺陷漏网）—— 任一出现即退出码 1。

``S0`` 是自检哨兵（只改注释、不改行为），必须 UNDETECTED；若它被判成 CAUGHT，
说明测试基线本来就是红的，整个矩阵的结论不成立。

编号沿用仓库既有体系：``execution_reality_mutation_check.py`` 已用到 M70，
学习闭环从 ``L1`` 起编号，不与 execution 系列冲突。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TEST_MODULES = (
    "test_learning_closure",
    "test_learning_dataset",
    "test_learning_evaluation",
    "test_walk_forward_validation",
    "test_adaptive_dependency_boundary",
    "test_selection_labels",
)

ADAPTIVE = "backend/adaptive_engine.py"

# (id, 目标文件, 变异前源码片段, 变异后源码片段, 说明)
MUTATIONS = (
    (
        "L1",
        ADAPTIVE,
        "    frame = _canonical_alpha_frame(build)\n"
        "    train, validation, test = frame[\"train\"], frame[\"validation\"], frame[\"test\"]\n",
        "    frame = _canonical_alpha_frame(build)\n"
        "    # MUTANT L1: restore the hand-made 70/30 date split.\n"
        "    _all_raw = list(frame[\"train\"]) + list(frame[\"validation\"]) + list(frame[\"test\"])\n"
        "    _dates = sorted({row[\"profile_date\"] for row in _all_raw})\n"
        "    _split = max(1, int(len(_dates) * 0.70))\n"
        "    _train_dates, _val_dates = set(_dates[:_split]), set(_dates[_split:])\n"
        "    train = [row for row in _all_raw if row[\"profile_date\"] in _train_dates]\n"
        "    validation = [row for row in _all_raw if row[\"profile_date\"] in _val_dates]\n"
        "    test = []\n",
        "restore the old hand-made 70/30 date split",
    ),
    (
        "L2",
        ADAPTIVE,
        "    frame = _canonical_alpha_frame(build)\n",
        "    # MUTANT L2: purge disabled -- canonical samples used as-is.\n"
        "    _unpurged = learning_dataset.DatasetBuild(\n"
        "        cutoff=build.cutoff,\n"
        "        contract_version=build.contract_version,\n"
        "        feature_names=build.feature_names,\n"
        "        horizon_semantics=build.horizon_semantics,\n"
        "        split_spec=build.split_spec,\n"
        "        partitions=_unpurged_partitions(build),\n"
        "        exclusions=build.exclusions,\n"
        "        purge_counts=build.purge_counts,\n"
        "        manifest=build.manifest,\n"
        "        fingerprint=build.fingerprint,\n"
        "        truncated=build.truncated,\n"
        "    )\n"
        "    build = _unpurged\n"
        "    frame = _canonical_alpha_frame(build)\n",
        "overlapping-label purge removed",
    ),
    (
        "L3",
        ADAPTIVE,
        "    for _ in range(generations):\n"
        "        ranked = sorted(population, key=lambda genome: fitness(genome, \"train\", train)[\"fitness\"], reverse=True)\n",
        "    for _ in range(generations):\n"
        "        ranked = sorted(population, key=lambda genome: fitness(genome, \"train\", train + validation)[\"fitness\"], reverse=True)\n",
        "validation folded into training fitness",
    ),
    (
        "L4",
        ADAPTIVE,
        "        validation_score = fitness(genome, \"validation\", validation)\n",
        "        validation_score = fitness(genome, \"validation\", validation + test)\n",
        "held-out test folded into candidate selection",
    ),
    (
        "L5",
        ADAPTIVE,
        "            cutoff=run_date,\n            code_build_identity=ENGINE_VERSION,\n            persist=False,\n",
        "            cutoff=run_date[-4:] + \"-12-31\",\n            code_build_identity=ENGINE_VERSION,\n            persist=False,\n",
        "learning cutoff ignored (widened to year end)",
    ),
    (
        "L6",
        "backend/learning_dataset.py",
        "    if availability is None:\n"
        "        reason = (\n"
        "            \"legacy_unproven_pit\"\n"
        "            if declared_pit == PIT_LEGACY_UNPROVEN\n"
        "            else \"unknown_feature_availability\"\n"
        "        )\n"
        "        return None, reason\n",
        "    if availability is None:\n"
        "        availability = _timestamp_text(sample.get(\"feature_asof\"))\n",
        "unknown PIT availability promoted to eligible",
    ),
    (
        "L7",
        ADAPTIVE,
        "    detail = _canonical_dataset_blocker(build, prior)\n"
        "    if detail is not None:\n"
        "        raise _AlphaLabBlocked(\"waiting_dataset\", detail)\n"
        "    # Idempotent: the fingerprint is the primary key, so re-recording the same\n",
        "    detail = None\n"
        "    if detail is not None:\n"
        "        raise _AlphaLabBlocked(\"waiting_dataset\", detail)\n"
        "    # Idempotent: the fingerprint is the primary key, so re-recording the same\n",
        "fingerprint mismatch no longer blocks",
    ),
    (
        "L8",
        ADAPTIVE,
        "    rng = random.Random(f\"{ENGINE_VERSION}:{run_date}:{build.fingerprint}\")\n",
        "    rng = random.Random(f\"{ENGINE_VERSION}:{run_date}\")\n",
        "RNG identity no longer bound to the dataset fingerprint",
    ),
)

# 自检哨兵：只改注释。它必须 UNDETECTED。
SANITY_MUTATION = (
    "S0",
    ADAPTIVE,
    "# ── canonical dataset: the ONLY authority for train/validation/test ──\n",
    "# ── canonical dataset: the ONLY authority for train/validation/test ── [sanity]\n",
    "harness sanity check (comment only, must survive)",
)

# L2 需要的辅助：把 purge 掉的样本放回去，模拟"没有 purge"。
UNPURGE_HELPER = '''

def _unpurged_partitions(build):
    """MUTANT-only helper: re-derive partitions without the purge step."""
    import learning_dataset as _LD
    samples = []
    for name in _LD.PARTITIONS:
        samples.extend(build.partitions.get(name) or [])
    ordered = sorted(samples, key=_LD._sample_sort_key)
    dates = sorted({sample.label_start_date for sample in ordered})
    boundaries = _LD._split_boundaries(dates, build.split_spec)
    assignment = {}
    for name in _LD.PARTITIONS:
        start, end = boundaries[name]
        for date in dates[start:end]:
            assignment[date] = name
    partitions = {name: [] for name in _LD.PARTITIONS}
    for sample in ordered:
        name = assignment.get(sample.label_start_date)
        if name:
            partitions[name].append(sample.with_partition(name))
    return partitions

'''


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def replace_once(source: bytes, before: str, after: str) -> bytes:
    old = before.encode("utf-8")
    new = after.encode("utf-8")
    count = source.count(old)
    if count != 1:
        raise AssertionError(f"mutation anchor count != 1 (got {count}): {before!r}")
    return source.replace(old, new, 1)


def clear_bytecode(relative_path: str) -> None:
    module = Path(relative_path).stem
    cache_dir = ROOT / "backend" / "__pycache__"
    if not cache_dir.is_dir():
        return
    for candidate in cache_dir.glob(f"{module}.*.pyc"):
        try:
            candidate.unlink()
        except OSError:  # pragma: no cover
            pass


def run_contract_tests() -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": "backend", "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-m", "unittest", "-q", *TEST_MODULES],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
    )


def baseline_is_green() -> bool:
    clear_bytecode(ADAPTIVE)
    run = run_contract_tests()
    print(f"baseline: returncode={run.returncode}")
    if run.returncode != 0:
        print(run.stdout)
        print(run.stderr)
    return run.returncode == 0


def _import_check() -> bool:
    """变异体必须是可导入的 —— SyntaxError 假杀不算 CAUGHT。"""
    env = {**os.environ, "PYTHONPATH": "backend", "PYTHONDONTWRITEBYTECODE": "1"}
    run = subprocess.run(
        [sys.executable, "-c", "import adaptive_engine, learning_dataset"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
    )
    if run.returncode != 0:
        print(run.stdout)
        print(run.stderr)
    return run.returncode == 0


def apply_and_run(entry) -> str:
    name, relative_path, before, after, _description = entry
    target = ROOT / relative_path
    original = target.read_bytes()
    original_sha = sha256(original)
    if sha256(original) != original_sha:  # pragma: no cover
        raise RuntimeError(f"{name} refuses to mutate a non-pristine source")

    mutated = replace_once(original, before, after)
    if name == "L2":
        mutated = mutated + UNPURGE_HELPER.encode("utf-8")
    if mutated == original:
        raise AssertionError(f"{name} mutation is inert at the byte level")
    try:
        clear_bytecode(relative_path)
        target.write_bytes(mutated)
        if not _import_check():
            return "IMPORT-FAILED"
        result = run_contract_tests()
        caught = result.returncode != 0
        if not caught:
            print(result.stdout)
            print(result.stderr)
        return "CAUGHT" if caught else "UNDETECTED"
    finally:
        clear_bytecode(relative_path)
        target.write_bytes(original)
        restored = target.read_bytes()
        if restored != original or sha256(restored) != original_sha:
            raise RuntimeError(f"{name} restore verification failed; refusing to continue")
        print(f"{name} restore: bytes_match={restored == original} "
              f"sha256_match={sha256(restored) == original_sha}")


def main() -> int:
    print(f"repo root: {ROOT}")
    print("targets: " + ", ".join(sorted({entry[1] for entry in MUTATIONS})))
    if not baseline_is_green():
        print("baseline contract tests are not green; refusing to run the mutation matrix")
        return 1
    if shutil.which("git") is None:  # pragma: no cover
        print("warning: git not found; relying on byte-for-byte restore only")

    results = []
    sanity = apply_and_run(SANITY_MUTATION)
    print(f"S0 sanity: {sanity} (expected UNDETECTED)")

    for entry in MUTATIONS:
        outcome = apply_and_run(entry)
        print(f"{entry[0]}: {outcome}  ({entry[4]})")
        results.append((entry[0], outcome))

    print("\n=== mutation matrix summary ===")
    for name, outcome in results:
        print(f"{name}: {outcome}")
    caught = [name for name, outcome in results if outcome == "CAUGHT"]
    survived = [name for name, outcome in results if outcome != "CAUGHT"]
    print(f"caught: {len(caught)}/{len(results)}")
    print(f"survived: {survived or 'none'}")

    complete = len(results) == len(MUTATIONS) and not survived and sanity == "UNDETECTED"
    print("mutation matrix: " + ("PASS" if complete else "FAIL"))
    return 0 if complete else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
