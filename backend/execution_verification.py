# -*- coding: utf-8 -*-
"""消费层**执行验证闸门**（live execution verification gate）。

本模块把 PR150 的 execution reality contract（:mod:`execution_evidence` /
:mod:`execution_lifecycle` / :mod:`execution_outcome`）接入 paper trading 主链。
它**只做消费层 wiring**：不重新定义成交判定、不复制生命周期状态机、不重算收益口径。
所有"这是不是一次成交"的结论一律委托 :func:`execution_evidence.fill_verdict`。

──────────────────────── 为什么需要这一层 ────────────────────────

``paper_orders.status = 'filled'`` 是**账本自称**的成交，不是**证据证明**的成交。
历史上两者被当成同一件事，于是：

* 一条 ``status='filled'`` 但没有任何 ``paper_fills`` 流水的行，
  会被计入已实现盈亏、成交统计与执行绩效；
* 升级前的旧订单没有成交流水，却被默认当作真实成交继续参与统计。

这就是"执行幻觉"：选股口径的收益被当成真实成交收益来读。本闸门把两者拆开：

=================================  ==========================================
关注点                              判定依据
=================================  ==========================================
``selection_executable``            PR149 选股契约（本层**不回写**）
``execution_status`` / ``verified`` 本层：**真实成交流水证据**
=================================  ==========================================

``selection_executable=True`` 而 ``execution_verified=False`` 是**合法且常见**的：
策略选中了、也判得可执行，但真实账本里没有成交证据。此时：

* **禁止**计入已实现执行收益 / 真实成交统计 / 执行绩效；
* **保留** selection success 与 market counterfactual（它们本来就不是成交）。

──────────────────────── 四态（机器只读 code） ────────────────────────

``verified``
    成交流水证据证明**完整成交**（数量=目标量，且价格/时段可信）。
    这是 ``execution_verified = True`` 的**唯一**来源。
``partial``
    有正成交但**不完整**（数量少于目标量，或数量对得上而价格/时段证据不足）。
    部分成交**绝不**提升为完整成交。
``not_executed``
    有**肯定性**证据表明没有成交（被拒 / 撤单 / 过期 / 从未提交）。
    这是"确认的零"，不是"不知道"。
``unknown``
    证据不足或自相矛盾（缺流水、仍在途、状态无法识别、旧行无证据）。
    **fail closed**：不得当作成交，也不得当作没成交。

──────────────────────── 旧数据（Phase 4 历史兼容） ────────────────────────

升级前的 ``paper_orders`` 行没有成交流水。它们一律是 ``unknown``，
``execution_verified`` 为假，``execution_evidence_source`` 记明原因。
**禁止**把 ``status='filled'`` 自动升级成 ``verified`` —— 那正是本层要消灭的幻觉。
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

try:  # ``backend`` on sys.path（生产与 ``cd backend`` 测试）
    import execution_evidence as EE
except ImportError:  # pragma: no cover - package-style import
    from . import execution_evidence as EE


EXECUTION_VERIFICATION_VERSION = "execution-verification-v1"

#: 闸门四态。机器只读这些字面量。
EXECUTION_STATUS_VERIFIED = "verified"
EXECUTION_STATUS_PARTIAL = "partial"
EXECUTION_STATUS_UNKNOWN = "unknown"
EXECUTION_STATUS_NOT_EXECUTED = "not_executed"
EXECUTION_STATUSES = (
    EXECUTION_STATUS_VERIFIED,
    EXECUTION_STATUS_PARTIAL,
    EXECUTION_STATUS_UNKNOWN,
    EXECUTION_STATUS_NOT_EXECUTED,
)

#: 证据来源（机器可读）：审计要能区分"证据证明过"与"没有证据"。
EVIDENCE_SOURCE_LEDGER = "paper_orders+paper_fills"
EVIDENCE_SOURCE_LEGACY = "legacy_row_without_fill_evidence"
EVIDENCE_SOURCE_ABSENT = "no_evidence_available"
EVIDENCE_SOURCE_INCONSISTENT = "evidence_inconsistent"
EVIDENCE_SOURCES = (
    EVIDENCE_SOURCE_LEDGER,
    EVIDENCE_SOURCE_LEGACY,
    EVIDENCE_SOURCE_ABSENT,
    EVIDENCE_SOURCE_INCONSISTENT,
)

#: ``execution_evidence.fill_verdict`` → 本层四态的**唯一**映射。
#:
#: 这是一张**穷尽**表：每个 verdict 都有归宿，且映射到 ``verified`` 的只有
#: ``fill_verified`` 一个。``fill_pending``（在途）映射到 ``unknown`` 而不是
#: ``partial``：在途既不是"成交了一部分"，也不是"确认没成交"，如实报未知。
VERDICT_TO_STATUS = {
    EE.FILL_VERDICT_VERIFIED: EXECUTION_STATUS_VERIFIED,
    EE.FILL_VERDICT_PARTIAL: EXECUTION_STATUS_PARTIAL,
    EE.FILL_VERDICT_PENDING: EXECUTION_STATUS_UNKNOWN,
    EE.FILL_VERDICT_NONE_CONFIRMED: EXECUTION_STATUS_NOT_EXECUTED,
    EE.FILL_VERDICT_NOT_ATTEMPTED: EXECUTION_STATUS_NOT_EXECUTED,
    EE.FILL_VERDICT_UNKNOWN: EXECUTION_STATUS_UNKNOWN,
}

#: 旧账本行的默认落点：**未知**，绝不因为 ``status='filled'`` 就升级。
LEGACY_EXECUTION_STATUS = EXECUTION_STATUS_UNKNOWN

#: 闸门的**唯一** SQL 谓词。读路径必须引用它，不得各写一份。
#:
#: 同时要求 ``execution_verified = 1`` **与** ``execution_status = 'verified'``：
#: 两列不一致的行（例如被手工改过、或写入时状态没同步）**fail closed**。
#: 旧行的两列都是 NULL → ``COALESCE`` 取 0 → 被排除，这正是 Phase 4 的要求。
VERIFIED_PREDICATE = (
    "(COALESCE(execution_verified, 0) = 1 AND execution_status = 'verified')"
)
#: 未验证谓词（统计"被闸门拦下多少"用，不是用来放行的）。
UNVERIFIED_PREDICATE = "NOT " + VERIFIED_PREDICATE

#: 谓词里要求的两列。**任何**模块都不得自己拼这两列的字面量条件 ——
#: 需要 SQL 就用 :data:`VERIFIED_PREDICATE`，需要判断一行用
#: :func:`is_verified_row`。架构测试会拒绝重复实现。
VERIFIED_PREDICATE_COLUMNS = ("execution_verified", "execution_status")
#: 架构测试据此识别"有人在 SQL 里手写了一份谓词"。
VERIFIED_PREDICATE_SIGNATURE = "execution_status = 'verified'"


# ────────────────── 正成交谓词：部分成交也算"发生过" ──────────────────
#
# ``VERIFIED_PREDICATE`` 回答的是"**整张订单**是否被证明完整成交"，
# `execution_verified = 1` 只属于 ``verified``，**部分成交按设计为假**。
#
# 但"这个一次性动作是否已经真正执行过"是**另一个问题**：目标卖 1000 股、
# 实际只成交 300 股，那 300 股是已经发生的事实。若用整单谓词去回答它，
# 部分成交就会被读成"没发生过"，于是风险扫描每轮再生成一张同样的减仓单，
# 300+300+300… 迅速突破配置的减仓比例。
#
# 因此这里给出**唯一**的"存在经过验证的正成交"谓词与函数：
# ``verified`` 或 ``partial`` 两态都意味着"有正成交量"、且结论来自账本证据，
# 而不是订单自称。``not_executed`` / ``unknown`` 一律排除 —— 前者是"确认的零"，
# 后者是"证据不足"，都不能算动作已执行。
#: 某笔委托是否**有经过验证的正成交**（完整成交或部分成交皆可）。
#:
#: 两态各自要求**两列一致**，与 :data:`VERIFIED_PREDICATE` 同样 fail closed：
#:
#: * ``verified``：``execution_verified = 1`` **且** ``execution_status = 'verified'``；
#: * ``partial``：``execution_verified = 0`` **且** ``execution_status = 'partial'``
#:   （部分成交按设计 ``execution_verified`` 就是 0，它只在完整成交时为 1）。
#:
#: 于是"两列被改得不一致"（例如手工把 flag 置 0 却留着 status='verified'）
#: 会同时落空两态，被当作**没有正成交证据** —— 而不是被宽泛的 ``IN`` 收进来。
POSITIVE_EXECUTION_PREDICATE = (
    "((COALESCE(execution_verified, 0) = 1 AND execution_status = 'verified')"
    " OR (execution_verified = 0 AND execution_status = 'partial'))"
)

#: 可能**携带成交流水**的委托生命周期状态。
#:
#: 这是"要不要去读这张订单的流水"的选取条件，与"这些流水是否构成成交证据"是
#: 两件事，必须分开：
#:
#: * 选取（本常量）：只要订单可能已经有成交，就必须把它的流水读进来，
#:   让验证层去判 —— 否则一条 ``status='filled'`` 却没有证据的旧行会被查询
#:   直接漏掉，"证据不足"就变成了"没有这笔委托"，读路径静默放行（fail open）。
#: * 证据（:data:`POSITIVE_EXECUTION_PREDICATE`）：读进来之后由它决定这些
#:   流水算不算正成交。
#:
#: 因此 ``cancelled`` / ``expired`` / ``superseded`` / ``risk_rejected`` 也在集合里：
#: 撤销或终止的委托**保留**已经发生的部分成交，剩余量不再执行。
FILL_CARRYING_ORDER_STATUSES = (
    "filled", "partially_filled",
    "cancelled", "expired", "superseded", "risk_rejected",
)
#: 选取谓词：该委托是否可能携带成交流水。读路径用它取代 ``status='filled'``。
FILL_CARRYING_PREDICATE = (
    "(status IN ('" + "','".join(FILL_CARRYING_ORDER_STATUSES) + "'))"
)


def status_from_verdict(verdict: Any) -> str:
    """把 PR150 的 ``fill_verdict`` 映射成本层四态。

    未知 verdict（契约之外的字符串）→ ``unknown``：不认识的结论不能当成成交。
    """
    return VERDICT_TO_STATUS.get(str(verdict or ""), EXECUTION_STATUS_UNKNOWN)


def is_verified_status(status: Any) -> bool:
    """只有 ``verified`` 才是 ``execution_verified = True``。"""
    return str(status or "") == EXECUTION_STATUS_VERIFIED


def _row_field(row: Any, name: str) -> Any:
    """从 dict / sqlite3.Row / 普通对象里取一个字段；取不到给 ``None``。"""
    if isinstance(row, Mapping):
        return row.get(name)
    keys = getattr(row, "keys", None)
    if callable(keys):
        try:
            if name in row.keys():
                return row[name]
        except Exception:  # pragma: no cover - 防御未来行类型
            return None
        return None
    return getattr(row, name, None)


def _verified_flag_value(value: Any) -> bool:
    """``execution_verified`` 是否**就是**数值 1（与 SQL 的 ``= 1`` 等价）。

    必须与 SQLite 的 ``COALESCE(execution_verified, 0) = 1`` 给出同一个答案，
    因此这里**不做任何数值转换**：

    * ``bool`` 先判（``bool`` 是 ``int`` 的子类）：``True`` 在 SQLite 里存成整数
      1，所以 ``True → True``；``False → False``；
    * ``int`` / ``float``：**精确等于 1** 才算真（``1`` 与 ``1.0`` → ``True``；
      ``1.1``、``1.9``、``2`` → ``False``）。绝不 ``int(value)`` —— 那会把
      ``1.1`` 截断成 ``1``，让 Python 判成已验证而 SQL 拒绝它；
    * ``str`` / ``bytes``：**一律 False**。SQLite 里是 TEXT / BLOB 存储类，
      与数值 1 比较恒为假。特别地不许把 ``"1"`` 或 ``b"1"`` 当成交：
      在生产列（INTEGER affinity）上写入 ``"1"`` 时 SQLite 会把它**存成整数 1**，
      读回来也就是整数 1，所以到这里根本不会出现字符串 —— 一旦出现，说明该值
      不是走正常写入路径塞进来的，必须 fail closed；
    * ``None`` 与其它类型 → ``False``。
    """
    if isinstance(value, bool):
        return value is True
    if isinstance(value, int):
        return value == 1
    if isinstance(value, float):
        return value == 1.0
    return False


def _verified_status_value(value: Any) -> bool:
    """``execution_status`` 是否**就是**那个字符串 ``'verified'``。

    只接受 ``str`` 且精确相等。SQLite 里 TEXT ``'verified'`` 才与 ``= 'verified'``
    相等；BLOB、数值、``'VERIFIED'``（SQLite 的 ``=`` 对字符串默认大小写敏感）
    一律为假。``is_verified_status``（宽松、给 verdict 用）刻意保持独立，
    因为它处理的是内部 verdict 常量而不是账本列值。
    """
    return isinstance(value, str) and value == EXECUTION_STATUS_VERIFIED


def is_verified_row(row: Any) -> bool:
    """Python 侧的 :data:`VERIFIED_PREDICATE`：一行账本是否被证明成交。

    SQL 读路径必须用 :data:`VERIFIED_PREDICATE`；只有在行的列已经在手
    （归档快照、已读出的 dict / sqlite3.Row）时才用本函数，避免再下发一次查询。

    **与 SQL 谓词逐值等价**：对同一行，本函数与
    ``COALESCE(execution_verified, 0) = 1 AND execution_status = 'verified'``
    必须选中完全相同的行集合。两列不一致、缺列、``NULL``、类型不符一律
    ``False``（fail closed）—— 旧行与归档快照里没有这两列，因此**不会**因为
    ``status='filled'`` 就被当成成交。

    为什么不能用 ``int(flag)``：SQLite 允许没有 ``CHECK`` 约束的列存任意
    存储类的值。``int("1")``、``int(b"1")``、``int(1.1)`` 都会成功并得到 1，
    于是 Python 判成已验证、SQL 拒绝该行 —— 同一笔成交在 SQL 路径与 Python
    路径上结论不同。本函数因此只做**精确比较**，不做任何转换。

    支持的输入形状（与 :func:`_row_field` 一致）：``dict`` / 其它 ``Mapping`` /
    ``sqlite3.Row``（有 ``keys()`` 且支持下标）/ 普通对象（属性）。列值按 SQLite
    原生存储类处理：``int`` / ``float`` / ``bool`` / ``str`` / ``bytes`` / ``None``。
    不引入 numpy / pandas 依赖；这类标量（含 ``numpy.bool_`` 等非原生类型）不是
    本契约接受的存储表示，一律 fail closed 为 ``False``。
    """
    if row is None:
        return False
    flag = _row_field(row, "execution_verified")
    status = _row_field(row, "execution_status")
    if flag is None or status is None:
        return False
    return _verified_flag_value(flag) and _verified_status_value(status)


def is_positive_execution_row(row: Any) -> bool:
    """Python 侧的 :data:`POSITIVE_EXECUTION_PREDICATE`：该行是否有**正成交**。

    与 :func:`is_verified_row` 的关系是**包含**关系，不是替代：

    * 完整成交（``execution_verified = 1`` 且 ``execution_status = 'verified'``）⇒ True；
    * 部分成交（``execution_verified = 0`` 且 ``execution_status = 'partial'``）⇒ True；
    * ``not_executed`` / ``unknown`` / 缺列 / NULL / 两列不一致 ⇒ False。

    这个区分是 R26 的核心之一："**整张订单**是否完整成交"与"**这笔委托**是否已经
    真实成交过一部分"是两个不同的问题。用前者回答后者，会让部分成交被读成"没发生"。

    与 SQL 谓词逐值等价：只做精确比较（``_verified_flag_value`` /
    exact ``str``），不做 ``int()`` 之类的宽容转换，两列必须**同时**成立。
    """
    status = _row_field(row, "execution_status")
    if not isinstance(status, str):
        return False
    if status == EXECUTION_STATUS_VERIFIED:
        return _verified_flag_value(_row_field(row, "execution_verified"))
    if status == EXECUTION_STATUS_PARTIAL:
        flag = _row_field(row, "execution_verified")
        # 部分成交的 flag 必须是精确的 0（SQLite 存成整数 0）。
        return flag is not None and not isinstance(flag, (str, bytes)) and flag == 0
    return False


def verification_from_evidence(evidence: Any, *, fill_rows_present: bool = True) -> dict:
    """从一条 :class:`execution_evidence.ExecutionEvidence` 得出验证结论。

    返回 ``{"execution_status", "execution_verified", "execution_evidence_source"}``。
    没有证据（``None``）→ ``unknown``，来源 ``no_evidence_available``。

    ``execution_evidence_source`` 说明**为什么**结论是这样，优先级（先到先判）：

    1. 证据自相矛盾（例如写着 ``filled`` 却一条流水都没有）→
       ``evidence_inconsistent``：这是审计要看的完整性问题；
    2. 完全没有成交流水（``fill_rows_present=False``）→
       ``legacy_row_without_fill_evidence``：升级前的旧行，本来就没有证据；
    3. 其余 → ``paper_orders+paper_fills``：证据齐备，结论来自逐条核对。

    第 1 条优先于第 2 条：一条 ``status='filled'`` 的旧行**同时**是"旧数据"与
    "自称与证据矛盾"，后者更具体、更可操作，所以报它。
    """
    if evidence is None:
        return {
            "execution_status": EXECUTION_STATUS_UNKNOWN,
            "execution_verified": False,
            "execution_evidence_source": EVIDENCE_SOURCE_ABSENT,
        }
    verdict = evidence.fill_verdict_value()
    status = status_from_verdict(verdict)
    try:
        inconsistent = bool(evidence.inconsistencies())
    except Exception:  # pragma: no cover - 契约自身不会抛；防御未来改动
        inconsistent = True
    if inconsistent:
        source = EVIDENCE_SOURCE_INCONSISTENT
    elif not fill_rows_present:
        source = EVIDENCE_SOURCE_LEGACY
    else:
        source = EVIDENCE_SOURCE_LEDGER
    return {
        "execution_status": status,
        "execution_verified": is_verified_status(status),
        "execution_evidence_source": source,
    }


def legacy_verification(*, has_fill_rows: bool = False) -> dict:
    """升级前旧行的验证结论。

    ``has_fill_rows=False``（没有成交流水）→ ``unknown`` + ``legacy`` 来源。
    **绝不允许**因为 ``status='filled'`` 就返回 ``verified``。

    ``has_fill_rows=True`` 的旧行其实有流水，调用方应走
    :func:`verification_from_evidence`；这里只兜住"连流水都没有"的路径。
    """
    if has_fill_rows:
        # 有流水却走"旧行"路径：调用方应当改用 verification_for_order。
        return {
            "execution_status": EXECUTION_STATUS_UNKNOWN,
            "execution_verified": False,
            "execution_evidence_source": EVIDENCE_SOURCE_ABSENT,
        }
    return {
        "execution_status": LEGACY_EXECUTION_STATUS,
        "execution_verified": False,
        "execution_evidence_source": EVIDENCE_SOURCE_LEGACY,
    }


def verification_for_order(order: Any, fill_rows: Any = None) -> dict:
    """给一行 ``paper_orders``（+ 它的 ``paper_fills``）算出验证结论。

    这是写入路径的**唯一**入口：:func:`execution_planner.commit_fill` 与迁移脚本
    都调用它，因此"什么算成交"只有一处实现。

    **没有成交流水不等于"未知"**：被拒 / 撤单 / 过期且没有流水的订单是**肯定性的
    零**（``not_executed``），而 ``status='filled'`` 却没有流水才是证据缺失
    （``unknown``）。这个区分由 :func:`execution_evidence.evidence_from_order` 按
    生命周期状态决定，所以这里**一律**走它，不按"有没有流水"提前短路 —— 提前
    短路会把一笔明确被拒的订单误报成"不知道"。

    ``fill_rows`` 里的行**应当**带上身份列（``account_id`` / ``side`` / ``code``），
    这样 :func:`execution_evidence.evidence_from_order` 才能核对"这条流水真的
    属于这笔委托"。按 ``order_id`` 关联时不做这层核对，一条串了账户/方向/标的的
    流水就能把委托验证成成交。调用方若确实没有身份列，必须显式声明，而不是让它
    默认通过。
    """
    if order is None:
        return verification_from_evidence(None)
    rows = list(fill_rows or ())
    if not rows:
        return verification_from_evidence(
            EE.evidence_from_order(order, ()), fill_rows_present=False
        )
    has_identity = all(
        any(key in row for key in ("account_id", "side", "code"))
        for row in rows
    )
    if has_identity:
        return verification_from_evidence(
            EE.evidence_from_order(order, rows, fill_identity_rows=rows)
        )
    # 没有身份列可核对 → 不声称核对过（fail closed，见 PR151）。
    return verification_from_evidence(
        EE.evidence_from_order(order, rows, fill_identity_known=False)
    )


def _row_as_dict(cursor: Any, row: Any) -> dict:
    """把一行转成 dict，**对行形状自适应**。

    三种行对象都会出现，且都合法：

    - ``dict``（测试替身、手工构造的行）→ 直接用；
    - ``sqlite3.Row``（生产读路径设了 ``row_factory``）→ 有 ``keys()``，``dict(row)`` 可用；
    - ``tuple``（``db_migrate`` 的裸连接）→ 只能借 ``cursor.description`` 配列名。

    之前这里写死了第三条路径，于是任何"行已经是 dict"的调用方（例如 planner 的
    测试替身）都会撞 ``AttributeError: 'X' object has no attribute 'description'``。
    行形状是调用方的事实，不是本模块可以假定的前提。
    """
    if hasattr(row, "keys"):
        return dict(row)
    columns = [item[0] for item in (cursor.description or ())]
    return dict(zip(columns, row, strict=True))


def _dict_rows(cursor: Any) -> list:
    """把游标结果转成 dict 行，**不依赖** ``row_factory``。

    ``db_migrate`` 用的是裸 ``sqlite3.connect()``，行对象是 tuple：对它做
    ``row["order_id"]`` 会抛 ``TypeError``，``dict(row)`` 同样失败。生产读路径的
    连接都设了 ``row_factory = sqlite3.Row``，所以这类缺陷只在迁移/运维入口上暴露
    —— 恰恰是 v12 回填必须跑通的地方。列名取自 ``cursor.description``，因此对
    ``SELECT *`` 与本模块自己的显式列清单都成立。
    """
    return [_row_as_dict(cursor, row) for row in cursor.fetchall()]


def _load_fills_with_identity(conn, order_id: Any) -> list:
    """读出某笔委托的成交流水，**含**身份列（供身份核对）。"""
    return _dict_rows(conn.execute(
        "SELECT order_id,account_id,side,code,qty,price,amount,fees,fill_date,quote_at"
        " FROM paper_fills WHERE order_id=? ORDER BY id",
        (order_id,),
    ))


#: 批量读流水时每批的订单数上限（SQLite 变量数量上限是 999）。
_FILL_FETCH_CHUNK = 400


def _fills_for_orders(conn, order_ids: Any) -> dict:
    """一次批量读出多笔委托的流水，避免逐行回填把启动拖成 O(n) 次查询。"""
    grouped: dict = {}
    ids = [int(value) for value in order_ids]
    for start in range(0, len(ids), _FILL_FETCH_CHUNK):
        chunk = ids[start:start + _FILL_FETCH_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        try:
            rows = _dict_rows(conn.execute(
                "SELECT order_id,account_id,side,code,qty,price,amount,fees,fill_date,quote_at"
                f" FROM paper_fills WHERE order_id IN ({placeholders}) ORDER BY id",
                tuple(chunk),
            ))
        except sqlite3.OperationalError:
            # 流水表还不存在（新库/最小 fixture 上跑迁移）→ 没有任何成交证据。
            return grouped
        for row in rows:
            grouped.setdefault(int(row["order_id"]), []).append(row)
    return grouped


#: marker 读写允许的列 / JSON key 白名单（两者都会被拼进 SQL）。
_MARKER_COLUMNS = frozenset({"risk_payload"})
_MARKER_KEYS = frozenset({"exit_marker"})


def has_verified_positive_execution(
    conn,
    *,
    marker: str,
    account_id: Any,
    code: Any,
    asof_day: Any,
    legacy_reason_like: str | None = None,
    column: str = "risk_payload",
    marker_key: str = "exit_marker",
) -> bool:
    """某个一次性风险动作**是否已经真正执行过**（有经过验证的正成交）。

    这是"这张减仓动作还要不要重发"的**唯一**判据，供风险扫描去重使用。它问的不是
    "整张订单是否完全成交"（那会让部分成交被读成没发生），而是"是否存在一笔经过
    证据验证、且带该 marker 的**正成交**委托"。

    语义边界（必须与"订单剩余量继续执行"分开）：

    * 命中 → 该 one-shot 动作**已经消费**，风险扫描**不再**新建第二张同样的单；
    * 未命中 → 动作尚未发生，允许发起；
    * 至于那张已存在、可能部分成交的订单自身的剩余股份，仍由 Execution Authority
      在后续行情事件里继续执行 —— 这里**不**、也**不应**阻止它，两者是不同问题。

    因此本函数只回答去重，不返回任何"剩余量"或"是否可继续成交"的信息。

    ``legacy_reason_like`` 是**标记上线前**旧订单的兜底：那些行没有 ``exit_marker``，
    只能靠中文 reason 文本识别。它刻意做成显式参数（而不是在 SQL 里悄悄多一个
    ``OR``），让"这里在为历史数据破例"在读代码时无法被忽略；新写入的行一律走
    结构化 marker。

    ``column`` / ``marker_key`` 会被拼进 SQL，因此做白名单校验。
    """
    if column not in _MARKER_COLUMNS:
        raise ValueError(f"unsupported marker column: {column!r}")
    if marker_key not in _MARKER_KEYS:
        raise ValueError(f"unsupported marker key: {marker_key!r}")
    if not marker:
        return False
    day = str(asof_day or "")[:10]
    if not day:
        return False
    matcher = f"json_extract({column},'$.{marker_key}')=?"
    params: list = [str(account_id), str(code), day, str(marker)]
    if legacy_reason_like:
        matcher = f"({matcher} OR reason LIKE ?)"
        params.append(str(legacy_reason_like))
    sql = (
        "SELECT 1 FROM paper_orders "
        "WHERE account_id=? AND code=? AND side='sell' "
        f"  AND {POSITIVE_EXECUTION_PREDICATE} "
        "  AND substr(created_at,1,10)=? "
        f"  AND {matcher} "
        "LIMIT 1"
    )
    try:
        row = conn.execute(sql, tuple(params)).fetchone()
    except sqlite3.Error:
        # 账本读不出结论时**不得**放行新动作：宁可暂缓一次减仓，也不能因为查询
        # 失败而重复减仓（重复减仓会真正改变持仓，代价不可逆）。
        return True
    return row is not None


def stamp_order(conn, order_id: Any) -> dict:
    """把验证结论写回 ``paper_orders`` 的三列。返回写入的结论。

    写路径专用。读路径**不得**调用它（读面板写库会与 3 分钟 worker 抢锁）。
    """
    cursor = conn.execute("SELECT * FROM paper_orders WHERE id=?", (order_id,))
    row = cursor.fetchone()
    if row is None:
        return verification_from_evidence(None)
    verdict = verification_for_order(
        _row_as_dict(cursor, row), _load_fills_with_identity(conn, order_id)
    )
    conn.execute(
        "UPDATE paper_orders SET execution_status=?, execution_verified=?,"
        " execution_evidence_source=? WHERE id=?",
        (
            verdict["execution_status"],
            int(bool(verdict["execution_verified"])),
            verdict["execution_evidence_source"],
            order_id,
        ),
    )
    return verdict


def backfill_legacy_orders(conn, *, limit: Any = None) -> dict:
    """给尚未盖章的行补上结论（幂等，批量）。

    Phase 4：旧行的结论**由证据决定**，不按 ``status`` 猜：

    * 没有成交流水（或流水不足以证明满额成交）→ ``unknown``，
      **不**自动升级为 ``verified``；
    * 有**完整**成交流水证据（数量对得上、价格与时段可信）→ ``verified``。
      这正是升级前由生产写路径落库、却因为没有盖章而从统计里消失的**真实成交**，
      回填把它们的结论一次算清，避免"上线即把历史真实成交当成没发生"。

    只处理 ``execution_status IS NULL`` 的行，已经盖过章的行永不重写（幂等）。
    读路径**不得**调用它（写库会与 3 分钟 worker 抢锁）；它只属于迁移与
    显式的运维入口。
    """
    sql = "SELECT * FROM paper_orders WHERE execution_status IS NULL"
    params: tuple = ()
    if limit:
        sql += " LIMIT ?"
        params = (int(limit),)
    try:
        orders = _dict_rows(conn.execute(sql, params))
    except sqlite3.OperationalError:
        # 迁移会在"表还不存在"的库上运行（新建库 / 最小 fixture）：
        # 没有订单可回填，按"零行"返回，绝不因此让迁移失败。
        return {"stamped": 0, "verified": 0, "scanned": 0}
    if not orders:
        return {"stamped": 0, "verified": 0, "scanned": 0}
    fills_by_order = _fills_for_orders(conn, [row["id"] for row in orders])
    updates = []
    verified = 0
    for row in orders:
        verdict = verification_for_order(
            dict(row), fills_by_order.get(int(row["id"]), [])
        )
        is_verified = bool(verdict["execution_verified"])
        verified += int(is_verified)
        updates.append((
            verdict["execution_status"],
            int(is_verified),
            verdict["execution_evidence_source"],
            row["id"],
        ))
    conn.executemany(
        "UPDATE paper_orders SET execution_status=?, execution_verified=?,"
        " execution_evidence_source=? WHERE id=?",
        updates,
    )
    return {"stamped": len(updates), "verified": verified, "scanned": len(orders)}


def gate_report(rows: Any) -> dict:
    """按四态统计一批订单行；用于审计"闸门拦下了多少"。

    每条输入都落在某个状态里，不静默丢行。
    """
    counts = {status: 0 for status in EXECUTION_STATUSES}
    blocked = 0
    total = 0
    for row in rows or ():
        total += 1
        status = str(
            (row.get("execution_status") if isinstance(row, Mapping)
             else getattr(row, "execution_status", None)) or ""
        )
        if status not in counts:
            status = EXECUTION_STATUS_UNKNOWN
        counts[status] += 1
        if status != EXECUTION_STATUS_VERIFIED:
            blocked += 1
    return {
        "version": EXECUTION_VERIFICATION_VERSION,
        "total": total,
        "counts": counts,
        "blocked_from_execution_stats": blocked,
        "verified": counts[EXECUTION_STATUS_VERIFIED],
    }


# ─────────────────── owner fact contract（R27-B2C-1）───────────────────
#
# 这一段是 execution owner **正式发布**的 fact contract：它回答"这条 execution 事实的
# 身份是什么、业务日是什么、观测时点是什么、owner 自己怎么核验它"。
#
# 三条刻意的边界：
#
# * **不回答 research 语义**（status / reason / confidence / 支持什么 thesis）—— 那些属于
#   research 契约，不属于 owner。
# * **不使用 market 的核验词表**。execution 有自己的四态与证据来源；把 market 的
#   ``verified / single_source / cross_source / coverage_integrity`` 抄过来，会让
#   "行情双源核验"与"账本证明成交"变成同一句话。research 层将来必须消费
#   **owner-native** 核验，而不是让所有 owner 伪装成 market_data。
# * **不推断业务日**。owner 记录了什么就报什么；没有记录就如实报 unknown，
#   绝不用 ``created_at`` 的墙钟日期冒充交易日。

EXECUTION_FACT_CONTRACT_VERSION = "execution-fact-v1"

#: 本 owner 的核验**语义范围**。research 层必须能区分"这笔委托是否被账本证据证明成交"
#: 与"行情是否可信" —— 两者是不同的维度，因此范围是契约的一部分。
EXECUTION_VERIFICATION_SCOPE = "execution_order_fill_evidence"

#: 状态 × 证据来源的**合法组合**（穷尽表，与 :data:`VERDICT_TO_STATUS` 同一风格）。
#: 不在表里的组合 fail closed：``evidence_inconsistent`` 只是一个来源标签，
#: 不代表"任何状态都可以配它"。
_LEGAL_SOURCES_BY_STATUS = {
    EXECUTION_STATUS_VERIFIED: (EVIDENCE_SOURCE_LEDGER, EVIDENCE_SOURCE_INCONSISTENT),
    EXECUTION_STATUS_PARTIAL: (EVIDENCE_SOURCE_LEDGER, EVIDENCE_SOURCE_INCONSISTENT),
    EXECUTION_STATUS_NOT_EXECUTED: (
        EVIDENCE_SOURCE_LEDGER, EVIDENCE_SOURCE_LEGACY, EVIDENCE_SOURCE_INCONSISTENT,
    ),
    EXECUTION_STATUS_UNKNOWN: (
        EVIDENCE_SOURCE_LEDGER, EVIDENCE_SOURCE_LEGACY, EVIDENCE_SOURCE_INCONSISTENT,
        EVIDENCE_SOURCE_ABSENT,
    ),
}

#: ``ExecutionFactProjection`` 发布的 owner-native execution **事实字段**，顺序固定。
#:
#: 这是 B2C-4A 关闭的缺口：B2C-1 的投影只有 identity / 生命周期 / verdict / PIT / 核验，
#: 因此 B2C-3 能证明"发生了 partial / verified / not_executed"，却拿不出
#: ``pnl_attribution`` 需要的成交数量、成交价格、费用、方向与标的。
#:
#: 六个字段**直接**取自 :class:`execution_evidence.ExecutionEvidence` 的同名
#: :class:`execution_evidence.EvidenceField`，不重算、不从兼容列补值，也**不**压成裸值 ——
#: ``known`` / ``unknown`` / ``not_applicable`` 必须继续保有区别（``known(0)`` 不等于
#: ``unknown``，也不等于"没成交"）。
#:
#: 刻意**不**包含 ``realized_pnl`` / ``NAV`` / ``position_cost`` / ``daily_return``：
#: 那些依赖成本基准与组合记账，不是 execution owner 的事实（属于 B2C-4B 的
#: portfolio/accounting owner）。硬塞进来等于让组合事实冒充执行事实。
EXECUTION_FACTUAL_FIELDS = (
    "code", "action", "requested_qty", "filled_qty", "fill_price", "fees",
)

#: identity 的来源。审计必须看得见"这条身份是怎么来的"，否则一个字符串无法复核。
IDENTITY_KIND_FILL_EVENT_KEY = "fill_event_key"
IDENTITY_KIND_FILL_EVENT_KEY_SET = "fill_event_key_set"
IDENTITY_KIND_ORDER_ONLY = "order_id_only"
#: 部分成交行缺 ``event_key``（两个身份列都可空，旧行没有 event_key）。此时**不**能
#: 用"存在的那几个 key"代表整笔成交的身份：那会把一条混合了新旧的成交说成一个完整的
#: 逐次身份。身份退成 ``order:<id>``，并由本 kind 明确说明原因。
IDENTITY_KIND_INCOMPLETE_EVENT_KEYS = "fill_event_key_incomplete"
IDENTITY_KINDS = (
    IDENTITY_KIND_FILL_EVENT_KEY, IDENTITY_KIND_FILL_EVENT_KEY_SET,
    IDENTITY_KIND_ORDER_ONLY, IDENTITY_KIND_INCOMPLETE_EVENT_KEYS,
)


class ExecutionFactContractError(ValueError):
    """owner fact contract 的构造被拒绝 —— fail closed。

    ``reason`` 是稳定 machine code，供调用方与测试依赖；文案本身不承载判定：

    * ``not_execution_evidence`` —— 入口收到非 typed ``ExecutionEvidence``（含子类）；
    * ``unknown_verification_status`` —— 状态不在 owner 的四态闭集里；
    * ``unknown_evidence_source`` —— 证据来源不在 owner 的闭集里；
    * ``illegal_verification_pair`` —— 状态与来源的组合不在合法组合表里；
    * ``alien_verification_scope`` —— 核验声明不是本 owner 的核验范围；
    * ``non_canonical_verification`` —— 声明与 ``verification_contract`` 的产出不精确相等
      （伪造的 version / 相反的 is_verified / 额外字段）；
    * ``identity_unavailable`` —— 既没有完整成交身份，也没有可用的 order id；
    * ``unknown_identity_kind`` / ``alien_identity`` —— identity 形态非法；
    * ``version_mismatch`` —— fact contract 版本不符；
    * ``field_not_an_evidence_field`` —— 三态字段缺失或名不对。
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = str(reason)
        text = self.reason if not detail else "%s: %s" % (self.reason, detail)
        super().__init__(text)


def verification_contract(status: Any, source: Any) -> Mapping:
    """execution owner **正式发布**的一条核验声明（owner-native 词表）。

    返回 owner 自己的维度：``verification_status`` 是四态之一，``verification_source``
    是证据来源之一，``verification_scope`` 说明"这条核验是关于什么的"。它**不是** market
    的 ``(verification, verification_method)`` 对，也刻意不做任何到那套词表的翻译 ——
    翻译就是由非 owner 发明核验结论。

    非法词表或**不可能的组合**一律 :class:`ExecutionFactContractError`，
    而不是被静默降级成"看起来能用"。
    """
    text_status = str(status or "")
    text_source = str(source or "")
    if text_status not in EXECUTION_STATUSES:
        raise ExecutionFactContractError(
            "unknown_verification_status", f"{text_status!r} not in {EXECUTION_STATUSES}",
        )
    if text_source not in EVIDENCE_SOURCES:
        raise ExecutionFactContractError(
            "unknown_evidence_source", f"{text_source!r} not in {EVIDENCE_SOURCES}",
        )
    if text_source not in _LEGAL_SOURCES_BY_STATUS.get(text_status, ()):
        raise ExecutionFactContractError(
            "illegal_verification_pair",
            f"{text_status!r} cannot carry source {text_source!r}",
        )
    # 不可变：投影是 frozen dataclass，但嵌套的 dict 是可变的 —— 拿到一条合法投影的人
    # 可以改掉 ``verification["verification_status"]``，然后 ``as_dict()`` 会用同一个
    # "owner 已发布"的对象发布一个被改过的裁决。这里用只读视图封住它
    # （与 ``ai_research_contract`` 冻结 payload 的手法一致）。
    return MappingProxyType({
        "verification_scope": EXECUTION_VERIFICATION_SCOPE,
        "verification_version": EXECUTION_VERIFICATION_VERSION,
        "verification_status": text_status,
        "verification_source": text_source,
        "is_verified": is_verified_status(text_status),
    })


def _fact_identity(provenance: Any, order_id: Any) -> tuple:
    """owner 派生的 identity，以及它是怎么来的。

    * 有**完整**的逐次成交身份（``event_key``）→ 用它；多个成交 → 按排序后拼接
      （``identity_kind`` 说明这是一条**成交集合**的身份，而不是某一次成交）；
    * 只有部分成交行带 ``event_key`` → 不冒充完整身份，退成 ``order:<id>`` 并标注
      ``fill_event_key_incomplete``；
    * 没有任何成交身份 → ``order:{id}``，并且 ``identity_kind`` 明确写
      ``order_id_only``：这只标识**委托**，不冒充"逐次执行事实身份"。

    **没有可用的 order id 时 fail closed**（``identity_unavailable``）。绝不拼出
    ``order:None`` / ``order:`` 这种"看起来稳定、其实会把多条不同事实撞成同一条"的
    占位身份 —— 那正是本轮 identity contract 要根除的东西。
    """
    data = provenance if isinstance(provenance, Mapping) else {}
    keys = sorted({
        str(item) for item in (data.get("fill_event_keys") or []) if str(item or "").strip()
    })
    usable = _int_or_zero(data.get("fill_rows"))
    recorded = _int_or_zero(data.get("fill_event_key_rows"))
    if usable and recorded < usable:
        return "order:%s" % _required_identity_anchor(order_id), IDENTITY_KIND_INCOMPLETE_EVENT_KEYS
    if len(keys) == 1:
        return keys[0], IDENTITY_KIND_FILL_EVENT_KEY
    if keys:
        return "|".join(keys), IDENTITY_KIND_FILL_EVENT_KEY_SET
    return "order:%s" % _required_identity_anchor(order_id), IDENTITY_KIND_ORDER_ONLY


def _required_identity_anchor(order_id: Any) -> Any:
    """identity 的兜底锚点：必须是**真实存在**的 order id。

    ``ExecutionEvidence.order_id`` 默认允许 ``None``，所以这里不能直接字符串化：
    ``"order:%s" % None`` 是一个非空字符串，会被 ``__post_init__`` 当成合法 identity，
    于是多条没有 order id 的不同事实共享同一个身份。
    """
    if isinstance(order_id, bool) or order_id is None:
        raise ExecutionFactContractError(
            "identity_unavailable",
            "no complete fill event identity and no usable order id",
        )
    if isinstance(order_id, int):
        return order_id
    text = str(order_id).strip()
    if not text:
        raise ExecutionFactContractError(
            "identity_unavailable",
            "no complete fill event identity and no usable order id",
        )
    return text


#: 业务日必须是**可证明的**交易日写法（``YYYY-MM-DD``）。数据库列是 TEXT，契约不能
#: 因为"非空"就把 ``banana`` 发布成 typed PIT 事实。
_BUSINESS_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def _is_business_day(value: Any) -> bool:
    text = str(value or "")
    if not _BUSINESS_DAY_RE.match(text):
        return False
    try:
        dt.date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _is_observed_at(value: Any) -> bool:
    """观测时点必须是**带时区**的可解析时间戳。

    不带时区的时间戳不是一个可证明的瞬时（它取决于解释方的时区），因此不发布为
    ``known``。这一条宁可在 owner contract 里 fail closed，也不要让脏值流到
    research adapter 再被发现。
    """
    text = str(value or "")
    if not text:
        return False
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _single_value(field_name: str, values: Any, *, subject: str,
                  recorded_rows: Any = None, usable_rows: Any = None,
                  validator=None) -> EE.EvidenceField:
    """逐成交的取值聚合成一个 owner 字段。

    只有一个不同取值 → ``known``；多个不同取值 → ``unknown`` 并把集合写进 ``detail``。
    刻意**不**挑一个代表值，也刻意不取 min/max：一次跨业务日的成交，报"某一个业务日"
    就是在编造一个它没有的 PIT 事实。

    ``recorded_rows`` / ``usable_rows`` 是**完整性**判据：两个身份列都可空，所以一次
    混合了新旧的成交可能只有部分流水带时间戳。那种情况下报 ``known`` 等于把"其中一条
    有"说成"这一整笔有" —— 因此只要行数对不上就报 ``unknown``。

    ``validator`` 是**格式**判据：``business_day`` 必须是 ``YYYY-MM-DD``，``observed_at``
    必须是带时区的可解析时间戳。格式不合法同样报 ``unknown``（不是 known、也不是
    not_applicable）—— 契约在 owner 这一侧就拒绝把脏值发布成 typed PIT 事实。
    """
    if recorded_rows is not None and usable_rows is not None:
        recorded, usable = _int_or_zero(recorded_rows), _int_or_zero(usable_rows)
        if recorded < usable:
            return EE.EvidenceField.unknown(
                field_name, source=EXECUTION_VERIFICATION_SCOPE,
                detail="%s is recorded for only %d of %d usable fills"
                       % (subject, recorded, usable),
            )
    distinct = sorted({str(item) for item in (values or ()) if str(item or "").strip()})
    if len(distinct) == 1:
        value = distinct[0]
        if validator is not None and not validator(value):
            return EE.EvidenceField.unknown(
                field_name, source=EXECUTION_VERIFICATION_SCOPE,
                detail="%s is not a provable value: %r" % (subject, value[:40]),
            )
        return EE.EvidenceField.known(
            field_name, value, source=EXECUTION_VERIFICATION_SCOPE,
        )
    if distinct:
        return EE.EvidenceField.unknown(
            field_name, source=EXECUTION_VERIFICATION_SCOPE,
            detail="%s spans several values: %s" % (subject, distinct),
        )
    return EE.EvidenceField.unknown(
        field_name, source=EXECUTION_VERIFICATION_SCOPE,
        detail="no %s recorded by the owner for this fact" % subject,
    )


@dataclass(frozen=True, slots=True, init=False)
class ExecutionFactProjection:
    """execution owner 对**一条 execution fact** 的正式投影。

    研究层将来引用一条 execution 事实时，读到的就是它：身份、业务日、观测时点、
    owner-native 的执行事实字段（方向 / 标的 / 委托数量 / 成交数量 / 成交价格 / 费用），
    以及 owner 自己的核验声明。它**不**包含 research 语义，也**不**携带 market 词表。

    **没有公开 raw 构造器。** ``ExecutionFactProjection(...)`` 一律抛 ``TypeError``；
    唯一签发路径是 :func:`fact_projection`（它再走私有 :func:`_issue_fact_projection`）。
    否则任何调用方都能拿一个自造的 ``identity`` / 业务日字段拼出一个与 owner 签发的对象
    **无法区分**的投影 —— "唯一发布入口"就只是一句声明。这与
    ``ai_research_contract.ResearchEvidenceRef`` 的处理方式一致。

    ``business_day`` 与 ``observed_at`` 刻意都是三态字段：一次被拒/被撤的委托今天
    **没有** owner 记录的业务日（``paper_orders`` 没有交易日列），因此它如实报
    ``unknown`` —— 而不是拿 ``created_at`` 的墙钟日期冒充。这个缺口是 R27-B2C-1 明确
    记录的下一步前置条件，不是被隐藏的"以后再说"。

    :data:`EXECUTION_FACTUAL_FIELDS` 里的六个字段（B2C-4A 起）同样是三态
    :class:`execution_evidence.EvidenceField`，且**逐字**派生自 owner 的
    :class:`execution_evidence.ExecutionEvidence`：投影只发布，不重算成交数量/价格/费用。
    ``as_dict()`` 写的是 ``EvidenceField.as_dict()`` 而不是 ``maybe()`` —— ``maybe()``
    会把 ``unknown`` 与 ``not_applicable`` 一起压成 ``None``，那正是本契约要区分的三态。
    """

    version: str
    identity: str
    identity_kind: str
    order_id: Any
    lifecycle_state: str
    fill_verdict: str
    code: EE.EvidenceField
    action: EE.EvidenceField
    requested_qty: EE.EvidenceField
    filled_qty: EE.EvidenceField
    fill_price: EE.EvidenceField
    fees: EE.EvidenceField
    business_day: EE.EvidenceField
    observed_at: EE.EvidenceField
    verification: Mapping
    inconsistencies: tuple

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError(
            "ExecutionFactProjection has no public constructor: 调用方不能自述一条 "
            "execution 事实的身份 / 业务日 / 核验结论。请使用 fact_projection(evidence) —— "
            "identity、business_day、observed_at 与 verification 全部由 owner 从它自己"
            "记录的证据派生"
        )

    def __post_init__(self) -> None:
        if self.version != EXECUTION_FACT_CONTRACT_VERSION:
            raise ExecutionFactContractError(
                "version_mismatch",
                f"{self.version!r} != {EXECUTION_FACT_CONTRACT_VERSION!r}",
            )
        if self.identity_kind not in IDENTITY_KINDS:
            raise ExecutionFactContractError("unknown_identity_kind", str(self.identity_kind))
        if not str(self.identity or "").strip():
            raise ExecutionFactContractError("alien_identity", "identity must be non-empty")
        for name in EXECUTION_FACTUAL_FIELDS + ("business_day", "observed_at"):
            holder = getattr(self, name)
            if not isinstance(holder, EE.EvidenceField) or holder.name != name:
                raise ExecutionFactContractError(
                    "field_not_an_evidence_field",
                    f"{name} must be an EvidenceField named {name!r}",
                )
        declared = dict(self.verification or {})
        # 核验声明必须是 owner **自己**发布的 canonical 形状。三层判定，每层给一个更具体的
        # 原因（"范围不对"与"版本/布尔位被改过"是不同的事故，不该共用一个 reason）：
        #
        #   1. 范围必须是本 owner 的核验范围；
        #   2. 状态 / 来源必须是 owner 的闭集词，且组合合法；
        #   3. 整份 mapping 与 ``verification_contract(status, source)`` **精确相等** ——
        #      含 scope / version / status / source / is_verified 五个字段，且**不接受额外字段**。
        #
        # 只校验前两层是不够的：那样同一个对象可以同时说 ``status='verified'`` 与
        # ``is_verified=False``，或者带一个伪造的 ``verification_version``，而下游只会看到
        # "这是一条已发布的裁决"。
        if declared.get("verification_scope") != EXECUTION_VERIFICATION_SCOPE:
            raise ExecutionFactContractError(
                "alien_verification_scope",
                f"verification scope is {declared.get('verification_scope')!r}, expected "
                f"{EXECUTION_VERIFICATION_SCOPE!r}",
            )
        canonical = dict(verification_contract(
            declared.get("verification_status"), declared.get("verification_source"),
        ))
        if declared != canonical:
            extra = sorted(set(declared) - set(canonical))
            raise ExecutionFactContractError(
                "non_canonical_verification",
                "verification statement does not match the owner statement"
                + (f"; unexpected fields {extra}" if extra else ""),
            )

    def as_dict(self) -> Mapping:
        return {
            "version": self.version,
            "identity": self.identity,
            "identity_kind": self.identity_kind,
            "order_id": self.order_id,
            "lifecycle_state": self.lifecycle_state,
            "fill_verdict": self.fill_verdict,
            "code": self.code.as_dict(),
            "action": self.action.as_dict(),
            "requested_qty": self.requested_qty.as_dict(),
            "filled_qty": self.filled_qty.as_dict(),
            "fill_price": self.fill_price.as_dict(),
            "fees": self.fees.as_dict(),
            "business_day": self.business_day.as_dict(),
            "observed_at": self.observed_at.as_dict(),
            "verification": dict(self.verification or {}),
            "inconsistencies": list(self.inconsistencies),
        }


def _issue_fact_projection(**fields: Any) -> ExecutionFactProjection:
    """签发一条 owner fact 投影。只有 :func:`fact_projection` 调用它。

    绕过恒抛错的 ``__init__`` 并在设置完全部字段后跑 ``__post_init__``，使校验逻辑仍然
    只有一份、且紧挨字段定义（与 ``ai_research_contract._issue_evidence_ref`` 同一手法）。
    """
    projection = object.__new__(ExecutionFactProjection)
    for name, value in fields.items():
        object.__setattr__(projection, name, value)
    projection.__post_init__()
    return projection


def fact_projection(evidence: Any, *, fill_rows_present: bool = True) -> ExecutionFactProjection:
    """把一个 :class:`execution_evidence.ExecutionEvidence` 投影成 owner fact contract。

    这是"owner 发布事实"的**唯一**入口：身份、业务日、观测时点全部从 owner 自己记录的
    ``provenance`` 派生，核验声明由 :func:`verification_contract` 出，
    :data:`EXECUTION_FACTUAL_FIELDS` 六个事实字段逐字取自 evidence 自己发布的
    :class:`execution_evidence.EvidenceField`。调用方**不能**提供这些值（签名里没有这些
    参数），也无法绕开本函数自造一个投影。

    **入口先做类型校验**：``evidence`` 必须是真正的
    :class:`execution_evidence.ExecutionEvidence`（``type(...) is``，子类也不算）。
    少了这一步，一个普通伪对象只要实现 ``fill_verdict_value()`` / ``inconsistencies()``
    并塞一个看起来合法的 ``provenance``，就能让本函数发布一条 ``verified + ledger`` 的
    "owner projection" —— 那会让"owner contract"这个边界形同虚设。

    **已知限制（不假装已关闭）**：``ExecutionEvidence`` 本身仍是公开可构造的，因此
    "手工造 evidence → fact_projection(...)" 仍是一条两步伪造路径，与
    ``MarketDataReading`` 在 R27-A 的情况相同。本层关闭的是
    *contract-issued execution projection*；*owner-origin provenance* 仍是
    **OPEN / REQUIRED**，由 owner/provenance 架构（B2C-2 起）继续关闭。
    """
    if type(evidence) is not EE.ExecutionEvidence:
        raise ExecutionFactContractError(
            "not_execution_evidence",
            "fact_projection requires a typed execution_evidence.ExecutionEvidence; "
            f"got {type(evidence).__name__} — 伪对象不得冒充 owner 证据",
        )
    verdict = verification_from_evidence(evidence, fill_rows_present=fill_rows_present)
    provenance = getattr(evidence, "provenance", None) or {}
    usable = provenance.get("fill_rows")
    identity, identity_kind = _fact_identity(provenance, getattr(evidence, "order_id", None))
    return _issue_fact_projection(
        version=EXECUTION_FACT_CONTRACT_VERSION,
        identity=identity,
        identity_kind=identity_kind,
        order_id=getattr(evidence, "order_id", None),
        lifecycle_state=str(getattr(evidence, "lifecycle_state", "") or ""),
        fill_verdict=str(evidence.fill_verdict_value()),
        # 六个 factual 字段**逐字**复制 owner 自己发布的 EvidenceField（B2C-4A）。
        # 刻意不取 ``.maybe()``：那会把 unknown / not_applicable 压成 None，而三态的区别
        # 正是 research 必须看见的东西。也刻意不重算 / 不查 DB / 不从兼容列补值。
        code=evidence.code,
        action=evidence.action,
        requested_qty=evidence.requested_qty,
        filled_qty=evidence.filled_qty,
        fill_price=evidence.fill_price,
        fees=evidence.fees,
        business_day=_single_value(
            "business_day", provenance.get("fill_sessions"), subject="a fill business date",
            recorded_rows=provenance.get("fill_session_rows"), usable_rows=usable,
            validator=_is_business_day,
        ),
        observed_at=_single_value(
            "observed_at", provenance.get("fill_observed_ats"), subject="a fill observation time",
            recorded_rows=provenance.get("fill_observed_at_rows"), usable_rows=usable,
            validator=_is_observed_at,
        ),
        verification=verification_contract(
            verdict["execution_status"], verdict["execution_evidence_source"],
        ),
        inconsistencies=tuple(evidence.inconsistencies()),
    )


# ───────────────────────────── self-check ─────────────────────────────


def _self_check() -> None:
    assert status_from_verdict(EE.FILL_VERDICT_VERIFIED) == EXECUTION_STATUS_VERIFIED
    assert status_from_verdict(EE.FILL_VERDICT_PARTIAL) == EXECUTION_STATUS_PARTIAL
    assert status_from_verdict(EE.FILL_VERDICT_PENDING) == EXECUTION_STATUS_UNKNOWN
    assert status_from_verdict(EE.FILL_VERDICT_NONE_CONFIRMED) == EXECUTION_STATUS_NOT_EXECUTED
    assert status_from_verdict(EE.FILL_VERDICT_NOT_ATTEMPTED) == EXECUTION_STATUS_NOT_EXECUTED
    assert status_from_verdict(EE.FILL_VERDICT_UNKNOWN) == EXECUTION_STATUS_UNKNOWN
    # 契约之外的 verdict 不得被当成成交。
    assert status_from_verdict("something_new") == EXECUTION_STATUS_UNKNOWN
    assert status_from_verdict(None) == EXECUTION_STATUS_UNKNOWN

    # 每个 verdict 都有归宿（穷尽），且只有 verified 一个映射到 verified。
    for verdict in EE.FILL_VERDICTS:
        assert verdict in VERDICT_TO_STATUS, verdict
    mapped = [v for v, s in VERDICT_TO_STATUS.items() if s == EXECUTION_STATUS_VERIFIED]
    assert mapped == [EE.FILL_VERDICT_VERIFIED], mapped

    # 没有证据 → unknown，绝不是 verified。
    none_verdict = verification_from_evidence(None)
    assert none_verdict["execution_status"] == EXECUTION_STATUS_UNKNOWN, none_verdict
    assert none_verdict["execution_verified"] is False, none_verdict

    # 旧行（无流水）→ unknown + legacy 来源，**不**升级。
    legacy = legacy_verification(has_fill_rows=False)
    assert legacy["execution_status"] == EXECUTION_STATUS_UNKNOWN, legacy
    assert legacy["execution_verified"] is False, legacy
    assert legacy["execution_evidence_source"] == EVIDENCE_SOURCE_LEGACY, legacy

    # 谓词必须是 fail closed 的：NULL 行被排除。
    assert "COALESCE(execution_verified, 0) = 1" in VERIFIED_PREDICATE
    assert "execution_status = 'verified'" in VERIFIED_PREDICATE

    # Python 侧谓词与 SQL 谓词逐字对齐，且同样 fail closed。
    assert is_verified_row({"execution_status": "verified", "execution_verified": 1})
    assert not is_verified_row({"execution_status": "verified", "execution_verified": 0})
    assert not is_verified_row({"execution_status": "unknown", "execution_verified": 1})
    assert not is_verified_row({"status": "filled"}), "旧行缺列不得被当成成交"
    assert not is_verified_row({"execution_status": None, "execution_verified": None})
    assert not is_verified_row(None)

    report = gate_report([
        {"execution_status": EXECUTION_STATUS_VERIFIED},
        {"execution_status": EXECUTION_STATUS_UNKNOWN},
        {"execution_status": None},
        {"execution_status": "nonsense"},
    ])
    assert report["total"] == 4, report
    assert report["verified"] == 1, report
    assert report["blocked_from_execution_stats"] == 3, report
    print("execution_verification self-check: ok")


if __name__ == "__main__":
    _self_check()
