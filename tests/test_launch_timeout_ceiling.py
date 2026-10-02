"""The per-launch wall-clock ceiling: `max_launch_timeout_s` (2026-09-27) and, since 2026-10-02, the
rule that an OPERATOR's own wall clock is never cut by it.

Every subprocess deadline is clamped at the launch (`runtime/sandbox.py::finite_timeout`), and the
ceiling was the literal `sandbox.MAX_TIMEOUT_S = 24 * 3600`. 2026-09-27 made it a per-run setting
(`Settings.max_launch_timeout_s`, range in `core/numeric.py`). That still left the operator's own
numbers under it: on 2026-10-01 (`minionerec-backbones-v11`) the task declared `eval.timeout = 100800`
(28 h), the setting was the 24 h default, nothing refused or warned at submit or start, and a 24-hour
training was SIGKILLed at 24 h during its final eval.

The ceiling installed at engine start is now `min(7 days, max(setting, the largest operator-declared
wall clock))` (`cli/__init__.py::_engine` -> `sandbox.install_launch_timeout_ceiling`), a live
`budget_extend{eval_timeout}` raises it again (`engine/width_settling.py::lift_launch_ceiling`), and
the 7-day `LAUNCH_TIMEOUT_LIMIT_S` is the one absolute bound: refused at submit
(`EvalSpec._wall_clocks_within_launch_limit`) and at the server, clamped loudly on a recorded run.

What is DRIVEN here, not pinned:
  * the default is the historical 24 h everywhere a run declares nothing longer;
  * one range for the setting: the Settings field and the runtime setter refuse the same values;
  * a configured week reaches a REAL launch (`run_argv` hands the child 604800 s);
  * the incident: a task declaring 100800 s under DEFAULT settings installs 100800 and its launch is
    given 100800 — through `_engine` itself, before the roles, and end to end through an engine;
  * the week is absolute: refused at submit for every declaring field, clamped with a WARNING on the
    resume of a run that recorded more;
  * the server accepts up to a week on ANY run (no snapshot, no process global read) and refuses one
    second more, for `eval_timeout` and `timeout`;
  * a live extension above the engine's ceiling RAISES it (upward only), once-logged, and is the next
    evaluation's deadline on a default-configured run; above the week it is clamped and reported;
  * agent-originated timeouts are unchanged: a Researcher override is still clamped by
    `max_eval_timeout`, and a Developer manifest stage is admitted as before and bounded by the
    (possibly raised) ceiling.
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
from looplab.runtime.command_eval import (eval_spec_time_budget, eval_timeout_override,
                                          operator_declared_timeouts)
from looplab.runtime.sandbox import (MAX_TIMEOUT_S, finite_timeout, install_launch_timeout_ceiling,
                                     launch_timeout_ceiling, raise_launch_timeout_ceiling)

WEEK = 7 * 24 * 3600.0
DAY = 24 * 3600.0
INCIDENT = 100800.0          # minionerec-backbones-v11's declared eval.timeout: 28 h


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


def test_the_server_accepts_up_to_a_week_on_any_run_and_refuses_one_second_more(tmp_path):
    """The bound is the absolute limit, not the run's setting: the run's engine raises its own ceiling
    to whatever the operator gives it, so a day-configured run accepts a week too."""
    rd = _seeded(tmp_path)
    _snapshot(rd)                                          # max_launch_timeout_s at its 24 h default
    assert _normalize(tmp_path, {"eval_timeout": DAY + 1})["eval_timeout"] == DAY + 1
    assert _normalize(tmp_path, {"eval_timeout": INCIDENT})["eval_timeout"] == INCIDENT
    assert _normalize(tmp_path, {"eval_timeout": "172800"})["eval_timeout"] == 2 * DAY
    assert _normalize(tmp_path, {"eval_timeout": WEEK})["eval_timeout"] == WEEK
    detail = _refusal(tmp_path, {"eval_timeout": WEEK + 1})
    assert "(0, 604800]" in detail and "7 days" in detail
    # the shape refusals name the same bound
    assert "(0, 604800]" in _refusal(tmp_path, {"eval_timeout": float("nan")})


def test_the_script_path_timeout_has_the_same_absolute_bound(tmp_path):
    """`budget_extend{timeout}` is a per-eval wall clock too, lifted the same way by the engine — so
    above the week it would be accepted and then cut, and is refused instead."""
    _seeded(tmp_path)
    assert _normalize(tmp_path, {"timeout": WEEK})["timeout"] == WEEK
    detail = _refusal(tmp_path, {"timeout": WEEK + 1})
    assert "timeout" in detail and "604800" in detail


@pytest.mark.parametrize("damage", [
    None,                                                  # no snapshot at all
    "{not json",
    json.dumps({"max_launch_timeout_s": 10 * WEEK}),       # hand-edited past the limit
    json.dumps(["not", "an", "object"]),
])
def test_the_server_reads_no_snapshot_and_no_process_global(tmp_path, damage, monkeypatch):
    """The server installs no ceiling (`runtime/sandbox.py`), so it must not depend on one: whatever
    the run's snapshot holds — or whatever this process's global happens to be — the bound is the
    constant week. (Before 2026-10-02 it read the run's snapshot and failed closed to a day.)"""
    rd = _seeded(tmp_path)
    if damage is not None:
        (rd / "config.snapshot.json").write_text(damage, encoding="utf-8")
    monkeypatch.setattr(sandbox, "_launch_timeout_ceiling_s", 1.0)   # a global nobody installed
    assert _normalize(tmp_path, {"eval_timeout": WEEK})["eval_timeout"] == WEEK
    assert "(0, 604800]" in _refusal(tmp_path, {"eval_timeout": WEEK + 1})


def test_the_operators_http_path_extends_to_a_week_without_touching_the_config(tmp_path):
    """What the operator does, over HTTP: a week is accepted on a DEFAULT run and lands; one second
    more is a rejected command that appends nothing. The per-run config editor still holds the
    setting's own range."""
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

    refused = post_command(client, "budget_extend", {"eval_timeout": WEEK + 1}, key="too-long").json()
    assert refused["status"] == "rejected"
    assert refused["error"]["code"] == "invalid_command"
    assert "7 days" in refused["error"]["message"]
    assert extends() == []

    accepted = post_command(client, "budget_extend", {"eval_timeout": WEEK}, key="week")
    assert accepted.status_code in (200, 201, 202), accepted.text
    command_terminal(client, accepted.json())
    assert [row["eval_timeout"] for row in extends()] == [WEEK]
    assert fold(EventStore(rd / "events.jsonl").read_all()).budget_overrides["eval_timeout"] == WEEK

    # the editor refuses what the engine would: the setting's own range, over HTTP
    too_far = _run_config_put(client, "demo", {"settings": {"max_launch_timeout_s": WEEK + 1}})
    assert too_far.status_code == 422, too_far.text


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


def test_a_library_engine_that_installs_nothing_keeps_the_historical_cut(tmp_path):
    """The install is `_engine`'s (the CLI/UI funnel). A bare library `Engine` with nothing installed
    still clamps at the 24 h default, byte for byte — the eval prints its OWN leash as its metric
    (`LOOPLAB_EVAL_TIMEOUT_S`), so each number below is the deadline the process really had."""
    from tests.test_live_eval_timeout import _metrics

    _task_, _rd, run = _run_task(tmp_path, "default", 2 * DAY)
    assert _metrics(run()) == [DAY]


def test_the_incident_a_28_hour_task_under_default_settings_is_given_28_hours(tmp_path):
    """`minionerec-backbones-v11`, end to end: `eval.timeout = 100800`, `max_launch_timeout_s` left at
    its 24 h default. The engine-start install (what `_engine` runs for this task and these settings)
    lifts the ceiling to 100800, and the launch is given 100800 — not SIGKILLed at 86400."""
    from tests.test_live_eval_timeout import _metrics

    task, _rd, run = _run_task(tmp_path, "incident", INCIDENT)
    settings = Settings()
    assert settings.max_launch_timeout_s == DAY
    assert install_launch_timeout_ceiling(
        settings.max_launch_timeout_s, operator_declared_timeouts(task.eval_spec())) == INCIDENT
    assert finite_timeout(INCIDENT) == INCIDENT
    assert _metrics(run()) == [INCIDENT]


def test_the_cli_engine_builder_lifts_the_ceiling_to_the_tasks_declared_timeout(
        tmp_path, monkeypatch, caplog):
    """Through `_engine` itself, with a repo task and DEFAULT settings: the ceiling the roles are
    built under is the task's 100800, and the raise is said ONCE, naming the value and the field."""
    import looplab.cli as cli
    from looplab.adapters.repo_task import EvalSpec, RepoTask
    from looplab.core import tracing

    monkeypatch.setattr(tracing, "_CAPTURE_LLM_IO", tracing._CAPTURE_LLM_IO)
    repo = tmp_path / "repo"
    repo.mkdir()
    task = RepoTask(id="t", direction="max", editable_path=str(repo), edit_surface=["*.py"],
                    eval=EvalSpec(command=[sys.executable, "run.py"],
                                  metric={"kind": "stdout_json", "key": "metric"},
                                  timeout=INCIDENT))
    seen = []
    real = cli.make_roles

    def recording(task, settings, run_dir):
        seen.append(launch_timeout_ceiling())
        return real(task, settings, run_dir)

    monkeypatch.setattr(cli, "make_roles", recording)
    with caplog.at_level(logging.WARNING, logger="looplab.runtime.sandbox"):
        cli._engine(tmp_path / "run", task, Settings(backend="toy"), None)
    assert seen and seen[0] == INCIDENT
    assert launch_timeout_ceiling() == INCIDENT
    raised = [r.getMessage() for r in caplog.records if "launch ceiling raised" in r.getMessage()]
    assert len(raised) == 1, raised
    assert "100800" in raised[0] and "eval.timeout" in raised[0] and "86400" in raised[0]
    # a stage that declares the operator's number is not clamped
    assert command_eval.build_command(task.eval_spec(), {}, None)[1] == INCIDENT


def test_the_operators_week_long_extension_is_the_next_evaluations_deadline(tmp_path):
    """End to end on a DEFAULT-configured run (no install, no config edit, no restart before the
    extension): the server admits `eval_timeout = 604800`, the fold keeps it, and the evaluation
    after it — on a fresh engine, i.e. a resume — is launched with a week, because the engine lifted
    its own ceiling from the log at re-entry."""
    from tests.test_live_eval_timeout import _metrics

    _task_, rd, run = _run_task(tmp_path, "run", 4 * 3600.0)
    assert _metrics(run()) == [4 * 3600.0]
    assert launch_timeout_ceiling() == DAY
    _snapshot(rd)                                          # what `looplab run` recorded: defaults
    data = _normalize(tmp_path, {"eval_timeout": WEEK})
    store = EventStore(rd / "events.jsonl")
    store.append("budget_extend", data)
    store.append("budget_extend", {"add_nodes": 1})
    store.append("run_reopened", {})
    assert _metrics(run()) == [4 * 3600.0, WEEK]
    assert launch_timeout_ceiling() == WEEK


def test_a_live_extension_above_the_ceiling_raises_it_once_and_never_lowers_it(caplog, monkeypatch):
    """The live half, at the seam every turn goes through: `_apply_control_overrides` lifts the
    ceiling to the operator's value BEFORE reading the override through it, so nothing is clamped —
    and says so once. A later, smaller value does not lower the bound."""
    from types import SimpleNamespace

    from looplab.core.models import RunState
    from looplab.engine import width_settling
    from looplab.engine.width_settling import WidthSettlingMixin

    monkeypatch.setattr(width_settling, "_EVAL_TIMEOUT_CLAMPS_REPORTED", set())
    eng = SimpleNamespace(_speculation_gate_calibration=False, max_seconds=None,
                          max_eval_seconds=None, timeout=30.0, _eval_timeout_override=None)
    st = RunState()
    st.budget_overrides = {"eval_timeout": INCIDENT}
    assert launch_timeout_ceiling() == DAY
    with caplog.at_level(logging.WARNING, logger="looplab.engine.width_settling"):
        WidthSettlingMixin._apply_control_overrides(eng, st)
        WidthSettlingMixin._apply_control_overrides(eng, st)
    assert eng._eval_timeout_override == INCIDENT, "not clamped to the 24 h it started under"
    assert launch_timeout_ceiling() == INCIDENT
    raised = [r.getMessage() for r in caplog.records if "raised this engine" in r.getMessage()]
    assert len(raised) == 1, raised
    assert "100800" in raised[0] and "86400" in raised[0]
    assert not [r for r in caplog.records if "absolute launch limit" in r.getMessage()]

    st.budget_overrides = {"eval_timeout": 3600.0}           # the operator shortens the budget
    WidthSettlingMixin._apply_control_overrides(eng, st)
    assert eng._eval_timeout_override == 3600.0
    assert launch_timeout_ceiling() == INCIDENT, "upward only: the bound is not cut under it"

    # the script-path lever lifts it the same way
    st.budget_overrides = {"timeout": 2 * INCIDENT}
    WidthSettlingMixin._apply_control_overrides(eng, st)
    assert eng.timeout == 2 * INCIDENT and launch_timeout_ceiling() == 2 * INCIDENT


def test_a_logged_extension_above_the_week_is_clamped_and_reported_once(caplog, monkeypatch):
    """The server refuses more than a week, so this reaches the engine only past it (a hand-edited
    log, an older build). The week holds, and the clamp is said once per value."""
    from types import SimpleNamespace

    from looplab.core.models import RunState
    from looplab.engine import width_settling
    from looplab.engine.width_settling import WidthSettlingMixin

    monkeypatch.setattr(width_settling, "_EVAL_TIMEOUT_CLAMPS_REPORTED", set())
    eng = SimpleNamespace(_speculation_gate_calibration=False, max_seconds=None,
                          max_eval_seconds=None, timeout=30.0, _eval_timeout_override=None)
    st = RunState()
    st.budget_overrides = {"eval_timeout": 10 * WEEK}
    with caplog.at_level(logging.WARNING, logger="looplab.engine.width_settling"):
        WidthSettlingMixin._apply_control_overrides(eng, st)
        WidthSettlingMixin._apply_control_overrides(eng, st)
    assert eng._eval_timeout_override == WEEK
    assert launch_timeout_ceiling() == WEEK
    assert math.isfinite(eng._eval_timeout_override)
    clamped = [r.getMessage() for r in caplog.records if "absolute launch limit" in r.getMessage()]
    assert len(clamped) == 1, clamped
    assert "6048000" in clamped[0] and "604800" in clamped[0]


# ------------------------------------------------------------- the install and the raise, directly

def test_raise_is_upward_only_bounded_by_the_week_and_total_over_junk():
    assert launch_timeout_ceiling() == DAY
    for junk in (None, "abc", True, float("nan"), 0, -5, DAY - 1, [1]):
        assert raise_launch_timeout_ceiling(junk) == DAY
    assert raise_launch_timeout_ceiling(INCIDENT) == INCIDENT
    assert raise_launch_timeout_ceiling(DAY) == INCIDENT, "never lowered"
    assert raise_launch_timeout_ceiling("172800") == 2 * DAY
    assert raise_launch_timeout_ceiling(float("inf")) == WEEK
    assert raise_launch_timeout_ceiling(10 * WEEK) == WEEK


def test_the_install_is_this_runs_own_and_never_carries_over(caplog):
    """`set` first, then lift: a second engine in the same process (an embedder, a test) installs
    ITS run's value, and an install that lifts nothing says nothing."""
    assert install_launch_timeout_ceiling(DAY, [("eval.timeout", INCIDENT)]) == INCIDENT
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="looplab.runtime.sandbox"):
        assert install_launch_timeout_ceiling(DAY, [("eval.timeout", 3600.0)]) == DAY
        assert install_launch_timeout_ceiling(2 * DAY, [("eval.timeout", INCIDENT)]) == 2 * DAY
        assert install_launch_timeout_ceiling(DAY, []) == DAY
    assert not caplog.records
    # the setting's own range is still refused, not narrowed
    with pytest.raises(ConfigRefusal):
        install_launch_timeout_ceiling(WEEK + 1, [])


def test_a_recorded_non_finite_declaration_neither_lifts_nor_claims_a_clamp(caplog):
    """A grandfathered `eval.timeout: inf` never reached a launch as itself: `finite_timeout` runs it
    at the caller's fallback. So it must not lift the ceiling to the week (loosening the bound on
    every agent stage) nor be reported as "clamped to 604800" — neither would be true."""
    with caplog.at_level(logging.WARNING, logger="looplab.runtime.sandbox"):
        assert install_launch_timeout_ceiling(
            DAY, operator_declared_timeouts({"command": ["x"], "timeout": float("inf")})) == DAY
    assert not caplog.records
    assert command_eval.build_command({"command": ["x"], "timeout": float("inf")})[1] == 600.0


def test_every_operator_declared_wall_clock_is_enumerated_unclamped():
    """THE walk both the install and the submit gate read. Unclamped: read through the ceiling it is
    meant to lift, it could only reproduce the old one."""
    spec = {"command": ["x"], "timeout": INCIDENT,
            "profiles": {"smoke": {"timeout": 60}, "full": {"timeout": 2 * DAY}},
            "stages": [{"name": "prep", "command": ["a"], "timeout": 600},
                       {"name": "train", "command": ["b"], "timeout": 3 * DAY},
                       {"name": "score", "command": ["c"]}],
            "canary": {"timeout": 900.0}, "host_scorer": {"command": ["/s"], "timeout": 4 * DAY},
            "holdout_scorer": {"command": ["/h"], "timeout": 5 * DAY},
            "setup_timeout": 600.0, "run_setup_timeout": 6 * DAY}
    got = dict(operator_declared_timeouts(spec))
    assert got == {
        "eval.timeout": INCIDENT, "eval.profiles['smoke'].timeout": 60.0,
        "eval.profiles['full'].timeout": 2 * DAY, "eval.stages['prep'].timeout": 600.0,
        "eval.stages['train'].timeout": 3 * DAY, "eval.canary.timeout": 900.0,
        "eval.host_scorer.timeout": 4 * DAY, "eval.holdout_scorer.timeout": 5 * DAY,
        "eval.setup_timeout": 600.0, "eval.run_setup_timeout": 6 * DAY}
    assert launch_timeout_ceiling() == DAY                   # nothing read through the ceiling
    # junk is skipped; +inf is KEPT (it is a declaration above every limit, not junk)
    assert operator_declared_timeouts({"timeout": float("inf"), "profiles": {"a": {"timeout": "x"}},
                                       "setup_timeout": True}) == [("eval.timeout", float("inf"))]
    assert operator_declared_timeouts(None) == [] and operator_declared_timeouts({}) == []
    # the budget walk is shared with eval_spec_time_budget, which still reads through the ceiling
    assert eval_spec_time_budget(spec) == DAY
    install_launch_timeout_ceiling(DAY, operator_declared_timeouts(spec))
    assert launch_timeout_ceiling() == 6 * DAY
    assert eval_spec_time_budget(spec) == 2 * DAY


# ------------------------------------------------------------------- the week at submit and resume

def _repo_task_dict(tmp_path, **eval_kw) -> dict:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return {"kind": "repo", "id": "t", "goal": "g", "direction": "max",
            "editable_path": str(repo), "edit_surface": ["*.py"],
            "eval": {"command": ["python", "run.py"], "timeout": 600, **eval_kw}}


@pytest.mark.parametrize("field, eval_kw", [
    ("eval.timeout", {"timeout": 8 * DAY}),
    ("eval.timeout", {"timeout": float("inf")}),
    ("eval.profiles['full'].timeout", {"profiles": {"full": {"timeout": 8 * DAY}}}),
    ("eval.stages['train'].timeout", {"stages": [{"name": "train", "command": ["python", "t.py"],
                                                  "timeout": 8 * DAY}]}),
    ("eval.canary.timeout", {"canary": {"timeout": 8 * DAY}}),
    ("eval.run_setup_timeout", {"run_setup_timeout": 8 * DAY}),
])
def test_a_task_declaring_more_than_a_week_is_refused_at_submit(tmp_path, field, eval_kw):
    from looplab.adapters.tasks import validate_task

    with pytest.raises(ValueError) as refused:
        validate_task(_repo_task_dict(tmp_path, **eval_kw))
    assert field in str(refused.value)
    assert "7-day" in str(refused.value) and "604800" in str(refused.value)


def test_every_offending_field_is_named_at_once_and_a_week_exactly_is_admitted(tmp_path):
    from looplab.adapters.repo_task import EvalSpec
    from looplab.adapters.tasks import validate_task

    with pytest.raises(ValueError) as refused:
        EvalSpec(command=["x"], timeout=8 * DAY, profiles={"full": {"timeout": 9 * DAY}},
                 host_scorer={"command": [str(tmp_path / "s.py")], "timeout": 10 * DAY})
    msg = str(refused.value)
    assert "eval.timeout" in msg and "eval.profiles['full'].timeout" in msg
    assert "eval.host_scorer.timeout" in msg
    task = validate_task(_repo_task_dict(
        tmp_path, timeout=WEEK, profiles={"full": {"timeout": WEEK}}, canary={"timeout": WEEK},
        stages=[{"name": "train", "command": ["python", "t.py"], "timeout": WEEK}]))
    assert max(v for _l, v in operator_declared_timeouts(task.eval_spec())) == WEEK


def test_a_recorded_run_holding_more_than_a_week_resumes_clamped_with_a_warning(
        tmp_path, caplog):
    """Not refused on resume — a recorded run must stay resumable (`_grandfathered`) — but never
    silently cut either: the install clamps to the week and names the field."""
    from looplab.adapters.tasks import load_task

    snap = tmp_path / "task.snapshot.json"
    snap.write_text(json.dumps(_repo_task_dict(tmp_path, timeout=8 * DAY)), encoding="utf-8")
    with pytest.raises(ValueError):
        load_task(snap)                                      # a fresh submit of the same file
    task = load_task(snap, existing_run=True)                # the resume path: accepted
    assert task.eval_spec()["timeout"] == 8 * DAY
    with caplog.at_level(logging.WARNING, logger="looplab.runtime.sandbox"):
        installed = install_launch_timeout_ceiling(
            Settings().max_launch_timeout_s, operator_declared_timeouts(task.eval_spec()))
    assert installed == WEEK
    over = [r.getMessage() for r in caplog.records if "absolute launch limit" in r.getMessage()]
    assert len(over) == 1, over
    assert "eval.timeout = 691200 s" in over[0] and "CLAMPED to 604800" in over[0]
    assert command_eval.build_command(task.eval_spec(), {}, None)[1] == WEEK


# ----------------------------------------------------------------- agent-originated: unchanged

def test_a_researcher_override_is_still_clamped_by_max_eval_timeout():
    """The operator's 28 h lifts the LAUNCH ceiling, not the clamp an AGENT's request meets."""
    from types import SimpleNamespace

    from looplab.engine.shared import effective_researcher_eval_timeout

    install_launch_timeout_ceiling(DAY, [("timeout (the run's per-eval setting)", INCIDENT)])
    assert launch_timeout_ceiling() == INCIDENT
    eng = SimpleNamespace(_eval_spec={}, _eval_timeout_override=None, max_eval_timeout=3600.0,
                          timeout=INCIDENT, _agent_may=lambda role, setting: True)
    assert effective_researcher_eval_timeout(eng, SimpleNamespace(eval_timeout=INCIDENT)) == 3600.0
    assert effective_researcher_eval_timeout(eng, SimpleNamespace(eval_timeout=600.0)) == 600.0


def test_a_developer_manifest_stage_is_admitted_as_before_and_bounded_by_the_raised_ceiling():
    """A Developer's `looplab_stages.json` is not an operator declaration: the 7-day refusal is
    `EvalSpec`'s, never `validate_stages`' (which the Developer's manifest shares), so an 8-day
    manifest stage is still admitted and recorded — and at the launch it meets the ceiling, now the
    operator's larger number when they declared one."""
    clean, err = command_eval.validate_stages(
        [{"name": "train", "command": ["python", "train.py"], "timeout": 8 * DAY}],
        reserved=("score",))
    assert err is None and clean[0]["timeout"] == 8 * DAY
    assert finite_timeout(8 * DAY) == DAY                     # default: the setting bounds it
    install_launch_timeout_ceiling(DAY, [("eval.timeout", INCIDENT)])
    assert finite_timeout(8 * DAY) == INCIDENT                # raised: the operator's number does
