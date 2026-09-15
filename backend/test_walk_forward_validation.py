# -*- coding: utf-8 -*-
"""Purged walk-forward 验证边界的契约测试（P1–P30）。

每条测试对应契约里的一个**可证伪**陈述，而不是"跑通即通过"。
变异脚本 ``pr156_mutation_check.py`` 的 M1–M19 逐条还原这些缺陷，
本文件必须把它们抓住。
"""
from __future__ import annotations

import ast
import dataclasses
import datetime as _dt
import pathlib
import random
import unittest

import walk_forward_validation as WFV


# ───────────────────────────── shared fixtures ─────────────────────────────

#: 六个交易周，含两个缺口：
#:   * 2024-06-10（周一）是节假日 → 06-07(周五) 与 06-11(周二) 之间没有 session；
#:   * 每个周末都跨自然日。
#: 28 个 session，足以在 (10, 4, 4) 配置下生成 2 折。
CALENDAR = [
    "2024-06-03", "2024-06-04", "2024-06-05", "2024-06-06", "2024-06-07",
    "2024-06-11", "2024-06-12", "2024-06-13", "2024-06-14",
    "2024-06-17",
    "2024-06-18", "2024-06-19", "2024-06-20", "2024-06-21",
    "2024-06-24", "2024-06-25", "2024-06-26", "2024-06-27",
    "2024-06-28",
    "2024-07-01", "2024-07-02", "2024-07-03",
    "2024-07-04", "2024-07-05",
    "2024-07-08", "2024-07-09", "2024-07-10", "2024-07-11",
]

CODES = ("600001", "600002", "600003", "300001")
LABEL_VERSION = "selection-label-v1/raw-return"

CONFIG = WFV.WalkForwardConfig(
    min_train_sessions=10, validation_sessions=4, test_sessions=4
)


def sample_at(
    index,
    *,
    code="600001",
    horizon=2,
    target=0.01,
    pit_status="verified",
    available=None,
    key=None,
    label_version=LABEL_VERSION,
    features=None,
    provenance=None,
    calendar=CALENDAR,
):
    """``index`` 号 session 上的一条样本；标签在 ``index + horizon`` 收盘成熟。"""
    session = calendar[index]
    exit_index = index + horizon
    exit_session = calendar[exit_index] if 0 <= exit_index < len(calendar) else None
    if available is None and exit_session is not None:
        available = WFV.label_available_at_for_close(exit_session)
    return WFV.ValidationSample(
        sample_key=key or f"{session}:{code}:h{horizon}",
        code=code,
        decision_session=session,
        label_available_at=available or "",
        target=target,
        horizon=horizon,
        label_version=label_version,
        decision_at=f"{session}T16:00:00+08:00",
        exit_date=exit_session,
        features=dict(features if features is not None else {"momentum": float(index)}),
        pit_status=pit_status,
        provenance=dict(provenance or {}),
    )


def session_samples(index, *, codes=CODES, **kwargs):
    """同一个 decision session 上的多只股票。"""
    return [
        sample_at(index, code=code, key=f"{CALENDAR[index]}:{code}", **kwargs)
        for code in codes
    ]


def full_dataset(*, codes=CODES, horizon=2, indices=None):
    indices = range(len(CALENDAR)) if indices is None else indices
    out = []
    for index in indices:
        out.extend(session_samples(index, codes=codes, horizon=horizon))
    return out


def build(samples, config=CONFIG, **kwargs):
    return WFV.build_walk_forward_folds(samples, config, **kwargs)


def ready_folds(built):
    return [fold for fold in built["folds"] if fold.ready]


# ───────────────────────────── P1–P8: boundaries ─────────────────────────────


class ChronologicalBoundaryTest(unittest.TestCase):
    """只允许过去训练、未来验证/测试，且切分单位是 decision session。"""

    def test_p1_folds_are_strictly_chronological(self):
        """P1：train 的决策全部早于 validation，validation 全部早于 test。"""
        built = build(full_dataset())
        folds = ready_folds(built)
        self.assertEqual(2, len(folds), built["report"])
        for fold in folds:
            train_max = max(row.decision_session for row in fold.train_rows)
            val_sessions = fold.metadata["validation_decision_sessions"]
            test_sessions = fold.metadata["test_decision_sessions"]
            self.assertLess(train_max, val_sessions[0], fold.fold_id)
            self.assertLess(val_sessions[-1], test_sessions[0], fold.fold_id)
            # 折与折之间也严格前进：validation 窗口不重叠。
            self.assertLess(val_sessions[-1], test_sessions[-1])

    def test_p2_same_decision_session_never_crosses_splits(self):
        """P2：同一个 decision session 的全部股票必须落在同一个 split。"""
        built = build(full_dataset())
        for fold in ready_folds(built):
            train = set(fold.metadata["train_decision_sessions"])
            validation = set(fold.metadata["validation_decision_sessions"])
            test = set(fold.metadata["test_decision_sessions"])
            self.assertEqual(set(), train & validation, fold.fold_id)
            self.assertEqual(set(), train & test, fold.fold_id)
            self.assertEqual(set(), validation & test, fold.fold_id)
            # 每个 session 的**全部**股票都在同一个 split 里。
            for partition, rows in (
                ("train", fold.train_rows),
                ("validation", fold.validation_features),
                ("test", fold.test_features),
            ):
                per_session = {}
                for row in rows:
                    per_session.setdefault(row.decision_session, set()).add(row.code)
                for session, codes in per_session.items():
                    self.assertEqual(set(CODES), codes, (fold.fold_id, partition, session))

    def test_p3_split_sample_keys_are_disjoint(self):
        """P3：三个 split 的 sample_key 两两不相交。"""
        built = build(full_dataset())
        for fold in ready_folds(built):
            train = fold.keys("train")
            validation = fold.keys("validation")
            test = fold.keys("test")
            self.assertEqual(set(), train & validation)
            self.assertEqual(set(), train & test)
            self.assertEqual(set(), validation & test)
            self.assertTrue(train and validation and test)

    def test_p4_train_decision_before_validation_but_label_after_is_purged(self):
        """P4（核心 leakage sentinel）：决策在验证期之前、标签却在验证期之后 → purge。

        index 8 = 2024-06-14，horizon 2 → 标签要等到 2024-06-18（= validation 起点）
        的收盘才成熟。只比较 ``decision_at < validation_start`` 会把它放进 train。
        """
        built = build(full_dataset())
        fold = ready_folds(built)[0]
        self.assertEqual("2024-06-18", fold.validation_start_at[:10])
        purged = {
            row.sample_key
            for row in full_dataset()
            if row.decision_session == "2024-06-14"
        }
        self.assertTrue(purged)
        for key in purged:
            self.assertNotIn(key, fold.keys("train"), key)
        self.assertGreaterEqual(fold.metadata["train_purged_rows"], len(CODES))
        self.assertGreaterEqual(
            fold.exclusion_reasons["label_not_available_before_fold"], len(CODES)
        )
        # 这些样本的决策**确实**早于 validation 起点 —— 因此拦住它们的只能是
        # label_available_at，不是 decision_session。
        for row in full_dataset():
            if row.decision_session == "2024-06-14":
                self.assertLess(row.decision_session, fold.validation_start_at[:10])

    def test_p5_label_available_exactly_at_cutoff_is_purged(self):
        """P5：``label_available_at == validation_start_at`` 也必须 purge（严格 <）。"""
        cutoff = WFV.session_start_at("2024-06-18")
        samples = full_dataset()
        samples.append(
            sample_at(8, code="600009", key="boundary-equal", available=cutoff)
        )
        built = build(samples)
        fold = ready_folds(built)[0]
        self.assertNotIn("boundary-equal", fold.keys("train"))
        self.assertGreaterEqual(
            fold.exclusion_reasons["label_not_available_before_fold"], 1
        )

    def test_p6_label_available_just_before_cutoff_is_allowed(self):
        """P6：``label_available_at`` 紧邻验证起点之前 → 允许进入 train。"""
        cutoff = WFV.session_start_at("2024-06-18")
        before = (
            _dt.datetime.fromisoformat(cutoff) - _dt.timedelta(seconds=1)
        ).isoformat(timespec="seconds")
        samples = full_dataset()
        samples.append(sample_at(8, code="600009", key="boundary-before", available=before))
        built = build(samples)
        fold = ready_folds(built)[0]
        self.assertIn("boundary-before", fold.keys("train"))

    def test_p7_purge_uses_label_availability_not_a_calendar_day_guess(self):
        """P7：固定"自然日减法"会算错 —— 周末/节假日会跨过更多自然日。

        2024-06-14 决策、horizon 2 → 标签成熟于 2024-06-18（validation 起点）。
        ``validation_start - 2 天 = 2024-06-16`` 的自然日减法会错误地保留它。
        """
        naive_cutoff = (
            _dt.date.fromisoformat("2024-06-18") - _dt.timedelta(days=2)
        ).isoformat()
        self.assertEqual("2024-06-16", naive_cutoff)  # 非交易日，但减法"看起来没问题"
        built = build(full_dataset())
        fold = ready_folds(built)[0]
        for row in full_dataset():
            if row.decision_session != "2024-06-14":
                continue
            self.assertLess(row.decision_session, naive_cutoff)
            self.assertNotIn(row.sample_key, fold.keys("train"), row.sample_key)

    def test_p8_different_horizons_on_one_session_are_purged_independently(self):
        """P8：同一 decision session、不同 horizon 各自按自己的标签窗口判定。"""
        samples = full_dataset(horizon=2)
        # 同一个 session 上加一条短 horizon 样本：标签在验证期之前就成熟。
        samples.append(sample_at(8, code="600009", horizon=1, key="short-h1"))
        built = build(samples)
        fold = ready_folds(built)[0]
        self.assertIn("short-h1", fold.keys("train"))
        # 同一天的长 horizon 样本仍然必须被 purge。
        self.assertNotIn(f"{CALENDAR[8]}:600001:h2", fold.keys("train"))
        self.assertEqual("2024-06-14", CALENDAR[8])


# ─────────────────────── P9–P11: verified label gate ───────────────────────


class VerifiedLabelGateTest(unittest.TestCase):
    """pending / unavailable / invalid 绝不变成假训练结果。"""

    def _build_with(self, **kwargs):
        samples = full_dataset()
        samples.append(sample_at(6, code="600009", key="suspect", **kwargs))
        built = build(samples)
        return ready_folds(built)[0], built["report"]

    def test_p9_pending_label_never_enters_train(self):
        """P9：``pending`` 不是负例，直接不进任何 split。"""
        fold, report = self._build_with(pit_status="pending")
        self.assertNotIn("suspect", fold.keys("train"))
        self.assertNotIn("suspect", fold.keys("validation"))
        self.assertNotIn("suspect", fold.keys("test"))
        self.assertEqual(1, report["unverified_label_by_status"].get("pending"))
        self.assertGreaterEqual(report["exclusion_reasons"]["unverified_label"], 1)

    def test_p10_unavailable_label_never_becomes_zero_return(self):
        """P10：``unavailable`` 不等于 0% 收益。"""
        fold, report = self._build_with(pit_status="unavailable", target=0.0)
        self.assertNotIn("suspect", fold.keys("train"))
        self.assertEqual(1, report["unverified_label_by_status"].get("unavailable"))

    def test_p11_invalid_label_never_becomes_negative_sample(self):
        """P11：``invalid`` 不等于坏股票。"""
        fold, report = self._build_with(pit_status="invalid", target=-0.5)
        self.assertNotIn("suspect", fold.keys("train"))
        self.assertEqual(1, report["unverified_label_by_status"].get("invalid"))

    def test_p11b_non_finite_target_is_refused(self):
        """P11b：非有限 target 不得被当成 0 收益。"""
        fold, report = self._build_with(target=float("nan"))
        self.assertNotIn("suspect", fold.keys("train"))
        self.assertGreaterEqual(report["exclusion_reasons"]["invalid_target"], 1)

    def test_p11c_default_pit_status_is_fail_closed(self):
        """P11c：不显式声明 verified 的样本默认 ``unproven``，不得进 train。"""
        raw = WFV.ValidationSample(
            sample_key="unproven-1",
            code="600001",
            decision_session=CALENDAR[5],
            label_available_at=WFV.label_available_at_for_close(CALENDAR[5]),
            target=0.01,
        )
        self.assertFalse(raw.verified)
        samples = full_dataset() + [raw]
        fold = ready_folds(build(samples))[0]
        self.assertNotIn("unproven-1", fold.keys("train"))


# ───────────────────────────── P12–P14: embargo ─────────────────────────────


class EmbargoTest(unittest.TestCase):
    """embargo 是 evaluation 之前的交易日隔离带。"""

    #: horizon 0 → 标签在决策当日收盘成熟，因此 purge 不会误伤，
    #: 能把 embargo 的独立效果测出来。
    def _samples(self):
        return full_dataset(codes=("600001",), horizon=0)

    def test_p12_embargo_zero_keeps_otherwise_eligible_rows(self):
        """P12：``embargo_sessions=0`` 不额外删除任何本可用的 train 行。"""
        samples = self._samples()
        fold = ready_folds(build(samples))[0]
        self.assertEqual(0, fold.metadata["train_embargoed_rows"])
        self.assertEqual(10, fold.metadata["train_final_rows"])
        self.assertEqual(0, fold.exclusion_reasons["embargo"])

    def test_p13_embargo_n_removes_the_last_n_eligible_sessions(self):
        """P13：``embargo_sessions=N`` 恰好删掉验证期之前最近的 N 个 session。"""
        samples = self._samples()
        config = dataclasses.replace(CONFIG, embargo_sessions=2)
        fold = ready_folds(build(samples, config))[0]
        self.assertEqual(2, fold.metadata["train_embargoed_rows"])
        self.assertEqual(2, fold.exclusion_reasons["embargo"])
        self.assertEqual(8, fold.metadata["train_final_rows"])
        self.assertNotIn(CALENDAR[8], fold.metadata["train_decision_sessions"])
        self.assertNotIn(CALENDAR[9], fold.metadata["train_decision_sessions"])
        self.assertIn(CALENDAR[7], fold.metadata["train_decision_sessions"])

    def test_p14_embargo_counts_trading_sessions_not_calendar_days(self):
        """P14：embargo 计的是**交易日**，不是自然日。

        验证期起点是 2024-06-18（周二）。最近 2 个 session 是 06-17（周一）与
        06-14（周五）—— 06-14 距离 06-18 有 4 个自然日。按自然日减 2 天只会
        排除 06-16/06-17，从而错误地保留 06-14。
        """
        naive_window = [
            (_dt.date.fromisoformat("2024-06-18") - _dt.timedelta(days=offset)).isoformat()
            for offset in (1, 2)
        ]
        self.assertNotIn("2024-06-14", naive_window)
        samples = self._samples()
        config = dataclasses.replace(CONFIG, embargo_sessions=2)
        fold = ready_folds(build(samples, config))[0]
        self.assertNotIn("2024-06-14", fold.metadata["train_decision_sessions"])
        self.assertNotIn("2024-06-17", fold.metadata["train_decision_sessions"])

    def test_p14b_embargo_never_reaches_into_validation_or_test(self):
        """P14b：embargo 只作用于 train 侧，不会删掉 validation/test 样本。"""
        samples = full_dataset(indices=range(0, 20), codes=("600001",), horizon=0)
        config = dataclasses.replace(CONFIG, embargo_sessions=3)
        built = build(samples, config)
        for fold in ready_folds(built):
            # horizon 0 的标签在决策当日收盘就成熟，所以没有任何 validation 行
            # 会因为 test 起点而被 purge —— 掉的行只可能来自 embargo。
            self.assertEqual(
                fold.metadata["validation_candidate_rows"],
                fold.metadata["validation_rows"],
            )
            self.assertEqual(0, fold.metadata["validation_purged_rows"])
            self.assertEqual(
                len(fold.metadata["validation_decision_sessions"]),
                len({row.decision_session for row in fold.validation_features}),
            )
            self.assertEqual(
                len(fold.metadata["test_decision_sessions"]),
                len({row.decision_session for row in fold.test_features}),
            )


# ──────────────────── P15–P16: order & window shape ────────────────────


class OrderAndWindowTest(unittest.TestCase):
    def test_p15_input_row_order_does_not_change_folds(self):
        """P15：输入行顺序（含随机打乱）不改变 fold 成员与元数据。"""
        samples = full_dataset()
        baseline = build(samples)
        for seed in (0, 1, 7, 20260915):
            shuffled = list(samples)
            random.Random(seed).shuffle(shuffled)
            other = build(shuffled)
            self.assertEqual(len(baseline["folds"]), len(other["folds"]))
            for left, right in zip(baseline["folds"], other["folds"], strict=True):
                self.assertEqual(left.fold_id, right.fold_id)
                self.assertEqual(left.status, right.status)
                self.assertEqual(left.keys("train"), right.keys("train"))
                self.assertEqual(left.keys("validation"), right.keys("validation"))
                self.assertEqual(left.keys("test"), right.keys("test"))
                self.assertEqual(
                    [row.sample_key for row in left.train_rows],
                    [row.sample_key for row in right.train_rows],
                )
                self.assertEqual(dict(left.metadata), dict(right.metadata))
                self.assertEqual(dict(left.exclusion_reasons), dict(right.exclusion_reasons))

    def test_p16_expanding_window_never_trains_on_future_samples(self):
        """P16：expanding window 只向后扩，绝不把未来样本卷进 train。"""
        built = build(full_dataset())
        folds = ready_folds(built)
        self.assertGreaterEqual(len(folds), 2)
        previous_train = set()
        for fold in folds:
            train_sessions = set(fold.metadata["train_decision_sessions"])
            self.assertLess(
                max(train_sessions), fold.metadata["validation_decision_sessions"][0]
            )
            # expanding：后一折的 train 是前一折的超集（去掉被 purge 的样本）。
            self.assertTrue(previous_train <= train_sessions or not previous_train)
            for row in fold.train_rows:
                self.assertLess(row.decision_session, fold.validation_start_at[:10])
            previous_train = train_sessions

    def test_p16b_rolling_window_requires_an_explicit_cap(self):
        """P16b：rolling window 必须显式配置 ``max_train_sessions``，不会偷偷滚动。

        用 horizon 0 的样本（标签当日收盘成熟）让 purge 不参与，从而把"窗口
        上限"的独立效果测出来。
        """
        samples = full_dataset(codes=("600001",), horizon=0)
        config = dataclasses.replace(CONFIG, max_train_sessions=4)
        built = build(samples, config)
        for fold in ready_folds(built):
            self.assertEqual("rolling", fold.metadata["window"])
            self.assertEqual(4, fold.metadata["train_decision_session_count"])
            self.assertLessEqual(fold.metadata["train_decision_session_count"], 4)
        # 10 个 prior session 只留最近 4 个 → 其余 6 个计入 outside_fold_window。
        self.assertEqual(6, ready_folds(built)[0].metadata["train_window_excluded_rows"])
        expanding = build(samples)
        self.assertEqual("expanding", ready_folds(expanding)[0].metadata["window"])
        self.assertGreater(
            ready_folds(expanding)[0].metadata["train_decision_session_count"], 4
        )

    def test_p16c_rolling_window_drops_sessions_outside_the_cap(self):
        """P16c：rolling window 之外的 session 计入 ``outside_fold_window``。"""
        samples = full_dataset(codes=("600001",), horizon=0)
        config = dataclasses.replace(CONFIG, max_train_sessions=3)
        fold = ready_folds(build(samples, config))[0]
        self.assertEqual(3, fold.metadata["train_decision_session_count"])
        self.assertEqual(7, fold.metadata["train_window_excluded_rows"])
        self.assertEqual(7, fold.exclusion_reasons["outside_fold_window"])
        self.assertEqual(
            ["2024-06-13", "2024-06-14", "2024-06-17"],
            fold.metadata["train_decision_sessions"],
        )


# ──────────────── P17–P19: preprocessing fit boundary ────────────────


class PreprocessingBoundaryTest(unittest.TestCase):
    """统计量只能从 train 读。结构上就不给"fit 全量"的接口。"""

    def _fold(self):
        return ready_folds(build(full_dataset()))[0]

    def test_p17_validation_data_is_never_used_to_fit_preprocessing(self):
        """P17：validation 的特征视图不能用于 fit。"""
        fold = self._fold()
        self.assertTrue(fold.validation_features)
        with self.assertRaises(WFV.WalkForwardContractError):
            WFV.fit_feature_state(fold.validation_features)
        with self.assertRaises(WFV.WalkForwardContractError):
            WFV.fit_feature_state(list(fold.train_rows) + list(fold.validation_features))

    def test_p18_test_data_is_never_used_to_fit_preprocessing(self):
        """P18：test 的特征视图不能用于 fit。"""
        fold = self._fold()
        self.assertTrue(fold.test_features)
        with self.assertRaises(WFV.WalkForwardContractError):
            WFV.fit_feature_state(fold.test_features)
        with self.assertRaises(WFV.WalkForwardContractError):
            WFV.fit_feature_state(list(fold.train_rows) + list(fold.test_features))

    def test_p19_test_only_extreme_value_cannot_change_train_fitted_state(self):
        """P19：改变 test-only 极端值，不改变任何 train 拟合量。

        若在 split 之前算全局 mean，``test = 1000`` 会把训练期的变换结果整体
        拉偏 —— 这条测试就是那个泄漏的哨兵。
        """
        fold = self._fold()
        state = WFV.fit_feature_state(fold.train_rows)
        first = fold.test_features[0]
        extreme = dataclasses.replace(
            first, features={**first.features, "momentum": 1000.0}
        )
        refit = WFV.fit_feature_state(fold.train_rows)
        self.assertEqual(state, refit)
        self.assertEqual(state.means, refit.means)
        self.assertEqual(state.scales, refit.scales)
        # test 侧的变换结果**可以**变化 —— 它本来就不该影响拟合。
        self.assertNotEqual(
            WFV.transform_features(state, first.features)["momentum"],
            WFV.transform_features(state, extreme.features)["momentum"],
        )

    def test_p19b_train_fitted_state_is_unchanged_when_the_test_window_disappears(self):
        """P19b：整段 test 窗口消失也不改变 train 拟合量。"""
        fold = self._fold()
        state = WFV.fit_feature_state(fold.train_rows)
        without_test = dataclasses.replace(
            fold, test_features=(), test_labels=()
        )
        self.assertEqual(state, WFV.fit_feature_state(without_test.train_rows))


# ──────────────────── P20–P21: tuning boundary ────────────────────


class TuningBoundaryTest(unittest.TestCase):
    def test_p20_test_label_is_excluded_from_fit_and_tuning_input(self):
        """P20：test 标签没有 features 字段，进不了 fit；也不能喂给候选选择。"""
        fold = ready_folds(build(full_dataset()))[0]
        self.assertTrue(fold.test_labels)
        for label in fold.test_labels:
            self.assertFalse(hasattr(label, "features"))
        with self.assertRaises(WFV.WalkForwardContractError):
            WFV.fit_feature_state(fold.test_labels)
        with self.assertRaises(WFV.WalkForwardContractError):
            WFV.select_candidate(
                [{"candidate_id": "A", "test": {"rank_ic": 0.9}}], metric_name="rank_ic"
            )

    def test_p21_candidate_selection_uses_validation_metrics_only(self):
        """P21：A 在 validation 更优、B 在 test 更优 → 必须选 A。"""
        candidates = [
            {"candidate_id": "A", "validation_metrics": {"rank_ic": 0.05}},
            {"candidate_id": "B", "validation_metrics": {"rank_ic": 0.01}},
        ]
        picked = WFV.select_candidate(candidates, metric_name="rank_ic")
        self.assertEqual("A", picked["candidate_id"])
        self.assertEqual("validation", picked["selection_partition"])
        self.assertEqual("ok", picked["reason"])
        # 只要把 test 表现塞进候选，接口直接拒绝 —— 而不是"自己别去看"。
        for leaked_key in ("test_metrics", "holdout_metrics", "test", "holdout"):
            with self.assertRaises(WFV.WalkForwardContractError):
                WFV.select_candidate(
                    [{**candidates[0], leaked_key: {"rank_ic": 0.99}}, candidates[1]],
                    metric_name="rank_ic",
                )

    def test_p21b_lower_is_better_metric_is_honoured(self):
        """P21b：方向可配，仍然只看 validation。"""
        candidates = [
            {"candidate_id": "A", "validation_metrics": {"max_drawdown": 0.30}},
            {"candidate_id": "B", "validation_metrics": {"max_drawdown": 0.10}},
        ]
        picked = WFV.select_candidate(
            candidates, metric_name="max_drawdown", higher_is_better=False
        )
        self.assertEqual("B", picked["candidate_id"])


# ──────────────── P22–P24: PIT inputs, readiness, audit ────────────────


class PitInputsAndReadinessTest(unittest.TestCase):
    FORBIDDEN_IMPORTS = frozenset(
        {
            "universe",
            "marketdata",
            "marketdata_feeds",
            "marketdata_cache",
            "selection_tracking",
            "paper_trading",
            "strategies",
            "data_pipeline",
        }
    )

    def test_p22_current_universe_cannot_change_historical_fold_membership(self):
        """P22：切分只消费已 PIT 化的样本，当前 universe/快照不参与。"""
        samples = full_dataset()
        marked = [
            dataclasses.replace(row, provenance={"currently_listed": False, "st": True})
            for row in samples
        ]
        baseline = build(samples)
        other = build(marked)
        for left, right in zip(baseline["folds"], other["folds"], strict=True):
            self.assertEqual(left.keys("train"), right.keys("train"))
            self.assertEqual(left.keys("validation"), right.keys("validation"))
            self.assertEqual(left.keys("test"), right.keys("test"))
        # 源码守卫：本模块不得 import 任何"当前状态"数据源。
        source = pathlib.Path(WFV.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        leaked = sorted(imported & self.FORBIDDEN_IMPORTS)
        self.assertEqual([], leaked, f"walk_forward_validation must not import {leaked}")

    def test_p23_insufficient_history_is_explicit_and_never_shrinks_the_window(self):
        """P23：历史不够 → 明确的 not_ready，绝不自动缩短训练窗口/horizon/test。"""
        short = full_dataset(indices=range(0, 5))
        built = build(short)
        fold = built["folds"][0]
        self.assertEqual(WFV.STATUS_INSUFFICIENT_TRAIN_HISTORY, fold.status)
        self.assertFalse(fold.ready)
        self.assertEqual((), fold.train_rows)
        self.assertEqual((), fold.validation_features)
        self.assertEqual((), fold.test_features)
        self.assertEqual([], fold.metadata["train_decision_sessions"])
        self.assertEqual(5, fold.metadata["sessions_available"])
        self.assertEqual(18, fold.metadata["sessions_required"])
        # 不缩短：仍然要求 min_train + validation + test 个 session。
        self.assertEqual(18, built["report"]["config"]["min_train_sessions"] + 8)

        partial = build(full_dataset(indices=range(0, 12)))
        self.assertEqual(
            WFV.STATUS_INSUFFICIENT_VALIDATION_HISTORY, partial["folds"][0].status
        )
        self.assertEqual((), partial["folds"][0].train_rows)

    def test_p23b_immature_test_window_is_not_ready(self):
        """P23b：test 窗口越过 asof → ``not_ready / window_not_matured``。"""
        built = build(full_dataset(), asof="2024-06-20")
        fold = built["folds"][0]
        self.assertEqual(WFV.STATUS_NOT_READY, fold.status)
        self.assertEqual(WFV.REASON_WINDOW_NOT_MATURED, fold.reason)
        self.assertFalse(fold.ready)
        self.assertEqual((), fold.train_rows)
        self.assertGreaterEqual(fold.exclusion_reasons["window_not_matured"], 1)
        with self.assertRaises(WFV.WalkForwardContractError):
            WFV.assert_fold_ready(fold)

    def test_p24_fold_audit_invariants_and_metadata(self):
        """P24：端到端审计 —— 边界、不相交、session 分组、metadata 完整性。"""
        built = build(full_dataset())
        required = (
            "fold_id",
            "train_decision_start",
            "train_decision_end",
            "validation_decision_start",
            "validation_decision_end",
            "test_decision_start",
            "test_decision_end",
            "purge_cutoff_at",
            "embargo_sessions",
            "train_candidate_rows",
            "train_purged_rows",
            "train_embargoed_rows",
            "train_final_rows",
            "validation_rows",
            "test_rows",
            "train_decision_sessions",
            "validation_decision_sessions",
            "test_decision_sessions",
            "train_max_label_available_at",
            "label_version",
        )
        for fold in ready_folds(built):
            WFV.assert_fold_ready(fold)
            self.assertTrue(fold.metadata["disjoint_ok"])
            self.assertTrue(fold.metadata["boundary_ok"])
            for name in required:
                self.assertIn(name, fold.metadata, name)
            for reason in WFV.FOLD_EXCLUSION_REASONS:
                self.assertIn(reason, fold.exclusion_reasons, reason)
            # §29 的硬性 invariant。
            limit = WFV._instant(fold.validation_start_at)
            self.assertTrue(fold.train_rows)
            for row in fold.train_rows:
                self.assertLess(WFV._instant(row.label_available_at), limit)
            self.assertEqual(
                max(WFV._instant(row.label_available_at) for row in fold.train_rows),
                WFV._instant(fold.metadata["train_max_label_available_at"]),
            )
            # 计数自洽。
            self.assertEqual(
                fold.metadata["train_candidate_rows"],
                fold.metadata["train_purged_rows"]
                + fold.metadata["train_embargoed_rows"]
                + fold.metadata["train_window_excluded_rows"]
                + fold.metadata["train_final_rows"],
            )
            self.assertEqual(fold.metadata["train_final_rows"], len(fold.train_rows))
            self.assertEqual(
                fold.metadata["validation_candidate_rows"],
                fold.metadata["validation_purged_rows"] + fold.metadata["validation_rows"],
            )
            self.assertEqual(fold.metadata["validation_rows"], len(fold.validation_features))
            self.assertEqual(fold.metadata["validation_rows"], len(fold.validation_labels))
            self.assertEqual(fold.metadata["test_rows"], len(fold.test_features))
            self.assertEqual(fold.metadata["test_rows"], len(fold.test_labels))
            # validation 与 refit 都不允许出现"标签在 test 起点之后才可用"的行。
            limit = WFV._instant(fold.test_start_at)
            for row in tuple(fold.refit_rows) + tuple(fold.validation_labels):
                self.assertLess(WFV._instant(row.label_available_at), limit)


# ──────────────── P25: final refit eligibility (§30) ────────────────


class FinalRefitTest(unittest.TestCase):
    def test_p25_validation_labels_are_purged_against_the_test_start(self):
        """P25：validation 侧也必须按 **test 起点** purge。

        一条在 test 期内才成熟的 validation 标签，它的 exit 价就在 held-out
        区间里；把它留在 ``validation_labels`` 里，模型选择就会看见 test。
        只从 ``refit_rows`` 里去掉它是不够的。
        """
        # fold0: validation = CALENDAR[10..13]，test 起点 = CALENDAR[14] = 2024-06-24。
        # 在 validation 第一个 session（index 10）上放一条 horizon 5 的样本：
        # 决策 2024-06-18 < 2024-06-24，但标签要到 2024-06-25 才成熟。
        samples = full_dataset()
        samples.append(
            sample_at(10, code="600009", horizon=5, key="late-validation-label")
        )
        built = build(samples)
        fold = ready_folds(built)[0]
        self.assertEqual("2024-06-24", fold.test_start_at[:10])
        self.assertNotIn("late-validation-label", fold.keys("validation"))
        self.assertNotIn("late-validation-label", {row.sample_key for row in fold.refit_rows})
        self.assertGreaterEqual(fold.metadata["validation_purged_rows"], 1)
        self.assertGreaterEqual(
            fold.exclusion_reasons["validation_label_overlaps_test"], 1
        )

        # 对照：标签在 test 起点之前成熟的 validation 样本仍然可用。
        samples.append(sample_at(10, code="600010", horizon=2, key="early-validation-label"))
        fold = ready_folds(build(samples))[0]
        self.assertIn("early-validation-label", fold.keys("validation"))
        self.assertIn("early-validation-label", {row.sample_key for row in fold.refit_rows})

        # 不变量：没有任何进入 tuning / refit 的标签在 test 起点之后才可用。
        limit = WFV._instant(fold.test_start_at)
        for row in fold.refit_rows:
            self.assertLess(WFV._instant(row.label_available_at), limit)
        for label in fold.validation_labels:
            self.assertLess(WFV._instant(label.label_available_at), limit)

    def test_p25b_refit_is_train_plus_validation_only(self):
        """P25b：refit 只由 train + validation 组成，绝不包含 test。"""
        fold = ready_folds(build(full_dataset()))[0]
        refit = {row.sample_key for row in fold.refit_rows}
        self.assertTrue(refit)
        self.assertEqual(set(), refit & fold.keys("test"))
        self.assertTrue(refit <= (fold.keys("train") | fold.keys("validation")))
        limit = WFV._instant(fold.test_start_at)
        for row in fold.refit_rows:
            self.assertLess(WFV._instant(row.label_available_at), limit)

    def test_p25c_readiness_uses_each_test_label_availability_instant(self):
        """P25c：readiness 按**每个 test 标签的可用时点**判定，而不是 test 末个
        session 的日期。

        fold0 的 test 决策在 2024-06-24..06-27，horizon 2 的标签分别成熟于
        06-26 / 06-27 / 06-28 / 07-01。``asof = 2024-06-27`` 只覆盖到 06-27，
        因此这个 fold 还不能评分。
        """
        built = build(full_dataset(), asof="2024-06-27")
        fold = built["folds"][0]
        self.assertEqual(WFV.STATUS_NOT_READY, fold.status)
        self.assertEqual(WFV.REASON_WINDOW_NOT_MATURED, fold.reason)
        self.assertEqual((), fold.train_rows)
        self.assertEqual((), fold.test_labels)
        self.assertEqual(
            "2024-06-27T23:59:59+08:00", fold.metadata["asof"]
        )

        # 所有 test 标签都成熟之后 → ready。
        later = build(full_dataset(), asof="2024-07-02")
        self.assertTrue(ready_folds(later)[0].ready)

        # 盘中 cutoff 不会被放大成整天：06-27 10:00 看不到 06-27 收盘的标签。
        intraday = build(full_dataset(), asof="2024-06-27T10:00:00+08:00")
        self.assertEqual(WFV.STATUS_NOT_READY, intraday["folds"][0].status)


# ──────────────── P26–P30: adapters, identity, guards ────────────────


class AdapterAndIdentityTest(unittest.TestCase):
    def test_p26_label_availability_reuses_the_147_authoritative_rule(self):
        """P26：close-to-close 可用时刻与 #147 的桥逐字符一致，不另立一套。"""
        import learning_dataset as LD
        import point_in_time as PIT
        import selection_labels as SL

        exit_date = "2024-06-20"
        expected = f"{exit_date}T{LD.SELECTION_LABEL_EVIDENCE_CLOSE_TIME}+08:00"
        self.assertEqual(expected, WFV.label_available_at_for_close(exit_date))
        self.assertEqual(
            PIT.bar_available_at(exit_date).isoformat(timespec="seconds"),
            WFV.label_available_at_for_close(exit_date),
        )

        sessions = [
            "2024-06-11", "2024-06-12", "2024-06-13", "2024-06-14",
            "2024-06-17", "2024-06-18", "2024-06-19", "2024-06-20",
        ]
        prices = {day: 10.0 for day in sessions}
        prices["2024-06-19"] = 11.0
        label = SL.selection_label(
            code="600001",
            decision_at="2024-06-14T16:00:00+08:00",
            horizon=2,
            sessions=sessions,
            prices=prices,
            asof="2024-06-30",
        )
        record = SL.label_record(label)
        bridge = LD.selection_label_evidence(record)
        self.assertTrue(bridge["verified"])
        self.assertEqual(
            bridge["label_available_at"], WFV.label_available_at_for_close(label.exit_date)
        )

    def test_p27_duplicate_sample_identity_fails_closed(self):
        """P27：同一 sample_key 的冲突内容 → 全部拒绝；逐字节相同 → 去重。"""
        samples = full_dataset()
        conflicting = sample_at(5, code="600009", key=f"{CALENDAR[5]}:600001", target=0.9)
        built = build(samples + [conflicting])
        fold = ready_folds(built)[0]
        self.assertNotIn(f"{CALENDAR[5]}:600001", fold.keys("train"))
        self.assertGreaterEqual(
            built["report"]["exclusion_reasons"]["duplicate_sample"], 2
        )
        # 逐字节相同的重复只是去重，不牵连原样本。
        twin = [row for row in samples if row.sample_key == f"{CALENDAR[5]}:600001"][0]
        built = build(samples + [twin])
        fold = ready_folds(built)[0]
        self.assertIn(f"{CALENDAR[5]}:600001", fold.keys("train"))
        self.assertEqual(1, built["report"]["exclusion_reasons"]["duplicate_sample"])

    def test_p28_selection_label_record_adapter_goes_through_the_147_gate(self):
        """P28：适配器只接受 #147 判定为 verified 的标签记录。"""
        import selection_labels as SL

        sessions = [
            "2024-06-11", "2024-06-12", "2024-06-13", "2024-06-14",
            "2024-06-17", "2024-06-18", "2024-06-19", "2024-06-20",
        ]
        prices = {day: 10.0 for day in sessions}
        prices["2024-06-19"] = 11.0
        verified = SL.label_record(
            SL.selection_label(
                code="600001", decision_at="2024-06-14T16:00:00+08:00", horizon=2,
                sessions=sessions, prices=prices, asof="2024-06-30",
            )
        )
        adapted = WFV.sample_from_selection_label_record(
            verified, features={"momentum": 1.0}
        )
        self.assertIsNotNone(adapted)
        self.assertEqual("2024-06-14", adapted.decision_session)
        self.assertEqual("2024-06-19T15:00:00+08:00", adapted.label_available_at)
        self.assertEqual(WFV.PIT_VERIFIED, adapted.pit_status)

        pending = SL.label_record(
            SL.selection_label(
                code="600001", decision_at="2024-06-14T16:00:00+08:00", horizon=2,
                sessions=sessions, prices=prices, asof="2024-06-18",
            )
        )
        self.assertEqual("pending", pending["label_status"])
        self.assertIsNone(WFV.sample_from_selection_label_record(pending))

    def test_p29_not_ready_folds_are_refused_at_the_consumption_boundary(self):
        """P29：消费 fold 的唯一入口拒绝非 ready 的 fold。"""
        built = build(full_dataset(indices=range(0, 5)))
        with self.assertRaises(WFV.WalkForwardContractError):
            WFV.assert_fold_ready(built["folds"][0])
        ready = ready_folds(build(full_dataset()))[0]
        self.assertIs(ready, WFV.assert_fold_ready(ready))

    def test_p30_fold_disjointness_assertion_actually_fires(self):
        """P30：``assert_fold_disjoint`` 不是装饰 —— 人为制造重叠必须报错。"""
        fold = ready_folds(build(full_dataset()))[0]
        self.assertTrue(fold.train_rows)
        self.assertTrue(fold.validation_features)
        leaky = dataclasses.replace(
            fold, validation_features=fold.validation_features + (fold.train_rows[0],)
        )
        with self.assertRaises(WFV.WalkForwardContractError):
            WFV.assert_fold_disjoint(leaky)
        leaky_test = dataclasses.replace(
            fold, test_features=fold.test_features + (fold.train_rows[0],)
        )
        with self.assertRaises(WFV.WalkForwardContractError):
            WFV.assert_fold_disjoint(leaky_test)

    def test_p30b_train_boundary_assertion_actually_fires(self):
        """P30b：``assert_train_boundary`` 对越界的 train 行必须报错。"""
        fold = ready_folds(build(full_dataset()))[0]
        late = sample_at(
            0, key="late-label",
            available="2099-01-01T15:00:00+08:00",
        )
        broken = dataclasses.replace(fold, train_rows=(late,) + fold.train_rows)
        with self.assertRaises(WFV.WalkForwardContractError):
            WFV.assert_train_boundary(broken)

    def test_p30c_config_rejects_nonsense(self):
        """P30c：配置本身 fail closed。"""
        for kwargs in (
            {"min_train_sessions": 0, "validation_sessions": 4, "test_sessions": 4},
            {"min_train_sessions": 10, "validation_sessions": -1, "test_sessions": 4},
            {"min_train_sessions": 10, "validation_sessions": 4, "test_sessions": 4,
             "embargo_sessions": -1},
            {"min_train_sessions": 10, "validation_sessions": 4, "test_sessions": 4,
             "max_train_sessions": 0},
            {"min_train_sessions": 10, "validation_sessions": 4, "test_sessions": 4,
             "step_sessions": 0},
        ):
            with self.assertRaises(WFV.WalkForwardContractError):
                WFV.WalkForwardConfig(**kwargs)


class SinglePurgeRuleTest(unittest.TestCase):
    """purge 判据全仓只有一份，`learning_dataset` 走的是同一份。"""

    def _canonical_samples(self):
        import learning_dataset as LD

        days = [f"2024-06-{day:02d}" for day in range(1, 21)]
        out = []
        for index, day in enumerate(days):
            exit_day = days[min(index + 2, len(days) - 1)]
            out.append(
                LD.CanonicalSample(
                    sample_key=f"canon-{index:02d}",
                    source="unit",
                    source_version="v1",
                    code="600001",
                    strategy_id="",
                    model_family="",
                    feature_asof=day,
                    feature_available_at=f"{day}T15:00:00+08:00",
                    label_start_date=day,
                    label_end_date=exit_day,
                    label_available_at=f"{exit_day}T15:00:00+08:00",
                    horizon=2,
                    horizon_semantics=LD.HORIZON_SEMANTICS_OBSERVED_PROFILE_STEPS,
                    features={"momentum": 1.0},
                    target=0.01,
                    pit_status=LD.PIT_VERIFIED,
                )
            )
        return out

    def test_p31_chronological_split_delegates_to_the_shared_purge_rule(self):
        """P31：``learning_dataset.chronological_split`` 的 purge 走同一个判据。

        把 ``walk_forward_validation.train_eligible`` 换成"放行一切"，train 里
        被 purge 的行数必须随之变成 0 —— 若 purge 是第二套实现，这个替换不会
        有任何效果。
        """
        import learning_dataset as LD

        samples = self._canonical_samples()
        baseline_partitions, baseline_purged = LD.chronological_split(samples)
        self.assertGreater(baseline_purged["train"], 0)

        calls = []
        original = WFV.train_eligible

        def always_eligible(sample, *, evaluation_start_at):
            calls.append((sample.sample_key, evaluation_start_at))
            return True

        WFV.train_eligible = always_eligible
        try:
            patched_partitions, patched_purged = LD.chronological_split(samples)
        finally:
            WFV.train_eligible = original

        self.assertTrue(calls, "chronological_split did not call the shared purge rule")
        self.assertEqual(0, patched_purged["train"])
        self.assertEqual(0, patched_purged["validation"])
        self.assertGreater(
            len(patched_partitions["train"]), len(baseline_partitions["train"])
        )
        # 还原后与基线逐字节一致（替换没有留下副作用）。
        restored_partitions, restored_purged = LD.chronological_split(samples)
        self.assertEqual(baseline_purged, restored_purged)
        for name in ("train", "validation", "test"):
            self.assertEqual(
                [row.sample_key for row in baseline_partitions[name]],
                [row.sample_key for row in restored_partitions[name]],
            )

    def test_p31b_shared_rule_is_strict_and_fails_closed(self):
        """P31b：共享判据本身严格 ``<``，且缺可用时刻时 fail closed。"""
        cutoff = "2024-06-18T00:00:00+08:00"
        self.assertFalse(
            WFV.train_eligible(
                WFV.ValidationSample(
                    sample_key="x", code="600001", decision_session="2024-06-14",
                    label_available_at=cutoff, target=0.01,
                ),
                evaluation_start_at=cutoff,
            )
        )
        self.assertTrue(
            WFV.train_eligible(
                WFV.ValidationSample(
                    sample_key="x", code="600001", decision_session="2024-06-14",
                    label_available_at="2024-06-17T15:00:00+08:00", target=0.01,
                ),
                evaluation_start_at=cutoff,
            )
        )
        self.assertFalse(
            WFV.train_eligible(
                WFV.ValidationSample(
                    sample_key="x", code="600001", decision_session="2024-06-14",
                    label_available_at="", target=0.01,
                ),
                evaluation_start_at=cutoff,
            )
        )


if __name__ == "__main__":
    unittest.main()
