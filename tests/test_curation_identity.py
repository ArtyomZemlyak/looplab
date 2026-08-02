"""The content-addressed identities that bind a finalize receipt to what paid for it (doc 25 EM-04).

These are not cache keys. `_validate_v2_curation_row` RECOMPUTES `source_key` and rejects any row
whose stored value differs, so writer and validator must derive it identically forever. A changed
field, a different key order, a flipped `ensure_ascii` — and every previously valid row in the
ledger starts failing, surfacing as `GovernanceLedgerUnavailable` on reads that used to work.

They were written out twice, in `lessons.py` and `governance_health.py`, with nothing but convention
holding the copies together. There is one derivation now, and the golden digests below are what make
an accidental change loud instead of retroactive.
"""
from __future__ import annotations

import pytest

from looplab.engine.governance_health import curation_source_key, facets_curation_key
from looplab.engine.lessons import LessonMemory


class _Final:
    def __init__(self, run_id="run-7", task_id="task-x", last_finish_seq=41):
        self.run_id = run_id
        self.task_id = task_id
        self.last_finish_seq = last_finish_seq


# Golden values, computed from the PRE-extraction derivation in `lessons.py` at HEAD so they prove
# the move changed nothing. Changing one means every receipt already on disk stops validating; if a
# schema change genuinely requires it, the key's VERSION prefix has to move too.
GOLDEN_SOURCE = "source:v1:e9323fba28d4b3f20c52487fca0caeebaa3da942011169e61dc6f3bc28b81dc7"
GOLDEN_FACETS = "facets:v2:99c6b68bd0e2acac2d471e4be7c2d44acf57cd8c69d9ba4be5646c3287080e49"


def test_the_writer_and_the_validator_derive_the_same_source_key():
    """The whole point of the extraction: `LessonMemory` writes this key and `governance_health`
    checks it. Two spellings that happened to agree is not the same as one derivation."""
    assert LessonMemory._curation_source_key(_Final()) == curation_source_key(
        run_id="run-7", task_id="task-x", finish_seq=41)


def test_the_writer_and_the_validator_derive_the_same_facets_key():
    assert LessonMemory._facets_curation_key("task-x") == facets_curation_key("task-x")


def test_the_source_key_is_stable_against_its_golden_digest():
    assert LessonMemory._curation_source_key(_Final()) == GOLDEN_SOURCE


def test_the_facets_key_is_stable_against_its_golden_digest():
    assert LessonMemory._facets_curation_key("task-x") == GOLDEN_FACETS


def test_every_input_field_changes_the_source_key():
    """A field that did not participate would let two DIFFERENT run sources share one receipt — the
    second one silently reading the first's paid work as its own."""
    base = curation_source_key(run_id="a", task_id="b", finish_seq=1)
    assert curation_source_key(run_id="z", task_id="b", finish_seq=1) != base
    assert curation_source_key(run_id="a", task_id="z", finish_seq=1) != base
    assert curation_source_key(run_id="a", task_id="b", finish_seq=2) != base
    assert curation_source_key(run_id="a", task_id="b", finish_seq=None) != base


def test_an_absent_finish_seq_is_its_own_identity_not_zero():
    """"the run never finished" and "the run finished at seq 0" are different sources."""
    assert (curation_source_key(run_id="a", task_id="b", finish_seq=None)
            != curation_source_key(run_id="a", task_id="b", finish_seq=0))


def test_a_malformed_finish_seq_reads_as_absent_rather_than_as_a_value():
    """`last_finish_seq` comes off a folded state; a bool or a negative is not a sequence number,
    and coercing one would mint an identity no validator could reproduce."""
    for junk in (True, -1, "12", 1.5, None):
        assert LessonMemory._curation_source_key(_Final(last_finish_seq=junk)) == (
            curation_source_key(run_id="run-7", task_id="task-x", finish_seq=None))


def test_facets_are_keyed_on_the_task_alone_so_a_family_reuses_one_overlay():
    """Facets describe a task family. Keying them on the run would re-purchase the same overlay for
    every run of that task."""
    assert facets_curation_key("shared") == facets_curation_key("shared")
    assert facets_curation_key("shared") != facets_curation_key("other")


@pytest.mark.parametrize("empty", ["", None])
def test_facets_refuse_an_empty_task_rather_than_minting_a_shared_identity(empty):
    """An empty id would give every task without one the SAME key — one task's paid overlay served
    to another."""
    with pytest.raises(ValueError):
        facets_curation_key(empty)
    with pytest.raises(ValueError):
        LessonMemory._facets_curation_key(empty)


def test_a_written_receipt_validates_against_the_shared_derivation():
    """End to end: a row carrying the writer's key must pass the validator's recomputation."""
    from looplab.engine.governance_health import _curation_source_key

    row_key = LessonMemory._curation_source_key(_Final(run_id="r", task_id="t",
                                                       last_finish_seq=3))
    assert row_key == _curation_source_key(run_id="r", task_id="t", finish_seq=3)
