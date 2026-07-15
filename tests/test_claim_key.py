"""Structured semantic CLAIM key (§21.20.13) — the scope+polarity-safe identity that replaces the lean
fuzzy merge. Pins: paraphrase/inflection collapse, POLARITY separates opposite assertions, SCOPE separates
same-worded claims across tasks, and the O(n) exact-key grouping never bridges transitively.
"""
from __future__ import annotations

from looplab.engine.claim_key import claim_signature, claim_uid


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
    # 'prevent' negates exactly once (was double-counted by the nt-regex before the fix)
    assert claim_signature("dropout prevents overfitting")["polarity"] == -1


def test_short_negation_no_still_flips():
    # 'no' is 2 chars (dropped from the SUBJECT) but must still flip POLARITY (mega-review regression)
    assert claim_signature("no improvement from dropout")["polarity"] == -1
    assert claim_signature("improvement from dropout")["polarity"] == 1
