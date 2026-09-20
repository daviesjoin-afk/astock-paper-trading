# -*- coding: utf-8 -*-
"""R16 负向变异矩阵（M-RS1 ~ M-RS14）。

每条变异都对应一条 R16 / 架构契约，必须让**对应**契约测试变红 —— 否则门禁是空的。

流程（每条变异，严格逐字节备份/还原）::

    1. 读取目标文件字节 + sha256；
    2. 断言变异锚点存在（not found = 立即失败，绝不静默跳过）；
    3. 注入变异（bytes 级替换，只替换第一处）；
    4. 用仓库真实测试套件跑对应契约测试（``python -m unittest``）；
    5. 断言 RED：退出码非 0，且输出中目标测试以 FAIL:/ERROR: 出现
       （non-vacuity —— 失败必须来自对应契约，而不是 import/收集错误）；
    6. finally 逐字节还原，sha256 必须与原文件一致。

**每次注入都必须用独立冷字节码缓存**（``PYTHONPYCACHEPREFIX``）：CPython 判定
``.pyc`` 是否有效只看源文件 mtime（秒级）+ 字节长度。两条**字节长度相同**的变异
若发生在同一秒内，第二条会被误判"缓存仍然有效"而直接复用上一条的旧字节码 ——
注入没有真正执行，测试却是绿的，于是产生**假阴性**。

变异矩阵::

    M-RS1   scan identity 去掉 cycle_id              -> RS-06 同分钟跨周期独立
    M-RS2   scan identity 去掉 asof_date             -> RS-07 同分钟跨 asof 独立
    M-RS3   failed 身份不允许重试                    -> RS-04 failed 可重试
    M-RS4   failed 重试不递增 attempt                -> RS-05 attempt 递增
    M-RS5   complete 不检查 status='running'         -> RS-08 CAS 精确命中
    M-RS6   身份可被 UPDATE 改写                     -> RS-10 identity immutable
    M-RS7   非法周期行被允许插入                     -> RS-11 非法 cycle 拒绝
    M-RS8   快照改回 current_positions()             -> Guard 7h + RISK-SCAN-P4
    M-RS9   外部 I/O 后删掉 cycle fence              -> RISK-SCAN-P4 cycle rollover
    M-RS10  cycle changed 时自动改用新周期继续        -> RISK-SCAN-P4 fail closed
    M-RS11  catch block 重新计算 scan_minute          -> RISK-SCAN-P6 分钟边界
    M-RS12  paper_trading 重新读 audit 做控制         -> Guard 7f
    M-RS13  paper_risk_scan_state import paper_trading -> Guard 7a
    M-RS14  migration 从旧 paper_audit 回填           -> RS-12 不回填

用法（仓库根目录）::

    python work/r16_mutation_check.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
SCAN_MODULE = "test_paper_risk_scan_state"
GUARD_MODULE = "test_paper_trading_architecture_guard"
PRODUCTION_MODULE = "test_paper_risk_exit_production_path"
SCAN_STATE_FILE = "backend/paper_risk_scan_state.py"
PAPER_TRADING_FILE = "backend/paper_trading.py"
MIGRATIONS_FILE = "backend/paper_schema_migrations.py"


def b(text: str) -> bytes:
    return text.encode("utf-8")


#: 每次测试用一份全新的字节码缓存目录，杜绝"同长度变异复用旧 .pyc"的假阴性。
#: 放在系统临时目录（而非仓库内），避免生成物污染工作区。
PYCACHE_ROOT = tempfile.mkdtemp(prefix="r16_mutation_pycache_")
_PYCACHE_SEQ = [0]


def run_test(target: str) -> subprocess.CompletedProcess:
    _PYCACHE_SEQ[0] += 1
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = os.path.join(PYCACHE_ROOT, f"run{_PYCACHE_SEQ[0]:03d}")
    return subprocess.run(
        [sys.executable, "-m", "unittest", target],
        cwd=BACKEND, capture_output=True, text=True, timeout=600, env=env,
    )


MUTATIONS = [
    {
        "id": "M-RS1",
        "file": SCAN_STATE_FILE,
        "old": '        " ON CONFLICT(cycle_id,asof_date,scan_minute) DO NOTHING",\n',
        # 唯一冲突目标去掉 cycle_id：同分钟跨周期会被当成同一身份互相抑制。
        "new": '        " ON CONFLICT(asof_date,scan_minute) DO NOTHING",\n',
        "test": f"{SCAN_MODULE}.IdentityScopeTests."
                "test_rs06_different_cycle_same_minute_is_independent",
        "desc": "scan identity 去掉 cycle_id（同分钟跨周期互相抑制）",
    },
    {
        "id": "M-RS2",
        "file": SCAN_STATE_FILE,
        "old": '    return cycle_id, asof, minute\n',
        # 身份丢掉 asof_date：同一分钟的不同 asof replay 会碰撞。
        "new": '    return cycle_id, "1970-01-01", minute  # mutation: 丢掉 asof\n',
        "test": f"{SCAN_MODULE}.IdentityScopeTests."
                "test_rs07_different_asof_same_minute_is_independent",
        "desc": "scan identity 去掉 asof_date（同分钟不同 asof 碰撞）",
    },
    {
        "id": "M-RS3",
        "file": SCAN_STATE_FILE,
        "old": '    if status == "failed":\n',
        # failed 不再可重试：provider 异常后的同分钟重试被当成已完成吞掉。
        "new": '    if False:  # mutation: failed 身份不允许重试\n',
        "test": f"{SCAN_MODULE}.ClaimLifecycleTests.test_rs04_failed_identity_is_retryable",
        "desc": "failed 身份不允许重试（异常后同分钟重试被吞掉）",
    },
    {
        "id": "M-RS4",
        "file": SCAN_STATE_FILE,
        "old": '            " SET status=\'running\', attempt=attempt+1, started_at=?,"\n',
        "new": '            " SET status=\'running\', attempt=attempt, started_at=?,"\n',
        "test": f"{SCAN_MODULE}.ClaimLifecycleTests."
                "test_rs05_retry_increments_attempt_and_clears_failure",
        "desc": "failed 重试不递增 attempt（重试次数不可观测）",
    },
    {
        "id": "M-RS5",
        "file": SCAN_STATE_FILE,
        "old": '        " WHERE cycle_id=? AND asof_date=? AND scan_minute=? AND status=\'running\'",\n'
               '        (finished, _json(detail), *identity),\n',
        # complete 不再要求 status='running'：可以对 failed/completed 重复"完成"。
        "new": '        " WHERE cycle_id=? AND asof_date=? AND scan_minute=?",\n'
               '        (finished, _json(detail), *identity),\n',
        "test": f"{SCAN_MODULE}.ExactIdentityCastests."
                "test_rs08b_completion_twice_is_a_conflict",
        "desc": "complete 不检查 status='running'（状态转换不再精确）",
    },
    {
        "id": "M-RS6",
        "file": MIGRATIONS_FILE,
        "old": '            WHEN NEW.cycle_id IS NOT OLD.cycle_id\n'
               '              OR NEW.asof_date IS NOT OLD.asof_date\n'
               '              OR NEW.scan_minute IS NOT OLD.scan_minute\n',
        # 身份可变：repair 脚本能把一次跑过的扫描改挂到别的周期/日期/分钟。
        "new": '            WHEN 0\n',
        "test": f"{SCAN_MODULE}.IdentityImmutabilityTests."
                "test_rs10_identity_columns_are_immutable",
        "desc": "scan identity 可被 UPDATE 改写（事后重写归属历史）",
    },
    {
        "id": "M-RS7",
        "file": MIGRATIONS_FILE,
        "old": '    when = "NEW.cycle_id IS NULL"\n'
               '    if has_cycles:\n'
               '        when += (" OR NOT EXISTS (SELECT 1 FROM paper_cycles c"\n'
               '                 " WHERE c.id=NEW.cycle_id)")\n'
               '    conn.execute(\n'
               '        f"""CREATE TRIGGER IF NOT EXISTS trg_paper_risk_scan_runs_cycle_required_insert\n',
        # 不存在的周期也被允许：扫描归属可以指向任何 id。
        "new": '    when = "NEW.cycle_id IS NULL AND 0"\n'
               '    if has_cycles:\n'
               '        when += " AND 0"\n'
               '    conn.execute(\n'
               '        f"""CREATE TRIGGER IF NOT EXISTS trg_paper_risk_scan_runs_cycle_required_insert\n',
        "test": f"{SCAN_MODULE}.InvalidCycleTests.test_rs11_nonexistent_cycle_is_rejected",
        "desc": "非法/不存在的 cycle 被允许插入（扫描归属可伪造）",
    },
    {
        "id": "M-RS8",
        "file": PAPER_TRADING_FILE,
        "old": '        positions = [p for p in PPRM.positions_for_cycle(snapshot_conn, cycle_id, asof_day=day)\n'
               '                     if p["account_id"] in risk_ids]\n',
        # 快照改回 current-cycle 读取：扫描开始后周期 rollover 就会读到新周期持仓。
        "new": '        positions = [p for p in _position_rows(snapshot_conn, asof_day=day)\n'
               '                     if p["account_id"] in risk_ids]\n',
        "test": f"{GUARD_MODULE}.RiskScanStateIsACycleOwnedBoundary."
                "test_guard7h_scan_snapshot_is_pinned_to_the_claimed_cycle",
        "desc": "风险扫描快照改回 current_positions()（不再钉死已认领周期）",
    },
    {
        "id": "M-RS9",
        "file": PAPER_TRADING_FILE,
        "old": '        PRSS.assert_cycle_active(conn, cycle_id=cycle_id)\n'
               '        risk_ids = _risk_exit_account_ids(conn)\n',
        # 删掉外部 I/O 之后的 cycle fence：旧快照直接操作新周期。
        "new": '        risk_ids = _risk_exit_account_ids(conn)\n',
        "test": f"{PRODUCTION_MODULE}.TestRiskScanLifecycleProductionPath."
                "test_RISK_SCAN_P4_cycle_rollover_during_quote_fetch_fails_closed",
        "desc": "外部 I/O 后删掉 cycle fence（周期 rollover 仍继续执行）",
    },
    {
        "id": "M-RS10",
        "file": SCAN_STATE_FILE,
        "old": '    if current != expected:\n'
               '        raise RiskScanCycleChanged(\n'
               '            f"risk scan 已认领 cycle {expected}，但当前 active cycle 是 {current}"\n'
               '        )\n',
        # cycle changed 时静默"采纳"新周期：provenance fabrication。
        "new": '    if current != expected:\n'
               '        return current  # mutation: 自动改用新周期继续执行\n',
        "test": f"{PRODUCTION_MODULE}.TestRiskScanLifecycleProductionPath."
                "test_RISK_SCAN_P4_cycle_rollover_during_quote_fetch_fails_closed",
        "desc": "cycle changed 时自动改用新周期继续（provenance fabrication）",
    },
    {
        "id": "M-RS11",
        "file": PAPER_TRADING_FILE,
        "old": '                PRSS.fail_scan(conn, **ident, finished_at=_now(),\n'
               '                               error=f"{type(exc).__name__}: {exc}")\n',
        # 真实行为变异：catch block 重新取一次时钟来拼身份，而不是复用认领时的
        # identity。跨分钟边界时 failed 落在另一个身份上，原来的 running 永远
        # 不会转换 ⇒ orphan running（R16 的第三条真实缺陷）。
        "new": '                PRSS.fail_scan(\n'
               '                    conn, cycle_id=ident["cycle_id"],\n'
               '                    asof_date=ident["asof_date"],\n'
               '                    scan_minute=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),\n'
               '                    finished_at=_now(),\n'
               '                    error=f"{type(exc).__name__}: {exc}")\n',
        "test": f"{PRODUCTION_MODULE}.TestRiskScanLifecycleProductionPath."
                "test_RISK_SCAN_P6_minute_boundary_leaves_no_orphan_running",
        "desc": "scan identity 被计算两次（跨分钟边界留下 orphan running）",
    },
    {
        "id": "M-RS12",
        "file": PAPER_TRADING_FILE,
        "old": '        claimed = PRSS.claim_scan(\n'
               '            conn, **ident, started_at=scan_context["started_at"])\n',
        # paper_audit 重新充当执行权威。
        "new": '        _row = conn.execute(\n'
               '            "SELECT detail FROM paper_audit WHERE event=\'risk_scan_state\'"\n'
               '            " ORDER BY id DESC LIMIT 1").fetchone()\n'
               '        claimed = ({"claimed": False, "state": "completed", "attempt": 1}\n'
               '                   if _row else\n'
               '                   PRSS.claim_scan(\n'
               '                       conn, **ident, started_at=scan_context["started_at"]))\n',
        "test": f"{GUARD_MODULE}.RiskScanStateIsACycleOwnedBoundary."
                "test_guard7f_paper_audit_is_not_risk_scan_control_state",
        "desc": "paper_audit 重新充当 risk scan 执行权威",
    },
    {
        "id": "M-RS13",
        "file": SCAN_STATE_FILE,
        "old": 'import json\nimport sqlite3\n',
        # 反向 import：状态模块变成 god module 的延伸。
        "new": 'import json\nimport sqlite3\n\nimport paper_trading  # mutation: 反向依赖\n',
        "test": f"{GUARD_MODULE}.RiskScanStateIsACycleOwnedBoundary."
                "test_guard7a_scan_state_module_has_zero_project_imports",
        "desc": "paper_risk_scan_state 反向 import paper_trading",
    },
    {
        "id": "M-RS14",
        "file": MIGRATIONS_FILE,
        "old": '    changes = {}\n'
               '    if not table_columns(conn, "paper_risk_scan_runs"):\n'
               '        conn.execute(risk_scan_run_ddl("paper_risk_scan_runs"))\n'
               '        changes["paper_risk_scan_runs"] = "created"\n',
        # 从旧 audit 标记回填：把"不知道"洗白成"知道"。
        "new": '    changes = {}\n'
               '    if not table_columns(conn, "paper_risk_scan_runs"):\n'
               '        conn.execute(risk_scan_run_ddl("paper_risk_scan_runs"))\n'
               '        changes["paper_risk_scan_runs"] = "created"\n'
               '        _cur = conn.execute(\n'
               '            "SELECT id FROM paper_cycles ORDER BY id DESC LIMIT 1").fetchone()\n'
               '        if _cur is not None:\n'
               '            conn.execute(\n'
               '                "INSERT OR IGNORE INTO paper_risk_scan_runs(cycle_id,asof_date,"\n'
               '                "scan_minute,status,attempt,started_at,detail) "\n'
               '                "SELECT ?,substr(created_at,1,10),substr(created_at,1,16),"\\\n'
               '                "\'completed\',1,created_at,\'{}\' FROM paper_audit "\n'
               '                "WHERE event=\'risk_scan_state\'", (_cur[0],))\n',
        "test": f"{SCAN_MODULE}.NoAuditBackfillTests."
                "test_rs12_migration_leaves_new_table_empty",
        "desc": "migration 从旧 paper_audit 回填 scan runs（伪造历史归属）",
    },
    {
        "id": "M-RS15",
        "file": PAPER_TRADING_FILE,
        "old": '        result = _monitor_risk_impl(asof_date, cycle_id=ident["cycle_id"])\n'
               '        with _db(immediate=True, hot_path=True) as conn:\n'
               '            _assert_active_lease(conn, "risk scan completion")\n'
               '            PRSS.complete_scan(conn, **ident, finished_at=_now())\n'
               '            _audit(conn, None, "risk_scan_completed", _json(dict(ident)))\n'
               '        return result\n'
               '    except Exception as exc:\n'
               '        if _lease_lost(exc):\n'
               '            raise\n'
               '        try:\n'
               '            with _db(immediate=True, hot_path=True) as conn:\n',
        # 把 completion 移回 try 之外（评审指出的原始结构）：收尾事务自身失败时
        # 异常绕过 fail 路径，durable 行永远停在 running。
        "new": '        result = _monitor_risk_impl(asof_date, cycle_id=ident["cycle_id"])\n'
               '    except Exception as exc:\n'
               '        if _lease_lost(exc):\n'
               '            raise\n'
               '        try:\n'
               '            with _db(immediate=True, hot_path=True) as conn:\n',
        "test": f"{PRODUCTION_MODULE}.TestRiskScanLifecycleProductionPath."
                "test_RISK_SCAN_P7_completion_failure_does_not_orphan_running",
        "desc": "completion 移出受保护区域（收尾失败不再回落 fail，留下 orphan）",
    },
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    print(f"repo root: {ROOT}")
    for module in (SCAN_MODULE, GUARD_MODULE, PRODUCTION_MODULE):
        base = run_test(module)
        if base.returncode != 0:
            print(f"BASELINE FAILED —— {module} 必须全绿，终止。")
            print(base.stdout[-2000:], base.stderr[-2000:])
            return 2
    print(f"BASELINE: {SCAN_MODULE} + {GUARD_MODULE} + {PRODUCTION_MODULE} all green\n")

    results = []
    for mut in MUTATIONS:
        path = os.path.join(ROOT, mut["file"])
        with open(path, "rb") as fh:
            original = fh.read()
        original_hash = sha256(original)
        try:
            candidates = []
            for style in ("\n", "\r\n"):
                if style == "\r\n" and b"\r\n" not in original:
                    continue
                old_b = b(mut["old"].replace("\n", style))
                if old_b in original:
                    candidates.append((old_b, b(mut["new"].replace("\n", style))))
            if not candidates:
                raise AssertionError(f"变异锚点未找到: {mut['id']} in {mut['file']}")
            old_b, new_b = candidates[0]
            injected = original.replace(old_b, new_b, 1)
            if injected == original:
                raise AssertionError(f"变异未产生任何字节变化: {mut['id']}")
            with open(path, "wb") as fh:
                fh.write(injected)

            proc = run_test(mut["test"])
            output = (proc.stdout or "") + (proc.stderr or "")
            red = proc.returncode != 0
            method = mut["test"].split(".")[-1]
            marker = f"FAIL: {method}" in output or f"ERROR: {method}" in output
            status = "RED" if (red and marker) else "SUSPECT"
            results.append((mut["id"], mut["desc"], status, mut["test"]))
            print(f"[{mut['id']}] {mut['desc']}")
            print(f"    -> {status} (rc={proc.returncode}, contract_failure={marker})")
        finally:
            with open(path, "wb") as fh:
                fh.write(original)
            with open(path, "rb") as fh:
                restored = fh.read()
            assert sha256(restored) == original_hash, f"还原后 sha256 不一致: {mut['file']}"

    print("\n===== R16 mutation summary =====")
    failed = [r for r in results if r[2] != "RED"]
    for mid, desc, status, _test in results:
        print(f"  {mid:<8} {status:<8} {desc}")
    if failed:
        print(f"\nRESULT: {len(failed)} mutation(s) NOT confirmed -> DO NOT TRUST GATE")
        return 1
    print(f"\nRESULT: {len(results)}/{len(results)} mutations RED, "
          "all files restored byte-identical")
    return 0


if __name__ == "__main__":
    try:
        _code = main()
    finally:
        shutil.rmtree(PYCACHE_ROOT, ignore_errors=True)
    raise SystemExit(_code)
