"""Owned loopback proxy: lose or replace replies AFTER the private UI has answered.

No request retry, token logging or production server changes. Replacement responses
are explicit fixture faults; they never count as measured evidence.
Used by the real SGD/MCP acceptance fixture, never by LoopLab at runtime.
"""
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import gzip
import json
import socket
import threading
from urllib.parse import urlsplit
from looplab.harness.mcp_server import MAX_RESPONSE_BYTES


class ResponseLossProxy:
    def __init__(self, upstream, mode="disconnect", read_mode="disconnect"):
        assert mode in ("disconnect", "invalid_json", "oversized", "server_error")
        assert read_mode in ("disconnect", "stale_generation", "wrong_receipt", "incomplete_result_page", "incomplete_receipt", "incomplete_progress")
        target = urlsplit(upstream)
        assert target.hostname == "127.0.0.1"
        self.drop_next_read = False
        self.catalog_fault = None
        self.drop_next_write = "/api/runs/demo/commands"
        self.dropped = []
        self._lock = threading.Lock()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # Fixture credentials and request bodies never enter proxy logs.

            def forward(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                headers = {k: v for k, v in self.headers.items()
                           if k.lower() not in {"host", "connection", "content-length"}}
                connection = HTTPConnection(target.hostname, target.port, timeout=30)
                try:
                    connection.request(self.command, self.path, body, headers)
                    response = connection.getresponse()
                    payload = response.read()
                    with fixture._lock:
                        lose_write = self.command == "POST" and self.path == fixture.drop_next_write
                        lose_read = (self.command == "GET" and fixture.drop_next_read
                                     and (read_mode != "incomplete_progress" or
                                          self.path.split("?", 1)[0].endswith("/harness-progress")))
                        lose_catalog = self.command == "GET" and self.path == "/openapi.json" and fixture.catalog_fault is not None
                        if lose_write or lose_read or lose_catalog:
                            fixture.drop_next_read = False
                            if lose_write:
                                fixture.drop_next_write = None
                            fixture.dropped.append({"method": self.command, "route": self.path.rsplit("/", 1)[-1].split("?", 1)[0],
                                                    "upstream_status": response.status})
                            self.close_connection = True
                            if lose_catalog:
                                fault, fixture.catalog_fault = fixture.catalog_fault, None
                                if fault == "disconnect":
                                    self.connection.shutdown(socket.SHUT_RDWR)
                                    return
                                assert fault in ("invalid_json", "invalid_catalog")
                                payload = b"<html>Disposable catalog fault</html>" if fault == "invalid_json" else b'{"paths":[]}'
                                self.send_response(response.status)
                                self.send_header("Content-Length", str(len(payload)))
                                self.send_header("Content-Type", "application/json")
                                self.end_headers()
                                self.wfile.write(payload)
                                return
                            if lose_read and read_mode != "disconnect":
                                if response.getheader("Content-Encoding", "").lower() == "gzip":
                                    payload = gzip.decompress(payload)
                                value = json.loads(payload)
                                if read_mode == "stale_generation":
                                    value["generation"] = "f" * 64
                                elif read_mode == "incomplete_result_page":
                                    del value["items"]
                                elif read_mode == "incomplete_receipt":
                                    del value["terminal"]
                                elif read_mode == "incomplete_progress":
                                    del value["candidate_decisions_per_idea"]
                                else:
                                    value["command"]["id"] = "cmd_" + "f" * 32
                                payload = json.dumps(value).encode("utf8")
                                self.send_response(response.status)
                                self.send_header("Content-Length", str(len(payload)))
                                self.send_header("Content-Type", "application/json")
                                self.end_headers()
                                self.wfile.write(payload)
                                return
                            if lose_write and mode != "disconnect":
                                if mode == "invalid_json":
                                    payload = b"<html>Disposable invalid acknowledgement</html>"
                                elif mode == "oversized":
                                    payload = b'{"padding":"' + b"x" * MAX_RESPONSE_BYTES + b'"}'
                                else:
                                    payload = b'{"detail":"Disposable proxy error after upstream acceptance"}'
                                self.send_response(503 if mode == "server_error" else 200)
                                self.send_header("Content-Length", str(len(payload)))
                                self.send_header("Content-Type", "text/html" if mode == "invalid_json" else "application/json")
                                self.end_headers()
                                self.wfile.write(payload)
                                return
                            self.connection.shutdown(socket.SHUT_RDWR)
                            return
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in {"content-length", "transfer-encoding", "connection"}:
                            self.send_header(key, value)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                finally:
                    connection.close()

            do_GET = forward
            do_POST = forward

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()
