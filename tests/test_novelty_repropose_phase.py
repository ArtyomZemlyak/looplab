"""The gate's adjudication and the second proposal it buys are DIFFERENT phases (doc 53 §2).

MEASURED, `/var/tmp/looplab-bench/runs-B` (20 AlgoTune task-arms, 2026-08-26). Attributing every
generation span to its `attributes.phase`, the `novelty` phase carried $1.3151 of $17.6867. Walking
each generation's `input_from` chain back to the system prompt that ROOTED it splits that in two:

    adjudicator ("You judge experiment NOVELTY…")   $0.1141   231 calls    0.6 % of the run
    Researcher + claim verifier (the re-proposal)   $1.1758   257 calls    6.6 % of the run

The second line is the whole second proposal `_reject_and_repropose` pays for on a rejection — on
`convex_hull`, $0.1530 against $0.0026 of adjudication, and that $0.1530 is priced exactly like an
ordinary `propose` phase on the same task ($0.038–$0.135) because it IS one. It inherited the label
`novelty` for one reason: `Tracer.span` stamps the innermost open OPERATION onto every generation
underneath, and the re-proposal had no span of its own.

That is a measurement trap, not a cosmetic one. Doc 53 §2 opened `novelty-gate-costs-two-thirds-
and-decides-nothing` on this money, and `shared.py::_paid_progress` records an earlier reading of
the same conflation ($1.77, "the novelty gate"). So this pins the SPLIT itself: an adjudication that
costs what it costs, and a re-proposal billed where re-proposals are billed. It deliberately does
NOT pin the total — the total was never wrong; the label on it was.

The last test pins the other half of the contract: the span is an observation, so it may not move a
verdict. `_reject_and_repropose` must still return the changed idea and still audit `reproposed`.
"""
from __future__ import annotations

import types

import orjson

from looplab.core import tracing
from looplab.core.models import Idea
from looplab.core.tracing import JsonlSpanExporter, Tracer
from looplab.engine.novelty import NoveltyGateMixin
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


class _Store:
    def __init__(self):
        self.appended: list[tuple[str, dict]] = []

    def append(self, event_type, data):
        self.appended.append((event_type, data))

    def read_all(self):
        return []


class _Gate(NoveltyGateMixin):
    """A bare mixin host — `self` IS the Engine in production (see test_novelty_rejection_audit)."""

    def __init__(self, tracer=None):
        self.store = _Store()
        self.researcher = types.SimpleNamespace()
        self.tracer = tracer
        self._novelty_stance = "balanced"
        self._novelty_mode = "llm"


def _state(tmp_path):
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "improve",
                              "idea": {"operator": "improve", "params": {},
                                       "rationale": "the duplicate experiment"}})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.42})
    return fold(s.read_all())


def _spend(model: str, dollars: float):
    """One priced LLM call under whatever operation span is live."""
    with tracing.generation(op="chat", model=model,
                            messages=[{"role": "system", "content": model}]) as g:
        g.output("ok").usage({"prompt_tokens": 10, "completion_tokens": 1,
                              "total_tokens": 11}).cost(dollars)


def _run_a_rejection(tmp_path):
    """The gate's shape on a rejection: adjudication is paid INSIDE the `novelty` span, then the
    Researcher is asked once more — the call that actually costs the money."""
    path = tmp_path / "spans.jsonl"
    gate = _Gate(Tracer(JsonlSpanExporter(path), run_id="r", capture_llm_io=True))
    state = _state(tmp_path)
    original = Idea(operator="improve", params={}, rationale="a rewording of #0")

    def _repropose():
        _spend("researcher", 0.1500)          # the second proposal, priced like the first one
        _spend("verifier", 0.0030)            # ... and its claim verifier
        return Idea(operator="improve", params={}, rationale="something genuinely different")

    with gate.tracer.span("novelty", new_trace=True, node_id=1):
        _spend("adjudicator", 0.0026)         # "You judge experiment NOVELTY…"
        out = gate._reject_and_repropose(
            state, original, state.nodes[0], kind="llm",
            hint="\nNOVELTY GATE (LLM): your proposal near-duplicates experiment #0.",
            payload={"reason": "a rewording"}, repropose=_repropose,
            researcher=None, prospective_node_id=1)

    recs = [orjson.loads(line) for line in path.read_bytes().splitlines()]
    by_phase: dict[str, float] = {}
    for r in recs:
        if r["kind"] != "generation":
            continue
        a = r["attributes"]
        by_phase[a.get("phase")] = round(by_phase.get(a.get("phase"), 0.0) + a["cost"], 6)
    return gate, out, by_phase, recs


def test_only_the_adjudication_is_billed_to_the_novelty_phase(tmp_path):
    """$0.0026 of judging is what the gate cost. Anything else under this label is a trap: it is
    what turned a 0.6 % adjudicator into "two thirds of the budget"."""
    _, _, by_phase, _ = _run_a_rejection(tmp_path)
    assert by_phase["novelty"] == 0.0026


def test_the_second_proposal_the_gate_buys_is_billed_to_repropose(tmp_path):
    """The Researcher call and its verifier are proposal work. They get a phase of their own, so
    `novelty` can be read as "what the gate itself cost" without walking any prompt chains."""
    _, _, by_phase, _ = _run_a_rejection(tmp_path)
    assert by_phase["repropose"] == 0.1530


def test_the_repropose_span_nests_inside_the_gate_that_opened_it(tmp_path):
    """It is a CHILD of `novelty`, never a sibling: the re-proposal exists because the gate rejected
    something, so a reader summing the gate's total consequence must still be able to."""
    _, _, _, recs = _run_a_rejection(tmp_path)
    by_name = {r["name"]: r for r in recs if r["kind"] == "operation"}
    assert by_name["repropose"]["parent_id"] == by_name["novelty"]["span_id"]
    assert by_name["repropose"]["trace_id"] == by_name["novelty"]["trace_id"]


def test_the_span_changes_no_verdict_and_no_audit(tmp_path):
    """An observation may not move a decision. The gate still returns the CHANGED idea and still
    records `reproposed` — the one line in the log that says the paid second call bought anything."""
    gate, out, _, _ = _run_a_rejection(tmp_path)
    assert out.rationale == "something genuinely different"
    assert [name for name, _ in gate.store.appended] == ["novelty_rejected"]
    assert gate.store.appended[0][1]["action"] == "reproposed"


def test_a_gate_with_no_tracer_still_re_proposes(tmp_path):
    """Tests build `Engine` via `__new__` and carry no tracer; observability may never decide
    whether the re-proposal runs."""
    gate, state = _Gate(tracer=None), _state(tmp_path)
    out = gate._reject_and_repropose(
        state, Idea(operator="improve", params={}, rationale="a rewording of #0"),
        state.nodes[0], kind="llm", hint="dup", payload={"reason": "r"},
        repropose=lambda: Idea(operator="improve", params={}, rationale="different"),
        researcher=None, prospective_node_id=1)
    assert out.rationale == "different"
