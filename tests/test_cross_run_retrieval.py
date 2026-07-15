"""PART IV cross-run CR2a (§21.20.5) — cross_run_retrieve: relevance-ranked hybrid search + receipt.

RRF-fuses claims + concepts over the shipped HybridRetriever (lexical+BM25+vector — reuses hybrid_merge,
no new fuser). Pins: relevance ranking finds the on-topic doc, the receipt is well-formed, operator-rejected
claims are excluded, empty query/corpus is safe, and the CrossRunTools `cross_run_search` tool + CLI.
"""
from __future__ import annotations

import orjson

from looplab.engine.claims import cross_run_retrieve, record_claim_decision
from looplab.engine.memory import build_concept_capsule


def _seed(d, *, lessons=None, capsules=None):
    if lessons is not None:
        (d / "lessons.jsonl").write_bytes(b"\n".join(orjson.dumps(x) for x in lessons) + b"\n")
    if capsules is not None:
        from looplab.engine.memory import ConceptCapsuleStore
        s = ConceptCapsuleStore(d / "concept_capsules.jsonl")
        for c in capsules:
            s.add(c)


def _lesson(statement, outcome="supported", evidence=(1,), run_id="r1"):
    return {"statement": statement, "outcome": outcome, "evidence": list(evidence),
            "run_id": run_id, "task_id": "t"}


def test_ranks_relevant_claim_first(tmp_path):
    _seed(tmp_path, lessons=[
        _lesson("hard negative mining improves recall"),
        _lesson("learning rate warmup stabilizes training"),
        _lesson("weight decay prevents overfitting"),
    ])
    r = cross_run_retrieve(str(tmp_path), "hard negatives for retrieval", k=3)
    assert r["results"], "expected hits"
    assert "hard negative" in r["results"][0]["text"].lower()   # the on-topic claim ranks first
    assert r["receipt"]["n_corpus"] == 3 and r["receipt"]["channels"] == ["lexical", "bm25", "vector"]


def test_includes_concepts_from_capsules(tmp_path):
    _seed(tmp_path, capsules=[build_concept_capsule(
        run_id="r1", fingerprint=["k"], direction="max", concepts=["distillation"], concept_outcomes={})])
    r = cross_run_retrieve(str(tmp_path), "distillation", k=5)
    assert any(h["kind"] == "concept" and "distillation" in h["text"] for h in r["results"])


def test_operator_rejected_claim_is_excluded(tmp_path):
    _seed(tmp_path, lessons=[_lesson("dubious trick helps")])
    record_claim_decision(str(tmp_path), statement="dubious trick helps", decision="rejected")
    r = cross_run_retrieve(str(tmp_path), "dubious trick", k=5)
    assert all("dubious trick" not in h["text"] for h in r["results"])   # rejected -> never in the corpus


def test_empty_query_and_corpus_are_safe(tmp_path):
    assert cross_run_retrieve(str(tmp_path), "anything")["results"] == []       # empty corpus
    _seed(tmp_path, lessons=[_lesson("x")])
    assert cross_run_retrieve(str(tmp_path), "  ")["results"] == []             # blank query
    assert cross_run_retrieve(str(tmp_path), "  ")["receipt"]["n_hits"] == 0


def test_tool_and_cli(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app
    from looplab.tools.cross_run_tools import CrossRunTools
    _seed(tmp_path, lessons=[_lesson("hard negative mining improves recall")])
    out = CrossRunTools(tmp_path).execute("cross_run_search", {"query": "hard negatives"})
    assert "hard negative" in out.lower() and "claim" in out
    res = CliRunner().invoke(app, ["cross-run-search", str(tmp_path), "hard negatives"])
    assert res.exit_code == 0 and "cross-run search" in res.stdout and "hard negative" in res.stdout.lower()
