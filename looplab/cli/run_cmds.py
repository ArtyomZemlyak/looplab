"""Run-lifecycle commands: `run` / `resume` / `stop` / `finalize` / `approve` / `init`.

Split verbatim out of the flat `looplab/cli.py` (docs/15 §P5.2). The shared builders
(`_engine_singleton`, `_load_task`, `_print_result`, …) live in the package `__init__`; the two
names tests monkeypatch on `looplab.cli` are late-bound below so the patch seam survives the split.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Optional

import anyio
import typer
from pydantic import ValidationError

from looplab.core.atomicio import atomic_write_text
from looplab.core.latebind import late_bound
from looplab.events.eventstore import EventStore, EventStoreConcurrencyError
from looplab.events.types import (EV_APPROVAL_GRANTED, EV_PAUSE, EV_RESUME, EV_RESUME_SERVED,
                                  EV_RUN_ABORT, EV_RUN_FINISHED, EV_RUN_REOPENED, EV_SPEC_APPROVED)
from looplab.engine.orchestrator import (
    Engine,
    SPECULATION_CALIBRATION_PROFILE_DIGEST,
    RunStartPinError,
)
from looplab.engine.finalize import finalize_run, incomplete_finalize_scope
from looplab.events.replay import fold
from looplab.adapters.repo_task import eval_reader_path_errors
from looplab.adapters.tasks import validate_task
from looplab.search.speculation_calibration import canonical_speculation_toy_task
from looplab.core import appconfig
from looplab.serve.run_files import run_config_write_lock
from looplab.cli import (_BACKENDS, _DEV_BACKENDS, _TASK_KINDS, _choice, _engine_singleton,
                         _apply_speculation_calibration_profile,
                         _assert_run_deletion_namespace_available,
                         _exit_nonzero_if_the_run_produced_nothing, _load_task, _print_result,
                         _require_healthy_log, _require_run_dir, app, load_run_settings)


# How long `resume` waits for a stopped run's previous owner to release engine.lock, and how often
# it says so while waiting. Generous: a legitimate finalization tail (report + cross-run lessons +
# cost roll-up) can take minutes. The point is that a WEDGED owner ends in an actionable message
# instead of a terminal that hangs with no output at all.
_HANDOFF_WAIT_S = 600.0
_HANDOFF_ECHO_EVERY_S = 15.0


# Late-bound through the package module: tests patch `looplab.cli._engine`
# (test_cli.py::_capture_backend) and `cli.make_llm_client` (×5, to stub the Genesis path offline),
# and the command bodies here must see those patches at call time. `looplab.core.latebind` explains
# the freeze-at-import hazard these shims exist to avoid.
_engine = late_bound("looplab.cli", "_engine")
make_llm_client = late_bound("looplab.cli", "make_llm_client")


def _refuse_wrap_up_engine_that_could_propose(eng) -> None:
    """A `wrap_up_only` engine must not drive a loop on a boundary that can start new work.

    The backstop for the whole refuse-vs-warn split, placed at the ONE point all three commands
    enter the loop. `wrap_up_only` is a promise made when the engine is built — "this entry point
    can only complete the terminal already on disk" — and it is what turns the endpoint/credential
    REFUSAL into a warning. Every command decides it from a folded log, and a fold is a snapshot: if
    anything lifts the run between that fold and this line, the engine the operator was told is only
    wrapping up starts proposing instead, against a model it has already been told is unavailable.
    That produced N identical `x=0,y=0` fallback nodes, a flat metric and exit 0 (`resume`, whose
    decision sat on the far side of the handoff wait — fixed at the source below, since a backstop
    that only catches the outcome would still have let a `finalize` become a search epoch).

    Costs one fold on every entry, and answers with the classifier the decision was made with, so
    the check and the promise cannot drift apart. Engines built outside `_engine` (test stubs) carry
    no promise and are not checked.
    """
    if not getattr(eng, "wrap_up_only", False):
        return
    events = eng.store.read_all()
    kind = classify_prior_run(fold(events), events)
    if is_wrap_up(kind):
        return
    from looplab.agents.preflight import wrap_up_lift_refusal
    raise wrap_up_lift_refusal(kind, getattr(eng, "wrap_up_degradation_warning", None))


def _run_engine_guarded(eng: Engine):
    """Drive the engine loop to completion, funneling any fatal abort into a terminal event.
    Shared by `run` and `resume` (previously duplicated verbatim in both)."""
    _refuse_wrap_up_engine_that_could_propose(eng)
    started = time.time()
    try:
        return anyio.run(eng.run)
    except RunStartPinError:
        # A refused re-entry (stale speculation evidence, or a resume spelling a width other than the
        # one run_started pinned) deliberately leaves the untrusted/stale run prefix untouched.  The
        # generic fatal-error path below writes run_finished + finalization receipts, which would turn
        # a refused re-entry into a mutation by the very engine that refused to trust it.
        raise
    except Exception as e:  # noqa: BLE001 - any fatal abort (e.g. an unreachable LLM endpoint
        # during implement/repair, a missing dep) must surface as a TERMINAL event, not a silent
        # stalled run the UI shows "thinking" forever. Mark finished-with-error, then re-raise so
        # the failure still lands in engine.stderr.log — as a traceback for a genuine bug, and as
        # the one-line refusal for the `core/errors.py::OperatorRefusal` family, which the CLI
        # boundary (`cli/__init__.py::_RefusalBoundaryGroup`) renders as a message instead
        # (`LOOPLAB_TRACEBACK=1` puts the frames back). That boundary is where an operator now
        # reads WHY, and it matters here: `error_text` below is `str(e)` of whatever escaped
        # `anyio.run`, which for anything raised inside the eval task group is the group's own
        # "unhandled errors in a TaskGroup (1 sub-exception)" — the leaf message never reaches this
        # event. (A user Ctrl-C / cancel is BaseException, not Exception, so an intentional stop
        # stays resumable.)
        try:
            error_text = str(e)[:500]
        except BaseException:  # an adversarial __str__ must not replace the root exception
            error_text = type(e).__name__
        error = {"reason": "error", "error": error_text}
        try:
            events = eng.store.read_all()
            current = fold(events)
            # A fatal serial Developer/plugin exception may escape after card_added+node_building but
            # before node_created. Close those reservations (Card drop first, then node failure) BEFORE
            # run_finished clears the transient projection; otherwise reopening can resurrect a native
            # Card as selectable work with no authoritative owner.
            if getattr(current, "buildings", None):
                eng._recover_interrupted_builds(current)
                events = eng.store.read_all()
                current = fold(events)
            # A fatal policy/scorer exception can escape while Layer 5 owns a durable request head.
            # Finalization is not quiescent until every exact head has a done receipt; commits are
            # forbidden on this error path, so an already-created prefix is linked and every other
            # request is closed stale. Bounded by the durable queue length, with one spare CAS retry.
            head_request = getattr(eng, "_head_request", None)
            serve_card_builds = getattr(eng, "_serve_card_builds", None)
            if callable(head_request) and callable(serve_card_builds):
                pending = max(
                    0,
                    len(getattr(current, "card_build_requests", []) or [])
                    - int(getattr(current, "card_builds_done", 0) or 0),
                )
                for _request in range(pending + 1):
                    if head_request(current) is None:
                        break
                    if not serve_card_builds(allow_commit=False):
                        break
                    events = eng.store.read_all()
                    current = fold(events)
                queue_quiescent = head_request(current) is None
            else:
                queue_quiescent = True
            if not current.finished and queue_quiescent:
                after_seq = events[-1].seq if events else -1
                try:
                    eng._finish_with_report_if_quiescent(
                        current, error, after_seq=after_seq)
                except Exception:  # noqa: BLE001 - fall through to the minimal CAS finish
                    pass
                # Report generation itself may have appended before failing. Re-read and bind the
                # fallback finish to the new tail so it is still the ordinary replay-checked CAS.
                # A UI control can win one of these tail CAS attempts; bounded refold/retry prevents
                # that benign race from leaving a fatal engine as a permanent zombie.
                for _attempt in range(8):
                    events = eng.store.read_all()
                    current = fold(events)
                    if current.finished:
                        break
                    after_seq = events[-1].seq if events else -1
                    try:
                        eng.store.append(
                            EV_RUN_FINISHED,
                            {**error, "after_seq": after_seq, "finalization_required": True},
                            expected_last_seq=after_seq)
                    except EventStoreConcurrencyError:
                        continue
                    except Exception:  # noqa: BLE001 - preserve the original failure
                        break
                    break
            # `run_finished` and wrap-up are distinct durable boundaries. Even when the exception
            # happened after the terminal append (or in an optional side effect), repair the exact
            # pending finish before re-raising the ORIGINAL exception.
            if queue_quiescent:
                finalize_run(eng, entry_finished=current.finished, start_time=started)
        except Exception:  # noqa: BLE001 - terminal recovery must never replace the root traceback
            pass
        raise


def _preflight_speculation_authority(eng: Engine, events=None) -> None:
    """Authorize an existing durable prefix before the CLI performs lifecycle/snapshot writes.

    ``Engine.run`` repeats this check at every semantic decision boundary.  CLI ``run``/``resume``
    also own writes before that coroutine starts, so they need the same read-only boundary first.
    The callable guard keeps lightweight compatibility stubs used by command-level tests working.
    """
    guard = getattr(eng, "_require_pinned_speculation_receipt", None)
    if not callable(guard):
        return
    source = eng.store.read_all() if events is None else events
    guard(fold(source))


def _preflight_settled_widths(eng: Engine, events=None, *, surface: str) -> None:
    """Adopt or refuse this run's pinned concurrency widths BEFORE the CLI writes anything.

    The exact twin of `_preflight_speculation_authority`, and it exists for the exact same reason.
    `SettledWidthPinError` was given `RunStartPinError` as its base so that a refused re-entry gets
    the receipt's "do not mutate the log you refused to trust" handling — but the receipt has this
    command-level boundary and the width check had only the one inside `Engine.run`, which the CLI
    reaches AFTER it has already written. Both entry points therefore mutated the run they went on to
    refuse, measured on a real run dir:

    * `looplab run <existing dir>` with a disagreeing width overwrote `config.snapshot.json` (2 -> 3)
      and appended `run_reopened` (38 -> 39 events), THEN raised. The refusal's own remedy pointed at
      the snapshot it had just clobbered — and at a file `run` never reads back;
    * `looplab resume <finished|paused run>` appended `resume` first (38 -> 39). The byte-unchanged
      property held only on the SECOND identical attempt, i.e. once the run was no longer in a state
      worth lifting — not in the case an operator actually hits.

    `_repin_settled_widths` is left as the sole owner of the adopt/refuse rule; this only moves WHERE
    it is first asked. It is idempotent, so `Engine.run`'s own call still runs and still guards a
    concurrent tail edit: on the adopt path it has already set the width and re-reads its own value.
    ``surface`` tells the refusal which knob this command actually reads (`engine/widths.py`).
    The callable guard keeps lightweight compatibility stubs used by command-level tests working.

    DELIBERATELY NOT WIRED INTO the two WRAP-UP boundaries — `finalize`, and `run`'s
    `finalization_pending` branch — unlike the receipt preflight. The width pin protects an execution
    TREATMENT, and both of those are `wrap_up_only`: they can only complete the terminal already on
    disk, at no width. Neither publishes a snapshot or appends a lifecycle event, so there is nothing
    for an early refusal to protect; both also take their settings from the run's OWN snapshot rather
    than the launch flags, so `surface="run"` would name the wrong knob. Refusing there would buy
    nothing and could strand a run that can no longer be wrapped up — the same reason `finalize`
    warns about a dead endpoint instead of refusing. (`Engine.run` still refuses if a wrap-up ends up
    driving the loop; that is its backstop's business, not a boundary to move earlier.)
    """
    guard = getattr(eng, "_repin_settled_widths", None)
    if not callable(guard):
        return
    source = eng.store.read_all() if events is None else events
    guard(fold(source), source=surface)


def terminal_projection_incomplete(state, events) -> bool:
    """Whether a folded run has an ACCEPTED terminal boundary whose wrap-up did not complete.

    `run_finished` lands BEFORE the engine's finalization checklist, and a richer scoped checklist
    can be interrupted part-way, so "finished" never answers "is the wrap-up done" on its own. Three
    commands need this exact disjunction and each used to spell it out (doc 25 CT-03) — and it is
    replay-critical, because it decides whether a command may append a lifecycle event at all.
    """
    return incomplete_finalize_scope(events) is not None or state.finalization_pending()


# The two notices that were byte-identical in `run` and `resume`. Both mean the same thing — respect
# the wrap-up already on disk, do NOT lift the run — so the mapping is owned here.
WRAP_UP_NOTICE = {
    "finalization_pending":
        "run has an incomplete terminal projection — completing its existing wrap-up",
    "pending_finalize":
        "run has a pending finalize — wrapping it up (report / cross-run lessons / cost)",
}


def classify_prior_run(prior, prior_events) -> str:
    """Which lifecycle boundary a folded prior run sits at, before a command re-enters the loop.

    `run` and `resume` each carried this ladder inline with identical echo strings for the first two
    rungs, and `finalize` a third variant of the same predicates. It decides WHICH EVENT, if any, is
    appended before re-entry, so a fix applied to one copy and not the others silently gives the
    entry points different lifecycles (doc 25 CT-03).

    The ORDER is the contract, not an implementation detail:

    * an incomplete terminal projection outranks everything — the wrap-up on disk owns the boundary;
    * a stop request newer than the finish, or a finish whose reason is `error`, is a PENDING
      FINALIZE and must be respected rather than lifted (the UI's finalize path spawns `resume` and
      still has to finalize);
    * only then the liftable states: a plain finish, then a plain pause.

    Callers keep their surface-specific differences — `run` REOPENS a finished dir while `resume`
    appends `resume` to it — because those genuinely differ; only the classification is shared.
    """
    if terminal_projection_incomplete(prior, prior_events):
        return "finalization_pending"
    if prior.stop_requested and (
            not prior.finished or str(prior.stop_reason or "").lower() == "error"):
        return "pending_finalize"
    if prior.finished:
        return "finished"
    if prior.paused:
        return "paused"
    return "live"


def is_wrap_up(kind: str) -> bool:
    """Whether this boundary is WRAP-UP ONLY: the loop may complete the terminal the log already
    carries, and cannot start new work.

    Separate from `announce_wrap_up` because it is needed EARLIER and without the echo: `_engine`
    decides refuse-vs-warn on an unreachable LLM endpoint from exactly this predicate (a run that
    can only finish an existing wrap-up has no proposal left to degrade), and that decision happens
    before the engine exists, while the notice belongs at the point where the run is re-entered.
    """
    return kind in WRAP_UP_NOTICE


def announce_wrap_up(kind: str) -> bool:
    """Echo the shared notice for a wrap-up state. True means "the caller must not lift this run"."""
    if not is_wrap_up(kind):
        return False
    typer.echo(WRAP_UP_NOTICE[kind])
    return True


def wrap_up_degradation_note(eng) -> str | None:
    """The closing half of a wrap-up that ran without a model, or None when it had one.

    `_engine` runs both LLM gates on a wrap-up entry — is the credential usable, is the endpoint
    there — and warns instead of refusing; the artifacts it then produces are real but partly
    model-free. A bare "finalized <dir>" after that would be the silent half of the degradation —
    the operator would have to scroll back past a whole wrap-up to learn that the report is a
    placeholder and that no lessons were distilled.
    """
    if not getattr(eng, "wrap_up_degradation_warning", None):
        return None
    return ("wrapped up WITHOUT the model: the end-of-run report is the \"(report unavailable)\" "
            "placeholder and nothing model-authored reached cross-run memory. Those steps are "
            "marked complete and will not be retried on a later finalize.")


def _pending_finalize(state) -> bool:
    """A stop/finalize request is newer than the terminal finish that last served one."""
    last_stop = state.last_stop_request_seq
    if last_stop >= 0:
        return last_stop > state.last_finish_seq
    # Compatibility while folding logs written before the sequence field existed.
    return bool(state.stop_requested and not state.finished)


def _pending_finalization_inputs(run_dir: Path, task_id: str | None):
    """Load the immutable inputs of an already-terminal run before repairing its wrap-up.

    A new ``looplab run`` invocation may carry different flags even when its task id is unchanged.
    Those inputs belong to a possible next search epoch, not to the exact terminal boundary already
    on disk. Recovery therefore uses the snapshots that produced that boundary and never replaces a
    missing or corrupt snapshot with the new invocation's values.
    """
    task_snap = run_dir / "task.snapshot.json"
    config_snap = run_dir / "config.snapshot.json"
    missing = [path.name for path in (task_snap, config_snap) if not path.exists()]
    if missing:
        raise typer.BadParameter(
            "cannot complete pending finalization without the original run snapshot(s): "
            + ", ".join(missing))

    recovery_task = _load_task(task_snap, existing_run=True)
    if task_id and recovery_task.id != task_id:
        raise typer.BadParameter(
            f"task.snapshot.json belongs to task {recovery_task.id!r}, but the event log belongs "
            f"to {task_id!r}; refusing to finalize with mismatched inputs")
    return recovery_task, load_run_settings(run_dir, strict=True)


def _pin_offline_speculation_profile(settings, *, calibration: bool, genesis: bool, goal) -> None:
    """Force the immutable offline speculation profile when this run is a calibration/receipt run.

    Runs BEFORE Genesis client construction or any role wiring — the bootstrap must be provably
    offline, and merely resolving to a ToyTask AFTER a model-authored Genesis call is insufficient.
    Lifted out of `run` (doc 25 CT-02): it is a maintainer-only lane that most readers of `run`
    never need, and it was ~20 of the lines between "merge settings" and "author the task".
    """
    if calibration:
        if genesis:
            raise typer.BadParameter(
                "--speculation-gate-calibration requires --no-genesis")
        if settings.speculation_gate_receipt is not None:
            raise typer.BadParameter(
                "--speculation-gate-calibration is restricted: "
                "speculation_gate_receipt must be unset")
        _apply_speculation_calibration_profile(settings)
        typer.echo(
            "Speculation calibration profile "
            f"{SPECULATION_CALIBRATION_PROFILE_DIGEST} (offline toy/greedy/GPU)"
        )
    elif settings.speculation_gate_receipt is not None:
        # A receipt is not a general product rollout. Force the exact offline profile before Genesis
        # or any role/client construction, preserving only max_nodes/depth/receipt. An explicit
        # model-authored task would cross that boundary before Engine admission, so reject it here.
        if genesis and goal is not None:
            raise typer.BadParameter(
                "receipt-backed Card speculation requires --no-genesis")
        _apply_speculation_calibration_profile(settings)


def _calibration_envelope_task_dict(task, settings) -> dict:
    """Prove the calibration envelope, then return the canonical task dict its evidence needs.

    A receipt cannot bootstrap itself after an implementation digest changes.  Keep the only
    no-receipt path narrow and non-production: an explicit CLI-only flag, a deterministic toy
    adapter/backend, a real-GPU requirement, Card authority and a small budget. Both the serial
    depth=0 baseline and a positive-depth treatment use this same immutable envelope.
    It is deliberately absent from Settings/snapshots/env/UI and resume has no corresponding
    option, so it cannot silently authorize an application workload or survive re-entry.

    Every clause is collected before raising so an operator sees the whole envelope at once rather
    than fixing it one rejection per invocation.
    """
    from looplab.adapters.toytask import ToyTask
    calibration_errors: list[str] = []
    if type(task) is not ToyTask:
        calibration_errors.append("task must be the exact offline quadratic ToyTask")
    else:
        try:
            canonical_speculation_toy_task(task, require_seed_set=True)
        except ValueError as exc:
            calibration_errors.append(str(exc))
    if settings.backend != "toy":
        calibration_errors.append("backend must be toy")
    if settings.policy != "greedy" or settings.strategist_backend != "off":
        calibration_errors.append("policy must be greedy and strategist must be off")
    if settings.card_driven_selection is not True:
        calibration_errors.append("card_driven_selection must be true")
    if not 1 <= settings.max_nodes <= 64:
        calibration_errors.append("max_nodes must be in 1..64")
    if settings.speculation_gate_receipt is not None:
        calibration_errors.append("speculation_gate_receipt must be unset")
    try:
        from looplab.core.hardware import effective_gpu_inventory
        if not effective_gpu_inventory():
            calibration_errors.append("an effective visible GPU must be detected")
    except Exception:
        calibration_errors.append("GPU inventory could not be detected")
    if calibration_errors:
        raise typer.BadParameter(
            "--speculation-gate-calibration is restricted: "
            + "; ".join(calibration_errors)
        )
    # Unlike an ordinary authoring snapshot, calibration evidence must contain every defaulted
    # task identity/provenance field (id/direction/noise/seed included), not just the flags the
    # command happened to spell.
    return task.model_dump(mode="json", exclude_none=False)


def _assert_calibration_dir_is_fresh(out: Path, prior_events) -> None:
    """Calibration evidence is only meaningful from a directory with exactly zero prior anything.

    Checked under the singleton lock, against the events this command actually read — a prior log,
    a non-empty events file, or any leftover material means the run dir already carries history the
    receipt would silently inherit.
    """
    stale_material = sorted(
        path.name for path in out.iterdir()
        if path.name not in {"engine.lock", "events.jsonl"}
    )
    events_path = out / "events.jsonl"
    if prior_events != [] or (events_path.exists() and events_path.stat().st_size) \
            or stale_material:
        detail = (f"; stale material: {', '.join(stale_material)}"
                  if stale_material else "")
        raise typer.BadParameter(
            "--speculation-gate-calibration requires a fresh empty run directory "
            "with exactly zero prior events" + detail
        )


def _publish_run_snapshots(out: Path, task_dict: dict, settings) -> None:
    """Publish this epoch's input snapshots, both inside ONE reset-aware config transaction.

    Called only after the new Engine is viable, so a construction failure leaves the PRIOR run's
    provenance intact. Self-describing run: the RESOLVED task dict (after file + flags) goes to
    disk as canonical JSON so `resume` (CLI or UI) can re-enter without the original file. The task
    snapshot shares the config lock deliberately — a direct `looplab run` must not refresh either
    snapshot while Replay has archived generation A but not proven generation B.
    """
    config_snapshot = out / "config.snapshot.json"
    with run_config_write_lock(config_snapshot):
        atomic_write_text(
            config_snapshot, json.dumps(settings.masked_snapshot(), indent=2))
        try:
            atomic_write_text(out / "task.snapshot.json", json.dumps(task_dict, indent=2))
        except OSError:
            pass


def _missing_task_paths(task_dict: dict) -> list[tuple[str, str]]:
    """Return (field, expanded_path) for every input path the task names that does NOT exist on disk.
    CLI Genesis is a single LLM call (not an agent) — it can author a path the user mis-stated or that
    the model invented, and the run then dies deep inside the first eval with a cryptic
    'No such file or directory'. Surfacing it up front (a warning, since some paths are created by a
    repo's setup step) lets the user fix the path before spending a run. ~ and $VARS are expanded."""
    if not isinstance(task_dict, dict):
        return []
    candidates: list[tuple[str, object]] = []
    # `repo` is the composable alias of editable_path — the preflight runs on the RAW task dict
    # (normalize_task only renames it inside validate_task's copy), so a typo'd composable repo
    # path used to skip this warning entirely and die deep in workspace seeding instead.
    for key in ("data_path", "editable_path", "repo"):
        if task_dict.get(key):
            candidates.append((key, task_dict[key]))
    for ed in (task_dict.get("editables") or []):
        if isinstance(ed, dict) and ed.get("path"):
            candidates.append((f"editables[{ed.get('name', '?')}]", ed["path"]))
    # `data`/`dataset` values may be a bare path or a DataSpec {path, mount, …}; check the path either way.
    for dkey in ("data", "dataset"):
        dv = task_dict.get(dkey)
        if isinstance(dv, dict):
            for k, v in dv.items():
                p = v.get("path") if isinstance(v, dict) else v
                if p:
                    candidates.append((f"{dkey}.{k}", p))
        elif isinstance(dv, str) and dv:
            candidates.append((dkey, dv))
    for ref in (task_dict.get("references") or []):
        if isinstance(ref, dict) and ref.get("path"):
            candidates.append((f"references[{ref.get('name', '?')}]", ref["path"]))
    missing = []
    for field, raw in candidates:
        if not isinstance(raw, str):
            continue
        p = os.path.expandvars(os.path.expanduser(raw))
        if not Path(p).exists():
            missing.append((field, p))
    return missing


@app.command()
def run(
    task_file: Optional[Path] = typer.Argument(
        None, help="Config or task file (YAML or JSON). A unified file has task:/settings:/out: keys; "
                   "a bare task file is just the task. Omit it and build the task from --goal/--kind."),
    goal: Optional[str] = typer.Option(None, help="Task goal in plain words (build a task with no file)."),
    kind: Optional[str] = typer.Option(None, help=f"Task kind. With --goal it PINS the kind and "
                                                  f"Genesis fills the rest; with --no-genesis it's used "
                                                  f"as written. One of: {', '.join(_TASK_KINDS)}."),
    direction: Optional[str] = typer.Option(None, help="Optimize: min | max."),
    data: Optional[str] = typer.Option(None, help="Path to your data/repo. Optional under Genesis — "
                                                  "you can instead just say where the data is in --goal."),
    genesis: bool = typer.Option(
        True, "--genesis/--no-genesis",
        help="With --goal, let the LLM author the task (--kind pins the kind, Genesis fills the rest, "
             "including data locations you mention). --no-genesis builds it from --kind/--set as written."),
    set_: list[str] = typer.Option(
        [], "--set", "-s", metavar="KEY=VALUE",
        help="Override ANY engine setting, repeatable (e.g. -s max_nodes=20 -s policy=asha). "
             "Same keys as the settings: block / LOOPLAB_* env."),
    out: Optional[Path] = typer.Option(None, help="Run directory (default: the file's out: or runs/run_local)."),
    max_nodes: Optional[int] = typer.Option(None, help="Override node budget."),
    backend: Optional[str] = typer.Option(None, help="Role backend: toy | llm."),
    developer_backend: Optional[str] = typer.Option(
        None, help="Developer: default | opencode | aider | goose | continue."),
    agent_cmd: Optional[str] = typer.Option(
        None, help="Path/launcher override for the external coding agent."),
    validate_agent: Optional[bool] = typer.Option(
        None, help="Validate external-agent output (retry+fallback). Default on."),
    agent_patch_gate: Optional[bool] = typer.Option(
        None, help="Run the agent in a git worktree and surface-gate its diff. Default on."),
    agent_surface: Optional[str] = typer.Option(
        None, help="Comma-separated edit-surface globs for the agent (default '*.py')."),
    model: Optional[str] = typer.Option(None, help="LLM model id (when backend=llm)."),
    knowledge_dir: Optional[str] = typer.Option(None, help="Notes dir for agentic retrieval."),
    memory_dir: Optional[str] = typer.Option(None, help="Cross-run case memory dir."),
    max_seconds: Optional[float] = typer.Option(None, help="Wall-clock budget; abort when exceeded."),
    ablate_every: Optional[int] = typer.Option(None, help="Ablation refinement every N improves (0=off)."),
    require_approval: Optional[bool] = typer.Option(
        None, help="HITL: pause for `approve` before finishing."),
    confirm_top_k: Optional[int] = typer.Option(None, help="Confirm top-k under multiple seeds."),
    confirm_seeds: Optional[int] = typer.Option(None, help="Seeds for the confirmation pass."),
    crash_after: Optional[int] = typer.Option(None, hidden=True,
                                              help="Test hook: hard-exit after N evals."),
    speculation_gate_calibration: bool = typer.Option(
        False,
        "--speculation-gate-calibration",
        hidden=True,
        help="Maintainer hook: bootstrap bounded offline GPU speculation evidence."),
):
    """Start a new run (or continue if the run dir already has events).

    Three equivalent ways to say what to solve:

      - looplab run config.yaml                # one file: task + settings + out
      - looplab run task.json --max-nodes 20   # a bare task file + flags (legacy)
      - looplab run --kind dataset --goal "predict target" --data data.csv -s backend=llm

    Any engine setting can be overridden with `-s/--set key=value` (full parity with the settings:
    block and LOOPLAB_* env). Run `looplab init` to scaffold a documented config file.

    Maintainer note: the typed `--flag` surface below is FROZEN. `-s/--set` already reaches every
    `Settings` field with full parity, so a NEW engine knob needs only a `Settings` field — do NOT
    add a new typer.Option here (each one also has to be threaded into the settings dict at the
    `# 3. Merge engine settings` block below, doubling the edit and the drift risk). The existing
    flags stay for back-compat and ergonomics."""
    if backend is not None:
        _choice(backend, _BACKENDS, "--backend")
    if developer_backend is not None:
        _choice(developer_backend, _DEV_BACKENDS, "--developer-backend")
    # 1. Read the file (if any): a unified doc yields task + settings + out; a bare file is the task.
    file_task, file_settings, file_out = {}, {}, None
    if task_file is not None:
        try:
            file_task, file_settings, file_out = appconfig.load_document(task_file)
        except FileNotFoundError:
            raise typer.BadParameter(f"config file not found: {task_file}")
        except ValueError as e:
            raise typer.BadParameter(f"could not read {task_file}: {e}")
    # 2. Overlay the task-building flags onto the (possibly empty) file task.
    try:
        task_dict = appconfig.apply_task_flags(
            file_task, kind=kind, goal=goal, direction=direction, data=data)
    except ValueError as e:
        raise typer.BadParameter(str(e))
    # 3. Merge engine settings (file < typed flags < --set). Typed bool flags only override when set,
    # so a settings: file can still enable them.
    typed: dict = {}
    for name, value in (("max_nodes", max_nodes), ("backend", backend),
                        ("developer_backend", developer_backend), ("agent_cmd", agent_cmd),
                        ("validate_agent", validate_agent), ("agent_patch_gate", agent_patch_gate),
                        ("llm_model", model), ("knowledge_dir", knowledge_dir),
                        ("memory_dir", memory_dir), ("max_seconds", max_seconds),
                        ("ablate_every", ablate_every), ("confirm_top_k", confirm_top_k),
                        ("confirm_seeds", confirm_seeds),
                        # TRI-STATE here like every other typed bool on this command, and for the
                        # same reason: `Optional[bool]` defaulting to None makes
                        # `--no-require-approval` forward FALSE instead of being indistinguishable
                        # from "flag absent". Declared `bool` + a separate `if require_approval:`, it
                        # could only ever forward the TRUE case, so the documented "CLI over file
                        # over environment" precedence (docs/guide/cli-reference.md) held in ONE
                        # direction: a `require_approval: true` config plus `--no-require-approval`
                        # still paused for an `approve` the operator had explicitly opted out of, and
                        # the run ended finished=False, blocked forever.
                        ("require_approval", require_approval)):
        if value is not None:
            typed[name] = value
    if agent_surface is not None:
        typed["agent_surface"] = [g.strip() for g in agent_surface.split(",") if g.strip()]
    try:
        sets = appconfig.parse_sets(set_)
    except ValueError as e:
        raise typer.BadParameter(str(e))
    try:
        settings = appconfig.build_settings(file_settings, typed, sets)
    except ValidationError as e:
        raise typer.BadParameter(f"invalid settings: {e}")
    # `parse_sets` returns a DICT, so intersect its keys — `set & dict` is a TypeError, and this line
    # runs on every `looplab run`, before any of the work the command exists to do.
    set_profile_replaced = bool({"llm_profile", "llm_profiles"} & sets.keys())
    effective_model_override = (sets["llm_model"] if "llm_model" in sets else
                                model if not set_profile_replaced else None)
    if effective_model_override is not None:
        # --set is the final precedence layer. An explicit profile replacement there wins over the
        # typed --model flag; otherwise make the highest-precedence model spelling effective inside
        # the active profile while retaining that profile's endpoint and credential binding.
        from looplab.core.llm import apply_llm_model_override
        apply_llm_model_override(settings, str(effective_model_override))
    _pin_offline_speculation_profile(
        settings, calibration=speculation_gate_calibration, genesis=genesis, goal=goal)
    # 3b. Genesis: you described the goal in words — let the LLM author the task (the headless
    # counterpart of the UI's "New run"). Fires on an explicit --goal (so no file-based / legacy flow
    # is affected). --kind does NOT skip it: it PINS the kind and Genesis fills the rest within it;
    # describe data locations in the goal and Genesis authors the mounts (no --data needed). Opt out
    # with --no-genesis (then --kind + flags are used as written), or run a complete file with no --goal.
    backend_chosen = (backend is not None or "backend" in file_settings or "backend" in sets
                      or "LOOPLAB_BACKEND" in os.environ
                      # also covers a backend set via the .env file (env vars alone miss it), so
                      # Genesis doesn't clobber an explicit user choice.
                      or "backend" in getattr(settings, "model_fields_set", set()))
    if genesis and goal is not None:
        from looplab.engine import genesis as _genesis
        try:
            from looplab.core.llm import make_llm_client_for
            client = make_llm_client_for(
                settings, role="strategist", factory=make_llm_client)
        except Exception as e:  # noqa: BLE001 - no endpoint configured/reachable
            raise typer.BadParameter(
                f"Genesis needs an LLM to author the task ({e}). Point LOOPLAB_LLM_BASE_URL/--model "
                f"at a reachable model, or use --no-genesis to build the task from --kind/--set alone.")
        # Pass the file's task: block (if any) as a draft so --goal refines it instead of discarding it.
        result = _genesis.author_task(goal, client=client, kinds=_TASK_KINDS, kind=kind, data=data,
                                      direction=direction, draft=(file_task or None),
                                      parser=settings.llm_parser,
                                      memory_dir=getattr(settings, "memory_dir", None),          # PART V §22
                                      cross_run_read_tools=getattr(settings, "cross_run_read_tools", False))
        if result.error:    # transport/endpoint failure -> NOT a vague goal; say so plainly
            raise typer.BadParameter(
                f"Genesis couldn't reach the model to author the task ({result.error}). Check "
                f"LOOPLAB_LLM_BASE_URL/--model, or use --no-genesis to build it from --kind/--set.")
        if not result.kind:
            typer.echo("Genesis couldn't author a task from that goal. "
                       + (result.reply or "Add detail (e.g. where the data is), or pass --kind."))
            raise typer.Exit(2)
        task_dict = result.task
        # A generative kind (the agent writes/edits code) implies an LLM-driven run; default the
        # backend to llm when the user didn't pick one. Offline-optimizable kinds keep their default.
        # The kind→backend rule lives in `engine/genesis.py::default_backend` (shared with the web
        # UI's /api/start funnel, `serve/launch.py::_resolve_settings`, and the genesis card's
        # display-only `launch.py::_defaults_backend_llm`) — only the CLI-surface `backend_chosen`
        # detection above stays here.
        genesis_backend = _genesis.default_backend(result.kind, chosen=backend_chosen)
        if genesis_backend is not None:
            settings.backend = genesis_backend
        typer.echo(f"Genesis -> kind={result.kind}: {result.rationale or result.reply}".rstrip())
    # A goal described in words but no kind, with Genesis off: do NOT silently fall back to the
    # quadratic toy optimizer (validate_task's default) — that would run nonsense on a real goal and
    # drop --data. Make the user pin a kind or let Genesis infer it.
    if (goal is not None or data is not None) and not task_dict.get("kind"):
        raise typer.BadParameter(
            "no task kind: pass --kind, or drop --no-genesis to let Genesis infer it "
            "(a bare --data would otherwise run the quadratic toy and drop your data path).")
    # 4. Validate the resolved task, then resolve the run dir: explicit --out > file out: > default.
    if not task_dict:
        raise typer.BadParameter(
            "no task: pass a config/task file, or build one with --goal/--kind "
            "(scaffold one with `looplab init`).")
    try:
        task = validate_task(task_dict)
    except (ValueError, KeyError, TypeError) as e:
        raise typer.BadParameter(f"invalid task: {e}")
    if speculation_gate_calibration:
        task_dict = _calibration_envelope_task_dict(task, settings)
    # the task adapters own comparison-contract validation and identity.  Persist their
    # canonical value (including contract_id), never the pre-validation authoring dict, so CLI and
    # Web/TUI/API launches produce the same scientific provenance in task.snapshot.json.
    comparison_contract = getattr(task, "comparison_contract", None)
    if comparison_contract is not None:
        task_dict["comparison_contract"] = comparison_contract.model_dump(
            mode="json", by_alias=True, exclude_none=True)
    else:
        task_dict.pop("comparison_contract", None)
    # Path sanity-check (esp. for Genesis-authored tasks): warn loudly when an input path doesn't
    # exist, so a mistyped/invented data/repo path is caught HERE — not as a cryptic mid-run
    # 'No such file or directory'. A warning (not a hard stop): a repo's setup step may create some
    # paths, and the user may know better. Use --no-genesis or fix the path to silence it.
    for field, p in _missing_task_paths(task_dict):
        typer.echo(f"⚠ task {field} does not exist on disk: {p}", err=True)
    out = out or (Path(file_out) if file_out else Path("runs/run_local"))
    _assert_run_deletion_namespace_available(out)
    out.mkdir(parents=True, exist_ok=True)
    store = EventStore(out / "events.jsonl")
    _require_healthy_log(store, out)   # fail closed on a mid-file corruption before appending (P0-4)
    with _engine_singleton(out) as ok:
        if not ok:
            typer.echo(f"engine already running on {out} — not starting a second loop")
            return
        # Fold the EXISTING log FIRST (before writing any snapshot): a `run` on a dir that already
        # belongs to a DIFFERENT task must REFUSE rather than overwrite its task/config snapshot and
        # then reopen the old event log — that silently mixed two experiments (a reproduced
        # task.snapshot=poly_regression while run_started.task_id=toy_quadratic — arch-review §3 P0-5).
        # Continuing the SAME task is fine; an empty/fresh dir has no prior run_started.
        prior_events = store.read_all()
        prior = fold(prior_events)
        if speculation_gate_calibration:
            _assert_calibration_dir_is_fresh(out, prior_events)
        prior_kind = classify_prior_run(prior, prior_events)
        finalization_pending = prior_kind == "finalization_pending"
        if prior.run_id and prior.task_id and prior.task_id != task.id:
            typer.echo(
                f"run dir {out} already holds task {prior.task_id!r}, not {task.id!r} — refusing to "
                f"mix experiments in one event log. Use a new --out for a fresh run, or "
                f"`looplab resume {out}` to continue the existing one.", err=True)
            raise typer.Exit(2)
        # Write the run snapshots only AFTER winning the singleton lock AND passing the identity check —
        # a second `run` on a dir a live engine already owns must NOT clobber config.snapshot.json /
        # task.snapshot.json. A later `resume` reads them, so a stale overwrite would re-enter the run
        # with the wrong settings/task.
        if finalization_pending:
            # The exact/scoped terminal boundary belongs to the ORIGINAL task/settings. Loading the
            # old snapshots before Engine construction prevents same-id changed flags from altering a
            # paid report/cost wrap-up. Missing or corrupt snapshots fail closed without rewriting them.
            engine_task, engine_settings = _pending_finalization_inputs(out, prior.task_id)
            eng = _engine(out, engine_task, engine_settings, crash_after=None, wrap_up_only=True)
            # No WIDTH preflight on this wrap-up branch — see `_preflight_settled_widths`.
            _preflight_speculation_authority(eng, prior_events)
        else:
            # Construction initializes roles/clients and can fail. Preserve the prior run's provenance
            # until the new Engine is viable; only then publish the new epoch's input snapshots.
            eng = _engine(
                out,
                task,
                settings,
                crash_after,
                # A `run` on a dir whose log already carries a wrap-up (a pending finalize) only
                # completes that boundary — `announce_wrap_up` below refuses to lift it — so the
                # endpoint preflight warns there instead of refusing, exactly as on the branch above.
                wrap_up_only=is_wrap_up(prior_kind),
                **({"speculation_gate_calibration": True}
                   if speculation_gate_calibration else {}),
            )
            # This existing prefix is still the authority until the new snapshots are published:
            # refuse a stale receipt, or a width this log never pinned, BEFORE the publish below.
            _preflight_speculation_authority(eng, prior_events)
            _preflight_settled_widths(eng, prior_events, surface="run")
            _publish_run_snapshots(out, task_dict, settings)
        # Continue a run dir that ALREADY FINISHED. Without this, re-entering the loop folds the log,
        # sees finished=True and breaks at once — printing the OLD best and doing no work. That silently
        # no-ops a re-run with a bigger --max-nodes, and (worse) makes a run that finished with
        # reason=error un-retryable: fixing the cause and re-running the same command does nothing.
        # Reopen it (the same event the Web UI/TUI append to continue a finished run) so the loop
        # processes the new budget / retries the failure, and SAY so — never silently no-op.
        # Both wrap-up states are RESPECTED, never lifted: let the loop finish the existing
        # boundary rather than reopening or resuming behind it.
        if announce_wrap_up(prior_kind):
            pass
        elif prior_kind == "finished":
            typer.echo(
                f"run dir {out} already finished"
                + (f" (reason={prior.stop_reason})" if prior.stop_reason else "")
                + " — reopening to continue with the current task/settings "
                  "(use a new --out for a fresh run).")
            eng.store.append(EV_RUN_REOPENED, {})
        elif prior_kind == "paused":
            # a STOPped (paused) run: the loop would fold paused=True and break at once, printing the
            # STALE best and doing no work — the exact silent no-op the finished branch guards against.
            # Lift the pause and continue, mirroring `resume` (whose paused branch appends EV_RESUME).
            typer.echo(f"run dir {out} is stopped — resuming to continue with the current task/settings.")
            eng.store.append(EV_RESUME, {})
        state = _run_engine_guarded(eng)
    _print_result(state)
    _note = wrap_up_degradation_note(eng)
    if _note:
        typer.echo(_note, err=True)
    _exit_nonzero_if_the_run_produced_nothing(state, out, wrap_up_only=is_wrap_up(prior_kind))


@app.command()
def resume(
    run_dir: Path = typer.Argument(..., help="Existing run directory to resume."),
    task_file: Optional[Path] = typer.Option(
        None, help="The task file used to start the run. Defaults to the run's task.snapshot.json."),
    max_nodes: Optional[int] = typer.Option(None),
):
    """Resume a crashed/incomplete run by re-entering the loop (replay-based)."""
    # `healthy=True` fails closed on a MID-FILE log corruption before re-entering the loop: iter_jsonl
    # stops at the first bad line, so a byte flipped mid-log (FUSE/NFS/S3 only) would replay just the
    # prefix, drop a valid tail, and — worse — resume would append MORE records behind the boundary (an
    # invisible tail). Refuse and direct to `repair-log` (P0-4); a torn TAIL (normal crash-mid-append)
    # is fine.
    entry_store = _require_run_dir(
        run_dir, healthy=True,
        hint="`resume` continues a run started by `looplab run`; use `run` to start one.")
    # Fall back to the verbatim task snapshot `run` wrote into the run dir, so a run can be resumed
    # from the dir alone (the UI relies on this to continue a finished run without ui_meta.json).
    snap = run_dir / "task.snapshot.json"
    if task_file is None:
        if not snap.exists():
            raise typer.BadParameter(
                "no --task-file given and no task.snapshot.json in the run dir")
        task_file = snap
    # `existing_run=True`: this run already has an event log, so a validation rule added since it
    # started must not be able to refuse it. The rules that CAN be waived this way report themselves
    # as warnings below instead — the diagnosis survives, only the refusal is dropped (invariant #6:
    # what the run recorded at `run_started` wins on resume).
    task = _load_task(task_file, existing_run=True)
    for _warn in eval_reader_path_errors(task):
        typer.echo(f"warning: {_warn}", err=True)
    # Restore the ORIGINAL run's settings from the snapshot `run` wrote — a fresh Settings()
    # would silently drop run-only flags (require_approval, trust_mode, confirm_*, eval_trust_mode,
    # backend, …), e.g. finishing a paused not-yet-approved run without any approval.
    settings = load_run_settings(run_dir, strict=True)
    # Settings.max_nodes carries ge=1, but assignment validation is disabled (flat-settings/snapshot
    # design), so a direct override would silently accept 0/negative and then append resume/reopen work
    # that can only finish immediately. Enforce the field's own floor here, before `_engine` and before
    # any lifecycle event (`run` validates the same 1<= bound on the fresh path).
    # `resume` is ALSO called directly as a plain function (tests/test_finalization_recovery.py,
    # tests/test_stop_finalize_resume.py), where an omitted `max_nodes` is Typer's `OptionInfo` sentinel
    # rather than None — so gate on "is a real int", not on "is not None". That keeps the sentinel from
    # being assigned as a bogus budget the way the previous `is not None` check did.
    if isinstance(max_nodes, int) and not isinstance(max_nodes, bool):
        if max_nodes < 1:
            raise typer.BadParameter("--max-nodes must be >= 1")
        settings.max_nodes = max_nodes
    # Continuing a STOPPED run: a `stop` (paused) or natural finish re-breaks on the first iteration
    # and does no work unless we LIFT it — so append the universal `resume` event (fold clears
    # paused + finished). BUT a pending FINALIZE (stop_requested set, not yet finished — e.g. the UI
    # appended run_abort then spawned us) must be RESPECTED: don't lift it, let the loop fold
    # stop_requested -> run_finished -> the wrap-up. This is why the UI's finalize path can spawn the
    # same `resume` command and still finalize.
    # A direct CLI can arrive while the old owner is in the post-run finalization tail: replay already
    # says finished/stopped, but engine.lock is still held. Returning there loses the user's continue
    # intent. Only that stopped-state handoff waits; a second CLI against an actively working run still
    # returns immediately. Re-fold between attempts so another owner that already lifted the gate wins.
    # Read through the store `_require_run_dir` already scanned for divergence (its consumed prefix is
    # cached); the engine does not exist yet, deliberately — see the block below.
    initial_events = entry_store.read_all()
    initial = fold(initial_events)
    wait_for_handoff = bool(
        initial.paused or initial.finished or initial.stop_requested
        or incomplete_finalize_scope(initial_events) is not None
        or initial.finalization_pending())
    # The handoff wait is DIAGNOSABLE, not silent. The old owner can be wedged — crashed while
    # holding engine.lock on a filesystem that doesn't release it, or stuck in a hung finalization
    # tail — and this loop would then spin forever with no output at all, so `resume` just looks
    # hung. Echo periodically while waiting, and give up with an actionable message rather than
    # blocking the operator's terminal indefinitely.
    _handoff_deadline = time.time() + _HANDOFF_WAIT_S
    _handoff_next_echo = time.time() + _HANDOFF_ECHO_EVERY_S
    while True:
        with _engine_singleton(run_dir) as ok:
            if ok:
                # Lifecycle mutation belongs under singleton ownership. A losing CLI in an old
                # engine's post-finish lock tail must not reopen the run without an owner.
                prior_events = entry_store.read_all()
                prior = fold(prior_events)
                prior_kind = classify_prior_run(prior, prior_events)
                # …and so does the REFUSE-VS-WARN decision that depends on it. `resume` is the one
                # entry point that is wrap-up-only SOMETIMES: it LIFTS a finished/paused run back
                # into the loop (new work — a dead endpoint must refuse), but on a wrap-up boundary
                # it may only complete the terminal already on disk (`announce_wrap_up` below
                # refuses to lift it). Both answers come from `prior_kind`, so they are read from
                # ONE fold, under the lock, and the engine is built here rather than before the
                # wait.
                #
                # Classifying earlier — before the singleton, before the handoff loop — is what the
                # split shipped with, and it was wrong in the one way that matters: the handoff wait
                # exists to let the PREVIOUS OWNER finish its wrap-up, which is precisely the
                # transition from a wrap-up boundary to a liftable one. Measured with two real
                # processes (a `finalize` holding the singleton, this command arriving ~1s later,
                # endpoint at a closed port): the entry fold said `pending_finalize`, so the probe
                # only WARNED; by the time the lock was free the run was plainly `finished`, so the
                # same command LIFTED it and ran the remaining budget as four identical `x=0,y=0`
                # fallback nodes with a flat metric and exit 0 — verbatim the fake success
                # `preflight_role_endpoints` exists to refuse. Nothing conflicting happened: the
                # previous owner simply did what this command waited for.
                #
                # Cost of building here: the endpoint probe (bounded by PREFLIGHT_TIMEOUT_S) now
                # runs while this process owns the singleton. That is the same window the run
                # itself owns it for, and a losing CLI already no-ops with a message.
                eng = _engine(run_dir, task, settings, crash_after=None,
                              wrap_up_only=is_wrap_up(prior_kind))
                # A control may have landed while this command waited for singleton ownership.
                # Re-authorize the exact prefix immediately before resume/resume_served can append.
                _preflight_speculation_authority(eng, prior_events)
                # …and settle the widths on the same terms, BEFORE the `resume` append below. A
                # refused width used to land that append first, so the "byte-unchanged" property held
                # only on a second, identical attempt — after the state worth lifting was gone.
                _preflight_settled_widths(eng, prior_events, surface="resume")
                # `resume` LIFTS both liftable states the same way; `run` reopens a finished dir
                # instead. That difference is real, so it stays here rather than in the classifier.
                if not announce_wrap_up(prior_kind) and prior_kind in ("finished", "paused"):
                    typer.echo(
                        f"run was {'finished' if prior_kind == 'finished' else 'stopped'} — "
                        "resuming to continue with the current settings")
                    eng.store.append(EV_RESUME, {})
                # P1-1: we hold the singleton lock and are about to drive the loop, so FULFILL any
                # outstanding durable resume intent. Seq-gated in the fold, so one serve satisfies
                # all piled-up requests; a no-op for a direct CLI resume (no intent recorded).
                if fold(eng.store.read_all()).resume_pending():
                    eng.store.append(EV_RESUME_SERVED, {})
                state = _run_engine_guarded(eng)
                break
        if not wait_for_handoff:
            typer.echo(f"engine already running on {run_dir} — not resuming a second loop")
            return
        current = fold(entry_store.read_all())
        if not (current.paused or current.finished or current.stop_requested):
            typer.echo(f"run {run_dir} was already resumed by the active engine")
            return
        now = time.time()
        if now >= _handoff_deadline:
            typer.echo(
                f"gave up after {_HANDOFF_WAIT_S:g}s waiting for the engine lock on {run_dir}; the "
                "previous owner still holds it (a wedged finalization tail, or a stale lock left by "
                "a crash). Check for a live engine process, then retry `looplab resume`.")
            raise typer.Exit(code=1)
        if now >= _handoff_next_echo:
            _handoff_next_echo = now + _HANDOFF_ECHO_EVERY_S
            typer.echo(f"waiting for the engine lock on {run_dir} "
                       f"({_handoff_deadline - now:.0f}s left) — the previous owner is finishing up")
        time.sleep(0.05)
    _print_result(state)
    _note = wrap_up_degradation_note(eng)
    if _note:
        typer.echo(_note, err=True)
    _exit_nonzero_if_the_run_produced_nothing(state, run_dir, wrap_up_only=is_wrap_up(prior_kind))


@app.command()
def stop(run_dir: Path = typer.Argument(..., help="Run directory to STOP (freeze, no finalize).")):
    """STOP a run: freeze it WITHOUT finalizing — no end-of-run report/lessons/cost roll-up. A running
    engine breaks on its next iteration; the run is resumable (`looplab resume`) or you can `finalize`
    it later to wrap it up."""
    # `healthy=True`: fail closed on a mid-file corruption before appending (P0-4).
    store = _require_run_dir(run_dir, healthy=True)
    store.append(EV_PAUSE, {})
    typer.echo(f"stopped {run_dir} (frozen, not finalized) — `looplab resume` to continue, "
               "`looplab finalize` to wrap it up")


@app.command()
def finalize(
    run_dir: Path = typer.Argument(..., help="Run directory to FINALIZE (stop + wrap up)."),
    task_file: Optional[Path] = typer.Option(
        None,
        help="Task file for wrap-up recovery. Defaults to the run's task.snapshot.json."),
):
    """FINALIZE a run: stop it AND run the end-of-run wrap-up (report, cross-run lessons/case, cost
    roll-up, tree.html). Works whether the run is live or already `stop`ped. Idempotent."""
    # `healthy=True`: fail closed on a mid-file corruption before appending (P0-4).
    store = _require_run_dir(run_dir, healthy=True)
    # Record exactly one stop intent. The server may already have appended it before spawning this
    # command; two direct CLIs can also race. A tail CAS makes both cases idempotent. A terminal run
    # whose current finish is only partially finalized repairs that finish first instead of creating
    # a spurious new stop/finish pair.
    while True:
        events = store.read_all()
        before = fold(events)
        wrap_up_incomplete = terminal_projection_incomplete(before, events)
        if (before.finished and not wrap_up_incomplete
                and not _pending_finalize(before) and not before.resume_pending()):
            # Already complete is a pure read/no-op. Appending a fresh run_abort here would leave a
            # misleading last_stop_request_seq newer than last_finish_seq and make a later raw resume
            # look like an unserved FINALIZE request.
            typer.echo(f"finalized {run_dir}")
            return
        if wrap_up_incomplete or _pending_finalize(before):
            break
        tail = events[-1].seq if events else -1
        try:
            store.append(
                EV_RUN_ABORT, {"reason": "finalized"}, expected_last_seq=tail)
            break
        except EventStoreConcurrencyError:
            continue
    # Command functions are also part of the Python compatibility surface (`looplab.cli.finalize(rd)`
    # in integrations/tests). In a direct call Typer leaves its OptionInfo default object in place;
    # only an actual Path is an explicit override.
    snap = task_file if isinstance(task_file, Path) else (run_dir / "task.snapshot.json")
    if not snap.exists():
        typer.echo(f"marked {run_dir} for finalize; a running engine will wrap it up "
                   f"(task file not found: {snap})")
        return
    settings = load_run_settings(run_dir, strict=True)
    # `wrap_up_only` unconditionally: this command's whole contract is "stop it AND wrap it up". It
    # has already appended the stop intent above, so the only move the loop below has left is to
    # emit the final report and finish — it cannot propose, so an unreachable endpoint is a warning
    # about which artifacts the wrap-up loses, not a reason to refuse the wrap-up. Before this, a run
    # whose endpoint had since died could not be finalized AT ALL, and the refusal blamed "empty
    # fallback proposals" on a run that will never propose again.
    eng = _engine(run_dir, _load_task(snap, existing_run=True), settings, crash_after=None,
                  wrap_up_only=True)
    _preflight_speculation_authority(eng)
    with _engine_singleton(run_dir) as ok:
        if not ok:
            typer.echo(f"engine already running on {run_dir} — it will finalize on its next iteration")
            return
        started = time.time()
        current_events = eng.store.read_all()
        _preflight_speculation_authority(eng, current_events)
        current = fold(current_events)

        # The server records a durable wake intent before spawning this process. Once singleton
        # ownership is ours, acknowledge it even when the run is already terminal; otherwise the
        # resume reconciler would keep treating the successfully handled request as a zombie.
        if current.resume_pending():
            eng.store.append(EV_RESUME_SERVED, {})
            current = fold(eng.store.read_all())

        # Crash-boundary repair: an accepted run_finished or any richer incomplete terminal scope
        # must be wrapped up even though Engine.run() would immediately hit the terminal gate. This
        # emits no resume event, so it cannot advance the search epoch or launch candidate work.
        current_events = eng.store.read_all()
        if terminal_projection_incomplete(current, current_events):
            finalize_run(eng, entry_finished=current.finished, start_time=started)
            current = fold(eng.store.read_all())

        if not current.finished:
            # Normal live/stopped path: the loop sees stop_requested at its first decision boundary,
            # emits the common final report + run_finished, and performs the durable wrap-up.
            _run_engine_guarded(eng)
    _note = wrap_up_degradation_note(eng)
    typer.echo(f"finalized {run_dir}" + (f" — {_note}" if _note else ""))


@app.command()
def approve(run_dir: Path = typer.Argument(..., help="Run dir awaiting approval."),
            node_id: Optional[int] = typer.Option(
                None, help="Node to approve (default: exact pending subject).")):
    """Approve a paused run (human-in-the-loop): ratify whatever it's waiting on — an agent-proposed
    eval spec, or the final-best node — by appending the matching event so `resume` can finish."""
    # `healthy=True`: fail closed on a mid-file corruption before appending (P0-4).
    store = _require_run_dir(run_dir, healthy=True)
    events = store.read_all()
    state = fold(events)
    expected_seq = events[-1].seq if events else -1

    def append_exact(event_type: str, data: dict) -> None:
        try:
            store.append(event_type, data, expected_last_seq=expected_seq)
        except EventStoreConcurrencyError:
            typer.echo(f"run {run_dir.name} changed while approval was being prepared; refresh and retry")
            raise typer.Exit(2)

    if (state.proposed_spec is not None and state.spec_approval_requested
            and not state.spec_confirmed):
        append_exact(EV_SPEC_APPROVED, {})       # ratify the agent-proposed eval/adapter
        typer.echo(f"approved eval spec for run {run_dir.name}")
        return
    if not state.awaiting_approval:
        typer.echo(f"run {run_dir.name} is not currently awaiting result approval")
        raise typer.Exit(2)
    nid = node_id if node_id is not None else state.approval_subject
    # Validate the target before appending: the fold honors a grant only for a REAL candidate node
    # (subject-bound approval, P0-2), so an explicit `--node-id` typo would append a grant the fold
    # silently ignores while this command printed "approved" — a confusing no-op. Fail loudly instead.
    if nid is not None and nid not in state.nodes:
        typer.echo(f"no node #{nid} in run {run_dir.name} — nothing approved "
                   f"(pass a real node id, or omit --node-id to approve the pending subject)")
        raise typer.Exit(2)
    # A malformed legacy request may be awaiting without a verifiable subject. Never guess best.
    if nid is None:
        typer.echo(f"run {run_dir.name} has no verifiable pending approval subject "
                   "(inspect its events, or pass an explicit --node-id).")
        raise typer.Exit(2)
    node = state.nodes[nid]
    if nid in state.aborted_nodes or node.tombstoned:
        typer.echo(f"node #{nid} in run {run_dir.name} is aborted — reset it before approval")
        raise typer.Exit(2)
    if node_id is None and (state.approval_generation is None
                            or state.approval_generation != node.attempt):
        typer.echo(f"run {run_dir.name} has a stale pending approval lifecycle; inspect events and retry")
        raise typer.Exit(2)
    append_exact(EV_APPROVAL_GRANTED, {"node_id": nid, "generation": node.attempt})
    typer.echo(f"approved node {nid} for run {run_dir.name}")


@app.command(name="repair-log")
def repair_log_cmd(run_dir: Path = typer.Argument(..., help="Run dir whose events.jsonl to repair.")):
    """Repair a MID-FILE corrupted event log (the FUSE/NFS/S3 case `run`/`resume` fail closed on).

    Backs up the original bytes to `events.jsonl.corrupt-<ts>.bak`, atomically truncates the log to
    its last valid boundary (the recoverable prefix replay already folds), and records the repair as a
    `log_repaired` event. The dropped tail is preserved in the backup for manual salvage. A torn final
    line (the normal crash-mid-append case) is not a corruption and needs no repair.

    Refuses while an engine is live on the run: repair replaces the log with a prefix of itself, and
    a running engine's appends are authoritative events that a replace would silently delete. Holding
    engine.lock for the repair also stops one from starting halfway through it."""
    from looplab.cli import _engine_singleton
    from looplab.events.eventstore import repair_log
    log = run_dir / "events.jsonl"
    # The one caller that must NOT pass `healthy=True`: a mid-file corruption is this command's INPUT,
    # so failing closed on it would make the repair unreachable exactly when it is needed.
    _require_run_dir(run_dir, hint="`repair-log` repairs a run's event log; there is none here.")
    with _engine_singleton(run_dir) as offline:
        if not offline:
            typer.echo(f"refusing to repair {log}: an engine is still running on this run "
                       "(engine.lock is held). Stop it, then repair.")
            raise typer.Exit(2)
        rec = repair_log(log)
    if not rec:
        typer.echo(f"{log} has no mid-file corruption — nothing to repair "
                   "(a torn final line is tolerated on read).")
        return
    typer.echo(
        f"repaired {log}: truncated at line {rec['corrupt_line']} "
        f"(kept {rec['good_records']} record(s), dropped {rec['dropped_lines']}). "
        f"Original backed up to {rec['backup']}. You can now `looplab resume {run_dir}`.")


@app.command()
def init(
    out: Path = typer.Option(Path("looplab.yaml"), help="Where to write the config template."),
    kind: str = typer.Option("dataset", help=f"Task kind to scaffold. One of: {', '.join(_TASK_KINDS)}."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file."),
):
    """Scaffold a documented config file (YAML) you can edit and `looplab run`.

    The template leads with the task and active values for the knobs most runs touch, then lists every
    remaining setting commented out at its default. Active values override matching environment
    variables. Run it with
    `looplab run looplab.yaml`."""
    if out.exists() and not force:
        typer.echo(f"{out} already exists (use --force to overwrite)")
        raise typer.Exit(1)
    if kind not in _TASK_KINDS:
        raise typer.BadParameter(f"unknown task kind {kind!r}; choose one of: {', '.join(_TASK_KINDS)}")
    atomic_write_text(out, appconfig.render_template(kind))
    typer.echo(f"wrote {out} — edit it, then: looplab run {out}")
