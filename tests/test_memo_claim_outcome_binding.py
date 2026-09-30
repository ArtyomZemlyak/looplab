"""A research claim is ratified on the NUMBER it was verified on, not only on the node's lifecycle
(doc 69 §6.3, 69.26).

`finalize_verified_evidence` re-checked a verdict's evidence identity `(node_id, generation)` — a
reset, a delete, an abort — and nothing else. A node's number can move without any of those: a
`metric_retarget` re-ranks every node on another key and can restate the goal, so "Node 6 (champion,
UnseenRecall@20=0.03328)" verified under one objective would have gone into the shared claims store
as supported under the next. The verdict now records what it was judged on — the objective in force
and one outcome digest per cited node — and finalize refuses when either moved.
"""
from __future__ import annotations

from types import SimpleNamespace

from looplab.core.advisory_payloads import sanitize_research_memo_payload
from looplab.core.models import Event, Idea, Node, NodeStatus
from looplab.engine.claims import load_research_claims
from looplab.engine.lessons import LessonMemory
from looplab.events.replay import fold
from looplab.trust.memo_verify import _node_outcome_sig, verify_memo


def _log(*, retarget: bool) -> list:
    rows = [
        ("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"}),
        ("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                          "idea": {"operator": "draft", "params": {}, "rationale": "seed"},
                          "code": "x"}),
        ("node_evaluated", {"node_id": 0, "metric": 0.5, "extra_metrics": {"recall": 0.9},
                            "extra_metrics_provenance": {"recall": "declared"}}),
    ]
    if retarget:
        rows.append(("metric_retarget", {"key": "recall"}))
    return [Event(seq=i + 1, type=t, ts=float(i), data=d) for i, (t, d) in enumerate(rows)]


def _verified_memo(state) -> dict:
    memo = sanitize_research_memo_payload(
        {"claims": [{"statement": "node 0 scored 0.5, the best so far", "node_ids": [0]}]})
    verification = verify_memo(memo, state, client=None)
    for verdict in verification["verdicts"]:
        verdict["verdict"] = "supported"
    return sanitize_research_memo_payload({**memo, "verification": verification})


def _finalize(tmp_path, memo, final) -> dict:
    engine = SimpleNamespace(memory_dir=str(tmp_path))
    LessonMemory(engine).store_research_claims(SimpleNamespace(
        research=[memo], run_id="r", task_id="t", direction="max", nodes=final.nodes,
        aborted_nodes=list(getattr(final, "aborted_nodes", []) or []), trust_gate="audit",
        reward_hacks=[], objective_key=getattr(final, "objective_key", None)))
    (row,) = load_research_claims(tmp_path)
    return row


def test_a_claim_verified_before_a_retarget_is_not_ratified_after_it(tmp_path):
    """Driven through the real fold: the retarget moves node 0's number 0.5 -> 0.9 on the SAME
    lifecycle, which is exactly what the lifecycle check could not see. MUTATION: drop the objective
    check -> the outcome check refuses it instead; drop both -> it is ratified."""
    before = fold(_log(retarget=False))
    memo = _verified_memo(before)
    after = fold(_log(retarget=True))
    assert after.nodes[0].attempt == before.nodes[0].attempt, "premise: no new lifecycle"
    assert (before.nodes[0].metric, after.nodes[0].metric) == (0.5, 0.9)
    row = _finalize(tmp_path / "moved", memo, after)
    assert row["verification"]["verdict"] == "unverified"
    assert row["verification"]["note"] == "verification evidence was judged under another objective"
    # …and with nothing moved, the same memo is ratified exactly as before.
    row = _finalize(tmp_path / "still", memo, fold(_log(retarget=False)))
    assert row["verification"]["verdict"] == "supported"


def _node(metric=1.0, *, status=NodeStatus.evaluated, error=None) -> Node:
    return Node(id=0, operator="draft", idea=Idea(operator="draft"), metric=metric, status=status,
                attempt=0, error_reason=error or "")


def test_a_cited_number_that_moved_without_a_new_lifecycle_is_refused(tmp_path):
    """The objective can stay put and one cited number still move — a hand-edited log, or any later
    path that rewrites a terminal in place. The outcome digest is the second half."""
    verified = SimpleNamespace(nodes={0: _node(1.0)}, objective_key=None)
    memo = _verified_memo(verified)
    row = _finalize(tmp_path / "moved", memo, SimpleNamespace(nodes={0: _node(2.0)}))
    assert row["verification"]["verdict"] == "unverified"
    assert row["verification"]["note"] == "verification evidence outcome changed after it was judged"
    row = _finalize(tmp_path / "same", memo, SimpleNamespace(nodes={0: _node(1.0)}))
    assert row["verification"]["verdict"] == "supported"


def test_a_failed_nodes_outcome_is_its_failure_reason():
    crashed = _node(None, status=NodeStatus.failed, error="crash")
    assert _node_outcome_sig(crashed) != _node_outcome_sig(
        _node(None, status=NodeStatus.failed, error="timeout"))
    assert _node_outcome_sig(crashed) == _node_outcome_sig(
        _node(None, status=NodeStatus.failed, error="crash"))
    assert _node_outcome_sig(_node(1.0)) != _node_outcome_sig(_node(1.0000001))
    assert _node_outcome_sig(None) is None


def test_a_receipt_written_before_the_binding_is_checked_by_lifecycle_alone(tmp_path):
    """Additive with a reader-side default: a verdict row without `objective` / `outcomes` (every row
    written before 69.26) keeps the historical reading, so no finished run's claims change."""
    verified = SimpleNamespace(nodes={0: _node(1.0)}, objective_key=None)
    memo = _verified_memo(verified)
    for verdict in memo["verification"]["verdicts"]:
        verdict["evidence"].pop("objective")
        verdict["evidence"].pop("outcomes")
    legacy = sanitize_research_memo_payload(memo)
    assert "outcomes" not in legacy["verification"]["verdicts"][0]["evidence"]
    row = _finalize(tmp_path, legacy, SimpleNamespace(nodes={0: _node(2.0)}, objective_key="x"))
    assert row["verification"]["verdict"] == "supported"


def test_the_sanitizer_carries_a_well_formed_binding_and_refuses_a_damaged_one():
    verified = SimpleNamespace(nodes={0: _node(1.0)}, objective_key="recall")
    memo = _verified_memo(verified)
    evidence = memo["verification"]["verdicts"][0]["evidence"]
    assert evidence["objective"] == "recall" and evidence["complete"] is True
    assert evidence["outcomes"] == [_node_outcome_sig(_node(1.0))]
    assert sanitize_research_memo_payload(memo) == memo, "idempotent across the two boundaries"
    for damaged in (["not-a-digest"], [], [evidence["outcomes"][0]] * 2, "x"):
        bad = {**memo, "verification": {**memo["verification"], "verdicts": [
            {**memo["verification"]["verdicts"][0],
             "evidence": {**evidence, "outcomes": damaged}}]}}
        out = sanitize_research_memo_payload(bad)["verification"]["verdicts"][0]["evidence"]
        assert out["complete"] is False and "outcomes" not in out, damaged
    bad = {**memo, "verification": {**memo["verification"], "verdicts": [
        {**memo["verification"]["verdicts"][0], "evidence": {**evidence, "objective": 7}}]}}
    out = sanitize_research_memo_payload(bad)["verification"]["verdicts"][0]["evidence"]
    assert out["complete"] is False


# -------------------------------------------------------------------------------- critic 2026-09-30
_URL = "https://arxiv.org/abs/2106.09685"


def _log_with_failure(*extra) -> list:
    rows = [(e.type, e.data) for e in _log(retarget=False)] + [
        ("node_created", {"node_id": 1, "parent_ids": [0], "operator": "improve",
                          "idea": {"operator": "improve", "params": {}, "rationale": "r"},
                          "code": "y"}),
        ("node_failed", {"node_id": 1, "error": "boom", "reason": "crash"})] + list(extra)
    return [Event(seq=i + 1, type=t, ts=float(i), data=d) for i, (t, d) in enumerate(rows)]


def _memo_for(state, claim) -> dict:
    memo = sanitize_research_memo_payload({
        "claims": [claim],
        "sources": [{"url": _URL, "title": "LoRA: Low-Rank Adaptation", "snippet": "rank 8"}]})
    verification = verify_memo(memo, state, client=None)
    for verdict in verification["verdicts"]:
        verdict["verdict"] = "supported"
    return sanitize_research_memo_payload({**memo, "verification": verification})


def test_a_claim_no_ruler_can_move_keeps_its_verdict_across_a_retarget(tmp_path):
    """The critic's `d1`: a URL-only literature claim and a claim citing only a FAILED node were
    refused "judged under another objective" after a retarget, though neither was shown a number —
    and each refusal flipped the run's D8 `producer_complete`. MUTATION: drop the `_carries_number`
    clause -> both are refused."""
    before = fold(_log_with_failure())
    after = fold(_log_with_failure(("metric_retarget", {"key": "recall"})))
    assert (before.objective_key, after.objective_key) == (None, "recall")
    for name, claim in (("url", {"statement": "LoRA reports rank 8 suffices", "urls": [_URL]}),
                        ("failed", {"statement": "node 1 crashed", "node_ids": [1]})):
        memo = _memo_for(before, claim)
        assert memo["verification"]["verdicts"][0]["evidence"]["complete"] is True, name
        row = _finalize(tmp_path / name, memo, after)
        assert row["verification"]["verdict"] == "supported", (name, row["verification"])
    # A claim that WAS shown a number is still refused on the objective.
    memo = _memo_for(before, {"statement": "node 0 scored 0.5", "node_ids": [0, 1]})
    row = _finalize(tmp_path / "numbered", memo, after)
    assert row["verification"]["note"] == "verification evidence was judged under another objective"


def test_a_number_that_vanished_under_the_new_objective_is_an_outcome_change(tmp_path):
    """A node shown a number, retargeted onto a key it never reported, carries none now: the
    objective clause steps aside and the outcome digest refuses it — never a silent ratification."""
    verified = SimpleNamespace(nodes={0: _node(1.0)}, objective_key=None)
    memo = _verified_memo(verified)
    row = _finalize(tmp_path, memo, SimpleNamespace(nodes={0: _node(None)}, objective_key="recall"))
    assert row["verification"]["note"] == "verification evidence outcome changed after it was judged"


def test_a_half_binding_is_incomplete_and_never_ratifies(tmp_path):
    """The critic's `d6`: a receipt with ONE of the two keys — a shape the writer never mints — was
    kept complete, and finalize then checked the objective and skipped the numbers: the number moved
    and the verdict stood. MUTATION: drop the pair test from `_outcome_binding` -> `supported`."""
    verified = SimpleNamespace(nodes={0: _node(1.0)}, objective_key=None)
    memo = _verified_memo(verified)
    for dropped in ("outcomes", "objective"):
        half = {**memo, "verification": {**memo["verification"], "verdicts": [
            {**memo["verification"]["verdicts"][0], "evidence": {
                k: v for k, v in memo["verification"]["verdicts"][0]["evidence"].items()
                if k != dropped}}]}}
        out = sanitize_research_memo_payload(half)
        evidence = out["verification"]["verdicts"][0]["evidence"]
        assert evidence["complete"] is False, dropped
        row = _finalize(tmp_path / dropped, out, SimpleNamespace(nodes={0: _node(2.0)},
                                                                 objective_key=None))
        assert row["verification"]["verdict"] == "unverified", dropped


def test_the_objective_the_sanitizer_carries_is_exactly_the_one_the_fold_admits():
    """The fold's `metric_retarget` rule (`events/replay.py`): a non-blank string of at most 256
    characters. MUTATIONS: drop `.strip()`; `<=` -> `<`; a smaller cap."""
    edge = "k" * 256
    state = fold(_log_with_failure(("metric_retarget", {"key": edge})))
    assert state.objective_key == edge, "premise: the fold admits a 256-character key"
    memo = _verified_memo(SimpleNamespace(nodes={0: _node(1.0)}, objective_key=edge))
    assert memo["verification"]["verdicts"][0]["evidence"]["complete"] is True
    assert fold(_log_with_failure(("metric_retarget", {"key": edge + "k"}))).objective_key is None
    assert fold(_log_with_failure(("metric_retarget", {"key": "   "}))).objective_key is None
    for refused in (edge + "k", "   "):
        memo = _verified_memo(SimpleNamespace(nodes={0: _node(1.0)}, objective_key=refused))
        assert memo["verification"]["verdicts"][0]["evidence"]["complete"] is False, refused


def test_outcomes_that_are_not_a_list_are_refused_whatever_they_contain():
    """A mapping iterates its keys, so `{digest: 0}` has the right length and a valid first element.
    MUTATION: drop the list/tuple test -> it is carried as a binding."""
    memo = _verified_memo(SimpleNamespace(nodes={0: _node(1.0)}, objective_key=None))
    evidence = memo["verification"]["verdicts"][0]["evidence"]
    bad = {**memo, "verification": {**memo["verification"], "verdicts": [
        {**memo["verification"]["verdicts"][0],
         "evidence": {**evidence, "outcomes": {evidence["outcomes"][0]: 0}}}]}}
    out = sanitize_research_memo_payload(bad)["verification"]["verdicts"][0]["evidence"]
    assert out["complete"] is False and "outcomes" not in out


def test_a_digest_that_cannot_be_minted_leaves_the_receipt_incomplete(monkeypatch):
    """No fold produces a cited terminal without a canonical outcome, so this is the rule's own
    truth table. MUTATION: drop the `all(sig is not None …)` clause -> complete."""
    from looplab.trust import memo_verify
    monkeypatch.setattr(memo_verify, "_node_outcome_sig", lambda node: None)
    memo = sanitize_research_memo_payload(
        {"claims": [{"statement": "node 0 scored 1.0", "node_ids": [0]}]})
    verification = verify_memo(memo, SimpleNamespace(nodes={0: _node(1.0)}, objective_key=None),
                               client=None)
    assert verification["verdicts"][0]["evidence"]["complete"] is False


def test_finalize_refuses_an_outcome_list_of_the_wrong_length_on_its_own():
    """The sanitizer refuses it too; finalize must not depend on having been handed a sanitized row.
    MUTATION: drop the length test -> `zip` truncates and the verdict is ratified."""
    from looplab.core.models import RunState
    from looplab.trust.memo_verify import finalize_verified_evidence
    state = RunState(nodes={0: _node(1.0)})
    memo = _verified_memo(state)
    claim = memo["claims"][0]
    verdict = memo["verification"]["verdicts"][0]
    assert finalize_verified_evidence(claim, verdict, state)[1] == ""
    short = {**verdict, "evidence": {**verdict["evidence"], "outcomes": []}}
    assert finalize_verified_evidence(claim, short, state) == (
        None, "verification evidence identity is malformed")
