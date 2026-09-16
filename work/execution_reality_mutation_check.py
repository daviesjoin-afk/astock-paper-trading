# -*- coding: utf-8 -*-
"""执行真实性层（execution reality layer）本地变异验证工具 M51–M55。

用法::

    python work/execution_reality_mutation_check.py

设计口径（与 ``work/pr_cycle_capital_negative_check.py`` 一致，两处刻意不同之处如下）::

1. 每个变异条目显式携带**目标文件**，因为本轮的缺陷跨越
   ``execution_evidence`` / ``execution_lifecycle`` / ``execution_outcome`` 三个模块；
2. 每轮变异前后都会清掉目标模块的 ``__pycache__`` 并关闭字节码写入，避免同一秒内的
   写入被缓存掩盖（"变异没生效"会伪装成 UNDETECTED）。

判定语义：

* ``CAUGHT``      = 变异后契约测试失败（缺陷被抓住）—— 本轮**要求全部 CAUGHT**；
* ``UNDETECTED``  = 变异后测试仍全绿（缺陷漏网）—— 任一出现即退出码 1。

额外的 ``S0`` 是**自检哨兵**（只改注释、不改行为）：它必须 ``UNDETECTED``。
如果连 S0 都被判成 CAUGHT，说明测试本来就红的，整个矩阵的结论不成立。
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
    "test_execution_evidence",
    "test_execution_lifecycle",
    "test_execution_outcome",
)

# (id, 目标文件, 变异前源码片段, 变异后源码片段, 说明)
MUTATIONS = (
    (
        "M51",
        "backend/execution_evidence.py",
        '    filled = evidence.field("filled_qty")\n'
        "    if filled.is_unknown:\n"
        "        return FILL_VERDICT_UNKNOWN\n",
        '    filled = evidence.field("filled_qty")\n'
        "    if filled.is_unknown:\n"
        "        return FILL_VERDICT_VERIFIED\n",
        "missing fill => assume filled",
    ),
    (
        "M52",
        "backend/execution_lifecycle.py",
        "    if status in REJECTED_STORED_STATUSES:\n"
        "        return STATE_REJECTED\n",
        "    if status in REJECTED_STORED_STATUSES:\n"
        "        return STATE_FILLED\n",
        "reject => filled",
    ),
    (
        "M53",
        "backend/execution_lifecycle.py",
        "            if filled != requested:\n"
        "                raise IllegalLifecycleTransition(\n"
        '                    "FILLED requires filled_qty == requested_qty "\n',
        "            if filled > requested:\n"
        "                raise IllegalLifecycleTransition(\n"
        '                    "FILLED requires filled_qty == requested_qty "\n',
        "partial fill => full fill",
    ),
    (
        "M54",
        "backend/execution_outcome.py",
        "    execution_field = realized_execution_return(entry, exit_side)\n",
        "    execution_field = (\n"
        "        realized_execution_return(entry, exit_side)\n"
        "        if entry is not None and entry.has_positive_fill()\n"
        "        else market_field\n"
        "    )\n",
        "execution return fallback market return",
    ),
    (
        "M55",
        "backend/execution_lifecycle.py",
        "    STATE_CREATED: frozenset({\n"
        "        STATE_SUBMITTED, STATE_REJECTED, STATE_CANCELLED, STATE_EXPIRED, STATE_UNKNOWN,\n",
        "    STATE_CREATED: frozenset({\n"
        "        STATE_FILLED, STATE_SUBMITTED, STATE_REJECTED, STATE_CANCELLED,\n"
        "        STATE_EXPIRED, STATE_UNKNOWN,\n",
        "illegal lifecycle transition accepted",
    ),
)

# 自检哨兵：只改注释。它必须 UNDETECTED，否则说明测试基线本来就是红的。
SANITY_MUTATION = (
    "S0",
    "backend/execution_lifecycle.py",
    "#: 终态：不可逆。任何离开终态的跳转都必须失败（重试 = 新委托）。\n",
    "#: 终态：不可逆。任何离开终态的跳转都必须失败（重试 = 新委托）。 [sanity]\n",
    "harness sanity check (comment only, must survive)",
)


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
    """删除目标模块的缓存 pyc，避免同一秒内的源码写入被缓存掩盖。"""
    module = Path(relative_path).stem
    cache_dir = ROOT / "backend" / "__pycache__"
    if not cache_dir.is_dir():
        return
    for candidate in cache_dir.glob(f"{module}.*.pyc"):
        try:
            candidate.unlink()
        except OSError:  # pragma: no cover - 并发清理失败不应让矩阵崩
            pass


def run_contract_tests() -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PYTHONPATH": "backend",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [sys.executable, "-m", "unittest", "-q", *TEST_MODULES],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def baseline_is_green() -> bool:
    clear_bytecode("backend/execution_evidence.py")
    run = run_contract_tests()
    print(f"baseline: returncode={run.returncode}")
    if run.returncode != 0:
        print(run.stdout)
        print(run.stderr)
    return run.returncode == 0


def apply_and_run(entry) -> str:
    """返回 ``CAUGHT`` / ``UNDETECTED``，并把源码逐字节还原。"""
    name, relative_path, before, after, _description = entry
    target = ROOT / relative_path
    original = target.read_bytes()
    original_sha = sha256(original)

    if target.read_bytes() != original or sha256(target.read_bytes()) != original_sha:
        raise RuntimeError(f"{name} refuses to mutate a non-pristine production source")

    mutated = replace_once(original, before, after)
    if mutated == original:
        raise AssertionError(f"{name} mutation is inert at the byte level")
    try:
        clear_bytecode(relative_path)
        target.write_bytes(mutated)
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
            raise RuntimeError(
                f"{name} restore verification failed; refusing to continue"
            )
        print(
            f"{name} restore: bytes_match={restored == original} "
            f"sha256_match={sha256(restored) == original_sha}"
        )


def main() -> int:
    print(f"repo root: {ROOT}")
    print("targets: " + ", ".join(sorted({entry[1] for entry in MUTATIONS})))
    if not baseline_is_green():
        print("baseline contract tests are not green; refusing to run the mutation matrix")
        return 1

    if shutil.which("git") is None:  # pragma: no cover - git is present in CI and locally
        print("warning: git not found; relying on byte-for-byte restore only")

    results = []
    sanity = apply_and_run(SANITY_MUTATION)
    print(f"S0 sanity: {sanity} (expected UNDETECTED)")

    for entry in MUTATIONS:
        name = entry[0]
        outcome = apply_and_run(entry)
        print(f"{name}: {outcome}  ({entry[4]})")
        results.append((name, outcome))

    print("\n=== mutation matrix summary ===")
    for name, outcome in results:
        print(f"{name}: {outcome}")
    caught = [name for name, outcome in results if outcome == "CAUGHT"]
    survived = [name for name, outcome in results if outcome != "CAUGHT"]
    print(f"caught: {len(caught)}/{len(results)}")
    print(f"survived: {survived or 'none'}")

    complete = (
        len(results) == len(MUTATIONS)
        and not survived
        and sanity == "UNDETECTED"
    )
    print("mutation matrix: " + ("PASS" if complete else "FAIL"))
    return 0 if complete else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
