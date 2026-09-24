# -*- coding: utf-8 -*-
"""R27-B2C-4A mutation matrix —— M-EXATTR-1 .. M-EXATTR-10。

只覆盖本轮**新的高风险 invariant**。每条 mutation 都必须让唯一指定的永久回归变 RED，
anchor 恰好命中一次；``--non-vacuity`` 先跑 baseline，``SyntaxError`` / ``ImportError`` /
``NameError`` 一律计为 FAKE（接线错误是假杀，不能算 caught）。

本轮的核心不变量分四组：

* **事实必须真的从 owner 到达投影**（M-EXATTR-1 / 2 / 8 / 9）：``filled_qty`` / ``fees`` /
  ``account_id`` 在投影里被替换成别的值（unknown / known zero / 常量账户），意味着 owner
  已经发布的事实被本层重新解释 —— 那正是 B2C-4A 要根除的东西；而把 owner 的
  ``not_applicable`` 伪造成 ``known(0)`` 是**发明**一笔零元费用。
* **三态不许被压平**（M-EXATTR-3）：``as_dict`` 退回 ``maybe()`` 会让 ``unknown`` 与
  ``not_applicable`` 一起变成 ``None``，于是下游再也分不清"我们不知道成交了多少"与
  "这笔委托从未提交、这个问题不存在"。
* **内容变了必须报冲突**（M-EXATTR-4 / 5 / 6 / 10）：指纹忽略 ``filled_qty`` /
  ``fill_price`` / ``fees`` / ``cycle_id`` 中的任何一个，同一条 execution identity 下被
  改写的成交就会被静默去重，研究结论可以悄悄换掉依据、甚至落到错误的周期上。
* **确认未执行的肯定性零不许被降级**（M-EXATTR-7）：owner 发布 ``known(0)`` 时把它变成
  ``unknown``，等于把一个肯定的事实说成"不知道"。

沿用 R27-B2C-1 / B2C-2 / B2C-3 的逐次唯一 ``PYTHONCACHEPREFIX``，否则 baseline 与 mutant
会共享字节码缓存，整张矩阵静默失效。**必须串行运行**：每条 mutation 就地改写 production
source，跑完按启动快照做 byte-identical 还原并校验 sha256。

用法：
    python work/r27b2c4a_execution_attribution_mutation_check.py
    python work/r27b2c4a_execution_attribution_mutation_check.py --only M-EXATTR-1
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

VERIFICATION = "backend/execution_verification.py"
ADAPTER = "backend/ai_research_execution_adapter.py"

CONTRACT_SUITE = "test_execution_fact_contract"
ADAPTER_SUITE = "test_ai_research_execution_adapter"

COMPLETENESS = f"{CONTRACT_SUITE}.AttributionFactCompletenessTests"
OWNERSHIP = f"{CONTRACT_SUITE}.OwnershipIdentityTests"
FINGERPRINT = f"{ADAPTER_SUITE}.AttributionFactFingerprintTests"

#: 投影逐字复制 owner 字段的那两行 —— 多个 mutation 共用同一个 anchor（各自独立运行）。
FILLED_QTY_LINE = "        filled_qty=evidence.filled_qty,\n"
FEES_LINE = "        fees=evidence.fees,\n"
ACCOUNT_ID_LINE = (
    '        account_id=_order_fact(\n'
    '            "account_id", provenance.get("account_id"), subject="account",\n'
    "            validator=_is_account_id,\n"
    "        ),\n"
)
PROJECTED_FILLED_QTY_LINE = '            "filled_qty": self.filled_qty.as_dict(),\n'
FINGERPRINT_FILLED_QTY_LINE = '        "filled_qty": projection.filled_qty.as_dict(),\n'
FINGERPRINT_FILL_PRICE_LINE = '        "fill_price": projection.fill_price.as_dict(),\n'
FINGERPRINT_FEES_LINE = '        "fees": projection.fees.as_dict(),\n'
FINGERPRINT_CYCLE_ID_LINE = '        "cycle_id": projection.cycle_id.as_dict(),\n'

MUTATIONS = [
    {
        "id": "M-EXATTR-1",
        # 投影不再发布 owner 的成交数量：owner 已发布的事实被本层改写。
        "file": VERIFICATION,
        "old": FILLED_QTY_LINE,
        "new": '        filled_qty=EE.EvidenceField.unknown("filled_qty"),  # MUTANT —— 丢弃 owner 的成交数量\n',
        "test": f"{COMPLETENESS}."
                "test_EXFACT_20_the_projection_publishes_the_owner_execution_facts",
        "desc": "投影不再携带 owner 的 filled_qty（改报 unknown）",
    },
    {
        "id": "M-EXATTR-2",
        # 投影不再发布 owner 的费用：PnL attribution 的直接输入消失。
        "file": VERIFICATION,
        "old": FEES_LINE,
        "new": '        fees=EE.EvidenceField.unknown("fees"),  # MUTANT —— 丢弃 owner 的费用\n',
        "test": f"{COMPLETENESS}."
                "test_EXFACT_20_the_projection_publishes_the_owner_execution_facts",
        "desc": "投影不再携带 owner 的 fees（改报 unknown）",
    },
    {
        "id": "M-EXATTR-3",
        # 三态被 maybe() 压平：unknown 与 not_applicable 变成同一个 None。
        "file": VERIFICATION,
        "old": PROJECTED_FILLED_QTY_LINE,
        "new": '            "filled_qty": self.filled_qty.maybe(),  # MUTANT —— 三态被压成 None\n',
        "test": f"{COMPLETENESS}."
                "test_EXFACT_25_as_dict_carries_the_full_three_state_payload",
        "desc": "as_dict 用 maybe() 压平 filled_qty 的三态",
    },
    {
        "id": "M-EXATTR-4",
        # 指纹忽略成交数量：同一 identity 下数量被改写会被静默去重。
        "file": ADAPTER,
        "old": FINGERPRINT_FILLED_QTY_LINE,
        "new": "",
        "test": f"{FINGERPRINT}."
                "test_EXEC_REF_21_a_changed_filled_quantity_is_a_conflict",
        "desc": "内容指纹忽略 filled_qty",
    },
    {
        "id": "M-EXATTR-5",
        # 指纹忽略成交价格。
        "file": ADAPTER,
        "old": FINGERPRINT_FILL_PRICE_LINE,
        "new": "",
        "test": f"{FINGERPRINT}."
                "test_EXEC_REF_22_a_changed_fill_price_is_a_conflict",
        "desc": "内容指纹忽略 fill_price",
    },
    {
        "id": "M-EXATTR-6",
        # 指纹忽略费用。
        "file": ADAPTER,
        "old": FINGERPRINT_FEES_LINE,
        "new": "",
        "test": f"{FINGERPRINT}."
                "test_EXEC_REF_23_a_changed_fee_is_a_conflict",
        "desc": "内容指纹忽略 fees",
    },
    {
        "id": "M-EXATTR-7",
        # owner 的肯定性零被降级成 unknown（"确认没有成交"变成"不知道"）。
        "file": VERIFICATION,
        "old": FILLED_QTY_LINE,
        "new": (
            "        filled_qty=(  # MUTANT —— 肯定性零被降级成 unknown\n"
            '            EE.EvidenceField.unknown("filled_qty")\n'
            "            if evidence.filled_qty.maybe() == 0 else evidence.filled_qty\n"
            "        ),\n"
        ),
        "test": f"{COMPLETENESS}."
                "test_EXFACT_24_a_confirmed_non_execution_is_not_padded_with_known_zeros",
        "desc": "owner 的 known(0) 在投影里被降级成 unknown",
    },
    {
        "id": "M-EXATTR-8",
        # owner 的 not_applicable 被伪造成 known(0)：凭空发明一笔零元费用。
        "file": VERIFICATION,
        "old": FEES_LINE,
        "new": (
            "        fees=(  # MUTANT —— not_applicable 被伪造成 known(0)\n"
            "            evidence.fees if evidence.fees.is_known\n"
            '            else EE.EvidenceField.known("fees", 0.0)\n'
            "        ),\n"
        ),
        "test": f"{COMPLETENESS}."
                "test_EXFACT_24_a_confirmed_non_execution_is_not_padded_with_known_zeros",
        "desc": "owner 的 not_applicable fees 被伪造成 known(0)",
    },
    {
        "id": "M-EXATTR-9",
        # 投影不再发布归属账户：跨 owner 的 PnL join 键消失。
        "file": VERIFICATION,
        "old": ACCOUNT_ID_LINE,
        "new": (
            "        account_id=(  # MUTANT —— 归属账户不再由 owner 记录派生\n"
            '            EE.EvidenceField.unknown("account_id")\n'
            "            if not provenance.get(\"account_id\")\n"
            '            else EE.EvidenceField.known("account_id", "active")\n'
            "        ),\n"
        ),
        "test": f"{OWNERSHIP}."
                "test_EXFACT_26_the_ownership_identity_is_published_from_the_owner_order_row",
        "desc": "投影不再发布 owner 记录的 account_id（改成常量/unknown）",
    },
    {
        "id": "M-EXATTR-10",
        # 指纹忽略周期：同一条成交被搬到另一个周期会被静默接受。
        "file": ADAPTER,
        "old": FINGERPRINT_CYCLE_ID_LINE,
        "new": "",
        "test": f"{FINGERPRINT}."
                "test_EXEC_REF_27_a_changed_cycle_is_a_conflict",
        "desc": "内容指纹忽略 cycle_id",
    },
]


def sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _adapt_eol(text: str, original: bytes) -> bytes:
    if original.count(b"\r\n") > 0:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("utf-8")


PYCACHE_ROOT = tempfile.mkdtemp(prefix="r27b2c4a_execution_attribution_pycache_")
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
    print(f"R27-B2C-4A mutations: {len(results) - len(bad)}/{len(results)} CAUGHT; "
          f"survived={sum(1 for _, v in bad if v.startswith('SURVIVED'))}; "
          f"fake={sum(1 for _, v in bad if v == 'FAKE')}; "
          f"other={sum(1 for _, v in bad if not v.startswith('SURVIVED') and v != 'FAKE')}")
    print("restore sha256: PASS")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
