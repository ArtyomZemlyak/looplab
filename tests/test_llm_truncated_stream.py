"""A stream the gateway CUTS mid-body is a truncated answer, not a failed request.

The defect, measured on a 20-run AlgoTune campaign (`/var/tmp/looplab-bench/runs-B` +
`/var/tmp/looplab-bench/meter`, 2026-08-22/23). A metering gateway cuts any generation at ~1800 s.
Twenty-six streams across ten of the twenty tasks hit it; every one had already forwarded
196,286-238,423 content deltas and cost $0.055-$0.067 before the cut. litellm reports its own
upstream failure as an in-band `data: {"error": …}` frame INSIDE the HTTP-200 SSE body, and the
openai SDK turns that into a BARE `openai.APIError` — not an `APIStatusError`, not an
`APIConnectionError`. It therefore matched no `_RETRY_POLICY` row, fell to `_policy_unclassified`
and raised on the FIRST attempt with `max_retries=8` untouched, while `_accumulate_stream`
discarded every delta it had accumulated.

Three consequences, and this file states each as a property rather than as a source pin:

  1. the run lost the answer it had already paid thirty minutes for, and re-asked (or died). Those
     26 streams burned 13.15 hours, 18.7-94.6 % of each affected run's whole lifetime;
  2. the call never reached `CostAccountant` at all — not as spend, not even as a CALL. Comparing
     each run's own `llm_usage` rows against the meter's, the per-task gap equals the aborted spend
     EXACTLY on four tasks (max_common_subgraph $0.1919, max_weighted_independent_set $0.1938,
     min_dominating_set $0.1259, rectanglepacking $0.0646), and five arms overran a $1.00 ceiling by
     6.9-19.3 % while their own ledgers reported them under it;
  3. nothing said so.

The driver here is the REAL openai SDK decoding REAL SSE bytes over an `httpx.MockTransport`, not a
scripted `_sdk_chat`. That is deliberate: the whole defect turns on WHICH exception class the SDK
builds for an error frame, so a test that raises the class itself would assume the very fact that
was wrong. If a later SDK stops building a bare `APIError` there, these tests say so.
"""
from __future__ import annotations

import json

import httpx
import openai
import pytest

import looplab.core.llm as llm
import looplab.core.llm_streaming as llm_streaming
import looplab.core.llm_transient as llm_transient
from looplab.core.llm import LLMError, OpenAICompatibleClient

_REQ = httpx.Request("POST", "http://x/v1/chat/completions")

# The exact body the metering proxy forwarded, verbatim from the run's own `node_failed.error`.
LITELLM_CUT = ("litellm.APIConnectionError: APIConnectionError: OpenAIException - Response payload "
               "is not completed: <TransferEncodingError: 400, message='Not enough data to satisfy "
               "transfer length header.'>")


def _sse(*frames: dict) -> bytes:
    return b"".join(b"data: " + json.dumps(f).encode() + b"\n\n" for f in frames)


def _delta(**delta) -> dict:
    return {"id": "1", "object": "chat.completion.chunk", "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}


def _error_frame(message: str = LITELLM_CUT) -> dict:
    """What litellm puts on the wire when ITS upstream dies mid-generation: an error object in a
    `data:` frame of a response that already returned HTTP 200 and has been streaming for minutes."""
    return {"error": {"message": message, "type": "None", "code": "500"}}


def _usage_frame(cost: float) -> dict:
    return {"id": "1", "object": "chat.completion.chunk", "model": "m", "choices": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
                      "cost": cost}}


def _done() -> bytes:
    return b"data: [DONE]\n\n"


def _meter_estimate(cost: float, tokens: int) -> dict:
    """The frame `benchmarks/meter/proxy.py` synthesises for a stream its upstream cut: the tokens
    it actually forwarded, priced as a FLOOR. It is emitted AFTER the upstream's error frame,
    because the proxy only knows the stream ended once its forward loop has drained — which is
    exactly why reaching it is the whole problem."""
    return {"id": "meter-estimate", "object": "chat.completion.chunk", "model": "m", "choices": [],
            "usage": {"prompt_tokens": 0, "completion_tokens": tokens, "total_tokens": tokens,
                      "cost": cost, "cost_basis": "estimated_from_deltas",
                      "meter_note": "completion_tokens is a FLOOR counted from forwarded deltas"}}


class _FakeSSE:
    """The two attributes `Stream.__stream__` and the reorder read off a decoded SSE."""

    def __init__(self, data: str, event=None):
        self.data = data
        self.event = event

    def json(self):
        return json.loads(self.data)


def _chunk(data: dict):
    """An SDK-shaped streaming chunk built from a raw frame, for the few tests that must drive
    `_accumulate_stream` without a transport."""
    choices = []
    for raw in (data.get("choices") or []):
        d = raw.get("delta") or {}
        delta = type("D", (), {"content": d.get("content"), "tool_calls": d.get("tool_calls"),
                               "reasoning": d.get("reasoning"),
                               "reasoning_content": d.get("reasoning_content")})()
        choices.append(type("C", (), {"delta": delta,
                                      "finish_reason": raw.get("finish_reason")})())
    return type("Ev", (), {"choices": choices, "usage": data.get("usage")})()


class _Transport:
    """One fake endpoint, recording every request, answering streams and non-streams differently.

    The non-stream answer matters as much as the SSE one: the degrade this client reaches for after
    a broken stream is "ask the same question without SSE", and a test whose fake cannot answer that
    way cannot tell a rescued call from a lucky one.
    """

    def __init__(self, sse_body: bytes, blocking_text: str = "answered without sse"):
        self.sse_body = sse_body
        self.blocking_text = blocking_text
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        if payload.get("stream"):
            return httpx.Response(200, content=self.sse_body,
                                  headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json={
            "id": "1", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": self.blocking_text}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    @property
    def streamed(self) -> list[bool]:
        return [bool(r.get("stream")) for r in self.requests]


def _client(transport: _Transport, **kw) -> OpenAICompatibleClient:
    kw.setdefault("temperature", 0.0)
    client = OpenAICompatibleClient("m", base_url="http://x/v1", **kw)
    # Swap the SDK, not `_sdk_chat`: everything under test (the SSE decoder that builds the
    # exception, `_stream_with_idle_guard`, `_accumulate_stream`, `_bounded_create`'s worker
    # thread) has to be the real thing for this to prove anything.
    client._sdk = openai.OpenAI(base_url="http://x/v1", api_key="k", max_retries=0,
                                http_client=httpx.Client(transport=httpx.MockTransport(transport)))
    return client


@pytest.fixture
def no_sleep(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: slept.append(s))
    return slept


# --------------------------------------------------------------------------------------------
# The mechanism: which exception the SDK really builds, and which row really claims it.
# --------------------------------------------------------------------------------------------

def test_an_in_band_error_frame_is_the_status_less_apierror_the_new_row_claims():
    """The fact the whole fix rests on, read off the SDK rather than asserted about it.

    A `data: {"error": …}` frame on an HTTP-200 stream yields a BARE `openai.APIError`: no
    `status_code`, not an `APIConnectionError`. Every row above the new one keys on a class that
    excludes it, which is why `max_retries=8` never applied — and why the fix is a table row rather
    than a widened `_sdk_transient`, whose only caller is `_policy_connection` and which a bare
    APIError therefore never reaches.
    """
    transport = _Transport(_sse(_delta(content="tok "), _error_frame()))
    client = _client(transport)
    with pytest.raises(openai.APIError) as caught:
        for _event in client._sdk.chat.completions.create(
                model="m", messages=[{"role": "user", "content": "go"}], stream=True):
            pass

    exc = caught.value
    assert type(exc) is openai.APIError, "the SDK stopped building a bare APIError for error frames"
    assert not isinstance(exc, (openai.APIStatusError, openai.APIConnectionError))
    assert not isinstance(exc, tuple(t for t, _ in OpenAICompatibleClient._RETRY_POLICY[:5]
                                     for t in (t if isinstance(t, tuple) else (t,)))), (
        "an earlier row now claims it, so the ordering argument below has to be re-derived")
    assert str(exc) == LITELLM_CUT, "the run's span error text IS this exception's message"

    handler = next(name for types, name in OpenAICompatibleClient._RETRY_POLICY
                   if types is None or isinstance(exc, types))
    assert handler == "_policy_stream_interrupted", (
        "a cut stream is dispatched to the unclassified tail, which raises without retrying")


# --------------------------------------------------------------------------------------------
# 1. A truncated stream must not end the run.
# --------------------------------------------------------------------------------------------

def test_a_cut_stream_returns_what_it_produced_instead_of_raising(no_sleep):
    """200,438 deltas arrived, then the transport died. That is a truncated ANSWER.

    The run this reproduces spent 1,817.6 s on the call and recorded an ERROR span with a
    zero-second gap to its own last event. One attempt, no second one, nothing kept.
    """
    transport = _Transport(_sse(_delta(content="def solve"), _delta(content="(p): pass"),
                                _error_frame()))
    client = _client(transport)

    assert client.complete_text([{"role": "user", "content": "go"}]) == "def solve(p): pass"
    assert len(transport.requests) == 1, "the salvaged answer was re-asked anyway"
    assert no_sleep == [], "a stream that ANSWERED must not pay a retry backoff"


def test_the_salvaged_answer_is_marked_truncated_rather_than_borrowing_length(no_sleep):
    """The mark is the body's own record that this is a partial answer, and it is what stops the
    cache below. It is deliberately not OpenAI's `"length"`, which would claim a token limit the
    model never hit."""
    transport = _Transport(_sse(_delta(content="partial"), _error_frame()))
    body = _client(transport)._post({"model": "m", "messages": [{"role": "user", "content": "go"}],
                                     "temperature": 0.0})

    assert body["choices"][0]["finish_reason"] == llm.STREAM_TRUNCATED_FINISH_REASON
    assert llm._envelope_is_truncated(body)
    assert body["choices"][0]["finish_reason"] != "length"
    assert OpenAICompatibleClient._keepalive_stall(body, True) is False, (
        "a salvaged answer must not read as a keepalive-only stall and be discarded again")


def test_a_cut_that_produced_nothing_is_retried_WITHOUT_sse(no_sleep):
    """The complement, and the reason this needs no retry cap of its own.

    A retry costs exactly what the aborted attempt generated, so the branch that retries is the one
    where nothing was generated. It degrades off SSE for the same reason `_policy_connection` does:
    the non-stream path reads the body under one wall-clock guard, so an in-band frame cannot cut it
    the same way — and here that is what rescues the call.
    """
    transport = _Transport(_sse(_error_frame()))
    client = _client(transport, max_retries=3)

    assert client.complete_text([{"role": "user", "content": "go"}]) == "answered without sse"
    assert transport.streamed == [True, False], (
        "a barren cut either was not retried at all, or was retried over SSE again")
    assert no_sleep == [llm._backoff(0)]
    assert client._stream_stalls == 1, "the permanent-degrade ratchet ignored a broken stream"


def test_a_persistently_cut_stream_still_ends_as_a_clean_llm_error(no_sleep):
    """Retrying must not become never-failing: only `LLMError` triggers the role layer's own
    retry+fallback, so the budget still has to run out into one."""
    transport = _Transport(_sse(_error_frame()))
    client = _client(transport, max_retries=2, stream=True)
    client._sdk_chat = lambda payload, use_stream: (_ for _ in ()).throw(
        openai.APIError(LITELLM_CUT, request=_REQ, body=None))

    with pytest.raises(LLMError, match="Not enough data"):
        client.complete_text([{"role": "user", "content": "go"}])


# --------------------------------------------------------------------------------------------
# 2. The spend must land in the run's ledger.
# --------------------------------------------------------------------------------------------

def test_a_cut_call_reaches_the_ledger_as_one_unpriced_call(no_sleep):
    """`_stream_envelope_is_billable` already says an envelope that produced content records the
    call it made. That rule reached `complete_text_stream` and not `_post`, so twenty-six real
    provider calls left no row at all: not a cost, not a token, not a `calls` increment.

    What lands is the honest maximum. The provider's usage frame is the LAST thing on the wire and
    a cut stream never carries one, so the amount is genuinely unknown — but `cost_is_reported`'s
    rule is that UNPRICED IS NOT FREE, and `priced_calls` is the counter that says which this was.
    """
    transport = _Transport(_sse(_delta(content="x" * 40), _error_frame()))
    client = _client(transport)

    client.complete_text([{"role": "user", "content": "go"}])

    assert client.accountant.calls == 1, "the call the endpoint really served is missing entirely"
    assert client.accountant.priced_calls == 0, "an unpriced cut must not be recorded as priced"
    assert client.accountant.spent == pytest.approx(0.0)


def test_the_unpriced_cut_still_reaches_the_DURABLE_ledger(no_sleep):
    """"…without writing one word into the durable log" is the half `calls` alone would not fix.

    `CostAccountant` pushes each delta at a sink the engine binds, and that sink DROPS a delta with
    nothing in it (`engine/costs.py::_has_value`) — so an all-zero row would still have left the run
    silent. `calls: 1` is what carries it through, and `priced_calls: 0` is what the row then says:
    a provider call happened here and nobody priced it. This is the seam between this client and the
    engine's ledger, so it is asserted against the engine's OWN gate rather than a copy of it.
    """
    from looplab.engine.costs import _has_value, sanitize_usage_delta

    deltas: list[dict] = []
    transport = _Transport(_sse(_delta(content="cut off here"), _error_frame()))
    client = _client(transport)
    client.accountant.set_sink(deltas.append)

    client.complete_text([{"role": "user", "content": "go"}])

    assert len(deltas) == 1, "the cut call pushed no delta at the ledger"
    clean = sanitize_usage_delta(deltas[0])
    assert _has_value(clean), "the engine's own sink would drop this row and the run stays silent"
    assert clean["calls"] == 1 and clean["priced_calls"] == 0 and clean["cost"] == 0.0


def test_usage_the_stream_DID_report_before_the_cut_is_still_billed(no_sleep):
    """A provider that reports usage incrementally (or a proxy that injects it before the error
    frame) has stated the amount. Salvage must keep it — dropping it would be the same
    under-count in the other direction."""
    transport = _Transport(_sse(_delta(content="hi"), _usage_frame(0.25), _error_frame()))
    client = _client(transport)

    client.complete_text([{"role": "user", "content": "go"}])

    assert client.accountant.spent == pytest.approx(0.25)
    assert client.accountant.priced_calls == 1
    assert client.accountant.completion_tokens == 20


def test_the_cut_and_its_missing_price_are_said_out_loud(caplog, no_sleep):
    """The third half of the incident: nothing anywhere said this happened.

    A salvaged answer is shorter than it should be and its cost is missing from a ledger the
    operator reads as complete, and both facts are invisible from the call site — `complete_text`
    returns a perfectly ordinary string. WARNING for the reason `_policy_throttled`'s backoff notice
    is: it is the level logging's `lastResort` handler puts on stderr in a CLI run that configured
    no logging at all, which is what a benchmark harness is.
    """
    transport = _Transport(_sse(_delta(content="1234567890"), _error_frame()))
    with caplog.at_level("WARNING", logger="looplab.core.llm"):
        _client(transport).complete_text([{"role": "user", "content": "go"}])

    # FILTERED BY LOGGER, matching the `at_level(..., logger="looplab.core.llm")` scope this
    # test already declares. Reading `caplog.records` unfiltered counted ANY logger's warning:
    # after the 2026-08-29 master merge the span-coverage guard
    # (`core/tracing.py::untraced LLM generation`) also fires inside these tests, and this
    # assertion started counting two records for one notice — but only in the FULL suite, so
    # the file passed in isolation and failed together. The subject was always llm.py's notice.
    notices = [r for r in caplog.records if r.levelname == "WARNING" and r.name == "looplab.core.llm"]
    assert len(notices) == 1, "a cut stream is salvaged silently, or says so more than once"
    said = notices[0].getMessage()
    assert "http://x/v1" in said and "10 characters" in said
    assert "UNPRICED" in said, "the notice does not say the money is missing"


def test_a_stream_that_was_NOT_cut_says_nothing(caplog, no_sleep):
    """The other way to make a notice worthless. `_post` runs on every provider call this client
    makes, so a notice that fires on the whole traffic is one nobody reads by the second node."""
    transport = _Transport(_sse(_delta(content="whole answer"),
                                {"id": "1", "object": "chat.completion.chunk", "model": "m",
                                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))
    with caplog.at_level("WARNING", logger="looplab.core.llm"):
        assert _client(transport).complete_text([{"role": "user", "content": "go"}]) == "whole answer"
    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING" and r.name == "looplab.core.llm"] == []


def test_the_notice_names_the_price_when_the_cut_WAS_priced(caplog, no_sleep):
    """The complement, and since the reorder it is the LIVE branch, not the rare one.

    A notice that is wrong half the time is one people learn to ignore, and "UNPRICED — real spend
    this run's ledger cannot state" is a serious claim to make about a call that was in fact priced
    to the cent. The two readings ask different things of the operator: a priced cut is spend
    `llm_budget_usd` will enforce, an unpriced one is spend only the invoice will ever show.
    """
    transport = _Transport(_sse(_delta(content="hi"), _error_frame(),
                                _meter_estimate(0.0617918, 220685)) + _done())
    client = _client(transport)
    with caplog.at_level("WARNING", logger="looplab.core.llm"):
        client.complete_text([{"role": "user", "content": "go"}])

    said = [r.getMessage() for r in caplog.records if r.levelname == "WARNING" and r.name == "looplab.core.llm"][0]
    assert "0.061792" in said, "the notice does not name the price the endpoint stated"
    assert "UNPRICED" not in said, "a priced call is being reported as money nobody can account"


def test_a_truncated_answer_is_never_served_from_the_deterministic_cache(no_sleep):
    """The hazard salvage introduces, closed in the same change.

    The T7 cache exists for exactly the traffic that would hit this: a retry, a panel re-ask, a
    verify pass re-issuing the same temperature-0 prompt. Storing a truncated answer under that key
    turns one cut stream into a permanently amputated answer for the process's lifetime.
    """
    transport = _Transport(_sse(_delta(content="half an ans"), _error_frame()))
    client = _client(transport, cache=True)
    messages = [{"role": "user", "content": "go"}]

    assert client.complete_text(messages) == "half an ans"
    assert client.complete_text(messages) == "half an ans"
    assert len(transport.requests) == 2, "the truncated answer was cached and served again"
    assert not client._cache, "a truncated envelope was stored under a deterministic key"


# --------------------------------------------------------------------------------------------
# The salvage rule itself, and the blast radius of the new table rows.
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kw,expected,why", [
    (dict(produced_content=True, produced_tool_calls=False, produced_reasoning=False), True,
     "content is the measured case: 196k-238k deltas, ~30 min, $0.055-$0.067 to regenerate"),
    (dict(produced_content=False, produced_tool_calls=True, produced_reasoning=False), True,
     "a completion delivered as tool_call arguments carries no content deltas and is still an answer"),
    (dict(produced_content=False, produced_tool_calls=False, produced_reasoning=True), True,
     "reasoning is generated and BILLED work — the same row `_keepalive_stall` refuses to call a stall"),
    (dict(produced_content=False, produced_tool_calls=False, produced_reasoning=False), False,
     "nothing was generated, so a retry costs a backoff — that is the branch that retries"),
])
def test_the_salvage_truth_table(kw, expected, why):
    assert llm._interrupted_stream_is_salvageable(**kw) is expected, why


def test_our_own_accumulation_bug_is_not_laundered_into_a_partial_answer():
    """NARROW on purpose, exactly as `_policy_unparseable` is. If the merge code below raises, that
    is OUR bug: masking it would turn a real defect into a plausible-looking truncated answer that
    every caller downstream would treat as the model's own words."""
    class _Boom:
        def model_dump(self):
            raise ValueError("index merge is broken")

    delta = type("D", (), {"content": "some text", "tool_calls": [_Boom()],
                           "reasoning_content": None})()
    choice = type("C", (), {"delta": delta, "finish_reason": None})()
    event = type("Ev", (), {"choices": [choice], "usage": None})()

    with pytest.raises(ValueError, match="index merge is broken"):
        OpenAICompatibleClient._accumulate_stream(iter([event]))


def test_the_new_rows_did_not_widen_onto_a_request_defect(no_sleep):
    """The cost of getting the table's ORDER wrong. `openai.APIError` is the base of the whole SDK
    error tree, so a single row for it would also claim every status-bearing error no row above
    named — a 404 for a mis-typed model name retried eight times with backoff, ~2.5 minutes of
    waiting for an answer that cannot change. The two rows above it are what hold that still."""
    client = _client(_Transport(b""))
    for status, cls in ((404, openai.NotFoundError), (409, openai.ConflictError),
                        (422, openai.UnprocessableEntityError)):
        exc = cls("nope", response=httpx.Response(status, request=_REQ, json={"error": "nope"}),
                  body={"error": "nope"})
        with pytest.raises(LLMError):
            client._retry_or_raise(exc, 0, use_stream=True)
    assert no_sleep == [], "a permanent request defect now waits out a retry budget"


def test_a_status_less_apierror_on_a_NON_stream_attempt_keeps_failing_fast(no_sleep):
    """Only a STREAM can carry an in-band error frame (the SDK builds this class in `_streaming.py`
    and nowhere else), so a non-stream attempt keeps the answer it has always had rather than
    retrying an error it could not have caused."""
    client = _client(_Transport(b""))
    with pytest.raises(LLMError):
        client._retry_or_raise(openai.APIError("weird", request=_REQ, body=None), 0,
                               use_stream=False)
    assert no_sleep == []


def test_the_response_validation_error_kept_its_fail_fast_answer(no_sleep):
    """`APIResponseValidationError` is the only OTHER member of the status-less family, and it is a
    RESPONSE-SHAPE defect, not a cut stream: retrying it eight times re-buys the same wrong shape."""
    client = _client(_Transport(b""))
    exc = openai.APIResponseValidationError(
        response=httpx.Response(200, request=_REQ, json={}), body=None)
    with pytest.raises(LLMError):
        client._retry_or_raise(exc, 0, use_stream=True)
    assert no_sleep == []


def test_the_predicate_and_the_policy_table_name_the_SAME_family():
    """Two readers of one rule, and §0.8's lesson about what happens when they are two rules.

    `_accumulate_stream` decides whether to salvage with `_inband_stream_error`; `_RETRY_POLICY`
    decides whether to retry by DISPATCHING on classes. If those ever disagree, an exception is
    salvaged by one half and fail-fast-raised by the other. Derived over the SDK's whole exception
    table, so a class the SDK adds later is covered without anyone re-listing it here.
    """
    import inspect

    from openai import _exceptions

    families = [c for _n, c in vars(_exceptions).items()
                if inspect.isclass(c) and issubclass(c, openai.APIError)]
    assert len(families) > 8, "the SDK exception table did not resolve; the derivation is vacuous"

    for cls in families:
        # These classes take incompatible constructor arguments, so build an un-initialised instance
        # of each: `isinstance` — which is all both halves use — is a property of the type, and this
        # is what lets the REAL predicate be asked rather than a copy of it restated here. Restating
        # it is exactly how the two implementations of one rule drift apart (CLAUDE.md §0.8).
        probe = cls.__new__(cls)
        row = next(name for types, name in OpenAICompatibleClient._RETRY_POLICY
                   if types is None or isinstance(probe, types))
        salvages = llm_transient._inband_stream_error(probe)
        assert (row == "_policy_stream_interrupted") == salvages, (
            f"{cls.__name__}: the table routes it to {row} while the salvage predicate "
            f"{'claims' if salvages else 'disclaims'} it")

    # …and the family really is the one the row's name promises, on live instances.
    assert llm._inband_stream_error(openai.APIError("x", request=_REQ, body=None)) is True
    assert llm._inband_stream_error(openai.APITimeoutError(request=_REQ)) is False
    assert llm._inband_stream_error(ValueError("not an sdk error")) is False
    assert llm._inband_stream_error is llm_transient._inband_stream_error, (
        "the barrel re-export stopped naming the owning module's object (CLAUDE.md CO-10)")


def test_a_stall_after_content_still_belongs_to_the_connection_policy(no_sleep):
    """The boundary this fix deliberately does NOT cross.

    An idle-guard kill and a mid-body reset are also mid-stream, and they are also `openai.APIError`
    subclasses — a salvage catch keyed on that base class alone would have swallowed both. But
    `_policy_connection`'s degrade-and-retry has its own measured provenance (a proxied endpoint
    that wedges on SSE and answers the same request in 2 s without it) and this repo has no
    measurement against it, so a stream that produced content and then STALLED still retries.
    """
    class _Wedged:
        def __init__(self):
            self.n = 0

        def __iter__(self):
            return self

        def __next__(self):
            self.n += 1
            if self.n == 1:
                delta = type("D", (), {"content": "half an answer", "tool_calls": None,
                                       "reasoning_content": None})()
                return type("Ev", (), {"choices": [type("C", (), {
                    "delta": delta, "finish_reason": None})()], "usage": None})()
            raise openai.APITimeoutError(request=_REQ)

    with pytest.raises(openai.APITimeoutError):
        OpenAICompatibleClient._accumulate_stream(_Wedged())


def test_the_cut_still_classifies_as_a_protocol_failure():
    """`classify_llm_failure` picks the operator's remedy, and "protocol" is already the honest
    answer for "the endpoint said something we cannot interpret". Retrying the call must not have
    promoted it to `unreachable`, whose remedy ("start the endpoint") is advice that cannot help
    against a gateway that answered HTTP 200 and streamed for half an hour."""
    exc = LLMError("wrapped")
    exc.__cause__ = openai.APIError(LITELLM_CUT, request=_REQ, body=None)
    assert llm.classify_llm_failure(exc) == "protocol"
    assert "protocol" in llm.LLM_FAILURE_CAUSES


# --------------------------------------------------------------------------------------------
# Round two, from live fire (2026-08-24). The first fix kept the run alive and marked the envelope
# truncated, but it stopped reading at the error frame — so the meter's synthesised usage frame,
# which arrives AFTER it, was still lost — and it measured the salvage with the wrong ruler.
#
# The run: `count_riemann_zeta_zeros` arm B a1, cut at 1819.2 s after 220,685 forwarded deltas
# priced $0.0617918 (`cost_basis=estimated_from_deltas`). Its span records `status OK`,
# `output ""`, `thinking` at the 64,000-char trace cap and `usage {0,0,0}` — the answer was
# reasoning-only, the salvage worked, and the money did not land.
# --------------------------------------------------------------------------------------------

def test_the_usage_frame_BEHIND_the_error_frame_still_reaches_the_ledger(no_sleep):
    """The reconciliation the coordinator ran, as a property.

    Every other lane matched the meter to the cent; only the truncated call did not
    ($0.0482 recorded against $0.1100 real, so the run believed it had spent 4.8 % of a $1.00
    ceiling when it had spent 11.0 %). The cause is positional, not arithmetic: the SDK treats the
    first error frame as terminal and closes the response, and the usage frame is BEHIND it.
    """
    transport = _Transport(_sse(_delta(reasoning_content="think "),
                                _error_frame(),
                                _meter_estimate(0.0617918, 220685)) + _done())
    client = _client(transport)

    client.complete_text([{"role": "user", "content": "go"}])

    assert client.accountant.spent == pytest.approx(0.0617918), (
        "the price the endpoint stated for this call is still not in the ledger")
    assert client.accountant.priced_calls == 1 and client.accountant.calls == 1
    assert client.accountant.completion_tokens == 220685
    assert len(transport.requests) == 1, "reading to the end of the stream cost an extra call"


def test_a_cut_that_is_priced_is_STILL_marked_truncated_and_still_uncached(no_sleep):
    """The trap the `[DONE]` frame sets, and the reason this test exists at its own name.

    `Stream.__stream__` BREAKS on `[DONE]`. A reorder that simply queues the error frame behind
    everything else therefore never delivers it at all: the call comes back as an ordinary complete
    answer — correctly billed, but with no truncation mark, so it is CACHEABLE and silent. That is
    strictly worse than the defect being fixed, and it is what the first draft of
    `defer_inband_error` did (caught by replaying the live wire, which ends `usage` then `[DONE]`).
    """
    transport = _Transport(_sse(_delta(content="half "), _error_frame(),
                                _meter_estimate(0.05, 1000)) + _done())
    client = _client(transport, cache=True)
    messages = [{"role": "user", "content": "go"}]

    body = client._post({"model": "m", "messages": messages, "temperature": 0.0})
    assert body["choices"][0]["finish_reason"] == llm.STREAM_TRUNCATED_FINISH_REASON, (
        "a priced cut lost its truncation mark — `[DONE]` swallowed the deferred error")
    assert not client._cache, "a truncated answer is cacheable again"
    assert client.accountant.spent == pytest.approx(0.05)


def test_the_error_still_arrives_when_the_stream_ends_without_a_done_frame(no_sleep):
    """The other termination: the connection simply ends. The held frame must be delivered at the
    end of the iteration too, or a cut stream with no `[DONE]` loses its mark the same way."""
    transport = _Transport(_sse(_delta(content="half "), _error_frame(),
                                _meter_estimate(0.05, 1000)))       # no [DONE]
    body = _client(transport)._post({"model": "m", "messages": [{"role": "user", "content": "go"}],
                                     "temperature": 0.0})
    assert body["choices"][0]["finish_reason"] == llm.STREAM_TRUNCATED_FINISH_REASON
    assert body["usage"]["cost"] == pytest.approx(0.05)


def test_a_barren_cut_still_raises_even_though_the_stream_was_read_to_the_end(no_sleep):
    """Reading further must not turn a failure into an answer. Nothing salvageable is still nothing
    salvageable, and `_policy_stream_interrupted` still owns it — with the ENDPOINT's own words, not
    a generic 'empty body after N attempts'."""
    transport = _Transport(_sse(_error_frame(), _meter_estimate(0.01, 50)) + _done())
    client = _client(transport, max_retries=0)

    with pytest.raises(LLMError, match="Not enough data"):
        client.complete_text([{"role": "user", "content": "go"}])


def test_a_reasoning_only_cut_reports_what_it_kept_not_what_it_lacks(caplog, no_sleep):
    """The misreport, as a property. The notice read `keeping the 0 characters that arrived` about a
    call that had retained 220,685 deltas of reasoning, and the fix was read as broken on live fire.

    A reasoning model cut mid-think is the NORMAL shape here, not a corner: it has spent everything
    on `reasoning_content` and has not begun its answer, which is the case `_keepalive_stall`
    already refuses to call a stall.
    """
    transport = _Transport(_sse(_delta(reasoning_content="x" * 30), _error_frame()))
    client = _client(transport)
    with caplog.at_level("WARNING", logger="looplab.core.llm"):
        msg = client.chat([{"role": "user", "content": "go"}], tools=[])

    assert msg.get("content") == "" and len(msg.get("reasoning") or "") == 30, (
        "precondition: this is a reasoning-only truncation, exactly like the live one")
    said = [r.getMessage() for r in caplog.records if r.levelname == "WARNING" and r.name == "looplab.core.llm"][0]
    assert "30 characters" in said and "30 reasoning" in said, said
    assert "the 0 characters" not in said, "the notice still measures only `content`"


@pytest.mark.parametrize("message,expected", [
    ({"content": "abc"}, {"content": 3, "reasoning": 0, "tool_arguments": 0}),
    ({"content": "", "reasoning": "think"}, {"content": 0, "reasoning": 5, "tool_arguments": 0}),
    ({"content": "", "tool_calls": [{"function": {"name": "emit", "arguments": '{"a":1}'}}]},
     {"content": 0, "reasoning": 0, "tool_arguments": 7}),
    ({}, {"content": 0, "reasoning": 0, "tool_arguments": 0}),
    (None, {"content": 0, "reasoning": 0, "tool_arguments": 0}),
])
def test_the_salvaged_length_table(message, expected):
    """Three carriers, counted separately: `content` is the answer, `reasoning` is what the money
    was spent thinking, `tool_arguments` is an answer that never touches `content` at all."""
    assert llm.salvaged_lengths(message) == expected


def test_the_reorder_leaves_a_healthy_stream_byte_identical(no_sleep):
    """Blast radius. `defer_inband_error` rebinds a private on EVERY streaming call this client
    makes, so a stream with no error frame must come back exactly as it did before."""
    ok = _sse(_delta(content="hello"),
              {"id": "1", "object": "chat.completion.chunk", "model": "m",
               "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10,
                         "cost": 0.01}}) + _done()
    client = _client(_Transport(ok))
    body = client._post({"model": "m", "messages": [{"role": "user", "content": "go"}],
                         "temperature": 0.0})

    assert body["choices"][0]["message"]["content"] == "hello"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert client.accountant.spent == pytest.approx(0.01) and not llm._envelope_is_truncated(body)


def test_the_hook_is_inert_on_anything_that_is_not_an_sdk_stream():
    """It fails SOFT by construction: a plain iterator (every `_accumulate_stream` unit test, a
    foreign client, a future SDK that renames the method) is left exactly as it was, and the caller
    simply never sees a held frame. A hook that raised here would take out the whole client."""
    plain = iter([1, 2, 3])
    assert llm_streaming.defer_inband_error(plain) == {}
    assert list(plain) == [1, 2, 3]
    assert llm_streaming.defer_inband_error(object()) == {}


def test_the_reorder_hooks_the_method_the_sdk_actually_calls():
    """The one private name this depends on, checked against the REAL SDK rather than assumed:
    `__stream__` looks `_iter_events` up on `self` at first iteration, which is what makes rebinding
    the instance attribute reach the live call instead of being a silent no-op."""
    import inspect

    assert "_iter_events" in inspect.getsource(openai.Stream.__stream__), (
        "the SDK no longer routes its iteration through `_iter_events`; the reorder is a no-op")
    assert callable(getattr(openai.Stream, "_iter_events", None))


def test_an_inband_error_wins_over_a_later_socket_timeout(no_sleep):
    """The hazard the reorder introduces, closed with it.

    Holding the error frame back means the read continues; if the endpoint then says nothing and
    keeps the socket open, the idle guard fires and an APITimeoutError arrives INSTEAD. Classifying
    on the exception alone would hand a 220k-token cut generation to `_policy_connection` and re-buy
    thirty minutes of it. The endpoint already told us what happened, so the held frame decides.
    """
    class _CutThenSilent:
        """An SDK-shaped stream: content, an in-band error, then the watchdog's own exception."""

        def __init__(self):
            self.box = {}

        def _iter_events(self):
            yield _FakeSSE(json.dumps({"id": "1", "object": "chat.completion.chunk", "model": "m",
                                       "choices": [{"index": 0, "delta": {"content": "kept"},
                                                    "finish_reason": None}]}))
            yield _FakeSSE(json.dumps({"error": {"message": LITELLM_CUT}}))
            raise openai.APITimeoutError(request=_REQ)

        def __iter__(self):
            for sse in self._iter_events():
                data = json.loads(sse.data)
                if data.get("error"):
                    raise openai.APIError(data["error"]["message"], request=_REQ, body=data["error"])
                yield _chunk(data)

    stream = _CutThenSilent()
    body = OpenAICompatibleClient._accumulate_stream(stream)
    assert body["choices"][0]["message"]["content"] == "kept"
    assert body["choices"][0]["finish_reason"] == llm.STREAM_TRUNCATED_FINISH_REASON, (
        "the socket's own timeout overruled what the endpoint had already said in band")


def test_the_caller_still_sees_the_FIRST_error_the_stream_reported(no_sleep):
    """Which exception the reorder finally delivers is not arbitrary. `Stream.__stream__` raises on
    the FIRST error frame it meets; a reorder that held one and then let a later one through would
    quietly change the diagnosis the operator reads — a gateway's own upstream error replaced by
    whatever it said next."""
    # A BARREN cut, so the held exception is the whole answer rather than being salvaged away.
    transport = _Transport(_sse(_error_frame("FIRST: the upstream cut the stream"),
                                _error_frame("SECOND: and then the gateway gave up")) + _done())
    client = _client(transport, max_retries=0)

    with pytest.raises(LLMError, match="FIRST") as caught:
        client.complete_text([{"role": "user", "content": "go"}])
    assert "SECOND" not in str(caught.value)


@pytest.mark.parametrize("data,is_error,is_done", [
    ('{"error": {"message": "boom"}}', True, False),
    ('{"error": null}', False, False),
    ('{"choices": [], "usage": {"cost": 1}}', False, False),
    ('[DONE]', False, True),
    ('not json at all', False, False),
    ('"a bare string"', False, False),
])
def test_the_frame_classifier_table(data, is_error, is_done):
    """The two frame tests the reorder turns on, stated where they can be read. A frame we cannot
    decode is not a frame we may suppress — answering True there would hold back arbitrary payloads
    and never deliver them."""
    sse = _FakeSSE(data)
    assert llm_streaming._sse_is_error(sse) is is_error
    assert llm_streaming._sse_is_done(sse) is is_done
