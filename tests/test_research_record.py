"""The durable research record (doc 52 row 16): plan, exact-span evidence, literature, phases.

Four markers, one record. A Deep-Research memo's evidence was a URL plus a 200-character snippet
and its plan lived only inside one tool-loop context (doc 28 DR-01 / DR-02); the papers a pass read
left no durable trace (doc 51); a phase's trajectory lived only in `spans.jsonl` (doc 27). Now:
`ResearchMemo.plan` / `.evidence` / `.literature` ride the folded `research_completed` row, claims
are BOUND to evidence deterministically, `literature_retrieved` is a registered folded event, and
the three `agent_phase_*` DIAGNOSTIC rows are written by the engine's sink.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from looplab.agents import tool_loop as loop_mod
from looplab.agents.deep_research import DeepResearcher
from looplab.agents.tool_loop import drive_tool_loop
from looplab.core.advisory_payloads import sanitize_research_memo_payload
from looplab.core.models import Event, ResearchMemo, RunState
from looplab.core.phase_events import (PHASE_CHECKPOINTED, PHASE_COMPLETED, PHASE_STARTED,
                                       current_phase_sink, emit_phase_event, phase_sink_scope)
from looplab.core.research_record import (QUOTE_CHARS, bind_claims_to_evidence, evidence_item,
                                          parse_literature)
from looplab.core.source_identity import canonical_source_ref
from looplab.events.replay import fold
from looplab.events.types import (DIAGNOSTIC_EVENTS, EV_AGENT_CHECKPOINTED,
                                  EV_AGENT_PHASE_COMPLETED, EV_AGENT_PHASE_STARTED,
                                  EV_LITERATURE_RETRIEVED, EV_RESEARCH_COMPLETED)

from factories import make_engine

_ARXIV = ("1. Contrastive Learning of Sentence Embeddings\n"
          "   We present a simple contrastive framework for sentence embeddings.\n"
          "2. Hard Negative Mining for Dense Retrieval\n"
          "   Mining harder negatives improves dense retrievers.")


# ------------------------------------------------------------------ the builders

def test_an_evidence_item_is_an_exact_span_with_a_stable_identity():
    text = "x" * 2000
    a = evidence_item(tool="web_fetch", locator="web_fetch(u)", result=text, turn=3,
                      locator_identity="ident-1")
    b = evidence_item(tool="web_fetch", locator="web_fetch(u)", result=text, turn=9,
                      locator_identity="ident-1")
    assert a["id"] == b["id"], "same bytes from the same place: the same id, in any run"
    assert a["id"].startswith("ev-") and len(a["id"]) == 27
    assert a["quote"] == "x" * QUOTE_CHARS and a["bytes"] == 2000
    assert a["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert a["kind"] == "web" and a["turn"] == 3 and a["locator_identity"] == "ident-1"
    changed = evidence_item(tool="web_fetch", locator="web_fetch(u)", result=text + "!", turn=3,
                            locator_identity="ident-1")
    assert changed["id"] != a["id"], "a changed page is a different item"
    node = evidence_item(tool="read_experiment", locator="read_experiment(4)", result="m", turn=1,
                         node_id=4)
    assert node["kind"] == "experiment" and node["node_id"] == 4


def test_literature_is_parsed_from_the_arxiv_render_and_refusals_yield_nothing():
    papers = parse_literature(_ARXIV, query="dense retrieval")
    assert [p["title"] for p in papers] == ["Contrastive Learning of Sentence Embeddings",
                                            "Hard Negative Mining for Dense Retrieval"]
    assert all(p["id"].startswith("lit-") and len(p["id"]) == 28 for p in papers)
    assert papers[0]["abstract_sha256"] == hashlib.sha256(
        b"We present a simple contrastive framework for sentence embeddings.").hexdigest()
    assert papers[0]["query"] == "dense retrieval" and papers[0]["tool"] == "arxiv_search"
    assert parse_literature("(literature search unavailable: blocked)") == []
    assert parse_literature("(no results)") == [] and parse_literature("") == []
    assert parse_literature(_ARXIV)[0]["id"] == papers[0]["id"], "the id is over the title"


def test_claims_are_bound_to_evidence_by_url_identity_and_node_id_never_by_prose():
    ref = canonical_source_ref("https://example.test/paper")
    web = evidence_item(tool="web_fetch", locator="web_fetch(https://example.test/paper)",
                        result="page", turn=1, locator_identity=ref.identity)
    exp = evidence_item(tool="read_experiment", locator="read_experiment(7)", result="r", turn=2,
                        node_id=7)
    claims = [{"statement": "a", "url_identities": [ref.identity], "node_ids": []},
              {"statement": "b", "url_identities": [], "node_ids": [7]},
              {"statement": "c", "url_identities": ["other"], "node_ids": [1]}]
    bind_claims_to_evidence(claims, [web, exp])
    assert claims[0]["evidence_ids"] == [web["id"]]
    assert claims[1]["evidence_ids"] == [exp["id"]]
    assert claims[2]["evidence_ids"] == []


# ------------------------------------------------------------------ the stage captures it

class _Tools:
    def specs(self):
        return [{"type": "function", "function": {"name": n, "description": n,
                 "parameters": {"type": "object", "properties": {"query": {"type": "string"},
                                                                 "url": {"type": "string"}}}}}
                for n in ("arxiv_search", "web_fetch")]

    def execute(self, name, args):
        if name == "arxiv_search":
            return _ARXIV
        return "the page says the recall was 0.79"


class _Client:
    def __init__(self, scripted):
        self.scripted = list(scripted)

    def chat(self, messages, tools, tool_choice="auto"):
        return self.scripted.pop(0)


def _call(name, args, cid="c"):
    return {"content": "", "tool_calls": [{"id": cid, "function": {"name": name,
                                                                   "arguments": json.dumps(args)}}]}


def _driven_memo():
    client = _Client([
        _call("update_plan", {"plan": "read, then decide", "todos": [{"item": "read", "status": "in_progress"}]}, "p1"),
        _call("arxiv_search", {"query": "dense retrieval"}, "a1"),
        _call("web_fetch", {"url": "https://example.test/paper"}, "w1"),
        _call("update_plan", {"plan": "decide", "todos": [{"item": "read", "status": "done"},
                                                            {"item": "decide", "status": "in_progress"}]}, "p2"),
        _call("emit", {"summary": "s", "findings": ["f"],
                       "claims": [{"statement": "recall was 0.79", "node_ids": [],
                                   "urls": ["https://example.test/paper"]}],
                       "recommended_directions": ["d"]}, "e1"),
    ])
    return DeepResearcher(client, _Tools()).research(RunState(goal="g"), trigger="cadence")


def test_the_memo_carries_the_plan_the_evidence_and_the_literature():
    memo = _driven_memo()
    assert memo.plan == {"plan": "decide",
                         "todos": [{"item": "read", "status": "done"},
                                   {"item": "decide", "status": "in_progress"}],
                         "updates": 2}
    assert [e["kind"] for e in memo.evidence] == ["literature", "web"]
    assert [e["turn"] for e in memo.evidence] == [1, 2]
    assert [p["title"] for p in memo.literature][0] == "Contrastive Learning of Sentence Embeddings"
    ref = canonical_source_ref("https://example.test/paper")
    web = memo.evidence[1]
    assert web["locator_identity"] == ref.identity and web["quote"] == "the page says the recall was 0.79"
    assert memo.claims[0]["evidence_ids"] == [web["id"]], "bound to the page it cited, by identity"
    assert len(memo.sources) == 2, "the legacy sources ledger is untouched beside the record"


def test_the_record_survives_a_junk_emit():
    """The lists live on the memo from the start, so the summary-only fallback keeps them."""
    client = _Client([
        _call("update_plan", {"plan": "p", "todos": []}, "p1"),
        _call("arxiv_search", {"query": "q"}, "a1"),
        _call("emit", {"summary": 42, "claims": "not a list", "findings": 7}, "e1"),
    ])
    memo = DeepResearcher(client, _Tools()).research(RunState(goal="g"), trigger="cadence")
    assert memo.plan["plan"] == "p" and memo.plan["updates"] == 1
    assert len(memo.evidence) == 1 and len(memo.literature) == 2


# ------------------------------------------------------------------ the sanitizer and the fold

def test_the_sanitizer_keeps_only_well_formed_record_rows():
    good = evidence_item(tool="web_fetch", locator="l", result="r", turn=1, locator_identity="i")
    memo = sanitize_research_memo_payload({
        "summary": "s",
        "plan": {"plan": "p", "todos": [{"item": "a", "status": "bogus"}, {"item": ""}, "junk"],
                 "updates": -1},
        "evidence": [good, {"id": "ev-forged", "sha256": "x"}, {**good, "sha256": "zz"}, "junk"],
        "literature": [{"id": "lit-" + "c" * 24, "title": "T", "abstract_sha256": "d" * 64,
                        "abstract_chars": 3}, {"id": "nope", "title": "T2"}],
        "claims": [{"statement": "c", "evidence_ids": [good["id"], "ev-nope", 3]}],
    })
    assert memo["plan"] == {"plan": "p", "todos": [{"item": "a", "status": "pending"}], "updates": 0}
    assert [e["id"] for e in memo["evidence"]] == [good["id"]]
    assert [p["id"] for p in memo["literature"]] == ["lit-" + "c" * 24]
    assert memo["claims"][0]["evidence_ids"] == [good["id"]]
    old = sanitize_research_memo_payload({"summary": "old"})
    assert "plan" not in old and "evidence" not in old and "literature" not in old


def _events(rows):
    return [Event(seq=i, ts=float(i), type=t, data=d) for i, (t, d) in enumerate(rows)]


def test_the_fold_applies_the_record_and_an_old_log_reads_as_none():
    good = evidence_item(tool="web_fetch", locator="l", result="r", turn=1, locator_identity="i")
    plan = {"plan": "p", "todos": [{"item": "a", "status": "done"}], "updates": 1}
    lit = {"id": "lit-" + "c" * 24, "title": "T", "abstract_sha256": "d" * 64, "abstract_chars": 3,
           "query": "q", "tool": "arxiv_search"}
    st = fold(_events([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "min"}),
        (EV_RESEARCH_COMPLETED, {"memo": {"summary": "old memo"}, "at_node": 0, "trigger": "cadence"}),
        (EV_RESEARCH_COMPLETED, {"memo": {"summary": "s", "plan": plan, "evidence": [good],
                                          "literature": [lit]}, "at_node": 1, "trigger": "cadence"}),
        (EV_LITERATURE_RETRIEVED, {"memo_id": None, "at_node": 1, "items": [lit]}),
        (EV_LITERATURE_RETRIEVED, {"at_node": 2, "items": [lit]}),      # a repeat is one paper
    ]))
    assert st.research_plan == plan
    assert list(st.research_evidence) == [good["id"]]
    assert [row["id"] for row in st.literature] == [lit["id"]] and st.literature[0]["at_node"] == 1
    old = fold(_events([("run_started", {"run_id": "r", "task_id": "t", "direction": "min"}),
                        (EV_RESEARCH_COMPLETED, {"memo": {"summary": "old"}, "at_node": 0})]))
    assert old.research_plan is None and old.research_evidence == {} and old.literature == []


def test_the_recorder_appends_the_literature_event_beside_the_memo(tmp_path):
    engine = make_engine(tmp_path / "run")
    memo = ResearchMemo(summary="s", at_node=0, trigger="cadence",
                        literature=parse_literature(_ARXIV, query="q"))
    engine._record_deep_research(memo, trigger="cadence", manual=False)
    types = [e.type for e in engine.store.read_all()]
    assert types[-2:] == [EV_RESEARCH_COMPLETED, EV_LITERATURE_RETRIEVED]
    row = engine.store.read_all()[-1].data
    assert [p["title"] for p in row["items"]][1] == "Hard Negative Mining for Dense Retrieval"
    bare = ResearchMemo(summary="s2", at_node=0, trigger="cadence")
    engine._record_deep_research(bare, trigger="cadence", manual=False)
    assert [e.type for e in engine.store.read_all()][-1] == EV_RESEARCH_COMPLETED


# ------------------------------------------------------------------ phases, event-sourced

_EMIT = {"type": "function", "function": {"name": "answer", "description": "a",
         "parameters": {"type": "object", "properties": {"reply": {"type": "string"}}}}}


class _PlanThenEmit:
    model = "m"

    def __init__(self, emits=True):
        self.turns = 0
        self.emits = emits

    def chat(self, messages, tool_specs, tool_choice="auto", **kw):
        self.turns += 1
        if self.turns == 1:
            return _call("update_plan", {"plan": "first", "todos": [{"item": "t", "status": "pending"}]})
        if self.emits:
            return _call("answer", {"reply": "done"}, "e")
        return {"content": "prose only"}


def test_the_loop_reports_its_three_moments_to_the_installed_sink():
    rows = []
    with phase_sink_scope(lambda t, d: rows.append((t, d))):
        out = drive_tool_loop(_PlanThenEmit(), None, [{"role": "user", "content": "go"}], _EMIT,
                              finalize=lambda a: a.get("reply"), fallback=lambda m: "fb",
                              self_plan=True, phase_label="propose", time_budget_s=30.0)
    assert out == "done"
    assert [t for t, _ in rows] == [PHASE_STARTED, PHASE_CHECKPOINTED, PHASE_COMPLETED]
    started, checkpoint, completed = (d for _, d in rows)
    assert started["label"] == "propose" and started["emit"] == "answer"
    assert started["time_budget_s"] == 30.0
    assert checkpoint["plan"] == "first" and checkpoint["todos"] == [{"item": "t", "status": "pending"}]
    assert completed["exit"] == "emitted" and completed["plan_updates"] == 1


def test_a_fallback_exit_is_named_and_the_label_defaults_to_the_emit():
    rows = []
    with phase_sink_scope(lambda t, d: rows.append((t, d))):
        drive_tool_loop(_PlanThenEmit(emits=False), None, [{"role": "user", "content": "go"}], _EMIT,
                        finalize=lambda a: a.get("reply"), fallback=lambda m: "fb",
                        self_plan=True, max_turns=2)
    assert rows[0][1]["label"] == "answer"
    assert rows[-1][0] == PHASE_COMPLETED and rows[-1][1]["exit"] == "fallback"


def test_without_a_sink_nothing_is_reported_and_a_bad_type_is_refused():
    assert current_phase_sink() is None
    emit_phase_event(PHASE_STARTED, {"label": "x"})          # a no-op, not an error
    with pytest.raises(ValueError):
        emit_phase_event("node_created", {})


def test_a_broken_sink_never_breaks_the_loop():
    def boom(t, d):
        raise RuntimeError("sink down")
    with phase_sink_scope(boom):
        out = drive_tool_loop(_PlanThenEmit(), None, [{"role": "user", "content": "go"}], _EMIT,
                              finalize=lambda a: a.get("reply"), fallback=lambda m: "fb",
                              self_plan=True)
    assert out == "done"


def test_run_phase_names_the_phase_it_drives(monkeypatch):
    from looplab.agents import agent as agent_mod
    seen = {}

    def fake_loop(client, tools, messages, emit_spec, **kw):
        seen.update(kw)
        return "r"

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    agent_mod.run_phase(object(), None, [{"role": "user", "content": "go"}], _EMIT,
                        label="Developer·plan", handoff=False,
                        finalize=lambda a: a, fallback=lambda m: None)
    assert seen["phase_label"] == "Developer·plan"


def test_the_engine_sink_appends_redacted_diagnostic_rows(tmp_path):
    engine = make_engine(tmp_path / "run")
    secret = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890abcd"
    engine._append_phase_event(EV_AGENT_CHECKPOINTED, {
        "label": "propose", "plan": f"use {secret}", "turn": 1,
        "todos": [{"item": f"call {secret}", "status": "pending"}]})
    row = engine.store.read_all()[-1]
    assert row.type == EV_AGENT_CHECKPOINTED and row.type in DIAGNOSTIC_EVENTS
    assert secret not in row.data["plan"] and secret not in row.data["todos"][0]["item"]
    with pytest.raises(AssertionError):
        engine._append_phase_event(EV_RESEARCH_COMPLETED, {})
    for etype in (EV_AGENT_PHASE_STARTED, EV_AGENT_PHASE_COMPLETED):
        assert etype in DIAGNOSTIC_EVENTS


def test_the_run_installs_the_sink_for_its_whole_loop():
    """By AST over the function that owns the run's scopes: the sink scope is entered beside the
    broker scope, so every loop on the main task, a build worker or the research task reports."""
    import ast
    import inspect
    import textwrap

    from looplab.engine.orchestrator import Engine

    src = inspect.getsource(Engine)
    tree = ast.parse(textwrap.dedent(src))
    withs = [n for n in ast.walk(tree) if isinstance(n, ast.With)]
    assert any(
        {getattr(item.context_expr.func, "id", "") for item in w.items
         if isinstance(item.context_expr, ast.Call)} >= {"llm_broker_scope", "phase_sink_scope"}
        for w in withs), "the phase sink must be installed in the same `with` as the broker scope"
