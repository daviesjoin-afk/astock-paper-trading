# -*- coding: utf-8 -*-
"""R27-B2B mutation matrix —— M-B2B-1 .. M-B2B-11。

只覆盖本轮**新的高风险 invariant**，刻意不造几十条。每条 mutation 都必须让**唯一指定
的永久回归**变 RED，anchor 恰好命中一次；``--non-vacuity`` 先跑 baseline，
``SyntaxError`` / ``ImportError`` / ``NameError`` 一律计为 FAKE（改红了不等于证明了业务
性质）。

本轮的核心不变量分五组，mutation 也按这五组设计：

* **只有一个 orchestration boundary，且它只走 typed provider**
  （M-B2B-1 / 7）：绕过 provider 自己造结论、或迁移路径又调一次 legacy provider，都必须
  立刻 RED。
* **失败不可观测是缺陷**（M-B2B-2）：provider / 持久化失败被降级成"完成"必须 RED。
* **不写第二份、也不把历史伪装成 typed 研究**（M-B2B-3 / 6）：canonical 之后继续
  dual-write legacy 表、或把 legacy 历史行抬成 canonical 研究，都必须 RED。
* **research 不获得权限、时间语义不混用**（M-B2B-4 / 5 / 8）：``status == supported``
  变成权威标记、网络调用被移进事务、``as_of`` 被运维时间替换，都必须 RED。
* **provider 配置权威只有一份**（M-B2B-9 / 10 / 11，来自人工审核的两处 blocker）：
  忽略槽位的 ``enabled``、把 legacy 环境变量重新当成准入条件、或让配置解析异常裸逃逸，
  都必须 RED。

沿用 R27-B2A 已修好的 ``PYTHONPYCACHEPREFIX`` 逐次唯一目录，否则 baseline 与 mutant 会
共享字节码缓存，整张矩阵静默失效。

**必须串行运行**：每条 mutation 就地改写 production source，跑完按启动快照做
byte-identical 还原并校验 sha256。

用法：
    python work/r27b2b_research_runtime_mutation_check.py
    python work/r27b2b_research_runtime_mutation_check.py --only M-B2B-1,M-B2B-11 --non-vacuity
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")

SERVICE = "backend/ai_research_service.py"
ADVISOR = "backend/deepseek_advisor.py"
REVIEW_SERVICE = "backend/ai_review_service.py"

SUITE = "test_ai_research_service"


def _svc(name: str) -> str:
    """Shortcut for a test in the R27-B2B suite."""
    return f"{SUITE}.{name}"


MUTATIONS = [
    {
        "id": "M-B2B-1",
        # 新 service 绕过 typed provider，直接把一个 legacy 形状的 dict 当成研究结论 ——
        # 那正是"服务变成第二个 research authority"的写法。
        "file": SERVICE,
        "old": (
            "    try:\n"
            "        result = provider.run_research(\n"
            "            provider_config=provider_config,\n"
            "            hypothesis_id=hypothesis_id,\n"
            "            as_of=as_of,\n"
            "            subject=subject,\n"
            "            question=question,\n"
            "            events=events,\n"
            "            max_tokens=max_tokens,\n"
            "        )\n"
        ),
        "new": "    try:\n        result = _legacy_result(hypothesis_id, as_of, subject)\n",
        "extra": [(
            "def run_research_run(\n",
            (
                "def _legacy_result(hypothesis_id, as_of, subject):\n"
                "    class _Fake:\n"
                "        model = \"legacy\"\n"
                "    fake = _Fake()\n"
                "    fake.hypothesis = {\"hypothesis_id\": hypothesis_id, \"as_of\": as_of,\n"
                "                      \"subject\": subject}\n"
                "    fake.narrative = \"\"\n"
                "    fake.counter_arguments = ()\n"
                "    fake.input_tokens = 0\n"
                "    fake.output_tokens = 0\n"
                "    fake.latency_ms = 0\n"
                "    return fake\n"
                "\n"
                "\n"
                "def run_research_run(\n"
            ),
        )],
        "test": _svc("MigratedRuntimeTests."
                     "test_RUNTIME_01_production_entrypoint_reaches_the_canonical_ledger"),
        "desc": "service 绕过 typed provider，直接用 legacy 形状的 dict 充当结论",
    },
    {
        "id": "M-B2B-2",
        # provider / 持久化失败被降级成"完成" —— 零产出的运行被当成一次成功的复核。
        "file": ADVISOR,
        "old": (
            "    except ai_research_service.ResearchServiceError as exc:\n"
            "        # 明确失败：**不**回落 legacy provider、**不**写 legacy 表、**不**假装\"AI 没意见\"。\n"
            "        # provider 或持久化任一段失败都不留下 canonical 行，调用方必须看到失败。\n"
            "        return {\"id\": None, \"status\": \"failed\", \"report\": None,\n"
            "                \"error_code\": (\"%s_%s\" % (exc.stage, exc.reason))[:80], \"latency_ms\": 0}\n"
        ),
        "new": (
            "    except ai_research_service.ResearchServiceError as exc:\n"
            "        return {\"id\": None, \"status\": \"completed\", \"report\": None,\n"
            "                \"error_code\": None, \"latency_ms\": 0}  # MUTANT\n"
        ),
        "test": _svc("FailureSemanticsTests."
                     "test_RUNTIME_06_persistence_failure_is_never_reported_as_success"),
        "desc": "provider/持久化失败被降级成 status=completed（失败不可观测）",
    },
    {
        "id": "M-B2B-3",
        # canonical 成功后继续 dual-write legacy 表 —— 两个 authority、两条审计链。
        "file": ADVISOR,
        "old": (
            "    return {\n"
            "        \"id\": run.run_id,\n"
            "        \"status\": run.hypothesis.status,\n"
            "        \"report\": research_report_view(run),\n"
            "        \"error_code\": None,\n"
            "        \"latency_ms\": run.latency_ms,\n"
            "    }\n"
        ),
        "new": (
            "    with connect_factory() as _conn:  # MUTANT —— dual-write\n"
            "        ensure_schema(_conn)\n"
            "        _conn.execute(\n"
            "            \"INSERT INTO adaptive_advisor_runs(\"\n"
            "            \"purpose,trigger,status,provider,model,evidence_hash,evidence,report,error_code,\"\n"
            "            \"latency_ms,input_tokens,output_tokens,created_at,finished_at)\"\n"
            "            \" VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)\",\n"
            "            (\"data_quality\", str(trigger or \"manual\")[:80], run.hypothesis.status,\n"
            "             PROVIDER, run.provider_model, \"\", \"{}\", None, None, run.latency_ms,\n"
            "             run.input_tokens, run.output_tokens, run.created_at, run.created_at),\n"
            "        )\n"
            "    return {\n"
            "        \"id\": run.run_id,\n"
            "        \"status\": run.hypothesis.status,\n"
            "        \"report\": research_report_view(run),\n"
            "        \"error_code\": None,\n"
            "        \"latency_ms\": run.latency_ms,\n"
            "    }\n"
        ),
        "test": _svc("MigratedRuntimeTests."
                     "test_RUNTIME_03_canonical_append_happens_exactly_once_without_dual_write"),
        "desc": "canonical 成功后继续写 legacy 表（dual-write）",
    },
    {
        "id": "M-B2B-4",
        # research status 直接驱动权威标记 —— status == supported 变成一种授权。
        "file": ADVISOR,
        "old": (
            "        \"cross_source_status\": \"verified\" if cross_source_verified else \"not_verified\",\n"
            "        \"authority\": \"research\",\n"
            "        \"is_authoritative\": False,\n"
        ),
        "new": (
            "        \"cross_source_status\": \"verified\" if cross_source_verified else \"not_verified\",\n"
            "        \"authority\": \"research\",\n"
            "        \"is_authoritative\": hypothesis.get(\"status\") == \"supported\",  # MUTANT\n"
        ),
        "test": _svc("AuthorityBoundaryTests."
                     "test_RUNTIME_09_research_gains_no_signal_order_risk_or_promotion_authority"),
        "desc": "canonical research status 直接驱动 authority 标记",
    },
    {
        "id": "M-B2B-5",
        # 网络调用被移进事务：一次 LLM 等待持有 SQLite 写锁。
        "file": SERVICE,
        "old": "    # ② provider / network：**不在任何 DB transaction 内**。\n",
        "new": (
            "    _tx = connect_factory()  # MUTANT —— 网络调用进入事务\n"
            "    _tx.__enter__()\n"
            "    # ② provider / network：**不在任何 DB transaction 内**。\n"
        ),
        "test": _svc("NetworkAndTransactionTests."
                     "test_RUNTIME_12_network_call_happens_outside_any_db_transaction"),
        "desc": "provider 的 HTTP 调用被移动进 DB transaction",
    },
    {
        "id": "M-B2B-6",
        # legacy 历史行被抬成 canonical 研究：把"读不出来"变成"这里有研究结论"，
        # 并给 free-form 旧行伪造 typed provenance。
        "file": ADVISOR,
        "old": (
            "    rows = ai_research_repository.recent_runs(\n"
            "        conn, limit=1, purpose=RESEARCH_PURPOSE_DATA_QUALITY,\n"
            "    )\n"
            "    return rows[0] if rows else None\n"
        ),
        "new": (
            "    rows = ai_research_repository.recent_runs(\n"
            "        conn, limit=1, purpose=RESEARCH_PURPOSE_DATA_QUALITY,\n"
            "    )\n"
            "    if rows:\n"
            "        return rows[0]\n"
            "    try:  # MUTANT —— 把 legacy 历史行伪装成 typed 研究\n"
            "        legacy = conn.execute(\n"
            "            \"SELECT * FROM adaptive_advisor_runs WHERE purpose='data_quality' \"\n"
            "            \"ORDER BY id DESC LIMIT 1\"\n"
            "        ).fetchone()\n"
            "    except Exception:\n"
            "        legacy = None\n"
            "    if legacy is None:\n"
            "        return None\n"
            "    item = dict(legacy)\n"
            "    return {\n"
            "        \"id\": item.get(\"id\"), \"purpose\": item.get(\"purpose\"),\n"
            "        \"trigger\": item.get(\"trigger\"), \"status\": item.get(\"status\"),\n"
            "        \"reason\": None, \"as_of\": item.get(\"created_at\"),\n"
            "        \"provider_model\": item.get(\"model\") or \"\",\n"
            "        \"hypothesis\": {\n"
            "            \"thesis\": \"\", \"status\": item.get(\"status\"), \"confidence\": 0.0,\n"
            "            \"as_of\": item.get(\"created_at\"), \"reason\": None, \"evidence\": [],\n"
            "        },\n"
            "        \"narrative\": \"\", \"counter_arguments\": [],\n"
            "        \"authority\": \"research\", \"is_authoritative\": False,\n"
            "        \"created_at\": item.get(\"created_at\"),\n"
            "    }\n"
        ),
        "test": _svc("LegacyBoundaryTests."
                     "test_RUNTIME_07_legacy_historical_rows_are_never_migrated"),
        "desc": "legacy 历史行被 backfill/伪装成 canonical typed 研究",
    },
    {
        "id": "M-B2B-7",
        # 迁移路径又调用一次 legacy provider：同一次用户动作付两次费。
        "file": ADVISOR,
        "old": "    events = market_research_events(snapshot_paths)\n",
        "new": (
            "    call_json(\"system\", \"user\", 16)  # MUTANT —— 迁移路径又调一次 legacy provider\n"
            "    events = market_research_events(snapshot_paths)\n"
        ),
        "test": _svc("MigratedRuntimeTests."
                     "test_RUNTIME_01_production_entrypoint_reaches_the_canonical_ledger"),
        "desc": "迁移路径又调用一次 legacy provider（重复付费）",
    },
    {
        "id": "M-B2B-8",
        # ``as_of`` 被运维/墙上时钟替换：用"跑研究的那一刻"重新解释历史证据。
        "file": SERVICE,
        "old": "            as_of=as_of,\n            subject=subject,\n",
        "new": (
            "            as_of=(clock or _now)()[:10],  # MUTANT —— as_of 被运维时间替换\n"
            "            subject=subject,\n"
        ),
        "test": _svc("TimeAndIdempotencyTests."
                     "test_RUNTIME_13_as_of_and_created_at_are_separate"),
        "desc": "as_of 被 current wall-clock（运维时间）替换",
    },
    {
        "id": "M-B2B-9",
        # 就绪判据忽略槽位 enabled：操作员的 disable 只挡住 UI，挡不住真实付费调用
        # （transport 只检查 api_key / base_url / model）。
        "file": REVIEW_SERVICE,
        "old": (
            "    if not bool(cfg.get(\"enabled\")):\n"
            "        return {\"ready\": False, \"reason\": SLOT_DISABLED}\n"
        ),
        "new": (
            "    if False:  # MUTANT —— 忽略操作员的 enabled\n"
            "        return {\"ready\": False, \"reason\": SLOT_DISABLED}\n"
        ),
        "test": _svc("ProviderConfigAuthorityTests."
                     "test_RUNTIME_19_disabled_slot_performs_zero_provider_calls"),
        "desc": "canonical 就绪判据忽略 enabled（禁用槽位照样付费）",
    },
    {
        "id": "M-B2B-10",
        # 把 legacy 环境变量重新当成 canonical research 的准入条件：只在数据库 / UI 里
        # 配好的槽位被判成"未配置"。
        "file": ADVISOR,
        "old": (
            "    if not enabled(config):\n"
            "        raise RuntimeError(\"advisor_disabled\")\n"
            "    events = market_research_events(snapshot_paths)\n"
        ),
        "new": (
            "    if not enabled(config):\n"
            "        raise RuntimeError(\"advisor_disabled\")\n"
            "    if not configured():  # MUTANT —— legacy env 重新成为准入条件\n"
            "        raise RuntimeError(\"api_key_missing\")\n"
            "    events = market_research_events(snapshot_paths)\n"
        ),
        "test": _svc("ProviderConfigAuthorityTests."
                     "test_RUNTIME_18_db_only_canonical_slot_runs_without_legacy_env_key"),
        "desc": "legacy 环境变量重新成为 canonical research 的准入条件",
    },
    {
        "id": "M-B2B-11",
        # 配置解析异常裸逃逸：与 run_review 声明的 best-effort 契约不一致。
        "file": ADVISOR,
        "old": (
            "    except Exception as exc:  # noqa: BLE001\n"
            "        # 配置解析或落库之外的裸异常也不得逃逸：本函数的契约是 best-effort，只声明\n"
            "        # ``advisor_disabled`` 一种抛出（与 ``ai_review_service._call_reviewer`` 同一约定）。\n"
            "        return {\"id\": None, \"status\": \"failed\", \"report\": None,\n"
            "                \"error_code\": (\"research_config_%s\" % type(exc).__name__)[:80], \"latency_ms\": 0}\n"
        ),
        "new": "",
        "test": _svc("ProviderConfigAuthorityTests."
                     "test_RUNTIME_20_config_resolution_failure_obeys_declared_failure_semantics"),
        "desc": "配置解析异常裸逃逸（不遵守声明的 best-effort 契约）",
    },
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _adapt_eol(text: str, original: bytes) -> bytes:
    if original.count(b"\r\n") > 0:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("utf-8")


PYCACHE_ROOT = tempfile.mkdtemp(prefix="r27b2b_research_runtime_pycache_")
_SEQ = [0]

#: 变异体必须因**业务断言**失败。接线错误是假杀，不能计为 CAUGHT。
BROKEN_RE = re.compile(
    r"(SyntaxError|IndentationError|TabError"
    r"|ImportError|ModuleNotFoundError"
    r"|NameError|UnboundLocalError"
    r"|_FailedTest|AttributeError: module"
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
    """Static + behavioural assertion that run caches never collapse to one dir."""
    seen = [_next_seq() for _ in range(5)]
    assert len(set(seen)) == len(seen), f"sequence not unique: {seen}"
    assert seen == sorted(seen), f"sequence not increasing: {seen}"
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
    assert len(dirs) == 3, f"expected 3 invocations, got {dirs}"
    assert len(set(dirs)) == 3, f"invocations share a cache dir: {dirs}"


def assert_no_leftover(mutation: dict) -> None:
    path = os.path.join(ROOT, mutation["file"])
    with open(path, encoding="utf-8") as handle:
        if "MUTANT" in handle.read():
            raise RuntimeError(f'{mutation["id"]}: leftover mutant in {mutation["file"]}')


def _apply(text: str, mutation: dict) -> str:
    """Apply one mutation, requiring **every** anchor to be unique.

    ``replace(..., 1)`` rewrites the first hit; a duplicated anchor would let the
    mutation land elsewhere while still reporting CAUGHT. A mutation may carry
    ``extra`` — a list of additional ``(old, new)`` pairs — each checked for
    uniqueness too, so a multi-anchor mutation is no weaker than a single-anchor one.
    """
    pairs = [(mutation["old"], mutation["new"])]
    for extra in mutation.get("extra", ()):
        pairs.append((extra[0], extra[1]))
    for old, new in pairs:
        count = text.count(old)
        assert count == 1, (
            f'{mutation["id"]}: mutation anchor must be unique; count={count}; '
            f'file={mutation["file"]}; anchor={old[:60]!r}'
        )
        text = text.replace(old, new, 1)
    return text


def _is_fake_kill(result: subprocess.CompletedProcess) -> bool:
    blob = (result.stdout or "") + (result.stderr or "")
    return bool(BROKEN_RE.search(blob))


def run_mutation(mutation: dict, *, non_vacuity: bool) -> str:
    """Return ``CAUGHT`` / ``SURVIVED`` / ``FAKE`` / ``BASELINE-RED``."""
    path = os.path.join(ROOT, mutation["file"])
    with open(path, "rb") as handle:
        original = handle.read()
    before = sha256(original)
    text = original.decode("utf-8").replace("\r\n", "\n")

    if non_vacuity:
        baseline = run_test(mutation["test"])
        if baseline.returncode != 0:
            return f"BASELINE-RED({baseline.returncode})"

    mutated = _apply(text, mutation)
    try:
        with open(path, "wb") as handle:
            handle.write(_adapt_eol(mutated, original))
        result = run_test(mutation["test"])
        if result.returncode == 0:
            return "SURVIVED"
        if _is_fake_kill(result):
            return "FAKE"
        return "CAUGHT"
    finally:
        with open(path, "wb") as handle:
            handle.write(original)
        with open(path, "rb") as handle:
            after = sha256(handle.read())
        if after != before:
            raise RuntimeError(f'{mutation["id"]}: restore sha256 mismatch')
        assert_no_leftover(mutation)


def main() -> int:
    print(f"repo root: {ROOT}")
    argv = sys.argv[1:]
    only: set[str] | None = None
    if "--only" in argv:
        only = {item for item in argv[argv.index("--only") + 1].split(",") if item}
    non_vacuity = "--non-vacuity" in argv

    self_test_sequence()
    print("runner self-test: PASS (unique, increasing pycache sequence)")

    selected = [m for m in MUTATIONS if only is None or m["id"] in only]
    results: list[tuple[str, str]] = []
    for mutation in selected:
        verdict = run_mutation(mutation, non_vacuity=non_vacuity)
        results.append((mutation["id"], verdict))
        print(f'{mutation["id"]} {mutation["desc"]}: {verdict}', flush=True)

    bad = [(mid, v) for mid, v in results if v != "CAUGHT"]
    for mid, verdict in bad:
        print(f"NOT-CAUGHT {mid}: {verdict}")
    print(f"R27-B2B mutations: {len(results) - len(bad)}/{len(results)} CAUGHT; "
          f"survived={sum(1 for _, v in bad if v.startswith('SURVIVED'))}; "
          f"fake={sum(1 for _, v in bad if v == 'FAKE')}; "
          f"other={sum(1 for _, v in bad if not v.startswith('SURVIVED') and v != 'FAKE')}")
    print("restore sha256: PASS")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
