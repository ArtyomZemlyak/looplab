"""`OpenAICompatibleClient` transport safety: the wedged-call abort must not kill healthy siblings."""
from __future__ import annotations


def test_streaming_sibling_is_visible_to_the_pool_teardown_guard():
    """A stream past its headers still owns a pooled connection and must block the pool-wide abort.

    `_bounded_create`'s worker returns the Stream the MOMENT headers land, so `_inflight` is already
    back at 0 while the caller thread spends minutes reading the body. `_alone` therefore saw a
    healthy multi-minute generation as absent and ran `_shutdown_pool_sockets` + rebuilt the client
    under it — the exact failure the guard exists to prevent, in the DEFAULT (streaming) mode.
    """
    import time

    from looplab.core.llm import OpenAICompatibleClient

    client = OpenAICompatibleClient(base_url="http://x/v1", api_key="k", model="m")

    class _Stream:
        def __iter__(self):
            for _ in range(2):
                time.sleep(0.05)
                yield None

        def close(self):
            pass

    class _SDK:
        class chat:
            class completions:
                @staticmethod
                def create(**_kwargs):
                    return _Stream()

    client._sdk = _SDK()
    handle = client._bounded_create({"stream": True}, 5.0)
    assert client._inflight == 0, "precondition: the header wait is already over"

    with client._streaming_body():
        # A wedged sibling holds `_inflight == 1` of its own; the real abort path then asks exactly
        # this predicate. With a live stream body present it must refuse the pool-wide teardown.
        client._inflight += 1
        try:
            assert not client._pool_teardown_is_safe_locked(), (
                "a stream mid-body is invisible to the teardown guard, so a wedged sibling would "
                "shut down the shared pool and kill this live generation")
        finally:
            client._inflight -= 1
        for _ in handle:
            pass
    assert client._stream_inflight == 0          # released on exit
    # With no stream in flight the wedged call really is alone and may tear the pool down.
    client._inflight += 1
    try:
        assert client._pool_teardown_is_safe_locked()
    finally:
        client._inflight -= 1


def test_inflight_is_not_leaked_when_the_worker_thread_cannot_start():
    """`_call`'s finally is the only decrement, so a failed `th.start()` must undo the increment.

    Leaking it pins `_alone` False for the process lifetime, disabling the wedged-call abort for
    every later call — and thread exhaustion is precisely when that path is needed.
    """
    import threading

    from looplab.core.llm import OpenAICompatibleClient

    client = OpenAICompatibleClient(base_url="http://x/v1", api_key="k", model="m")
    before = client._inflight

    real_thread = threading.Thread

    class _NoStart(real_thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    threading.Thread = _NoStart
    try:
        try:
            client._bounded_create({"stream": False}, 1.0)
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected the thread-start failure to propagate")
    finally:
        threading.Thread = real_thread
    assert client._inflight == before, "the in-flight counter leaked when the thread never started"


def test_a_stream_holds_exactly_one_inflight_slot_from_header_wait_through_body():
    """A wedged stream must count itself EXACTLY once — never twice, never zero.

    Two arrangements were wrong before this. Nesting `_bounded_create` inside `_streaming_body` while
    `_bounded_create` also took its own `_inflight` slot charged ONE call to both counters, so
    `_pool_teardown_is_safe_locked` saw a phantom sibling and a black-holed stream skipped the socket
    shutdown + client rebuild the abort path exists for. Un-nesting fixed that but left a handoff
    window where NEITHER counter held the stream — `_bounded_create` returns only after its worker
    decremented `_inflight`, and `_streaming_body` had not yet incremented — long enough for a
    sibling wedged in its own header wait to judge the teardown safe and rip this live socket away.

    The invariant that rules both out: the total is 1 for the whole call, header wait and body alike.
    """
    from looplab.core.llm import OpenAICompatibleClient

    client = OpenAICompatibleClient(base_url="http://x/v1", api_key="k", model="m",
                                    stream=True, timeout=0.3, header_timeout=0.3)
    during_headers: list[tuple[int, int, bool]] = []
    during_body: list[tuple[int, int]] = []

    def _chunks():
        with client._inflight_lock:
            during_body.append((client._inflight, client._stream_inflight))
        return
        yield        # pragma: no cover - generator with no chunks

    class _SDK:
        class chat:
            class completions:
                @staticmethod
                def create(**_kwargs):
                    # Sampled from INSIDE the header wait, where the abort decision is made.
                    with client._inflight_lock:
                        during_headers.append((client._inflight, client._stream_inflight,
                                               client._pool_teardown_is_safe_locked()))
                    return _chunks()

    client._sdk = _SDK()
    # Sampled at the ENTRY to `_bounded_create` — the instant that proves there is no handoff window.
    # Under the un-nested arrangement the slot was taken only AFTER create() returned, so a live
    # header-complete stream was momentarily counted by neither cell.
    on_entry: list[tuple[int, int]] = []
    real_bounded = client._bounded_create

    def _spy(kwargs, join_s, **kw):
        with client._inflight_lock:
            on_entry.append((client._inflight, client._stream_inflight))
        return real_bounded(kwargs, join_s, **kw)

    client._bounded_create = _spy
    client._sdk_chat({"model": "m", "messages": []}, use_stream=True)

    assert on_entry and sum(on_entry[0]) == 1, (
        f"the slot was not held when create() began ({on_entry}) — a sibling sampling this window "
        "would judge the pool teardown safe and rip the socket out from under a live stream")
    assert during_headers, "the transport never reached the SDK"
    inflight, stream_inflight, alone = during_headers[0]
    assert inflight + stream_inflight == 1, (inflight, stream_inflight)   # counted ONCE, not twice
    assert alone, "a lone wedged stream would refuse its own teardown"
    assert during_body, "the body was never iterated"
    assert sum(during_body[0]) == 1, during_body[0]      # ...and still counted during the body

    # The slot is released once the call is over — no leak across calls.
    with client._inflight_lock:
        assert (client._inflight, client._stream_inflight) == (0, 0)


def test_a_sibling_that_starts_during_the_abort_never_binds_the_doomed_client():
    """The wedged-call abort must not rip the pool out from under a call that starts DURING it.

    `_alone` was read under `_inflight_lock`, the lock released, and only then was the pool shut down
    and `self._sdk` rebuilt — a window of the whole shutdown+close+join(5) in which a sibling could
    bind the client about to be destroyed and have its fresh connection killed. That is precisely the
    spurious sibling failure the in-flight counting exists to prevent. The check and the swap now
    happen under one hold, and every call binds its client under the same lock, so a call either
    counted itself in BEFORE the check (forbidding the teardown) or gets the replacement.
    """
    import threading

    import pytest

    from looplab.core import llm as llm_mod
    from looplab.core.llm import OpenAICompatibleClient

    client = OpenAICompatibleClient(base_url="http://x/v1", api_key="k", model="m")
    started = threading.Event()
    sibling_sdks: list = []

    class _WedgedSDK:
        """The client the abort is about to tear down; its `create` never returns."""
        _client = None

        class chat:
            class completions:
                @staticmethod
                def create(**_kwargs):
                    started.set()
                    threading.Event().wait(30)      # wedged in "recv" forever

    class _FreshSDK:
        _client = None

        class chat:
            class completions:
                @staticmethod
                def create(**_kwargs):
                    return {"ok": True}

    client._sdk = wedged = _WedgedSDK()
    client._new_sdk = lambda: _FreshSDK()
    # Observe which client a call starting DURING the teardown binds. `_shutdown_pool_sockets` is the
    # first thing the abort does after publishing the replacement, so a sibling launched from here
    # races the rest of the teardown exactly as a real one would.
    torn_down: list = []
    real_shutdown = llm_mod._shutdown_pool_sockets

    def _observing_shutdown(http_client):
        torn_down.append(http_client)
        sibling = threading.Thread(
            target=lambda: sibling_sdks.append(client._sdk), daemon=True)
        sibling.start()
        sibling.join(5)
        return real_shutdown(http_client)

    llm_mod._shutdown_pool_sockets = _observing_shutdown
    try:
        with pytest.raises(Exception):              # APITimeoutError after the join deadline
            client._bounded_create({"stream": False}, 0.2)
    finally:
        llm_mod._shutdown_pool_sockets = real_shutdown
    assert started.is_set(), "precondition: the wedged call really did reach the transport"

    assert torn_down == [None], "the abort tore down exactly the WEDGED client's http client"
    assert sibling_sdks and sibling_sdks[0] is not wedged, (
        "a call starting during the abort bound the client the teardown was destroying")
    assert isinstance(client._sdk, _FreshSDK)


# ------------------------------------------------------- the failure CAUSE, and the retry budget
def _sdk_status_error(status: int, body: dict):
    """A real `openai` error of the class the SDK maps `status` to, over a real httpx response."""
    import httpx
    import openai
    request = httpx.Request("POST", "http://x/v1/chat/completions")
    response = httpx.Response(status, json={"error": body}, request=request,
                              headers={"retry-after": "60"} if status == 429 else {})
    # The SDK's own status -> class mapping (`openai._base_client._make_status_error`): an exact
    # match for the 4xx family, and EVERY 5xx as InternalServerError — whose class attribute reads
    # 500, so a 502/503 does not match itself. Mirroring the real mapping matters twice over here:
    # `_RETRY_POLICY` dispatches on the CLASS (so a plain APIStatusError would reach the
    # unclassified tail and never retry), while `classify_llm_failure` reads the STATUS off the
    # response — and the two must agree about what a live 503 is.
    cls = openai.APIStatusError
    if status >= 500:
        cls = openai.InternalServerError
    else:
        for candidate in (openai.BadRequestError, openai.AuthenticationError,
                          openai.PermissionDeniedError, openai.NotFoundError, openai.ConflictError,
                          openai.UnprocessableEntityError, openai.RateLimitError):
            if getattr(candidate, "status_code", None) == status:
                cls = candidate
                break
    return cls("boom", response=response, body=body)


def test_the_failure_cause_truth_table_is_read_off_the_status_not_the_message():
    """The rule a refusal's remedy hangs off, stated where it can be reviewed (CLAUDE.md guard #2).

    Every row was measured before it was written down: 401 and 403 against the team endpoint
    (`team_model_access_denied` for a model outside the allow-list, including for a `:max` / `:high`
    tier suffix), a real 429 carrying `Current limit: 50` and `Retry-After: 60`, 500/502/503 and an
    empty 200 from a local stub, and a refused connection. The point of the table is that its two
    ENDS demand opposite actions: "throttled" is an endpoint that is up, correctly configured and
    answering, so "start the endpoint, or point LOOPLAB_LLM_BASE_URL at one" — the single remedy the
    refusal used to give ALL of these — is advice that cannot possibly help there.
    """
    import httpx
    import openai
    from looplab.core.errors import LLMError
    from looplab.core.llm import LLM_FAILURE_CAUSES, classify_llm_failure

    rows = [
        (401, {"message": "Invalid proxy server token passed"}, "credential"),
        (403, {"message": "team not allowed to access model. Tried to access qwen3.5-122b:max"},
         "model"),
        (403, {"message": "Access denied by security policy"}, "throttled"),   # a burst throttle
        (429, {"message": "Rate limit exceeded ... Current limit: 50"}, "throttled"),
        (500, {"message": "internal server error"}, "overloaded"),
        (502, {"message": "Bad gateway"}, "overloaded"),
        (503, {"message": "The model is overloaded"}, "overloaded"),
        # 408 keeps the 5xx company because it says the same thing to the operator, even though the
        # SDK gives it no class of its own (so it is a plain APIStatusError the retry policy does NOT
        # retry — the classification is about the ADVICE, not about retryability).
        (408, {"message": "Request Timeout"}, "overloaded"),
        (400, {"message": "model `nope` does not exist"}, "model"),
        (404, {"message": "The model `nope` does not exist"}, "model"),
        (409, {"message": "conflict"}, "protocol"),
    ]
    for status, body, expected in rows:
        sdk_error = _sdk_status_error(status, body)
        assert classify_llm_failure(sdk_error) == expected, status
        # …and through the wrapper the client actually raises. `_post` raises `LLMError(...) from
        # exc` at every policy site, so the classification has to survive a hop of wrapping — that
        # is the ONLY form the preflight ever sees.
        wrapped = LLMError(f"LLM request to http://x/v1 failed: {sdk_error}")
        wrapped.__cause__ = sdk_error
        assert classify_llm_failure(wrapped) == expected, status

    assert classify_llm_failure(
        openai.APIConnectionError(request=httpx.Request("POST", "http://x/v1"))) == "unreachable"
    assert classify_llm_failure(
        openai.APITimeoutError(request=httpx.Request("POST", "http://x/v1"))) == "unreachable"
    # NOT "unreachable": defaulting an unclassifiable failure to the diagnosis with the loudest
    # remedy ("start the endpoint") is the whole defect this function was added to remove.
    assert classify_llm_failure(LLMError("returned an unparseable body")) == "protocol"
    assert classify_llm_failure(RuntimeError("a custom transport said something")) == "protocol"
    assert {row[2] for row in rows} | {"unreachable"} <= set(LLM_FAILURE_CAUSES)


def test_a_cause_chain_cycle_or_depth_cannot_hang_the_classifier():
    """`__cause__` is caller-settable, so the walk is bounded and cycle-safe by construction."""
    from looplab.core.errors import LLMError
    from looplab.core.llm import classify_llm_failure

    first, second = LLMError("a"), LLMError("b")
    first.__cause__, second.__cause__ = second, first
    assert classify_llm_failure(first) == "protocol"

    deep = _sdk_status_error(429, {"message": "rate limited"})
    for _ in range(20):                       # buried far below the bounded walk
        wrapper = LLMError("wrapped")
        wrapper.__cause__ = deep
        deep = wrapper
    assert classify_llm_failure(deep) == "protocol"


def test_an_over_budget_retry_after_ends_the_retries_instead_of_being_clamped(monkeypatch):
    """A caller with a wall-clock budget must not silently SERVE a minutes-long Retry-After.

    Measured against the team endpoint's real `Retry-After: 60`: the launch preflight, which
    advertises a 60 s bound, slept 2 x 60 s and refused anyway — 121.4 s in which it printed nothing
    at all. Refusing an out-of-range directive rather than clamping it is the same rule
    `engine/widths.py` settles concurrency widths by, and the refusal quotes the SERVER's number so
    the operator learns the wait instead of serving it.
    """
    import pytest
    from looplab.core.errors import LLMError
    from looplab.core.llm import OpenAICompatibleClient

    slept: list[float] = []
    monkeypatch.setattr("looplab.core.llm.time.sleep", slept.append)
    throttled = _sdk_status_error(429, {"message": "Current limit: 50"})   # carries Retry-After: 60

    budgeted = OpenAICompatibleClient(base_url="http://x/v1", api_key="k", model="m",
                                      max_retries=2, retry_after_cap=15.0)
    with pytest.raises(LLMError) as raised:
        budgeted._retry_or_raise(throttled, 0, use_stream=False)
    assert slept == [], "an over-budget directive must not be slept at all"
    assert "60s" in str(raised.value) and "15s" in str(raised.value)
    assert raised.value.__cause__ is throttled          # the cause survives -> still "throttled"

    # UNDER the cap the wait is still served, so a short blip is still ridden out…
    short = _sdk_status_error(503, {"message": "overloaded"})              # no Retry-After header
    assert budgeted._retry_or_raise(short, 0, use_stream=False) is False
    assert slept == [2.0], "our own backoff is inside any sane budget and must still be served"

    # …and a caller that declared NO budget keeps the historical clamp-and-sleep behaviour.
    slept.clear()
    OpenAICompatibleClient(base_url="http://x/v1", api_key="k", model="m", max_retries=2
                           )._retry_or_raise(throttled, 0, use_stream=False)
    assert slept == [60.0]


def test_the_throttle_wait_is_narrated_rather_than_silent(monkeypatch, caplog):
    """A wait nobody can see reads as a hang — the same failure the GPU host-lease notice records.

    WARNING, so it reaches stderr through logging's `lastResort` handler in a CLI run that never
    configured logging (`engine/resources.py::_note_gpu_host_lease_contention`, same reasoning).
    """
    import logging

    from looplab.core.llm import OpenAICompatibleClient

    monkeypatch.setattr("looplab.core.llm.time.sleep", lambda _s: None)
    client = OpenAICompatibleClient(base_url="http://x/v1", api_key="k", model="m", max_retries=2)
    with caplog.at_level(logging.WARNING, logger="looplab.core.llm"):
        client._retry_or_raise(_sdk_status_error(429, {"message": "limit"}), 0, use_stream=False)
    assert len(caplog.records) == 1 and caplog.records[0].levelno == logging.WARNING
    said = caplog.records[0].getMessage()
    assert "http://x/v1" in said and "429" in said and "throttled" in said
    assert "60s" in said and "attempt 2 of 3" in said       # the wait AND how much is left
