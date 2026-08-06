"""The cross-run concept RATIFICATION stage (`engine/concept_tidy.py`).

The stage applies merges an LLM already proposed and the paid curation protocol already logged. Its
predecessor, `apply_concept_curation`, was deleted on 2026-08-03 (72ae8487 / EM-14) for bypassing the
CAS discipline — "neither `expected_revision` nor `action_id` reached the writers" — so the tests that
matter here are the CONCURRENCY ones, and every one of them is driven by real concurrent execution:
real threads racing a real interprocess lock, real forked processes, and a real SIGKILL between a
durable decision and the next one. None of them asserts anything about the source text.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from looplab.engine import concept_tidy
from looplab.engine.concept_registry import (
    _append_governance,
    canonicalize_concepts,
    clear_concept_alias,
    concept_governance_global_revision,
    concept_governance_snapshot,
    load_concept_aliases,
    record_concept_alias,
)
from looplab.engine.concept_tidy import (
    RATIFIER_ACTOR,
    proposed_merges,
    ratification_action_id,
    ratified_alias_sources,
    ratify_concept_merges,
    read_ratification_receipts,
)
from looplab.engine.governance_health import curation_source_key


# --------------------------------------------------------------------------- fixtures
# The two durable inputs the stage reads, built in the exact shapes their own strict validators
# accept — `concept_capsules.jsonl` (the portfolio `require_existing` proves membership against) and
# `concept_curation_log.jsonl` (the paid steward's v2 finalize receipts).

def _capsule(run_id: str, concepts: list[str], task_id: str = "t") -> dict:
    return {
        "v": 2, "concept_evidence": "classifier", "run_id": run_id, "task_id": task_id,
        "fingerprint": ["tok"], "direction": "min", "best_metric": 1.0,
        "concepts": list(concepts), "concept_outcomes": {}, "concept_signs": {},
    }


def _write_capsules(memory_dir: Path, capsules: list[dict]) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "concept_capsules.jsonl").write_text(
        "".join(json.dumps(c) + "\n" for c in capsules), encoding="utf-8")


def _append_proposal(memory_dir: Path, *, merges=(), splits=(), purges=(),
                     run_id: str = "r1", digest: str = "a" * 64) -> dict:
    """One v2 finalize curation receipt, written through the same append primitive finalize uses."""
    row = {
        "v": 2, "curation_key": f"concept:v2:{digest}",
        "source_key": curation_source_key(run_id=run_id, task_id="t", finish_seq=1),
        "run_id": run_id, "task_id": "t", "finish_seq": 1,
        "input_digest": digest, "input_schema": "finalize-concept-curation/v3",
        "model": "test-model", "parser": "tool_call_once", "outcome": "proposed",
        "auto": False, "auto_requested": False, "receipt": None,
        "proposals": {"merges": list(merges), "splits": list(splits), "purges": list(purges)},
    }
    return _append_governance(memory_dir / "concept_curation_log.jsonl", row, require_durable=True)


def _merge(src: str, dst: str, why: str = "same technique") -> dict:
    return {"from_concept": src, "to_concept": dst, "why": why}


@pytest.fixture
def portfolio(tmp_path):
    """A memory dir carrying three real drift pairs from the 46-run corpus and their proposals."""
    memory_dir = tmp_path / "mem"
    _write_capsules(memory_dir, [
        _capsule("run-a", ["model/gradient-boosting", "optimization/hyperparameter-tuning"]),
        _capsule("run-b", ["model/gradient_boosting", "optimization/hyperparameter_tuning"]),
        _capsule("run-c", ["regularization/r-drop", "regularization/rdrop"]),
    ])
    _append_proposal(memory_dir, merges=[
        _merge("model/gradient_boosting", "model/gradient-boosting"),
        _merge("optimization/hyperparameter_tuning", "optimization/hyperparameter-tuning"),
        _merge("regularization/rdrop", "regularization/r-drop"),
    ])
    return memory_dir


def _alias_rows(memory_dir: Path) -> list[dict]:
    path = Path(memory_dir) / "concept_aliases.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


_EXPECTED = {
    "model/gradient_boosting": "model/gradient-boosting",
    "optimization/hyperparameter_tuning": "optimization/hyperparameter-tuning",
    "regularization/rdrop": "regularization/r-drop",
}


# --------------------------------------------------------------------------- identity rules

def test_the_action_id_is_a_pure_function_of_the_semantic_payload(tmp_path):
    """Equal ids imply equal `_idempotency_payload`s, which is what makes a repeat a replay.

    This is the property that decides whether a second pass appends a second row, and it is why the
    id is keyed on the merge and NOT on the proposal that carried it.
    """
    from looplab.engine.concept_registry import _idempotency_payload

    def row(src, dst):
        return {"v": 1, "action": "set", "from": src, "to": dst}

    same = ratification_action_id("  Model/Gradient_Boosting ", "model/gradient-boosting")
    assert same == ratification_action_id("model/gradient_boosting", "model/gradient-boosting")
    assert _idempotency_payload(row("model/gradient_boosting", "model/gradient-boosting")) == \
        _idempotency_payload(row("  Model/Gradient_Boosting ".strip().lower(),
                                 "model/gradient-boosting"))
    # A different TARGET is a different decision and must get a different id, or a refinement would
    # be swallowed as a replay of the earlier merge.
    assert same != ratification_action_id("model/gradient_boosting", "model/gbm")


def test_two_proposals_of_the_same_merge_yield_one_decision(portfolio):
    """A second finalize over a changed portfolio re-proposes the same merge; that is not new work."""
    _append_proposal(portfolio, merges=[_merge("regularization/rdrop", "regularization/r-drop")],
                     run_id="r2", digest="b" * 64)
    merges = proposed_merges(portfolio)
    assert [m.action_id for m in merges] == sorted(set(m.action_id for m in merges),
                                                   key=[m.action_id for m in merges].index)
    assert sum(1 for m in merges if m.source == "regularization/rdrop") == 1


def test_only_merges_are_ratified_and_the_rest_is_reported(tmp_path):
    """SPLIT and PURGE are not deduplication; the stage applies neither and says so."""
    memory_dir = tmp_path / "mem"
    _write_capsules(memory_dir, [_capsule("run-a", ["a/one", "a/two", "noise/x", "coarse/thing"])])
    _append_proposal(
        memory_dir,
        merges=[_merge("a/two", "a/one")],
        splits=[{"from_concept": "coarse/thing",
                 "rules": [{"to": "coarse/left", "when_any": ["one"]}], "default": "coarse/thing"}],
        purges=[{"from_concept": "noise/x"}])
    result = ratify_concept_merges(memory_dir, at="now")
    assert [e["from"] for e in result["applied"]] == ["a/two"]
    assert result["pending"] == {"splits": 1, "purges": 1}
    # Nothing about the split/purge reached policy.
    assert load_concept_aliases(memory_dir) == {"a/two": "a/one"}
    assert not (memory_dir / "concept_splits.jsonl").exists()


def test_a_ratified_edge_names_its_author_and_an_operator_edge_does_not(portfolio):
    """A governed agent merge and a human's merge must never be indistinguishable in the ledger."""
    ratify_concept_merges(portfolio, at="now")
    record_concept_alias(str(portfolio), from_concept="regularization/r-drop",
                         to_concept="model/gradient-boosting", by="operator", at="now")
    actors = {row["from"]: row["by"] for row in _alias_rows(portfolio)}
    assert actors["model/gradient_boosting"] == RATIFIER_ACTOR
    assert actors["regularization/r-drop"] == "operator"
    assert "regularization/r-drop" not in ratified_alias_sources(portfolio)
    assert "model/gradient_boosting" in ratified_alias_sources(portfolio)


def test_a_pass_is_idempotent_and_leaves_one_row_per_decision(portfolio):
    first = ratify_concept_merges(portfolio, at="now")
    assert len(first["applied"]) == 3
    second = ratify_concept_merges(portfolio, at="later")
    assert second["applied"] == []
    assert {e["outcome"] for e in second["skipped"]} == {"already_applied"}
    assert len(_alias_rows(portfolio)) == 3
    assert load_concept_aliases(portfolio) == _EXPECTED


def test_a_dry_run_writes_no_policy_and_no_receipt(portfolio):
    """The operator's preview shares the stage's code path and must leave the portfolio untouched.

    "Untouched" is scoped to POLICY and AUDIT, not to the directory listing: reading a coherent
    governance snapshot takes the memory-wide lock, and `_interprocess_lock` creates its lock file.
    Every read-side surface (the HTTP concept lens, the CLI) does the same, so a dry run must not be
    held to a stricter rule than a plain read.
    """
    curation_before = (portfolio / "concept_curation_log.jsonl").read_bytes()
    result = ratify_concept_merges(portfolio, dry_run=True)
    assert {e["outcome"] for e in result["skipped"]} == {"would_apply"}
    assert result["applied"] == [] and result["receipt"] is False
    assert not (portfolio / "concept_aliases.jsonl").exists()
    assert not (portfolio / "concept_ratification_log.jsonl").exists()
    assert (portfolio / "concept_curation_log.jsonl").read_bytes() == curation_before
    assert load_concept_aliases(portfolio) == {}


# --------------------------------------------------------------------------- the undo

def test_a_cleared_ratification_is_never_re_applied(portfolio):
    """The undo, and the single most important property of the action-id choice.

    An operator who reverses a wrong auto-merge must not have it re-applied by the next pass. The
    original SET row stays physically in the append-only ledger, so its action id still matches and
    the repeat is a replay of that receipt — no new row, and the clear stands.
    """
    ratify_concept_merges(portfolio, at="now")
    before = canonicalize_concepts(list(_EXPECTED) + list(_EXPECTED.values()),
                                   aliases=load_concept_aliases(portfolio))
    clear_concept_alias(str(portfolio), from_concept="model/gradient_boosting",
                        by="operator", at="now")
    rows_after_clear = len(_alias_rows(portfolio))

    again = ratify_concept_merges(portfolio, at="even-later")

    assert "model/gradient_boosting" not in load_concept_aliases(portfolio)
    assert len(_alias_rows(portfolio)) == rows_after_clear      # the replay appended nothing
    assert [e["from"] for e in again["applied"]] == []
    # and the pass says so honestly rather than counting the replayed receipt as a fresh merge
    assert [e["outcome"] for e in again["skipped"] if e["from"] == "model/gradient_boosting"] == [
        "cleared_not_reapplied"]
    # and the reversal really restored the pre-merge view for that concept
    restored = canonicalize_concepts(list(_EXPECTED) + list(_EXPECTED.values()),
                                     aliases=load_concept_aliases(portfolio))
    assert "model/gradient_boosting" in restored and "model/gradient_boosting" not in before


def test_raw_tags_are_never_rewritten_so_the_undo_is_total(portfolio):
    """Canonicalization is a read-time rule; clearing every decision restores the exact prior view."""
    raw = ["model/gradient-boosting", "model/gradient_boosting",
           "optimization/hyperparameter-tuning", "optimization/hyperparameter_tuning",
           "regularization/r-drop", "regularization/rdrop"]
    before = canonicalize_concepts(raw, aliases=load_concept_aliases(portfolio))
    capsules_before = (portfolio / "concept_capsules.jsonl").read_bytes()

    ratify_concept_merges(portfolio, at="now")
    assert canonicalize_concepts(raw, aliases=load_concept_aliases(portfolio)) != before
    for source in _EXPECTED:
        clear_concept_alias(str(portfolio), from_concept=source, by="operator", at="now")

    assert canonicalize_concepts(raw, aliases=load_concept_aliases(portfolio)) == before
    assert (portfolio / "concept_capsules.jsonl").read_bytes() == capsules_before


# --------------------------------------------------------------------------- concurrency face 1:
# two runs finishing at once, both ratifying. REAL processes, so the interprocess lock in
# `_concept_governance_transaction` is genuinely exercised (threads would share the in-process one).

def _child_ratify(memory_dir: str, queue) -> None:
    try:
        result = ratify_concept_merges(memory_dir, at="concurrent")
        queue.put(("ok", len(result["applied"]), len(result["skipped"])))
    except Exception as exc:  # noqa: BLE001 — surfaced to the parent as a failure
        queue.put(("error", type(exc).__name__, str(exc)[:200]))


def test_concurrent_processes_ratify_each_merge_exactly_once(portfolio):
    ctx = mp.get_context("fork")
    queue = ctx.Queue()
    workers = [ctx.Process(target=_child_ratify, args=(str(portfolio), queue)) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(120)
        if worker.is_alive():                    # no pytest-timeout in this suite: never wait forever
            worker.kill()
            worker.join(10)
            pytest.fail("a concurrent ratification process did not finish")
    outcomes = [queue.get(timeout=10) for _ in workers]

    assert all(o[0] == "ok" for o in outcomes), outcomes
    # Every process saw all three decisions and reported each one as decided — none crashed, none
    # silently dropped one. The count of `applied` is deliberately NOT asserted to sum to three: it
    # means "in force as of this pass", so a genuine race can legitimately have two processes report
    # the same merge. The exactly-once property belongs to the LEDGER, and is asserted there.
    assert all(o[1] + o[2] == 3 for o in outcomes), outcomes
    rows = _alias_rows(portfolio)
    assert len(rows) == 3
    assert len({row["action_id"] for row in rows}) == 3
    assert load_concept_aliases(portfolio) == _EXPECTED
    # The shared global revision stayed a total order over the physical rows.
    assert concept_governance_global_revision(str(portfolio)) == 3
    assert sorted(row["governance_revision"] for row in rows) == [1, 2, 3]


def test_two_proposals_pointing_at_each_other_fail_closed(tmp_path):
    """Two finalize passes over different snapshots can propose A->B and B->A. One must lose."""
    memory_dir = tmp_path / "mem"
    _write_capsules(memory_dir, [_capsule("run-a", ["axis/one", "axis/two"])])
    _append_proposal(memory_dir, merges=[_merge("axis/one", "axis/two")])
    _append_proposal(memory_dir, merges=[_merge("axis/two", "axis/one")],
                     run_id="r2", digest="c" * 64)

    result = ratify_concept_merges(memory_dir, at="now")

    assert len(result["applied"]) == 1
    # Pin the fail-closed SET, not one member: which refusal fires depends on whether the stage's own
    # pre-check or the writer's locked cycle check sees the conflict first, and under a concurrent
    # operator write it can be either. Both are refusals; the invariant below is what matters.
    assert [e["outcome"] for e in result["skipped"]][0] in {
        "would_close_cycle", "target_not_canonical"}
    aliases = load_concept_aliases(memory_dir)
    # A cycle has no canonical result; the ledger must not contain one.
    assert len(aliases) == 1
    assert canonicalize_concepts(["axis/one", "axis/two"], aliases=aliases) == ["axis/two"]


# --------------------------------------------------------------------------- concurrency face 2:
# an operator editing concepts through the governance surface while the stage runs. Real threads,
# sequenced so the operator's write lands strictly INSIDE the stage's snapshot->append window.

def _blocking_writer(monkeypatch, released: threading.Event, reached: threading.Event):
    """Wrap the stage's writer so it parks after the stage has taken its governance snapshot."""
    original = concept_tidy.record_concept_alias

    def blocked(*args, **kwargs):
        reached.set()
        assert released.wait(30), "the racing operator write never completed"
        return original(*args, **kwargs)

    monkeypatch.setattr(concept_tidy, "record_concept_alias", blocked)


def test_an_operator_purge_landing_mid_pass_is_refused_not_overwritten(portfolio, monkeypatch):
    """The stage must never write an edge into a concept the operator has just tombstoned."""
    reached, released = threading.Event(), threading.Event()
    _blocking_writer(monkeypatch, released, reached)
    result: dict = {}

    def stage():
        result.update(ratify_concept_merges(portfolio, at="now"))

    worker = threading.Thread(target=stage)
    worker.start()
    try:
        assert reached.wait(30), "the stage never reached its write"
        # The operator purges the target the stage is about to merge INTO, from another thread,
        # after the stage's snapshot and before its append.
        record_concept_alias(str(portfolio), from_concept="model/gradient-boosting",
                             to_concept="", by="operator", at="now")
    finally:
        released.set()
        worker.join(60)
    assert not worker.is_alive()

    outcomes = {e["from"]: e["outcome"] for e in result["skipped"]}
    assert outcomes["model/gradient_boosting"] in {"purged", "target_not_canonical",
                                                  "revision_conflict"}
    aliases = load_concept_aliases(portfolio)
    # No live edge points at the tombstoned concept.
    assert aliases.get("model/gradient_boosting") != "model/gradient-boosting"
    assert canonicalize_concepts(["model/gradient_boosting"], aliases=aliases) in ([], ["model/gradient_boosting"])


def test_the_per_decision_cas_token_really_reaches_the_writer(portfolio, monkeypatch):
    """The EM-14 property: a governance write between snapshot and append must be DETECTED.

    Driven by removing the retry, so a real intervening write has to surface as a conflict rather
    than being absorbed. If the stage stopped passing `expected_governance_revision`, the writer
    would happily append and this decision would report `applied`.
    """
    monkeypatch.setattr(concept_tidy, "_MAX_CAS_ATTEMPTS", 1)
    reached, released = threading.Event(), threading.Event()
    _blocking_writer(monkeypatch, released, reached)
    result: dict = {}

    def stage():
        result.update(ratify_concept_merges(portfolio, at="now"))

    worker = threading.Thread(target=stage)
    worker.start()
    try:
        assert reached.wait(30)
        # An UNRELATED governance write: it invalidates no part of the stage's decision, so only the
        # CAS token can possibly notice it.
        record_concept_alias(str(portfolio), from_concept="unrelated/slug",
                             to_concept="regularization/r-drop", by="operator", at="now")
    finally:
        released.set()
        worker.join(60)
    assert not worker.is_alive()

    outcomes = [e["outcome"] for e in result["skipped"]]
    assert "revision_conflict" in outcomes


def test_the_shipped_retry_converges_after_a_conflict(portfolio, monkeypatch):
    """Same race with the shipped retry budget: the conflict is absorbed, not reported."""
    reached, released = threading.Event(), threading.Event()
    _blocking_writer(monkeypatch, released, reached)
    result: dict = {}

    def stage():
        result.update(ratify_concept_merges(portfolio, at="now"))

    worker = threading.Thread(target=stage)
    worker.start()
    try:
        assert reached.wait(30)
        record_concept_alias(str(portfolio), from_concept="unrelated/slug",
                             to_concept="regularization/r-drop", by="operator", at="now")
    finally:
        released.set()
        worker.join(60)
    assert not worker.is_alive()
    assert [e["outcome"] for e in result["skipped"]] == []
    assert len(result["applied"]) == 3


# --------------------------------------------------------------------------- concurrency face 3:
# the same stage re-entered after a crash. A REAL process, SIGKILLed between two durable decisions.

def test_a_pass_killed_after_its_first_write_completes_on_re_entry(portfolio):
    repo_root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(repo_root)}
    proc = subprocess.run(
        [sys.executable, "-m", "tests._concept_ratify_crash_driver", str(portfolio)],
        cwd=str(repo_root), env=env, capture_output=True, timeout=180)
    assert proc.returncode == -9, (proc.returncode, proc.stderr[-2000:])

    # Exactly one decision is durable, and no receipt was written — the crash left no claim behind.
    partial = _alias_rows(portfolio)
    assert len(partial) == 1
    assert read_ratification_receipts(portfolio) == []

    resumed = ratify_concept_merges(portfolio, at="after-crash")

    rows = _alias_rows(portfolio)
    assert len(rows) == 3                                  # no duplicate of the killed pass's row
    assert len({row["action_id"] for row in rows}) == 3
    assert load_concept_aliases(portfolio) == _EXPECTED    # identical to an uninterrupted pass
    assert len(resumed["applied"]) == 2
    assert [e["outcome"] for e in resumed["skipped"]] == ["already_applied"]


# --------------------------------------------------------------------------- concurrency face 4:
# a merge landing while the map is being rendered. Real reader thread against a real writer.

def test_a_reader_never_observes_a_half_applied_merge(portfolio):
    """Every concurrent observation is a legal governance state, and each source only ever moves once.

    The read side of the map takes ONE governance-locked snapshot (`concept_governance_snapshot`) and
    canonicalizes against it, so a decision landing mid-render is either wholly visible or wholly
    invisible. What must never appear is a source resolving to something that is not its proposed
    target, or a source that un-merges after having been seen merged.
    """
    raw = list(_EXPECTED) + list(_EXPECTED.values())
    observations: list[dict] = []
    stop = threading.Event()
    failures: list[str] = []

    def reader():
        while not stop.is_set():
            snapshot = concept_governance_snapshot(str(portfolio))
            aliases = snapshot["aliases"]
            seen = {source: aliases.get(source) for source in _EXPECTED}
            for source, target in seen.items():
                if target is not None and target != _EXPECTED[source]:
                    failures.append(f"{source} resolved to {target}")
            # The projection is always internally consistent: no canonical id is itself aliased away.
            canonical = canonicalize_concepts(raw, aliases=aliases)
            for concept in canonical:
                if concept in aliases:
                    failures.append(f"{concept} is canonical and aliased at once")
            observations.append(seen)

    worker = threading.Thread(target=reader)
    worker.start()
    try:
        ratify_concept_merges(portfolio, at="now")
    finally:
        stop.set()
        worker.join(60)
    assert not worker.is_alive()

    assert failures == []
    assert len(observations) > 1, "the reader never got a chance to observe a race"
    # Monotone per source: once merged, never unmerged.
    for source in _EXPECTED:
        merged_at = [i for i, obs in enumerate(observations) if obs[source] is not None]
        if merged_at:
            assert all(observations[i][source] is not None
                       for i in range(merged_at[0], len(observations)))
    assert load_concept_aliases(portfolio) == _EXPECTED


# --------------------------------------------------------------------------- fail-closed reads

def test_a_poisoned_curation_ledger_is_not_reported_as_nothing_to_do(portfolio):
    """"The policy is unreadable" must never read as "the taxonomy is clean"."""
    from looplab.engine.governance_health import GovernanceLedgerUnavailable

    with (portfolio / "concept_curation_log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    with pytest.raises(GovernanceLedgerUnavailable):
        ratify_concept_merges(portfolio, at="now")
    assert load_concept_aliases(portfolio) == {}


def test_a_merge_whose_source_left_the_portfolio_is_declined(tmp_path):
    """`require_existing` is what keeps a stale proposal from minting policy about nothing."""
    memory_dir = tmp_path / "mem"
    _write_capsules(memory_dir, [_capsule("run-a", ["axis/live"])])
    _append_proposal(memory_dir, merges=[_merge("axis/gone", "axis/live")])
    result = ratify_concept_merges(memory_dir, at="now")
    assert [e["outcome"] for e in result["skipped"]] == ["absent_from_portfolio"]
    assert load_concept_aliases(memory_dir) == {}


def test_the_receipt_records_what_landed_and_what_is_left_for_the_operator(tmp_path):
    memory_dir = tmp_path / "mem"
    _write_capsules(memory_dir, [_capsule("run-a", ["a/one", "a/two", "noise/x"])])
    _append_proposal(memory_dir, merges=[_merge("a/two", "a/one", why="same thing")],
                     purges=[{"from_concept": "noise/x"}])
    ratify_concept_merges(memory_dir, at="2026-08-06T00:00:00", by=RATIFIER_ACTOR)

    receipts = read_ratification_receipts(memory_dir)
    assert len(receipts) == 1
    applied = receipts[0]["applied"]
    assert applied[0]["from"] == "a/two" and applied[0]["to"] == "a/one"
    assert applied[0]["why"] == "same thing"
    # The join back to the agent's reasoning is the action id, which is also the alias row's.
    assert applied[0]["action_id"] == _alias_rows(memory_dir)[0]["action_id"]
    assert applied[0]["proposal_key"].startswith("concept:v2:")
    assert receipts[0]["pending"] == {"splits": 0, "purges": 1}
