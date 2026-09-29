"""Orchestrator internals: workspace fingerprinting + resume-drift detection, the run failure
taxonomy (error_reason), and gap-safe node-id allocation.

Regressions consolidated from the code-review rounds (#4 workspace fingerprint + resume drift,
#6 failure taxonomy) and the hourly review loop (iter 6: gap-safe node-id allocation)."""
from __future__ import annotations

import sys
from pathlib import Path

import anyio
import pytest

from looplab.engine.orchestrator import Engine, _dir_fingerprint
from looplab.search.policy import GreedyTree
from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.events.replay import fold

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"
_M = {"kind": "stdout_json", "key": "metric"}


def _repo(tmp_path, body):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "run.py").write_text(body, encoding="utf-8")
    return repo


# large data/ref mounts use a cheap shallow fingerprint (no recursive walk), still
# catching top-level add/remove; missing path -> "absent".
def test_shallow_fingerprint(tmp_path):
    from looplab.engine.orchestrator import _shallow_fingerprint
    d = tmp_path / "data"; d.mkdir()
    (d / "a.bin").write_text("x", encoding="utf-8")
    fp1 = _shallow_fingerprint(str(d))
    assert fp1.startswith("dir:")
    (d / "b.bin").write_text("y", encoding="utf-8")          # top-level add -> changes
    assert _shallow_fingerprint(str(d)) != fp1
    assert _shallow_fingerprint(str(tmp_path / "nope")) == "absent"


# --------------------------------- #6b failure taxonomy ------------------------------
def test_failure_reason_no_metric(tmp_path):
    repo = _repo(tmp_path, "print('hello, no metric here')\n")
    t = RepoTask(id="f", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
    r, d = t.build_roles()
    state = anyio.run(Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                             sandbox=SubprocessSandbox(),
                             policy=GreedyTree(n_seeds=1, max_nodes=1)).run)
    assert state.nodes[0].error_reason == "no_metric"


def test_failure_reason_crash(tmp_path):
    repo = _repo(tmp_path, "raise SystemExit('boom')\n")
    t = RepoTask(id="f", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
    r, d = t.build_roles()
    state = anyio.run(Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                             sandbox=SubprocessSandbox(),
                             policy=GreedyTree(n_seeds=1, max_nodes=1)).run)
    assert state.nodes[0].error_reason == "crash"


# --------------------------------- #4 workspace fingerprint --------------------------
def test_dir_fingerprint_changes_with_content(tmp_path):
    d = tmp_path / "r"; d.mkdir()
    (d / "a.py").write_text("x=1\n", encoding="utf-8")
    fp1 = _dir_fingerprint(str(d))
    (d / "b.py").write_text("y=2\n", encoding="utf-8")     # add a file -> different signature
    assert _dir_fingerprint(str(d)) != fp1
    assert _dir_fingerprint(str(tmp_path / "missing")) == "absent"


def test_workspace_fingerprints_survive_a_wedged_git(tmp_path, monkeypatch):
    """Both fingerprinters shell out to `git rev-parse`, and both run at setup AND on every resume.
    On a wedged FUSE/network mount an unbounded call hangs the run with no diagnostic — so the call
    must be bounded, and a timeout must fall through to the stat/scandir signature, not propagate."""
    import subprocess

    from looplab.engine import triage

    d = tmp_path / "r"; d.mkdir()
    (d / "a.py").write_text("x=1\n", encoding="utf-8")
    seen: list[object] = []

    def wedged_run(argv, **kwargs):
        seen.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 0)

    monkeypatch.setattr(subprocess, "run", wedged_run)   # both fingerprinters import it locally

    assert _dir_fingerprint(str(d)).startswith("hash:")          # fell back, did not raise
    assert triage._shallow_fingerprint(str(d)).startswith("dir:")
    # A deadline was actually passed — without one the hang is what the test could never observe.
    assert seen and all(t == triage._GIT_TIMEOUT_S for t in seen), seen


def test_run_records_workspace_and_resume_detects_change(tmp_path):
    repo = _repo(tmp_path, 'import json; print(json.dumps({"metric": 1.0}))\n')
    t = RepoTask(id="w", direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
    r, d = t.build_roles()
    run_dir = tmp_path / "run"
    s1 = anyio.run(Engine(run_dir, task=t, researcher=r, developer=d,
                          sandbox=SubprocessSandbox(),
                          policy=GreedyTree(n_seeds=1, max_nodes=1)).run)
    assert s1.workspace and "editable:." in s1.workspace
    assert s1.workspace_changed is False
    # The operator's source changes after the run started; a resume must NOT pretend it's the
    # same workspace.
    (repo / "run.py").write_text(
        'import json; print(json.dumps({"metric": 2.0}))\n', encoding="utf-8")
    r2, d2 = t.build_roles()
    s2 = anyio.run(Engine(run_dir, task=t, researcher=r2, developer=d2,
                          sandbox=SubprocessSandbox(),
                          policy=GreedyTree(n_seeds=1, max_nodes=1)).run)
    assert s2.workspace_changed is True


def test_every_workspace_change_is_recorded_and_the_rows_chain(tmp_path):
    """Doc 69 69.20: gated on the folded flag, only a run's first change was written — a real run's
    editable repo changed three times, the decoder fix among them, and left one row. Every resume
    that finds a NEW fingerprint records it against the last one recorded; a resume that finds the
    one already recorded writes nothing."""
    from looplab.events.eventstore import EventStore

    repo = _repo(tmp_path, 'import json; print(json.dumps({"metric": 1.0}))\n')
    t = RepoTask(id="w", direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
    run_dir = tmp_path / "run"

    def enter():
        r, d = t.build_roles()
        return anyio.run(Engine(run_dir, task=t, researcher=r, developer=d,
                                sandbox=SubprocessSandbox(),
                                policy=GreedyTree(n_seeds=1, max_nodes=1)).run)

    def rows():
        return [e.data for e in EventStore(run_dir / "events.jsonl").read_all()
                if e.type == "workspace_changed"]

    start = enter().workspace
    for metric in (2.0, 3.0):
        (repo / "run.py").write_text(
            f'import json; print(json.dumps({{"metric": {metric}}}))\n', encoding="utf-8")
        enter()
    enter()                                             # nothing changed since the last row
    # …nor does a file no node is ever seeded with: an import rewrote the bytecode cache of a
    # non-git editable, and that read as a change on every re-entry (critic 2026-09-27, driven).
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "run.cpython-311.pyc").write_bytes(b"\x00" * 16)
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "x.js").write_text("1\n", encoding="utf-8")
    enter()
    first, second = rows()
    assert first["was"] == start and first["now"] != start
    assert second["was"] == first["now"] and second["now"] != first["now"]


def _workspace_entries(tmp_path):
    """A non-git editable and a function that ENTERS the run once (start or resume)."""
    repo = _repo(tmp_path, 'import json; print(json.dumps({"metric": 1.0}))\n')
    t = RepoTask(id="w", direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
    run_dir = tmp_path / "run"

    def enter():
        r, d = t.build_roles()
        return anyio.run(Engine(run_dir, task=t, researcher=r, developer=d,
                                sandbox=SubprocessSandbox(),
                                policy=GreedyTree(n_seeds=1, max_nodes=1)).run)

    def rows():
        from looplab.events.eventstore import EventStore
        return [e.data for e in EventStore(run_dir / "events.jsonl").read_all()
                if e.type == "workspace_changed"]

    return enter, rows


def test_a_lasting_change_of_kind_keeps_the_chain_alive(tmp_path, monkeypatch):
    """A `git init` + commit in a hash-read editable is read as git from then on. The first cut of the
    kind rule wrote nothing across kinds and compared every later git reading with the run's hash,
    so the chain went silent for good — two real commits after it left no row (critic 2026-09-27,
    driven). Now the first git reading is a move (nothing recorded vouches for it) and every later
    one is compared with the last git reading. MUTATION: compare only same-kind pairs -> no rows."""
    enter, rows = _workspace_entries(tmp_path)
    start = enter().workspace
    assert all(v.startswith("hash:") for v in start.values()), start
    for sha in ("1" * 40, "2" * 40, "2" * 40):
        monkeypatch.setattr(Engine, "_workspace_fingerprint",
                            lambda self, sha=sha: {key: "git:" + sha for key in start})
        enter()
    first, second = rows()
    assert first["was"] == start and set(first["now"].values()) == {"git:" + "1" * 40}
    assert second["was"] == first["now"] and set(second["now"].values()) == {"git:" + "2" * 40}


def test_a_one_off_read_the_other_way_is_one_row_and_the_way_back_is_none(tmp_path, monkeypatch):
    """A `git rev-parse` that timed out once falls to the stat hash: that entry has nothing to vouch
    for its reading and records it; the next entry reads git again, and is compared with the LAST
    GIT reading — the same, so nothing — where the raw rule wrote a second row for the flip back.
    A later real commit is still a move, against the git reading it replaced."""
    enter, rows = _workspace_entries(tmp_path)
    a, b, c = ("git:" + ch * 40 for ch in "abc")
    h = "hash:0123456789abcdef"
    # The git reading the flip comes back to was recorded by a ROW (a -> b), not by the run's
    # start: the look-back is the fold's, not the start's (MUTATION: seed it from the start only ->
    # the flip back to b reads as a move against a).
    readings = iter([a, b, h, b, c])
    current = {}

    def _fingerprint(self):
        return {"editable:.": current["v"]}

    monkeypatch.setattr(Engine, "_workspace_fingerprint", _fingerprint)
    for _ in range(5):
        current["v"] = next(readings)
        enter()
    assert [(r["was"]["editable:."], r["now"]["editable:."]) for r in rows()] == [
        (a, b), (b, h), (h, c)]


def test_a_task_edited_between_entries_is_recorded_and_the_rows_chain(tmp_path):
    """Doc 69 69.19: `run_started.config_hash` is what a resume compares its task against, and
    nothing compared it — an operator's edit to the task changed what later nodes are measured
    against and left no trace. Every re-entry that reads a task hashing differently from the last
    recorded writes a fold-ignored `task_changed` row; one that reads the same writes nothing.
    MUTATION: compare against the run's start only -> the second edit's `was` is wrong; skip the
    check -> no rows."""
    from looplab.events.eventstore import EventStore
    from looplab.events.types import DIAGNOSTIC_EVENTS, EV_TASK_CHANGED

    repo = _repo(tmp_path, 'import json; print(json.dumps({"metric": 1.0}))\n')
    run_dir = tmp_path / "run"

    def enter(goal):
        t = RepoTask(id="w", goal=goal, direction="max", editable_path=str(repo),
                     edit_surface=["*.txt"],
                     eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
        r, d = t.build_roles()
        return anyio.run(Engine(run_dir, task=t, researcher=r, developer=d,
                                sandbox=SubprocessSandbox(),
                                policy=GreedyTree(n_seeds=1, max_nodes=1)).run)

    def rows():
        return [e.data for e in EventStore(run_dir / "events.jsonl").read_all()
                if e.type == EV_TASK_CHANGED]

    enter("maximize the metric")
    started = next(e.data["task_identity"] for e in EventStore(run_dir / "events.jsonl").read_all()
                   if e.type == "run_started")
    assert started and rows() == [], "a fresh run records its own task, no change"
    enter("maximize the metric")
    assert rows() == [], "the same task: nothing"
    enter("maximize the metric on the filtered split")
    enter("maximize the metric on the filtered split")
    enter("maximize the metric on the held-out split")
    enter("maximize the metric on the held-out split")   # MUTATION: chain off the FIRST row -> a 3rd
    first, second = rows()
    assert first["was"] == started and first["now"] != started
    assert second["was"] == first["now"] and second["now"] not in (started, first["now"])
    assert EV_TASK_CHANGED in DIAGNOSTIC_EVENTS
    # A log whose `run_started` recorded no hash (written before the field) has nothing to compare:
    # no row, rather than one claiming the task changed from "" (MUTATION: drop that guard).
    from looplab.events.replay import fold

    legacy = EventStore(tmp_path / "legacy" / "events.jsonl")
    legacy.append("run_started", {"run_id": "r", "task_id": "w", "goal": "g", "direction": "max"})
    t = RepoTask(id="w", goal="g", direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
    r, d = t.build_roles()
    engine = Engine(tmp_path / "legacy", task=t, researcher=r, developer=d,
                    sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1))
    engine._record_task_change(legacy.read_all(), fold(legacy.read_all()))
    assert not [e for e in legacy.read_all() if e.type == EV_TASK_CHANGED]


def _task_entry(tmp_path, goal="g"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = _repo(tmp_path, 'import json; print(json.dumps({"metric": 1.0}))\n')
    t = RepoTask(id="w", goal=goal, direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
    r, d = t.build_roles()
    return t, Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                     sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1))


def test_a_build_that_only_adds_a_defaulted_field_records_no_task_change(tmp_path, monkeypatch):
    """Critic 2026-09-27 (driven): the same task document hashed differently under a build that
    added defaulted task fields — nine since 2026-08-17 — and every resume across an upgrade wrote
    a `task_changed` nobody made. The identity leaves defaults out (`core/setup_identity.py::
    task_identity`). MUTATION: hash the full dump -> the added field reads as an edit."""
    from looplab.core import setup_identity
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold

    t, engine = _task_entry(tmp_path)
    store = EventStore(tmp_path / "run" / "events.jsonl")
    store.append("run_started", {"run_id": "run", "task_id": "w", "goal": "g", "direction": "max",
                                 "task_identity": setup_identity.task_identity(t)})
    real_dump = type(t).model_dump

    def _upgraded_dump(self, **kw):        # a new build: one more field, at its default
        out = real_dump(self, **kw)
        if not kw.get("exclude_defaults"):
            out["a_field_added_later"] = 0
        return out

    monkeypatch.setattr(type(t), "model_dump", _upgraded_dump)
    engine._record_task_change(store.read_all(), fold(store.read_all()))
    assert not [e for e in store.read_all() if e.type == "task_changed"]


def test_a_duck_typed_task_has_no_identity_and_does_not_raise():
    """A task that is no pydantic model has no identity, decided by TYPE: the host-graded and memory
    tests' duck-typed tasks define `model_dump(mode=...)` without `exclude_defaults`, and asking
    for it raised a TypeError out of `run_started` (full suite 2026-09-29, 7 failures). MUTATION:
    decide by a callable `model_dump` -> TypeError."""
    from looplab.core import setup_identity

    class _Duck:
        def model_dump(self, mode="python"):
            return {"id": "duck"}

    assert setup_identity.task_identity(_Duck()) == ""
    assert setup_identity.task_identity(object()) == ""


def test_a_run_started_before_the_identity_reads_its_own_snapshot(tmp_path):
    """A log whose `run_started` records no `task_identity` is compared against its own
    `task.snapshot.json`, read by THIS build; an edit to the task is still a row."""
    import json

    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold

    repo = _repo(tmp_path, 'import json; print(json.dumps({"metric": 1.0}))\n')

    def engine_for(goal):
        t = RepoTask(id="w", goal=goal, direction="max", editable_path=str(repo),
                     edit_surface=["*.txt"],
                     eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
        r, d = t.build_roles()
        return t, Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                         sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1))

    t, same = engine_for("the goal it started on")
    (tmp_path / "run" / "task.snapshot.json").write_text(
        json.dumps(t.model_dump(mode="json")), encoding="utf-8")
    store = same.store
    store.append("run_started", {"run_id": "run", "task_id": "w", "goal": "g", "direction": "max",
                                 "config_hash": "0" * 12})
    same._record_task_change(store.read_all(), fold(store.read_all()))
    assert not [e for e in store.read_all() if e.type == "task_changed"], "same document: no row"
    _, edited = engine_for("an edited goal")
    edited._record_task_change(store.read_all(), fold(store.read_all()))
    assert len([e for e in EventStore(store.path).read_all() if e.type == "task_changed"]) == 1


@pytest.mark.parametrize("why", ["finished", "stop_requested", "refused"])
def test_no_task_row_where_a_reader_keys_on_position_or_the_entry_is_refused(tmp_path, monkeypatch,
                                                                             why):
    """A re-entry that only FINISHES the run writes no `task_changed`: finalize recovery keys on what
    lies between its durable report and `run_finished`, and a row there republished the report
    (critic 2026-09-27, driven). A finished run CONTINUED is still asked. And the check sits AFTER
    the receipt fence: an entry refused there writes nothing (MUTATION: call it first -> a row,
    then the refusal)."""
    from looplab.engine.reentry import RunStartPinError
    from looplab.events.eventstore import EventStore

    _, engine = _task_entry(tmp_path, goal="an edited goal")
    store = engine.store
    store.append("run_started", {"run_id": "run", "task_id": "w", "goal": "g", "direction": "max",
                                 "task_identity": "0" * 12})
    if why == "finished":
        store.append("run_finished", {"reason": "budget"})
    elif why == "stop_requested":
        store.append("run_abort", {"reason": "operator"})
    else:
        def _refuse(entry):
            raise RunStartPinError("refused")
        monkeypatch.setattr(engine, "_require_pinned_speculation_receipt", _refuse)
    try:
        engine._reentry_repin()
    except RunStartPinError:
        assert why == "refused"
    rows = [e for e in EventStore(store.path).read_all() if e.type == "task_changed"]
    assert len(rows) == (1 if why == "finished" else 0), why


def test_a_source_is_compared_with_the_last_reading_of_its_own_kind():
    """`setup_phase.py::workspace_moved` as a truth table. Two readings of different kinds say
    nothing about the source on their own, so a reading is compared with the last recorded one OF
    ITS KIND; a source read a way it never was before is a move (nothing vouches for it); a source
    added, removed or gone is a move whatever the kinds. MUTATIONS: compare the raw values -> the
    flip back reads as a move; compare only same-kind pairs -> a new way of reading is silent."""
    from looplab.engine.setup_phase import recorded_readings, workspace_moved

    git, hashed = {"editable:.": "git:abc"}, {"editable:.": "hash:0123"}
    both = recorded_readings(git, {"editable:.": {"hash": "hash:0123"}})
    assert both == {"editable:.": {"git": "git:abc", "hash": "hash:0123"}}
    assert not workspace_moved(hashed, git, both), "the flip back to the last git reading"
    assert not workspace_moved(git, hashed, both), "…and to the last hash reading"
    assert workspace_moved(hashed, {"editable:.": "git:def"}, both)
    assert workspace_moved(git, {"editable:.": "hash:4567"}, both)
    assert workspace_moved(git, hashed, recorded_readings(git, None)), "never read by hash before"
    assert workspace_moved(hashed, git, recorded_readings(hashed, None)), "never read by git before"
    assert workspace_moved(git, {"editable:.": "git:def"}, recorded_readings(git, None))
    assert workspace_moved(git, {"editable:.": "absent"}, both)
    assert workspace_moved({"editable:.": "absent"}, hashed, both)
    assert workspace_moved(git, {**git, "data:x": "dir:1:2"}) and workspace_moved({**git, "data:x": "dir:1:2"}, git)
    assert not workspace_moved(git, dict(git))
    # A later row's reading of a kind replaces the run's own.
    assert recorded_readings(git, {"editable:.": {"git": "git:new"}}) == {"editable:.": {"git": "git:new"}}
    assert recorded_readings(None, None) == {} and recorded_readings({"editable:.": "absent"}, None) == {}


def test_a_nested_cache_no_node_is_seeded_with_never_moves_the_stat_hash(tmp_path):
    """The stat hash skips every path whose ANY part is a name no node is seeded with
    (`workspace_seed.IGNORE_NAMES`), not only a top-level one. MUTATION: ask the first part only ->
    a nested `pkg/__pycache__` rewrite moves the hash again (critic 2026-09-27, 0a-02)."""
    from looplab.engine.triage import _dir_fingerprint

    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    before = _dir_fingerprint(repo)
    assert before.startswith("hash:"), before
    (repo / "pkg" / "__pycache__").mkdir()
    (repo / "pkg" / "__pycache__" / "mod.cpython-311.pyc").write_bytes(b"\x00" * 16)
    (repo / "pkg" / "sub" / "node_modules").mkdir(parents=True)
    (repo / "pkg" / "sub" / "node_modules" / "x.js").write_text("1\n", encoding="utf-8")
    assert _dir_fingerprint(repo) == before
    (repo / "pkg" / "mod.py").write_text("x = 2  # and longer\n", encoding="utf-8")
    assert _dir_fingerprint(repo) != before


def test_the_fold_keeps_each_source_s_last_reading_per_kind():
    from looplab.events.replay import fold
    from looplab.events.eventstore import Event

    def _row(seq, now):
        return Event(seq=seq, ts=float(seq), type="workspace_changed", data={"was": {}, "now": now})

    st = fold([_row(1, {"editable:.": "hash:1", "data:d": "dir:1:2"}),
               _row(2, {"editable:.": "git:a"}), _row(3, {"editable:.": "git:b"})])
    assert st.workspace_now == {"editable:.": "git:b"}
    assert st.workspace_seen == {"editable:.": {"hash": "hash:1", "git": "git:b"},
                                 "data:d": {"dir": "dir:1:2"}}
    assert "workspace_seen" not in st.model_dump(), "fold-internal, like workspace_now"


# --------------------------------------------------------------------------- gap-safe node-id alloc
def test_create_node_id_is_gap_safe(tmp_path):
    # A dropped/malformed node_created leaves a GAP in node ids (fold skips the bad event). The next
    # created node must take max(id)+1, NOT len(nodes) — len would collide with an existing higher id
    # and silently overwrite it (corrupting lineage/best-selection). Regression for that bug.
    from looplab.engine.orchestrator import Engine
    from looplab.search.policy import GreedyTree
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.adapters.toytask import ToyTask

    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    eng = Engine(tmp_path / "gap", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=8))
    eng.store.append("run_started", {"run_id": "gap", "task_id": "t", "direction": "min"})
    eng.store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": ""}})
    # node id 2 with NO id 1 -> a gap, as if node 1's event was dropped by fold's malformed-event guard
    eng.store.append("node_created", {"node_id": 2, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {"x": 9.0}, "rationale": ""}})
    assert set(fold(eng.store.read_all()).nodes) == {0, 2}   # gap at 1; len(nodes)==2 would hit node 2

    eng._create_node({"kind": "draft"})

    after = fold(eng.store.read_all())
    assert 2 in after.nodes and after.nodes[2].idea.params["x"] == 9.0   # node 2 NOT overwritten
    assert 3 in after.nodes                                              # new node took max+1, not len(=2)


# ------------------------------------------- the run spine's phase helpers (doc 25 XP-06)
# `_run_with_llm_broker` shed three blocks: the re-entry prologue (`_enter_run`), the eval-spec
# onboarding gates (`_run_spec_gates`) and the empty-action ladder (`_handle_no_actions`). Two
# things must hold for that to be a refactor rather than a regression: the signal vocabulary the
# loop dispatches on stays closed, and the helpers that FOLD stay in the module whose `fold` global
# is the monkeypatch seam.


def _toy_engine(tmp_path, **kwargs):
    from looplab.adapters.toytask import ToyTask
    from looplab.engine.orchestrator import Engine

    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    return Engine(tmp_path / "spine", task=task, researcher=r, developer=d,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=2),
                  auto_install_deps=False, **kwargs)


def test_the_run_loop_dispatches_on_a_closed_signal_vocabulary():
    """A phase helper reports an outer-loop `break`/`continue` it cannot execute itself. A typo'd
    literal is the `TRIAGE_ACTIONS` failure class: `"brake"` is not `"break"`, so a stop silently
    becomes another turn. Resolved as real `ast.Return` constants — a comment is not an AST node."""
    import ast

    from _source_scan import function_tree
    from looplab.engine.orchestrator import Engine

    expected = {
        "_run_spec_gates": {"continue", "break", None},   # this one may also fall through
        "_handle_no_actions": {"continue", "break"},
        "_handle_create_actions": {"continue", "break"},
    }
    for name, vocabulary in expected.items():
        tree = function_tree(getattr(Engine, name))
        returned = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Return):
                continue
            value = node.value
            if isinstance(value, ast.Tuple):          # (signal, state, no_mint_turns)
                value = value.elts[0]
            assert value is None or isinstance(value, ast.Constant), (
                f"{name} returns a computed signal: {ast.unparse(node)}")
            returned.add(None if value is None else value.value)
        assert returned == vocabulary, f"{name} returns {returned}, expected {vocabulary}"


class _GateProbe:
    """Stands in for the Engine so the terminal-gate ladder can be driven as a rule.

    Inline in the run loop it was reachable only by staging a real leakage/abort/budget trip with a
    real in-flight Card build, which is why its ORDER — settle first, finish second — was never
    tested in the one direction that matters: that finalization is not even ATTEMPTED while a
    durable request head is open."""

    def __init__(self, *, head=False, forced_head=False, finished=False):
        self.calls: list[tuple] = []
        self._head, self._forced_head, self._finished = head, forced_head, finished

    def _close_card_build_before_terminal_gate(self, state, max_eval_seconds=None):
        self.calls.append(("card_build", max_eval_seconds))
        return self._head

    def _close_node_creating_forced_request_before_terminal_gate(self, state, *, reason):
        self.calls.append(("forced_request", reason))
        return self._forced_head

    def _finish_with_report_if_quiescent(self, state, data, *, after_seq):
        self.calls.append(("finish", data["reason"], after_seq))
        return self._finished


def _drive_gate(probe, reason="aborted", **kwargs):
    from looplab.engine.orchestrator import Engine

    return Engine._settle_terminal_gate(probe, object(), reason, decision_seq=7, **kwargs)


def test_every_run_loop_gate_that_finishes_the_run_goes_through_the_settle_ladder():
    """The ladder is only a rule if every gate is ON it.

    `_settle_terminal_gate` is well covered as a unit below, but that proves the helper, not that a
    gate uses it. The systemic-failure stop shipped calling `_finish_with_report_if_quiescent`
    directly, and it sits BEFORE the speculation block — so unlike the bare call sites further down
    it has no structural guarantee that no Card build head is open, and finishing over one leaves the
    run's own durable request unacknowledged. Resolved as real `ast.Call` nodes inside the gate's own
    branch: a comment naming the helper is not an AST node.
    """
    import ast

    from _source_scan import function_tree
    from looplab.engine.orchestrator import Engine

    tree = function_tree(Engine._run_with_llm_broker)          # the loop spine, not the `run` wrapper
    gates = [node for node in ast.walk(tree)
             if isinstance(node, ast.If) and "_systemic" in ast.unparse(node.test)]
    assert gates, "the systemic-failure gate is no longer in the run loop"
    for gate in gates:
        called = {node.func.attr for node in ast.walk(gate)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        assert "_settle_terminal_gate" in called, (
            "the systemic-failure gate must settle in-flight work through the shared ladder")
        assert "_finish_with_report_if_quiescent" not in called, (
            "finishing directly skips `_close_card_build_before_terminal_gate`, which is the whole "
            "point of the ladder — route the reason through `_settle_terminal_gate` instead")


def test_a_terminal_gate_never_attempts_finalization_while_a_build_head_is_open():
    probe = _GateProbe(head=True, finished=True)
    assert _drive_gate(probe, drain_forced_request=True) == "continue"
    assert probe.calls == [("card_build", None)], (
        "an open Card build must stop the ladder before anything else is tried")


def test_a_terminal_gate_never_attempts_finalization_while_a_forced_creator_is_open():
    probe = _GateProbe(forced_head=True, finished=True)
    assert _drive_gate(probe, "time_budget", max_es=1.5, drain_forced_request=True) == "continue"
    assert probe.calls == [("card_build", 1.5), ("forced_request", "time_budget")], (
        "the eval-seconds ceiling is forwarded, and the drain runs before the finish attempt")


def test_a_terminal_gate_finishes_only_when_the_log_is_quiescent():
    quiet = _GateProbe(finished=True)
    assert _drive_gate(quiet, "leakage") == "break"
    assert quiet.calls == [("card_build", None), ("finish", "leakage", 7)]

    noisy = _GateProbe(finished=False)
    assert _drive_gate(noisy, "leakage") == "continue", (
        "a tail that moved under the decision prefix re-enters every gate, it does not finish")


def test_only_a_budget_gate_drains_a_forced_node_creator():
    """`leakage`/`aborted` outrank a forced creator outright; the budget gates have to skip it
    durably first, so the flag is what tells the two ladders apart."""
    probe = _GateProbe(forced_head=True, finished=True)
    assert _drive_gate(probe, "aborted") == "break"
    assert [call[0] for call in probe.calls] == ["card_build", "finish"]


def test_the_reentry_prologue_still_folds_through_the_orchestrator_module_global(tmp_path,
                                                                                 monkeypatch):
    """`monkeypatch.setattr(orch, "fold", ...)` is what puts the run spine under test in four test
    files, and `_enter_run` folds twice. What this pins is that those folds still REACH the patched
    global: a direct `from looplab.events.replay import fold` binds a different object, so the patch
    would still apply and simply stop reaching the prologue — a silent narrowing, not a red test.
    (Since ENG1-04 step 0 an engine file elsewhere reaches the same seam at call time through
    `engine/shared.py::engine_fold`, so this is no longer why `_enter_run` lives in the orchestrator.)"""
    import looplab.engine.orchestrator as orch

    real = orch.fold
    seen: list[int] = []

    def counting(events):
        seen.append(len(events))
        return real(events)

    eng = _toy_engine(tmp_path)
    monkeypatch.setattr(orch, "fold", counting)
    # `_reentry_repin` folds too (through `engine_fold`, from `reentry.py` since ENG1-04 step 2), so
    # leaving it in place would satisfy this assertion no matter what `_enter_run` itself binds —
    # measured: the guard was vacuous until this stub. Only the prologue's own folds can register now.
    monkeypatch.setattr(eng, "_reentry_repin", lambda: False)
    eng._enter_run()
    assert seen, "_enter_run no longer folds through the orchestrator module global"


def test_the_empty_action_ladder_still_folds_through_the_orchestrator_module_global(tmp_path,
                                                                                    monkeypatch):
    """Same seam, driven through the branch that actually folds: the HITL gate re-reads the log
    after appending its request, because an abort can win the race against the stale snapshot."""
    import looplab.engine.orchestrator as orch

    eng = _toy_engine(tmp_path, require_approval=True, confirm_top_k=0, confirm_seeds=0)
    eng.store.append("run_started", {"run_id": "spine", "task_id": "t", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": ""}})
    eng.store.append("node_evaluated", {"node_id": 0, "metric": 1.0, "metrics": {}})
    events = eng.store.read_all()
    state = fold(events)
    assert state.best() is not None and not state.awaiting_approval

    real = orch.fold
    seen: list[int] = []

    def counting(evs):
        seen.append(len(evs))
        return real(evs)

    monkeypatch.setattr(orch, "fold", counting)
    # The request carries the seq it believes it follows; the fold honours it only when it lands
    # exactly there, so this has to be the log's real tail rather than a placeholder.
    signal = anyio.run(lambda: eng._handle_no_actions(state, decision_seq=events[-1].seq))

    assert seen, "_handle_no_actions no longer folds through the orchestrator module global"
    assert signal == "break", "an accepted approval request stops the loop without finishing"
    assert fold(eng.store.read_all()).awaiting_approval


def test_a_binary_task_asset_materializes_instead_of_crashing_every_node(tmp_path):
    """The setup-provenance hash already handles bytes assets (`c.encode(...) if isinstance(c, str)
    else bytes(c)`), so a task exposing a BINARY asset — a pickled encoder, a parquet shard — passes
    setup cleanly. `write_assets` then called `write_text` on it, which raises TypeError on bytes:
    every node materialization crashed, after setup had already declared the asset fine."""
    from types import SimpleNamespace

    from looplab.engine.workspace import WorkspaceSeeder

    ws = WorkspaceSeeder(SimpleNamespace(_assets={
        "notes.txt": "plain text\n",
        "encoder.pkl": b"\x80\x04\x95binary",
        "shard.bin": bytearray(b"\x00\x01\x02"),
    }))
    wd = tmp_path / "node"
    ws.write_assets(wd)

    assert (wd / "notes.txt").read_text(encoding="utf-8") == "plain text\n"
    assert (wd / "encoder.pkl").read_bytes() == b"\x80\x04\x95binary"
    assert (wd / "shard.bin").read_bytes() == b"\x00\x01\x02"
