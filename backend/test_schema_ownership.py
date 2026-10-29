# -*- coding: utf-8 -*-
"""Schema 所有权守卫：**只减不增**。

2026-10-29 对抗性审查 P1-1 的结论：PR #290 把 paper 账本的建表/迁移收进了
``paper_schema_migrations``，但同一份代码里仍有 48 个生产模块自己
``CREATE TABLE``（183 处）、15 个自己 ``ALTER TABLE``（33 处）；更有说服力的是
``db_migrate.MIGRATIONS`` 里 ``paper_trading`` 有 40 个版本步骤、
``adaptive_learning`` 只有 **1 个**——adaptive 库里 40+ 张表其实是各模块在连接时
建出来的，所以"已是最新 v1"这句话与真实 schema 无关。这与 #290 修掉的
"两条升级链漂移 → 升级后缺表"是同一个根因，只是发生在另一个库。

本批不动迁移逻辑（那需要 5 个 owner 的数据流快照），先把**事实**锁住：
任何模块新增建表/改表能力都会失败，收敛时只能从 lock 里删条目。
"""
from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
LOCK = BACKEND / "schema_ownership.lock.json"
CREATE = re.compile(r"\bCREATE\s+TABLE\b", re.I)
ALTER = re.compile(r"\bALTER\s+TABLE\b", re.I)


def _docstring_nodes(tree) -> set[int]:
    """模块/类/函数的 docstring 常量节点 id。

    2026-10-29：本守卫原先按"任何字符串常量里出现 CREATE TABLE"判定，于是
    ``adaptive_schema_migrations`` 只在**文档字符串**里解释 DDL 就被判为"新增建表"。
    文档不是建表语句，这里把 docstring 节点排除掉——既消掉假阳性，也避免为了
    绕开守卫去改写注释。
    """
    skip = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            skip.add(id(first.value))
    return skip


def _scan(symbol: re.Pattern) -> set[str]:
    modules: set[str] = set()
    for path in sorted(BACKEND.glob("*.py")):
        if path.name.startswith("test_") or path.name == "__init__.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in docstrings:
                continue
            if symbol.search(node.value):
                modules.add(path.stem)
                break
    return modules


class SchemaOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lock = json.loads(LOCK.read_text(encoding="utf-8"))

    def test_no_module_starts_creating_tables(self):
        allowed = set(self.lock["create_table"])
        self.assertTrue(allowed, "白名单为空等于守卫失效")
        offenders = sorted(_scan(CREATE) - allowed)
        self.assertEqual(
            offenders, [],
            "以下模块新增了 CREATE TABLE：schema 所有权必须归 owner，"
            "新表请加进对应 *_schema_migrations 并同步收敛本 lock")

    def test_no_module_starts_altering_tables(self):
        allowed = set(self.lock["alter_table"])
        self.assertTrue(allowed, "白名单为空等于守卫失效")
        offenders = sorted(_scan(ALTER) - allowed)
        self.assertEqual(
            offenders, [],
            "以下模块新增了 ALTER TABLE：列升级必须由迁移 owner 持有")

    def test_lock_entries_are_all_real_modules(self):
        names = set(self.lock["create_table"]) | set(self.lock["alter_table"])
        for name in names:
            with self.subTest(module=name):
                self.assertTrue((BACKEND / f"{name}.py").is_file(),
                                f"lock 里登记了不存在的模块：{name}")

    def test_the_scan_itself_is_not_vacuous(self):
        """守卫必须是真扫描：扫不到东西时"没有违规"毫无意义。"""
        self.assertGreater(len(_scan(CREATE)), 10)
        self.assertGreater(len(_scan(ALTER)), 3)


if __name__ == "__main__":
    unittest.main()
