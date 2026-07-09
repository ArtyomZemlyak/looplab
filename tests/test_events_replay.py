"""I1 keystone: event store durability + replay determinism (the #1 P0 risk)."""
from __future__ import annotations

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
