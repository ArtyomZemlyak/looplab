"""A cut generation must arrive as an ANSWER, and it must arrive after a bounded number of tokens.

WHAT THIS FILE PINS AND WHAT IT COST TO LEARN IT. Measured 2026-08-26 over
`/var/tmp/looplab-bench/meter/meter.jsonl` (9,235 rows): **134 aborted streams, $6.646 and 62.85 h
of wall clock** -- the three headline numbers of doc 53 9a reproduce exactly at the 131st abort
($6.552, 61.91 h), which is where that section was written. 113 of the 134 are arm A on the
stream-adapted path and 21 are arm B streaming.

**The cut is not the cost; the RETRY of the cut is.** Grouped by `(arm, task, attempt)` and read in
arrival order, the 134 aborts fall into 44 consecutive runs, and four of those runs are 23, 23, 16
and 15 long -- 77 aborts, on one task-arm each. Arm B's longest run is 2. Recomputed over the same
log:

* keep only the FIRST abort of each run -> 44 aborts, **43.34 h and $4.55 saved (69 %, 68 %)**;
* a 135,000-delta ceiling alone -> **16.27 h and $1.94 saved (26 %, 29 %)**;
* both -> **48.67 h and $5.19 (77 %, 78 %)**.

So the multiplier is worth two and a half times the per-call cap, and it is a CLIENT loop:
`AlgoTuner/interfaces/llm_interface.py` catches `(RateLimitError, APIError, APIConnectionError)`
and retries ten times, escaping only on a substring match against a payment/quota list. Its logs
show `LiteLLM API non-retryable error` -- litellm's own classifier, correctly refusing -- followed
immediately by `Transient LLM error (APIError) ... Retrying in 2.26s (attempt 1/10)` on the SAME
exception, 72 times across the campaign's logs, with five runs reaching `Exceeded max retries (10)`.

**There is therefore no honest error shape that stops it.** No status and no body makes that loop
give up; only a claim about the account ("insufficient credits", "402") would, and it would be
false. What is left is to stop the call being an error at all -- which is honest, because it is not
one: the request partially succeeded and this meter has already billed it. That is
`abort_is_not_retryable`, and the ceiling is what makes it RELIABLE rather than best-effort: a cut
the proxy chooses is complete and identical every time, where a cut it merely survives is whatever
the dying socket leaves behind (31 of the 134 rows carry a `BrokenPipeError` from trying).

None of this touches a live proxy: every test here stands up its own upstream, its own meter and its
own ephemeral port, and the ceilings used are tiny so that nothing here depends on a real gateway.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "_meter_proxy_ceiling", Path(__file__).resolve().parents[1] / "benchmarks/meter/proxy.py")
proxy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(proxy)

RATE_IN = 0.00000014
RATE_OUT = 0.00000028
CEILING = 50
# What the upstream would produce if nobody stopped it. Deliberately far more than the ceiling, and
# each frame deliberately fat, so that "the generation stopped" cannot be confused with "the kernel
# socket buffer swallowed the rest": 4,000 x ~2 kB is ~8 MB, orders of magnitude past any buffer.
RUNAWAY_DELTAS = 4000
_FAT = "x" * 2000

# The measured maxima this ceiling has to clear to be parity-neutral. Both are COMPLETE streams that
# the gateway priced itself, from `meter/meter.jsonl` on 2026-08-26: arm A's largest is a
# `pde_heat1d` call of 132,269 deltas and arm B's is 126,559. A ceiling below either of them is a
# cap on how long one framework is allowed to think, which is the reason `max_tokens: 8192` was
# rejected in doc 53 9b (it cut 7.7 % of arm B against 0.06 % of arm A).
LARGEST_COMPLETE_ARM_A = 132_269
LARGEST_COMPLETE_ARM_B = 126_559


class _Upstream(BaseHTTPRequestHandler):
    """A generation that does not stop on its own -- the thing the ceiling exists for.

    A request whose last message says `SHORT` instead returns three deltas and a proper usage frame:
    that is the traffic the ceiling must not touch.

    It counts what it managed to WRITE in `written`, so a test can tell "the proxy stopped reading"
    from "the proxy read it all and threw most of it away". The two are the whole difference between
    a ceiling that saves money and one that only shortens a log line.
    """

    protocol_version = "HTTP/1.1"
    written: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        short = "SHORT" in json.dumps(body.get("messages") or [])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        n = 0
        try:
            for i in range(3 if short else RUNAWAY_DELTAS):
                self._frame({"id": "cmpl-1", "object": "chat.completion.chunk", "model": "m",
                             "created": 1,
                             "choices": [{"index": 0, "delta": {"content": _FAT}}]})
                self.wfile.flush()
                n += 1
            if short:
                self._frame({"id": "cmpl-1", "choices": [{"index": 0, "delta": {},
                                                          "finish_reason": "stop"}]})
                self._frame({"id": "cmpl-1", "choices": [],
                             "usage": {"prompt_tokens": 200, "completion_tokens": 3,
                                       "total_tokens": 203}})
                self._frame_done()
                self.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass                                 # the reader hung up: that is the ceiling working
        finally:
            type(self).written.append(n)
            self.close_connection = True

    def _frame(self, payload: dict) -> None:
        raw = b"data: " + json.dumps(payload).encode() + b"\n\n"
        self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")

    def _frame_done(self) -> None:
        raw = b"data: [DONE]\n\n"
        self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")

    def log_message(self, *_args):
        pass


@pytest.fixture()
def upstream():
    _Upstream.written = []
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
    srv.delta_ceiling = CEILING
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
        # The server object is handed out too. One test turns the ceiling off, which is what
        # `main()` does with `--delta-ceiling 0`; reaching it through a module-level registry
        # instead would let these tests change the ceiling under each other.
        yield base, tmp_path / "meter.jsonl", srv
    finally:
        srv.shutdown()


def _call(base: str, *, stream: bool, short: bool = False) -> tuple[int, bytes]:
    """Return `(status, body)`. A raised HTTPError is a STATUS, not a test failure -- the whole
    question this file asks is which of the two the client gets."""
    text = ("SHORT " if short else "") + "the quick brown fox " * 40
    payload = {"model": "m", "messages": [{"role": "user", "content": text}]}
    if stream:
        payload["stream"] = True
    req = urllib.request.Request(
        base + "/v1/chat/completions", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _sse_events(raw: bytes) -> list[bytes]:
    """Split the body the way a conformant SSE client does: on the BLANK LINE, not on `data: `.

    This distinction is the whole reason this helper exists rather than a line filter. A line filter
    parses each `data:` line on its own and is therefore blind to the one failure this file's own
    first draft shipped: a stream cut between a `data:` line and its terminator leaves the event
    OPEN, so the next frame is glued in as a SECOND data line of the SAME event. A real client
    concatenates the two payloads and `json.loads` reports `Extra data: line 2 column 1` --
    measured against arm A's own litellm 1.97.0, which surfaces it as a `MidStreamFallbackError`
    wrapping an `APIConnectionError`, one of the three types its ten-attempt loop retries.

    So: an event is a block, its `data:` lines are joined per the spec, and the result must parse.
    """
    events = []
    for block in raw.split(b"\n\n"):
        data = b"".join(l[len(b"data:"):].lstrip() for l in block.split(b"\n")
                        if l.strip().startswith(b"data:"))
        if data:
            events.append(data)
    return events


def _frames(raw: bytes) -> list[dict]:
    """Every non-sentinel event, parsed. A malformed one fails here rather than being skipped."""
    out = []
    for data in _sse_events(raw):
        if data == b"[DONE]":
            continue
        try:
            out.append(json.loads(data))
        except ValueError as exc:
            raise AssertionError(
                f"an SSE event on the wire does not parse ({exc}); a conformant client raises here "
                f"and AlgoTuner's loop retries what it raises. Event: {data[:200]!r}") from None
    return out


def _sse_payloads(raw: bytes) -> list[bytes]:
    """Every event payload in arrival order, `[DONE]` included -- the sentinel is the point."""
    return _sse_events(raw)


def _usage_as_an_openai_client_reads_it(frames: list[dict]) -> dict | None:
    """`stream_options.include_usage`'s own rule: usage lives on the chunk with EMPTY choices."""
    for frame in frames:
        if isinstance(frame.get("usage"), dict) and not frame.get("choices"):
            return frame["usage"]
    return None


def _the_client_loop_would_retry(status: int, frames: list[dict]) -> bool:
    """AlgoTuner's outer loop, transcribed from its source rather than guessed at.

    `AlgoTuner/interfaces/llm_interface.py` wraps `self.model.query(...)` in
    `for attempt in range(10)` / `except (RateLimitError, APIError, APIConnectionError)`, and the
    only thing that reaches those handlers is an EXCEPTION out of litellm. litellm raises on a
    non-2xx status and on an OpenAI `error` envelope; a 200 carrying a completion is returned, not
    raised, whatever its `finish_reason` says. So the loop retries exactly when the proxy hands back
    an error -- which is why the fix is to stop handing back one, and why this predicate is the
    falsifier for the whole file. It deliberately does NOT model litellm's leniency about anything
    else: it asks only the question the retry loop asks.
    """
    if status >= 400:
        return True
    return any(isinstance(f, dict) and f.get("error") is not None for f in frames)


def _rows(path: Path, expect: int = 1) -> list[dict]:
    """The meter row is written AFTER the client's last byte, so a reader must wait for it."""
    for _ in range(400):
        if path.exists():
            rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
            if len(rows) >= expect:
                return rows
        time.sleep(0.02)
    raise AssertionError(f"the meter never recorded {expect} row(s) for a call it answered")


def test_a_runaway_generation_is_stopped_by_the_meter_and_billed_for_what_it_forwarded(meter):
    """The damage cap. Delete the `break` at the ceiling and the upstream runs to 4,000.

    This is the half doc 53 9b proposed: cap the tokens, not the request. It is asserted at the
    UPSTREAM as well as at the client, because a ceiling that merely truncates the answer while the
    gateway keeps generating saves a log line and no money at all -- and the 62.85 h that motivated
    this file are wall clock upstream, not bytes downstream.
    """
    base, log, _srv = meter
    status, raw = _call(base, stream=True)
    assert status == 200

    frames = _frames(raw)
    deltas = [f for f in frames
              for ch in (f.get("choices") or [])
              if (ch.get("delta") or {}).get("content")]
    assert len(deltas) == CEILING, "the client must receive every delta the meter counted, and no more"

    row = _rows(log)[-1]
    assert row["stream_cut_by"] == "meter_delta_ceiling"
    assert row["meter_delta_ceiling"] == CEILING
    assert row["deltas_seen"] == CEILING
    assert row["cost_basis"] == "estimated_from_deltas"
    assert row["cost"] > 0, "a cut generation still cost money and must still be billed"
    # `stream_aborted` keeps its meaning for every reader that already counts money on it
    # (`benchmarks/algotune/compare_arms.py`), and the CAUSE is a separate key.
    assert row["stream_aborted"] is True

    # And upstream really stopped. Poll: the handler records its count on the way out.
    for _ in range(200):
        if _Upstream.written:
            break
        time.sleep(0.02)
    assert _Upstream.written, "the upstream handler never finished; nothing can be concluded"
    assert _Upstream.written[0] < RUNAWAY_DELTAS, (
        f"the generation ran to completion ({_Upstream.written[0]} deltas) — the meter truncated "
        "the ANSWER but went on paying for the tokens, which is the defect and not the fix")


def test_the_cut_is_handed_back_as_an_answer_so_the_clients_retry_loop_has_nothing_to_catch(meter):
    """THE falsifier for `an-aborted-stream-is-retried-ten-times`.

    Make the ceiling return a status or an `error` envelope instead -- 504, or `{"error": ...}`,
    any of the shapes that read as "the gateway failed" -- and this dies, because that is precisely
    what AlgoTuner's ten-attempt loop retries. The response must be a 200 that says, in the two
    places a client looks, that the generation was truncated and what it cost.
    """
    base, log, _srv = meter
    status, raw = _call(base, stream=True)
    frames = _frames(raw)

    assert not _the_client_loop_would_retry(status, frames), (
        "the meter's own cut was delivered as an error; a client that retries errors ten times "
        "turns one cut into ten, which is where 62.85 h and $6.65 went")

    cuts = [f for f in frames
            for ch in (f.get("choices") or [])
            if ch.get("finish_reason") == proxy.STREAM_TRUNCATED_FINISH_REASON]
    assert cuts, "a cut generation must still be announced as cut, or the client cannot tell"
    assert all(not f.get("usage") for f in cuts), (
        "the cut chunk must not carry usage: a chunk with populated choices AND usage is the shape "
        "litellm 1.97.0 silently drops (2afb287c)")

    usage = _usage_as_an_openai_client_reads_it(frames)
    assert usage is not None, "the price must arrive on the empty-choices chunk `include_usage` reads"
    assert usage["completion_tokens"] == CEILING
    assert usage["cost"] > 0
    assert usage["meter_cut_by"] == "delta_ceiling"
    assert usage["meter_delta_ceiling"] == CEILING
    assert str(CEILING) in usage["meter_note"], (
        "the frame must say the METER cut it and where; a client told only 'upstream ended the "
        "stream' would read an exact count as a floor")


def test_the_stream_ends_on_the_sentinel_and_the_sentinel_is_last(meter):
    """Delete `data: [DONE]` from `abort_is_not_retryable` and this dies.

    It is the one part of the ending that was never there: before this commit the proxy's cut sent
    two minted chunks and then the body simply stopped. `[DONE]` is how the OpenAI streaming
    protocol says "this is the end and it is the end I meant", and a truncated stream is exactly the
    case where a client has to be able to tell that apart from a connection that died mid-answer.
    """
    base, _log, _srv = meter
    _status, raw = _call(base, stream=True)
    payloads = _sse_payloads(raw)
    assert payloads, "the client received no SSE payloads at all"
    assert b"[DONE]" in payloads, "a cut stream must still terminate on the sentinel"
    assert payloads[-1] == b"[DONE]", (
        f"the sentinel must be the LAST payload, not followed by {payloads[-1][:60]!r}")


def test_the_adapted_client_gets_the_same_cut_as_one_whole_answer(meter, monkeypatch):
    """113 of the 134 recorded aborts were on this path, so it is not the secondary case.

    `METER_STREAM_ADAPT=1` hands the client one JSON body. The ceiling has to reach it too, and it
    has to reach it as a 200 with a finish reason and a price -- not as the 502 an unhandled
    upstream failure produces on this path.
    """
    monkeypatch.setenv("METER_STREAM_ADAPT", "1")
    base, log, _srv = meter
    status, raw = _call(base, stream=False)

    assert status == 200, f"the adapted client got {status}, which its retry loop retries"
    body = json.loads(raw)
    assert not _the_client_loop_would_retry(status, [body])
    assert body["choices"][0]["finish_reason"] == proxy.STREAM_TRUNCATED_FINISH_REASON
    assert len(body["choices"][0]["message"]["content"]) == CEILING * len(_FAT)
    assert body["usage"]["meter_cut_by"] == "delta_ceiling"
    assert body["usage"]["completion_tokens"] == CEILING
    assert body["usage"]["cost"] > 0

    row = _rows(log)[-1]
    assert row["stream_adapted"] is True and row["stream_cut_by"] == "meter_delta_ceiling"


def test_an_answer_that_finishes_under_the_ceiling_is_untouched(meter):
    """The false-positive guard. A ceiling is only symmetric while it does not fire on real answers.

    Three deltas and the gateway's own usage frame: nothing the meter minted may appear, the price
    must be the gateway's, and the row must not claim a cut that did not happen.
    """
    base, log, _srv = meter
    status, raw = _call(base, stream=True, short=True)
    assert status == 200
    frames = _frames(raw)

    finishes = [ch.get("finish_reason") for f in frames for ch in (f.get("choices") or [])
                if ch.get("finish_reason")]
    assert finishes == ["stop"], f"a complete answer must keep its own finish reason, got {finishes}"
    usage = _usage_as_an_openai_client_reads_it(frames)
    assert usage is not None and "meter_cut_by" not in usage

    row = _rows(log)[-1]
    assert "stream_cut_by" not in row and row.get("stream_aborted") is None
    assert row["completion_tokens"] == 3, "the gateway's own count, not a delta count"


def test_the_default_ceiling_clears_both_arms_largest_measured_complete_answer():
    """The parity guard, and the one that makes lowering the number cost something.

    Measured over `meter/meter.jsonl` (8,830 complete streams, 2026-08-26): the largest complete
    answer is 132,269 deltas in arm A and 126,559 in arm B, so 135,000 truncates nothing in either
    arm on the whole recorded corpus. Every value that saves materially more has a measured
    false-positive rate and an ASYMMETRIC one: at 32,768 it would have cut 2 arm-A calls and 38 arm-B
    ones, at 8,192 it is 2 against 468. A cap that binds one framework nineteen times harder than
    the other is not a meter setting, it is a handicap -- the reason doc 53 9b rejected `max_tokens`
    and the reason this number may not quietly drift downwards to make a table look better.
    """
    assert proxy.DELTA_CEILING_VALUE >= LARGEST_COMPLETE_ARM_A
    assert proxy.DELTA_CEILING_VALUE >= LARGEST_COMPLETE_ARM_B


def test_the_ceiling_is_off_unless_someone_asks_for_it():
    """An instrument that changes the ruler is CHOSEN, never inherited.

    This reverses the value the ceiling shipped with hours earlier, and the reason is a live fact
    about this box rather than a principle. `benchmarks/watchdog.sh` runs on a 300 s loop, pings
    `/healthz`, and calls `meter/start_meter.sh --restart` when it fails. With the ceiling defaulting
    ON, ANY transient proxy failure re-meters the remainder of a running campaign through a
    different instrument, and the task-arms before and after the blip are priced by two rulers --
    the exact failure docs/53 was opened over. The watchdog was running (pid 3249487) beside a
    23-hour campaign while the on-disk proxy already carried the new code and the loaded process
    did not.

    Off, the runaway runs to its own end and is priced the way it always was. Turning it on is one
    flag, and the NEXT campaign is where it belongs -- the same rule `rules_clause` states for the
    goal card: adopt between arms, never inside one.
    """
    assert proxy.DELTA_CEILING_DEFAULT == 0, (
        "the ceiling defaults on again; a watchdog restart would change the ruler mid-campaign")
    assert proxy.DELTA_CEILING_VALUE > 0, "the value to switch ON must still be a real one"


def test_the_ceiling_can_be_switched_off_and_then_the_gateway_decides(meter):
    """`0` means off, and off must mean the previous behaviour exactly -- no cut, no new key.

    A guard with no off switch cannot be bisected against, and this one changes what a campaign
    measures: switching it on mid-run would price the tasks before and after it with two different
    instruments. Off, the runaway runs to its own end and is priced the way it always was.
    """
    base, log, srv = meter
    srv.delta_ceiling = 0                        # exactly what `--delta-ceiling 0` does

    status, raw = _call(base, stream=True)
    assert status == 200
    row = _rows(log)[-1]
    assert "stream_cut_by" not in row, "with the ceiling off nothing may claim to have cut"
    assert row["deltas_seen"] == RUNAWAY_DELTAS, (
        "with the ceiling off the whole runaway must be forwarded and billed, exactly as before")
    assert len(_frames(raw)) >= RUNAWAY_DELTAS
