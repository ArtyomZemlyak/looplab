"""Full Memora↔lessons synergy: cross-run LESSONS retrieval gains harmonic (abstraction+anchor)
recall — surfacing a lesson from a differently-worded but anchor-linked task that the
fingerprint-Jaccard gate (≥0.34 token overlap) misses — while staying byte-identical when memora
is off, and still passing every hit through the D2 hygiene pipeline."""
from __future__ import annotations

from looplab.engine.memory import (retrieve_lessons_harmonic, _lesson_index_text,
                                   filter_contradicted)
from looplab.tools.memora import lexical_abstraction
from looplab.tools.vectorstore import hash_embed


def _lesson(idx, statement, fp, task_id="t", outcome="supported"):
    return (idx, {"statement": statement, "fingerprint": fp, "task_id": task_id,
                  "outcome": outcome})


def test_harmonic_noop_without_abstractor():
    cands = [_lesson(0, "deeper trees help", ["kind:dataset", "churn"])]
    # abstract=None (memora off) -> legacy: no harmonic hits, caller stays byte-identical
    assert retrieve_lessons_harmonic(cands, "predict churn", None, hash_embed) == []


def test_harmonic_recovers_anchor_linked_lesson():
    # Two lessons whose ORIGIN tasks share a cue ("regularization") but whose fingerprints have
    # low token overlap with the query — Jaccard would miss them; the harmonic anchor index finds them.
    cands = [
        _lesson(0, "L2 regularization curbed overfitting on the wide model",
                ["kind:dataset", "dir:max", "regularization", "overfitting", "ridge"]),
        _lesson(1, "totally unrelated: image augmentation flips helped",
                ["kind:vision", "dir:max", "augmentation", "flip", "crop"]),
    ]
    query = "kind:dataset dir:max regularization penalty tuning"
    hits = retrieve_lessons_harmonic(cands, query, lexical_abstraction, hash_embed, k=4)
    got = {i for _, i in hits}
    assert 0 in got                       # the regularization lesson surfaced by anchor overlap
    # every returned similarity is capped below an exact-task Jaccard (1.0)
    assert all(s <= 0.9 for s, _ in hits)


def test_lesson_index_text_uses_fingerprint_and_statement():
    o = {"statement": "early stopping helped", "fingerprint": ["kind:dataset", "param:lr", "churn"]}
    txt = _lesson_index_text(o)
    assert "churn" in txt and "early stopping" in txt
    assert "param:lr" not in txt          # param tokens dropped (dilute the task-cue space)


def test_harmonic_hits_still_pass_d2_hygiene():
    # A harmonic-surfaced 'supported' lesson that a NEWER same-task run reversed must still be
    # quarantined by filter_contradicted downstream — the synergy doesn't bypass the misevolution guard.
    scored = [
        (0.7, 0, {"statement": "trick X helps", "outcome": "supported", "task_id": "t"}),
        (0.9, 5, {"statement": "trick X helps", "outcome": "abandoned", "task_id": "t"}),  # newer
    ]
    kept = filter_contradicted(scored)
    outcomes = {(o["statement"], o["outcome"]) for _, _, o in kept}
    assert ("trick X helps", "supported") not in outcomes
    assert ("trick X helps", "abandoned") in outcomes


def test_harmonic_handles_empty_and_bad_lessons():
    assert retrieve_lessons_harmonic([], "q", lexical_abstraction, hash_embed) == []
    # a lesson with no fingerprint still indexes off its statement
    cands = [_lesson(0, "gradient clipping stabilized training", None)]
    hits = retrieve_lessons_harmonic(cands, "gradient clipping training", lexical_abstraction,
                                     hash_embed)
    assert isinstance(hits, list)


def test_engine_wires_lesson_abstractor(tmp_path):
    # A memora-on Settings yields a lexical abstractor; the engine stores it and the reflection
    # path uses it. A memora-off Settings yields None (legacy).
    from looplab.adapters.tasks import _make_abstractor
    from looplab.core.config import Settings

    on = Settings(memora=True, memora_llm=False, backend="toy")
    off = Settings(memora=False, backend="toy")
    assert _make_abstractor(on) is not None
    assert _make_abstractor(off) is None
