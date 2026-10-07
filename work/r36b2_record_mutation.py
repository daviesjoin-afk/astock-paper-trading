import json, subprocess, sys
from pathlib import Path
runs = [
 "r36b2_candidate_robustness_mutation_check.py",
 "r36b1_candidate_experiment_mutation_check.py",
 "r36a_search_mutation_check.py",
 "r35a_candidate_mutation_check.py",
 "r35b_candidate_expansion_mutation_check.py",
 "r35c_ai_candidate_mutation_check.py",
]
result = {}
for name in runs:
    p = subprocess.run((sys.executable, f"work/{name}"), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=1800)
    tail = [l for l in (p.stdout + p.stderr).splitlines()
            if l.strip().startswith(("M-", "survived", "restore", "baseline after"))]
    result[name] = {"exit_code": p.returncode, "summary": tail}
    print(name, p.returncode, "|", [l for l in tail if "detected =" in l or "survived =" in l])
Path("work/r36b2_mutation_results.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
