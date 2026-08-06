"""A `file_json`/`file_regex` reader with no `path` — in the THREE secondary reader slots.

`eval.metric` was closed first (tests/test_silent_misconfiguration.py): the reader needs the file in
`path`, `key` is the key INSIDE the file, and a spec missing `path` validated on its kind, started a
run, and failed every node with `no_metric`. The same spec in `eval.metrics`, `eval.constraints` and
`eval.cross_check` still validated — and each one failed a DIFFERENT quiet way, which is why the
refusals do not share one consequence sentence:

  - `eval.metrics`     — the reader returns None and `run_command_eval`'s `declared` comprehension
                         drops it, so the named metric is simply absent from the node's report;
  - `eval.constraints` — an unverifiable constraint counts as a VIOLATION (never a silent pass), so
                         EVERY node is infeasible and excluded from best-selection;
  - `eval.cross_check` — `_drift(primary, None, tol)` is True by construction ("can't corroborate ->
                         drift"), so under ratify_freeze_drift EVERY node's metric is discarded.

Every test below reproduces the runtime behaviour FIRST — against `runtime/command_eval.py`, which
still reads a PATHLESS spec exactly as it did — and only then asserts the refusal. That ordering is
the point: the refusal is only worth having if it stands in front of a real defect. The one runtime
behaviour that has since CHANGED is section 4's: a non-string path used to raise TypeError out of
`read_metric` and kill the run, and now abstains like the pathless spec (the submit-time refusal
could not stand alone there, because a grandfathered snapshot never passes it).

NEW FILE because these span `runtime` (the message + reader table), `adapters` (the four slots and
the refusal) and `cli` (the resume grandfathering), and the resume half has no home in any of them.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from looplab.adapters.repo_task import (EvalSpec, RepoTask, _PATHLESS_COST,
                                        eval_reader_path_errors)
from looplab.adapters.tasks import load_task, validate_task
from looplab.runtime.command_eval import (READERS_REQUIRING_PATH, metric_spec_path_error,
                                          run_command_eval, validate_cross_check)

# Writes a metrics file AND prints the metric, so a correctly-pathed file reader and a stdout reader
# both have something real to read — the "still runs" half of every test below.
_PROG = ('import json\n'
         'open("metrics.json","w").write(json.dumps({"metric": 1.0, "lat": 7.0}))\n'
         'print(json.dumps({"metric": 1.0}))\n')

_PRIMARY = {"kind": "stdout_json", "key": "metric"}
_PATHLESS = {"kind": "file_json", "key": "lat"}                          # the bug
_WITH_PATH = {"kind": "file_json", "path": "metrics.json", "key": "lat"}  # the fix
_BAD_TYPE = {"kind": "file_json", "path": 123, "key": "lat"}             # worse: crashes the RUN


def _eval(tmp_path: Path, **kw):
    """Run one real eval in `tmp_path` with the given secondary readers."""
    (tmp_path / "p.py").write_text(_PROG, encoding="utf-8")
    return run_command_eval([sys.executable, "p.py"], str(tmp_path), 60, _PRIMARY, **kw)


def _task(**eval_kw) -> dict:
    return {"kind": "repo", "id": "t", "goal": "g", "direction": "max",
            "editable_path": "examples/repo_example", "edit_surface": ["*.json"],
            "eval": {"command": ["python", "ttrain.py"], "timeout": 60, **eval_kw}}


def _msg(excinfo) -> str:
    return str(excinfo.value)


def load_task_dict(d: dict):
    """Build a task from a dict the way `resume` does — grandfathered, so a spec submit refuses can
    still be constructed for the tests that need one."""
    return validate_task(d, existing_run=True)


# ------------------------------------------------------------------ 1. eval.metrics (extra readers)
def test_a_pathless_extra_metric_is_silently_dropped_then_refused(tmp_path):
    """Reproduction: the declared `lat` reader produces nothing and NOTHING says so — the node's
    report just has no `lat` in it, which reads as "the experiment didn't emit one"."""
    assert _eval(tmp_path, metrics={"lat": _PATHLESS}).extra_metrics is None
    assert _eval(tmp_path, metrics={"lat": _WITH_PATH}).extra_metrics == {"lat": 7.0}

    with pytest.raises(ValueError) as ei:
        RepoTask(**_task(metrics={"lat": _PATHLESS}))
    msg = _msg(ei)
    assert "eval.metrics['lat']" in msg                      # WHICH field
    assert "file_json" in msg and "`path`" in msg            # WHICH reader, WHICH field is missing
    assert '"path": "metrics.json"' in msg                   # a correct example
    assert "silently dropped" in msg                         # the measured consequence, not another's
    assert "no_metric" not in msg


def test_a_pathed_extra_metric_still_validates_and_still_reports(tmp_path):
    task = RepoTask(**_task(metrics={"lat": _WITH_PATH}))
    assert task.eval.metrics["lat"]["path"] == "metrics.json"
    assert _eval(tmp_path, metrics={"lat": _WITH_PATH}).extra_metrics == {"lat": 7.0}


# --------------------------------------------------------------------------- 2. eval.constraints
def test_a_pathless_constraint_makes_every_node_infeasible_then_is_refused(tmp_path):
    """Reproduction: `_violations` treats an unreadable value as a violation (fail-closed, by
    design), so a constraint that can NEVER be read excludes every node from best-selection — the
    run measures fine and then reports no winner."""
    res = _eval(tmp_path, constraints=[dict(_PATHLESS, name="lat", max=10.0)])
    assert res.metric == 1.0                                  # the node is measured …
    assert res.violations == [{"name": "lat", "value": None, "max": 10.0, "min": None}]  # … and excluded
    assert _eval(tmp_path, constraints=[dict(_WITH_PATH, name="lat", max=10.0)]).violations is None

    with pytest.raises(ValueError) as ei:
        RepoTask(**_task(constraints=[dict(_PATHLESS, name="lat", max=10.0)]))
    msg = _msg(ei)
    assert "eval.constraints[0]" in msg and "name='lat'" in msg
    assert "file_json" in msg and "`path`" in msg
    assert '"path": "metrics.json"' in msg
    assert "excluded from best-selection" in msg
    assert "no_metric" not in msg


def test_a_pathed_constraint_still_validates_and_still_gates(tmp_path):
    RepoTask(**_task(constraints=[dict(_WITH_PATH, name="lat", max=10.0)]))
    assert _eval(tmp_path, constraints=[dict(_WITH_PATH, name="lat", max=10.0)]).violations is None
    # and still VIOLATES when the bound is actually exceeded — the guard didn't defang the gate
    assert _eval(tmp_path, constraints=[dict(_WITH_PATH, name="lat", max=1.0)]).violations == [
        {"name": "lat", "value": 7.0, "max": 1.0, "min": None}]


# ---------------------------------------------------------------------------- 3. eval.cross_check
def test_a_pathless_cross_check_discards_every_nodes_metric_then_is_refused(tmp_path):
    """Reproduction, and the most consequential of the three: `_drift` is fail-closed on a cross
    reader that produced nothing, so under `ratify_freeze_drift` a metric the eval genuinely
    produced is thrown away on every node and the run ends with no best node."""
    res = _eval(tmp_path, cross_check=_PATHLESS, enforce_drift=True)
    assert res.metric is None
    assert res.drift == {"primary": 1.0, "cross": None, "tolerance": 1e-6}
    ok = _eval(tmp_path, cross_check={"kind": "file_json", "path": "metrics.json", "key": "metric"},
               enforce_drift=True)
    assert ok.metric == 1.0 and ok.drift is None

    with pytest.raises(ValueError) as ei:
        RepoTask(**_task(cross_check=_PATHLESS))
    msg = _msg(ei)
    assert "eval.cross_check" in msg
    assert "file_json" in msg and "`path`" in msg
    assert '"path": "metrics.json"' in msg
    assert "discarded as drift" in msg
    assert "no_metric" not in msg


def test_the_pathless_cross_check_costs_the_whole_run_end_to_end(tmp_path):
    """The claim the refusal rests on, proved through a REAL engine run rather than one eval: two
    nodes that each produced a metric, both recorded as drift, and no winner."""
    import anyio

    from looplab.engine.orchestrator import Engine
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "run.py").write_text(_PROG, encoding="utf-8")
    # `existing_run=True` is the ONLY way to build this task now, which is exactly the point: the
    # document is one submit refuses, so it can only reach an engine from a pre-existing snapshot.
    task = validate_task({"kind": "repo", "id": "d", "goal": "g", "direction": "max",
                          "editable_path": str(repo), "edit_surface": ["*.txt"],
                          "eval": {"command": [sys.executable, "run.py"], "metric": _PRIMARY,
                                   "cross_check": _PATHLESS, "timeout": 60}},
                         existing_run=True)
    r, d = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=2),
                 eval_trust_mode="ratify_freeze_drift")
    state = anyio.run(eng.run)
    assert [n.metric for n in state.nodes.values()] == [None, None]
    assert len(state.drifts) == 2 and state.drifts[0]["cross"] is None
    assert state.best_node_id is None


# ------------------------------------------------- 4. a non-string path: it took down the RUN
@pytest.mark.parametrize("slot,payload", [
    ("metrics", {"metrics": {"lat": _BAD_TYPE}}),
    ("constraints", {"constraints": [dict(_BAD_TYPE, name="lat", max=10.0)]}),
    ("cross_check", {"cross_check": _BAD_TYPE}),
])
def test_a_non_string_path_in_any_slot_is_refused_at_submit_and_abstains_at_runtime(slot, payload,
                                                                                    tmp_path):
    """Worse than missing, in every slot: `_confined` catches only (OSError, ValueError,
    RuntimeError), so `Path(workdir) / 123` raised an uncaught TypeError out of `read_metric` —
    the eval worker's only handler is `except GpuPinUnenforceable`, so the node got NO terminal
    event and the whole run died (and re-died on every resume). For a RepoTask it does not even get
    that far: `_eval_protected` used to `pre + 123` inside `repo_spec()`, i.e. inside
    `Engine.__init__`.

    The runtime half is now CLOSED as well: `read_metric` asks `_nonstring_path_slot` before it
    dispatches, so the eval degrades to EXACTLY the pathless spec's measured cost in this slot
    (asserted below, one branch per slot) and the NODE fails instead of the run. Both halves are
    needed — `_grandfathered` reloads a run's recorded `task.snapshot.json` without re-validating it,
    so a document authored before the submit-time refusal existed reaches `read_metric` regardless.
    The per-reader coverage of the runtime guard lives in tests/test_metric_reader_confinement.py.
    """
    kw = dict(payload)
    if slot == "cross_check":
        kw["enforce_drift"] = True
    res = _eval(tmp_path, **kw)                              # the defect: this used to raise TypeError
    assert res.extra_metrics is None                         # the broken reader produced nothing …
    if slot == "constraints":                                # … and each slot pays the PATHLESS cost
        assert res.metric == 1.0
        assert res.violations == [{"name": "lat", "value": None, "max": 10.0, "min": None}]
    elif slot == "cross_check":
        assert res.metric is None
        assert res.drift == {"primary": 1.0, "cross": None, "tolerance": 1e-6}
    else:
        assert res.metric == 1.0 and res.violations is None
    with pytest.raises(ValueError, match="STRING"):          # …and unreachable from a submit
        RepoTask(**_task(**payload))


# ------------------------------------------------------------------ 5. the authoring spellings
@pytest.mark.parametrize("payload", [
    {"metrics": {"lat": {"reader": "file_json", "key": "lat"}}},
    {"constraints": [{"reader": "file_json", "key": "lat", "name": "lat", "max": 1.0}]},
    {"cross_check": {"reader": "file_json", "key": "metric"}},
])
def test_the_composable_reader_spelling_reaches_the_refusal_too(payload):
    """`{"reader": ...}` is normalized to `kind` by `adapters/tasks.py` BEFORE construction, and
    that is the spelling Genesis and the UI actually author — a refusal that only the legacy
    `kind` spelling reached would miss every model-written spec."""
    with pytest.raises(ValueError, match="path"):
        validate_task({"id": "t", "goal": "g", "direction": "max",
                       "repo": "examples/repo_example",
                       "cmd": {"command": ["python", "ttrain.py"], **payload}})


def test_every_broken_slot_is_reported_in_one_refusal():
    """One authoring mistake ("`key` is the file") lands in several slots at once; reporting only
    the first would cost a submit round-trip per slot."""
    with pytest.raises(ValueError) as ei:
        RepoTask(**_task(metric=_PATHLESS, metrics={"lat": _PATHLESS},
                         constraints=[dict(_PATHLESS, name="lat", max=1.0)],
                         cross_check=_PATHLESS))
    msg = _msg(ei)
    for label in ("eval.metric:", "eval.metrics['lat']", "eval.constraints[0]", "eval.cross_check"):
        assert label in msg, label


# ------------------------------------------------------- 6. the resume decision (the judgement)
_LEGACY_SLOTS = [
    ("metric", {"metric": _PATHLESS}),
    ("metrics", {"metrics": {"lat": _PATHLESS}}),
    ("constraints", {"constraints": [dict(_PATHLESS, name="lat", max=1.0)]}),
    ("cross_check", {"cross_check": _PATHLESS}),
    ("non-string path", {"cross_check": _BAD_TYPE}),
]


@pytest.mark.parametrize("slot,payload", _LEGACY_SLOTS, ids=[s for s, _ in _LEGACY_SLOTS])
def test_a_run_that_already_exists_still_resumes(slot, payload, tmp_path):
    """THE constraint on this fix. `resume`/`finalize` rebuild the task by re-validating the
    verbatim `task.snapshot.json` the run was started with, so a validator that raises makes every
    pre-existing run permanently unresumable — settings recorded at `run_started` are supposed to
    win on resume (CLAUDE.md invariant #6), and the same has to hold for the task document.

    `repo_spec()` is called too: it is what `Engine.__init__` calls, and it is where a grandfathered
    NON-STRING path would otherwise still crash on the way back in."""
    snap = tmp_path / "task.snapshot.json"
    snap.write_text(json.dumps(_task(**payload)), encoding="utf-8")
    with pytest.raises(ValueError):                          # a fresh submit of the same file
        load_task(snap)
    task = load_task(snap, existing_run=True)                # the reload path
    assert task.repo_spec()["protected_names"] is not None
    assert task.eval_spec()["command"] == ["python", "ttrain.py"]


class _StubCommands:
    def _sequence_path(self, rd):
        return rd / "commands.seq"


class _StubSettings:
    def ordinary_settings_env(self, launch_settings):
        return {}


class _StubSrv:
    """`_prepare_receipt` touches `srv` for one derived name; `_frozen_launch` for one env dict."""

    commands = _StubCommands()
    settings = _StubSettings()


def _existing_run_dir(tmp_path: Path) -> Path:
    """A run dir whose recorded task is WELL-FORMED — the control case. `config.snapshot.json` is
    written too, so the receipt freezes real settings rather than falling back to defaults."""
    from looplab.core.config import Settings

    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "task.snapshot.json").write_text(
        json.dumps(_task(cross_check=_WITH_PATH)), encoding="utf-8")
    (rd / "config.snapshot.json").write_text(
        json.dumps(Settings().masked_snapshot()), encoding="utf-8")
    return rd


@pytest.mark.parametrize("slot,payload", _LEGACY_SLOTS, ids=[s for s, _ in _LEGACY_SLOTS])
def test_replay_stays_strict_on_the_runs_own_snapshot_unlike_resume(slot, payload, tmp_path):
    """The asymmetry is DELIBERATE, and this test exists because it does not look it.

    `resume` and UI Replay read the same bytes — a run's own `task.snapshot.json` — and disagree:
    resume grandfathers, Replay refuses. That reads exactly like a missed call site, and the reason
    it isn't lived only in `2dd8cfa4`'s commit message, where nobody hunting the asymmetry will look.

    The reason: Replay does not RE-ENTER the run. `_frozen_launch` spawns `looplab run` against the
    frozen stage, and `run` is a fresh SUBMIT that validates strictly regardless. So waiving the
    check in `reset_route` cannot make Replay succeed — it only moves the identical refusal past a
    committed receipt. The third assertion below is that premise, pinned: if `load_task` ever stops
    refusing a fresh submit of these bytes, the argument for strictness here has evaporated and this
    decision must be revisited rather than silently inherited."""
    from fastapi import HTTPException

    from looplab.serve import reset_route

    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "task.snapshot.json").write_text(json.dumps(_task(**payload)), encoding="utf-8")

    load_task(rd / "task.snapshot.json", existing_run=True)  # resume: accepted
    with pytest.raises(ValueError):                          # Replay's spawned `run`: refused
        load_task(rd / "task.snapshot.json")

    with pytest.raises(HTTPException) as excinfo:            # so the preflight refuses too, early
        reset_route._prepare_receipt(
            _StubSrv(), rd, expected_generation="gen-a", operation_id="op-1")
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "replay_task_invalid"


def test_replay_refuses_before_it_publishes_anything(tmp_path):
    """Strictness is only defensible while the refusal lands BEFORE the operation is committed.

    `_prepare_receipt` stages the task last, precisely so a validation error is the one unpublished
    failure (its own comment says so). If a refusal ever moved after publication it would hit
    `_frozen_launch`'s 503, whose remediation forbids the only escape — "Inspect the durable receipt;
    do not create another Replay" — and the committed operation would be stuck with inputs the
    operator cannot edit. Pin that the staged path is cleaned up and no receipt survives."""
    from fastapi import HTTPException

    from looplab.serve import reset_route

    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "task.snapshot.json").write_text(
        json.dumps(_task(cross_check=_PATHLESS)), encoding="utf-8")

    before = {p.name for p in rd.iterdir()}
    with pytest.raises(HTTPException):
        reset_route._prepare_receipt(
            _StubSrv(), rd, expected_generation="gen-a", operation_id="op-1")
    assert {p.name for p in rd.iterdir()} == before, "a refused Replay left a staged task behind"


def test_a_valid_snapshot_still_replays(tmp_path):
    """The other side of the fence: strictness must not refuse a well-formed run. Without this the
    two tests above would pass just as well if Replay were broken for everyone."""
    from fastapi import HTTPException

    from looplab.serve import reset_route

    rd = _existing_run_dir(tmp_path)
    record = reset_route._prepare_receipt(
        _StubSrv(), rd, expected_generation="gen-a", operation_id="op-1")
    assert record["task_digest"] and (rd / record["task_stage"]).is_file()

    spawn_args, env, launch_settings = reset_route._frozen_launch(
        _StubSrv(), rd, record, operation_id="op-1")
    assert spawn_args[0] == "run" and str(rd) in spawn_args
    assert isinstance(env, dict) and isinstance(launch_settings, dict)


def test_resume_reports_the_refusal_it_waived(tmp_path, monkeypatch, capsys):
    """Grandfathering must not be SILENT: the operator resuming such a run is re-entering the exact
    failure the refusal exists to name, so `resume` prints the same message as a warning."""
    import looplab.cli.run_cmds as rc

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "task.snapshot.json").write_text(
        json.dumps(_task(cross_check=_PATHLESS)), encoding="utf-8")
    monkeypatch.setattr(rc, "_require_run_dir", lambda *a, **k: None)
    monkeypatch.setattr(rc, "load_run_settings", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("stop-after-load")))
    with pytest.raises(RuntimeError, match="stop-after-load"):
        rc.resume(run_dir, task_file=None, max_nodes=None)
    err = capsys.readouterr().err
    assert "warning:" in err and "eval.cross_check" in err and "discarded as drift" in err


def test_the_runtime_guard_is_unchanged_so_a_resumed_run_still_evaluates(tmp_path):
    """Why the check is NOT inside `validate_cross_check`: that predicate is also the per-eval
    runtime guard (`run_command_eval` calls it before every drift read), so a raise there would
    abort the eval worker with no terminal event for the node — turning a run that merely loses its
    metrics into one that cannot finish or resume at all. It still refuses only `adapter`."""
    assert validate_cross_check(_PATHLESS) == _PATHLESS       # no new runtime refusal
    with pytest.raises(ValueError, match="independent built-in reader"):
        validate_cross_check({"kind": "adapter", "path": "x.py"})
    # and the eval itself still completes (fail-closed on the metric, not on the node's terminal)
    res = _eval(tmp_path, cross_check=_PATHLESS, enforce_drift=True)
    assert res.exit_code == 0 and res.drift is not None


# ------------------------------------------------------------------- 7. the shared enumerations
def test_every_reader_slot_goes_through_one_walk():
    """`EvalSpec.readers()` is THE enumeration of "where a reader can appear". A fifth slot that
    forgets it inherits neither the submit-time `path` check nor `_eval_protected`'s write
    protection — which is exactly how these three came to be missing the first one."""
    spec = EvalSpec(command=["python", "t.py"],
                    metric={"kind": "file_json", "path": "a.json", "key": "m"},
                    metrics={"lat": {"kind": "file_json", "path": "b.json", "key": "lat"}},
                    constraints=[{"kind": "file_json", "path": "c.json", "key": "lat",
                                  "name": "lat", "max": 1.0}],
                    cross_check={"kind": "file_json", "path": "d.json", "key": "m"})
    slots = [slot for _label, slot, _r in spec.readers()]
    assert slots == ["metric", "metrics", "constraints", "cross_check"]
    assert set(slots) == set(_PATHLESS_COST)                 # a slot with no measured consequence …
    for slot in slots:                                       # … or no field is a KeyError, not a quiet miss
        assert slot in EvalSpec.model_fields

    # behavioural half: the write gate protects the file named by EVERY slot (review C1)
    task = RepoTask(id="t", direction="max", editable_path="examples/repo_example",
                    edit_surface=["*.json"], eval=spec)
    assert set(task.repo_spec()["protected_names"]) >= {"a.json", "b.json", "c.json", "d.json"}


def test_which_readers_need_a_path_is_spelled_once():
    """The consequence sentence is per-slot; the RULE is not. Every slot asks the same authority
    (`runtime/command_eval.metric_spec_path_error`, which lives beside the reader table that decides
    it), so a reader added to `READERS_REQUIRING_PATH` is refused in all four slots at once."""
    for slot, cost in _PATHLESS_COST.items():
        for kind in READERS_REQUIRING_PATH:
            assert metric_spec_path_error({"kind": kind}, consequence=cost).endswith(cost)
        # a reader that needs no path is not refused in ANY slot
        assert metric_spec_path_error({"kind": "stdout_json"}, consequence=cost) is None
        assert metric_spec_path_error({"kind": "adapter"}, consequence=cost) is None


def test_the_walk_takes_a_whole_task_and_is_empty_when_there_are_no_readers():
    """`cli/run_cmds.py::resume` calls this on whatever task it loaded — which may be a toy/dataset
    task with no `eval` at all, or (in tests that stub the loader) not a task at all. None of those
    may become an AttributeError on the resume path."""
    task = load_task_dict(_task(cross_check=_PATHLESS))              # a RepoTask, not an EvalSpec
    assert eval_reader_path_errors(task) == eval_reader_path_errors(task.eval) != []
    assert eval_reader_path_errors(None) == []
    assert eval_reader_path_errors(object()) == []                   # the stubbed-loader case
    assert eval_reader_path_errors({"metric": _PATHLESS}) == []      # a bare dict is not an EvalSpec
    assert eval_reader_path_errors(EvalSpec(command=["python", "t.py"])) == []
