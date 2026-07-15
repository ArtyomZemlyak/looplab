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


# --------------------------------------------------------------------------- #
# Full CR2a: intent classification + contradiction quota + source scoping + why-recalled receipt
# --------------------------------------------------------------------------- #

def test_intent_classified_in_receipt():
    from looplab.engine.claims import _classify_intent
    assert _classify_intent("what pitfalls should I avoid") == "failed"
    assert _classify_intent("which contested tricks disagree") == "contested"
    assert _classify_intent("the best proven effective approach") == "worked"
    # ML technique words must NOT trip 'failed' — this is neutral EXPLORE
    assert _classify_intent("hard negatives for retrieval") == "explore"


def test_contradiction_quota_surfaces_a_caveat_even_amid_positives(tmp_path):
    # 6 supported claims about retrieval + one CONTESTED claim; a positive-heavy recall must still surface
    # the contested one (the reserved contradiction quota), not bury it under the positives.
    lessons = [_lesson(f"retrieval trick {i} improves recall", run_id=f"r{i}") for i in range(6)]
    lessons += [_lesson("retrieval reranking improves recall", "supported", (1,), "rA"),
                _lesson("retrieval reranking improves recall", "tested", (2,), "rB")]   # -> mixed/contested
    _seed(tmp_path, lessons=lessons)
    r = cross_run_retrieve(str(tmp_path), "retrieval recall trick", k=4)
    assert r["receipt"]["n_caveats"] >= 1                       # a contested claim reserved a slot
    assert any(h.get("epistemic") == "mixed" for h in r["results"])


def test_failed_intent_prioritizes_caveats(tmp_path):
    lessons = [_lesson("augmentation improves recall", "supported", (1,), "r1"),
               _lesson("dropout tuning improves recall", "supported", (2,), "r2"),
               _lesson("aggressive augmentation improves recall", "supported", (3,), "rA"),
               _lesson("aggressive augmentation improves recall", "failed", (4,), "rB")]   # contested
    _seed(tmp_path, lessons=lessons)
    r = cross_run_retrieve(str(tmp_path), "what augmentation pitfalls to avoid", k=3)
    # failed intent RAISES the effective quota (0.5) above the configured base (0.34) — receipt shows both
    assert r["receipt"]["intent"] == "failed" and r["receipt"]["contradiction_quota"] == 0.34
    assert r["receipt"]["effective_quota"] == 0.5 and r["receipt"]["caveat_target"] >= 1
    assert any(h.get("epistemic") == "mixed" for h in r["results"])   # the caveat is surfaced


def test_scope_task_isolates_d8_claims(tmp_path):
    from looplab.engine.claims import record_research_claims
    _seed(tmp_path, lessons=[_lesson("shared topic marker one")])
    # a D8 claim in ANOTHER task must not surface for a scoped query
    record_research_claims(str(tmp_path), run_id="rX", task_id="otherTask",
                           claims=[{"statement": "shared topic marker secret", "node_ids": [9]}])
    scoped = cross_run_retrieve(str(tmp_path), "shared topic marker", k=8, scope_task="t")
    assert all("secret" not in h["text"] for h in scoped["results"])   # other-task D8 excluded
    wide = cross_run_retrieve(str(tmp_path), "shared topic marker", k=8)   # portfolio-wide sees it
    assert any("secret" in h["text"] for h in wide["results"])


def test_explicit_intent_overrides_the_classifier(tmp_path):
    # the AGENT passes intent explicitly — a neutral query text but intent='failed' still raises the quota
    _seed(tmp_path, lessons=[_lesson(f"topic claim {i}", run_id=f"r{i}") for i in range(4)])
    r = cross_run_retrieve(str(tmp_path), "topic claim", k=3, intent="failed")
    assert r["receipt"]["intent"] == "failed" and r["receipt"]["effective_quota"] == 0.5
    # an unknown intent value falls back to deterministic classification
    r2 = cross_run_retrieve(str(tmp_path), "topic claim", k=3, intent="nonsense")
    assert r2["receipt"]["intent"] == "explore"


def test_receipt_is_a_why_recalled_receipt(tmp_path):
    _seed(tmp_path, lessons=[_lesson("hard negative mining improves recall")])
    rc = cross_run_retrieve(str(tmp_path), "hard negatives", k=3)["receipt"]
    assert rc["intent"] == "explore" and "corpus_digest" in rc and rc["truncated"] == 0
    assert "hash_embed" in rc["vector_channel"]                 # degraded channel declared, not hidden
    assert rc["k"] == 3 and "n_caveats" in rc


def test_corpus_cap_is_reported_not_silent(tmp_path):
    _seed(tmp_path, lessons=[_lesson(f"claim number {i} about topic", run_id=f"r{i}") for i in range(10)])
    rc = cross_run_retrieve(str(tmp_path), "topic", k=3, max_corpus=4)["receipt"]
    assert rc["n_corpus"] == 10 and rc["truncated"] == 6        # full size known, drop count reported


def test_tool_and_cli(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app
    from looplab.tools.cross_run_tools import CrossRunTools
    _seed(tmp_path, lessons=[_lesson("hard negative mining improves recall")])
    out = CrossRunTools(tmp_path).execute("cross_run_search", {"query": "hard negatives"})
    assert "hard negative" in out.lower() and "claim" in out
    res = CliRunner().invoke(app, ["cross-run-search", str(tmp_path), "hard negatives"])
    assert res.exit_code == 0 and "cross-run search" in res.stdout and "hard negative" in res.stdout.lower()
