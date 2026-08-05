"""The LLM client has ONE streaming path, and the retired one stays retired (doc 25 CO-03).

`OpenAICompatibleClient._read_stream` was urllib-era SSE reassembly kept alive only by its own two
tests: since the openai-SDK migration nothing in production called it, and it exclusively drove four
more helpers (`_sse_chunks`, `_socket_watchdog`, `_SSETail`, `_raw_socket`) plus the whole-body
fallback parser `_parse_chat_body`. ~185 lines of transport code, and two module docstrings that had
to keep explaining which of two SSE paths was the real one.

Deleting it is only safe if the contracts it carried are carried somewhere that still runs, so this
file states both halves: the surface is gone AND the live path still owns stall-kill and degenerate
bodies. A dead-code deletion whose contracts quietly went with it is a regression wearing a cleanup's
clothes.
"""
from __future__ import annotations

import inspect
import re

import openai

from _source_scan import called_names
from looplab.core import llm, llm_streaming

# The exact names the legacy path owned. Re-adding one means re-adding a second streaming path, and
# that is a decision someone should have to make on purpose.
RETIRED = ("_read_stream", "_sse_chunks", "_socket_watchdog", "_SSETail", "_raw_socket",
           "_parse_chat_body")


def test_the_legacy_reassembly_surface_is_gone():
    for name in RETIRED:
        assert not hasattr(llm.OpenAICompatibleClient, name), (
            f"OpenAICompatibleClient.{name} is back — a second streaming path")
        assert not hasattr(llm_streaming, name), f"llm_streaming.{name} is back"
        assert not hasattr(llm, name), f"llm re-exports {name} again"


def test_the_urllib_import_that_only_existed_for_those_tests_is_gone():
    """`core.llm` imported `urllib.request` solely so old unit tests could monkeypatch
    `llm.urllib.request.urlopen`. The live transport is the SDK; the import was test scaffolding
    living in production."""
    assert not hasattr(llm, "urllib") or not hasattr(getattr(llm, "urllib"), "request"), (
        "core.llm imports urllib.request again")
    source = inspect.getsource(llm)
    assert "import urllib.request" not in source


RETIREMENT_MARKER = "There used to be a SECOND, urllib-era reassembly path here"


def test_no_module_docstring_still_promises_two_sse_paths():
    """Stale comments are a bug here specifically: the whole cost of the dead path was that every
    reader had to be told which of two SSE implementations was real.

    Positional, not keyword-based: a docstring that merely CONTAINS the word "gone" somewhere would
    satisfy a naive check while its opening paragraph still described `_sse_chunks` as machinery
    this module provides. A retired name may appear only AFTER the sentence that retires it.
    """
    for module in (llm, llm_streaming):
        doc = module.__doc__ or ""
        cutoff = doc.find(RETIREMENT_MARKER)
        prologue = doc if cutoff < 0 else doc[:cutoff]
        for name in ("_sse_chunks", "_socket_watchdog", "_SSETail", "_raw_socket",
                     "_read_stream", "_parse_chat_body"):
            # Word-boundary, because `_raw_socket` is a SUBSTRING of the live `_stream_raw_socket`
            # and a plain `in` check flags the surviving helper the prologue is supposed to name.
            assert re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
                             prologue) is None, (
                f"{module.__name__} still describes {name} as a live path")


# --------------------------------------------------- the contracts the deletion had to preserve

def test_the_idle_guard_kills_a_stream_that_goes_silent_on_a_REAL_socket():
    """The stall-kill, driven against a real blocked `recv()` — the module docstring's promised
    "real-socketpair test", which did not exist until 2026-08-05.

    Both tests that looked like they covered this drive a MOCK socket whose `shutdown()` merely sets
    an Event (and, in `test_assistant_mega_review.py`, whose `close()` sets the SAME event) — so they
    assert that a method was called, which is a weaker claim than "the wedged read came back". A
    mutation audit confirmed the gap: deleting `sock.shutdown(_socket.SHUT_RDWR)` from
    `_stream_with_idle_guard` leaves the streaming/client surface green apart from one mock-level
    call assertion, while the idle guard's entire purpose — BOUNDING a silent stream — is gone.

    Here the stream really blocks in the kernel and `resp.close()` really cannot help, which is the
    live lesson `_stream_raw_socket`'s docstring records. `shutdown(SHUT_RDWR)` makes the blocked
    `recv` return EOF; without it this generator never ends.
    """
    import socket as _socket
    import threading

    ours, peer = _socket.socketpair()                  # `peer` never sends: a black-holed body
    closed = []

    class _NetworkStream:
        def get_extra_info(self, _key):
            return ours

    class _Resp:
        request = None
        extensions = {"network_stream": _NetworkStream()}

        def close(self):
            # A real httpx response close does NOT interrupt a recv() another thread is already
            # blocked inside; recording the call is all it may do here, or this test would prove
            # nothing about which of the two mechanisms unblocked the read.
            closed.append(True)

    class _SilentBody:
        response = _Resp()

        def __iter__(self):
            return self

        def __next__(self):
            if not ours.recv(4096):                    # blocks until SHUT_RDWR makes it return b""
                raise StopIteration
            return type("Ev", (), {"choices": [], "usage": None})()

    outcome: list = []
    drain = threading.Thread(
        target=lambda: outcome.append(
            _run_to_completion(llm_streaming._stream_with_idle_guard(_SilentBody(), 0.3))),
        daemon=True)
    try:
        drain.start()
        # Bounded join, because the REGRESSION is a hang: an in-line drain would wedge the suite
        # instead of reporting a failure (the same reason test_assistant_mega_review.py joins).
        drain.join(timeout=10)
        assert not drain.is_alive(), (
            "the idle guard could not kill a silent stream — resp.close() does not unblock a recv() "
            "already blocked in the kernel, so this run would hang rather than degrade")
        assert isinstance(outcome[0], openai.APITimeoutError), (
            f"the stall must surface as APITimeoutError for `_post` to degrade+retry; got {outcome[0]!r}")
    finally:
        try:
            ours.shutdown(_socket.SHUT_RDWR)           # release the worker if the assert above failed
        except OSError:
            pass
        ours.close()
        peer.close()


def _run_to_completion(generator):
    """Drain *generator*, returning the exception it ended with (or None). Returned rather than
    raised because this runs on a worker thread, where an exception would be lost."""
    try:
        for _event in generator:
            pass
    except BaseException as error:  # noqa: BLE001 - the outcome IS the assertion
        return error
    return None


def test_the_live_path_still_owns_the_stall_kill_the_legacy_watchdog_carried():
    """The mechanism, alongside the behavioural test above: which call the guard reaches for.

    Through the AST, not `"sock.shutdown(" in body`: the pin this replaces is satisfied by
    `pass  # sock.shutdown(_socket.SHUT_RDWR)`, i.e. by the exact mutation it exists to catch. The
    distinction it was reaching for is still the point — `close()` does not unblock a recv() wedged
    in the kernel — so the assertion is that `shutdown` is really called on the socket handle.
    """
    calls = called_names(llm_streaming._stream_with_idle_guard)
    assert "sock.shutdown" in calls, "the idle guard no longer shuts the socket down"
    assert "_chunk_has_content" in calls, (
        "the idle clock is no longer reset by REAL content only — keepalives would defeat it")
    assert "_stream_raw_socket" in calls, "the guard no longer resolves the socket to shut down"
    assert "idle_limit" in inspect.signature(llm_streaming._stream_with_idle_guard).parameters


def test_the_pool_teardown_shuts_sockets_down_for_the_same_reason():
    """`_shutdown_pool_sockets`'s docstring says it mirrors the stream path. Same AST check, because
    the same comment mutation applies and this one bounds worker threads wedged in `create()`."""
    assert "sock.shutdown" in called_names(llm_streaming._shutdown_pool_sockets)


def test_the_live_path_still_reassembles_a_stream_into_one_chat_body():
    """`_accumulate_stream` is the surviving reassembly. Pinned end to end so "the legacy one is
    gone" cannot be true while the replacement quietly stops merging deltas."""
    def _delta(content=None, tool=None, finish=None):
        fn = None
        if tool is not None:
            index, call_id, name, args = tool
            fn = [type("TC", (), {
                "index": index, "id": call_id,
                "function": type("F", (), {"name": name, "arguments": args})(),
                "model_dump": lambda self: {
                    "index": index, "id": call_id,
                    "function": {"name": name, "arguments": args}},
            })()]
        delta = type("D", (), {"content": content, "tool_calls": fn, "reasoning_content": None})()
        choice = type("C", (), {"delta": delta, "finish_reason": finish})()
        return type("Ev", (), {"choices": [choice], "usage": None})()

    body = llm.OpenAICompatibleClient._accumulate_stream(iter([
        _delta(content="hel"), _delta(content="lo"), _delta(finish="stop"),
    ]))
    assert body["choices"][0]["message"]["content"] == "hello"
    assert body["choices"][0]["finish_reason"] == "stop"


def test_an_empty_stream_still_produces_a_body_rather_than_raising():
    """The degenerate-body case the legacy non-SSE fallback covered. On the SDK path a
    non-streaming endpoint behind a streaming request yields nothing, and `_post` classifies the
    resulting empty body — it must not become a crash inside reassembly."""
    body = llm.OpenAICompatibleClient._accumulate_stream(iter([]))
    assert isinstance(body, dict) and "choices" in body
    assert body["choices"][0]["message"].get("content") in ("", None)
