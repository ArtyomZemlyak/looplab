"""`_post`'s six concerns are separable units with pinned truth tables (doc 25 CO-05).

`OpenAICompatibleClient._post` was a ~200-line loop body that interleaved T7 cache lookup/zeroing,
the per-attempt stream/degrade decision, a six-way exception ladder, empty-200 classification,
billing of empty-but-billable envelopes, and cache insertion with LRU eviction. Every branch was
incident-motivated and well-commented, but nothing below the whole method was reachable from a test:
the keepalive-stall truth table — the branch with the most expensive failure mode, since a false
"stall" regenerates minutes of reasoning tokens AND ratchets the client permanently off SSE — could
only be probed through a full fake transport.

The extraction is behaviour-preserving, so the existing `_post` tests in `test_openai_client.py`
(timeout retry, fail-fast refuse, reasoning/stream_options drops, empty-stream billing, LRU
eviction) remain the end-to-end proof. What this file adds is the per-unit tables those tests can
only sample, plus the structural guards that keep the concerns apart.
"""
from __future__ import annotations

import ast
import inspect
import json
import textwrap

import httpx as _httpx
import openai as _openai
import pytest

import looplab.core.llm as llm
from looplab.core.llm import LLMError, OpenAICompatibleClient

_REQ = _httpx.Request("POST", "http://x/v1/chat/completions")


def _client(**kw) -> OpenAICompatibleClient:
    kw.setdefault("cache", True)          # `_cache` is None unless caching is on (see `_cache_key`)
    return OpenAICompatibleClient("m", base_url="http://x/v1", **kw)


def _status(cls, code, body):
    return cls("err", response=_httpx.Response(code, request=_REQ, json=body), body=body)


def _conn(cause=None):
    e = _openai.APIConnectionError(message="connection error", request=_REQ)
    e.__cause__ = cause
    return e


def _decode_error():
    try:
        json.loads("")
    except json.JSONDecodeError as e:
        return e
    raise AssertionError("json.loads('') must raise")           # pragma: no cover


@pytest.fixture
def no_sleep(monkeypatch):
    """Record backoff instead of serving it — every policy branch that retries must also back off."""
    slept: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: slept.append(s))
    return slept


# --------------------------------------------------------------------------------------------
# The keepalive-stall classifier: the table that was previously unreachable below `_post`.
# --------------------------------------------------------------------------------------------

def _streamed(message: dict, finish=None) -> dict:
    return {"choices": [{"message": message, "finish_reason": finish}]}


@pytest.mark.parametrize("parsed,use_stream,expected,why", [
    (None, True, False, "an unparseable body is the OTHER retry case, not a stall"),
    ({}, True, False, "no `choices` at all is an {'error': ...} envelope — the no-choices check owns it"),
    (_streamed({}), False, False, "a NON-stream empty body is never a stream stall"),
    (_streamed({}), True, True, "no content, no tool_calls, no reasoning, no finish_reason = a stall"),
    (_streamed({"content": "hi"}), True, False, "content is a real answer"),
    (_streamed({"content": "", "tool_calls": [{"id": "1"}]}), True, False, "a tool call is a real answer"),
    # The two rows with real incident history: a reasoning model that spent its budget thinking.
    (_streamed({"content": "", "reasoning": "thinking…"}), True, False,
     "reasoning WITHOUT finish_reason is still generated (and billed) work, not a stall"),
    (_streamed({"content": ""}, finish="length"), True, False,
     "finish_reason=length is a TRUNCATED answer; retrying it 5x regenerates minutes of reasoning"),
    (_streamed({"content": ""}, finish="stop"), True, False,
     "finish_reason=stop means the endpoint answered — let the no-choices check decide"),
])
def test_the_keepalive_stall_table(parsed, use_stream, expected, why):
    assert OpenAICompatibleClient._keepalive_stall(parsed, use_stream) is expected, why


def test_a_stalled_stream_is_billed_before_it_is_discarded():
    """A keepalive-only stream is a completed, BILLABLE provider call. Accounting only the ACCEPTED
    body let up to `_max_retries` empty attempts spend real money that never reached the cost limits
    or the durable ledger — with a flapping endpoint the run's recorded spend drifted arbitrarily far
    below the invoice."""
    c = _client()
    c._account_keepalive_stall({"usage": {"prompt_tokens": 7, "completion_tokens": 0,
                                          "total_tokens": 7, "cost": 0.25}})
    assert c.accountant.spent == pytest.approx(0.25)
    assert c._last_usage["total_tokens"] == 7


def test_a_stalled_stream_with_no_usage_is_not_billed():
    """The complement: a heartbeat-only stream that reported nothing must not fabricate a charge, and
    must not clobber `_last_usage` with zeros the caller would trace as a real call."""
    c = _client()
    c._last_usage = {"total_tokens": 99, "cost": 1.0}
    c._account_keepalive_stall({"usage": {}})
    c._account_keepalive_stall(None)
    assert c.accountant.spent == pytest.approx(0.0)
    assert c._last_usage["total_tokens"] == 99


# --------------------------------------------------------------------------------------------
# The retry-policy table. RETURN = retry (backoff already served); the flag is the SSE ratchet.
# --------------------------------------------------------------------------------------------

def test_a_transient_timeout_retries_with_backoff(no_sleep):
    c = _client()
    assert c._retry_or_raise(_openai.APITimeoutError(request=_REQ), 0, use_stream=False) is False
    assert no_sleep == [llm._backoff(0)]


def test_a_stream_timeout_ratchets_the_degrade_counter_and_asks_for_non_stream(no_sleep):
    """The returned flag is what makes the NEXT attempt drop SSE; `_stream_stalls` is what makes this
    client drop SSE for good. A flaky proxied endpoint often answers the SAME request fine without
    SSE while its stream wedges mid-generation."""
    c = _client()
    assert c._retry_or_raise(_openai.APITimeoutError(request=_REQ), 0, use_stream=True) is True
    assert c._stream_stalls == 1


def test_a_non_stream_timeout_leaves_the_degrade_counter_alone(no_sleep):
    """Otherwise a plain HTTP timeout would push a healthy client toward permanent non-streaming."""
    c = _client()
    c._retry_or_raise(_openai.APITimeoutError(request=_REQ), 0, use_stream=False)
    assert c._stream_stalls == 0


def test_the_last_attempt_raises_instead_of_asking_for_another(no_sleep):
    c = _client()
    with pytest.raises(LLMError):
        c._retry_or_raise(_openai.APITimeoutError(request=_REQ), c._max_retries, use_stream=False)
    assert no_sleep == [], "a raise must not pay for a backoff nobody will use"


def test_a_refused_connection_fails_fast_without_backoff(no_sleep):
    """Steady state ('endpoint down or misconfigured'), not a hiccup — /api/llm/health stays instant."""
    c = _client()
    with pytest.raises(LLMError):
        c._retry_or_raise(_conn(_httpx.ConnectError("refused")), 0, use_stream=False)
    assert no_sleep == []


def test_a_mid_read_protocol_drop_is_transient(no_sleep):
    c = _client()
    assert c._retry_or_raise(_conn(_httpx.RemoteProtocolError("server disconnected")), 0,
                             use_stream=False) is False
    assert no_sleep


def test_a_positive_retry_after_wins_over_our_backoff(no_sleep):
    c = _client()
    exc = _status(_openai.RateLimitError, 429, {"error": "slow down"})
    exc.response.headers["retry-after"] = "3"
    c._retry_or_raise(exc, 0, use_stream=False)
    assert no_sleep == [3.0]


def test_a_retry_after_directive_is_capped(no_sleep):
    """An hour-long directive from a shared endpoint must not park the run for an hour."""
    c = _client()
    exc = _status(_openai.RateLimitError, 429, {"error": "slow down"})
    exc.response.headers["retry-after"] = "3600"
    c._retry_or_raise(exc, 0, use_stream=False)
    assert no_sleep == [llm.RETRY_AFTER_CAP_S]


def test_a_non_positive_retry_after_falls_back_to_backoff(no_sleep):
    """`Retry-After: 0` (or a date already in the past, from clock skew) parses to 0.0. Honoring it
    would sleep(0) and burn every retry in milliseconds, defeating the 429 backoff entirely."""
    c = _client()
    exc = _status(_openai.RateLimitError, 429, {"error": "slow down"})
    exc.response.headers["retry-after"] = "0"
    c._retry_or_raise(exc, 0, use_stream=False)
    assert no_sleep == [llm._backoff(0)] and no_sleep != [0.0]


def test_a_throttle_403_retries_but_a_real_403_does_not(no_sleep):
    """A hosted gateway returns 403 when a request BURST trips its abuse policy — observed live as
    'Access denied by security policy'. A backed-off retry rides through it; a bad key must not."""
    c = _client()
    throttle = _status(_openai.PermissionDeniedError, 403,
                       {"success": False, "error": "Access denied by security policy"})
    assert c._retry_or_raise(throttle, 0, use_stream=False) is False
    with pytest.raises(LLMError):
        c._retry_or_raise(_status(_openai.PermissionDeniedError, 403, {"error": "forbidden"}), 0,
                          use_stream=False)


def test_a_bad_key_names_the_setting_to_fix(no_sleep):
    c = _client()
    with pytest.raises(LLMError, match="LOOPLAB_LLM_API_KEY"):
        c._retry_or_raise(_status(_openai.AuthenticationError, 401, {"error": "nope"}), 0,
                          use_stream=False)


def test_a_stream_options_reject_is_checked_before_the_reasoning_reject(no_sleep):
    """Order is load-bearing: `_is_reasoning_reject`'s generic keys ('unrecognized', …) also match a
    stream_options rejection, so letting reasoning win would drop the reasoning toggle and retry with
    the ACTUAL offending field still attached — the identical 400, every attempt."""
    c = _client(reasoning={"reasoning_effort": "high"})
    body = {"error": {"message": "unrecognized request argument supplied: stream_options"}}
    assert c._retry_or_raise(_status(_openai.BadRequestError, 400, body), 0, use_stream=True) is False
    assert c._stream_options_ok is False
    assert c._reasoning_ok is True, "the reasoning toggle was not the offending field"


def test_a_final_attempt_param_reject_explains_that_a_retry_will_succeed(no_sleep):
    """The ratchet already flipped, so the caller's own retry/fallback WILL work. Surfacing the
    generic 'no response after retries' instead would send a reader hunting a transport problem."""
    c = _client(reasoning={"reasoning_effort": "high"})
    body = {"error": {"message": "litellm.UnsupportedParamsError: unrecognized: ['reasoning_effort']"}}
    with pytest.raises(LLMError, match="will succeed"):
        c._retry_or_raise(_status(_openai.BadRequestError, 400, body), c._max_retries,
                          use_stream=False)
    assert c._reasoning_ok is False


def test_a_plain_bad_request_is_not_retried(no_sleep):
    c = _client()
    with pytest.raises(LLMError):
        c._retry_or_raise(_status(_openai.BadRequestError, 400, {"error": "bad schema"}), 0,
                          use_stream=False)
    assert no_sleep == []


def test_an_unparseable_body_retries_then_names_itself(no_sleep):
    """A gateway 200 with a keepalive-only body makes the SDK's decoder raise a RAW
    json.JSONDecodeError — NOT an openai.APIError — which without a policy branch would escape `_post`
    uncaught and abort the run (the role layer only retries+falls back on LLMError)."""
    c = _client()
    assert c._retry_or_raise(_decode_error(), 0, use_stream=False) is False
    with pytest.raises(LLMError, match="unparseable body"):
        c._retry_or_raise(_decode_error(), c._max_retries, use_stream=False)


def test_an_unclassified_sdk_error_still_becomes_a_clean_llm_error(no_sleep):
    """The ladder's final `except openai.APIError`: whatever the SDK grows next must not escape as a
    raw transport exception, because only LLMError triggers the role layer's retry+fallback."""
    c = _client()
    with pytest.raises(LLMError):
        c._retry_or_raise(_openai.APIError("weird", request=_REQ, body=None), 0, use_stream=False)


# --------------------------------------------------------------------------------------------
# The T7 cache accessors.
# --------------------------------------------------------------------------------------------

def test_a_cache_hit_is_a_deep_copy_with_every_billed_counter_zeroed():
    """Callers mutate the body they get back (complete_text -> _apply_native_tool_calls), and trace
    aggregation sums whatever `usage` says — a shared entry with live counters would both corrupt
    later hits and double-bill tokens `CostAccountant` correctly declined to charge."""
    c = _client()
    body = {"choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10, "cost": 0.5}}
    c._cache_put("k", body)

    first = c._cache_get("k")
    assert first["choices"][0]["message"]["content"] == "ok"
    assert first["usage"]["total_tokens"] == 0 and first["usage"]["cost"] == 0.0
    assert c._last_usage["cost"] == 0.0

    first["choices"][0]["message"]["content"] = "MUTATED"
    assert c._cache_get("k")["choices"][0]["message"]["content"] == "ok"
    assert body["usage"]["total_tokens"] == 10, "the caller's body must not be zeroed in place"


def test_an_uncacheable_request_round_trips_as_a_miss():
    """`_cache_key` returns None for a SAMPLE (temperature>0) — caching those would collapse the
    diversity best-of-N, the researcher panel and the novelty re-propose all depend on."""
    c = _client()
    c._cache_put(None, {"choices": [{"message": {}}]})
    assert c._cache_get(None) is None
    assert not c._cache


def test_the_accessors_are_inert_when_caching_is_disabled():
    c = _client()
    c._cache = None
    c._cache_put("k", {"choices": []})
    assert c._cache_get("k") is None


def test_the_cache_is_a_unit_with_its_own_bound_and_lock():
    """The map, its bound and the lock pairing each read-modify-write were three loose attributes on
    the client, so every reader had to know that a hit's `move_to_end` and a put's eviction are each
    a PAIR of OrderedDict ops — and worker threads share one client (doc 25 CO-05)."""
    from looplab.core.llm import _ResponseCache

    cache = _ResponseCache(max_entries=2)
    assert len(cache) == 0 and "k" not in cache
    cache.put("k", {"n": 1})
    assert len(cache) == 1 and "k" in cache and list(cache) == ["k"]


def test_the_cache_copies_in_both_directions():
    """Neither the caller's body nor the stored entry may be reachable by reference from the other."""
    from looplab.core.llm import _ResponseCache

    cache = _ResponseCache()
    body = {"choices": [{"message": {"content": "ok"}}]}
    cache.put("k", body)

    body["choices"][0]["message"]["content"] = "MUTATED BY THE CALLER"
    assert cache.peek("k")["choices"][0]["message"]["content"] == "ok"

    got = cache.get("k")
    got["choices"][0]["message"]["content"] = "MUTATED BY THE READER"
    assert cache.peek("k")["choices"][0]["message"]["content"] == "ok"


def test_a_miss_is_none_not_a_key_error():
    from looplab.core.llm import _ResponseCache

    cache = _ResponseCache()
    assert cache.get("absent") is None and cache.peek("absent") is None


def test_peek_does_not_refresh_recency():
    """`peek` exists for introspection; letting it bump recency would make a test's own look change
    which entry the next eviction takes."""
    from looplab.core.llm import _ResponseCache

    cache = _ResponseCache(max_entries=2)
    cache.put("a", {}); cache.put("b", {})
    cache.peek("a")
    cache.put("c", {})
    assert set(cache) == {"b", "c"}, "peek refreshed 'a' and evicted the wrong entry"


def test_the_cache_evicts_the_least_recently_used():
    c = _client()
    c._cache.max_entries = 3
    for i in range(3):
        c._cache_put(f"k{i}", {"n": i})
    c._cache_get("k0")                       # k0 becomes the most recent, so k1 is now the victim
    c._cache_put("k3", {"n": 3})
    assert set(c._cache) == {"k0", "k2", "k3"}


# --------------------------------------------------------------------------------------------
# Structural: the concerns must stay apart.
# --------------------------------------------------------------------------------------------

def _body(fn) -> ast.FunctionDef:
    return ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]


def test_post_delegates_its_retry_policy_instead_of_re_growing_the_ladder():
    """The finding was a ~200-line method with six interleaved concerns. A single catch that hands
    off to `_retry_or_raise` is what keeps a new provider quirk from becoming a seventh inline
    branch — the ladder had already reached six."""
    fn = _body(OpenAICompatibleClient._post)
    handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
    assert len(handlers) == 1, (
        f"`_post` grew back to {len(handlers)} inline exception branches; the retry policy belongs "
        "in `_retry_or_raise` where it is unit-testable")
    assert (fn.end_lineno - fn.lineno + 1) < 110, "`_post` is drifting back toward the 199-line method"


def test_post_still_catches_the_raw_decode_error_that_is_not_an_sdk_error():
    """The one hazard of collapsing the ladder to a single `except`: `json.JSONDecodeError` does NOT
    derive from `openai.APIError`, so narrowing the catch to the SDK base would let a keepalive-only
    gateway body escape `_post` and abort the whole run."""
    caught = {ast.unparse(t)
              for h in ast.walk(_body(OpenAICompatibleClient._post))
              if isinstance(h, ast.ExceptHandler)
              for t in (h.type.elts if isinstance(h.type, ast.Tuple) else [h.type])}
    assert "openai.APIError" in caught
    assert "json.JSONDecodeError" in caught
    assert not issubclass(json.JSONDecodeError, _openai.APIError), (
        "the two-family catch is only redundant if the SDK starts owning decode errors")


def _policy_handlers():
    return [getattr(OpenAICompatibleClient, name)
            for _types, name in OpenAICompatibleClient._RETRY_POLICY]


def test_the_policy_table_is_ordered_and_ends_in_a_catch_all():
    """A dict keyed on type would lose both properties this table needs. ORDER: a stream_options
    rejection also matches `_is_reasoning_reject`'s generic keys, so BadRequest must be tried before
    anything that could claim it. CATCH-ALL: whatever the SDK grows next must still become a clean
    LLMError, because only LLMError triggers the role layer's retry+fallback."""
    table = OpenAICompatibleClient._RETRY_POLICY
    assert table[0][0] is _openai.BadRequestError
    assert [t for t, _ in table[:-1]] == [t for t, _ in table[:-1] if t is not None], (
        "a catch-all before the end makes every entry after it dead")
    assert table[-1][0] is None, "the table falls through with no handler"


def test_every_declared_handler_exists_and_every_handler_is_declared():
    """Two-way registry guard, the same discipline CLAUDE.md applies to the other duck-typed seams:
    a handler renamed out of the table stops running and NOTHING says so."""
    declared = {name for _t, name in OpenAICompatibleClient._RETRY_POLICY}
    defined = {n for n in vars(OpenAICompatibleClient) if n.startswith("_policy_")}
    assert declared == defined, (declared ^ defined)
    for name in declared:
        assert callable(getattr(OpenAICompatibleClient, name))


def test_no_policy_handler_returns_on_a_path_it_cannot_retry():
    """Fail-closed by construction: a `return` means "attempt again", so a handler that fell through
    without raising would silently retry a hard 401/400 until the retries ran out and then report the
    misleading generic 'no response after retries'."""
    for fn in _policy_handlers() + [OpenAICompatibleClient._retry_or_raise]:
        tree = _body(fn)
        # `ast.Call` is allowed ONLY for the dispatcher, whose single `return` IS the delegation to
        # the selected handler. Allowing it for the six handlers too — which is what a blanket
        # widening did — lets `return dict().get("stalled")` through: a call that evaluates to None,
        # exactly the shape the message below forbids, in exactly the place it matters.
        allowed = ((ast.Constant, ast.Name, ast.Call)
                   if fn is OpenAICompatibleClient._retry_or_raise
                   else (ast.Constant, ast.Name))
        for node in [n for n in ast.walk(tree) if isinstance(n, ast.Return)]:
            assert isinstance(node.value, allowed), (
                f"{fn.__name__}: a retry decision must be a plain flag"
                + (" or a delegated call" if len(allowed) == 3 else "")
                + ", not an expression that could evaluate to None and read as 'no stall'")
        assert isinstance(tree.body[-1], ast.Raise), (
            f"{fn.__name__} falls off its end without raising — an unclassified error would return "
            "None, which `_post` reads as 'retry, not stalled'")


def test_the_dispatcher_holds_no_policy_of_its_own():
    """The point of the table: a new provider quirk is a new ENTRY, not a seventh inline branch."""
    tree = _body(OpenAICompatibleClient._retry_or_raise)
    tests = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "isinstance"]
    assert len(tests) == 1, "the dispatcher grew its own type checks alongside the table"
