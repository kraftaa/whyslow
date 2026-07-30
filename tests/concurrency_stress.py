import threading
import time
import shutil

from whyslow.storage import Store

shutil.rmtree("/tmp/concurrency_stress", ignore_errors=True)
DB_PATH = "/tmp/concurrency_stress/store.sqlite3"

barrier = threading.Barrier(3)
errors = []
lock = threading.Lock()


def writer(worker_id):
    store = Store(DB_PATH)
    barrier.wait()  # force all three to hit their first write at the same instant
    for batch in range(50):
        rows = [
            (worker_id * 10000 + batch * 100 + i, "client backend", "role", "app", "active", "cpu", "SELECT 1")
            for i in range(50)  # larger batch -> longer write, more collision chance
        ]
        try:
            store.write_sessions(rows, ts=time.time())
        except Exception as e:
            with lock:
                errors.append((worker_id, batch, type(e).__name__, str(e)))
    store.close()


threads = [threading.Thread(target=writer, args=(i,)) for i in range(3)]
for t in threads:
    t.start()
for t in threads:
    t.join()

print(f"errors observed: {len(errors)}")
for e in errors[:10]:
    print(f"  worker={e[0]} batch={e[1]} {e[2]}: {e[3]}")

if errors:
    print(f"\nFAIL: {len(errors)} write errors under genuine concurrent load "
          f"(current settings: no WAL mode, no busy_timeout)")
else:
    print("\nno errors even under forced concurrent load -- unexpected given no WAL/busy_timeout, re-check")
