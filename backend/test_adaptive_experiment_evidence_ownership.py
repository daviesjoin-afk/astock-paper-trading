# -*- coding: utf-8 -*-
"""R27-B2C-6 —— adaptive / experiment **owner fact** 的永久回归（EXP-01 ~ EXP-18）。

本文件只验证 **owner 侧**：谁能写那张 ledger、identity 由谁派生、可用性从哪个瞬间来、
核验闭集与 lifecycle status 是否真的分开。adapter 侧的 EXP-19 ~ EXP-28 在
``test_ai_research_strategy_adapter``。

────────────── 本轮要钉死的四条边界 ──────────────

```text
selection candidate production writer       只有 adaptive_selection owner
candidate lifecycle status                  ≠ owner verification
run_date                                    ≠ revision availability
malformed / unprovable row                  fail closed（绝不 {} / run_date / created_at 兜底）
```

────────────── 为什么这些必须是**结构**断言而不是"记得别这么写" ──────────────

``EXP-01/02`` 直接扫 production 源码：只要有人再写一个 ``INSERT INTO
adaptive_selection_candidates``，测试就红 —— 这条不变量不能靠 code review 维护。
``EXP-04`` / ``EXP-18`` 直接断言两个词表**不相交**，因此"把 ``applied`` 读成 verified"、
"把 ``pit_status=verified`` 读成 owner verified"在词汇层面就不可表达。
"""
from __future__ import annotations

import dataclasses
import ast
import inspect
import json
import os
import re
import sqlite3
import sys
import unittest
from unittest import mock

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import adaptive_risk as AR  # noqa: E402
import adaptive_selection as ASEL  # noqa: E402
import ai_research_strategy_adapter as ADAPTER  # noqa: E402
import deepseek_advisor as DA  # noqa: E402
import learning_dataset as LD  # noqa: E402
import learning_evaluation as LE  # noqa: E402

SELECTION_TABLE = "adaptive_selection_candidates"
RISK_TABLE = "adaptive_risk_candidates"

#: 写候选 ledger 的 SQL 动词。任何一处出现在非 owner 模块里都是 owner split 复发。
_WRITE_VERB = re.compile(
    r"\b(INSERT(?:\s+OR\s+\w+)?|UPDATE|DELETE|REPLACE)\b[\s\S]{0,80}?\bINTO\s+",
    re.IGNORECASE,
)


def _production_modules() -> list[str]:
    return sorted(
        name for name in os.listdir(BACKEND)
        if name.endswith(".py") and not name.startswith("test_")
    )


def _source(name: str) -> str:
    with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
        return handle.read()


def _candidate_table_writers(table: str) -> set[str]:
    """生产里对 ``table`` 发起写语句的模块集合（INSERT / UPDATE / DELETE / REPLACE）。"""
    pattern = re.compile(rf"\b(?:INSERT|UPDATE|DELETE|REPLACE)\b[\s\S]{{0,60}}?\b{table}\b")
    writers = set()
    for name in _production_modules():
        if pattern.search(_source(name)):
            writers.add(name)
    return writers


def _imported_roots(name: str) -> set[str]:
    """模块**真正 import** 的顶层模块名。

    刻意用 AST 而不是子串搜索：文档里点名一个模块（说明"这里刻意不读它"）与真的 import 它
    是两件完全不同的事，子串搜索会把前者误报成后者。
    """
    roots: set[str] = set()
    for node in ast.walk(ast.parse(_source(name))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


# ───────────────────────────── fixtures ─────────────────────────────

#: 本模块建的每个 SQLite 连接。未关闭的连接会在解释器收尾时发出 ``ResourceWarning``，
#: 污染 harness 的 stderr 分类（``_is_fake_kill`` 依赖 stderr 干净）。
_OPEN_CONNECTIONS: list = []


def _track(conn: sqlite3.Connection) -> sqlite3.Connection:
    _OPEN_CONNECTIONS.append(conn)
    return conn


class _DbTestCase(unittest.TestCase):
    """在 ``tearDown`` 里关掉本用例建的所有连接。"""

    def tearDown(self) -> None:
        while _OPEN_CONNECTIONS:
            try:
                _OPEN_CONNECTIONS.pop().close()
            except Exception:  # pragma: no cover - close 失败不该掩盖真失败
                pass


def _selection_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ASEL.ensure_schema(conn)
    return _track(conn)


def _risk_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    AR.ensure_schema(conn)
    return _track(conn)


def _adaptive_conn() -> sqlite3.Connection:
    """tuner 用的 adaptive 库：同时具备 advisor 记账表与 selection candidate ledger。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    DA.ensure_schema(conn)
    ASEL.ensure_schema(conn)
    return _track(conn)


def _weights_patch(model_id: str = "one_to_two", *, spread: float = 0.02):
    """在有界幅度内造一个纯因子权重补丁（键集与 BASE_WEIGHTS 完全一致）。"""
    baseline = {"weights": dict(ASEL.BASE_WEIGHTS[model_id])}
    keys = sorted(baseline["weights"])
    candidate = dict(baseline["weights"])
    first, second = keys[0], keys[1]
    candidate[first] = round(candidate[first] + spread, 6)
    candidate[second] = round(candidate[second] - spread, 6)
    return baseline, {"weights": candidate}


def _insert_selection(conn, *, owner_written=True, **overrides) -> int:
    """直接写一行 selection candidate。

    ``owner_written=True``（默认）会带上 owner 的 durable 来源记号，代表"这条行是 owner
    写路径产出的"。要构造 **R27-B2C-6 之前第二 writer 的行**（旧的 ``deepseek_advisor``
    裸 INSERT）就必须显式 ``owner_written=False`` —— 那个区别是本轮 review 抓出的核心
    不变量，所以它必须是调用点上的**显式选择**，不能由 helper 悄悄补齐。
    """
    row = {
        "run_date": "2026-09-18", "account_id": "tq_breakout", "regime": "momentum",
        "model_id": "one_to_two", "baseline_params": "{}", "candidate_params": "{}",
        "evidence": (
            json.dumps({ASEL.SELECTION_OWNER_ORIGIN_KEY: ASEL.SELECTION_OWNER_ORIGIN_MARKER})
            if owner_written else "{}"
        ),
        "status": "shadow_candidate", "tier": "micro",
        "reason": "r", "created_at": "2026-09-18T09:00:00+08:00",
        "updated_at": "2026-09-21T09:00:00+08:00",
    }
    row.update(overrides)
    cursor = conn.execute(
        f"""INSERT INTO {SELECTION_TABLE}(
           run_date,account_id,regime,model_id,baseline_params,candidate_params,evidence,
           status,tier,reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (row["run_date"], row["account_id"], row["regime"], row["model_id"],
         row["baseline_params"], row["candidate_params"], row["evidence"], row["status"],
         row["tier"], row["reason"], row["created_at"], row["updated_at"]),
    )
    return int(cursor.lastrowid)


def _insert_risk(conn, **overrides) -> int:
    row = {
        "run_date": "2026-09-18", "account_id": "tq_breakout", "regime": "momentum",
        "baseline_params": "{}", "candidate_params": "{}", "evidence": "{}",
        "risk_reduction_pct": 1.25, "change_kind": "conservative_tighten",
        "status": "shadow_candidate", "application_mode": "shadow", "reason": "r",
        "created_at": "2026-09-18T09:00:00+08:00",
        "updated_at": "2026-09-21T09:00:00+08:00",
    }
    row.update(overrides)
    cursor = conn.execute(
        f"""INSERT INTO {RISK_TABLE}(
           run_date,account_id,regime,baseline_params,candidate_params,evidence,
           risk_reduction_pct,change_kind,status,application_mode,reason,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (row["run_date"], row["account_id"], row["regime"], row["baseline_params"],
         row["candidate_params"], row["evidence"], row["risk_reduction_pct"],
         row["change_kind"], row["status"], row["application_mode"], row["reason"],
         row["created_at"], row["updated_at"]),
    )
    return int(cursor.lastrowid)


def _learning_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    LD.ensure_schema(conn)
    LE.ensure_schema(conn)
    return _track(conn)


def _dataset_manifest(conn, *, cutoff="2026-09-20") -> str:
    manifest = LD.build_manifest(
        {}, exclusions={}, cutoff=cutoff, feature_names=["mom_short"],
        horizon_semantics=LD.HORIZON_SEMANTICS_OBSERVED_CLOSE_STEPS,
        split_spec=LD.DEFAULT_SPLIT_SPEC, source_row_count=0,
    )
    if not LD.persist_manifest(conn, manifest):
        raise RuntimeError("dataset manifest fixture was not persisted")
    return manifest["dataset_fingerprint"]


#: 结果产生瞬间刻意**晚于**数据集 cutoff —— 9/21 跑出来的评估不能声明 9/20 可用。
EVALUATION_RESULT_AT = "2026-09-21T02:00:00+00:00"


def _evaluation_manifest(dataset_fingerprint, **overrides) -> dict:
    manifest = {
        "evaluation_fingerprint": "e" * 64,
        "evaluation_schema_version": LE.EVALUATION_SCHEMA_VERSION,
        "evaluation_contract_version": LE.EVALUATION_CONTRACT_VERSION,
        "metric": LE.METRIC_SPEARMAN_RANK_IC,
        "dataset_fingerprint": dataset_fingerprint,
        "model_id": "shadow-a",
        "holdout_partition": "test",
        "scoring": {"kind": "rank", "sign": 1},
        "prediction_digest": "d" * 64,
        "evaluated_dates": 5,
        "evaluated_rows": 20,
        "dropped_dates": 0,
        "mean_rank_ic": "0.120000",
        "ic_std": "0.050000",
        "ic_std_error": "0.022360",
        "ic_lower_bound": "0.075000",
        "confidence_label": "research_only",
        "confidence_z": "1.960000",
        "per_date_ic": {"2026-01-05": "0.1", "2026-01-06": "0.14"},
        "exclusion_reasons": {"none": 0},
        "evaluation_blockers": [],
        "evaluation_contract_ok": True,
        "execution_authority": "none",
        "evaluation_scope": "research_read_only",
        "created_at": EVALUATION_RESULT_AT,
        "model_version": "2026.09",
        "model_artifact_fingerprint": "a" * 64,
        "training_dataset_fingerprint": "t" * 64,
        "trained_through": "2026-08-31",
        "selection_partition": "validation",
        "hyperparameters_fingerprint": "h" * 64,
        "random_seed": "7",
        "provenance_fingerprint": "p" * 64,
        "coverage": {
            "coverage_ratio": 1.0, "expected_prediction_rows": 20,
            "observed_prediction_rows": 20, "missing_prediction_rows": 0,
        },
        "holdout": {
            "holdout_date_count": 2, "holdout_mean_rank_ic": 0.11,
            "holdout_positive_ratio": 1.0, "holdout_start_date": "2026-01-08",
            "holdout_end_date": "2026-01-09", "holdout_valid_date_count": 2,
            "holdout_undefined_date_count": 0, "holdout_undefined_dates": [],
            "canonical_test_date_count": 5, "valid_ic_date_count": 5,
            "undefined_ic_date_count": 0, "undefined_ic_dates": [],
        },
    }
    manifest.update(overrides)
    return manifest


def _persist_evaluation(conn, dataset_fingerprint, **overrides) -> str:
    manifest = _evaluation_manifest(dataset_fingerprint, **overrides)
    if not LE.persist_evaluation_manifest(conn, manifest):
        raise RuntimeError("evaluation manifest fixture was not persisted")
    return manifest["evaluation_fingerprint"]


class SelectionWriterConvergenceTests(_DbTestCase):
    """EXP-01 ~ EXP-03：先收敛 owner，才谈 typed 投影。"""

    def test_EXP_01_selection_candidate_production_writer_is_the_owner_only(self):
        """EXP-01：生产里写 ``adaptive_selection_candidates`` 的模块只有 owner 自己。

        R27-B2C-6 之前这张表有**两个**生产 writer（``adaptive_selection._upsert`` 与
        ``deepseek_advisor`` 的裸 INSERT），因此连"单一 owner"都不成立。这条断言让
        writer 收敛**可执行**：任何一处新的裸写都会让集合变大而红。
        """
        writers = _candidate_table_writers(SELECTION_TABLE)
        self.assertEqual(
            {"adaptive_selection.py"}, writers,
            f"selection candidate ledger 的写者集合发生变化：{sorted(writers)}",
        )
        # 非空性：扫描器必须真的看得见 owner 的那几条写语句（否则空集断言会空转）。
        owner_source = _source("adaptive_selection.py")
        self.assertIn("INSERT INTO adaptive_selection_candidates", owner_source)
        self.assertIn("UPDATE adaptive_selection_candidates", owner_source)

    def test_EXP_02_deepseek_advisor_never_writes_the_selection_ledger(self):
        """EXP-02：``deepseek_advisor`` 不直接 INSERT（也不再提到该表）。

        它现在是 **producer / caller**：提出 bounded proposal，然后调用 owner 的窄接口。
        owner 才是 ledger owner。这条用"整个模块名都不出现"这一更强的形式断言，避免只掐
        住某一种 INSERT 拼法而留下 ``INSERT OR REPLACE`` / 多行拼接的缺口。
        """
        source = _source("deepseek_advisor.py")
        self.assertNotIn(
            SELECTION_TABLE, source,
            "deepseek_advisor 又在直接操作 selection ledger —— proposal producer 不得成为 owner",
        )
        self.assertIn(
            "record_shadow_proposal", source,
            "deepseek_advisor 必须通过 owner 的窄接口持久化影子候选",
        )

    def test_EXP_03_ai_proposal_still_cannot_auto_apply(self):
        """EXP-03：收敛 writer **没有**顺手打开 auto apply，human apply 边界逐字未变。

        三条一起断言：(a) owner 窄接口签发的生命周期是 ``shadow_proposal``；(b) 该状态
        **不在** apply 允许的资格集合里，因此 ``apply_candidate`` 会拒绝；(c) owner 的
        apply 资格集合本身没有被改动。
        """
        conn = _selection_conn()
        baseline, candidate = _weights_patch()
        candidate_id = ASEL.record_shadow_proposal(
            conn, run_date="2026-09-18", account_id="tq_breakout",
            regime="momentum:ai:1030", model_id="one_to_two",
            baseline_params=baseline, candidate_params=candidate,
            evidence={"source": "DeepSeek"}, reason="bounded",
            now="2026-09-21T09:10:00+08:00",
        )
        status = conn.execute(
            f"SELECT status,tier FROM {SELECTION_TABLE} WHERE id=?", (candidate_id,),
        ).fetchone()
        self.assertEqual(ASEL.SHADOW_PROPOSAL_STATUS, status["status"])
        self.assertEqual(ASEL.AI_REALTIME_TIER, status["tier"])
        # apply 资格门禁本身逐字未变 —— 收敛 writer 不得顺手放宽它。
        source = _source("adaptive_selection.py")
        for eligible in ("eligible_auto_adjust", "eligible_manual_review",
                         "eligible_structural_review"):
            self.assertIn(f'"{eligible}"', source)
        with self.assertRaises(ValueError):
            ASEL.apply_candidate(conn, "unused-paper.db", candidate_id, lambda: "now")
        # 资格集合仍是那三个 —— 收敛 writer 不得扩大 apply authority。
        with self.assertRaises(ValueError):
            ASEL.apply_candidate(conn, "unused-paper.db", candidate_id, lambda: "now",
                                 approved_by="bounded-auto")

    def test_EXP_03b_shadow_proposal_is_append_only_and_never_rewrites_lifecycle(self):
        """EXP-03（补充）：提案是**追加**的，绝不把已 apply / 回滚的候选改回影子态。

        否则一次 AI 提案就能把一条已经生效的候选"洗白"成新的影子候选。
        """
        conn = _selection_conn()
        baseline, candidate = _weights_patch()
        args = dict(
            run_date="2026-09-18", account_id="tq_breakout", regime="momentum:ai:1030",
            model_id="one_to_two", baseline_params=baseline, candidate_params=candidate,
            evidence={"source": "DeepSeek"}, reason="bounded",
        )
        first = ASEL.record_shadow_proposal(conn, now="2026-09-21T09:10:00+08:00", **args)
        conn.execute(
            f"UPDATE {SELECTION_TABLE} SET status='applied',updated_at=? WHERE id=?",
            ("2026-09-22T09:00:00+08:00", first),
        )
        again = ASEL.record_shadow_proposal(conn, now="2026-09-23T09:10:00+08:00", **args)
        self.assertEqual(first, again)
        row = conn.execute(
            f"SELECT status,updated_at FROM {SELECTION_TABLE} WHERE id=?", (first,),
        ).fetchone()
        self.assertEqual("applied", row["status"])
        self.assertEqual("2026-09-22T09:00:00+08:00", row["updated_at"])

    def test_EXP_03c_shadow_proposal_rejects_anything_but_a_bounded_factor_patch(self):
        """EXP-03（补充）：阈值 / 条件 / 入场路径 / 越界幅度一律被 owner 拒绝。

        这些字段若被记成候选，就会留下一个"以后可能被 apply"的 bad row。owner 在**入口**
        拒绝它们，而不是把它们持久化后指望下游过滤。
        """
        conn = _selection_conn()
        baseline, candidate = _weights_patch()
        common = dict(
            run_date="2026-09-18", account_id="tq_breakout", regime="momentum:ai:1030",
            model_id="one_to_two", baseline_params=baseline,
            evidence={"source": "DeepSeek"}, reason="x", now="2026-09-21T09:10:00+08:00",
        )
        too_wide = {"weights": dict(candidate["weights"])}
        keys = sorted(too_wide["weights"])
        too_wide["weights"][keys[0]] = round(too_wide["weights"][keys[0]] + 0.10, 6)
        for label, bad in (
            ("conditions", {"weights": candidate["weights"], "conditions": {"pct_high": 6.0}}),
            ("entry_paths", {"weights": candidate["weights"], "entry_paths": {"normal": True}}),
            ("threshold", {"entry_score_delta": 0.02}),
            ("out of bound", too_wide),
        ):
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    ASEL.record_shadow_proposal(conn, candidate_params=bad, **common)
        self.assertEqual(
            0, conn.execute(f"SELECT COUNT(*) FROM {SELECTION_TABLE}").fetchone()[0],
            "被拒绝的提案不得留下任何行",
        )
        # 非空性对照：合法的纯因子补丁必须被接受，否则上面全是"什么都拒绝"。
        accepted = ASEL.record_shadow_proposal(conn, candidate_params=candidate, **common)
        self.assertIsInstance(accepted, int)


    def test_EXP_03d_an_occupied_slot_with_different_content_is_an_explicit_conflict(self):
        """EXP-03d：槽位被**另一条**候选占用时抛冲突，而不是静默返回既有 id。

        这是 R27-B2C-6 review 抓出的第二个 blocker：第一版对"槽位已存在"一律
        ``return existing["id"]``，于是本次 proposal 根本没落库，runtime 却继续报告
        "仅保存候选"。收敛前旧逻辑遇到 UNIQUE 冲突至少会失败，收敛后如果静默吞掉就比收敛前
        更危险 —— API 层的成功与实际 durable 状态被分开了。

        三种情形必须给出**不同**结论：同一提案重放 → 幂等成功；内容不同 → 抛冲突；
        冲突路径不得改写既有行、也不得留下新行。
        """
        conn = _selection_conn()
        baseline, candidate = _weights_patch()
        common = dict(
            run_date="2026-09-18", account_id="tq_breakout", regime="momentum:ai:1030",
            model_id="one_to_two", baseline_params=baseline,
            evidence={"source": "DeepSeek"}, reason="bounded",
        )
        first = ASEL.record_shadow_proposal(
            conn, candidate_params=candidate, now="2026-09-21T09:10:00+08:00", **common,
        )
        before = dict(conn.execute(
            f"SELECT * FROM {SELECTION_TABLE} WHERE id=?", (first,),
        ).fetchone())

        # 同一个提案（内容逐字相同）→ 幂等成功，返回同一个 id，不新增行。
        _same_baseline, same_candidate = _weights_patch(spread=0.02)
        self.assertEqual(
            first,
            ASEL.record_shadow_proposal(
                conn, candidate_params=same_candidate, now="2026-09-21T09:20:00+08:00", **common,
            ),
        )

        # 内容不同 → 显式冲突，且**什么都不写**。
        _other_baseline, other_candidate = _weights_patch(spread=0.01)
        with self.assertRaises(ASEL.SelectionProposalConflict):
            ASEL.record_shadow_proposal(
                conn, candidate_params=other_candidate, now="2026-09-21T09:30:00+08:00", **common,
            )
        self.assertEqual(
            1, conn.execute(f"SELECT COUNT(*) FROM {SELECTION_TABLE}").fetchone()[0],
            "冲突的提案不得留下任何行",
        )
        after = dict(conn.execute(
            f"SELECT * FROM {SELECTION_TABLE} WHERE id=?", (first,),
        ).fetchone())
        self.assertEqual(before, after, "冲突不得改写既有行（含 lifecycle 与 updated_at）")

    def test_EXP_03e_the_tuner_reports_a_slot_conflict_instead_of_claiming_it_saved(self):
        """EXP-03e：``run_realtime_tuning`` 必须把冲突记成 not persisted。

        owner 只在"同一条提案的幂等重放"时返回既有 id，内容不同则抛
        :class:`SelectionProposalConflict`。若 runtime 把这个异常吞掉后继续报告
        "仅保存候选"，审计就会看到一次**没有发生**的持久化 —— 这正是 blocker 2 的形态。
        本测试真实驱动 tuner（只 mock 掉 provider / evidence 这两个外部边界），断言：
        部分冲突时理由必须写明 1/2 未持久化；全部冲突时状态必须是 ``proposal_conflict``
        且不得声称保存。
        """
        conn = _adaptive_conn()
        baseline, candidate = _weights_patch()
        _other_baseline, other_candidate = _weights_patch(spread=0.01)
        profile = {"profile_date": "2026-09-18", "regime": "momentum",
                   "quality": "valid_close", "valid_rows": 5000}
        accounts = [{"account_id": "tq_breakout", "version": "v1", "style": "s",
                     "weights": {}, "entry_score_delta": 0.0, "conditions": {}}]

        def response_for(*candidates):
            return {"decision": "propose", "confidence": 90, "proposals": [
                {"account_id": "tq_breakout", "reason": "r", "weights": item["weights"]}
                for item in candidates
            ]}

        def run(target_conn, response):
            with mock.patch.object(DA, "enabled", return_value=True), \
                    mock.patch.object(DA, "configured", return_value=True), \
                    mock.patch.object(DA, "_now", return_value="2026-09-18T10:30:00+08:00"), \
                    mock.patch.object(DA, "_tuning_accounts", return_value=accounts), \
                    mock.patch.object(
                        DA, "collect_evidence",
                        return_value=({"market_snapshot": {}}, "h" * 8)), \
                    mock.patch.object(DA, "call_json", return_value=(response, 0, 0)):
                return DA.run_realtime_tuning(
                    lambda: target_conn, "unused-paper.db", [],
                    config={"llm_realtime_require_cross_source": False},
                    profile=profile,
                )

        # (a) 两条内容不同的提案落在同一个槽位 → 1 条持久化、1 条冲突。
        result = run(conn, response_for(candidate, other_candidate))
        self.assertEqual(1, len(result["persisted_ids"]))
        self.assertEqual(1, len(result["conflicts"]))
        self.assertIn("1/2", result["reason"])
        self.assertIn("未持久化", result["reason"])
        self.assertNotIn("仅保存候选，未同日应用", result["reason"])
        self.assertEqual(
            1, conn.execute(f"SELECT COUNT(*) FROM {SELECTION_TABLE}").fetchone()[0],
        )

        # (b) 槽位已被一条内容不同的 owner 候选占用 → 全部冲突，状态必须显式。
        #     用新的库，避免 (a) 已经占用了同一个槽位。
        blocked_conn = _adaptive_conn()
        ASEL.record_shadow_proposal(
            blocked_conn, run_date="2026-09-18", account_id="tq_breakout",
            regime="momentum:ai:1030", model_id="one_to_two",
            baseline_params=baseline, candidate_params=candidate,
            evidence={"source": "other"}, reason="occupied",
            now="2026-09-18T10:29:00+08:00",
        )
        blocked = run(blocked_conn, response_for(other_candidate))
        self.assertEqual("proposal_conflict", blocked["status"])
        self.assertEqual([], blocked["applied_ids"])
        self.assertEqual([], blocked.get("persisted_ids", []))
        self.assertTrue(blocked["conflicts"])
        self.assertEqual(
            1, blocked_conn.execute(f"SELECT COUNT(*) FROM {SELECTION_TABLE}").fetchone()[0],
            "冲突路径不得写入任何行",
        )


    def test_EXP_03f_idempotency_must_cover_the_whole_factual_payload(self):
        """EXP-03f：幂等判据必须覆盖**完整 factual payload**（含 ``evidence`` / ``reason``）。

        review 抓出的剩余缺口：owner fact 自己把 ``evidence_canonical`` 与 ``reason`` 纳入了
        ``content_fingerprint``（也就是 owner 已声明"这两个字段变化 ⇒ 事实内容变化"），但
        写入口一度只用"weights 相同"定义"同一个请求"。于是同一次重放会在两个 durable 层留下
        **不一致**的描述：

        ```text
        旧 candidate: weights=W, evidence=E1, reason=R1
        新 proposal:  weights=W, evidence=E2, reason=R2
        → 返回旧 id（幂等成功），E2/R2 没有写进候选 ledger
        → 但本次 tuning runtime 把 W+E2+R2 记进 adaptive_ai_tuning_runs
        ```

        这与"没有真正保存却报告保存"是同一类缺陷。本测试用**单字段隔离矩阵**钉住它：
        candidate 权重、baseline、evidence、reason 四个 factual 字段**各自**变化都必须冲突；
        四个全部一致才允许幂等成功（正向非空性）。
        """
        conn = _selection_conn()
        baseline, candidate = _weights_patch()
        base_evidence = {"source": "DeepSeek", "confidence": 90, "evidence_hash": "A"}
        base_reason = "R1"
        common = dict(
            run_date="2026-09-18", account_id="tq_breakout", regime="momentum:ai:1030",
            model_id="one_to_two",
        )
        first = ASEL.record_shadow_proposal(
            conn, baseline_params=baseline, candidate_params=candidate,
            evidence=base_evidence, reason=base_reason,
            now="2026-09-21T09:10:00+08:00", **common,
        )

        # 正向非空性：四个 factual 字段全部一致 → 幂等成功（同一个 id，不新增行）。
        _same_baseline, same_candidate = _weights_patch(spread=0.02)
        self.assertEqual(
            first,
            ASEL.record_shadow_proposal(
                conn, baseline_params=dict(baseline), candidate_params=same_candidate,
                evidence={"confidence": 90, "evidence_hash": "A", "source": "DeepSeek"},
                reason="R1", now="2026-09-21T09:20:00+08:00", **common,
            ),
            "factual payload 完全一致时必须幂等成功（键序不同不算差异）",
        )
        self.assertEqual(
            1, conn.execute(f"SELECT COUNT(*) FROM {SELECTION_TABLE}").fetchone()[0],
        )

        # 单字段隔离矩阵：任一 factual 字段变化 → 显式冲突。
        _other_baseline, other_candidate = _weights_patch(spread=0.01)
        shifted_baseline = {"weights": dict(baseline["weights"]), "entry_score_delta": 0.02}
        for label, overrides in (
            ("candidate_params", {"candidate_params": other_candidate}),
            ("baseline_params", {"baseline_params": shifted_baseline}),
            ("evidence", {"evidence": {"source": "DeepSeek", "confidence": 80,
                                       "evidence_hash": "B"}}),
            ("reason", {"reason": "R2"}),
        ):
            with self.subTest(field=label):
                payload = dict(
                    baseline_params=baseline, candidate_params=candidate,
                    evidence=base_evidence, reason=base_reason,
                )
                payload.update(overrides)
                with self.assertRaises(ASEL.SelectionProposalConflict):
                    ASEL.record_shadow_proposal(
                        conn, now="2026-09-21T09:30:00+08:00", **{**common, **payload},
                    )
        self.assertEqual(
            1, conn.execute(f"SELECT COUNT(*) FROM {SELECTION_TABLE}").fetchone()[0],
            "冲突路径不得留下任何行",
        )

    def test_EXP_03g_the_runtime_never_reuses_a_row_for_a_different_proposal(self):
        """EXP-03g：同一槽位里 evidence / reason 变化时，runtime 必须报冲突而不是复用旧行。

        端到端版本（review 给的场景）：两次 tuning 运行命中**同一个槽位**、**同样的 weights**，
        但 `evidence_hash` 与 `reason` 不同。第一版会把这判成"同一个请求"，返回旧 id ——
        于是候选 ledger 里留着 E1/R1，而 `adaptive_ai_tuning_runs` 记着 E2/R2，两层对"这次
        持久化了什么"说法不一致。

        现在第二次必须 `proposal_conflict`，且候选 ledger **仍然只有一行**（E1/R1 未被静默
        替换，也没有多出第二行）。同时断言第一次落库的 factual payload 确实就是它报告的那份。
        """
        conn = _adaptive_conn()
        _baseline, candidate = _weights_patch()
        profile = {"profile_date": "2026-09-18", "regime": "momentum",
                   "quality": "valid_close", "valid_rows": 5000}
        accounts = [{"account_id": "tq_breakout", "version": "v1", "style": "s",
                     "weights": {}, "entry_score_delta": 0.0, "conditions": {}}]

        def run(evidence_hash, reason):
            response = {"decision": "propose", "confidence": 90, "proposals": [
                {"account_id": "tq_breakout", "reason": reason, "weights": candidate["weights"]},
            ]}
            with mock.patch.object(DA, "enabled", return_value=True), \
                    mock.patch.object(DA, "configured", return_value=True), \
                    mock.patch.object(DA, "_now", return_value="2026-09-18T10:30:00+08:00"), \
                    mock.patch.object(DA, "_tuning_accounts", return_value=accounts), \
                    mock.patch.object(DA, "collect_evidence",
                                      return_value=({"market_snapshot": {}}, evidence_hash)), \
                    mock.patch.object(DA, "call_json", return_value=(response, 0, 0)):
                return DA.run_realtime_tuning(
                    lambda: conn, "unused-paper.db", [],
                    config={"llm_realtime_require_cross_source": False}, profile=profile,
                )

        first = run("H" * 8, "R1")
        self.assertEqual(1, len(first["persisted_ids"]))
        self.assertEqual([], first["conflicts"])
        fact = ASEL.selection_candidate_fact(conn, first["persisted_ids"][0], as_of="2026-09-18")
        self.assertEqual("R1", fact.reason)
        self.assertEqual("H" * 8, fact.evidence["evidence_hash"])
        self.assertEqual(90.0, fact.evidence["confidence"])
        self.assertEqual(
            candidate["weights"], fact.candidate_params["weights"],
            "落库候选的权重必须就是本次提案请求的权重",
        )
        self.assertEqual(
            ASEL.SELECTION_OWNER_ORIGIN_MARKER,
            fact.evidence[ASEL.SELECTION_OWNER_ORIGIN_KEY],
            "来源记号必须落在同一份 evidence 里，不引入第二套 metadata",
        )

        # 同一槽位、同一 weights，但 evidence_hash 与 reason 不同 → 不得复用旧行。
        second = run("G" * 8, "R2")
        self.assertEqual("proposal_conflict", second["status"])
        self.assertEqual([], second.get("persisted_ids", []))
        self.assertTrue(second["conflicts"])
        self.assertEqual(
            1, conn.execute(f"SELECT COUNT(*) FROM {SELECTION_TABLE}").fetchone()[0],
            "冲突路径不得新增行，也不得改写既有行",
        )
        still = ASEL.selection_candidate_fact(conn, first["persisted_ids"][0], as_of="2026-09-18")
        self.assertEqual("R1", still.reason)
        self.assertEqual("H" * 8, still.evidence["evidence_hash"])


class CandidateLifecycleIsNotVerificationTests(_DbTestCase):
    """EXP-04：lifecycle status 不是核验。"""

    def test_EXP_04_candidate_lifecycle_status_never_changes_verification(self):
        """EXP-04：遍历 owner 能签发的**每一个** lifecycle status，核验结论都不变。

        这是本轮最容易被写错的一条：把 ``eligible_auto_tighten`` 或 ``applied`` 读成
        "owner verified"。两个词表**零交集**，因此它在词汇层面就不可表达。
        """
        self.assertEqual(
            frozenset(), frozenset(ASEL.SELECTION_LIFECYCLE_STATUSES)
            & frozenset(ASEL.SELECTION_FACT_VERIFICATION_STATUSES),
            "selection lifecycle 词表与核验闭集必须不相交",
        )
        self.assertEqual(
            frozenset(), frozenset(AR.RISK_LIFECYCLE_STATUSES)
            & frozenset(AR.RISK_FACT_VERIFICATION_STATUSES),
            "risk lifecycle 词表与核验闭集必须不相交",
        )

        conn = _selection_conn()
        for status in ASEL.SELECTION_LIFECYCLE_STATUSES:
            with self.subTest(status=status):
                candidate_id = _insert_selection(
                    conn, status=status, regime=f"r-{status}",
                )
                fact = ASEL.selection_candidate_fact(conn, candidate_id, as_of="2026-09-21")
                self.assertEqual(
                    ASEL.SELECTION_FACT_RECORDED, fact.fact_verification_status,
                    f"lifecycle {status!r} 不得改变 owner 核验结论",
                )
                self.assertEqual(status, fact.lifecycle_status)

        rconn = _risk_conn()
        for status in AR.RISK_LIFECYCLE_STATUSES:
            with self.subTest(status=status):
                candidate_id = _insert_risk(rconn, status=status, regime=f"r-{status}")
                fact = AR.risk_candidate_fact(rconn, candidate_id, as_of="2026-09-21")
                self.assertEqual(AR.RISK_FACT_RECORDED, fact.fact_verification_status)

    def test_EXP_04b_unknown_vocabulary_is_unproven_not_verified(self):
        """EXP-04（补充）：用了 owner 不签发的词汇 → ``unproven``，而不是"照样可信"。

        这条同时给 ``unproven`` 一个**非空**的使用场景，避免它成为永不触发的装饰态。
        """
        conn = _selection_conn()
        cases = (
            {"account_id": "not_an_owner_account"},
            {"model_id": "not_a_model"},
            {"status": "some_other_tool_status"},
            {"tier": "not_a_tier"},
        )
        for index, override in enumerate(cases):
            with self.subTest(override=override):
                candidate_id = _insert_selection(conn, regime=f"r{index}", **override)
                fact = ASEL.selection_candidate_fact(conn, candidate_id, as_of="2026-09-21")
                self.assertEqual(ASEL.SELECTION_FACT_OWNER_UNPROVEN, fact.fact_verification_status)

        rconn = _risk_conn()
        for index, override in enumerate((
            {"account_id": "not_an_owner_account"},
            {"status": "some_other_tool_status"},
            {"change_kind": "not_a_kind"},
        )):
            with self.subTest(override=override):
                candidate_id = _insert_risk(rconn, regime=f"r{index}", **override)
                fact = AR.risk_candidate_fact(rconn, candidate_id, as_of="2026-09-21")
                self.assertEqual(AR.RISK_FACT_OWNER_UNPROVEN, fact.fact_verification_status)

    def test_EXP_04c_application_mode_is_not_part_of_the_risk_origin_proof(self):
        """EXP-04（补充）：``application_mode`` 承载的是**审批者身份**，不是 owner 词汇。

        apply 路径会把它覆写成 ``approved_by``。若把它当成来源证据，就会制造一个假的核验
        维度：一个运维账号名会让一条事实看起来"更可信"。
        """
        rconn = _risk_conn()
        candidate_id = _insert_risk(rconn, application_mode="zhang.san@ops")
        fact = AR.risk_candidate_fact(rconn, candidate_id, as_of="2026-09-21")
        self.assertEqual(AR.RISK_FACT_RECORDED, fact.fact_verification_status)
        self.assertEqual("zhang.san@ops", fact.application_mode)
        self.assertNotIn("zhang.san@ops", fact.fact_verification_status)

    def test_EXP_04d_legacy_second_writer_rows_are_unproven_not_recorded(self):
        """EXP-04d：历史**第二 writer**的行必须是 ``unproven``，哪怕词汇完全合法。

        这是 R27-B2C-6 review 抓出的第一个 blocker，也是本轮最重要的一条。收敛前这张表有
        两个生产 writer，而旧的 ``deepseek_advisor`` 直写行用的正是
        ``status='shadow_proposal'`` / ``tier='ai_realtime'`` —— 两个值现在都是 owner 的
        合法词汇。第一版只用**词汇归属**做判据，于是这些行会被判成
        ``selection_candidate_recorded`` → ``is_verified = True``：新 owner **反向认证**了
        一批它明确没有独占写入的行。

        词表回答的是"这行看起来像 owner 的产物"，不是"这行是 owner 写的"。因此 writer
        origin 必须来自一条只有 owner 写路径会盖的 durable 记号；本测试把两个历史 writer
        的形状**都**钉住，并附一条正向对照（owner 行仍然 ``recorded``），证明这不是"什么都
        判 unproven"。
        """
        conn = _selection_conn()
        # 旧 deepseek_advisor 裸 INSERT 的形状（含它写下的 status / tier）。
        legacy_ai = _insert_selection(
            conn, owner_written=False, regime="momentum:ai:0900",
            status="shadow_proposal", tier="ai_realtime",
            candidate_params='{"weights":{"flow":0.25}}',
        )
        # 旧 owner ``_upsert`` 的行同样在记号引入之前 —— 无法与上一行区分，因此也必须 unproven。
        legacy_owner = _insert_selection(
            conn, owner_written=False, regime="momentum",
            status="eligible_auto_adjust", tier="micro",
        )
        for label, candidate_id in (("legacy ai direct write", legacy_ai),
                                    ("pre-marker owner row", legacy_owner)):
            with self.subTest(row=label):
                fact = ASEL.selection_candidate_fact(conn, candidate_id, as_of="2026-09-21")
                self.assertEqual(
                    ASEL.SELECTION_FACT_OWNER_UNPROVEN, fact.fact_verification_status,
                    "没有 owner 来源记号的行不得被认证成 owner 事实",
                )
                ref = ADAPTER.evidence_ref_from_strategy_projection(fact)
                self.assertFalse(ref.is_verified)

        # 正向对照：带记号（默认 owner_written=True）的同形行仍然 recorded。
        owner_row = _insert_selection(
            conn, regime="momentum:ai:0900:owner",
            status="shadow_proposal", tier="ai_realtime",
        )
        self.assertEqual(
            ASEL.SELECTION_FACT_RECORDED,
            ASEL.selection_candidate_fact(conn, owner_row, as_of="2026-09-21")
            .fact_verification_status,
        )

    def test_EXP_04e_the_caller_cannot_supply_the_owner_origin_marker(self):
        """EXP-04e：来源记号由 owner 覆盖式盖章，调用方无法自己"声明"。

        否则 ``deepseek_advisor`` 只要在 evidence 里塞一个同名键就能把自己提升成 owner 行 ——
        那会把"谁来证明来源"重新交回 producer。同时断言：带记号但用了 owner 不签发的词汇的
        行仍然是 ``unproven``（记号是来源判据，不是"免检通行证"）。
        """
        conn = _selection_conn()
        baseline, candidate = _weights_patch()
        for forge in ("forged.by.caller", ASEL.SELECTION_OWNER_ORIGIN_MARKER):
            with self.subTest(forge=forge):
                candidate_id = ASEL.record_shadow_proposal(
                    conn, run_date="2026-09-18", account_id="tq_breakout",
                    regime=f"momentum:ai:10{len(forge) % 10}", model_id="one_to_two",
                    baseline_params=baseline, candidate_params=candidate,
                    evidence={"source": "x", ASEL.SELECTION_OWNER_ORIGIN_KEY: forge},
                    reason="r", now="2026-09-21T09:10:00+08:00",
                )
                raw = conn.execute(
                    f"SELECT evidence FROM {SELECTION_TABLE} WHERE id=?", (candidate_id,),
                ).fetchone()[0]
                # 盖章是**覆盖式**：caller 传的值（包括逐字抄写正确值）都被 owner 覆写，
                # 因此"调用方申明来源"这件事在 API 上不可表达。
                self.assertEqual(
                    1, raw.count(ASEL.SELECTION_OWNER_ORIGIN_KEY),
                    "evidence 里只能有一个来源记号（owner 覆盖 caller 的同名键）",
                )
                if forge != ASEL.SELECTION_OWNER_ORIGIN_MARKER:
                    self.assertNotIn(forge, raw, "caller 传入的伪造记号必须被覆盖")
                self.assertEqual(
                    ASEL.SELECTION_FACT_RECORDED,
                    ASEL.selection_candidate_fact(conn, candidate_id, as_of="2026-09-21")
                    .fact_verification_status,
                )

        # 记号是来源判据，不是免检：带记号 + owner 不签发的词汇 → 仍然 unproven。
        crafted = _insert_selection(
            conn, regime="momentum:ai:0800", status="some_foreign_status",
        )
        self.assertEqual(
            ASEL.SELECTION_FACT_OWNER_UNPROVEN,
            ASEL.selection_candidate_fact(conn, crafted, as_of="2026-09-21")
            .fact_verification_status,
        )


class CandidateIdentityTests(_DbTestCase):
    """EXP-05 ~ EXP-07：identity 只能由 owner 派生，且随 revision 变化。"""

    def test_EXP_05_risk_candidate_identity_comes_from_the_owner_revision(self):
        """EXP-05：risk identity = ``<id>@<updated_at>``，不是 ``id``，也不是 ``run_date``。

        行可被 UPDATE，所以 ``id`` 单独一个值会让"内容变了"看起来像"同一条事实"，
        冲突检测随之失效。``source_id`` 由 adapter 从 owner 投影派生，调用方不参与。
        """
        conn = _risk_conn()
        candidate_id = _insert_risk(conn)
        fact = AR.risk_candidate_fact(conn, candidate_id, as_of="2026-09-21")
        self.assertEqual(f"{candidate_id}@2026-09-21T09:00:00+08:00", fact.revision_identity)
        self.assertEqual(AR.RISK_CANDIDATE_RECORD_KIND, fact.record_kind)
        ref = ADAPTER.evidence_ref_from_strategy_projection(fact)
        self.assertEqual(
            f"{AR.RISK_CANDIDATE_RECORD_KIND}|{fact.revision_identity}", ref.source_id,
        )
        # run_date 不参与 identity（它不是 revision）。
        self.assertNotIn(fact.run_date, ref.source_id)

    def test_EXP_06_selection_candidate_identity_comes_from_the_owner_revision(self):
        """EXP-06：selection identity 同样由 owner 派生，且 adapter 不接受调用方命名。"""
        conn = _selection_conn()
        candidate_id = _insert_selection(conn)
        fact = ASEL.selection_candidate_fact(conn, candidate_id, as_of="2026-09-21")
        self.assertEqual(f"{candidate_id}@2026-09-21T09:00:00+08:00", fact.revision_identity)
        ref = ADAPTER.evidence_ref_from_strategy_projection(fact)
        self.assertEqual(
            f"{ASEL.SELECTION_CANDIDATE_RECORD_KIND}|{fact.revision_identity}", ref.source_id,
        )
        # 入口签名里没有 caller 可命名的 identity / as_of。
        for owner in (AR.risk_candidate_fact, ASEL.selection_candidate_fact):
            parameters = set(inspect.signature(owner).parameters)
            self.assertEqual({"conn", "candidate_id", "as_of"}, parameters)

    def test_EXP_07_a_revision_change_moves_the_identity_or_creates_a_new_revision(self):
        """EXP-07：改写行内容必须让 identity（或 revision）移动。

        旧 revision 被覆盖后**不再可引用**，因此这里同时断言：新 revision 的 identity 不同、
        指纹不同，且旧 ``as_of`` 下**拿不到**这一行（而不是拿到被改写的内容）。
        """
        conn = _selection_conn()
        candidate_id = _insert_selection(conn)
        before = ASEL.selection_candidate_fact(conn, candidate_id, as_of="2026-09-21")
        conn.execute(
            f"""UPDATE {SELECTION_TABLE} SET status='eligible_auto_adjust',
               candidate_params=?,updated_at=? WHERE id=?""",
            ('{"weights":{"flow":1.0}}', "2026-09-24T09:00:00+08:00", candidate_id),
        )
        after = ASEL.selection_candidate_fact(conn, candidate_id, as_of="2026-09-24")
        self.assertNotEqual(before.revision_identity, after.revision_identity)
        self.assertNotEqual(before.content_fingerprint, after.content_fingerprint)
        self.assertNotEqual(before.identity, after.identity)
        # 旧 revision 已被覆盖 → 旧 as_of 拿不到当前行（不是"拿到新内容"）。
        self.assertIsNone(
            ASEL.selection_candidate_fact(conn, candidate_id, as_of="2026-09-21")
        )


    def test_EXP_05b_the_risk_ledger_has_a_single_business_writer_module(self):
        """EXP-05b：risk candidate ledger 只有一个业务 writer 模块 —— 因此它**不需要**
        selection 那一层 writer-origin 记号。

        R27-B2C-6 review 指出 selection 侧必须有一条只有 owner 写路径会盖的 durable 记号，
        否则历史第二 writer 的行会被反向认证。risk 侧刻意**没有**加同一层，理由必须是
        **可执行的不对称**而不是口头说明：这条断言把写 ``adaptive_risk_candidates`` 的模块
        钉成闭集，一旦出现第二个业务 writer 就红 —— 届时必须为 risk 补上同样的证明。

        跨库补偿（``adaptive_engine._restore_candidate_snapshot``）刻意**不**算第二个 origin：
        它用**动态**表名把**同一行**的旧快照逐列还原回去，不产生新的来源。这一点也一并断言
        （engine 里不得出现对这两张表的字面写语句）。
        """
        self.assertEqual(
            {"adaptive_risk.py"}, _candidate_table_writers(RISK_TABLE),
            "risk ledger 出现第二个业务 writer —— 那时必须为 risk 补上 writer-origin 证明",
        )
        engine_source = _source("adaptive_engine.py")
        for literal in (f"INTO {RISK_TABLE}", f"UPDATE {RISK_TABLE}",
                        f"INTO {SELECTION_TABLE}", f"UPDATE {SELECTION_TABLE}"):
            self.assertNotIn(
                literal, engine_source,
                "补偿路径必须走动态表名；出现字面写语句意味着它已经是第二个业务 writer",
            )
        # 非空性：补偿路径确实存在，且确实用动态表名（否则上面的否定断言会空转）。
        self.assertIn("{candidate_table}", engine_source)
        self.assertIn(RISK_TABLE, engine_source)
        self.assertIn(SELECTION_TABLE, engine_source)


class CandidatePITTests(_DbTestCase):
    """EXP-08 ~ EXP-11：可用性只来自 owner 的 revision 瞬间，且 fail closed。"""

    def test_EXP_08_updated_at_after_as_of_never_returns_the_current_row(self):
        """EXP-08：``updated_at > as_of`` 的历史读必须**不返回**当前行。

        这是 look-ahead 的核心门禁：拿一条 9/21 形成的候选去解释 9/20 的研究，就是把未来
        信息倒填进历史。正确行为是 UNAVAILABLE，而不是"当前行看起来也差不多"。
        """
        conn = _selection_conn()
        candidate_id = _insert_selection(conn, updated_at="2026-09-21T09:00:00+08:00")
        self.assertIsNotNone(
            ASEL.selection_candidate_fact(conn, candidate_id, as_of="2026-09-21")
        )
        for earlier in ("2026-09-20", "2026-01-01", "2025-12-31"):
            with self.subTest(as_of=earlier):
                self.assertIsNone(
                    ASEL.selection_candidate_fact(conn, candidate_id, as_of=earlier)
                )
        rconn = _risk_conn()
        risk_id = _insert_risk(rconn, updated_at="2026-09-21T09:00:00+08:00")
        self.assertIsNone(AR.risk_candidate_fact(rconn, risk_id, as_of="2026-09-13"))

    def test_EXP_09_run_date_cannot_substitute_for_revision_availability(self):
        """EXP-09：``run_date`` 是标签，**不是**可用瞬间。

        一条 ``run_date=2026-09-18`` 却在 9/21 才形成内容的候选，其 ``availability_day``
        必须是 9/21；而按 9/18 读必须拿不到。把 ``run_date`` 当 ``as_of`` 就是 look-ahead。
        """
        conn = _selection_conn()
        candidate_id = _insert_selection(
            conn, run_date="2026-09-18", updated_at="2026-09-21T09:00:00+08:00",
        )
        fact = ASEL.selection_candidate_fact(conn, candidate_id, as_of="2026-09-21")
        self.assertEqual("2026-09-18", fact.run_date)
        self.assertEqual("2026-09-21", fact.availability_day)
        self.assertNotEqual(fact.run_date, fact.availability_day)
        self.assertIsNone(
            ASEL.selection_candidate_fact(conn, candidate_id, as_of="2026-09-18")
        )
        # adapter 的 as_of 只能是 availability_day，绝不是 run_date。
        ref = ADAPTER.evidence_ref_from_strategy_projection(fact)
        self.assertEqual(fact.availability_day, ref.as_of)
        self.assertNotEqual(fact.run_date, ref.as_of)

    def test_EXP_09b_owner_timezone_normalization_decides_the_availability_day(self):
        """EXP-09（补充）：跨 offset 的同一瞬间必须归一到 owner 时区那一天。

        ``2026-09-20T16:30+00:00`` 在上海已经是 9/21 00:30，业务日必须是 9/21 —— 直接取
        原始 offset 的日期会得到 9/20，并让这条事实提前一天可用。
        """
        conn = _selection_conn()
        candidate_id = _insert_selection(conn, updated_at="2026-09-20T16:30:00+00:00")
        fact = ASEL.selection_candidate_fact(conn, candidate_id, as_of="2026-09-21")
        self.assertEqual("2026-09-21", fact.availability_day)
        self.assertIsNone(
            ASEL.selection_candidate_fact(conn, candidate_id, as_of="2026-09-20")
        )
        # 镜像时间点（同一瞬间写成 +08:00）必须得到同一个业务日。
        other = _insert_selection(conn, updated_at="2026-09-21T00:30:00+08:00",
                                  regime="mirror")
        self.assertEqual(
            "2026-09-21",
            ASEL.selection_candidate_fact(conn, other, as_of="2026-09-21").availability_day,
        )

    def test_EXP_10_malformed_candidate_content_fails_closed(self):
        """EXP-10：坏 JSON / naive 时间戳一律 fail closed —— 绝不 ``{}`` 兜底。

        typed evidence path 上的"宽松读"就是"把损坏内容伪装成一条空事实"，那是数据损坏
        变成业务结论的路径。legacy overview 可以继续宽松，typed path 不行。
        """
        conn = _selection_conn()
        for label, override in (
            ("bad params json", {"candidate_params": "{not json"}),
            ("json array params", {"candidate_params": "[1,2,3]"}),
            ("blank params", {"candidate_params": "   "}),
            ("naive updated_at", {"updated_at": "2026-09-21 09:00:00"}),
            ("bad updated_at", {"updated_at": "yesterday"}),
            ("blank updated_at", {"updated_at": ""}),
            ("NaN content", {"evidence": '{"x": NaN}'}),
        ):
            with self.subTest(case=label):
                candidate_id = _insert_selection(conn, regime=f"m-{label}", **override)
                with self.assertRaises(ASEL.SelectionFactContractError):
                    ASEL.selection_candidate_fact(conn, candidate_id, as_of="2026-09-30")

        rconn = _risk_conn()
        for label, override in (
            ("bad params json", {"candidate_params": "{not json"}),
            ("naive updated_at", {"updated_at": "2026-09-21 09:00:00"}),
            ("non numeric reduction", {"risk_reduction_pct": "abc"}),
        ):
            with self.subTest(case=f"risk {label}"):
                candidate_id = _insert_risk(rconn, regime=f"m-{label}", **override)
                with self.assertRaises(AR.RiskFactContractError):
                    AR.risk_candidate_fact(rconn, candidate_id, as_of="2026-09-30")

    def test_EXP_11_candidate_readers_never_fall_back_to_latest_or_current(self):
        """EXP-11：读侧没有 ``as_of=None → latest``，也不按 run_date / 当前候选回退。

        三条：``as_of`` 缺失即错误；不存在的 id 返回 ``None``（不是"最近一条"）；
        历史 read 从不返回当前行。
        """
        conn = _selection_conn()
        candidate_id = _insert_selection(conn)
        for bad in (None, "", "  ", "2026-9-1", "not-a-day", "2026-09-21T09:00:00"):
            with self.subTest(as_of=repr(bad)):
                with self.assertRaises(ASEL.SelectionFactContractError):
                    ASEL.selection_candidate_fact(conn, candidate_id, as_of=bad)
        with self.assertRaises(TypeError):
            ASEL.selection_candidate_fact(conn, candidate_id)
        self.assertIsNone(ASEL.selection_candidate_fact(conn, 424242, as_of="2026-09-21"))

        rconn = _risk_conn()
        risk_id = _insert_risk(rconn)
        for bad in (None, "", "not-a-day"):
            with self.subTest(as_of=repr(bad)):
                with self.assertRaises(AR.RiskFactContractError):
                    AR.risk_candidate_fact(rconn, risk_id, as_of=bad)
        with self.assertRaises(TypeError):
            AR.risk_candidate_fact(rconn, risk_id)
        self.assertIsNone(AR.risk_candidate_fact(rconn, 424242, as_of="2026-09-21"))

    def test_EXP_11b_missing_normalized_columns_fail_closed(self):
        """EXP-11（补充）：缺必需归一列 → 抛错，而不是"缺什么补什么"。

        归一列是身份来源；缺列时若回落到 raw JSON blob 或造一个默认值，identity 就不再由
        owner 拥有。``overview`` 那类 legacy 展示读仍可宽松，两者刻意不同。
        """
        conn = _track(sqlite3.connect(":memory:"))
        conn.row_factory = sqlite3.Row
        conn.execute(
            f"CREATE TABLE {SELECTION_TABLE}(id INTEGER PRIMARY KEY, run_date TEXT,"
            " account_id TEXT, regime TEXT)"
        )
        conn.execute(
            f"INSERT INTO {SELECTION_TABLE}(id,run_date,account_id,regime)"
            " VALUES(1,'2026-09-18','tq_breakout','momentum')"
        )
        with self.assertRaises(ASEL.SelectionFactContractError):
            ASEL.selection_candidate_fact(conn, 1, as_of="2026-09-21")


class ExperimentEvaluationFactTests(_DbTestCase):
    """EXP-12 ~ EXP-18：cutoff ≠ availability，verdict ≠ verification。"""

    def test_EXP_12_evaluation_cutoff_is_not_evaluation_availability(self):
        """EXP-12：dataset cutoff 与 result availability 是两个不同的东西。

        ``cutoff`` 是**数据集内容冻结边界**，``result_available_at`` 是**结果产生瞬间**。
        本测试同时断言二者在投影里都保留、且 ``availability_day`` 只由后者派生。
        """
        conn = _learning_conn()
        dataset_fingerprint = _dataset_manifest(conn, cutoff="2026-09-20")
        _persist_evaluation(conn, dataset_fingerprint)
        fact = LE.experiment_evaluation_fact(conn, "e" * 64, as_of="2026-09-21")
        self.assertEqual("2026-09-20", fact.dataset_cutoff)
        self.assertEqual("2026-09-21", fact.availability_day)
        self.assertNotEqual(fact.dataset_cutoff, fact.availability_day)
        self.assertEqual(EVALUATION_RESULT_AT, fact.result_available_at)
        # cutoff 与 availability 都必须进指纹：两者都是重要事实内容。
        other = LE.experiment_evaluation_fact(conn, "e" * 64, as_of="2026-09-21")
        self.assertEqual(fact.content_fingerprint, other.content_fingerprint)
        self.assertNotEqual(
            fact.content_fingerprint,
            dataclasses.replace(fact, result_available_at="2026-09-25T02:00:00+00:00",
                                availability_day="2026-09-25").content_fingerprint,
        )

    def test_EXP_13_evaluation_produced_later_cannot_be_backfilled_to_the_cutoff_day(self):
        """EXP-13：9/21 才跑出来的评估**不得**在 9/20 的研究里出现。

        这是本轮最实用的一条 PIT 断言：只要有人把 ``as_of`` 改成 cutoff，它立刻红。
        """
        conn = _learning_conn()
        dataset_fingerprint = _dataset_manifest(conn, cutoff="2026-09-20")
        _persist_evaluation(conn, dataset_fingerprint)
        self.assertIsNone(
            LE.experiment_evaluation_fact(conn, "e" * 64, as_of="2026-09-20"),
            "cutoff 当天不得看到 9/21 才产生的评估结果",
        )
        self.assertIsNone(LE.experiment_evaluation_fact(conn, "e" * 64, as_of="2026-09-19"))
        self.assertIsNotNone(LE.experiment_evaluation_fact(conn, "e" * 64, as_of="2026-09-21"))

    def test_EXP_13b_evaluation_availability_is_owner_timezone_normalized(self):
        """EXP-13（补充）：结果瞬间同样按 owner（交易所）时区归一到业务日。"""
        conn = _learning_conn()
        dataset_fingerprint = _dataset_manifest(conn)
        _persist_evaluation(conn, dataset_fingerprint, created_at="2026-09-20T16:30:00+00:00")
        fact = LE.experiment_evaluation_fact(conn, "e" * 64, as_of="2026-09-21")
        self.assertEqual("2026-09-21", fact.availability_day)
        self.assertIsNone(LE.experiment_evaluation_fact(conn, "e" * 64, as_of="2026-09-20"))

    def test_EXP_13c_unprovable_dataset_freeze_bound_is_unproven_not_recorded(self):
        """EXP-13（补充）：数据集 manifest 不存在 → ``unproven``，绝不是"照样 recorded"。

        内容冻结边界不可证时，owner 不为这次评估的完整性背书；而且这条状态是**派生**的，
        不能被一个没证明任何东西的调用方贴上来。
        """
        conn = _learning_conn()  # 刻意**不**写 dataset manifest
        _persist_evaluation(conn, "9" * 64)
        fact = LE.experiment_evaluation_fact(conn, "e" * 64, as_of="2026-09-21")
        self.assertIsNone(fact.dataset_cutoff)
        self.assertEqual(LE.EXPERIMENT_FACT_OWNER_UNPROVEN, fact.fact_verification_status)
        # 该状态经 adapter 归口后只能是 unverified。
        ref = ADAPTER.evidence_ref_from_strategy_projection(fact)
        self.assertFalse(ref.is_verified)
        # 核验状态不是自由字段：它逐字等价于"冻结边界是否可证"。
        with self.assertRaises(LE.ExperimentFactContractError):
            dataclasses.replace(fact, fact_verification_status=LE.EXPERIMENT_FACT_RECORDED)
        with self.assertRaises(LE.ExperimentFactContractError):
            dataclasses.replace(
                fact, dataset_cutoff="2026-09-20",
                fact_verification_status=LE.EXPERIMENT_FACT_OWNER_UNPROVEN,
            )

    def test_EXP_14_evaluation_admitted_true_is_not_strategy_verified(self):
        """EXP-14：``evaluation_admitted=True`` **不得**被解释成"策略为真"。

        核验闭集回答的是"这条评估结果是不是 owner 自洽签发的事实"。``admitted`` 只是事实
        字段；把它读成核验结论，就等于用一次契约门禁冒充科学结论。
        """
        conn = _learning_conn()
        dataset_fingerprint = _dataset_manifest(conn)
        _persist_evaluation(conn, dataset_fingerprint)  # blockers=[] → admitted True
        fact = LE.experiment_evaluation_fact(conn, "e" * 64, as_of="2026-09-21")
        self.assertTrue(fact.evaluation_admitted)
        self.assertEqual(LE.EXPERIMENT_FACT_RECORDED, fact.fact_verification_status)
        # 核验维度里不得出现 admitted 这个词（否则它已经被塞进核验语义）。
        ref = ADAPTER.evidence_ref_from_strategy_projection(fact)
        self.assertNotIn("admitted", str(ref.verification_attributes))
        self.assertNotIn("admitted", ref.verification)
        self.assertEqual("experiment_evaluation_recorded", ref.verification)
        # 更紧一条：attributes 的键集必须**恰好**是三个事实性审计维度。
        # 这样任何 verdict / lifecycle / score 想混进核验维度都必须改这条断言 —— 是显式的
        # 人工决定，而不是一次"顺手多加一个字段"。
        self.assertEqual(
            {"record_kind", "contract_version", "fact_fingerprint"},
            set(ref.verification_attributes),
        )

    def test_EXP_15_evaluation_admitted_false_is_still_an_owner_verified_fact(self):
        """EXP-15：``admitted=False`` 照样可以是一条 owner 已核验的**事实**。

        "科学门禁明确判定这次评估不通过"本身是可信事实 —— 与 execution 侧
        "not_executed but verified fact" 是同一种语义区分。
        """
        conn = _learning_conn()
        dataset_fingerprint = _dataset_manifest(conn)
        _persist_evaluation(
            conn, dataset_fingerprint,
            evaluation_blockers=["holdout_tail_degraded"],
            evaluation_contract_ok=False,
        )
        fact = LE.experiment_evaluation_fact(conn, "e" * 64, as_of="2026-09-21")
        self.assertFalse(fact.evaluation_admitted)
        self.assertEqual(
            ("holdout_tail_degraded",), fact.evaluation_blockers,
        )
        self.assertEqual(LE.EXPERIMENT_FACT_RECORDED, fact.fact_verification_status)
        ref = ADAPTER.evidence_ref_from_strategy_projection(fact)
        self.assertTrue(
            ref.is_verified,
            "不满意的评估结果本身仍是一条 owner 签发的可靠事实",
        )

    def test_EXP_16_promotable_is_not_the_definition_of_owner_verification(self):
        """EXP-16：``promotable=True`` 不是 ``OwnerVerification verified`` 的定义。

        结构断言：投影里根本没有 ``promotable`` 字段，而 promotion 结论也**没有被 import**
        进 research 链路 —— 因此"晋升结论"无法成为核验依据。核验结论只来自 owner 的
        ``fact_verification_status``。
        """
        names = {item.name for item in dataclasses.fields(LE.ExperimentEvaluationProjection)}
        self.assertNotIn("promotable", names)
        self.assertNotIn("evaluable", names)
        for module in (
            "ai_research_strategy_adapter.py", "ai_research_contract.py",
            "adaptive_risk.py", "adaptive_selection.py", "learning_evaluation.py",
        ):
            with self.subTest(module=module):
                self.assertNotIn(
                    "promotion_science", _imported_roots(module),
                    f"{module} 不得把晋升结论读成核验依据",
                )
        # 核验归口表里也没有 promotable 这个词（它只认识 owner 的核验闭集）。
        self.assertNotIn("promotable", str(ADAPTER._STRATEGY_OUTCOME_BY_STATUS))

    def test_EXP_17_promotable_false_is_not_source_unusable(self):
        """EXP-17：``promotable=False`` 也不是 ``source_unusable``。

        ``source_unusable`` 表示"核验过程做不出判定"（源不可用 / 多源否证）。一个明确判定
        "不允许晋级"的科学门禁**恰恰做出了判定**；把它读成 source_unusable 会让假设层报出
        ``evidence_unavailable`` —— 一个错误的原因。今天这三个 owner 的闭集里**没有任何**
        取值归到 ``SOURCE_UNUSABLE``，这条断言让"顺手加一个"变成一次显式的人工决定。
        """
        table = ADAPTER._STRATEGY_OUTCOME_BY_STATUS
        self.assertEqual(
            {ADAPTER.ARC.OWNER_OUTCOME_VERIFIED, ADAPTER.ARC.OWNER_OUTCOME_UNVERIFIED},
            set(table.values()),
            "candidate / evaluation 事实不得归到 source_unusable（那是核验过程不可用）",
        )
        self.assertNotIn(ADAPTER.ARC.OWNER_OUTCOME_SOURCE_UNUSABLE, set(table.values()))
        self.assertNotIn("promotable", str(table))
        self.assertNotIn("promotable", str(ADAPTER._OWNER_VERIFICATION_VOCABULARIES))

    def test_EXP_18_learning_pit_status_cannot_map_to_owner_verification(self):
        """EXP-18：``learning_dataset`` 的 PIT 状态**不能**直接映射成 owner 核验。

        ``verified`` / ``unproven`` / ``legacy_unproven`` / ``unknown`` / ``future`` 表达的是
        **PIT 可用性 / 资格**，不是"这条事实是否通过 owner 核验"。两个词表必须不相交，
        否则 ``pit_status == "verified"`` 会被读成 owner verified，把可用性冒充成核验。
        """
        pit_statuses = set(LD.KNOWN_PIT_STATUSES)
        self.assertTrue(pit_statuses, "PIT 闭集必须非空（否则本条断言空转）")
        self.assertEqual(
            frozenset(), pit_statuses & frozenset(ADAPTER._STRATEGY_OUTCOME_BY_STATUS),
            "PIT 可用性词表与 strategy 核验归口表必须不相交",
        )
        self.assertEqual(
            frozenset(), pit_statuses & frozenset(LE.EXPERIMENT_FACT_VERIFICATION_STATUSES),
        )
        # 非空性对照：PIT 的 verified 与 owner 的 recorded 是**两个不同的词**。
        self.assertIn(LD.PIT_VERIFIED, pit_statuses)
        self.assertNotIn(LD.PIT_VERIFIED, LE.EXPERIMENT_FACT_VERIFICATION_STATUSES)
        # 归口表里也没有任何 *_unusable 之外的"核验过程不可用"被 PIT 借走。
        self.assertEqual(
            {ADAPTER.ARC.OWNER_OUTCOME_VERIFIED, ADAPTER.ARC.OWNER_OUTCOME_UNVERIFIED},
            set(ADAPTER._STRATEGY_OUTCOME_BY_STATUS.values()),
        )


if __name__ == "__main__":
    unittest.main()
