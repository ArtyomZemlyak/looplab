"""Structured semantic CLAIM key (§21.20.13) — the scope+polarity-safe identity that replaces the lean
fuzzy merge. Pins: paraphrase/inflection collapse, POLARITY separates opposite assertions, SCOPE separates
same-worded claims across tasks, and the O(n) exact-key grouping never bridges transitively.
"""
from __future__ import annotations

from looplab.engine.claim_key import CLAIM_KEY_VERSION, claim_signature, claim_uid


def test_paraphrase_and_inflection_share_a_merge_key():
    a = claim_signature("hard negative mining improves recall")
    b = claim_signature("hard negative mining improves recall greatly")   # degree adverb dropped
    c = claim_signature("hard negative mining improved recall")           # tense folded by the stemmer
    assert a["merge_key"] == b["merge_key"] == c["merge_key"]
    assert a["polarity"] == 1


def test_polarity_separates_opposite_assertions_but_shares_contra_key():
    pos = claim_signature("dropout improves model generalization")
    neg = claim_signature("dropout never improves model generalization")
    assert pos["merge_key"] != neg["merge_key"]      # opposite assertions do NOT merge
    assert pos["contra_key"] == neg["contra_key"]    # ...but they are contradiction partners
    assert pos["polarity"] == 1 and neg["polarity"] == -1


def test_scope_separates_same_words_across_tasks():
    a = claim_signature("distillation helps", scope="retrieval")
    b = claim_signature("distillation helps", scope="classification")
    assert a["merge_key"] != b["merge_key"] and a["uid"] != b["uid"]   # task A != task B (governance-safe)


def test_distinct_subjects_do_not_merge():
    a = claim_signature("hard negative mining improves recall")
    b = claim_signature("hard negative mining improves precision")      # recall != precision
    assert a["merge_key"] != b["merge_key"]


def test_uid_is_stable_and_prefixed():
    assert claim_uid("x helps", scope="t").startswith("clm_")
    assert claim_uid("x helps", scope="t") == claim_uid("x  HELPS", scope="t")   # case/space-insensitive


def test_empty_or_stopword_only_has_no_polarity():
    assert claim_signature("")["polarity"] == 0
    assert claim_signature("the and of to")["polarity"] == 0


def test_nt_ending_words_do_not_flip_polarity():
    # mega-review regression: an optional-apostrophe n't regex would match any word ending in "nt"
    for s in ["gradient clipping improves recall", "the component helps accuracy",
              "consistent augmentation improves recall", "the current setup helps"]:
        assert claim_signature(s)["polarity"] == 1, s
    # real contractions still negate
    assert claim_signature("dropout doesn't help")["polarity"] == -1
    # relation-local prevention is a positive assertion, not a sentential negator
    assert claim_signature("dropout prevents overfitting")["polarity"] == 1


def test_avoid_and_prevent_do_not_invert_positive_effect_assertions():
    avoid = claim_signature("Avoiding overfitting improves generalization")
    prevent = claim_signature("Preventing overfitting improves generalization")
    assert avoid["polarity"] == prevent["polarity"] == 1
    assert avoid["merge_key"] == prevent["merge_key"]
    assert avoid["contra_key"] == prevent["contra_key"]


def test_short_negation_no_still_flips():
    # 'no' is 2 chars (dropped from the SUBJECT) but must still flip POLARITY (mega-review regression)
    assert claim_signature("no improvement from dropout")["polarity"] == -1
    assert claim_signature("improvement from dropout")["polarity"] == 1


def test_causal_roles_prevent_reverse_relation_collision():
    forward = claim_signature("teacher distillation improves student recall")
    reverse = claim_signature("student recall improves teacher distillation")
    assert forward["roles"] != reverse["roles"]
    assert forward["merge_key"] != reverse["merge_key"]
    assert forward["contra_key"] != reverse["contra_key"]


def test_single_letter_causal_roles_are_not_discarded():
    forward = claim_signature("A causes B")
    reverse = claim_signature("B causes A")
    assert forward["roles"] == (("a",), ("b",))
    assert reverse["roles"] == (("b",), ("a",))
    assert forward["contra_key"] != reverse["contra_key"]
    negated = claim_signature("A does not improve B")
    assert negated["roles"] == (("a",), ("b",)) and negated["polarity"] == -1


def test_roles_still_pair_same_direction_opposites():
    improve = claim_signature("augmentation improves retrieval recall")
    degrade = claim_signature("augmentation degrades retrieval recall")
    assert improve["roles"] == degrade["roles"]
    assert improve["contra_key"] == degrade["contra_key"]
    assert improve["polarity"] == 1 and degrade["polarity"] == -1


def test_v3_identity_is_nfkc_normalized_and_metric_qualified():
    assert CLAIM_KEY_VERSION == 3
    normal = claim_signature("FULLWIDTH improves recall", scope="t", metric="recall")
    compat = claim_signature("ＦＵＬＬＷＩＤＴＨ improves recall", scope="t", metric="recall")
    other_metric = claim_signature("FULLWIDTH improves recall", scope="t", metric="precision")
    assert normal["uid"] == compat["uid"]
    assert normal["uid"] != other_metric["uid"]


def test_delimiter_bearing_qualifiers_cannot_alias():
    left = claim_signature("augmentation improves recall", scope="a|b", metric="c")
    right = claim_signature("augmentation improves recall", scope="a", metric="b|c")
    assert left["uid"] != right["uid"] and left["contra_key"] != right["contra_key"]


def test_null_effect_does_not_merge_with_negative_effect():
    # concept-conformance regression (§21.20.5 "hurts != refuted-positive"): a "does NOT improve" (null /
    # refuted-positive) claim must NOT share identity with a "reduces" (supported negative-effect) claim,
    # even though both net to polarity -1. The old single-bit polarity merged them and pooled evidence.
    null = claim_signature("hard negatives do not improve recall")
    harm = claim_signature("hard negatives reduce recall")
    helps = claim_signature("hard negatives improve recall")
    assert null["merge_key"] != harm["merge_key"]              # null-effect never merges into a harm claim
    assert null["polarity"] == harm["polarity"] == -1          # ...though both net negative
    assert null["relation_sign"] == 1 and null["negated"]      # relation=helps, but negated
    assert harm["relation_sign"] == -1 and not harm["negated"]  # relation=hurts, asserted
    # helps vs harm and helps vs not-helps still surface as contradictions (opposite net polarity, same subj)
    assert helps["contra_key"] == harm["contra_key"] == null["contra_key"]
    assert helps["polarity"] == 1


# --- EM-06: the identity modes, and the overlay key each one uses -------------------------------

def test_the_one_identity_is_documented_where_a_reviewer_reads_them():
    """doc 25 EM-06's concrete cost is "operator decisions must overlay correctly across every mode",
    and nothing stated which overlay key guards which mode. The table lives at `claim_assessments`
    because that was the function whose flag selected between them. Three modes became two on
    2026-09-08 when the fuzzy merge was deleted, and two became ONE later the same day when the lean
    read path went; the table is checked for the count it claims, so re-introducing a second identity
    row without a second identity — or the reverse — is red."""
    import inspect

    from looplab.engine.claims_assessments import claim_assessments

    # Check the TABLE, not the function text: the first draft grepped the whole source, and every
    # needle also occurs in the body, so deleting a table row left it green.
    src = inspect.getsource(claim_assessments)
    assert "THE ONE IDENTITY" in src, "the identity table is gone"
    table = [l for l in src.split("\n")
             if l.lstrip().startswith("#") and "  " in l
             and ("normalize_statement grouping" in l or "claim_key.claim_signature" in l)]
    assert len(table) == 1, (
        f"the table lists {len(table)} identities; there is exactly one, and a second row means a "
        "caller flag value is undocumented — which is exactly how an operator decision overlays "
        "the wrong claim")


def test_there_is_one_projection_and_the_retired_keyword_cannot_select_another():
    """The DEFAULT is driven, not pinned, and so is the retirement. Two facts decide what a caller
    gets: the same words in two different TASKS are two claims, and two opposite-polarity assertions
    in one task are a CONTRADICTION rather than two unrelated rows. Passing the retired
    `structured=False` — the lean normalized-statement projection, deleted 2026-09-08 (doc 25
    EM-06) — changes NEITHER, byte for byte.

    That is the EM-06 flip and then its close. The task boundary is the half that matters for
    governance: under the lean projection a caller merged task A's evidence into task B's claim and
    handed the merged row an operator decision — while `record_claim_decision` validated that
    operator's `evidence_digest` against the structured, task-precise projection only."""
    from looplab.engine.claims import claim_assessments

    across_tasks = [{"statement": "dropout helps accuracy", "outcome": "supported",
                     "evidence": [1], "run_id": "rA", "task_id": "A"},
                    {"statement": "dropout helps accuracy", "outcome": "supported",
                     "evidence": [2], "run_id": "rB", "task_id": "B"}]

    default = claim_assessments(across_tasks)
    assert len(default) == 2, "the default projection merged two tasks' claims into one"
    assert sorted(c["scope"] for c in default) == ["A", "B"]
    assert len({c["claim_uid"] for c in default}) == 2, "the default projection carries no scope-precise uid"

    # The keyword survives only because `EngineOptions.cross_run_structured_claims` still reaches
    # here through `proposal_cues`/`strategy` and `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` pins it False.
    # Accepting it is safe ONLY while both values name the same projection, which is what this drives.
    assert claim_assessments(across_tasks, structured=False) == default, (
        "`structured=False` produced a different projection — the lean read path is back, or the "
        "retired keyword selects something again")

    opposed = [{"statement": "dropout helps accuracy", "outcome": "supported",
                "evidence": [1], "run_id": "r", "task_id": "t"},
               {"statement": "dropout never helps accuracy", "outcome": "supported",
                "evidence": [2], "run_id": "r", "task_id": "t"}]
    contested = claim_assessments(opposed)
    assert all(c["contradicts"] for c in contested), "the default projection lost the contradiction"
    assert claim_assessments(opposed, structured=False) == contested


def test_the_lean_shadow_namespace_is_gone_and_the_unscoped_fallback_is_not():
    """`_scoped_key` was the loader index only the lean projection read; `_global_key` is read by
    the STRUCTURED projection's `_decision_for` as its explicitly-unscoped fallback. They were one
    bullet in the finding and they are two different questions, so only one of them left."""
    from looplab.engine import claims, claims_assessments

    for module in (claims, claims_assessments):
        assert not hasattr(module, "_scoped_key"), (
            f"{module.__name__} still exports the deleted lean overlay namespace")
    assert hasattr(claims, "_global_key"), (
        "`_global_key` was deleted with the lean path, but `_decision_for` reads it as the "
        "structured projection's unscoped fallback — its removal is a separate question")


def test_the_fuzzy_paraphrase_merge_is_gone():
    """`_fuzzy_merge_claims` was the third identity mode: an opt-in token-Jaccard merge whose own
    docstring called the structured key its full CR. Deleting it is half of EM-06's close, so the
    kwarg must REFUSE rather than be quietly ignored — a silently-accepted `fuzzy=True` would read
    as "paraphrases still merge" to every caller that still passes it."""
    import pytest

    from looplab.engine import claims, claims_assessments
    from looplab.engine.claims import claim_assessments

    with pytest.raises(TypeError):
        claim_assessments([], fuzzy=True)
    for module in (claims, claims_assessments):
        assert not hasattr(module, "_fuzzy_merge_claims"), (
            f"{module.__name__} still exports the deleted fuzzy merge")
