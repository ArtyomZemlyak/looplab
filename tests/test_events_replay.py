"""I1 keystone: event store durability + replay determinism (the #1 P0 risk)."""
from __future__ import annotations

import pytest

from looplab.core.models import Event
from looplab.events.eventstore import EventStore, iter_jsonl
from looplab.events.replay import fold
from looplab.search.archive import DiversityArchive


def _seed(store: EventStore) -> None:
    store.append("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"x": 1.0}}, "code": ""})
    store.append("node_evaluated", {"node_id": 0, "metric": 0.5, "violations": []})


def _seed_events(store: EventStore) -> None:
    store.append("run_started", {"run_id": "r1", "task_id": "t", "goal": "g", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": ""}})
    store.append("node_evaluated", {"node_id": 0, "metric": 5.0})
    store.append("node_created", {"node_id": 1, "parent_ids": [0], "operator": "improve",
                                  "idea": {"operator": "improve", "params": {"x": 2.0}, "rationale": ""}})
    store.append("node_evaluated", {"node_id": 1, "metric": 2.0})


def test_replay_is_deterministic(tmp_path):
    p = tmp_path / "events.jsonl"
    _seed_events(EventStore(p))

    a = fold(EventStore(p).read_all())
    b = fold(EventStore(p).read_all())
    assert a.model_dump() == b.model_dump()
    # best is the lower metric, deterministically
    assert a.best_node_id == 1
    assert a.best().metric == 2.0


def test_torn_final_line_is_ignored(tmp_path):
    """A crash mid-append leaves a partial last line; read_all must drop it and the
    surviving prefix must replay to a consistent state."""
    p = tmp_path / "events.jsonl"
    _seed_events(EventStore(p))

    full = fold(EventStore(p).read_all())

    # Simulate a torn write: append a partial (no trailing newline) record.
    with open(p, "ab") as f:
        f.write(b'{"seq": 99, "ts": 0, "type": "node_eval')  # truncated, no newline

    after = fold(EventStore(p).read_all())
    assert after.model_dump() == full.model_dump()  # torn record had no effect


def test_seq_is_monotonic_and_resumes(tmp_path):
    p = tmp_path / "events.jsonl"
    s1 = EventStore(p)
    _seed_events(s1)
    last = list(s1.read_all())[-1].seq
    # A fresh store on the same file must continue numbering, not restart.
    s2 = EventStore(p)
    e = s2.append("run_finished", {})
    assert e.seq == last + 1


# --- fold tolerance for corrupt / hand-edited logs (second review pass) ---------------------------

def test_fold_tolerates_null_metric_node(tmp_path):
    # a hand-edited/BYO node_evaluated with metric=null folds to an evaluated node — best-selection and
    # the diversity archive must skip it, not crash with TypeError(None < float) and brick every re-fold.
    s = EventStore(tmp_path / "events.jsonl")
    _seed(s)
    s.append("node_created", {"node_id": 1, "parent_ids": [], "operator": "improve",
                              "idea": {"operator": "improve", "params": {"x": 2.0}}, "code": ""})
    s.append("node_evaluated", {"node_id": 1, "metric": None, "violations": []})
    st = fold(s.read_all())                  # raised TypeError before the fix
    assert st.best_node_id == 0              # null-metric node skipped; node 0 wins
    DiversityArchive(0.1).summary(st)        # archive must also tolerate the null-metric node


def test_fold_quarantines_non_numeric_and_non_finite_node_metrics(tmp_path):
    s = EventStore(tmp_path / "events.jsonl")
    _seed(s)
    s.append("node_created", {"node_id": 1, "parent_ids": [], "operator": "improve",
                              "idea": {"operator": "improve", "params": {}}, "code": ""})
    s.append("node_evaluated", {"node_id": 1, "metric": "oops"})
    st = fold(s.read_all())
    assert st.best_node_id == 0 and st.nodes[1].metric is None

    events = list(s.read_all())
    for node_id, metric in ((2, float("nan")), (3, float("inf")), (4, float("-inf")),
                            (5, 10**400)):
        events.extend([
            Event(type="node_created", data={
                "node_id": node_id, "parent_ids": [], "operator": "improve",
                "idea": {"operator": "improve", "params": {}}, "code": ""}),
            Event(type="node_evaluated", data={"node_id": node_id, "metric": metric}),
        ])
    st = fold(events)
    assert st.best_node_id == 0
    assert [node.id for node in st.feasible_nodes()] == [0]
    assert all(st.nodes[node_id].metric is None for node_id in (1, 2, 3, 4, 5))

    class HostileInt(int):
        def __float__(self):
            raise TypeError("hostile numeric adapter")

    from looplab.core.fitness import is_usable_metric
    assert is_usable_metric(HostileInt(1)) is False


def test_malformed_confirm_and_holdout_scalars_cannot_poison_selection():
    events = [
        Event(type="run_started", data={"run_id": "r", "task_id": "t", "direction": "min",
                                               "holdout_select": True}),
        Event(type="node_created", data={"node_id": 0, "parent_ids": [], "operator": "draft",
                                         "idea": {"operator": "draft", "params": {}}, "code": ""}),
        Event(type="node_evaluated", data={"node_id": 0, "metric": 1.0}),
        Event(type="node_confirmed", data={"node_id": 0, "mean": "bad", "std": float("inf"),
                                           "seeds": "3"}),
        Event(type="holdout_evaluated", data={"node_id": 0, "metric": float("nan")}),
    ]
    st = fold(events)
    assert st.best_node_id == 0 and st.best().metric == 1.0
    assert st.nodes[0].confirmed_mean is None and st.nodes[0].confirmed_std is None
    assert st.nodes[0].confirmed_seeds is None and st.nodes[0].holdout_metric is None


def test_fold_skips_malformed_node_created(tmp_path):
    s = EventStore(tmp_path / "events.jsonl")
    _seed(s)
    s.append("node_created", {"node_id": 2})  # missing operator/idea — skip, don't crash the whole fold
    st = fold(s.read_all())
    assert 2 not in st.nodes and 0 in st.nodes


def test_direction_normalized_in_fold(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "Maximize"})
    assert fold(s.read_all()).direction == "min"      # invalid -> safe default, never inverts
    s2 = EventStore(tmp_path / "e2.jsonl")
    s2.append("run_started", {"run_id": "r", "task_id": "t", "direction": "MAX"})
    assert fold(s2.read_all()).direction == "max"     # case-insensitive valid value accepted


def test_fold_idempotent_to_duplicate_terminal_events(tmp_path):
    # A duplicate node_evaluated (corrupt/hand-edited log) must not double-count eval time.
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}}, "code": ""})
    s.append("node_evaluated", {"node_id": 0, "metric": 1.0, "eval_seconds": 2.0})
    s.append("node_evaluated", {"node_id": 0, "metric": 1.0, "eval_seconds": 2.0})  # dup
    st = fold(s.read_all())
    assert st.total_eval_seconds == 2.0          # counted once, not 4.0


def test_fold_idempotent_to_duplicate_node_created_lifecycle(tmp_path):
    # A DUPLICATE complete lifecycle (node_created + terminal for the SAME id, from a corrupt /
    # double-appended / hand-edited log) must not double-charge the budget. Before first-create-wins,
    # the second node_created replaced node 0 with a fresh status=pending Node, re-arming the
    # first_terminal guard so the second node_evaluated re-added its eval_seconds (2.0 -> 4.0).
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}}, "code": ""})
    s.append("node_evaluated", {"node_id": 0, "metric": 1.0, "eval_seconds": 2.0})
    # duplicate whole lifecycle — the second create must NOT reset node 0 off `evaluated`
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}}, "code": ""})
    s.append("node_evaluated", {"node_id": 0, "metric": 1.0, "eval_seconds": 2.0})
    st = fold(s.read_all())
    assert st.total_eval_seconds == 2.0          # counted once, not 4.0
    assert st.nodes[0].status.name == "evaluated"


def test_fold_duplicate_node_created_does_not_flip_settled_metric(tmp_path):
    # A duplicate node_created carrying DIFFERENT terminal data must not overwrite the settled
    # node (first-create-wins): the first lifecycle's metric/status is authoritative.
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}}, "code": ""})
    s.append("node_evaluated", {"node_id": 0, "metric": 1.0, "eval_seconds": 2.0})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}}, "code": ""})
    s.append("node_failed", {"node_id": 0, "error": "boom", "eval_seconds": 5.0})   # conflicting terminal
    st = fold(s.read_all())
    assert st.nodes[0].status.name == "evaluated" and st.nodes[0].metric == 1.0
    assert st.total_eval_seconds == 2.0


def test_fold_node_tombstoned_excludes_from_selection(tmp_path):
    # Append-only delete (§6.3): a node_tombstoned event marks the subtree logically deleted — the
    # node STAYS in st.nodes (parent links resolve) but is excluded from every selection helper, so
    # it can't be chosen best or bred from. Here node 1 is the min (best) until it is tombstoned.
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    _n(s, 0, 5.0)
    _n(s, 1, 1.0)          # strictly better -> best while alive
    st = fold(s.read_all())
    assert st.best_node_id == 1
    s.append("node_tombstoned", {"node_ids": [1]})
    st = fold(s.read_all())
    assert 1 in st.nodes and st.nodes[1].tombstoned          # kept in the log, flagged
    assert st.best_node_id == 0                              # excluded from best-pick
    assert 1 not in {n.id for n in st.evaluated_nodes()}
    assert 1 not in {n.id for n in st.feasible_nodes()}


def test_fold_node_tombstoned_idempotent_and_tolerant(tmp_path):
    # Duplicate / overlapping tombstone events are a no-op; a forged/unknown id is skipped, not a crash.
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    _n(s, 0, 5.0)
    s.append("node_tombstoned", {"node_ids": [0, 999, "bad", None]})   # 999/"bad"/None ignored
    s.append("node_tombstoned", {"node_ids": [0]})                     # duplicate -> no-op
    st = fold(s.read_all())
    assert st.nodes[0].tombstoned and st.best_node_id is None          # nothing selectable left


def test_fold_tombstoned_pending_node_not_re_evaluated(tmp_path):
    # A pending node whose subtree was tombstoned must not be handed back to the eval loop on resume.
    from looplab.core.models import NodeStatus
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}}, "code": ""})
    st = fold(s.read_all())
    assert st.nodes[0].status is NodeStatus.pending and st.pending_nodes()
    s.append("node_tombstoned", {"node_ids": [0]})
    st = fold(s.read_all())
    assert st.pending_nodes() == []


def test_tombstone_invalidates_confirmation_approval_and_late_builds(tmp_path):
    """A logically deleted node cannot re-enter selection through an explicit override or late work."""
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    _n(s, 0, 5.0)
    _n(s, 1, 1.0)
    s.append("best_confirmed", {
        "node_id": 1, "generations": {"0": 0, "1": 0}, "significant": True})
    s.append("approval_granted", {"node_id": 1, "generation": 0})
    s.append("promote", {"node_id": 1, "alias": "winner"})
    s.append("node_tombstoned", {"node_ids": [1]})
    # A stale worker completes after deletion. It must not replace the tombstoned record.
    s.append("node_created", {
        "node_id": 1, "generation": 0, "parent_ids": [], "operator": "improve",
        "idea": {"operator": "improve", "params": {"late": True}}, "code": "late"})
    st = fold(s.read_all())
    assert st.nodes[1].tombstoned and st.nodes[1].code != "late"
    assert st.best_node_id == 0
    assert not st.confirmed_done and not st.approved and st.approved_node_id is None
    assert st.champion is None


def test_tombstoned_parent_and_confirmation_snapshot_are_inactive(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    _n(s, 0, 2.0)
    _n(s, 1, 1.0)
    s.append("node_tombstoned", {"node_ids": [1]})
    # Confirmation snapshots cover the ACTIVE set only; a deleted audit record is not a candidate.
    s.append("best_confirmed", {
        "node_id": 0, "generations": {"0": 0}, "significant": False})
    # A child built from a now-deleted parent is stale and must never land.
    s.append("node_created", {
        "node_id": 2, "generation": 0, "parent_ids": [1],
        "parent_generations": {"1": 0}, "operator": "improve",
        "idea": {"operator": "improve", "params": {}}, "code": "child"})
    st = fold(s.read_all())
    assert st.confirmed_done and st.best_node_id == 0
    assert 2 not in st.nodes


@pytest.mark.parametrize("event_type,event_data", [
    ("node_tombstoned", {"node_ids": [1]}),
    ("node_abort", {"node_id": 1, "generation": 0}),
])
def test_posthoc_finished_node_removal_preserves_finish_and_evidence_until_actual_resume(
        tmp_path, event_type, event_data):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {
        "run_id": "r", "task_id": "t", "direction": "min", "holdout_select": True})
    _n(s, 0, 2.0)
    _n(s, 1, 1.0)
    s.append("confirm_eval", {
        "node_id": 0, "generation": 0, "seed": 1, "metric": 2.1, "eval_seconds": 1.0})
    s.append("node_confirmed", {
        "node_id": 0, "generation": 0, "mean": 2.1, "std": 0.0, "seeds": 1})
    s.append("proxy_scored", {"node_id": 0, "generation": 0, "score": 2.2})
    s.append("holdout_evaluated", {
        "node_id": 0, "generation": 0, "metric": 2.3, "search_epoch": 0})
    s.append("holdout_evaluated", {
        "node_id": 1, "generation": 0, "metric": 1.3, "search_epoch": 0})
    s.append("best_confirmed", {
        "node_id": 1, "generations": {"0": 0, "1": 0}, "significant": True})
    s.append("approval_granted", {"node_id": 0, "generation": 0})
    s.append("report_generated", {"content": {"summary": "sealed"}})
    finish = s.append("run_finished", {"finalization_required": True})
    s.append("finalization_finished", {"finish_seq": finish.seq})

    s.append(event_type, event_data)
    posthoc = fold(s.read_all())
    assert posthoc.finished and posthoc.search_epoch == 0
    assert not posthoc.finalization_pending() and posthoc.report["summary"] == "sealed"
    assert posthoc.confirmed_done and posthoc.approved and posthoc.approved_node_id == 0
    assert posthoc.nodes[0].metric == 2.0 and posthoc.nodes[0].confirmed_mean == 2.1
    assert posthoc.nodes[1].metric == 1.0 and posthoc.nodes[1].holdout_metric == 1.3
    assert posthoc.holdout_evaluated_ids == [0, 1]

    # The explicit resume is the one and only epoch edge. It rotates the disclosed partition and
    # requeues only surviving active incumbents; removed-node evidence remains audit-visible.
    s.append("resume", {})
    reopened = fold(s.read_all())
    assert not reopened.finished and reopened.search_epoch == 1
    assert reopened.nodes[0].attempt == 1 and reopened.nodes[0].status.value == "pending"
    assert reopened.nodes[0].metric is None and reopened.nodes[0].confirmed_mean is None
    assert 0 not in reopened.proxy_scores and 0 not in reopened.confirm_seed_results
    assert reopened.nodes[1].metric == 1.0 and reopened.nodes[1].holdout_metric == 1.3
    s.append("resume", {})
    assert fold(s.read_all()).search_epoch == 1


def test_reset_of_any_confirmation_competitor_invalidates_winner_snapshot(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    _n(s, 0, 5.0)
    _n(s, 1, 1.0)
    # Robust confirmation deliberately overrode the raw winner with node 0.
    s.append("best_confirmed", {
        "node_id": 0, "generations": {"0": 0, "1": 0}, "significant": True})
    assert fold(s.read_all()).best_node_id == 0
    # Reset the OTHER competitor and land a fresh, even better lifecycle. The old whole-set
    # generations snapshot is invalid and may not keep overriding the new result.
    s.append("node_reset", {"node_id": 1, "generation": 0, "from_stage": "eval"})
    s.append("node_evaluated", {"node_id": 1, "generation": 1, "metric": 0.5})
    st = fold(s.read_all())
    assert not st.confirmed_done and st.best_node_id == 1


def test_fold_legacy_best_confirmed_survives_an_unrelated_abort(tmp_path):
    """Backward-compat (invariant 5b): a pre-batch `best_confirmed` carries NO `generations` map
    (modern producers always stamp it). It must still mark confirmation complete — and keep its
    robust-winner override — even when some OTHER node was later aborted. The new map-validation's
    legacy branch used to reject the event outright whenever any node was aborted/tombstoned, which
    silently dropped `confirmed_done` and the winner override on replay of an old log."""
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    _n(s, 0, 2.0)                      # confirmation's robust winner (override)
    _n(s, 1, 1.0)                      # raw best (min) — would win WITHOUT the confirmed override
    _n(s, 2, 3.0)                      # an also-ran…
    s.append("node_abort", {"node_id": 2, "reason": "ui"})   # …later aborted (legacy, unstamped)
    s.append("best_confirmed", {"node_id": 0, "significant": True})   # legacy: no generations map
    st = fold(s.read_all())
    assert st.confirmed_done is True
    assert st.best_node_id == 0        # the confirmed override survives the unrelated abort
    # A legacy best_confirmed naming an aborted/tombstoned WINNER is still correctly rejected.
    s2 = EventStore(tmp_path / "e2.jsonl")
    s2.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    _n(s2, 0, 2.0)
    _n(s2, 1, 1.0)
    s2.append("node_abort", {"node_id": 0, "reason": "ui"})
    s2.append("best_confirmed", {"node_id": 0, "significant": True})   # winner itself aborted
    assert fold(s2.read_all()).confirmed_done is False


def test_fold_legacy_node_reset_after_holdout_leaves_other_incumbents_intact(tmp_path):
    """Backward-compat (invariant 5b): a pre-batch `node_reset` carries no generation stamp and
    predates search epochs. The new 'requeue every surviving incumbent on a disclosed holdout' epoch
    rotation must NOT fire for such a legacy event — else replaying an old log wipes the OTHER
    incumbents' metrics and bumps the epoch, diverging from the pre-batch fold of the same bytes
    (which reset only the target node)."""
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    _n(s, 0, 2.0)
    _n(s, 1, 1.0)
    s.append("holdout_evaluated", {"node_id": 0, "metric": 2.3})   # legacy: no generation/search_epoch
    s.append("holdout_evaluated", {"node_id": 1, "metric": 1.3})
    s.append("node_reset", {"node_id": 0, "from_stage": "eval"})   # legacy: no generation stamp
    st = fold(s.read_all())
    # The reset target IS reopened…
    assert st.nodes[0].status.value == "pending" and st.nodes[0].metric is None
    # …but the OTHER incumbent is untouched (the bug requeued it: attempt->1, metric+holdout wiped).
    assert st.nodes[1].status.value == "evaluated"
    assert st.nodes[1].metric == 1.0
    assert st.nodes[1].attempt == 0
    assert st.nodes[1].holdout_metric == 1.3
    assert st.search_epoch == 0        # no epoch rotation for a legacy reset
    # A MODERN (stamped) reset after a disclosed holdout still requeues the incumbents + rotates.
    s.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 2.0})
    s.append("holdout_evaluated", {"node_id": 0, "generation": 1, "metric": 2.3, "search_epoch": 0})
    s.append("holdout_evaluated", {"node_id": 1, "generation": 0, "metric": 1.3, "search_epoch": 0})
    s.append("node_reset", {"node_id": 0, "generation": 1, "from_stage": "eval"})
    st2 = fold(s.read_all())
    assert st2.search_epoch == 1
    assert st2.nodes[1].status.value == "pending" and st2.nodes[1].metric is None


def test_fold_confirm_and_holdout_bound_to_attempt(tmp_path):
    # P0-1: confirm-seed / node_confirmed / holdout_evaluated events stamped with an attempt the node
    # has since abandoned (node_reset bumped the generation) must be dropped — their stale metrics must
    # not land on the post-reset code, and the stale confirm-seed cost must not inflate the budget.
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}}, "code": "c"})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.5, "eval_seconds": 0.5, "attempt": 0})
    s.append("node_reset", {"node_id": 0, "from_stage": "implement"})     # attempt 0 -> 1
    # LATE events from the abandoned attempt 0 (their eval was in flight when the reset landed)
    s.append("confirm_eval", {"node_id": 0, "seed": 1, "attempt": 0, "eval_seconds": 2.0, "metric": 0.4})
    s.append("node_confirmed", {"node_id": 0, "attempt": 0, "mean": 0.4, "std": 0.0, "seeds": 3})
    s.append("holdout_evaluated", {"node_id": 0, "attempt": 0, "metric": 0.3})
    st = fold(s.read_all())
    n = st.nodes[0]
    assert n.confirmed_mean is None and n.holdout_metric is None    # stale confirm/holdout dropped
    assert 0 not in st.holdout_evaluated_ids                        # gate not marked by a stale attempt
    assert 0 not in st.confirm_seed_results                         # stale confirm-seed not memoized
    assert st.total_eval_seconds == 0.5                             # stale confirm cost (2.0) NOT added
    # A FRESH confirm at the current attempt (1) lands only after that lifecycle is rebuilt+evaluated.
    s.append("node_created", {
        "node_id": 0, "generation": 1, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}}, "code": "new"})
    s.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 0.25})
    s.append("node_confirmed", {"node_id": 0, "attempt": 1, "mean": 0.2, "std": 0.0, "seeds": 3})
    assert fold(s.read_all()).nodes[0].confirmed_mean == 0.2


def test_confirm_and_holdout_results_require_an_evaluated_current_lifecycle(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}}, "code": "c"})
    # Forged/out-of-order effects cannot attach to pending code or poison the confirm cost key.
    s.append("confirm_eval", {
        "node_id": 0, "generation": 0, "seed": 1, "metric": 9.0, "eval_seconds": 0.0})
    s.append("node_confirmed", {
        "node_id": 0, "generation": 0, "mean": 9.0, "std": 0.0, "seeds": 1})
    s.append("holdout_evaluated", {
        "node_id": 0, "generation": 0, "metric": 9.0, "search_epoch": 0})
    pending = fold(s.read_all())
    assert pending.confirm_seed_results == {} and pending.total_eval_seconds == 0.0
    assert pending.nodes[0].confirmed_mean is None and pending.nodes[0].holdout_metric is None

    s.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 1.0})
    s.append("confirm_eval", {
        "node_id": 0, "generation": 0, "seed": 1, "metric": 1.1, "eval_seconds": 3.0})
    s.append("node_confirmed", {
        "node_id": 0, "generation": 0, "mean": 1.1, "std": 0.0, "seeds": 1})
    s.append("holdout_evaluated", {
        "node_id": 0, "generation": 0, "metric": 1.2, "search_epoch": 0})
    evaluated = fold(s.read_all())
    assert evaluated.confirm_seed_results == {0: {1: 1.1}}
    assert evaluated.total_eval_seconds == 3.0
    assert evaluated.nodes[0].confirmed_mean == 1.1 and evaluated.nodes[0].holdout_metric == 1.2


def test_fold_reopen_rehides_holdout(tmp_path):
    # P0-2: reopening a finished run bumps the search epoch and CLEARS the disclosed holdout (gate +
    # node metrics) so the new epoch re-scores its leaders on a fresh split; a holdout score stamped
    # with the prior epoch is dropped after the reopen, and a current-epoch one lands.
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min", "holdout_select": True})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}}, "code": "c"})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.5})
    s.append("holdout_evaluated", {"node_id": 0, "metric": 0.4, "search_epoch": 0})
    s.append("best_confirmed", {"node_id": 0, "significant": True})
    s.append("run_finished", {"reason": "done"})
    st = fold(s.read_all())
    assert 0 in st.holdout_evaluated_ids and st.nodes[0].holdout_metric == 0.4
    s.append("run_reopened", {})                                     # epoch 0 -> 1
    st = fold(s.read_all())
    assert st.search_epoch == 1
    assert list(st.holdout_evaluated_ids) == [] and st.nodes[0].holdout_metric is None
    # a STALE holdout score (prior epoch 0) after the reopen is dropped
    s.append("holdout_evaluated", {"node_id": 0, "metric": 0.99, "search_epoch": 0})
    st = fold(s.read_all())
    assert st.nodes[0].holdout_metric is None and 0 not in st.holdout_evaluated_ids
    # Even a current-epoch score cannot land before the incumbent's fresh raw eval finishes.
    s.append("holdout_evaluated", {
        "node_id": 0, "generation": 1, "metric": 0.88, "search_epoch": 1})
    assert fold(s.read_all()).nodes[0].holdout_metric is None
    s.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 0.45})
    # A FRESH holdout score at the current epoch (1) now lands.
    s.append("holdout_evaluated", {
        "node_id": 0, "generation": 1, "metric": 0.2, "search_epoch": 1})
    st = fold(s.read_all())
    assert st.nodes[0].holdout_metric == 0.2 and 0 in st.holdout_evaluated_ids


def test_unfinished_reset_after_holdout_rotates_hidden_epoch(tmp_path):
    """A control winning after holdout scoring but before run_finished cannot reuse the revealed split."""
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {
        "run_id": "r", "task_id": "t", "direction": "min", "holdout_select": True})
    _n(s, 0, 2.0)
    _n(s, 1, 1.0)
    s.append("confirm_eval", {
        "node_id": 0, "generation": 0, "seed": 1, "metric": 2.1, "eval_seconds": 1.0})
    s.append("node_confirmed", {
        "node_id": 0, "generation": 0, "mean": 2.1, "std": 0.0, "seeds": 1})
    s.append("proxy_scored", {"node_id": 0, "generation": 0, "score": 2.2})
    s.append("stage_finished", {
        "node_id": 1, "generation": 0, "name": "train", "status": "ok"})
    s.append("stage_finished", {
        "node_id": 1, "generation": 0, "name": "score", "status": "ok"})
    s.append("holdout_evaluated", {
        "node_id": 0, "generation": 0, "metric": 2.5, "search_epoch": 0})
    s.append("holdout_evaluated", {
        "node_id": 1, "generation": 0, "metric": 1.5, "search_epoch": 0})
    assert not fold(s.read_all()).finished
    s.append("node_reset", {"node_id": 1, "generation": 0, "from_stage": "score"})
    st = fold(s.read_all())
    assert st.search_epoch == 1 and not st.holdout_evaluated_ids
    assert all(n.holdout_metric is None for n in st.nodes.values())
    assert st.nodes[1].attempt == 1 and st.nodes[1].status.value == "pending"
    assert st.nodes[1].rerun_stage is None and st.nodes[1].stages == []
    assert st.nodes[0].attempt == 1 and st.nodes[0].status.value == "pending"
    assert st.nodes[0].metric is None and st.nodes[0].confirmed_mean is None
    assert 0 not in st.confirm_seed_results and 0 not in st.proxy_scores
    s.append("holdout_evaluated", {
        "node_id": 0, "generation": 0, "metric": 999.0, "search_epoch": 0})
    assert fold(s.read_all()).nodes[0].holdout_metric is None


def test_new_candidate_or_resume_after_disclosed_holdout_rotates_epoch(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {
        "run_id": "r", "task_id": "t", "direction": "min", "holdout_select": True})
    _n(s, 0, 1.0)
    s.append("holdout_evaluated", {
        "node_id": 0, "generation": 0, "metric": 1.2, "search_epoch": 0})
    s.append("node_created", {
        "node_id": 1, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}}, "code": "new"})
    st = fold(s.read_all())
    assert st.search_epoch == 1 and not st.holdout_evaluated_ids
    assert st.nodes[0].attempt == 1 and st.nodes[0].status.value == "pending"
    assert st.nodes[0].metric is None and st.nodes[1].attempt == 0
    assert st.nodes[1].status.value == "pending" and st.nodes[1].code == "new"
    # A second disclosure followed by pause/resume is another search reopening, even though the
    # old engine never managed to append run_finished.
    s.append("node_evaluated", {
        "node_id": 0, "generation": 1, "metric": 1.1})
    s.append("holdout_evaluated", {
        "node_id": 0, "generation": 1, "metric": 1.3, "search_epoch": 1})
    s.append("pause", {})
    s.append("resume", {})
    st = fold(s.read_all())
    assert st.search_epoch == 2 and not st.holdout_evaluated_ids


def test_fold_resume_intent_seq_gated(tmp_path):
    # P1-1: a resume_requested newer than the last resume_served is a PENDING (unfulfilled) resume;
    # a serve at a later seq fulfills it, and one serve satisfies several piled-up requests.
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    assert not fold(s.read_all()).resume_pending()
    s.append("resume_requested", {})
    st = fold(s.read_all())
    assert st.resume_pending() and st.last_resume_request_ts > 0
    s.append("resume_served", {})
    assert not fold(s.read_all()).resume_pending()             # fulfilled by a later-seq serve
    s.append("resume_requested", {})
    s.append("resume_requested", {})                            # two piled-up requests
    assert fold(s.read_all()).resume_pending()
    s.append("resume_served", {})
    assert not fold(s.read_all()).resume_pending()             # one serve satisfies both


def test_restart_is_one_durable_operator_pause_and_resume_request(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("resume_served", {})  # historical owner acknowledgement cannot satisfy a new restart
    restart = s.append("restart", {})

    stopped = fold(s.read_all())
    assert stopped.paused is True and stopped.pause_node_id is None
    assert stopped.pause_event_seq == restart.seq
    assert stopped.resume_pending() is True
    assert stopped.last_resume_request_seq == restart.seq
    assert stopped.last_resume_request_mode == "resume"

    # Only the replacement CLI lifts the operator pause. The served marker then fulfills this exact
    # durable handoff, so replay after any process/browser restart reaches the same settled state.
    s.append("resume", {})
    assert not fold(s.read_all()).paused and fold(s.read_all()).resume_pending()
    served = s.append("resume_served", {})
    resumed = fold(s.read_all())
    assert served.seq > restart.seq and not resumed.paused and not resumed.resume_pending()


def test_append_expected_last_seq_cas(tmp_path):
    # P1-12 explicit-seq optimistic concurrency: an append with expected_last_seq lands only if the
    # log tail is still exactly that seq — else it raises and writes NOTHING.
    import pytest
    from looplab.events.eventstore import EventStoreConcurrencyError
    s = EventStore(tmp_path / "e.jsonl")
    e0 = s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    e1 = s.append("pause", {}, expected_last_seq=e0.seq)       # tail matches -> lands
    assert e1.seq == e0.seq + 1
    with pytest.raises(EventStoreConcurrencyError):
        s.append("resume", {}, expected_last_seq=e0.seq)       # tail moved to e1 -> conflict
    assert [ev.type for ev in s.read_all()] == ["run_started", "pause"]   # rejected write left no trace
    # None (default) is unconditional, unchanged behavior
    s.append("resume", {})
    assert [ev.type for ev in s.read_all()][-1] == "resume"


def test_fold_eval_seconds_split_into_buckets(tmp_path):
    # P1-2: total_eval_seconds is ALSO split by category (node vs confirm) for observability; the
    # buckets sum to the total, and confirm-seed cost is tracked apart from node-eval cost.
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}}, "code": "c"})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.5, "eval_seconds": 3.0})
    s.append("confirm_eval", {"node_id": 0, "seed": 1, "eval_seconds": 2.0, "metric": 0.4})
    s.append("confirm_eval", {"node_id": 0, "seed": 2, "eval_seconds": 1.0, "metric": 0.45})
    st = fold(s.read_all())
    assert st.eval_seconds_by_kind == {"node": 3.0, "confirm": 3.0}
    assert st.total_eval_seconds == 6.0 == sum(st.eval_seconds_by_kind.values())


def test_log_divergence_detects_mid_file_corruption(tmp_path):
    from looplab.events.eventstore import log_divergence
    p = tmp_path / "events.jsonl"
    # 2 good records, a COMPLETE corrupt line, then a valid tail record — iter_jsonl would silently
    # drop the tail (break at the corrupt line); log_divergence must flag it.
    p.write_bytes(b'{"seq":0,"type":"a"}\n{"seq":1,"type":"b"}\n{corrupt json\n{"seq":2,"type":"c"}\n')
    assert log_divergence(p) == {"good_records": 2, "corrupt_line": 3, "dropped_lines": 1}


def test_log_divergence_ignores_a_torn_tail(tmp_path):
    from looplab.events.eventstore import log_divergence
    p = tmp_path / "events.jsonl"
    # a torn/partial FINAL line (no trailing newline) is the normal crash-mid-append case, not a
    # mid-file divergence — must return None.
    p.write_bytes(b'{"seq":0,"type":"a"}\n{"seq":1,"type":"partial')
    assert log_divergence(p) is None
    # A newline-terminated corrupt line is COMPLETE, so the next append would otherwise grow an
    # invisible tail behind it. It must fail closed even before a later record exists.
    p.write_bytes(b'{"seq":0,"type":"a"}\n{corrupt\n')
    assert log_divergence(p) == {"good_records": 1, "corrupt_line": 2, "dropped_lines": 0}
    # a wholly clean log: None
    p.write_bytes(b'{"seq":0,"type":"a"}\n{"seq":1,"type":"b"}\n')
    assert log_divergence(p) is None


def test_fail_closed_detects_dict_valid_but_event_invalid_corruption(tmp_path):
    """Review of P0-4: read_all stops not only at non-JSON lines but at a dict that fails Event(**o)
    (a byte-flip renaming a required key like `type`). The divergence guard must match that stop
    condition, else such a corruption drops the tail on read yet appends past it, undetected."""
    from looplab.events.eventstore import EventStore, EventLogCorruptionError, log_divergence
    p = tmp_path / "events.jsonl"
    # line 3 is a valid JSON DICT but not a constructible Event (`type` renamed to `typ3`); line 4 valid
    p.write_bytes(b'{"seq":0,"type":"a"}\n{"seq":1,"type":"b"}\n'
                  b'{"seq":2,"typ3":"c"}\n{"seq":3,"type":"d"}\n')
    div = log_divergence(p)
    assert div and div["corrupt_line"] == 3 and div["dropped_lines"] == 1
    es = EventStore(p)
    assert es.divergence and es.divergence["corrupt_line"] == 3
    with pytest.raises(EventLogCorruptionError):
        es.append("resume", {})


def test_append_fails_closed_on_mid_file_corruption(tmp_path):
    """arch-review §3 P0-4: a store opened over a MID-FILE divergence must REFUSE to append — else
    the new record is durable on disk but invisible to fold (grows behind the corrupt boundary)."""
    from looplab.events.eventstore import EventStore, EventLogCorruptionError
    p = tmp_path / "events.jsonl"
    p.write_bytes(b'{"seq":0,"type":"a"}\n{"seq":1,"type":"b"}\n{corrupt\n{"seq":2,"type":"c"}\n')
    es = EventStore(p)
    assert es.divergence and es.divergence["corrupt_line"] == 3
    with pytest.raises(EventLogCorruptionError):
        es.append("resume", {})
    # a torn tail (no divergence) still appends fine — this is NOT the corruption case
    p.write_bytes(b'{"seq":0,"type":"a"}\n{"seq":1,"type":"partial')
    EventStore(p).append("c", {"x": 1})   # heals the torn tail, does not raise


def test_repair_log_truncates_backs_up_and_reopens(tmp_path):
    """`repair_log` backs up the original, truncates to the last valid boundary, records provenance,
    and leaves a log a fresh store can append to again."""
    from looplab.events.eventstore import EventStore, repair_log, iter_jsonl
    p = tmp_path / "events.jsonl"
    p.write_bytes(b'{"seq":0,"type":"a"}\n{"seq":1,"type":"b"}\n{corrupt\n{"seq":2,"type":"c"}\n')
    rec = repair_log(p)
    assert rec["good_records"] == 2 and rec["dropped_lines"] == 1 and rec["corrupt_line"] == 3
    assert (tmp_path / rec["backup"]).exists()                       # original preserved
    types = [r["type"] for r in iter_jsonl(p)]
    assert types == ["a", "b", "log_repaired"]                       # prefix + provenance, tail gone
    es = EventStore(p)
    assert es.divergence is None                                     # clean now
    es.append("resume", {})                                          # appends without raising
    assert repair_log(p) == {}                                       # idempotent no-op on a clean log


def test_eventstore_heals_torn_final_line(tmp_path):
    p = tmp_path / "events.jsonl"
    es = EventStore(p)
    es.append("a", {"x": 1})
    es.append("b", {"x": 2})
    # Simulate a crash mid-append: a partial final record with no trailing newline.
    with open(p, "ab") as f:
        f.write(b'{"seq":2,"ts":0,"type":"node_ev')
    # A fresh store (resume) must not glue its next record onto the torn line.
    es2 = EventStore(p)
    es2.append("c", {"x": 3})
    types = [r["type"] for r in iter_jsonl(p)]
    assert types == ["a", "b", "c"], types


def test_fold_tolerates_metric_less_evaluated_event(tmp_path):
    from looplab.events.replay import fold

    p = tmp_path / "events.jsonl"
    st_store = EventStore(p)
    st_store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    st_store.append("node_created",
                    {"node_id": 0, "parent_ids": [], "operator": "draft",
                     "idea": {"operator": "draft", "params": {}, "rationale": "r"}, "code": "c"})
    # malformed: node_evaluated with no metric key — must fold without KeyError
    st_store.append("node_evaluated", {"node_id": 0})
    st = fold(EventStore(p).read_all())
    assert 0 in st.nodes
    # metric-less node is excluded from the feasible set (can't be sorted/selected)
    assert st.nodes[0] not in st.feasible_nodes()


# C2 — confirm_eval events populate the per-seed resume memo
def test_fold_confirm_seed_results():
    from looplab.core.models import Event
    evs = [Event(type="run_started", data={"run_id": "r", "task_id": "t"}),
           Event(type="node_created", data={
               "node_id": 3, "parent_ids": [], "operator": "draft",
               "idea": {"operator": "draft", "params": {}}}),
           Event(type="node_evaluated", data={"node_id": 3, "metric": 1.0}),
           Event(type="confirm_eval", data={"node_id": 3, "seed": 0, "eval_seconds": 1.0, "metric": 0.5}),
           Event(type="confirm_eval", data={"node_id": 3, "seed": 1, "eval_seconds": 1.0, "metric": None})]
    st = fold(evs)
    assert st.confirm_seed_results == {3: {0: 0.5, 1: None}}


# Code-review pass: budget_extend must reject NON-FINITE values. `float("nan")`/`float("inf")` PASS the
# numeric coercion but `total_eval_seconds >= nan` is always False (budget silently disabled) / inf never
# trips — and the poison value re-folds on every resume, permanently. Reject it; keep the prior ceiling.
def test_budget_extend_rejects_nonfinite():
    from looplab.core.models import Event
    base = Event(type="run_started", data={"run_id": "r", "task_id": "t"})
    for bad in ("nan", "inf", "-inf", float("nan"), float("inf")):
        st = fold([base, Event(type="budget_extend", data={"max_eval_seconds": bad})])
        assert "max_eval_seconds" not in st.budget_overrides, bad
    # a FINITE string still coerces (the legitimate UI/TUI case the coercion exists for)
    st = fold([base, Event(type="budget_extend", data={"max_eval_seconds": "600", "max_seconds": "30"})])
    assert st.budget_overrides["max_eval_seconds"] == 600.0
    assert st.budget_overrides["max_seconds"] == 30.0


def test_policy_decision_tolerates_non_dict_scores():
    # A non-dict `scores` (list/str/number from a corrupt or hand-edited log) must not brick the fold
    # with an AttributeError (the whole run would become unopenable). It folds to empty policy_scores.
    from looplab.core.models import Event
    base = Event(type="run_started", data={"run_id": "r", "task_id": "t"})
    for bad in ([1, 2, 3], "oops", 5, None):
        st = fold([base, Event(type="policy_decision", data={"scores": bad, "chosen": 0})])
        assert st.policy_scores == {}, bad
    # a well-formed dict still parses (int-coerced keys)
    st = fold([base, Event(type="policy_decision", data={"scores": {"3": 0.5}, "chosen": 3})])
    assert st.policy_scores == {3: 0.5} and st.policy_chosen == 3


# A "reused" stage marker (a re-eval that SKIPPED a stage the inline-repair reuse kept) must NOT clobber
# the REAL completion record from the attempt that actually ran the stage — else the node reads as if it
# trained in 0s. Keep the informative record; order-tolerant (a real record still supersedes a reused).
def test_fold_reused_stage_marker_does_not_clobber_real_record():
    from looplab.core.models import Event
    base = [Event(type="run_started", data={"run_id": "r", "task_id": "t", "direction": "min"}),
            Event(type="node_created", data={"node_id": 0, "parent_ids": [], "operator": "draft",
                                             "idea": {"operator": "draft", "params": {}}}),
            Event(type="stage_finished", data={"node_id": 0, "name": "train", "status": "ok",
                                               "exit_code": 0, "seconds": 7200.0})]
    reused = Event(type="stage_finished", data={"node_id": 0, "name": "train", "status": "reused",
                                                "exit_code": 0, "seconds": 0.0})
    st = fold(base + [reused])
    train = next(s for s in st.nodes[0].stages if s["name"] == "train")
    assert train["status"] == "ok" and train["seconds"] == 7200.0   # real record kept, not the 0s reused one
    # order-tolerant: a real record arriving AFTER a reused marker still wins
    st2 = fold([base[0], base[1], reused, base[2]])
    train2 = next(s for s in st2.nodes[0].stages if s["name"] == "train")
    assert train2["status"] == "ok" and train2["seconds"] == 7200.0


# D14 — node_reset must clear the per-seed confirm memo along with confirmed_mean/std/seeds: the
# confirm phase memo-skips every seed already in confirm_seed_results, so a stale post-reset entry
# would re-emit node_confirmed from PRE-reset seed metrics for the post-reset code without running
# a single seed.
def test_fold_node_reset_clears_confirm_seed_memo():
    from looplab.core.models import Event
    base = [Event(type="run_started", data={"run_id": "r", "task_id": "t", "direction": "min"}),
            Event(type="node_created", data={"node_id": 0, "parent_ids": [], "operator": "draft",
                                             "idea": {"operator": "draft", "params": {}}, "code": "c"}),
            Event(type="node_created", data={"node_id": 1, "parent_ids": [], "operator": "draft",
                                             "idea": {"operator": "draft", "params": {}}, "code": "c"}),
            Event(type="node_evaluated", data={"node_id": 0, "metric": 1.0, "eval_seconds": 1.0}),
            Event(type="node_evaluated", data={"node_id": 1, "metric": 2.0}),
            Event(type="confirm_eval", data={"node_id": 0, "seed": 1, "eval_seconds": 2.0, "metric": 0.4}),
            Event(type="confirm_eval", data={"node_id": 1, "seed": 1, "eval_seconds": 2.0, "metric": 0.9}),
            Event(type="node_confirmed", data={"node_id": 0, "mean": 0.4, "std": 0.0, "seeds": 1})]
    st = fold(base)
    assert st.confirm_seed_results[0] == {1: 0.4}
    reset = Event(type="node_reset", data={"node_id": 0, "from_stage": "eval"})
    st2 = fold(base + [reset])
    assert 0 not in st2.confirm_seed_results        # memo gone: a later confirm re-runs the seeds
    assert st2.nodes[0].confirmed_mean is None
    assert st2.confirm_seed_results[1] == {1: 0.9}  # another node's memo is untouched
    # a POST-reset confirm_eval repopulates the memo, and its cost is counted again (the seed
    # genuinely re-ran) — order-tolerant and deterministic across re-folds.
    reevaluated = Event(type="node_evaluated", data={
        "node_id": 0, "generation": 1, "metric": 0.8})
    post = Event(type="confirm_eval", data={
        "node_id": 0, "generation": 1, "seed": 1, "eval_seconds": 3.0, "metric": 0.7})
    st3 = fold(base + [reset, reevaluated, post])
    assert st3.confirm_seed_results[0] == {1: 0.7}
    assert st3.total_eval_seconds == 1.0 + 2.0 + 2.0 + 3.0
    assert fold(base + [reset, reevaluated, post]).model_dump() == st3.model_dump()


# confirm-seed eval cost is first-occurrence accounted (like node terminals): a duplicated/
# double-folded confirm_eval must not inflate total_eval_seconds or make the budget order-sensitive.
def test_fold_confirm_eval_cost_deduped_on_duplicate():
    from looplab.core.models import Event
    base = [Event(type="run_started", data={"run_id": "r", "task_id": "t"}),
            Event(type="node_created", data={
                "node_id": 3, "parent_ids": [], "operator": "draft",
                "idea": {"operator": "draft", "params": {}}}),
            Event(type="node_evaluated", data={"node_id": 3, "metric": 1.0}),
            Event(type="confirm_eval", data={"node_id": 3, "seed": 0, "eval_seconds": 5.0, "metric": 0.5})]
    once = fold(base)
    dup = fold(base + [Event(type="confirm_eval",
                             data={"node_id": 3, "seed": 0, "eval_seconds": 5.0, "metric": 0.5})])
    assert once.total_eval_seconds == 5.0
    assert dup.total_eval_seconds == 5.0                      # counted once, not 10.0
    # distinct seeds still each contribute their cost
    two = fold(base + [Event(type="confirm_eval",
                             data={"node_id": 3, "seed": 1, "eval_seconds": 4.0, "metric": 0.6})])
    assert two.total_eval_seconds == 9.0


# #17/#18 — event seq advances only after a durable write; a non-dict line stops the reader
def test_eventstore_seq_and_nondict_guard(tmp_path):
    from looplab.events.eventstore import EventStore, iter_jsonl
    s = EventStore(tmp_path / "e.jsonl")
    s.append("a", {})
    s.append("b", {})
    assert [e.seq for e in s.read_all()] == [0, 1]
    with open(tmp_path / "e.jsonl", "ab") as f:
        f.write(b"5\n")                                             # valid JSON but not an object
    assert len(list(iter_jsonl(tmp_path / "e.jsonl"))) == 2         # stops cleanly, keeps the 2 records


# #6a event version
def test_event_envelope_has_version(tmp_path):
    s = EventStore(tmp_path / "events.jsonl")
    s.append("x", {"a": 1})
    e = list(s.read_all())[0]
    assert e.v == 1                                  # ADR-1 envelope version present


# --- Batch-1 P0 regressions (first framework mega-review) -----------------------------------------

def test_budget_extend_string_value_is_coerced_not_poison(tmp_path):
    """A UI/TUI can post `max_seconds` as a STRING; the engine compares it numerically, so an
    un-coerced string would TypeError in the loop and re-crash every resume. The fold coerces it."""
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("budget_extend", {"max_seconds": "600", "max_eval_seconds": "300", "max_parallel": "2"})
    bo = fold(s.read_all()).budget_overrides
    assert bo["max_seconds"] == 600.0 and isinstance(bo["max_seconds"], float)
    assert bo["max_parallel"] == 2 and isinstance(bo["max_parallel"], int)
    assert (0.0 >= bo["max_eval_seconds"]) is False        # numeric compare no longer raises
    # a non-numeric value is skipped, keeping the last good one
    s.append("budget_extend", {"max_seconds": "abc"})
    assert fold(s.read_all()).budget_overrides["max_seconds"] == 600.0


def test_conflicting_second_terminal_does_not_flip_the_node(tmp_path):
    """First-terminal-wins for the WHOLE node: a corrupt/double-appended node_failed after a
    node_evaluated must not flip the evaluated node to failed and drop its metric."""
    from looplab.core.models import NodeStatus
    s = EventStore(tmp_path / "e.jsonl")
    _seed_events(s)                                        # node 0 evaluated metric=5.0
    s.append("node_failed", {"node_id": 0, "error": "boom", "reason": "crash"})
    n = fold(s.read_all()).nodes[0]
    assert n.status is NodeStatus.evaluated and n.metric == 5.0   # not flipped to failed


def test_late_terminal_from_an_abandoned_attempt_is_rejected(tmp_path):
    """arch-review §3 P0-1: after a node_reset bumps the attempt generation, a LATE node_evaluated
    stamped with the OLD attempt (its eval was in flight when the reset happened) must be DROPPED — it
    can't land as the first-terminal-after-reset and accept a metric from discarded code. Its actual
    compute is still charged to the cumulative eval budget."""
    from looplab.core.models import NodeStatus
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": ""}})
    s.append("node_reset", {"node_id": 0, "from_stage": "implement"})   # attempt 0 -> 1
    # a LATE terminal from the pre-reset attempt (attempt=0) — must NOT be accepted
    s.append("node_evaluated", {"node_id": 0, "attempt": 0, "metric": 9.0, "eval_seconds": 3.0})
    st = fold(s.read_all())
    assert st.nodes[0].status is NodeStatus.pending          # still pending — late terminal dropped
    assert st.nodes[0].metric is None and st.total_eval_seconds == 3.0
    # the NEW attempt's terminal (attempt=1) IS accepted
    s.append("node_evaluated", {"node_id": 0, "attempt": 1, "metric": 1.0, "eval_seconds": 2.0})
    st2 = fold(s.read_all())
    assert st2.nodes[0].status is NodeStatus.evaluated and st2.nodes[0].metric == 1.0
    assert st2.total_eval_seconds == 5.0                     # discarded + live compute both counted


def test_rebuild_preserves_generation_and_rejects_every_late_lifecycle_effect(tmp_path):
    """An implement/propose reset emits a second node_created for the SAME id. That rebuild must keep
    generation 1, and every effect still arriving from generation 0 must be inert — not only terminals."""
    s = EventStore(tmp_path / "e.jsonl")
    created = {"node_id": 0, "parent_ids": [], "operator": "draft",
               "idea": {"operator": "draft", "params": {}, "rationale": ""}}
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min",
                             "trust_gate": "gate"})
    s.append("node_created", {**created, "code": "old"})
    s.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 5.0})
    s.append("reward_hack_suspected", {"node_id": 0, "generation": 0,
                                        "signals": [{"signal": "protected_missing"}]})
    s.append("node_abort", {"node_id": 0})
    s.append("node_reset", {"node_id": 0, "from_stage": "implement"})
    s.append("node_created", {**created, "generation": 1, "code": "new"})

    # All of these belong to the abandoned worker. None may mutate/gate the rebuilt node.
    s.append("node_repaired", {"node_id": 0, "generation": 0, "attempt": 1,
                                "code": "stale repair"})
    s.append("stage_finished", {"node_id": 0, "generation": 0, "name": "old",
                                 "status": "ok"})
    s.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 99.0,
                                 "eval_seconds": 9.0})
    s.append("confirm_eval", {"node_id": 0, "generation": 0, "seed": 1,
                               "metric": 99.0, "eval_seconds": 4.0})
    s.append("node_confirmed", {"node_id": 0, "generation": 0, "mean": 99.0,
                                 "std": 0.0, "seeds": 1})
    s.append("holdout_evaluated", {"node_id": 0, "generation": 0, "metric": 99.0})
    s.append("reward_hack_suspected", {"node_id": 0, "generation": 0,
                                        "signals": [{"signal": "grader_access"}]})
    s.append("agent_validated", {"node_id": 0, "generation": 0, "ok": False})
    s.append("proxy_scored", {"node_id": 0, "generation": 0, "score": 999.0,
                               "skipped": True})

    s.append("stage_finished", {"node_id": 0, "generation": 1, "name": "current",
                                 "status": "ok"})
    s.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 1.0,
                                 "eval_seconds": 2.0})
    st = fold(s.read_all())
    n = st.nodes[0]
    assert n.attempt == 1 and n.code == "new" and n.metric == 1.0
    assert [stage["name"] for stage in n.stages] == ["current"]
    assert n.confirmed_mean is None and n.holdout_metric is None and n.agent_report is None
    assert 0 not in st.confirm_seed_results and 0 not in st.holdout_evaluated_ids
    assert 0 not in st.aborted_nodes and 0 not in st.proxy_scores and 0 not in st.proxy_skipped
    assert st.total_eval_seconds == 6.0   # stale confirm seed cost 4 + current terminal cost 2
    assert st.best_node_id == 0 and st.breed_excluded == set()  # old trust flag is historical only


def test_reset_of_finished_approved_run_starts_one_new_epoch_and_reopens_gates(tmp_path):
    """node_reset is itself a reopen edge. It clears finished before a later resume can see it, so the
    reset handler must invalidate confirmation/approval and bump the epoch exactly once."""
    s = EventStore(tmp_path / "e.jsonl")
    _confirmed_finished_log(s)
    s.append("approval_requested", {"node_id": 0})
    s.append("approval_granted", {"node_id": 0})
    s.append("node_abort", {"node_id": 0})
    s.append("node_reset", {"node_id": 0, "from_stage": "implement"})
    st = fold(s.read_all())
    assert st.search_epoch == 1 and not st.finished
    assert not st.confirmed_done and not st.approved and not st.awaiting_approval
    assert st.approval_subject is None and st.best_node_id is None
    assert st.nodes[0].attempt == 1 and st.nodes[0].status.value == "pending"
    assert 0 not in st.aborted_nodes

    s.append("resume", {})
    assert fold(s.read_all()).search_epoch == 1                 # reset+resume is one edge, not two


def test_best_confirmed_rejects_a_mixed_generation_candidate_snapshot(tmp_path):
    """Checking only the chosen node is insufficient: a reset competitor's stale seeds may have changed
    the robust winner. best_confirmed is accepted only when its whole top-k generation map is current."""
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    _n(s, 0, 0.5)
    _n(s, 1, 0.6)
    s.append("node_reset", {"node_id": 1, "from_stage": "eval"})
    s.append("node_evaluated", {"node_id": 1, "generation": 1, "metric": 0.4})
    s.append("best_confirmed", {"node_id": 0, "significant": True,
                                 "generations": {"0": 0, "1": 0}})
    assert fold(s.read_all()).confirmed_done is False
    s.append("best_confirmed", {"node_id": 1, "significant": True,
                                 "generations": {"0": 0, "1": 1}})
    st = fold(s.read_all())
    assert st.confirmed_done and st.best_node_id == 1


def test_best_confirmed_stale_epoch_is_rejected(tmp_path):
    """R1 epoch identity: a best_confirmed STAMPED with a search_epoch other than the current one is
    rejected — an epoch-(N-1) confirmation can't authorize confirmed_done or the confirm-override in a
    fresh epoch (the non-requeuing-reopen gap _generation_map_matches doesn't catch)."""
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    _n(s, 0, 5.0)
    _n(s, 1, 1.0)
    # run is at epoch 0; a certificate stamped epoch 7 is stale -> dropped
    s.append("best_confirmed", {"node_id": 0, "significant": True,
                                "generations": {"0": 0, "1": 0}, "search_epoch": 7})
    st = fold(s.read_all())
    assert not st.confirmed_done
    assert st.best_node_id == 1        # the mean pick stands; the stale confirm-override of #0 was dropped


def test_best_confirmed_current_epoch_is_accepted(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    _n(s, 0, 5.0)
    _n(s, 1, 1.0)
    s.append("best_confirmed", {"node_id": 0, "significant": True,
                                "generations": {"0": 0, "1": 0}, "search_epoch": 0})
    st = fold(s.read_all())
    assert st.confirmed_done and st.best_node_id == 0     # current-epoch stamp -> override applies


def test_best_confirmed_without_epoch_stamp_folds_byte_identically(tmp_path):
    # additive/reader-defaulted: a legacy best_confirmed with NO search_epoch is treated as legacy-current
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    _n(s, 0, 5.0)
    _n(s, 1, 1.0)
    s.append("best_confirmed", {"node_id": 0, "significant": True, "generations": {"0": 0, "1": 0}})
    st = fold(s.read_all())
    assert st.confirmed_done and st.best_node_id == 0


def test_new_candidate_after_best_confirmed_invalidates_completion(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    _n(s, 0, 0.5)
    s.append("best_confirmed", {"node_id": 0, "significant": True,
                                 "generations": {"0": 0}})
    _n(s, 1, 0.1)                                      # materialized after the confirm event
    st = fold(s.read_all())
    assert not st.confirmed_done and st.best_node_id == 1


def test_unstamped_terminal_still_accepted_backward_compat(tmp_path):
    """Old logs don't carry `attempt`; a terminal with no attempt field defaults to the node's current
    generation, so legacy runs (no resets) fold exactly as before."""
    from looplab.core.models import NodeStatus
    s = EventStore(tmp_path / "e.jsonl")
    _seed_events(s)                                          # node_evaluated events carry no `attempt`
    assert fold(s.read_all()).nodes[0].status is NodeStatus.evaluated


def test_unstamped_terminal_cannot_impersonate_a_post_reset_generation(tmp_path):
    from looplab.core.models import NodeStatus
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    s.append("node_reset", {"node_id": 0, "from_stage": "eval"})
    s.append("node_evaluated", {"node_id": 0, "metric": 99.0, "eval_seconds": 3.0})
    st = fold(s.read_all())
    assert st.nodes[0].status is NodeStatus.pending and st.nodes[0].metric is None
    # The metric is ambiguous and therefore rejected, but an unstamped terminal is a legacy
    # generation-0 record: its real abandoned-work cost is charged once (never to generation 1).
    assert st.total_eval_seconds == 3.0

    s.append("node_evaluated", {"node_id": 0, "metric": 99.0, "eval_seconds": 3.0})
    assert fold(s.read_all()).total_eval_seconds == 3.0   # duplicate legacy terminal is not re-charged


def test_future_generation_cost_cannot_poison_budget(tmp_path):
    from looplab.core.models import NodeStatus

    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    s.append("node_evaluated", {"node_id": 0, "generation": 99, "metric": 99.0,
                                 "eval_seconds": 10_000.0})
    s.append("confirm_eval", {"node_id": 0, "generation": 99, "seed": 1,
                               "metric": 99.0, "eval_seconds": 10_000.0})
    s.append("confirm_eval", {"node_id": 999, "generation": 0, "seed": 1,
                               "metric": 99.0, "eval_seconds": 10_000.0})
    st = fold(s.read_all())
    assert st.total_eval_seconds == 0.0
    assert st.nodes[0].status is NodeStatus.pending
    assert st.confirm_seed_results == {}


def test_multiple_unstamped_legacy_resets_still_replay(tmp_path):
    """Old persisted runs used unstamped reset/rebuild controls but stamped terminal attempts."""
    from looplab.core.models import NodeStatus

    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    created = {"node_id": 0, "parent_ids": [], "operator": "draft",
               "idea": {"operator": "draft", "params": {}, "rationale": ""}}
    s.append("node_created", {**created, "code": "g0"})
    s.append("node_evaluated", {"node_id": 0, "attempt": 0, "metric": 3.0})
    s.append("node_reset", {"node_id": 0, "from_stage": "implement"})
    s.append("node_created", {**created, "code": "g1"})
    s.append("node_evaluated", {"node_id": 0, "attempt": 1, "metric": 2.0})
    s.append("node_reset", {"node_id": 0, "from_stage": "implement"})
    s.append("node_created", {**created, "code": "g2"})
    s.append("node_evaluated", {"node_id": 0, "attempt": 2, "metric": 1.0})
    node = fold(s.read_all()).nodes[0]
    assert node.attempt == 2 and node.code == "g2"
    assert node.status is NodeStatus.evaluated and node.metric == 1.0


def test_parent_generation_snapshot_rejects_reset_or_aborted_lineage(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    created = {"parent_ids": [], "operator": "draft",
               "idea": {"operator": "draft", "params": {}, "rationale": ""}}
    s.append("node_created", {**created, "node_id": 0, "code": "parent"})
    s.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 2.0})
    s.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})
    child = {"parent_ids": [0], "operator": "improve",
             "idea": {"operator": "improve", "params": {}, "rationale": ""}, "code": "child"}
    s.append("node_created", {**child, "node_id": 1, "parent_generations": {"0": 0}})
    assert 1 not in fold(s.read_all()).nodes                    # reset won before stale child append

    s.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 1.0})
    s.append("node_created", {**child, "node_id": 1, "parent_generations": {"0": 1}})
    assert 1 in fold(s.read_all()).nodes
    s.append("node_abort", {"node_id": 0, "generation": 1})
    s.append("node_created", {**child, "node_id": 2, "parent_generations": {"0": 1}})
    s.append("node_created", {**child, "node_id": 3})          # legacy/mapless must still honor abort
    nodes = fold(s.read_all()).nodes
    assert 2 not in nodes and 3 not in nodes                    # same-gen abort invalidates parent


def test_abort_makes_late_same_generation_effects_inert_but_charges_cost(tmp_path):
    from looplab.core.models import NodeStatus

    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    s.append("node_abort", {"node_id": 0, "generation": 0})
    s.append("stage_finished", {"node_id": 0, "generation": 0, "name": "eval", "status": "ok"})
    s.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 99.0,
                                 "eval_seconds": 3.0})
    s.append("confirm_eval", {"node_id": 0, "generation": 0, "seed": 1,
                               "metric": 99.0, "eval_seconds": 2.0})
    s.append("node_confirmed", {"node_id": 0, "generation": 0, "mean": 99.0})
    s.append("reward_hack_suspected", {"node_id": 0, "generation": 0,
                                         "signals": [{"signal": "grader_access"}]})
    s.append("best_confirmed", {"node_id": 0, "generations": {"0": 0}})
    s.append("node_failed", {"node_id": 0, "generation": 0, "reason": "aborted",
                              "error": "aborted by operator", "eval_seconds": 3.0})
    st = fold(s.read_all())
    assert st.nodes[0].status is NodeStatus.failed and st.nodes[0].metric is None
    assert st.nodes[0].stages == [] and st.nodes[0].confirmed_mean is None
    assert st.confirm_seed_results == {} and st.reward_hacks == []
    assert st.best_node_id is None and not st.confirmed_done
    assert st.total_eval_seconds == 5.0            # terminal + confirm compute, each charged once


def test_repeated_ablation_operations_each_charge_but_duplicate_id_dedupes(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    s.append("ablate", {"parent_id": 0, "generation": 0, "ablation_id": "a",
                         "impacts": {}, "eval_seconds": 4.0})
    s.append("ablate", {"parent_id": 0, "generation": 0, "ablation_id": "b",
                         "impacts": {}, "eval_seconds": 5.0})
    s.append("ablate", {"parent_id": 0, "generation": 0, "ablation_id": "b",
                         "impacts": {}, "eval_seconds": 5.0})
    assert fold(s.read_all()).total_eval_seconds == 9.0


def test_scoped_developer_crash_pause_does_not_survive_reset(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    s.append("node_failed", {"node_id": 0, "generation": 0,
                              "reason": "developer_crash", "error": "offline"})
    s.append("pause", {"node_id": 0, "generation": 0, "reason": "auto"})
    assert fold(s.read_all()).paused

    s.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "implement"})
    state = fold(s.read_all())
    assert not state.paused and state.pause_node_id is None and state.nodes[0].attempt == 1
    s.append("pause", {"node_id": 0, "generation": 0, "reason": "late auto pause"})
    assert not fold(s.read_all()).paused

    s.append("pause", {})                         # explicit operator pause is not lifecycle-scoped
    s.append("node_reset", {"node_id": 0, "generation": 1, "from_stage": "eval"})
    assert fold(s.read_all()).paused


def test_auto_crash_pause_cannot_take_ownership_from_explicit_pause(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    s.append("pause", {})                       # human stop while the developer is still failing
    s.append("node_failed", {"node_id": 0, "generation": 0,
                              "reason": "developer_crash", "error": "offline"})
    s.append("pause", {"node_id": 0, "generation": 0, "reason": "auto"})
    paused = fold(s.read_all())
    assert paused.paused and paused.pause_node_id is None

    s.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "implement"})
    reset = fold(s.read_all())
    assert reset.paused and reset.pause_node_id is None


def test_stale_terminal_cannot_clear_new_generation_building_marker(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    s.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "implement"})
    s.append("node_reset", {"node_id": 0, "generation": 1, "from_stage": "implement"})
    s.append("node_building", {"node_id": 0, "generation": 2, "operator": "draft"})
    s.append("node_failed", {"node_id": 0, "generation": 1,
                              "reason": "superseded", "error": "late"})
    assert fold(s.read_all()).building["generation"] == 2


def test_run_finished_sequence_cas_rejects_intervening_control(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    started = s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("hint", {"text": "arrived after the finish decision"})
    s.append("run_finished", {"reason": "done", "after_seq": started.seq})
    assert not fold(s.read_all()).finished

    latest = s.read_all()[-1]
    s.append("run_finished", {"reason": "done", "after_seq": latest.seq})
    assert fold(s.read_all()).finished


def test_finalization_handshake_is_opt_in_and_marker_is_bound_to_current_finish(tmp_path):
    legacy = EventStore(tmp_path / "legacy.jsonl")
    legacy.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    old_finish = legacy.append("run_finished", {"reason": "legacy"})
    old_state = fold(legacy.read_all())
    assert old_state.finished and not old_state.finalization_pending()
    assert old_state.last_finish_seq == old_state.finalized_finish_seq == old_finish.seq

    modern = EventStore(tmp_path / "modern.jsonl")
    modern.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    first = modern.append("run_finished", {
        "reason": "done", "finalization_required": True})
    assert fold(modern.read_all()).finalization_pending()
    second = modern.append("run_finished", {
        "reason": "newer", "finalization_required": True})
    modern.append("finalization_finished", {"finish_seq": first.seq})
    stale = fold(modern.read_all())
    assert stale.last_finish_seq == second.seq and stale.finalization_pending()
    modern.append("finalization_finished", {"finish_seq": second.seq})
    assert not fold(modern.read_all()).finalization_pending()


def test_finish_report_is_provisional_until_same_cas_or_legacy_adjacent_finish(tmp_path):
    modern = EventStore(tmp_path / "modern.jsonl")
    started = modern.append("run_started", {
        "run_id": "r", "task_id": "t", "direction": "min"})
    report = modern.append("report_generated", {
        "at_node": 1, "trigger": "finish",
        "content": {"summary": "stale", "at_node": 999, "trigger": "forged"}})
    modern.append("hint", {"text": "won the race"})
    modern.append("run_finished", {"after_seq": report.seq})
    rejected = fold(modern.read_all())
    assert not rejected.finished and rejected.report is None
    fresh_report = modern.append("report_generated", {
        "at_node": 2, "trigger": "finish",
        "content": {"summary": "fresh", "at_node": 999, "trigger": "forged",
                    "published_seq": 999, "published_at": 999}})
    modern.append("run_finished", {"after_seq": fresh_report.seq})
    accepted = fold(modern.read_all())
    assert accepted.finished and accepted.report["summary"] == "fresh"
    assert accepted.report["at_node"] == 2 and accepted.report["trigger"] == "finish"
    assert accepted.report["published_seq"] == fresh_report.seq
    assert accepted.report["published_at"] == fresh_report.ts
    assert started.seq < report.seq

    adjacent = EventStore(tmp_path / "adjacent.jsonl")
    legacy_report = adjacent.append("report_generated", {
        "trigger": "finish", "content": {"summary": "legacy"}})
    adjacent.append("run_finished", {})
    legacy = fold(adjacent.read_all()).report
    assert legacy["summary"] == "legacy" and legacy["trigger"] == "finish"
    assert legacy["published_seq"] == legacy_report.seq
    assert legacy["published_at"] == legacy_report.ts

    spaced = EventStore(tmp_path / "spaced.jsonl")
    spaced_report = spaced.append("report_generated", {
        "trigger": "\tfinish ", "content": {"summary": "sanitized outer trigger"}})
    assert fold(spaced.read_all()).report is None
    spaced.append("run_finished", {"after_seq": spaced_report.seq})
    assert fold(spaced.read_all()).report["trigger"] == "finish"

    separated = EventStore(tmp_path / "separated.jsonl")
    separated.append("report_generated", {
        "trigger": "finish", "content": {"summary": "must stay hidden"}})
    separated.append("future_unknown_event", {})
    separated.append("run_finished", {})
    assert fold(separated.read_all()).report is None


def test_finalize_resume_intent_mode_survives_launch_claim_and_served_consumes_tail_stop(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    finish = s.append("run_finished", {})
    stop = s.append("run_abort", {"reason": "finalized"})
    requested = s.append("resume_requested", {"mode": "finalize"})
    s.append("resume_requested", {"launch_claim": True})
    pending = fold(s.read_all())
    assert pending.finished and pending.stop_requested == "finalized"
    assert pending.last_stop_request_seq == stop.seq > finish.seq
    assert pending.last_resume_request_seq > requested.seq
    assert pending.last_resume_request_mode == "finalize"

    s.append("resume_served", {})
    served = fold(s.read_all())
    assert served.finished and served.stop_requested is None
    s.append("resume_requested", {})
    assert fold(s.read_all()).last_resume_request_mode == "resume"


def test_foreign_eval_cost_does_not_poison_the_fold(tmp_path):
    """arch-review §5 P2: a hand-edited/foreign eval_seconds (string / negative / non-finite) must not
    TypeError the whole fold nor reduce the cumulative budget."""
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    s.append("node_evaluated", {"node_id": 0, "metric": 1.0, "eval_seconds": "3"})   # numeric str -> 3.0
    s.append("node_created", {"node_id": 1, "parent_ids": [], "operator": "improve",
                              "idea": {"operator": "improve", "params": {}, "rationale": ""}})
    s.append("node_evaluated", {"node_id": 1, "metric": 2.0, "eval_seconds": -50.0})  # negative -> 0.0
    s.append("node_created", {"node_id": 2, "parent_ids": [], "operator": "improve",
                              "idea": {"operator": "improve", "params": {}, "rationale": ""}})
    s.append("node_evaluated", {"node_id": 2, "metric": 3.0, "eval_seconds": "junk"})  # unparseable -> 0.0
    st = fold(s.read_all())                                                            # must not raise
    # "3" recovers to 3.0; the negative and the junk both contribute 0.0 (never REDUCE the budget)
    assert st.total_eval_seconds == 3.0 and st.nodes[0].status.value == "evaluated"


def test_ablation_eval_cost_is_budgeted(tmp_path):
    """arch-review §4 P1-2: ablation probes run real evals; their wall-clock must count against the
    cumulative budget (total_eval_seconds), not spend entirely outside accounting."""
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("ablate", {"parent_id": 0, "impacts": {"x": 0.1}, "eval_seconds": 4.5})
    assert fold(s.read_all()).total_eval_seconds == 4.5
    # an old ablate event with no eval_seconds adds nothing (backward compatible)
    s.append("ablate", {"parent_id": 1, "impacts": {}})
    assert fold(s.read_all()).total_eval_seconds == 4.5


def test_normalize_task_rejects_cmd_and_eval_both():
    import pytest
    from looplab.adapters.tasks import normalize_task
    with pytest.raises(ValueError, match="EITHER"):
        normalize_task({"cmd": {"command": ["python", "x.py"]}, "eval": {"command": ["python", "y.py"]}})


# --------------------------------------------------------------- P0-2 search epoch / approval
def _confirmed_finished_log(s: EventStore) -> None:
    """A run that evaluated + confirmed node 0 and finished (direction=min)."""
    s.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.80})
    s.append("node_confirmed", {"node_id": 0, "mean": 0.75, "std": 0.0, "seeds": 3})
    s.append("best_confirmed", {"node_id": 0, "significant": True})
    s.append("run_finished", {"reason": "done"})


def test_reopen_after_finish_starts_new_epoch_and_reopens_confirmation(tmp_path):
    """arch-review §3 P0-2: reopening a FINISHED, confirmed run advances the search epoch and
    re-opens the confirmation/approval completion gates, so a better candidate added in the new
    epoch is confirmed and wins instead of being locked out by the prior confirmed champion."""
    s = EventStore(tmp_path / "e.jsonl")
    _confirmed_finished_log(s)
    s0 = fold(s.read_all())
    assert s0.finished and s0.confirmed_done and s0.best_node_id == 0 and s0.search_epoch == 0

    # Reopen (resume a finished run): epoch advances, confirmed_done re-opens, finished cleared.
    s.append("resume", {})
    s1 = fold(s.read_all())
    assert s1.search_epoch == 1 and not s1.confirmed_done and not s1.finished

    # A strictly-better new candidate is evaluated + confirmed in the new epoch -> it wins.
    s.append("node_created", {"node_id": 1, "parent_ids": [], "operator": "improve",
                              "idea": {"operator": "improve", "params": {}, "rationale": ""}})
    s.append("node_evaluated", {"node_id": 1, "metric": 0.10})
    s.append("node_confirmed", {"node_id": 1, "mean": 0.10, "std": 0.0, "seeds": 3})
    s.append("best_confirmed", {"node_id": 1, "significant": True})
    s2 = fold(s.read_all())
    assert s2.best_node_id == 1 and s2.confirmed_done and s2.search_epoch == 1


def test_run_reopened_alias_also_advances_epoch(tmp_path):
    """The legacy `run_reopened` alias of resume advances the epoch identically."""
    s = EventStore(tmp_path / "e.jsonl")
    _confirmed_finished_log(s)
    s.append("run_reopened", {})
    st = fold(s.read_all())
    assert st.search_epoch == 1 and not st.confirmed_done


def test_resume_after_pause_keeps_same_epoch_and_gates(tmp_path):
    """A resume from a mere PAUSE (finished never set) is the SAME epoch — the confirmation gate
    must NOT re-open (nothing about the candidate set changed)."""
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.5})
    s.append("best_confirmed", {"node_id": 0, "significant": True})   # confirmed_done True
    s.append("pause", {})
    s.append("resume", {})
    st = fold(s.read_all())
    assert st.search_epoch == 0 and st.confirmed_done and not st.paused


def _n(s: EventStore, nid: int, metric: float) -> None:
    s.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    s.append("node_evaluated", {"node_id": nid, "metric": metric})


def test_subject_bound_approval_rejects_a_forged_nonexistent_node(tmp_path):
    """arch-review §3 P0-2: an `approval_granted` for a node that doesn't exist in the run (a forged
    `node_id=999`) is a no-op — it can't globally flip `approved`; the run stays awaiting approval. A
    grant for a real candidate IS honored."""
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    _n(s, 3, 0.5)                                                     # the best (min)
    s.append("approval_requested", {"node_id": 3, "metric": 0.5})
    s.append("approval_granted", {"node_id": 999})                   # not a real node -> ignored
    st = fold(s.read_all())
    assert st.approved is False and st.awaiting_approval is True and st.approval_subject == 3
    s.append("approval_granted", {"node_id": 3})                     # the real best -> honored
    st2 = fold(s.read_all())
    assert st2.approved is True and st2.awaiting_approval is False and st2.approval_subject is None


def test_operator_may_approve_a_real_non_best_node(tmp_path):
    """arch-review §3 P0-2 (regression guard): `approve --node-id N` / the boss approve action let an
    operator ratify a SPECIFIC real node that isn't the current best — that grant must still be honored
    (binding to node existence, not to the exact best, so the human isn't silently ignored and hung)."""
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    _n(s, 3, 0.5)                                                    # best (min)
    _n(s, 7, 0.9)                                                    # a real, non-best node
    s.append("approval_requested", {"node_id": 3, "metric": 0.5})    # engine requests for the best
    s.append("approval_granted", {"node_id": 7})                     # operator chooses node 7
    st = fold(s.read_all())
    assert st.approved is True and st.awaiting_approval is False
    assert st.approved_node_id == 7 and st.best_node_id == 7     # publish what the human chose


def test_approval_grant_is_bound_to_node_generation(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    _n(s, 0, 0.5)
    s.append("node_reset", {"node_id": 0, "from_stage": "eval"})
    s.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 0.4})
    s.append("approval_requested", {"node_id": 0, "generation": 1})
    s.append("approval_granted", {"node_id": 0, "generation": 0})  # delayed gen-0 grant
    assert fold(s.read_all()).approved is False
    s.append("approval_granted", {"node_id": 0, "generation": 1})
    st = fold(s.read_all())
    assert st.approved and st.approved_node_id == 0


def test_forced_confirm_completion_is_generation_scoped(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    _n(s, 0, 0.5)
    s.append("force_confirm", {"node_id": 0})
    s.append("confirm_done", {"node_id": 0, "generation": 0})
    s.append("node_reset", {"node_id": 0, "from_stage": "eval"})
    s.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 0.4})
    s.append("force_confirm", {"node_id": 0})
    st = fold(s.read_all())
    assert {"node_id": 0, "generation": 1} in st.confirm_request_generations
    assert {"node_id": 0, "generation": 1} not in st.confirmed_forced_generations


def test_approval_backward_compat_direct_grant(tmp_path):
    """Back-compat: a bare grant (no node_id) is accepted, and a direct grant for a REAL node with no
    prior approval_requested is accepted — so legacy HITL runs fold identically."""
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("approval_granted", {})                                 # bare grant (no subject) -> accepted
    assert fold(s.read_all()).approved is True
    # a direct grant for a real node, no prior request
    s2 = EventStore(tmp_path / "e2.jsonl")
    s2.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    _n(s2, 0, 0.5)
    s2.append("approval_granted", {"node_id": 0})
    assert fold(s2.read_all()).approved is True


def test_run_setup_finished_folds_exactly_once_by_command(tmp_path):
    """arch-review §5 P2: a SUCCESSFUL run-level run_setup is folded (keyed by its command) so a
    resume can skip re-installing deps; a failed/timed-out one is NOT recorded (must re-run)."""
    from looplab.core.models import run_setup_key
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    cmd = ["pip", "install", "-r", "requirements.txt"]
    s.append("run_setup_finished", {"command": cmd, "exit_code": 1, "timed_out": False})   # failed
    assert run_setup_key(cmd) not in fold(s.read_all()).run_setup_done
    s.append("run_setup_finished", {"command": cmd, "exit_code": 0, "timed_out": False})   # ok
    st = fold(s.read_all())
    assert run_setup_key(cmd) in st.run_setup_done
    # a DIFFERENT command is keyed separately (not skipped by this record)
    assert run_setup_key(["pip", "install", "numpy"]) not in st.run_setup_done
    # a timed-out completion is not recorded either
    s.append("run_setup_finished", {"command": ["make"], "exit_code": 0, "timed_out": True})
    assert run_setup_key(["make"]) not in fold(s.read_all()).run_setup_done


def _fake_eval_engine(store, cmd, trust_mode="trusted_local"):
    """A minimal object exposing exactly what `EvalDispatchMixin._ensure_run_setup` reads, so the ENGINE
    skip path (fold-based, cross-process) can be unit-tested without building a whole Engine."""
    import threading
    from looplab.engine.eval_dispatch import EvalDispatchMixin

    class _FakeEngine(EvalDispatchMixin):
        def __init__(self):
            self.store = store
            self._eval_spec = {"run_setup": cmd}
            self.trust_mode = trust_mode
            self._run_setup_done = False
            self._run_setup_lock = threading.Lock()
            self.ran: list = []

        def _do_run_setup(self, c):
            self.ran.append(c)      # record instead of actually installing

    return _FakeEngine()


def test_ensure_run_setup_skips_a_completed_command_on_resume(tmp_path):
    """arch-review §5 P2 (engine skip path): a fresh Engine whose log already carries a SUCCESSFUL
    run_setup_finished for this command skips re-running it (crash-safe exactly-once across resume),
    rather than re-installing deps every resume."""
    cmd = ["pip", "install", "-r", "requirements.txt"]
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    store.append("run_setup_finished", {"command": cmd, "exit_code": 0, "timed_out": False})
    eng = _fake_eval_engine(store, cmd)
    eng._ensure_run_setup()
    assert eng.ran == [] and eng._run_setup_done is True            # skipped, not re-run


def test_ensure_run_setup_runs_when_not_yet_completed(tmp_path):
    """Conversely, with no prior successful record (fresh run, or only a FAILED prior attempt), the
    engine actually runs run_setup."""
    cmd = ["pip", "install", "-r", "requirements.txt"]
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    store.append("run_setup_finished", {"command": cmd, "exit_code": 1, "timed_out": False})  # failed
    eng = _fake_eval_engine(store, cmd)
    eng._ensure_run_setup()
    assert eng.ran == [cmd]                                         # not skipped — the prior attempt failed
    # an untrusted tier never runs the host-side run_setup (fresh container uses per-node setup)
    store2 = EventStore(tmp_path / "e2.jsonl")
    store2.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    eng2 = _fake_eval_engine(store2, cmd, trust_mode="untrusted")
    eng2._ensure_run_setup()
    assert eng2.ran == [] and eng2._run_setup_done is True          # gated off by trust_mode, not run


def test_run_setup_done_serializes_sorted_for_determinism(tmp_path):
    """final ultra-review §A: a str-set dumps in hash-randomized order across processes; the projection
    (looplab replay / /state) must be deterministic, so run_setup_done serializes as a SORTED list."""
    from looplab.core.models import run_setup_key
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    for cmd in (["pip", "install", "b"], ["pip", "install", "a"], ["make"]):
        s.append("run_setup_finished", {"command": cmd, "exit_code": 0, "timed_out": False})
    dumped = fold(s.read_all()).model_dump(mode="json")["run_setup_done"]
    assert dumped == sorted(run_setup_key(c) for c in
                            (["pip", "install", "b"], ["pip", "install", "a"], ["make"]))
    assert dumped == sorted(dumped)                                  # deterministic order


def test_approval_granted_coerces_string_node_id(tmp_path):
    """final ultra-review §F: a grant carrying a JSON STRING node id ("3") must be coerced and honored
    (node ids are int keys) — else it folds as `"3" not in {3: Node}` and the run hangs awaiting
    approval. A non-numeric id still fails the existence test."""
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    _n(s, 3, 0.5)
    s.append("approval_requested", {"node_id": 3})
    s.append("approval_granted", {"node_id": "3"})                   # string id -> coerced to 3
    assert fold(s.read_all()).approved is True
    # a non-numeric garbage id is still rejected
    s2 = EventStore(tmp_path / "e2.jsonl")
    s2.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    _n(s2, 3, 0.5)
    s2.append("approval_granted", {"node_id": "not-a-node"})
    assert fold(s2.read_all()).approved is False


def test_approval_granted_tolerates_unhashable_bool_and_fractional_node_id(tmp_path):
    """final-verification §F (blocker regression): a forged approval_granted with an UNHASHABLE node_id
    (list/dict) — a sanctioned /control event appended verbatim — must NOT crash fold (hashing an
    unhashable in `subj not in st.nodes` raises TypeError and bricks every replay). A bool id must not
    spuriously match node 1 (bool subclasses int), and a fractional id must not truncate to a real
    node. All are ignored; the run stays awaiting approval."""
    for i, bad in enumerate([[999], {}, {"x": 1}, True, False, 0.9]):
        s = EventStore(tmp_path / f"e_{i}.jsonl")
        s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
        _n(s, 0, 0.4)                       # node 0 exists; a bool id must NOT approve it via True->1/0
        _n(s, 1, 0.5)
        s.append("approval_requested", {"node_id": 0})
        s.append("approval_granted", {"node_id": bad})
        st = fold(s.read_all())             # must not raise
        assert st.approved is False and st.awaiting_approval is True


def test_annotation_tolerates_unhashable_and_bool_node_id(tmp_path):
    """final-verification §4: `annotation` is a sanctioned /control event appended verbatim and
    `_on_annotation` keys `st.annotations` by node id — a forged unhashable/bool id must not crash the
    fold (setdefault would hash it), the same blocker class as approval_granted."""
    for i, bad in enumerate([[999], {}, True, "x", None]):   # enumerate -> deterministic, no filename collision
        s = EventStore(tmp_path / f"a_{i}.jsonl")
        s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
        s.append("annotation", {"node_id": bad, "text": "note"})
        st = fold(s.read_all())             # must not raise
        assert st.annotations == {}         # forged/garbage id dropped, no note recorded
    # a real int id still records the note
    s2 = EventStore(tmp_path / "a_ok.jsonl")
    s2.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s2.append("annotation", {"node_id": 2, "text": "hello"})
    assert fold(s2.read_all()).annotations == {2: ["hello"]}


def test_spec_approved_requires_a_proposal(tmp_path):
    """arch-review §3 P0-2: a premature `spec_approved` with no `spec_proposed` must not confirm the
    spec (which would skip onboarding); a real proposal-then-approval works."""
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("spec_approved", {})                                    # forged: no proposal
    assert fold(s.read_all()).spec_confirmed is False
    # the real flow: proposal first, then approval
    s.append("spec_proposed", {"eval_spec": {"metric": {"kind": "adapter"}}})
    s.append("spec_approved", {})
    assert fold(s.read_all()).spec_confirmed is True


def test_spec_content_is_frozen_at_the_human_review_boundary(tmp_path):
    s = EventStore(tmp_path / "spec-freeze.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    proposal_a = {"eval_spec": {"metric": {"kind": "builtin", "name": "accuracy"}}}
    proposal_b = {"eval_spec": {"metric": {"kind": "adapter", "name": "unreviewed"}}}
    s.append("spec_proposed", proposal_a)
    s.append("spec_approval_requested", {})
    s.append("spec_proposed", proposal_b)  # late agent output cannot swap the reviewed content
    pending = fold(s.read_all())
    assert pending.proposed_spec == proposal_a and pending.spec_approval_requested
    s.append("spec_approved", {})
    s.append("spec_proposed", proposal_b)  # ratified spec remains frozen as well
    ratified = fold(s.read_all())
    assert ratified.spec_confirmed is True and ratified.proposed_spec == proposal_a


def test_legacy_structured_extra_metrics_fold_to_warning_free_numeric_projection(tmp_path):
    """Real legacy runs contain bookkeeping dicts/lists beside scalar objectives.  Replay must preserve the
    raw append-only event but expose the documented numeric map without Pydantic serializer warnings."""
    import warnings

    s = EventStore(tmp_path / "legacy-extra.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "max"})
    s.append("node_created", {"node_id": 0, "operator": "draft", "parent_ids": [],
                              "idea": {"operator": "draft", "params": {}}})
    raw = {"score2": 0.75, "target_weights": {"a": 1}, "labels": ["x"], "flag": True,
           "nan": float("nan")}
    s.append("node_evaluated", {"node_id": 0, "metric": 1.0, "extra_metrics": raw})
    state = fold(s.read_all())
    assert state.nodes[0].extra_metrics == {"score2": 0.75}
    assert s.read_all()[-1].data["extra_metrics"]["target_weights"] == {"a": 1}  # event remains auditable
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        dumped = state.model_dump(mode="json")
    assert dumped["nodes"]["0"]["extra_metrics"] == {"score2": 0.75}
    assert not any("PydanticSerializationUnexpectedValue" in str(w.message) for w in caught)


def test_extra_metric_projection_rejects_hostile_numeric_adapter():
    from looplab.core.models import normalize_extra_metrics

    class HostileInt(int):
        def __float__(self):
            raise TypeError("hostile numeric adapter")

    assert normalize_extra_metrics({"hostile": HostileInt(1), "ok": 2}) == {"ok": 2.0}
