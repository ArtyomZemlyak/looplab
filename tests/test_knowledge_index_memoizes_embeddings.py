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
