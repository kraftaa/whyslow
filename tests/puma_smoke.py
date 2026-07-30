import shutil
import time

from fake_puma import start as start_fake_puma
from whyslow.storage import Store
from whyslow.collector_puma import PumaCollector

DB_PATH = "/tmp/puma_smoke/store.sqlite3"
shutil.rmtree("/tmp/puma_smoke", ignore_errors=True)

# --- single-mode ---
srv1 = start_fake_puma(9301, mode="single")
time.sleep(0.2)
store = Store(DB_PATH)
collector = PumaCollector("web-single", "http://127.0.0.1:9301/stats", store)
backlog, pool_capacity = collector.poll_once()
print(f"single-mode: backlog={backlog} pool_capacity={pool_capacity}")
assert backlog == 5, f"expected backlog=5, got {backlog}"
assert pool_capacity == 6, f"expected pool_capacity=6, got {pool_capacity}"
srv1.shutdown()

# --- clustered-mode: one worker saturated, three idle ---
srv2 = start_fake_puma(9302, mode="clustered")
time.sleep(0.2)
collector2 = PumaCollector("web-clustered", "http://127.0.0.1:9302/stats", store)
backlog2, pool_capacity2 = collector2.poll_once()
print(f"clustered-mode: backlog={backlog2} pool_capacity={pool_capacity2}")
# Worst-worker aggregation: the saturated worker's backlog/capacity should
# show through, not get averaged/summed away by the idler workers.
# Sum across workers would give backlog=25 (20+5+0+0) and pool_capacity=27
# (0+12+15+15) -- both wrong. Worst-worker gives the real signal: 20 and 0.
assert backlog2 == 20, f"expected worst-worker backlog=20, got {backlog2}"
assert pool_capacity2 == 0, f"expected worst-worker pool_capacity=0, got {pool_capacity2}"

store.close()
print("\nPASS: single-mode and clustered-mode (worst-worker) Puma parsing both correct")
