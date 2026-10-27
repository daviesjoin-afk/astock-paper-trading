# -*- coding: utf-8 -*-
"""自动采用真的落地了吗（方案 §5.2 "唯一写入者"这段的验证）。

这里的重点不是"能不能改参数"，而是**改得对不对**：

- 有凭据 + 在范围内 + 候选一致 → 真正写进 ``paper_accounts.params``，并追加
  ``paper_parameter_versions``（用既有写入者，不自己写）。
- 基线在决策之后变了 → ``stale_base``，一个字节都不写。
- 候选要改的值 ≠ 授权要改的值 → 拒绝（不能拿一份授权去落另一份候选）。
- 既有门禁不允许自动应用的目标（条件类覆盖）→ 原样把写入者的拒绝抛出来，不绕过。
- 没有凭据 → 根本不进入写入。
"""
import datetime as dt
import os
import sqlite3
import tempfile
import unittest

import adaptive_selection as AS
import ai_adoption_writer as AW
import ai_capability as CAP
import ai_capability_store as STORE
import paper_trading as PT

NOW = dt.datetime(2026, 10, 26, 2, 0, 0, tzinfo=dt.timezone.utc)
ACCOUNT = "tq_breakout"


def _now_fn():
    return "2026-10-26 10:00:00"


def credential(**overrides):
    kwargs = dict(
        capability_id="cap-weights", mode=CAP.MODE_BOUNDED_AUTO,
        strategy_ids=(ACCOUNT,), allowed_target_kinds=("selection_weights",),
        allowed_fields=("flow",), bounds_version="bounds-v1",
        evidence_policy_id="evidence-v1", max_change_per_apply=0.05,
        max_change_per_day=0.10, loss_stop_policy_id="loss-v1",
        max_trials_per_day=3, daily_cost_cap=5.0, max_tool_steps_per_task=12,
        expires_at="2026-11-26T02:00:00+00:00", revocation_epoch=1,
        issued_by="operator:alice",
    )
    kwargs.update(overrides)
    return CAP.CapabilityCredential(**kwargs)


def request(credential_value, *, field="flow", value=None, baseline=None, **overrides):
    kwargs = dict(
        credential=credential_value, strategy_id=ACCOUNT, target_kind="selection_weights",
        field_changes=((field, value),),
        current_baseline=((field, baseline),), requested_baseline=((field, baseline),),
        evidence_policy_id="evidence-v1", revocation_epoch=1,
        producer="research-agent", approver="operator:alice", cost=1.0,
        now=NOW.isoformat(),
    )
    kwargs.update(overrides)
    return CAP.AdoptionRequest(**kwargs)


class WriterWiringTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(prefix="adopt-")
        self._old_data_dir = os.environ.get("ASTOCK_DATA_DIR")
        os.environ["ASTOCK_DATA_DIR"] = self._folder.name
        self._old_paper_db = PT.DB_PATH
        os.environ.pop("ASTOCK_CACHE_DIR", None)
        PT.DB_PATH = os.path.join(self._folder.name, "paper_trading.sqlite3")
        PT.init_db()
        self.paper_path = PT.DB_PATH
        self.adaptive_path = os.path.join(self._folder.name, "adaptive.sqlite3")
        self.adaptive = sqlite3.connect(self.adaptive_path)
        self.adaptive.row_factory = sqlite3.Row
        AS.ensure_schema(self.adaptive)
        self.capability_path = os.path.join(self._folder.name, "cap.sqlite3")
        self.capability = sqlite3.connect(self.capability_path)
        self.capability.row_factory = sqlite3.Row
        STORE.ensure_schema(self.capability)
        self.paper = sqlite3.connect(self.paper_path)
        self.paper.row_factory = sqlite3.Row

    def tearDown(self):
        for conn in (self.adaptive, self.capability, self.paper):
            conn.close()
        PT.DB_PATH = self._old_paper_db
        if self._old_data_dir is None:
            os.environ.pop("ASTOCK_DATA_DIR", None)
        else:
            os.environ["ASTOCK_DATA_DIR"] = self._old_data_dir
        self._folder.cleanup()

    # ── 夹具 ──────────────────────────────────────────────────────────────
    def _current_weights(self):
        """与写入者相同的口径读"当前生效权重"（含没有 overlay 时的模型默认值）。"""
        row = self.paper.execute("SELECT * FROM paper_accounts WHERE id=?",
                                 (ACCOUNT,)).fetchone()
        _model, weights, _delta, _params, _meta = AS._current(dict(row))
        return dict(weights)

    def _seed_candidate(self, *, mutate, regime="momentum"):
        import json

        base = self._current_weights()
        candidate = dict(base)
        mutate(candidate)
        self.adaptive.execute(
            "INSERT INTO adaptive_selection_candidates(run_date,account_id,regime,model_id,"
            "baseline_params,candidate_params,evidence,status,tier,reason,created_at,"
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-10-26", ACCOUNT, regime, "first_board_relay", "{}",
             # 既有自动应用门禁要求候选**只**含 weights 键。
             json.dumps({"weights": candidate}, ensure_ascii=False),
             "{}", "eligible_auto_adjust", "standard", "test",
             _now_fn(), _now_fn()))
        self.adaptive.commit()
        return int(self.adaptive.execute(
            "SELECT id FROM adaptive_selection_candidates ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])

    def _params_snapshot(self):
        row = self.paper.execute("SELECT params, version FROM paper_accounts WHERE id=?",
                                 (ACCOUNT,)).fetchone()
        return row["params"], row["version"]

    def _applied_version_count(self):
        """只数"采用器追加"的版本行（初始化那一行属于夹具，不算）。"""
        check = sqlite3.connect(self.paper_path)
        try:
            return check.execute(
                "SELECT COUNT(*) FROM paper_parameter_versions"
                " WHERE account_id=? AND version LIKE 'select-evo-%'", (ACCOUNT,)).fetchone()[0]
        finally:
            check.close()

    # ── 用例 ──────────────────────────────────────────────────────────────
    def test_a_weight_change_within_scope_really_lands(self):
        issued = STORE.issue(self.capability, credential(), operator="operator:alice", now=NOW)
        self.capability.commit()
        before_base = self._current_weights()
        target = round(before_base["flow"] + 0.02, 6)
        candidate_id = self._seed_candidate(mutate=lambda w: w.__setitem__("flow", target))
        before_params, _version = self._params_snapshot()

        decision = AW.adopt_candidate(
            capability_conn=self.capability, adaptive_conn=self.adaptive,
            paper_conn=self.paper,
            request=request(issued, value=target, baseline=round(before_base["flow"], 6)),
            candidate_id=candidate_id, now=NOW)
        self.assertTrue(decision.applied)

        after_params, after_version = self._params_snapshot()
        self.assertNotEqual(before_params, after_params, "正式参数必须真的变了")
        import json
        overlay = (json.loads(after_params).get("adaptive_selection") or {})
        self.assertAlmostEqual(overlay["weights"]["flow"], target, places=6)
        self.assertTrue(after_version.startswith("select-evo-"))
        check = sqlite3.connect(self.paper_path)
        try:
            versions = check.execute(
                "SELECT COUNT(*) FROM paper_parameter_versions WHERE account_id=? AND version=?",
                (ACCOUNT, after_version)).fetchone()[0]
        finally:
            check.close()
        self.assertEqual(versions, 1, "必须追加一条参数版本")

    def test_a_changed_baseline_is_refused_before_any_write(self):
        issued = STORE.issue(self.capability, credential(), operator="operator:alice", now=NOW)
        self.capability.commit()
        before_base = self._current_weights()
        target = round(before_base["flow"] + 0.02, 6)
        candidate_id = self._seed_candidate(mutate=lambda w: w.__setitem__("flow", target))
        before_params, before_version = self._params_snapshot()

        with self.assertRaises(AW.AdoptionWiringError) as caught:
            AW.adopt_candidate(
                capability_conn=self.capability, adaptive_conn=self.adaptive,
                paper_conn=self.paper,
                request=request(issued, value=target,
                                baseline=round(before_base["flow"] - 0.01, 6)),
                candidate_id=candidate_id,
                declared_baseline={"weights": {"flow": 0.999}}, now=NOW)
        self.assertEqual(caught.exception.reason, AW.R_STALE_BASE)
        self.assertEqual(self._params_snapshot(), (before_params, before_version))

    def test_a_candidate_that_does_not_match_the_authorization_is_refused(self):
        issued = STORE.issue(self.capability, credential(), operator="operator:alice", now=NOW)
        self.capability.commit()
        before_base = self._current_weights()
        authorised = round(before_base["flow"] + 0.02, 6)
        # 候选实际写的是另一个值。
        candidate_id = self._seed_candidate(
            mutate=lambda w: w.__setitem__("flow", round(before_base["flow"] + 0.03, 6)))
        with self.assertRaises(AW.AdoptionWiringError) as caught:
            AW.adopt_candidate(
                capability_conn=self.capability, adaptive_conn=self.adaptive,
                paper_conn=self.paper,
                request=request(issued, value=authorised,
                                baseline=round(before_base["flow"], 6)),
                candidate_id=candidate_id, now=NOW)
        self.assertEqual(caught.exception.reason, AW.R_CANDIDATE_MISMATCH)

    def test_a_target_the_writer_refuses_is_not_silently_applied(self):
        """条件类覆盖超出既有自动应用范围：沿用写入者的拒绝，不绕过。"""
        condition_capability = credential(
            capability_id="cap-conditions", allowed_target_kinds=("entry_policy",),
            allowed_fields=("first_board_bonus",))
        issued = STORE.issue(self.capability, condition_capability,
                             operator="operator:alice", now=NOW)
        self.capability.commit()
        base = ADV_FLOW = self._current_weights()  # noqa: F841 - 只为读一次账户

        def mutate(weights):
            weights.clear()

        candidate_id = self._seed_candidate(mutate=mutate)
        self.adaptive.execute(
            "UPDATE adaptive_selection_candidates SET candidate_params=? WHERE id=?",
            ('{"conditions": {"first_board_bonus": 0.20}}', candidate_id))
        self.adaptive.commit()
        before_params, before_version = self._params_snapshot()
        with self.assertRaises(AW.AdoptionWiringError) as caught:
            AW.adopt_candidate(
                capability_conn=self.capability, adaptive_conn=self.adaptive,
                paper_conn=self.paper,
                request=request(issued, field="first_board_bonus", value=0.20,
                                baseline=0.22,
                                target_kind="entry_policy"),
                candidate_id=candidate_id, now=NOW)
        self.assertIn(caught.exception.reason,
                      (AW.R_WRITER_REFUSED, AW.R_CANDIDATE_MISMATCH))
        self.assertEqual(self._params_snapshot(), (before_params, before_version))
        self.assertIsNotNone(base)

    def test_without_a_credential_nothing_is_written(self):
        before_base = self._current_weights()
        candidate_id = self._seed_candidate(
            mutate=lambda w: w.__setitem__("flow", round(before_base["flow"] + 0.02, 6)))
        before_params, before_version = self._params_snapshot()
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            AW.adopt_candidate(
                capability_conn=self.capability, adaptive_conn=self.adaptive,
                paper_conn=self.paper,
                request=request(None, value=round(before_base["flow"] + 0.02, 6),
                                baseline=round(before_base["flow"], 6)),
                candidate_id=candidate_id, now=NOW)
        self.assertEqual(caught.exception.reason, CAP.R_NO_CREDENTIAL)
        self.assertEqual(self._params_snapshot(), (before_params, before_version))

    def test_the_second_application_is_budgeted_then_refused(self):
        issued = STORE.issue(self.capability, credential(max_change_per_day=0.02),
                             operator="operator:alice", now=NOW)
        self.capability.commit()
        before_base = self._current_weights()
        target = round(before_base["flow"] + 0.02, 6)
        candidate_id = self._seed_candidate(mutate=lambda w: w.__setitem__("flow", target))
        first = AW.adopt_candidate(
            capability_conn=self.capability, adaptive_conn=self.adaptive,
            paper_conn=self.paper,
            request=request(issued, value=target, baseline=round(before_base["flow"], 6)),
            candidate_id=candidate_id, now=NOW)
        self.assertTrue(first.applied)
        self.capability.commit()

        # 同一 adoption 的重放是幂等：不重复扣预算，也不重复落版本。
        replay = AW.adopt_candidate(
            capability_conn=self.capability, adaptive_conn=self.adaptive,
            paper_conn=self.paper,
            request=request(issued, value=target, baseline=round(before_base["flow"], 6)),
            candidate_id=candidate_id, now=NOW)
        self.assertTrue(replay.applied)
        self.assertEqual(STORE.budget(self.capability, issued.capability_id, "2026-10-26").trials_used, 1)
        self.assertEqual(self._applied_version_count(), 1)

        # 换一个**新候选**再改一次就超日额度：拒绝且不再写。
        live_after = self._current_weights()
        next_target = round(live_after["flow"] + 0.02, 6)
        next_candidate = self._seed_candidate(
            mutate=lambda w: w.__setitem__("flow", next_target), regime="rebound")
        before_params, before_version = self._params_snapshot()
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            AW.adopt_candidate(
                capability_conn=self.capability, adaptive_conn=self.adaptive,
                paper_conn=self.paper,
                request=request(issued, value=next_target,
                                baseline=round(live_after["flow"], 6)),
                candidate_id=next_candidate, now=NOW)
        self.assertEqual(caught.exception.reason, CAP.R_BUDGET_EXHAUSTED)
        self.assertEqual(self._params_snapshot(), (before_params, before_version))
        self.assertEqual(self._applied_version_count(), 1)

    def _prepared(self, cap=None, delta=0.02):
        issued = STORE.issue(self.capability, cap or credential(), operator="operator:alice", now=NOW)
        self.capability.commit()
        base = self._current_weights()
        target = round(base["flow"] + delta, 6)
        candidate_id = self._seed_candidate(mutate=lambda w: w.__setitem__("flow", target))
        return issued, base, target, candidate_id

    def test_revoked_and_never_issued_credentials_reach_no_formal_writer(self):
        issued, base, target, candidate_id = self._prepared()
        STORE.revoke(self.capability, issued.capability_id, operator="operator:alice")
        self.capability.commit()
        before = self._params_snapshot()
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            AW.adopt_candidate(capability_conn=self.capability, adaptive_conn=self.adaptive,
                paper_conn=self.paper, request=request(issued, value=target, baseline=base["flow"]),
                candidate_id=candidate_id, now=NOW)
        self.assertEqual(caught.exception.reason, CAP.R_REVOKED)
        self.assertEqual(self._params_snapshot(), before)
        unissued = credential(capability_id="never-issued")
        with self.assertRaises(CAP.AdoptionRefused):
            AW.adopt_candidate(capability_conn=self.capability, adaptive_conn=self.adaptive,
                paper_conn=self.paper, request=request(unissued, value=target, baseline=base["flow"]),
                candidate_id=candidate_id, now=NOW)
        self.assertEqual(self._params_snapshot(), before)

    def test_candidate_cannot_smuggle_an_unauthorized_weight(self):
        issued, base, target, candidate_id = self._prepared()
        self.adaptive.execute("UPDATE adaptive_selection_candidates SET candidate_params=json_set(candidate_params,'$.weights.mom_short',?) WHERE id=?",
                              (round(base["mom_short"] - 0.01, 6), candidate_id))
        self.adaptive.commit()
        before = self._params_snapshot()
        with self.assertRaises(AW.AdoptionWiringError) as caught:
            AW.adopt_candidate(capability_conn=self.capability, adaptive_conn=self.adaptive,
                paper_conn=self.paper, request=request(issued, value=target, baseline=base["flow"]),
                candidate_id=candidate_id, now=NOW)
        self.assertEqual(caught.exception.reason, AW.R_CANDIDATE_MISMATCH)
        self.assertEqual(self._params_snapshot(), before)

    def test_caller_supplied_baselines_cannot_hide_actual_change_or_budget(self):
        tight = credential(max_change_per_apply=0.001, max_change_per_day=0.001)
        issued, base, target, candidate_id = self._prepared(cap=tight, delta=0.02)
        # The real baseline is base, despite both caller tuples claiming the target.
        req = request(issued, value=target, baseline=target)
        before = self._params_snapshot()
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            AW.adopt_candidate(capability_conn=self.capability, adaptive_conn=self.adaptive,
                paper_conn=self.paper, request=req, candidate_id=candidate_id, now=NOW)
        self.assertEqual(caught.exception.reason, CAP.R_CHANGE_TOO_LARGE)
        self.assertEqual(self._params_snapshot(), before)
        self.assertEqual(STORE.budget(self.capability, issued.capability_id, "2026-10-26").changes_used, 0.0)

    def test_missing_declared_baseline_still_uses_live_effective_values(self):
        issued, base, target, candidate_id = self._prepared()
        req = request(issued, value=target, baseline=999.0)
        decision = AW.adopt_candidate(capability_conn=self.capability, adaptive_conn=self.adaptive,
            paper_conn=self.paper, request=req, candidate_id=candidate_id, now=NOW)
        self.assertAlmostEqual(decision.budget_after.changes_used, 0.02, places=8)
        self.assertTrue(decision.applied)

    def test_receipt_failure_after_formal_commit_is_recovered_not_reapplied(self):
        issued, base, target, candidate_id = self._prepared()
        original = STORE.receipt
        calls = {"n": 0}
        def fail_receipt(*args, **kwargs):
            calls["n"] += 1
            raise RuntimeError("injected receipt crash")
        STORE.receipt = fail_receipt
        try:
            with self.assertRaises(RuntimeError):
                AW.adopt_candidate(capability_conn=self.capability, adaptive_conn=self.adaptive,
                    paper_conn=self.paper, request=request(issued, value=target, baseline=base["flow"]),
                    candidate_id=candidate_id, now=NOW)
        finally:
            STORE.receipt = original
        self.assertEqual(STORE.budget(self.capability, issued.capability_id, "2026-10-26").trials_used, 1)
        self.assertEqual(STORE.get_attempt(self.capability, f"{issued.capability_id}:{ACCOUNT}:{candidate_id}")["status"], "reserved")
        recovered = AW.replay_pending_adoptions(capability_conn=self.capability,
            adaptive_conn=self.adaptive, paper_conn=self.paper, now=NOW)
        self.assertEqual(recovered, [candidate_id])
        self.assertEqual(calls["n"], 1)
        self.assertEqual(STORE.budget(self.capability, issued.capability_id, "2026-10-26").trials_used, 1)
        # 恢复只补回执，不能再写一次正式参数（初始化那一行不计）。
        self.assertEqual(self._applied_version_count(), 1)

    def test_recovery_rechecks_revocation_and_does_not_write(self):
        issued, base, target, candidate_id = self._prepared()
        reservation = STORE.reserve(self.capability, request(issued, value=target, baseline=base["flow"]),
            adoption_id="crash-before-write", context={"candidate_id": candidate_id,
            "paper_path": self.paper_path, "approved_by": "bounded-auto", "declared_baseline": None}, now=NOW)
        STORE.revoke(self.capability, issued.capability_id, operator="operator:alice")
        self.capability.commit()
        before = self._params_snapshot()
        with self.assertRaises(CAP.AdoptionRefused) as caught:
            AW.adopt_candidate(capability_conn=self.capability, adaptive_conn=self.adaptive,
                paper_conn=self.paper, request=reservation.request, candidate_id=candidate_id,
                adoption_id="crash-before-write", recovery=True, now=NOW)
        self.assertEqual(caught.exception.reason, CAP.R_REVOKED)
        self.assertEqual(self._params_snapshot(), before)
        self.assertEqual(self._applied_version_count(), 0)

    def test_same_adoption_replay_is_idempotent(self):
        issued, base, target, candidate_id = self._prepared()
        req = request(issued, value=target, baseline=base["flow"])
        first = AW.adopt_candidate(capability_conn=self.capability, adaptive_conn=self.adaptive,
            paper_conn=self.paper, request=req, candidate_id=candidate_id, now=NOW)
        second = AW.adopt_candidate(capability_conn=self.capability, adaptive_conn=self.adaptive,
            paper_conn=self.paper, request=req, candidate_id=candidate_id, now=NOW)
        self.assertEqual(first.projection(), second.projection())
        self.assertEqual(STORE.budget(self.capability, issued.capability_id, "2026-10-26").trials_used, 1)

    def test_concurrent_same_attempt_writes_only_once(self):
        """两个线程抢同一个 adoption：只写一次，两边都拿到 applied，且谁都不许挂住。

        线程里必须自己开连接：主线程的 sqlite3.Connection 不能跨线程使用，
        跨了会抛 ProgrammingError 让 worker 提前死掉，主线程就永远卡在 barrier 上。
        """
        import threading
        issued, base, target, candidate_id = self._prepared()
        barrier = threading.Barrier(3)
        outcomes = []

        def worker():
            cap = sqlite3.connect(self.capability_path, timeout=30)
            adaptive = sqlite3.connect(self.adaptive_path, timeout=30)
            paper = sqlite3.connect(self.paper_path, timeout=30)
            cap.row_factory = adaptive.row_factory = paper.row_factory = sqlite3.Row
            try:
                barrier.wait(timeout=15)
                result = AW.adopt_candidate(capability_conn=cap, adaptive_conn=adaptive,
                    paper_conn=paper, request=request(issued, value=target, baseline=base["flow"]),
                    candidate_id=candidate_id, now=NOW)
                outcomes.append(result.applied)
            except Exception as exc:  # 记下原因，别让主线程干等
                outcomes.append(exc)
            finally:
                cap.close(); adaptive.close(); paper.close()

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=15)
        for thread in threads:
            thread.join(60)
        self.assertFalse([thread for thread in threads if thread.is_alive()], "并发用例不允许挂起")
        self.assertEqual(outcomes.count(True), 2, outcomes)
        self.assertEqual(STORE.budget(self.capability, issued.capability_id, "2026-10-26").trials_used, 1)
        self.assertEqual(self._applied_version_count(), 1)

    def test_a_second_adoption_over_a_materialized_baseline_is_not_stale(self):
        """params 已经是非平凡 JSON：第二次合法采用不能被 guard 误判成 stale_base。

        写入者按候选原样落 overlay；生效值要再经 ``_normalize``（和 3%/65% 边界），
        所以"生效 flow"本来就不等于候选里的数字——这里把两步都钉住。
        """
        import json

        issued, base, target, candidate_id = self._prepared()
        first = AW.adopt_candidate(capability_conn=self.capability, adaptive_conn=self.adaptive,
            paper_conn=self.paper, request=request(issued, value=target, baseline=base["flow"]),
            candidate_id=candidate_id, now=NOW)
        self.assertTrue(first.applied)
        live = self._current_weights()
        next_target = round(live["flow"] + 0.02, 6)
        next_candidate = self._seed_candidate(
            mutate=lambda w: w.__setitem__("flow", next_target), regime="rebound")
        second = AW.adopt_candidate(capability_conn=self.capability, adaptive_conn=self.adaptive,
            paper_conn=self.paper,
            request=request(issued, value=next_target, baseline=round(live["flow"], 6)),
            candidate_id=next_candidate, now=NOW)
        self.assertTrue(second.applied)
        self.assertEqual(self._applied_version_count(), 2)
        stored_params, version = self._params_snapshot()
        self.assertEqual(version, f"select-evo-20261026-{next_candidate}")
        stored = json.loads(stored_params)["adaptive_selection"]["weights"]
        requested = dict(live)
        requested["flow"] = next_target
        # 1) 正式库里的 overlay 与候选请求值逐项一致（写入没有被吞或被改写）。
        self.assertEqual({k: round(float(v), 6) for k, v in stored.items()},
                         {k: round(float(v), 6) for k, v in requested.items()})
        # 2) 生效权重恰好是写入值的归一化结果；方向向上，且不越 3pp 以外的边界。
        self.assertEqual(self._current_weights(), AS._normalize(stored))
        self.assertGreater(self._current_weights()["flow"], round(live["flow"], 6))

    def test_a_baseline_that_moves_inside_the_write_window_is_stale_base(self):
        """读基线之后、正式写之前被人改了：guard 必须 abort，并且要映射成 stale_base。"""
        issued, base, target, candidate_id = self._prepared()
        original = AS.apply_candidate

        def raced(*args, **kwargs):
            rival = sqlite3.connect(self.paper_path, timeout=10)
            try:
                rival.execute("UPDATE paper_accounts SET params=json_set(params,'$._manual',1)"
                              " WHERE id=?", (ACCOUNT,))
                rival.commit()
            finally:
                rival.close()
            return original(*args, **kwargs)

        before_params, before_version = self._params_snapshot()
        AS.apply_candidate = raced
        try:
            with self.assertRaises(CAP.AdoptionRefused) as caught:
                AW.adopt_candidate(capability_conn=self.capability, adaptive_conn=self.adaptive,
                    paper_conn=self.paper, request=request(issued, value=target,
                                                           baseline=base["flow"]),
                    candidate_id=candidate_id, now=NOW)
        finally:
            AS.apply_candidate = original
        self.assertEqual(caught.exception.reason, CAP.R_STALE_BASE)
        # 人工写入保留，采用的版本/预算/尝试都不留残留。
        after_params, after_version = self._params_snapshot()
        self.assertEqual(after_version, before_version)
        self.assertNotEqual(after_params, before_params)
        self.assertIn('"_manual"', after_params)
        self.assertEqual(self._applied_version_count(), 0)
        self.assertEqual(STORE.budget(self.capability, issued.capability_id, "2026-10-26").changes_used, 0.0)
        self.assertIsNone(STORE.get_attempt(
            self.capability, f"{issued.capability_id}:{ACCOUNT}:{candidate_id}"))

    def test_the_baseline_guard_compares_exact_text_and_yields_to_a_later_edit(self):
        """guard 是"比较并交换"的前置条件：按读到的原文比对，不是按 JSON 规范化。"""
        issued, base, target, candidate_id = self._prepared()
        AW.adopt_candidate(capability_conn=self.capability, adaptive_conn=self.adaptive,
            paper_conn=self.paper, request=request(issued, value=target, baseline=base["flow"]),
            candidate_id=candidate_id, now=NOW)
        # 此时 params 已经是非平凡 JSON（带空格、键序固定），重新读一次作为基线。
        live = AW.live_selection_baseline(self.paper, ACCOUNT)
        self.assertIn('"adaptive_selection"', live["params_text"])

        # 正例：同一份原文照写必须放行（JSON 文本本身不唯一，不能拿规范化结果比）。
        with AW._PaperBaselineGuard(self.paper_path, ACCOUNT, live["params_text"]):
            writer = sqlite3.connect(self.paper_path, timeout=10)
            try:
                writer.execute("UPDATE paper_accounts SET params=?,version='guard-probe' WHERE id=?",
                               (live["params_text"], ACCOUNT))
                writer.commit()
            finally:
                writer.close()

        # 反例：guard 装上之后基线被别人改了，这一次写入必须 abort，不能覆盖。
        with AW._PaperBaselineGuard(self.paper_path, ACCOUNT, live["params_text"]):
            drift = sqlite3.connect(self.paper_path, timeout=10)
            try:
                drift.execute("UPDATE paper_accounts SET params=json_set(params,'$._manual',1) WHERE id=?",
                              (ACCOUNT,))
                drift.commit()
            finally:
                drift.close()
            writer = sqlite3.connect(self.paper_path, timeout=10)
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    writer.execute("UPDATE paper_accounts SET params=?,version='guard-probe-2' WHERE id=?",
                                   (live["params_text"], ACCOUNT))
                    writer.commit()
            finally:
                writer.close()


if __name__ == "__main__":
    unittest.main()
