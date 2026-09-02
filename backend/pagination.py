# -*- coding: utf-8 -*-
"""API 分页工具模块。

提供标准化的分页响应格式。
"""
from __future__ import annotations

from typing import Any, Optional


def paginate(
    items: list,
    page: int = 1,
    page_size: int = 20,
    total: Optional[int] = None,
) -> dict:
    """对列表进行分页。

    Args:
        items: 数据列表
        page: 页码（从 1 开始）
        page_size: 每页数量
        total: 总数量（如果已知，避免重新计算）

    Returns:
        分页响应字典
    """
    page = max(1, page)
    page_size = max(1, min(100, page_size))  # 限制 1-100

    if total is None:
        total = len(items)

    total_pages = (total + page_size - 1) // page_size if total > 0 else 1
    start = (page - 1) * page_size
    end = start + page_size

    # 如果 items 是完整列表，直接切片
    if isinstance(items, list):
        page_items = items[start:end]
    else:
        page_items = items

    return {
        "items": page_items,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1,
        },
    }


def paginate_query(
    conn,
    table: str,
    where: str = "",
    params: tuple = (),
    order_by: str = "id DESC",
    page: int = 1,
    page_size: int = 20,
    select: str = "*",
) -> dict:
    """对数据库查询进行分页。

    Args:
        conn: 数据库连接
        table: 表名
        where: WHERE 条件
        params: 查询参数
        order_by: 排序
        page: 页码
        page_size: 每页数量
        select: SELECT 字段

    Returns:
        分页响应字典
    """
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    offset = (page - 1) * page_size

    # 查询总数
    count_sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        count_sql += f" WHERE {where}"
    total = conn.execute(count_sql, params).fetchone()[0]

    # 查询当前页
    query_sql = f"SELECT {select} FROM {table}"
    if where:
        query_sql += f" WHERE {where}"
    query_sql += f" ORDER BY {order_by} LIMIT ? OFFSET ?"
    rows = conn.execute(query_sql, (*params, page_size, offset)).fetchall()

    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    return {
        "items": [dict(r) for r in rows],
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1,
        },
    }
