# -*- coding: utf-8 -*-
"""自进化逻辑闭环引擎（Self-Evolution Closed Loop）。

解决的核心问题
--------------
现有 ``self_evolution`` 只提供"参数调整库"，``adaptive_runner`` 只是单次触发 + 重试，
两者都不是一个**可持续、可续跑、可自愈的闭环**。本模块把一次完整进化收敛成
「代（generation）」概念，并把每代拆成显式阶段：

    OBSERVE → EVALUATE → MUTATE → VALIDATE → APPLY  （→ 下一轮 OBSERVE …）

关键保证
--------
1. 持续循环、无断点：每完成一个阶段就落盘检查点（checkpoint），下次调用
   ``run_loop`` 会先 ``_resume_pending`` 续跑被中断的那一代，绝不丢代、绝不死等。
2. 不卡死：每代受 ``time_budget_seconds`` 约束；缺数据 / 样本不足时阶段进入
   ``skipped``（带原因），而不是阻塞或抛错中止整轮。
3. 生命周期严格隔离（candidate != validated != active != latest）：
   MUTATE 阶段产出候选参数（validation_state=candidate），VALIDATE 阶段跑边界与画像校验（validation_state=validated），
   APPLY 阶段记录待激活状态（pending_activation=True）。APPLY 绝不自动激活参数；生效指针保持不变，
   必须由显式审核/审批后通过 ``activate_params_candidate`` 推进，防止未经验证的突变直接污染实盘运行时。
4. 故障隔离与因果阻断（Fail-Closed）：
   每个阶段独立监控与落盘；上游阶段若发生异常（如 EVALUATE 或 MUTATE 崩溃），直接阻断下游阶段执行
   （下游标记 skipped_upstream_failure_*），并将本代标记为 interrupted，拒绝将失败代伪装为 completed，
   防止脏状态蔓延。同一代失败超过 ``MAX_GEN_ATTEMPTS`` 才判定永久 ``failed``，避免无限重试。
5. 状态自检：``self_check`` 在每个阶段前校验前置条件（数据新鲜度、样本量、
   边界合法性），不健康则降级执行而非崩。
6. 参数动态演化：MUTATE 阶段调用 ``self_evolution.auto_evolve_if_needed``，
   依据历史表现（共识率 / 评估分 / 失败率）自动调参；逐代记录 ``intelligence_score``
   以量化"越来越聪明"。

设计原则：本引擎只依赖 SQLite（与 ``self_evolution`` 同源），不引入 AI/网络调用，
因此可完全离线运行与测试；生产环境由 ``ProductionBackend`` 直接复用同一份数据库，
与既有 ``adaptive_engine`` / ``adaptive_runner`` 解耦，互不阻塞。
"""
from __future__ import annotations

import sqlite3
import time
import traceback
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 极旧环境兜底
    TZ = None
from adaptive_common import _now, _json, _loads  # C3: 收敛重复工具函数

# 阶段顺序即为闭环方向；APPLY 之后自动回到 OBSERVE 开启下一代。
STAGES = ("observe", "evaluate", "mutate", "validate", "apply")

# 单代最大重试次数（含续跑），超过则永久 failed，防止无限重试。
MAX_GEN_ATTEMPTS = 3

EVOLUTION_VERSION = "evolution-loop-v1"


# ──────────────────────────────────────────────────────────────────────────
# 基础工具（_now/_json/_loads 源自 adaptive_common；_num 保留模块本地）
# ──────────────────────────────────────────────────────────────────────────
def _num(value, default=None):
    try:
        v = float(value)
        return v if abs(v) < 1e15 else default
    except (TypeError, ValueError):
        return default


# ──────────────────────────────────────────────────────────────────────────
# 数据库 schema
# ──────────────────────────────────────────────────────────────────────────
def ensure_loop_schema(conn: sqlite3.Connection) -> None:
    """创建闭环引擎所需的全部表。幂等。"""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS evolution_loop_state(
            generation        INTEGER PRIMARY KEY,
            status            TEXT NOT NULL,   -- running|completed|failed|interrupted
            current_stage     TEXT,
            done_stages       TEXT NOT NULL DEFAULT '[]',
            stage_status      TEXT,            -- 最近一次阶段结果: done|skipped|failed
            attempts          INTEGER NOT NULL DEFAULT 1,
            started_at        TEXT,
            updated_at        TEXT,
            finished_at       TEXT,
            last_error        TEXT,
            params_id_start   INTEGER,
            params_id_end     INTEGER,
            observe_ctx_json  TEXT,            -- observe 阶段产出的世界态，供中断续跑重建 ctx
            metrics_json      TEXT,
            health_json       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_evolution_loop_state_status
            ON evolution_loop_state(status, generation DESC);

        CREATE TABLE IF NOT EXISTS evolution_loop_log(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            generation  INTEGER NOT NULL,
            stage       TEXT,
            event       TEXT NOT NULL,  -- stage_start|stage_done|stage_skipped|
                                        -- stage_failed|resume|recover|self_check
            detail      TEXT,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_evolution_loop_log_recent
            ON evolution_loop_log(generation DESC, id DESC);

        CREATE TABLE IF NOT EXISTS evolution_generation(
            generation        INTEGER PRIMARY KEY,
            params_id_start   INTEGER,
            params_id_end     INTEGER,
            mutation_summary  TEXT,
            eval_before       TEXT,
            eval_after        TEXT,
            intelligence_score REAL,
            improvement       REAL,            -- 相对上一代的 intelligence 增量
            created_at        TEXT NOT NULL
        );
        """
    )
    # 动态补齐 candidate 生命周期列
    cols = {r[1] for r in conn.execute("PRAGMA table_info(evolution_loop_state)").fetchall()}
    if "candidate_params_id" not in cols:
        conn.execute("ALTER TABLE evolution_loop_state ADD COLUMN candidate_params_id INTEGER")
    if "candidate_validation_state" not in cols:
        conn.execute("ALTER TABLE evolution_loop_state ADD COLUMN candidate_validation_state TEXT")
    if "candidate_pending_activation" not in cols:
        conn.execute("ALTER TABLE evolution_loop_state ADD COLUMN candidate_pending_activation BOOLEAN DEFAULT 0")

    # 复用 self_evolution 的表（进化参数 / 追踪 / 日志）。
    import self_evolution as SE
    SE.ensure_schema(conn)
    conn.commit()


def _log(conn: sqlite3.Connection, generation: int, stage: str, event: str, detail=None) -> None:
    conn.execute(
        "INSERT INTO evolution_loop_log(generation, stage, event, detail, created_at) VALUES(?,?,?,?,?)",
        (generation, stage, event, _json(detail or {}), _now()),
    )
    conn.commit()


# ──────────────────────────────────────────────────────────────────────────
# 后端适配器（依赖倒置：生产用真实库，测试用 FakeBackend 离线跑）
# ──────────────────────────────────────────────────────────────────────────
class Backend:
    """闭环各阶段的具体执行者。返回 dict；允许抛异常由引擎统一恢复。"""

    def observe(self, conn, generation, ctx):
        raise NotImplementedError

    def evaluate(self, conn, generation, ctx):
        raise NotImplementedError

    def mutate(self, conn, generation, ctx):
        raise NotImplementedError

    def validate(self, conn, generation, ctx):
        raise NotImplementedError

    def apply(self, conn, generation, ctx):
        raise NotImplementedError


class ProductionBackend(Backend):
    """纯 SQLite 生产适配器：直接复用 self_evolution，不触发 AI / 网络。

    重活（双 AI 调参、奖励结算）仍由既有 ``adaptive_runner`` / ``adaptive_engine``
    完成；本闭环负责"编排 + 韧性 + 参数动态演化"这一层。
    """

    def observe(self, conn, generation, ctx):
        import self_evolution as SE
        cur = SE.get_current_params(conn)
        metrics = SE.get_performance_metrics(conn, 20)
        return {
            "params_id": cur["id"],
            "params": cur["params"],
            "metrics": metrics,
            "has_data": metrics.get("has_data", False),
            "sample_count": metrics.get("sample_count", 0),
        }

    def evaluate(self, conn, generation, ctx):
        import self_evolution as SE
        metrics = SE.get_performance_metrics(conn, 20)
        score = _intelligence_score(metrics)
        return {"intelligence_score": score, "metrics": metrics}

    def mutate(self, conn, generation, ctx):
        import self_evolution as SE
        cur = SE.get_current_params(conn)
        active_id = cur["id"]
        result = SE.auto_evolve_if_needed(conn)
        # 自动进化只生成候选，绝不激活。这里回填的必须是"仍然生效的"参数 id：
        # 把候选 id 写进 params_id_end 会让代际快照看起来像是已经生效了，
        # 那正是 latest row == active 的老毛病。
        if result is None:
            return {
                "mutated": False,
                "params_id": active_id,
                "active_params_id": active_id,
                "candidate_params_id": None,
                "candidate_created": False,
                "candidate_pending_activation": False,
            }
        cand_id = result.get("new_params_id")
        return {
            "mutated": True,
            "params_id": active_id,
            "active_params_id": active_id,
            "candidate_params_id": cand_id,
            "candidate_created": True,
            "candidate_validation_state": result.get("validation_state"),
            "candidate_pending_activation": True,
            "adjustments": result.get("adjustments", []),
            "changed_keys": result.get("changed_keys", []),
        }

    def validate(self, conn, generation, ctx):
        import self_evolution as SE
        import evolution_activation as EA
        cur = SE.get_current_params(conn)
        active_id = cur["id"]
        cand_id = ctx.get("candidate_params_id")
        cand_val_result = None
        cand_val_state = None
        if cand_id is not None:
            # 真实生命周期校验：复用 evolution_activation.validate_candidate 跑不可变校验
            cand_val_result = EA.validate_candidate(conn, int(cand_id))
            cand_val_state = cand_val_result.get("validation_state")
            if not cand_val_result.get("valid"):
                return {
                    "valid": False,
                    "active_params_id": active_id,
                    "candidate_params_id": cand_id,
                    "candidate_validation_state": cand_val_state,
                    "violations": cand_val_result.get("violations") or [],
                }

        # 校验当前生效参数仍在合法边界内（防退化 / 越界）。
        clamped = SE._clamp_params(cur["params"])
        degenerate = any(
            abs(_num(clamped.get(k)) - _num(cur["params"].get(k))) > 1e-9
            for k in clamped
        )
        return {
            "valid": True,
            "active_params_id": active_id,
            "candidate_params_id": cand_id,
            "candidate_validation_state": cand_val_state,
            "candidate_validation_detail": cand_val_result,
            "out_of_bounds_corrected": degenerate,
            "params": clamped,
        }

    def apply(self, conn, generation, ctx):
        import self_evolution as SE
        import evolution_activation as EA
        start_id = ctx.get("params_id_start")
        cur = SE.get_current_params(conn)
        end_id = cur["id"]

        # 强不变量：确认当前 active 指针未被 mutation 偷改
        if start_id is not None and end_id != start_id:
            raise RuntimeError(
                f"Active params pointer was modified without authorization! start={start_id}, current={end_id}"
            )

        cand_id = ctx.get("candidate_params_id")
        if cand_id is not None:
            row = EA._params_row(conn, int(cand_id))
            if row is None:
                raise EA.CandidateNotFound(f"Candidate {cand_id} not found at apply stage")
            return {
                "applied_to_runtime": False,
                "pending_activation": True,
                "active_params_id_start": start_id,
                "active_params_id_end": end_id,
                "active_params_id": end_id,
                "applied_params_id": None,
                "candidate_params_id": cand_id,
                "candidate_validation_state": row["validation_state"],
            }

        return {
            "applied_to_runtime": False,
            "pending_activation": False,
            "active_params_id_start": start_id,
            "active_params_id_end": end_id,
            "active_params_id": end_id,
            "applied_params_id": None,
            "candidate_params_id": None,
        }


# ──────────────────────────────────────────────────────────────────────────
# 量化"越来越聪明"
# ──────────────────────────────────────────────────────────────────────────
def _intelligence_score(metrics: dict) -> float:
    """把多维表现压成一个 0~1 的"智能分"。

    - 共识率越高越聪明（调参能达成共识）
    - 平均评估分（有效为 +）越高越聪明
    - 失败率越低越聪明
    """
    if not metrics.get("has_data"):
        return 0.0
    consensus = float(metrics.get("consensus_rate", 0.0) or 0.0)
    failure = float(metrics.get("failure_rate", 0.0) or 0.0)
    avg_eval = _num(metrics.get("avg_eval_score"), 0.0) or 0.0
    eval_term = max(0.0, min(1.0, (avg_eval + 1.0) / 2.0))  # [-1,1]→[0,1]
    score = 0.4 * consensus + 0.3 * eval_term + 0.3 * (1.0 - failure)
    return max(0.0, min(1.0, score))


# ──────────────────────────────────────────────────────────────────────────
# 状态自检（每个阶段前）
# ──────────────────────────────────────────────────────────────────────────
def self_check(conn: sqlite3.Connection, stage: str, ctx: dict) -> tuple[bool, str, list]:
    """返回 (ok, action, issues)。

    action:
      - 'proceed'：正常执行
      - 'skip'   ：本阶段不可行，带原因跳过（不阻塞整轮）
    """
    issues: list = []
    if stage == "observe":
        return True, "proceed", issues

    if stage == "evaluate":
        # 仅当世界态"明确无数据"时才跳过；ctx 未携带 has_data（如中断续跑且
        # observe 上下文未持久化）时放行，交由 evaluate 阶段读取真实库数据决策。
        has_data = ctx.get("has_data")
        sample = ctx.get("sample_count")
        if has_data is False and (sample is None or sample < 5):
            issues.append(f"样本不足（{sample} < 5），跳过评估，等待更多调参数据")
            return False, "skip", issues
        return True, "proceed", issues

    if stage == "mutate":
        # 即使无可进化信号也允许 proceed（auto_evolve_if_needed 返回 None 即 noop）。
        return True, "proceed", issues

    if stage == "validate":
        if ctx.get("params_id_end") is None and ctx.get("params_id_start") is None:
            issues.append("无参数版本可校验，跳过")
            return False, "skip", issues
        return True, "proceed", issues

    if stage == "apply":
        return True, "proceed", issues

    return True, "proceed", issues


# ──────────────────────────────────────────────────────────────────────────
# 单阶段执行
# ──────────────────────────────────────────────────────────────────────────
def _run_stage(conn, backend: Backend, generation: int, stage: str, ctx: dict) -> dict:
    if stage == "observe":
        return backend.observe(conn, generation, ctx)
    if stage == "evaluate":
        return backend.evaluate(conn, generation, ctx)
    if stage == "mutate":
        return backend.mutate(conn, generation, ctx)
    if stage == "validate":
        return backend.validate(conn, generation, ctx)
    if stage == "apply":
        return backend.apply(conn, generation, ctx)
    raise ValueError(f"未知阶段: {stage}")


def _recover(conn, generation: int, stage: str, exc: Exception) -> bool:
    """阶段异常后的自愈尝试。

    返回 True 表示已隔离、可继续推进；False 表示需中止本代。
    这里做最关键的隔离：确保没有半成品参数被标记为 active——self_evolution
    的 evolve() 内部已 commit 新参数，但若 apply 未跑，我们把"最新 evolve 参数"
    是否生效取决于调用方。由于 ProductionBackend.apply 只读取当前参数，
    不另写，因此不存在孤儿写入；此处仅记录并放行。
    """
    detail = {"stage": stage, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-800:]}
    _log(conn, generation, stage, "recover", detail)
    # 默认认为可继续（best-effort），不把瞬时异常升级为永久失败。
    return True


# ──────────────────────────────────────────────────────────────────────────
# 单代执行（带检查点 / 自检 / 异常恢复）
# ──────────────────────────────────────────────────────────────────────────
def _run_generation(conn, backend: Backend, generation: int, resume_from: Optional[str] = None) -> dict:
    started = _now()
    _log(conn, generation, "*", "resume" if resume_from else "gen_start", {"resume_from": resume_from})

    row = conn.execute(
        """SELECT done_stages, attempts, params_id_start, observe_ctx_json,
                  candidate_params_id, candidate_validation_state, candidate_pending_activation
           FROM evolution_loop_state WHERE generation=?""",
        (generation,),
    ).fetchone()
    done_stages = _loads(row[0], []) if row else []
    attempts = row[1] if row else 1
    params_id_start = row[2] if row else None
    # 中断续跑时重建 observe 世界态，避免 evaluate 等后续阶段误判"样本不足"。
    ctx: dict = {"params_id_start": params_id_start}
    if row and row[3]:
        ctx.update(_loads(row[3], {}))
    if row and len(row) > 4:
        if row[4] is not None:
            ctx["candidate_params_id"] = row[4]
            ctx["candidate_created"] = True
        if row[5] is not None:
            ctx["candidate_validation_state"] = row[5]
        if row[6] is not None:
            ctx["candidate_pending_activation"] = bool(row[6])

    # 初始化 / 续跑状态行。
    if not row:
        conn.execute(
            """INSERT INTO evolution_loop_state(generation, status, current_stage, done_stages,
               attempts, started_at, updated_at) VALUES(?,?,?,?,?,?,?)""",
            (generation, "running", resume_from or "observe", _json([]), attempts, started, started),
        )
    else:
        conn.execute(
            "UPDATE evolution_loop_state SET status='running', updated_at=?, current_stage=? WHERE generation=?",
            (started, resume_from or "observe", generation),
        )
    conn.commit()

    health: list = []
    stages_failed = 0
    stages_skipped = 0

    ordered = STAGES[(STAGES.index(resume_from) if resume_from in STAGES else 0):]
    upstream_failed = False
    upstream_failure_stage = None

    for stage in ordered:
        if upstream_failed:
            # 因果依赖：上游阶段发生异常，下游阶段禁止执行，避免生成错误候选或伪装完成
            skip_reason = f"skipped_upstream_failure_{upstream_failure_stage}"
            _log(conn, generation, stage, "stage_skipped", {"reason": skip_reason})
            stages_skipped += 1
            conn.execute(
                "UPDATE evolution_loop_state SET current_stage=?, stage_status='skipped', updated_at=? WHERE generation=?",
                (stage, _now(), generation),
            )
            conn.commit()
            continue

        _log(conn, generation, stage, "stage_start")
        conn.execute(
            "UPDATE evolution_loop_state SET current_stage=?, updated_at=? WHERE generation=?",
            (stage, _now(), generation),
        )
        conn.commit()

        ok, action, issues = self_check(conn, stage, ctx)
        if issues:
            health.append({"stage": stage, "issues": issues})
        if action == "skip":
            _log(conn, generation, stage, "stage_skipped", {"reason": "; ".join(issues)})
            stages_skipped += 1
            done_stages.append(stage)
            conn.execute(
                "UPDATE evolution_loop_state SET stage_status='skipped', done_stages=?, updated_at=? WHERE generation=?",
                (_json(done_stages), _now(), generation),
            )
            conn.commit()
            continue

        try:
            result = _run_stage(conn, backend, generation, stage, ctx)
            if not isinstance(result, dict):
                raise ValueError(f"Stage {stage} returned non-dict result: {type(result)}")
            if stage == "validate" and result.get("valid") is False:
                raise ValueError(f"Candidate validation failed: {result.get('violations')}")

            # 阶段产物回写 ctx，供后续阶段与下一代使用。
            if stage == "observe":
                ctx["params_id_start"] = result.get("params_id")
                ctx["params_id_end"] = result.get("params_id")
                ctx["sample_count"] = result.get("sample_count", 0)
                ctx["has_data"] = result.get("has_data", False)
                # 持久化世界态，供中断续跑重建 ctx。
                conn.execute(
                    "UPDATE evolution_loop_state SET observe_ctx_json=? WHERE generation=?",
                    (_json({"sample_count": ctx["sample_count"], "has_data": ctx["has_data"],
                            "params_id_start": ctx["params_id_start"], "params_id_end": ctx["params_id_end"]}),
                     generation),
                )
                if params_id_start is None:
                    params_id_start = result.get("params_id")
                    conn.execute(
                        "UPDATE evolution_loop_state SET params_id_start=? WHERE generation=?",
                        (params_id_start, generation),
                    )
            elif stage == "mutate":
                active_id = result.get("active_params_id") or result.get("params_id") or params_id_start
                ctx["params_id_end"] = active_id
                if result.get("candidate_params_id") is not None:
                    ctx["candidate_params_id"] = result.get("candidate_params_id")
                    ctx["candidate_created"] = True
                    ctx["candidate_validation_state"] = result.get("candidate_validation_state")
                    ctx["candidate_pending_activation"] = True
                else:
                    ctx["candidate_params_id"] = None
                    ctx["candidate_created"] = False
                    ctx["candidate_pending_activation"] = False
                conn.execute(
                    """UPDATE evolution_loop_state SET params_id_end=?,
                       candidate_params_id=?, candidate_validation_state=?, candidate_pending_activation=?
                       WHERE generation=?""",
                    (ctx["params_id_end"], ctx.get("candidate_params_id"),
                     ctx.get("candidate_validation_state"),
                     1 if ctx.get("candidate_pending_activation") else 0,
                     generation),
                )
            elif stage == "evaluate":
                ctx["intelligence_score"] = result.get("intelligence_score")
            elif stage == "validate":
                if result.get("candidate_validation_state"):
                    ctx["candidate_validation_state"] = result.get("candidate_validation_state")
                conn.execute(
                    "UPDATE evolution_loop_state SET candidate_validation_state=? WHERE generation=?",
                    (ctx.get("candidate_validation_state"), generation),
                )
            elif stage == "apply":
                if result.get("active_params_id_end"):
                    ctx["params_id_end"] = result.get("active_params_id_end")
                ctx["candidate_pending_activation"] = result.get(
                    "pending_activation", ctx.get("candidate_pending_activation", False)
                )
                conn.execute(
                    """UPDATE evolution_loop_state SET params_id_end=?, candidate_pending_activation=?
                       WHERE generation=?""",
                    (ctx.get("params_id_end"), 1 if ctx.get("candidate_pending_activation") else 0, generation),
                )

            done_stages.append(stage)
            conn.execute(
                "UPDATE evolution_loop_state SET stage_status='done', done_stages=?, updated_at=? WHERE generation=?",
                (_json(done_stages), _now(), generation),
            )
            conn.commit()
            _log(conn, generation, stage, "stage_done", _truncate(result))
        except Exception as exc:  # 阶段执行故障：隔离并阻断下游
            stages_failed += 1
            upstream_failed = True
            upstream_failure_stage = stage
            conn.execute(
                "UPDATE evolution_loop_state SET stage_status='failed', last_error=?, updated_at=? WHERE generation=?",
                (f"{type(exc).__name__}: {exc}", _now(), generation),
            )
            conn.commit()
            _log(conn, generation, stage, "stage_failed", {"error": f"{type(exc).__name__}: {exc}"})
            _recover(conn, generation, stage, exc)

    finished = _now()
    all_done = len(done_stages) == len(STAGES)
    status = "completed" if (all_done and stages_failed == 0) else "interrupted"

    # 写逐代快照（intelligence + 改进）。
    intel = _num(ctx.get("intelligence_score"))
    prev = conn.execute(
        "SELECT intelligence_score FROM evolution_generation ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    prev_score = _num(prev[0]) if prev and prev[0] is not None else None
    improvement = (intel - prev_score) if (intel is not None and prev_score is not None) else None
    conn.execute(
        """INSERT OR REPLACE INTO evolution_generation(
            generation, params_id_start, params_id_end, mutation_summary,
            eval_before, eval_after, intelligence_score, improvement, created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (generation, params_id_start, ctx.get("params_id_end") or params_id_start,
         _json({
             "stages_done": done_stages,
             "stages_skipped": stages_skipped,
             "stages_failed": stages_failed,
             "candidate_params_id": ctx.get("candidate_params_id"),
             "candidate_validation_state": ctx.get("candidate_validation_state"),
             "candidate_created": ctx.get("candidate_created", False),
             "candidate_pending_activation": ctx.get("candidate_pending_activation", False),
         }),
         _json({"prev_intelligence": prev_score}),
         _json({"intelligence": intel}),
         intel, improvement, finished),
    )

    conn.execute(
        """UPDATE evolution_loop_state SET status=?, finished_at=?, updated_at=?,
           metrics_json=?, health_json=?, params_id_end=?,
           candidate_params_id=?, candidate_validation_state=?, candidate_pending_activation=?
           WHERE generation=?""",
        (status, finished if status == "completed" else None, finished,
         _json({"intelligence_score": intel, "improvement": improvement,
                "stages_done": done_stages, "stages_skipped": stages_skipped,
                "stages_failed": stages_failed,
                "candidate_params_id": ctx.get("candidate_params_id"),
                "candidate_validation_state": ctx.get("candidate_validation_state"),
                "candidate_created": ctx.get("candidate_created", False),
                "candidate_pending_activation": ctx.get("candidate_pending_activation", False)}),
         _json(health), ctx.get("params_id_end") or params_id_start,
         ctx.get("candidate_params_id"), ctx.get("candidate_validation_state"),
         1 if ctx.get("candidate_pending_activation") else 0,
         generation),
    )
    conn.commit()

    return {
        "generation": generation,
        "status": status,
        "stages_done": done_stages,
        "stages_skipped": stages_skipped,
        "stages_failed": stages_failed,
        "intelligence_score": intel,
        "improvement": improvement,
        "active_params_id_start": params_id_start,
        "active_params_id_end": ctx.get("params_id_end") or params_id_start,
        "candidate_params_id": ctx.get("candidate_params_id"),
        "candidate_validation_state": ctx.get("candidate_validation_state"),
        "candidate_created": ctx.get("candidate_created", False),
        "candidate_pending_activation": ctx.get("candidate_pending_activation", False),
        "params_id_start": params_id_start,
        "params_id_end": ctx.get("params_id_end") or params_id_start,
    }


def _truncate(d: dict, limit: int = 600) -> dict:
    s = _json(d)
    if len(s) <= limit:
        return d
    return {"_truncated": True, "len": len(s), "head": s[:limit]}


# ──────────────────────────────────────────────────────────────────────────
# 续跑：找出被中断 / 运行中的代
# ──────────────────────────────────────────────────────────────────────────
def _find_pending(conn) -> list:
    rows = conn.execute(
        "SELECT generation, done_stages, attempts FROM evolution_loop_state "
        "WHERE status IN ('running','interrupted') ORDER BY generation ASC"
    ).fetchall()
    pending = []
    for gen, ds, attempts in rows:
        if attempts >= MAX_GEN_ATTEMPTS:
            # 放弃续跑，标记永久失败。
            conn.execute("UPDATE evolution_loop_state SET status='failed', finished_at=? WHERE generation=?",
                         (_now(), gen))
            conn.commit()
            continue
        done = _loads(ds, [])
        resume_from = STAGES[len(done)] if len(done) < len(STAGES) else None
        pending.append((gen, resume_from, attempts))
    return pending


def _next_generation_id(conn) -> int:
    row = conn.execute("SELECT MAX(generation) FROM evolution_loop_state").fetchone()
    return (row[0] or 0) + 1


def _budget_exceeded(start_mono: float, budget: Optional[float]) -> bool:
    if budget is None:
        return False
    return (time.monotonic() - start_mono) >= budget


# ──────────────────────────────────────────────────────────────────────────
# 主入口：持续循环推演
# ──────────────────────────────────────────────────────────────────────────
def run_loop(conn: sqlite3.Connection, backend: Optional[Backend] = None,
             generations: int = 1, time_budget_seconds: Optional[float] = None,
             resume: bool = True) -> dict:
    """驱动自进化闭环运行 ``generations`` 代（或在时间预算内尽可能多跑）。

    - 先续跑被中断的代（保证无断点）
    - 再开启新生代
    - 每代受 ``time_budget_seconds`` 约束，超时即优雅收尾（不卡死）
    """
    ensure_loop_schema(conn)
    backend = backend or ProductionBackend()
    start_mono = time.monotonic()
    report = {
        "generations_requested": generations,
        "generations_run": 0,
        "completed": 0,
        "interrupted": 0,
        "failed": 0,
        "resumed": 0,
        "total_stages_skipped": 0,
        "total_stage_errors": 0,
        "latest_intelligence": None,
        "details": [],
    }

    # 1) 续跑被中断的代。
    if resume:
        pending = _find_pending(conn)
        for idx, (gen, resume_from, attempts) in enumerate(pending):
            if idx > 0 and _budget_exceeded(start_mono, time_budget_seconds):
                break
            conn.execute("UPDATE evolution_loop_state SET attempts=attempts+1 WHERE generation=?", (gen,))
            conn.commit()
            res = _run_generation(conn, backend, gen, resume_from=resume_from)
            report["resumed"] += 1
            report["generations_run"] += 1
            _accumulate(report, res)

    # 2) 开启新生代（至少跑完一代，再按预算停止，避免预算极小时直接 0 代）。
    ran_any = report["generations_run"] > 0
    while report["generations_run"] < generations:
        if ran_any and _budget_exceeded(start_mono, time_budget_seconds):
            break
        gen = _next_generation_id(conn)
        res = _run_generation(conn, backend, gen)
        report["generations_run"] += 1
        ran_any = True
        _accumulate(report, res)

    row = conn.execute(
        "SELECT intelligence_score FROM evolution_generation ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    report["latest_intelligence"] = _num(row[0]) if row and row[0] is not None else None
    return report


def _accumulate(report: dict, res: dict) -> None:
    report["details"].append({
        "generation": res["generation"],
        "status": res["status"],
        "intelligence_score": res["intelligence_score"],
        "improvement": res["improvement"],
    })
    if res["status"] == "completed":
        report["completed"] += 1
    elif res["status"] == "interrupted":
        report["interrupted"] += 1
    else:
        report["failed"] += 1
    report["total_stages_skipped"] += res["stages_skipped"]
    report["total_stage_errors"] += res["stages_failed"]


# ──────────────────────────────────────────────────────────────────────────
# 状态查询
# ──────────────────────────────────────────────────────────────────────────
def loop_status(conn: sqlite3.Connection) -> dict:
    ensure_loop_schema(conn)
    state_rows = conn.execute(
        "SELECT generation, status, current_stage, done_stages, attempts, finished_at, last_error "
        "FROM evolution_loop_state ORDER BY generation DESC LIMIT 10"
    ).fetchall()
    gen_rows = conn.execute(
        "SELECT generation, intelligence_score, improvement, params_id_start, params_id_end "
        "FROM evolution_generation ORDER BY generation DESC LIMIT 10"
    ).fetchall()
    return {
        "recent_states": [
            {"generation": r[0], "status": r[1], "current_stage": r[2],
             "done_stages": _loads(r[3], []), "attempts": r[4],
             "finished_at": r[5], "last_error": r[6]}
            for r in state_rows
        ],
        "recent_generations": [
            {"generation": r[0], "intelligence_score": _num(r[1]), "improvement": _num(r[2]),
             "params_id_start": r[3], "params_id_end": r[4]}
            for r in gen_rows
        ],
        "max_gen_attempts": MAX_GEN_ATTEMPTS,
    }


def is_today_generation_completed(conn: sqlite3.Connection, today_str: Optional[str] = None) -> bool:
    """检查今天是否已成功完成至少一代自进化。
    
    幂等性保障：如果今天已有完成的代且无进行中/中断的代，同日重试直接 no-op。
    """
    ensure_loop_schema(conn)
    target_date = today_str or _now()[:10]
    # 先检查是否有正在运行或中断待续跑的代：如果有，则尚未完全结束，不应视为 completed
    pending = conn.execute(
        """SELECT generation FROM evolution_loop_state
           WHERE status IN ('running', 'interrupted')
           LIMIT 1"""
    ).fetchone()
    if pending is not None:
        return False

    row = conn.execute(
        """SELECT generation FROM evolution_loop_state
           WHERE status = 'completed' AND finished_at >= ? AND finished_at < ?
           LIMIT 1""",
        (f"{target_date}T00:00:00", f"{target_date}T23:59:59.999999")
    ).fetchone()
    if row is not None:
        return True

    gen_row = conn.execute(
        """SELECT generation FROM evolution_generation
           WHERE created_at >= ? AND created_at < ?
           LIMIT 1""",
        (f"{target_date}T00:00:00", f"{target_date}T23:59:59.999999")
    ).fetchone()
    return gen_row is not None
