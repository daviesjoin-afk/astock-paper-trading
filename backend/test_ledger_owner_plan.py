"""PR-10 收尾清单守卫：源码扫描 ↔ 计划 ↔ 文档三者一致。

这些测试的作用不是证明迁移正确，而是保证「清单一经冻结就不能被悄悄绕过」：
新增或删除任何一条对四张 pending 表的运行时写入，都会先在这里变红，
必须先把它登记到 `backend/ledger_owner_plan.json` 并指名 owner 与工作包。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import tools.ledger_write_inventory as INV

BACKEND = Path(__file__).resolve().parent
PLAN = json.loads((BACKEND / "ledger_owner_plan.json").read_text(encoding="utf-8"))
INVENTORY = json.loads((BACKEND / "ledger_write_inventory.json").read_text(encoding="utf-8"))
DOC = BACKEND.parent / "docs" / "LEDGER_OWNERSHIP.md"


class InventoryScanTests(unittest.TestCase):
    def test_committed_inventory_matches_fresh_scan(self):
        fresh = INV.scan()
        self.assertEqual(fresh, INVENTORY,
                         "ledger_write_inventory.json 与源码不一致；"
                         "改完源码要重新生成：python backend/tools/ledger_write_inventory.py")

    def test_scan_separates_write_targets_from_subquery_reads(self):
        entries = INVENTORY["entries"]
        by_key = {(e["module"], e["function"]): e for e in entries}
        # 信号对账只写信号表：子查询里出现的订单表不应被记成订单写入。
        signal_write = by_key[("signal_service", "reconcile_signal_status")]
        self.assertEqual(["paper_signals"], signal_write["write_targets"])
        # 等待池信号的写入不再读订单表（身份由调用方给出）。
        waitlist = by_key[("signal_service", "retire_waitlist_signals")]
        self.assertEqual(["paper_signals"], waitlist["write_targets"])
        self.assertEqual([], waitlist["read_refs"])
        # 正例：确实同时读订单表的语句，写目标与读引用分别登记。
        readers = [e for e in entries
                   if e["role"] == "runtime" and e["read_refs"]
                   and "paper_orders" in e["read_refs"]]
        self.assertTrue(readers, "扫描器应当能识别语句里的跨表读引用")
        for entry in readers:
            self.assertNotIn("paper_orders", entry["write_targets"])

    def test_non_runtime_roles_are_not_counted_as_writers(self):
        roles = {e["role"] for e in INVENTORY["entries"]}
        self.assertTrue({"runtime", "test", "seed", "migration"}.issubset(roles))
        runtime_modules = {e["module"] for e in INVENTORY["entries"] if e["role"] == "runtime"}
        for excluded in ("demo_seed", "paper_schema_migrations", "execution_quality_shadow"):
            self.assertNotIn(excluded, runtime_modules, f"{excluded} 混进了运行时写方集合")

    def test_dynamic_writes_are_flagged_not_dropped(self):
        dynamic = [e for e in INVENTORY["entries"]
                   if e["role"] == "runtime" and e["kind"] == "dynamic"]
        # 现状登记：动态拼接确实存在，扫描器必须标出来而不是当作没有写入。
        self.assertTrue(dynamic, "运行时动态 SQL 不应为空；为空说明扫描口径漏了 f-string")
        for entry in dynamic:
            self.assertTrue(entry["write_targets"])


class OwnerPlanCoverageTests(unittest.TestCase):
    def test_plan_covers_exactly_the_scanned_groups(self):
        groups = set(INV.group_targets(INVENTORY))
        planned = set(PLAN["operations"])
        self.assertEqual(groups, planned,
                         "计划与扫描不一致：多出的写点必须登记，删除的写点必须清出计划")

    def test_every_group_names_owner_and_package_per_table(self):
        grouped = INV.group_targets(INVENTORY)
        for key, tables in grouped.items():
            spec = PLAN["operations"][key]
            self.assertTrue(spec["operation"].strip(), key)
            self.assertTrue(spec["judge"].strip(), key)
            self.assertTrue(spec["transaction"].strip(), key)
            targets = spec.get("targets")
            if targets is None:
                self.assertIn(spec["single_package"], INV.PLAN_PACKAGES, key)
                self.assertEqual(1, len(tables), f"{key} 写了多张表却没有分表登记")
                owner = spec["single_owner"]
                self.assertTrue(owner.strip(), key)
            else:
                self.assertEqual(set(tables), set(targets),
                                 f"{key} 的分表登记与扫描不一致")
                for table, target in targets.items():
                    self.assertIn(target["package"], INV.PLAN_PACKAGES, f"{key}:{table}")
                    self.assertTrue(target["owner"].strip(), f"{key}:{table}")

    def test_owner_targets_are_registered(self):
        owners = PLAN["planned_owners"]
        for key, spec in PLAN["operations"].items():
            names = [spec["single_owner"]] if "single_owner" in spec else [
                target["owner"] for target in spec["targets"].values()]
            for name in names:
                self.assertIn(name, owners, f"{key} 的 owner {name} 未登记")
        for name, meta in owners.items():
            self.assertIn(meta["status"], ("existing", "planned"))
            self.assertIn(meta["package"], INV.PLAN_PACKAGES)
            self.assertTrue(meta["scope"].strip())

    def test_packages_are_the_frozen_five(self):
        self.assertEqual(("A", "B", "C", "D"), INV.PLAN_PACKAGES)
        used = set()
        for spec in PLAN["operations"].values():
            used.update({spec["single_package"]} if "single_package" in spec
                        else {target["package"] for target in spec["targets"].values()})
        self.assertEqual({"A", "B", "C", "D"}, used, "每个工作包都必须有实际登记的操作")

    def test_pending_tables_are_still_pending(self):
        # 收口完成前不允许仅靠改标签宣布单写。
        for table in INV.PENDING_TABLES:
            self.assertIn(table, INV.CORE_TABLES)
            self.assertIn(table, ("paper_orders", "paper_signals",
                                  "paper_accounts", "paper_cycles"))


class FrozenListDocTests(unittest.TestCase):
    def test_doc_section_matches_rendered_plan(self):
        text = DOC.read_text(encoding="utf-8")
        start = text.index(INV.PLAN_START) + len(INV.PLAN_START)
        end = text.index(INV.PLAN_END)
        committed = text[start:end].strip()
        rendered = INV.render_plan_markdown(INVENTORY, PLAN).strip()
        self.assertEqual(rendered, committed,
                         "文档里的冻结清单与计划不一致；"
                         "重新生成后同步到 docs/LEDGER_OWNERSHIP.md 第16节")

    def test_doc_lists_all_four_pending_tables(self):
        text = DOC.read_text(encoding="utf-8")
        section = text[text.index("## 16."):]
        for table in INV.PENDING_TABLES:
            self.assertIn(table, section, f"第16节缺少 {table}")


if __name__ == "__main__":
    unittest.main()
