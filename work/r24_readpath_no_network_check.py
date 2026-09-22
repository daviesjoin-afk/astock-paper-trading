import os, sys, tempfile, time
data_dir = tempfile.mkdtemp(prefix="r24-migrate-")
os.environ["HTTP_PROXY"]="http://127.0.0.1:9"; os.environ["HTTPS_PROXY"]="http://127.0.0.1:9"
os.environ["http_proxy"]="http://127.0.0.1:9"; os.environ["https_proxy"]="http://127.0.0.1:9"
os.environ["NO_PROXY"]=""; os.environ["ASTOCK_DATA_DIR"]=data_dir
os.environ["ASTOCK_DEMO"]="1"; os.environ["ASTOCK_DEMO_FORCE"]="1"
os.environ["PYTHONDONTWRITEBYTECODE"]="1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND); os.chdir(BACKEND)

import data_fetcher as dfc
import paper_trading as PT

CALLED = []
def _boom(*a, **k):
    CALLED.append(1)
    raise AssertionError("network must not be called from read path")
for name in ("fetch_market_snapshot_full","fetch_market_snapshot","fetch_indices",
             "fetch_realtime_for_codes","fetch_independent_realtime_for_codes",
             "check_data_source_health"):
    setattr(dfc, name, _boom)

t0=time.time()
try:
    res = PT.strategy_allocation_explain(); err=None
except Exception as exc:
    res=None; err=f"{type(exc).__name__}: {exc}"
el=time.time()-t0
print(f"elapsed={el:.3f}s  error={err}")
if res:
    print("market_data:", res.get("market_data"))
    print("strategies:", len(res.get("strategies") or []))
print("network attempts:", len(CALLED))
