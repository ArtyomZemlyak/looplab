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


def test_the_streaming_guard_does_not_open_until_the_header_wait_is_over():
    """A wedged call must not be double-counted as its own sibling.

    `_streaming_body` covers the BODY read; `_bounded_create`'s `_inflight` covers the header wait.
    Nesting them (evaluating `_bounded_create` as an argument inside the `with`) charged ONE call to
    both counters at once, so `_pool_teardown_is_safe_locked` saw a phantom sibling and a black-holed
    stream skipped the socket shutdown + client rebuild that is the whole point of the abort path.
    """
    from looplab.core.llm import OpenAICompatibleClient

    client = OpenAICompatibleClient(base_url="http://x/v1", api_key="k", model="m",
                                    stream=True, timeout=0.3, header_timeout=0.3)
    seen: list[tuple[int, int, bool]] = []

    class _SDK:
        class chat:
            class completions:
                @staticmethod
                def create(**_kwargs):
                    # Sampled from INSIDE the header wait, where the abort decision is made.
                    with client._inflight_lock:
                        seen.append((client._inflight, client._stream_inflight,
                                     client._pool_teardown_is_safe_locked()))
                    return []

    client._sdk = _SDK()
    client._sdk_chat({"model": "m", "messages": []}, use_stream=True)
    assert seen, "the transport never reached the SDK"
    inflight, stream_inflight, alone = seen[0]
    assert (inflight, stream_inflight) == (1, 0)
    assert alone, "a lone wedged stream would refuse its own teardown"


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
