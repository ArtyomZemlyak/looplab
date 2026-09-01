"""The `phase_progress` beacon: a node build must not be a blank panel.

Tier 1 wherever possible (CLAUDE.md's guard-test ladder) — these drive a REAL engine over a REAL
event log and then read the log, rather than pinning source text. The properties that cost something
when they break are: the beacon fires at all on the path a shipped default actually takes; it does
NOT reach the run prologue, whose readers key on the raw log; and its (stage, phase) vocabulary
cannot be typo'd.
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


def test_the_calibration_setup_prefix_still_opens_the_log(offline_run):
    """`speculation_quality._validate_calibration_setup` pins the log's FIRST FIVE events as an exact
    setup prefix, and the bootstrap separately refuses a run whose log is not exactly empty at start.
    Both fail silently — a calibration run simply stops being able to mint a receipt — so the head of
    a real run's log is asserted here rather than left to the gate to discover six GPU runs later."""
    rows, _beaconed = _beacons(offline_run)
    assert [r["type"] for r in rows[:3]] == ["setup_started", "setup_step", "run_started"]


def test_the_run_prologue_stays_write_free_because_its_readers_key_on_the_raw_log(offline_run,
                                                                                  tmp_path):
    """The measured reason `PROGRESS_STAGES` has one stage and not two.

    A resume is just as blank as a build, and beacons WERE added to `Engine._enter_run` and reverted.
    Thirteen tests across four files broke and every one was a real property — the receipt gate pins
    the log BYTES as unchanged when it rejects a run, and finalize RECOVERY changed branch and minted
    a fresh PAID scope instead of resuming the existing one. This drives the invariant that made
    those breaks possible: a resume must add NO row of its own to the log before the loop's first
    turn. If someone re-adds a prologue beacon, this fails here rather than in `test_report`, where
    it reads as a finalize bug.
    """
    import shutil
    from looplab.cli import app
    from typer.testing import CliRunner

    resumed = tmp_path / "resumed"
    shutil.copytree(offline_run, resumed)
    (resumed / "engine.lock").unlink(missing_ok=True)
    before = [json.loads(line) for line in
              (resumed / "events.jsonl").read_text().splitlines() if line.strip()]
    result = CliRunner().invoke(app, [
        "run", "--no-genesis", "--kind", "quadratic", "--goal", "min (x-3)^2",
        "--direction", "min", "--backend", "toy", "--out", str(resumed), "--max-nodes", "8",
    ])
    assert result.exit_code == 0, result.output
    rows, beacons = _beacons(resumed)
    # Nothing may claim a stage the registry does not declare, and `resume` is not declared.
    # Pinned as the ABSENCE of that one word rather than as the whole set, deliberately. This test's
    # property is "the prologue writes no row of its own", which is what the byte-identical prefix
    # below actually drives; an equality over `PROGRESS_STAGES` also forbids every UNRELATED stage,
    # so registering the eval pipeline's own beacon (`PROGRESS_STAGE_EVAL`, a loop that runs hours
    # after the prologue has returned) failed HERE, reading as a resume regression. That is the
    # over-broad-pin trap CLAUDE.md's contract rule names: re-point it at the property, then
    # re-verify the property — which the two assertions below do.
    assert "resume" not in PROGRESS_STAGES
    assert PROGRESS_STAGE_BUILD in PROGRESS_STAGES
    assert all(b["stage"] in PROGRESS_STAGES for b in beacons)
    # The already-durable prefix is untouched byte-for-byte: a re-entry appends only AFTER its
    # authorization and finalize-reconciliation reads, never before or between them.
    assert rows[:len(before)] == before


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
    with engine._progress(PROGRESS_STAGE_BUILD, "propose", enabled=False):
        pass
    assert engine.store.read_all() == []
    with engine._progress(PROGRESS_STAGE_BUILD, "propose") as learned:
        learned["events"] = 7
    types = [e.type for e in engine.store.read_all()]
    assert types == [EV_PHASE_PROGRESS, EV_PHASE_PROGRESS]
    # A fact only the BODY can know reaches the `finished` row, never the already-appended `started`.
    assert "events" not in engine.store.read_all()[0].data
    assert engine.store.read_all()[1].data["events"] == 7


def test_the_browser_label_table_names_exactly_the_phases_this_engine_emits():
    """The UI's word for each beacon lives in `ui/src/buildingModel.js::PHASE_TEXT`, and NOTHING held
    it against the registry that decides which beacons exist. Both directions cost something and
    neither goes red on its own:

      * a phase with NO row renders as `phaseLabel(...) === null`, i.e. the caller's generic
        fallback — the very "Planning next experiment…" blankness the beacon was added to remove,
        arriving as a silent regression the day a phase is added;
      * a row with NO phase is a label for a state nothing can emit. `assert_progress_phase` REFUSES
        an unregistered triple at every append site, so such a row is unreachable code that reads as
        coverage. `build|repair` was in exactly that state: the phase it named was deleted with the
        `debug` operator's build branch on 2026-08-13 and the label stayed, so the strip advertised a
        "Repairing experiment #N…" state the engine can no longer produce.

    This test lives in the PYTHON suite on purpose. `PROGRESS_PHASES` is Python and `python -m pytest`
    is what runs on every change here; a cross-check parked only in `ui/test/` is the half that gets
    skipped, which is how the two tables drifted in the first place.
    """
    import re
    from pathlib import Path

    model = (Path(__file__).resolve().parents[1] / "ui" / "src" / "buildingModel.js").read_text()
    body = model.split("const PHASE_TEXT = {", 1)
    assert len(body) == 2, "PHASE_TEXT must stay findable — the label table moved or was renamed"
    # Stop at the closing brace of the object literal, so the module's later exports are not scanned.
    literal = body[1].split("\n}", 1)[0]
    # Comments carry example keys; a guard test must not be satisfiable by one (CLAUDE.md).
    code = "\n".join(line for line in literal.splitlines()
                     if not line.lstrip().startswith("//"))
    labelled = set(re.findall(r"^\s*'([a-z_]+\|[a-z_]+)'\s*:", code, re.M))
    registered = {f"{stage}|{phase}"
                  for stage, phases in PROGRESS_PHASES.items() for phase in phases}
    assert labelled == registered, (
        f"unlabelled phases render the caller's fallback: {sorted(registered - labelled)}; "
        f"labels for phases nothing emits are dead: {sorted(labelled - registered)}")


# ------------------------------------------- what a CONCURRENT append site owes invariant #1 (2026-08-15)

def test_the_diagnostic_classification_the_concurrent_append_leans_on_is_enforced_at_import():
    """`_progress` appends `EV_PHASE_PROGRESS` DIRECTLY from the speculative producer worker and
    from the parallel-build worker threads. Invariant #1 permits a non-main-task append only for a
    fold-ignored DIAGNOSTIC type, and `_progress`'s own docstring says so — but the module carried no
    assertion, while every sibling concurrent-diagnostic writer (`evaluate.py`, `eval_dispatch.py`,
    both watchdogs) asserts it AT the append site.

    Driven, not pinned: the module SOURCE is re-executed with the registry membership removed, which
    is what a later registry edit folding this type would look like. The guard has to be at IMPORT and
    not inside `_emit`, whose body is wrapped in a containment `except Exception` that would swallow
    it.

    Executed into a THROWAWAY namespace rather than through `importlib.reload`: reloading rebinds
    `SharedEngineMixin` to a new class object while `Engine` still inherits the old one, which is a
    process-wide identity change made to prove a local property.
    """
    from pathlib import Path

    import looplab.events.types as types_mod
    import looplab.engine.shared as shared

    source = Path(shared.__file__).read_text(encoding="utf-8")
    original = types_mod.DIAGNOSTIC_EVENTS
    try:
        types_mod.DIAGNOSTIC_EVENTS = tuple(e for e in original if e != EV_PHASE_PROGRESS)
        with pytest.raises(AssertionError, match="fold-ignored DIAGNOSTIC"):
            exec(compile(source, shared.__file__, "exec"),
                 {"__name__": "looplab.engine.shared_folded_probe", "__file__": shared.__file__})
    finally:
        types_mod.DIAGNOSTIC_EVENTS = original
    # ...and the same source is fine once the membership is back, so the probe proved the assertion
    # and not merely that the module can be made to fail.
    exec(compile(source, shared.__file__, "exec"),
         {"__name__": "looplab.engine.shared_probe", "__file__": shared.__file__})
    assert EV_PHASE_PROGRESS in shared.DIAGNOSTIC_EVENTS


def test_a_body_that_reports_a_fact_named_ok_does_not_destroy_the_failure_it_was_reporting(tmp_path):
    """The yielded dict exists so a body can add what it LEARNED, and `learned["ok"]` used to be a
    duplicate-keyword `TypeError`.

    Raised at the CALL site — outside `_emit`'s containment — from inside the `finally`, where it
    REPLACES the exception the phase was propagating. So a build that failed for a real reason
    surfaced as a TypeError about keyword arguments, and the beacon that was supposed to narrate the
    failure destroyed it. `seconds` is the other colliding name; `detail` collisions were always
    absorbed by the dict merge, which is what made this easy to introduce and hard to notice.
    """
    from looplab.engine.shared import SharedEngineMixin
    from looplab.events.eventstore import EventStore

    class _E(SharedEngineMixin):
        pass

    engine = _E()
    engine.store = EventStore(tmp_path / "events.jsonl")

    with pytest.raises(RuntimeError, match="the real failure"):
        with engine._progress(PROGRESS_STAGE_BUILD, "implement") as learned:
            learned["ok"] = "whatever the body thinks"
            learned["seconds"] = 999
            learned["files"] = 3
            raise RuntimeError("the real failure")

    _rows, beacons = _beacons(tmp_path)
    finished = [b for b in beacons if b["status"] == "finished"]
    assert len(finished) == 1
    # The engine's own fields are authority and win the merge; the body's fact rides beside them.
    assert finished[0]["ok"] is False
    assert isinstance(finished[0]["seconds"], float)
    assert finished[0]["files"] == 3
    assert finished[0]["stage"] == PROGRESS_STAGE_BUILD and finished[0]["phase"] == "implement"
