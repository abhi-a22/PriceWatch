"""Local HTTP stub used as a webhook receiver in tests."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Stub:
    """Local HTTP server whose responses are scripted and whose requests are recorded."""

    def __init__(self, statuses):
        self.statuses = list(statuses)  # status per request; last one repeats
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                stub.requests.append((self.path, dict(self.headers), body))
                idx = min(len(stub.requests), len(stub.statuses)) - 1
                status = stub.statuses[idx]
                self.send_response(status)
                if status in (301, 302, 307):
                    self.send_header("Location", "/redirected")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):  # a followed redirect would arrive here
                stub.requests.append((self.path, dict(self.headers), b""))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
