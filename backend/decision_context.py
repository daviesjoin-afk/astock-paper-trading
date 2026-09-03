# -*- coding: utf-8 -*-
"""决策层使用的证据快照契约。

这个模块只定义决策需要的输入形状，并提供一个兼容旧调用的加载适配器。
后续可以把适配器迁移到 marketdata service，而不再让规则函数直接读取
行情源；显式传入全部字段时，加载器不会发起网络或文件读取。
"""

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence


@dataclass(frozen=True)
class EvidenceSnapshot:
    """一次决策所需的完整证据集合。

    ``kline`` 保持现有 pandas DataFrame 兼容，避免在第一阶段改变指标计算。
    ``source`` 仅用于审计和测试，不参与交易决策。
    """

    code: str
    name: str
    snap: Mapping[str, Any] = field(default_factory=dict)
    kline: Any = None
    sector_flow: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    overseas_gate: Mapping[str, Any] = field(default_factory=dict)
    news_hits: Any = None
    source: str = "injected"


def load_evidence(
    code: str,
    name: Optional[str] = None,
    kline: Any = None,
    snap: Optional[Mapping[str, Any]] = None,
    sector_flow: Optional[Sequence[Mapping[str, Any]]] = None,
    overseas_gate: Optional[Mapping[str, Any]] = None,
    news_hits: Any = None,
) -> EvidenceSnapshot:
    """加载决策证据，并保留旧版 ``decision_engine`` 的缺省行为。

    这是临时兼容适配器：决策调用方可以先显式注入证据，未来再将本函数
    移到独立的 marketdata service。所有读取都集中在这里，规则函数不再
    需要知道行情提供方的细节。
    """

    source = "injected"
    if snap is None:
        import data_fetcher as dfc

        rows = dfc.fetch_realtime_for_codes([code])
        snap = rows[0] if rows else {}
        source = "provider"
    else:
        snap = dict(snap)

    if kline is None:
        import data_fetcher as dfc

        kline = dfc.load_cached_kline(code)
        source = "provider"

    if sector_flow is None:
        import data_fetcher as dfc

        sector_flow = dfc.fetch_sector_flow("industry")
        source = "provider"

    if overseas_gate is None:
        try:
            import factors as factor_module

            overseas_gate = factor_module.overseas_risk_gate()
        except Exception:
            overseas_gate = {"light": "unknown"}
        source = "provider"

    return EvidenceSnapshot(
        code=code,
        name=name or snap.get("name") or code,
        snap=snap,
        kline=kline,
        sector_flow=sector_flow or (),
        overseas_gate=overseas_gate or {},
        news_hits=news_hits,
        source=source,
    )
