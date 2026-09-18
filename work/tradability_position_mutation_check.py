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
    # v18 订单周期归属：迁移/guard/order-writer 与跨周期身份契约。
    "test_order_cycle_provenance",
    "test_order_cycle_identity",
    # Round-6：延迟成交的周期绑定（Blockers 1/2）。
    "test_deferred_fill_cycle_binding",
    # Round-9：legacy paper_positions 镜像不得被重物化成 executable position。
    "test_legacy_position_rematerialization",
)

ADAPTER = "backend/tradability_position_evidence.py"
SHADOW = "backend/tradability_position_shadow.py"
#: v18 订单周期归属：迁移/guard 与生产订单写入口（供 M-OC* 变异使用）。
MIGRATIONS = "backend/paper_schema_migrations.py"
WRITER = "backend/paper_trading.py"

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
        "                    elif relation == \"excluded\":\n"
        "                        attribution = CYCLE_ATTRIBUTION_MISMATCH\n",
        "                    # MUTANT M-T1-12: outside-window sell accepted as our cycle\n"
        "                    elif False:\n"
        "                        attribution = CYCLE_ATTRIBUTION_MISMATCH\n",
        "周期窗外的卖出被当成属于本周期（跨周期成交可扣减本周期 lot）",
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
    (
        "M-T1-17",
        ADAPTER,
        "            if existence is None or existence > sell_executed_at:\n"
        "                # 无法证明它在卖出之前存在，或明确晚于卖出 → 不得消费。\n"
        "                continue\n",
        "            # MUTANT M-T1-17: lot existence-time gate removed\n",
        "删除 lot 存在性闸门（未来 lot 可反向满足更早的 SELL）",
    ),
    (
        "M-T1-18",
        ADAPTER,
        "                    if relation == \"undecidable\":\n"
        "                        # 周期行存在但起点不可证明 / 边界同一天且日内顺序未知。\n"
        "                        attribution = CYCLE_ATTRIBUTION_UNPROVABLE\n",
        "                    # MUTANT M-T1-18: missing cycle start defaults to proven\n"
        "                    if False:\n"
        "                        attribution = CYCLE_ATTRIBUTION_UNPROVABLE\n",
        "周期起点缺失时重新默认 cycle_ok=True（静默升级归属）",
    ),
    (
        "M-T1-19",
        ADAPTER,
        "                        ambiguous, unprovable = self._competing_cycles(\n"
        "                            cycle_id, session, executed_instant,\n"
        "                        )\n",
        "                        # MUTANT M-T1-19: overlapping cycle ambiguity ignored\n"
        "                        ambiguous, unprovable = False, ()\n",
        "重叠周期歧义被静默接受（直接采信请求周期）",
    ),
    (
        "M-CYCLE-FC1",
        ADAPTER,
        "            relation = self._cycle_relation(self._cycle_facts(other), session, instant)\n"
        "            if relation == \"excluded\":\n"
        "                # 已证明不拥有该卖出 → 可以排除。\n"
        "                continue\n",
        "            # MUTANT M-CYCLE-FC1: undecidable competitor silently skipped\n"
        "            if relation == \"undecidable\":\n"
        "                continue\n",
        "未知竞争周期被忽略（缺失边界证据 ⇒ 仍报 proven）",
    ),
    (
        "M-CYCLE-FC2",
        ADAPTER,
        "            relation = self._cycle_relation(self._cycle_facts(other), session, instant)\n"
        "            if relation == \"excluded\":\n"
        "                # 已证明不拥有该卖出 → 可以排除。\n"
        "                continue\n",
        "            # MUTANT M-CYCLE-FC2: paused competitor treated as non-competing\n"
        "            if relation == \"undecidable\" and str(_row_field(row, \"status\") or \"\") == \"paused\":\n"
        "                continue\n"
        "            if relation == \"undecidable\":\n"
        "                unprovable.append(other)\n"
        "                continue\n",
        "paused 被当作「不竞争」的证据（status 冒充时间证据）",
    ),
    (
        "M-CYCLE-FC3",
        ADAPTER,
        "            raw_start = _text(_row_field(row, \"started_at\"))\n"
        "            raw_end = _text(_row_field(row, \"ended_at\"))\n",
        "            # MUTANT M-CYCLE-FC3: created_at silently promoted to economic start\n"
        "            raw_start = _text(_row_field(row, \"started_at\")) or _text(\n"
        "                _row_field(row, \"created_at\"))\n"
        "            raw_end = _text(_row_field(row, \"ended_at\"))\n",
        "created_at 被偷偷升级成 economic started_at（起点未知被补成 proven）",
    ),
    (
        "M-CYCLE-FC4",
        ADAPTER,
        "                        elif unprovable:\n"
        "                            # 有周期无法证明不竞争 → 归属不可证明（不是 proven）。\n"
        "                            attribution = CYCLE_ATTRIBUTION_UNPROVABLE\n"
        "                            cycle_diagnostics.append(\"competing_cycle_unprovable\")\n",
        "                        # MUTANT M-CYCLE-FC4: unprovable competitor only logged\n"
        "                        elif False:\n"
        "                            attribution = CYCLE_ATTRIBUTION_UNPROVABLE\n"
        "                            cycle_diagnostics.append(\"competing_cycle_unprovable\")\n",
        "unprovable 竞争者只记录不影响结论（仍报 proven）",
    ),
    (
        "M-CYCLE-FC5",
        ADAPTER,
        "        if self._orders_have_cycle_column():\n"
        "            columns.append(\"o.cycle_id AS order_cycle_id\")\n",
        "        # MUTANT M-CYCLE-FC5: existing cycle column never read from SQL\n"
        "        if False:\n"
        "            columns.append(\"o.cycle_id AS order_cycle_id\")\n",
        "未来 schema 有 order_cycle_id 却不从 SQL 读取（durable identity 失效）",
    ),
    (
        "M-CYCLE-FC6",
        ADAPTER,
        "                    elif requested_facts is not None and self._cycle_relation(\n"
        "                            requested_facts, session, executed_instant) == \"excluded\":\n"
        "                        # 显式身份说属于本周期，本周期可证明的时间窗却把它排除 →\n"
        "                        # 硬冲突：两边证据都在，不能静默相信任意一方。§20：这里也按\n"
        "                        # **时刻**比较，否则「同日 16:00 才开始的周期」会被误当成已覆盖。\n"
        "                        attribution = CYCLE_ATTRIBUTION_UNPROVABLE\n"
        "                        cycle_diagnostics.append(\"sell_fill_cycle_identity_conflict\")\n",
        "                    # MUTANT M-CYCLE-FC6: identity/time-window conflict silently accepted\n"
        "                    elif False:\n"
        "                        attribution = CYCLE_ATTRIBUTION_UNPROVABLE\n"
        "                        cycle_diagnostics.append(\"sell_fill_cycle_identity_conflict\")\n",
        "显式 order_cycle_id 与时间窗冲突仍被接受（不 fail closed）",
    ),
    (
        "M-T1-20",
        ADAPTER,
        "            key=lambda item: (\n"
        "                str(item.get(\"session\") or \"\"),\n"
        "                str(item.get(\"executed_at\") or \"\"),\n"
        "                _int(item.get(\"fill_id\")),\n"
        "            ),\n",
        "            # MUTANT M-T1-20: event ordering ignores executed_at\n"
        "            key=lambda item: (\n"
        "                str(item.get(\"session\") or \"\"),\n"
        "                _int(item.get(\"fill_id\")),\n"
        "            ),\n",
        "卖出事件排序忽略 executed_at（按数据库 id 排）",
    ),
    (
        "M-OC1",
        MIGRATIONS,
        "    definitions = {\"cycle_id\": \"INTEGER\"}\n",
        "    # MUTANT M-OC1: legacy rows backfilled from the active cycle\n"
        "    definitions = {\"cycle_id\": \"INTEGER\"}\n"
        "    if table_columns(conn, \"paper_cycles\"):\n"
        "        conn.execute(\n"
        "            \"UPDATE paper_orders SET cycle_id=(SELECT MAX(id) FROM paper_cycles) \"\n"
        "            \"WHERE cycle_id IS NULL\"\n"
        "        )\n",
        "migration 用当前 active cycle 回填 legacy 行（伪造历史 provenance）",
    ),
    (
        "M-OC2",
        MIGRATIONS,
        "    for table in (\"paper_orders\", \"paper_orders_archive\"):\n"
        "        if \"cycle_id\" not in table_columns(conn, table):\n"
        "            continue\n",
        "    for table in ():\n"
        "        if \"cycle_id\" not in table_columns(conn, table):\n"
        "            continue\n",
        "cycle_id 可被 UPDATE（immutable guard 未安装）",
    ),
    (
        "M-OC3",
        MIGRATIONS,
        "    definitions = {\"cycle_id\": \"INTEGER\"}\n"
        "    changes = {}\n"
        "    for table in (\"paper_orders\", \"paper_orders_archive\"):\n"
        "        changes[table] = ensure_columns(conn, table, definitions)\n",
        "    definitions = {\"cycle_id\": \"INTEGER\"}\n"
        "    changes = {}\n"
        "    for table in (\"paper_orders\",):\n"
        "        changes[table] = ensure_columns(conn, table, definitions)\n",
        "archive 表漏 cycle_id（SELECT * 整行拷贝错位）",
    ),
    (
        "M-OC4",
        MIGRATIONS,
        "                    OR NOT EXISTS (\n"
        "                        SELECT 1 FROM paper_cycles c WHERE c.id=NEW.cycle_id\n"
        "                    )\n",
        "                    OR 0\n",
        "不存在的 cycle_id 被接受（guard 不再校验引用完整性）",
    ),
    (
        "M-OC5",
        WRITER,
        "    if order_id is not None:\n"
        "        prov = _order_cycle_provenance_for_order(conn, order_id)\n"
        "        if not prov.is_proven:\n",
        "    # MUTANT M-OC5: lot falls back to the active cycle for a legacy order\n"
        "    if False:\n",
        "BUY lot 不继承来源订单 cycle（订单与 lot 可跨周期）",
    ),
    (
        "M-OC6",
        WRITER,
        "    cycle_id = _order_cycle_id(conn, cycle_id)\n"
        "    remaining = int(qty)\n",
        "    # MUTANT M-OC6: lot consumption re-resolves the active cycle\n"
        "    cycle_id = _active_cycle(conn)[\"id\"]\n"
        "    remaining = int(qty)\n",
        "SELL 消耗 lot 时重新解析周期（split-brain）",
    ),
    (
        "M-OC7",
        ADAPTER,
        "        if self._orders_have_cycle_column():\n"
        "            columns.append(\"o.cycle_id AS order_cycle_id\")\n",
        "        # MUTANT M-OC7: adapter ignores the explicit order cycle\n"
        "        if False:\n"
        "            columns.append(\"o.cycle_id AS order_cycle_id\")\n",
        "adapter 忽略显式 order cycle（durable provenance 失效）",
    ),
    (
        "M-OC8",
        ADAPTER,
        "        if instant is not None and end_precise and end_instant is not None \\\n"
        "                and instant > end_instant:\n"
        "            return \"excluded\"\n",
        "        if False:\n"
        "            return \"excluded\"\n",
        "同日 cycle 边界退回 date-only 比较（时刻精度丢失）",
    ),
    (
        "M-CF1",
        "backend/paper_trading.py",
        "    if order_id is not None:\n"
        "        prov = _order_cycle_provenance_for_order(conn, order_id)\n"
        "        if not prov.is_proven:\n"
        "            raise OrderCycleProvenanceUnknown(\n"
        "                order_id, prov.status,\n"
        "                \"来源买单的周期归属不可证明；拒绝创建带确定周期的新 lot\",\n"
        "            )\n"
        "        cycle_id = prov.cycle_id\n"
        "    else:\n"
        "        cycle_id = _order_cycle_id(conn, cycle_id)\n",
        "    order_cycle = _order_cycle_id_for_order(conn, order_id) if order_id is not None else None\n"
        "    cycle_id = order_cycle if order_cycle is not None else _order_cycle_id(conn, cycle_id)\n",
        "legacy order 的 lot 回退到当前 active cycle",
    ),
    (
        "M-CF2",
        "backend/execution_planner.py",
        "        consumed, cost_amount = PT._consume_available_lots(\n"
        "            conn, account_id, code, qty, asof_day, cycle_id=order_cycle_id,\n"
        "        )\n",
        "        consumed, cost_amount = PT._consume_available_lots(conn, account_id, code, qty, asof_day)\n",
        "SELL 成交不把订单周期传给 FIFO 消耗",
    ),
    (
        "M-CF3",
        "backend/paper_trading.py",
        "    if not provenance.is_proven:\n"
        "        raise OrderCycleProvenanceUnknown(\n"
        "            order_id, provenance.status,\n"
        "            \"订单周期归属不可证明；拒绝进入成交语义\",\n"
        "        )\n"
        "    order_cycle_id = provenance.cycle_id\n",
        "    if not provenance.is_proven:\n"
        "        order_cycle_id = _active_cycle_id_readonly(conn)\n"
        "    else:\n"
        "        order_cycle_id = provenance.cycle_id\n",
        "归属不可证明时回退到当前 active cycle（NULL/未知被当成可成交）",
    ),
    (
        "M-CF4",
        "backend/paper_trading.py",
        "    if order_id is not None:\n"
        "        prov = _order_cycle_provenance_for_order(conn, order_id)\n"
        "        if not prov.is_proven:\n"
        "            raise OrderCycleProvenanceUnknown(\n"
        "                order_id, prov.status,\n"
        "                \"来源买单的周期归属不可证明；拒绝创建带确定周期的新 lot\",\n"
        "            )\n"
        "        cycle_id = prov.cycle_id\n",
        "    cycle_id = _order_cycle_id(conn, cycle_id)\n",
        "BUY lot 不继承订单周期，改用当前 active cycle",
    ),
    (
        "M-CF5",
        "backend/execution_planner.py",
        "    order_cycle_id = PT._assert_order_execution_cycle(\n"
        "        conn, order_id, account_id=account_id, provenance=provenance,\n",
        "    order_cycle_id = PT._order_cycle_id(conn)\n",
        "成交闸门被替换成当前 active cycle（execution-cycle 一致性校验消失）",
    ),
    (
        "M-CF6",
        "backend/paper_trading.py",
        "        if not same:\n"
        "            # 跨周期 ⇒ 终止血缘，新订单作为独立尝试写入（不带 retry_of_order_id）。\n"
        "            return None\n",
        "",
        "跨周期 retry lineage 被静默接受",
    ),
    (
        "M-CF7",
        "backend/paper_trading.py",
        "    if cycle_id is None:\n"
        "        raise OrderCycleProvenanceUnknown(\n"
        "            None, ORDER_CYCLE_ORDER_MISSING,\n"
        "            \"lot 消耗必须由来源订单显式提供周期；拒绝回退到当前 active cycle\",\n"
        "        )\n",
        "    cycle_id = _order_cycle_id(conn, cycle_id)\n",
        "匿名 lot 消耗回退到当前 active cycle",
    ),
    (
        "M-CF8",
        "backend/execution_planner.py",
        "    if mismatches:\n"
        "        raise RuntimeError(\n"
        "            f\"order identity mismatch for order_id={order_id}: \" + \"; \".join(mismatches)\n"
        "        )\n",
        "    if False:\n"
        "        raise RuntimeError('order identity mismatch')\n",
        "订单身份冲突被静默接受",
    ),
    (
        "M-CF9",
        "backend/paper_trading.py",
        "    if account_cycle_id != order_cycle_id or active_cycle_id != order_cycle_id:\n",
        "    if False:\n",
        "删除 order_cycle == account_cycle / active_cycle 一致性检查",
    ),
    (
        "M-CF10",
        "backend/paper_trading.py",
        "    account_cycle_id = _account_cycle_id_readonly(conn, account_id)\n",
        "    account_cycle_id = order_cycle_id\n",
        "账户周期证据被替换成订单周期（account 检查恒成立而失去意义）",
    ),
    (
        "M-CF11",
        "backend/manual_orders.py",
        "                PT_assert_execution_cycle = _order_execution_cycle_guard()\n"
        "                guarded_cycle_id = PT_assert_execution_cycle(\n"
        "                    conn, order[\"id\"], account_id=order[\"account_id\"],\n"
        "                )\n",
        "                guarded_cycle_id = None\n",
        "pending 扫描跳过 cycle guard，直接进入预占",
    ),
    (
        "M-CF12",
        "backend/execution_planner.py",
        "    order_cycle_id = PT._assert_order_execution_cycle(\n"
        "        conn, order_id, account_id=account_id, provenance=provenance,\n",
        "    order_cycle_id = provenance.cycle_id\n",
        "commit_fill 的 defense-in-depth 周期校验被删除（仅剩上层预检）",
    ),
    (
        "M-CF14",
        "backend/paper_capital_reservations.py",
        "        reserved_cycle = _as_int(existing.get(\"cycle_id\"))\n"
        "        if reserved_cycle != int(expected_cycle_id):\n",
        "        reserved_cycle = int(expected_cycle_id)\n"
        "        if reserved_cycle != int(expected_cycle_id):",
        "预占周期与订单周期不一致时仍允许 resize",
    ),
    (
        "M-CF15",
        "backend/manual_orders.py",
        "                terminal = _terminalize_cycle_stale_order(conn, order, guard_exc)\n"
        "                output.append(terminal)\n"
        "                continue\n",
        "                output.append({\"order_id\": order[\"id\"], \"status\": \"pending_limit\"})\n"
        "                continue\n",
        "stale 订单不终态化（每轮 pending → 失败 → pending，永久污染扫描器）",
    ),
    (
        "M-CF16",
        "backend/manual_orders.py",
        "                    reserved, reserve_reason = _reserve_shared_capital(\n"
        "                        conn, order[\"id\"], order[\"account_id\"], order[\"code\"],\n"
        "                        reserve_amount, reserve_fees,\n"
        "                        expected_cycle_id=guarded_cycle_id,\n"
        "                    )\n"
        "                except Exception as exc:\n"
        "                    # §3：归属冲突是**永久性**冲突，不是临时资金不足 ⇒ 终态化，\n"
        "                    # 不能打回 pending_limit 让下一轮再试（永远不会成功）。\n"
        "                    if not _is_reservation_cycle_mismatch(exc, _ReservationCycleMismatch):\n"
        "                        raise\n"
        "                    output.append(_terminalize_cycle_stale_order(conn, order, exc))\n"
        "                    continue\n",
        "                reserved, reserve_reason = _reserve_shared_capital(\n"
        "                    conn, order[\"id\"], order[\"account_id\"], order[\"code\"],\n"
        "                    reserve_amount, reserve_fees,\n"
        "                )\n",        "未触发分支漏传 expected_cycle_id（预占周期失去校验）",
    ),
    (
        "M-CF17",
        "backend/manual_orders.py",
        "            try:\n"
        "                reserved, reserve_reason = _reserve_shared_capital(\n"
        "                    conn, order[\"id\"], order[\"account_id\"], order[\"code\"],\n"
        "                    reserve_amount, reserve_fees, expected_cycle_id=guarded_cycle_id,\n"
        "                )\n"
        "            except Exception as exc:\n"
        "                # §3：预占归属冲突是永久性事实冲突，**不是**临时资金不足。\n"
        "                # 旧行为把它和 funding shortage 混在一起 ⇒ 打回 pending_limit\n"
        "                # 让下一轮再试 —— 而 order.cycle_id 与 reservation.cycle_id 都\n"
        "                # 不可变，所以这个重试永远不会成功，只会永久污染扫描器。\n"
        "                if not _is_reservation_cycle_mismatch(exc, _ReservationCycleMismatch):\n"
        "                    raise\n"
        "                output.append(_terminalize_cycle_stale_order(conn, order, exc))\n"
        "                continue\n",
        "            reserved, reserve_reason = _reserve_shared_capital(\n"
        "                conn, order[\"id\"], order[\"account_id\"], order[\"code\"],\n"
        "                reserve_amount, reserve_fees, expected_cycle_id=guarded_cycle_id,\n"
        "            )\n",        "归属冲突被当成普通资金不足（无终态化分支）",
    ),
    (
        "M-CF18",
        "backend/manual_orders.py",
        "            try:\n"
        "                reserved, reserve_reason = _reserve_shared_capital(\n"
        "                    conn, order[\"id\"], order[\"account_id\"], order[\"code\"],\n"
        "                    reserve_amount, reserve_fees, expected_cycle_id=guarded_cycle_id,\n"
        "                )\n"
        "            except Exception as exc:\n"
        "                # §3：预占归属冲突是永久性事实冲突，**不是**临时资金不足。\n"
        "                # 旧行为把它和 funding shortage 混在一起 ⇒ 打回 pending_limit\n"
        "                # 让下一轮再试 —— 而 order.cycle_id 与 reservation.cycle_id 都\n"
        "                # 不可变，所以这个重试永远不会成功，只会永久污染扫描器。\n"
        "                if not _is_reservation_cycle_mismatch(exc, _ReservationCycleMismatch):\n"
        "                    raise\n"
        "                output.append(_terminalize_cycle_stale_order(conn, order, exc))\n"
        "                continue\n",
        "            try:\n"
        "                reserved, reserve_reason = _reserve_shared_capital(\n"
        "                    conn, order[\"id\"], order[\"account_id\"], order[\"code\"],\n"
        "                    reserve_amount, reserve_fees, expected_cycle_id=guarded_cycle_id,\n"
        "                )\n"
        "            except Exception as exc:\n"
        "                if not _is_reservation_cycle_mismatch(exc, _ReservationCycleMismatch):\n"
        "                    raise\n"
        "                conn.execute(\n"
        "                    'UPDATE paper_orders SET status=\\'pending_limit\\',reason=? WHERE id=?',\n"
        "                    (str(exc), order[\"id\"]),\n"
        "                )\n"
        "                continue\n",        "归属冲突后仍保持 pending_limit（永久重试）",
    ),
    (
        "M-CF19",
        "backend/manual_orders.py",
        "    # §6/§7：释放既有预占（若无预占，UPDATE 命中 0 行，天然 no-op，无需 catch-all）。\n"
        "    # 释放失败即让本事务失败 —— 绝不留下「订单终态 + 资金仍被占用」的组合。\n"
        "    _finish_capital_reservation(conn, order_id, \"released\")\n",
        "    # §6/§7：释放既有预占（若无预占，UPDATE 命中 0 行，天然 no-op，无需 catch-all）。\n"
        "    # 释放失败即让本事务失败 —— 绝不留下「订单终态 + 资金仍被占用」的组合。\n"
        "    conn.execute(\n"
        "        'UPDATE paper_capital_reservations SET cycle_id=? WHERE order_key=?',\n"
        "        (detail.get('order_cycle_id'), str(order_id)),\n"
        "    )\n"
        "    _finish_capital_reservation(conn, order_id, \"released\")\n",        "终态化改写 reservation.cycle_id（伪造归属）",
    ),
    (
        "M-CF20",
        "backend/manual_orders.py",
        "    # §6/§7：释放既有预占（若无预占，UPDATE 命中 0 行，天然 no-op，无需 catch-all）。\n"
        "    # 释放失败即让本事务失败 —— 绝不留下「订单终态 + 资金仍被占用」的组合。\n"
        "    _finish_capital_reservation(conn, order_id, \"released\")\n",
        "    # §6/§7：释放既有预占（若无预占，UPDATE 命中 0 行，天然 no-op，无需 catch-all）。\n"
        "    # 释放失败即让本事务失败 —— 绝不留下「订单终态 + 资金仍被占用」的组合。\n"
        "    conn.execute(\n"
        "        'UPDATE paper_capital_reservations SET amount=0.0,fees=0.0 WHERE order_key=?',\n"
        "        (str(order_id),),\n"
        "    )\n"
        "    _finish_capital_reservation(conn, order_id, \"released\")\n",        "终态化 resize 预占金额/费用",
    ),
    (
        "M-CF21",
        "backend/manual_orders.py",
        "    # §6/§7：释放既有预占（若无预占，UPDATE 命中 0 行，天然 no-op，无需 catch-all）。\n"
        "    # 释放失败即让本事务失败 —— 绝不留下「订单终态 + 资金仍被占用」的组合。\n"
        "    _finish_capital_reservation(conn, order_id, \"released\")\n",
        "    # §6/§7：释放既有预占（若无预占，UPDATE 命中 0 行，天然 no-op，无需 catch-all）。\n"
        "    # 释放失败即让本事务失败 —— 绝不留下「订单终态 + 资金仍被占用」的组合。\n"
        "    try:\n"
        "        _finish_capital_reservation(conn, order_id, \"released\")\n"
        "    except Exception:\n"
        "        pass\n",        "预占释放失败被静默忽略（订单终态但资金仍被占用）",
    ),
    # ── Round-9：legacy paper_positions 不得成为 lot creator（§23） ──────────
    (
        "M-LP1",
        WRITER,
        "    cycle_id = _active_cycle_id_readonly(conn)\n"
        "    if cycle_id is None:\n"
        "        return []\n"
        "    cycle = {\"id\": cycle_id}\n",
        "    # MUTANT M-LP1: runtime legacy-position auto-migration restored\n"
        "    _active_cycle(conn)\n"
        "    for _legacy in _rows(conn, \"SELECT * FROM paper_positions\"):\n"
        "        if _num(_legacy.get(\"qty\")) > 0:\n"
        "            conn.execute(\n"
        "                \"INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,remaining_qty,cost,acquired_at,available_date,asset_type,is_t_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)\",\n"
        "                (_active_cycle_id_readonly(conn), _legacy[\"account_id\"], _legacy[\"code\"], _legacy.get(\"name\"),\n"
        "                 _legacy.get(\"industry\"), int(_legacy[\"qty\"]), int(_legacy[\"qty\"]), _num(_legacy[\"cost\"]),\n"
        "                 _legacy.get(\"entry_date\") or _date().isoformat(),\n"
        "                 _legacy.get(\"available_date\") or _date().isoformat(),\n"
        "                 _legacy.get(\"asset_type\") or \"stock_t1\"),\n"
        "            )\n"
        "    cycle_id = _active_cycle_id_readonly(conn)\n"
        "    if cycle_id is None:\n"
        "        return []\n"
        "    cycle = {\"id\": cycle_id}\n",
        "在 _position_rows 中恢复 runtime legacy-position 迁移",
    ),
    (
        "M-LP2",
        WRITER,
        "    legacy_rows = _rows(conn, \"SELECT * FROM paper_positions\")\n",
        "    # MUTANT M-LP2: mirror rows stamped into the current cycle as lots\n"
        "    legacy_rows = _rows(conn, \"SELECT * FROM paper_positions\")\n"
        "    for _mirror in legacy_rows:\n"
        "        if _num(_mirror.get(\"qty\")) > 0 and not conn.execute(\n"
        "            \"SELECT 1 FROM paper_position_lots WHERE cycle_id=? AND account_id=? AND code=? LIMIT 1\",\n"
        "            (cycle[\"id\"], _mirror[\"account_id\"], _mirror[\"code\"]),\n"
        "        ).fetchone():\n"
        "            conn.execute(\n"
        "                \"INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,remaining_qty,cost,acquired_at,available_date,asset_type,is_t_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)\",\n"
        "                (cycle[\"id\"], _mirror[\"account_id\"], _mirror[\"code\"], _mirror.get(\"name\"),\n"
        "                 _mirror.get(\"industry\"), int(_mirror[\"qty\"]), int(_mirror[\"qty\"]), _num(_mirror[\"cost\"]),\n"
        "                 _mirror.get(\"entry_date\") or _date().isoformat(),\n"
        "                 _mirror.get(\"available_date\") or _date().isoformat(),\n"
        "                 _mirror.get(\"asset_type\") or \"stock_t1\"),\n"
        "            )\n",
        "legacy 镜像行按 current active cycle 插入 lot",
    ),
    (
        "M-LP3",
        "backend/paper_portfolio.py",
        "    legacy = {(p[\"account_id\"], p[\"code\"]): p for p in legacy_rows}\n"
        "    out = []\n"
        "    for key, item in grouped.items():\n",
        "    legacy = {(p[\"account_id\"], p[\"code\"]): p for p in legacy_rows}\n"
        "    out = []\n"
        "    # MUTANT M-LP3: legacy-only mirror rows emitted as executable positions\n"
        "    for _key, _row in legacy.items():\n"
        "        if _key not in grouped and num(_row.get(\"qty\")) > 0:\n"
        "            grouped[_key] = {\n"
        "                \"account_id\": _row[\"account_id\"], \"code\": _row[\"code\"],\n"
        "                \"name\": _row.get(\"name\"), \"industry\": _row.get(\"industry\") or \"未知\",\n"
        "                \"qty\": int(num(_row.get(\"qty\"))), \"cost_amount\": num(_row.get(\"qty\")) * num(_row.get(\"cost\")),\n"
        "                \"entry_date\": str(_row.get(\"entry_date\") or day)[:10], \"available_qty\": 0,\n"
        "                \"locked_qty\": 0, \"asset_type\": _row.get(\"asset_type\") or \"stock_t1\",\n"
        "                \"available_date\": _row.get(\"available_date\") or day,\n"
        "            }\n"
        "    for key, item in grouped.items():\n",
        "aggregate_positions 把无 lot 的 legacy 行输出为持仓",
    ),
    (
        "M-LP4",
        WRITER,
        "def _sync_positions(conn, account_id=None, asof_day=None):\n"
        "    \"\"\"保留聚合表供旧接口兼容；交易结算逻辑只读取 lots。\"\"\"\n"
        "    positions = _position_rows(conn, account_id, asof_day)\n",
        "def _sync_positions(conn, account_id=None, asof_day=None):\n"
        "    \"\"\"保留聚合表供旧接口兼容；交易结算逻辑只读取 lots。\"\"\"\n"
        "    # MUTANT M-LP4: mirror rematerialized into lots before syncing\n"
        "    _cycle = _active_cycle_id_readonly(conn)\n"
        "    for _mirror in _rows(conn, \"SELECT * FROM paper_positions\"):\n"
        "        if _cycle is not None and _num(_mirror.get(\"qty\")) > 0 and not conn.execute(\n"
        "            \"SELECT 1 FROM paper_position_lots WHERE cycle_id=? AND account_id=? AND code=? LIMIT 1\",\n"
        "            (_cycle, _mirror[\"account_id\"], _mirror[\"code\"]),\n"
        "        ).fetchone():\n"
        "            conn.execute(\n"
        "                \"INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,remaining_qty,cost,acquired_at,available_date,asset_type,is_t_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)\",\n"
        "                (_cycle, _mirror[\"account_id\"], _mirror[\"code\"], _mirror.get(\"name\"),\n"
        "                 _mirror.get(\"industry\"), int(_mirror[\"qty\"]), int(_mirror[\"qty\"]), _num(_mirror[\"cost\"]),\n"
        "                 _mirror.get(\"entry_date\") or _date().isoformat(),\n"
        "                 _mirror.get(\"available_date\") or _date().isoformat(),\n"
        "                 _mirror.get(\"asset_type\") or \"stock_t1\"),\n"
        "            )\n"
        "    positions = _position_rows(conn, account_id, asof_day)\n",
        "_sync_positions 前先 rematerialize 镜像",
    ),
    (
        "M-LP5",
        WRITER,
        "def _shared_account_exposure(conn, quotes, asof_day=None):\n"
        "    positions = _position_rows(conn, asof_day=asof_day)\n",
        "def _shared_account_exposure(conn, quotes, asof_day=None):\n"
        "    # MUTANT M-LP5: exposure read materializes lots from the mirror\n"
        "    _cycle = _active_cycle_id_readonly(conn)\n"
        "    for _mirror in _rows(conn, \"SELECT * FROM paper_positions\"):\n"
        "        if _cycle is not None and _num(_mirror.get(\"qty\")) > 0 and not conn.execute(\n"
        "            \"SELECT 1 FROM paper_position_lots WHERE cycle_id=? AND account_id=? AND code=? LIMIT 1\",\n"
        "            (_cycle, _mirror[\"account_id\"], _mirror[\"code\"]),\n"
        "        ).fetchone():\n"
        "            conn.execute(\n"
        "                \"INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,remaining_qty,cost,acquired_at,available_date,asset_type,is_t_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)\",\n"
        "                (_cycle, _mirror[\"account_id\"], _mirror[\"code\"], _mirror.get(\"name\"),\n"
        "                 _mirror.get(\"industry\"), int(_mirror[\"qty\"]), int(_mirror[\"qty\"]), _num(_mirror[\"cost\"]),\n"
        "                 _mirror.get(\"entry_date\") or _date().isoformat(),\n"
        "                 _mirror.get(\"available_date\") or _date().isoformat(),\n"
        "                 _mirror.get(\"asset_type\") or \"stock_t1\"),\n"
        "            )\n"
        "    positions = _position_rows(conn, asof_day=asof_day)\n",
        "敞口计算触发 lot 创建",
    ),
    (
        "M-LP6",
        "backend/paper_portfolio.py",
        "        qty = int(lot[\"remaining_qty\"])\n",
        "        # MUTANT M-LP6: stale mirror quantity overrides the authoritative lot\n"
        "        _mirror_qty = None\n"
        "        for _p in legacy_rows:\n"
        "            if (_p[\"account_id\"], _p[\"code\"]) == (lot[\"account_id\"], lot[\"code\"]):\n"
        "                _mirror_qty = _p.get(\"qty\")\n"
        "                break\n"
        "        qty = int(num(_mirror_qty)) if _mirror_qty is not None else int(lot[\"remaining_qty\"])\n",
        "陈旧镜像数量覆盖权威 lot 数量",
    ),
)

#: 自检哨兵：只改注释。它必须 UNDETECTED —— 否则测试基线本来就是红的，
#: 整个矩阵的结论不成立。
SANITY_MUTATION = (
    "S0",
    ADAPTER,
    "POSITION_EVIDENCE_VERSION = \"position-evidence-v3\"\n",
    "POSITION_EVIDENCE_VERSION = \"position-evidence-v3\"  # sanity\n",
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
    # FL1 对存在性闸门**不敏感**：FIFO 本来就会先取更早的 lot，因此闸门被删掉
    # 后它的结论不变（已实测）。真正依赖该闸门的是"更早的 lot 不够、缺口只能由
    # 未来 lot 补足"这一条，故指名 FL2。
    "M-T1-17": (
        "test_tradability_position_shadow"
        ".FutureLotMustNotSatisfyAnEarlierSell"
        ".test_FUTURE_LOT_2_insufficient_then_future_lot_is_unprovable",
    ),
    "M-T1-18": (
        "test_tradability_position_shadow"
        ".CycleAttributionMustFailClosed"
        ".test_CYCLE_A3_unprovable_start_boundary_fails_closed",
    ),
    "M-T1-19": (
        "test_tradability_position_shadow"
        ".CycleAttributionFailClosedMatrix"
        ".test_CYCLE_FC5_overlapping_proven_window_is_ambiguous",
    ),
    "M-CYCLE-FC1": (
        "test_tradability_position_shadow"
        ".CycleAttributionFailClosedMatrix"
        ".test_CYCLE_FC2_competitor_with_no_boundaries_makes_it_unprovable",
        "test_tradability_position_shadow"
        ".CycleAttributionMustFailClosed"
        ".test_CYCLE_FC7_paused_cycle_without_start_makes_attribution_unprovable",
    ),
    "M-CYCLE-FC2": (
        "test_tradability_position_shadow"
        ".CycleAttributionMustFailClosed"
        ".test_CYCLE_FC7_paused_cycle_without_start_makes_attribution_unprovable",
    ),
    "M-CYCLE-FC3": (
        # created_at 被升级成**竞争周期**的起点 ⇒ 归属退化成 ambiguous，
        # 因此真正抓住它的是 FC2（而不是只覆盖请求周期起点的 FC6）。
        "test_tradability_position_shadow"
        ".CycleAttributionFailClosedMatrix"
        ".test_CYCLE_FC2_competitor_with_no_boundaries_makes_it_unprovable",
        "test_tradability_position_shadow"
        ".CycleAttributionMustFailClosed"
        ".test_CYCLE_FC7_paused_cycle_without_start_makes_attribution_unprovable",
    ),
    "M-CYCLE-FC4": (
        "test_tradability_position_shadow"
        ".CycleAttributionFailClosedMatrix"
        ".test_CYCLE_FC2_competitor_with_no_boundaries_makes_it_unprovable",
    ),
    "M-CYCLE-FC5": (
        "test_tradability_position_shadow"
        ".CycleAttributionFailClosedMatrix"
        ".test_CYCLE_FC8_explicit_order_cycle_id_matching_is_durable_identity",
    ),
    "M-CYCLE-FC6": (
        "test_tradability_position_shadow"
        ".CycleAttributionFailClosedMatrix"
        ".test_CYCLE_FC10_explicit_identity_conflicting_with_window_fails_closed",
    ),
    "M-T1-20": (
        "test_tradability_position_shadow"
        ".SellReplayOrderingUsesRealEventTime"
        ".test_EVENT_ORDER_3_snapshot_separates_before_and_after",
    ),
    # ── v18 订单周期归属（M-OC*）──
    "M-OC1": (
        "test_order_cycle_provenance"
        ".MigrationNoBackfillTests"
        ".test_MIG_CYCLE_3_and_7_legacy_rows_stay_null_and_unchanged",
    ),
    "M-OC2": (
        "test_order_cycle_provenance"
        ".ImmutabilityGuardTests"
        ".test_MIG_CYCLE_8_null_to_cycle_update_is_rejected",
    ),
    "M-OC3": (
        # 实测：archive 少一列时"列顺序相等"断言仍可能全绿（两边都少），
        # 真正抓住它的是 ``ensure_columns`` 的返回契约。
        "test_order_cycle_provenance"
        ".SchemaContractTests"
        ".test_ensure_is_idempotent",
    ),
    "M-OC4": (
        # 实测：去掉引用完整性校验后，NULL 仍会被 `c.id = NULL` 拦下，
        # 因此指名真正观察该分支的用例。
        "test_order_cycle_provenance"
        ".InsertGuardTests"
        ".test_nonexistent_cycle_id_is_rejected",
    ),
    "M-OC5": (
        "test_order_cycle_provenance"
        ".ProductionPrimitiveCycleTests"
        ".test_buy_lot_inherits_its_source_order_cycle_not_the_active_cycle",
    ),
    "M-OC6": (
        "test_order_cycle_provenance"
        ".ProductionPrimitiveCycleTests"
        ".test_lot_consumption_uses_the_explicit_cycle_not_the_active_one",
    ),
    "M-OC7": (
        # 实测：忽略显式 cycle 后 OC6 仍 proven（窗口恰好也能证明），
        # 真正暴露该缺陷的是"跨周期卖出必须被拒绝"的 OC3。
        "test_order_cycle_identity"
        ".CrossCycleIdentityTests"
        ".test_OC3_sell_order_cycle_must_match_the_consumed_lot_cycle",
    ),
    "M-OC8": (
        "test_order_cycle_identity"
        ".ExactTimeFallbackTests"
        ".test_TIME_CYCLE_2_competitor_ended_before_the_sell_is_excluded",
    ),
    "M-CF1": (
        "test_deferred_fill_cycle_binding"
        ".PrimitiveGuardsAreReachableDirectly"
        ".test_record_lot_refuses_a_legacy_source_order",
    ),
    "M-CF2": (
        # 实测：该变异把成交闸门换成当前 active cycle。被测的"拒绝"用例在
        # 变异下依然会拒绝（它读到的是同一个漂移事实），真正转红的是
        # "同周期必须正常成交"这条正对照。
        "test_deferred_fill_cycle_binding"
        ".RealPendingSellPathEndToEnd"
        ".test_same_cycle_deferred_order_still_fills_normally",
    ),
    "M-CF3": (
        # 实测：变异让"归属不可证明"回退到当前 active cycle。commit_fill 在调用
        # 闸门之前自己也读了一次归属并抛异常，把闸门内部这个判断遮蔽了；
        # 直接驱动闸门的用例才是唯一能观测它的测试。
        "test_deferred_fill_cycle_binding"
        ".ExecutionCycleInvariantIsChecked"
        ".test_guard_refuses_an_unprovable_order_directly",
    ),
    "M-CF4": (
        "test_deferred_fill_cycle_binding"
        ".PendingSellStaysInItsOwnCycle"
        ".test_R1_pending_sell_is_refused_when_execution_cycle_changed",
    ),
    "M-CF5": (
        "test_deferred_fill_cycle_binding"
        ".PendingSellStaysInItsOwnCycle"
        ".test_R1_pending_sell_is_refused_when_execution_cycle_changed",
    ),
    "M-CF6": (
        "test_deferred_fill_cycle_binding"
        ".RetryLineageHardConstraint"
        ".test_cross_cycle_retry_does_not_inherit_lineage",
    ),
    "M-CF7": (
        "test_deferred_fill_cycle_binding"
        ".PrimitiveGuardsAreReachableDirectly"
        ".test_consume_lots_refuses_a_missing_cycle",
    ),
    "M-CF8": (
        "test_deferred_fill_cycle_binding"
        ".OrderIdentityMismatchFailsClosed"
        ".test_code_mismatch_is_rejected",
    ),
    "M-CF9": (
        "test_deferred_fill_cycle_binding"
        ".ExecutionCycleInvariantIsChecked"
        ".test_account_cycle_mismatch_is_rejected",
    ),
    "M-CF10": (
        "test_deferred_fill_cycle_binding"
        ".ExecutionCycleInvariantIsChecked"
        ".test_null_account_cycle_is_rejected",
    ),
    "M-CF11": (
        "test_deferred_fill_cycle_binding"
        ".RealPendingSellPathEndToEnd"
        ".test_scan_refuses_pending_buy_after_execution_cycle_changed",
    ),
    "M-CF12": (
        "test_deferred_fill_cycle_binding"
        ".PendingSellStaysInItsOwnCycle"
        ".test_R1_pending_sell_is_refused_when_execution_cycle_changed",
    ),
    "M-CF14": (
        "test_deferred_fill_cycle_binding"
        ".ReservationCycleProvenance"
        ".test_mismatched_reservation_is_not_resized",
    ),
    "M-CF15": (
        "test_deferred_fill_cycle_binding"
        ".RealPendingSellPathEndToEnd"
        ".test_scan_refuses_pending_order_after_execution_cycle_changed",
    ),
    "M-CF16": (
        "test_deferred_fill_cycle_binding"
        ".ReservationCycleMismatchEndToEnd"
        ".test_not_triggered_mismatched_reservation_terminalizes",
    ),
    "M-CF17": (
        "test_deferred_fill_cycle_binding"
        ".ReservationCycleMismatchEndToEnd"
        ".test_triggered_mismatched_reservation_terminalizes",
    ),
    "M-CF18": (
        "test_deferred_fill_cycle_binding"
        ".ReservationCycleMismatchEndToEnd"
        ".test_triggered_mismatched_reservation_terminalizes",
    ),
    "M-CF19": (
        "test_deferred_fill_cycle_binding"
        ".ReservationCycleMismatchEndToEnd"
        ".test_not_triggered_mismatched_reservation_terminalizes",
    ),
    "M-CF20": (
        "test_deferred_fill_cycle_binding"
        ".ReservationCycleMismatchEndToEnd"
        ".test_not_triggered_mismatched_reservation_terminalizes",
    ),
    "M-CF21": (
        # 实测：M-CF21 把释放失败改回静默吞掉。原来指定的终态化用例观测不到它
        # —— 那些用例不会让释放失败。真正能杀掉它的是专门注入释放失败的用例。
        "test_deferred_fill_cycle_binding"
        ".ReservationCycleMismatchEndToEnd"
        ".test_release_failure_is_not_swallowed",
    ),
    # ── Round-9：legacy mirror 不得成为 lot / executable position（§24） ──────
    "M-LP1": (
        "test_legacy_position_rematerialization"
        ".NewCycleMustNotInheritStaleMirror"
        ".test_LP1_new_cycle_gets_no_inferred_lot",
    ),
    "M-LP2": (
        "test_legacy_position_rematerialization"
        ".NewCycleMustNotInheritStaleMirror"
        ".test_LP1_new_cycle_gets_no_inferred_lot",
    ),
    "M-LP3": (
        "test_legacy_position_rematerialization"
        ".LegacyOnlyMirrorIsNotExecutable"
        ".test_LP5_legacy_only_mirror_creates_nothing",
    ),
    "M-LP4": (
        "test_legacy_position_rematerialization"
        ".SyncPositionsFlowsOneWay"
        ".test_LP4b_sync_never_builds_lots_from_mirror",
    ),
    "M-LP5": (
        "test_legacy_position_rematerialization"
        ".ExposureReadCreatesNoLot"
        ".test_LP3_shared_account_exposure_does_not_write_lots",
    ),
    "M-LP6": (
        "test_legacy_position_rematerialization"
        ".LegacyMetadataCompatibility"
        ".test_LP7_authoritative_lot_qty_beats_stale_mirror_qty",
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
    only = None
    if "--only" in argv:
        idx = argv.index("--only")
        raw = argv[idx + 1] if idx + 1 < len(argv) else ""
        only = {token.strip() for token in raw.split(",") if token.strip()}
        if not only:
            print("--only requires a comma-separated list of mutation ids")
            return 2
        unknown = only - {entry[0] for entry in MUTATIONS}
        if unknown:
            print(f"--only names unknown mutations: {sorted(unknown)}")
            return 2
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
        return _run_matrix(only=only)
    finally:
        release_run_lock()


def _run_matrix(only=None) -> int:
    """运行变异矩阵。

    ``only`` 给出变异 id 集合时只跑这些条目，供**隔离 worktree 里的并发 worker**
    使用：每个 worker 拿到一个不相交的子集，各自在自己的 worktree 里改写源码，
    因此「矩阵运行期间不得并行跑测试」这条铁律仍然成立（没有两个进程共享同一份
    源码）。子集模式跳过 sanity 与 baseline 自检 —— 那两项由主进程跑一次即可，
    20 个 worker 各跑一遍纯属浪费。
    """
    entries = [e for e in MUTATIONS if only is None or e[0] in only]
    if only is None:
        if not baseline_is_green():
            print("baseline contract tests are not green; refusing to run the matrix")
            return 1
    results = []
    sanity = "UNDETECTED"
    if only is None:
        sanity = apply_and_run(SANITY_MUTATION)
        print(f"S0 sanity: {sanity} (expected UNDETECTED)")
    for entry in entries:
        outcome = apply_and_run(entry)
        print(f"{entry[0]}: {outcome}  ({entry[4]})")
        results.append((entry[0], outcome))

    equivalent = verify_equivalent() if only is None else 0

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
        len(results) == len(entries)
        and not survived
        and (only is not None or sanity == "UNDETECTED")
        and equivalent == 0
    )
    print("mutation matrix: " + ("PASS" if complete else "FAIL"))
    return 0 if complete else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
