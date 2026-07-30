import threading
import time
import shutil

from whyslow.storage import Store
from whyslow import explain as explain_mod

shutil.rmtree("/tmp/read_write_stress", ignore_errors=True)
DB_PATH = "/tmp/read_write_stress/store.sqlite3"

stop = threading.Event()
write_errors = []
read_errors = []
read_count = [0]
read_timings = []


def continuous_writer():
    store = Store(DB_PATH)
    i = 0
    while not stop.is_set():
        try:
            store.write_sessions(
                [(i, "client backend", "role", "app", "active", "cpu", "SELECT 1")],
                ts=time.time(),
            )
        except Exception as e:
            write_errors.append((type(e).__name__, str(e)))
        i += 1
        time.sleep(1.0)  # matches the real collector's actual default poll interval
    store.close()


def concurrent_reader():
    store = Store(DB_PATH)
    while not stop.is_set():
        try:
            now = time.time()
            t0 = time.perf_counter()
            explain_mod.explain(store, now - 5, now)
            elapsed = time.perf_counter() - t0
            read_timings.append(elapsed)
            read_count[0] += 1
        except Exception as e:
            read_errors.append((type(e).__name__, str(e)))
        time.sleep(0.05)  # explain called ~20x/sec, deliberately aggressive
    store.close()


writer_thread = threading.Thread(target=continuous_writer)
reader_thread = threading.Thread(target=concurrent_reader)
writer_thread.start()
reader_thread.start()

time.sleep(5)
stop.set()
writer_thread.join()
reader_thread.join()

print(f"reads completed: {read_count[0]}")
if read_timings:
    print(f"read timings: min={min(read_timings)*1000:.1f}ms avg={sum(read_timings)/len(read_timings)*1000:.1f}ms max={max(read_timings)*1000:.1f}ms")
print(f"write errors: {len(write_errors)}")
print(f"read errors: {len(read_errors)}")
for e in (write_errors + read_errors)[:5]:
    print(" ", e)

assert not write_errors, f"writer hit errors while explain was reading concurrently: {write_errors[:3]}"
assert not read_errors, f"explain hit errors while a collector was writing concurrently: {read_errors[:3]}"
print("\nPASS: whyslow can run concurrently with an active collector, no lock errors")
