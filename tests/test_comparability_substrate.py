"""A number produced on a different SOURCE TREE is not on the same scale, and the record says so.

THE GAP THIS CLOSES. `RunState.repair_candidates()` ranks the files a run's nodes keep re-fixing
precisely so an operator will promote one into the editable source repo — measured on
`runs/e5small-dr-unified-v4`, SIX nodes repaired `looplab_stages.json` and FIVE repaired
`vectorsearch/configs/config.yaml`, because a node inherits its PARENT's files and can never inherit
a fix a SIBLING found. Promoting moves the ground every later experiment is measured on, and until
now nothing recorded which side of that move a node ran on: the engine fingerprints the workspace at
`run_started` and compares it again only on RESUME, so a promotion made while the engine keeps
running was invisible to every reader.

WHAT IS NOT DONE HERE, deliberately. No new event type, no operator declaration, no per-node payload
field. The fingerprint is read live at the metric read — the instant that already decides what the
number is about — so two nodes either side of a promotion get different substrates for free, and a
task with no editable repo records none at all.

THE ONE RULE WORTH GETTING RIGHT: the substrate DISCRIMINATES and never CERTIFIES.
"""
from __future__ import annotations

from looplab.engine.comparability import (AUTHORITY_MEASURED, DIFFERENT, SAME, UNKNOWN,
                                          comparability_notice, comparability_record,
                                          comparability_status, group_token)

_TASK = {"eval": {"command": "python -m vectorsearch.test"}}


def _bound_inputs(digest: str) -> dict:
    """An `eval.inputs` record whose every declared input bound — the MEASURED family's material."""
    return {"inputs_bound": True,
            "inputs": [{"bound": True, "kind": "file", "digest": digest, "digest_mode": "full"}]}


def test_the_substrate_rides_the_record_only_when_there_is_one():
    with_tree = comparability_record(task=_TASK, substrate={"repo": "sha-aaa"})
    without = comparability_record(task=_TASK)
    assert with_tree["substrate"]
    assert "substrate" not in without, (
        "a task with no editable repo must record no substrate — absence is `unknown`, never `same`")


def test_a_different_source_tree_refuses_two_otherwise_identical_measurements():
    """The whole point: same command, same bound inputs, same digest — and a promoted fix between
    them. Nothing in the authority families can see that, which is why this lives beside them."""
    inputs = _bound_inputs("d0")
    before = comparability_record(task=_TASK, inputs_prov=inputs, substrate={"repo": "sha-before"})
    after = comparability_record(task=_TASK, inputs_prov=inputs, substrate={"repo": "sha-after"})
    assert before["keys"][AUTHORITY_MEASURED] == after["keys"][AUTHORITY_MEASURED], (
        "the pair must be identical at the strongest authority, or this proves nothing")
    assert comparability_status(before, after) == DIFFERENT


def test_the_substrate_outranks_a_measured_agreement_and_can_only_refuse():
    """It is checked FIRST because a code move invalidates a comparison the data would have blessed.
    The mutation this refuses: checking it after the authority compare, where a `measured` match
    returns SAME before the substrate is ever read."""
    inputs = _bound_inputs("d0")
    a = comparability_record(task=_TASK, inputs_prov=inputs, substrate={"repo": "x"})
    b = comparability_record(task=_TASK, inputs_prov=inputs, substrate={"repo": "y"})
    assert comparability_status(a, b) == DIFFERENT
    # …and matching substrates change NOTHING: equal code with different data is not one evaluation.
    c = comparability_record(task=_TASK, inputs_prov=_bound_inputs("d1"), substrate={"repo": "x"})
    assert comparability_status(a, c) == DIFFERENT, "a shared source tree may not certify anything"


def test_a_matching_substrate_still_needs_a_certifying_authority_to_say_SAME():
    inputs = _bound_inputs("d0")
    a = comparability_record(task=_TASK, inputs_prov=inputs, substrate={"repo": "x"})
    b = comparability_record(task=_TASK, inputs_prov=inputs, substrate={"repo": "x"})
    assert comparability_status(a, b) == SAME
    # The same pair WITHOUT a certifying authority stays `unknown` — the inversion is untouched.
    thin_a = comparability_record(task=_TASK, substrate={"repo": "x"})
    thin_b = comparability_record(task=_TASK, substrate={"repo": "x"})
    assert comparability_status(thin_a, thin_b) == UNKNOWN


def test_a_missing_substrate_is_unknown_and_never_agreement():
    """Every log written before this shipped carries none, and reads exactly as it did."""
    inputs = _bound_inputs("d0")
    keyed = comparability_record(task=_TASK, inputs_prov=inputs, substrate={"repo": "x"})
    legacy = comparability_record(task=_TASK, inputs_prov=inputs)
    assert comparability_status(keyed, legacy) == SAME, (
        "a one-sided substrate must not refuse a pair its authorities agree on")
    assert comparability_status(legacy, legacy) == SAME


def test_the_refusal_names_the_source_tree_and_not_a_phantom_authority():
    """The `DIFFERENT` sentence has to be checkable against what refused it. Falling through to the
    authority notice prints "different inferred key" with two `?` placeholders while the inferred
    keys are byte-identical — the "cannot be told apart from a bug in this file" failure `_NOTICES`
    exists to forbid."""
    inputs = _bound_inputs("d0")
    a = comparability_record(task=_TASK, inputs_prov=inputs, substrate={"repo": "x"})
    b = comparability_record(task=_TASK, inputs_prov=inputs, substrate={"repo": "y"})
    notice = comparability_notice(a, b, other_run_id="v9")
    assert "different source tree" in notice
    assert "?" not in notice, "the sentence must name real digests, never a placeholder"
    assert a["substrate"] in notice and b["substrate"] in notice


def test_the_partition_splits_on_the_substrate_too():
    """A token that ignored it would put two rows in ONE group while `comparability_status` calls
    them incomparable — a surface would then rank numbers the rule refuses."""
    inputs = _bound_inputs("d0")
    a = comparability_record(task=_TASK, inputs_prov=inputs, substrate={"repo": "x"})
    b = comparability_record(task=_TASK, inputs_prov=inputs, substrate={"repo": "y"})
    assert group_token(a) != group_token(b)


def test_a_record_with_no_substrate_keeps_its_exact_historical_token():
    record = comparability_record(task=_TASK, inputs_prov=_bound_inputs("d0"))
    assert group_token(record) == f"{record['authority']}:{record['keys'][record['authority']]}"
    assert "@" not in group_token(record)


def test_the_substrate_digest_is_content_addressed_not_identity():
    """Two equal fingerprints from different objects must produce the same digest — the record is
    compared across processes and across runs, so object identity is not available to it."""
    a = comparability_record(task=_TASK, substrate={"repo": "sha", "dirty": ["f.py"]})
    b = comparability_record(task=_TASK, substrate={"repo": "sha", "dirty": ["f.py"]})
    assert a["substrate"] == b["substrate"]
    c = comparability_record(task=_TASK, substrate={"repo": "sha", "dirty": []})
    assert a["substrate"] != c["substrate"], "an uncommitted change must move the digest"
