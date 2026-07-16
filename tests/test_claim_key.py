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
