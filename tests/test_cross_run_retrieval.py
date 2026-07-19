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


def _lesson(statement, outcome="supported", evidence=(1,), run_id="r1", task_id="t", **extra):
    return {"statement": statement, "outcome": outcome, "evidence": list(evidence),
            "run_id": run_id, "task_id": task_id, **extra}


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


def test_query_preselection_sees_concepts_beyond_public_overview_cap():
    popular = [f"popular/c{index:03d}" for index in range(512)]
    capsules = [
        build_concept_capsule(
            run_id="popular-a", fingerprint=["k"], direction="max",
            concepts=popular[:256], concept_outcomes={}),
        build_concept_capsule(
            run_id="popular-b", fingerprint=["k"], direction="max",
            concepts=popular[256:], concept_outcomes={}),
        build_concept_capsule(
            run_id="sentinel", fingerprint=["k"], direction="max",
            concepts=["zz/sentinel"], concept_outcomes={}),
    ]

    out = cross_run_retrieve(
        "", "zz sentinel", capsules=capsules, lessons=[], k=1, max_corpus=3)

    assert out["results"][0]["text"] == "zz/sentinel"
    receipt = out["receipt"]
    assert receipt["source_complete"] is True
    assert receipt["n_corpus"] == receipt["concepts_total"] == 513
    assert receipt["overview_concepts_omitted"] == 1
    assert (receipt["concepts_indexed"], receipt["concepts_omitted"]) == (3, 510)
    assert receipt["truncated"] == 510
    full = cross_run_retrieve(
        "", "zz sentinel", capsules=capsules, lessons=[], k=1, max_corpus=513)
    assert full["receipt"]["corpus_digest"] == receipt["corpus_digest"]
    assert full["receipt"]["retrieval_digest"] != receipt["retrieval_digest"]
    assert (full["receipt"]["concepts_indexed"],
            full["receipt"]["concepts_omitted"]) == (513, 0)


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


def test_scope_task_isolates_every_joined_source(tmp_path):
    from looplab.engine.claims import record_research_claims
    _seed(tmp_path,
          lessons=[_lesson("shared topic marker visible", task_id="t"),
                   _lesson("shared topic marker secret lesson", task_id="otherTask")],
          capsules=[
              build_concept_capsule(run_id="c1", task_id="t", fingerprint=["a"], direction="max",
                                    concepts=["visible-concept"], concept_outcomes={}),
              build_concept_capsule(run_id="c2", task_id="otherTask", fingerprint=["b"], direction="max",
                                    concepts=["secret-concept"], concept_outcomes={}),
          ])
    record_research_claims(str(tmp_path), run_id="rX", task_id="otherTask",
                           claims=[{"statement": "shared topic marker secret", "node_ids": [9]}])
    scoped = cross_run_retrieve(str(tmp_path), "shared topic marker secret concept", k=20, scope_task="t")
    assert all("secret" not in h["text"] for h in scoped["results"])
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


def test_k1_quota_never_evicts_the_top_relevance_hit(tmp_path):
    # mega-review regression: at k=1 the contradiction quota must NOT displace the single top hit.
    _seed(tmp_path, lessons=[
        _lesson("hard negative mining improves recall", "supported", (1,), "r1"),
        _lesson("unrelated warmup claim", "supported", (2,), "rA"),
        _lesson("unrelated warmup claim", "tested", (3,), "rB")])   # a contested caveat exists
    r = cross_run_retrieve(str(tmp_path), "hard negatives for retrieval", k=1, intent="failed")
    assert r["receipt"]["caveat_target"] == 0                        # nothing reserved at k=1
    assert "hard negative" in r["results"][0]["text"].lower()        # top relevance hit preserved


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


def test_corpus_cap_preselection_is_query_aware_not_file_prefix(tmp_path):
    lessons = [_lesson(f"common filler claim {i}", evidence=(1, 2), run_id=f"r{i}") for i in range(20)]
    lessons.append(_lesson("zirconium sentinel uniquely improves recall", evidence=(1,), run_id="tail"))
    _seed(tmp_path, lessons=lessons)
    out = cross_run_retrieve(str(tmp_path), "zirconium sentinel", k=2, max_corpus=3)
    assert out["receipt"]["n_corpus"] == 21 and out["receipt"]["n_indexed"] == 3
    assert out["receipt"]["truncated"] == 18
    assert out["results"] and "zirconium sentinel" in out["results"][0]["text"]


def test_receipt_digest_tracks_metadata_but_document_id_stays_stable():
    first = cross_run_retrieve("", "stable claim", k=2, capsules=[], lessons=[
        _lesson("stable claim improves recall", evidence=(1,), run_id="r1")])
    second = cross_run_retrieve("", "stable claim", k=2, capsules=[], lessons=[
        _lesson("stable claim improves recall", evidence=(9,), run_id="r2")])
    assert first["receipt"]["corpus_digest_version"] == 6
    assert first["receipt"]["corpus_digest"] != second["receipt"]["corpus_digest"]
    assert first["results"][0]["stable_id"] == second["results"][0]["stable_id"]


def test_receipt_digest_commits_to_aggregate_capsule_completeness():
    complete = build_concept_capsule(
        run_id="r1", fingerprint=["k"], direction="max",
        concepts=["distillation"], concept_outcomes={})
    legacy = dict(complete)
    for stem in ("concepts", "concept_outcomes"):
        for suffix in ("total", "omitted", "complete"):
            legacy.pop(f"{stem}_{suffix}")

    partial = cross_run_retrieve("", "distillation", capsules=[legacy], lessons=[])
    exact = cross_run_retrieve("", "distillation", capsules=[complete], lessons=[])

    receipt = partial["receipt"]
    assert receipt["n_capsules"] == receipt["partial_capsules"] == 1
    assert receipt["source_complete"] is False
    assert receipt["source_unknown_capsules"] == 1
    assert receipt["corpus_digest"] != exact["receipt"]["corpus_digest"]
    assert receipt["retrieval_digest"] != exact["receipt"]["retrieval_digest"]
    assert partial["results"][0]["stable_id"] == exact["results"][0]["stable_id"]


def test_receipt_digest_commits_to_applicability_scope_completeness():
    capsule = build_concept_capsule(
        run_id="r1", fingerprint=["k"], direction="max",
        concepts=["distillation"], concept_outcomes={})
    complete_scope = {
        "scope_complete": True, "scope_unknown_capsules": 0,
        "scope_fingerprint_unknown_capsules": 0,
        "scope_fingerprint_items_omitted": 0, "scope_direction_unknown_capsules": 0,
    }
    partial_scope = {
        "scope_complete": False, "scope_unknown_capsules": 1,
        "scope_fingerprint_unknown_capsules": 1,
        "scope_fingerprint_items_omitted": 0, "scope_direction_unknown_capsules": 0,
    }

    exact = cross_run_retrieve(
        "", "distillation", capsules=[capsule], lessons=[], scope_receipt=complete_scope)
    partial = cross_run_retrieve(
        "", "distillation", capsules=[capsule], lessons=[], scope_receipt=partial_scope)

    assert exact["receipt"]["scope_complete"] is True
    assert partial["receipt"]["scope_complete"] is False
    assert partial["receipt"]["scope_unknown_capsules"] == 1
    assert exact["receipt"]["corpus_digest"] != partial["receipt"]["corpus_digest"]
    assert exact["receipt"]["retrieval_digest"] != partial["receipt"]["retrieval_digest"]
    assert exact["results"][0]["stable_id"] == partial["results"][0]["stable_id"]

    malformed = cross_run_retrieve("", "distillation", capsules=[capsule], lessons=[], scope_receipt={
        **partial_scope, "scope_fingerprint_unknown_capsules": 2,
    })
    assert malformed["receipt"]["scope_receipt_known"] is False
    assert malformed["receipt"]["scope_complete"] is False


def test_quota_does_not_inject_an_unrelated_caveat(tmp_path):
    lessons = [_lesson(f"retrieval recall method {i} improves quality", run_id=f"r{i}") for i in range(5)]
    lessons += [_lesson("banana orchard irrigation helps yield", "supported", (1,), "rA"),
                _lesson("banana orchard irrigation helps yield", "failed", (2,), "rB")]
    _seed(tmp_path, lessons=lessons)
    out = cross_run_retrieve(str(tmp_path), "retrieval recall method", k=3, contradiction_quota=0.8)
    assert out["receipt"]["caveat_target"] == 2
    assert out["receipt"]["n_caveats"] == 0
    assert all("banana" not in h["text"] for h in out["results"])


def test_failed_intent_is_a_soft_bonus_not_an_unrelated_hard_tier(tmp_path):
    _seed(tmp_path, lessons=[
        _lesson("hard negative mining improves retrieval recall", run_id="r1"),
        _lesson("unrelated banana irrigation helps yield", "supported", (2,), "rA"),
        _lesson("unrelated banana irrigation helps yield", "failed", (3,), "rB")])
    out = cross_run_retrieve(str(tmp_path), "hard negative retrieval", k=1, intent="failed")
    assert "hard negative" in out["results"][0]["text"]
    assert out["results"][0]["intent_bonus"] == 0.0


def test_tool_and_cli(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app
    from looplab.tools.cross_run_tools import CrossRunTools
    _seed(tmp_path, lessons=[_lesson("hard negative mining improves recall")])
    out = CrossRunTools(tmp_path).execute("cross_run_search", {"query": "hard negatives"})
    assert "hard negative" in out.lower() and "claim" in out
    res = CliRunner().invoke(app, ["cross-run-search", str(tmp_path), "hard negatives"])
    assert res.exit_code == 0 and "cross-run search" in res.stdout and "hard negative" in res.stdout.lower()


def test_tool_and_cli_search_label_partial_concept_source_as_retained(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app
    from looplab.tools.cross_run_tools import CrossRunTools

    capsule = build_concept_capsule(
        run_id="legacy", fingerprint=["k"], direction="max",
        concepts=["distillation"], concept_outcomes={})
    for stem in ("concepts", "concept_outcomes"):
        for suffix in ("total", "omitted", "complete"):
            capsule.pop(f"{stem}_{suffix}")
    _seed(tmp_path, capsules=[capsule])

    tools = CrossRunTools(tmp_path)
    hit = tools.execute("cross_run_search", {"query": "distillation"})
    cli = CliRunner().invoke(app, ["cross-run-search", str(tmp_path), "distillation"])

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    empty_legacy = build_concept_capsule(
        run_id="empty-legacy", fingerprint=["k"], direction="max",
        concepts=[], concept_outcomes={})
    for stem in ("concepts", "concept_outcomes"):
        for suffix in ("total", "omitted", "complete"):
            empty_legacy.pop(f"{stem}_{suffix}")
    _seed(empty_dir, capsules=[empty_legacy])
    miss = CrossRunTools(empty_dir).execute(
        "cross_run_search", {"query": "unseen zirconium"})

    assert "WARNING: PARTIAL capsule source" in hit
    assert "retained in at least 1 run(s)" in hit
    assert "source_complete=false" in hit
    assert "no retained cross-run knowledge matched" in miss
    assert "not proof that no matching concept exists" in miss
    assert "PARTIAL capsule source" in miss
    assert cli.exit_code == 0
    assert "concept capsule source is partial" in cli.stdout
    assert "retained in at least 1 run(s)" in cli.stdout


def test_pinned_claim_retained_in_retrieval_under_quota(tmp_path):
    # concept-conformance regression (§22.4/§21.20.5): the 'pinned is retained' governance projection applies
    # to cross_run_retrieve too — a relevant operator-pinned claim survives the contradiction-quota swap (the
    # swap victim filter now excludes operator-pinned, mirroring build_context_pack).
    from looplab.engine.claims import cross_run_retrieve, record_claim_decision
    lessons = [_lesson("hard negative mining improves recall", "supported", (1,), "r0")]
    lessons += [_lesson(f"hard negative mining variant {i}", "supported", (i + 2,), f"r{i}") for i in range(2)]
    lessons += [_lesson("hard negative mining reranker", "supported", (7,), "rA"),
                _lesson("hard negative mining reranker", "tested", (8,), "rB")]   # contested -> caveat
    _seed(tmp_path, lessons=lessons)
    record_claim_decision(str(tmp_path), statement="hard negative mining improves recall", decision="pinned")
    r = cross_run_retrieve(str(tmp_path), "hard negative mining recall", k=4, intent="failed")  # high quota
    assert r["receipt"]["caveat_target"] >= 1
    assert any(h.get("maturity") == "operator-pinned" for h in r["results"])       # pin retained under quota
    assert any(h.get("epistemic") == "mixed" for h in r["results"])                # ...and caveats surfaced
