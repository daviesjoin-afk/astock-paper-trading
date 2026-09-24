# -*- coding: utf-8 -*-
"""R27-B2C-4B mutation matrix —— M-PFACT-1 .. M-PFACT-10。

只覆盖本轮**新的高风险 invariant**。每条 mutation 都必须让**唯一指定**的永久回归变 RED，
anchor 恰好命中一次；``--non-vacuity`` 先跑 baseline，``SyntaxError`` / ``ImportError`` /
``NameError`` / collection failure 一律计为 FAKE（接线错误是假杀，不能算 detected）。

本轮的核心不变量分四组：

* **归属必须先被证明**（M-PFACT-1）：去掉 account 属于该 cycle 且在 asof 前已挂载的证明，
  一个虚构账户就会拿到 ``realized_pnl = verified 0.0`` 与 ``position_cost_summary =
  verified (0, 0.0)`` —— "账户不存在"被发布成"账户确实什么都没做"。
* **事实只能来自 bounded owner 读路径**（M-PFACT-2 / 3 / 5 / 6）：现金改读
  ``paper_accounts.cash`` 当前可变状态、持仓成本改读 compatibility-only 的
  ``paper_positions``、去掉 as-of 边界把未来成交算进当日、已实现盈亏绕过 verified 口径直接
  ``SUM(paper_orders.realized_pnl)`` —— 四条都是"用别的东西冒充 owner 已证明的事实"。
* **证明不了就不许发布**（M-PFACT-4）：数量未证明时照发 verified zero summary，等于把
  "我们不知道持仓是多少"说成"确实没有持仓"。
* **research 侧不得替 owner 猜语义、不得接受伪投影**（M-PFACT-7 / 8 / 9 / 10）：把
  ``STATUS_UNKNOWN`` 映成 verified、identity 丢掉 account/cycle、内容指纹忽略事实值、
  入口接受 duck-typed 对象 —— 四者都会让一条被改写的事实静默变成"同一条可信证据"。

沿用 R27-B2C-1 ~ B2C-4A 的逐次唯一 ``PYTHONCACHEPREFIX``，否则 baseline 与 mutant 会共享
字节码缓存，整张矩阵静默失效。**必须串行运行**：每条 mutation 就地改写 production source，
跑完按启动快照做 byte-identical 还原并校验 sha256。

用法：
    python work/r27b2c4b_portfolio_accounting_mutation_check.py
    python work/r27b2c4b_portfolio_accounting_mutation_check.py --non-vacuity
    python work/r27b2c4b_portfolio_accounting_mutation_check.py --only M-PFACT-1
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

OWNER = "backend/paper_portfolio_read_model.py"
ADAPTER = "backend/ai_research_portfolio_adapter.py"

CONTRACT_SUITE = "test_portfolio_fact_contract"
ADAPTER_SUITE = "test_ai_research_portfolio_adapter"

CONTRACT_TESTS = f"{CONTRACT_SUITE}.PortfolioFactContractTests"
ADAPTER_TESTS = f"{ADAPTER_SUITE}.PortfolioAdapterTests"

#: 归属证明块 —— M-PFACT-1 的 anchor。
MEMBERSHIP_BLOCK = (
    "    if _cycle_initial(conn, context, account_id=account) is None:\n"
    "        # 归属/挂载不可证明 —— 绝不能发布 verified zero，也不能发布别的数字。\n"
    "        return tuple(\n"
    "            _portfolio_fact(context, account, kind, None)\n"
    "            for kind in PORTFOLIO_FACT_KINDS\n"
    "        )\n"
)
CASH_LINE = "    cash_value, cash_status = cash(conn, context, account_id=account)\n"
REALIZED_LINE = (
    "    realized_value, realized_status = realized_pnl(conn, context, account_id=account)\n"
)
BOUNDED_LOTS_BLOCK = (
    "    # 唯一 authority 仍然是 cycle/as-of bounded durable lots —— 兼容投影不参与。\n"
    "    lots, quantity_status = bounded_lots_with_status(\n"
    "        conn, context, account_id=account_id,\n"
    "    )\n"
)
QUANTITY_GUARD_BLOCK = (
    "    if quantity_status != STATUS_VERIFIED:\n"
    "        # 数量未经证明时**不发布** summary：既不发 \"能算多少算多少\"，\n"
    "        # 也不发 verified zero。\n"
    "        return None\n"
)
ASOF_BOUND_LINE = "        if economic > context.asof_day.isoformat():\n"
SOURCE_ID_BLOCK = (
    "        source_id=(\n"
    "            f\"{projection.fact_kind}|cycle={projection.cycle_id}\"\n"
    "            f\"|account={projection.account_id}\"\n"
    "        ),\n"
)
FINGERPRINT_VALUE_LINE = '        "value": _canonical_value(projection.value),\n'
TYPE_GUARD_LINE = "    if type(projection) is not PPRM.PortfolioFactProjection:\n"
#: 归口表里的 unknown 行 —— 必须带上注释才唯一（模块 docstring 也引用了同一行）。
UNKNOWN_OUTCOME_BLOCK = (
    "    # owner 做了核验判定，结论是\"证明不了\"（归属不可证明 / archived / 数量未证明 /\n"
    "    # 账本值非有限）。**不是** source_unusable —— 障碍不在核验过程，而在这条事实本身\n"
    "    # 还不足以被证明。\n"
    "    PPRM.STATUS_UNKNOWN: ARC.OWNER_OUTCOME_UNVERIFIED,\n"
)

MUTATIONS = [
    {
        "id": "M-PFACT-1",
        # 归属/挂载证明被移除：虚构账户会拿到 verified zero。
        "file": OWNER,
        "old": MEMBERSHIP_BLOCK,
        "new": (
            "    if False:  # MUTANT —— 归属/挂载证明被移除\n"
            "        return tuple(\n"
            "            _portfolio_fact(context, account, kind, None)\n"
            "            for kind in PORTFOLIO_FACT_KINDS\n"
            "        )\n"
        ),
        "test": f"{CONTRACT_TESTS}."
                "test_PFACT_06_nonexistent_or_unattached_account_is_never_verified_zero",
        "desc": "移除 account 归属/挂载证明 → 虚构账户拿到 verified zero",
    },
    {
        "id": "M-PFACT-2",
        # 现金改读当前可变状态，而不是 bounded 重建。
        "file": OWNER,
        "old": CASH_LINE,
        "new": (
            "    _acct = conn.execute(  # MUTANT —— 直接读当前可变 cash\n"
            "        \"SELECT cash FROM paper_accounts WHERE id=?\", (account,)\n"
            "    ).fetchone()\n"
            "    cash_value, cash_status = (\n"
            "        float(_acct[0]) if _acct and _acct[0] is not None else None,\n"
            "        STATUS_VERIFIED if _acct else STATUS_UNKNOWN,\n"
            "    )\n"
        ),
        "test": f"{CONTRACT_TESTS}."
                "test_PFACT_04_cash_is_bounded_reconstruction_not_current_account_state",
        "desc": "cash 改读 paper_accounts.cash 当前可变状态",
    },
    {
        "id": "M-PFACT-3",
        # 持仓成本摘要改读 compatibility-only 投影。
        "file": OWNER,
        "old": BOUNDED_LOTS_BLOCK,
        "new": (
            "    lots, quantity_status = (  # MUTANT —— 改读兼容投影\n"
            "        _row_dicts(conn.execute(\n"
            "            \"SELECT account_id,code,qty AS remaining_qty,cost FROM paper_positions\"\n"
            "            \" WHERE account_id=? AND qty>0\",\n"
            "            (str(account_id),),\n"
            "        )),\n"
            "        STATUS_VERIFIED,\n"
            "    )\n"
        ),
        "test": f"{CONTRACT_TESTS}."
                "test_PFACT_07_position_cost_summary_uses_bounded_lots_not_the_projection",
        "desc": "position cost summary 改读 paper_positions",
    },
    {
        "id": "M-PFACT-4",
        # 数量未证明也照发 summary（verified zero）。
        "file": OWNER,
        "old": QUANTITY_GUARD_BLOCK,
        "new": (
            "    if False:  # MUTANT —— 数量未证明也照发 summary\n"
            "        return None\n"
        ),
        "test": f"{CONTRACT_TESTS}."
                "test_PFACT_08_unproven_quantity_never_becomes_a_verified_summary",
        "desc": "quantity unknown 时仍发布 verified summary",
    },
    {
        "id": "M-PFACT-5",
        # 去掉 as-of 边界：未来的 fill / lot 进入当日事实。
        "file": OWNER,
        "old": ASOF_BOUND_LINE,
        "new": "        if False:  # MUTANT —— as-of 边界被移除\n",
        "test": f"{CONTRACT_TESTS}."
                "test_PFACT_09_future_fills_and_lots_never_enter_the_fact",
        "desc": "去掉 durable lot 的 as-of 边界（未来持仓进入当日事实）",
    },
    {
        "id": "M-PFACT-6",
        # 已实现盈亏绕过 verified 口径，直接 SUM 全部 SELL。
        "file": OWNER,
        "old": REALIZED_LINE,
        "new": (
            "    _sum = conn.execute(  # MUTANT —— 绕过 verified 口径直接 SUM\n"
            "        \"SELECT COALESCE(SUM(realized_pnl),0.0) FROM paper_orders\"\n"
            "        \" WHERE cycle_id=? AND account_id=? AND side='sell'\",\n"
            "        (context.cycle_id, str(account)),\n"
            "    ).fetchone()\n"
            "    realized_value, realized_status = float(_sum[0] or 0.0), STATUS_VERIFIED\n"
        ),
        "test": f"{CONTRACT_TESTS}."
                "test_PFACT_05_realized_pnl_consumes_only_bounded_verified_sell_evidence",
        "desc": "realized_pnl 纳入未验证 / 未来 SELL（绕过 verified 口径）",
    },
    {
        "id": "M-PFACT-7",
        # owner 的 unknown 被映成 verified。
        "file": ADAPTER,
        "old": UNKNOWN_OUTCOME_BLOCK,
        "new": "    PPRM.STATUS_UNKNOWN: ARC.OWNER_OUTCOME_VERIFIED,  # MUTANT —— unknown 被映成 verified\n",
        "test": f"{ADAPTER_TESTS}."
                "test_PORT_REF_06_unknown_maps_to_owner_neutral_unverified",
        "desc": "adapter 把 STATUS_UNKNOWN 映成 owner-neutral verified",
    },
    {
        "id": "M-PFACT-8",
        # identity 丢掉 account_id → 跨账户碰撞。
        "file": ADAPTER,
        "old": SOURCE_ID_BLOCK,
        "new": (
            "        source_id=(  # MUTANT —— identity 丢掉 account\n"
            "            f\"{projection.fact_kind}|cycle={projection.cycle_id}\"\n"
            "        ),\n"
        ),
        "test": f"{ADAPTER_TESTS}."
                "test_PORT_REF_12_identity_separates_account_cycle_and_kind",
        "desc": "source_id 丢掉 account_id → 跨账户 identity 碰撞",
    },
    {
        "id": "M-PFACT-9",
        # 内容指纹忽略 factual value → 同 identity 下内容被改写不再报冲突。
        "file": ADAPTER,
        "old": FINGERPRINT_VALUE_LINE,
        "new": "",
        "test": f"{ADAPTER_TESTS}."
                "test_PORT_REF_09_a_changed_cash_value_is_a_conflict",
        "desc": "内容指纹忽略 factual value → 内容改写被静默去重",
    },
    {
        "id": "M-PFACT-10",
        # 入口接受 duck-typed 伪投影。
        "file": ADAPTER,
        "old": TYPE_GUARD_LINE,
        "new": "    if not hasattr(projection, \"status\"):  # MUTANT —— 接受 duck-typed 伪投影\n",
        "test": f"{ADAPTER_TESTS}."
                "test_PORT_REF_01_only_exact_owner_projection_is_accepted",
        "desc": "adapter 接受 dict / duck-typed 伪投影",
    },
]


def sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _adapt_eol(text: str, original: bytes) -> bytes:
    if original.count(b"\r\n") > 0:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("utf-8")


PYCACHE_ROOT = tempfile.mkdtemp(prefix="r27b2c4b_portfolio_accounting_pycache_")
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
    count = text.count(mutation["old"])
    assert count == 1, (
        f'{mutation["id"]}: mutation anchor must be unique; count={count}; '
        f'file={mutation["file"]}; anchor={mutation["old"][:60]!r}'
    )
    return text.replace(mutation["old"], mutation["new"], 1)


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
    print(f"R27-B2C-4B mutations: {len(results) - len(bad)}/{len(results)} DETECTED; "
          f"survived={sum(1 for _, v in bad if v.startswith('SURVIVED'))}; "
          f"fake={sum(1 for _, v in bad if v == 'FAKE')}; "
          f"other={sum(1 for _, v in bad if not v.startswith('SURVIVED') and v != 'FAKE')}")
    print("restore sha256: PASS")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
