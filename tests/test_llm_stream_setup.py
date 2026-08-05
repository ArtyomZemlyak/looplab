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

2026-08-05, from a mutation audit: the accounting was pinned by SUBSTRING
(`"not delegated_to_fallback and (stream_completed or bool(pieces))" in body`), and deleting the
clause while leaving a comment carrying that literal left all eight LLM/accounting/cost test files
green. The rule now lives in `core/llm._stream_envelope_is_billable`, which has its own truth table
here; what remains structural is checked through the AST (`_source_scan.called_names`), which a
comment cannot satisfy.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from _source_scan import called_names, function_tree, names_read
from looplab.core.llm import OpenAICompatibleClient, _stream_envelope_is_billable


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
    """AST, not substrings: `pass  # self._header_join()` satisfies a source pin while the create
    goes out unbounded again and a black-holed endpoint hangs for minutes (see `_source_scan`)."""
    for method in (OpenAICompatibleClient._sdk_chat, OpenAICompatibleClient.complete_text_stream):
        calls = called_names(method)
        assert "self._header_join" in calls, f"{method.__name__} re-derives the header budget"
        assert "self._bounded_create" in calls, (
            f"{method.__name__} no longer bounds its create on the header budget")
        # `header_join` is READ, so the budget it computed is what the bounded create is given —
        # a call that computes it and throws the value away would still pass the two checks above.
        assert "header_join" in names_read(method), (
            f"{method.__name__} computes the header budget and does not use it")
        # the negative is still a substring check, and correctly so: what must not COME BACK is a
        # second copy of the expression, and a commented-out copy is exactly as much of a
        # re-derivation risk as a live one.
        assert "min(10.0, self.header_timeout)" not in inspect.getsource(method), (
            f"{method.__name__} still spells the budget expression itself")


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


def _delegation_sites(tree) -> list[ast.YieldFrom]:
    """Every REAL `yield from _fallback_to_blocking()` in *tree*. A commented-out one is not a site,
    and (the direction a raw `body.count(...) == 3` gets wrong) a real one deleted in favour of a
    comment keeps the count at three. Takes a TREE, not a function: the callers below cross-reference
    nodes by identity, and re-parsing per helper would hand them different objects."""
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.YieldFrom) and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", None) == "_fallback_to_blocking"]


def test_the_fallback_is_written_once_and_all_three_sites_go_through_it():
    stream = OpenAICompatibleClient.complete_text_stream
    assert len(_delegation_sites(function_tree(stream))) == 3, (
        "the three delegation sites must all go through the shared closure")
    # The flag is RAISED in exactly one place — a second delegation site that forgot to set it is
    # the double-bill this closure exists to make impossible. (Two stores total: the `False`
    # initializer and the one flip inside the closure.)
    raised = [node for node in ast.walk(function_tree(stream))
              if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
              and node.value.value is True
              and any(getattr(target, "id", None) == "delegated_to_fallback"
                      for target in node.targets)]
    assert len(raised) == 1
    assert called_names(stream).count("self.complete_text") == 1, (
        "the blocking call is written once, inside the shared closure")


def test_each_site_still_ends_the_stream_itself():
    """`return` stays at the call site rather than inside the closure: a generator cannot end its
    caller, and hiding the termination would make the control flow unreadable at the three points
    where it actually matters.

    Structural via the AST rather than the indentation-sensitive two-line substring it replaced:
    that spelling silently stopped matching anything the moment either site was re-indented, and a
    pin that matches nothing is a test that asserts `0 == 0`.
    """
    tree = function_tree(OpenAICompatibleClient.complete_text_stream)
    ended = 0
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for statement, following in zip(body, body[1:]):
            if (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.YieldFrom)
                    and isinstance(following, ast.Return)):
                ended += 1
    assert ended == 3, "a delegation site no longer ends the stream at the call site"


@pytest.mark.parametrize("delegated,expected", [(False, True), (True, False)])
def test_a_delegated_envelope_is_not_billable_even_though_it_produced_content(delegated, expected):
    """THE money row, and the only one the deleted clause decides.

    A stream that already yielded content and THEN handed the answer to `complete_text` must not
    charge its own envelope: the blocking call it delegated to makes a provider call and accounts
    for it, so billing both puts the run's recorded spend ABOVE the invoice — the mirror image of
    the under-count `_account_keepalive_stall` was added to fix.

    This was the survivor: dropping `not delegated_to_fallback` from the old inline expression left
    the whole LLM/accounting/cost surface green (verified by mutation), because all three delegation
    sites sit under `if not pieces:` and therefore never reach this row. The rule is now a rule, so
    the row can be stated whether or not a caller reaches it yet.
    """
    assert _stream_envelope_is_billable(
        usage_observed=False, delegated_to_fallback=delegated,
        stream_completed=True, produced_content=True) is expected


@pytest.mark.parametrize("usage_observed", [True, False])
@pytest.mark.parametrize("delegated", [True, False])
@pytest.mark.parametrize("completed", [True, False])
@pytest.mark.parametrize("produced", [True, False])
def test_the_billing_rule_is_reported_defaults_then_delegation_then_evidence_of_a_call(
        usage_observed, delegated, completed, produced):
    """The whole truth table, so no input can quietly change meaning: REPORTED usage is spend
    whatever else happened; otherwise a delegated envelope is the fallback's to bill; otherwise a
    stream that completed or produced content is one logical call."""
    expected = True if usage_observed else (False if delegated else (completed or produced))
    assert _stream_envelope_is_billable(
        usage_observed=usage_observed, delegated_to_fallback=delegated,
        stream_completed=completed, produced_content=produced) is expected


def test_the_streaming_finally_bills_through_that_rule_and_feeds_it_the_flag():
    """The rule is worthless orphaned. AST, not substrings: the pin this replaces was
    `"not delegated_to_fallback and (stream_completed or bool(pieces))" in body`, which a comment
    carrying the same literal satisfies while the expression itself is gone."""
    stream = OpenAICompatibleClient.complete_text_stream
    assert "_stream_envelope_is_billable" in called_names(stream), (
        "complete_text_stream no longer decides billing through the shared rule")
    assert "delegated_to_fallback" in names_read(stream), (
        "the delegation flag is not read — the rule is being fed a constant")
    assert "self.accountant.add" in called_names(stream)
    assert any(isinstance(node, ast.Nonlocal) and "delegated_to_fallback" in node.names
               for node in ast.walk(function_tree(stream))), (
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
    envelope it abandoned.

    Driven with a COUNTING accountant through the real fallback, so what is asserted is the ledger's
    contents rather than the shape of the code that writes it: exactly one charge reaches the
    accountant for a stream that fell back, and it is the fallback's.
    """
    client = _Stub("recovered text")
    charged: list[float] = []

    class _EmptyStream:
        response = None

        def __iter__(self):
            return iter(())

        def close(self):
            pass

    def _billed_blocking(messages, **_kw):
        client.blocking_calls += 1
        client.accountant.add(0.25, usage=None)      # what the real blocking path does for itself
        return "recovered text"

    monkeypatch.setattr(client, "_bounded_create", lambda *a, **k: _EmptyStream())
    monkeypatch.setattr(client, "complete_text", _billed_blocking)
    monkeypatch.setattr(client.accountant, "add",
                        lambda cost, usage=None: charged.append(cost))
    assert list(client.complete_text_stream([{"role": "user", "content": "hi"}])) == ["recovered text"]
    assert client.blocking_calls == 1
    assert charged == [0.25], (
        "a delegated stream must reach the ledger exactly once — the fallback's own call")


def test_every_delegation_site_is_guarded_on_having_yielded_nothing():
    """Why the money row above is not reachable from `complete_text_stream` TODAY — and the tripwire
    for the day it becomes reachable.

    All three delegation sites sit under `if not pieces:`, so at each of them `pieces` is empty and
    `stream_completed` is still False: the rule is asked about an envelope that produced nothing,
    which is not billable for a reason that has nothing to do with delegation. A FOURTH site that
    delegates AFTER yielding content is what makes `delegated_to_fallback` decide real money — and
    when someone adds it, this test is what tells them the end-to-end accounting path now needs a
    test of its own rather than only the rule's truth table.
    """
    tree = function_tree(OpenAICompatibleClient.complete_text_stream)
    guarded = {
        id(site)
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.op, ast.Not) and getattr(node.test.operand, "id", None) == "pieces"
        for statement in node.body
        for site in ast.walk(statement)
        if isinstance(site, ast.YieldFrom)
    }
    sites = _delegation_sites(tree)
    assert len(sites) == 3
    unguarded = [site.lineno for site in sites if id(site) not in guarded]
    assert not unguarded, (
        f"the delegation(s) at complete_text_stream line(s) {unguarded} are not guarded on "
        "`not pieces` — `delegated_to_fallback` now decides real money on a reachable path, so "
        "add a behavioural test that a delegated stream is billed exactly once end to end")


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
