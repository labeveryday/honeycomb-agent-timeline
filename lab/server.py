"""Local test server: the portal, the inventory API, and the busy deployment API. Fake data, real HTTP."""
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIXTURES = json.loads(Path(__file__).with_name("fixtures.json").read_text())


@contextmanager
def start_lab():
    state = {"inventory_path": FIXTURES["deployment"]["diff"]["INVENTORY_BASE_URL"]["after"], "busy_until": None, "requests": {},
             "deployment": FIXTURES["deployment"], "logs": FIXTURES["logs"]}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"][self.path] = state["requests"].get(self.path, 0) + 1
            now = time.monotonic()
            if self.path == "/deployment" and state["busy_until"] is None:
                # Everyone hits the deployment API during an incident, so it starts out busy.
                # Retry-After is whole seconds, so the server rounds this half second up to 1.
                state["busy_until"] = now + 0.5
            status = 200
            payload = {"evidence_id": self.path, "status": "healthy"}
            if self.path == "/deployment" and now < state["busy_until"]:
                status, payload = 429, {"error": "deployment history API is busy"}
            elif self.path == "/dns":
                payload = FIXTURES["dns"]
            elif self.path in ("/logs", "/deployment"):
                payload = state[self.path[1:]]
            elif self.path == "/portal":
                healthy = state["inventory_path"] == "/inventory/v2"
                status = 200 if healthy else 502
                # Like a real health endpoint, the portal reports the dependency it calls.
                payload = {"evidence_id": "HTTP-PORTAL", "inventory_lookup": "ok" if healthy else "failed",
                           "upstream": "http://inventory.lab" + state["inventory_path"], "upstream_status": 200 if healthy else 410}
            elif self.path == "/inventory/v1":
                status, payload = 410, {"evidence_id": "HTTP-V1", "error": "endpoint retired"}
            elif self.path != "/inventory/v2":
                status, payload = 404, {"error": "not found"}
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if status == 429:
                self.send_header("Retry-After", "1")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def roll_back(state):
    """The human applies the fix as a new deploy, so the evidence records it too."""
    state["inventory_path"] = FIXTURES["rollback"]["diff"]["INVENTORY_BASE_URL"]["after"]
    state["deployment"] = FIXTURES["rollback"]
    state["logs"] = {**FIXTURES["logs"], "entries": FIXTURES["logs"]["entries"] + [FIXTURES["rollback_log"]]}
