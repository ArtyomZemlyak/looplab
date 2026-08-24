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
and the budget parity docs/50 5f settles on -- equal SPEND, not equal wall-clock -- silently
becomes no budget at all on either side.

This proxy prices each response from a pinned table (`pricing.json`) and stamps the result into
`usage.cost`, where both arms already look. Neither framework is modified.

It is also the single meter docs/50 5a asks for: one call log, one token count, one dollar figure,
identical for both arms -- which is what makes the reported cost columns comparable.

WHAT IT DOES AND DOES NOT TOUCH
-------------------------------
It does not rewrite prompts, cache, or alter routing. Request body bytes go up unchanged; the
response body comes back unchanged except for `usage.cost` / `usage.cost_basis` / `usage.cost_source`.

It DOES shape traffic, and that is deliberate: one shared RPM budget and one 429-retry policy for
both arms (see `RateLimiter`), because the endpoint's own limit is shared and each framework would
otherwise absorb it with its own private backoff.

A streamed response is forwarded frame by frame and priced from its usage frame; a stream the
GATEWAY cut without one is priced from the content deltas it forwarded, labelled
`cost_basis: estimated_from_deltas`; a stream that produced neither is recorded `metered=false`
-- never as $0.

    OPEN[meter-delta-estimator-is-uncalibrated] `estimated_from_deltas` charges ONE token per
    content delta; this proxy's own log says that is low by a length-dependent factor. Over 4,874
    complete streams carrying both numbers, deltas/completion_tokens has median 0.156 (<100 tokens),
    0.803 (1k-5k) and 0.996 (>20k), and the counter is blind to `delta.tool_calls` entirely.
    DEFERRED: the 23 aborted streams on record are 6.9 % of a live campaign's $21.21 of metered
    spend, so re-pricing them mid-campaign charges the tasks before and after the change by two
    different instruments. The rate is derivable in-process from streams already priced.
    proof:absent:tokens_per_delta@benchmarks/meter/proxy.py

USAGE
-----
    python proxy.py --port 8801 --upstream https://host/v1 --api-key sk-... [--log meter.jsonl]

Arms address it with a path prefix that attributes each call without either framework knowing:

    http://127.0.0.1:8801/m/<arm>/<task>/<attempt>/v1/chat/completions -> <upstream>/chat/completions

`/v1/...` also works and is attributed to arm `?`.

WHAT AN ATTEMPT SEGMENT IS FOR, AND HOW TO READ A LOG WRITTEN BEFORE IT EXISTED
------------------------------------------------------------------------------
`(arm, task)` is not an identity: a task-arm gets RE-RUN, and until 2026-08-23 every attempt at one
task added to one bucket. Measured on `/var/tmp/looplab-bench/meter/meter.jsonl`: `B/kcenters`
holds **$2.0086 over 816 calls** against ONE `.done` marker whose run really cost **$1.0070**, so a
naive per-task sum reads 2x the $1.00 ceiling and looks like a budget breach that never happened --
a colleague read it as one. `B/discrete_log` is $1.4749 over 526 calls and
`B/count_riemann_zeta_zeros` $0.8386 over 127.

The third segment is that identity, and it is **the campaign's**, not this proxy's:
`benchmarks/algotune/campaign.sh::next_attempt` allocates `a1`, `a2`, ... per task-arm, appends the
allocation to `$CAMPAIGN_OUT/<arm>-<task>.attempts`, stamps it into the `.done` marker as
`attempt=aN`, and puts it in the URL. A proxy that invented one instead (a start-up counter, a
first-seen-at timestamp) would renumber itself on every restart and could not be joined to a marker.
So this file only ever COPIES what the path says, and copies the empty string when the path says
nothing -- `attempt` is never synthesised here.

Both forms are accepted, and the two-segment one is not deprecated: `docs/52`, `setup_gateway_arm.py`
and any hand-built curl still spell it, and a metered call that arrives on the short path must be
metered, not refused. It is recorded with `"attempt": ""`.

**Reading the 9,456-row log that predates this**: rows written before the change carry NO `attempt`
key at all, and `row.get("attempt")` returning `None` (key absent) rather than `""` (key present,
caller named no attempt) is exactly that distinction -- do not collapse them. Such a row is
attributable to `(arm, task)` and to nothing finer, and **the attempts inside it are not
recoverable**: the only handle left is a gap heuristic over `ts`, and its answer is whatever
threshold you picked. `B/count_riemann_zeta_zeros` splits into 19 / 16 / 14 / 12 / 2 sessions at a
5 / 10 / 15 / 20 / 40-minute gap. So sum an OLD log per `(arm, task)` and label it "all attempts";
sum a NEW one per `(arm, task, attempt)` and label it with the marker's own `attempt=`. Do not mix
the two in one total -- a task whose log spans the change has both shapes, and the honest split is
"before" and "after", not a re-sessionised guess.
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

# The `finish_reason` this proxy stamps on a completion it watched the gateway CUT -- one word for
# both transports, the streamed frame and the reassembled body, because the two saying different
# things about the same cut is half of the defect this constant was added for.
#
# It is deliberately the SAME word `looplab/core/llm.py::STREAM_TRUNCATED_FINISH_REASON` already
# stamps on its own salvaged envelopes, for the reason stated there: not OpenAI's `"length"`, which
# names a token limit the MODEL ran into and would assert something false about the generation on
# every gateway cut. This file is a standalone script (it imports no `looplab`), so the word is
# COPIED rather than imported -- `tests/test_meter_stream_adapter.py` asserts the two spellings are
# equal, so the copy cannot drift in silence.
#
# Neither arm reads the VALUE, which is what makes the honest word free: LoopLab reads only a
# finish_reason's PRESENCE (`_envelope_is_truncated`, `_keepalive_stall`), and AlgoTuner names
# `finish_reason` only in a retry-pattern list matched against ERROR STRINGS. Measured 2026-08-24
# against that arm's own litellm 1.97.0: `ModelResponse` accepts it, logs `Unmapped finish_reason
# 'truncated', defaulting to 'stop'` and normalises it away. So the mark is honest on the wire, in
# the meter row and to a human reading either -- and the half of this fix arm A's ACCOUNTANT reads
# is the `usage` beside it, never this word.
STREAM_TRUNCATED_FINISH_REASON = "truncated"


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

    def _split_path(self) -> tuple[str, str, str, str]:
        """`/m/<arm>/<task>/<attempt>/v1/x` -> (arm, task, attempt, '/v1/x').

        THE TWO-SEGMENT FORM STILL WORKS and yields `attempt=''`. That is not a courtesy: `docs/52`
        and `setup_gateway_arm.py` document the short path, and refusing a call because its URL is
        the old shape would drop a real, paid request out of the ledger -- the one thing this file
        must never do.

        The two are told apart by the segment itself, not by a length count, because both forms have
        the same number of `/` once the tail is long enough: `/m/A/t/v1/chat/completions` and
        `/m/A/t/a3/v1/chat` both split into six. `/v1` is where the UPSTREAM path begins, so a
        fourth segment that is not `v1` is an attempt id and a fourth segment that IS `v1` is the
        tail. An attempt id may therefore not be spelled `v1`; `campaign.sh` spells them `a<N>`.
        """
        path = self.path
        if path.startswith("/m/"):
            parts = path.split("/", 5)          # ['', 'm', arm, task, seg, rest]
            if len(parts) >= 6 and parts[4] != "v1":
                return parts[2], parts[3], parts[4], "/" + parts[5]
            if len(parts) >= 5:
                return parts[2], parts[3], "", "/" + "/".join(parts[4:])
        return "?", "?", "", path

    def _send(self, status: int, body: bytes, headers: dict) -> None:
        self.send_response(status)
        for key, value in headers.items():
            if key.lower() in HOP_BY_HOP:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, status: int, message: str, arm: str, task: str, attempt: str,
              t0: float) -> None:
        body = json.dumps({"error": {"message": message, "type": "meter_proxy"}}).encode()
        self.server.meter.record({
            "ts": time.time(), "arm": arm, "task": task, "attempt": attempt, "status": status,
            "latency_ms": round((time.time() - t0) * 1000, 1), "error": message, "metered": False,
        })
        self._send(status, body, {"Content-Type": "application/json"})

    # -- the actual proxy -------------------------------------------------------------------
    def do_GET(self):
        self._proxy(b"")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._proxy(self.rfile.read(length) if length else b"")

    # THE NGINX ASYMMETRY, and why this exists.
    #
    # The gateway in front of the models is nginx with `proxy_read_timeout` at 300 s: measured
    # 2026-08-23, forty non-streaming arm-A calls returned `504 Gateway Time-out` at latency
    # 300.011 s each, byte-identical nginx HTML. A STREAM keeps bytes flowing, so it survives to the
    # model server's own ~1800 s limit -- which is why arm B (100% streaming) never saw a 504 and
    # arm A (0% streaming) saw forty. One task-arm, `count_riemann_zeta_zeros`, spent three and a
    # half of its four wall-clock hours inside those timeouts and reached $0.14 of its $1.00.
    #
    # That is a difference between TRANSPORTS, not between agents, and neither framework chose it.
    # Fixing it inside either one would mean editing a thing under measurement, so it is fixed here:
    # the meter asks upstream for a stream on the client's behalf and hands back the ordinary
    # non-streaming JSON the client asked for. Both arms then face the same gateway window.
    #
    # OFF BY DEFAULT (`METER_STREAM_ADAPT=1` to enable). Turning it on mid-campaign would split an
    # arm into two halves measured through two transports, which is the defect it exists to remove.
    def _adapt_to_stream(self, payload: dict) -> tuple:
        """`(upstream_body, adapted)` — the same request, asked for as a stream."""
        if os.environ.get("METER_STREAM_ADAPT") != "1" or payload.get("stream"):
            return None, False
        out = dict(payload)
        out["stream"] = True
        opts = dict(out.get("stream_options") or {})
        opts["include_usage"] = True             # or the reassembled answer carries no usage at all
        out["stream_options"] = opts
        return json.dumps(out).encode(), True

    @staticmethod
    def _reassemble(frames: list) -> dict:
        """One non-streaming completion, rebuilt from the deltas of a streamed one.

        Content, REASONING, role, finish_reason, usage and tool_call fragments, in arrival order. A
        field this does not know about is carried from the LAST frame that had it, so an unfamiliar
        extension survives instead of being silently dropped -- the client is the framework under
        measurement and must see what the provider sent.

        Reasoning is carried under the delta's OWN key (`reasoning_content` here, `reasoning` on an
        OpenRouter-shaped stream) rather than folded into `content`, because they are different
        fields to every reader and merging them would put a model's private thinking into the answer
        the arm parses for commands. It has to be carried at all for the reason this whole path
        exists: a reasoning model the gateway cuts mid-think has spent EVERYTHING on
        `reasoning_content` with `content` still empty -- one real cut on this box carried 220,685
        deltas, all of them reasoning -- so dropping it handed the client an empty answer for a call
        that had produced a great deal. `looplab/core/llm.py::salvaged_lengths` is the same reading
        one layer up, and the arm's own litellm keeps the key (measured 2026-08-24, 1.97.0).
        """
        content: list = []
        reasoning: dict = {}
        tool_calls: dict = {}
        finish = None
        usage = None
        base: dict = {}
        for f in frames:
            if not isinstance(f, dict):
                continue
            for k, v in f.items():
                if k not in ("choices", "usage", "object"):
                    base[k] = v
            if isinstance(f.get("usage"), dict):
                usage = f["usage"]
            for ch in (f.get("choices") or []):
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
                d = ch.get("delta") or {}
                if d.get("content"):
                    content.append(d["content"])
                for key in ("reasoning_content", "reasoning"):
                    if d.get(key):
                        reasoning.setdefault(key, []).append(d[key])
                for tc in (d.get("tool_calls") or []):
                    idx = tc.get("index", 0)
                    slot = tool_calls.setdefault(idx, {"index": idx, "type": tc.get("type", "function"),
                                                       "id": tc.get("id"),
                                                       "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
        message = {"role": "assistant", "content": "".join(content)}
        for key, parts in reasoning.items():
            message[key] = "".join(parts)
        if tool_calls:
            message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        out = dict(base)
        out["object"] = "chat.completion"
        out["choices"] = [{"index": 0, "message": message, "finish_reason": finish}]
        if usage is not None:
            out["usage"] = usage
        return out

    def _proxy(self, body: bytes) -> None:
        t0 = time.time()
        arm, task, attempt, tail = self._split_path()

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
            self._proxy_stream(req, arm, task, attempt, tail, model, t0)
            return

        # The client asked for a whole answer. With the adapter on, ask UPSTREAM for a stream (so
        # nginx's 300 s read window never fires) and give the client the whole answer regardless.
        adapted_body, adapted = (None, False)
        if tail.endswith("/chat/completions"):
            try:
                adapted_body, adapted = self._adapt_to_stream(json.loads(body) if body else {})
            except (ValueError, TypeError):
                adapted_body, adapted = None, False
        if adapted:
            req = urllib.request.Request(url, data=adapted_body, headers=req_headers,
                                         method=self.command)
            self._proxy_stream(req, arm, task, attempt, tail, model, t0, collect_for_client=True)
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
            self._fail(502, f"upstream {type(exc).__name__}: {exc}", arm, task, attempt, t0)
            return

        latency_ms = round((time.time() - t0) * 1000, 1)
        row = {
            "ts": time.time(), "arm": arm, "task": task, "attempt": attempt, "path": tail,
            "model": model, "status": status, "latency_ms": latency_ms, "stream": streaming,
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

    def _proxy_stream(self, req, arm: str, task: str, attempt: str, tail: str, model: str,
                      t0: float, *, collect_for_client: bool = False) -> None:
        """Forward an SSE stream chunk-by-chunk, pricing the usage frame on its way past.

        LoopLab streams by default (`Settings.llm_stream`) and AlgoTuner does not. Metering only
        the non-streaming half would price one arm and not the other -- and switching LoopLab's
        streaming off to dodge that would change the loop under measurement (its stall watchdogs
        read the token stream). So the usage frame is rewritten in flight instead.

        Frames are forwarded the moment they arrive: a proxy that buffered them would make every
        liveness signal in the run a property of this file.
        """
        row = {"ts": time.time(), "arm": arm, "task": task, "attempt": attempt, "path": tail,
               "model": model, "stream": True}
        if collect_for_client:
            # The client is NOT expecting SSE. Frames are collected here and answered as one
            # JSON body, so nothing about this is visible to it except that the call did not
            # time out at nginx's 300 s read window.
            row["stream_adapted"] = True
        collected: list = []
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
            self._fail(502, f"upstream {type(exc).__name__}: {exc}", arm, task, attempt, t0)
            return

        if not collect_for_client:
            self.send_response(resp.status)
            for key, value in resp.headers.items():
                if key.lower() in HOP_BY_HOP:
                    continue
                self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

        # NOTHING A CLIENT MUST SEE MAY LEAVE THROUGH `emit()` ALONE. It is the SSE writer and it
        # returns immediately while collecting, so a frame this proxy MINTS -- as opposed to one it
        # forwards, which `collected` already holds -- reaches the streaming client and nobody else.
        # That is exactly how the `estimated_from_deltas` usage frame below came to be dropped on
        # the one path that had no other copy of it. Mint into a variable, then route it BOTH ways.
        def emit(payload: bytes) -> None:
            if collect_for_client:      # the client gets ONE JSON body at the end, not frames
                return
            self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
            self.wfile.flush()

        pin = pout = 0
        cost = 0.0
        basis = ""
        # Did the GATEWAY send a usage frame? Its own flag rather than `bool(basis)`, because the
        # synthesis below overwrites `basis` and the reassembled body has to be able to tell "the
        # provider closed the books on this call" from "we closed them for it".
        usage_frame_seen = False
        # Content deltas SEEN, counted as they pass. They are the only evidence left when a stream
        # ends without its usage frame, and measured 2026-08-22 that is not a corner case: this
        # gateway CUTS a generation at ~1800 s (eight streams ended at 1817-1824 s, status 200, no
        # exception on our side and therefore not our socket timeout). LoopLab prices from
        # `usage.cost`, so a stream with no usage frame produced NO `llm_usage` event at all -- the
        # run's own accounting was short four calls on one task and never said so. A budget that
        # silently under-counts is worse than one that stops early: the arm looks cheap.
        deltas = 0
        try:
            for line in resp:
                out = line
                if line.startswith(b"data: ") and not line.startswith(b"data: [DONE]"):
                    try:
                        frame = json.loads(line[6:])
                    except ValueError:
                        frame = None
                    if isinstance(frame, dict) and collect_for_client:
                        collected.append(frame)
                    if isinstance(frame, dict):
                        for ch in (frame.get("choices") or []):
                            d = ch.get("delta") or {}
                            if d.get("content") or d.get("reasoning_content"):
                                deltas += 1
                    if isinstance(frame, dict) and isinstance(frame.get("usage"), dict):
                        usage_frame_seen = True
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
            if usage_frame_seen:
                pass                            # the gateway priced it; nothing to estimate
            elif not basis and deltas:
                # THE STREAM PRODUCED TOKENS AND NOBODY PRICED THEM. Synthesise the usage frame the
                # gateway did not send, from the deltas actually forwarded, and label it for what it
                # is. Injected rather than merely logged because the arm's accountant reads the
                # stream, not this file: logging it here would fix the report and leave the run's own
                # `llm_budget_usd` blind, which is the defect, not a smaller version of it.
                #
                # WHAT ONE DELTA IS WORTH, measured against this proxy's OWN log rather than assumed.
                # This block used to assert "one delta per token is the SSE shape every provider on
                # this box emits". That is false as a general claim and the evidence is in
                # `meter/meter.jsonl`: 4,874 COMPLETE streams carry both `deltas_seen` and the
                # gateway's authoritative `completion_tokens`, and the ratio has a strong length
                # dependence -- median 0.156 below 100 completion tokens, 0.384 at 100-1k, 0.803 at
                # 1k-5k, 0.981 at 5k-20k and 0.996 above 20k (overall median 0.476, and deltas are
                # BELOW completion_tokens on 99.88 % of rows). The short-stream gap is structural,
                # not noise: this counter reads `delta.content` / `delta.reasoning_content` and a
                # completion delivered as a TOOL CALL arrives on `delta.tool_calls[].function
                # .arguments`, which it never sees. 505 complete streams of >500 completion tokens
                # spent under 20 % of them on content deltas -- 1.17 M tokens this estimator would
                # have valued at less than a fifth.
                #
                # It is still a FLOOR in every one of those directions, which is the honest side to
                # be wrong on for a budget, and the 23 aborts actually on record are all long
                # runaway generations (226k-238k deltas at 1817-1830 s) where the ratio is ~1.0. But
                # a floor that is 5x low on a tool-call stream is a different instrument from the one
                # the old comment described, so the number is not corrected here: the open item
                # in this module's docstring holds the measurement and says why it is deferred.
                rate_in, rate_out, est_basis = self.server.pricing.rate(model or "")
                pin, pout = 0, deltas
                cost = deltas * rate_out
                basis = "estimated_from_deltas"
                # BUILT ONCE, DELIVERED BY WHICHEVER ROUTE THE CLIENT IS ON. Writing it through
                # `emit()` alone is how it came to be lost: `emit()` returns immediately when the
                # client is being collected for, so the adapted (non-streaming) client got a body
                # with no `usage` at all and a null finish reason — a truncated answer arriving as a
                # clean 200 with nothing for that arm's accountant to read. That is the very leak the
                # estimate exists to close, one layer up, and it was untested because the fake
                # upstream in the guard test always sent a usage frame.
                estimate = {
                    "id": "meter-estimate", "object": "chat.completion.chunk", "model": model,
                    "choices": [{"index": 0, "delta": {},
                                 "finish_reason": STREAM_TRUNCATED_FINISH_REASON}],
                    "usage": {
                        "prompt_tokens": 0, "completion_tokens": deltas, "total_tokens": deltas,
                        "cost": cost, "cost_basis": basis,
                        "cost_source": self.server.pricing.fetched_at,
                        "meter_note": "upstream ended the stream without a usage frame; "
                                      "completion_tokens is a FLOOR counted from forwarded deltas"},
                }
                if collect_for_client:
                    collected.append(estimate)
                else:
                    emit(b"data: " + json.dumps(estimate).encode() + b"\n\n")
            emit(b"")           # terminating zero-length chunk
            if collect_for_client:
                # ONE body, built from the frames, with the usage frame this proxy already priced
                # in place — so the client sees exactly the shape it asked for and the ledger sees
                # exactly what it would have seen either way.
                whole = json.dumps(self._reassemble(collected)).encode()
                self._send(resp.status, whole, {"Content-Type": "application/json"})
            else:
                self.wfile.flush()
        except Exception as exc:  # noqa: BLE001 - a broken client must not take the server down
            row["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            resp.close()

        latency_ms = round((time.time() - t0) * 1000, 1)
        row.update({"status": resp.status, "latency_ms": latency_ms, "prompt_tokens": pin,
                    "completion_tokens": pout, "cost": cost, "cost_basis": basis,
                    "metered": bool(basis)})
        row["deltas_seen"] = deltas
        if basis == "estimated_from_deltas":
            row["note"] = ("upstream ended the stream with no usage frame after "
                           f"{latency_ms/1000:.0f}s; priced from {deltas} forwarded deltas (a FLOOR)")
            row["stream_aborted"] = True
        elif not basis and row.get("error"):
            # THE ROW MUST NOT CONTRADICT ITSELF. The synthesis above lives inside the `try`, so any
            # exception while forwarding -- a client that hung up is the one that happens; five
            # `BrokenPipeError` rows are in `meter/meter.jsonl` -- skips it and lands here. Two of
            # those five carry `deltas_seen` 3149 and 1 while the note beside them said, in words,
            # that there were no deltas. Whether those tokens should be PRICED is a separate
            # question (the client left; the provider still generated and still billed) and this
            # branch deliberately does not answer it -- `metered` stays false and `cost` stays 0.0,
            # which is the module's "unpriced, never $0" rule. What it must not do is state a fact
            # about the row that the field next to it falsifies.
            row["note"] = (f"stream ended in an error after {deltas} forwarded delta(s) and no "
                           f"usage frame; nothing was priced ({row['error']})")
        elif not basis:
            # No usage frame AND no deltas: nothing was produced to price. Unpriced, and recorded as
            # unpriced -- never as $0.
            row["note"] = "streamed response carried no usage frame and no deltas"
        if pout:
            row["tok_per_s"] = round(pout / max(latency_ms / 1000.0, 1e-6), 2)
        self.server.meter.record(row)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET

    # A CLIENT HANGING UP IS NOT AN INCIDENT. `http.server` prints a full traceback for every
    # exception in a handler thread, and an httpx/aiohttp connection pool closes idle keep-alive
    # sockets constantly: measured 2026-08-23, 1,301 `ConnectionResetError` tracebacks — 2,604 log
    # entries — in one campaign's meter log, every one of them raised at `handle_one_request`'s
    # `readline` while WAITING for a request that never came. Not one was a lost call.
    #
    # They are suppressed rather than reduced because a log that is 99% noise is a log nobody reads
    # the exception in, and this file's whole purpose is to be believed about money. Anything else
    # still prints, including the same error class raised anywhere other than an idle socket.
    def handle_error(self, request, client_address) -> None:      # noqa: D102 - stdlib override
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            tb = sys.exc_info()[2]
            frames = []
            while tb is not None:
                frames.append(tb.tb_frame.f_code.co_name)
                tb = tb.tb_next
            if "handle_one_request" in frames or "readline" in frames:
                return
        super().handle_error(request, client_address)


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
