# -*- coding: utf-8 -*-
"""历史可交易性证据摄取层变异矩阵 TTI1–TTI12。

用法::

    python work/tradability_ingestion_mutation_check.py

设计口径与 ``work/tradability_mutation_check.py`` 一致：每个变异条目显式携带
目标文件；变异前后都清 ``__pycache__`` 并关闭字节码写入；变异体必须**可导入**，
靠 SyntaxError 假杀不算 CAUGHT。

判定语义::

* ``CAUGHT``     = 变异后契约测试失败（缺陷被抓住）—— 要求全部 CAUGHT；
* ``UNDETECTED`` = 变异后测试仍全绿（缺陷漏网）—— 任一出现即退出码 1。

``S0`` 是自检哨兵（只改注释、不改行为），必须 UNDETECTED。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TEST_MODULES = (
    "test_tradability_ingestion",
)

INGESTION = "backend/tradability_ingestion.py"

# (id, 目标文件, 变异前源码片段, 变异后源码片段, 说明)
MUTATIONS = (
    (
        "TTI1",
        INGESTION,
        "    if not observed_values:\n"
        "        unprovable = True\n"
        "        observed = _canonical_instant(cutoff)\n"
        "    else:\n"
        "        observed = max(observed_values)\n",
        "    if not observed_values:\n"
        "        observed = max(effective_values) if effective_values else _canonical_instant(cutoff)\n"
        "    else:\n"
        "        observed = max(observed_values)\n",
        "observed_at 缺失时偷偷用 effective_at 兜底（生效时间冒充观察时间，时间穿越）",
    ),
    (
        "TTI2",
        INGESTION,
        "        risk_flag = state.get(\"risk_flag\")\n"
        "        is_st = PIT.as_strict_bool(risk_flag)\n",
        "        risk_flag = state.get(\"risk_flag\")\n"
        "        is_st = PIT.as_strict_bool(risk_flag)\n"
        "        if \"ST\" in str(state.get(\"name\") or \"\").upper():\n"
        "            is_st = True\n",
        "当前股票名称重新用于历史 ST 判断（name 子串推断 ST）",
    ),
    (
        "TTI3",
        INGESTION,
        "        if not contributions:\n"
        "            composed[fname] = None\n"
        "            continue\n",
        "        if not contributions:\n"
        "            composed[fname] = False if fname in _BOOL_FIELDS else None\n"
        "            continue\n",
        "Provider 缺失状态默认 False（未知被当作明确否定）",
    ),
    (
        "TTI4",
        INGESTION,
        "        if not contributions:\n"
        "            composed[fname] = None\n"
        "            continue\n",
        "        if not contributions:\n"
        "            composed[fname] = True if fname in _BOOL_FIELDS else None\n"
        "            continue\n",
        "Provider 缺失状态默认 True（未知被当作明确肯定）",
    ),
    (
        "TTI5",
        INGESTION,
        "        distinct = {value for _, value in contributions}\n"
        "        if len(distinct) > 1:\n",
        "        distinct = {value for _, value in contributions}\n"
        "        if False:  # MUTANT TTI5: last-write-wins, conflicts never flagged\n",
        "来源冲突采用 last-write-wins（多来源不一致不标记，静默选第一个）",
    ),
    (
        "TTI6",
        INGESTION,
        "                    \"observed_at\": times[\"observed_at\"],\n",
        "                    \"observed_at\": _now_utc(),\n",
        "重复 ingest 产生重复历史 revision（每次 run 用墙钟 observed）",
    ),
    (
        "TTI7",
        INGESTION,
        "        # fully_proven：核心事实全部可证明（非 None）且无冲突。\n"
        "        if (\n"
        "            composed.get(\"is_listed\") is not None\n"
        "            and composed.get(\"is_st\") is not None\n"
        "            and composed.get(\"is_suspended\") is not None\n"
        "            and composed.get(\"has_market_quote\") is not None\n"
        "            and composed.get(\"has_trade_volume\") is not None\n"
        "            and not conflicts\n"
        "        ):\n",
        "        # MUTANT TTI7: unknown counted as fully_proven\n"
        "        if True:\n",
        "coverage 把 UNKNOWN 算作 fully_proven（覆盖率虚高）",
    ),
    (
        "TTI8",
        INGESTION,
        "        if result.observed_kind not in PIT_PROVABLE_OBSERVED_KINDS:\n"
        "            unprovable = True\n",
        "        if False:  # MUTANT TTI8: unprovable never flagged\n"
        "            unprovable = True\n",
        "today snapshot 被回填为历史 observed_at（unprovable 标记失效）",
    ),
    (
        "TTI9",
        INGESTION,
        "            {\"is_price_limit_locked\": locked, \"price_limit_direction\": direction},\n",
        "            {\"is_price_limit_locked\": locked, \"price_limit_direction\": None},\n",
        "涨跌停方向在 ingestion 中丢失（up/down 变成 None）",
    ),
    (
        "TTI10",
        INGESTION,
        "        record = (self._records.get(str(code)) or {}).get(_text(session))\n"
        "        if not isinstance(record, Mapping):\n"
        "            return _result(self, OUTCOME_UNKNOWN)\n",
        "        record = (self._records.get(str(code)) or {}).get(_text(session))\n"
        "        if not isinstance(record, Mapping):\n"
        "            return _result(self, OUTCOME_EVIDENCE, {\"is_price_limit_locked\": True, \"price_limit_direction\": \"up\"})\n",
        "仅触及 limit price 被误判 locked（无封单证据也推断涨停锁定）",
    ),
    (
        "TTI11",
        INGESTION,
        "    if effective_values:\n"
        "        effective = max(effective_values)\n"
        "    else:\n"
        "        effective = (\n"
        "            PIT.bar_available_at(session).isoformat()\n"
        "            if PIT.bar_available_at(session) is not None\n"
        "            else _canonical_instant(cutoff)\n"
        "        )\n",
        "    if effective_values:\n"
        "        effective = max(effective_values)\n"
        "    else:\n"
        "        effective = _canonical_instant(f\"{session}T00:00:00\")\n",
        "EOD volume 被提前到 session 盘中可见（effective 兜底到当日 00:00）",
    ),
    (
        "TTI12",
        INGESTION,
        "        record = self._records.get(str(code))\n"
        "        if record is None:\n"
        "            return _result(self, OUTCOME_UNKNOWN, observed_kind=self._observed_kind)\n",
        "        record = self._records.get(str(code))\n"
        "        if record is None:\n"
        "            return _result(self, OUTCOME_UNKNOWN, observed_kind=self._observed_kind)\n"
        "        self._repo.save(record)  # MUTANT TTI12: provider writes archive directly\n",
        "Provider 直接绕过 ingestion 写 archive（写 authority 逃逸到 adapter 层）",
    ),
)

# 自检哨兵：只改注释。它必须 UNDETECTED。
SANITY_MUTATION = (
    "S0",
    INGESTION,
    "CONTRACT_VERSION = \"tradability-ingestion-v1\"\n",
    "CONTRACT_VERSION = \"tradability-ingestion-v1\"  # sanity\n",
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


def _import_check() -> bool:
    env = {**os.environ, "PYTHONPATH": "backend", "PYTHONDONTWRITEBYTECODE": "1"}
    run = subprocess.run(
        [sys.executable, "-c", "import tradability_ingestion"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
    )
    if run.returncode != 0:
        print(run.stdout)
        print(run.stderr)
    return run.returncode == 0


def baseline_is_green() -> bool:
    clear_bytecode(INGESTION)
    run = run_contract_tests()
    print(f"baseline: returncode={run.returncode}")
    if run.returncode != 0:
        print(run.stdout)
        print(run.stderr)
    return run.returncode == 0


def apply_and_run(entry) -> str:
    name, relative_path, before, after, _description = entry
    target = ROOT / relative_path
    original = target.read_bytes()
    original_sha = sha256(original)

    mutated = replace_once(original, before, after)
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


def audit_anchors() -> int:
    print("=== anchor audit (read-only) ===")
    bad = 0
    entries = [SANITY_MUTATION, *MUTATIONS]
    for entry in entries:
        name, relative_path = entry[0], entry[1]
        before, after = entry[2], entry[3]
        data = (ROOT / relative_path).read_bytes()
        old = before.encode("utf-8")
        count = data.count(old)
        if count != 1:
            bad += 1
            print(f"{name}: BAD (count={count})")
            print(f"    anchor: {before[:120]!r}")
        else:
            print(f"{name}: ok")
        if after == before:
            print(f"{name}: INERT (before == after)")
            bad += 1
    print(f"=== audit result: {'PASS' if bad == 0 else f'{bad} problem(s)'} ===")
    return 1 if bad else 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--audit" in argv:
        return audit_anchors()
    print(f"repo root: {ROOT}")
    print("targets: " + ", ".join(sorted({entry[1] for entry in MUTATIONS})))
    if not baseline_is_green():
        print("baseline contract tests are not green; refusing to run the mutation matrix")
        return 1

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
