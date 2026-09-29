"""A provider's PROMPT-CACHE hits reach the durable ledger, the fold, the spans and `looplab tokens`
(doc 69 69.32).

`core/llm.py::_normalize_usage` kept prompt/completion/total and dropped every cache field a provider
reports — OpenAI's `prompt_tokens_details.cached_tokens` (OpenRouter, vLLM and LiteLLM speak it
too), Anthropic's `cache_read_input_tokens`, DeepSeek's `prompt_cache_hit_tokens` — so no run could
say what the same calls would cost on a provider that prices a cache hit below a fresh prompt token.

The counter is SPARSE everywhere it travels: a call whose provider reports no cache hit writes the
historical bytes (normalized dict, accountant delta, `llm_usage` row, outbox record, folded ledger,
span `usage`, `llm_cost` roll-up), which is also what every row written before the field says. And
it is never more than the prompt it is a part of, at every sanitizer, so a hand-edited row cannot
claim more hits than tokens sent.
"""
from __future__ import annotations

import json
import random
import types
from types import SimpleNamespace

import orjson
import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.core import tracing
from looplab.core.llm import (
    CostAccountant, LiteLLMClient, OpenAICompatibleClient, _MAX_USAGE_TOKENS, _normalize_usage)
from looplab.core.models import Event
from looplab.core.tracing import JsonlSpanExporter, Tracer
from looplab.engine.costs import (
    _decode_outbox, in_memory_cost_total, persisted_usage_deltas, reconcile_cost_accountants,
    sanitize_usage_delta)
from looplab.engine.finalize import emit_llm_cost
from looplab.events.replay import fold
from looplab.events.token_spend import token_spend_by_phase
from looplab.events.types import EV_LLM_COST, EV_LLM_USAGE
from factories import make_engine

_HISTORICAL = {"prompt_tokens", "completion_tokens", "total_tokens", "cost", "priced"}


def _usage(prompt: int, completion: int, **extra) -> dict:
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion, **extra}


# ------------------------------------------------------------------ the provider's four spellings
@pytest.mark.parametrize("extra", [
    {"prompt_tokens_details": {"cached_tokens": 60}},          # OpenAI / OpenRouter / vLLM / LiteLLM
    {"cache_read_input_tokens": 60},                           # Anthropic-compatible
    {"prompt_cache_hit_tokens": 60},                           # DeepSeek
    {"cached_tokens": 60},                                     # this module's own (a second pass)
], ids=["openai", "anthropic", "deepseek", "normalized"])
def test_every_provider_spelling_of_a_cache_hit_is_kept(extra):
    """MUTATION: drop a spelling from `_cached_prompt_tokens` -> that provider's hits read 0 again."""
    normalized = _normalize_usage(_usage(100, 5, **extra))

    assert normalized["cached_tokens"] == 60
    assert normalized["prompt_tokens"] == 100 and normalized["total_tokens"] == 105


def test_a_second_normalization_keeps_the_count():
    """`_post` hands its normalized body back through `CostAccountant.add`, which normalizes again.

    MUTATION: drop the `cached_tokens` spelling -> the accountant sees a dict with no provider
    shape left in it and records 0 for every call that went through `_post`."""
    once = _normalize_usage(_usage(100, 5, prompt_tokens_details={"cached_tokens": 40}))
    assert _normalize_usage(once) == once


def test_the_first_non_zero_spelling_wins():
    both = _normalize_usage(_usage(100, 5, prompt_tokens_details={"cached_tokens": 0},
                                   cache_read_input_tokens=30))
    assert both["cached_tokens"] == 30, "a zero in the first spelling must not hide the second"


@pytest.mark.parametrize("hostile", [True, "60", -60, 60.0, float("nan"), None,
                                     _MAX_USAGE_TOKENS + 1, [60], {"n": 60}])
def test_a_cache_figure_that_is_not_a_count_states_nothing(hostile):
    normalized = _normalize_usage(_usage(100, 5, prompt_tokens_details={"cached_tokens": hostile},
                                         cache_read_input_tokens=hostile))
    assert "cached_tokens" not in normalized
    assert normalized["prompt_tokens"] == 100


def test_a_cache_hit_is_never_more_than_the_prompt_it_is_part_of():
    """MUTATION: drop the clamp -> 900 cached tokens of a 100-token prompt reach the ledger."""
    assert _normalize_usage(_usage(100, 5, cache_read_input_tokens=900))["cached_tokens"] == 100
    assert "cached_tokens" not in _normalize_usage(_usage(0, 5, cache_read_input_tokens=900))


def test_a_call_with_no_cache_hit_keeps_the_historical_dict():
    """SPARSE: the key is absent, not 0, so every row a provider without caching writes is unchanged.

    MUTATION: always write the key -> every `llm_usage` row of every run changes shape."""
    for usage in (_usage(100, 5), _usage(100, 5, prompt_tokens_details={"cached_tokens": 0}),
                  _usage(100, 5, prompt_tokens_details=None), None, {}):
        assert set(_normalize_usage(usage)) == _HISTORICAL


# ------------------------------------------------------------------------------ the accountant
def test_the_accountant_counts_hits_and_its_delta_carries_them_only_when_non_zero():
    deltas: list[dict] = []
    acc = CostAccountant(on_delta=deltas.append)

    acc.add(0.1, _usage(100, 5, prompt_tokens_details={"cached_tokens": 70}))
    acc.add(0.1, _usage(50, 5))
    acc.add(0.1, _usage(30, 5, cache_read_input_tokens=10))

    assert acc.cached_tokens == 80
    assert deltas[0]["cached_tokens"] == 70
    assert "cached_tokens" not in deltas[1]
    assert deltas[2]["cached_tokens"] == 10


def test_the_binding_boundary_carries_the_count_only_when_non_zero():
    acc = CostAccountant()
    assert "cached_tokens" not in acc.bind_sink(lambda previous: None)
    acc.add(0.0, _usage(10, 1, cache_read_input_tokens=4))
    assert acc.bind_sink(lambda previous: None)["cached_tokens"] == 4


def test_a_local_response_cache_replay_claims_no_provider_cache_hit(monkeypatch):
    """A T7 cache hit performs no provider work: its prompt is zeroed, and so must its cache hits be.

    MUTATION: drop the `pop` in `_cache_get` -> the replay's usage says 70 cached tokens of a 0-token
    prompt, and the span of a call nobody made carries the stored call's cache figure."""
    deltas: list[dict] = []
    accountant = CostAccountant(on_delta=deltas.append)
    client = OpenAICompatibleClient("m", base_url="http://x/v1", temperature=0,
                                    stream=False, cache=True, accountant=accountant)
    body = {"choices": [{"message": {"role": "assistant", "content": "cached"},
                         "finish_reason": "stop"}],
            "usage": _usage(100, 2, prompt_tokens_details={"cached_tokens": 70})}
    requests = []
    monkeypatch.setattr(client, "_sdk_chat",
                        lambda payload, _stream: requests.append(payload) or body)

    messages = [{"role": "user", "content": "same"}]
    assert client.complete_text(messages) == "cached"
    assert accountant.cached_tokens == 70 and deltas[0]["cached_tokens"] == 70
    assert client.complete_text(messages) == "cached"

    assert len(requests) == 1 and len(deltas) == 1
    assert "cached_tokens" not in client._last_usage
    assert accountant.cached_tokens == 70


def test_a_streamed_call_reports_its_hits_from_the_final_usage_chunk(monkeypatch):
    """The SDK's streamed usage is an OBJECT; `_stream_usage` dumps it, nested details included."""
    reported = {"prompt_tokens": 40, "completion_tokens": 2, "total_tokens": 42,
                "prompt_tokens_details": {"cached_tokens": 32}}
    usage = types.SimpleNamespace(model_dump=lambda **_kw: reported)
    delta = types.SimpleNamespace(content="ok")
    chunks = [types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta,
                                                                  finish_reason="stop")],
                                    usage=None),
              types.SimpleNamespace(choices=[], usage=usage)]
    client = OpenAICompatibleClient("m", base_url="http://x/v1", stream=True)
    monkeypatch.setattr(client._sdk.chat.completions, "create", lambda **_kw: iter(chunks))

    assert "".join(client.complete_text_stream([{"role": "user", "content": "go"}])) == "ok"
    assert client.accountant.cached_tokens == 32


@pytest.mark.parametrize(("details", "flat", "expected"), [
    ({"cached_tokens": 12}, {}, 12),
    (SimpleNamespace(cached_tokens=12), {}, 12),
    (None, {"cache_read_input_tokens": 9}, 9),
    (None, {"prompt_cache_hit_tokens": 7}, 7),
    (None, {}, None),
], ids=["dict", "object", "anthropic", "deepseek", "none"])
def test_litellm_usage_objects_carry_the_hits(details, flat, expected):
    usage = SimpleNamespace(prompt_tokens=20, completion_tokens=3, total_tokens=23,
                            prompt_tokens_details=details, **flat)
    client = LiteLLMClient.__new__(LiteLLMClient)
    client._cost = lambda _resp: None
    normalized = client._usage(SimpleNamespace(usage=usage))

    assert normalized["prompt_tokens"] == 20
    assert normalized.get("cached_tokens") == expected


def test_an_exotic_litellm_details_object_costs_the_cache_figure_not_the_call():
    """MUTATION: fold the extraction back into `_usage`'s own try -> the whole usage becomes None
    and the call is ledgered with no tokens at all."""
    class Exploding:
        @property
        def cached_tokens(self):
            raise RuntimeError("provider shim")

    usage = SimpleNamespace(prompt_tokens=20, completion_tokens=3, total_tokens=23,
                            prompt_tokens_details=Exploding())
    client = LiteLLMClient.__new__(LiteLLMClient)
    client._cost = lambda _resp: None
    normalized = client._usage(SimpleNamespace(usage=usage))

    assert normalized is not None and normalized["total_tokens"] == 23
    assert "cached_tokens" not in normalized


# ------------------------------------------------------------------- the durable engine ledger
def test_the_ledger_row_and_the_fold_carry_the_hits(tmp_path):
    acc = CostAccountant()
    eng = make_engine(tmp_path / "run", researcher=SimpleNamespace(
        client=SimpleNamespace(accountant=acc)))
    acc.add(0.1, _usage(100, 5, prompt_tokens_details={"cached_tokens": 70}))
    acc.add(0.1, _usage(50, 5))

    rows = [e.data for e in eng.store.read_all() if e.type == EV_LLM_USAGE]
    assert rows[0]["cached_tokens"] == 70
    assert "cached_tokens" not in rows[1], "a call with no hit writes the historical row"
    ledger = fold(eng.store.read_all()).llm_cost
    assert ledger["cached_tokens"] == 70 and ledger["prompt_tokens"] == 150

    assert emit_llm_cost(eng, finalize_scope="finish:test") is True
    summary = [e.data for e in eng.store.read_all() if e.type == EV_LLM_COST][-1]
    assert summary["cached_tokens"] == 70


def test_a_run_with_no_hit_keeps_the_historical_ledger_and_roll_up(tmp_path):
    acc = CostAccountant()
    eng = make_engine(tmp_path / "run", researcher=SimpleNamespace(
        client=SimpleNamespace(accountant=acc)))
    acc.add(0.1, _usage(100, 5))

    assert "cached_tokens" not in fold(eng.store.read_all()).llm_cost
    assert emit_llm_cost(eng, finalize_scope="finish:test") is True
    summary = [e.data for e in eng.store.read_all() if e.type == EV_LLM_COST][-1]
    assert "cached_tokens" not in summary


def test_a_legacy_accountant_reconciled_at_finalize_carries_its_hits(tmp_path):
    """No sink: finalization infers the gap from the aggregate counters, the optional one included.

    MUTATION: reconcile only `_COUNTER_KEYS` -> the inferred row drops the legacy accountant's hits."""
    legacy = SimpleNamespace(spent=0.0, calls=0, prompt_tokens=0, completion_tokens=0,
                             total_tokens=0, cached_tokens=0)
    eng = make_engine(tmp_path / "legacy", researcher=SimpleNamespace(
        client=SimpleNamespace(accountant=legacy)))
    legacy.spent, legacy.calls = 0.5, 1
    legacy.prompt_tokens, legacy.completion_tokens, legacy.total_tokens = 40, 2, 42
    legacy.cached_tokens = 30

    assert reconcile_cost_accountants(eng) is True
    rows = [e.data for e in eng.store.read_all() if e.type == EV_LLM_USAGE]
    assert len(rows) == 1 and rows[0]["cached_tokens"] == 30
    assert fold(eng.store.read_all()).llm_cost["cached_tokens"] == 30


def _legacy_engine(tmp_path):
    legacy = SimpleNamespace(spent=0.0, calls=0, prompt_tokens=0, completion_tokens=0,
                             total_tokens=0, cached_tokens=0)
    eng = make_engine(tmp_path / "legacy", researcher=SimpleNamespace(
        client=SimpleNamespace(accountant=legacy)))
    return eng, legacy


def _charge(legacy, prompt: int, cached: int) -> None:
    legacy.spent += 0.25
    legacy.calls += 1
    legacy.prompt_tokens += prompt
    legacy.completion_tokens += 1
    legacy.total_tokens += prompt + 1
    legacy.cached_tokens += cached


def test_a_second_reconcile_does_not_re_infer_hits_already_recorded(tmp_path):
    """MUTATION: record only `_COUNTER_KEYS` in `_record` -> the second pass infers the FIRST call's
    30 hits again and stamps them on a call that had none."""
    eng, legacy = _legacy_engine(tmp_path)
    _charge(legacy, 40, 30)
    assert reconcile_cost_accountants(eng) is True
    _charge(legacy, 40, 0)
    assert reconcile_cost_accountants(eng) is True

    rows = [e.data for e in eng.store.read_all() if e.type == EV_LLM_USAGE]
    assert [row.get("cached_tokens") for row in rows] == [30, None]
    assert fold(eng.store.read_all()).llm_cost["cached_tokens"] == 30


def test_a_pending_retry_reserves_its_hits_too(tmp_path):
    """A failed append stays queued under its id; the next pass must subtract ITS hits as well as its
    tokens before inferring anything new.

    MUTATION: reserve only `_COUNTER_KEYS` in `pending_total` -> the new call is stamped with the
    pending call's 30 hits."""
    eng, legacy = _legacy_engine(tmp_path)
    real_append = eng.store.append
    reject = True

    def flaky_append(event_type, data, *args, **kwargs):
        if reject and event_type == EV_LLM_USAGE:
            raise OSError("temporary ledger outage")
        return real_append(event_type, data, *args, **kwargs)

    eng.store.append = flaky_append
    _charge(legacy, 40, 30)
    assert reconcile_cost_accountants(eng) is False
    _charge(legacy, 40, 0)
    reject = False
    assert reconcile_cost_accountants(eng) is True

    rows = [e.data for e in eng.store.read_all() if e.type == EV_LLM_USAGE]
    assert sorted(row.get("cached_tokens") or 0 for row in rows) == [0, 30]
    assert fold(eng.store.read_all()).llm_cost["cached_tokens"] == 30


def test_the_in_memory_total_sums_the_hits_sparsely():
    engine = SimpleNamespace(researcher=SimpleNamespace(client=SimpleNamespace(
        accountant=SimpleNamespace(spent=0.1, calls=1, prompt_tokens=10, completion_tokens=1,
                                   total_tokens=11, cached_tokens=6))),
        developer=SimpleNamespace(client=SimpleNamespace(accountant=SimpleNamespace(
            spent=0.1, calls=1, prompt_tokens=10, completion_tokens=1, total_tokens=11))))
    assert in_memory_cost_total(engine)["cached_tokens"] == 6
    engine.researcher.client.accountant.cached_tokens = 0
    assert "cached_tokens" not in in_memory_cost_total(engine)


def test_the_engine_sanitizer_clamps_and_drops():
    assert sanitize_usage_delta({"prompt_tokens": 10, "cached_tokens": 99})["cached_tokens"] == 10
    for junk in (0, -1, True, "5", 5.0, None):
        assert "cached_tokens" not in sanitize_usage_delta({"prompt_tokens": 10,
                                                            "cached_tokens": junk})


def _outbox_record(path, delta) -> None:
    path.write_bytes(orjson.dumps({"version": 1, "usage_id": path.stem, "delta": delta}))


@pytest.mark.parametrize(("cached", "accepted"), [
    (6, True), (60, False), (0, False), (True, False), ("6", False), (-6, False),
], ids=["clean", "over-prompt", "zero", "bool", "str", "negative"])
def test_an_outbox_record_carries_the_hits_only_as_the_writer_would(tmp_path, cached, accepted):
    """The outbox holds only what `sanitize_usage_delta` wrote, so any other `cached_tokens` is
    evidence this ledger did not write — refused, never coerced (the record's existing rule)."""
    usage_id = "a" * 32
    path = tmp_path / f"{usage_id}.json"
    base = {"cost": 0.1, "calls": 1, "priced_calls": 1, "prompt_tokens": 10,
            "completion_tokens": 1, "total_tokens": 11}
    _outbox_record(path, {**base, "cached_tokens": cached})
    if accepted:
        assert _decode_outbox(path) == (usage_id, {**base, "cached_tokens": cached})
    else:
        with pytest.raises(ValueError):
            _decode_outbox(path)
    _outbox_record(path, base)
    assert _decode_outbox(path) == (usage_id, base), "the historical record still decodes"


# ------------------------------------------------------------------------------------- the fold
def _events(rows) -> list[Event]:
    head = [("run_started", {"run_id": "r", "task_id": "t", "direction": "max"})]
    return [Event(seq=i, ts=float(i + 1), type=t, data=d)
            for i, (t, d) in enumerate(head + list(rows))]


def test_the_fold_sanitizes_every_row_and_stays_sparse():
    ledger = fold(_events([
        ("llm_usage", {"usage_id": "u1", "calls": 1, "prompt_tokens": 100, "cached_tokens": 60}),
        ("llm_usage", {"usage_id": "u1", "calls": 1, "prompt_tokens": 100, "cached_tokens": 60}),
        ("llm_usage", {"usage_id": "u2", "calls": 1, "prompt_tokens": 10, "cached_tokens": 99}),
        ("llm_usage", {"usage_id": "u3", "calls": 1, "prompt_tokens": 10, "cached_tokens": True}),
        ("llm_usage", {"usage_id": "u4", "calls": 1, "prompt_tokens": 10, "cached_tokens": "9"}),
        ("llm_usage", {"usage_id": "u5", "calls": 1, "prompt_tokens": 10, "cached_tokens": -9}),
    ])).llm_cost
    assert ledger["cached_tokens"] == 70, "a repeat id counts once; 99 clamps to its prompt of 10"

    assert "cached_tokens" not in fold(_events([
        ("llm_usage", {"usage_id": "u1", "calls": 1, "prompt_tokens": 100}),
        ("llm_usage", {"usage_id": "u2", "calls": 1, "prompt_tokens": 10, "cached_tokens": 0}),
    ])).llm_cost


def test_a_legacy_summary_base_is_sanitized_not_copied():
    """`_clean_llm_totals` copies a summary's other keys verbatim; this one it must not.

    MUTATION: leave `cached_tokens` to the verbatim copy -> a junk string lands in the ledger and
    the next usage row's integer addition raises out of the fold."""
    for junk, expected in (("lots", None), (500, 40), (-1, None), (25, 25)):
        ledger = fold(_events([
            ("llm_cost", {"cost": 1.0, "calls": 2, "prompt_tokens": 40, "cached_tokens": junk}),
            ("llm_usage", {"usage_id": "u1", "calls": 1, "prompt_tokens": 10,
                           "cached_tokens": 5}),
        ])).llm_cost
        assert ledger.get("cached_tokens") == (5 if expected is None else expected + 5)


def test_the_resume_mirror_agrees_with_the_fold_on_the_hits(tmp_path):
    """`persisted_usage_deltas` is the fold's rule for the engine; the new column is held to it too."""
    rng = random.Random(6932)
    for case in range(200):
        rows = []
        for _ in range(rng.randint(1, 12)):
            prompt = rng.choice([0, 10, 250])
            data = {"cost": 0.01, "calls": 1, "prompt_tokens": prompt,
                    "cached_tokens": rng.choice([0, 5, 300, -1, True, "5", None])}
            if rng.random() < 0.2:
                rows.append(("llm_cost", data))
            else:
                data["usage_id"] = rng.choice(["a", "b", "c", "d"])
                rows.append(("llm_usage", data))
        events = _events(rows)
        folded = fold(events).llm_cost or {}
        mirrored = persisted_usage_deltas(events)
        assert sum(d.get("cached_tokens", 0) for d in mirrored) == folded.get("cached_tokens", 0), (
            f"case {case}")


# ---------------------------------------------------------------------------------- the spans
def test_the_span_usage_carries_the_hits_sparsely_and_clamped():
    assert tracing._norm_usage(_usage(100, 5, cached_tokens=40))["cached"] == 40
    assert tracing._norm_usage({"prompt": 10, "completion": 1, "cached": 50})["cached"] == 10
    assert set(tracing._norm_usage(_usage(100, 5))) == {"prompt", "completion", "total"}
    assert "cached" not in tracing._norm_usage(_usage(100, 5, cached_tokens=float("inf")))


def test_two_billings_of_one_generation_sum_their_hits():
    summed = tracing._sum_usage({"prompt": 100, "completion": 5, "total": 105, "cached": 40},
                                {"prompt": 100, "completion": 5, "total": 105, "cached": 60})
    assert summed == {"prompt": 200, "completion": 10, "total": 210, "cached": 100}
    assert "cached" not in tracing._sum_usage({"prompt": 1, "completion": 1, "total": 2},
                                              {"prompt": 1, "completion": 1, "total": 2})


def test_a_committed_call_stamps_its_hits_on_the_open_generation(tmp_path):
    """`CostAccountant.add` stamps the span from below (`tracing.record_paid_call`); a generation
    billed twice carries the SUM of both commits' hits, like its tokens and its cost."""
    path = tmp_path / "s.jsonl"
    tracer = Tracer(JsonlSpanExporter(path), run_id="r")
    with tracer.span("propose", new_trace=True, node_id=1):
        with tracing.generation(op="chat", model="m"):
            CostAccountant().add(0.0, _usage(100, 5, prompt_tokens_details={"cached_tokens": 64}))
            CostAccountant().add(0.0, _usage(100, 5, prompt_tokens_details={"cached_tokens": 36}))
    spans = [orjson.loads(line) for line in path.read_bytes().splitlines()]
    generation = next(span for span in spans if span["kind"] == "generation")
    assert generation["attributes"]["usage"] == {"prompt": 200, "completion": 10, "total": 210,
                                                 "cached": 100}


def _gen(phase, prompt, cached=None):
    usage = {"prompt": prompt, "completion": 1, "total": prompt + 1}
    if cached is not None:
        usage["cached"] = cached
    return {"kind": "generation", "attributes": {"phase": phase, "usage": usage}}


def test_the_phase_breakdown_sums_the_hits_per_phase():
    out = token_spend_by_phase([_gen("plan", 100, 60), _gen("plan", 50), _gen("propose", 10, 99),
                                _gen("stages", 10, "junk")])
    by = {row["phase"]: row for row in out["rows"]}
    assert by["plan"]["cached"] == 60
    assert by["propose"]["cached"] == 10, "clamped to the generation's own prompt"
    assert by["stages"]["cached"] == 0


# -------------------------------------------------------------------------- `looplab tokens`
def _run_dir(tmp_path, *, cached_ledger: int | None, span_cached: int | None):
    d = tmp_path / "run"
    d.mkdir()
    usage = {"usage_id": "u1", "calls": 1, "prompt_tokens": 300, "completion_tokens": 100,
             "total_tokens": 400}
    if cached_ledger is not None:
        usage["cached_tokens"] = cached_ledger
    rows = [{"v": 1, "seq": 0, "ts": 1.0, "type": "run_started",
             "data": {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"}},
            {"v": 1, "seq": 1, "ts": 2.0, "type": "llm_usage", "data": usage}]
    (d / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    span_usage = {"prompt": 300, "completion": 100, "total": 400}
    if span_cached is not None:
        span_usage["cached"] = span_cached
    (d / "spans.jsonl").write_text(json.dumps({
        "name": "generation", "kind": "generation", "trace_id": "a" * 32, "span_id": "b" * 16,
        "run_id": "r", "attributes": {"op": "chat", "model": "m", "phase": "propose",
                                      "usage": span_usage}}) + "\n")
    return d


def _tokens(run_dir) -> str:
    result = CliRunner().invoke(app, ["tokens", str(run_dir)])
    assert result.exit_code == 0, result.output
    return result.output


def test_tokens_says_how_much_of_the_prompt_the_provider_cache_served(tmp_path):
    out = _tokens(_run_dir(tmp_path, cached_ledger=240, span_cached=240))
    assert "cache hits :            240 of 300 prompt tokens (80.0%)" in out
    header = next(line for line in out.splitlines() if line.lstrip().startswith("tokens"))
    assert "cached" in header
    row = next(line for line in out.splitlines() if line.rstrip().endswith("propose"))
    assert "240" in row


def test_tokens_prints_the_historical_report_when_no_hit_was_reported(tmp_path):
    out = _tokens(_run_dir(tmp_path, cached_ledger=None, span_cached=None))
    assert "cache hits" not in out and "cached" not in out


def test_tokens_without_spans_still_says_the_ledgers_hits(tmp_path):
    run = _run_dir(tmp_path, cached_ledger=150, span_cached=None)
    (run / "spans.jsonl").unlink()
    result = CliRunner().invoke(app, ["tokens", str(run)])
    assert result.exit_code == 2
    assert "cache hits :            150 of 300 prompt tokens (50.0%)" in result.output
