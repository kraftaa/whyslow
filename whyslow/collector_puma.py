import time
import json
import urllib.request

from .storage import Store


class PumaCollector:
    """Polls Puma's control-app /stats endpoint (activate_control_app in
    config/puma.rb) for thread-pool saturation. Requires no Ruby-side code
    beyond enabling the control app -- this is a plain HTTP client."""

    def __init__(self, host_name, stats_url, store: Store, interval=1.0, auth_token=None):
        self.host_name = host_name
        self.stats_url = stats_url
        self.store = store
        self.interval = interval
        self.auth_token = auth_token

    def poll_once(self):
        req = urllib.request.Request(self.stats_url)
        if self.auth_token:
            req.add_header("Authorization", f"Bearer {self.auth_token}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())

        # Puma's /stats shape: single-mode has these fields at top level;
        # clustered mode nests them under "worker_status" per worker.
        # Report the WORST worker, not a sum -- one saturated worker among
        # several idle ones is a real, distinct failure mode that summing
        # would hide (e.g. 1 worker at backlog=20, 3 idle -> sum looks like
        # moderate average load instead of one worker actually maxed out).
        if "worker_status" in data:
            worst_backlog = 0
            worst_pool_capacity = None
            total_running = 0
            max_threads = 0
            for w in data["worker_status"]:
                s = w.get("last_status", {})
                total_running += s.get("running", 0)
                max_threads = max(max_threads, s.get("max_threads", 0))
                worst_backlog = max(worst_backlog, s.get("backlog", 0))
                pc = s.get("pool_capacity", 0)
                worst_pool_capacity = pc if worst_pool_capacity is None else min(worst_pool_capacity, pc)
            running = total_running
            pool_capacity = worst_pool_capacity if worst_pool_capacity is not None else 0
            backlog = worst_backlog
        else:
            running = data.get("running", 0)
            pool_capacity = data.get("pool_capacity", 0)
            max_threads = data.get("max_threads", 0)
            backlog = data.get("backlog", 0)

        self.store.write_puma_stat(
            host=self.host_name,
            backlog=backlog,
            pool_capacity=pool_capacity,
            max_threads=max_threads,
            running=running,
            # Puma's control-app stats do not expose worker RSS. Reading
            # /proc/self/status here used to report the Python collector's
            # RSS as if it belonged to Puma, which was actively misleading.
            rss_mb=None,
        )
        return backlog, pool_capacity

    def run_forever(self):
        print(f"[whyslow] puma collector polling {self.stats_url} every {self.interval}s")
        consecutive_failures = 0
        while True:
            try:
                backlog, pool_capacity = self.poll_once()
                self.store.write_heartbeat(
                    f"puma:{self.host_name}",
                    detail=f"backlog={backlog} pool_capacity={pool_capacity}",
                    expected_interval=self.interval,
                )
                if consecutive_failures:
                    print(f"[whyslow] {self.host_name}: recovered after "
                          f"{consecutive_failures} failed poll(s)")
                    consecutive_failures = 0
                print(f"[whyslow] {self.host_name}: backlog={backlog} pool_capacity={pool_capacity}")
                time.sleep(self.interval)
            except KeyboardInterrupt:
                break
            except Exception as e:
                # A restarting Puma, or a control app briefly unavailable,
                # must not kill the collector -- it previously did.
                consecutive_failures += 1
                if consecutive_failures <= 3 or consecutive_failures % 60 == 0:
                    print(f"[whyslow] {self.host_name}: poll failed "
                          f"({consecutive_failures}x): {e}")
                time.sleep(min(self.interval * consecutive_failures, 30))
