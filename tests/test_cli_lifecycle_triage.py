"""One prior-run lifecycle classification, shared by `run` / `resume` / `finalize` (doc 25 CT-03).

Deciding which lifecycle event — if any — a command may append before re-entering the loop is
REPLAY-CRITICAL, and it was implemented three times: `run` and `resume` each carried the same
four-rung ladder inline with byte-identical echo strings for the first two rungs, and `finalize` a
third variant of the same predicates. A fix applied to one copy and not the others silently gives
the entry points different lifecycles.

`classify_prior_run` is now the single answer. This file pins the ladder ORDER (which is the
contract, not an implementation detail), the surface differences that legitimately remain, and that
no command re-derives the predicates it replaced.

It also covers the CT-02 extractions from `run()`: the maintainer-only speculation-calibration lane
and the snapshot-publication transaction, both of which most readers of `run` never need.
"""
from __future__ import annotations

import ast
import inspect
import json

import pytest

from looplab.cli import run_cmds
from looplab.cli.run_cmds import (
    WRAP_UP_NOTICE, announce_wrap_up, classify_prior_run, terminal_projection_incomplete,
)


class _Prior:
    """The handful of folded fields the ladder reads. A stub, so the ORDER is what is under test."""

    def __init__(self, *, finished=False, paused=False, stop_requested=False,
                 stop_reason=None, pending=False):
        self.finished = finished
        self.paused = paused
        self.stop_requested = stop_requested
        self.stop_reason = stop_reason
        self._pending = pending

    def finalization_pending(self):
        return self._pending


# ------------------------------------------------------------------ the ladder

def test_a_quiet_run_dir_is_live():
    assert classify_prior_run(_Prior(), []) == "live"


def test_an_incomplete_terminal_projection_outranks_every_other_state():
    """The wrap-up already on disk owns the boundary. If a finished/paused/stop-requested run could
    outrank it, a command would lift a run whose terminal projection was still mid-flight."""
    for other in (
        _Prior(pending=True),
        _Prior(pending=True, finished=True),
        _Prior(pending=True, paused=True),
        _Prior(pending=True, stop_requested=True),
        _Prior(pending=True, finished=True, stop_requested=True, stop_reason="error"),
    ):
        assert classify_prior_run(other, []) == "finalization_pending"


def test_a_pending_finalize_outranks_a_plain_finish_and_is_never_lifted():
    """A stop newer than the finish, or a finish whose reason is `error`, must be RESPECTED: the UI's
    finalize path spawns `resume`, and lifting there would restart a run it asked to wrap up."""
    assert classify_prior_run(_Prior(stop_requested=True), []) == "pending_finalize"
    assert classify_prior_run(
        _Prior(stop_requested=True, finished=True, stop_reason="error"), []) == "pending_finalize"
    # ...but a clean stop that already produced its finish is just a finished run.
    assert classify_prior_run(
        _Prior(stop_requested=True, finished=True, stop_reason="budget"), []) == "finished"


def test_finished_outranks_paused():
    """Both are liftable, but they are lifted DIFFERENTLY by `run` (reopen vs resume), so a run that
    is both must resolve to exactly one."""
    assert classify_prior_run(_Prior(finished=True, paused=True), []) == "finished"
    assert classify_prior_run(_Prior(paused=True), []) == "paused"


def test_the_scope_half_of_the_predicate_is_read_from_the_events():
    """`finalization_pending()` is only half of it — an incomplete SCOPED checklist lives in the
    event list, and a classifier that ignored it would call a mid-checklist run `live`."""
    class _Scope:
        pass

    calls = []

    def _incomplete(events):
        calls.append(events)
        return _Scope() if events else None

    original = run_cmds.incomplete_finalize_scope
    run_cmds.incomplete_finalize_scope = _incomplete
    try:
        assert terminal_projection_incomplete(_Prior(), ["an event"]) is True
        assert terminal_projection_incomplete(_Prior(), []) is False
        assert classify_prior_run(_Prior(), ["an event"]) == "finalization_pending"
    finally:
        run_cmds.incomplete_finalize_scope = original
    assert calls, "the scope check must actually consult the events it was handed"


# ------------------------------------------------------------------ the act-on-it helper

def test_announce_wrap_up_reports_whether_the_run_may_be_lifted(capsys):
    for kind in ("finalization_pending", "pending_finalize"):
        assert announce_wrap_up(kind) is True
        assert WRAP_UP_NOTICE[kind] in capsys.readouterr().out
    for kind in ("finished", "paused", "live"):
        assert announce_wrap_up(kind) is False
        assert capsys.readouterr().out == ""


def test_the_two_shared_notices_are_owned_in_one_place():
    """They were byte-identical in two commands. If a command spells one again, the mapping here has
    stopped being the source and the strings can drift apart unnoticed."""
    import re

    # Join Python's implicit adjacent-literal concatenation first. Counting the raw source would let
    # a re-spelled notice hide simply by being wrapped across two lines, which is how a long string
    # is written back in practice — the guard has to see the value, not the formatting.
    source = re.sub(r'"\s*\n\s*"', "", inspect.getsource(run_cmds))
    for notice in WRAP_UP_NOTICE.values():
        # once in the mapping, and nowhere else
        assert source.count(notice) == 1, f"{notice!r} is spelled more than once"


@pytest.mark.parametrize("command", ["run", "resume"])
def test_no_command_re_derives_the_ladder_it_replaced(command):
    source = inspect.getsource(getattr(run_cmds, command))
    assert "classify_prior_run(" in source
    assert "incomplete_finalize_scope(prior_events)" not in source, (
        f"{command} re-derives the scope half of the predicate the classifier owns")
    assert "prior.stop_requested and (" not in source, (
        f"{command} re-derives the pending-finalize rung")


def test_finalize_uses_the_same_predicate_rather_than_a_third_variant():
    source = inspect.getsource(run_cmds.finalize)
    assert "terminal_projection_incomplete(" in source
    assert "before.finalization_pending()" not in source
    assert "current.finalization_pending()" not in source


def test_the_surface_differences_that_legitimately_remain_are_still_there():
    """The classification is shared; the ACTION is not. `run` reopens a finished dir so the loop
    processes a new budget, while `resume` appends the universal resume event. Collapsing those
    would be the wrong kind of deduplication, so they are pinned as intentionally different."""
    run_source = inspect.getsource(run_cmds.run)
    resume_source = inspect.getsource(run_cmds.resume)
    assert "EV_RUN_REOPENED" in run_source and "EV_RUN_REOPENED" not in resume_source
    assert 'prior_kind == "finished"' in run_source and 'prior_kind == "paused"' in run_source
    assert 'prior_kind in ("finished", "paused")' in resume_source


# ------------------------------------------------------------------ CT-02: run() got smaller

def test_run_no_longer_inlines_the_maintainer_only_calibration_lane():
    """~100 lines of one-purpose validation that most readers of `run` never need. Each block is now
    a named helper, so the command reads as the pipeline it is."""
    source = inspect.getsource(run_cmds.run)
    for helper in ("_pin_offline_speculation_profile(", "_calibration_envelope_task_dict(",
                   "_assert_calibration_dir_is_fresh(", "_publish_run_snapshots("):
        assert helper in source, f"{helper} is not called from run()"
    for inlined in ("card_driven_selection must be true", "must be in 1..64",
                    "run_config_write_lock(", "effective_gpu_inventory"):
        assert inlined not in source, f"run() still inlines {inlined!r}"


def test_run_is_no_longer_a_347_line_command():
    """The finding's own measure, applied to the BODY.

    `run`'s typer signature is ~65 lines of frozen `--flag` surface and its docstring another ~20;
    neither is work the command does, and counting them would let a real regression hide behind
    "well, most of it is options". The body — where the seven jobs lived — is what is bounded here.
    """
    import textwrap

    source = textwrap.dedent(inspect.getsource(run_cmds.run))
    function = ast.parse(source).body[0]
    body = function.body[1:] if isinstance(function.body[0], ast.Expr) else function.body
    lines = body[-1].end_lineno - body[0].lineno + 1
    assert lines < 230, f"run()'s body is back to {lines} lines"


def test_the_calibration_envelope_reports_every_violation_at_once(tmp_path):
    """An operator fixing a restricted envelope one rejection per invocation is the reason these are
    collected rather than raised on the first failure."""
    import typer

    from looplab.adapters.toytask import ToyTask
    from looplab.core.config import Settings

    settings = Settings(backend="llm", policy="asha", max_nodes=999)
    with pytest.raises(typer.BadParameter) as excinfo:
        run_cmds._calibration_envelope_task_dict(object(), settings)
    message = str(excinfo.value)
    for clause in ("task must be the exact offline quadratic ToyTask", "backend must be toy",
                   "policy must be greedy", "max_nodes must be in 1..64"):
        assert clause in message, f"{clause!r} missing; the envelope reported only the first failure"
    assert ToyTask is not None      # the helper's own import path stays reachable


def test_publishing_snapshots_writes_both_under_one_lock(tmp_path):
    """Both files belong to ONE reset-aware transaction: a `run` must not refresh either while a
    Replay has archived generation A but not proven generation B."""
    from looplab.core.config import Settings

    held = []
    original = run_cmds.run_config_write_lock

    class _Lock:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            held.append("open")
            return self

        def __exit__(self, *exc):
            held.append("close")
            return False

    run_cmds.run_config_write_lock = _Lock
    try:
        run_cmds._publish_run_snapshots(tmp_path, {"kind": "quadratic"}, Settings())
    finally:
        run_cmds.run_config_write_lock = original

    assert held == ["open", "close"], "the snapshots were not published inside one transaction"
    assert json.loads((tmp_path / "task.snapshot.json").read_text())["kind"] == "quadratic"
    assert json.loads((tmp_path / "config.snapshot.json").read_text())


def test_a_task_snapshot_that_cannot_be_written_does_not_lose_the_config_one(tmp_path):
    """The `except OSError: pass` around the task snapshot is deliberate — a read-only or full disk
    must not abort a run whose config snapshot already landed. Pinned so it is not "cleaned up"."""
    from looplab.core.config import Settings

    (tmp_path / "task.snapshot.json").mkdir()        # a directory: the write must fail, not raise out
    run_cmds._publish_run_snapshots(tmp_path, {"kind": "quadratic"}, Settings())
    assert (tmp_path / "config.snapshot.json").exists()


# ------------------------------------------------------------------ CT-15: one Foresight ctor

def test_the_foresight_panel_is_constructed_in_exactly_one_place():
    """Five getattr-defaulted kwargs written out in two branches is the shape that grows a sixth in
    only one of them, silently changing unified-vs-plain behaviour (doc 25 CT-15)."""
    from looplab import cli

    source = inspect.getsource(cli)
    assert source.count("ForesightPanelResearcher(") == 1
    assert source.count("_wrap_with_foresight_panel(") == 3        # def + two call sites


def test_the_two_foresight_guards_stopped_differing_by_one_clause():
    """The non-unified branch tested `backend == "llm"` and the unified one did not — it gets that
    for free from `_unified`. Written twice with a difference, they read as different checks."""
    from looplab import cli

    source = inspect.getsource(cli._engine)
    assert source.count("_foresight_panel_applies(settings, researcher)") == 2
    assert 'getattr(settings, "foresight_panel", 2) > 1' not in source, (
        "_engine still re-derives the guard the helper owns")

    predicate = inspect.getsource(cli._foresight_panel_applies)
    assert 'settings.backend == "llm"' in predicate, (
        "folding the clause in is what removes the asymmetry; dropping it would widen the guard")


def test_the_foresight_guard_still_yields_to_an_explicit_numeric_panel():
    """Behaviour, not shape: `researcher_panel > 1` is an explicit opt-in to the k-NN panel and must
    never be silently overridden by the foresight default."""
    from looplab import cli
    from looplab.core.config import Settings

    class _WithClient:
        client = object()

    class _NoClient:
        client = None

    base = Settings(backend="llm")
    assert cli._foresight_panel_applies(base, _WithClient()) is True
    assert cli._foresight_panel_applies(base, _NoClient()) is False
    assert cli._foresight_panel_applies(Settings(backend="toy"), _WithClient()) is False
    assert cli._foresight_panel_applies(
        Settings(backend="llm", researcher_panel=4), _WithClient()) is False
    assert cli._foresight_panel_applies(
        Settings(backend="llm", foresight=False), _WithClient()) is False
    assert cli._foresight_panel_applies(
        Settings(backend="llm", foresight_panel=1), _WithClient()) is False


def test_source_has_no_stray_ast_import_problem():
    """Cheap tripwire: this module parses the CLI package's sources in several tests above, so a
    syntax-level breakage in the extraction shows up here rather than as five confusing failures."""
    from looplab import cli

    for module in (cli, run_cmds):
        ast.parse(inspect.getsource(module))
