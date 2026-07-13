"""Adversarial accounting/stream-finalization regressions.

Provider usage is optional, untrusted telemetry.  These tests keep its validation transactional and
prove that a paid streaming response is not lost when the consumer closes at a suspended yield.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import math
import sys
import types

import httpx
import openai
import pytest

from looplab.core.llm import (
    BudgetExceeded,
    CostAccountant,
    LLMError,
    OpenAICompatibleClient,
    _MAX_USAGE_TOKENS,
)


def _chunk(text: str = "", *, usage=None, choices: bool = True):
    delta = types.SimpleNamespace(content=text)
    choice = types.SimpleNamespace(delta=delta, finish_reason=None)
    return types.SimpleNamespace(choices=[choice] if choices else [], usage=usage)


def _usage(payload):
    return types.SimpleNamespace(model_dump=lambda: payload)


@pytest.mark.parametrize(("usage", "expected"), [
    (None, (0, 0, 0)),
    ("not-a-dict", (0, 0, 0)),
    ([1, 2, 3], (0, 0, 0)),
    ({"prompt_tokens": True, "completion_tokens": False, "total_tokens": True}, (0, 0, 0)),
    ({"prompt_tokens": "7", "completion_tokens": "2", "total_tokens": "9"}, (0, 0, 0)),
    ({"prompt_tokens": 7.0, "completion_tokens": float("nan"),
      "total_tokens": float("inf")}, (0, 0, 0)),
    ({"prompt_tokens": -7, "completion_tokens": -2, "total_tokens": -9}, (0, 0, 0)),
    ({"prompt_tokens": 10 ** 400, "completion_tokens": 2,
      "total_tokens": 10 ** 400}, (0, 2, 2)),
    ({"prompt_tokens": 7, "completion_tokens": object(), "total_tokens": "bad"}, (7, 0, 7)),
    # A provider total below its components is contradictory; use the component sum.
    ({"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 0}, (7, 2, 9)),
    ({"prompt_tokens": _MAX_USAGE_TOKENS, "completion_tokens": _MAX_USAGE_TOKENS},
     (_MAX_USAGE_TOKENS, _MAX_USAGE_TOKENS, _MAX_USAGE_TOKENS)),
])
def test_accountant_normalizes_every_usage_field_before_one_commit(usage, expected):
    observed = []
    acc = CostAccountant(on_delta=observed.append)

    assert acc.add(float("nan"), usage=usage) == 0.0

    assert (acc.calls, acc.prompt_tokens, acc.completion_tokens, acc.total_tokens) == (1, *expected)
    assert observed == [{
        "cost": 0.0,
        "calls": 1,
        "prompt_tokens": expected[0],
        "completion_tokens": expected[1],
        "total_tokens": expected[2],
    }]


def test_accountant_sink_observes_committed_decimal_delta_and_failure_is_nonfatal():
    snapshots = []
    acc = CostAccountant()

    def sink(delta):
        snapshots.append((delta, acc.spent, acc.calls, acc.total_tokens))

    acc.set_sink(sink)
    assert acc.add(Decimal("0.0123"), usage={
        "prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5,
    }) == pytest.approx(0.0123)
    assert snapshots == [({
        "cost": pytest.approx(0.0123), "calls": 1,
        "prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5,
    }, pytest.approx(0.0123), 1, 5)]

    def broken(_delta):
        raise OSError("ledger unavailable")

    acc.set_sink(broken)
    assert acc.add(0.1, usage=None) == pytest.approx(0.1123)
    assert acc.calls == 2
    assert acc.last_sink_error == "OSError: ledger unavailable"


def test_accountant_sink_runs_once_before_budget_exception():
    deltas = []
    acc = CostAccountant(limit=0.1, on_delta=deltas.append)

    with pytest.raises(BudgetExceeded, match="spent .* >= budget"):
        acc.add(0.2, usage=None)

    # Provider accounting was committed and delivered once even though enforcement aborts afterward.
    assert acc.calls == 1 and acc.spent == pytest.approx(0.2)
    assert deltas == [{"cost": 0.2, "calls": 1, "prompt_tokens": 0,
                       "completion_tokens": 0, "total_tokens": 0}]


def test_cost_rollup_remains_finite_when_individually_finite_values_overflow_sum():
    acc = CostAccountant()
    acc.add(sys.float_info.max)
    acc.add(sys.float_info.max)
    assert acc.spent == sys.float_info.max and math.isfinite(acc.spent)
    assert acc.calls == 2

    # The durable ledger uses signed-int64 counters. Keep the in-memory source within the same
    # representable contract instead of letting a pathological long-lived process grow a bigint.
    acc.calls = _MAX_USAGE_TOKENS
    acc.add(0.0)
    assert acc.calls == _MAX_USAGE_TOKENS


@pytest.mark.parametrize("reported_usage", [None, {}, "bad", [1, 2], {"prompt_tokens": True}])
def test_success_without_valid_usage_is_still_one_logical_provider_call(monkeypatch, reported_usage):
    body = {"choices": [{"message": {"role": "assistant", "content": "ok"},
                          "finish_reason": "stop"}]}
    if reported_usage is not None:
        body["usage"] = reported_usage
    client = OpenAICompatibleClient("m", base_url="http://x/v1", stream=False)
    monkeypatch.setattr(client, "_sdk_chat", lambda _payload, _stream: body)

    assert client.complete_text([{"role": "user", "content": "go"}]) == "ok"
    assert (client.accountant.calls, client.accountant.total_tokens) == (1, 0)
    assert client._last_usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0.0,
    }


def test_no_choices_response_with_known_usage_is_accounted_before_error(monkeypatch):
    client = OpenAICompatibleClient("m", base_url="http://x/v1", stream=False)
    body = {
        "choices": [],
        "usage": {"prompt_tokens": 8, "completion_tokens": 2,
                  "total_tokens": 10, "cost": .012},
    }
    monkeypatch.setattr(client, "_sdk_chat", lambda _payload, _stream: body)

    with pytest.raises(LLMError, match="no choices"):
        client.complete_text([{"role": "user", "content": "go"}])

    assert client.accountant.spent == pytest.approx(.012)
    assert (client.accountant.calls, client.accountant.prompt_tokens,
            client.accountant.completion_tokens, client.accountant.total_tokens) == (1, 8, 2, 10)
    assert client._last_usage == {
        "prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10, "cost": .012,
    }


def test_cache_hit_keeps_zero_usage_and_emits_no_second_delta(monkeypatch):
    deltas = []
    accountant = CostAccountant(on_delta=deltas.append)
    client = OpenAICompatibleClient("m", base_url="http://x/v1", temperature=0,
                                    stream=False, cache=True, accountant=accountant)
    body = {"choices": [{"message": {"role": "assistant", "content": "cached"},
                          "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2,
                      "total_tokens": 7, "cost": 0.007}}
    requests = []
    monkeypatch.setattr(client, "_sdk_chat", lambda payload, _stream: requests.append(payload) or body)

    messages = [{"role": "user", "content": "same"}]
    assert client.complete_text(messages) == client.complete_text(messages) == "cached"
    assert len(requests) == 1 and accountant.calls == 1 and len(deltas) == 1
    assert client._last_usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0.0,
    }


@pytest.mark.parametrize("cancel", [False, True], ids=["close", "cancel"])
def test_stream_usage_is_accounted_once_when_consumer_stops_at_yield(monkeypatch, cancel):
    reported = _usage({"prompt_tokens": 4, "completion_tokens": 1,
                       "total_tokens": 5, "cost": 0.006})
    client = OpenAICompatibleClient("m", base_url="http://x/v1", stream=True)
    monkeypatch.setattr(client._sdk.chat.completions, "create",
                        lambda **_kwargs: iter([_chunk("paid", usage=reported), _chunk("unread")]))

    stream = client.complete_text_stream([{"role": "user", "content": "go"}])
    assert next(stream) == "paid"
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            stream.throw(asyncio.CancelledError())
    else:
        stream.close()
        stream.close()  # finalization is idempotent; a repeated close cannot double-charge

    assert client.accountant.spent == pytest.approx(0.006)
    assert (client.accountant.calls, client.accountant.total_tokens) == (1, 5)


def test_partial_stream_closed_after_content_counts_unknown_call_once(monkeypatch):
    reported = _usage({"prompt_tokens": 4, "completion_tokens": 1,
                       "total_tokens": 5, "cost": 0.006})
    client = OpenAICompatibleClient("m", base_url="http://x/v1", stream=True)
    monkeypatch.setattr(client._sdk.chat.completions, "create",
                        lambda **_kwargs: iter([_chunk("first"), _chunk(usage=reported, choices=False)]))

    stream = client.complete_text_stream([{"role": "user", "content": "go"}])
    assert next(stream) == "first"
    stream.close()
    assert (client.accountant.calls, client.accountant.prompt_tokens,
            client.accountant.completion_tokens, client.accountant.total_tokens,
            client.accountant.spent) == (1, 0, 0, 0, 0.0)
    assert client._last_usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0.0,
    }


def test_clean_stream_without_usage_counts_once_and_fallback_does_not_double(monkeypatch):
    client = OpenAICompatibleClient("m", base_url="http://x/v1", stream=True)
    monkeypatch.setattr(client._sdk.chat.completions, "create",
                        lambda **_kwargs: iter([_chunk("clean")]))
    assert list(client.complete_text_stream([{"role": "user", "content": "go"}])) == ["clean"]
    assert client.accountant.calls == 1

    request = httpx.Request("POST", "http://x/v1/chat/completions")

    def failed_stream(**_kwargs):
        def events():
            raise openai.APIConnectionError(message="reset", request=request)
            yield  # pragma: no cover - keeps this an iterator
        return events()

    monkeypatch.setattr(client._sdk.chat.completions, "create", failed_stream)

    def fallback(_messages):
        client.accountant.add(0.02, usage={"prompt_tokens": 2, "completion_tokens": 1})
        return "fallback"

    monkeypatch.setattr(client, "complete_text", fallback)
    assert list(client.complete_text_stream([{"role": "user", "content": "retry"}])) == ["fallback"]
    assert (client.accountant.calls, client.accountant.total_tokens) == (2, 3)
    assert client.accountant.spent == pytest.approx(0.02)


def test_stream_non_mapping_usage_never_crashes_accounting_or_trace(monkeypatch):
    malformed = types.SimpleNamespace(model_dump=lambda: "not-a-dict")
    client = OpenAICompatibleClient("m", base_url="http://x/v1", stream=True)
    monkeypatch.setattr(client._sdk.chat.completions, "create",
                        lambda **_kwargs: iter([_chunk("ok", usage=malformed)]))

    assert list(client.complete_text_stream([{"role": "user", "content": "go"}])) == ["ok"]
    assert (client.accountant.calls, client.accountant.total_tokens,
            client.accountant.spent) == (1, 0, 0.0)
