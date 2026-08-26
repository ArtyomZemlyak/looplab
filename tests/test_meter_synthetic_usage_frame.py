"""A ceiling is only as honest as the usage frame the client can actually READ.

Measured 2026-08-26 against `meter/meter.jsonl` (9,053 rows) and arm A's own logs.

The gateway cuts a generation at ~1800 s: 128 streams in that log ended with no usage frame, 113 of
them between 1795 s and 1825 s, every one status 200 with no exception on our side, median 190,307
forwarded deltas. The proxy already priced those from the deltas it forwarded and already MINTED a
synthetic usage frame for them. Two things about that frame were still wrong, and both are what this
file pins.

1. THE CLIENT COULD NOT READ IT. The frame carried a populated `choices` array (to hold the
   `finish_reason`) and the `usage` block in the SAME chunk. That is not the shape an
   OpenAI-compatible client takes usage out of. Measured against arm A's own litellm 1.97.0, driving
   this proxy in front of a fake aborting upstream: streaming that frame yields `usage=None`, and
   streaming it with `stream_options={"include_usage": true}` yields a MINTED
   `Usage(prompt_tokens=0, completion_tokens=0)` with no cost -- a zero this proxy never sent, in the
   accountant of the arm whose ceiling then fired at twice its real spend. Split into the two frames
   `include_usage` itself uses -- the cut, then a `choices: []` chunk carrying the price -- the same
   client reports `completion_tokens=500` and the cost. That is `test_the_price_arrives_where_a_usage_frame_priced_client_looks`.

2. IT SAID THE PROMPT WAS ZERO TOKENS. Not a floor: a false measurement, and the expensive one.
   Across arm A's 1,773 complete streams in that log the prompt side is 97.3 % of metered spend
   ($13.93 of $14.31; median prompt 42,698 tokens against a median completion of 537). Charging each
   of the 129 aborted streams the prompt of the nearest complete call in its own task-arm adds $1.20
   to their $6.44 -- 18.6 % of what those aborts cost, 21.5 % for arm A alone. The proxy is holding
   the request, so it counts the prompt's characters exactly and converts them with a ratio it
   calibrates from calls the gateway itself priced. With no such call yet it reports 0 and says
   `unmeasured` rather than inventing one.

None of this touches a live proxy: every test here stands up its own upstream, its own meter and its
own ephemeral port.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "_meter_proxy_usage", Path(__file__).resolve().parents[1] / "benchmarks/meter/proxy.py")
proxy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(proxy)

RATE_IN = 0.00000014
RATE_OUT = 0.00000028
CUT_DELTAS = 40


class _Upstream(BaseHTTPRequestHandler):
    """Two behaviours on one endpoint, chosen by the request itself.

    A request whose last message says `PRICE ME` gets a complete stream with the gateway's own usage
    frame -- that is the traffic the prompt-token ratio is calibrated from. Anything else gets what
    the real gateway does at ~1800 s: deltas, then the socket, with no usage frame and no `[DONE]`.
    """

    protocol_version = "HTTP/1.1"

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        priced = "PRICE ME" in json.dumps(body.get("messages") or [])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        n = 3 if priced else CUT_DELTAS
        for i in range(n):
            self._frame({"id": "cmpl-1", "model": "m", "created": 1,
                         "choices": [{"index": 0, "delta": {"reasoning_content": f"t{i} "}}]})
        if priced:
            self._frame({"id": "cmpl-1", "choices": [{"index": 0, "delta": {},
                                                      "finish_reason": "stop"}]})
            # The authoritative prompt count. `_PRICED_PROMPT_TOKENS` below is the same number.
            self._frame({"id": "cmpl-1", "choices": [],
                         "usage": {"prompt_tokens": 200, "completion_tokens": 3,
                                   "total_tokens": 203}})
            self.wfile.write(b"0\r\n\r\n")
            return
        self.wfile.flush()
        self.close_connection = True                 # the cut: no usage frame, no terminating chunk
        try:
            self.wfile.close()
        except OSError:
            pass

    def _frame(self, payload: dict) -> None:
        raw = b"data: " + json.dumps(payload).encode() + b"\n\n"
        self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")

    def log_message(self, *_args):
        pass


_PRICED_PROMPT_TOKENS = 200


@pytest.fixture()
def upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


@pytest.fixture()
def meter(upstream, tmp_path):
    srv = proxy.Server(("127.0.0.1", 0), proxy.Handler)
    srv.upstream = upstream
    srv.api_key = ""
    srv.timeout = 30.0
    srv.max_retries = 0
    price = tmp_path / "pricing.json"
    price.write_text(json.dumps({
        "source": "test", "fetched_at": "now", "cost_basis": "imputed",
        "default": {"input_per_token": RATE_IN, "output_per_token": RATE_OUT},
        "models": {}}), encoding="utf-8")
    srv.pricing = proxy.Pricing(str(price))
    srv.meter = proxy.Meter(tmp_path / "meter.jsonl")
    srv.limiter = proxy.RateLimiter(1000)
    # No system proxy: a `$http_proxy` in the environment would turn every test into a 502.
    srv.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}/m/A/t/a1"
    try:
        yield base, tmp_path / "meter.jsonl"
    finally:
        srv.shutdown()


_PROMPT = "the quick brown fox jumps over the lazy dog, " * 40


def _post(base: str, *, price_me: bool, stream: bool = True) -> bytes:
    text = ("PRICE ME " if price_me else "") + _PROMPT
    req = urllib.request.Request(
        base + "/v1/chat/completions",
        data=json.dumps({"model": "m", "stream": stream,
                         "messages": [{"role": "user", "content": text}]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _frames(raw: bytes) -> list[dict]:
    out = []
    for line in raw.split(b"\n"):
        line = line.strip()
        if line.startswith(b"data: ") and not line.startswith(b"data: [DONE]"):
            out.append(json.loads(line[6:]))
    return out


def _rows(path: Path, expect: int = 1) -> list[dict]:
    """The meter row is written AFTER the client's last byte, so a reader must wait for it.

    Deliberately a poll and not a sleep: the ordering is the proxy's (frames first, ledger second)
    and a fixed sleep would make this file's greenness a property of how loaded the box is.
    """
    for _ in range(200):
        if path.exists():
            rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
            if len(rows) >= expect:
                return rows
        time.sleep(0.02)
    raise AssertionError(f"the meter never recorded {expect} row(s) for a call it answered")


def _usage_as_an_openai_client_reads_it(frames: list[dict]) -> dict | None:
    """The client-side rule, written out so the test asserts against it and not against our own code.

    An OpenAI-compatible streaming client takes usage from the chunk `stream_options.include_usage`
    delivers it on -- the one whose `choices` is EMPTY. litellm 1.97.0 implements exactly this, which
    is why a combined frame was invisible to arm A. A test that just looked for `usage` anywhere in
    the stream would pass on the broken shape.
    """
    for frame in frames:
        if isinstance(frame.get("usage"), dict) and not frame.get("choices"):
            return frame["usage"]
    return None


def test_the_price_arrives_where_a_usage_frame_priced_client_looks(meter):
    """THE falsifier. Merge the two frames back into one and this dies, as it did in production.

    Both halves are asserted: the cut has to be announced (a client that never sees a finish_reason
    cannot tell a cut from an answer) and the price has to arrive on a chunk a usage-frame-priced
    client will actually read.
    """
    base, log = meter
    frames = _frames(_post(base, price_me=False))

    usage = _usage_as_an_openai_client_reads_it(frames)
    assert usage is not None, (
        "the synthetic usage frame is not in the shape an OpenAI-compatible client reads usage "
        "from — it must arrive on a chunk with an EMPTY choices array")
    assert usage["completion_tokens"] == CUT_DELTAS
    assert usage["cost"] > 0 and usage["cost_basis"] == "estimated_from_deltas"

    cuts = [f for f in frames
            for ch in (f.get("choices") or [])
            if ch.get("finish_reason") == proxy.STREAM_TRUNCATED_FINISH_REASON]
    assert cuts, "a cut generation must still be announced as cut"
    # ...and the two must not be the same chunk, which is the whole point.
    assert all(not f.get("usage") for f in cuts), (
        "the cut chunk must not carry the usage block: a chunk with populated choices AND usage is "
        "the shape litellm 1.97.0 silently drops")

    row = _rows(log)[-1]
    assert row["stream_aborted"] is True
    assert row["completion_tokens"] == usage["completion_tokens"]
    assert row["cost"] == pytest.approx(usage["cost"])


def test_the_prompt_side_is_recovered_from_the_request_and_says_so(meter):
    """`prompt_tokens: 0` was not a floor, it was wrong — and on this campaign it was 97.3 % of the money.

    One priced call establishes what a character is worth on this endpoint; the cut that follows is
    charged for the prompt it actually sent, and the frame names the basis so nobody reads the number
    as something the gateway reported.
    """
    base, log = meter
    _post(base, price_me=True)                       # calibrates: 200 prompt tokens for this text

    frames = _frames(_post(base, price_me=False))
    usage = _usage_as_an_openai_client_reads_it(frames)
    assert usage is not None

    assert usage["meter_prompt_tokens_basis"] == "estimated_from_request_chars"
    assert usage["meter_prompt_chars"] > 0
    assert usage["meter_prompt_calibration_calls"] == 1
    # The two requests differ only by the marker, so the recovered count must land near the one the
    # gateway reported for the calibrating call. A loose band: this asserts the ratio was USED, not
    # that a character-based estimate is exact — `PromptTokens` documents its measured error.
    assert 0.7 * _PRICED_PROMPT_TOKENS <= usage["prompt_tokens"] <= 1.3 * _PRICED_PROMPT_TOKENS
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]

    # And the money follows the tokens: the prompt side is PRICED, not merely reported.
    assert usage["cost"] == pytest.approx(
        usage["prompt_tokens"] * RATE_IN + CUT_DELTAS * RATE_OUT)
    assert usage["cost"] > CUT_DELTAS * RATE_OUT, "the prompt side must add to the bill"

    row = _rows(log, 2)[-1]
    assert row["prompt_tokens"] == usage["prompt_tokens"]
    assert row["prompt_tokens_basis"] == "estimated_from_request_chars"
    assert row["prompt_chars"] == usage["meter_prompt_chars"]


def test_with_nothing_measured_it_reports_zero_and_calls_it_unmeasured(meter):
    """A proxy restarted straight into a cut has no ratio. It must under-report, never invent.

    This is the half that keeps the estimate honest: the number is only allowed to be non-zero when
    something the GATEWAY priced put it there.
    """
    base, log = meter
    frames = _frames(_post(base, price_me=False))    # first call of the process: nothing to learn from
    usage = _usage_as_an_openai_client_reads_it(frames)
    assert usage is not None
    assert usage["prompt_tokens"] == 0
    assert usage["meter_prompt_tokens_basis"] == "unmeasured"
    assert "meter_chars_per_prompt_token" not in usage, "no ratio exists; none may be quoted"
    assert usage["cost"] == pytest.approx(CUT_DELTAS * RATE_OUT)
    assert _rows(log)[-1]["prompt_tokens_basis"] == "unmeasured"


def test_a_complete_stream_still_carries_the_gateways_own_numbers(meter):
    """The calibration must not become a second opinion about a call the gateway already priced."""
    base, log = meter
    frames = _frames(_post(base, price_me=True))
    usage = _usage_as_an_openai_client_reads_it(frames)
    assert usage["prompt_tokens"] == _PRICED_PROMPT_TOKENS
    assert usage["cost_basis"].startswith("imputed")
    assert "meter_prompt_tokens_basis" not in usage, (
        "a frame the gateway sent must come back with the gateway's own fields, not ours")
    row = _rows(log)[-1]
    assert row["prompt_tokens_basis"] == "reported_by_upstream"
    assert row.get("stream_aborted") is None


def test_the_adapted_client_gets_the_same_two_facts_in_one_body(meter, monkeypatch):
    """Arm A does not stream; the meter streams FOR it (`METER_STREAM_ADAPT=1`) and reassembles.

    The split into two frames must survive that reassembly, or fixing the streaming client would
    have broken the non-streaming one.
    """
    monkeypatch.setenv("METER_STREAM_ADAPT", "1")
    base, log = meter
    _post(base, price_me=True, stream=False)
    body = json.loads(_post(base, price_me=False, stream=False))
    assert body["choices"][0]["finish_reason"] == proxy.STREAM_TRUNCATED_FINISH_REASON
    usage = body["usage"]
    assert usage["completion_tokens"] == CUT_DELTAS
    assert usage["prompt_tokens"] > 0
    assert usage["meter_prompt_tokens_basis"] == "estimated_from_request_chars"
    assert _rows(log, 2)[-1]["stream_adapted"] is True


def test_the_calibrator_never_answers_from_evidence_it_does_not_have():
    """`PromptTokens` in isolation: the two states it is allowed to be in, and nothing between."""
    scale = proxy.PromptTokens()
    assert scale.estimate(1000) == (0, "unmeasured", None, 0)
    scale.observe(0, 50)                  # no characters is not evidence
    scale.observe(500, 0)                 # no tokens is not evidence either
    assert scale.estimate(1000)[1] == "unmeasured"
    scale.observe(400, 100)               # four characters to the token
    tokens, basis, per_token, calls = scale.estimate(1000)
    assert (tokens, basis, calls) == (250, "estimated_from_request_chars", 1)
    assert per_token == pytest.approx(4.0)
