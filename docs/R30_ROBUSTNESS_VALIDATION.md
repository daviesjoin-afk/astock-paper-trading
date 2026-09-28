# R30 — Robustness / Adversarial Validation

R30 examines where one exact, completed R29 experiment loses evidence or changes under explicit stresses. A completed scenario means its derived replay ran; it does not mean the strategy passed. R30 does not score, rank, approve, promote, or change strategy lifecycle.

## Authority and identity

`backend/robustness_contract.py` defines immutable `RobustnessPlan`, `RobustnessScenario`, case-result, and report identities. Canonical JSON and SHA-256 make the plan and cases reproducible. The plan seed and every stress value are part of identity; the canonical R29 `run_key` binds the scenario to the baseline owners. `created_at` is descriptive and excluded from identity.

The only accepted baseline is one exact R29 ledger row with `validation_status=ready`, a completed `ExperimentResult`, matching experiment and result fingerprints, known R29 runner version, matching strategy identity, and matching calendar, market, universe, dataset, financial, and tradability identities. The caller supplies the complete `ExperimentSpec`; current/latest and legacy backtest fallbacks are not used.

Before execution, R30 recomputes the R29 replay tradability fingerprint from the baseline's exact owner sessions and historical-universe members. That identity covers both the 15:00 validation facts and the 09:30 execution facts consumed by replay. A mismatch against the stored run identity rejects the baseline, including an archive revision that changes only the open-time selection. Each changed date range gets its own canonical replay fingerprint in the ranged `ExperimentSpec` and R29 proof.

## Scenario dimensions

The plan accepts explicit commission, minimum-commission, and stamp-duty multipliers; fixed-rate slippage multipliers; owner-session execution and signal delays; liquidity reduction; deterministic whole-bar missingness; allowlisted strategy-parameter perturbations; owner-session start/end shifts; and deterministic removal from the baseline historical universe. Plans are bounded to 1–100 scenarios.

Bull/bear/sideways trend and high/middle/low/unknown volatility are derived independently from the pinned benchmark bars. Each label uses only that session and earlier owner sessions. Unknown stays unknown, and regime labels never enter the strategy DSL.

Stress views are in-memory derived inputs. They do not change OHLC truth or write to historical market, universe, tradability, or financial archives. Missing required bars and unproven execution inputs produce `unavailable` with null metrics. Parameter changes must name an allowlisted strategy DSL parameter path; there is no parameter search or performance-directed selection.

Date shifts use the owner-issued benchmark session calendar. Spec boundaries may fall on non-trading dates: the baseline anchors to the first owner session on or after `start_date` and the last owner session on or before `end_date`, then applies session offsets. Every changed start or end range, including contractions, is revalidated by the existing R29 PIT validator with the exact dataset, samples, strategy, walk-forward policy, and historical owners. Expanded ranges also require the pinned raw archive to issue the exact extended calendar. A missing or blocked proof makes that case unavailable.

## Execution and reporting

`backend/experiment_execution_model.py` remains the sole execution loop. Its optional deterministic trace is emitted by the same loop as aggregate R29 metrics. The R30 runner replays the exact baseline and checks metric equality before it creates a report.

`RobustnessReport` contains baseline identities, plan/scenario fingerprints, per-case evidence and results, actual trace-based regime summaries, sensitivity rows, coverage, observed fragilities, and unavailable/failed case identities. Unknown values remain null. There is no single score, grade, or promotion field.

`backend/robustness_repository.py` stores reports in an append-only ledger. Repeated identical report keys are idempotent; same-key content changes and corrupt payloads fail closed. The offline API is:

- `POST /api/adaptive/experiments/runs/{run_id}/robustness`
- `GET /api/adaptive/experiments/runs/{run_id}/robustness?limit=...`
- `GET /api/adaptive/robustness/{report_id}`

GET routes are read-only and do not create the database or execute a report. The R29 Research Workspace renders the baseline, scenario matrix, regime slices, sensitivity, and evidence. It only displays server-computed fields and keeps nulls unavailable.

## Architecture boundary

R29 remains the canonical PIT authority. R30 adds one plan contract, one offline robustness runner, one report ledger, and one derived regime module. There is still one R29 runner and one execution implementation. No provider/network, current/latest lookup, historical refill, lifecycle write, promotion action, AI score, or R31 logic is added.

Roadmap state: R28 COMPLETE; R29 COMPLETE; R30 COMPLETE after the full exit matrix, full backend validation, and exact-head CI passed; R31 NOT STARTED.
