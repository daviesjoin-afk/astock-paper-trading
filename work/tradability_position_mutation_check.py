# -*- coding: utf-8 -*-
"""Position-aware T+1 Shadow 变异矩阵 M-T1-1 … M-T1-10。

用法::

    python work/tradability_position_mutation_check.py            # 跑矩阵
    python work/tradability_position_mutation_check.py --audit    # 只审锚点（只读）

设计口径与 ``work/tradability_mutation_check.py`` 一致：每条变异显式携带目标文件；
变异前后都清 ``__pycache__`` 并关闭字节码写入，避免同一秒内的写入被缓存掩盖；
变异体必须**可导入** —— ``SyntaxError`` / ``IMPORT-FAILED`` 假杀不算 CAUGHT；
变异之后必须逐字节还原并校验 sha256。

判定语义::

* ``CAUGHT``     = 变异后契约测试失败（缺陷被抓住）；
* ``UNDETECTED`` = 变异后测试仍全绿（缺陷漏网）—— 任一出现即退出码 1；
* ``EQUIVALENT`` = 显式登记的等价变异（见 ``EQUIVALENT_MUTATIONS``），
  必须由本文件里的 ``verify_equivalent`` 给出**可执行证明**，不能只是口头声明。

``S0`` 是自检哨兵（只改注释），必须 UNDETECTED；它若被判成 CAUGHT，说明测试基线
本来就是红的，整个矩阵的结论不成立。
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
    "test_tradability_position_shadow",
    "test_tradability_position_shadow_architecture_guard",
)

ADAPTER = "backend/tradability_position_evidence.py"
SHADOW = "backend/tradability_position_shadow.py"

# (id, 目标文件, 变异前, 变异后, 说明)
MUTATIONS = (
    (
        "M-T1-1",
        ADAPTER,
        "            exit_session=decision_session,\n"
        "            entry_session=lot.acquisition_session,\n",
        "            exit_session=decision_session,\n"
        "            # MUTANT M-T1-1: acquisition context dropped on SELL\n"
        "            entry_session=None,\n",
        "SELL 时丢掉 entry/acquisition context",
    ),
    (
        "M-T1-2",
        ADAPTER,
        "                            fill_session = sessions.pop()\n",
        "                            # MUTANT M-T1-2: the intended order date usurps the actual fill\n"
        "                            fill_session = _session_of(_row_field(order, \"created_at\"))\n",
        "实际成交 session 被订单创建日（意图日）冒充",
    ),
    (
        "M-T1-3",
        ADAPTER,
        "        if not kept:\n"
        "            status = PositionEvidenceStatus.UNKNOWN\n"
        "            diagnostics.append(\"no_open_lot_visible_at_decision\")\n"
        "        elif unknown == 0:\n"
        "            status = PositionEvidenceStatus.PROVEN\n"
        "        elif sellable + locked > 0:\n"
        "            status = PositionEvidenceStatus.PARTIAL\n"
        "        else:\n"
        "            status = PositionEvidenceStatus.UNPROVABLE\n",
        "        # MUTANT M-T1-3: anything with a sellable shard is declared fully proven\n"
        "        if sellable + locked > 0:\n"
        "            status = PositionEvidenceStatus.PROVEN\n"
        "        elif not kept:\n"
        "            status = PositionEvidenceStatus.UNKNOWN\n"
        "        else:\n"
        "            status = PositionEvidenceStatus.UNPROVABLE\n",
        "缺建仓证据 / 部分证据默认判已证明（unknown 折成可卖）",
    ),
    (
        "M-T1-4",
        ADAPTER,
        "            earliest = ST.earliest_sellable_session(\n"
        "                lot.code, name=None, entry_session=lot.acquisition_session\n"
        "            )\n",
        "            # MUTANT M-T1-4: calendar +1 day replaces the authoritative calendar\n"
        "            import datetime as _d\n"
        "            _base = _d.date.fromisoformat(str(lot.acquisition_session)[:10])\n"
        "            earliest = (_base + _d.timedelta(days=1)).isoformat()\n",
        "下一交易日被换成日历 +1 天",
    ),
    (
        "M-T1-5",
        ADAPTER,
        "            earliest = ST.earliest_sellable_session(\n"
        "                lot.code, name=None, entry_session=lot.acquisition_session\n"
        "            )\n",
        "            # MUTANT M-T1-5: T+0 ETF forced onto the T+1 calendar\n"
        "            earliest = PTR.next_weekday(lot.acquisition_session)\n",
        "T+0 ETF 被当 T+1",
    ),
    (
        "M-T1-6",
        ADAPTER,
        "        if reason == ST.REASON_T1_NOT_SELLABLE:\n"
        "            return dataclasses.replace(\n"
        "                lot, sellability=LotSellability.BLOCKED, sellability_reason=reason,\n",
        "        if reason == ST.REASON_T1_NOT_SELLABLE:  # MUTANT M-T1-6\n"
        "            # normal stock treated as T+0 (same-day sell allowed)\n"
        "            return dataclasses.replace(\n"
        "                lot, sellability=LotSellability.SELLABLE, sellability_reason=reason,\n",
        "普通股票被当 T+0",
    ),
    (
        "M-T1-7",
        ADAPTER,
        "            if not self._visible(lot.available_at, asof):\n",
        "            if False:  # MUTANT M-T1-7: future evidence leaks into the snapshot\n",
        "未来成交 / 仓位证据泄漏进历史快照",
    ),
    (
        "M-T1-8",
        ADAPTER,
        "        held = sum(max(0, lot.historical_quantity) for lot in kept)\n"
        "        sellable = sum(max(0, lot.historical_quantity) for lot in kept\n"
        "                       if lot.sellability == LotSellability.SELLABLE)\n",
        "        # MUTANT M-T1-8: mixed lots collapse into the single earliest entry session\n"
        "        _earliest = min((lot.acquisition_session for lot in kept\n"
        "                         if lot.acquisition_session), default=None)\n"
        "        _verdicts = {lot.acquisition_session: lot.sellability for lot in kept}\n"
        "        _unified = _verdicts.get(_earliest)\n"
        "        held = sum(max(0, lot.historical_quantity) for lot in kept)\n"
        "        sellable = (held if _unified == LotSellability.SELLABLE else 0)\n",
        "混合 lot 被压成单一最早 entry date",
    ),
    (
        "M-T1-9",
        SHADOW,
        "        comparable = context.comparable\n",
        "        # MUTANT M-T1-9: any position with a proof status counts as comparable\n"
        "        comparable = context.held_quantity > 0\n",
        "position unknown / unprovable 被算成可比（进 agreement / disagreement 分母）",
    ),
    (
        "M-T1-10",
        SHADOW,
        "        if not comparable:\n"
        "            status = PositionShadowStatus.NOT_COMPARABLE\n"
        "        elif context.sellability_status == PE.SellabilityStatus.T1_SELLABLE:\n"
        "            status = PositionShadowStatus.COMPARABLE_T1_PASS\n"
        "        else:\n"
        "            status = PositionShadowStatus.COMPARABLE_T1_BLOCKED\n",
        "        # MUTANT M-T1-10: observation rewrites the production-side market status\n"
        "        status = PositionShadowStatus.COMPARABLE_T1_BLOCKED\n"
        "        market_status = status\n",
        "仓位层改写生产侧结论（把市场层面 status 换成自己的）",
    ),
    (
        "M-HQ1",
        ADAPTER,
        "            historical = _int(snapshot.get(lot_id, 0))\n",
        "            # MUTANT M-HQ1: today's mutable balance usurps the decision-time quantity\n"
        "            historical = _int(_row_field(row, \"remaining_qty\"))\n",
        "当前可变余额被当作决策时点历史数量",
    ),
    (
        "M-HQ2",
        ADAPTER,
        "            if historical <= 0:\n",
        "            # MUTANT M-HQ2: a fully-consumed lot silently disappears from history\n"
        "            if _int(_row_field(row, \"remaining_qty\")) <= 0:\n",
        "完全消耗的历史 lot 被静默丢弃",
    ),
    (
        "M-HQ3",
        ADAPTER,
        "        elif unknown == 0:\n"
        "            status = PositionEvidenceStatus.PROVEN\n",
        "        # MUTANT M-HQ3: a partially-proven position is declared fully proven\n"
        "        elif True:\n"
        "            status = PositionEvidenceStatus.PROVEN\n",
        "部分可证明的仓位被标成 position_proven",
    ),
    (
        "M-SCOPE1",
        ADAPTER,
        "\"FROM paper_position_lots WHERE cycle_id=? AND account_id=? AND code=?\"\n",
        "\"FROM paper_position_lots WHERE 1=1 AND account_id=? AND code=?\"\n",
        "cycle 过滤被删除（跨周期 lot 汇入同一 context）",
    ),
    (
        "M-SCOPE2",
        ADAPTER,
        "\"FROM paper_position_lots WHERE cycle_id=? AND account_id=? AND code=?\"\n",
        "\"FROM paper_position_lots WHERE cycle_id=? AND 1=1 AND code=?\"\n",
        "account 过滤被删除（跨账户份额被池化）",
    ),
    (
        "M-PIT1",
        ADAPTER,
        "            resolved_decision_at = _instant(decision_at)\n",
        "            # MUTANT M-PIT1: the caller's exact decision_at is replaced by session close\n"
        "            resolved_decision_at = _instant(ST.session_close_at(session))\n",
        "精确 decision_at 被 session close 覆盖",
    ),
    (
        "M-PIT2",
        ADAPTER,
        "            if asof is None:\n",
        "            if False:  # MUTANT M-PIT2: invalid validation_as_of widens to unlimited future\n",
        "非法 validation_as_of 被当成无上界（future leak）",
    ),
    (
        "M-ID1",
        ADAPTER,
        "                    if (order_account != account_id or lot_account != account_id\n"
        "                            or order_code != code or lot_code != code):\n",
        "                    # MUTANT M-ID1: the account half of the identity check is dropped\n"
        "                    if (order_code != code or lot_code != code):\n",
        "跨账户来源委托被接受",
    ),
    (
        "M-ID2",
        ADAPTER,
        "                            if (str(_row_field(item, \"side\") or \"\").lower() != \"buy\"\n"
        "                                    or _text(_row_field(item, \"account_id\")) != account_id\n"
        "                                    or _text(_row_field(item, \"code\")) != code):\n",
        "                            # MUTANT M-ID2: the code half of the fill identity check is dropped\n"
        "                            if (str(_row_field(item, \"side\") or \"\").lower() != \"buy\"\n"
        "                                    or _text(_row_field(item, \"account_id\")) != account_id):\n",
        "跨股票成交被接受",
    ),
    (
        "M-QTY1",
        ADAPTER,
        "            requested = _positive_int_or_none(requested_sell_quantity)\n"
        "            if requested is None:\n",
        "            # MUTANT M-QTY1: a non-positive sell quantity is coerced instead of rejected\n"
        "            requested = _int(requested_sell_quantity)\n"
        "            if False:\n",
        "非正数请求卖出量被接受（随后被判成可卖）",
    ),
    (
        "M-T1-11",
        ADAPTER,
        "                executed_at = event.get(\"executed_at\")\n",
        "                # MUTANT M-T1-11: same-session sell timing falls back to session close\n"
        "                executed_at = None\n",
        "同 session 卖出改用 session 收盘近似，忽略真实 executed_at",
    ),
    (
        "M-T1-12",
        ADAPTER,
        "        window = self._cycle_window(cycle_id)\n"
        "        clause = \"\"\n",
        "        # MUTANT M-T1-12: cycle window dropped from the sell-event query\n"
        "        window = None\n"
        "        clause = \"\"\n",
        "卖出事件查询丢掉周期窗过滤（窗外的卖出可扣减本周期 lot）",
    ),
    (
        "M-T1-13",
        ADAPTER,
        "            available_total = sum(remaining[lot[\"id\"]] for lot in eligible)\n"
        "            if available_total < qty:\n",
        "            available_total = sum(remaining[lot[\"id\"]] for lot in eligible)\n"
        "            if False:  # MUTANT M-T1-13: oversell只记诊断后继续\n",
        "卖出量超过当时可卖 lots 时只记诊断并 continue（不再立即 unprovable）",
    ),
    (
        "M-T1-14",
        ADAPTER,
        "                \"executed_at\": _instant(_row_field(row, \"executed_at\")),\n",
        "                # MUTANT M-T1-14: real execution instant dropped\n"
        "                \"executed_at\": None,\n",
        "卖出事件丢失真实成交时刻",
    ),
    (
        "M-T1-15",
        ADAPTER,
        "                    diagnostics.append(\"same_session_sell_time_unknown\")\n"
        "                    return {\"snapshot\": {}, \"final\": {}, \"consistent\": False,\n"
        "                            \"diagnostics\": diagnostics}\n",
        "                    # MUTANT M-T1-15: 盘中未知时刻被当成\"未发生\"\n"
        "                    consumed_before_decision = False\n",
        "盘中拿不到成交时刻时按\"尚未发生\"处理（猜值）",
    ),
    (
        "M-T1-16",
        SHADOW,
        "            self.account_id,\n"
        "            self.cycle_id,\n"
        "            self.code,\n",
        "            # MUTANT M-T1-16: account/cycle dropped from identity\n"
        "            self.code,\n",
        "观察身份丢掉 account_id / cycle_id",
    ),
)

#: 自检哨兵：只改注释。它必须 UNDETECTED —— 否则测试基线本来就是红的，
#: 整个矩阵的结论不成立。
SANITY_MUTATION = (
    "S0",
    ADAPTER,
    "POSITION_EVIDENCE_VERSION = \"position-evidence-v2\"\n",
    "POSITION_EVIDENCE_VERSION = \"position-evidence-v2\"  # sanity\n",
    "harness sanity check (comment only, must survive)",
)

#: 显式登记的等价变异：必须给出可执行证明（见 ``verify_equivalent``）。
EQUIVALENT_MUTATIONS = {
    "M-T1-8": (
        "本实现的 lot 集来自 `paper_position_lots` 的**开放行**（remaining_qty>0），"
        "每行带自己的 `acquisition_session`，可卖性逐 lot 计算后求和；"
        "不存在把多 lot 归约成单一 entry_session 的代码路径，因此"
        "「压成最早 entry date」这个缺陷在本实现里不可达。"
        "为让该结论可执行，`verify_equivalent` 会构造一批多 lot 夹具并断言："
        "每条 lot 都有独立结论、且被锁份额没有被吞掉。"
        "对应的普通变异体（M-T1-8 本体）把 kept 过滤成单一最早 session，"
        "契约测试必须把它抓住。"
    ),
}


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


def assert_no_leftover_mutants() -> int:
    """拒绝在被中断的变异体上继续跑。

    本矩阵用 ``try/finally`` 逐字节还原，但 ``SIGKILL``（超时、Ctrl-C 之后的强杀）
    会让 ``finally`` 不执行，把 ``# MUTANT`` 留在盘上。此时"基线全绿"的假设不成立，
    后续所有结论都会失真 —— 因此宁可拒绝运行，也不要在污染源上出报告。
    """
    offenders = []
    for relative_path in sorted({entry[1] for entry in MUTATIONS}):
        target = ROOT / relative_path
        if not target.exists():  # pragma: no cover - 清单防御
            continue
        text = target.read_text(encoding="utf-8", errors="replace")
        if "MUTANT " in text:
            offenders.append(relative_path)
    if offenders:
        print("检测到上一次运行遗留的变异体（SIGKILL 会跳过 finally 还原）：")
        for relative_path in offenders:
            print(f"  - {relative_path}")
        print("请先 `git checkout -- <file>` 还原，再重跑本矩阵。")
        return 1
    return 0


def _lock_path() -> Path:
    return ROOT / "backend" / ".mutation_matrix.lock"


def acquire_run_lock() -> int:
    """防止**并发**跑测试读到变异中的文件。

    本矩阵会在若干秒内把生产文件改成变异体。若与此同时有人在别处跑
    ``unittest discover``，那些测试会读到变异体并报出一批莫名其妙的失败 ——
    看起来像"新改动破坏了既有防线"，实际只是时序冲突。这个锁让矩阵自己
    声明"我正在改文件"，并且拒绝在锁已被占用时重复进入。
    """
    path = _lock_path()
    if path.exists():
        try:
            holder = path.read_text(encoding="utf-8").strip()
        except OSError:  # pragma: no cover - 竞态
            holder = "?"
        print(f"检测到正在运行的变异矩阵（lock={path}，holder={holder}）。")
        print("请等它跑完再重跑；并发跑测试会读到变异中的文件。")
        return 1
    path.write_text(f"pid={os.getpid()}\n", encoding="utf-8")
    return 0


def release_run_lock() -> None:
    try:
        _lock_path().unlink()
    except OSError:  # pragma: no cover - 已被清理
        pass


def _env() -> dict:
    return {**os.environ, "PYTHONPATH": "backend", "PYTHONDONTWRITEBYTECODE": "1"}


def run_contract_tests() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "unittest", "-q", *TEST_MODULES],
        cwd=str(ROOT), env=_env(), capture_output=True, text=True,
    )


def _import_check() -> bool:
    """变异体必须可导入 —— SyntaxError 假杀不算 CAUGHT。"""
    run = subprocess.run(
        [sys.executable, "-c",
         "import tradability_position_evidence, tradability_position_shadow"],
        cwd=str(ROOT), env=_env(), capture_output=True, text=True,
    )
    if run.returncode != 0:
        print(run.stdout)
        print(run.stderr)
    return run.returncode == 0


def baseline_is_green() -> bool:
    for path in (ADAPTER, SHADOW):
        clear_bytecode(path)
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
    """只读审计：每条变异的锚点在**当前盘上源码**里必须恰好出现一次。"""
    print("=== anchor audit (read-only) ===")
    bad = 0
    for entry in (SANITY_MUTATION, *MUTATIONS):
        name, relative_path = entry[0], entry[1]
        before, after = entry[2], entry[3]
        data = (ROOT / relative_path).read_bytes()
        count = data.count(before.encode("utf-8"))
        status = "ok" if count == 1 else f"BAD (count={count})"
        if count != 1:
            bad += 1
        print(f"{name}: {status}")
        if before == after:
            print(f"{name}: INERT (before == after)")
            bad += 1
    if not shutil.which("git"):
        print("note: git not found; relying on byte-for-byte restore only")
    print(f"=== audit result: {'PASS' if bad == 0 else f'{bad} problem(s)'} ===")
    return 1 if bad else 0


def verify_equivalent() -> int:
    """对 :data:`EQUIVALENT_MUTATIONS` 里的条目给出**可执行**证明。

    当前只有 ``M-T1-8``：证明本实现的 lot 集逐个保留独立身份与结论，
    不存在"归约成单一 entry_session"的代码路径。
    """
    import sqlite3
    import tempfile

    sys.path.insert(0, str(ROOT / "backend"))
    import selection_tradability as ST
    import tradability_position_evidence as PE

    print("=== equivalent mutation verification ===")
    if not EQUIVALENT_MUTATIONS:
        print("no equivalent mutations registered")
        return 0

    ddl = """
    CREATE TABLE paper_orders (id INTEGER PRIMARY KEY, account_id TEXT, side TEXT,
      code TEXT, name TEXT, status TEXT, created_at TEXT, executed_at TEXT,
      execution_status TEXT, execution_verified INTEGER, execution_evidence_source TEXT);
    CREATE TABLE paper_fills (id INTEGER PRIMARY KEY, order_id INTEGER, account_id TEXT,
      side TEXT, code TEXT, qty INTEGER, price REAL, amount REAL, fees REAL,
      fill_date TEXT, quote_at TEXT, assumption TEXT);
    CREATE TABLE paper_position_lots (id INTEGER PRIMARY KEY, cycle_id INTEGER,
      account_id TEXT, code TEXT, name TEXT, industry TEXT, qty INTEGER,
      remaining_qty INTEGER, cost REAL, acquired_at TEXT, available_date TEXT,
      asset_type TEXT, source_order_id INTEGER, cost_fee_included INTEGER,
      is_t_base INTEGER);
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(ddl)
    # 五个不同 acquisition session 的 lot，全部开放。
    sessions = ("2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18")
    for index, session in enumerate(sessions, start=1):
        conn.execute(
            "INSERT INTO paper_orders(id,account_id,side,code,name,status,created_at,"
            "executed_at,execution_status,execution_verified,execution_evidence_source) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (index, "A", "buy", "600001", "平安银行", "filled",
             f"{session} 09:30:00", f"{session} 10:00:00", "verified", 1, "ledger"),
        )
        conn.execute(
            "INSERT INTO paper_fills(id,order_id,account_id,side,code,qty,price,"
            "amount,fees,fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (index, index, "A", "buy", "600001", 100, 10.0, 1000.0, 0.0,
             session, None, "close"),
        )
        conn.execute(
            "INSERT INTO paper_position_lots(id,cycle_id,account_id,code,name,industry,"
            "qty,remaining_qty,cost,acquired_at,available_date,asset_type,"
            "source_order_id,cost_fee_included,is_t_base) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (index, 1, "A", "600001", "平安银行", None, 100, 100, 10.0,
             f"{session} 10:00:00", "2026-09-19", "stock_t1", index, 1, 1),
        )

    adapter = PE.PositionEvidenceAdapter(
        conn, evidence_provider=lambda code, session: ST.MarketEvidence(
            session=session, available_at=ST.session_close_at(session),
            price=10.6, reference_price=10.5, volume=1e6, halted=False,
            name="平安银行", risk_flag=None,
        )
    )
    context = adapter.context_for("600001", cycle_id=1, account_id="A",
                                 decision_session="2026-09-17",
                                 requested_sell_quantity=None)
    problems = []
    if len(context.lots) != len(sessions):
        problems.append(f"lot 数被归约：{len(context.lots)} != {len(sessions)}")
    if len(context.acquisition_sessions) != len(sessions):
        problems.append(
            f"acquisition session 被归约：{len(context.acquisition_sessions)} "
            f"!= {len(sessions)}"
        )
    # 09-17 当天：09-16 及更早可卖（3×100），09-17 锁，09-18（未来）进 unknown。
    if context.sellable_quantity != 300:
        problems.append(f"可卖份额错误：{context.sellable_quantity} != 300")
    if context.held_quantity != 500:
        problems.append(f"持仓份额错误：{context.held_quantity} != 500")
    if context.t1_locked_quantity != 100:
        problems.append(f"锁定份额错误：{context.t1_locked_quantity} != 100")
    if context.unknown_quantity != 100:
        problems.append(f"未知份额错误：{context.unknown_quantity} != 100")
    print(f"M-T1-8 proof: lots={len(context.lots)} "
          f"sessions={list(context.acquisition_sessions)} "
          f"held={context.held_quantity} sellable={context.sellable_quantity} "
          f"locked={context.t1_locked_quantity} unknown={context.unknown_quantity}")
    if problems:
        for problem in problems:
            print(f"M-T1-8 proof FAILED: {problem}")
        return 1
    print("M-T1-8 proof: PASS（多 lot 各自保留独立身份与结论，无归约路径）")
    return 0


#: 每条变异**指名**必须因它而失败的那个测试（§30 non-vacuity 的载体）。
#: 只断言"有测试失败"是不够的 —— 那可能是别的测试顺带崩了；必须由指名的
#: 那条测试**在未变异时是绿的、变异后是红的**，缺陷才算被真正定位。
DESIGNATED_NON_VACUITY = {
    "M-T1-1": (
        "test_tradability_position_shadow"
        ".NormalStockSameDaySellIsT1Blocked"
        ".test_the_lot_reports_the_authoritative_reason",
        "test_tradability_position_shadow"
        ".PartialAndMixedLotsRespectSellableQuantity"
        ".test_quantity_split_is_exact",
    ),
    "M-T1-2": (
        "test_tradability_position_shadow"
        ".ActualFillSessionOverridesIntendedSession"
        ".test_acquisition_session_is_the_fill_date",
        "test_tradability_position_shadow"
        ".ActualFillSessionOverridesIntendedSession"
        ".test_it_unlocks_one_session_after_the_actual_fill",
    ),
    "M-T1-3": (
        "test_tradability_position_shadow"
        ".PositionTaxonomyKeepsNotComparableOutOfDenominators"
        ".test_a_held_but_unprovable_position_is_not_comparable",
        "test_tradability_position_shadow"
        ".PositionTaxonomyKeepsNotComparableOutOfDenominators"
        ".test_partial_evidence_is_not_comparable_and_keeps_the_split_visible",
    ),
    "M-T1-4": (
        "test_tradability_position_shadow"
        ".AuthorityCalendarIsRespected"
        ".test_statutory_holiday_is_not_a_sellable_session",
        "test_tradability_position_shadow"
        ".AuthorityCalendarIsRespected"
        ".test_the_friday_itself_and_the_weekend_are_both_blocked",
        "test_tradability_position_shadow"
        ".T0EtfIsNotBlockedByT1"
        ".test_same_day_sell_is_not_blocked",
        "test_tradability_position_shadow"
        ".ActualFillSessionOverridesIntendedSession"
        ".test_it_unlocks_one_session_after_the_actual_fill",
    ),
    "M-T1-5": (
        "test_tradability_position_shadow"
        ".T0EtfIsNotBlockedByT1"
        ".test_same_day_sell_is_not_blocked",
    ),
    "M-T1-6": (
        "test_tradability_position_shadow"
        ".NormalStockSameDaySellIsT1Blocked"
        ".test_same_day_sell_is_blocked_by_the_t1_authority",
    ),
    "M-T1-7": (
        "test_tradability_position_shadow"
        ".FutureEvidenceCannotEnterAnEarlierSnapshot"
        ".test_an_as_of_before_the_record_excludes_the_lot",
    ),
    "M-T1-8": (
        "test_tradability_position_shadow"
        ".PartialAndMixedLotsRespectSellableQuantity"
        ".test_quantity_split_is_exact",
        "test_tradability_position_shadow"
        ".PartialAndMixedLotsRespectSellableQuantity"
        ".test_mixed_sessions_do_not_collapse_into_one_entry_session",
    ),
    "M-T1-9": (
        "test_tradability_position_shadow"
        ".PositionTaxonomyKeepsNotComparableOutOfDenominators"
        ".test_a_held_but_unprovable_position_is_not_comparable",
    ),
    "M-T1-10": (
        "test_tradability_position_shadow"
        ".BuyIsUnaffectedByThePositionLayer"
        ".test_buy_status_equals_the_market_level_status",
        "test_tradability_position_shadow"
        ".MarketBlockRemainsAMarketBlock"
        ".test_position_pass_does_not_overwrite_a_market_block",
    ),
    "M-HQ1": (
        "test_tradability_position_shadow"
        ".HistoricalQuantityMustBeReplayedNotBorrowed"
        ".test_HIST_Q4_current_remaining_differs_from_decision_time_quantity",
    ),
    "M-HQ2": (
        "test_tradability_position_shadow"
        ".HistoricalQuantityMustBeReplayedNotBorrowed"
        ".test_HIST_Q2_fully_consumed_lot_is_not_dropped_from_the_snapshot",
    ),
    "M-HQ3": (
        "test_tradability_position_shadow"
        ".PositionTaxonomyKeepsNotComparableOutOfDenominators"
        ".test_partial_evidence_is_not_comparable_and_keeps_the_split_visible",
    ),
    "M-SCOPE1": (
        "test_tradability_position_shadow"
        ".ScopeIsolationAcrossCyclesAndAccounts"
        ".test_other_cycle_lots_are_invisible_to_this_comparison",
    ),
    "M-SCOPE2": (
        "test_tradability_position_shadow"
        ".ScopeIsolationAcrossCyclesAndAccounts"
        ".test_same_code_across_accounts_is_never_pooled",
    ),
    "M-PIT1": (
        "test_tradability_position_shadow"
        ".ExactDecisionAtIsConsumedAndInvalidAsOfFailsClosed"
        ".test_position_identity_keeps_the_exact_decision_at",
    ),
    "M-PIT2": (
        "test_tradability_position_shadow"
        ".ExactDecisionAtIsConsumedAndInvalidAsOfFailsClosed"
        ".test_explicit_invalid_validation_as_of_never_widens_to_unlimited",
    ),
    "M-ID1": (
        "test_tradability_position_shadow"
        ".IdentityIntegrityIsVerified"
        ".test_cross_account_order_cannot_prove_this_lot",
    ),
    "M-ID2": (
        "test_tradability_position_shadow"
        ".IdentityIntegrityIsVerified"
        ".test_cross_code_fill_cannot_prove_this_lot",
    ),
    "M-QTY1": (
        "test_tradability_position_shadow"
        ".RequestedSellQuantityMustBeStrictlyPositive"
        ".test_zero_negative_and_invalid_are_all_rejected",
    ),
    "M-T1-11": (
        "test_tradability_position_shadow"
        ".SameSessionSellMustUseRealExecutionTime"
        ".test_HIST_T1_sell_before_decision_at_is_already_consumed",
    ),
    "M-T1-12": (
        "test_tradability_position_shadow"
        ".SellReplayMustStayInsideTheCycle"
        ".test_HIST_C1_sell_outside_the_cycle_window_does_not_consume",
    ),
    "M-T1-13": (
        "test_tradability_position_shadow"
        ".SellReplayMustStayInsideTheCycle"
        ".test_HIST_C3_oversell_is_immediately_unprovable",
    ),
    "M-T1-14": (
        "test_tradability_position_shadow"
        ".SameSessionSellMustUseRealExecutionTime"
        ".test_HIST_T1_sell_before_decision_at_is_already_consumed",
    ),
    "M-T1-15": (
        "test_tradability_position_shadow"
        ".SameSessionSellMustUseRealExecutionTime"
        ".test_HIST_T3_executed_at_absent_falls_back_to_close_and_fails_closed",
    ),
    "M-T1-16": (
        "test_tradability_position_shadow"
        ".ShadowIdentityMustIncludeAccountAndCycle"
        ".test_identity_separates_two_accounts_on_the_same_code_and_session",
    ),
}


def _run_specific(test_ids) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "unittest", "-v", *test_ids],
        cwd=str(ROOT), env=_env(), capture_output=True, text=True,
    )


def non_vacuity() -> int:
    """§30：revert-then-run —— 指名的测试必须**先绿后红**。

    对每条变异：先在**未变异**源码上跑它指名的测试（必须全绿），再变异后跑
    同样的测试（必须全红）。两步都成立才说明该测试真的在守护这个缺陷，
    而不是碰巧跟着别的失败一起变红。
    """
    print("=== non-vacuity (revert-then-run) ===")
    failures = []
    for name, designated in DESIGNATED_NON_VACUITY.items():
        entry = next((item for item in MUTATIONS if item[0] == name), None)
        if entry is None:
            failures.append(f"{name}: 变异未登记")
            continue

        # 第一步：未变异（revert 状态）下指名的测试必须全绿。
        baseline = _run_specific(designated)
        if baseline.returncode != 0:
            failures.append(f"{name}: 指名测试在未变异时就是红的（非空洞前提不成立）")
            print(f"{name}: baseline RED (unexpected)")
            print(baseline.stdout[-1500:])
            print(baseline.stderr[-1500:])
            continue

        # 第二步：变异后同一条测试必须变红。
        _, relative_path, before, after, _description = entry
        target = ROOT / relative_path
        original = target.read_bytes()
        original_sha = sha256(original)
        mutated = replace_once(original, before, after)
        try:
            clear_bytecode(relative_path)
            target.write_bytes(mutated)
            if not _import_check():
                failures.append(f"{name}: 变异体不可导入（IMPORT-FAILED 不算 kill）")
                print(f"{name}: IMPORT-FAILED")
                continue
            result = _run_specific(designated)
            if result.returncode == 0:
                failures.append(f"{name}: 指名测试在变异后仍然全绿（空洞）")
                print(f"{name}: mutated GREEN (vacuous!)")
            else:
                print(f"{name}: baseline GREEN -> mutated RED  (ok)")
        finally:
            clear_bytecode(relative_path)
            target.write_bytes(original)
            restored = target.read_bytes()
            if restored != original or sha256(restored) != original_sha:
                raise RuntimeError(
                    f"{name} restore verification failed; refusing to continue"
                )

    if failures:
        print("\nnon-vacuity failures:")
        for item in failures:
            print(f"  {item}")
    print(f"non-vacuity: {'PASS' if not failures else 'FAIL'}")
    return 1 if failures else 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    leftover = assert_no_leftover_mutants()
    if leftover:
        return leftover
    if "--audit" in argv:
        return audit_anchors()
    if "--non-vacuity" in argv:
        locked = acquire_run_lock()
        if locked:
            return locked
        try:
            if not baseline_is_green():
                print("baseline contract tests are not green; refusing to run non-vacuity")
                return 1
            return non_vacuity()
        finally:
            release_run_lock()
    print(f"repo root: {ROOT}")
    print("targets: " + ", ".join(sorted({entry[1] for entry in MUTATIONS})))
    locked = acquire_run_lock()
    if locked:
        return locked
    try:
        return _run_matrix()
    finally:
        release_run_lock()


def _run_matrix() -> int:
    if not baseline_is_green():
        print("baseline contract tests are not green; refusing to run the matrix")
        return 1

    results = []
    sanity = apply_and_run(SANITY_MUTATION)
    print(f"S0 sanity: {sanity} (expected UNDETECTED)")
    for entry in MUTATIONS:
        outcome = apply_and_run(entry)
        print(f"{entry[0]}: {outcome}  ({entry[4]})")
        results.append((entry[0], outcome))

    equivalent = verify_equivalent()

    print("\n=== mutation matrix summary ===")
    for name, outcome in results:
        print(f"{name}: {outcome}")
    caught = [name for name, outcome in results if outcome == "CAUGHT"]
    survived = [name for name, outcome in results if outcome != "CAUGHT"]
    print(f"caught: {len(caught)}/{len(results)}")
    print(f"survived: {survived or 'none'}")
    for name in survived:
        if name in EQUIVALENT_MUTATIONS:
            print(f"  {name} 登记为等价变异（见 verify_equivalent 的证明）")

    complete = (
        len(results) == len(MUTATIONS)
        and not survived
        and sanity == "UNDETECTED"
        and equivalent == 0
    )
    print("mutation matrix: " + ("PASS" if complete else "FAIL"))
    return 0 if complete else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
