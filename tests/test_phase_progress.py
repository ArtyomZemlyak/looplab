"""The `phase_progress` beacon: a build and a resume must not be a blank panel.

Tier 1 wherever possible (CLAUDE.md's guard-test ladder) — these drive a REAL engine over a REAL
event log and then read the log, rather than pinning source text. The properties that cost something
when they break are: the beacon fires at all on the path a shipped default actually takes; it does
NOT fire before the prologue's own `read_all`; and its (stage, phase) vocabulary cannot be typo'd.
"""
from __future__ import annotations

import json

import pytest

from looplab.events.types import (
    ALL_EVENT_TYPES,
    DIAGNOSTIC_EVENTS,
    EV_PHASE_PROGRESS,
    PROGRESS_PHASES,
    PROGRESS_STAGE_BUILD,
    PROGRESS_STAGE_RESUME,
    PROGRESS_STAGES,
    PROGRESS_STATUSES,
    assert_progress_phase,
)


def _beacons(run_dir):
    rows = [json.loads(line) for line in
            (run_dir / "events.jsonl").read_text().splitlines() if line.strip()]
    return rows, [r["data"] for r in rows if r.get("type") == EV_PHASE_PROGRESS]


# --------------------------------------------------------------------- the registry's own rules

def test_the_beacon_is_registered_and_fold_ignored():
    assert EV_PHASE_PROGRESS in ALL_EVENT_TYPES
    # It must never fold. It is emitted at a rate the fold has no reason to carry, it says nothing
    # about selection, and a resume must rebuild the same RunState from a log with these rows and
    # from one without them.
    assert EV_PHASE_PROGRESS in DIAGNOSTIC_EVENTS


def test_the_stage_phase_pair_is_closed_and_refuses_a_typo():
    # The defect this exists to prevent: a progress beacon has no reader that fails loudly. The fold
    # skips it and a UI keyed on an unknown phase renders nothing, so a typo'd phase ships as a
    # silently missing signal — which is the exact thing this whole change removes.
    assert set(PROGRESS_PHASES) == set(PROGRESS_STAGES)
    for stage, phases in PROGRESS_PHASES.items():
        assert phases, f"stage {stage} declares no phases"
        for phase in phases:
            for status in PROGRESS_STATUSES:
                assert_progress_phase(stage, phase, status)
    with pytest.raises(ValueError, match="unknown progress stage"):
        assert_progress_phase("buildd", "propose", "started")
    with pytest.raises(ValueError, match="unknown progress phase"):
        assert_progress_phase(PROGRESS_STAGE_BUILD, "implememt", "started")
    with pytest.raises(ValueError, match="unknown progress status"):
        assert_progress_phase(PROGRESS_STAGE_BUILD, "implement", "begun")


def test_the_diagnostic_fence_covers_it_so_a_beacon_cannot_discard_a_paid_proposal():
    """Invariant #1's real rule: not "does the fold read it?" but "does any reader key on position?".

    `_proposal_authority_seq` fences a paid proposal by comparing a max-seq for EQUALITY across the
    window in which `_prepare_node_idea` makes its call — and beacons are appended INSIDE that very
    window, by construction, since that call is what they bracket. The fence excludes
    DIAGNOSTIC_EVENTS wholesale, which is what makes their position immaterial. That is a property of
    the READER; if the exclusion were ever narrowed back to a named list, every build would discard
    the proposal it just paid for.
    """
    from looplab.engine.speculation import SpeculationMixin

    events = [
        type("E", (), {"type": EV_PHASE_PROGRESS, "seq": 99})(),
        type("E", (), {"type": "node_created", "seq": 5})(),
    ]
    assert SpeculationMixin._proposal_authority_seq(events) == 5


# ------------------------------------------------------------------- driven over a real engine

@pytest.fixture(scope="module")
def offline_run(tmp_path_factory):
    """One real offline run, reused: it is the only way to prove a beacon reaches the log on the
    path a shipped default actually takes rather than the one a hand-built call would take."""
    from looplab.cli import app
    from typer.testing import CliRunner

    out = tmp_path_factory.mktemp("phase-progress") / "run"
    result = CliRunner().invoke(app, [
        "run", "--no-genesis", "--kind", "quadratic", "--goal", "min (x-3)^2",
        "--direction", "min", "--backend", "toy", "--out", str(out),
    ])
    assert result.exit_code == 0, result.output
    return out


def test_a_real_build_narrates_its_phases(offline_run):
    rows, beacons = _beacons(offline_run)
    build = [b for b in beacons if b["stage"] == PROGRESS_STAGE_BUILD]
    assert build, "a real run emitted no build beacon at all"
    # The PROPOSAL specifically. It is the phase that used to be wholly invisible — it runs before
    # `node_building` is appended, so no marker and no node existed while it ran — and it is emitted
    # from `_consume_batch_proposal`, the funnel the shipped default width goes through. A beacon
    # only on the serial `_create_node_scoped` path would leave the default config exactly as blank
    # as before, which is how the first version of this change was wrong.
    assert any(b["phase"] == "propose" for b in build)
    for b in build:
        assert_progress_phase(b["stage"], b["phase"], b["status"])
    # Every started row is answered, so nothing on screen can tick upward forever.
    started = [b for b in build if b["status"] == "started"]
    finished = [b for b in build if b["status"] == "finished"]
    assert len(started) == len(finished)
    assert all("seconds" in b and "ok" in b for b in finished)


def test_a_fresh_run_emits_no_resume_beacon_and_keeps_the_calibration_setup_prefix(offline_run):
    """The one-stat re-entry gate, driven rather than asserted about.

    A `resume/read_log` started row on a fresh run would land in the prologue's own `read_all()`.
    That breaks two things that both fail silently: the speculation-calibration bootstrap refuses a
    run whose log is not exactly empty at start, and `speculation_quality._validate_calibration_setup`
    pins the log's FIRST FIVE events as an exact setup prefix.
    """
    rows, beacons = _beacons(offline_run)
    assert [b for b in beacons if b["stage"] == PROGRESS_STAGE_RESUME] == []
    assert [r["type"] for r in rows[:3]] == ["setup_started", "setup_step", "run_started"]


def test_a_resume_narrates_the_prologue_that_runs_before_anything_else_moves(offline_run, tmp_path):
    """A resume's blank wait is structural: the prologue happens before the loop's first turn, so no
    node, marker or pending count has moved and the run looks dead."""
    import shutil
    from looplab.cli import app
    from typer.testing import CliRunner

    resumed = tmp_path / "resumed"
    shutil.copytree(offline_run, resumed)
    (resumed / "engine.lock").unlink(missing_ok=True)
    result = CliRunner().invoke(app, [
        "run", "--no-genesis", "--kind", "quadratic", "--goal", "min (x-3)^2",
        "--direction", "min", "--backend", "toy", "--out", str(resumed), "--max-nodes", "8",
    ])
    assert result.exit_code == 0, result.output
    _rows, beacons = _beacons(resumed)
    resume = [b for b in beacons if b["stage"] == PROGRESS_STAGE_RESUME]
    assert {b["phase"] for b in resume} == {"read_log", "reconcile"}
    read_done = next(b for b in resume if b["phase"] == "read_log" and b["status"] == "finished")
    # The numbers are what make the beacon actionable rather than decorative: an operator staring at
    # a still screen is told the process is working, and roughly on what.
    assert read_done["events"] > 0 and read_done["nodes"] > 0


def test_the_beacon_never_takes_down_the_work_it_reports_on(tmp_path):
    """A progress beacon that can fail the operation it narrates is a downgrade, so the appends are
    contained. `assert_progress_phase` deliberately stays OUTSIDE that containment — an unregistered
    phase is a coding error to fix, not a runtime condition to survive."""
    from looplab.engine.orchestrator import Engine

    class _ExplodingStore:
        path = tmp_path / "events.jsonl"

        def append(self, *_a, **_k):
            raise OSError("the log is gone")

    engine = Engine.__new__(Engine)
    engine.store = _ExplodingStore()
    ran = []
    with engine._progress(PROGRESS_STAGE_BUILD, "implement", node_id=1):
        ran.append(True)
    assert ran == [True]
    # A bare Engine built via __new__ (which ~170 test call sites do) carries no store at all.
    bare = Engine.__new__(Engine)
    with bare._progress(PROGRESS_STAGE_BUILD, "propose"):
        ran.append(True)
    assert ran == [True, True]
    with pytest.raises(ValueError):
        with bare._progress(PROGRESS_STAGE_BUILD, "not_a_phase"):
            pass


def test_enabled_false_runs_the_body_and_emits_nothing(tmp_path):
    from looplab.engine.orchestrator import Engine
    from looplab.events.eventstore import EventStore

    engine = Engine.__new__(Engine)
    engine.store = EventStore(tmp_path / "events.jsonl")
    with engine._progress(PROGRESS_STAGE_RESUME, "read_log", enabled=False):
        pass
    assert engine.store.read_all() == []
    with engine._progress(PROGRESS_STAGE_RESUME, "read_log") as learned:
        learned["events"] = 7
    types = [e.type for e in engine.store.read_all()]
    assert types == [EV_PHASE_PROGRESS, EV_PHASE_PROGRESS]
    # A fact only the BODY can know reaches the `finished` row, never the already-appended `started`.
    assert "events" not in engine.store.read_all()[0].data
    assert engine.store.read_all()[1].data["events"] == 7
