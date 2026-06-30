"""Typer CLI (I0/I6, ADR-17): run / resume / inspect / replay.

The engine is a *process*, not a server (ADR-18): `LoopLab run task.json` spawns one
async orchestrator that drives the loop to completion (or crash). `resume` re-enters
the same run dir; `replay`/`inspect` are pure read-only folds of the event log.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import anyio
import orjson
import typer
from pydantic import ValidationError

from . import __version__
from .atomicio import atomic_write_text
from .config import Settings
from .eventstore import EventStore
from .orchestrator import Engine
from .policy import make_policy
from .replay import fold
from .sandbox import make_sandbox
from .tasks import TaskAdapter, kinds, load_task, make_llm_client, make_roles, validate_task
from . import appconfig

_TASK_KINDS = tuple(kinds())

# rich_markup_mode="markdown" (not the Typer default "rich"): in "rich" mode square brackets are
# parsed as console style tags, so help text like `pip install 'looplab[ui]'` silently renders as
# `pip install 'looplab'` — the [ui]/[dev]/[otel] extra names vanish. Markdown mode keeps the pretty
# help panels but treats brackets literally, so install hints stay correct.
app = typer.Typer(
    add_completion=False,
    rich_markup_mode="markdown",
    help="LoopLab — autonomous ML/DS research engine. Give it a goal; it invents -> implements -> "
         "tests -> improves candidate solutions in a loop and returns the best *verified* result.",
)

# Accepted choices for the role/developer backends, surfaced in errors so a typo gets a clear list
# instead of silently degrading (e.g. `--backend ll` would otherwise run the offline `toy` backend).
_BACKENDS = ("toy", "llm")
_DEV_BACKENDS = ("default", "opencode", "aider", "goose", "continue")


def _version_cb(value: bool) -> None:
    if value:
        typer.echo(f"LoopLab {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_cb, is_eager=True,
        help="Show the LoopLab version and exit."),
) -> None:
    """LoopLab CLI: run / resume / inspect / replay a research loop. See `looplab COMMAND --help`."""


def _choice(value: str, choices: tuple[str, ...], param: str) -> str:
    """Validate `value` against `choices`, raising a clear BadParameter (lists the valid options)
    instead of letting an unknown value silently fall through to a degraded default."""
    if value not in choices:
        raise typer.BadParameter(f"{value!r} is not valid for {param}; choose one of: "
                                 f"{', '.join(choices)}")
    return value


def _load_task(task_file: Path) -> TaskAdapter:
    """Load a task JSON with friendly CLI errors: a missing file or malformed/invalid task becomes a
    one-line BadParameter (exit 2), not a raw multi-frame Python traceback dumped at the user."""
    try:
        return load_task(task_file)
    except FileNotFoundError:
        raise typer.BadParameter(f"task file not found: {task_file}")
    except (ValueError, KeyError, TypeError) as e:
        raise typer.BadParameter(f"could not load task {task_file}: {e}")


def _require_run_dir(run_dir: Path) -> EventStore:
    """Open a run's event log, erroring clearly if the dir has none. Without this guard a typo'd path
    folds to an empty state and the read commands print a blank `run=` line with exit 0 — looking like
    a real but empty run rather than a wrong path."""
    if not (run_dir / "events.jsonl").exists():
        typer.echo(f"no run found at {run_dir} (no events.jsonl). "
                   f"Pass the run directory created by `looplab run --out <dir>`.")
        raise typer.Exit(2)
    return EventStore(run_dir / "events.jsonl")


@contextmanager
def _engine_singleton(run_dir: Path):
    """Hold an exclusive OS lock on <run_dir>/engine.lock for the engine's whole lifetime, so a second
    `run`/`resume` on the SAME dir can't spawn a concurrent loop (two engines folding+appending the one
    events.jsonl corrupts the log / double-spends the budget). The UI's agentic chat now auto-reopens+
    resumes a finished run per message, so two tabs acting at once is a real race this closes. Yields
    True when the lock was acquired (run), False when another engine already holds it (caller no-ops).
    The OS frees the lock when the process exits (even on crash), so there's no stale-lock problem.
    Degrades to a no-op (yields True) where file locking is unavailable."""
    run_dir.mkdir(parents=True, exist_ok=True)
    f = open(run_dir / "engine.lock", "a+")
    acquired = True
    try:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    acquired = False    # byte held by a live engine (Windows local FS supports locking)
            else:
                import fcntl
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    acquired = False    # genuinely HELD by a live engine (EWOULDBLOCK) -> caller no-ops
                except OSError:
                    # flock UNSUPPORTED on this filesystem (FUSE/S3 like geesefs, some NFS) raises
                    # ENOTSUP/EINVAL — NOT a held lock. Degrade to a no-op (acquired stays True) so the
                    # engine STILL RUNS, matching the docstring and server._engine_alive's fail-open.
                    # The old bare `except OSError: acquired = False` failed CLOSED here: on a JupyterHub
                    # home mounted via geesefs, every `run`/`resume` saw a phantom "already running" and
                    # silently exited. Locking just isn't available on such a mount; single-writer
                    # discipline (one engine per run dir) still holds in practice.
                    pass
        except OSError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        f.close()


def _engine(run_dir: Path, task: TaskAdapter, settings: Settings,
            crash_after: Optional[int]) -> Engine:
    from .tracing import set_llm_capture
    # Capture LLM prompts/completions into spans (UI per-node trace) unless disabled. Diagnostics
    # only; never read by replay.fold. Honors LOOPLAB_TRACE_LLM_IO via Settings.
    set_llm_capture(settings.trace_llm_io)
    researcher, developer = make_roles(task, settings, run_dir)
    # Unified mode: researcher IS developer (one agent). Skip the researcher-only wrappers
    # (surrogate/panel) — they would re-wrap `researcher` without re-wrapping `developer`, so the
    # two handles would diverge mid-run (R1). The unified agent owns its own ideation machinery.
    _unified = settings.unified_agent and settings.backend == "llm"
    if not _unified:
        # A2 surrogate-guided proposer: wrap the base Researcher when the task exposes numeric bounds
        # (it bootstraps via the wrapped Researcher and delegates on non-numeric spaces). A3 BOHB =
        # ASHA racing + the surrogate, so `policy=bohb` auto-enables it.
        if settings.surrogate_proposer or settings.policy == "bohb":
            from .surrogate import SurrogateResearcher
            _bounds = getattr(researcher, "bounds", None)
            if _bounds:
                researcher = SurrogateResearcher(_bounds, fallback=researcher,
                                                 explore=settings.surrogate_explore)
        # E2 researcher panel: generate K ideas and keep the best by the empirical surrogate.
        if settings.researcher_panel > 1:
            from .panel import PanelResearcher
            researcher = PanelResearcher(researcher, k=settings.researcher_panel)
    # RepoTask onboarding (Phase 3): if the task can propose its own eval spec, build the
    # onboarder (Researcher proposes + Developer writes the adapter).
    mk = getattr(task, "make_onboarder", None)
    onboarder = mk(settings) if callable(mk) else None
    # A7 Strategist (optional adaptive meta-control) + live Developer-backend swap factory.
    from .strategist import make_strategist
    from .tasks import make_developer_factory
    if _unified:
        # The unified agent IS the strategist: its `.decide()` delegates to the strategy-stage
        # backend it built internally (None when strategist_backend="off"). One identity, replay
        # path unchanged (the engine still records/replays `strategy_decision`).
        strategist = researcher
    else:
        strat_client = (make_llm_client(settings)
                        if settings.backend == "llm" and settings.strategist_backend == "llm" else None)
        strategist = make_strategist(settings, client=strat_client, n_seeds=settings.n_seeds)
    # Deep-Research stage (Phase 2): reachable only with an LLM backend. Reuses the run's LLM client;
    # tools (arXiv/web/knowledge) are wired from config inside make_deep_researcher. None when off.
    from .deep_research import make_deep_researcher
    deep_researcher = (make_deep_researcher(settings, client=make_llm_client(settings), task=task)
                       if settings.backend == "llm" else None)
    # Agent-authored run report (Workstream A): reachable only with an LLM backend; reuses the run's
    # LLM client. None in toy mode -> the UI shows the deterministic report only.
    from .report import make_report_writer
    report_writer = (make_report_writer(settings, client=make_llm_client(settings))
                     if settings.backend == "llm" else None)
    dev_factory = make_developer_factory(task, settings) if settings.backend == "llm" else None
    proxy_scorer = None
    if settings.proxy_scoring or settings.proxy_kill_fraction > 0:
        from .proxy import ProxyScorer
        proxy_scorer = ProxyScorer(kill_fraction=settings.proxy_kill_fraction)
    return Engine(
        run_dir,
        task=task,
        researcher=researcher,
        developer=developer,
        sandbox=make_sandbox(settings.trust_mode, image=settings.docker_image),
        policy=make_policy(settings.policy, n_seeds=settings.n_seeds,
                           max_nodes=settings.max_nodes, ablate_every=settings.ablate_every,
                           eta=settings.asha_eta,     # forwarded to ASHA (greedy/mcts/evo ignore it)
                           rung_nodes=settings.asha_rung_nodes),
        max_parallel=settings.max_parallel,
        timeout=settings.timeout,
        sweep_timeout_mult=settings.sweep_timeout_mult,
        crash_after=crash_after,
        confirm_top_k=settings.confirm_top_k,
        confirm_seeds=settings.confirm_seeds,
        max_seconds=settings.max_seconds,
        max_eval_seconds=settings.max_eval_seconds,
        memory_dir=settings.resolved_memory_dir(),
        require_approval=settings.require_approval,
        archive_resolution=settings.archive_resolution,
        onboarder=onboarder,
        eval_trust_mode=settings.eval_trust_mode,
        trust_mode=settings.trust_mode,
        docker_image=settings.docker_image,
        n_seeds=settings.n_seeds,
        max_nodes=settings.max_nodes,
        policy_name=settings.policy,
        ablate_every=settings.ablate_every,
        strategist=strategist,
        strategist_every=settings.strategist_every,
        deep_researcher=deep_researcher,
        deep_research_every=settings.deep_research_every,
        concurrent_research=settings.concurrent_research,
        report_writer=report_writer,
        report_every=settings.report_every,
        developer_factory=dev_factory,
        merge_mode=settings.merge_mode,
        complexity_cue=settings.complexity_cue,
        budget_aware=settings.budget_aware,
        failure_reflection=settings.failure_reflection,
        deep_repair=settings.deep_repair,
        inline_repair=settings.inline_repair,
        inline_repair_attempts=settings.inline_repair_attempts,
        inline_repair_reasons=settings.inline_repair_reasons,
        auto_install_deps=settings.auto_install_deps,
        dep_install_timeout=settings.dep_install_timeout,
        agent_control=settings.agent_control,
        localize_faults=settings.localize_faults,
        feature_engineering=settings.feature_engineering,
        ablate_code_blocks=settings.ablate_code_blocks,
        proxy_scorer=proxy_scorer,
        proxy_kill_fraction=settings.proxy_kill_fraction,
        reward_hack_detect=settings.reward_hack_detect,
        code_leakage_detect=settings.code_leakage_detect,
        critic_check=settings.critic_check,
        redact_output=settings.redact_output,
        novelty_gate=settings.novelty_gate,
        novelty_epsilon=settings.novelty_epsilon,
        reflection_priors=settings.reflection_priors,
        surrogate_explore=settings.surrogate_explore,
        unified_agent=settings.unified_agent,
        agent_drives_actions=settings.agent_drives_actions,
    )


def _print_result(state) -> None:
    best = state.best()
    typer.echo(f"run={state.run_id} task={state.task_id} finished={state.finished}")
    typer.echo(f"nodes={len(state.nodes)} evaluated={len(state.evaluated_nodes())}")
    if best is not None:
        m = best.confirmed_mean if best.confirmed_mean is not None else best.metric
        ms = f"{m:.6g}" if m is not None else "n/a"
        typer.echo(f"BEST node {best.id}: metric={ms} params={best.idea.params}")


@app.command()
def run(
    task_file: Optional[Path] = typer.Argument(
        None, help="Config or task file (YAML or JSON). A unified file has task:/settings:/out: keys; "
                   "a bare task file is just the task. Omit it and build the task from --goal/--kind."),
    goal: Optional[str] = typer.Option(None, help="Task goal in plain words (build a task with no file)."),
    kind: Optional[str] = typer.Option(None, help=f"Task kind (build a task with no file). One of: "
                                                  f"{', '.join(_TASK_KINDS)}."),
    direction: Optional[str] = typer.Option(None, help="Optimize: min | max."),
    data: Optional[str] = typer.Option(None, help="Path to your data (dataset) or repo (repo task)."),
    genesis: bool = typer.Option(
        True, "--genesis/--no-genesis",
        help="When you give --goal but no --kind, let the LLM infer the task kind from your words. "
             "--no-genesis falls back to the legacy default kind."),
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
    memory: Optional[bool] = typer.Option(
        None, "--memory/--no-memory", help="Cross-run case memory (learn across runs). Default on."),
    knowledge: Optional[bool] = typer.Option(
        None, "--knowledge/--no-knowledge", help="Knowledge base the agent can search + grow. Default on."),
    knowledge_dir: Optional[str] = typer.Option(None, help="Custom KB notes dir (default: <home_dir>/knowledge)."),
    memory_dir: Optional[str] = typer.Option(None, help="Custom cross-run memory dir (default: <home_dir>/memory)."),
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
    block and LOOPLAB_* env). Run `looplab init` to scaffold a documented config file."""
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
    task_dict = appconfig.apply_task_flags(
        file_task, kind=kind, goal=goal, direction=direction, data=data)
    # 3. Merge engine settings (file < typed flags < --set). Typed bool flags only override when set,
    # so a settings: file can still enable them.
    typed: dict = {}
    for name, value in (("max_nodes", max_nodes), ("backend", backend),
                        ("developer_backend", developer_backend), ("agent_cmd", agent_cmd),
                        ("validate_agent", validate_agent), ("agent_patch_gate", agent_patch_gate),
                        ("llm_model", model), ("knowledge_dir", knowledge_dir),
                        ("memory_enabled", memory), ("knowledge_enabled", knowledge),
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
    # 3b. Genesis: you described the goal in words but didn't name a kind — let the LLM infer it (the
    # headless counterpart of the UI's "New run"). Only fires on an explicit --goal with no kind, so
    # no file-based / legacy flow is affected.
    backend_chosen = backend is not None or "backend" in file_settings or "backend" in sets
    if genesis and goal is not None and "kind" not in task_dict:
        from . import genesis as _genesis
        try:
            client = make_llm_client(settings)
        except Exception as e:  # noqa: BLE001 - no endpoint configured/reachable
            raise typer.BadParameter(
                f"Genesis needs an LLM to infer the task kind ({e}). Either pass --kind explicitly, "
                f"or point LOOPLAB_LLM_BASE_URL/--model at a reachable model.")
        result = _genesis.author_task(goal, client=client, kinds=_TASK_KINDS, data=data,
                                      direction=direction, parser=settings.llm_parser)
        if not result.kind:
            typer.echo("Genesis couldn't infer the task from that goal. "
                       + (result.reply or "Add detail, or pass --kind explicitly."))
            raise typer.Exit(2)
        task_dict = result.task
        # A generative kind (the agent writes/edits code) implies an LLM-driven run; default the
        # backend to llm when the user didn't pick one. Offline-optimizable kinds keep their default.
        if not backend_chosen and result.kind in _genesis.GENERATIVE_KINDS:
            settings.backend = "llm"
        typer.echo(f"Genesis -> kind={result.kind}: {result.rationale or result.reply}".rstrip())
    # 4. Validate the resolved task, then resolve the run dir: explicit --out > file out: > default.
    if not task_dict:
        raise typer.BadParameter(
            "no task: pass a config/task file, or build one with --goal/--kind "
            "(scaffold one with `looplab init`).")
    try:
        task = validate_task(task_dict)
    except (ValueError, KeyError, TypeError) as e:
        raise typer.BadParameter(f"invalid task: {e}")
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
        try:
            state = anyio.run(eng.run)
        except Exception as e:  # noqa: BLE001 - any fatal abort (e.g. an unreachable LLM endpoint
            # during implement/repair, a missing dep) must surface as a TERMINAL event, not a silent
            # stalled run the UI shows "thinking" forever. Mark finished-with-error, then re-raise so
            # the traceback still lands in engine.stderr.log. (A user Ctrl-C / cancel is BaseException,
            # not Exception, so an intentional stop stays resumable.)
            try:
                eng.store.append("run_finished", {"reason": "error", "error": str(e)[:500]})
            except Exception:  # noqa: BLE001 - best-effort; never mask the original failure
                pass
            raise
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
    with _engine_singleton(run_dir) as ok:
        if not ok:
            typer.echo(f"engine already running on {run_dir} — not resuming a second loop")
            return
        try:
            state = anyio.run(eng.run)
        except Exception as e:  # noqa: BLE001 - any fatal abort (e.g. an unreachable LLM endpoint
            # during implement/repair, a missing dep) must surface as a TERMINAL event, not a silent
            # stalled run the UI shows "thinking" forever. Mark finished-with-error, then re-raise so
            # the traceback still lands in engine.stderr.log. (A user Ctrl-C / cancel is BaseException,
            # not Exception, so an intentional stop stays resumable.)
            try:
                eng.store.append("run_finished", {"reason": "error", "error": str(e)[:500]})
            except Exception:  # noqa: BLE001 - best-effort; never mask the original failure
                pass
            raise
    _print_result(state)


@app.command()
def smoke(model: Optional[str] = typer.Option(None, help="Override model id.")):
    """Ping the configured LLM endpoint to verify it's reachable and tool-calling works."""
    settings = Settings()
    if model is not None:
        settings.llm_model = model
    client = make_llm_client(settings)
    typer.echo(f"endpoint={settings.llm_base_url} model={settings.llm_model}")
    try:
        txt = client.complete_text([{"role": "user", "content": "Reply with one word: ready"}])
        typer.echo(f"text OK: {txt.strip()[:80]!r}")
    except Exception as e:  # noqa: BLE001
        typer.echo(f"text FAILED: {e}")
        raise typer.Exit(1)
    try:
        from .models import Idea
        from .parse import parse_structured
        idea = parse_structured(
            client,
            [{"role": "user", "content": "Propose params x=1.0, y=2.0 to try."}],
            Idea, settings.llm_parser,
        )
        typer.echo(f"structured OK: operator={idea.operator} params={idea.params}")
    except Exception as e:  # noqa: BLE001
        typer.echo(f"structured FAILED (will rely on fallback at runtime): {e}")


def _run_curator(goal: str, *, from_file: Optional[Path], web: Optional[bool],
                 model: Optional[str]) -> None:
    """Shared driver for `curate` / `remember`: build the curator from config and run one session,
    printing what it changed. Reuses the resolved memory + KB dirs (on by default — no path needed)."""
    settings = Settings()
    if model is not None:
        settings.llm_model = model
    if web is not None:
        settings.web_search = web
    context = ""
    if from_file is not None:
        if not from_file.exists():
            typer.echo(f"file not found: {from_file}")
            raise typer.Exit(2)
        context = from_file.read_text(encoding="utf-8", errors="replace")
    from .curator import make_curator
    try:
        client = make_llm_client(settings)
    except Exception as e:  # noqa: BLE001 - no endpoint configured/reachable
        typer.echo(f"curation needs a reachable LLM ({e}). Point --model / LOOPLAB_LLM_BASE_URL at one.")
        raise typer.Exit(1)
    curator = make_curator(settings, client=client)
    if curator is None:
        typer.echo("both memory and the knowledge base are disabled — nothing to curate "
                   "(enable with --memory / --knowledge or memory_enabled/knowledge_enabled).")
        raise typer.Exit(1)
    typer.echo(f"curating → memory={settings.resolved_memory_dir()} kb={settings.resolved_knowledge_dir()}")
    res = curator.run(goal, context=context)
    if not res.ok:
        typer.echo(f"curation failed: {res.error}")
        raise typer.Exit(1)
    typer.echo(res.summary or "(done)")
    for c in res.changes:
        typer.echo(f"  changed: {c}")
    for f in res.followups:
        typer.echo(f"  follow-up: {f}")


@app.command()
def curate(
    goal: str = typer.Argument(..., help="What to add/organize, e.g. 'research mixup augmentation "
                               "and add it to the KB' or 'consolidate this report into the KB'."),
    from_file: Optional[Path] = typer.Option(None, "--from", help="File whose contents to file/structure "
                                             "into the stores (e.g. a run report)."),
    web: Optional[bool] = typer.Option(None, "--web/--no-web", help="Allow web search while curating."),
    model: Optional[str] = typer.Option(None, help="Override LLM model id."),
):
    """Run a goal-driven curator session that reads, edits, and grows the markdown memory +
    knowledge base — a full agentic task, not a single write. Memory and KB are on by default and
    need no path; the agent surveys what exists, then files new material where it belongs."""
    _run_curator(goal, from_file=from_file, web=web, model=model)


@app.command()
def remember(
    lesson: str = typer.Argument(..., help="The lesson/mistake to record in cross-run memory."),
    model: Optional[str] = typer.Option(None, help="Override LLM model id."),
):
    """Record a dev-process lesson in cross-run memory via the curator (it places the note into the
    right topic file, extending an existing one instead of duplicating)."""
    _run_curator(
        f"Record this lesson in cross-run MEMORY (not the knowledge base). Read the existing memory "
        f"notes first and extend the most relevant one if it fits; otherwise file it under a sensible "
        f"topic. Lesson: {lesson}",
        from_file=None, web=False, model=model)


@app.command()
def approve(run_dir: Path = typer.Argument(..., help="Run dir awaiting approval."),
            node_id: Optional[int] = typer.Option(None, help="Node to approve (default: best).")):
    """Approve a paused run (human-in-the-loop): ratify whatever it's waiting on — an agent-proposed
    eval spec, or the final-best node — by appending the matching event so `resume` can finish."""
    store = _require_run_dir(run_dir)
    state = fold(store.read_all())
    if state.proposed_spec is not None and not state.spec_confirmed:
        store.append("spec_approved", {})       # ratify the agent-proposed eval/adapter
        typer.echo(f"approved eval spec for run {run_dir.name}")
        return
    best = state.best()
    nid = node_id if node_id is not None else (best.id if best else None)
    store.append("approval_granted", {"node_id": nid})
    typer.echo(f"approved node {nid} for run {run_dir.name}")


@app.command()
def bench(
    task_files: list[Path] = typer.Argument(..., help="Task JSON files to benchmark end-to-end."),
    out: Path = typer.Option(Path("runs/bench"), help="Output dir for the benchmark runs + report."),
    backend: str = typer.Option("toy", help="Role backend: toy | llm."),
    max_nodes: int = typer.Option(8, help="Node budget per task."),
):
    """Capability self-benchmark — run each task end-to-end and report best-metric / eval-seconds /
    reward-hack flags (a regression test for capability, not just code)."""
    _choice(backend, _BACKENDS, "--backend")
    for tf in task_files:
        if not tf.exists():
            raise typer.BadParameter(f"task file not found: {tf}")
    from .bench import run_benchmark
    settings = Settings()
    settings.backend = backend
    settings.max_nodes = max_nodes
    results = run_benchmark(task_files, settings, out)
    solved = sum(1 for r in results if r.get("finished") and r.get("best_metric") is not None)
    typer.echo(f"benchmark: {solved}/{len(results)} solved  (report: {out / 'benchmark.json'})")
    for r in results:
        if r.get("error"):
            typer.echo(f"  {r['task']}: ERROR {r['error']}")
        else:
            typer.echo(f"  {r['task']}: best={r['best_metric']} nodes={r['nodes']} "
                       f"eval_s={r['eval_seconds']} hacks={r['reward_hack_flags']}")


@app.command(name="export-mlflow")
def export_mlflow(
    run_dir: Path = typer.Argument(..., help="Run dir to export to MLflow."),
    tracking_uri: Optional[str] = typer.Option(None, help="MLflow tracking URI (default: local ./mlruns)."),
    experiment: Optional[str] = typer.Option(None, help="MLflow experiment name."),
):
    """Log the run's champion (params/metrics/solution) to MLflow (needs the optional mlflow pkg)."""
    _require_run_dir(run_dir)
    from .mlflow_export import available, export_run_dir
    if not available():
        typer.echo("MLflow not installed: pip install mlflow"); raise typer.Exit(1)
    rid = export_run_dir(run_dir, tracking_uri=tracking_uri, experiment=experiment)
    typer.echo(f"logged to MLflow run {rid}")


@app.command(name="export-notebook")
def export_notebook(
    run_dir: Path = typer.Argument(..., help="Run dir to export the champion from."),
    out: Optional[Path] = typer.Option(None, help="Output .ipynb path (default: <run>/champion.ipynb)."),
):
    """Export the run's champion solution as a runnable Jupyter notebook (.ipynb)."""
    from .notebook import champion_notebook
    store = _require_run_dir(run_dir)
    state = fold(store.read_all())
    champ = state.nodes.get(state.champion) if state.champion is not None else state.best()
    if champ is None:
        typer.echo("no champion/best node to export"); raise typer.Exit(1)
    nb = champion_notebook(state.goal, champ.code, params=champ.idea.params,
                           metric=(champ.confirmed_mean if champ.confirmed_mean is not None else champ.metric),
                           task_id=state.task_id, run_id=state.run_id)
    dest = out or (run_dir / "champion.ipynb")
    atomic_write_text(dest, json.dumps(nb, indent=1))
    typer.echo(f"wrote {dest}")


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


@app.command()
def replay(run_dir: Path = typer.Argument(...)):
    """Pure fold of the event log -> current state (read-only)."""
    store = _require_run_dir(run_dir)
    state = fold(store.read_all())
    typer.echo(orjson.dumps(state.model_dump(mode="json"),
                            option=orjson.OPT_INDENT_2).decode())


@app.command()
def inspect(run_dir: Path = typer.Argument(...)):
    """Show the resolved config snapshot + the run's best result."""
    store = _require_run_dir(run_dir)
    snap = run_dir / "config.snapshot.json"
    if snap.exists():
        typer.echo(snap.read_text(encoding="utf-8"))
    _print_result(fold(store.read_all()))


@app.command()
def ui(run_root: Path = typer.Option(
           Path(os.environ.get("LOOPLAB_RUN_ROOT", "runs")),
           help="Directory containing run subdirs. Defaults to $LOOPLAB_RUN_ROOT or ./runs — under "
                "JupyterHub set LOOPLAB_RUN_ROOT to a persistent home path (e.g. ~/looplab-runs) so "
                "runs survive a pod cull/restart instead of landing in an ephemeral CWD."),
       host: str = typer.Option("127.0.0.1", help="Bind host."),
       port: int = typer.Option(8765, help="Bind port."),
       root_path: str = typer.Option(
           "", help="ASGI root_path for a NON-prefix-stripping proxy (e.g. /user/<name>/proxy/8765). "
                    "Auto-derived from JUPYTERHUB_SERVICE_PREFIX when unset; harmless for a stripping "
                    "proxy. Lets `looplab ui` work behind both proxy styles without raw uvicorn."),
       build: bool = typer.Option(True, "--build/--no-build",
                                  help="Auto-build the React bundle if it's missing (needs Node/npm)."),
       rebuild: bool = typer.Option(False, "--rebuild",
                                    help="Force a fresh `npm run build` even if a bundle exists.")):
    """Serve the live React UI over the run dirs (needs the [ui] extra: pip install 'looplab[ui]').

    A separate read/control process (ADR-18): tails events.jsonl -> SSE, serves the built React
    app, and turns UI actions into appended control events. Does not change the engine.

    On launch the React bundle is built automatically when it's missing and Node/npm are on PATH,
    so a fresh `pip install -e ".[ui]"` needs no manual `npm run build`. Use --no-build to skip or
    --rebuild to force one."""
    if build or rebuild:
        from .uibuild import ensure_ui_built  # stdlib-only; fine to import before the [ui] check
        ensure_ui_built(force=rebuild, log=typer.echo)
    try:
        from .server import serve  # lazy: keeps the core import-free of fastapi/uvicorn
    except ModuleNotFoundError as e:
        typer.echo(f"UI extra not installed: {e}")
        raise typer.Exit(1)
    jh_prefix = os.environ.get("JUPYTERHUB_SERVICE_PREFIX")
    # Default root_path to the JH proxied prefix when unset, so `looplab ui` works behind a
    # NON-stripping proxy without dropping to raw uvicorn. A stripping proxy is unharmed: root_path
    # only affects URL generation, and the SPA derives its own prefix from the served page path.
    if not root_path and jh_prefix:
        root_path = f"{jh_prefix.rstrip('/')}/proxy/{port}"
    if jh_prefix:
        # Behind JupyterHub the UI is reached through jupyter-server-proxy at the user's service
        # prefix, NOT the bind address — advertising http://127.0.0.1:8765 would send the operator to
        # an unreachable URL. Point them at the proxied path instead.
        typer.echo(f"LoopLab UI — open it via your Jupyter proxy: "
                   f"{jh_prefix.rstrip('/')}/proxy/{port}/  (run-root={run_root})")
    else:
        typer.echo(f"LoopLab UI on http://{host}:{port}  (run-root={run_root})")
    serve(run_root, host=host, port=port, root_path=root_path)


@app.command("build-ui")
def build_ui(force: bool = typer.Option(False, "--force",
                                        help="Rebuild even if a bundle already exists.")):
    """Build the React UI bundle (ui/dist) so `looplab ui` can serve it.

    Runs `npm ci` (first build) + `npm run build` in the UI source tree. Normally you don't need
    this — `looplab ui` builds on demand — but it's handy for CI or a warm-up step."""
    from .uibuild import ensure_ui_built, ui_dist_dir
    ok = ensure_ui_built(force=force, log=typer.echo)
    if ok:
        typer.echo(f"UI bundle ready at {ui_dist_dir()}")
    else:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
