"""Card ledger (Kanban re-architecture, docs/23 Layer 1a).

`_derive_cards` is the durable, ADVISORY card projection that MIRRORS `_derive_hypotheses`: it never
touches best-selection, folds order-tolerantly, and — crucially — computes a card's verdict with the
SAME `_evidence_verdict` helper the hypotheses use, so a card is byte-identical to its hash-joined
hypothesis wherever their evidence coincides. These tests pin: the shadow invariant, the id/statement-
hash join, card_added/merged/dropped, the derived lifecycle lanes, empty-on-old-logs, and the reserved
operator-override overlay phase (filled by Layer 6, a no-op here)."""
from __future__ import annotations

from looplab.core.models import Event, hypothesis_id
from looplab.events.replay import _derive_cards, fold


def _mk(evs):
    return [Event(type=t, data=d) for t, d in evs]


def _run(direction="max", extra=None):
    evs = [
        ("run_started", {"run_id": "r", "task_id": "t", "direction": direction}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "a linear baseline is enough"}}),
        ("node_evaluated", {"node_id": 1, "metric": 0.80}),
        ("node_created", {"node_id": 2, "operator": "improve", "parent_ids": [1],
                          "idea": {"operator": "improve", "hypothesis": "interaction features help"}}),
        ("node_evaluated", {"node_id": 2, "metric": 0.88}),          # improved over parent -> supported
        ("node_created", {"node_id": 3, "operator": "improve", "parent_ids": [2],
                          "idea": {"operator": "improve", "hypothesis": "a deeper model helps"}}),
        ("node_evaluated", {"node_id": 3, "metric": 0.85}),          # worse than parent
        ("node_created", {"node_id": 4, "operator": "improve", "parent_ids": [2],
                          "idea": {"operator": "improve", "hypothesis": "a deeper model helps"}}),  # pending
    ]
    return fold(_mk(evs + (extra or [])))


def _cards_by_stmt(st):
    return {c.statement: c for c in st.cards.values()}


def test_card_is_a_byte_identical_shadow_of_the_hypothesis():
    # The load-bearing Layer-1a invariant: for every hypothesis there is a card with the SAME id whose
    # verdict/best_delta/evidence match exactly (the shared _evidence_verdict helper guarantees it).
    st = _run()
    assert set(st.cards) == set(st.hypotheses)
    for cid, h in st.hypotheses.items():
        c = st.cards[cid]
        assert c.verdict == h.status
        assert c.best_delta == h.best_delta
        assert c.evidence == h.evidence
        assert c.seed_statement == c.statement == h.statement


def test_link_by_card_id_overrides_the_statement_hash():
    # When idea.card_id is set, evidence links to THAT card id, not the statement hash.
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "x", "card_id": "card-7"}}),
        ("node_evaluated", {"node_id": 1, "metric": 0.5}),
    ]))
    assert "card-7" in st.cards
    assert st.cards["card-7"].evidence == [1]
    assert hypothesis_id("x") not in st.cards          # the hash key was NOT used


def test_link_falls_back_to_statement_hash_without_card_id():
    st = _run()
    assert hypothesis_id("interaction features help") in st.cards


def test_card_added_seeds_a_proposed_card_with_no_evidence():
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("card_added", {"id": "card-1", "statement": "external data helps", "source": "operator"}),
    ]))
    c = st.cards["card-1"]
    assert c.status == "proposed" and c.verdict == "open"
    assert c.evidence == [] and c.source == "operator" and c.seed_statement == "external data helps"


def test_card_merged_unions_evidence_into_the_canonical():
    a, b = "interaction features help", "a linear baseline is enough"
    st = _run(extra=[("card_merged", {"canonical": hypothesis_id(b), "aliases": [hypothesis_id(a)]})])
    by = _cards_by_stmt(st)
    # the alias card is gone; the canonical carries the union of evidence and records the alias.
    assert a not in by
    canon = st.cards[hypothesis_id(b)]
    assert canon.evidence == [1, 2]
    assert hypothesis_id(a) in canon.aliases


def test_card_dropped_sets_dropped_status_and_provenance():
    cid = hypothesis_id("a linear baseline is enough")
    st = _run(extra=[("card_dropped", {"id": cid, "reason": "superseded", "dropped_by": "operator"})])
    c = st.cards[cid]
    assert c.status == "dropped" and c.dropped_reason == "superseded" and c.dropped_by == "operator"


def test_status_lanes_proposed_running_evaluated():
    st = _run()
    by = _cards_by_stmt(st)
    assert by["interaction features help"].status == "evaluated"   # a finished eval
    assert by["a deeper model helps"].status == "running"          # node 4 still pending


def test_abandoned_hypothesis_makes_the_shadow_card_abandoned():
    st = _run(extra=[("hypothesis_updated",
                      {"id": hypothesis_id("interaction features help"), "status": "abandoned"})])
    assert _cards_by_stmt(st)["interaction features help"].verdict == "abandoned"


def test_old_log_without_card_or_hypothesis_events_has_empty_cards():
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "operator": "draft", "idea": {"operator": "draft"}}),
        ("node_evaluated", {"node_id": 1, "metric": 0.5}),
    ]))
    assert st.cards == {}                              # no idea.hypothesis, no card_added -> nothing


def test_reserved_operator_override_overlay_is_applied_last():
    # Layer 6 fills these maps via control events; Layer 1a reserves the FINAL overlay phase so no
    # _derive_cards rewrite is needed later. Populate them directly and re-derive: operator wins.
    st = _run()
    cid = hypothesis_id("interaction features help")
    st.card_operator_edits = {cid: {"statement": "OPERATOR RENAME"}}
    st.card_priority_pins = {cid: 3}
    st.card_resource_pins = {cid: {"gpus": 2, "gpu_mem_mib": 8000}}
    _derive_cards(st)
    c = st.cards[cid]
    assert c.statement == "OPERATOR RENAME"           # DISPLAY overlaid
    assert c.seed_statement == "interaction features help"   # join key UNCHANGED
    assert c.priority == 3
    assert c.footprint == {"gpus": 2, "gpu_mem_mib": 8000, "pinned_by": "operator"}


def test_derive_cards_does_not_touch_selection_or_hypotheses():
    # Advisory guarantee: the card pass runs AFTER best-selection and mutates only st.cards.
    st = _run()
    best_before = st.best_node_id
    hyps_before = {k: (v.status, list(v.evidence), v.best_delta) for k, v in st.hypotheses.items()}
    _derive_cards(st)                                  # idempotent re-run
    assert st.best_node_id == best_before
    assert {k: (v.status, list(v.evidence), v.best_delta) for k, v in st.hypotheses.items()} == hyps_before


def _shadow_holds(st):
    """The Layer-1a invariant: every hypothesis has a same-id card with an identical verdict/evidence."""
    if set(st.cards) != set(st.hypotheses):
        return False
    for cid, h in st.hypotheses.items():
        c = st.cards[cid]
        if (c.verdict, c.evidence, c.best_delta) != (h.status, h.evidence, h.best_delta):
            return False
    return True


def test_shadow_holds_across_engine_hypothesis_events():
    # The engine mints hypothesis_added (deep research), hypothesis_merged (consolidation) and
    # hypothesis_updated(deleted) on the DEFAULT path (track_hypotheses=True) — but never card_* yet.
    # In Layer 1a `_derive_cards` mirrors those same inputs, so the shadow must survive all three. (This
    # is the case the first shadow test missed: its fixture emitted none of these events.)
    a, b = "interaction features help", "a linear baseline is enough"
    st = _run(extra=[
        ("hypothesis_added", {"statement": "external data raises accuracy", "source": "deep_research"}),
        ("hypothesis_merged", {"canonical": hypothesis_id(b), "aliases": [hypothesis_id(a)]}),
        ("hypothesis_updated", {"id": hypothesis_id("a deeper model helps"), "status": "deleted"}),
    ])
    assert _shadow_holds(st)
    # spot-check each path: node-less added card exists; merged canonical carries the union; deleted gone.
    assert hypothesis_id("external data raises accuracy") in st.cards
    assert st.cards[hypothesis_id(b)].evidence == [1, 2]
    assert hypothesis_id("a deeper model helps") not in st.cards


def test_malformed_records_do_not_brick_the_fold():
    # "one bad record must not brick the fold" — a truthy-but-non-iterable `aliases` on a merge and a
    # non-numeric `at_node` on an add reach the guarded loops (their handlers only check truthiness).
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "baseline"}}),
        ("node_evaluated", {"node_id": 1, "metric": 0.7}),
        ("card_added", {"id": "card-x", "statement": "ok card", "at_node": "not-a-number"}),
        ("card_merged", {"canonical": hypothesis_id("baseline"), "aliases": 1}),          # non-iterable
        ("hypothesis_merged", {"canonical": hypothesis_id("baseline"), "aliases": True}),  # non-iterable
        ("card_dropped", {"id": "card-x", "reason": "n/a", "dropped_by": "engine"}),
    ]))
    # the fold survived and still produced a coherent ledger
    assert hypothesis_id("baseline") in st.cards
    assert st.cards["card-x"].created_at_node == 0        # bad at_node coerced, not crashed
    assert st.cards["card-x"].status == "dropped"


def test_gated_lane_for_infeasible_only_evidence():
    # A card whose sole evidence node is INFEASIBLE (constraint violations) -> lifecycle 'gated', and the
    # verdict is 'open' (no usable evidence). Distinguishes an excluded card from a fresh 'proposed' one.
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "constraint-breaking idea"}}),
        ("node_evaluated", {"node_id": 1, "metric": 0.9, "violations": ["used the test set"]}),
    ]))
    c = st.cards[hypothesis_id("constraint-breaking idea")]
    assert c.status == "gated" and c.verdict == "open"


# --- Layer 1b: enrichment ------------------------------------------------------------------------

def test_footprint_rides_the_idea_onto_the_card():
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "big model",
                                   "footprint": {"gpus": 4, "gpu_mem_mib": 16000}}}),
        ("node_evaluated", {"node_id": 1, "metric": 0.9}),
    ]))
    c = st.cards[hypothesis_id("big model")]
    assert c.footprint == {"gpus": 4, "gpu_mem_mib": 16000, "proposed_by": "researcher"}


def test_novelty_and_cross_run_signals_are_rehomed_by_node_id():
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "novel idea"}}),
        ("node_evaluated", {"node_id": 1, "metric": 0.7}),
        ("novelty_graded", {"node_id": 1, "grade": "REOPEN", "level": 5, "near_node": 0,
                            "recommendation": "allow"}),
        ("cross_run_prior", {"node_id": 1, "matched_concepts": ["loss/contrastive"],
                             "prior_runs": [{"run": "x", "metric": 0.6}]}),
    ]))
    c = st.cards[hypothesis_id("novel idea")]
    assert c.novelty_verdict == {"grade": "REOPEN", "level": 5, "near_node": 0, "recommendation": "allow"}
    assert c.cross_run_prior == {"matched_concepts": ["loss/contrastive"],
                                 "prior_runs": [{"run": "x", "metric": 0.6}]}


def test_card_ranked_stamps_priority_on_open_cards_only():
    a = "an open direction with no evidence"
    st = _run(extra=[
        ("hypothesis_added", {"statement": a, "source": "deep_research"}),  # open, no node
        ("card_ranked", {"order": [hypothesis_id(a), hypothesis_id("interaction features help")]}),
    ])
    assert st.cards[hypothesis_id(a)].priority == 0                 # open -> ranked
    assert st.cards[hypothesis_id("interaction features help")].priority is None  # supported -> not


def test_card_enriched_applies_allow_listed_fields_and_tolerates_malformed():
    cid = hypothesis_id("interaction features help")
    st = _run(extra=[
        ("card_enriched", {"id": cid, "research_origin": "memo-9", "provenance_tier": "authored",
                           "footprint": {"gpus": 2}, "evil_field": "should be ignored"}),
        ("card_enriched", {"id": cid, "footprint": "not-a-dict"}),        # malformed -> skipped, no crash
        ("card_enriched", {"id": "no-such-card", "research_origin": "x"}),  # unknown id -> skipped
    ])
    c = st.cards[cid]
    assert c.research_origin == "memo-9" and c.provenance_tier == "authored"
    assert c.footprint == {"gpus": 2}                              # dict delta applied, string one skipped


def test_card_enriched_cannot_overwrite_shadow_load_bearing_fields():
    # The allow-list is the ONLY thing protecting the shadow: a delta naming a REAL Card field that is
    # NOT in the allow-list (verdict/status/evidence/best_delta/id/statement) must be a no-op — otherwise
    # a hostile/buggy enrichment could silently corrupt the card away from its hypothesis.
    cid = hypothesis_id("interaction features help")
    st = _run(extra=[("card_enriched", {"id": cid, "verdict": "abandoned", "status": "dropped",
                                        "evidence": [], "best_delta": 99.0, "statement": "HIJACK"})])
    c, h = st.cards[cid], st.hypotheses[cid]
    assert c.verdict == h.status and c.evidence == h.evidence and c.best_delta == h.best_delta
    assert c.status == "evaluated" and c.statement == "interaction features help"


def test_card_enriched_bad_numeric_does_not_drop_sibling_fields():
    # The numeric coercions (foresight_rank/confidence) are guarded PER FIELD, so a malformed numeric
    # ordered BEFORE valid string fields must not abort the rest of the delta (key-order independence).
    cid = hypothesis_id("interaction features help")
    st = _run(extra=[("card_enriched", {"id": cid, "confidence": "not-a-float",
                                        "research_origin": "memo-1", "provenance_tier": "authored"})])
    c = st.cards[cid]
    assert c.research_origin == "memo-1" and c.provenance_tier == "authored" and c.confidence is None


def test_research_origin_is_rehomed_from_the_node_provenance():
    # Node.research_origin is the deep-research provenance {at_node, trigger}; the card carries a ref to it.
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "researched idea"},
                          "research_origin": {"at_node": 4, "trigger": "cadence"}}),
        ("node_evaluated", {"node_id": 1, "metric": 0.6}),
    ]))
    assert st.cards[hypothesis_id("researched idea")].research_origin == "node:4"


def test_priority_falls_back_to_hypothesis_ranking_and_matches_the_shadow():
    # No card_ranked producer exists yet, so hypothesis_ranking is the SOLE live card-priority path in
    # Layer 1b. The shadow requires card.priority == hypothesis.priority wherever both derive from it.
    a = "an open direction with no evidence"
    order = [hypothesis_id(a), hypothesis_id("interaction features help")]
    st = _run(extra=[
        ("hypothesis_added", {"statement": a, "source": "deep_research"}),   # open, no node
        ("hypothesis_ranked", {"order": order}),                              # NOT card_ranked
    ])
    for cid, h in st.hypotheses.items():
        assert st.cards[cid].priority == h.priority                          # fallback == shadow
    assert st.cards[hypothesis_id(a)].priority == 0                          # open card ranked first


def test_operator_pin_wins_over_engine_enrichment():
    # The reserved operator overlay runs AFTER 1b enrichment, so an operator resource pin overrides a
    # researcher-proposed footprint (docs/23 decision 27: operator wins).
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "h", "footprint": {"gpus": 8}}}),
        ("node_evaluated", {"node_id": 1, "metric": 0.5}),
    ]))
    cid = hypothesis_id("h")
    assert st.cards[cid].footprint == {"gpus": 8, "proposed_by": "researcher"}
    st.card_resource_pins = {cid: {"gpus": 1}}
    _derive_cards(st)
    assert st.cards[cid].footprint["gpus"] == 1 and st.cards[cid].footprint["pinned_by"] == "operator"
