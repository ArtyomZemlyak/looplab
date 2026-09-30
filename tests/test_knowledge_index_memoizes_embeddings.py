"""A rebuild of the knowledge index must re-embed only what MOVED.

A rebuild fires whenever `_source_revision` changes — every append to the case store — and on every
scope rebind, and it used to re-embed every note and every case from scratch. With a real
`LLMEmbedder` that is a paid provider call per record per write; the spend is visible in `llm_usage`
and it was entirely avoidable, because an embedding is a pure function of (model, text).

Driven with an ACCOUNTANT over the embedder: the property is HOW MANY provider calls a rebuild
makes, and nothing else can distinguish a memo that works from one that is merely present. The
embedder is a stub, which is also what makes this offline — no endpoint is contacted, and the stub
is deterministic so the vectors can be compared against a from-scratch build.
"""
from __future__ import annotations

import json

from looplab.tools.knowledge_tools import KnowledgeTools
from looplab.tools.vectorstore import hash_embed


class _CountingEmbedder:
    """`hash_embed`, plus a count of the calls a real endpoint would have been billed for."""

    def __init__(self):
        self.calls = 0
        self.texts: list[str] = []

    def __call__(self, text):
        self.calls += 1
        self.texts.append(str(text))
        return hash_embed(str(text))


def _notes(tmp_path, count: int):
    d = tmp_path / "kb"
    d.mkdir()
    for i in range(count):
        (d / f"note-{i}.md").write_text(f"# note {i}\nsome operator knowledge number {i}\n",
                                        encoding="utf-8")
    return d


def _case(i: int) -> dict:
    return {"task_id": "t", "direction": "min", "metric": 0.1 * i, "goal": f"goal {i}",
            "params": {"lr": 0.001 * (i + 1)}, "rationale": f"because {i}", "run_id": f"r{i}",
            "active": True}


def _cases(tmp_path, count: int):
    path = tmp_path / "cases.jsonl"
    with open(path, "w", encoding="utf-8") as handle:
        for i in range(count):
            handle.write(json.dumps(_case(i)) + "\n")
    return path


def _append_case(path, i: int):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(_case(i)) + "\n")


def test_an_append_to_the_case_store_embeds_ONLY_the_new_record(tmp_path):
    """THE DEFECT, driven. MUTATION: drop the memo -> the rebuild costs 9 calls again, on every
    single write to the store."""
    notes, cases = _notes(tmp_path, 4), _cases(tmp_path, 5)
    embed = _CountingEmbedder()

    tools = KnowledgeTools(str(notes), str(cases), embed=embed)
    cold = embed.calls
    assert cold == 9, f"premise: 4 notes + 5 cases are embedded once on the cold build ({cold})"

    _append_case(cases, 99)
    tools.execute("kb_search", {"query": "goal"})       # a revision change -> a rebuild

    # 1 for the new case + 1 for the query `kb_search` embeds (never memoized: queries are unbounded)
    assert embed.calls == cold + 2, (
        f"the rebuild re-embedded {embed.calls - cold - 1} record(s) whose text had not moved")


def test_the_memoized_index_is_the_SAME_index_a_cold_build_produces(tmp_path):
    """Correctness first: a cache that can serve a different answer is worse than the cost."""
    notes, cases = _notes(tmp_path, 3), _cases(tmp_path, 3)
    warm = KnowledgeTools(str(notes), str(cases), embed=_CountingEmbedder())
    _append_case(cases, 42)
    warm._build_index()                                 # rebuild, served largely from the memo

    cold = KnowledgeTools(str(notes), str(cases), embed=_CountingEmbedder())

    def _vectors(tools):
        return {it.id: list(it.vector) for it in tools._index._idx["kb"].values()}

    assert _vectors(warm) == _vectors(cold), "a memoized vector differs from a freshly embedded one"


def test_the_memo_keeps_only_what_THIS_build_used(tmp_path):
    """It is a memo, not a cache with a policy: nothing sizes it, because it holds exactly the
    records the index was just built from. A note that leaves the store stops being held."""
    notes, cases = _notes(tmp_path, 3), _cases(tmp_path, 0)
    tools = KnowledgeTools(str(notes), str(cases), embed=_CountingEmbedder())
    assert len(tools._vector_memo) == 3

    (notes / "note-2.md").unlink()
    tools._build_index()
    assert len(tools._vector_memo) == 2, (
        "the memo grew past the corpus — it would then need a bound, and a bound needs a number")


def test_a_REBOUND_embedder_invalidates_the_memo_wholesale(tmp_path):
    """A vector is a function of (model, text). Serving one model's vectors into another model's
    index mixes two embedding spaces, and `cosine` cannot tell — it only refuses a dim mismatch."""
    notes, cases = _notes(tmp_path, 2), _cases(tmp_path, 2)
    tools = KnowledgeTools(str(notes), str(cases), embed=_CountingEmbedder())

    second = _CountingEmbedder()
    tools.embed = second                                # a re-wired provider / a different model
    tools._build_index()

    assert second.calls == 4, (
        f"the new embedder was asked for {second.calls} of 4 records — the rest came from the "
        "previous model's memo")


def test_the_harmonic_build_is_memoized_too(tmp_path):
    """The consolidating path embeds the ABSTRACTION (and re-embeds a merged one), which is where
    an LLM abstractor's index actually spends. It must not be the one path that keeps paying."""
    from looplab.tools.memora import Abstraction

    notes, cases = _notes(tmp_path, 4), _cases(tmp_path, 2)
    embed = _CountingEmbedder()
    abstract = lambda text: Abstraction(str(text)[:40].strip(), [])   # noqa: E731 - a stub, inline

    tools = KnowledgeTools(str(notes), str(cases), embed=embed, abstract=abstract)
    cold = embed.calls
    assert cold >= 6, f"premise: every record is embedded on the cold build ({cold})"

    tools._build_index()                                # nothing moved
    assert embed.calls == cold, (
        f"the harmonic rebuild re-embedded {embed.calls - cold} unchanged record(s)")


# ------------------------------------------------ per-call views share one index (crit_v53 N2)
def _counting_builds(monkeypatch):
    builds = []
    real = KnowledgeTools._build_index

    def counting(self):
        builds.append(self._scope_key(self._scope))
        return real(self)

    monkeypatch.setattr(KnowledgeTools, "_build_index", counting)
    return builds


def test_every_per_call_view_adopts_the_index_its_scope_already_owes(tmp_path, monkeypatch):
    """crit_v53 N2: a view is a shallow copy of a provider that is itself never bound, so every view
    saw "unbound -> bound" and rebuilt, re-embedding what the construction-time memo lacked — driven,
    5 proposals made 6 builds and 7 embeddings where the shared binding had made 2 and 3. MUTATION:
    `_adopt_or_build` always builds -> one build (and one paid embedding) per view."""
    from looplab.agents.tool_loop import bound_toolset
    from looplab.core.models import RunState

    builds = _counting_builds(monkeypatch)
    notes, embed = _notes(tmp_path, 2), _CountingEmbedder()
    tools = KnowledgeTools(str(notes), embed=embed)
    (notes / "late.md").write_text("# late\nwritten after the provider was built\n", encoding="utf-8")
    state = RunState(goal="g", direction="min", run_id="r1")
    views = [bound_toolset(tools, state) for _ in range(5)]
    assert len(builds) == 2 and embed.calls == 3, (builds, embed.texts)
    assert len({id(v._index) for v in views}) == 1 and views[0]._index is not tools._index

    # A search after the sources moved rebuilds ONCE; the next view's search adopts it.
    (notes / "later.md").write_text("# later\nanother note\n", encoding="utf-8")
    first = views[0].execute("kb_search", {"query": "note"})
    adopted = views[1].execute("kb_search", {"query": "note"})
    assert len(builds) == 3 and embed.calls == 3 + 1 + 2, embed.texts   # 1 record + 2 queries
    # …and the adopting view reports the revision it now searches, not the one it was bound at.
    assert first.splitlines()[0] == adopted.splitlines()[0]
    assert views[2]._index is not views[0]._index            # bound before: keeps its own until asked
    assert bound_toolset(tools, state)._index is views[0]._index
    # A view still holding the OLD memo that owes a build starts from the freshest one: only the
    # newest note is embedded, not `later.md` again. MUTATION: build from the view's own memo.
    (notes / "latest.md").write_text("# latest\none more\n", encoding="utf-8")
    views[2].execute("kb_search", {"query": "note"})
    assert len(builds) == 4 and embed.calls == 6 + 1 + 1, embed.texts


def test_a_view_of_another_run_builds_its_own_scope_and_leaves_the_others_alone(tmp_path,
                                                                                monkeypatch):
    """Adoption is keyed by the SCOPE (`LessonScope`): a view of another run never reads this run's
    cases, and binding it does not move a view already bound."""
    from looplab.agents.tool_loop import bound_toolset
    from looplab.core.models import RunState

    builds = _counting_builds(monkeypatch)
    tools = KnowledgeTools(str(_notes(tmp_path, 1)), embed=_CountingEmbedder())
    one = bound_toolset(tools, RunState(goal="g", direction="min", run_id="r1"))
    two = bound_toolset(tools, RunState(goal="other goal", direction="max", run_id="r2"))
    assert len(builds) == 3 and one._index is not two._index
    assert one._scope.run_id == "r1" and two._scope.run_id == "r2"
    again = bound_toolset(tools, RunState(goal="g", direction="min", run_id="r1"))
    assert len(builds) == 4 and again._index is not one._index   # the slot holds the LAST scope


def test_proposals_through_the_researcher_build_the_index_once(tmp_path, monkeypatch):
    """The critic's scenario through `ToolUsingResearcher.propose`, which binds a view per call."""
    from test_foresight_alternatives import _Model, _emit, _turn

    from looplab.agents.agent import ToolUsingResearcher
    from looplab.core.models import RunState

    builds = _counting_builds(monkeypatch)
    notes, embed = _notes(tmp_path, 2), _CountingEmbedder()
    tools = KnowledgeTools(str(notes), embed=embed)
    (notes / "late.md").write_text("# late\nwritten mid-run\n", encoding="utf-8")
    researcher = ToolUsingResearcher(_Model(lambda msgs: _turn(_emit("e", "idea"))), tools)
    state = RunState(goal="g", direction="min", run_id="r1")
    for _ in range(4):
        researcher.propose(state, None)
    assert (len(builds), embed.calls) == (2, 3), (builds, embed.texts)


def test_a_view_never_adopts_an_index_another_embedder_built(tmp_path):
    """The shared index is keyed by the EMBEDDER as well as the scope: a re-wired provider's views
    must not search one model's vectors with another model's query. MUTATION: drop the embedder
    clause from the adoption -> the second model embeds nothing and the index mixes two spaces."""
    from looplab.agents.tool_loop import bound_toolset
    from looplab.core.models import RunState

    tools = KnowledgeTools(str(_notes(tmp_path, 3)), embed=_CountingEmbedder())
    state = RunState(goal="g", direction="min", run_id="r1")
    bound_toolset(tools, state)
    second = _CountingEmbedder()
    tools.embed = second
    view = bound_toolset(tools, state)
    assert second.calls == 3 and view._vector_memo_embedder is second
