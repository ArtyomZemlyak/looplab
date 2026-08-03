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

def test_the_live_path_still_owns_the_stall_kill_the_legacy_watchdog_carried():
    """The legacy watchdog's reason for existing: a server that trickles keepalive bytes without
    completing a message resets every per-read timeout forever, so only `socket.shutdown()`
    interrupts the wedged recv. That contract must not have left with the code.

    It is EXERCISED by `test_stream_idle_guard_kills_keepalive_trickle` in test_openai_client.py,
    against a stream that blocks until the watchdog shuts its socket. Re-driving a real socketpair
    here would be a second, more fragile copy of that test — so what is checked here is that the
    live helper still contains the mechanism, which is what the deletion could plausibly have taken
    with it.
    """
    body = inspect.getsource(llm_streaming._stream_with_idle_guard)
    # The CALL, not the word: the function's prose mentions shutdown either way, so a bare
    # substring check stays green with `sock.shutdown(...)` swapped for `sock.close()` — which is
    # exactly the regression, since close() does not unblock a recv() wedged in the kernel.
    assert "sock.shutdown(" in body, "the idle guard no longer shuts the socket down"
    assert "_chunk_has_content" in body, (
        "the idle clock is no longer reset by REAL content only — keepalives would defeat it")
    assert "idle_limit" in inspect.signature(llm_streaming._stream_with_idle_guard).parameters


def test_the_covering_stall_test_still_exists_and_targets_the_live_helper():
    """A cross-reference with teeth: if that test is renamed or deleted, the contract above becomes
    unexercised and this file is the only place that would notice."""
    from pathlib import Path as _Path

    covering = (_Path(__file__).parent / "test_openai_client.py").read_text(encoding="utf-8")
    assert "def test_stream_idle_guard_kills_keepalive_trickle(" in covering
    assert "_stream_with_idle_guard(_Stream(), idle_limit=0.3)" in covering


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
