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
