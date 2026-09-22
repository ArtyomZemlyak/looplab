"""Paid wrappers let the operator's spend ceiling through — driven, one site at a time.

Review 2026-09-22 (TAT-01, SCJ-03, ENG1-09): the containment census keyed on eight callee names, so
a blind `except Exception` around any WRAPPER of a paid call was invisible, and 38 of them turned
`BudgetExceeded` into their fallback. Under the legacy accountant (which raises AFTER the call is
billed) every swallow buys at least one more paid call: the fallback, the next roll, the next tick.
`tests/test_containment_census.py` now finds these sites by a transitive AST census; this file
DRIVES a representative of each shape through its real code with a client, summarizer, embedder or
judge that raises the ceiling, and pins beside each one that an ORDINARY failure still degrades
exactly as it always did — the fallback is kept for everything except the stop.
"""
from __future__ import annotations

import pytest

from looplab.core.errors import BudgetExceeded


def _ceiling(*_a, **_k):
    raise BudgetExceeded("LLM spend ceiling reached")


def _broken(*_a, **_k):
    raise RuntimeError("an ordinary failure")


# ------------------------------------------------------------------ core/context_budget.py

def _long_history():
    turns = [{"role": "system", "content": "the task"}]
    for i in range(6):
        turns += [{"role": "user", "content": f"u{i} " + "x" * 400},
                  {"role": "assistant", "content": f"a{i} " + "y" * 400}]
    return turns


def test_compact_history_lets_the_summarizers_ceiling_through():
    """The summarizer is a paid call (`agents/tool_loop.py::_summarizer`); its ceiling degraded to
    deterministic truncation and the tool loop went on to its next paid turn."""
    from looplab.core.context_budget import compact_history

    with pytest.raises(BudgetExceeded):
        compact_history(_long_history(), 1_500, _ceiling)
    out = compact_history(_long_history(), 1_500, _broken)
    assert out and out[0]["content"] == "the task"            # still truncates, still keeps the task


# ------------------------------------------------------------------ tools/knowledge_tools.py

class _Switch:
    """An embedder and an abstractor that work while the index is BUILT (the constructor builds it)
    and raise `fail` once armed — so the failure lands where the tool call does, at query time."""

    def __init__(self):
        self.fail = None

    def embed(self, text):
        from looplab.tools.vectorstore import hash_embed
        if self.fail is not None:
            raise self.fail
        return hash_embed(text)

    def abstract(self, text):
        from looplab.tools.memora import lexical_abstraction
        if self.fail is not None:
            raise self.fail
        return lexical_abstraction(text)


@pytest.mark.parametrize("seam", ["embed", "abstract"])
def test_kb_search_lets_the_ceiling_through_instead_of_reporting_a_tool_error(tmp_path, seam):
    """TAT-01's driven case: `kb_search` answered the model `(tool error: LLM spend ceiling
    reached)` and the agent loop spent its next turn on the answer."""
    from looplab.tools.knowledge_tools import KnowledgeTools

    kdir = tmp_path / "kb"
    kdir.mkdir()
    (kdir / "warmup.md").write_text("linear warmup stabilises the first epoch", encoding="utf-8")
    for fail, stops in ((BudgetExceeded("LLM spend ceiling reached"), True),
                        (RuntimeError("an ordinary failure"), False)):
        switch = _Switch()
        tools = KnowledgeTools(knowledge_dir=str(kdir), **{seam: getattr(switch, seam)})
        switch.fail = fail
        if stops:
            with pytest.raises(BudgetExceeded):
                tools.execute("kb_search", {"query": "warmup"})
        else:                                        # an ordinary failure is still fed back
            answer = tools.execute("kb_search", {"query": "warmup"})
            assert answer.startswith("(tool error:"), answer


# ------------------------------------------------------------------ search/

class _Client:
    """An LLM client every structured path reaches (`complete_tool` for `parse_structured`, the text
    and chat paths for the rest), raising `exc` on each call and counting them."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def complete_tool(self, messages, schema):
        self.calls += 1
        raise self.exc

    def complete_text(self, messages, **_kw):
        self.calls += 1
        raise self.exc

    def chat(self, messages, tools=None, **_kw):
        self.calls += 1
        raise self.exc


def _experiment_state(n: int):
    from looplab.core.models import Idea, Node, NodeStatus, RunState

    idea = Idea(operator="draft", params={}, rationale="cosine warmup with label smoothing 0.1")
    return RunState(nodes={i: Node(id=i, operator="draft", idea=idea, metric=0.5,
                                   status=NodeStatus.evaluated) for i in range(n)})


def _search_sites():
    """`(id, call(client) -> result, the fallback an ORDINARY failure must still return)`."""
    from types import SimpleNamespace

    from looplab.search import best_of_n, concept_map, foresight, hybrid_merge

    return [
        ("foresight.rank", lambda c: foresight.rank(c, "report", ["a", "b"]), None),
        ("best_of_n._listwise_pick",
         lambda c: best_of_n._listwise_pick(c, SimpleNamespace(rationale="r"), ["c0", "c1"]), 0),
        ("hybrid_merge.agent_merge",
         lambda c: hybrid_merge.agent_merge(c, ["alpha", "beta"], kind="lessons"),
         [{"members": [0], "merged": "alpha"}, {"members": [1], "merged": "beta"}]),
        ("concept_map.derive_reference_concepts",
         lambda c: concept_map.derive_reference_concepts(
             "rank passages", {"concept_touch": {"loss/infonce": 3}}, client=c), []),
    ]


@pytest.mark.parametrize("site", range(4), ids=lambda i: ["rank", "listwise", "merge", "reference"][i])
def test_search_wrappers_let_the_ceiling_through_and_still_degrade_otherwise(site):
    """SCJ-03's search sites: each ADVISORY wrapper fell back to its safe value on the ceiling too —
    `foresight.rank` returned None, the best-of-N pick returned candidate 0, the merge returned
    singletons, the blind-spot audit returned [] — and the caller went on to its next paid call."""
    name, call, fallback = _search_sites()[site]
    ceiling = _Client(BudgetExceeded("LLM spend ceiling reached"))
    with pytest.raises(BudgetExceeded):
        call(ceiling)
    assert ceiling.calls == 1, (name, ceiling.calls)
    assert call(_Client(RuntimeError("endpoint 500"))) == fallback, name


def test_the_paraphrase_audit_stops_at_the_first_ceiling_instead_of_once_per_pair():
    """The one loop among these sites: `paraphrase_leaks` adjudicates PER PAIR, so a swallowed
    ceiling was re-paid once per candidate pair (up to `max_pairs`, 60)."""
    from looplab.search.novelty_recall import paraphrase_leaks

    ceiling = _Client(BudgetExceeded("LLM spend ceiling reached"))
    with pytest.raises(BudgetExceeded):
        paraphrase_leaks(_experiment_state(3), client=ceiling)
    assert ceiling.calls == 1, ceiling.calls
    ordinary = _Client(RuntimeError("endpoint 500"))
    out = paraphrase_leaks(_experiment_state(3), client=ordinary)
    assert ordinary.calls == 3 and out["leaks"] == []           # every pair still tried, none judged


def test_the_foresight_verifier_confidence_lets_the_ceiling_through(monkeypatch):
    """`trust/verifier.py::verify` re-raises the ceiling (doc 50 AG-01); its caller here swallowed it
    again and degraded to the self-reported confidence."""
    from types import SimpleNamespace

    from looplab.core.models import Idea
    from looplab.search.foresight import ForesightPanelResearcher
    from looplab.trust import verifier

    panel = ForesightPanelResearcher.__new__(ForesightPanelResearcher)
    panel.client, panel.verify_samples, panel.parser = object(), 1, "tool_call"
    idea = Idea(operator="draft", params={}, rationale="r")
    state = SimpleNamespace(direction="min")
    monkeypatch.setattr(verifier, "verify", _ceiling)
    with pytest.raises(BudgetExceeded):
        panel._verifier_confidence(state, idea, "report")
    monkeypatch.setattr(verifier, "verify", _broken)
    assert panel._verifier_confidence(state, idea, "report") is None


# ------------------------------------------------------------------ engine/

_STOPS = pytest.mark.parametrize("exc, stops", [
    (BudgetExceeded("LLM spend ceiling reached"), True),
    (RuntimeError("endpoint 500"), False),
], ids=["ceiling", "ordinary"])


@_STOPS
def test_genesis_reports_the_ceiling_as_a_stop_not_as_an_unreachable_model(exc, stops):
    """The CLI turned `GenesisResult.error` into "Genesis couldn't reach the model ... check
    LOOPLAB_LLM_BASE_URL" — for the operator's own spend ceiling."""
    from looplab.engine.genesis import author_task

    client = _Client(exc)
    if stops:
        with pytest.raises(BudgetExceeded):
            author_task("minimize (x-3)^2", client=client, kinds=("quadratic",))
    else:
        assert author_task("minimize (x-3)^2", client=client, kinds=("quadratic",)).error
    assert client.calls >= 1


@_STOPS
def test_a_finalize_steward_closes_its_paid_claim_before_the_ceiling_goes_on(tmp_path, exc,
                                                                                stops):
    """The paid-work ledger is claim -> terminal, and a claim left without its terminal reads as
    "provider outcome unknown" forever. So the ceiling is let through only AFTER the same durable
    `error` terminal any failure writes — never instead of it."""
    import json

    from looplab.core.models import RunState
    from looplab.engine.governance_health import curation_ledger_file
    from looplab.engine.lessons import LessonMemory

    from test_steward_semantic_identity import _engine, _seed_claim

    _seed_claim(tmp_path)
    client = _Client(exc)
    memory = LessonMemory(_engine(tmp_path, client))
    final = RunState(run_id="r", task_id="task")
    if stops:
        with pytest.raises(BudgetExceeded):
            memory.store_claim_curation(final)
    else:
        assert memory.store_claim_curation(final) == "error"
    ledger = tmp_path / curation_ledger_file("claim")
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    assert [r["outcome"] for r in rows] == ["error"], rows
    assert rows[0]["error_type"] == type(exc).__name__
    assert client.calls == 1


def _stale_comparative(mem):
    from test_lessons_reconcile import _seed

    _seed(mem, [{"task_id": "toy_quadratic", "run_id": "run_me", "source": "comparative",
                 "statement": "OLD STALE moving x helped by 5", "outcome": "supported",
                 "evidence": [1, 0], "delta": 5.0,
                 "evidence_sig": {"1": "evaluated:4.0", "0": "evaluated:9.0"},
                 "fingerprint": [], "kind": "quadratic"}])


def test_reconcile_retires_the_stale_lesson_before_a_rederivation_ceiling_goes_on(tmp_path,
                                                                                 monkeypatch):
    """"Re-derivation is optional; retirement is not" (`reconcile_lessons`). The paid re-derivation
    hitting the ceiling must not leave a stale lesson steering future runs — the stop is HELD until
    the locked retirement and its audit row landed. MUTATION: re-raise at the re-derivation -> the
    stale row survives."""
    from test_lessons_reconcile import FakeClient, _engine, _node, _rows, _state

    mem = tmp_path / "mem"
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(mem), comparative_lessons=True)
    _stale_comparative(mem)
    state = _state([_node(0, metric=9.0), _node(1, metric=6.0, parent_ids=[0])])
    monkeypatch.setattr(eng, "_reflect_client", lambda: FakeClient("unused"))
    monkeypatch.setattr(eng, "_comparative_lessons", _ceiling)
    with pytest.raises(BudgetExceeded):
        eng.lessons.reconcile_lessons(state)
    assert _rows(mem) == [], "the stale lesson must be retired even though the stop ended the pass"
    event = [e for e in eng.store.read_all() if e.type == "lessons_reconciled"][-1]
    assert event.data["derivation"] == "failed" and event.data["n_retired"] == 1


def test_reconcile_records_its_spend_before_a_consolidation_ceiling_goes_on(tmp_path, monkeypatch):
    """The post-write consolidation is paid; the pairs it follows are already committed, and
    without the `lessons_distilled(reconcile)` receipt run-end reflection re-buys them. So the ceiling
    there is held until that receipt lands. MUTATION: re-raise at the consolidation -> no receipt."""
    from looplab.events.replay import fold

    from test_lessons_reconcile import FakeClient, _engine, _node, _rows, _state

    mem = tmp_path / "mem"
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(mem), comparative_lessons=True)
    _stale_comparative(mem)
    st = _state([_node(0, metric=9.0, op="draft", params={"x": 1.0}, code="x=1\n"),
                 _node(1, metric=6.0, parent_ids=[0], params={"x": 3.0}, code="x=3\n")])
    monkeypatch.setattr(eng, "_reflect_client",
                        lambda: FakeClient("P1 [BAD] this change regressed the metric\n"))
    monkeypatch.setattr(eng, "_consolidate_lessons_file", _ceiling)
    with pytest.raises(BudgetExceeded):
        eng.lessons.reconcile_lessons(st)
    fresh = [r for r in _rows(mem) if r.get("source") == "comparative"]
    assert len(fresh) == 1 and "regressed" in fresh[0]["statement"]
    assert [d for d in fold(eng.store.read_all()).lessons_distilled
            if d.get("trigger") == "reconcile"], "the spend receipt must land before the stop"


@_STOPS
def test_lesson_hygiene_lets_the_paid_merge_ceiling_through_and_keeps_the_store(tmp_path, exc,
                                                                              stops):
    """`consolidate_lessons_file` -> `_agentic_merge_lessons` -> `hybrid_merge.consolidate` ->
    `agent_merge`: three layers, each of which returned its input on the ceiling. The store is
    rewritten only after a merge SUCCEEDS, so letting the stop through loses nothing."""
    import json

    from looplab.engine.lessons import LessonMemory

    path = tmp_path / "lessons.jsonl"
    rows = [{"statement": s, "outcome": "supported", "evidence": [1], "run_id": "seed",
             "task_id": "task"}
            for s in ("linear warmup stabilises the first epoch of training",
                      "linear warm-up stabilizes the first epoch of training")]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    before = path.read_bytes()
    client = _Client(exc)
    if stops:
        with pytest.raises(BudgetExceeded):
            LessonMemory.consolidate_lessons_file(path, client, None)
    else:
        LessonMemory.consolidate_lessons_file(path, client, None)
    assert client.calls == 1, "the paraphrase pair must reach the paid adjudication"
    assert path.read_bytes() == before


def test_a_ceiling_in_one_finalize_steward_buys_no_call_from_the_next(tmp_path):
    """The finalize steward loop contained a `BudgetExceeded` like any steward failure and moved
    on, so each remaining steward bought one more paid call against a ceiling already reached (the
    gap the transitive census's write-up named; a name graph cannot see `steward(final)`). The loop
    now records the same `error` receipt and STOPS — and finalization still completes, because the
    steps after it write records and call no model."""
    import anyio

    from factories import make_engine
    from looplab.events.replay import fold

    engine = make_engine(tmp_path / "run", n_seeds=2, max_nodes=2)
    engine._cross_run_curation = True
    engine._task_facets_finalize = True
    later: list[str] = []

    def over_ceiling(_final):
        raise BudgetExceeded("run spend ceiling reached")

    engine._store_concept_curation = over_ceiling
    engine._store_claim_curation = lambda _final: later.append("claim_curation") or "completed"
    engine._store_task_facets = lambda _final: later.append("task_facets") or "completed"
    state = anyio.run(engine.run)
    assert later == [], f"stewards after the ceiling still ran: {later}"
    assert state.finished and fold(engine.store.read_all()).finished
    steps = [e.data for e in engine.store.read_all() if e.type == "finalize_step"
             and e.data.get("step") == "concept_curation"]
    assert steps and steps[-1].get("outcome") == "error", steps
