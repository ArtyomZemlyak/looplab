"""I1 keystone: event store durability + replay determinism (the #1 P0 risk)."""
from __future__ import annotations

import pytest

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
    # a corrupt LAST complete line with nothing valid after it is also just a tail, not a divergence
    p.write_bytes(b'{"seq":0,"type":"a"}\n{corrupt\n')
    assert log_divergence(p) is None
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
    post = Event(type="confirm_eval", data={"node_id": 0, "seed": 1, "eval_seconds": 3.0, "metric": 0.7})
    st3 = fold(base + [reset, post])
    assert st3.confirm_seed_results[0] == {1: 0.7}
    assert st3.total_eval_seconds == 1.0 + 2.0 + 2.0 + 3.0
    assert fold(base + [reset, post]).model_dump() == st3.model_dump()   # determinism


# confirm-seed eval cost is first-occurrence accounted (like node terminals): a duplicated/
# double-folded confirm_eval must not inflate total_eval_seconds or make the budget order-sensitive.
def test_fold_confirm_eval_cost_deduped_on_duplicate():
    from looplab.core.models import Event
    base = [Event(type="run_started", data={"run_id": "r", "task_id": "t"}),
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
    s.append("a", {}); s.append("b", {})
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
    can't land as the first-terminal-after-reset and accept a metric/cost from the discarded code."""
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
    assert st.nodes[0].metric is None and st.total_eval_seconds == 0.0
    # the NEW attempt's terminal (attempt=1) IS accepted
    s.append("node_evaluated", {"node_id": 0, "attempt": 1, "metric": 1.0, "eval_seconds": 2.0})
    st2 = fold(s.read_all())
    assert st2.nodes[0].status is NodeStatus.evaluated and st2.nodes[0].metric == 1.0
    assert st2.total_eval_seconds == 2.0                     # only the live attempt's cost counted


def test_unstamped_terminal_still_accepted_backward_compat(tmp_path):
    """Old logs don't carry `attempt`; a terminal with no attempt field defaults to the node's current
    generation, so legacy runs (no resets) fold exactly as before."""
    from looplab.core.models import NodeStatus
    s = EventStore(tmp_path / "e.jsonl")
    _seed_events(s)                                          # node_evaluated events carry no `attempt`
    assert fold(s.read_all()).nodes[0].status is NodeStatus.evaluated


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


def test_approval_granted_tolerates_unhashable_and_bool_node_id(tmp_path):
    """final-verification §F (blocker regression): a forged approval_granted with an UNHASHABLE node_id
    (list/dict) — a sanctioned /control event appended verbatim — must NOT crash fold (hashing an
    unhashable in `subj not in st.nodes` raises TypeError and bricks every replay). A bool id must not
    spuriously match node 1 (bool subclasses int). Both are ignored; the run stays awaiting approval."""
    for bad in ([999], {}, {"x": 1}, True, False):
        s = EventStore(tmp_path / f"e_{hash(str(bad)) & 0xffff}.jsonl")
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
    for bad in ([999], {}, True, "x", None):
        s = EventStore(tmp_path / f"a_{hash(str(bad)) & 0xffff}.jsonl")
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
