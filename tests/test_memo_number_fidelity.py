"""Where a memo's quoted numbers came from (doc 52 row 32, second half).

`trust/memo_verify.py::check_claims` declined to look at numbers at all, for a reason that is still
correct: a regex cannot tell an arXiv id from a metric, so a numeric "confabulation" heuristic
labels well-supported claims fabricated. MLReplicate's 59 % (fabricated numbers are what survives
review) says the question is worth asking anyway, and the answer is to stop CLASSIFYING and start
MATCHING — `core/research_record.py::number_fidelity` asks only whether a quoted decimal is a
number the CITED experiments recorded.

The property under test is that the instrument says exactly that and no more: a match is exact at
the precision the memo quoted, an unmatched number is never called anything, no verdict moves, and
the denominator is honest about what it counts (measured on the one real memo in this tree: 21
decimals across 8 claims, of which 6 are results and 13 are hyperparameter VALUES — which is why
the share this block records is an instrument reading and not a grade).
"""
from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from looplab.core.advisory_payloads import sanitize_research_memo_payload
from looplab.core.models import NodeStatus, ResearchMemo, RunState
from looplab.core.research_record import (NUMBER_FIDELITY_VERSION, number_fidelity,
                                          number_matches_metric, quoted_numbers)
from looplab.events.replay import fold
from looplab.trust.memo_verify import check_claims, number_fidelity_report

from factories import make_engine

_V8_MEMO = pathlib.Path(__file__).resolve().parent / "data" / "v8_research_memo.json"


# ------------------------------------------------------------------ the pure builder

def test_a_decimal_the_cited_experiment_recorded_is_matched_at_the_quoted_precision():
    """The memo's own rounding is the tolerance, so nothing here invents one."""
    exact = number_fidelity("in-batch InfoNCE reached recall@100=0.8776 on this backbone",
                            cited_metrics=[(9, 0.8776)])
    assert exact["matched"] == 1 and exact["quoted"] == 1 and exact["unmatched"] == 0
    assert exact["values"] == [{"text": "0.8776", "match": "cited", "node_id": 9}]

    rounded = number_fidelity("it climbed to about 0.88 on the held-out split",
                              cited_metrics=[(9, 0.87764)])
    assert rounded["matched"] == 1, "0.88 IS 0.87764 to the two places the memo quoted"

    off = number_fidelity("it climbed to 0.8776 on the held-out split",
                          cited_metrics=[(9, 0.8835)])
    assert off["matched"] == 0 and off["unmatched"] == 1
    assert off["values"][0]["match"] == "none" and "node_id" not in off["values"][0]


def test_a_number_of_an_experiment_the_claim_does_not_cite_is_its_own_channel():
    """The one finding a reader cannot get from the statement and the verdict alone: the decimal
    is real and it belongs to another experiment of this run — a mis-attribution, not a fabrication
    and not a citation either, so it is neither `cited` nor `none`."""
    row = number_fidelity("the DCL recipe reached 0.7280 here",
                          cited_metrics=[(2, 0.6)], other_metrics=[(5, 0.728), (6, 0.1)])
    assert row["matched"] == 0 and row["elsewhere"] == 1 and row["unmatched"] == 0
    assert row["values"] == [{"text": "0.7280", "match": "run", "node_id": 5}]


def test_an_arxiv_id_and_a_url_are_excluded_rather_than_counted_unmatched():
    """THE REASON THIS INSTRUMENT EXISTS IN THIS SHAPE. `check_claims`'s NOTE names the arXiv id as
    the number a regex cannot classify; both shapes here are excluded by LEXICAL span — no judgement
    about the number — and the count of what was excluded rides on the record so the denominator is
    checkable rather than quietly shrunk."""
    row = number_fidelity("R-Drop (arXiv 2106.14448, see https://arxiv.org/abs/2106.14448) "
                          "reached 0.8835 here",
                          cited_metrics=[(1, 0.8835)])
    assert row["quoted"] == 1 and row["matched"] == 1
    assert row["excluded"] == 2, "the id, and the id inside the URL, are not quoted measurements"
    assert [v["text"] for v in row["values"]] == ["0.8835"]


def test_integers_and_version_triples_are_not_quoted_measurements():
    """A memo quotes hyperparameters as integers by the dozen (batch 8192, 10 epochs, seed 42) and a
    recorded metric is a measured decimal — counting them would fill the denominator with numbers
    nobody claims are results."""
    assert quoted_numbers("bs 8192, accumulate 2, 10 epochs, positive_threshold=1") == []
    assert [n["text"] for n in quoted_numbers("torch 2.1.0 with lr 1e-3 and wd 0.1")] == ["0.1"]


def test_the_sign_is_part_of_the_number():
    """A quote that drops the minus of a negative metric reads as unmatched rather than as a match:
    deciding that the memo "meant" the absolute value is the guess this instrument exists to avoid.
    """
    assert number_matches_metric({"value": -0.32, "places": 2}, -0.32) is True
    assert number_matches_metric({"value": 0.32, "places": 2}, -0.32) is False
    assert number_fidelity("the loss settled at 0.32", cited_metrics=[(1, -0.32)])["matched"] == 0


def test_a_percentage_is_the_same_measurement_at_another_scale():
    row = number_fidelity("recall reached 87.8% after the sweep", cited_metrics=[(4, 0.87764)])
    assert row["matched"] == 1, "the `%` right after the literal is scale, not a different number"
    assert number_fidelity("the plateau is 87.8 recall", cited_metrics=[(4, 0.87764)])["matched"] == 0


def test_a_metric_nobody_measured_is_never_a_match():
    for metric in (None, "n/a", float("nan"), float("inf")):
        assert number_matches_metric({"value": 0.5, "places": 1}, metric) is False


# ------------------------------------------------------------------ the memo-level report

def _state(metrics: dict[int, float]) -> RunState:
    state = RunState(run_id="r", task_id="t", direction="max")
    for nid, value in metrics.items():
        state.nodes[nid] = _node(nid, value)
    return state


def _node(nid: int, metric: float):
    from looplab.core.models import Idea, Node
    return Node(id=nid, operator="seed", metric=metric, attempt=0,
                idea=Idea(operator="seed", theme="t", rationale="r", params={}),
                status=NodeStatus.evaluated)


def _claim(statement: str, node_ids: list[int]) -> dict:
    return {"statement": statement, "node_ids": node_ids, "urls": [], "url_identities": []}


def test_the_report_is_one_row_per_claim_with_the_totals_recomputed_beside_them():
    memo = {"claims": [_claim("node 3 reached 0.8776 against a 0.8000 baseline", [3]),
                       _claim("the plateau elsewhere is 0.9100", [3]),
                       _claim("no numbers here at all", [3])]}
    report = number_fidelity_report(memo, _state({3: 0.8776, 4: 0.91}))
    assert report["v"] == NUMBER_FIDELITY_VERSION
    assert report["claims"] == [
        {"quoted": 2, "matched": 1, "elsewhere": 0, "unmatched": 1, "excluded": 0},
        {"quoted": 1, "matched": 0, "elsewhere": 1, "unmatched": 0, "excluded": 0},
        {"quoted": 0, "matched": 0, "elsewhere": 0, "unmatched": 0, "excluded": 0}]
    assert report["quoted"] == 3 and report["matched"] == 1 and report["elsewhere"] == 1
    assert report["fidelity"] == pytest.approx(1 / 3)


def test_no_decimal_to_measure_is_None_and_not_zero():
    """A memo whose claims quote no number has not failed the measurement, and a memo with no
    claims has nothing to measure at all — neither may read as "nothing matched"."""
    assert number_fidelity_report({"claims": [_claim("no numbers here", [1])]},
                                  _state({1: 0.5}))["fidelity"] is None
    assert number_fidelity_report({"claims": []}, _state({1: 0.5})) is None
    assert number_fidelity_report({}, _state({})) is None


def test_a_cited_node_the_run_cannot_show_is_not_evidence_for_its_number():
    """The metric map applies the verifier's OWN lifecycle rule (`_is_terminal_evidence`): a
    tombstoned experiment is gone, so a number quoted from it is unmatched rather than matched
    against a lifecycle the verifier itself refuses to show."""
    state = _state({3: 0.8776})
    assert number_fidelity_report({"claims": [_claim("node 3 reached 0.8776", [3])]},
                                  state)["matched"] == 1
    state.nodes[3].tombstoned = True
    report = number_fidelity_report({"claims": [_claim("node 3 reached 0.8776", [3])]}, state)
    assert report["matched"] == 0 and report["unmatched"] == 1


# ------------------------------------------------------------------ what it must NOT move

def test_no_verdict_moves_because_of_a_number():
    """The instrument RECORDS. A claim whose every number is unmatched keeps the verdict it had
    before the measure existed — because "this run's metrics do not contain that decimal" is true of
    every honest quotation of a paper or of a sibling run."""
    state = _state({3: 0.5})
    claim = _claim("node 3 reached 0.9999, far above anything else", [3])
    verdicts = check_claims([claim], state, [])
    assert verdicts[0]["verdict"] == "cited" and verdicts[0]["note"] == ""
    assert number_fidelity_report({"claims": [claim]}, state)["unmatched"] == 1


# ------------------------------------------------------------------ the durable record

def test_the_block_survives_the_sanitizer_with_its_aggregates_recomputed():
    memo = {"summary": "s", "claims": [_claim("node 3 reached 0.8776", [3])],
            "numbers": {"v": NUMBER_FIDELITY_VERSION,
                        "claims": [{"quoted": 1, "matched": 9, "elsewhere": 0,
                                    "unmatched": 0, "excluded": 0}],
                        "quoted": 1, "matched": 9, "elsewhere": 0, "unmatched": 0,
                        "excluded": 0, "fidelity": 9.0}}
    out = sanitize_research_memo_payload(memo)
    assert out["numbers"]["claims"] == [{"quoted": 1, "matched": 1, "elsewhere": 0,
                                         "unmatched": 0, "excluded": 0}], "9 of 1 is not a count"
    assert out["numbers"]["matched"] == 1 and out["numbers"]["fidelity"] == pytest.approx(1.0)


def test_a_memo_without_the_block_stays_without_it():
    """Replay stability: a row written before this existed folds to what it always folded to."""
    out = sanitize_research_memo_payload({"summary": "from before the measure existed"})
    assert "numbers" not in out
    out = sanitize_research_memo_payload({"summary": "s", "numbers": {"v": 99, "claims": []}})
    assert "numbers" not in out, "an unknown version is not a block"


def test_the_engine_records_it_for_every_memo_with_claims():
    """Driven through the real writer: the block has to survive `_record_deep_research`'s two
    sanitizer passes and land on the folded record, and it must be there with the verifier OFF —
    the measurement is free, so a run that buys no verdicts still gets it."""
    engine = make_engine(pathlib.Path(tempfile.mkdtemp()) / "run")
    engine._research_verify = False
    engine.store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "max"})
    engine.store.append("node_created", {"node_id": 0, "parent_id": None, "operator": "seed",
                                         "idea": {"theme": "t", "rationale": "r", "params": {},
                                                  "operator": "seed"}})
    engine.store.append("node_evaluated", {"node_id": 0, "metric": 0.8776, "artifacts": {}})
    engine._record_deep_research(
        ResearchMemo(summary="s", at_node=1,
                     claims=[{"statement": "experiment 0 reached 0.8776, above the 0.8000 mark",
                              "node_ids": [0], "urls": []}]),
        trigger="cadence", manual=False)
    memo = fold(engine.store.read_all()).research[-1]
    assert memo["numbers"]["v"] == NUMBER_FIDELITY_VERSION
    assert memo["numbers"]["claims"] == [{"quoted": 2, "matched": 1, "elsewhere": 0,
                                          "unmatched": 1, "excluded": 0}]
    assert memo["numbers"]["fidelity"] == pytest.approx(0.5)
    assert "verification" not in memo, "the instrument is independent of the paid verifier"


# ------------------------------------------------------------------ the one real memo in the tree

def test_the_real_memo_in_this_tree_measures_what_the_audit_page_reports():
    """`docs/audit/memo-number-fidelity.md` §3 reports 21 decimals over the 8 claims of
    `rubertlite-dr-unified-v8`'s recorded memo, none of them excluded. That is the corpus this box
    has, and the page's number has to be re-derivable from the fixture rather than remembered."""
    memo = json.loads(_V8_MEMO.read_text(encoding="utf-8"))
    report = number_fidelity_report(memo, RunState(run_id="r", task_id="t", direction="max"))
    assert len(report["claims"]) == len(memo["claims"]) == 8
    assert report["quoted"] == 21 and report["excluded"] == 0
    # The run held ZERO nodes when this memo was written (`at_node: 0`), so every decimal in it
    # comes from a sibling run — and the honest reading of that is `unmatched`, not `fabricated`.
    assert report["unmatched"] == 21 and report["matched"] == 0 and report["elsewhere"] == 0
    assert report["fidelity"] == pytest.approx(0.0)
