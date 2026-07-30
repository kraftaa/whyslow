import http.server
import json
import threading


SINGLE_MODE_STATS = {
    "started_at": "2026-07-28T00:00:00Z",
    "backlog": 5,
    "running": 10,
    "pool_capacity": 6,
    "max_threads": 16,
    "requests_count": 12345,
}

# One worker saturated (backlog=20, pool_capacity=0), three idle -- summing
# these would look like moderate average load and hide the real problem.
CLUSTERED_MODE_STATS = {
    "started_at": "2026-07-28T00:00:00Z",
    "workers": 4,
    "worker_status": [
        {"pid": 100, "last_status": {"backlog": 20, "running": 16, "pool_capacity": 0, "max_threads": 16}},
        {"pid": 101, "last_status": {"backlog": 5, "running": 4, "pool_capacity": 12, "max_threads": 16}},
        {"pid": 102, "last_status": {"backlog": 0, "running": 1, "pool_capacity": 15, "max_threads": 16}},
        {"pid": 103, "last_status": {"backlog": 0, "running": 1, "pool_capacity": 15, "max_threads": 16}},
    ],
}


class FakePumaHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "single"  # class attribute, set before starting the server

    def do_GET(self):
        if self.path.startswith("/stats"):
            payload = json.dumps(
                SINGLE_MODE_STATS if FakePumaHandler.mode == "single" else CLUSTERED_MODE_STATS
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.send_header("Connection", "close")
            self.end_headers()
        self.close_connection = True

    def log_message(self, *a):
        pass


def start(port, mode="single"):
    FakePumaHandler.mode = mode
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), FakePumaHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv
