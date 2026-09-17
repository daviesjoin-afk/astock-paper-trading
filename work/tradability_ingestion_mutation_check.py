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
BACKFILL = "backend/tradability_backfill.py"
INGESTION_TEST = "backend/test_tradability_ingestion.py"

# 每个变异条目缺省跑的契约测试模块；某些条目（跨文件的 Docker / dry-run /
# fingerprint / session 契约）需要额外模块，用 TEST_MODULES_BY_ID 覆盖。
TEST_MODULES_BY_ID = {
    # M-D1 改的是 test 文件：只跑架构护栏（AST 静态扫描 test_*.py 的 work import），
    # 不 import 被注入的 test 文件，避免"运行时 import 失败"遮蔽"guard 抓到"的信号。
    "M-D1": ("test_tradability_architecture_guard",),
    # M-S1 / M-DR1 改的是 tradability_backfill.py，由回填契约测试抓住。
    "M-S1": ("test_tradability_backfill",),
    "M-DR1": ("test_tradability_backfill",),
    # M-F1/M-F2 改的是 tradability_ingestion.py：dry-run≠write 与 replay 漂移
    # 由回填契约测试的 fingerprint 断言抓住。
    "M-F1": ("test_tradability_backfill",),
    "M-F2": ("test_tradability_backfill", "test_tradability_ingestion"),
    # M-F3 把 run_id 加回 fingerprint payload：P-F5（不同 run_id → 同一指纹）在
    # 回填契约测试里，必须指定该模块，否则默认只跑 test_tradability_ingestion 抓不到。
    "M-F3": ("test_tradability_backfill",),
}

# 每个条目 import-check 的目标模块（排除"生产代码变异后语法错误无法 import"的假杀）。
# 缺省检查 tradability_ingestion；改 test 文件或别的生产模块时覆盖。
IMPORT_MODULE_BY_ID = {
    "M-D1": "tradability_ingestion",   # 改的是 test 文件，生产代码未动
    "M-S1": "tradability_backfill",
    "M-DR1": "tradability_backfill",
}

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
        "        core_fields_complete = (\n"
        "            composed.get(\"is_listed\") is not None\n"
        "            and composed.get(\"is_st\") is not None\n"
        "            and composed.get(\"is_suspended\") is not None\n"
        "            and composed.get(\"has_market_quote\") is not None\n"
        "            and composed.get(\"has_trade_volume\") is not None\n"
        "        )\n"
        "        if core_fields_complete and not conflicts and not unprovable:\n"
        "            stats[\"fully_proven\"] += 1\n",
        "        core_fields_complete = (\n"
        "            composed.get(\"is_listed\") is not None\n"
        "            and composed.get(\"is_st\") is not None\n"
        "            and composed.get(\"is_suspended\") is not None\n"
        "            and composed.get(\"has_market_quote\") is not None\n"
        "            and composed.get(\"has_trade_volume\") is not None\n"
        "        )\n"
        "        if True:  # MUTANT TTI7: unknown counted as fully_proven\n"
        "            stats[\"fully_proven\"] += 1\n",
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
    (
        "M-D1",
        INGESTION_TEST,
        "import tradability_ingestion as TI  # noqa: E402\n",
        "import tradability_ingestion as TI  # noqa: E402\n"
        "import backfill_tradability_archive as BF  # noqa: E402  # MUTANT M-D1: work-only import\n",
        "backend test 重新 import work-only 模块（镜像内 ImportError，回归事故根因）",
    ),
    (
        "M-F1",
        INGESTION,
        "                normalized_records += 1\n"
        "                normalized_evidence.append(evidence)\n"
        "                if write:\n",
        "                normalized_records += 1\n"
        "                if write:\n"
        "                    normalized_evidence.append(evidence)\n"
        "                if write:\n",
        "normalized evidence 收集被移回 write gate（dry-run fingerprint 漂移）",
    ),
    (
        "M-F2",
        INGESTION,
        "            codes, sessions, self._cutoff, normalized_evidence\n",
        "            codes, sessions, self._cutoff, persisted\n",
        "fingerprint 使用本次 inserted rows（幂等重放后指纹漂移）",
    ),
    (
        "M-S1",
        BACKFILL,
        "    if calendar is None:\n"
        "        return sessions_between(first, last)\n"
        "    return sessions_between(first, last, calendar=calendar)\n",
        "    if calendar is None:\n"
        "        return sessions_between(first, last)\n"
        "    _f = _dt.date.fromisoformat(str(first)[:10])  # MUTANT M-S1\n"
        "    _l = _dt.date.fromisoformat(str(last)[:10])\n"
        "    return [(_f + _dt.timedelta(days=i)).isoformat() for i in range((_l - _f).days + 1)]\n",
        "日期范围恢复自然日枚举（注入 calendar 也被忽略，周末/法定休市进入 denominator）",
    ),
    (
        "M-DR1",
        BACKFILL,
        "    if not write:\n"
        "        return service.ingest(codes, sessions, write=False, run_id=run_id)\n",
        "    if not write:\n"
        "        TA.ensure_schema(conn)  # MUTANT M-DR1: dry-run mutates schema\n"
        "        TI.ensure_ingestion_schema(conn)\n"
        "        return service.ingest(codes, sessions, write=False, run_id=run_id)\n",
        "dry-run 恢复 schema mutation（悄悄建表，违反无副作用契约）",
    ),
    (
        "M-F3",
        INGESTION,
        [
            "        codes: Sequence[str], sessions: Sequence[str], cutoff: str,\n",
            "            \"version\": FINGERPRINT_VERSION,\n",
            "            codes, sessions, self._cutoff, normalized_evidence\n",
        ],
        [
            "        run_id: str, codes: Sequence[str], sessions: Sequence[str], cutoff: str,\n",
            "            \"version\": FINGERPRINT_VERSION,\n"
            "            \"run_id\": run_id,\n",
            "            run_id, codes, sessions, self._cutoff, normalized_evidence\n",
        ],
        "run_id 重新参与 fingerprint payload（audit identity 泄漏进内容指纹）",
    ),
    (
        "M-C1",
        INGESTION,
        "            \"unknown\": total_unknown_pairs,\n",
        "            \"unknown\": total_unknown_fields,\n",
        "pair-level unknown 被改回 field counter 之和（维度混淆）",
    ),
    (
        "M-C2",
        INGESTION,
        "            \"known\": total_known,\n",
        "            \"known\": total_present,\n",
        "pair-level known 被改回 evidence_present（部分证据也算 known）",
    ),
    (
        "M-C3",
        INGESTION,
        "        if conflicts:\n"
        "            stats[\"conflict\"] += 1\n"
        "        elif unprovable:\n",
        "        if conflicts:\n"
        "            stats[\"conflict\"] += 1\n"
        "            stats[\"unknown\"] += 1\n"
        "        elif unprovable:\n",
        "conflict pair 同时计入 unknown（破坏排他分类）",
    ),
    (
        "M-C4",
        INGESTION,
        "        elif unprovable:\n"
        "            stats[\"unprovable\"] += 1\n"
        "        elif not core_fields_complete:\n",
        "        elif unprovable:\n"
        "            stats[\"unprovable\"] += 1\n"
        "            stats[\"known\"] += 1\n"
        "        elif not core_fields_complete:\n",
        "unprovable pair 同时计入 known（不可证明却算已知）",
    ),
    (
        "M-C5",
        INGESTION,
        "            \"coverage_ratio\": round(total_known / requested_pairs * 100, 1)\n",
        "            \"coverage_ratio\": round(total_present / requested_pairs * 100, 1)\n",
        "coverage_ratio 被改回 evidence_present / requested_pairs（一点证据 = 100%覆盖）",
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


def _as_fragments(x):
    """单片段字符串或片段列表统一为列表（M-F3 等多处替换的 mutation 用）。"""
    return [x] if isinstance(x, str) else list(x)


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


def run_contract_tests(modules=None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": "backend", "PYTHONDONTWRITEBYTECODE": "1"}
    targets = list(modules) if modules is not None else list(TEST_MODULES)
    return subprocess.run(
        [sys.executable, "-m", "unittest", "-q", *targets],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
    )


def _import_check(module: str = "tradability_ingestion") -> bool:
    env = {**os.environ, "PYTHONPATH": "backend", "PYTHONDONTWRITEBYTECODE": "1"}
    run = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
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

    befores = _as_fragments(before)
    afters = _as_fragments(after)
    if len(befores) != len(afters):
        raise AssertionError(
            f"{name}: fragment count mismatch {len(befores)} != {len(afters)}"
        )
    mutated = original
    for b, a in zip(befores, afters):
        mutated = replace_once(mutated, b, a)
    if mutated == original:
        raise AssertionError(f"{name} mutation is inert at the byte level")
    try:
        clear_bytecode(relative_path)
        target.write_bytes(mutated)
        import_module = IMPORT_MODULE_BY_ID.get(name, "tradability_ingestion")
        if not _import_check(import_module):
            return "IMPORT-FAILED"
        modules = TEST_MODULES_BY_ID.get(name)
        result = run_contract_tests(modules)
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
        befores = _as_fragments(before)
        afters = _as_fragments(after)
        data = (ROOT / relative_path).read_bytes()
        if len(befores) != len(afters):
            bad += 1
            print(f"{name}: fragment count mismatch {len(befores)} != {len(afters)}")
            continue
        for b, a in zip(befores, afters):
            count = data.count(b.encode("utf-8"))
            if count != 1:
                bad += 1
                print(f"{name}: BAD (count={count})")
                print(f"    anchor: {b[:120]!r}")
            elif a == b:
                print(f"{name}: INERT (before == after)")
                bad += 1
            else:
                print(f"{name}: ok")
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
