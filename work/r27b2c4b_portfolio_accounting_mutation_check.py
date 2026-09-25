# -*- coding: utf-8 -*-
"""R27-B2C-4B mutation matrix —— M-PFACT-1 .. M-PFACT-10。

只覆盖本轮**新的高风险 invariant**。每条 mutation 都必须让**唯一指定**的永久回归变 RED，
anchor 恰好命中一次；baseline 是 matrix 的**强制前提**（无条件先于任何 mutation 运行，
没有开关），``SyntaxError`` / ``ImportError`` / ``NameError`` / collection failure 一律计为
FAKE（接线错误是假杀，不能算 detected）。

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

baseline 规则（OCR finding 的修正）：``--non-vacuity`` 开关已删除。selected mutations 确定
后，先对**全部去重后的永久 regression target** 依次运行 baseline；任一非 GREEN 即输出
``BASELINE-RED`` 并立即失败，**不进入** mutation 阶段。若不强制，一个在干净源码上本来就红的
目标会让它的所有 mutation 都被记成 CAUGHT —— 那是假证据，违反 harness 自身的证据链要求。

超时是**独立的分类**，既不是 CAUGHT 也不是 FAKE：mutation 运行抛
``subprocess.TimeoutExpired`` 记 ``TIMEOUT``；baseline 抛则记 ``BASELINE-TIMEOUT``，
两者都让 matrix FAIL。把超时折叠进 "被杀死"，等于把一个从未作出判定的运行发布成有效证据。

CLI 的选择语义**绝不能静默变化**：受支持的形式只有 ``--only <ids>`` 与 ``--only=<ids>``。
除此之外的任何 argv —— ``--only*`` 的拼写错误、空 id 列表、重复 selector，以及
``--onl M-PFACT-1`` / ``--dry-run`` / 裸位置参数这类完全不认识的 token —— 都是受控
ERROR + exit 2，而不是"没有 selector 所以跑全量 matrix"。操作者请求 targeted mutation 时，
覆盖范围绝不能因为 CLI 拼写问题被悄悄放大：``--onl`` 连 ``--only`` 前缀都不匹配，
静默忽略它等于把"只跑 1 条"变成"跑 10 条"。

本文件自身的硬不变量**不使用 Python ``assert``**：``python -O`` 会剥除 assert，而证据链的守卫
不能因为一个优化开关消失。所有 runtime evidence 断言走 :func:`_require`（显式 ``RuntimeError``），
并由 :func:`self_test_optimization` 在 ``python -O`` 子进程里证明守卫没有被 optimization 消掉。

用法：
    python work/r27b2c4b_portfolio_accounting_mutation_check.py
    python work/r27b2c4b_portfolio_accounting_mutation_check.py --only M-PFACT-1
    python work/r27b2c4b_portfolio_accounting_mutation_check.py --only=M-PFACT-1,M-PFACT-2
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


#: mutation 的终态分类。TIMEOUT 与 CAUGHT 语义不同：前者从未作出判定。
VERDICT_CAUGHT = "CAUGHT"
VERDICT_SURVIVED = "SURVIVED"
VERDICT_FAKE = "FAKE"
VERDICT_TIMEOUT = "TIMEOUT"

#: baseline 超时 —— 与 BASELINE-RED 语义不同（测试根本没跑完，而非在干净源码上失败）。
BASELINE_RED = "BASELINE-RED"
BASELINE_TIMEOUT = "BASELINE-TIMEOUT"


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
    还原、``--only`` 选择器与 argv 白名单的全部参数边界。
    """
    root = tempfile.mkdtemp(prefix="r27b2c4b_mutation_semantics_")
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
    _require("MUTANT" not in source_bytes().decode("utf-8"),
             "TIMEOUT must not leave the mutant on disk")

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
        (["--only", "M-PFACT-1"], {"M-PFACT-1"}),
        (["--only=M-PFACT-1"], {"M-PFACT-1"}),
        (["--only", "M-PFACT-1,M-PFACT-2"], {"M-PFACT-1", "M-PFACT-2"}),
        (["--only=M-PFACT-1,M-PFACT-2"], {"M-PFACT-1", "M-PFACT-2"}),
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
        ["--onlyy=M-PFACT-1"],
        ["--only", "--only"],
        ["--only", "M-PFACT-1", "--only", "M-PFACT-2"],
    ):
        only, err = _parse_only(argv)
        _require(only is None and isinstance(err, str) and err.startswith("ERROR:"),
                 f"{argv} must be a controlled ERROR, got {(only, err)}")

    # 9b) argv 白名单（main 真正走的入口）：不认识的 token 不是"没有 selector"，
    #     不能被静默忽略成一次全量 matrix。
    _require(_parse_argv([]) == (None, None), "empty argv must mean the full matrix")
    for argv, ids in (
        (["--only", "M-PFACT-1"], {"M-PFACT-1"}),
        (["--only=M-PFACT-1"], {"M-PFACT-1"}),
    ):
        _require(_parse_argv(argv) == (ids, None), f"{argv} must select {ids}")
    _require(_parse_argv(["--only", "UNKNOWN-ID"]) == ({"UNKNOWN-ID"}, None),
             "an unknown id is a selection miss, not a parse error")
    for argv in (
        ["--only"],
        ["--only="],
        ["--onlyy=M-PFACT-1"],
        ["--onl", "M-PFACT-1"],
        ["--dry-run"],
        ["foo"],
        ["--only", "M-PFACT-1", "foo"],
        ["--only", "M-PFACT-1", "--only", "M-PFACT-2"],
        ["--non-vacuity"],
    ):
        only, err = _parse_argv(argv)
        _require(only is None and isinstance(err, str) and err.startswith("ERROR:"),
                 f"{argv} must be a controlled ERROR, got {(only, err)}")

    # 10) 同一条边界在**真实 CLI** 上：exit 2、受控 ERROR、不抛裸 traceback、
    #     不进选择/baseline/mutation 阶段，且 production source 逐字节不变。
    guarded = {}
    for name in (OWNER, ADAPTER):
        with open(os.path.join(ROOT, name), "rb") as handle:
            guarded[name] = sha256(handle.read())
    for argv in (["--only"], ["--only="], ["--onlyy=M-PFACT-1"], ["--onl", "M-PFACT-1"],
                 ["--dry-run"], ["foo"], ["--non-vacuity"],
                 ["--only", "M-PFACT-1", "foo"],
                 ["--only", "M-PFACT-1", "--only", "M-PFACT-2"]):
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
    root = tempfile.mkdtemp(prefix="r27b2c4b_optimization_probe_")
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
        if "MUTANT" in handle.read():
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


def run_baselines(selected, *, runner=run_test) -> int:
    """在触碰任何 production source **之前**，先证明全部目标永久回归都是 GREEN。

    baseline 是 matrix 自身的强制前提，不是可选观察项：某个目标若在干净源码上本来就红，
    它的所有 mutation 都会因 ``returncode != 0`` 被记成 CAUGHT —— 那是假证据。因此对
    selected mutations 的**去重** target 集合依次运行；任一非 GREEN 立即失败，
    **不进入** mutation 阶段。

    baseline 阶段的 ``subprocess.TimeoutExpired`` 记 :data:`BASELINE_TIMEOUT`，与
    ``BASELINE-RED`` **语义不同**（前者根本没跑完，后者是在干净源码上失败），但同样
    让 matrix FAIL 且 mutation 阶段不启动。
    """
    targets: list[str] = []
    for mutation in selected:
        if mutation["test"] not in targets:
            targets.append(mutation["test"])
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
    含任何"是否跑 baseline"的分支 —— 那正是 OCR 抓到的假证据缺口：默认路径允许跳过
    baseline，于是一个本来就红的目标会让它的所有 mutation 被记成 CAUGHT。

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
    理由：操作者请求 targeted mutation 时，实际覆盖范围绝不能因为 CLI 拼写问题被静默放大 ——
    那会让一份"只跑了 1 条 mutation"的证据看起来像一次全量验证。

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
                "(for example: --only M-PFACT-1,M-PFACT-2)"
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

    **不做静默忽略**：``--onl M-PFACT-1`` / ``--dry-run`` / ``foo`` 都不是"没有 selector"，
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
    #: 覆盖范围必须显式回显 —— 参数谜题的代价正是"以为只跑了 1 条，其实跑了 10 条"。
    print(f'selected: {len(selected)}/{len(MUTATIONS)} mutation(s): '
          f'{[m["id"] for m in selected]}', flush=True)

    self_test_sequence()
    print("runner self-test: PASS (unique, increasing pycache sequence)")
    self_test_semantics()
    print("semantics self-test: PASS (anchor uniqueness / BASELINE-RED / BASELINE-TIMEOUT / "
          "CAUGHT / SURVIVED / FAKE / TIMEOUT / restore / --only)")
    self_test_optimization()
    print("optimization self-test: PASS (hard guards survive python -O)")

    if run_baselines(selected) != 0:
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
    print(f"R27-B2C-4B mutation matrix: baseline=GREEN; "
          f"{detected}/{len(results)} DETECTED; survived={survived}; fake={fake}; "
          f"timeout={timeout}")
    gate_pass = not bad
    print("gate: baseline=GREEN, survived=0, fake=0, timeout=0, restore sha256=PASS -> "
          f"{'PASS' if gate_pass else 'FAIL'}")
    return 0 if gate_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
