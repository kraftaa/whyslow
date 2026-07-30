import threading
import time
import shutil

from whyslow.storage import Store
from whyslow import explain as explain_mod

shutil.rmtree("/tmp/burst_stress", ignore_errors=True)
DB_PATH = "/tmp/burst_stress/store.sqlite3"

stop = threading.Event()
read_errors = []
read_count = [0]
write_errors = []


def burst_writer():
    store = Store(DB_PATH)
    # Simulate a severe incident: a large burst of session rows in one
    # poll (e.g. a real connection-count spike), repeated several times.
    for burst in range(10):
        rows = [
            (burst * 1000 + i, "client backend", "role", "app", "active", "cpu", "SELECT 1")
            for i in range(300)
        ]
        try:
            store.write_sessions(rows, ts=time.time())
        except Exception as e:
            write_errors.append((type(e).__name__, str(e)))
        time.sleep(0.1)
    store.close()


def concurrent_reader():
    store = Store(DB_PATH)
    while not stop.is_set():
        try:
            now = time.time()
            explain_mod.explain(store, now - 5, now)
            read_count[0] += 1
        except Exception as e:
            read_errors.append((type(e).__name__, str(e)))
        time.sleep(0.02)
    store.close()


writer_thread = threading.Thread(target=burst_writer)
reader_thread = threading.Thread(target=concurrent_reader)
writer_thread.start()
reader_thread.start()

writer_thread.join(timeout=10)
stop.set()
reader_thread.join(timeout=5)

print(f"reads completed while bursts were writing: {read_count[0]}")
print(f"write errors: {len(write_errors)}")
print(f"read errors: {len(read_errors)}")

assert not write_errors, f"burst writer hit errors: {write_errors[:3]}"
assert not read_errors, f"reader hit errors during burst writes: {read_errors[:3]}"
assert read_count[0] > 0, "expected at least some reads to complete during the burst writes"
print("\nPASS: burst writes (300 rows x 10) + concurrent reads both succeed under WAL mode")
