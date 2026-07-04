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

from looplab import __version__
from looplab.core.atomicio import atomic_write_text
from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.events.types import (EV_APPROVAL_GRANTED, EV_RUN_FINISHED, EV_RUN_REOPENED,
                                  EV_SPEC_APPROVED)
from looplab.engine.options import EngineOptions
from looplab.engine.orchestrator import Engine
from looplab.search.policy import make_policy
from looplab.events.replay import fold
from looplab.runtime.sandbox import make_sandbox
from looplab.adapters.tasks import TaskAdapter, kinds, load_task, make_llm_client, make_roles, validate_task
from looplab.tools.vectorstore import make_embedder as _make_embedder
from looplab.adapters.tasks import _make_abstractor as _make_lesson_abstractor
from looplab.core import appconfig
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


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_cb, is_eager=True,
        help="Show the LoopLab version and exit."),
) -> None:
    """LoopLab CLI: run / resume / inspect / replay a research loop. See `looplab COMMAND --help`.

    Run bare `looplab` (no command) to open the terminal control plane (the `tui` command) — a
    chat-first dashboard to start, watch and steer runs."""
    if ctx.invoked_subcommand is None:
        # Bare `looplab` -> the terminal control plane. `looplab --help` / `--version` are handled by
        # Typer's eager options before we get here, so this fires only on a genuine no-arg invocation.
        from looplab.serve.tui import main as tui_main
        raise typer.Exit(tui_main(None, os.environ.get("LOOPLAB_RUN_ROOT", "runs")))


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
    from looplab.core.tracing import set_llm_capture
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
            from looplab.search.surrogate import SurrogateResearcher
            _bounds = getattr(researcher, "bounds", None)
            if _bounds:
                researcher = SurrogateResearcher(_bounds, fallback=researcher,
                                                 explore=settings.surrogate_explore)
        # FOREAGENT predict-before-execute for HYPOTHESES: rank K candidate ideas with the LLM world
        # model primed with the data profile + experiment memory — it compares the structural / text
        # ideas the numeric surrogate can't. ON by default for the LLM backend; takes precedence over
        # the numeric E2 panel. Needs a client (a bare surrogate wrapper exposes none -> falls through).
        if (getattr(settings, "foresight", True) and settings.backend == "llm"
                and getattr(settings, "foresight_panel", 1) > 1
                and getattr(researcher, "client", None) is not None):
            from looplab.search.foresight import ForesightPanelResearcher
            researcher = ForesightPanelResearcher(researcher, k=settings.foresight_panel)
        # E2 researcher panel: generate K ideas and keep the best by the empirical surrogate.
        elif settings.researcher_panel > 1:
            from looplab.serve.panel import PanelResearcher
            researcher = PanelResearcher(researcher, k=settings.researcher_panel)
    # RepoTask onboarding (Phase 3): if the task can propose its own eval spec, build the
    # onboarder (Researcher proposes + Developer writes the adapter).
    mk = getattr(task, "make_onboarder", None)
    onboarder = mk(settings) if callable(mk) else None
    # A7 Strategist (optional adaptive meta-control) + live Developer-backend swap factory.
    from looplab.agents.strategist import make_strategist
    from looplab.adapters.tasks import make_developer_factory
    if _unified:
        # The unified agent IS the strategist: its `.decide()` delegates to the strategy-stage
        # backend it built internally (None when strategist_backend="off"). One identity, replay
        # path unchanged (the engine still records/replays `strategy_decision`).
        strategist = researcher
    else:
        strat_client = (make_llm_client(settings)
                        if settings.backend == "llm" and settings.strategist_backend in ("llm", "agent")
                        else None)
        from looplab.adapters.tasks import build_strategist_tools
        # Only build the (KB-indexing) strategist toolset when an agent strategist will actually use
        # it — i.e. a client is wired. Without one, make_strategist falls back to RuleStrategist and
        # the toolset would be built (paying KnowledgeTools' vector-index cost) only to be discarded.
        strat_tools = (build_strategist_tools(task, settings, run_dir)
                       if strat_client is not None and settings.strategist_backend == "agent" else None)
        strategist = make_strategist(settings, client=strat_client, n_seeds=settings.n_seeds,
                                     tools=strat_tools)
    # Deep-Research stage (Phase 2): reachable only with an LLM backend. Reuses the run's LLM client;
    # tools (arXiv/web/knowledge) are wired from config inside make_deep_researcher. None when off.
    from looplab.agents.deep_research import make_deep_researcher
    deep_researcher = (make_deep_researcher(settings, client=make_llm_client(settings), task=task)
                       if settings.backend == "llm" else None)
    # Agent-authored run report (Workstream A): reachable only with an LLM backend; reuses the run's
    # LLM client. None in toy mode -> the UI shows the deterministic report only.
    from looplab.serve.report import make_report_writer
    report_writer = (make_report_writer(settings, client=make_llm_client(settings))
                     if settings.backend == "llm" else None)
    dev_factory = make_developer_factory(task, settings) if settings.backend == "llm" else None
    proxy_scorer = None
    if settings.proxy_scoring or settings.proxy_kill_fraction > 0:
        from looplab.runtime.proxy import ProxyScorer
        proxy_scorer = ProxyScorer(kill_fraction=settings.proxy_kill_fraction)
    # Every pure-config Settings→Engine knob travels as ONE bundle (BACKLOG §4); only the built
    # OBJECTS (roles, sandbox, policy, strategist, scorers, …) and genuinely CLI-specific values
    # (crash_after comes from a CLI flag, not Settings) remain explicit kwargs.
    return Engine(
        run_dir,
        task=task,
        researcher=researcher,
        developer=developer,
        sandbox=make_sandbox(settings.trust_mode, image=settings.docker_image),
        policy=make_policy(settings.policy, n_seeds=settings.n_seeds,
                           max_nodes=settings.max_nodes, ablate_every=settings.ablate_every,
                           eta=settings.asha_eta,     # forwarded to ASHA (greedy/mcts/evo ignore it)
                           rung_nodes=settings.asha_rung_nodes,
                           debug_depth=settings.debug_depth,
                           operator_bandit=settings.operator_bandit),
        options=EngineOptions.from_settings(settings),
        crash_after=crash_after,
        onboarder=onboarder,
        strategist=strategist,
        deep_researcher=deep_researcher,
        report_writer=report_writer,
        developer_factory=dev_factory,
        proxy_scorer=proxy_scorer,
        embedder=_make_embedder(settings),
        # Memora synergy: harmonic recall over the cross-run lessons tier (same abstractor Memora
        # uses for the case/KB index; shares its content-hash cache). None when memora is off; a
        # dead LLM endpoint degrades to the deterministic lexical abstractor inside make_abstractor.
        lesson_abstractor=_make_lesson_abstractor(settings),
    )


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
    for key in ("data_path", "editable_path"):
        if task_dict.get(key):
            candidates.append((key, task_dict[key]))
    data = task_dict.get("data")
    if isinstance(data, dict):
        candidates += [(f"data.{k}", v) for k, v in data.items() if v]
    elif isinstance(data, str) and data:
        candidates.append(("data", data))
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
        if not backend_chosen and result.kind in _genesis.GENERATIVE_KINDS:
            settings.backend = "llm"
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
        state = _run_engine_guarded(eng)
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
        from looplab.core.models import Idea
        from looplab.core.parse import parse_structured
        idea = parse_structured(
            client,
            [{"role": "user", "content": "Propose params x=1.0, y=2.0 to try."}],
            Idea, settings.llm_parser,
        )
        typer.echo(f"structured OK: operator={idea.operator} params={idea.params}")
    except Exception as e:  # noqa: BLE001
        typer.echo(f"structured FAILED (will rely on fallback at runtime): {e}")


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
    from looplab.bench import run_benchmark
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
    from looplab.events.mlflow_export import available, export_run_dir
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
    from looplab.runtime.notebook import champion_notebook
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
def harden(
    memory_dir: Path = typer.Argument(..., help="Memory dir; the exploit suite lives at "
                                                "<memory_dir>/exploits.jsonl."),
    rounds: int = typer.Option(1, help="Hacker/fixer iterations."),
):
    """4.3 · Harden the reward-hack evaluator via a hacker-fixer-solver loop (arXiv:2606.08960).

    Grows a persisted exploit ruleset: a hacker proposes eval exploits, a fixer turns each one the
    current detector MISSES into a durable regex, and a solver guardrail rejects any rule that would
    flag an honest solution. Every future run with this memory_dir + reward_hack_detect loads the
    suite, so each discovered exploit stays guarded. Deterministic seed corpus (offline); no model."""
    from looplab.trust.harden import ExploitSuite, harden as _harden
    path = memory_dir / "exploits.jsonl"
    memory_dir.mkdir(parents=True, exist_ok=True)
    suite = ExploitSuite.load(path)
    # Honest baselines the solver guardrail protects (a fix must never flag these).
    legit = [
        "import json\nimport numpy as np\nX=json.load(open('train.json'))['X']\n"
        "pred=[0]*len(X)\njson.dump(pred, open('predictions.json','w'))",
        "from sklearn.ensemble import RandomForestClassifier\nm=RandomForestClassifier().fit(Xtr,ytr)\n"
        "json.dump(m.predict(Xte).tolist(), open('predictions.json','w'))",
    ]
    res = _harden(suite, legit_solutions=legit, rounds=rounds)
    suite.save(path)
    typer.echo(f"hardened: +{len(res['added'])} rules ({', '.join(res['added']) or 'none new'}); "
               f"caught={res['caught']} escaped={res['escaped']} "
               f"blocked_legit={len(res['blocked_legit'])}; suite now {len(suite.patterns)} rules "
               f"at {path}")


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
    snap = run_dir / "config.snapshot.json"
    events = run_dir / "events.jsonl"
    # Tolerate a run that crashed after writing config.snapshot.json but before its first event: still
    # show the config. Only error when the dir is neither — a typo'd path, not a real (if partial) run.
    if not snap.exists() and not events.exists():
        typer.echo(f"no run found at {run_dir} (no config.snapshot.json or events.jsonl).")
        raise typer.Exit(2)
    if snap.exists():
        typer.echo(snap.read_text(encoding="utf-8"))
    if events.exists():
        _print_result(fold(EventStore(events).read_all()))


@app.command()
def tensorboard(
    run_dir: Path = typer.Argument(..., help="Run dir; its nodes/ hold each experiment's training logs."),
    port: int = typer.Option(6006, help="Port to serve on."),
    host: str = typer.Option("0.0.0.0", help="Bind address."),
):
    """Serve TensorBoard over a run's per-node training logs — online curves for ALL metrics the
    training framework logged (loss, recall@k, grad norms, lr, …), one comparable run per experiment.
    RepoTask training scripts (e.g. PyTorch Lightning's TensorBoardLogger) write event files under each
    node's workdir; this points TensorBoard at nodes/ so every node shows up."""
    import shutil
    import subprocess
    import sys
    logdir = run_dir / "nodes"
    if not logdir.exists():
        logdir = run_dir
    exe = shutil.which("tensorboard")
    cmd = ([exe] if exe else [sys.executable, "-m", "tensorboard.main"]) + \
          ["--logdir", str(logdir), "--port", str(port), "--host", host]
    typer.echo(f"Serving TensorBoard for {run_dir} on http://{host}:{port}  (logdir={logdir})")
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass


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
        from looplab.serve.uibuild import ensure_ui_built  # stdlib-only; fine to import before the [ui] check
        ensure_ui_built(force=rebuild, log=typer.echo)
    try:
        from looplab.serve.server import serve  # lazy: keeps the core import-free of fastapi/uvicorn
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


@app.command()
def tui(server: Optional[str] = typer.Option(
            None, help="URL of a running LoopLab UI server (e.g. http://127.0.0.1:8765). When omitted, "
                       "reuses a local server if one is up, else auto-launches one (needs the [ui] extra)."),
        run_root: Path = typer.Option(
            Path(os.environ.get("LOOPLAB_RUN_ROOT", "runs")),
            help="Directory of run subdirs — used only when auto-launching a server. Defaults to "
                 "$LOOPLAB_RUN_ROOT or ./runs.")):
    """Drive LoopLab from the terminal: a chat-first TUI to start runs, watch what's running, and steer
    the boss — the most-used slice of the web UI, no browser needed.

    Describe a goal and the boss plans + launches a run; pick a running experiment to see its status at a
    glance and chat with the boss to change course (its actions apply to the live run). It is a thin
    client of the same control plane `looplab ui` serves, so a server is auto-started when none is found
    (API only — no React build); point it at a remote one with --server."""
    from looplab.serve.tui import main as tui_main
    raise typer.Exit(tui_main(server, str(run_root)))


@app.command("build-ui")
def build_ui(force: bool = typer.Option(False, "--force",
                                        help="Rebuild even if a bundle already exists.")):
    """Build the React UI bundle (ui/dist) so `looplab ui` can serve it.

    Runs `npm ci` (first build) + `npm run build` in the UI source tree. Normally you don't need
    this — `looplab ui` builds on demand — but it's handy for CI or a warm-up step."""
    from looplab.serve.uibuild import ensure_ui_built, ui_dist_dir
    ok = ensure_ui_built(force=force, log=typer.echo)
    if ok:
        typer.echo(f"UI bundle ready at {ui_dist_dir()}")
    else:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
