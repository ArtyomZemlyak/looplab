"""Every run that ends on the budget ceiling lost its report, which is the one artefact a human reads.

Finalization runs AFTER the ceiling has fired, so the report writer's call is refused and the whole
report collapsed to `headline: "(report unavailable)"`. Measured 2026-08-27 over the probe corpus:
THREE of three ceiling-terminated runs recorded exactly that — `fullctx` (convex_hull, champion
3.7777), `gpt56luna` (edge_expansion, champion 26.9553), `fxKcenters` (kcenters, champion 40.0878).
The runs that went the whole distance are precisely the ones that lose their summary.

Reflection already solves this correctly: `engine/finalize.py` says `write_reflection_note`
"degrades to a deterministic meta-note when the provider is exhausted -- which on this path it
usually is". The report had no such path and discarded the facts `_report_context` had assembled one
frame earlier: champion id, its metric, node counts, stop reason.

The verdict still OPENS with the legacy failure marker, so
`advisory_payloads._report_verdict` keeps collapsing a raw exception into its canonical phrase and
anything watching for a failed report still sees one.
"""
from __future__ import annotations

import pytest

from looplab.events.replay import fold
from looplab.core.models import Event
from looplab.serve.report import generate_report


def _boom(*_args, **_kwargs):
    raise RuntimeError("LLM spend ceiling reached: $1.0024 of the $1.0000 set by `llm_budget_usd`")


def _run_with_a_champion() -> "object":
    """A folded run with one evaluated node, the shape every ceiling-terminated probe has."""
    events = [
        Event(seq=0, type="run_started",
              data={"run_id": "r", "task_id": "algotune_kcenters", "goal": "g", "direction": "max"}),
        Event(seq=1, type="node_created",
              data={"node_id": 0, "parent_ids": [], "operator": "draft",
                    "idea": {"operator": "draft", "params": {"k": 3}, "rationale": "r"}}),
        Event(seq=2, type="node_evaluated", data={"node_id": 0, "metric": 40.0878}),
    ]
    return fold(events)


def test_a_refused_writer_still_reports_the_champion(monkeypatch):
    monkeypatch.setattr("looplab.agents.agent.agentic_struct", _boom)
    state = _run_with_a_champion()

    content = generate_report(state, client=object(), trigger="finalize")

    assert "(report unavailable)" not in content["headline"], content
    assert "40.0878" in content["headline"] or "40.09" in content["headline"], content["headline"]
    assert "#0" in content["headline"], content["headline"]
    assert "40.0878" in content["champion_summary"] or "40.09" in content["champion_summary"]
    assert "1 node" in content["summary"] and "1 evaluated" in content["summary"], content["summary"]
    # the failure is still declared, in the field the redactor canonicalizes
    assert content["verdict"].lstrip().startswith("(report generation failed:")


def test_the_deterministic_report_never_leaks_the_exception_text(monkeypatch):
    """The ceiling message names a dollar figure and a config key; neither belongs in a stored
    report, and `safe_provider_failure` is what keeps them out."""
    monkeypatch.setattr("looplab.agents.agent.agentic_struct", _boom)
    content = generate_report(_run_with_a_champion(), client=object(), trigger="finalize")
    blob = repr(content)
    assert "llm_budget_usd" not in blob, blob
    assert "$1.0024" not in blob, blob


def test_an_empty_run_says_so_rather_than_going_blank(monkeypatch):
    monkeypatch.setattr("looplab.agents.agent.agentic_struct", _boom)
    state = fold([Event(seq=0, type="run_started",
                        data={"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})])
    content = generate_report(state, client=object(), trigger="finalize")
    assert "no node was evaluated" in content["headline"].lower(), content["headline"]
