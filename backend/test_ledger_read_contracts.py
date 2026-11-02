"""PR-10 读取契约与动态写入登记的守卫（工作包 E）。

三条不变式：

1. 十张核心表各有读取契约，且契约里出现的模块必须真实存在；
2. 扫描到的读者与契约**逐一对齐** —— 新模块读核心表却没登记会立刻变红；
3. 动态拼接写入必须逐条登记原因，不允许"扫不到就当没有"。
"""
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

import tools.ledger_write_inventory as INV

BACKEND = Path(__file__).resolve().parent
CONTRACTS = json.loads((BACKEND / "ledger_read_contracts.json").read_text(encoding="utf-8"))["tables"]
DYNAMIC = json.loads((BACKEND / "ledger_dynamic_writes.json").read_text(encoding="utf-8"))["writes"]
MATRIX_DOC = BACKEND.parent / "docs" / "LEDGER_OWNERSHIP.md"


def _known_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


class ContractShapeTests(unittest.TestCase):
    def test_every_core_table_has_a_contract(self):
        self.assertEqual(set(INV.CORE_TABLES), set(CONTRACTS))

    def test_each_contract_names_readers_and_policy(self):
        for table, spec in CONTRACTS.items():
            with self.subTest(table=table):
                self.assertTrue(spec["authoritative_readers"] or spec.get("authoritative_read_policy"),
                                f"{table} 缺权威读者或无独立权威读入口的说明")
                for module in spec["authoritative_readers"]:
                    self.assertTrue(_known_module(module), f"{table}: 未知模块 {module}")
                self.assertTrue(spec["unknown_handling"].strip(), f"{table} 缺 unknown 处理说明")
                self.assertTrue(spec["reads_may_trigger_writes"].strip(),
                                f"{table} 缺「读取是否可能触发写入」说明")
                self.assertIsInstance(spec["cycle_scoped"], bool)

    def test_write_owner_matches_the_single_writer_reality(self):
        scan = INV.scan()
        for table in INV.CORE_TABLES:
            writers = sorted({e["module"] for e in scan["entries"]
                              if e["role"] == "runtime" and table in e["write_targets"]})
            spec = CONTRACTS[table]
            if len(writers) == 1:
                self.assertEqual([writers[0]], spec["write_owner"],
                                 f"{table} 单写方与契约不一致")
            else:
                self.assertEqual(writers, sorted(spec["write_owner"]),
                                 f"{table} 多写方时必须逐个登记")


class ReaderCoverageTests(unittest.TestCase):
    def test_scanned_readers_are_all_classified(self):
        readers = INV.scan_readers()
        for table in INV.CORE_TABLES:
            spec = CONTRACTS[table]
            listed = (set(spec["authoritative_readers"]) | set(spec["projection_readers"])
                      | set(spec["in_transaction_readers"]))
            scanned = set(readers[table])
            self.assertEqual(scanned - listed, set(),
                             f"{table}: 有模块读了这张表却没登记分类")
            self.assertEqual(listed - scanned, set(),
                             f"{table}: 契约里登记了不读这张表的模块")

    def test_no_module_is_classified_twice(self):
        for table, spec in CONTRACTS.items():
            buckets = [set(spec["authoritative_readers"]), set(spec["projection_readers"]),
                       set(spec["in_transaction_readers"])]
            total = sum(len(bucket) for bucket in buckets)
            self.assertEqual(total, len(set().union(*buckets)),
                             f"{table}: 同一模块被分到多个读取角色")

    def test_projection_readers_are_not_write_owners(self):
        scan = INV.scan()
        for table, spec in CONTRACTS.items():
            writers = {e["module"] for e in scan["entries"]
                       if e["role"] == "runtime" and table in e["write_targets"]}
            for module in spec["projection_readers"]:
                self.assertNotIn(module, writers,
                                 f"{table}: 展示投影模块 {module} 不应同时是写方")


class DynamicWriteRegistryTests(unittest.TestCase):
    def test_every_dynamic_write_is_registered_with_a_reason(self):
        scan = INV.scan()
        groups = {}
        for entry in scan["entries"]:
            if entry["role"] != "runtime" or entry["kind"] != "dynamic":
                continue
            key = f'{entry["module"]}::{entry["function"]}'
            groups.setdefault(key, set()).update(entry["write_targets"])
        self.assertEqual(set(groups), set(DYNAMIC),
                         "动态写入集合与登记不一致：新增拼接写入必须登记原因")
        for key, spec in DYNAMIC.items():
            self.assertEqual(sorted(groups[key]), sorted(spec["tables"]), key)
            self.assertTrue(spec["reason"].strip(), key)
            self.assertFalse(spec["reason"].startswith("未登记"), key)

    def test_contract_section_is_documented(self):
        text = MATRIX_DOC.read_text(encoding="utf-8")
        self.assertIn("## 17. 十张核心表的读取契约", text)
        for table in INV.CORE_TABLES:
            self.assertIn(table, text)


if __name__ == "__main__":
    unittest.main()
