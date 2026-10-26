# -*- coding: utf-8 -*-
"""研究运行时的连接边界。

三种连接不互相借用：

* 研究队列/实验进度由 ``agent_task_queue`` 的调用方持有；
* 正式绩效事实由 ``strategy_performance`` 用只读账本连接读取；
* AI 槽位配置只由 **配置 owner**（``adaptive_engine``）的连接工厂提供。

本模块**不 import** ``adaptive_engine``：宿主在组合根调用
:func:`bind_config_connection` 注册自己的连接工厂，依赖方向固定为
``adaptive_engine → research_runtime_config``，不会因为"读一次配置"而长出一条回边
（那会让编排、研究循环与学习引擎同时进入一个 import 环）。未绑定时
:func:`configuration_connection` 抛 :class:`ConfigurationConnectionUnbound`，调用方
按"配置不可达"处理，绝不悄悄换成别的库。

本模块不保存、复制或迁移模型凭据，也不给研究库创建配置表。
"""
from __future__ import annotations

import contextlib


class ConfigurationConnectionUnbound(RuntimeError):
    """没有任何 owner 注册连接工厂：这次配置读取没有可信来源。"""


#: 由宿主（组合根）注册的连接工厂：``() -> contextmanager[sqlite3.Connection]``。
_CONFIG_FACTORY = None


def bind_config_connection(factory):
    """注册配置 owner 的连接工厂；``None`` 表示解除绑定（宿主关停 / 测试隔离）。

    只允许宿主调用；重复注册等价于"换 owner"，明确覆盖而不是静默叠加。
    """
    if factory is not None and not callable(factory):
        raise TypeError("配置连接工厂必须可调用")
    global _CONFIG_FACTORY
    _CONFIG_FACTORY = factory
    return factory


def config_connection_factory():
    """当前绑定的工厂（诊断/测试用，未绑定时为 ``None``）。"""
    return _CONFIG_FACTORY


def config_connection_bound() -> bool:
    """诊断用：宿主是否已注册配置 owner。"""
    return _CONFIG_FACTORY is not None


@contextlib.contextmanager
def configuration_connection(conn=None):
    """复用调用方已打开的配置连接；否则用宿主注册的 owner 工厂打开一条。"""
    if conn is not None:
        yield conn
        return
    factory = _CONFIG_FACTORY
    if factory is None:
        raise ConfigurationConnectionUnbound(
            "模型配置 owner 未绑定：宿主进程应在启动时调用 "
            "research_runtime_config.bind_config_connection(adaptive_engine._connect)")
    with factory() as config_conn:
        yield config_conn


def read_performance_fact(request, *, db_path=None):
    """正式事实独立走只读账本；绝不把研究队列连接传进事实读取器。"""
    import strategy_performance as SP

    return SP.collect_performance_fact(request, db_path=db_path)
