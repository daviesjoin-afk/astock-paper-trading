# -*- coding: utf-8 -*-
"""R23 mutation matrix —— 21 条 mutation（M-SP1..M-SP21 + M-RF1..M-RF3）。

**必须串行运行。** 每条 mutation 会就地改写 production source，跑完再按启动时的
快照做 byte-identical 还原并校验 sha256；并发分片会同时改写同一批文件而互相污染，
结果无效（R23 期间实测分片 tally 只有 0-1/3）。矩阵运行期间不要编辑 production
文件，也不要同时跑别的测试套件。

每条 mutation 必须让**唯一指定的永久回归**变 RED，且 anchor 必须**恰好命中一次**
（``_apply`` 内强制）——否则「改哪一处」会由字符串顺序决定，anchor 漂移后可能悄悄
改到别的调用点却仍打印 CAUGHT。

``--non-vacuity`` 会先跑 baseline；``SyntaxError`` / ``ImportError`` / ``NameError``
等接线错误一律计为 FAKE（不算 KILL）——它们让测试变红却证明不了任何业务性质。

机制沿用 R22 已修好的 ``PYTHONPYCACHEPREFIX`` 逐次唯一目录：baseline 与 mutant
绝不能共享字节码缓存，否则整张矩阵静默失效。

用法：
    python work/r23_mutation_check.py                  # 全部
    python work/r23_mutation_check.py --only M-SP1,M-SP2
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")

RESOLVER = "backend/strategy_selection_resolver.py"
CONTRACT = "backend/strategy_selection_provenance.py"
SELECTION = "backend/paper_selection.py"
TRACKING = "backend/selection_tracking.py"
PAPER = "backend/paper_trading.py"
MIGRATIONS = "backend/paper_schema_migrations.py"

SP = "test_strategy_selection_provenance"

MUTATIONS = [
    # ---- M-SP1..M-SP3 / M-SP11 / M-SP14：版本解析权威 ----
    {
        "id": "M-SP1", "file": RESOLVER,
        "old": "        version = SR.cycle_version_for_account(conn, strategy_id, cycle_id=int(cycle_id))",
        "new": "        version = SR.get_version(strategy_id, conn=conn)",
        "test": f"{SP}.SelectionProvenanceTests.test_SP03_cycle_pinned_version_beats_current_head",
        "desc": "cycle resolver 改用 current head",
    },
    {
        "id": "M-SP2", "file": RESOLVER,
        "old": """    if version is None:
        return _unproven(
            f"cycle {int(cycle_id)} has no pinned immutable version for {strategy_id}",
            strategy_id,
        )""",
        "new": """    if version is None:
        version = SR.get_version(strategy_id, conn=conn)""",
        "test": f"{SP}.SelectionProvenanceTests.test_SP04_explicit_cycle_without_pin_fails_closed",
        "desc": "missing cycle pin fallback current head",
    },
    {
        "id": "M-SP3", "file": RESOLVER,
        "old": """        found = SR.get_version(
            provenance.strategy_id, provenance.strategy_version,
            checksum=provenance.strategy_checksum, conn=conn,
        )""",
        "new": """        found = SR.get_version(
            provenance.strategy_id, provenance.strategy_version, conn=conn,
        )""",
        "test": f"{SP}.SelectionProvenanceTests.test_SP05_wrong_checksum_is_rejected_not_corrected",
        "desc": "忽略 checksum 校验",
    },
    {
        "id": "M-SP11", "file": RESOLVER,
        "old": """        found = SR.get_version(
            provenance.strategy_id, provenance.strategy_version,
            checksum=provenance.strategy_checksum, conn=conn,
        )""",
        "new": """        found = conn.execute(
            "SELECT strategy_id FROM paper_strategy_versions "
            "WHERE version=? AND checksum=?",
            (int(provenance.strategy_version), str(provenance.strategy_checksum)),
        ).fetchone()""",
        "test": f"{SP}.LedgerProvenanceTests.test_SP15_same_version_number_does_not_cross_resolve",
        "desc": "只按 version 查，不带 strategy_id（不同策略交叉解析）",
    },
    {
        "id": "M-SP14", "file": RESOLVER,
        "old": """        found = SR.get_version(
            provenance.strategy_id, provenance.strategy_version,
            checksum=provenance.strategy_checksum, conn=conn,
        )
    except ValueError:
        return False
    return found is not None""",
        "new": """        found = SR.get_version(
            provenance.strategy_id, provenance.strategy_version,
            checksum=provenance.strategy_checksum, conn=conn,
        )
    except ValueError:
        found = SR.get_version(
            provenance.strategy_id, provenance.strategy_version, conn=conn,
        )
    return found is not None""",
        "test": f"{SP}.SelectionProvenanceTests.test_SP05_wrong_checksum_is_rejected_not_corrected",
        "desc": "checksum mismatch 自动「纠正」成 Registry 里的值",
    },
    # ---- M-SP4 / M-SP5 / M-SP12：历史行与 run identity ----
    {
        "id": "M-SP4", "file": SELECTION,
        "old": """            replaced = [int(row[0]) for row in conn.execute(
                "SELECT id FROM paper_selection_runs WHERE trade_date=? AND strategy_id=? "
                "AND provenance_key IS ?",
                (trade_date, item["strategy_id"], provenance["provenance_key"])).fetchall()]""",
        "new": """            replaced = [int(row[0]) for row in conn.execute(
                "SELECT id FROM paper_selection_runs WHERE trade_date=? AND strategy_id=?",
                (trade_date, item["strategy_id"])).fetchall()]""",
        "test": f"{SP}.SelectionProvenanceTests.test_SP02_same_day_versions_do_not_overwrite_each_other",
        "desc": "same-day key 只保留 strategy_id，删除不同 version 的证据",
    },
    {
        "id": "M-SP5", "file": CONTRACT,
        "old": """        if stored_status == STATUS_LEGACY_UNPROVEN:
            return ProvenanceReading(None, STATUS_LEGACY_UNPROVEN, detail, subject)""",
        "new": """        if stored_status == STATUS_LEGACY_UNPROVEN:
            return ProvenanceReading(
                StrategySelectionProvenance(
                    strategy_id=strategy_id, strategy_version=1,
                    strategy_checksum="a" * 64, asof_day="1970-01-01",
                    scope=scope if scope in SCOPES else SCOPE_RESEARCH),
                STATUS_VERIFIED, "", subject)""",
        "test": f"{SP}.SelectionProvenanceTests.test_SP07_legacy_strategy_id_only_row_is_unproven",
        "desc": "legacy NULL version 自动补一个 current 版本",
    },
    {
        "id": "M-SP6", "file": CONTRACT,
        "old": """    if not seen:
        raise AsOfUnprovable("cannot prove as-of day: no declared candidate date")""",
        "new": """    if not seen:
        return dt.date.today().isoformat()""",
        "test": f"{SP}.FamilyBProvenanceTests.test_missing_asof_stays_unknown_instead_of_today",
        "desc": "无法证明 asof 时改用 today",
    },
    {
        "id": "M-SP7", "file": TRACKING,
        "old": "                provenance[\"asof_day\"], SP.SCOPE_RESEARCH, None,",
        "new": "                provenance[\"asof_day\"], SP.SCOPE_RESEARCH, 1,",
        "test": f"{SP}.FamilyBProvenanceTests.test_model_family_is_not_applicable_never_fabricated",
        "desc": "research run 偷偷绑定一个 cycle",
    },
    {
        "id": "M-SP12", "file": TRACKING,
        "old": """        if existing is not None and str(day) < _today().isoformat():""",
        "new": """        if existing is not None and False:""",
        "test": f"{SP}.FamilyBProvenanceTests.test_historical_run_date_is_immutable_before_any_write",
        "desc": "历史 run 可被新 current head 覆盖",
    },
    # ---- M-SP8 / M-SP9 / M-SP13 / M-SP15：signal / order 链 ----
    {
        "id": "M-SP8", "file": RESOLVER,
        "old": """    stamp = SR.cycle_stamp_for_account(conn, account_id, cycle_id=requested)
    if stamp is None:
        raise SignalCycleUnprovable(
            account_id, requested, "cycle has no pinned immutable strategy version"
        )
    return requested, tuple(stamp)""",
        "new": """    stamp = SR.cycle_stamp_for_account(conn, account_id, cycle_id=requested)
    if stamp is None:
        stamp = SR.stamp_for_account(conn, account_id)
    return requested, tuple(stamp)""",
        "test": f"{SP}.LedgerProvenanceTests.test_SP11b_signal_writer_fails_closed_without_a_cycle_pin",
        "desc": "signal writer 缺 pin 时改用任何可解析的戳（不再 fail closed）",
    },
    {
        "id": "M-SP9", "file": PAPER,
        "old": """    if signal_id is not None:
        return SRES.signal_order_provenance(
            conn, signal_id=signal_id, account_id=account_id,
            expected_cycle_id=cycle_id,
        ).stamp
    stamp = SR.stamp_for_account(conn, account_id, cycle_id=cycle_id)""",
        "new": """    stamp = SR.stamp_for_account(conn, account_id, cycle_id=cycle_id)""",
        "test": f"{SP}.LedgerProvenanceTests.test_SP12_order_stamp_is_inherited_from_its_signal",
        "desc": "order writer 不再继承 signal 的因果戳，改查 current head",
    },
    {
        "id": "M-SP10", "file": PAPER,
        "old": """                strategy_id TEXT, strategy_version INTEGER, strategy_checksum TEXT,
                cycle_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS paper_positions""",
        "new": """                cycle_id INTEGER,
                strategy_id TEXT, strategy_version INTEGER, strategy_checksum TEXT
            );
            CREATE TABLE IF NOT EXISTS paper_positions""",
        "test": f"{SP}.LedgerProvenanceTests.test_SP13_archive_copy_preserves_full_provenance",
        "desc": "archive 表列序与 live 表不一致（SELECT * 整行拷贝错位）",
    },
    {
        "id": "M-SP13", "file": RESOLVER,
        "old": """    requested = SP.canonical_cycle_id(cycle_id)
    if requested is None:
        raise SignalCycleUnprovable(
            account_id, None, "signal requires an explicit canonical cycle id"
        )
    stamp = SR.cycle_stamp_for_account(conn, account_id, cycle_id=requested)""",
        "new": """    row = conn.execute(
        "SELECT cycle_id FROM paper_accounts WHERE id=?", (str(account_id),)
    ).fetchone()
    requested = SP.canonical_cycle_id(row[0] if row is not None else None)
    if requested is None:
        raise SignalCycleUnprovable(
            account_id, None, "signal requires an explicit canonical cycle id"
        )
    stamp = SR.cycle_stamp_for_account(conn, account_id, cycle_id=requested)""",
        "test": f"{SP}.LedgerProvenanceTests.test_SP11c_explicit_cycle_beats_rebound_account_cycle",
        "desc": "忽略显式 cycle_id，改从 paper_accounts 重新解析（旧错误 authority）",
    },
    {
        "id": "M-SP15", "file": RESOLVER,
        "old": """    try:
        provenance = SP.StrategySelectionProvenance(
            strategy_id=version.strategy_id, strategy_version=version.version,
            strategy_checksum=version.checksum, asof_day=asof_day, scope=scope,
            cycle_id=int(cycle_id),
        )""",
        "new": """    _spec = SR.get(version.strategy_id, conn=conn)
    if _spec is None or str(getattr(_spec, "status", "")) != "active":
        return _unproven(
            f"strategy {strategy_id} is not currently active", strategy_id)
    try:
        provenance = SP.StrategySelectionProvenance(
            strategy_id=version.strategy_id, strategy_version=version.version,
            strategy_checksum=version.checksum, asof_day=asof_day, scope=scope,
            cycle_id=int(cycle_id),
        )""",
        "test": f"{SP}.SelectionProvenanceTests.test_SP06_archived_strategy_history_still_resolves",
        "desc": "archived strategy 无法 replay 历史 version（lifecycle 当历史权威）",
    },
    # ---- M-RF1..M-RF3：review findings（迁移重建顺序 / 中断恢复 / 冲突刷新）----
    {
        'id': 'M-RF1',
        'file': 'backend/selection_tracking.py',
        'old': '    conn.executescript(_runs_ddl(staged))\n    conn.execute(\n        f"""INSERT OR IGNORE INTO {staged}({select_legacy},\n                provenance_status, provenance_key, asof_day, scope, cycle_id,\n                strategy_id, strategy_version, strategy_checksum)\n            SELECT {select_legacy}, \'{SP.STATUS_LEGACY_UNPROVEN}\',\n                   \'{LEGACY_KEY_PREFIX}|\' || run_date || \'|\' || strategy,\n                   data_asof_date, \'{SP.SCOPE_RESEARCH}\', NULL, NULL, NULL, NULL\n            FROM selection_runs"""\n    )\n    conn.execute("DROP TABLE selection_runs")\n    conn.execute(f"ALTER TABLE {staged} RENAME TO selection_runs")',
        'new': '    conn.execute(f"ALTER TABLE selection_runs RENAME TO {legacy}")\n    conn.executescript(_runs_ddl(staged))\n    conn.execute(\n        f"""INSERT OR IGNORE INTO {staged}({select_legacy},\n                provenance_status, provenance_key, asof_day, scope, cycle_id,\n                strategy_id, strategy_version, strategy_checksum)\n            SELECT {select_legacy}, \'{SP.STATUS_LEGACY_UNPROVEN}\',\n                   \'{LEGACY_KEY_PREFIX}|\' || run_date || \'|\' || strategy,\n                   data_asof_date, \'{SP.SCOPE_RESEARCH}\', NULL, NULL, NULL, NULL\n            FROM {legacy}"""\n    )\n    conn.execute(f"DROP TABLE {legacy}")\n    conn.execute(f"ALTER TABLE {staged} RENAME TO selection_runs")',
        'test': 'test_strategy_selection_provenance.RunTableRebuildTests.test_RF01_rebuild_keeps_child_fk_pointing_at_selection_runs',
        'desc': 'run 表重建改回「先重命名父表」（子表 FK 跟着走，DROP 后悬空）',
    },
    {
        'id': 'M-RF2',
        'file': 'backend/selection_tracking.py',
        'old': '    _absorb_leftover_runs(conn, legacy, staged)',
        'new': '    conn.execute(f"DROP TABLE IF EXISTS {legacy}")',
        'test': 'test_strategy_selection_provenance.RunTableRebuildTests.test_RF02_interrupted_rebuild_does_not_discard_the_only_copy',
        'desc': '中断恢复无条件 DROP legacy 表（丢掉唯一副本）',
    },
    {
        'id': 'M-RF3',
        'file': 'backend/paper_trading.py',
        'old': '                               created_at=excluded.created_at"""',
        'new': '                               created_at=excluded.created_at,\n                               strategy_id=excluded.strategy_id,\n                               strategy_version=excluded.strategy_version,\n                               strategy_checksum=excluded.strategy_checksum,\n                               cycle_id=excluded.cycle_id"""',
        'test': 'test_strategy_selection_provenance.SignalRefreshTests.test_RF04_bootstrap_refresh_updates_a_pre_upgrade_signal',
        'desc': '刷新语句重新写入不可变 provenance 列（升级后首次刷新 abort）',
    },
    # ---- M-SP19..M-SP21：本轮两个 provenance correctness blocker ----
    {
        'id': 'M-SP19', 'file': RESOLVER,
        'old': """    requested = SP.canonical_cycle_id(cycle_id)
    if requested is None:
        raise SignalCycleUnprovable(
            account_id, None, "signal requires an explicit canonical cycle id"
        )
    observed = SP.canonical_cycle_id(account_cycle_id)
    if observed != requested:
        raise SignalStaleContext(account_id, requested,
                                 observed if observed is not None else "none")
    stamp = SR.cycle_stamp_for_account(conn, account_id, cycle_id=requested)""",
        'new': """    row = conn.execute(
        "SELECT cycle_id FROM paper_accounts WHERE id=?", (str(account_id),)
    ).fetchone()
    requested = SP.canonical_cycle_id(row[0] if row is not None else None)
    if requested is None:
        raise SignalCycleUnprovable(
            account_id, None, "signal requires an explicit canonical cycle id"
        )
    stamp = SR.cycle_stamp_for_account(conn, account_id, cycle_id=requested)""",
        'test': "test_provenance_inflight_change.SignalCycleRolloverTests"
                ".test_RV01_close_signal_rollover_does_not_restamp_old_candidates",
        'desc': 'signal resolver 改回读取 mutable paper_accounts.cycle_id'
                '（rollover 后把旧候选写成新周期）',
    },
    {
        'id': 'M-SP20', 'file': SELECTION,
        # 合法的**错误实现**：pin 挪到 ``_run_one`` 返回之后（仍在同一 try 体内，
        # 因此语法/名称都是有效的）。这正是修复前的因果顺序 —— 计算期间发布的
        # 版本会被记成产出该结果的那一版。
        # 必须是「可运行的业务错误」，绝不能退化成 NameError/语法错误：
        # 那种 RED 什么也证明不了（fake detector 也会把它计为 FAKE）。
        'old': """            pin = _pin_research_version(item["strategy_id"])
            try:
                result = _run_one(item["model_id"], topn)""",
        'new': """            try:
                result = _run_one(item["model_id"], topn)
                pin = _pin_research_version(item["strategy_id"])""",
        'test': "test_provenance_inflight_change.ResearchVersionInflightTests"
                ".test_RV07_inflight_version_publication_does_not_change_the_run_stamp",
        'desc': 'research strategy pin 移到 _run_one 之后（in-flight 发布被错误归因）',
    },
    {
        'id': 'M-SP21', 'file': RESOLVER,
        'old': """    observed = SP.canonical_cycle_id(account_cycle_id)
    if observed != requested:
        raise SignalStaleContext(account_id, requested,
                                 observed if observed is not None else "none")""",
        'new': """    observed = SP.canonical_cycle_id(account_cycle_id)
    if observed != requested:
        requested = observed""",
        'test': "test_provenance_inflight_change.SignalCycleRolloverTests"
                ".test_RV01_close_signal_rollover_does_not_restamp_old_candidates",
        'desc': 'rollover 时把旧批次迁移到新周期（而不是整批 stale abort）',
    },
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _adapt_eol(text: str, original: bytes) -> bytes:
    if original.count(b"\r\n") > 0:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("utf-8")


PYCACHE_ROOT = tempfile.mkdtemp(prefix="r23_mutation_pycache_")
_SEQ = [0]

#: 变异体必须因**契约断言**失败。语法 / 导入 / 运行时接线错误都是假杀，
#: 不能计为 CAUGHT —— 一个 ``NameError`` 也能让测试变红，但它证明不了任何
#: 业务性质（测试根本没跑到被测的契约断言）。
BROKEN_RE = re.compile(
    r"(SyntaxError|IndentationError|TabError"
    r"|ImportError|ModuleNotFoundError"
    r"|NameError|UnboundLocalError"
    r"|_FailedTest|AttributeError: module"
    r"|TypeError: .*takes .* positional argument"
    r"|is not defined|local variable .* referenced before assignment)",
    re.MULTILINE,
)


def _next_seq() -> int:
    """Return a strictly increasing run id.

    Every subprocess invocation needs its own ``PYTHONPYCACHEPREFIX``: sharing a
    cache directory lets the baseline's ``.pyc`` be reused by the mutant (and
    vice versa), which silently invalidates the matrix.
    """
    _SEQ[0] += 1
    return _SEQ[0]


def run_test(target: str, seq: int | None = None) -> subprocess.CompletedProcess:
    if seq is None:
        seq = _next_seq()
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = os.path.join(PYCACHE_ROOT, f"run{seq:03d}")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "unittest", target],
        cwd=BACKEND, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=900, env=env,
    )


class _ShortCircuit(RuntimeError):
    """Raised by the self-test's subprocess stub."""


def self_test_sequence() -> None:
    """Static + behavioural assertion that run caches never collapse to one dir."""
    seen = [_next_seq() for _ in range(5)]
    assert len(set(seen)) == len(seen), f"sequence not unique: {seen}"
    assert seen == sorted(seen), f"sequence not increasing: {seen}"
    dirs: list[str] = []
    original = subprocess.run
    try:
        def _capture(args, **kwargs):
            dirs.append(kwargs["env"]["PYTHONPYCACHEPREFIX"])
            raise _ShortCircuit
        subprocess.run = _capture  # type: ignore[assignment]
        for _ in range(3):
            try:
                run_test("unittest")
            except _ShortCircuit:
                pass
    finally:
        subprocess.run = original  # type: ignore[assignment]
    assert len(dirs) == 3, f"expected 3 invocations, got {dirs}"
    assert len(set(dirs)) == 3, f"invocations share a cache dir: {dirs}"


def assert_no_leftover(mutation: dict) -> None:
    path = os.path.join(ROOT, mutation.get("file_override", mutation["file"]))
    if "MUTANT" in open(path, "r", encoding="utf-8").read():
        raise RuntimeError(f'{mutation["id"]}: leftover mutant in {mutation["file"]}')


def _apply(text: str, mutation: dict) -> str:
    """Apply one mutation, requiring its anchor to be **unique**.

    ``replace(..., 1)`` rewrites the first hit, so a duplicated anchor would let the
    mutation land on a different call site than intended while still reporting
    CAUGHT. The invariant is enforced here, in the function that actually performs
    the rewrite, rather than only in a separate audit script.
    """
    old, new = mutation["old"], mutation["new"]
    count = text.count(old)
    assert count == 1, (
        f'{mutation["id"]}: mutation anchor must be unique; count={count}; '
        f'file={mutation["file"]}'
    )
    return text.replace(old, new, 1)


def _is_fake_kill(result: subprocess.CompletedProcess) -> bool:
    blob = (result.stdout or "") + (result.stderr or "")
    return bool(BROKEN_RE.search(blob))


def run_mutation(mutation: dict, *, non_vacuity: bool) -> str:
    """Return ``CAUGHT`` / ``SURVIVED`` / ``FAKE`` / ``VACUOUS`` / ``BASELINE-RED``."""
    path = os.path.join(ROOT, mutation.get("file_override", mutation["file"]))
    with open(path, "rb") as handle:
        original = handle.read()
    before = sha256(original)
    text = original.decode("utf-8").replace("\r\n", "\n")

    if non_vacuity:
        baseline = run_test(mutation["test"])
        if baseline.returncode != 0:
            return f"BASELINE-RED({baseline.returncode})"

    mutated = _apply(text, mutation)
    try:
        with open(path, "wb") as handle:
            handle.write(_adapt_eol(mutated, original))
        result = run_test(mutation["test"])
        if result.returncode == 0:
            return "SURVIVED"
        if _is_fake_kill(result):
            return "FAKE"
        return "CAUGHT"
    finally:
        with open(path, "wb") as handle:
            handle.write(original)
        with open(path, "rb") as handle:
            after = sha256(handle.read())
        if after != before:
            raise RuntimeError(f'{mutation["id"]}: restore sha256 mismatch')
        assert_no_leftover(mutation)


def main() -> int:
    print(f"repo root: {ROOT}")
    argv = sys.argv[1:]
    only: set[str] | None = None
    if "--only" in argv:
        only = {item for item in argv[argv.index("--only") + 1].split(",") if item}
    non_vacuity = "--non-vacuity" in argv

    self_test_sequence()
    print("runner self-test: PASS (unique, increasing pycache sequence)")

    selected = [m for m in MUTATIONS if only is None or m["id"] in only]
    results: list[tuple[str, str]] = []
    for mutation in selected:
        verdict = run_mutation(mutation, non_vacuity=non_vacuity)
        results.append((mutation["id"], verdict))
        print(f'{mutation["id"]} {mutation["desc"]}: {verdict}', flush=True)

    bad = [(mid, v) for mid, v in results if v != "CAUGHT"]
    for mid, verdict in bad:
        print(f"NOT-CAUGHT {mid}: {verdict}")
    print(f"R23 mutations: {len(results) - len(bad)}/{len(results)} CAUGHT; "
          f"survived={sum(1 for _, v in bad if v.startswith('SURVIVED'))}; "
          f"fake={sum(1 for _, v in bad if v == 'FAKE')}; "
          f"other={sum(1 for _, v in bad if not v.startswith('SURVIVED') and v != 'FAKE')}")
    print("restore sha256: PASS")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
