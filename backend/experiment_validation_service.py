# -*- coding: utf-8 -*-
"""Canonical R29 验证与 R30 稳健性的**应用流程**。

**它回答什么**：给定一份请求（spec + owner 指纹 + walk-forward 配置），如何按
owner 声明的口径装配仓库、复核身份、构建数据集、跑 runner、持久化结果，以及失败
属于哪一类（请求非法 / run 不存在 / 输入不可用 / 持久化冲突）。

**它不回答什么**：HTTP 状态码、请求体校验、鉴权 —— 那是 `api_adaptive` 的事。
本模块只抛**有类型的异常**，由路由做唯一一次映射。

**为什么单独一层**：这两条路由以前各自在函数体里装配约 17 个仓库、内联复算
owner 身份、直接 `raise HTTPException`。后果是"哪条算请求非法、哪条算输入不可用"
只能读路由正文才知道，CLI/批处理想复用得复制一份。现在流程只有一份，错误分类
也是显式的；路由只剩解析请求 + 映射异常。

**边界**：连接由调用方给（`connection_factory`），本模块不建库、不迁移、不提交
调用方事务之外的东西；`created_at` / `build_revision` 由调用方注入，便于测试固定。
"""
from __future__ import annotations

from typing import Any, Callable, Mapping


class ExperimentValidationRequestInvalid(ValueError):
    """请求本身不合法（4xx）。``reason`` 永远是稳定标识符，不含 SQL/路径。"""

    def __init__(self, reason: str, *, fallback: str) -> None:
        text = str(reason)
        self.reason = text if text.isidentifier() else fallback
        super().__init__(self.reason)


class ExperimentValidationRunMissing(LookupError):
    """请求的 baseline run 不存在。"""

    def __init__(self, reason: str = "validation_run_not_found") -> None:
        self.reason = reason
        super().__init__(reason)


class ExperimentRobustnessBaselineError(ValueError):
    """稳健性 runner 拒绝了这份 baseline（422，保留原文）。"""


class ExperimentRobustnessConflict(RuntimeError):
    """稳健性报告持久化冲突（409）。"""


class ExperimentValidationInputsUnavailable(RuntimeError):
    """输入不可用（5xx）。刻意不携带内部细节。"""


_VALIDATION_RUN_FIELDS = (
    "id", "run_key", "experiment_fingerprint", "strategy_id", "strategy_version",
    "strategy_checksum", "calendar_fingerprint", "universe_archive_fingerprint",
    "financial_archive_fingerprint", "tradability_evidence_fingerprint",
    "market_archive_fingerprint", "dataset_fingerprint",
    "validation_status", "validation_evidence", "result", "folds", "runner_version",
    "runner_code_revision", "created_at",
)

_OWNER_IDENTITY_FIELDS = (
    "calendar_fingerprint", "universe_archive_fingerprint",
    "financial_archive_fingerprint", "tradability_evidence_fingerprint",
    "market_archive_fingerprint", "dataset_fingerprint", "strategy_id",
    "strategy_version", "strategy_checksum",
)


def validation_run_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in _VALIDATION_RUN_FIELDS}


def robustness_owner_identities(pinned: Mapping[str, Any],
                                baseline: Mapping[str, Any]) -> dict[str, Any]:
    """Baseline 的 owner 身份必须与请求里钉住的那份**逐字段**相等。"""
    expected = {key: baseline.get(key) for key in _OWNER_IDENTITY_FIELDS}
    if dict(pinned or {}) != expected:
        raise ValueError("baseline_owner_identity_mismatch")
    return expected


def _experiment_spec(payload: Mapping[str, Any], EC):
    raw = dict(payload["spec"])
    strategy = raw.get("strategy")
    if not isinstance(strategy, dict):
        raise ValueError("strategy_identity_required")
    raw["strategy"] = EC.StrategyIdentity(**strategy)
    date_range = raw.get("date_range") or {}
    raw["start_date"] = raw.pop("start_date", date_range.get("start"))
    raw["end_date"] = raw.pop("end_date", date_range.get("end"))
    raw.pop("date_range", None)
    return EC.ExperimentSpec(**raw)


def validate_experiment(
    payload: Mapping[str, Any],
    *,
    connection_factory: Callable[..., Any],
    build_revision: str,
    created_at: str,
) -> dict[str, Any]:
    """Run one exact-identity validation offline; never refresh or select inputs."""
    try:
        import experiment_contract as EC
        import experiment_execution_model  # noqa: F401
        import financial_feature_evidence as FFE
        import experiment_validation_repository as EVR
        import historical_financial_archive as HFA
        import experiment_validation_runner as runner
        import historical_market_archive as HMA
        import historical_session_calendar as HSC
        import historical_universe_archive as HUA
        import learning_dataset as LD
        import strategy_registry as SR
        import tradability_archive as TA
        import walk_forward_validation as WFV

        spec = _experiment_spec(payload, EC)
        if (spec.market_data_fingerprint != payload["market_archive_fingerprint"]
                or spec.universe_fingerprint != payload["universe_archive_fingerprint"]
                or spec.parameter_set.get("validation_calendar_fingerprint") != payload["calendar_fingerprint"]
                or spec.parameter_set.get("financial_archive_fingerprint") != payload.get("financial_archive_fingerprint")):
            raise ValueError("owner_identity_mismatch")
        config = WFV.WalkForwardConfig(**payload["walk_forward"])
        with connection_factory(query_only=False) as conn:
            market_repo = HMA.HistoricalMarketArchiveRepository(conn)
            financial_archive_repo = HFA.HistoricalFinancialArchiveRepository(conn)
            financial_feature_repo = FFE.FinancialFeatureEvidenceRepository(conn, financial_archive_repo)
            universe_repo = HUA.HistoricalUniverseArchiveRepository(conn)
            tradability_repo = TA.TradabilityArchiveRepository(conn)
            ledger = EVR.ExperimentValidationRepository(conn)
            calendar = HSC.issue_from_market_archive(
                market_repo, archive_fingerprint=payload["market_archive_fingerprint"],
                benchmark_symbol=payload["benchmark_symbol"], start=spec.start_date, end=spec.end_date,
            )
            if calendar.calendar_fingerprint != payload["calendar_fingerprint"]:
                raise ValueError("calendar_identity_mismatch")
            LD.ensure_schema(conn)
            manifest = LD.read_manifest(conn, spec.dataset_fingerprint)
            if manifest is None:
                raise ValueError("dataset_identity_unavailable")
            dataset = LD.build_dataset(
                conn, cutoff=manifest["cutoff"], split_spec=manifest["split_spec"],
                feature_names=manifest["feature_names"],
                horizon_semantics=manifest["horizon_semantics"],
                contract_version=manifest["contract_version"],
                financial_feature_repository=financial_feature_repo
                if payload.get("financial_archive_fingerprint") else None,
                financial_archive_fingerprint=payload.get("financial_archive_fingerprint"),
                persist=False,
            )
            if dataset.fingerprint != spec.dataset_fingerprint:
                raise ValueError("dataset_identity_mismatch")
            samples = [sample for partition in LD.PARTITIONS
                       for sample in dataset.partitions.get(partition, ())]
            strategy_version = SR.get_version(
                spec.strategy.strategy_id, spec.strategy.version,
                checksum=spec.strategy.checksum,
            )
            # The app process injects its build identity. The request cannot claim it.
            output = runner.run_validation(
                spec, runner_code_revision=build_revision,
                strategy_version=strategy_version, dataset_manifest=manifest,
                samples=samples, walk_forward_config=config,
                session_calendar=calendar, market_archive_repository=market_repo,
                market_archive_fingerprint=payload["market_archive_fingerprint"],
                universe_archive_repository=universe_repo,
                universe_archive_fingerprint=payload["universe_archive_fingerprint"],
                tradability_repository=tradability_repo,
                financial_feature_repository=financial_feature_repo,
                financial_archive_fingerprint=payload.get("financial_archive_fingerprint"),
                validation_repository=ledger,
                created_at=created_at,
            )
            if output.get("run"):
                output["run"] = validation_run_projection(output["run"])
            return output
    except ExperimentValidationRunMissing:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise ExperimentValidationRequestInvalid(
            exc, fallback="validation_request_invalid") from exc


def create_robustness_report(
    run_id: int,
    payload: Mapping[str, Any],
    *,
    connection_factory: Callable[..., Any],
    created_at: str,
) -> dict[str, Any]:
    """Run a bounded offline adversarial report against one canonical R29 run."""
    try:
        import experiment_contract as EC
        import experiment_execution_model  # noqa: F401
        import experiment_pit_validation as PV
        import financial_feature_evidence as FFE
        import experiment_validation_repository as EVR
        import historical_financial_archive as HFA
        import historical_market_archive as HMA
        import historical_session_calendar as HSC
        import historical_universe_archive as HUA
        import learning_dataset as LD
        import robustness_contract as RC
        import robustness_repository as RREP
        import robustness_runner as RRUN
        import strategy_dsl_schema as DSL
        import strategy_registry as SR
        import tradability_archive as TA
        import walk_forward_validation as WFV

        spec = _experiment_spec(payload, EC)
        plan = RC.RobustnessPlan(**payload["plan"])
        with connection_factory(query_only=False) as conn:
            market_repo = HMA.HistoricalMarketArchiveRepository(conn)
            financial_archive_repo = HFA.HistoricalFinancialArchiveRepository(conn)
            financial_feature_repo = FFE.FinancialFeatureEvidenceRepository(conn, financial_archive_repo)
            universe_repo = HUA.HistoricalUniverseArchiveRepository(conn)
            tradability_repo = TA.TradabilityArchiveRepository(conn)
            validation_repo = EVR.ExperimentValidationRepository(conn)
            baseline = validation_repo.get_run(run_id=run_id)
            if baseline is None:
                raise ExperimentValidationRunMissing()
            expected_owners = robustness_owner_identities(payload.get("owner_identities"), baseline)
            if (spec.market_data_fingerprint != expected_owners["market_archive_fingerprint"]
                    or spec.universe_fingerprint != expected_owners["universe_archive_fingerprint"]
                    or spec.dataset_fingerprint != expected_owners["dataset_fingerprint"]
                    or spec.parameter_set.get("validation_calendar_fingerprint") != expected_owners["calendar_fingerprint"]
                    or spec.parameter_set.get("financial_archive_fingerprint") != expected_owners["financial_archive_fingerprint"]
                    or payload["benchmark_symbol"] != plan.regime_policy["benchmark_symbol"]):
                raise ValueError("baseline_owner_identity_mismatch")
            calendar = HSC.issue_from_market_archive(
                market_repo, archive_fingerprint=spec.market_data_fingerprint,
                benchmark_symbol=payload["benchmark_symbol"],
                start=spec.start_date, end=spec.end_date)
            if calendar.calendar_fingerprint != expected_owners["calendar_fingerprint"]:
                raise ValueError("baseline_calendar_identity_mismatch")
            manifest = LD.read_manifest(conn, spec.dataset_fingerprint)
            if manifest is None:
                raise ValueError("baseline_dataset_unavailable")
            dataset = LD.build_dataset(
                conn, cutoff=manifest["cutoff"], split_spec=manifest["split_spec"],
                feature_names=manifest["feature_names"],
                horizon_semantics=manifest["horizon_semantics"],
                contract_version=manifest["contract_version"],
                financial_feature_repository=financial_feature_repo
                if expected_owners["financial_archive_fingerprint"] else None,
                financial_archive_fingerprint=expected_owners["financial_archive_fingerprint"],
                persist=False)
            if dataset.fingerprint != spec.dataset_fingerprint:
                raise ValueError("baseline_dataset_identity_mismatch")
            samples = [sample for partition in LD.PARTITIONS
                       for sample in dataset.partitions.get(partition, ())]
            strategy_version = SR.get_version(spec.strategy.strategy_id, spec.strategy.version,
                                              checksum=spec.strategy.checksum)
            ast_definition = DSL.normalize(strategy_version.definition.get("dsl_ast"))
            dependencies = PV.strategy_dsl_dependencies(ast_definition)
            financial_by_pair = {}
            if dependencies["financial_fields"]:
                for sample in samples:
                    sample_key = getattr(sample, "sample_key", None)
                    code = getattr(sample, "code", None)
                    session = (getattr(sample, "feature_asof", None)
                               or getattr(sample, "decision_session", None))
                    if not sample_key or not code or not session:
                        continue
                    for field_name in dependencies["financial_fields"]:
                        item = financial_feature_repo.resolve_for_dataset_sample(
                            dataset_fingerprint=spec.dataset_fingerprint,
                            sample_key=str(sample_key), feature_name=field_name,
                            financial_archive_fingerprint=expected_owners["financial_archive_fingerprint"],
                        ).projection()
                        if item.get("verification") != "proven":
                            continue
                        pair = (str(code), str(session))
                        entry = financial_by_pair.setdefault(pair, {"decision_at": item.get("decision_at")})
                        if entry["decision_at"] != item.get("decision_at"):
                            raise ValueError("financial_feature_decision_mismatch")
                        if field_name in entry and entry[field_name] != item.get("feature_value"):
                            raise ValueError("financial_feature_value_mismatch")
                        entry[field_name] = item.get("feature_value")
            walk_forward = payload.get("walk_forward")
            config = WFV.WalkForwardConfig(**walk_forward) if walk_forward else None
            extended_calendar = None
            date_scenarios = [scenario for scenario in plan.scenarios()
                              if scenario["category"] in {"start_date", "end_date"}]
            if date_scenarios:
                calendar_manifest = market_repo.get_manifest(spec.market_data_fingerprint)
                benchmark_calendar = (calendar_manifest.benchmark_calendars.get(payload["benchmark_symbol"])
                                      if calendar_manifest is not None else None)
                if not isinstance(benchmark_calendar, dict):
                    raise ValueError("historical_benchmark_calendar_unavailable")
                owner_sessions = list(benchmark_calendar.get("sessions") or ())
                try:
                    base_start, base_end = RRUN._baseline_session_bounds(spec, owner_sessions)
                except ValueError as exc:
                    raise ValueError("historical_benchmark_calendar_unavailable") from exc
                if owner_sessions != sorted(set(owner_sessions)):
                    raise ValueError("historical_benchmark_calendar_unavailable")
                baseline_sessions = owner_sessions[base_start:base_end + 1]
                ranges = []
                for scenario in date_scenarios:
                    try:
                        selected = RRUN._scenario_sessions(scenario, spec, owner_sessions)
                    except ValueError:
                        # Out-of-coverage scenarios remain unavailable in the
                        # runner; they must not make the whole POST fail.
                        continue
                    if selected != baseline_sessions:
                        ranges.append((selected[0], selected[-1]))
                if ranges:
                    coverage_start = min(spec.start_date, *(start for start, _ in ranges))
                    coverage_end = max(spec.end_date, *(end for _, end in ranges))
                    try:
                        extended_calendar = HSC.issue_from_market_archive(
                            market_repo, archive_fingerprint=spec.market_data_fingerprint,
                            benchmark_symbol=payload["benchmark_symbol"],
                            start=coverage_start, end=coverage_end)
                    except ValueError:
                        # The owner cannot prove this expanded range. R30 records
                        # each affected case as unavailable instead of guessing.
                        extended_calendar = None
            if extended_calendar is not None and extended_calendar.calendar_fingerprint == calendar.calendar_fingerprint:
                extended_calendar = None
            report = RRUN.run_robustness(
                baseline_run=baseline, spec=spec, plan=plan,
                strategy_version=strategy_version, session_calendar=calendar,
                market_archive_repository=market_repo,
                universe_archive_repository=universe_repo,
                tradability_repository=tradability_repo,
                financial_features=financial_by_pair,
                required_financial_fields=dependencies["financial_fields"],
                extended_session_calendar=extended_calendar,
                date_range_validation_context={
                    "benchmark_symbol": payload["benchmark_symbol"],
                    "walk_forward_config": config,
                    "dataset_manifest": manifest,
                    "samples": samples,
                    "financial_feature_repository": financial_feature_repo
                        if expected_owners["financial_archive_fingerprint"] else None,
                },
                created_at=created_at)
            report["plan_fingerprint"] = plan.fingerprint
            repository = RREP.RobustnessRepository(conn)
            stored = repository.append_report(report)
            return {"status": "ok", "report_id": stored["id"],
                    "report_key": stored["report_key"],
                    "report_fingerprint": stored["report_fingerprint"],
                    "report": stored["report"]}
    except ExperimentValidationRunMissing:
        raise
    except RRUN.RobustnessBaselineError as exc:
        raise ExperimentRobustnessBaselineError(str(exc)) from exc
    except RREP.RobustnessPersistenceError as exc:
        raise ExperimentRobustnessConflict(str(exc)) from exc
    except (TypeError, ValueError, KeyError) as exc:
        raise ExperimentValidationRequestInvalid(
            exc, fallback="robustness_request_invalid") from exc
    except Exception as exc:
        raise ExperimentValidationInputsUnavailable("robustness_inputs_unavailable") from exc
