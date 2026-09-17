# -*- coding: utf-8 -*-
"""非空洞性验证：把每处修复就地回退，确认新增的契约测试**真的会失败**。

一个"修复后才写"的测试，如果它从未真正经过缺陷路径，那它就是装饰。本脚本对每个
修复做一次 in-place revert → 跑对应测试模块 → 断言 rc != 0 → 还原并核对 sha256。

用法::

    PY=<仓库 venv 的 python>   # 本地绝对路径不进仓库（敏感扫描）
    $PY work/pr160_verify_new_tests.py
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
PY = sys.executable

INGESTION = BACKEND / "tradability_ingestion.py"
BACKFILL = BACKEND / "tradability_backfill.py"
ARCHIVE = BACKEND / "tradability_archive.py"
SHADOW = BACKEND / "tradability_shadow.py"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(modules) -> int:
    for cache in BACKEND.rglob("__pycache__"):
        for item in cache.glob("*.pyc"):
            item.unlink(missing_ok=True)
    proc = subprocess.run(
        [PY, "-m", "unittest", *modules],
        cwd=str(BACKEND), capture_output=True, text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return proc.returncode, (proc.stderr or "") + (proc.stdout or "")


def failing_tests(output: str) -> list:
    return sorted(
        line.split(" ")[1]
        for line in output.splitlines()
        if line.startswith("FAIL: ") or line.startswith("ERROR: ")
    )


REVERTS = [
    (
        "R1 divergent replay rejected",
        INGESTION,
        "            self._assert_replay_identity(run_id, run_fingerprint)\n",
        "            pass  # REVERTED\n",
        ("test_tradability_backfill",),
    ),
    (
        "R2 empty code scope rejected",
        INGESTION,
        '        if not codes:\n'
        '            raise IngestionError("ingestion 拒绝空 code scope（0 个代码）")\n'
        '        if not sessions:\n'
        '            raise IngestionError("ingestion 拒绝空 session scope（0 个交易日）")\n',
        "        pass  # REVERTED\n",
        ("test_tradability_backfill",),
    ),
    (
        "R3 explicit empty codes rejected (fallback removed)",
        BACKFILL,
        "        selected = normalize_codes(codes)\n",
        "        selected = sorted(load_listing_records().keys())  # REVERTED\n",
        ("test_tradability_backfill",),
    ),
    (
        "R4 zero-session range rejected",
        BACKFILL,
        '    if not sessions:\n'
        '        raise SessionScopeError(\n'
        '            f"日期范围 {first} → {last} 解析出 0 个交易日，无 session 可回填"\n'
        '        )\n',
        "    pass  # REVERTED\n",
        ("test_tradability_backfill",),
    ),
    (
        "R5 archive writes happen after replay validation",
        INGESTION,
        "            self._assert_replay_identity(run_id, run_fingerprint)\n"
        "            # autocommit 连接上没有事务可回滚，事实与审计无法原子提交 → 显式拒绝。\n"
        "            if self._enforce_explicit_transactions:\n"
        "                raise IngestionError(\n"
        "                    \"write=True 需要显式事务：该连接处于 autocommit（isolation_level=None），\"\n"
        "                    \"事实写入与审计插入会各自立即提交，任一后续失败都无法整体回滚\"\n"
        "                )\n"
        "            for evidence in normalized_evidence:\n"
        "                if self._repo.save(evidence):\n"
        "                    persisted.append(evidence)\n"
        "                else:\n"
        "                    skipped_records += 1  # 幂等重放：唯一键命中，逻辑状态不变。\n",
        "            for evidence in normalized_evidence:\n"
        "                if self._repo.save(evidence):\n"
        "                    persisted.append(evidence)\n"
        "                else:\n"
        "                    skipped_records += 1\n"
        "            self._assert_replay_identity(run_id, run_fingerprint)\n",
        ("test_tradability_backfill",),
    ),
    (
        "S1 archive gaps not counted as disagreement",
        SHADOW,
        "        if archive[\"archive_state\"] == ShadowStatus.ARCHIVE_MISSING.value:\n"
        "            status = ShadowStatus.ARCHIVE_MISSING\n"
        "        elif archive[\"archive_state\"] == ShadowStatus.ARCHIVE_UNPROVABLE.value:\n"
        "            status = ShadowStatus.ARCHIVE_UNPROVABLE\n"
        "        elif archive[\"archive_state\"] == ShadowStatus.ARCHIVE_UNKNOWN.value:\n"
        "            status = ShadowStatus.ARCHIVE_UNKNOWN\n"
        "        else:\n",
        "        if False:\n",
        ("test_tradability_shadow",),
    ),
    (
        "S2 agreement_rate uses requested as denominator",
        SHADOW,
        "            agreement_rate=_ratio(agree, comparable),\n",
        "            agreement_rate=_ratio(agree, requested),\n",
        ("test_tradability_shadow",),
    ),
    (
        "S3 future evidence may enter a past comparison",
        ARCHIVE,
        "    verdict = PIT.is_visible_at(evidence.observed_at, decision_time)\n"
        '    if verdict.get("mode") != "strict" or not verdict.get("visible"):\n'
        "        return False\n",
        "    pass  # REVERTED\n",
        ("test_tradability_shadow",),
    ),
    (
        "S4 BUY/SELL limit direction inverted",
        ARCHIVE,
        "            if evidence.price_limit_direction == PRICE_LIMIT_DOWN:\n"
        "                return TradabilityReason.OK\n"
        "            return TradabilityReason.BUY_LIMIT_LOCKED\n",
        "            if evidence.price_limit_direction == PRICE_LIMIT_UP:\n"
        "                return TradabilityReason.OK\n"
        "            return TradabilityReason.BUY_LIMIT_LOCKED\n",
        ("test_tradability_shadow",),
    ),
    (
        "S5 conflicting comparison content overwrites (last-write-wins)",
        SHADOW,
        "        if stored == row[\"content_fingerprint\"]:\n"
        '            return "identical"\n'
        "        raise ShadowConflictError(\n"
        '            "同一比对身份出现冲突内容："\n'
        "            f\"{comparison.identity} stored={stored} incoming={row['content_fingerprint']}\"\n"
        "        )\n",
        "        del stored\n"
        '        return "identical"  # REVERTED: last-write-wins\n',
        ("test_tradability_shadow",),
    ),
    (
        "S6 shadow exposes an authority entry point",
        SHADOW,
        "class ShadowError(ValueError):\n",
        "def allow_order(*args, **kwargs):  # REVERTED\n"
        '    """ARCHITECTURE VIOLATION"""\n'
        "    raise NotImplementedError\n\n\n"
        "class ShadowError(ValueError):\n",
        ("test_tradability_shadow_architecture_guard",),
    ),
    (
        "S7 comparison identity drops decision_at",
        SHADOW,
        '        return (self.code, self.session, self.decision_at, self.side, self.contract_version)\n',
        '        return (self.code, self.session, self.side, self.contract_version)  # REVERTED\n',
        ("test_tradability_shadow",),
    ),
]


def _refuse_if_mutation_running() -> None:
    """变异矩阵运行期间生产源码可能是变异体——此时"就地回退"会把它当成原始内容。

    一个真实事故：并行跑本脚本时，它把变异体读成"原始内容"记下来，随后又"还原"成
    那个变异体，留下了一段永久损坏的源码。这里改成明确拒绝，而不是静默损坏。
    """
    lock = BACKEND.parent / "work" / ".mutation_running"
    if lock.exists():
        raise SystemExit(
            f"变异矩阵正在运行（{lock} 存在）；源码此刻可能是变异体，拒绝并发运行"
        )


def main() -> int:
    _refuse_if_mutation_running()
    originals = {}
    for path in (INGESTION, BACKFILL, ARCHIVE, SHADOW):
        originals[path] = path.read_bytes()

    failures = []
    try:
        for label, path, old, new, modules in REVERTS:
            original = originals[path]
            text = original.decode("utf-8")
            if text.count(old) != 1:
                failures.append((label, f"anchor count={text.count(old)} (expected 1)"))
                print(f"[ANCHOR-FAIL] {label}: anchor occurs {text.count(old)}x")
                continue
            path.write_bytes(text.replace(old, new).encode("utf-8"))
            try:
                rc, output = run(modules)
            finally:
                path.write_bytes(original)
            digest_ok = sha256(path) == hashlib.sha256(original).hexdigest()
            if not digest_ok:
                failures.append((label, "restore hash mismatch"))
                print(f"[RESTORE-FAIL] {label}")
                continue
            caught = rc != 0
            names = failing_tests(output)[:3]
            status = "CAUGHT" if caught else "SURVIVED"
            print(f"[{status}] {label}  ->  {names}")
            if not caught:
                failures.append((label, "mutation survived"))
    finally:
        for path, blob in originals.items():
            if path.read_bytes() != blob:
                path.write_bytes(blob)
                print(f"[RESTORED] {path.name}")

    print()
    if failures:
        print(f"FAILED: {len(failures)} problem(s)")
        for label, why in failures:
            print(f"  - {label}: {why}")
        return 1
    print(f"OK: {len(REVERTS)} reverts all caught; all files restored byte-for-byte")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
