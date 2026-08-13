"""Card ledger (Kanban re-architecture, docs/23 Layer 1a).

`_derive_cards` is the durable, ADVISORY research board: it never touches best-selection, folds
order-tolerantly, and computes a card's verdict with the `_evidence_verdict` helper. These tests pin:
the id/statement-hash join, card_added/merged/dropped, the legacy hypothesis_* bridges, the derived
lifecycle lanes, empty-on-old-logs, and the reserved operator-override overlay phase (filled by
Layer 6, a no-op here)."""
from __future__ import annotations

import pytest

from looplab.events.belief_projection import grouped_beliefs
from looplab.core.models import (
    CARD_ACTION_DIGEST_V1_FIELDS,
    CARD_ACTION_DIGEST_V2_FIELDS,
    IDEA_PROPOSAL_DIGEST_V1_FIELDS,
    Event,
    Idea,
    IdeaEmission,
    card_action_digest,
    card_ownership_receipt,
    durable_idea_payload,
    hypothesis_id,
    hypothesis_statement_digest,
    idea_proposal_digest,
    idea_proposal_ref,
)
from looplab.events.replay import FoldCursor, _derive_cards, fold


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


def _fold_with_cursor(events):
    expected = fold(events)
    cursor = FoldCursor()
    midpoint = len(events) // 2
    cursor.extend(events[:midpoint])
    cursor.snapshot()  # finalizing a prefix must not mutate the raw incremental accumulator
    cursor.extend(events[midpoint:])
    actual = cursor.snapshot()
    assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
    return expected


def test_link_by_card_id_overrides_the_statement_hash():
    # When idea.card_id is set, evidence links to THAT card id, not the statement hash.
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "x", "card_id": "card-7",
                                   "eval_timeout": 45}}),
        ("node_evaluated", {"node_id": 1, "metric": 0.5}),
    ]))
    assert "card-7" in st.cards
    assert st.cards["card-7"].evidence == [1]
    assert st.cards["card-7"].eval_timeout == 45.0
    assert st.cards["card-7"].parent_generations == {}
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


def test_node_less_card_added_preserves_action_but_rejects_invalid_steering_snapshot():
    st = _fold_with_cursor(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("card_added", {
            "id": "card-ready", "statement": "ready to build", "source": "foresight",
            "idea": {"operator": "improve", "params": {"lr": 0.2, "bad": "x"},
                     "space": {"depth": [2, "bad", 4]}, "eval_profile": "smoke",
                     "concept_tags": ["model/tree", {"bad": True}]},
            "parent_id": 3, "parent_ids": [3, "bad", 3], "scored_against": 9,
            "footprint": {"gpus": 2, "gpu_mem_mib": 8_000},
            "steering_context": [1, {"kind": "coverage", "ref": "axis/model"}],
        }),
    ]))
    card = st.cards["card-ready"]
    assert card.evidence == [] and card.operator == "improve"
    assert card.params == {"lr": 0.2} and card.space == {"depth": [2.0, 4.0]}
    assert card.eval_profile == "smoke" and card.concept_tags == ["model/tree"]
    assert card.parent_id == 3 and card.parent_ids == [3] and card.scored_against == 9
    assert card.footprint == {"gpus": 2, "gpu_mem_mib": 8_000}
    # Steering is a closed, ref-shaped writer contract. One malformed member invalidates the atomic
    # snapshot instead of letting replay silently turn a lossy projection into an apparently exact cue.
    assert card.steering_context == []


def test_invalid_non_string_card_id_falls_back_to_statement_hash():
    statement = "invalid id does not become string seven"
    st = _fold_with_cursor(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("card_added", {"id": 7, "statement": statement}),
        ("hypothesis_added", {"statement": statement}),
    ]))
    assert set(st.cards) == {hypothesis_id(statement)}
    assert "7" not in st.cards


def test_card_merged_unions_evidence_into_the_canonical():
    a, b = "interaction features help", "a linear baseline is enough"
    st = _run(extra=[("card_merged", {"canonical": hypothesis_id(b), "aliases": [hypothesis_id(a)]})])
    by = _cards_by_stmt(st)
    # the alias card is gone; the canonical carries the union of evidence and records the alias.
    assert a not in by
    canon = st.cards[hypothesis_id(b)]
    assert canon.evidence == [1, 2]
    assert hypothesis_id(a) in canon.aliases


def test_transitive_card_merge_verdict_matches_one_hash_joined_card():
    nodes = [
        ("node_created", {"node_id": 0, "operator": "draft", "parent_ids": [],
                          "idea": {"operator": "draft", "hypothesis": "direction A"}}),
        ("node_evaluated", {"node_id": 0, "metric": 0.80}),
        ("node_created", {"node_id": 1, "operator": "improve", "parent_ids": [0],
                          "idea": {"operator": "improve", "hypothesis": "direction B"}}),
        ("node_evaluated", {"node_id": 1, "metric": 0.90}),
        ("node_created", {"node_id": 2, "operator": "improve", "parent_ids": [1],
                          "idea": {"operator": "improve", "hypothesis": "direction C"}}),
        ("node_evaluated", {"node_id": 2, "metric": 0.85}),
    ]
    reference_rows = [("run_started", {"run_id": "r", "task_id": "t", "direction": "max"})]
    for event_type, data in nodes:
        copied = {**data}
        if event_type == "node_created":
            copied["idea"] = {**data["idea"], "hypothesis": "one canonical direction"}
        reference_rows.append((event_type, copied))
    reference = fold(_mk(reference_rows))
    reference_card = reference.cards[hypothesis_id("one canonical direction")]

    card_rows = [("run_started", {"run_id": "r", "task_id": "t", "direction": "max"})]
    for card_id, statement in (("card-a", "direction A"), ("card-b", "direction B"),
                               ("card-c", "direction C")):
        card_rows.append(("card_added", {"id": card_id, "statement": statement}))
    for index, (event_type, data) in enumerate(nodes):
        copied = {**data}
        if event_type == "node_created":
            copied["idea"] = {**data["idea"], "card_id": f"card-{'abc'[index // 2]}"}
        card_rows.append((event_type, copied))
    card_rows.extend([
        ("card_merged", {"canonical": "card-b", "aliases": ["card-a"]}),
        ("card_merged", {"canonical": "card-c", "aliases": ["card-b"]}),
    ])
    merged = fold(_mk(card_rows)).cards["card-c"]
    assert (merged.verdict, merged.evidence, merged.best_delta) == (
        reference_card.verdict, reference_card.evidence, reference_card.best_delta)


def test_alias_enrichment_and_drop_resolve_to_the_canonical_card():
    alias = hypothesis_id("interaction features help")
    canonical = hypothesis_id("a linear baseline is enough")
    events = _mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "a linear baseline is enough"}}),
        ("node_created", {"node_id": 2, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "interaction features help"}}),
        ("card_merged", {"canonical": canonical, "aliases": [alias]}),
        ("card_enriched", {"id": alias, "research_origin": "memo-alias"}),
        ("card_dropped", {"id": alias, "reason": "merged duplicate", "dropped_by": "engine"}),
    ])
    st = _fold_with_cursor(events)
    assert set(st.cards) == {canonical}
    assert st.cards[canonical].research_origin == "memo-alias"
    assert st.cards[canonical].status == "dropped"
    assert st.cards[canonical].dropped_reason == "merged duplicate"


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
    assert c.pinned is True
    assert c.footprint is None                              # immutable action declaration unchanged
    assert c.resource_pin == {"gpus": 2, "gpu_mem_mib": 8000, "pinned_by": "operator"}


def test_derive_cards_does_not_touch_selection():
    # Advisory guarantee: the card pass runs AFTER best-selection and mutates only st.cards, and it is
    # idempotent — a re-run reproduces the same cards and leaves best-selection untouched.
    st = _run()
    best_before = st.best_node_id
    cards_before = {k: (v.verdict, list(v.evidence), v.best_delta) for k, v in st.cards.items()}
    _derive_cards(st)                                  # idempotent re-run
    assert st.best_node_id == best_before
    assert {k: (v.verdict, list(v.evidence), v.best_delta) for k, v in st.cards.items()} == cards_before


def test_card_board_tracks_engine_hypothesis_events():
    # The engine mints hypothesis_added (deep research), hypothesis_merged (consolidation) and
    # hypothesis_updated(deleted) on the DEFAULT path — `_derive_cards` folds those same inputs into the
    # card board, so all three legacy bridges must land on the cards.
    a, b = "interaction features help", "a linear baseline is enough"
    st = _run(extra=[
        ("hypothesis_added", {"statement": "external data raises accuracy", "source": "deep_research"}),
        ("hypothesis_merged", {"canonical": hypothesis_id(b), "aliases": [hypothesis_id(a)]}),
        ("hypothesis_updated", {"id": hypothesis_id("a deeper model helps"), "status": "deleted"}),
    ])
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


def test_string_merge_aliases_are_not_iterated_as_card_ids():
    st = _fold_with_cursor(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("hypothesis_added", {"id": "a", "statement": "first"}),
        ("hypothesis_added", {"id": "b", "statement": "second"}),
        ("hypothesis_merged", {"canonical": "b", "aliases": "a"}),
    ]))
    assert set(st.cards) == {"a", "b"}


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


def test_idea_advisory_fields_are_tolerant_on_replay_but_strict_at_emission():
    tolerant = Idea(
        operator="draft", card_id=7,
        footprint={"gpus": 2, "gpu_mem_mib": 8_000, "pinned_by": "researcher"},
    )
    assert tolerant.card_id is None
    assert tolerant.footprint == {"gpus": 2, "gpu_mem_mib": 8_000}

    with pytest.raises(ValueError, match="card_id"):
        IdeaEmission(operator="draft", concept_mode="full", card_id=7)
    with pytest.raises(ValueError, match="footprint"):
        IdeaEmission(
            operator="draft", concept_mode="full",
            footprint={"gpus": 2, "pinned_by": "researcher"},
        )


def test_corrupt_advisory_idea_fields_do_not_drop_the_best_node():
    statement = "the winning proposal survives advisory corruption"
    st = _fold_with_cursor(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 0, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "baseline"}}),
        ("node_evaluated", {"node_id": 0, "metric": 0.2}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": statement,
                                   "card_id": 7, "footprint": []}}),
        ("node_evaluated", {"node_id": 1, "metric": 0.9}),
    ]))
    assert st.best_node_id == 1 and set(st.nodes) == {0, 1}
    assert st.nodes[1].idea.card_id is None and st.nodes[1].idea.footprint is None
    assert st.cards[hypothesis_id(statement)].evidence == [1]


def test_card_added_sanitizes_footprint_authority_and_keeps_concepts_with_bad_operator():
    st = _fold_with_cursor(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("card_added", {
            "id": "card-safe", "statement": "safe staged action",
            "idea": {"operator": 7, "concepts": ["model/tree"]},
            "footprint": {
                "gpus": 2, "gpu_mem_mib": 8_000,
                "pinned_by": "researcher", "finalized_by": "researcher",
            },
        }),
    ]))
    card = st.cards["card-safe"]
    assert card.operator is None and card.concept_tags == ["model/tree"]
    assert card.footprint == {"gpus": 2, "gpu_mem_mib": 8_000}


def test_non_string_controls_and_infinite_at_node_cannot_alias_valid_cards():
    st = _fold_with_cursor(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("card_added", {"id": "True", "statement": "literal true", "at_node": float("inf")}),
        ("card_added", {"id": "target", "statement": "target"}),
        ("card_merged", {"canonical": "target", "aliases": [True]}),
        ("card_enriched", {"id": True, "research_origin": "must-not-coerce"}),
        ("card_dropped", {"id": True, "reason": "must-not-coerce"}),
    ]))
    assert set(st.cards) == {"True", "target"}
    assert st.cards["True"].created_at_node == 0
    assert st.cards["True"].status == "proposed" and st.cards["True"].research_origin is None


def test_proposal_digest_uses_the_durable_legacy_envelope_and_frozen_v1_fields():
    assert IDEA_PROPOSAL_DIGEST_V1_FIELDS == (
        "operator", "params", "rationale", "eval_profile", "eval_timeout", "theme",
        "concepts", "concept_mode", "concepts_added", "concepts_removed", "space",
        "hypothesis", "card_id", "footprint",
    )
    legacy = Idea(operator="draft")
    explicit_empty = Idea(operator="draft", concepts=[])
    assert "concepts" not in durable_idea_payload(legacy)
    assert durable_idea_payload(explicit_empty)["concepts"] == []
    assert idea_proposal_digest(legacy) != idea_proposal_digest(explicit_empty)
    assert idea_proposal_digest(Idea.model_validate(durable_idea_payload(legacy))) == idea_proposal_digest(legacy)


def test_proposal_digest_rejects_oversized_mapping_keys_before_sorting_them():
    class _ExplodingSortKey(str):
        def __lt__(self, other):
            raise AssertionError("oversized keys must fail before sorting")

    idea = Idea(operator="draft")
    idea.params = {
        _ExplodingSortKey("a" * 513): 1.0,
        _ExplodingSortKey("b" * 513): 2.0,
    }
    assert idea_proposal_digest(idea) is None


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
    assert c.cross_run_prior == {
        "v": None,
        "matched_concepts": ["loss/contrastive"],
        "prior_runs": [{"run": "x", "metric": 0.6}],
        "prior_runs_total": None,
        "prior_runs_omitted": None,
        "prior_runs_complete": False,
        "concept_source": {"source_complete": False},
    }


def test_modern_sidecars_attach_only_to_the_exact_proposal_and_lifecycles():
    near = Idea(operator="draft", hypothesis="near proposal")
    candidate = Idea(operator="draft", hypothesis="exact candidate", concepts=[])
    events = _mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 0, "generation": 0, "operator": "draft",
                          "idea": durable_idea_payload(near)}),
        ("node_created", {"node_id": 1, "generation": 0, "operator": "draft",
                          "idea": durable_idea_payload(candidate)}),
        ("novelty_graded", {
            "node_id": 1, "generation": 0, "proposal_ref": idea_proposal_ref(candidate),
            "grade": "REOPEN", "level": 5, "near_node": 0, "near_generation": 0,
            "recommendation": "allow",
        }),
        ("cross_run_prior", {
            "v": 2, "node_id": 1, "generation": 0,
            "proposal_ref": idea_proposal_ref(candidate),
            "matched_concepts": ["loss/contrastive"],
            "prior_runs": [{"run": "x", "metric": 0.6}],
            "prior_runs_total": 1, "prior_runs_omitted": 0,
            "prior_runs_complete": True,
            "concept_source": {"source_complete": True, "source_rows_total": 1},
        }),
    ])
    card = _fold_with_cursor(events).cards[hypothesis_id("exact candidate")]
    assert card.novelty_verdict == {
        "grade": "REOPEN", "level": 5, "near_node": 0,
        "near_generation": 0, "recommendation": "allow",
    }
    assert card.cross_run_prior == {
        "v": 2,
        "matched_concepts": ["loss/contrastive"],
        "prior_runs": [{"run": "x", "metric": 0.6}],
        "prior_runs_total": 1,
        "prior_runs_omitted": 0,
        "prior_runs_complete": True,
        "concept_source": {"source_complete": True, "source_rows_total": 1},
    }


def test_modern_sidecars_fail_closed_on_mismatch_future_or_malformed_refs():
    candidate = Idea(operator="draft", hypothesis="bound candidate")
    other = Idea(operator="draft", hypothesis="discarded candidate")
    exact = idea_proposal_ref(candidate)
    assert exact is not None
    events = _mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "generation": 0, "operator": "draft",
                          "idea": durable_idea_payload(candidate)}),
        ("novelty_graded", {"node_id": 1, "generation": 0,
                            "proposal_ref": idea_proposal_ref(other), "grade": "wrong-digest"}),
        ("novelty_graded", {"node_id": 1, "generation": 0,
                            "proposal_ref": {"v": 2, "digest": exact["digest"]},
                            "grade": "future-ref"}),
        ("novelty_graded", {"node_id": 1, "generation": 0,
                            "proposal_ref": {"v": 1, "digest": 7}, "grade": "bad-ref"}),
        ("novelty_graded", {"node_id": 1, "generation": 1,
                            "proposal_ref": exact, "grade": "wrong-generation"}),
        ("novelty_graded", {"node_id": 1, "proposal_ref": exact, "grade": "missing-generation"}),
        ("novelty_graded", {"node_id": 1, "generation": 0, "grade": "missing-ref"}),
        ("novelty_rejected", {"node_id": 1, "generation": 0, "proposal_ref": exact,
                              "action": "reproposed", "kind": "semantic"}),
        ("cross_run_prior", {"node_id": 1, "generation": 0,
                             "proposal_ref": idea_proposal_ref(other),
                             "matched_concepts": ["must/not-attach"]}),
    ])
    card = _fold_with_cursor(events).cards[hypothesis_id("bound candidate")]
    assert card.novelty_verdict is None and card.cross_run_prior is None


def test_same_digest_aba_is_fenced_by_generation_and_current_receipt_attaches():
    candidate = Idea(operator="draft", hypothesis="same proposal after reset")
    ref = idea_proposal_ref(candidate)
    base = _mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "generation": 0, "operator": "draft",
                          "idea": durable_idea_payload(candidate)}),
        ("novelty_graded", {"node_id": 1, "generation": 0, "proposal_ref": ref,
                            "grade": "stale-generation"}),
        ("node_reset", {"node_id": 1, "generation": 0, "from_stage": "propose"}),
        ("node_created", {"node_id": 1, "generation": 1, "operator": "draft",
                          "idea": durable_idea_payload(candidate)}),
    ])
    stale = _fold_with_cursor(base).cards[hypothesis_id("same proposal after reset")]
    assert stale.novelty_verdict is None

    current = _fold_with_cursor(base + _mk([
        ("novelty_graded", {"node_id": 1, "generation": 1, "proposal_ref": ref,
                            "grade": "current-generation"}),
    ])).cards[hypothesis_id("same proposal after reset")]
    assert current.novelty_verdict["grade"] == "current-generation"


@pytest.mark.parametrize(
    "transition", ["absent", "missing-generation", "reset", "tombstone", "abort"],
)
def test_modern_near_node_requires_a_live_matching_lifecycle(transition):
    near = Idea(operator="draft", hypothesis="near lifecycle")
    candidate = Idea(operator="draft", hypothesis=f"candidate {transition}")
    near_id = 99 if transition == "absent" else 0
    novelty = {
        "node_id": 1, "generation": 0, "proposal_ref": idea_proposal_ref(candidate),
        "grade": "near-check", "near_node": near_id, "near_generation": 0,
    }
    if transition == "missing-generation":
        novelty.pop("near_generation")
    events = _mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 0, "generation": 0, "operator": "draft",
                          "idea": durable_idea_payload(near)}),
        ("node_created", {"node_id": 1, "generation": 0, "operator": "draft",
                          "idea": durable_idea_payload(candidate)}),
        ("novelty_graded", novelty),
    ])
    if transition == "reset":
        events += _mk([
            ("node_reset", {"node_id": 0, "generation": 0, "from_stage": "propose"}),
            ("node_created", {"node_id": 0, "generation": 1, "operator": "draft",
                              "idea": durable_idea_payload(near)}),
        ])
    elif transition == "tombstone":
        events += _mk([("node_tombstoned", {"node_ids": [0]})])
    elif transition == "abort":
        events += _mk([("node_abort", {"node_id": 0, "generation": 0})])
    card = _fold_with_cursor(events).cards[hypothesis_id(f"candidate {transition}")]
    assert card.novelty_verdict["near_node"] is None
    if transition == "missing-generation":
        assert "near_generation" not in card.novelty_verdict
    else:
        assert card.novelty_verdict["near_generation"] == 0


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("transition", ["tombstone", "abort"])
def test_sidecars_never_annotate_an_inactive_subject_lifecycle(modern, transition):
    candidate = Idea(operator="draft", hypothesis=f"inactive subject {modern} {transition}")
    receipt = {"node_id": 1, "grade": "must-not-attach"}
    if modern:
        receipt.update({"generation": 0, "proposal_ref": idea_proposal_ref(candidate)})
    events = _mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "generation": 0, "operator": "draft",
                          "idea": durable_idea_payload(candidate)}),
        ("novelty_graded", receipt),
    ])
    if transition == "tombstone":
        events += _mk([("node_tombstoned", {"node_ids": [1]})])
    else:
        events += _mk([("node_abort", {"node_id": 1, "generation": 0})])
    card = _fold_with_cursor(events).cards[hypothesis_id(candidate.hypothesis)]
    assert card.novelty_verdict is None


def test_legacy_sidecars_only_attach_to_generation_zero_and_never_rehome_reproposals():
    events = _mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "legacy exact"}}),
        ("novelty_graded", {"node_id": 1, "grade": "legacy-attaches"}),
        ("node_created", {"node_id": 2, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "legacy replacement"}}),
        ("novelty_rejected", {"node_id": 2, "action": "reproposed", "kind": "semantic"}),
        ("node_created", {"node_id": 3, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "legacy cross run"}}),
        ("novelty_rejected", {"node_id": 3, "action": "reproposed", "kind": "semantic"}),
        ("cross_run_prior", {"node_id": 3, "matched_concepts": ["must/not-attach"]}),
    ])
    st = _fold_with_cursor(events)
    assert st.cards[hypothesis_id("legacy exact")].novelty_verdict["grade"] == "legacy-attaches"
    assert st.cards[hypothesis_id("legacy replacement")].novelty_verdict is None
    assert st.cards[hypothesis_id("legacy cross run")].cross_run_prior is None


def test_cross_run_projection_retains_completeness_receipt_without_claiming_exactness():
    candidate = Idea(operator="draft", hypothesis="bounded prior receipt")
    prior_runs = [{"run": f"r-{index}"} for index in range(65)]
    st = _fold_with_cursor(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "generation": 0, "operator": "draft",
                          "idea": durable_idea_payload(candidate)}),
        ("cross_run_prior", {
            "v": 2, "node_id": 1, "generation": 0,
            "proposal_ref": idea_proposal_ref(candidate),
            "matched_concepts": ["loss/contrastive"], "prior_runs": prior_runs,
            "prior_runs_total": 65, "prior_runs_omitted": 0,
            "prior_runs_complete": True,
            "concept_source": {"source_complete": True},
        }),
    ]))
    receipt = st.cards[hypothesis_id("bounded prior receipt")].cross_run_prior
    assert len(receipt["prior_runs"]) == 64 and receipt["prior_runs_total"] == 65
    assert receipt["prior_runs_omitted"] >= 1 and receipt["prior_runs_complete"] is False


def test_card_ranked_stamps_priority_on_open_cards_only():
    a = "an open direction with no evidence"
    st = _run(extra=[
        ("hypothesis_added", {"statement": a, "source": "deep_research"}),  # open, no node
        ("card_ranked", {
            "order": [hypothesis_id(a), hypothesis_id("interaction features help")],
            "confidence": 0.75,
        }),
    ])
    assert st.cards[hypothesis_id(a)].priority == 0                 # open -> ranked
    assert st.cards[hypothesis_id(a)].confidence == 0.75
    assert st.cards[hypothesis_id(a)].pinned is False
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
    assert c.research_origin == "memo-9"
    assert c.provenance_tier is None  # a free-form enrichment cannot self-certify concept provenance
    assert c.footprint == {"gpus": 2}                              # dict delta applied, string one skipped


def test_card_enriched_cannot_overwrite_load_bearing_fields():
    # The allow-list is the ONLY thing protecting the derived fields: a delta naming a REAL Card field
    # that is NOT in the allow-list (verdict/status/evidence/best_delta/id/statement) must be a no-op —
    # otherwise a hostile/buggy enrichment could silently corrupt the card's derived verdict/evidence.
    cid = hypothesis_id("interaction features help")
    st = _run(extra=[("card_enriched", {"id": cid, "verdict": "abandoned", "status": "dropped",
                                        "evidence": [], "best_delta": 99.0, "statement": "HIJACK"})])
    c = st.cards[cid]
    # node 2 (0.88) improved over its parent (0.80): supported, evidence [2], delta +0.08 — all unchanged.
    assert c.verdict == "supported" and c.evidence == [2] and c.best_delta == pytest.approx(0.08)
    assert c.status == "evaluated" and c.statement == "interaction features help"


def test_card_enriched_bad_numeric_does_not_drop_sibling_fields():
    # The numeric coercions (foresight_rank/confidence) are guarded PER FIELD, so a malformed numeric
    # ordered BEFORE valid string fields must not abort the rest of the delta (key-order independence).
    cid = hypothesis_id("interaction features help")
    st = _run(extra=[("card_enriched", {"id": cid, "confidence": "not-a-float",
                                        "research_origin": "memo-1", "provenance_tier": "authored"})])
    c = st.cards[cid]
    assert c.research_origin == "memo-1" and c.provenance_tier is None and c.confidence is None


def test_card_enriched_deep_values_and_numeric_fields_are_ref_shape_gated():
    cid = "card-bounded"
    st = _fold_with_cursor(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("card_added", {"id": cid, "statement": "bounded"}),
        ("card_enriched", {
            "id": cid,
            "aaa_unknown": {f"unknown-{i}": ["x" * 1_000] for i in range(1_000)},
            "steering_context": [1, {"note": "x" * 1_000}, ["invalid"]],
            "concept_tags": ["model/tree", {"invalid": True}, ["invalid"], "model/tree"],
            "lesson_refs": ["lesson-1", {"invalid": True}],
            "claim_refs": [["invalid"], "claim-1"],
            "cross_run_prior": {f"key-{i}": {"nested": [i]} for i in range(100)},
            "research_origin": "survives-unknown-budget",
            "confidence": float("nan"),
            "foresight_rank": 10_000,
        }),
    ]))
    card = st.cards[cid]
    assert card.steering_context == []
    assert card.concept_tags == ["model/tree"]
    assert card.lesson_refs == ["lesson-1"] and card.claim_refs == ["claim-1"]
    assert card.cross_run_prior is None
    assert card.research_origin == "survives-unknown-budget"
    assert card.confidence is None and card.foresight_rank is None
    assert "aaa_unknown" not in st.cards_enriched[0]


def test_card_enriched_order_comes_only_from_envelope_and_never_payload_seq():
    cid = hypothesis_id("ordered")
    events = [
        Event(seq=1, type="run_started", data={"run_id": "r", "task_id": "t", "direction": "max"}),
        Event(seq=2, type="card_added", data={"id": cid, "statement": "ordered"}),
        Event(seq=10, type="card_enriched",
              data={"id": cid, "research_origin": "older", "_seq": 10_000}),
        Event(seq=11, type="card_enriched",
              data={"id": cid, "research_origin": "newer", "_seq": "not-an-int"}),
    ]
    st = _fold_with_cursor(events)
    assert st.cards[cid].research_origin == "newer"
    # Replay retains only the newest value for a semantic field inside one exact Card lifecycle
    # fence. The authoritative envelope sequence wins; the caller-owned payload `_seq` never does.
    assert [row["_seq"] for row in st.cards_enriched] == [11]


def test_card_ranked_malformed_order_is_bounded_deduplicated_and_owns_rank():
    first, second = "card-first", "card-second"
    events = _mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("card_added", {"id": first, "statement": "first"}),
        ("card_added", {"id": second, "statement": "second"}),
        ("card_enriched", {"id": second, "foresight_rank": 99}),
        ("card_ranked", {"order": [first, first, 7, second]}),
    ])
    st = _fold_with_cursor(events)
    assert st.card_ranking["order"] == [first, second]
    assert st.cards[first].foresight_rank == 0
    assert st.cards[second].foresight_rank == 1

    malformed = _fold_with_cursor(events + _mk([("card_ranked", {"order": 1})]))
    assert malformed.card_ranking["order"] == []
    assert malformed.cards[first].foresight_rank is None
    assert malformed.cards[second].foresight_rank is None


def test_card_rerank_clears_stale_confidence_when_new_snapshot_omits_it():
    first, second = "card-first", "card-second"
    prefix = _mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("card_added", {"id": first, "statement": "first"}),
        ("card_added", {"id": second, "statement": "second"}),
        ("card_ranked", {"order": [first, second], "confidence": 0.8}),
    ])
    ranked = _fold_with_cursor(prefix)
    assert ranked.cards[first].confidence == ranked.cards[second].confidence == 0.8

    reranked = _fold_with_cursor(prefix + _mk([("card_ranked", {"order": [second]})]))
    assert reranked.cards[first].confidence is None
    assert reranked.cards[second].confidence is None
    assert reranked.cards[first].foresight_rank is None
    assert reranked.cards[second].foresight_rank == 0


def test_huge_card_confidence_is_ignored_without_overflowing_fold():
    huge = 10 ** 400
    st = _fold_with_cursor(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("card_added", {"id": "card-huge", "statement": "huge confidence"}),
        ("card_enriched", {"id": "card-huge", "confidence": huge,
                           "research_origin": "still-applies"}),
        ("card_ranked", {"order": ["card-huge"], "confidence": huge}),
    ]))
    assert st.cards["card-huge"].confidence is None
    assert st.cards["card-huge"].research_origin == "still-applies"
    assert "confidence" not in st.card_ranking


def test_staged_native_card_suppresses_hash_twin_backfills_action_and_bridges_controls():
    statement = "one queued direction"
    legacy_id = hypothesis_id(statement)
    ranked = _fold_with_cursor(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("card_added", {"id": "card-7", "statement": statement, "source": "engine"}),
        ("hypothesis_added", {"statement": statement, "source": "deep_research"}),
        ("card_ranked", {"order": [legacy_id, "card-7", "card-7"]}),
    ]))
    assert ranked.cards["card-7"].foresight_rank == 0

    base_events = _mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("card_added", {"id": "card-7", "statement": statement, "source": "engine"}),
        ("hypothesis_added", {"statement": statement, "source": "deep_research"}),
        ("node_created", {"node_id": 3, "operator": "draft",
                          "idea": {"operator": "draft"}}),
        ("node_created", {"node_id": 7, "operator": "improve", "parent_ids": [3],
                          "idea": {"operator": "improve", "hypothesis": statement,
                                   "card_id": "card-7", "params": {"lr": 0.1},
                                   "space": {"depth": [2, 4]}, "eval_profile": "smoke",
                                   "concepts": ["model/tree"]}}),
        ("card_ranked", {"order": [legacy_id, "card-7", "card-7"]}),
    ])
    events = base_events + _mk([
        ("hypothesis_updated", {"id": legacy_id, "status": "abandoned"}),
    ])
    st = _fold_with_cursor(events)
    assert set(st.cards) == {"card-7"}
    card = st.cards["card-7"]
    assert card.evidence == [7] and card.operator == "improve"
    assert card.params == {"lr": 0.1} and card.space == {"depth": [2.0, 4.0]}
    assert card.eval_profile == "smoke" and card.concept_tags == ["model/tree"]
    assert card.parent_id == 3 and card.parent_ids == [3]
    assert card.verdict == "abandoned" and card.actionable is False

    deleted = _fold_with_cursor(base_events + _mk([
        ("hypothesis_updated", {"id": legacy_id, "status": "deleted"}),
    ]))
    assert deleted.cards == {}


def test_thin_card_action_backfill_is_one_atomic_earliest_node_snapshot():
    events = _mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("card_added", {"id": "card-one", "statement": "one card"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "one card",
                                   "card_id": "card-one"}}),
        ("node_created", {"node_id": 2, "operator": "improve", "parent_ids": [1],
                          "idea": {"operator": "improve", "hypothesis": "one card",
                                   "card_id": "card-one", "params": {"x": 2},
                                   "space": {"depth": [3, 5]}, "eval_profile": "full",
                                   "concepts": ["model/tree"]}}),
    ])
    card = _fold_with_cursor(events).cards["card-one"]
    assert card.evidence == [1, 2]
    assert card.operator == "draft"
    assert card.params == {} and card.space == {} and card.eval_profile is None
    assert card.concept_tags == [] and card.parent_id is None and card.parent_ids == []


def test_native_seed_bridge_composes_with_legacy_merge_and_alias_controls():
    a, b = "native direction A", "native direction B"
    ha, hb = hypothesis_id(a), hypothesis_id(b)
    base = _mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("card_added", {"id": "card-a", "statement": a}),
        ("card_added", {"id": "card-b", "statement": b}),
        ("hypothesis_added", {"statement": a}),
        ("hypothesis_added", {"statement": b}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": a, "card_id": "card-a"}}),
        ("node_created", {"node_id": 2, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": b, "card_id": "card-b"}}),
        ("hypothesis_merged", {"canonical": hb, "aliases": [ha]}),
        ("card_enriched", {"id": ha, "research_origin": "memo-through-alias"}),
        ("card_dropped", {"id": ha, "reason": "merged", "dropped_by": "engine"}),
        ("hypothesis_updated", {"id": ha, "status": "abandoned"}),
    ])
    st = _fold_with_cursor(base)
    assert set(st.cards) == {"card-b"}
    assert st.cards["card-b"].evidence == [1, 2]
    assert st.cards["card-b"].verdict == "abandoned"
    assert st.cards["card-b"].research_origin == "memo-through-alias"
    assert st.cards["card-b"].status == "dropped"

    deleted = _fold_with_cursor(base + _mk([
        ("hypothesis_updated", {"id": ha, "status": "deleted"}),
    ]))
    assert deleted.cards == {}


def test_ambiguous_native_ids_preserve_both_and_do_not_guess_legacy_hash_binding():
    legacy_id = hypothesis_id("same seed")
    events = _mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("card_added", {"id": "card-first", "statement": "same seed"}),
        ("card_added", {"id": "card-second", "statement": "same seed"}),
        ("hypothesis_added", {"statement": "same seed"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "same seed",
                                   "card_id": "card-second"}}),
        ("card_enriched", {"id": "card-second", "research_origin": "memo-1"}),
        ("card_enriched", {"id": legacy_id, "research_origin": "must-not-guess"}),
        ("hypothesis_updated", {"id": legacy_id, "status": "abandoned"}),
        ("card_ranked", {"order": [legacy_id]}),
    ])
    st = _fold_with_cursor(events)
    assert set(st.cards) == {"card-first", "card-second"}
    assert st.cards["card-first"].evidence == []
    assert st.cards["card-second"].evidence == [1]
    assert st.cards["card-second"].research_origin == "memo-1"
    assert all(card.verdict != "abandoned" and card.actionable for card in st.cards.values())
    assert all(card.foresight_rank is None for card in st.cards.values())


def test_native_id_reused_for_two_seeds_fails_closed_instead_of_conflating_cards():
    st = _fold_with_cursor(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("card_added", {"id": "card-shared", "statement": "seed A"}),
        ("card_added", {"id": "card-shared", "statement": "seed B"}),
        ("hypothesis_added", {"statement": "seed A"}),
        ("hypothesis_added", {"statement": "seed B"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "seed A",
                                   "card_id": "card-shared"}}),
        ("node_created", {"node_id": 2, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "seed B",
                                   "card_id": "card-shared"}}),
    ]))
    assert st.cards == {}  # both raw card_added rows remain the audit receipt; no identity is guessed


def test_node_only_native_id_reuse_is_detected_before_evidence_can_be_conflated():
    st = _fold_with_cursor(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "node seed A",
                                   "card_id": "card-shared"}}),
        ("node_created", {"node_id": 2, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "node seed B",
                                   "card_id": "card-shared"}}),
    ]))
    assert st.cards == {}


def test_short_hash_collision_uses_full_statement_identity_and_suppresses_ambiguous_controls():
    first = (
        "same collision slug that exceeds forty eight chars abcdefghijklmnopqrstuvwxyz "
        "zkv1b41rqyc5sj96kmgg"
    )
    second = (
        "same collision slug that exceeds forty eight chars abcdefghijklmnopqrstuvwxyz "
        "djo35wgfbw9l72jaw2h8"
    )
    short_id = hypothesis_id(first)
    assert short_id == hypothesis_id(second)
    assert hypothesis_statement_digest(first) != hypothesis_statement_digest(second)

    st = _fold_with_cursor(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("card_added", {"id": "card-first", "statement": first}),
        ("card_added", {"id": "card-second", "statement": second}),
        ("hypothesis_updated", {"id": short_id, "status": "abandoned"}),
        ("card_dropped", {"id": short_id, "reason": "ambiguous legacy control"}),
    ]))
    assert set(st.cards) == {"card-first", "card-second"}
    assert {card.statement for card in st.cards.values()} == {first, second}
    assert all(card.verdict == "open" and card.status == "proposed" for card in st.cards.values())


def test_native_id_collision_with_another_seed_hash_preserves_both_without_guessing_controls():
    first = "unrelated alpha idea"
    second = "unrelated beta idea"
    second_hash = hypothesis_id(second)
    st = _fold_with_cursor(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("card_added", {"id": second_hash, "statement": first}),
        ("card_added", {"id": "card-second", "statement": second}),
        ("hypothesis_updated", {"id": second_hash, "status": "abandoned"}),
        ("card_dropped", {"id": second_hash, "reason": "ambiguous spelling"}),
        ("card_enriched", {"id": second_hash, "research_origin": "exact native card control"}),
        ("card_ranked", {"order": [second_hash]}),
    ]))
    assert set(st.cards) == {second_hash, "card-second"}
    assert st.cards[second_hash].statement == first
    assert st.cards["card-second"].statement == second
    # The legacy hypothesis control is ambiguous and fails closed, while card-native event types name
    # the explicit native id exactly and therefore still apply to the alpha card.
    assert st.cards[second_hash].status == "dropped" and st.cards[second_hash].verdict == "open"
    assert st.cards[second_hash].research_origin == "exact native card control"
    assert st.cards[second_hash].priority == 0 and st.cards[second_hash].foresight_rank == 0
    assert st.cards["card-second"].verdict == "open"
    assert st.cards["card-second"].priority is None


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


def test_priority_falls_back_to_hypothesis_ranking():
    # No card_ranked producer exists yet, so hypothesis_ranking is the SOLE live card-priority path in
    # Layer 1b: a hypothesis_ranked event ranks the OPEN (evidence-free) card and leaves the rest unpinned.
    a = "an open direction with no evidence"
    order = [hypothesis_id(a), hypothesis_id("interaction features help")]
    st = _run(extra=[
        ("hypothesis_added", {"statement": a, "source": "deep_research"}),   # open, no node
        ("hypothesis_ranked", {"order": order}),                              # NOT card_ranked
    ])
    assert st.cards[hypothesis_id(a)].priority == 0                          # open card ranked first
    # every other card (all carry evidence) keeps no ranking-derived priority
    for cid, card in st.cards.items():
        if cid != hypothesis_id(a):
            assert card.priority is None


# --- Layer 1c: the actionable exclusion seam ------------------------------------------------------

def test_actionable_excludes_dropped_gated_and_abandoned_cards():
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        # proposed (no node) + evaluated (a clean result) are actionable
        ("card_added", {"id": "card-fresh", "statement": "fresh", "source": "operator"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "clean"}}),
        ("node_evaluated", {"node_id": 1, "metric": 0.8}),
        # gated (evidence infeasible/trust-gated) is NOT actionable
        ("node_created", {"node_id": 2, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "leaky"}}),
        ("node_evaluated", {"node_id": 2, "metric": 0.9, "violations": ["used test set"]}),
        # abandoned is NOT actionable
        ("hypothesis_added", {"statement": "dead direction", "source": "human"}),
        ("hypothesis_updated", {"id": hypothesis_id("dead direction"), "status": "abandoned"}),
        # dropped is NOT actionable
        ("node_created", {"node_id": 3, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "dropped one"}}),
        ("node_evaluated", {"node_id": 3, "metric": 0.7}),
        ("card_dropped", {"id": hypothesis_id("dropped one"), "reason": "superseded"}),
    ]))
    # Poison every card's flag to the WRONG value, then re-derive: now BOTH the True and the False
    # assertions genuinely prove step 8 executed (a bare read would pass against the True default).
    for c in st.cards.values():
        c.actionable = not c.actionable
    _derive_cards(st)
    act = {c.statement: c.actionable for c in st.cards.values()}
    assert act["fresh"] is True and act["clean"] is True
    assert act["leaky"] is False and act["dead direction"] is False and act["dropped one"] is False


def test_running_card_is_actionable():
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "in flight"}}),   # pending
    ]))
    c = st.cards[hypothesis_id("in flight")]
    assert c.status == "running"
    # Poison the default so the assertion actually proves step 8 RE-derived it (a bare `is True` would
    # pass even if the whole derivation were deleted — Card.actionable defaults True).
    c.actionable = False
    _derive_cards(st)
    assert st.cards[hypothesis_id("in flight")].actionable is True


def test_tolerates_a_card_whose_only_node_was_superseded():
    # The Layer-5 freshness gate drops a stale speculation via node_failed(reason='superseded'). Such a
    # card must fold CLEANLY (no crash). A superseded/failed node is NOT a Layer-1c exclusion lane: with
    # no usable evidence the card lands in 'evaluated'/verdict 'open' and stays ACTIONABLE — the seam
    # excludes only dropped/gated/abandoned. (Suppressing a stale speculation is L5's job, done at the
    # NODE level via the freshness gate, not through this card flag.)
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "operator": "improve", "parent_ids": [],
                          "idea": {"operator": "improve", "hypothesis": "stale speculation"}}),
        ("node_failed", {"node_id": 1, "reason": "superseded", "eval_seconds": 0}),
    ]))
    c = st.cards[hypothesis_id("stale speculation")]
    assert c.status == "evaluated" and c.verdict == "open" and c.actionable is True


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
    assert st.cards[cid].footprint == {"gpus": 8, "proposed_by": "researcher"}
    assert st.cards[cid].resource_pin == {"gpus": 1, "pinned_by": "operator"}


def test_grouped_beliefs_aggregates_same_seed_cards_keeping_ids_distinct():
    # Peer review: the board keeps 1 card = 1 work item, but two cards reusing the exact hypothesis
    # wording are ONE belief. grouped_beliefs() groups them by seed hash (additively, without merging the
    # cards) so a consumer reads their evidence + verdict together instead of fragmented across cards.
    from looplab.core.models import Card, RunState, hypothesis_id
    seed = "log-transform the skewed target"
    other = "add class weights"
    st = RunState(direction="max", goal="g")
    st.cards = {
        "card-1": Card(id="card-1", seed_statement=seed, statement=seed, verdict="tested",
                       evidence=[1, 2], status="evaluated"),
        "card-2": Card(id="card-2", seed_statement=seed, statement=seed + " (v2)", verdict="supported",
                       evidence=[2, 3], status="evaluated"),
        "card-9": Card(id="card-9", seed_statement=other, statement=other, verdict="open",
                       evidence=[], status="proposed"),
    }
    groups = grouped_beliefs(st)
    assert len(groups) == 2                                   # two distinct beliefs
    belief = next(g for g in groups if g["seed_hash"] == hypothesis_id(seed))
    assert belief["card_ids"] == ["card-1", "card-2"]         # work items stay DISTINCT, grouped together
    assert belief["evidence"] == [1, 2, 3]                    # ordered union of member evidence
    assert belief["verdict"] == "supported"                  # strength roll-up (supported > tested)
    assert belief["statements"] == [seed, seed + " (v2)"]

    # a merged-away alias never appears; a belief whose EVERY member is abandoned rolls up to abandoned
    st.cards["card-2"].merged_into = "card-1"
    st.cards["card-1"].verdict = "abandoned"
    solo = next(g for g in grouped_beliefs(st) if g["seed_hash"] == hypothesis_id(seed))
    assert solo["card_ids"] == ["card-1"] and solo["verdict"] == "abandoned"


def test_open_research_beliefs_dedups_same_seed_untested_cards():
    # Peer review: two work-item cards reusing the exact hypothesis wording are ONE belief; the proposal
    # feed + foresight must see it ONCE (first-seen no-evidence card as the representative), not as
    # indistinguishable duplicates the model re-reads and re-ranks.
    from looplab.core.models import Card, RunState
    seed, other = "log-transform the skewed target", "add class weights"
    st = RunState(direction="max", goal="g")
    st.cards = {
        "card-1": Card(id="card-1", seed_statement=seed, statement=seed, verdict="open",
                       status="proposed", evidence=[]),
        "card-2": Card(id="card-2", seed_statement=seed, statement=seed + " v2", verdict="open",
                       status="proposed", evidence=[]),               # same seed -> same belief
        "card-9": Card(id="card-9", seed_statement=other, statement=other, verdict="open",
                       status="proposed", evidence=[]),
    }
    assert [c.id for c in st.open_research_beliefs()] == ["card-1", "card-9"]   # one per belief, 1st rep
    # a same-seed card WITH evidence is skipped so the untested sibling represents the belief
    st.cards["card-1"].evidence = [1]
    assert [c.id for c in st.open_research_beliefs()] == ["card-2", "card-9"]


def test_grouped_beliefs_keys_by_full_digest_not_the_short_hash():
    # Peer review: two DISTINCT statements can collide on the short `hypothesis_id`; grouping on it would
    # silently merge unrelated beliefs. Group by the full statement digest instead.
    from looplab.core.models import (Card, RunState, hypothesis_id,
                                     hypothesis_statement_digest)
    first = ("same collision slug that exceeds forty eight chars abcdefghijklmnopqrstuvwxyz "
             "zkv1b41rqyc5sj96kmgg")
    second = ("same collision slug that exceeds forty eight chars abcdefghijklmnopqrstuvwxyz "
              "djo35wgfbw9l72jaw2h8")
    assert hypothesis_id(first) == hypothesis_id(second)                       # short-hash collision...
    assert hypothesis_statement_digest(first) != hypothesis_statement_digest(second)   # ...distinct digest
    st = RunState(direction="max", goal="g")
    st.cards = {
        "card-1": Card(id="card-1", seed_statement=first, statement=first, verdict="open", evidence=[]),
        "card-2": Card(id="card-2", seed_statement=second, statement=second, verdict="open", evidence=[]),
    }
    groups = grouped_beliefs(st)
    assert len(groups) == 2                                                    # NOT merged into one belief
    assert {g["seed_digest"] for g in groups} == {hypothesis_statement_digest(first),
                                                  hypothesis_statement_digest(second)}


def test_grouped_beliefs_excludes_an_abandoned_members_evidence_from_the_union():
    # Peer review: the verdict roll-up drops an abandoned member; its evidence must drop from the union
    # too, so `evidence` and `verdict` describe the SAME (live, non-abandoned) set.
    from looplab.core.models import Card, RunState
    seed = "shared belief across two work items"
    st = RunState(direction="max", goal="g")
    st.cards = {
        "card-1": Card(id="card-1", seed_statement=seed, statement=seed, verdict="abandoned",
                       evidence=[1, 2], status="proposed"),
        "card-2": Card(id="card-2", seed_statement=seed, statement=seed, verdict="open",
                       evidence=[3], status="proposed"),
    }
    [group] = grouped_beliefs(st)
    assert group["card_ids"] == ["card-1", "card-2"]     # both are still listed as members
    assert group["evidence"] == [3]                       # the abandoned member's [1, 2] is excluded
    assert group["verdict"] == "open"                     # verdict + evidence describe the live member


def test_grouped_beliefs_verdict_matches_evidence_verdict_on_the_union():
    # Peer review: `testing` (a still-running experiment) must OUTRANK `tested` (finished, no improvement)
    # so the group is not reported "done" while an experiment is live — matching `_evidence_verdict`'s
    # precedence on the unioned evidence rather than the old `tested > testing` label max.
    from looplab.core.models import Card, RunState
    seed = "shared belief across two work items"
    st = RunState(direction="max", goal="g")
    st.cards = {
        "card-1": Card(id="card-1", seed_statement=seed, statement=seed, verdict="tested",
                       evidence=[1], status="evaluated"),
        "card-2": Card(id="card-2", seed_statement=seed, statement=seed, verdict="testing",
                       evidence=[2], status="pending"),
    }
    [group] = grouped_beliefs(st)
    assert group["verdict"] == "testing"          # active experiment not hidden by the finished sibling


# --------------------------------------------------------------------------------------------------
# The research-direction facet's OWN identity: `Card.belief_id` + `Card.retry_of`.
#
# The defect these drive was measured live in `runs/rubert-dr-0807`: card-0 (draft) and card-1 (debug)
# byte-identical in statement, rationale, all six params and footprint, differing ONLY in
# `idea.operator` — two work items, evidence [0] and [1], rendering as two identical hypotheses.
# Every test below folds a REAL receipt-bound event sequence rather than assembling `RunState.cards`
# by hand: the properties are about what a NATIVE card derives, and a faked receipt would leave every
# row a non-selectable shadow and make the assertions vacuous.
# --------------------------------------------------------------------------------------------------

_RETRY_STATEMENT = ("Teacher-mined hard negatives filtered by a positive-aware 0.95 threshold raise "
                    "test recall@100 by removing the false negatives that cap in-batch-only training.")


def _receipted_card_added(card_id, statement, *, operator, parents=(), at_node=0, params=None):
    """One native `card_added` in the shape `_card_added_payload` writes, with a REAL receipt."""
    parent_ids = list(parents)
    action = {
        "operator": operator,
        "params": dict(params or {}),
        "space": {},
        "eval_profile": None,
        "eval_timeout": None,
        "parent_id": parent_ids[0] if parent_ids else None,
        "parent_ids": parent_ids,
        "parent_generations": {str(parent): 0 for parent in parent_ids},
        "scored_against": None,
        "scored_against_generation": None,
        "scored_against_empty": True,
        "footprint": None,
    }
    receipt = card_ownership_receipt(card_id, statement, action)
    assert receipt is not None
    return ("card_added", {
        "id": card_id, "statement": statement, "source": "researcher", "at_node": at_node,
        "rationale": "",
        "idea": {"operator": operator, "params": action["params"], "space": {},
                 "eval_profile": None, "eval_timeout": None},
        "parent_id": action["parent_id"], "parent_ids": parent_ids,
        "parent_generations": action["parent_generations"],
        "scored_against": None, "scored_against_generation": None, "scored_against_empty": True,
        "footprint": None, "steering_context": [], "ownership_receipt": receipt,
    })


def _node_for_card(node_id, card_id, statement, *, operator, parents=()):
    """The node row that CLAIMS a card — `idea.card_id` is what makes it that card's work item."""
    return ("node_created", {
        "node_id": node_id, "operator": operator, "parent_ids": list(parents),
        "idea": {"operator": operator, "hypothesis": statement, "card_id": card_id, "params": {}},
    })


def _retry_run(second_operator="debug"):
    """A draft card whose node FAILED, then a second card on that failed node reusing its wording.

    With ``second_operator='debug'`` this is exactly the live `runs/rubert-dr-0807` shape.
    """
    return fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        _receipted_card_added("card-0", _RETRY_STATEMENT, operator="draft", at_node=0),
        _node_for_card(0, "card-0", _RETRY_STATEMENT, operator="draft"),
        ("node_failed", {"node_id": 0, "reason": "lightning_ddp", "eval_seconds": 1}),
        # The repair path copies the PARENT's Idea verbatim and flips only `operator`
        # (`engine/orchestrator.py::_prepare_node_idea`), so the statement is byte-identical.
        _receipted_card_added("card-1", _RETRY_STATEMENT, operator=second_operator,
                              parents=(0,), at_node=1),
        _node_for_card(1, "card-1", _RETRY_STATEMENT, operator=second_operator, parents=(0,)),
    ]))


def test_debug_retry_is_one_belief_across_two_distinct_work_items():
    # THE reported defect. The two cards must stay two work items with two distinct action digests —
    # a debug build genuinely IS a different executable action — while the belief facet reports them
    # as ONE hypothesis whose evidence is both nodes.
    st = _retry_run()
    card0, card1 = st.cards["card-0"], st.cards["card-1"]

    assert card0.identity.kind == "native" and card1.identity.kind == "native"
    assert card0.identity.action_digest != card1.identity.action_digest   # work items stay DISTINCT
    assert card0.evidence == [0] and card1.evidence == [1]

    # …and the belief facet joins them.
    assert card0.belief_id == card1.belief_id is not None
    assert card1.retry_of == "card-0"          # the debug card names the card it retries
    assert card0.retry_of is None              # a draft retries nothing

    [group] = grouped_beliefs(st)
    assert group["card_ids"] == ["card-0", "card-1"]
    assert group["evidence"] == [0, 1]         # the shape the operator should have seen all along


def test_retry_of_is_not_invented_from_shared_wording():
    # Only `debug` is a retry. `improve`/`merge` also name a parent NODE, but they propose a new point
    # in the space; linking those would claim every child re-runs its parent's question. Same events,
    # same statement, same parent — only the operator differs, and the retry edge must vanish.
    st = _retry_run(second_operator="improve")
    assert st.cards["card-1"].retry_of is None
    assert st.cards["card-1"].operator == "improve"
    # It is still one BELIEF (identical wording), which is exactly the distinction being drawn: same
    # question, genuinely different experiment.
    assert st.cards["card-0"].belief_id == st.cards["card-1"].belief_id
    [group] = grouped_beliefs(st)
    assert group["card_ids"] == ["card-0", "card-1"]


def test_retry_of_needs_the_parent_nodes_own_card_claim_not_its_evidence_membership():
    # The edge is resolved through the parent NODE ROW's own `idea.card_id` — the same "its own work
    # item" notion step 9 builds for the debug exemption — and deliberately NOT through
    # `Card.evidence`, which is a WIDER set: the legacy statement-hash join attaches a node to a card
    # by hypothesis wording alone, and such a node is evidence that was never that card's work item.
    #
    # Node 1 below is exactly that: it re-states card-0's hypothesis, names no card of its own, and so
    # lands in card-0's evidence. card-1 debugs it. Reading the owner map off `evidence` would report
    # `card-1 retries card-0`, which card-0 never authored — a lineage claim invented out of wording.
    other = "a completely different research direction with its own wording"
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        _receipted_card_added("card-0", _RETRY_STATEMENT, operator="draft", at_node=0),
        _node_for_card(0, "card-0", _RETRY_STATEMENT, operator="draft"),
        ("node_evaluated", {"node_id": 0, "metric": 0.5}),
        # node 1 states card-0's hypothesis with NO card_id of its own -> hash-joined evidence only.
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": _RETRY_STATEMENT}}),
        ("node_failed", {"node_id": 1, "reason": "boom", "eval_seconds": 1}),
        # …and the debug card carries its OWN wording, so it is not hash-joined into card-0 either.
        _receipted_card_added("card-1", other, operator="debug", parents=(1,), at_node=2),
        _node_for_card(2, "card-1", other, operator="debug", parents=(1,)),
    ]))
    assert st.cards["card-0"].evidence == [0, 1]          # node 1 IS card-0's evidence…
    assert st.cards["card-1"].operator == "debug" and st.cards["card-1"].parent_ids == [1]
    assert st.cards["card-1"].retry_of is None            # …and still NOT its work item


def test_retry_of_follows_a_merged_owner_to_its_canonical_survivor():
    # The owner map is canonicalized through `_canon`, like every other id join in the ledger. Without
    # it a debug card whose parent node claims a card that consolidation later merged away would report
    # the DEAD alias — which, because merged aliases are dropped from `st.cards`, silently degrades to
    # no edge at all. The survivor is the honest answer and the one a board can render.
    other = "an independently authored card that consolidation keeps"
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        _receipted_card_added("card-0", _RETRY_STATEMENT, operator="draft", at_node=0),
        _node_for_card(0, "card-0", _RETRY_STATEMENT, operator="draft"),
        ("node_failed", {"node_id": 0, "reason": "boom", "eval_seconds": 1}),
        _receipted_card_added("card-2", other, operator="draft", at_node=1),
        _node_for_card(1, "card-2", other, operator="draft"),
        ("node_evaluated", {"node_id": 1, "metric": 0.4}),
        # card-0 is consolidated INTO card-2; node 0 still names card-0 on its own row.
        ("card_merged", {"canonical": "card-2", "aliases": ["card-0"]}),
        _receipted_card_added("card-1", _RETRY_STATEMENT, operator="debug", parents=(0,), at_node=2),
        _node_for_card(2, "card-1", _RETRY_STATEMENT, operator="debug", parents=(0,)),
    ]))
    assert "card-0" not in st.cards                # the alias is gone from the board…
    assert st.cards["card-1"].retry_of == "card-2"  # …and the edge follows it to the survivor


def test_belief_id_is_the_full_digest_and_is_what_the_belief_views_read():
    # The key is the FULL sha256, never the short display `hypothesis_id`: two distinct statements can
    # share a short id, and keying on that would silently merge unrelated beliefs.
    st = _retry_run()
    assert st.cards["card-0"].belief_id == hypothesis_statement_digest(_RETRY_STATEMENT)
    assert st.cards["card-0"].belief_id != hypothesis_id(_RETRY_STATEMENT)

    # Non-vacuity for the "one spelling" claim: re-point ONE card's published belief_id and both views
    # must follow it. A view that re-derived the digest from `seed_statement` would ignore this and
    # keep reporting a single group — which is precisely the drift the published field removes.
    st.cards["card-1"].belief_id = "sha256-of-some-other-belief"
    assert [g["card_ids"] for g in grouped_beliefs(st)] == [["card-0"], ["card-1"]]
    # `open_research_beliefs` keys on the same field; give both cards an open, untested lane first.
    for card in st.cards.values():
        card.verdict, card.status, card.evidence = "open", "proposed", []
    assert [c.id for c in st.open_research_beliefs()] == ["card-0", "card-1"]
    st.cards["card-1"].belief_id = st.cards["card-0"].belief_id
    assert [c.id for c in st.open_research_beliefs()] == ["card-0"]      # collapsed to ONE belief


def test_belief_lineage_leaves_the_action_receipt_exactly_where_it_was():
    # The invariant the whole change is bounded by: the action digest must keep binding the executable
    # identity EXACTLY, and a card that cannot be claimed must still be refused at the mint. Rebuild
    # each native card's claim action with the ENGINE's own constructor and re-derive its receipt —
    # this is the same round trip `_prepare_existing_card_claim` performs before a claim is allowed.
    from looplab.engine.card_reservation import CardReservationMixin

    st = _retry_run()
    for card in st.cards.values():
        assert card.identity.kind == "native"
        action = CardReservationMixin._card_claim_receipt_action(card)
        assert card_action_digest(card.id, card.seed_statement, action) == card.identity.action_digest
        assert card_ownership_receipt(card.id, card.seed_statement, action) == {
            "v": 2, "card_id": card.id, "action_digest": card.identity.action_digest}

    # …and neither new field is in either frozen digest preimage, so no already-issued receipt moves.
    for fields in (CARD_ACTION_DIGEST_V1_FIELDS, CARD_ACTION_DIGEST_V2_FIELDS):
        assert "belief_id" not in fields and "retry_of" not in fields


def test_grouped_beliefs_unions_evidence_in_ascending_node_order():
    # Every per-card `evidence` list is ascending (`_link_cards_to_nodes` walks `sorted(st.nodes)`).
    # The union used to inherit `st.cards` order, which is lexicographic on card ids, so a belief held
    # by card-9 and card-10 published `evidence: [10, 9]` — measured on `runs/spec-live-0804`.
    from looplab.core.models import Card, RunState
    seed = "one belief carried by two lexicographically misordered work items"
    st = RunState(direction="max", goal="g")
    st.cards = {                                  # card-10 FIRST, exactly as dict ordering delivers it
        "card-10": Card(id="card-10", seed_statement=seed, statement=seed, verdict="tested",
                        evidence=[10], status="evaluated"),
        "card-9": Card(id="card-9", seed_statement=seed, statement=seed, verdict="tested",
                       evidence=[9], status="evaluated"),
    }
    [group] = grouped_beliefs(st)
    assert group["card_ids"] == ["card-10", "card-9"]   # membership stays in board (first-seen) order
    assert group["evidence"] == [9, 10]                 # …the node ids do not


# --- the score fence's truth table -------------------------------------------------------------
#
# `card_score_fence_state` is the ONE spelling of the score half of the Card freshness fence, shared
# by the fold (`events/card_ledger.py::_card_action_freshness`) and the Layer-5 recheck
# (`search/card_selection.py::_card_generation_fences_current`). Before it was hoisted, the rule
# existed only as two hand-synced inline expressions over `RunState`, so no test could state a single
# row of it — and the champion-equality clause it carried sat there unreviewed until it had idled a
# GPU for a whole run. Stating it is the point; keep this table total over the branches.
@pytest.mark.parametrize(
    ("case", "kwargs", "expected"),
    [
        # --- no incumbent at mint time.
        ("legacy row with no fence at all",
         dict(scored_against=None, scored_against_generation=None, scored_against_empty=False,
              anchor_live=False, anchor_attempt=None, board_empty=True), "unknown"),
        ("complete empty authority on a still-empty board",
         dict(scored_against=None, scored_against_generation=None, scored_against_empty=True,
              anchor_live=False, anchor_attempt=None, board_empty=True), "current"),
        # Deliberately asymmetric with the incumbent branch below — the helper's docstring owns why.
        ("complete empty authority once a champion exists",
         dict(scored_against=None, scored_against_generation=None, scored_against_empty=True,
              anchor_live=False, anchor_attempt=None, board_empty=False), "stale"),
        ("empty authority claiming an anchor generation is malformed",
         dict(scored_against=None, scored_against_generation=0, scored_against_empty=True,
              anchor_live=False, anchor_attempt=None, board_empty=True), "stale"),
        ("a non-bool empty marker is not an empty authority",
         dict(scored_against=None, scored_against_generation=None, scored_against_empty="yes",
              anchor_live=False, anchor_attempt=None, board_empty=True), "unknown"),
        # --- with an incumbent: an ANCHOR liveness question, and nothing else.
        ("live anchor at the exact attempt it was scored on",
         dict(scored_against=1, scored_against_generation=0, scored_against_empty=False,
              anchor_live=True, anchor_attempt=0, board_empty=False), "current"),
        ("…and still current after some other node became champion",
         dict(scored_against=1, scored_against_generation=0, scored_against_empty=False,
              anchor_live=True, anchor_attempt=0, board_empty=False), "current"),
        ("dead anchor: missing, tombstoned or aborted",
         dict(scored_against=1, scored_against_generation=0, scored_against_empty=False,
              anchor_live=False, anchor_attempt=None, board_empty=False), "stale"),
        ("the anchor was reset and re-ran under a new attempt",
         dict(scored_against=1, scored_against_generation=0, scored_against_empty=False,
              anchor_live=True, anchor_attempt=1, board_empty=False), "stale"),
        ("legacy row with an anchor but no generation",
         dict(scored_against=1, scored_against_generation=None, scored_against_empty=False,
              anchor_live=True, anchor_attempt=0, board_empty=False), "unknown"),
        ("a non-int generation can never match an attempt",
         dict(scored_against=1, scored_against_generation=True, scored_against_empty=False,
              anchor_live=True, anchor_attempt=1, board_empty=False), "stale"),
    ],
)
def test_card_score_fence_state_truth_table(case, kwargs, expected):
    from looplab.core.models import card_score_fence_state

    positional = (
        kwargs.pop("scored_against"),
        kwargs.pop("scored_against_generation"),
        kwargs.pop("scored_against_empty"),
    )
    assert card_score_fence_state(*positional, **kwargs) == expected, case
