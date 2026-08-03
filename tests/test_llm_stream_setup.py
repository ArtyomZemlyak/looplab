"""The two streaming entry points share one header budget and one blocking-fallback (doc 25 CO-04).

`_sdk_chat` and `complete_text_stream` both bound their `create()` on a HEADER budget so an endpoint
that accepts the socket and then never sends headers fails over near `header_timeout` instead of
minutes later on the idle timeout. They computed that budget with two separate copies of the same
expression, which is how one of them ends up loosened alone.

Inside `complete_text_stream`, three sites hand the answer to the blocking path — a clean EOF with
no content, a non-retryable BadRequestError, and any other APIError — and each wrote the same five
lines. The duplication mattered for one reason in particular: `delegated_to_fallback` is what stops
the `finally` from charging this envelope ON TOP of the call the fallback makes and accounts for
itself. A fourth site that forgot the flag would double-bill, silently, in money.

So this file pins the accounting and the invariant the accounting rests on, not just
the shape.
"""
from __future__ import annotations

import inspect

import pytest

from looplab.core.llm import OpenAICompatibleClient


# ------------------------------------------------------------------ one header budget

@pytest.mark.parametrize("header_timeout,expected", [
    (4.0, 8.0),        # small budget: slack equals the timeout, so failover stays fast
    (30.0, 40.0),      # large budget: slack is capped at 10s
    (0.3, 0.6),
])
def test_the_header_budget_is_the_timeout_plus_at_most_ten_seconds(header_timeout, expected):
    client = OpenAICompatibleClient("m", base_url="http://x/v1", header_timeout=header_timeout,
                                    timeout=180.0)
    assert client._header_join() == pytest.approx(expected)


def test_the_header_budget_is_not_the_idle_timeout():
    """The regression this exists to prevent: bounding the create on the ~180s idle timeout makes a
    black-holed request block for minutes before failover."""
    client = OpenAICompatibleClient("m", base_url="http://x/v1", header_timeout=2.0, timeout=180.0)
    assert client._header_join() < client.timeout


def test_both_streaming_entry_points_bound_their_create_on_it():
    for method in (OpenAICompatibleClient._sdk_chat, OpenAICompatibleClient.complete_text_stream):
        body = inspect.getsource(method)
        assert "self._header_join()" in body, f"{method.__name__} re-derives the header budget"
        assert "min(10.0, self.header_timeout)" not in body, (
            f"{method.__name__} still spells the budget expression itself")
        assert "_bounded_create(kwargs, header_join" in body, (
            f"{method.__name__} no longer bounds its create on the header budget")


# ------------------------------------------------- one blocking fallback, and its accounting

class _Stub(OpenAICompatibleClient):
    """A client whose blocking path is scripted, so the generator's delegation is observable."""

    def __init__(self, text="fallback answer"):
        super().__init__("m", base_url="http://x/v1")
        self._text = text
        self.blocking_calls = 0

    def complete_text(self, messages, **_kw):
        self.blocking_calls += 1
        return self._text


def test_the_fallback_is_written_once_and_all_three_sites_go_through_it():
    body = inspect.getsource(OpenAICompatibleClient.complete_text_stream)
    assert body.count("yield from _fallback_to_blocking()") == 3, (
        "the three delegation sites must all go through the shared closure")
    assert body.count("delegated_to_fallback = True") == 1, (
        "the flag is set in exactly one place; a second site can forget it and double-bill")
    assert body.count("text = self.complete_text(messages)") == 1


def test_each_site_still_ends_the_stream_itself():
    """`return` stays at the call site rather than inside the closure: a generator cannot end its
    caller, and hiding the termination would make the control flow unreadable at the three points
    where it actually matters."""
    body = inspect.getsource(OpenAICompatibleClient.complete_text_stream)
    assert body.count("yield from _fallback_to_blocking()\n                            return") == 3


def test_the_flag_is_what_stops_the_envelope_being_billed_twice():
    """The accounting rule the duplication put at risk: a delegated stream must NOT charge its own
    envelope, because the blocking call it delegated to charges its own."""
    body = inspect.getsource(OpenAICompatibleClient.complete_text_stream)
    assert "not delegated_to_fallback and (stream_completed or bool(pieces))" in body, (
        "the finally no longer excludes a delegated stream from accounting")
    assert "nonlocal delegated_to_fallback" in body, (
        "the closure must write the CALLER's flag, not shadow it with a fresh local")


def test_a_clean_empty_stream_delegates_and_yields_the_blocking_answer(monkeypatch):
    """End to end on the one site reachable without scripting a provider error: a stream that ends
    cleanly with no content is not a successful empty answer — it delegates."""
    client = _Stub("recovered text")

    class _EmptyStream:
        response = None

        def __iter__(self):
            return iter(())

        def close(self):
            pass

    monkeypatch.setattr(client, "_bounded_create", lambda *a, **k: _EmptyStream())
    out = list(client.complete_text_stream([{"role": "user", "content": "hi"}]))
    assert out == ["recovered text"]
    assert client.blocking_calls == 1


def test_a_delegated_stream_does_not_also_charge_its_own_envelope(monkeypatch):
    """The blocking call accounts for itself; the outer stream must not add a second charge for the
    envelope it abandoned."""
    client = _Stub("recovered text")
    charged: list[float] = []

    class _EmptyStream:
        response = None

        def __iter__(self):
            return iter(())

        def close(self):
            pass

    monkeypatch.setattr(client, "_bounded_create", lambda *a, **k: _EmptyStream())
    monkeypatch.setattr(client.accountant, "add",
                        lambda cost, usage=None: charged.append(cost))
    assert list(client.complete_text_stream([{"role": "user", "content": "hi"}])) == ["recovered text"]
    assert charged == [], "the delegated stream billed an envelope the fallback already paid for"


def test_every_delegation_site_is_guarded_on_having_yielded_nothing():
    """Why the `not delegated_to_fallback` clause is not currently observable — and why it stays.

    All three delegation sites sit under `if not pieces:`, so at each of them `pieces` is empty and
    `stream_completed` is still False; `(stream_completed or bool(pieces))` is therefore already
    False and the flag changes no arithmetic TODAY. It is a guard against the case that makes it
    load-bearing: a fourth site that delegates AFTER yielding content, where the outer envelope
    would otherwise be charged on top of the fallback's own call.

    That is exactly why the invariant is pinned here rather than the (currently unreachable)
    arithmetic: a behavioural test would pass with the clause deleted, and go on passing right up
    until someone adds the site that needs it.
    """
    body = inspect.getsource(OpenAICompatibleClient.complete_text_stream)
    lines = body.splitlines()
    sites = [index for index, line in enumerate(lines)
             if "yield from _fallback_to_blocking()" in line]
    assert len(sites) == 3
    for index in sites:
        guard = next((lines[back] for back in range(index - 1, max(index - 4, -1), -1)
                      if "if not pieces" in lines[back]), None)
        assert guard is not None, (
            f"the delegation at line {index + 1} of complete_text_stream is not guarded on "
            "`not pieces` — the accounting flag is now load-bearing and needs a behavioural test")


def test_an_empty_blocking_answer_yields_nothing_rather_than_an_empty_string(monkeypatch):
    """`if text:` — a blank fallback must not surface as a zero-length "answer" the caller then
    persists as a successful result."""
    client = _Stub("")

    class _EmptyStream:
        response = None

        def __iter__(self):
            return iter(())

        def close(self):
            pass

    monkeypatch.setattr(client, "_bounded_create", lambda *a, **k: _EmptyStream())
    assert list(client.complete_text_stream([{"role": "user", "content": "hi"}])) == []
    assert client.blocking_calls == 1
