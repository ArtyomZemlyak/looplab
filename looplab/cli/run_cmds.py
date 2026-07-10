"""Run-lifecycle commands: `run` / `resume` / `stop` / `finalize` / `approve` / `init`.

Split verbatim out of the flat `looplab/cli.py` (docs/15 §P5.2). The shared builders
(`_engine_singleton`, `_load_task`, `_print_result`, …) live in the package `__init__`; the two
names tests monkeypatch on `looplab.cli` are late-bound below so the patch seam survives the split.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import anyio
import typer
from pydantic import ValidationError

from looplab.core.atomicio import atomic_write_text
from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.events.types import (EV_APPROVAL_GRANTED, EV_PAUSE, EV_RESUME, EV_RUN_ABORT,
                                  EV_RUN_FINISHED, EV_RUN_REOPENED, EV_SPEC_APPROVED)
from looplab.engine.orchestrator import Engine
from looplab.events.replay import fold
from looplab.adapters.tasks import validate_task
from looplab.core import appconfig
from looplab.cli import (_BACKENDS, _DEV_BACKENDS, _TASK_KINDS, _choice, _engine_singleton,
                         _load_task, _print_result, _require_run_dir, app)


def _engine(*args, **kwargs):
    """Late-bound through the package module: tests patch `looplab.cli._engine`
    (test_cli.py::_capture_backend) and the command bodies here must see that patch at call time —
    a plain `from looplab.cli import _engine` would freeze the pre-patch object into this module's
    globals when the package initializes."""
    from looplab import cli
    return cli._engine(*args, **kwargs)


def make_llm_client(*args, **kwargs):
    """Late-bound for the same monkeypatch seam as `_engine` above: test_cli.py patches
    `cli.make_llm_client` (×5) to stub the Genesis path offline, and `run` below must pick the
    patched object up at call time."""
    from looplab import cli
    return cli.make_llm_client(*args, **kwargs)


def _run_engine_guarded(eng: Engine):
    """Drive the engine loop to completion, funneling any fatal abort into a terminal event.
    Shared by `run` and `resume` (previously duplicated verbatim in both)."""
    try:
        return anyio.run(eng.run)
    except Exception as e:  # noqa: BLE001 - any fatal abort (e.g. an unreachable LLM endpoint
        # during implement/repair, a missing dep) must surface as a TERMINAL event, not a silent
        # stalled run the UI shows "thinking" forever. Mark finished-with-error, then re-raise so
        # the traceback still lands in engine.stderr.log. (A user Ctrl-C / cancel is BaseException,
        # not Exception, so an intentional stop stays resumable.)
        try:
            eng.store.append(EV_RUN_FINISHED, {"reason": "error", "error": str(e)[:500]})
        except Exception:  # noqa: BLE001 - best-effort; never mask the original failure
            pass
        raise


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
    require_approval: bool = typer.Option(False, help="HITL: pause for `approve` before finishing."),
    confirm_top_k: Optional[int] = typer.Option(None, help="Confirm top-k under multiple seeds."),
    confirm_seeds: Optional[int] = typer.Option(None, help="Seeds for the confirmation pass."),
    crash_after: Optional[int] = typer.Option(None, hidden=True,
                                              help="Test hook: hard-exit after N evals."),
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
                        ("confirm_seeds", confirm_seeds)):
        if value is not None:
            typed[name] = value
    if agent_surface is not None:
        typed["agent_surface"] = [g.strip() for g in agent_surface.split(",") if g.strip()]
    if require_approval:
        typed["require_approval"] = True
    try:
        sets = appconfig.parse_sets(set_)
    except ValueError as e:
        raise typer.BadParameter(str(e))
    try:
        settings = appconfig.build_settings(file_settings, typed, sets)
    except ValidationError as e:
        raise typer.BadParameter(f"invalid settings: {e}")
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
            client = make_llm_client(settings)
        except Exception as e:  # noqa: BLE001 - no endpoint configured/reachable
            raise typer.BadParameter(
                f"Genesis needs an LLM to author the task ({e}). Point LOOPLAB_LLM_BASE_URL/--model "
                f"at a reachable model, or use --no-genesis to build the task from --kind/--set alone.")
        # Pass the file's task: block (if any) as a draft so --goal refines it instead of discarding it.
        result = _genesis.author_task(goal, client=client, kinds=_TASK_KINDS, kind=kind, data=data,
                                      direction=direction, draft=(file_task or None),
                                      parser=settings.llm_parser)
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
        # UI's /api/start funnel, `serve/routers/control.py::_defaults_backend_llm`) — only the
        # CLI-surface `backend_chosen` detection above stays here.
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
    # Path sanity-check (esp. for Genesis-authored tasks): warn loudly when an input path doesn't
    # exist, so a mistyped/invented data/repo path is caught HERE — not as a cryptic mid-run
    # 'No such file or directory'. A warning (not a hard stop): a repo's setup step may create some
    # paths, and the user may know better. Use --no-genesis or fix the path to silence it.
    for field, p in _missing_task_paths(task_dict):
        typer.echo(f"⚠ task {field} does not exist on disk: {p}", err=True)
    out = out or (Path(file_out) if file_out else Path("runs/run_local"))
    out.mkdir(parents=True, exist_ok=True)
    eng = _engine(out, task, settings, crash_after)
    with _engine_singleton(out) as ok:
        if not ok:
            typer.echo(f"engine already running on {out} — not starting a second loop")
            return
        # Write the run snapshots only AFTER winning the singleton lock — a second `run` on a dir a
        # live engine already owns must NOT clobber config.snapshot.json / task.snapshot.json. A later
        # `resume` reads them, so a stale overwrite would re-enter the run with the wrong settings/task.
        atomic_write_text(out / "config.snapshot.json",
                          json.dumps(settings.masked_snapshot(), indent=2))
        # Self-describing run: write the RESOLVED task dict (after file + flags) as canonical JSON so
        # `resume` (CLI or UI) can re-enter the loop from the run dir alone — no need to remember the
        # original file, and it works for a unified config or a no-file --goal/--kind run too.
        try:
            atomic_write_text(out / "task.snapshot.json", json.dumps(task_dict, indent=2))
        except OSError:
            pass
        # Continue a run dir that ALREADY FINISHED. Without this, re-entering the loop folds the log,
        # sees finished=True and breaks at once — printing the OLD best and doing no work. That silently
        # no-ops a re-run with a bigger --max-nodes, and (worse) makes a run that finished with
        # reason=error un-retryable: fixing the cause and re-running the same command does nothing.
        # Reopen it (the same event the Web UI/TUI append to continue a finished run) so the loop
        # processes the new budget / retries the failure, and SAY so — never silently no-op.
        prior = fold(eng.store.read_all())
        if prior.finished:
            typer.echo(
                f"run dir {out} already finished"
                + (f" (reason={prior.stop_reason})" if prior.stop_reason else "")
                + " — reopening to continue with the current task/settings "
                  "(use a new --out for a fresh run).")
            eng.store.append(EV_RUN_REOPENED, {})
        state = _run_engine_guarded(eng)
    _print_result(state)


@app.command()
def resume(
    run_dir: Path = typer.Argument(..., help="Existing run directory to resume."),
    task_file: Optional[Path] = typer.Option(
        None, help="The task file used to start the run. Defaults to the run's task.snapshot.json."),
    max_nodes: Optional[int] = typer.Option(None),
):
    """Resume a crashed/incomplete run by re-entering the loop (replay-based)."""
    if not (run_dir / "events.jsonl").exists():
        typer.echo(f"no run found at {run_dir} (no events.jsonl). "
                   f"`resume` continues a run started by `looplab run`; use `run` to start one.")
        raise typer.Exit(2)
    # Surface a MID-FILE log corruption before re-entering the loop: iter_jsonl stops at the first bad
    # line, so a byte flipped mid-log (FUSE/NFS/S3 only) would silently replay just the prefix and drop
    # a valid tail. Warn the operator (the run still resumes from the recoverable prefix); a torn TAIL
    # (the normal crash-mid-append case) is not flagged.
    from looplab.events.eventstore import log_divergence
    _div = log_divergence(run_dir / "events.jsonl")
    if _div:
        typer.echo(f"WARNING: events.jsonl looks corrupted at line {_div['corrupt_line']} — "
                   f"{_div['dropped_lines']} later record(s) will be DROPPED on replay "
                   f"(only the first {_div['good_records']} fold). Back up the log before resuming if "
                   f"that tail matters.", err=True)
    # Fall back to the verbatim task snapshot `run` wrote into the run dir, so a run can be resumed
    # from the dir alone (the UI relies on this to continue a finished run without ui_meta.json).
    snap = run_dir / "task.snapshot.json"
    if task_file is None:
        if not snap.exists():
            raise typer.BadParameter(
                "no --task-file given and no task.snapshot.json in the run dir")
        task_file = snap
    task = _load_task(task_file)
    # Restore the ORIGINAL run's settings from the snapshot `run` wrote — a fresh Settings()
    # would silently drop run-only flags (require_approval, trust_mode, confirm_*, eval_trust_mode,
    # backend, …), e.g. finishing a paused not-yet-approved run without any approval.
    settings = Settings()
    snap = run_dir / "config.snapshot.json"
    if snap.exists():
        data = json.loads(snap.read_text(encoding="utf-8"))
        data.pop("llm_api_key", None)   # masked in the snapshot; re-read from env/default
        settings = Settings(**data)
    if max_nodes is not None:
        settings.max_nodes = max_nodes
    eng = _engine(run_dir, task, settings, crash_after=None)
    # Continuing a STOPPED run: a `stop` (paused) or natural finish re-breaks on the first iteration
    # and does no work unless we LIFT it — so append the universal `resume` event (fold clears
    # paused + finished). BUT a pending FINALIZE (stop_requested set, not yet finished — e.g. the UI
    # appended run_abort then spawned us) must be RESPECTED: don't lift it, let the loop fold
    # stop_requested -> run_finished -> the wrap-up. This is why the UI's finalize path can spawn the
    # same `resume` command and still finalize.
    prior = fold(eng.store.read_all())
    if prior.stop_requested and not prior.finished:
        typer.echo("run has a pending finalize — wrapping it up (report / cross-run lessons / cost)")
    elif prior.paused or prior.finished:
        typer.echo(f"run was {'finished' if prior.finished else 'stopped'} — resuming to continue "
                   "with the current settings")
        eng.store.append(EV_RESUME, {})
    with _engine_singleton(run_dir) as ok:
        if not ok:
            typer.echo(f"engine already running on {run_dir} — not resuming a second loop")
            return
        state = _run_engine_guarded(eng)
    _print_result(state)


@app.command()
def stop(run_dir: Path = typer.Argument(..., help="Run directory to STOP (freeze, no finalize).")):
    """STOP a run: freeze it WITHOUT finalizing — no end-of-run report/lessons/cost roll-up. A running
    engine breaks on its next iteration; the run is resumable (`looplab resume`) or you can `finalize`
    it later to wrap it up."""
    if not (run_dir / "events.jsonl").exists():
        typer.echo(f"no run found at {run_dir}")
        raise typer.Exit(2)
    EventStore(run_dir / "events.jsonl").append(EV_PAUSE, {})
    typer.echo(f"stopped {run_dir} (frozen, not finalized) — `looplab resume` to continue, "
               "`looplab finalize` to wrap it up")


@app.command()
def finalize(run_dir: Path = typer.Argument(..., help="Run directory to FINALIZE (stop + wrap up).")):
    """FINALIZE a run: stop it AND run the end-of-run wrap-up (report, cross-run lessons/case, cost
    roll-up, tree.html). Works whether the run is live or already `stop`ped. Idempotent."""
    if not (run_dir / "events.jsonl").exists():
        typer.echo(f"no run found at {run_dir}")
        raise typer.Exit(2)
    store = EventStore(run_dir / "events.jsonl")
    store.append(EV_RUN_ABORT, {"reason": "finalized"})
    # If no engine is driving the run, re-enter the loop ourselves so the wrap-up actually runs: the
    # loop folds, sees stop_requested, appends run_finished and finalizes (report/lessons/…), then exits.
    if fold(store.read_all()).finished:
        typer.echo(f"finalized {run_dir}")
        return
    snap = run_dir / "task.snapshot.json"
    if not snap.exists():
        typer.echo(f"marked {run_dir} for finalize; a running engine will wrap it up "
                   "(no task.snapshot.json here to drive the wrap-up directly)")
        return
    settings = Settings()
    csnap = run_dir / "config.snapshot.json"
    if csnap.exists():
        data = json.loads(csnap.read_text(encoding="utf-8"))
        data.pop("llm_api_key", None)
        settings = Settings(**data)
    eng = _engine(run_dir, _load_task(snap), settings, crash_after=None)
    with _engine_singleton(run_dir) as ok:
        if not ok:
            typer.echo(f"engine already running on {run_dir} — it will finalize on its next iteration")
            return
        _run_engine_guarded(eng)
    typer.echo(f"finalized {run_dir}")


@app.command()
def approve(run_dir: Path = typer.Argument(..., help="Run dir awaiting approval."),
            node_id: Optional[int] = typer.Option(None, help="Node to approve (default: best).")):
    """Approve a paused run (human-in-the-loop): ratify whatever it's waiting on — an agent-proposed
    eval spec, or the final-best node — by appending the matching event so `resume` can finish."""
    store = _require_run_dir(run_dir)
    state = fold(store.read_all())
    if state.proposed_spec is not None and not state.spec_confirmed:
        store.append(EV_SPEC_APPROVED, {})       # ratify the agent-proposed eval/adapter
        typer.echo(f"approved eval spec for run {run_dir.name}")
        return
    best = state.best()
    nid = node_id if node_id is not None else (best.id if best else None)
    store.append(EV_APPROVAL_GRANTED, {"node_id": nid})
    typer.echo(f"approved node {nid} for run {run_dir.name}")


@app.command()
def init(
    out: Path = typer.Option(Path("looplab.yaml"), help="Where to write the config template."),
    kind: str = typer.Option("dataset", help=f"Task kind to scaffold. One of: {', '.join(_TASK_KINDS)}."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file."),
):
    """Scaffold a documented config file (YAML) you can edit and `looplab run`.

    The template leads with the task and the knobs most runs touch (each commented), then lists every
    remaining setting at its default — so it doubles as living documentation. Run it with
    `looplab run looplab.yaml`."""
    if out.exists() and not force:
        typer.echo(f"{out} already exists (use --force to overwrite)"); raise typer.Exit(1)
    if kind not in _TASK_KINDS:
        raise typer.BadParameter(f"unknown task kind {kind!r}; choose one of: {', '.join(_TASK_KINDS)}")
    atomic_write_text(out, appconfig.render_template(kind))
    typer.echo(f"wrote {out} — edit it, then: looplab run {out}")
