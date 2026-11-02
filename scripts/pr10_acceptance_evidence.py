"""PR-10 同口径证据；只读源码，不导入交易模块，不访问账本。

python scripts/pr10_acceptance_evidence.py --freeze 52eb50d  # 首次冻结，需完整 Git 历史
python scripts/pr10_acceptance_evidence.py                 # 更新当前对照
python scripts/pr10_acceptance_evidence.py --check         # 浅克隆也可验证当前源码
"""
from __future__ import annotations

import ast
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from tools import coupling_metrics as CM  # noqa: E402
from tools import ledger_write_inventory as INV  # noqa: E402

FROZEN = ROOT / "backend" / "ledger_frozen_baseline.json"
EVIDENCE = ROOT / "backend" / "ledger_fanout_evidence.json"


def json_text(value, depth=0):
    """证据中的边、位置与操作各占一行，避免机器清单淹没人工审阅。"""
    def leaf(item):
        return not isinstance(item, (dict, list)) or (
            isinstance(item, list) and all(not isinstance(v, (dict, list)) for v in item))

    if leaf(value) or (isinstance(value, dict) and all(leaf(v) for v in value.values())):
        return json.dumps(value, ensure_ascii=False)
    pad = " " * (depth + 1)
    if isinstance(value, dict):
        rows = [pad + json.dumps(k, ensure_ascii=False) + ": " + json_text(v, depth + 1)
                for k, v in value.items()]
        return "{\n" + ",\n".join(rows) + "\n" + " " * depth + "}"
    return "[\n" + ",\n".join(pad + json_text(v, depth + 1) for v in value) + "\n" + " " * depth + "]"


def statement_templates(root):
    """四张迁移表的 SQL 模板数；去掉 f-string/拼接表达式的内部重复片段。"""
    sites = []
    for path in sorted(root.glob("*.py")):
        if INV.module_role(path) != "runtime":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        nodes = [(n, dynamic, text) for n, dynamic, text in INV._iter_string_nodes(tree)
                 if INV.SQL_START.match(text)
                 and set(INV._write_targets(text)) & set(INV.PENDING_TABLES)]
        matched = {n for n, _, _ in nodes}
        spans = INV._function_spans(tree)
        for node, dynamic, text in nodes:
            parent = parents.get(node)
            while parent is not None and parent not in matched:
                parent = parents.get(parent)
            if parent is None:
                sites.append({"module": path.stem, "function": INV._owner_function(spans, node.lineno),
                              "line": node.lineno, "dynamic": dynamic,
                              "targets": sorted(set(INV._write_targets(text)) & set(INV.PENDING_TABLES))})
    return sites


def facts(root):
    """三列统一排除 test/seed/migration/fixture；全部引用包括注释和文档字符串。"""
    inventory = INV.scan(root)
    readers = INV.scan_readers(root)
    tables = {}
    runtime = [p for p in sorted(root.glob("*.py")) if INV.module_role(p) == "runtime"]
    for table in INV.CORE_TABLES:
        writers = sorted({e["module"] for e in inventory["entries"]
                          if e["role"] == "runtime" and table in e["write_targets"]})
        refs = [p.stem for p in runtime
                if re.search(rf"\b{table}\b", p.read_text(encoding="utf-8"))]
        tables[table] = {"writers": writers, "readers": readers[table], "references": refs}
    paths = CM.production_paths(root)
    modules = {p.stem for p in paths}
    edges = sorted([p.stem, target] for p in paths
                   for target in CM._local_imports(ast.parse(p.read_text(encoding="utf-8")), modules)
                   if CM.domain_of(p.stem) != CM.domain_of(target)
                   and "foundation" not in (CM.domain_of(p.stem), CM.domain_of(target)))
    digest = hashlib.sha256()
    for p in runtime:
        digest.update(p.name.encode())
        digest.update(p.read_bytes().replace(b"\r\n", b"\n"))
    return {"tables": tables, "cross_domain_edges": edges, "sha256": digest.hexdigest(),
            "sql_statement_templates": statement_templates(root)}


def freeze(ref):
    sha = subprocess.check_output(["git", "rev-parse", ref], cwd=ROOT, encoding="utf-8").strip()
    archive = subprocess.check_output(["git", "archive", sha, "backend"], cwd=ROOT)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "backend"
        root.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar.getmembers():
                path = Path(member.name)
                if len(path.parts) == 2 and path.suffix == ".py":
                    (root / path.name).write_bytes(tar.extractfile(member).read())
        plan = json.loads(subprocess.check_output(
            ["git", "show", f"{sha}:backend/ledger_owner_plan.json"], cwd=ROOT, encoding="utf-8"))
        inventory = INV.scan(root)
        groups = INV.group_targets(inventory)
        operations = []
        for key, tables in groups.items():
            spec = plan["operations"][key]
            for table in tables:
                target = spec.get("targets", {}).get(table)
                package = target["package"] if target else spec["single_package"]
                operations.append({"id": f"PR10:{key}:{table}", "source": key,
                                   "table": table, "package": package,
                                   "operation": (target or {}).get("operation", spec["operation"]),
                                   "transaction": spec["transaction"],
                                   "sql_sites": [{"line": e["line"], "kind": e["kind"], "sql": e["sql"]}
                                                 for e in inventory["entries"]
                                                 if f'{e["module"]}::{e["function"]}' == key
                                                 and table in e["write_targets"]]})
        payload = {"ref": sha, "writer_function_groups": len(groups),
                   "table_operations": len(operations), "packages": dict(Counter(
                       o["package"] for o in operations)), "operations": operations,
                   "facts": facts(root)}
        FROZEN.write_text(json_text(payload) + "\n", encoding="utf-8")


def evidence():
    before = json.loads(FROZEN.read_text(encoding="utf-8"))
    after = facts(INV.BACKEND)
    old_edges = {tuple(edge) for edge in before["facts"]["cross_domain_edges"]}
    new_edges = {tuple(edge) for edge in after["cross_domain_edges"]}
    return {"baseline_ref": before["ref"],
            "table_scope": "backend/*.py; module_role == runtime; exact table word; references includes comments/docstrings",
            "read_scope": "SQL FROM/JOIN; DELETE target excluded; subqueries retained",
            "import_scope": "coupling_metrics.production_paths/domain_of; excludes foundation edges; unchanged classification",
            "before": before["facts"], "after": after,
            "added_cross_domain_edges": [list(edge) for edge in sorted(new_edges - old_edges)],
            "removed_cross_domain_edges": [list(edge) for edge in sorted(old_edges - new_edges)]}


def main():
    if "--freeze" in sys.argv:
        freeze(sys.argv[sys.argv.index("--freeze") + 1])
    payload = evidence()
    if "--check" in sys.argv:
        assert payload == json.loads(EVIDENCE.read_text(encoding="utf-8")), "PR-10 同口径证据已漂移"
    else:
        EVIDENCE.write_text(json_text(payload) + "\n", encoding="utf-8")
    print(json.dumps({"edges_before": len(payload["before"]["cross_domain_edges"]),
                      "edges_after": len(payload["after"]["cross_domain_edges"]),
                      "tables": {t: {k: [len(payload["before"]["tables"][t][k]), len(v)]
                                     for k, v in spec.items()}
                                 for t, spec in payload["after"]["tables"].items()}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
