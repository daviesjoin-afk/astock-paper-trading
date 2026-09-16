# -*- coding: utf-8 -*-
"""历史可交易性事实资产层变异矩阵 TTA1–TTA7。

用法::

    python work/tradability_mutation_check.py

设计口径与 ``work/learning_closure_mutation_check.py`` 一致：每个变异条目显式携带
目标文件；变异前后都清 ``__pycache__`` 并关闭字节码写入，避免同一秒内的写入被缓存
掩盖（"变异没生效"会伪装成 UNDETECTED）；变异体必须**可导入**，靠 SyntaxError
假杀不算 CAUGHT。

判定语义::

* ``CAUGHT``     = 变异后契约测试失败（缺陷被抓住）—— 要求全部 CAUGHT；
* ``UNDETECTED`` = 变异后测试仍全绿（缺陷漏网）—— 任一出现即退出码 1。

``S0`` 是自检哨兵（只改注释、不改行为），必须 UNDETECTED；若它被判成 CAUGHT，
说明测试基线本来就是红的，整个矩阵的结论不成立。
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
    "test_tradability_archive",
    "test_tradability_architecture_guard",
    "test_selection_tradability",
    "test_selection_point_in_time",
)

ARCHIVE = "backend/tradability_archive.py"

# (id, 目标文件, 变异前源码片段, 变异后源码片段, 说明)
MUTATIONS = (
    (
        "TTA1",
        ARCHIVE,
        "        if evidence.is_listed is True:\n            return None\n"
        "        if evidence.is_listed is False:\n",
        "        if True:  # MUTANT TTA1: listing state no longer consulted\n"
        "            return None\n"
        "        if evidence.is_listed is False:\n",
        "删除上市日期判断（未上市/已退市被放行）",
    ),
    (
        "TTA2",
        ARCHIVE,
        "        if evidence.is_st is True:\n"
        "            return TradabilityReason.ST_RESTRICTED\n",
        "        if False:  # MUTANT TTA2: ST restriction ignored\n"
        "            return TradabilityReason.ST_RESTRICTED\n",
        "删除 ST 判断",
    ),
    (
        "TTA3",
        ARCHIVE,
        (
            # 买入侧：停牌判断 + ST 分支一起定位，保证在文件里唯一
            "        if evidence.is_suspended is True:\n"
            "            return TradabilityReason.SUSPENDED\n"
            "        if evidence.is_suspended is None:\n"
            "            return TradabilityReason.UNKNOWN_STATE\n"
            "        if evidence.is_st is True:\n",
            # 卖出侧：停牌判断 + 行情分支一起定位
            "        if evidence.is_suspended is True:\n"
            "            return TradabilityReason.SUSPENDED\n"
            "        if evidence.is_suspended is None:\n"
            "            return TradabilityReason.UNKNOWN_STATE\n"
            "        if evidence.has_market_quote is not True:\n",
        ),
        (
            "        if False:  # MUTANT TTA3: suspension ignored (buy)\n"
            "            return TradabilityReason.SUSPENDED\n"
            "        if evidence.is_suspended is None:\n"
            "            return TradabilityReason.UNKNOWN_STATE\n"
            "        if evidence.is_st is True:\n",
            "        if False:  # MUTANT TTA3: suspension ignored (sell)\n"
            "            return TradabilityReason.SUSPENDED\n"
            "        if evidence.is_suspended is None:\n"
            "            return TradabilityReason.UNKNOWN_STATE\n"
            "        if evidence.has_market_quote is not True:\n",
        ),
        "删除停牌判断（买入 + 卖出两侧）",
    ),
    (
        "TTA4",
        ARCHIVE,
        "        if evidence.is_listed is True:\n            return None\n"
        "        if evidence.is_listed is False:\n",
        "        if evidence.is_listed is None:\n            return None\n"
        "        if evidence.is_listed is False:\n",
        "unknown 被当作上市（未知默认允许交易）",
    ),
    (
        "TTA5",
        ARCHIVE,
        "    verdict = PIT.is_visible_at(evidence.observed_at, decision_time)\n"
        "    if verdict.get(\"mode\") != \"strict\" or not verdict.get(\"visible\"):\n"
        "        return False\n",
        "    verdict = PIT.is_visible_at(evidence.observed_at, decision_time)\n"
        "    if False:  # MUTANT TTA5: observation time no longer gates visibility\n"
        "        return False\n",
        "当前状态污染历史（observed_at 不再约束可见性）",
    ),
    (
        "TTA6",
        ARCHIVE,
        "            buy_block_reason=buy_reason,\n",
        "            buy_block_reason=TradabilityReason.OK,\n",
        "删除买入 reason 字段（阻断原因丢失）",
    ),
    (
        "TTA7",
        ARCHIVE,
        "    evidence = repository.evidence_at(code, session, decision_time)\n"
        "    if evidence is None:\n",
        "    evidence = repository.evidence_at(code, session, decision_time)\n"
        "    if evidence is None:\n"
        "        evidence = TradabilityEvidence(\n"
        "            code=code_text, session_date=session_text, is_listed=True,\n"
        "            listing_date=None, delisting_date=None, is_st=False,\n"
        "            is_suspended=False, suspension_reason=None,\n"
        "            has_market_quote=True, has_trade_volume=True,\n"
        "            is_price_limit_locked=False, source=\"bypass\",\n"
        "            observed_at=\"2000-01-01T00:00:00+08:00\",\n"
        "            effective_at=\"2000-01-01T00:00:00+08:00\",\n"
        "        )\n"
        "    if evidence is None:\n",
        "bypass archive（无记录时直接构造可交易事实）",
    ),
    (
        "TTA8",
        ARCHIVE,
        (
            "        if evidence.is_price_limit_locked is True:\n"
            "            # 涨停只拦买；方向未知时仍拦买（锁定事实已证，方向未知 → fail closed）\n"
            "            if evidence.price_limit_direction == PRICE_LIMIT_DOWN:\n"
            "                return TradabilityReason.OK\n"
            "            return TradabilityReason.BUY_LIMIT_LOCKED\n",
            "        if evidence.is_price_limit_locked is True:\n"
            "            # 跌停只拦卖；方向未知时仍拦卖（锁定事实已证，方向未知 → fail closed）\n"
            "            if evidence.price_limit_direction == PRICE_LIMIT_UP:\n"
            "                return TradabilityReason.OK\n"
            "            return TradabilityReason.SELL_LIMIT_LOCKED\n",
        ),
        (
            "        if evidence.is_price_limit_locked is True:  # MUTANT TTA8 buy\n"
            "            return TradabilityReason.BUY_LIMIT_LOCKED\n",
            "        if evidence.is_price_limit_locked is True:  # MUTANT TTA8 sell\n"
            "            return TradabilityReason.SELL_LIMIT_LOCKED\n",
        ),
        "涨跌停不分方向（涨停也拦卖、跌停也拦买）",
    ),
    (
        "TTA9",
        ARCHIVE,
        (
            "            if evidence.price_limit_direction == PRICE_LIMIT_DOWN:\n"
            "                return TradabilityReason.OK\n",
            "            if evidence.price_limit_direction == PRICE_LIMIT_UP:\n"
            "                return TradabilityReason.OK\n",
        ),
        (
            "            if evidence.price_limit_direction != PRICE_LIMIT_UP:\n"
            "                return TradabilityReason.OK\n",
            "            if evidence.price_limit_direction != PRICE_LIMIT_DOWN:\n"
            "                return TradabilityReason.OK\n",
        ),
        "锁定但方向未知时不再 fail closed（两侧放行）",
    ),
    (
        "TTA10",
        ARCHIVE,
        "            UNIQUE(code, session_date, effective_at, observed_at)\n",
        "            UNIQUE(code, session_date, effective_at)\n",
        "唯一键丢掉观测维度（同一生效时点的上游修正被静默丢弃）",
    ),
    (
        "TTA11",
        ARCHIVE,
        "    return moment.isoformat()\n",
        "    return moment.isoformat(timespec=\"seconds\")\n",
        "观测时点截断到整秒（同秒内两次修订塌缩，且证据提前可见）",
    ),
    (
        "TTA12",
        ARCHIVE,
        (
            "    named = _row_mapping(row)\n"
            "    if named is None:\n"
            "        values = list(row)\n"
            "        named = {\n"
            "            column: values[index]\n"
            "            for index, column in enumerate(ARCHIVE_COLUMNS)\n"
            "            if index < len(values)\n"
            "        }\n",
        ),
        (
            "    named = _row_mapping(row) or {}\n",
        ),
        "只支持具名行（默认连接的 tuple 行整表退化为未知）",
    ),
    (
        "TTA13",
        ARCHIVE,
        "        self._sync_cache_with_database()\n",
        "        pass  # MUTANT TTA13: external writes never invalidate the cache\n",
        "外部写入不失效缓存（读侧无限期返回过期事实）",
    ),
)

# 自检哨兵：只改注释。它必须 UNDETECTED。
SANITY_MUTATION = (
    "S0",
    ARCHIVE,
    "ARCHIVE_TABLE = \"historical_tradability_archive\"\n",
    "ARCHIVE_TABLE = \"historical_tradability_archive\"  # sanity\n",
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


def _fragments(before, after):
    """把 (before, after) 归一化成片段列表。

    单片段是常态；当同一个缺陷在源码里有多处等价实现（例如买入/卖出两条分支
    各自判断停牌），一条变异需要同时打断全部实现，否则只覆盖半个缺陷面。
    多片段模式下每一段仍各自要求 count==1，锚点模糊依旧会被拒绝。
    """
    if isinstance(before, (list, tuple)):
        if not isinstance(after, (list, tuple)) or len(after) != len(before):
            raise AssertionError(
                "multi-fragment mutation requires matching before/after lengths"
            )
        return list(zip(before, after))
    return [(before, after)]


def apply_fragments(source: bytes, before, after) -> bytes:
    result = source
    for old_text, new_text in _fragments(before, after):
        result = replace_once(result, old_text, new_text)
    return result


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
    """变异体必须可导入 —— SyntaxError 假杀不算 CAUGHT。"""
    env = {**os.environ, "PYTHONPATH": "backend", "PYTHONDONTWRITEBYTECODE": "1"}
    run = subprocess.run(
        [sys.executable, "-c", "import tradability_archive"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
    )
    if run.returncode != 0:
        print(run.stdout)
        print(run.stderr)
    return run.returncode == 0


def baseline_is_green() -> bool:
    clear_bytecode(ARCHIVE)
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

    mutated = apply_fragments(original, before, after)
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
    """只读审计：每条变异的每个片段在**当前盘上源码**里必须恰好出现一次。

    读盘源是未变异状态，因此这个检查反映的是真实锚点质量。历史教训是把锚点
    写在"矩阵正在跑时看到的源码"上，等矩阵 abort 才发现 count 不对——一次
    abort 只暴露一条，逐个试很贵。这里一次列全。
    """
    print("=== anchor audit (read-only) ===")
    bad = 0
    entries = [SANITY_MUTATION, *MUTATIONS]
    for entry in entries:
        name, relative_path = entry[0], entry[1]
        before, after = entry[2], entry[3]
        try:
            fragments = _fragments(before, after)
        except AssertionError as exc:
            print(f"{name}: MALFORMED fragments ({exc})")
            bad += 1
            continue
        data = (ROOT / relative_path).read_bytes()
        for index, (old_text, new_text) in enumerate(fragments, start=1):
            old = old_text.encode("utf-8")
            count = data.count(old)
            status = "ok" if count == 1 else f"BAD (count={count})"
            if count != 1:
                bad += 1
                print(f"{name} fragment {index}: {status}")
                print(f"    anchor: {old_text[:120]!r}")
            else:
                print(f"{name} fragment {index}: {status}")
            if new_text == old_text:
                print(f"{name} fragment {index}: INERT (before == after)")
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
