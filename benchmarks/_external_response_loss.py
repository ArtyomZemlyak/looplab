"""Owned loopback proxy: lose replies only AFTER the private UI has answered.

No request retry, response synthesis, token logging or production server changes.
Used by the real SGD/MCP acceptance fixture, never by LoopLab at runtime.
"""
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
from urllib.parse import urlsplit


class ResponseLossProxy:
    def __init__(self, upstream):
        target = urlsplit(upstream)
        assert target.hostname == "127.0.0.1"
        self.drop_next_read = False
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
                        lose_read = self.command == "GET" and fixture.drop_next_read
                        if lose_write or lose_read:
                            fixture.drop_next_read = False
                            if lose_write:
                                fixture.drop_next_write = None
                            fixture.dropped.append({"method": self.command, "route": self.path.rsplit("/", 1)[-1].split("?", 1)[0],
                                                    "upstream_status": response.status})
                            self.close_connection = True
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
