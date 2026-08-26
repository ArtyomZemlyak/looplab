"""EVERY PAID CALL IS IN A SPAN, and every span says what it cost.

WHAT THIS PINS. `events.jsonl` and `spans.jsonl` are two channels over the same money. The event log
is the ledger (`llm_usage`, appended by the accountant's sink); the span tree is the only channel
that can say WHERE the money went, because `Tracer.span` stamps `phase` onto every generation
beneath it. When the two disagree, every per-phase cost question in the repo — `looplab timings`,
the trace view, the cost panel, and any campaign post-mortem — is answered over a fraction of the
budget WITHOUT SAYING SO, which is worse than not answering at all.

MEASURED, twenty arm-B AlgoTune runs (`/var/tmp/looplab-bench/runs-B`, 2026-08-24): `llm_usage`
totalled $20.0081 over 6,819 calls, generation spans $17.6867 over 5,903. The missing $2.3214 was
three separate breakages of ONE property, and each has a test below:

  * $2.1921 / 817 calls — concurrent deep research. `_research_attempt_step` is the indivisible
    receipt→provider→record step shared by the serial cadence and both concurrent seams, and only
    the SERIAL caller wrapped it in a span. `tracing.generation()` yields a NULL handle when no span
    is open, so the two seams that actually run under the shipped `concurrent_research` wrote every
    call to the event log with `trace_id=null, span_id=null` and to no span at all.
  * $0.0211 / 88 calls — `_tag_hypothesis_concepts`, which pays AFTER the `concept_coverage` span
    it lives beside has already closed.
  * $0.1015 / 36 calls — calls that DID open a generation span and carried no `cost`: the accountant
    commits the delta, emits the `llm_usage` row and then raises `BudgetExceeded`, so the caller
    never reaches its own `.cost(...)`. A span that names a phase and no money is invisible to a sum
    over the span channel exactly like a call with no span.

WHY THE GUARD IS A TEST AND NOT AN ASSERTION IN THE TRACER. The tracer cannot see this defect: the
failure mode IS that nothing calls the tracer. The one place that sees every paid call is
`CostAccountant.add` — but there `"no span is open"` and `"nothing is being traced"` are the same
observation (`_current_tracer` is bound only inside `Tracer.span`), and paying without a tracer is
legitimate for the CLI one-shots, the library seam and most of this suite. An assertion there would
either fire constantly or need a new process-global "tracing is live" flag whose upkeep is itself an
invariant nobody would maintain. And the rule the observability code already lives by
(`shared.py::_progress`) is that a diagnostic may never fail the work it reports on. So the guard is
here, over runs that actually execute the paid seams, where the whole channel is checkable at once —
and it is a CONSERVATION check, not a per-site checklist, so a fourth spanless site fails it too.
"""
from __future__ import annotations

import json
import types
import uuid

import anyio
import pytest

from looplab.core import tracing
from looplab.core.fitness import VERIFIER_SELECTION_CONTRACT
from looplab.core.llm import BudgetExceeded, CostAccountant
from looplab.core.models import ResearchMemo
from looplab.events.replay import fold
from factories import make_engine


# --------------------------------------------------------------------------- the conservation check

def _rows(run_dir):
    """(`llm_usage` events, `generation` spans) as written to disk."""
    events = []
    for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") == "llm_usage":
            events.append(event)
    spans = []
    spans_file = run_dir / "spans.jsonl"
    if spans_file.exists():
        for line in spans_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            span = json.loads(line)
            if span.get("name") == "generation":
                spans.append(span)
    return events, spans


def assert_span_channel_accounts_for_every_paid_call(engine, *, at_least: int = 1):
    """THE GUARD. Fails when the paid calls and the span tree disagree about this run's money.

    `at_least` is the non-vacuity latch: a check that passes because nothing was bought proves
    nothing, so every caller states how many paid calls its scenario must have produced.
    """
    engine.tracer.force_flush()          # spans export asynchronously; settle before reading
    events, spans = _rows(engine.run_dir)
    assert len(events) >= at_least, (
        f"scenario bought nothing: {len(events)} llm_usage rows, expected >= {at_least}")

    by_id = {span["span_id"]: span for span in spans}
    spanless = [e for e in events if e.get("span_id") not in by_id]
    assert not spanless, (
        f"{len(spanless)} paid call(s) opened no generation span — "
        f"${sum(float(e['data'].get('cost') or 0) for e in spanless):.4f} in the event log and in "
        f"no span at all (span_ids: {[e.get('span_id') for e in spanless][:5]})")

    costless = [e for e in events if by_id[e["span_id"]]["attributes"].get("cost") is None]
    assert not costless, (
        f"{len(costless)} paid call(s) have a generation span that carries no cost — "
        f"${sum(float(e['data'].get('cost') or 0) for e in costless):.4f} invisible to every sum "
        "taken over the span channel")

    paid = sum(float(e["data"].get("cost") or 0) for e in events)
    traced = sum(float(by_id[e["span_id"]]["attributes"]["cost"]) for e in events)
    assert paid == pytest.approx(traced, abs=1e-9), (
        f"event log says ${paid:.6f}, generation spans say ${traced:.6f}")
    return paid


# --------------------------------------------------------------------------- a faithful paid call

def _pay(engine, cost=0.01):
    """One provider call, spelled the way `core/llm.py` spells it.

    The two things that matter are both real: `tracing.generation` writes a generation span only
    when a span is already open (else it yields the null handle), and `EventStore.append` stamps the
    row with `tracing.current_ids()`. A call made outside every span therefore lands in the ledger
    with null ids and in no span — which is the production defect, reproduced without a provider.
    """
    usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
    with tracing.generation(op="chat", model="fake-model",
                            messages=[{"role": "user", "content": "hi"}]) as gen:
        gen.output("ok").usage(usage).cost(cost)
        engine.store.append("llm_usage", {
            "cost": cost, "calls": 1, "priced_calls": 1, "prompt_tokens": 10,
            "completion_tokens": 2, "total_tokens": 12, "usage_id": uuid.uuid4().hex})


def _span_names(engine):
    engine.tracer.force_flush()
    path = engine.run_dir / "spans.jsonl"
    if not path.exists():          # a scenario that opened no span at all writes no file
        return []
    return [json.loads(line)["name"]
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------- deep research

class _PayingResearcher:
    """A DeepResearcher-shaped stub whose `research()` costs money, like the real one."""

    parser = "tool_call"
    client = None

    def __init__(self, engine, calls=3):
        self._engine, self._calls = engine, calls

    def research(self, state, *, trigger):
        for _ in range(self._calls):
            _pay(self._engine)
        return ResearchMemo(at_node=len(state.nodes), trigger=trigger,
                            summary="a memo the loop paid for")


def _research_engine(tmp_path, **overrides):
    engine = make_engine(tmp_path / "run", max_nodes=2, **overrides)
    engine.deep_researcher = _PayingResearcher(engine)
    return engine


def test_the_concurrent_research_seam_pays_inside_a_span(tmp_path):
    """The 817-call, $2.1921 hole. Driven through the SAME hop `_spawn_research` uses.

    `anyio.to_thread.run_sync` is not incidental: the old docstring justified the missing span with
    "the tracer is not safe to write from the concurrent worker", so the test has to run the step in
    the worker thread for its passing to mean anything.
    """
    engine = _research_engine(tmp_path)
    state = fold(engine.store.read_all())

    async def drive():
        await anyio.to_thread.run_sync(
            lambda: engine._research_attempt_step(state, "cadence", manual=False))

    anyio.run(drive)

    assert "deep_research" in _span_names(engine), (
        "the concurrent seam must open the deep_research op-trace, not leave its calls spanless")
    assert_span_channel_accounts_for_every_paid_call(engine, at_least=3)


def test_the_serial_cadence_still_opens_exactly_one_deep_research_span(tmp_path):
    """Moving the span down into the shared step must not double it on the path that already had it."""
    engine = _research_engine(tmp_path)
    engine._run_deep_research(fold(engine.store.read_all()), trigger="cadence", manual=False)

    assert _span_names(engine).count("deep_research") == 1
    assert_span_channel_accounts_for_every_paid_call(engine, at_least=3)


def test_a_researcher_that_pays_nothing_still_records_its_memo(tmp_path):
    """The span is opened unconditionally around the step, so the no-cost path must be unaffected."""
    engine = _research_engine(tmp_path)
    engine.deep_researcher = _PayingResearcher(engine, calls=0)
    state = engine._run_deep_research(fold(engine.store.read_all()), trigger="cadence", manual=False)
    types_seen = [e.type for e in engine.store.read_all()]
    assert "research_completed" in types_seen and state is not None


# --------------------------------------------------------------------------- hypothesis tagging

class _Graph:
    @staticmethod
    def concepts():
        return ["c1", "c2"]


def _card(card_id, statement):
    return types.SimpleNamespace(id=card_id, statement=statement, card_id=card_id)


def test_hypothesis_tagging_pays_inside_a_span(tmp_path, monkeypatch):
    """The 88-call, $0.0211 hole: a paid step that runs after `concept_coverage` has closed."""
    engine = make_engine(tmp_path / "run", max_nodes=2)
    cards = [_card("card-1", "vectorize the inner loop"), _card("card-2", "cache the kernel")]
    monkeypatch.setattr("looplab.search.concept_tagging.tag_text_llm",
                        lambda *a, **k: (_pay(engine, 0.002), ["c1"])[1])
    state = types.SimpleNamespace(hypothesis_concepts={}, hypothesis_concepts_at_vocab={},
                                  nodes={}, research_cards=lambda: cards)

    engine._tag_hypothesis_concepts(state, _Graph(), object(), "tool_call", "llm")

    names = _span_names(engine)
    assert "hypothesis_tagging" in names, "the tagging pass must open its own op-trace"
    assert names.count("hypothesis_tagging") == 1, "one span for the whole bounded pass, not one per card"
    assert_span_channel_accounts_for_every_paid_call(engine, at_least=2)


def test_a_tagging_pass_with_nothing_to_tag_opens_no_span(tmp_path, monkeypatch):
    """Lazily opened on the FIRST paid tag: the common cadence pays nothing and must add no noise."""
    engine = make_engine(tmp_path / "run", max_nodes=2)
    monkeypatch.setattr("looplab.search.concept_tagging.tag_text_llm",
                        lambda *a, **k: pytest.fail("nothing should have been tagged"))
    state = types.SimpleNamespace(hypothesis_concepts={}, hypothesis_concepts_at_vocab={},
                                  nodes={}, research_cards=list)

    engine._tag_hypothesis_concepts(state, _Graph(), object(), "tool_call", "llm")

    assert "hypothesis_tagging" not in _span_names(engine)


# --------------------------------------------------------------------------- the ceiling abort

def test_a_call_aborted_on_the_spend_ceiling_still_carries_its_cost(tmp_path):
    """The 36-call, $0.1015 hole: paid, spanned, and the span said nothing about the money.

    `CostAccountant.add` commits the delta, hands it to the durable sink and THEN raises, so the
    caller's own `.usage(...).cost(...)` line is never reached. The stamp therefore has to happen
    where the money is committed.
    """
    engine = make_engine(tmp_path / "run", max_nodes=2)
    accountant = CostAccountant(limit=0.001,
                            on_delta=lambda d: engine.store.append("llm_usage", dict(d)))

    with engine.tracer.span("card_build"):
        with pytest.raises(BudgetExceeded):
            with tracing.generation(op="chat", model="fake-model",
                                    messages=[{"role": "user", "content": "hi"}]):
                accountant.add(0.004, {"prompt_tokens": 10, "completion_tokens": 2,
                                       "total_tokens": 12})
                pytest.fail("unreachable: the accountant raises on the ceiling")   # pragma: no cover

    paid = assert_span_channel_accounts_for_every_paid_call(engine, at_least=1)
    assert paid == pytest.approx(0.004)
    engine.tracer.force_flush()
    generations = [json.loads(line)
                   for line in (engine.run_dir / "spans.jsonl").read_text().splitlines()
                   if line.strip() and json.loads(line)["name"] == "generation"]
    assert generations[0]["status"] == "ERROR"                    # the abort is still recorded…
    assert generations[0]["attributes"]["phase"] == "card_build"  # …under the phase that bought it


# --------------------------------------------------------------------------- the verifier tie-break

def test_the_verifier_tiebreak_pays_inside_a_span(tmp_path, monkeypatch):
    """Found by INSPECTION, not by measurement — and that is exactly why it needs a test.

    `_maybe_verify_ties` runs from `_run_cadences` with no span open, beside two siblings
    (`_maybe_snapshot_concept_coverage`, `_maybe_consult_strategist`) that each open one. It is off
    by default (`select_verifier`), so the arm-B corpus that measured the other sites bought nothing
    here and the defect would only have surfaced in the first campaign that turned the knob on.
    """
    engine = make_engine(tmp_path / "run", max_nodes=2)
    tied = [types.SimpleNamespace(id=1, attempt=0), types.SimpleNamespace(id=2, attempt=0)]
    monkeypatch.setattr(type(engine), "_metric_tie_groups", lambda self, state: [tied],
                        raising=True)
    monkeypatch.setattr(type(engine), "_reflect_client", lambda self: object(), raising=True)

    def _verify(self, state, node, client):
        _pay(engine, 0.003)
        return {"score": 0.5, "n_samples": 1, "agreement": 1.0, "method": "stub"}

    monkeypatch.setattr(type(engine), "_verifier_soundness", _verify, raising=True)
    state = types.SimpleNamespace(
        select_verifier_tiebreak=True, select_verifier_contract=VERIFIER_SELECTION_CONTRACT,
        select_verifier_samples=1, direction="min")

    engine._maybe_verify_ties(state)

    assert "verifier_tiebreak" in _span_names(engine)
    assert_span_channel_accounts_for_every_paid_call(engine, at_least=2)
