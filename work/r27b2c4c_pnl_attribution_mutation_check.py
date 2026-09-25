# -*- coding: utf-8 -*-
"""R27-B2C-4C mutation matrix —— M-PNL-01 .. M-PNL-14。

只覆盖本轮 ``pnl_attribution`` canonical typed runtime 的**新的高风险 invariant**。每条
mutation 都必须让**唯一指定**的永久回归变 RED，anchor 恰好命中一次；baseline 是 matrix 的
**强制前提**（无条件先于任何 mutation 运行，没有开关），``SyntaxError`` / ``ImportError`` /
``NameError`` / collection failure 一律计为 FAKE（接线错误是假杀，不能算 detected）。

本轮的核心不变量分五组：

* **业务日只能由调用边界声明**（M-PNL-01 / 02）：把 asof 换回 ``max(paper_nav.nav_date)``
  推断、或让 unknown ``business_day`` 用 legacy 时间戳兜底 —— 都是让**被解释的数据**决定
  **解释的口径**。
* **事实只能来自 owner typed fact**（M-PNL-03 / 04 / 05）：unknown 费用被补成 0、已实现
  盈亏绕过 portfolio owner 直接 ``SUM(paper_orders.realized_pnl)``、成本摘要改读
  compatibility-only 的 ``paper_positions`` —— 三条都是用别的东西冒充 owner 已证明的事实。
* **市场腿拿不到 / 对不上就是不可用**（M-PNL-06 / 07 / 08 / 09）：legacy ``quote_status``
  冒充市场核验、as-of 对不上也照用缓存快照、拿不到行情就回落到 legacy 当前报价或成本基准 ——
  四条都把"证明不了"发布成"就是这个数"。
* **核验语义不得被压平或改写**（M-PNL-10 / 13）：用 ``verification == "verified"`` 冒充
  双源核验、让 payload 自述的核验维度覆盖 owner 派生的核验结论。
* **边界必须是结构化的**（M-PNL-11 / 12 / 14）：typed 路径里长出 legacy SQL 兜底、account /
  cycle 不匹配也被接受、组合入口接受调用方塞进来的裸 ``valuations`` Mapping。

沿用 R27-B2C-1 ~ B2C-4B 的逐次唯一 ``PYTHONPYCACHEPREFIX``，否则 baseline 与 mutant 会共享
字节码缓存，整张矩阵静默失效。**必须串行运行**：每条 mutation 就地改写 production source，
跑完按启动快照做 byte-identical 还原并校验 sha256，并检查文件里不再残留 mutant 记号。

baseline 规则：selected mutations 确定后，先对**全部去重后的永久 regression target**（以及
:data:`BASELINE_ONLY_TARGETS`）依次运行 baseline；任一非 GREEN 即输出 ``BASELINE-RED`` 并
立即失败，**不进入** mutation 阶段。若不强制，一个在干净源码上本来就红的目标会让它的所有
mutation 都被记成 CAUGHT —— 那是假证据，违反 harness 自身的证据链要求。

超时是**独立的分类**，既不是 CAUGHT 也不是 FAKE：mutation 运行抛
``subprocess.TimeoutExpired`` 记 ``TIMEOUT``；baseline 抛则记 ``BASELINE-TIMEOUT``，
两者都让 matrix FAIL。把超时折叠进"被杀死"，等于把一个从未作出判定的运行发布成有效证据。

CLI 的选择语义**绝不能静默变化**：受支持的形式只有 ``--only <ids>`` 与 ``--only=<ids>``。
除此之外的任何 argv —— ``--only*`` 的拼写错误、空 id 列表、重复 selector，以及
``--onl M-PNL-01`` / ``--dry-run`` / 裸位置参数这类完全不认识的 token —— 都是受控
ERROR + exit 2，而不是"没有 selector 所以跑全量 matrix"。操作者请求 targeted mutation 时，
覆盖范围绝不能因为 CLI 拼写问题被悄悄放大。

本文件自身的硬不变量**不使用 Python ``assert``**：``python -O`` 会剥除 assert，而证据链的守卫
不能因为一个优化开关消失。所有 runtime evidence 断言走 :func:`_require`（显式 ``RuntimeError``），
并由 :func:`self_test_optimization` 在 ``python -O`` 子进程里证明守卫没有被 optimization 消掉。

**目标映射的两处刻意偏离**（都对照测试源码 + 实测跑过，不是猜测）：

* brief 给 M-PNL-13 指定的 PNL-25 只**直接**构造 ``ARC.InformationEvent`` —— ``kind`` /
  ``verification`` / ``verification_method`` 全部派生自 ``evidence_ref``，它从不经过本
  matrix 唯一能改写的 ``deepseek_research`` 组合层。单文件变异在**结构上**无法让它变红
  （实测：brief 原文形状的 payload 覆盖变异在 PNL-25 上 rc=0 → SURVIVED）。真正观察
  "组合发布的事件把 payload 自述核验当成核验结论"的守卫是 PNL-24：它把组合发布的事件元组
  与 owner 派生事件**逐一比对**，因此 M-PNL-13 指向 PNL-24。
* M-PNL-13 读的是 payload 实际携带的 ``verification_method``（不是 brief 字面上的
  ``verification`` 键）：组合层自己构造的 payload 里没有 ``verification`` 键，只读它等于
  **空变异**（实测 rc=0，PNL-24 与 PNL-25 都 SURVIVED）。只有读 payload 真有的那个维度，
  "payload 自述覆盖 owner 派生核验"才是可观察的（PNL-24 变 RED）。
* PNL-25 以及其余 brief 点名的永久回归（PNL-04 / 08 / 13 / 30）同样在 baseline 阶段无条件
  运行，见 :data:`BASELINE_ONLY_TARGETS` —— 它们必须是 GREEN，只是本文件不声称能打红它们。

用法：
    python work/r27b2c4c_pnl_attribution_mutation_check.py
    python work/r27b2c4c_pnl_attribution_mutation_check.py --only M-PNL-01
    python work/r27b2c4c_pnl_attribution_mutation_check.py --only=M-PNL-01,M-PNL-03
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")

#: 本轮的 production file under test —— 每条 mutation 都就地改写它。
DS_FILE = "backend/deepseek_research.py"

SUITE = "test_pnl_attribution_typed_runtime"
CLASS = f"{SUITE}.PnlAttributionTypedRuntimeTests"

T_PNL_01 = f"{CLASS}.test_PNL_01_asof_never_derived_from_paper_nav"
T_PNL_04 = f"{CLASS}.test_PNL_04_filled_trade_payload_comes_from_typed_fact"
T_PNL_05 = f"{CLASS}.test_PNL_05_unknown_business_day_never_falls_back_to_legacy_timestamps"
T_PNL_06 = f"{CLASS}.test_PNL_06_account_or_cycle_mismatch_is_excluded_fail_closed"
T_PNL_07 = f"{CLASS}.test_PNL_07_unknown_fees_are_never_zero_filled"
T_PNL_08 = f"{CLASS}.test_PNL_08_known_zero_fees_are_a_legal_verified_zero"
T_PNL_09 = f"{CLASS}.test_PNL_09_realized_pnl_comes_from_portfolio_fact"
T_PNL_12 = f"{CLASS}.test_PNL_12_canonical_path_never_reads_paper_positions"
T_PNL_13 = f"{CLASS}.test_PNL_13_paper_nav_is_not_a_market_authority"
T_PNL_14 = f"{CLASS}.test_PNL_14_paper_nav_quote_status_never_enters_market_verification"
T_PNL_17 = f"{CLASS}.test_PNL_17_market_asof_mismatch_makes_valuation_unavailable"
T_PNL_18 = f"{CLASS}.test_PNL_18_missing_market_leaves_nav_unavailable_not_cost_or_current"
T_PNL_19 = f"{CLASS}.test_PNL_19_coverage_integrity_is_not_cross_source_verified"
T_PNL_22 = f"{CLASS}.test_PNL_22_caller_cannot_inject_a_bare_valuations_mapping"
T_PNL_24 = f"{CLASS}.test_PNL_24_information_event_kind_derives_from_evidence_ref"
T_PNL_25 = f"{CLASS}.test_PNL_25_payload_cannot_override_verification"
T_PNL_28 = f"{CLASS}.test_PNL_28_no_legacy_sql_fallback_exists_in_the_pnl_path"
T_PNL_30 = f"{CLASS}.test_PNL_30_presentation_metadata_is_not_typed_evidence"

#: brief 点名、但本 matrix **不**声称能打红的永久回归：它们必须在 baseline 里是 GREEN。
#: PNL-25 的理由见模块 docstring（组合层之外的事件契约，单文件变异不可达）。
BASELINE_ONLY_TARGETS = (T_PNL_04, T_PNL_08, T_PNL_13, T_PNL_25, T_PNL_30)

# --- mutation anchors（逐字节，必须恰好命中一次）--------------------------------

#: ``_compose_pnl_attribution`` 的返回头 —— M-PNL-01 的 anchor。
RETURN_HEAD = (
    "    return {\n"
    '        "scope": "paper_trading_only",\n'
    '        "purpose": "pnl_attribution",\n'
    '        "asof": attribution.asof_day,\n'
    '        "asof_source": "explicit_attribution_request",\n'
)
#: ``_execution_leg`` 里 ``_matches_target`` 的调用点 —— M-PNL-02 的 anchor。
#: （``_matches_target`` 自己拿不到 conn，而 legacy 时间戳住在 ``paper_orders`` 里，
#: 所以"unknown 业务日改用 legacy 时间戳兜底"的形状必然长在调用点。）
EXEC_MATCH_CALL = (
    "        if not _matches_target(projection, account_id, cycle_id, attribution.asof_day):\n"
    "            continue\n"
)
#: ``_matches_target`` 的 account / cycle 归属判定。
TARGET_IDENTITY_GUARD = (
    "    if str(owner_account.value) != str(account_id):\n"
    "        return False\n"
    "    try:\n"
    "        if int(owner_cycle.value) != int(cycle_id):\n"
    "            return False\n"
    "    except (TypeError, ValueError):\n"
    "        return False\n"
    "    return str(owner_day.value) == str(asof_day)\n"
)
#: ``_execution_leg`` 的 unknown 费用分支。
FEES_UNKNOWN_BLOCK = (
    "        if projection.fees.is_known:\n"
    "            fee_total += float(projection.fees.value)\n"
    "        else:\n"
    '            # unknown ≠ 0：把没证明的费用补零，就是把"不知道"发布成"没有费用"。\n'
    "            fees_complete = False\n"
)
#: ``_compose_pnl_attribution`` 的 realized pnl 读取（走 portfolio owner typed fact）。
REALIZED_BLOCK = (
    "        realized = facts.get(PPRM.PORTFOLIO_FACT_REALIZED_PNL)\n"
    "        if isinstance(realized, (int, float)) and not isinstance(realized, bool):\n"
    "            realized_total += float(realized)\n"
    "        else:\n"
    "            realized_complete = False\n"
)
#: 持仓成本摘要的读取分支。
SUMMARY_BLOCK = (
    "        summary = facts.get(PPRM.PORTFOLIO_FACT_POSITION_COST_SUMMARY)\n"
    "        if isinstance(summary, dict):\n"
)
#: market provenance 的组合点 —— M-PNL-06 的 anchor。
MARKET_PROVENANCE_LINE = (
    "    market_provenance = _market_provenance(market_reading, market_reason)\n"
)
#: ``_market_leg`` 的可用性守卫 —— as-of / availability 对不上即整条腿不可用。
MARKET_AVAILABILITY_GUARD = (
    "    if reading.availability != MDC.AVAILABILITY_AVAILABLE or snapshot is None:\n"
    '        return None, None, str(reading.reason or reading.status or "market_unavailable"), reading\n'
)
#: NAV 组合里"拿不到行情就不发 NAV"的位置 —— M-PNL-08 / M-PNL-09 的 anchor。
NAV_VALUATIONS_BRANCH = (
    "        if valuations is not None:\n"
    "            view = PPRM.portfolio_for_context(\n"
    "                conn, context, account_id=account_id, valuations=valuations)\n"
)
#: NAV 块的初值（不可用形状）—— M-PNL-09 的 anchor。
NAV_BLOCK_HEAD = (
    '        nav_block = {"nav": None, "market_value": None, "unrealized_pnl": None,\n'
    '                     "availability": "unavailable", "reason": market_reason,\n'
)
#: 双源核验判据 —— 必须走 owner 谓词。
CROSS_SOURCE_LINE = (
    '        "cross_source_verified": bool(MDC.is_cross_source_verified(snapshot)),\n'
)
#: ``_pnl_evidence`` 的 canonical 组合体（只有 try / finally）。
PNL_EVIDENCE_BODY = (
    "    paper = _paper(paper_db_path)\n"
    "    try:\n"
    "        return _compose_pnl_attribution(paper, attribution)\n"
    "    finally:\n"
    "        paper.close()\n"
)
#: ``_compose_pnl_attribution`` 的签名 + docstring + market leg 调用 —— M-PNL-14 的 anchor。
COMPOSE_ENTRY = (
    "def _compose_pnl_attribution(conn, attribution):\n"
    '    """cross-owner research composition。\n'
    "\n"
    "    本层只做三件被允许的事：消费 owner typed facts、把它们组合成可归因的展示值、\n"
    "    把\"证明不了\"如实写成 unavailable + reason。它**不是**第四个事实 owner：\n"
    "    execution / portfolio / market 各自保留 identity（§31），也不签发新的\n"
    "    ``ResearchEvidenceRef``（§32）。\n"
    '    """\n'
    "    events = []\n"
    "    valuations, market_ref, market_reason, market_reading = _market_leg(attribution)\n"
)
#: canonical event 的发布字段（kind / verification 全部派生自 evidence_ref）。
EVENT_PROJECTION_BLOCK = (
    '             "kind": event.kind, "as_of": event.as_of, "source": event.source,\n'
    '             "verification": event.verification,\n'
    '             "verification_method": event.verification_method,\n'
)

MUTATIONS = [
    {
        "id": "M-PNL-01",
        # asof 改回 legacy paper_nav 推断。
        "file": DS_FILE,
        "old": RETURN_HEAD,
        "new": (
            "    paper_nav = [  # MUTANT —— 业务日改由 legacy paper_nav 推断\n"
            "        row[0] for row in conn.execute(\n"
            '            "SELECT nav_date FROM paper_nav"\n'
            "        ).fetchall()\n"
            "    ]\n"
            "    return {\n"
            '        "scope": "paper_trading_only",\n'
            '        "purpose": "pnl_attribution",\n'
            '        "asof": max(paper_nav) if paper_nav else attribution.asof_day,\n'
            '        "asof_source": "explicit_attribution_request",\n'
        ),
        "test": T_PNL_01,
        "desc": "asof 改由 max(paper_nav.nav_date) 推断（业务日不再由调用边界声明）",
    },
    {
        "id": "M-PNL-02",
        # unknown 业务日改用 legacy 时间戳兜底。
        "file": DS_FILE,
        "old": EXEC_MATCH_CALL,
        "new": (
            "        _target_match = _matches_target(\n"
            "            projection, account_id, cycle_id, attribution.asof_day)\n"
            "        if not _target_match and not projection.business_day.is_known:\n"
            "            # MUTANT —— unknown 业务日改用 legacy 时间戳兜底\n"
            '            _fallback_day = str(projection.observed_at.maybe() or "")[:10]\n'
            "            if not _fallback_day:\n"
            "                _legacy_row = conn.execute(\n"
            '                    "SELECT executed_at FROM paper_orders WHERE id=?",\n'
            "                    (projection.order_id,),\n"
            "                ).fetchone()\n"
            '                _fallback_day = str(_legacy_row[0] or "")[:10] if _legacy_row else ""\n'
            "            if _fallback_day == str(attribution.asof_day):\n"
            "                _backfill = {\n"
            "                    item.name: getattr(projection, item.name)\n"
            "                    for item in dataclasses.fields(EV.ExecutionFactProjection)\n"
            "                }\n"
            '                _backfill["business_day"] = EE.EvidenceField.known(\n'
            '                    "business_day", _fallback_day)\n'
            "                projection = EV._issue_fact_projection(**_backfill)\n"
            "                _target_match = True\n"
            "        if not _target_match:\n"
            "            continue\n"
        ),
        "test": T_PNL_05,
        "desc": "unknown business_day 改用 observed_at / executed_at 兜底",
    },
    {
        "id": "M-PNL-03",
        # unknown 费用被补成 0。
        "file": DS_FILE,
        "old": FEES_UNKNOWN_BLOCK,
        "new": (
            "        # MUTANT —— unknown 费用被补成 0（`or 0`）\n"
            "        fee_total += float(projection.fees.value or 0.0)\n"
        ),
        "test": T_PNL_07,
        "desc": "unknown 费用被当成 0（\"没证明\"被发布成\"没有费用\"）",
    },
    {
        "id": "M-PNL-04",
        # 已实现盈亏绕过 portfolio owner，直接 SUM paper_orders。
        "file": DS_FILE,
        "old": REALIZED_BLOCK,
        "new": (
            "        _realized_sum = conn.execute(  # MUTANT —— 绕过 portfolio owner 直接 SUM\n"
            '            "SELECT COALESCE(SUM(realized_pnl),0.0) FROM paper_orders"\n'
            '            " WHERE cycle_id=? AND account_id=?",\n'
            "            (cycle_id, account_id),\n"
            "        ).fetchone()\n"
            "        realized = float(_realized_sum[0] or 0.0)\n"
            "        realized_total += realized\n"
        ),
        "test": T_PNL_09,
        "desc": "realized_pnl 改为直接 SUM(paper_orders.realized_pnl)（owner 不统计的行也进来）",
    },
    {
        "id": "M-PNL-05",
        # 持仓成本摘要改读 compatibility-only 的 paper_positions。
        "file": DS_FILE,
        "old": SUMMARY_BLOCK,
        "new": (
            "        # MUTANT —— 成本摘要改读兼容投影 paper_positions\n"
            "        paper_positions = conn.execute(\n"
            '            "SELECT COUNT(*) AS position_count, COALESCE(SUM(qty*cost),0.0)"\n'
            '            " AS cost_value FROM paper_positions WHERE account_id=?",\n'
            "            (account_id,),\n"
            "        ).fetchone()\n"
            "        summary = {\n"
            '            "position_count": int(paper_positions[0]),\n'
            '            "cost_value": float(paper_positions[1]),\n'
            "        }\n"
            "        if isinstance(summary, dict):\n"
        ),
        "test": T_PNL_12,
        "desc": "position cost summary 改读 paper_positions（兼容投影冒充 owner 事实）",
    },
    {
        "id": "M-PNL-06",
        # legacy paper_nav.quote_status 冒充市场核验。
        "file": DS_FILE,
        "old": MARKET_PROVENANCE_LINE,
        "new": (
            "    market_provenance = _market_provenance(market_reading, market_reason)\n"
            '    if market_provenance["verification"] is None:  # MUTANT —— legacy quote_status '
            "冒充核验\n"
            '        _legacy_quotes = conn.execute("SELECT quote_status FROM paper_nav").fetchall()\n'
            '        market_provenance["verification"] = (\n'
            "            str(_legacy_quotes[0][0]) if _legacy_quotes else None\n"
            "        )\n"
        ),
        "test": T_PNL_14,
        "desc": "market leg 拿不到读数时用 legacy paper_nav.quote_status 冒充市场核验",
    },
    {
        "id": "M-PNL-07",
        # as-of 对不上也照用缓存快照。
        "file": DS_FILE,
        "old": MARKET_AVAILABILITY_GUARD,
        "new": (
            "    if reading.availability != MDC.AVAILABILITY_AVAILABLE or snapshot is None:\n"
            "        # MUTANT —— as-of 对不上也照用缓存快照去估値\n"
            "        _cached = MDS._load_cached_snapshot(MDS.KIND_FULL_MARKET_SNAPSHOT)\n"
            "        _mutant_valuations = {}\n"
            "        for _row in (() if _cached is None else _cached.rows):\n"
            '            if not hasattr(_row, "get"):\n'
            "                continue\n"
            '            _code = str(_row.get("code") or "").strip()\n'
            '            _price = _row.get("price")\n'
            "            if not _code or isinstance(_price, bool):\n"
            "                continue\n"
            "            if isinstance(_price, (int, float)) and _price > 0:\n"
            "                _mutant_valuations[_code] = float(_price)\n"
            "        if _mutant_valuations:\n"
            "            return _mutant_valuations, None, None, reading\n"
            '        return None, None, str(reading.reason or reading.status or "market_unavailable"), reading\n'
        ),
        "test": T_PNL_17,
        "desc": "as-of mismatch（reading 明确不可用）时仍然把缓存快照的价格交给组合层去估值",
    },
    {
        "id": "M-PNL-08",
        # 拿不到行情就回落到 legacy 当前报价。
        "file": DS_FILE,
        "old": NAV_VALUATIONS_BRANCH,
        "new": (
            "        if valuations is None:  # MUTANT —— 回落到 legacy 当前报价\n"
            "            valuations = {}\n"
            "            for _quote in conn.execute(\n"
            '                "SELECT code, filled_price FROM paper_orders"\n'
            '                " WHERE filled_price IS NOT NULL AND code IS NOT NULL"\n'
            "            ).fetchall():\n"
            "                if _quote[1] is not None:\n"
            "                    valuations[str(_quote[0])] = float(_quote[1])\n"
            "            if not valuations:\n"
            "                valuations = None\n"
            "        if valuations is not None:\n"
            "            view = PPRM.portfolio_for_context(\n"
            "                conn, context, account_id=account_id, valuations=valuations)\n"
        ),
        "test": T_PNL_18,
        "desc": "市场事实缺失时回落到 legacy paper_orders.filled_price 当当前报价",
    },
    {
        "id": "M-PNL-09",
        # 拿不到行情就回落到成本基准。
        "file": DS_FILE,
        "old": NAV_BLOCK_HEAD,
        "new": (
            "        # MUTANT —— 没有市场事实时用成本基准冒充 NAV\n"
            "        _cost_fact = facts.get(PPRM.PORTFOLIO_FACT_POSITION_COST_SUMMARY)\n"
            "        _cost_value = (\n"
            '            _cost_fact.get("cost_value") if isinstance(_cost_fact, dict) else None\n'
            "        )\n"
            '        nav_block = {"nav": _cost_value, "market_value": _cost_value,\n'
            '                     "unrealized_pnl": 0.0,\n'
            '                     "availability": "available" if _cost_value is not None else "unavailable",\n'
            "                     \"reason\": None if _cost_value is not None else market_reason,\n"
        ),
        "test": T_PNL_18,
        "desc": "市场事实缺失时用 portfolio 成本基准冒充 NAV",
    },
    {
        "id": "M-PNL-10",
        # verification == "verified" 冒充双源核验。
        "file": DS_FILE,
        "old": CROSS_SOURCE_LINE,
        "new": (
            "        # MUTANT —— 用 verification == \"verified\" 冒充双源核验\n"
            '        "cross_source_verified": bool(\n'
            '            snapshot.verification == MDC.VERIFICATION_VERIFIED\n'
            "        ),\n"
        ),
        "test": T_PNL_19,
        "desc": "cross_source_verified 改为从 verification 猜（coverage_integrity 被当成双源）",
    },
    {
        "id": "M-PNL-11",
        # typed 路径里长出 legacy SQL 兜底。
        "file": DS_FILE,
        "old": PNL_EVIDENCE_BODY,
        "new": (
            "    paper = _paper(paper_db_path)\n"
            "    try:\n"
            "        return _compose_pnl_attribution(paper, attribution)\n"
            "    except Exception:  # MUTANT —— 组合读不到就退回 legacy SQL\n"
            "        return {\n"
            '            "asof": attribution.asof_day,\n'
            '            "legacy_nav": _rows(paper, "SELECT nav FROM paper_nav"),\n'
            '            "legacy_orders": _rows(\n'
            '                paper, "SELECT realized_pnl FROM paper_orders"\n'
            "            ),\n"
            "        }\n"
            "    finally:\n"
            "        paper.close()\n"
        ),
        "test": T_PNL_28,
        "desc": "canonical 路径里长出 except + legacy SQL 兜底",
    },
    {
        "id": "M-PNL-12",
        # account / cycle 不匹配也被接受。
        "file": DS_FILE,
        "old": TARGET_IDENTITY_GUARD,
        "new": (
            "    # MUTANT —— account / cycle 不匹配也被接受\n"
            "    return True\n"
        ),
        "test": T_PNL_06,
        "desc": "account / cycle 不匹配的委托被重绑定到本次归因目标上",
    },
    {
        "id": "M-PNL-13",
        # payload 自述的核验维度覆盖 owner 派生的核验结论。
        "file": DS_FILE,
        "old": EVENT_PROJECTION_BLOCK,
        "new": (
            '             "kind": event.kind, "as_of": event.as_of, "source": event.source,\n'
            "             # MUTANT —— 组合发布的事件优先读 payload 自述的核验维度\n"
            '             "verification": event.payload.get(\n'
            '                 "verification",\n'
            '                 event.payload.get("verification_method", event.verification),\n'
            "             ),\n"
            '             "verification_method": event.payload.get(\n'
            '                 "verification_method", event.verification_method),\n'
        ),
        "test": T_PNL_24,
        "desc": "组合事件把 payload 自述核验当成核验结论（覆盖 owner 派生的 verification）",
    },
    {
        "id": "M-PNL-14",
        # 组合入口接受调用方塞进来的裸 valuations Mapping。
        "file": DS_FILE,
        "old": COMPOSE_ENTRY,
        "new": (
            "def _compose_pnl_attribution(conn, attribution, valuations=None):\n"
            '    """cross-owner research composition。\n'
            "\n"
            "    本层只做三件被允许的事：消费 owner typed facts、把它们组合成可归因的展示值、\n"
            "    把\"证明不了\"如实写成 unavailable + reason。它**不是**第四个事实 owner：\n"
            "    execution / portfolio / market 各自保留 identity（§31），也不签发新的\n"
            "    ``ResearchEvidenceRef``（§32）。\n"
            '    """\n'
            "    events = []\n"
            "    _injected_valuations = valuations  # MUTANT —— 调用方可以塞裸 Mapping\n"
            "    valuations, market_ref, market_reason, market_reading = _market_leg(attribution)\n"
            "    if _injected_valuations is not None:\n"
            "        valuations = dict(_injected_valuations)\n"
        ),
        "test": T_PNL_22,
        "desc": "组合入口新增 valuations 形参（调用方塞裸 Mapping 即可冒充估值证据）",
    },
]


#: mutation 的终态分类。TIMEOUT 与 CAUGHT 语义不同：前者从未作出判定。
VERDICT_CAUGHT = "CAUGHT"
VERDICT_SURVIVED = "SURVIVED"
VERDICT_FAKE = "FAKE"
VERDICT_TIMEOUT = "TIMEOUT"

#: baseline 超时 —— 与 BASELINE-RED 语义不同（测试根本没跑完，而非在干净源码上失败）。
BASELINE_RED = "BASELINE-RED"
BASELINE_TIMEOUT = "BASELINE-TIMEOUT"

#: 变异体落盘记号。还原之后文件里**不得**再有它（``assert_no_leftover``）。
MUTANT_MARKER = "MUTANT"


def sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _require(condition: bool, message: str) -> None:
    """本 harness 的 runtime evidence 断言 —— 显式失败，绝不用 ``assert``。

    ``python -O`` 会把 ``assert`` 整条剥掉，于是"证明自己 PASS"的语句静默消失，
    一个应该硬失败的证据链缺口会变成通过。所有 correctness / non-vacuity /
    restore / classification 断言都走这里。
    """
    if not condition:
        raise RuntimeError(message)


def _adapt_eol(text: str, original: bytes) -> bytes:
    if original.count(b"\r\n") > 0:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("utf-8")


PYCACHE_ROOT = tempfile.mkdtemp(prefix="r27b2c4c_pnl_attribution_pycache_")
_SEQ = [0]

#: 变异体必须因**业务断言**失败。接线错误是假杀，不能算 detected。
BROKEN_RE = re.compile(
    r"(SyntaxError|IndentationError|TabError"
    r"|ImportError|ModuleNotFoundError"
    r"|NameError|UnboundLocalError"
    r"|_FailedTest"
    r"|TypeError: .*takes .* positional argument"
    r"|is not defined|local variable .* referenced before assignment)",
    re.MULTILINE,
)


def _next_seq() -> int:
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
    seen = [_next_seq() for _ in range(5)]
    _require(len(set(seen)) == len(seen), f"sequence not unique: {seen}")
    _require(seen == sorted(seen), f"sequence not increasing: {seen}")
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
    _require(len(dirs) == 3, f"expected 3 invocations, got {dirs}")
    _require(len(set(dirs)) == 3, f"invocations share a cache dir: {dirs}")


#: ``-O`` 探针：在优化解释器里复算 harness 的硬守卫，输出一行 JSON 报告。
_OPTIMIZATION_PROBE = '''\
"""在普通 / ``-O`` 解释器下复算 harness 的硬守卫。"""
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("_harness_under_probe", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

ANCHOR = "    return a + b\\n"


def outcome(call):
    try:
        call()
    except RuntimeError:
        return "RuntimeError"
    except AssertionError:
        return "AssertionError"
    return "NO-ERROR"


def apply_anchor(text):
    return module._apply(
        text, {"id": "PROBE", "file": "probe.py", "old": ANCHOR, "new": ""}
    )


print(json.dumps({
    "optimized": not __debug__,
    "implementation": sys.implementation.name,
    "results": [
        outcome(lambda: apply_anchor("def add(a, b):\\n    return a * b\\n")),
        outcome(lambda: apply_anchor("def add(a, b):\\n" + ANCHOR + ANCHOR)),
        outcome(lambda: apply_anchor("def add(a, b):\\n" + ANCHOR)),
        outcome(lambda: module._require(False, "probe: hard guard must survive -O")),
    ],
}))
'''


def _cli_probe(argv: list[str], timeout: int = 300) -> tuple[int, str]:
    """真实跑一次 CLI —— 只用于**参数解析阶段就退出**的用例，不触碰 production source。"""
    proc = subprocess.run(
        [sys.executable, os.path.abspath(__file__), *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=ROOT, timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _assert_cli_rejected(argv: list[str]) -> None:
    """真实 CLI 上，一个非法 argv 必须 exit 2 + 受控 ERROR，且不进入任何执行阶段。

    ``selected:`` 只在参数与选择都通过之后才打印，因此它的缺席直接证明这次调用
    没有走到 selection / baseline / mutation —— 也就不会改写 production source。
    """
    code, blob = _cli_probe(argv)
    _require(code == 2, f"{argv}: expected exit 2, got {code}: {blob[:200]}")
    _require("Traceback" not in blob, f"{argv}: must not raise a bare traceback: {blob[:200]}")
    _require("ERROR:" in blob, f"{argv}: expected a controlled ERROR: {blob[:200]}")
    _require("selected:" not in blob, f"{argv}: must not reach the mutation phase")


def self_test_semantics() -> None:
    """在临时目录里自证分类语义（stub 掉真实 runner，不触碰任何 production source）。

    覆盖：anchor 唯一性、BASELINE-RED / BASELINE-TIMEOUT 且不进入 mutation、CAUGHT、
    SURVIVED、FAKE（四类接线错误）、TIMEOUT、restore sha256 硬失败、byte-identical
    还原、mutant 记号残留检测、``--only`` 选择器与 argv 白名单的全部参数边界。
    """
    root = tempfile.mkdtemp(prefix="r27b2c4c_mutation_semantics_")
    rel = "semantics_target.py"
    path = os.path.join(root, rel)
    original = "def add(a, b):\n    return a + b\n"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(original)

    def result(code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr=err)

    def source_bytes() -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    mutation = {
        "id": "SELF-1", "file": rel,
        "old": "    return a + b\n", "new": "    return a - b  # MUTANT\n",
        "test": "test_semantics.Fake.test_add", "desc": "self-test semantic mutant",
    }
    extra_target = "test_semantics.Fake.test_extra"

    # 0) anchor 唯一性是证据链的硬不变量：0 次命中与多次命中都必须硬失败，
    #    绝不允许落到 replace(..., 1) 上（那会让"测试被杀"归因到一个没发生的改写）。
    for label, text in (
        ("count=0", "def add(a, b):\n    return a * b\n"),
        ("count=2", "def add(a, b):\n    return a + b\n    return a + b\n"),
    ):
        try:
            _apply(text, {"id": "SELF-ANCHOR", "file": rel,
                          "old": mutation["old"], "new": ""})
        except RuntimeError as exc:
            _require("anchor must be unique" in str(exc) and label in str(exc), exc)
        else:
            raise RuntimeError(f"{label}: non-unique anchor did not hard-fail")

    # 1) baseline RED → 整体失败，且 production source 一个字节都不被触碰。
    seen: list[str] = []

    def red_baseline(target: str, seq: int | None = None):
        seen.append(target)
        return result(1, "", "AssertionError: expected 2 got 3")

    _require(run_baselines([mutation], runner=red_baseline) == 1, "baseline RED must fail")
    _require(seen == [mutation["test"]], f"baseline must run exactly the deduped target: {seen}")
    _require(source_bytes() == original.encode("utf-8"),
             "baseline phase must not touch the source")

    # 2) baseline TIMEOUT → 同样整体失败、mutation 阶段不启动。
    #    它与 BASELINE-RED 语义不同：测试根本没跑完，不是"在干净源码上本来就是红的"。
    seen.clear()

    def timeout_baseline(target: str, seq: int | None = None):
        seen.append(target)
        raise subprocess.TimeoutExpired(cmd=target, timeout=900)

    _require(run_baselines([mutation], runner=timeout_baseline) == 1,
             "baseline TIMEOUT must fail the matrix")
    _require(seen == [mutation["test"]],
             f"baseline TIMEOUT must stop after the first target: {seen}")
    _require(source_bytes() == original.encode("utf-8"),
             "baseline TIMEOUT must not touch the source")

    # 2b) baseline-only 的永久回归目标（brief 点名但本 matrix 不声称能打红的那批）
    #     必须一起跑、且与 mutation target 去重 —— 否则"这些目标也验过"只是句话。
    seen.clear()

    def green_baseline(target: str, seq: int | None = None):
        seen.append(target)
        return result(0)

    _require(run_baselines([mutation], runner=green_baseline,
                           extra=(mutation["test"], extra_target)) == 0,
             "baseline with extras must pass")
    _require(seen == [mutation["test"], extra_target],
             f"baseline extras must be appended and deduped: {seen}")

    # 3/4/5) baseline GREEN 之后的分类：CAUGHT / SURVIVED / FAKE。
    def runner_for(code: int, out: str = "", err: str = ""):
        def _run(target: str, seq: int | None = None):
            return result(code, out, err)
        return _run

    _require(run_mutation(mutation, root=root, runner=runner_for(1)) == VERDICT_CAUGHT,
             "returncode 1 with a business assertion failure must be CAUGHT")
    _require(source_bytes() == original.encode("utf-8"), "bytes must be restored exactly")
    _require(run_mutation(mutation, root=root, runner=runner_for(0)) == VERDICT_SURVIVED,
             "returncode 0 must be SURVIVED")
    _require(source_bytes() == original.encode("utf-8"), "bytes must be restored exactly")
    for err in ("SyntaxError: invalid syntax", "ImportError: no module named x",
                "NameError: name 'x' is not defined", "_FailedTest: collection failure"):
        verdict = run_mutation(mutation, root=root, runner=runner_for(1, err=err))
        _require(verdict == VERDICT_FAKE, f"{err} must be FAKE, got {verdict}")

    # 6) 超时是独立分类：不能算 CAUGHT，也不能算 FAKE，且必须仍然完整还原源码。
    def timeout_runner(target: str, seq: int | None = None):
        raise subprocess.TimeoutExpired(cmd=target, timeout=900)

    _require(run_mutation(mutation, root=root, runner=timeout_runner) == VERDICT_TIMEOUT,
             "TimeoutExpired must classify as TIMEOUT, not CAUGHT/FAKE")
    _require(source_bytes() == original.encode("utf-8"),
             "TIMEOUT must still restore the source byte-identically")
    _require(MUTANT_MARKER not in source_bytes().decode("utf-8"),
             "TIMEOUT must not leave the mutant on disk")

    # 6b) mutant 记号残留必须被 hard fail（"还原了"与"还原成什么"是两件事）。
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(original + "# MUTANT\n")
    try:
        assert_no_leftover(path, mutation["id"])
    except RuntimeError as exc:
        _require("leftover mutant" in str(exc), exc)
    else:
        raise RuntimeError("leftover mutant did not hard-fail")
    with open(path, "wb") as handle:
        handle.write(original.encode("utf-8"))

    # 7) restore 不一致 → 硬失败（人为给一个错误的启动快照 sha）。
    try:
        _restore_and_verify(path, original.encode("utf-8"), "0" * 64, mutation["id"])
    except RuntimeError as exc:
        _require("restore sha256 mismatch" in str(exc), exc)
    else:
        raise RuntimeError("restore mismatch did not hard-fail")

    # 8) 非空性：BROKEN_RE 必须真的能区分接线错误与业务断言失败。
    _require(_is_fake_kill(result(1, err="SyntaxError: invalid syntax")),
             "BROKEN_RE failed to flag a wiring error")
    _require(not _is_fake_kill(result(1, err="AssertionError: 2 != 3")),
             "BROKEN_RE must not flag a business assertion failure")

    # 9) --only 的选择语义（helper 层）：只有"没有 selector / 合法 ids / 受控 ERROR"三态，
    #    绝不能把未知拼写解释成"没有 selector，所以跑全量 matrix"。
    _require(_parse_only([]) == (None, None), "no selector must mean the full matrix")
    for argv, ids in (
        (["--only", "M-PNL-01"], {"M-PNL-01"}),
        (["--only=M-PNL-01"], {"M-PNL-01"}),
        (["--only", "M-PNL-01,M-PNL-02"], {"M-PNL-01", "M-PNL-02"}),
        (["--only=M-PNL-01,M-PNL-02"], {"M-PNL-01", "M-PNL-02"}),
    ):
        _require(_parse_only(argv) == (ids, None), f"{argv} must select {ids}")
    #    未知 id 的解析本身是成功的 —— 由 main 的 "no mutation selected" 统一处理。
    _require(_parse_only(["--only", "UNKNOWN-ID"]) == ({"UNKNOWN-ID"}, None),
             "an unknown id is a selection miss, not a parse error")
    for argv in (
        ["--only"],
        ["--only="],
        ["--only", ""],
        ["--only", ","],
        ["--only=,"],
        ["--onlyy=M-PNL-01"],
        ["--only", "--only"],
        ["--only", "M-PNL-01", "--only", "M-PNL-02"],
    ):
        only, err = _parse_only(argv)
        _require(only is None and isinstance(err, str) and err.startswith("ERROR:"),
                 f"{argv} must be a controlled ERROR, got {(only, err)}")

    # 9b) argv 白名单（main 真正走的入口）：不认识的 token 不是"没有 selector"，
    #     不能被静默忽略成一次全量 matrix。
    _require(_parse_argv([]) == (None, None), "empty argv must mean the full matrix")
    for argv, ids in (
        (["--only", "M-PNL-01"], {"M-PNL-01"}),
        (["--only=M-PNL-01"], {"M-PNL-01"}),
    ):
        _require(_parse_argv(argv) == (ids, None), f"{argv} must select {ids}")
    _require(_parse_argv(["--only", "UNKNOWN-ID"]) == ({"UNKNOWN-ID"}, None),
             "an unknown id is a selection miss, not a parse error")
    for argv in (
        ["--only"],
        ["--only="],
        ["--onlyy=M-PNL-01"],
        ["--onl", "M-PNL-01"],
        ["--dry-run"],
        ["foo"],
        ["--only", "M-PNL-01", "foo"],
        ["--only", "M-PNL-01", "--only", "M-PNL-02"],
        ["--non-vacuity"],
    ):
        only, err = _parse_argv(argv)
        _require(only is None and isinstance(err, str) and err.startswith("ERROR:"),
                 f"{argv} must be a controlled ERROR, got {(only, err)}")

    # 10) 同一条边界在**真实 CLI** 上：exit 2、受控 ERROR、不抛裸 traceback、
    #     不进选择/baseline/mutation 阶段，且 production source 逐字节不变。
    guarded = {}
    with open(os.path.join(ROOT, DS_FILE), "rb") as handle:
        guarded[DS_FILE] = sha256(handle.read())
    for argv in (["--only"], ["--only="], ["--onlyy=M-PNL-01"], ["--onl", "M-PNL-01"],
                 ["--dry-run"], ["foo"], ["--non-vacuity"],
                 ["--only", "M-PNL-01", "foo"],
                 ["--only", "M-PNL-01", "--only", "M-PNL-02"]):
        _assert_cli_rejected(argv)
    for name, before in guarded.items():
        with open(os.path.join(ROOT, name), "rb") as handle:
            _require(sha256(handle.read()) == before,
                     f"{name} 被一次被拒的 CLI 调用改动了")
    #     --only=<ids> 必须真的走到选择阶段（不是被解析层拒掉）：未知 id → 空选择。
    code, blob = _cli_probe(["--only=NO-SUCH-MUTATION"])
    _require(code == 2, f"--only=<ids>: expected exit 2, got {code}: {blob[:200]}")
    _require("no mutation selected" in blob,
             f"--only=<ids> must reach the selection stage: {blob[:200]}")


def self_test_optimization() -> None:
    """证明 harness 的硬守卫在 ``python -O`` 下**仍然存在**。

    ``assert`` 会被 ``-O`` 整条剥除。本 harness 的证据链守卫（anchor 唯一性、restore
    sha256、分类语义）一律走 :func:`_require`，这个 self-test 就是它的非空性证明：用
    ``sys.executable`` 起两个最小子进程（普通解释器 / ``-O``）跑同一份探针，要求两者都
    得到 ``RuntimeError``，并且 ``-O`` 那次**确实**处于优化模式（``__debug__ is False``）。
    否则"在 -O 下也成立"就是对着普通解释器做的空证明。
    """
    root = tempfile.mkdtemp(prefix="r27b2c4c_optimization_probe_")
    probe = os.path.join(root, "optimization_probe.py")
    with open(probe, "w", encoding="utf-8", newline="") as handle:
        handle.write(_OPTIMIZATION_PROBE)

    #: count=0 / count=2 / count=1 / _require(False) —— 前两个与第四个必须硬失败，
    #: 第三个是阳性对照：守卫不能被做得"一律失败"。
    expected = ["RuntimeError", "RuntimeError", "NO-ERROR", "RuntimeError"]
    env = {key: value for key, value in os.environ.items() if key != "PYTHONOPTIMIZE"}
    for flags, want_optimized in (([], False), (["-O"], True)):
        proc = subprocess.run(
            [sys.executable, *flags, probe, os.path.abspath(__file__)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=ROOT, timeout=300, env=env,
        )
        label = " ".join(flags) or "(default)"
        blob = (proc.stdout or "") + (proc.stderr or "")
        _require(proc.returncode == 0, f"optimization probe {label} failed: {blob[:400]}")
        try:
            report = json.loads((proc.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise RuntimeError(
                f"optimization probe {label} produced no report: {blob[:400]}") from exc
        _require(report.get("optimized") is want_optimized,
                 f"python {label} did not run in the expected mode: {report}")
        _require(report.get("results") == expected,
                 f"hard guards differ under python {label}: {report}")


def assert_no_leftover(path: str, mutation_id: str) -> None:
    with open(path, encoding="utf-8") as handle:
        if MUTANT_MARKER in handle.read():
            raise RuntimeError(f"{mutation_id}: leftover mutant in {path}")


def _restore_and_verify(path: str, original: bytes, before: str, mutation_id: str) -> None:
    """按启动快照 byte-identical 还原，并校验 sha256 —— 不一致即硬失败。"""
    with open(path, "wb") as handle:
        handle.write(original)
    with open(path, "rb") as handle:
        after = sha256(handle.read())
    if after != before:
        raise RuntimeError(f"{mutation_id}: restore sha256 mismatch")
    assert_no_leftover(path, mutation_id)


def _verify_untouched(path: str, original: bytes, before: str) -> None:
    """整张矩阵跑完后，production file 必须逐字节等于启动快照。"""
    with open(path, "rb") as handle:
        current = handle.read()
    if current != original or sha256(current) != before:
        raise RuntimeError(
            f"{path}: 矩阵结束后与启动快照不一致 "
            f"({sha256(current)} != {before})"
        )


def _targets_for(selected, extra=()) -> list[str]:
    """baseline 的目标集：selected mutations 的去重 target + 点名的永久回归目标。

    两条来源都按**首次出现**去重：同一目标被多条 mutation 驱动（或既是 mutation
    target 又是 baseline-only target）时只跑一次，但覆盖范围必须显式可见。
    """
    targets: list[str] = []
    for mutation in selected:
        if mutation["test"] not in targets:
            targets.append(mutation["test"])
    for target in extra:
        if target not in targets:
            targets.append(target)
    return targets


def run_baselines(selected, *, runner=run_test, extra=()) -> int:
    """在触碰任何 production source **之前**，先证明全部目标永久回归都是 GREEN。

    baseline 是 matrix 自身的强制前提，不是可选观察项：某个目标若在干净源码上本来就红，
    它的所有 mutation 都会因 ``returncode != 0`` 被记成 CAUGHT —— 那是假证据。因此对
    selected mutations 的**去重** target 集合（外加 :data:`BASELINE_ONLY_TARGETS`）依次
    运行；任一非 GREEN 立即失败，**不进入** mutation 阶段。

    baseline 阶段的 ``subprocess.TimeoutExpired`` 记 :data:`BASELINE_TIMEOUT`，与
    ``BASELINE-RED`` **语义不同**（前者根本没跑完，后者是在干净源码上失败），但同样
    让 matrix FAIL 且 mutation 阶段不启动。
    """
    targets = _targets_for(selected, extra)
    if not targets:
        print("baseline: no target selected", flush=True)
        return 1
    red: list[str] = []
    timed_out: list[str] = []
    for target in targets:
        try:
            result = runner(target)
        except subprocess.TimeoutExpired:
            print(f"baseline {target}: {BASELINE_TIMEOUT}", flush=True)
            timed_out.append(target)
            continue
        if result.returncode == 0:
            print(f"baseline {target}: GREEN", flush=True)
        else:
            print(f"baseline {target}: {BASELINE_RED}({result.returncode})", flush=True)
            red.append(target)
    if timed_out or red:
        if timed_out:
            print(
                f"baseline: TIMEOUT —— {len(timed_out)}/{len(targets)} 个目标未跑完：{timed_out}",
                flush=True,
            )
        if red:
            print(
                f"baseline: RED —— {len(red)}/{len(targets)} 个目标在干净源码上非 GREEN：{red}",
                flush=True,
            )
        print("mutation 阶段不启动（baseline 是强制前提）", flush=True)
        return 1
    print(f"baseline: GREEN（{len(targets)} 个目标全部先于 mutation 验证）", flush=True)
    return 0


def run_mutation(mutation: dict, *, root: str = ROOT, runner=run_test) -> str:
    """Return ``CAUGHT`` / ``SURVIVED`` / ``FAKE`` / ``TIMEOUT``。

    baseline 由 :func:`run_baselines` 在进入 mutation 阶段**之前**统一证明；本函数不再
    含任何"是否跑 baseline"的分支 —— 那正是假证据缺口：默认路径允许跳过 baseline，
    于是一个本来就红的目标会让它的所有 mutation 被记成 CAUGHT。

    ``subprocess.TimeoutExpired`` 单独归为 :data:`VERDICT_TIMEOUT`：超时既不是被业务断言
    杀死（CAUGHT），也不是接线错误（FAKE），它是一个**从未作出判定**的运行。无论走哪条
    路径，``finally`` 都按启动快照 byte-identical 还原并校验 sha256。
    """
    path = os.path.join(root, mutation["file"])
    with open(path, "rb") as handle:
        original = handle.read()
    before = sha256(original)
    text = original.decode("utf-8").replace("\r\n", "\n")

    mutated = _apply(text, mutation)
    try:
        with open(path, "wb") as handle:
            handle.write(_adapt_eol(mutated, original))
        #: 变异体必须真的落盘：写失败 / 锚点漂移却继续跑测试，会把一次空转记成 CAUGHT。
        with open(path, encoding="utf-8") as handle:
            on_disk = handle.read()
        _require(MUTANT_MARKER in on_disk,
                 f'{mutation["id"]}: mutant marker missing on disk')
        try:
            result = runner(mutation["test"])
        except subprocess.TimeoutExpired:
            return VERDICT_TIMEOUT
        if result.returncode == 0:
            return VERDICT_SURVIVED
        if _is_fake_kill(result):
            return VERDICT_FAKE
        return VERDICT_CAUGHT
    finally:
        _restore_and_verify(path, original, before, mutation["id"])


def _apply(text: str, mutation: dict) -> str:
    """应用 mutation；anchor 必须**恰好命中一次**，否则硬失败。

    ``count == 0`` 会让 ``str.replace`` 静默返回原文 —— mutation 从未落盘，随后那条测试
    "被杀死" 就另有原因，是假证据；``count > 1`` 会让 ``replace(..., 1)`` 只改第一处，
    改的不是被证明的那一处。两种都必须硬失败，且在 ``python -O`` 下同样硬失败，
    所以这里（以及本文件所有 runtime evidence 检查）走 :func:`_require`，不用 ``assert``。
    """
    count = text.count(mutation["old"])
    _require(count == 1, (
        f'{mutation["id"]}: mutation anchor must be unique; count={count}; '
        f'file={mutation["file"]}; anchor={mutation["old"][:60]!r}'
    ))
    return text.replace(mutation["old"], mutation["new"], 1)


def _is_fake_kill(result: subprocess.CompletedProcess) -> bool:
    blob = (result.stdout or "") + (result.stderr or "")
    return bool(BROKEN_RE.search(blob))


def _parse_only(argv: list[str]) -> tuple[set[str] | None, str | None]:
    """解析 ``--only`` 选择器；返回 ``(selected_ids, error_message)``，两者互斥。

    只有三种结果，不存在第四种：

    1. argv 里没有 selector → ``(None, None)`` → 默认 full matrix；
    2. selector 合法 → ``(ids, None)``；
    3. selector 形态存在但非法 → ``(None, "ERROR: ...")`` → 调用方 exit 2。

    受支持的形式只有 ``--only <ids>`` 与 ``--only=<ids>``。**任何**以 ``--only`` 开头但
    不属于这两种的 token（``--onlyy=...`` 之类的拼写错误、``--only=`` 空列表、缺少
    value、重复 selector）都是第 3 类，而不是"没有 selector 所以跑全量 matrix"。
    理由：操作者请求 targeted mutation 时，实际覆盖范围绝不能因为 CLI 拼写问题被静默放大。

    ``--only`` 指向未知 id 由 main 的 "no mutation selected" 统一处理，不在本 helper 重复：
    那是**选择落空**，不是**参数非法**。
    """
    matches = [item for item in argv if item.startswith("--only")]
    if not matches:
        return None, None
    if len(matches) > 1:
        return None, (
            f"ERROR: --only 只能出现一次（收到 {len(matches)} 个：{matches}）。"
            "重复选择器不是'取并集'，因此拒绝而不是猜。"
        )
    token = matches[0]
    if token == "--only":
        index = argv.index(token)
        if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
            return None, (
                "ERROR: --only requires a comma-separated mutation id list "
                "(for example: --only M-PNL-01,M-PNL-03)"
            )
        raw = argv[index + 1]
    elif token.startswith("--only="):
        raw = token[len("--only="):]
    else:
        return None, (
            f"ERROR: unrecognized selector argument {token!r}; 受支持的形式只有 "
            "--only <ids> 与 --only=<ids>。未知拼写不会被当作'没有 selector'来处理。"
        )
    ids = {item for item in raw.split(",") if item}
    if not ids:
        return None, f"ERROR: --only 需要非空的逗号分隔 id 列表，收到 {raw!r}"
    return ids, None


def _parse_argv(argv: list[str]) -> tuple[set[str] | None, str | None]:
    """argv 白名单 + ``--only`` 选择器；返回 ``(selected_ids, error_message)``，两者互斥。

    与 :func:`_parse_only` 同构的三态，但把"不认识的 token"也算进来：

    1. 无参数 → ``(None, None)`` → 默认 full matrix；
    2. 合法请求 → ``(ids, None)``；
    3. 其余一律 ``(None, "ERROR: ...")`` → 调用方 exit 2。

    **不做静默忽略**：``--onl M-PNL-01`` / ``--dry-run`` / ``foo`` 都不是"没有 selector"，
    而是参数错误。理由与 ``--only`` 那条完全相同 —— 操作者请求 targeted mutation 时，
    实际覆盖范围绝不能因为 CLI 拼写问题被悄悄放大成全量 matrix。

    被显式拒绝的 ``--non-vacuity`` 也在这里判定，保证参数判定只有一个入口：
    任何 argv 先过白名单，再交给 :func:`_parse_only` 判选择器形态。
    """
    if "--non-vacuity" in argv:
        return None, (
            "ERROR: --non-vacuity 已删除。baseline 现在是 matrix 的强制前提，"
            "无条件先于任何 mutation 运行，没有开关。"
        )
    unknown: list[str] = []
    expects_value = False
    for token in argv:
        if expects_value:
            # ``--only`` 的取值 token：它就是 mutation id，形态交给 _parse_only 判。
            expects_value = False
        elif token == "--only":
            expects_value = True
        elif token.startswith("--only"):
            continue
        else:
            unknown.append(token)
    if unknown:
        return None, (
            f"ERROR: unrecognized argument(s) {unknown}；受支持的形式只有 "
            "--only <ids> 与 --only=<ids>（以及被显式拒绝的 --non-vacuity）。"
            "未知 token 不会被当作'没有 selector'而跑全量 matrix。"
        )
    return _parse_only(argv)


def main() -> int:
    print(f"repo root: {ROOT}")
    #: 参数判定只有一个入口：白名单 + 选择器形态。任何不认识的 token 都是受控 ERROR，
    #: 绝不静默退化成"跑全量 matrix"。
    only, parse_error = _parse_argv(sys.argv[1:])
    if parse_error:
        print(parse_error, flush=True)
        return 2

    #: 选择阶段先于 self-test：参数 / 选择非法时立刻退出，不为一条误用的命令跑全套自检；
    #: 这也让 self-test 能用**真实子进程**验证 CLI 边界而不产生自递归。
    selected = [m for m in MUTATIONS if only is None or m["id"] in only]
    if not selected:
        print("no mutation selected", flush=True)
        return 2
    #: 覆盖范围必须显式回显 —— 参数谜题的代价正是"以为只跑了 1 条，其实跑了 14 条"。
    print(f'selected: {len(selected)}/{len(MUTATIONS)} mutation(s): '
          f'{[m["id"] for m in selected]}', flush=True)

    self_test_sequence()
    print("runner self-test: PASS (unique, increasing pycache sequence)")
    self_test_semantics()
    print("semantics self-test: PASS (anchor uniqueness / BASELINE-RED / BASELINE-TIMEOUT / "
          "CAUGHT / SURVIVED / FAKE / TIMEOUT / restore / --only)")
    self_test_optimization()
    print("optimization self-test: PASS (hard guards survive python -O)")

    #: 启动快照：整张矩阵的还原基准。任何一条 mutation 的 finally 都以它为终点，
    #: 矩阵结束后再整体复验一次（"每条都还原了"与"文件最后真的是原样"是两件事）。
    with open(os.path.join(ROOT, DS_FILE), "rb") as handle:
        startup = handle.read()
    startup_sha = sha256(startup)
    print(f"startup snapshot: {DS_FILE} sha256={startup_sha}", flush=True)

    if run_baselines(selected, extra=BASELINE_ONLY_TARGETS) != 0:
        print("mutation matrix: FAILED —— baseline 非 GREEN，mutation 阶段未启动", flush=True)
        return 1

    results: list[tuple[str, str]] = []
    for mutation in selected:
        verdict = run_mutation(mutation)
        results.append((mutation["id"], verdict))
        print(f'{mutation["id"]} {mutation["desc"]}: {verdict}', flush=True)

    bad = [(mid, v) for mid, v in results if v != VERDICT_CAUGHT]
    for mid, verdict in bad:
        print(f"NOT-CAUGHT {mid}: {verdict}")
    detected = sum(1 for _, v in results if v == VERDICT_CAUGHT)
    survived = sum(1 for _, v in results if v == VERDICT_SURVIVED)
    fake = sum(1 for _, v in results if v == VERDICT_FAKE)
    timeout = sum(1 for _, v in results if v == VERDICT_TIMEOUT)

    restore_ok = True
    try:
        _verify_untouched(os.path.join(ROOT, DS_FILE), startup, startup_sha)
    except RuntimeError as exc:
        print(f"restore: FAIL —— {exc}", flush=True)
        restore_ok = False

    print(f"R27-B2C-4C mutation matrix: baseline=GREEN; "
          f"{detected}/{len(results)} DETECTED; survived={survived}; fake={fake}; "
          f"timeout={timeout}")
    gate_pass = not bad and restore_ok
    print("gate: baseline=GREEN, survived=0, fake=0, timeout=0, "
          f"restore sha256={'PASS' if restore_ok else 'FAIL'} -> "
          f"{'PASS' if gate_pass else 'FAIL'}")
    return 0 if gate_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
