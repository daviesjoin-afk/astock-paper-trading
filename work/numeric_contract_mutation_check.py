"""变异验证：numeric guard 与删除 guard 都必须非恒真"""
import subprocess, sys
from pathlib import Path

BACKEND = Path("backend")
MUTATIONS = [
    ("paper_numeric.py",
     "    if isinstance(value, (int, float)) and value == value:\n        return float(value)\n    return default",
     "    try:\n        if value is None:\n            return default\n        return float(value)\n    except (TypeError, ValueError):\n        return default",
     "M1: 把严格契约换成宽松（吞掉字符串价格）"),
    ("paper_numeric.py",
     "    if isinstance(value, (int, float)) and value == value:\n        return float(value)\n    return default",
     "    if isinstance(value, (int, float)):\n        return float(value)\n    return default",
     "M2: 去掉 NaN 检查"),
    ("paper_trading.py",
     "_num = PNum.num",
     "def _num(value, default=0.0):\n    return float(value) if isinstance(value, (int, float)) and pd.notna(value) else default",
     "M3: 把直引用改回内联 def（复制第二份实现）"),
    ("rebalance_scanner.py",
     "def _num(value, default=0.0):",
     "def _json(value):\n    return json.dumps(value, ensure_ascii=False)\n\n\ndef _num(value, default=0.0):",
     "M4: 孤儿 _json 复活"),
    ("retention_maintenance.py",
     "def clean_orphan_klines(",
     "def _loads(value):\n    return json.loads(value)\n\n\ndef clean_orphan_klines(",
     "M5: 孤儿 _loads 复活"),
]

results = []
for filename, old, new, label in MUTATIONS:
    path = BACKEND / filename
    original = path.read_text(encoding="utf-8")
    if old not in original:
        results.append((label, "ANCHOR_MISSING", ""))
        continue
    path.write_text(original.replace(old, new, 1), encoding="utf-8", newline="\n")
    try:
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "discover",
             "-s", "backend", "-p", "test_paper_numeric_contract.py"],
            capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
        verdict = "DETECTED" if proc.returncode != 0 else "SURVIVED"
        tail = [l for l in (proc.stderr or "").splitlines() if "AssertionError" in l]
        results.append((label, verdict, tail[0][:95] if tail else ""))
    finally:
        path.write_text(original, encoding="utf-8", newline="\n")

print(f"{'变异':44s} {'结果':10s} 证据")
for label, verdict, evidence in results:
    print(f"{label:44s} {verdict:10s} {evidence}")
survived = [r for r in results if r[1] == "SURVIVED"]
print(f"\n检出 {len(results)-len(survived)}/{len(results)}，存活 {len(survived)}")
