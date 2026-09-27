"""`max_launch_timeout_s`: the HARD per-launch wall clock, configurable from 24 h up to a week
(2026-09-27).

Every subprocess deadline is clamped at the launch (`runtime/sandbox.py::finite_timeout`), and the
ceiling was the literal `sandbox.MAX_TIMEOUT_S = 24 * 3600`. A long SFT evaluation whose single
training stage needs more than a day therefore could not be given it: the server refused a
`budget_extend{eval_timeout}` above 86400 (deliberately — a larger number would have been accepted and
then cut at the launch), and a task that declared two days was cut at one.

The ceiling is now ONE value per run, `Settings.max_launch_timeout_s` (range in `core/numeric.py`),
read by two processes that must agree: the ENGINE installs it process-wide at start
(`cli/__init__.py::_engine`), and the SERVER validates `budget_extend{eval_timeout}` against the
run's recorded value (`serve/control_validation.py::_run_launch_ceiling`).

What is DRIVEN here, not pinned:
  * the default is the historical 24 h everywhere (Settings, the installed ceiling, the clamp, the
    override reader, the server) — a run that sets nothing is unchanged;
  * one range: the Settings field and the runtime setter refuse the same values;
  * a configured week reaches a REAL launch (`run_argv` hands the child 604800 s) and the task-spec
    timeouts above a day are honoured up to it;
  * the server accepts 604800 on a run configured for a week — through `normalize_control` and
    through `PUT /config` + `POST /commands` — refuses one second more LOUDLY, refuses anything above
    a day on an unconfigured run, and fails CLOSED to the default on a snapshot it cannot read;
  * the engine end to end: a task declaring two days runs one under the default and two under a
    week, and the operator's week-long extension is what the next evaluation's process is given;
  * the one place a clamp can still bite (an engine started before the ceiling was raised) is
    REPORTED, once per value.
"""
from __future__ import annotations

import json
import logging
import math
import sys

import anyio
import pytest

from looplab.core.config import Settings
from looplab.core.errors import ConfigRefusal
from looplab.core.numeric import LAUNCH_TIMEOUT_DEFAULT_S, LAUNCH_TIMEOUT_LIMIT_S
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.runtime import command_eval, sandbox
from looplab.runtime.command_eval import eval_spec_time_budget, eval_timeout_override
from looplab.runtime.sandbox import MAX_TIMEOUT_S, finite_timeout, launch_timeout_ceiling

WEEK = 7 * 24 * 3600.0
DAY = 24 * 3600.0


@pytest.fixture
def install_ceiling(monkeypatch):
    """`set_launch_timeout_ceiling`, with the process-wide value restored after the test — the
    ceiling is one per process, and a week leaking into the next test would be a false green there."""
    monkeypatch.setattr(sandbox, "_launch_timeout_ceiling_s", sandbox._launch_timeout_ceiling_s)
    return sandbox.set_launch_timeout_ceiling


def _snapshot(rd, **settings) -> None:
    """The run's `config.snapshot.json` as `looplab run` writes it (a full masked dump)."""
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "config.snapshot.json").write_text(
        json.dumps(Settings(**settings).masked_snapshot()), encoding="utf-8")


# ------------------------------------------------------------------------------ the default

def test_the_default_ceiling_is_the_historical_24_hours_everywhere():
    assert LAUNCH_TIMEOUT_DEFAULT_S == MAX_TIMEOUT_S == DAY
    assert LAUNCH_TIMEOUT_LIMIT_S == WEEK
    assert Settings().max_launch_timeout_s == DAY
    # Nothing installed in this process (and nothing left behind by another test): the clamp, the
    # override reader and the task budget all stop at a day, exactly as before the field existed.
    assert launch_timeout_ceiling() == DAY
    assert finite_timeout(1e18) == DAY
    assert finite_timeout(2 * DAY) == DAY
    assert eval_timeout_override({"eval_timeout": WEEK}) == DAY
    assert eval_spec_time_budget({"command": ["x"], "timeout": 2 * DAY}) == DAY


def test_the_agent_clamp_keeps_its_one_day_bound():
    """`max_eval_timeout` bounds what a MODEL may ask for and stays at the default ceiling — the
    operator's own `budget_extend{eval_timeout}` is what lifts it (`effective_max_eval_timeout`)."""
    field = Settings.model_fields["max_eval_timeout"]
    assert [getattr(m, "le", None) for m in field.metadata if getattr(m, "le", None)] == [DAY]
    Settings(max_eval_timeout=DAY)
    with pytest.raises(ValueError):
        Settings(max_eval_timeout=DAY + 1)


# ------------------------------------------------------------------------------ one range

@pytest.mark.parametrize("value", [DAY, 2 * DAY, WEEK, int(WEEK), "604800"])
def test_the_setting_and_the_runtime_accept_the_same_range(value, install_ceiling):
    assert Settings(max_launch_timeout_s=value).max_launch_timeout_s == float(value)
    assert install_ceiling(value) == float(value) == launch_timeout_ceiling()


@pytest.mark.parametrize("value", [DAY - 1, WEEK + 1, 0, -1, float("nan"), float("inf"), True,
                                   "abc", None])
def test_the_setting_and_the_runtime_refuse_the_same_values(value, install_ceiling):
    with pytest.raises(ValueError):
        Settings(max_launch_timeout_s=value)
    before = launch_timeout_ceiling()
    with pytest.raises(ConfigRefusal) as refused:
        install_ceiling(value)
    assert "max_launch_timeout_s" in str(refused.value)
    assert launch_timeout_ceiling() == before, "a refused value must not be half-installed"


def test_the_setting_reads_its_env_var(monkeypatch):
    monkeypatch.setenv("LOOPLAB_MAX_LAUNCH_TIMEOUT_S", "604800")
    assert Settings().max_launch_timeout_s == WEEK


# ------------------------------------------------------------------------ a configured week

def test_a_week_reaches_the_clamp_the_override_and_the_task_budget(install_ceiling):
    install_ceiling(WEEK)
    assert finite_timeout(WEEK) == WEEK
    assert finite_timeout(1e18) == WEEK, "still bounded — a week, not forever"
    assert eval_timeout_override({"eval_timeout": WEEK}) == WEEK
    assert eval_timeout_override({"eval_timeout": 10 * WEEK}) == WEEK
    assert eval_timeout_override({"eval_timeout": 10 * WEEK}, clamp=False) == 10 * WEEK
    spec = {"command": ["x"], "timeout": 2 * DAY, "profiles": {"long": {"timeout": WEEK}}}
    assert eval_spec_time_budget(spec) == WEEK
    assert command_eval.build_command(spec, {}, None)[1] == 2 * DAY
    assert command_eval.build_command(spec, {}, "long")[1] == WEEK


def test_a_real_launch_is_given_the_configured_week(tmp_path, install_ceiling):
    """At the universal choke point: `run_argv` clamps, then tells the child its own clock
    (`LOOPLAB_EVAL_TIMEOUT_S`), so the number the child prints IS the deadline it was given."""
    argv = [sys.executable, "-c", "import os; print(os.environ['LOOPLAB_EVAL_TIMEOUT_S'])"]

    def given(timeout):
        rc, out, err, timed_out = sandbox.run_argv(argv, str(tmp_path), timeout)
        assert rc == 0 and not timed_out, err
        return float(out.strip())

    assert given(WEEK) == DAY, "unconfigured: the historical cut"
    install_ceiling(WEEK)
    assert given(WEEK) == WEEK
    assert given(10 * WEEK) == WEEK


def test_task_spec_timeouts_above_a_day_are_admitted_and_meet_the_configured_ceiling(
        tmp_path, install_ceiling):
    """The task boundary never bounded a timeout ABOVE (only finite and positive), so a two-day
    `eval.timeout`, a week-long profile, a canary and a declared stage are all accepted at submit and
    the ONE clamp is the launch's — the configured ceiling, not a second 24 h somewhere else."""
    from looplab.adapters.repo_task import EvalSpec, RepoTask

    repo = tmp_path / "repo"
    repo.mkdir()
    task = RepoTask(
        id="t", direction="max", editable_path=str(repo), edit_surface=["*.py"],
        eval=EvalSpec(command=[sys.executable, "run.py"], metric={"kind": "stdout_json",
                                                                  "key": "metric"},
                      timeout=2 * DAY, profiles={"long": {"timeout": WEEK}},
                      canary={"timeout": 2 * DAY},
                      stages=[{"name": "train", "command": [sys.executable, "train.py"],
                               "timeout": WEEK}]))
    clean, err = command_eval.validate_stages(
        [{"name": "train", "command": ["python", "train.py"], "timeout": WEEK}])
    assert err is None and clean[0]["timeout"] == WEEK
    spec = task.eval_spec()
    assert eval_spec_time_budget(spec) == DAY
    hint = task._eval_profile_researcher_hint()
    assert "effective runtime timeout 86400 seconds" in hint
    assert "runtime cap of 86400 seconds" in hint
    install_ceiling(WEEK)
    assert eval_spec_time_budget(spec) == WEEK
    hint = task._eval_profile_researcher_hint()
    assert "effective runtime timeout 604800 seconds" in hint
    assert "runtime cap of 604800 seconds" in hint


# ------------------------------------------------------------------------------- the server

def _normalize(root, payload, *, run_id="run"):
    pytest.importorskip("fastapi")
    from tests.test_control_registry import _Srv
    from looplab.serve.control_validation import normalize_control
    return normalize_control(_Srv(root), root / run_id, "budget_extend", payload)


def _seeded(root, run_id="run"):
    pytest.importorskip("fastapi")
    from tests.test_control_registry import _seed
    return _seed(root, run_id)


def _refusal(root, payload, **kw) -> str:
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as refused:
        _normalize(root, payload, **kw)
    assert refused.value.status_code == 400
    return str(refused.value.detail)


def test_an_unconfigured_run_refuses_more_than_a_day_and_names_the_setting(tmp_path):
    _seeded(tmp_path)
    assert _normalize(tmp_path, {"eval_timeout": DAY})["eval_timeout"] == DAY
    detail = _refusal(tmp_path, {"eval_timeout": DAY + 1})
    assert "(0, 86400]" in detail and "max_launch_timeout_s" in detail and "7 days" in detail


def test_a_run_configured_for_a_week_accepts_a_week_and_refuses_one_second_more(tmp_path):
    rd = _seeded(tmp_path)
    _snapshot(rd, max_launch_timeout_s=WEEK)
    assert _normalize(tmp_path, {"eval_timeout": WEEK})["eval_timeout"] == WEEK
    assert _normalize(tmp_path, {"eval_timeout": "172800"})["eval_timeout"] == 2 * DAY
    detail = _refusal(tmp_path, {"eval_timeout": WEEK + 1})
    assert "(0, 604800]" in detail and "7 days" in detail
    # the shape refusals still name the run's own bound
    assert "(0, 604800]" in _refusal(tmp_path, {"eval_timeout": float("nan")})


def test_the_bound_is_the_runs_own_not_the_servers(tmp_path):
    """Two runs under one server, configured differently, are held to their OWN ceilings."""
    week = _seeded(tmp_path, "week")
    _snapshot(week, max_launch_timeout_s=WEEK)
    day = _seeded(tmp_path, "day")
    _snapshot(day)
    assert _normalize(tmp_path, {"eval_timeout": 3 * DAY}, run_id="week")["eval_timeout"] == 3 * DAY
    assert "(0, 86400]" in _refusal(tmp_path, {"eval_timeout": 3 * DAY}, run_id="day")


def test_a_snapshot_from_before_the_field_takes_the_env_var_the_resume_child_takes(
        tmp_path, monkeypatch):
    """A run started before 2026-09-27 has no key; the resume child resolves it from its
    environment (inherited from the server that spawns it), and the server resolves it the same way."""
    rd = _seeded(tmp_path)
    legacy = Settings().masked_snapshot()
    legacy.pop("max_launch_timeout_s")
    (rd / "config.snapshot.json").write_text(json.dumps(legacy), encoding="utf-8")
    assert "(0, 86400]" in _refusal(tmp_path, {"eval_timeout": WEEK})
    monkeypatch.setenv("LOOPLAB_MAX_LAUNCH_TIMEOUT_S", str(int(WEEK)))
    assert _normalize(tmp_path, {"eval_timeout": WEEK})["eval_timeout"] == WEEK
    # ...and a snapshot that RECORDS the field is not overridden by the environment
    _snapshot(rd, max_launch_timeout_s=DAY)
    assert json.loads((rd / "config.snapshot.json").read_text())["max_launch_timeout_s"] == DAY
    assert "(0, 86400]" in _refusal(tmp_path, {"eval_timeout": WEEK})


@pytest.mark.parametrize("damage", [
    "{not json",
    json.dumps({"max_launch_timeout_s": 10 * WEEK}),       # hand-edited past the limit
    json.dumps(["not", "an", "object"]),
])
def test_an_unreadable_snapshot_fails_closed_to_the_default(tmp_path, damage):
    """The run's ceiling cannot be known, so the floor every ceiling sits at or above is used: a
    value no engine can cut is accepted, and anything more is refused rather than risked."""
    rd = _seeded(tmp_path)
    (rd / "config.snapshot.json").write_text(damage, encoding="utf-8")
    assert _normalize(tmp_path, {"eval_timeout": DAY})["eval_timeout"] == DAY
    assert "(0, 86400]" in _refusal(tmp_path, {"eval_timeout": DAY + 1})


def test_the_operators_http_path_edit_the_run_config_then_extend_to_a_week(tmp_path):
    """What the operator does, over HTTP: a week is a REJECTED command on a default run (nothing
    appended); `PUT /config` raises the run's ceiling; the same week is then accepted and lands."""
    pytest.importorskip("fastapi")
    from tests.factories import command_terminal, post_command
    from tests.test_run_command_service import _Driver, _client, _seed
    from tests.test_server import _run_config_put

    rd = _seed(tmp_path)
    _snapshot(rd)
    client, _srv = _client(tmp_path, _Driver())

    def extends():
        return [e.data for e in EventStore(rd / "events.jsonl").read_all()
                if e.type == "budget_extend"]

    refused = post_command(client, "budget_extend", {"eval_timeout": WEEK}, key="too-long").json()
    assert refused["status"] == "rejected"
    assert refused["error"]["code"] == "invalid_command"
    assert "max_launch_timeout_s" in refused["error"]["message"]
    assert extends() == []

    put = _run_config_put(client, "demo", {"settings": {"max_launch_timeout_s": WEEK}})
    assert put.status_code == 200, put.text
    assert put.json()["changed"] == ["max_launch_timeout_s"]
    assert json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))[
        "max_launch_timeout_s"] == WEEK
    # the editor refuses what the engine would: the model's own range, over HTTP
    too_far = _run_config_put(client, "demo", {"settings": {"max_launch_timeout_s": WEEK + 1}})
    assert too_far.status_code == 422, too_far.text

    accepted = post_command(client, "budget_extend", {"eval_timeout": WEEK}, key="week")
    assert accepted.status_code in (200, 201, 202), accepted.text
    command_terminal(client, accepted.json())
    assert [row["eval_timeout"] for row in extends()] == [WEEK]
    assert fold(EventStore(rd / "events.jsonl").read_all()).budget_overrides["eval_timeout"] == WEEK


# ------------------------------------------------------------------------------- the engine

def test_the_cli_engine_builder_installs_the_runs_ceiling_before_the_roles(
        tmp_path, install_ceiling, monkeypatch):
    """`cli/__init__.py::_engine` is the funnel every run/resume/finalize (and UI spawn) goes
    through; it installs the ceiling from the Settings it was handed, BEFORE the roles are built —
    a repo Researcher's profile hint quotes the ceiling at construction."""
    import looplab.cli as cli
    from looplab.adapters.toytask import ToyTask
    from looplab.core import tracing

    # `_engine` also installs the process-wide LLM-capture default; keep this test from moving it.
    monkeypatch.setattr(tracing, "_CAPTURE_LLM_IO", tracing._CAPTURE_LLM_IO)
    seen = []
    real = cli.make_roles

    def recording(task, settings, run_dir):
        seen.append(launch_timeout_ceiling())
        return real(task, settings, run_dir)

    monkeypatch.setattr(cli, "make_roles", recording)
    cli._engine(tmp_path / "week", ToyTask(), Settings(backend="toy", max_launch_timeout_s=WEEK),
                None)
    assert seen and seen[0] == WEEK
    assert launch_timeout_ceiling() == WEEK
    # the next engine this process builds installs ITS run's value — a default run is a day again
    cli._engine(tmp_path / "day", ToyTask(), Settings(backend="toy"), None)
    assert launch_timeout_ceiling() == DAY


def _run_task(tmp_path, name: str, timeout: float, max_nodes: int = 1):
    from tests.test_live_eval_timeout import _PRINT_OWN_TIMEOUT, _engine, _task

    repo = tmp_path / f"{name}-repo"
    repo.mkdir(exist_ok=True)
    (repo / "run.py").write_text(_PRINT_OWN_TIMEOUT, encoding="utf-8")
    task = _task(repo, timeout=timeout)
    rd = tmp_path / name
    return task, rd, (lambda: anyio.run(_engine(rd, task, max_nodes).run))


def test_the_engine_runs_a_two_day_task_for_one_day_by_default_and_two_when_configured(
        tmp_path, install_ceiling):
    """The eval prints its OWN leash as its metric (`LOOPLAB_EVAL_TIMEOUT_S`), so each number below
    is the deadline the process really had — dispatch, stages, `run_argv` all between."""
    from tests.test_live_eval_timeout import _metrics

    _task_, _rd, run = _run_task(tmp_path, "default", 2 * DAY)
    assert _metrics(run()) == [DAY], "the historical cut, byte for byte"
    install_ceiling(WEEK)
    _task_, _rd, run = _run_task(tmp_path, "week", 2 * DAY)
    assert _metrics(run()) == [2 * DAY]


def test_the_operators_week_long_extension_is_the_next_evaluations_deadline(
        tmp_path, install_ceiling):
    """End to end on a run configured for a week: the server admits `eval_timeout = 604800` for
    THIS run, the fold keeps it, and the evaluation after it — on a fresh engine, i.e. a resume —
    is launched with a week."""
    from tests.test_live_eval_timeout import _metrics

    install_ceiling(WEEK)                                  # what `_engine` does for this run
    _task_, rd, run = _run_task(tmp_path, "run", 4 * 3600.0)
    assert _metrics(run()) == [4 * 3600.0]
    _snapshot(rd, max_launch_timeout_s=WEEK)               # what `looplab run` recorded
    data = _normalize(tmp_path, {"eval_timeout": WEEK})
    store = EventStore(rd / "events.jsonl")
    store.append("budget_extend", data)
    store.append("budget_extend", {"add_nodes": 1})
    store.append("run_reopened", {})
    assert _metrics(run()) == [4 * 3600.0, WEEK]


def test_an_engine_that_loaded_a_lower_ceiling_reports_the_clamp_once(
        caplog, install_ceiling, monkeypatch):
    """The one place a clamp can still bite: the run's config was raised while its engine ran, and
    the server (reading the new value) accepted a week. The engine caps at what it loaded — and says
    so, naming the restart, once per value rather than once per turn."""
    from types import SimpleNamespace

    from looplab.core.models import RunState
    from looplab.engine import width_settling
    from looplab.engine.width_settling import WidthSettlingMixin

    monkeypatch.setattr(width_settling, "_EVAL_TIMEOUT_CLAMPS_REPORTED", set())
    eng = SimpleNamespace(_speculation_gate_calibration=False, max_seconds=None,
                          max_eval_seconds=None, timeout=30.0, _eval_timeout_override=None)
    st = RunState()
    st.budget_overrides = {"eval_timeout": WEEK}
    with caplog.at_level(logging.WARNING, logger="looplab.engine.width_settling"):
        WidthSettlingMixin._apply_control_overrides(eng, st)
        WidthSettlingMixin._apply_control_overrides(eng, st)
    assert eng._eval_timeout_override == DAY
    warned = [r.getMessage() for r in caplog.records if "launch ceiling" in r.getMessage()]
    assert len(warned) == 1, warned
    assert "604800" in warned[0] and "86400" in warned[0]
    assert "max_launch_timeout_s" in warned[0] and "resume" in warned[0]

    caplog.clear()
    install_ceiling(WEEK)                                  # the restarted engine
    with caplog.at_level(logging.WARNING, logger="looplab.engine.width_settling"):
        WidthSettlingMixin._apply_control_overrides(eng, st)
    assert eng._eval_timeout_override == WEEK
    assert not [r for r in caplog.records if "launch ceiling" in r.getMessage()]
    assert math.isfinite(eng._eval_timeout_override)
