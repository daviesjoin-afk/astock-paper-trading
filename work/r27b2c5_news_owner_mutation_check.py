# -*- coding: utf-8 -*-
"""R27-B2C-5 mutation matrix —— M-NEWS-01 .. M-NEWS-17。

只覆盖本轮**新的高风险 invariant**。每条 mutation 都必须让唯一指定的永久回归变 RED，
anchor 恰好命中一次；baseline 是 matrix 的强制前提，接线错误（SyntaxError / ImportError /
NameError …）一律计为 FAKE（假杀不能算证据）。

本轮的不变量分五组：

* **availability 只能来自 first_seen_at**（M-NEWS-01 / 05 / 06 / 13）：
  ``published_at``（来源声称的发布时间）与 ``created_at``（行写入时刻）都**不是**可用性
  authority；把任一个拿来做边界，或让不可证明的 PIT 回退到描述性时间，都必须 RED。
* **grade / 单源 / 未知状态都不是核验**（M-NEWS-02 / 03 / 04 / 16）：
  ``grade="A"`` 不得升级；``single_source_linked`` 不得升级；未知 ledger 状态不得默认；
  归口退回 catch-all（把 source-unusable 压平进 unverified）同样必须 RED。
* **历史读不得联网 / 不得回填**（M-NEWS-07）：ledger 缺失时触发 ingestion fetch 必须 RED。
* **identity / PIT 不得由调用方自述**（M-NEWS-08 / 09 / 10 / 15）：
  adapter 接受 duck-typed 投影、签名多出 ``source_id`` / ``as_of``、内容指纹被常量化 ——
  都必须 RED。
* **reputation / candidate link 不得进入事件读路径**（M-NEWS-11 / 12）：
  把这两张表拉进 SELECT 必须 RED。
* **writer 闭集与 registry 是审计产物**（M-NEWS-14 / 17）：
  writer 冒出一个新的状态词、或 ``SUPPORTED_OWNER_ADAPTERS`` 与真身漂移，必须 RED。

**两处与 brief 字面写法的偏离（显式记录，不静默）**：

1. brief 的 M-NEWS-05 / 06 写作"missing first_seen → fallback published_at / created_at"。
   那个形状在当前代码里**不可观测**：typed projection 自己会独立地对 ``first_seen_at``
   fail closed（``NewsFactContractError``），因此"只在缺失时回退"的变异作用在同一行上时，
   读路径仍然会抛错，测试依旧通过 —— 那会得到一次**假杀**，不是证据。所以这两条改成同一
   不变量**可观测**的形状：availability 由 ``published_at`` / ``created_at`` 决定（含
   ``or`` 回退形态）。它们被 NEWS-06 / NEWS-30 打红，语义完全一致。
2. brief 的 M-NEWS-11 / 12 写作"confidence / reputation 升级 verification"。在正确设计里
   这两张表**根本不可达**（projection 不携带任何 confidence / score 字段，adapter 不碰
   DB），所以"升级"那条变异在最终代码形状上无法写出来。改成不变量**执行点**上的变异：
   把 reputation / candidate-link 表拉进事件读路径的 SELECT。它被 NEWS-14 / NEWS-15 打红。

**一次被变异逼出来的真实修正（记在这里，因为它改变了 production 代码）**：

M-NEWS-04 最初打在 ``_major_event_owner_status`` 的显式表查找上，结果 **SURVIVED** ——
因为 ``NewsFactProjection.__post_init__`` 当时**重复**了一遍等价的闭集检查，把这次退化挡住
了。两处等价的 fail-closed 互相掩盖，使"未知 ledger 状态被静默默认"这类退化逃过一次单点
变异；也就是说那条冗余检查让**两条路径中的一条**实际上不受回归保护。修正不是给变异找借口，
而是去掉重复：闭集合法性现在**只有** ``_major_event_owner_status`` 一处判定，所以它既被
NEWS-13 打红，也仍然对所有构造路径生效。这与本仓库"校验逻辑只有一份"的既有原则一致。

沿用 R27-B2C-1 / B2C-2 / B2C-3 / B2C-4x 的逐次唯一 ``PYTHONCACHEPREFIX``，否则 baseline 与
mutant 会共享字节码缓存，整张矩阵静默失效。**必须串行运行**：每条 mutation 就地改写
production source，跑完按启动快照做 byte-identical 还原并校验 sha256；本轮会改写**三个**
production file（adapter / owner / contract），三者都在启动快照与收尾复验的覆盖范围内。

用法：
    python work/r27b2c5_news_owner_mutation_check.py
    python work/r27b2c5_news_owner_mutation_check.py --only M-NEWS-01
    python work/r27b2c5_news_owner_mutation_check.py --only=M-NEWS-01,M-NEWS-11
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

#: 本轮的 production files under test —— 每条 mutation 就地改写其中之一。
ADAPTER_FILE = "backend/ai_research_news_adapter.py"
OWNER_FILE = "backend/news_learning.py"
CONTRACT_FILE = "backend/ai_research_contract.py"

#: 启动快照与收尾复验覆盖的**全部**被改写文件。
MUTATED_FILES = (ADAPTER_FILE, OWNER_FILE, CONTRACT_FILE)

#: 收尾复验的基准文件（与既有 matrix 的 DS_FILE 语义相同）。
DS_FILE = ADAPTER_FILE

SUITE = "test_ai_research_news_adapter"
OWNER_SUITE = "test_news_fact_contract"
CLASS = f"{SUITE}.NewsAdapterTests"
OWNER_CLASS = f"{OWNER_SUITE}.NewsFactContractTests"


def _adapter(name: str) -> str:
    return f"{CLASS}.{name}"


def _owner(name: str) -> str:
    return f"{OWNER_CLASS}.{name}"


T_NEWS_03 = _adapter("test_NEWS_03_source_type_is_the_existing_news_source")
T_NEWS_04 = _adapter("test_NEWS_04_information_event_kind_is_news_observed")
T_NEWS_06 = _owner("test_NEWS_06_published_at_cannot_make_evidence_available_earlier")
T_NEWS_07 = _owner("test_NEWS_07_future_first_seen_is_excluded_from_a_historical_read")
T_NEWS_08 = _owner("test_NEWS_08_missing_or_malformed_first_seen_fails_closed_without_any_fallback")
T_NEWS_09 = _adapter("test_NEWS_09_evidence_grade_a_never_becomes_owner_verified")
T_NEWS_10 = _adapter("test_NEWS_10_evidence_grade_b_and_c_never_become_owner_verified")
T_NEWS_11 = _owner("test_NEWS_11_untraceable_or_low_grade_source_fails_closed_without_verified")
T_NEWS_12 = _adapter("test_NEWS_12_single_source_linked_maps_to_unverified_not_verified")
T_NEWS_13 = _owner("test_NEWS_13_unknown_verification_status_is_a_hard_error_not_a_default")
T_NEWS_14 = _owner("test_NEWS_14_source_reputation_never_changes_event_verification")
T_NEWS_15 = _owner("test_NEWS_15_candidate_link_confidence_never_changes_event_verification")
T_NEWS_16 = _owner("test_NEWS_16_raw_payload_cannot_override_normalized_owner_columns")
T_NEWS_17 = _adapter("test_NEWS_17_adapter_rejects_dict_and_any_mapping")
T_NEWS_19 = _adapter("test_NEWS_19_content_fingerprint_is_deterministic")
T_NEWS_20 = _adapter("test_NEWS_20_identity_pit_and_verification_changes_move_the_fingerprint")
T_NEWS_21 = _adapter("test_NEWS_21_information_event_payload_cannot_override_verification")
T_NEWS_22 = _adapter("test_NEWS_22_future_news_evidence_cannot_enter_an_earlier_event")
T_NEWS_23 = _owner("test_NEWS_23_typed_read_never_calls_the_fetchers_and_is_a_pure_read")
T_NEWS_24 = _owner("test_NEWS_24_historical_miss_never_triggers_live_fetch_or_backfill")
T_NEWS_25 = _adapter("test_NEWS_25_owner_factory_registries_agree_with_the_news_adapter")
T_NEWS_26 = _adapter("test_NEWS_26_news_adapter_has_zero_production_callers")
T_NEWS_27 = _owner("test_NEWS_27_major_event_writer_status_closed_set_is_audited")
T_NEWS_28 = _owner("test_NEWS_28_first_seen_at_is_immutable_across_recaptures")
T_NEWS_29 = _owner("test_NEWS_29_duplicate_identity_reads_the_durable_row_not_the_new_payload")
T_NEWS_30 = _owner("test_NEWS_30_created_at_is_never_an_availability_authority")
T_NEWS_31 = _adapter("test_NEWS_31_adapter_signature_takes_only_the_owner_projection")
T_NEWS_32 = _adapter("test_NEWS_32_caller_cannot_name_the_identity_or_the_as_of")
T_NEWS_33 = _adapter("test_NEWS_33_adapter_is_pure_no_db_no_network_no_clock")

#: brief 点名、但本 matrix **不**声称能打红的永久回归：它们必须在 baseline 里是 GREEN。
#: 这些是"能力/边界已经存在"的断言（owner 端到端、契约层 look-ahead、registry 三方一致、
#: writer 幂等），本轮没有一条单点变异可归因到它们身上 —— 声称能打红才是假证据。
BASELINE_ONLY_TARGETS = (
    T_NEWS_03, T_NEWS_04, T_NEWS_07, T_NEWS_10, T_NEWS_11, T_NEWS_16, T_NEWS_19,
    T_NEWS_21, T_NEWS_22, T_NEWS_23, T_NEWS_26, T_NEWS_28, T_NEWS_29, T_NEWS_33,
)

# --- mutation anchors（逐字节，必须恰好命中一次）--------------------------------

#: ``news_fact_projections`` 的 availability 判定 —— 只允许读 ``first_seen_at``。
AVAILABILITY_LINE = (
    "        instant = _parse_owner_instant(row[\"first_seen_at\"], what=\"first_seen_at\")\n"
)
#: ``NewsFactProjection.__post_init__`` 的 PIT 校验 —— 不可证明即 fail closed。
POST_INIT_PIT_LINE = (
    "        instant = _parse_owner_instant(self.first_seen_at, what=\"first_seen_at\")\n"
)
#: 历史读遇到不可读 ledger 的 fail-closed 分支。
LEDGER_UNAVAILABLE = (
    "        except sqlite3.OperationalError as exc:\n"
    "            raise NewsFactContractError(\n"
    "                \"ledger_unavailable\",\n"
    "                f\"{kind} 的 durable ledger 不可读：{exc}\",\n"
    "            ) from exc\n"
)
#: major event 的 ledger 状态词 → owner 闭集（显式表查找）。
MAJOR_MAPPING_LOOKUP = (
    "    mapped = _NEWS_MAJOR_LEDGER_STATUS_TO_OWNER.get(str(ledger_status))\n"
)
#: ``capture_major_events`` 里的 writer 表达式 —— 审计过的闭集就来自它。anchor 带上同一行
#: 的取值上下文（含结尾逗号），避免命中 owner 侧文档里对它的引用，也避免把逗号挤进注释。
WRITER_EXPRESSION = (
    "\"single_source_linked\" if source_url else \"unverified\","
    " _json({**item, \"major_trigger\": profile[\"trigger\"]}), now, now),"
)
#: adapter 的 owner 状态 → 中性三态 显式表。
ADAPTER_MAPPING_LINE = (
    "    NL.NEWS_OWNER_SINGLE_SOURCE: ARC.OWNER_OUTCOME_UNVERIFIED,\n"
)
#: adapter 归口的唯一解释点。
ADAPTER_VERIFICATION_HEAD = "    status = projection.owner_verification_status\n"
#: adapter 的 exact-type 输入校验。
ADAPTER_TYPE_CHECK = "    if type(projection) is not NL.NewsFactProjection:\n"
#: adapter 的唯一公开签名 —— 只接受 owner 投影。
ADAPTER_SIGNATURE = (
    "def evidence_ref_from_news_projection(projection: Any) -> ARC.ResearchEvidenceRef:\n"
)
#: adapter 的归口结果赋值 —— catch-all 变异的落点。
ADAPTER_OUTCOME_LINE = "        outcome=_NEWS_OUTCOME_BY_STATUS[status],\n"
#: adapter 内容指纹的返回 —— 常量化变异的落点。
ADAPTER_FINGERPRINT_LINE = "    return hashlib.sha256(encoded.encode(\"utf-8\")).hexdigest()\n"
#: 事件读路径的 SQL —— reputation / candidate link 只能在这里被错误地拉进来。
EVENT_READ_SQL = (
    "    NEWS_RECORD_KIND_EVENT: f\"SELECT {_NEWS_FACT_EVENT_COLUMNS} FROM news_events\",\n"
)
MAJOR_READ_SQL = (
    "    NEWS_RECORD_KIND_MAJOR_EVENT: (\n"
    "        f\"SELECT {_NEWS_FACT_MAJOR_COLUMNS} FROM market_major_events\"\n"
    "    ),\n"
)
#: 契约的 owner adapter registry —— 新增/删除一个 owner 必须是一次有意识的动作。
REGISTRY_BLOCK = (
    "SUPPORTED_OWNER_ADAPTERS = frozenset({\n"
    "    EVIDENCE_SOURCE_MARKET_DATA,\n"
    "    EVIDENCE_SOURCE_EXECUTION,\n"
    "    EVIDENCE_SOURCE_PORTFOLIO_RESEARCH,\n"
    "    EVIDENCE_SOURCE_NEWS,\n"
    "})\n"
)

MUTATIONS = [
    {
        "id": "M-NEWS-01",
        # availability 直接改成来源声称的发布时间 —— 典型 look-ahead。
        "file": OWNER_FILE,
        "old": AVAILABILITY_LINE,
        "new": (
            "        instant = _parse_owner_instant(  # MUTANT —— 用 published_at 当可用性\n"
            "            row[\"published_at\"], what=\"first_seen_at\")\n"
        ),
        "test": T_NEWS_06,
        "desc": "historical availability 由 published_at 决定（9/20 就能看到 9/21 才摄取的新闻）",
    },
    {
        "id": "M-NEWS-02",
        # grade 被当成核验通过。
        "file": ADAPTER_FILE,
        "old": ADAPTER_VERIFICATION_HEAD,
        "new": (
            "    if projection.evidence_grade == \"A\":  # MUTANT —— grade A 被当成核验通过\n"
            "        return ARC.OwnerVerification(\n"
            "            outcome=ARC.OWNER_OUTCOME_VERIFIED,\n"
            "            status=projection.owner_verification_status,\n"
            "            attributes={\"evidence_grade\": projection.evidence_grade},\n"
            "        )\n"
            + ADAPTER_VERIFICATION_HEAD
        ),
        "test": T_NEWS_09,
        "desc": "evidence_grade A 被升级成 OWNER_OUTCOME_VERIFIED",
    },
    {
        "id": "M-NEWS-03",
        # 单源可追溯被当成已核验。
        "file": ADAPTER_FILE,
        "old": ADAPTER_MAPPING_LINE,
        "new": (
            "    NL.NEWS_OWNER_SINGLE_SOURCE: ARC.OWNER_OUTCOME_VERIFIED,"
            "  # MUTANT —— 单源被当成已核验\n"
        ),
        "test": T_NEWS_12,
        "desc": "single_source 被映射成 OWNER_OUTCOME_VERIFIED",
    },
    {
        "id": "M-NEWS-04",
        # 未知 ledger 状态被静默默认，而不是 hard error。
        "file": OWNER_FILE,
        "old": MAJOR_MAPPING_LOOKUP,
        "new": (
            "    mapped = _NEWS_MAJOR_LEDGER_STATUS_TO_OWNER.get(  # MUTANT —— 未知状态默认\n"
            "        str(ledger_status), NEWS_OWNER_UNVERIFIED)\n"
        ),
        "test": T_NEWS_13,
        "desc": "未审计过的 verification_status 被默认成 unverified 而不是 hard error",
    },
    {
        "id": "M-NEWS-05",
        # 缺失时回退到发布时间（可观测的等价形状，见模块 docstring）。
        "file": OWNER_FILE,
        "old": AVAILABILITY_LINE,
        "new": (
            "        instant = _parse_owner_instant(  # MUTANT —— 缺失就回退 published_at\n"
            "            row[\"published_at\"] or row[\"first_seen_at\"], what=\"first_seen_at\")\n"
        ),
        "test": T_NEWS_06,
        "desc": "availability 在 first_seen_at 不可用/更晚时回退到 published_at",
    },
    {
        "id": "M-NEWS-06",
        # 用行写入时刻当可用性 —— 不是 created_at fallback 的另一个假杀形状。
        "file": OWNER_FILE,
        "old": AVAILABILITY_LINE,
        "new": (
            "        instant = _parse_owner_instant(  # MUTANT —— created_at 被当成可用性\n"
            "            row[\"created_at\"] or row[\"first_seen_at\"], what=\"first_seen_at\")\n"
        ),
        "test": T_NEWS_30,
        "desc": "availability 由 created_at 决定（写入时刻不是观测时刻）",
    },
    {
        "id": "M-NEWS-07",
        # 历史 miss 触发 ingestion 抓取（PIT blocker）。
        "file": OWNER_FILE,
        "old": LEDGER_UNAVAILABLE,
        "new": (
            "        except sqlite3.OperationalError as exc:\n"
            "            _event_rows([])  # MUTANT —— 历史 miss 触发 ingestion fetch / backfill\n"
            + LEDGER_UNAVAILABLE[
                len("        except sqlite3.OperationalError as exc:\n"):]
        ),
        "test": T_NEWS_24,
        "desc": "ledger 缺失时触发 live fetch / backfill 而不是 fail closed",
    },
    {
        "id": "M-NEWS-08",
        # adapter 接受 duck-typed 投影。
        "file": ADAPTER_FILE,
        "old": ADAPTER_TYPE_CHECK,
        "new": "    if False:  # MUTANT —— 接受 dict / duck-typed 投影\n",
        "test": T_NEWS_17,
        "desc": "adapter 不再强制 exact-type，dict / duck type 可以冒充 owner 投影",
    },
    {
        "id": "M-NEWS-09",
        # 调用方可以自述 identity。
        "file": ADAPTER_FILE,
        "old": ADAPTER_SIGNATURE,
        "new": (
            "def evidence_ref_from_news_projection(  # MUTANT —— caller 可自述 identity\n"
            "        projection: Any, source_id: Any = None) -> ARC.ResearchEvidenceRef:\n"
        ),
        "test": T_NEWS_31,
        "desc": "adapter 签名多出 source_id 参数（调用方可以命名 evidence identity）",
    },
    {
        "id": "M-NEWS-10",
        # 调用方可以自述 as_of。
        "file": ADAPTER_FILE,
        "old": ADAPTER_SIGNATURE,
        "new": (
            "def evidence_ref_from_news_projection(  # MUTANT —— caller 可自述 as_of\n"
            "        projection: Any, as_of: Any = None) -> ARC.ResearchEvidenceRef:\n"
        ),
        "test": T_NEWS_32,
        "desc": "adapter 签名多出 as_of 参数（调用方可以自述 PIT 业务日）",
    },
    {
        "id": "M-NEWS-11",
        # source reputation 被拉进事件读路径。
        "file": OWNER_FILE,
        "old": EVENT_READ_SQL,
        "new": (
            "    NEWS_RECORD_KIND_EVENT: (  # MUTANT —— reputation 被拉进事件读路径\n"
            "        f\"SELECT {_NEWS_FACT_EVENT_COLUMNS},\"\n"
            "        \" (SELECT r.credibility_score FROM news_source_reputation r\"\n"
            "        \" WHERE r.source_name=news_events.source_name) AS source_credibility\"\n"
            "        \" FROM news_events\"\n"
            "    ),\n"
        ),
        "test": T_NEWS_14,
        "desc": "typed read 把 news_source_reputation 拉进事件事实读路径",
    },
    {
        "id": "M-NEWS-12",
        # candidate-link confidence 被拉进事件读路径。
        "file": OWNER_FILE,
        "old": MAJOR_READ_SQL,
        "new": (
            "    NEWS_RECORD_KIND_MAJOR_EVENT: (  # MUTANT —— candidate link 被拉进读路径\n"
            "        f\"SELECT {_NEWS_FACT_MAJOR_COLUMNS},\"\n"
            "        \" (SELECT MAX(l.confidence) FROM market_event_candidate_links l\"\n"
            "        \" WHERE l.event_id=market_major_events.id) AS link_confidence\"\n"
            "        \" FROM market_major_events\"\n"
            "    ),\n"
        ),
        "test": T_NEWS_15,
        "desc": "typed read 把 market_event_candidate_links 拉进事件事实读路径",
    },
    {
        "id": "M-NEWS-13",
        # PIT 不可证明时回退到描述性发布时间。
        "file": OWNER_FILE,
        "old": POST_INIT_PIT_LINE,
        "new": (
            "        try:  # MUTANT —— PIT 不可证明时回退到描述性发布时间\n"
            "            instant = _parse_owner_instant(self.first_seen_at,"
            " what=\"first_seen_at\")\n"
            "        except NewsFactContractError:\n"
            "            instant = _parse_owner_instant(self.published_at, what=\"published_at\")\n"
        ),
        "test": T_NEWS_08,
        "desc": "first_seen_at 缺失/畸形时回退到 published_at，而不是 fail closed",
    },
    {
        "id": "M-NEWS-14",
        # writer 冒出一个新的状态词，却没人更新 owner 映射。
        "file": OWNER_FILE,
        "old": WRITER_EXPRESSION,
        "new": WRITER_EXPRESSION.replace(
            "\"single_source_linked\"", "\"multi_source_verified\"",
        ) + "  # MUTANT",
        "test": T_NEWS_27,
        "desc": "writer 新增一个未审计的 verification_status，owner 映射没有跟着更新",
    },
    {
        "id": "M-NEWS-15",
        # 内容指纹被常量化：内容变了会被静默去重。
        "file": ADAPTER_FILE,
        "old": ADAPTER_FINGERPRINT_LINE,
        "new": "    return hashlib.sha256(b\"news\").hexdigest()  # MUTANT —— 指纹常量化\n",
        "test": T_NEWS_20,
        "desc": "content fingerprint 被常量化（identity / PIT / 核验变化不再可见）",
    },
    {
        "id": "M-NEWS-16",
        # 归口退回 catch-all：来源级别的不可用被压平。
        "file": ADAPTER_FILE,
        "old": ADAPTER_OUTCOME_LINE,
        "new": (
            "        outcome=ARC.OWNER_OUTCOME_UNVERIFIED,"
            "  # MUTANT —— catch-all：source_unusable 被压平\n"
        ),
        "test": T_NEWS_12,
        "desc": "归口退回 catch-all（source_unusable 与 unverified 混同）",
    },
    {
        "id": "M-NEWS-17",
        # registry 与真身漂移。
        "file": CONTRACT_FILE,
        "old": REGISTRY_BLOCK,
        "new": REGISTRY_BLOCK.replace("    EVIDENCE_SOURCE_NEWS,\n", "")
        + "# MUTANT —— registry 与已存在的 news adapter 漂移\n",
        "test": T_NEWS_25,
        "desc": "SUPPORTED_OWNER_ADAPTERS 删掉 news，与已存在的 news adapter 漂移",
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

#: 变异体落盘记号。还原之后文件里**不得**再有它（``assert_no_leftover``）。
MUTANT_MARKER = "MUTANT"


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


PYCACHE_ROOT = tempfile.mkdtemp(prefix="r27b2c5_news_owner_pycache_")
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
    还原、mutant 记号残留检测、``--only`` 选择器与 argv 白名单的全部参数边界。
    """
    root = tempfile.mkdtemp(prefix="r27b2c5_mutation_semantics_")
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
    extra_target = "test_semantics.Fake.test_extra"

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

    # 2b) baseline-only 的永久回归目标（brief 点名但本 matrix 不声称能打红的那批）
    #     必须一起跑、且与 mutation target 去重 —— 否则"这些目标也验过"只是句话。
    seen.clear()

    def green_baseline(target: str, seq: int | None = None):
        seen.append(target)
        return result(0)

    _require(run_baselines([mutation], runner=green_baseline,
                           extra=(mutation["test"], extra_target)) == 0,
             "baseline with extras must pass")
    _require(seen == [mutation["test"], extra_target],
             f"baseline extras must be appended and deduped: {seen}")

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
    _require(MUTANT_MARKER not in source_bytes().decode("utf-8"),
             "TIMEOUT must not leave the mutant on disk")

    # 6b) mutant 记号残留必须被 hard fail（"还原了"与"还原成什么"是两件事）。
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(original + "# MUTANT\n")
    try:
        assert_no_leftover(path, mutation["id"])
    except RuntimeError as exc:
        _require("leftover mutant" in str(exc), exc)
    else:
        raise RuntimeError("leftover mutant did not hard-fail")
    with open(path, "wb") as handle:
        handle.write(original.encode("utf-8"))

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
        (["--only", "M-NEWS-01"], {"M-NEWS-01"}),
        (["--only=M-NEWS-01"], {"M-NEWS-01"}),
        (["--only", "M-NEWS-01,M-NEWS-02"], {"M-NEWS-01", "M-NEWS-02"}),
        (["--only=M-NEWS-01,M-NEWS-02"], {"M-NEWS-01", "M-NEWS-02"}),
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
        ["--onlyy=M-NEWS-01"],
        ["--only", "--only"],
        ["--only", "M-NEWS-01", "--only", "M-NEWS-02"],
    ):
        only, err = _parse_only(argv)
        _require(only is None and isinstance(err, str) and err.startswith("ERROR:"),
                 f"{argv} must be a controlled ERROR, got {(only, err)}")

    # 9b) argv 白名单（main 真正走的入口）：不认识的 token 不是"没有 selector"，
    #     不能被静默忽略成一次全量 matrix。
    _require(_parse_argv([]) == (None, None), "empty argv must mean the full matrix")
    for argv, ids in (
        (["--only", "M-NEWS-01"], {"M-NEWS-01"}),
        (["--only=M-NEWS-01"], {"M-NEWS-01"}),
    ):
        _require(_parse_argv(argv) == (ids, None), f"{argv} must select {ids}")
    _require(_parse_argv(["--only", "UNKNOWN-ID"]) == ({"UNKNOWN-ID"}, None),
             "an unknown id is a selection miss, not a parse error")
    for argv in (
        ["--only"],
        ["--only="],
        ["--onlyy=M-NEWS-01"],
        ["--onl", "M-NEWS-01"],
        ["--dry-run"],
        ["foo"],
        ["--only", "M-NEWS-01", "foo"],
        ["--only", "M-NEWS-01", "--only", "M-NEWS-02"],
        ["--non-vacuity"],
    ):
        only, err = _parse_argv(argv)
        _require(only is None and isinstance(err, str) and err.startswith("ERROR:"),
                 f"{argv} must be a controlled ERROR, got {(only, err)}")

    # 10) 同一条边界在**真实 CLI** 上：exit 2、受控 ERROR、不抛裸 traceback、
    #     不进选择/baseline/mutation 阶段，且 production source 逐字节不变。
    guarded = {}
    for name in MUTATED_FILES:
        with open(os.path.join(ROOT, name), "rb") as handle:
            guarded[name] = sha256(handle.read())
    for argv in (["--only"], ["--only="], ["--onlyy=M-NEWS-01"], ["--onl", "M-NEWS-01"],
                 ["--dry-run"], ["foo"], ["--non-vacuity"],
                 ["--only", "M-NEWS-01", "foo"],
                 ["--only", "M-NEWS-01", "--only", "M-NEWS-02"]):
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
    root = tempfile.mkdtemp(prefix="r27b2c5_optimization_probe_")
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
        if MUTANT_MARKER in handle.read():
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


def _verify_untouched(path: str, original: bytes, before: str) -> None:
    """整张矩阵跑完后，production file 必须逐字节等于启动快照。"""
    with open(path, "rb") as handle:
        current = handle.read()
    if current != original or sha256(current) != before:
        raise RuntimeError(
            f"{path}: 矩阵结束后与启动快照不一致 "
            f"({sha256(current)} != {before})"
        )


def _targets_for(selected, extra=()) -> list[str]:
    """baseline 的目标集：selected mutations 的去重 target + 点名的永久回归目标。

    两条来源都按**首次出现**去重：同一目标被多条 mutation 驱动（或既是 mutation
    target 又是 baseline-only target）时只跑一次，但覆盖范围必须显式可见。
    """
    targets: list[str] = []
    for mutation in selected:
        if mutation["test"] not in targets:
            targets.append(mutation["test"])
    for target in extra:
        if target not in targets:
            targets.append(target)
    return targets


def run_baselines(selected, *, runner=run_test, extra=()) -> int:
    """在触碰任何 production source **之前**，先证明全部目标永久回归都是 GREEN。

    baseline 是 matrix 自身的强制前提，不是可选观察项：某个目标若在干净源码上本来就红，
    它的所有 mutation 都会因 ``returncode != 0`` 被记成 CAUGHT —— 那是假证据。因此对
    selected mutations 的**去重** target 集合（外加 :data:`BASELINE_ONLY_TARGETS`）依次
    运行；任一非 GREEN 立即失败，**不进入** mutation 阶段。

    baseline 阶段的 ``subprocess.TimeoutExpired`` 记 :data:`BASELINE_TIMEOUT`，与
    ``BASELINE-RED`` **语义不同**（前者根本没跑完，后者是在干净源码上失败），但同样
    让 matrix FAIL 且 mutation 阶段不启动。
    """
    targets = _targets_for(selected, extra)
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
    含任何"是否跑 baseline"的分支 —— 那正是假证据缺口：默认路径允许跳过 baseline，
    于是一个本来就红的目标会让它的所有 mutation 被记成 CAUGHT。

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
        #: 变异体必须真的落盘：写失败 / 锚点漂移却继续跑测试，会把一次空转记成 CAUGHT。
        with open(path, encoding="utf-8") as handle:
            on_disk = handle.read()
        _require(MUTANT_MARKER in on_disk,
                 f'{mutation["id"]}: mutant marker missing on disk')
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
    理由：操作者请求 targeted mutation 时，实际覆盖范围绝不能因为 CLI 拼写问题被静默放大。

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
                "(for example: --only M-NEWS-01,M-NEWS-03)"
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

    **不做静默忽略**：``--onl M-NEWS-01`` / ``--dry-run`` / ``foo`` 都不是"没有 selector"，
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
    #: 覆盖范围必须显式回显 —— 参数谜题的代价正是"以为只跑了 1 条，其实跑了 14 条"。
    print(f'selected: {len(selected)}/{len(MUTATIONS)} mutation(s): '
          f'{[m["id"] for m in selected]}', flush=True)

    self_test_sequence()
    print("runner self-test: PASS (unique, increasing pycache sequence)")
    self_test_semantics()
    print("semantics self-test: PASS (anchor uniqueness / BASELINE-RED / BASELINE-TIMEOUT / "
          "CAUGHT / SURVIVED / FAKE / TIMEOUT / restore / --only)")
    self_test_optimization()
    print("optimization self-test: PASS (hard guards survive python -O)")

    #: 启动快照：整张矩阵的还原基准。任何一条 mutation 的 finally 都以它为终点，
    #: 矩阵结束后再整体复验一次（"每条都还原了"与"文件最后真的是原样"是两件事）。
    snapshots: dict[str, bytes] = {}
    for name in MUTATED_FILES:
        with open(os.path.join(ROOT, name), "rb") as handle:
            snapshots[name] = handle.read()
        print(f"startup snapshot: {name} sha256={sha256(snapshots[name])}", flush=True)

    if run_baselines(selected, extra=BASELINE_ONLY_TARGETS) != 0:
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

    restore_ok = True
    for name, blob in snapshots.items():
        try:
            _verify_untouched(os.path.join(ROOT, name), blob, sha256(blob))
        except RuntimeError as exc:
            print(f"restore: FAIL —— {exc}", flush=True)
            restore_ok = False

    print(f"R27-B2C-5 mutation matrix: baseline=GREEN; "
          f"{detected}/{len(results)} DETECTED; survived={survived}; fake={fake}; "
          f"timeout={timeout}")
    gate_pass = not bad and restore_ok
    print("gate: baseline=GREEN, survived=0, fake=0, timeout=0, "
          f"restore sha256={'PASS' if restore_ok else 'FAIL'} -> "
          f"{'PASS' if gate_pass else 'FAIL'}")
    return 0 if gate_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
