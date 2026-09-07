"""What share of a memo's SYNTHESIS names evidence it can resolve (doc 52 row 32).

`trust/memo_verify.py` read only the `claims` list. A memo is mostly not claims: `summary`,
`findings` and `recommended_directions` are the synthesis statements Kosmos's expert evaluation
found ~57.9 % accurate against ~85 % for per-analysis ones, and nothing counted them, bound them, or
could say what share of them cited anything — so AAR's first measure could not be computed for a
LoopLab memo at all.

The property under test is that the number means what it says: BOUND is "names something this memo
can resolve", never "looks like a citation", and the denominator is not padded with fragments.
"""
from __future__ import annotations

import pytest

from looplab.core.advisory_payloads import (PROVENANCE_COVERAGE_SECTIONS,
                                            PROVENANCE_COVERAGE_VERSION,
                                            sanitize_research_memo_payload)
from looplab.trust.memo_verify import provenance_coverage

_EVIDENCE_ID = "ev-" + "a" * 24


def _memo(**kw):
    memo = {"summary": "", "findings": [], "recommended_directions": [], "claims": [],
            "sources": [], "evidence": []}
    memo.update(kw)
    return memo


def test_a_statement_that_cites_a_node_the_memo_cites_is_bound():
    memo = _memo(summary="Node 3 beat the baseline by a wide margin on the held-out split.",
                 claims=[{"statement": "s", "node_ids": [3], "urls": [], "evidence_ids": []}])
    report = provenance_coverage(memo)
    assert report["summary"] == {"statements": 1, "bound": 1}
    assert report["coverage"] == pytest.approx(1.0) and report["v"] == PROVENANCE_COVERAGE_VERSION


def test_a_statement_naming_a_node_the_memo_never_cites_is_unbound():
    """The lenient reading would report the OPPOSITE of the thing being measured: a memo that
    gestures at experiments it never cited is precisely the unsupported synthesis this counts."""
    memo = _memo(summary="Node 12 was clearly the strongest of the run.",
                 claims=[{"statement": "s", "node_ids": [3], "urls": [], "evidence_ids": []}])
    assert provenance_coverage(memo)["summary"] == {"statements": 1, "bound": 0}


def test_an_evidence_id_or_a_source_url_also_binds():
    memo = _memo(findings=[f"The scaling claim rests on {_EVIDENCE_ID} and nothing else.",
                           "Throughput improved substantially across the board."],
                 recommended_directions=["Follow https://arxiv.org/abs/2608.18312 next."],
                 claims=[{"statement": "s", "node_ids": [], "urls": [],
                          "evidence_ids": [_EVIDENCE_ID]}],
                 sources=[{"title": "A long enough source title", "url": "https://arxiv.org/abs/2608.18312"}])
    report = provenance_coverage(memo)
    assert report["findings"] == {"statements": 2, "bound": 1}
    assert report["recommended_directions"] == {"statements": 1, "bound": 1}
    assert report["statements"] == 3 and report["bound"] == 2


def test_fragments_do_not_pad_the_denominator():
    """"Yes." is not a synthesis statement, and counting it as unbound would make every memo look
    worse than it is — the failure that makes a coverage number ignorable."""
    memo = _memo(summary="Yes. No. The ensemble of node 4 and node 5 is what carried the result.",
                 claims=[{"statement": "s", "node_ids": [4, 5], "urls": [], "evidence_ids": []}])
    assert provenance_coverage(memo)["summary"] == {"statements": 1, "bound": 1}


def test_a_memo_with_nothing_to_measure_reports_none_not_zero():
    report = provenance_coverage(_memo())
    assert report["statements"] == 0 and report["coverage"] is None
    assert all(report[section] == {"statements": 0, "bound": 0}
               for section in PROVENANCE_COVERAGE_SECTIONS)
    assert provenance_coverage("not a memo")["coverage"] is None


def test_the_block_survives_the_sanitizer_with_its_aggregate_recomputed():
    """The counts are the record and the share is derived, so a payload cannot persist an aggregate
    that disagrees with the rows beside it."""
    memo = _memo(summary="Node 3 carried the run, on the evidence of its own held-out split.",
                 claims=[{"statement": "s", "node_ids": [3], "urls": [], "evidence_ids": []}])
    memo["provenance"] = provenance_coverage(memo)
    memo["provenance"]["coverage"] = 0.999                   # a forged aggregate
    memo["provenance"]["summary"]["bound"] = 97              # more bound than exist
    out = sanitize_research_memo_payload(memo)
    assert out["provenance"]["summary"] == {"statements": 1, "bound": 0}
    assert out["provenance"]["coverage"] == pytest.approx(0.0)
    assert out["provenance"]["v"] == PROVENANCE_COVERAGE_VERSION


def test_a_memo_without_the_block_stays_without_it():
    """Replay stability: a row written before this existed must fold to what it always folded to."""
    out = sanitize_research_memo_payload(_memo(summary="A memo from before the measure existed."))
    assert "provenance" not in out
    out = sanitize_research_memo_payload({**_memo(), "provenance": {"v": 99, "summary": {}}})
    assert "provenance" not in out                            # an unknown version is not a block


def test_the_engine_records_the_coverage_for_every_memo_it_writes():
    """Driven through the real writer, not asserted about its source: the block has to survive
    `_record_deep_research`'s two sanitizer passes and land on the folded record, and it must be
    there for a memo with NO claims — the shape the verifier never sees and the one whose synthesis
    is least supported."""
    import pathlib
    import tempfile

    from looplab.core.models import ResearchMemo
    from looplab.events.replay import fold
    from tests.factories import make_engine

    engine = make_engine(pathlib.Path(tempfile.mkdtemp()) / "run")
    engine.store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "max"})
    engine._record_deep_research(
        ResearchMemo(summary="Node 2 carried the run. The rest were noise around it.",
                     findings=["Nothing in this finding names an experiment at all."],
                     at_node=2),
        trigger="cadence", manual=False)
    memo = fold(engine.store.read_all()).research[-1]
    assert memo["provenance"]["v"] == PROVENANCE_COVERAGE_VERSION
    # two statements, neither bound: the memo cites no node, no evidence id and no source
    assert memo["provenance"]["statements"] == 3 and memo["provenance"]["bound"] == 0
    assert memo["provenance"]["coverage"] == pytest.approx(0.0)


def test_a_token_short_enough_to_appear_by_accident_does_not_bind():
    """`_is_bound` asks whether a statement CONTAINS one of the memo's literals, and the memo
    WRITES those literals — so a one-character evidence id binds every statement carrying that
    letter. Measured before the floor: `{"evidence_ids": ["e"]}` beside two ordinary sentences
    reported 1.0 coverage for a memo that cites nothing, which is the number reading backwards.

    The floors are set below the shortest honest cite (`ev-` + 24 hex, and a URL longer than its
    scheme), so an accidental match is refused and a real one is not."""
    prose = "The model converges quickly. Tuning the learning rate helped a lot here."
    assert provenance_coverage({"summary": prose, "claims": [{"evidence_ids": ["e"]}]})["coverage"] == 0.0
    assert provenance_coverage({"summary": prose, "evidence": [{"id": "a"}]})["coverage"] == 0.0
    assert provenance_coverage({"summary": prose, "claims": [{"urls": ["a.io"]}]})["coverage"] == 0.0

    real = "ev-1a2b3c4d5e6f7a8b9c0d1e2f"
    bound = provenance_coverage({"summary": f"The learning rate is what moved it ({real}).",
                                 "evidence": [{"id": real}]})
    assert bound["coverage"] == 1.0, "the floor must not refuse a real evidence id"
    url = "https://arxiv.org/abs/2401.00001"
    assert provenance_coverage({"summary": f"The schedule follows {url} exactly, step for step.",
                                "sources": [{"url": url}]})["coverage"] == 1.0
