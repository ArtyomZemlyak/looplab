"""PART IV Phase 2a — concept-graph coverage/uncovered-region wired into the Strategist pivot.

Locks in that: the strategist cadence records a deterministic concept-coverage snapshot (replay-safe,
audit-only) when `concept_pivot` is on; the snapshot names the uncovered winning region; and the
Researcher's `explore`-stance novelty hint pivots to "0 coverage in {X} — go there" instead of the vague
"broaden". No-op for a task with no curated concept skeleton; never touches selection."""
from __future__ import annotations

from types import SimpleNamespace

from looplab.core.config import Settings
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def _dr_store(tmp_path, themes, task_id="dense-retrieval") -> EventStore:
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": task_id, "goal": "g", "direction": "max"})
    for i, th in enumerate(themes):
        op = "draft" if i < 3 else "improve"
        s.append("node_created", {"node_id": i, "parent_ids": [], "operator": op,
                                  "idea": {"operator": op, "params": {"seed": float(i)}, "theme": th,
                                           "rationale": f"try {th} with r-drop"}})
        s.append("node_evaluated", {"node_id": i, "metric": 0.8 + i * 0.001})
    return s


_DCL = ["dcl-rdrop-ema", "dcl-temperature", "dcl-gc", "dcl-swa", "dcl-listwise"]


# --------------------------------------------------------------------------- #
# The snapshot content (deterministic, pure)
# --------------------------------------------------------------------------- #

def test_snapshot_names_uncovered_winning_region(tmp_path):
    st = fold(_dr_store(tmp_path, _DCL).read_all())
    snap = Engine._concept_coverage_snapshot(None, st)   # self unused; pure over state
    assert snap is not None and snap["fired"] is True
    for cid in ("negatives/external-mining", "distillation/teacher-distill"):
        assert cid in snap["uncovered_key"]
    assert "0 coverage in {" in snap["directive"]
    assert snap["locked_axis"] == "loss"        # the run is locked onto the loss axis


def test_snapshot_is_none_for_task_without_skeleton(tmp_path):
    st = fold(_dr_store(tmp_path, _DCL, task_id="some-tabular-task").read_all())
    assert Engine._concept_coverage_snapshot(None, st) is None   # no curated skeleton -> no-op


# The snapshot feeds a persisted event, so it must be byte-identical across PYTHONHASHSEED values — a
# same-process f(x)==f(x) can't catch a set/dict iteration order leaking into a list/string. Run the pure
# snapshot in two subprocesses with different hash seeds and compare the serialized result.
_SNAP_SNIPPET = """
import json
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.engine.orchestrator import Engine
st = fold(EventStore({path!r}).read_all())
print(json.dumps(Engine._concept_coverage_snapshot(None, st), sort_keys=True))
"""


def test_snapshot_is_hashseed_independent(tmp_path):
    import os, subprocess, sys
    _dr_store(tmp_path, _DCL)                             # writes events.jsonl (read-only in the subprocs)
    p = str(tmp_path / "events.jsonl")
    def _emit(seed: str) -> str:
        env = {**os.environ, "PYTHONHASHSEED": seed}
        return subprocess.check_output(
            [sys.executable, "-c", _SNAP_SNIPPET.format(path=p)], env=env, text=True).strip()
    out = _emit("0")
    assert out == _emit("424242") and out != "null"


# --------------------------------------------------------------------------- #
# The LIVE emission path — _maybe_snapshot_concept_coverage (the four gates)
# --------------------------------------------------------------------------- #

def _snap_engine(store, *, concept_pivot=True, consult=True):
    """Minimal host for the live emission path: it reads _concept_pivot, _should_consult(state), store,
    and _concept_coverage_snapshot(state) (which is pure over state, so bound as an unbound call)."""
    return SimpleNamespace(
        _concept_pivot=concept_pivot,
        _should_consult=lambda st: consult,
        _concept_coverage_snapshot=lambda st: Engine._concept_coverage_snapshot(None, st),
        store=store)


def test_cadence_emits_one_concept_snapshot(tmp_path):
    store = _dr_store(tmp_path, _DCL)
    st2 = Engine._maybe_snapshot_concept_coverage(_snap_engine(store), fold(store.read_all()))
    snaps = [e for e in store.read_all() if e.type == "concept_coverage_snapshot"]
    assert len(snaps) == 1 and snaps[0].data["at_node"] == len(st2.nodes)
    assert st2.concept_coverage_snapshots and st2.concept_coverage_snapshots[0]["fired"] is True


def test_cadence_is_at_node_idempotent(tmp_path):
    store = _dr_store(tmp_path, _DCL)
    eng = _snap_engine(store)
    st2 = Engine._maybe_snapshot_concept_coverage(eng, fold(store.read_all()))
    st3 = Engine._maybe_snapshot_concept_coverage(eng, st2)      # same node-count -> no second emit
    assert len([e for e in store.read_all() if e.type == "concept_coverage_snapshot"]) == 1
    assert len(st3.concept_coverage_snapshots) == 1


def test_flag_off_emits_no_snapshot(tmp_path):
    store = _dr_store(tmp_path, _DCL)
    Engine._maybe_snapshot_concept_coverage(_snap_engine(store, concept_pivot=False),
                                            fold(store.read_all()))
    assert not any(e.type == "concept_coverage_snapshot" for e in store.read_all())


def test_off_cadence_emits_no_snapshot(tmp_path):
    store = _dr_store(tmp_path, _DCL)
    Engine._maybe_snapshot_concept_coverage(_snap_engine(store, consult=False), fold(store.read_all()))
    assert not any(e.type == "concept_coverage_snapshot" for e in store.read_all())


def test_no_skeleton_task_emits_no_snapshot(tmp_path):
    store = _dr_store(tmp_path, _DCL, task_id="some-tabular-task")
    Engine._maybe_snapshot_concept_coverage(_snap_engine(store), fold(store.read_all()))
    assert not any(e.type == "concept_coverage_snapshot" for e in store.read_all())


# --------------------------------------------------------------------------- #
# Replay-safety (audit-only, additive)
# --------------------------------------------------------------------------- #

def test_snapshot_event_folds_audit_only(tmp_path):
    s = _dr_store(tmp_path, _DCL)
    s.append("concept_coverage_snapshot", {"at_node": 5, "fired": True,
                                           "uncovered_key": ["negatives/external-mining"],
                                           "directive": "0 coverage in {X} — go there"})
    st = fold(s.read_all())
    assert len(st.concept_coverage_snapshots) == 1
    assert st.concept_coverage_snapshots[0]["at_node"] == 5
    assert st.best_node_id == 4       # audit-only: selection unchanged by the snapshot


def test_old_logs_fold_without_the_field(tmp_path):
    st = fold(_dr_store(tmp_path, _DCL[:1]).read_all())   # no concept snapshot event
    assert st.concept_coverage_snapshots == []


# --------------------------------------------------------------------------- #
# The explore-stance pivot hint
# --------------------------------------------------------------------------- #

def _fake_engine(concept_pivot: bool):
    return SimpleNamespace(_concept_pivot=concept_pivot, researcher=SimpleNamespace())


def test_explore_hint_pivots_to_uncovered_regions(tmp_path):
    st = fold(_dr_store(tmp_path, _DCL).read_all())
    st.concept_coverage_snapshots.append(
        {"at_node": 5, "fired": True, "uncovered_key": ["negatives/external-mining"],
         "directive": "0 coverage in {negatives/external-mining, distillation} — go there"})
    eng = _fake_engine(concept_pivot=True)
    Engine._stamp_novelty_hint(eng, st, "explore")
    hint = eng.researcher._novelty_hint
    assert "Concept-graph pivot" in hint and "0 coverage in {" in hint
    assert "broaden the space" not in hint       # the specific directive REPLACES the vague one


def test_explore_hint_falls_back_to_broaden_when_pivot_off(tmp_path):
    st = fold(_dr_store(tmp_path, _DCL).read_all())
    st.concept_coverage_snapshots.append(
        {"at_node": 5, "fired": True, "uncovered_key": ["x"], "directive": "0 coverage in {x}"})
    eng = _fake_engine(concept_pivot=False)       # flag off -> unchanged behavior
    Engine._stamp_novelty_hint(eng, st, "explore")
    assert "Concept-graph pivot" not in eng.researcher._novelty_hint
    assert "broaden the space" in eng.researcher._novelty_hint


def test_explore_hint_falls_back_when_no_region_uncovered(tmp_path):
    st = fold(_dr_store(tmp_path, _DCL).read_all())   # no concept snapshot recorded
    eng = _fake_engine(concept_pivot=True)
    Engine._stamp_novelty_hint(eng, st, "explore")
    assert "broaden the space" in eng.researcher._novelty_hint   # graceful without a snapshot


def test_exploit_stance_unaffected(tmp_path):
    st = fold(_dr_store(tmp_path, _DCL).read_all())
    eng = _fake_engine(concept_pivot=True)
    Engine._stamp_novelty_hint(eng, st, "exploit")
    assert "Concept-graph pivot" not in eng.researcher._novelty_hint
    assert "EXPLOIT" in eng.researcher._novelty_hint


def test_settings_flag_defaults_on():
    # Part IV/V ships ON by default (concept tagging is audit + prompt-cue only; opt out per-run).
    assert Settings().concept_pivot is True
