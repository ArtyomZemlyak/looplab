#!/usr/bin/env python3
"""One meter in front of the LLM, for both arms.

WHY THIS EXISTS
---------------
Both arms enforce their per-task budget by reading a cost the PROVIDER reports:

  * arm A  `AlgoTuner/models/lite_llm_model.py::_extract_cost_from_response` -- method 2 is
    `response.usage.cost`, the OpenRouter shape;
  * arm B  `looplab/core/llm.py::_usage_cost` -- the same `usage.cost` key, and
    `cost_is_reported()` deliberately treats an ABSENT cost as unpriced rather than as free.

The corporate gateway (`llm-core-olap.samokat.ru/v1`, SGLang behind it) returns
`usage {prompt_tokens, completion_tokens, total_tokens}` and NO cost. Measured 2026-08-20.
So on that endpoint `spend_limit: 0.02` (arm A) and `LOOPLAB_LLM_BUDGET_USD` (arm B) never bind,
and the budget parity docs/47 5f settles on -- equal SPEND, not equal wall-clock -- silently
becomes no budget at all on either side.

This proxy prices each response from a pinned table (`pricing.json`) and stamps the result into
`usage.cost`, where both arms already look. Neither framework is modified.

It is also the single meter docs/47 5a asks for: one call log, one token count, one dollar figure,
identical for both arms -- which is what makes the reported cost columns comparable.

WHAT IT DOES AND DOES NOT TOUCH
-------------------------------
It does not rewrite prompts, cache, or alter routing. Request body bytes go up unchanged; the
response body comes back unchanged except for `usage.cost` / `usage.cost_basis` / `usage.cost_source`.

It DOES shape traffic, and that is deliberate: one shared RPM budget and one 429-retry policy for
both arms (see `RateLimiter`), because the endpoint's own limit is shared and each framework would
otherwise absorb it with its own private backoff.

A streamed response is forwarded frame by frame and priced from its usage frame; a stream that
carries no usage frame is recorded `metered=false` -- never as $0.

USAGE
-----
    python proxy.py --port 8801 --upstream https://host/v1 --api-key sk-... [--log meter.jsonl]

Arms address it with a path prefix that attributes each call without either framework knowing:

    http://127.0.0.1:8801/m/<arm>/<task>/v1/chat/completions   ->  <upstream>/chat/completions

`/v1/...` also works and is attributed to arm `?`.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "content-encoding",
    "host",
}


class Pricing:
    """The price table, loaded once. A reload would let two arms be priced differently."""

    def __init__(self, path: str):
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        self.path = path
        self.source = raw.get("source", "")
        self.fetched_at = raw.get("fetched_at", "")
        self.reference_slug = raw.get("reference_slug", "")
        self.basis = raw.get("cost_basis", "imputed")
        self.default = raw.get("default") or {}
        self.models = raw.get("models") or {}

    def rate(self, model: str) -> tuple[float, float, str]:
        """(input_per_token, output_per_token, basis) for a model id.

        An unknown model falls back to `default` and says so in the basis, because a silent
        fallback would price a model nobody chose at a price nobody checked.
        """
        entry = self.models.get(model)
        if entry is None:
            # OpenRouter-style ids arrive as `vendor/model`; the gateway serves the bare name.
            entry = self.models.get(model.split("/")[-1])
        if entry is not None:
            return float(entry["input_per_token"]), float(entry["output_per_token"]), self.basis
        return (
            float(self.default.get("input_per_token", 0.0)),
            float(self.default.get("output_per_token", 0.0)),
            self.basis + "-default-fallback",
        )


def _header_cost(headers: dict) -> float | None:
    """A positive cost the upstream reported in a header, or None.

    Zero is NOT a price here: LiteLLM emits `x-litellm-response-cost-original: 0.0` for a model
    group it has no price for, which is the unpriced case, not a free one.
    """
    for key, value in (headers or {}).items():
        if key.lower() == "x-litellm-response-cost-original":
            try:
                amount = float(value)
            except (TypeError, ValueError):
                return None
            return amount if amount > 0 else None
    return None


class RateLimiter:
    """One shared request budget for both arms, because the endpoint applies one.

    Measured 2026-08-20 on the corporate gateway: it is itself a LiteLLM proxy and publishes
    `x-litellm-key-rpm-limit: 50` (team 150). A 20-lane campaign trips that constantly -- a burst of
    20 concurrent calls returned 9 x HTTP 429, and further SEQUENTIAL calls kept 429-ing until the
    window rolled.

    Shaping the traffic HERE rather than letting each arm hit the wall is a parity decision, not
    only a politeness one: AlgoTuner retries with its own backoff and LoopLab with its own, so an
    unshaped 429 storm would charge the two loops differently for the same endpoint condition. One
    queue in front of both makes the endpoint's limit a constant of the experiment instead of a
    variable that correlates with how each framework happens to retry.
    """

    def __init__(self, rpm: int):
        self.rpm = rpm
        self._stamps: list[float] = []
        self._lock = threading.Lock()
        self.waited_s = 0.0

    def acquire(self) -> float:
        """Block until a request slot is free. Returns seconds waited (metered per call)."""
        if self.rpm <= 0:
            return 0.0
        waited = 0.0
        while True:
            with self._lock:
                now = time.time()
                self._stamps = [t for t in self._stamps if now - t < 60.0]
                if len(self._stamps) < self.rpm:
                    self._stamps.append(now)
                    self.waited_s += waited
                    return waited
                sleep_for = max(0.05, 60.0 - (now - self._stamps[0]))
            time.sleep(min(sleep_for, 5.0))
            waited += min(sleep_for, 5.0)


class Meter:
    """Append-only JSONL call log. One row per upstream request, written after it completes."""

    def __init__(self, path: str | None):
        self.path = path
        self._lock = threading.Lock()
        self.calls = 0
        self.cost = 0.0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.errors = 0

    def record(self, row: dict) -> None:
        with self._lock:
            self.calls += 1
            self.cost += float(row.get("cost") or 0.0)
            self.prompt_tokens += int(row.get("prompt_tokens") or 0)
            self.completion_tokens += int(row.get("completion_tokens") or 0)
            if int(row.get("status") or 0) >= 400 or row.get("error"):
                self.errors += 1
            if not self.path:
                return
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError as exc:  # a meter failure must not fail the call it is metering
                print(f"[meter] log write failed: {exc}", file=sys.stderr, flush=True)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "calls": self.calls,
                "cost_usd": round(self.cost, 6),
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "errors": self.errors,
            }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "looplab-meter/1"

    # -- plumbing ---------------------------------------------------------------------------
    def log_message(self, fmt, *args):  # the meter log is the log; keep stderr for real problems
        return

    def _split_path(self) -> tuple[str, str, str]:
        """`/m/<arm>/<task>/v1/x` -> (arm, task, '/v1/x'). Anything else -> ('?', '?', path)."""
        path = self.path
        if path.startswith("/m/"):
            parts = path.split("/", 4)
            if len(parts) >= 5:
                return parts[2], parts[3], "/" + parts[4]
        return "?", "?", path

    def _send(self, status: int, body: bytes, headers: dict) -> None:
        self.send_response(status)
        for key, value in headers.items():
            if key.lower() in HOP_BY_HOP:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, status: int, message: str, arm: str, task: str, t0: float) -> None:
        body = json.dumps({"error": {"message": message, "type": "meter_proxy"}}).encode()
        self.server.meter.record({
            "ts": time.time(), "arm": arm, "task": task, "status": status,
            "latency_ms": round((time.time() - t0) * 1000, 1), "error": message, "metered": False,
        })
        self._send(status, body, {"Content-Type": "application/json"})

    # -- the actual proxy -------------------------------------------------------------------
    def do_GET(self):
        self._proxy(b"")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._proxy(self.rfile.read(length) if length else b"")

    def _proxy(self, body: bytes) -> None:
        t0 = time.time()
        arm, task, tail = self._split_path()

        if tail in ("/healthz", "/m/healthz"):
            self._send(200, json.dumps(self.server.meter.snapshot()).encode(),
                       {"Content-Type": "application/json"})
            return

        upstream = self.server.upstream.rstrip("/")
        if tail.startswith("/v1/"):
            tail = tail[3:]                      # upstream base already ends in /v1
        url = upstream + tail

        streaming = False
        model = ""
        if body:
            try:
                payload = json.loads(body)
                streaming = bool(payload.get("stream"))
                model = str(payload.get("model") or "")
            except (ValueError, AttributeError):
                pass

        req_headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        # Ask upstream for an uncompressed body. The alternative -- forwarding the client's
        # `Accept-Encoding: gzip` -- hands us bytes we would have to decompress before we can find
        # `usage`, and returning them with the encoding header stripped is how a litellm client
        # reads `0x8b` and reports an InternalServerError from a 200 OK.
        req_headers["Accept-Encoding"] = "identity"
        if self.server.api_key:
            req_headers["Authorization"] = "Bearer " + self.server.api_key
        req = urllib.request.Request(url, data=body or None, headers=req_headers,
                                     method=self.command)

        if streaming:
            self._proxy_stream(req, arm, task, tail, model, t0)
            return

        attempts, queued_s = 1, 0.0
        try:
            # No proxy env: the gateway is reached directly, and http_proxy is set on this box
            # for the outside world. urlopen would otherwise send corporate traffic through it.
            resp, attempts, queued_s = self._open_upstream(req)
            with resp:
                status = resp.status
                raw = resp.read()
                resp_headers = {k: v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
            resp_headers = {k: v for k, v in (exc.headers or {}).items()}
        except Exception as exc:  # noqa: BLE001 - upstream failures are data, not crashes
            self._fail(502, f"upstream {type(exc).__name__}: {exc}", arm, task, t0)
            return

        latency_ms = round((time.time() - t0) * 1000, 1)
        row = {
            "ts": time.time(), "arm": arm, "task": task, "path": tail, "model": model,
            "status": status, "latency_ms": latency_ms, "stream": streaming,
            "attempts": attempts, "queued_s": round(queued_s, 2),
        }

        out = raw
        if status < 400:
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if isinstance(data, dict) and isinstance(data.get("usage"), dict):
                usage = data["usage"]
                pin = int(usage.get("prompt_tokens") or 0)
                pout = int(usage.get("completion_tokens") or 0)
                rate_in, rate_out, basis = self.server.pricing.rate(model or data.get("model", ""))
                header_cost = _header_cost(resp_headers)
                if "cost" in usage and usage.get("cost") is not None:
                    # The upstream priced it itself (a real OpenRouter, say). Never overwrite a
                    # provider's own invoice with an imputed one.
                    cost = float(usage["cost"])
                    basis = "upstream"
                elif header_cost is not None:
                    # The corporate gateway is itself LiteLLM and publishes its own figure in
                    # `x-litellm-response-cost-original`. Measured 2026-08-20 it is 0.0 for this
                    # model group (`x-litellm-model-name: openai/default`, i.e. unpriced there too),
                    # so this branch is dormant today -- but if that endpoint ever starts pricing,
                    # its own number outranks anything imputed here.
                    cost = header_cost
                    usage["cost"] = cost
                    basis = "upstream-header"
                else:
                    cost = pin * rate_in + pout * rate_out
                    usage["cost"] = cost
                usage["cost_basis"] = basis
                usage["cost_source"] = self.server.pricing.fetched_at
                out = json.dumps(data).encode()
                row.update({
                    "prompt_tokens": pin, "completion_tokens": pout, "cost": cost,
                    "cost_basis": basis, "metered": True,
                    "model_reported": data.get("model", ""),
                    "id": data.get("id", ""),
                })
                if pout:
                    row["tok_per_s"] = round(pout / max(latency_ms / 1000.0, 1e-6), 2)
            else:
                row["metered"] = False
                row["note"] = "no usage object in response"
        else:
            row["metered"] = False
            try:
                row["upstream_error"] = json.loads(raw).get("error")
            except ValueError:
                row["upstream_error"] = raw[:400].decode("utf-8", "replace")

        self.server.meter.record(row)
        resp_headers["Content-Type"] = resp_headers.get("Content-Type", "application/json")
        if row.get("cost") is not None:
            resp_headers["x-meter-cost-usd"] = f"{row.get('cost', 0.0):.8f}"
        self._send(status, out, resp_headers)


    def _open_upstream(self, req):
        """Shape, send, and absorb a 429 the way BOTH arms should have it absorbed.

        Returns (response, attempts, waited_s) or raises. A 429 is retried here with the endpoint's
        own `Retry-After` when it sends one, so neither framework's private retry policy becomes
        part of the measurement.
        """
        limiter = self.server.limiter
        attempts = 0
        waited = 0.0
        while True:
            attempts += 1
            waited += limiter.acquire()
            try:
                return self.server.opener.open(req, timeout=self.server.timeout), attempts, waited
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempts > self.server.max_retries:
                    raise
                retry_after = (exc.headers or {}).get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 0.0
                except (TypeError, ValueError):
                    delay = 0.0
                if delay <= 0:
                    delay = min(2.0 ** attempts, 30.0)
                exc.close()
                time.sleep(delay)
                waited += delay

    def _proxy_stream(self, req, arm: str, task: str, tail: str, model: str, t0: float) -> None:
        """Forward an SSE stream chunk-by-chunk, pricing the usage frame on its way past.

        LoopLab streams by default (`Settings.llm_stream`) and AlgoTuner does not. Metering only
        the non-streaming half would price one arm and not the other -- and switching LoopLab's
        streaming off to dodge that would change the loop under measurement (its stall watchdogs
        read the token stream). So the usage frame is rewritten in flight instead.

        Frames are forwarded the moment they arrive: a proxy that buffered them would make every
        liveness signal in the run a property of this file.
        """
        row = {"ts": time.time(), "arm": arm, "task": task, "path": tail, "model": model,
               "stream": True}
        try:
            resp, attempts, queued_s = self._open_upstream(req)
            row.update({"attempts": attempts, "queued_s": round(queued_s, 2)})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            row.update({"status": exc.code, "metered": False,
                        "latency_ms": round((time.time() - t0) * 1000, 1),
                        "upstream_error": raw[:400].decode("utf-8", "replace")})
            self.server.meter.record(row)
            self._send(exc.code, raw, {"Content-Type": "application/json"})
            return
        except Exception as exc:  # noqa: BLE001
            self._fail(502, f"upstream {type(exc).__name__}: {exc}", arm, task, t0)
            return

        self.send_response(resp.status)
        for key, value in resp.headers.items():
            if key.lower() in HOP_BY_HOP:
                continue
            self.send_header(key, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def emit(payload: bytes) -> None:
            self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
            self.wfile.flush()

        pin = pout = 0
        cost = 0.0
        basis = ""
        try:
            for line in resp:
                out = line
                if line.startswith(b"data: ") and not line.startswith(b"data: [DONE]"):
                    try:
                        frame = json.loads(line[6:])
                    except ValueError:
                        frame = None
                    if isinstance(frame, dict) and isinstance(frame.get("usage"), dict):
                        usage = frame["usage"]
                        pin = int(usage.get("prompt_tokens") or 0)
                        pout = int(usage.get("completion_tokens") or 0)
                        rate_in, rate_out, basis = self.server.pricing.rate(
                            model or frame.get("model", ""))
                        if usage.get("cost") is not None:
                            cost, basis = float(usage["cost"]), "upstream"
                        else:
                            cost = pin * rate_in + pout * rate_out
                            usage["cost"] = cost
                        usage["cost_basis"] = basis
                        usage["cost_source"] = self.server.pricing.fetched_at
                        out = b"data: " + json.dumps(frame).encode() + b"\n"
                emit(out)
            emit(b"")           # terminating zero-length chunk
            self.wfile.flush()
        except Exception as exc:  # noqa: BLE001 - a broken client must not take the server down
            row["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            resp.close()

        latency_ms = round((time.time() - t0) * 1000, 1)
        row.update({"status": resp.status, "latency_ms": latency_ms, "prompt_tokens": pin,
                    "completion_tokens": pout, "cost": cost, "cost_basis": basis,
                    "metered": bool(basis)})
        if not basis:
            # No usage frame arrived: the client did not ask for one (`stream_options.
            # include_usage`). Unpriced, and recorded as unpriced -- never as $0.
            row["note"] = "streamed response carried no usage frame"
        if pout:
            row["tok_per_s"] = round(pout / max(latency_ms / 1000.0, 1e-6), 2)
        self.server.meter.record(row)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8801)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--upstream", default=os.environ.get("METER_UPSTREAM", ""),
                    help="upstream OpenAI-compatible base URL, ending in /v1")
    ap.add_argument("--api-key", default=os.environ.get("METER_API_KEY", ""),
                    help="bearer token for the upstream; replaces whatever the client sent")
    ap.add_argument("--pricing", default=os.path.join(here, "pricing.json"))
    ap.add_argument("--log", default=os.environ.get("METER_LOG", ""),
                    help="JSONL call log; one row per upstream request")
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--rpm", type=int, default=int(os.environ.get("METER_RPM", "45")),
                    help="requests/minute allowed upstream, shared by both arms (0 = unlimited). "
                         "The gateway publishes x-litellm-key-rpm-limit: 50.")
    ap.add_argument("--max-retries", type=int, default=5,
                    help="429 retries absorbed here instead of by each framework's own policy")
    args = ap.parse_args()

    if not args.upstream:
        print("--upstream is required (or set METER_UPSTREAM)", file=sys.stderr)
        return 2

    pricing = Pricing(args.pricing)
    server = Server((args.host, args.port), Handler)
    server.upstream = args.upstream
    server.api_key = args.api_key
    server.pricing = pricing
    server.timeout = args.timeout
    server.meter = Meter(args.log or None)
    server.limiter = RateLimiter(args.rpm)
    server.max_retries = args.max_retries
    # Corporate gateway is inside the perimeter; the box's http_proxy is for the outside world.
    server.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    print(f"[meter] {args.host}:{args.port} -> {args.upstream}", flush=True)
    print(f"[meter] pricing {pricing.reference_slug} in={pricing.default.get('input_per_token')} "
          f"out={pricing.default.get('output_per_token')} basis={pricing.basis} "
          f"fetched_at={pricing.fetched_at}", flush=True)
    print(f"[meter] log {args.log or '(none)'}", flush=True)
    print(f"[meter] rpm cap {args.rpm or 'unlimited'} | 429 retries {args.max_retries}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print(f"[meter] final {json.dumps(server.meter.snapshot())}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
