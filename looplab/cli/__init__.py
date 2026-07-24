"""Typer CLI (I0/I6, ADR-17): run / resume / inspect / replay.

The engine is a *process*, not a server (ADR-18): `LoopLab run task.json` spawns one
async orchestrator that drives the loop to completion (or crash). `resume` re-enters
the same run dir; `replay`/`inspect` are pure read-only folds of the event log.

Package layout (docs/15 §P5.2): `looplab.cli` is a PACKAGE, not the old flat module — the command
groups live in sibling modules (`run_cmds`, `export_cmds`, `inspect_cmds`, `ui_cmds`) imported at
the bottom so their `@app.command` registrations run. This `__init__` keeps the Typer `app`, the
shared builders (`_engine`, `_load_task`, `_engine_singleton`, …) and re-exports every command, so
`looplab.cli:app` (BOTH console scripts), `python -m looplab.cli` (via `__main__.py`) and every
`looplab.cli.<name>` attribute that tests import or monkeypatch keep resolving exactly as they did
when this was one file.
"""
from __future__ import annotations

import os
import sys
import copy
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import typer

from looplab import __version__
from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.engine.options import EngineOptions
from looplab.engine.orchestrator import (
    Engine,
    SPECULATION_CALIBRATION_PROFILE_SETTINGS,
)
from looplab.search.policy import make_policy
from looplab.search.speculation_calibration import speculation_runtime_scope_digest
from looplab.runtime.sandbox import make_sandbox
from looplab.adapters.tasks import TaskAdapter, kinds, load_task, make_llm_client, make_roles
from looplab.tools.vectorstore import make_embedder as _make_embedder
from looplab.adapters.tasks import _make_abstractor as _make_lesson_abstractor
_TASK_KINDS = tuple(kinds())


def _make_cli_streams_total() -> None:
    """Prevent help/error rendering from crashing on a legacy text encoding.

    Typer's Rich help writes directly to ``sys.stdout``/``sys.stderr``.  On Windows those streams can
    still be strict cp1252 even though our command descriptions legitimately contain arrows and other
    Unicode notation.  Keep the host's chosen encoding (so a legacy console does not receive forced
    UTF-8 bytes), but make unrepresentable glyphs degrade to a replacement instead of turning
    ``looplab --help`` into a traceback.  Redirected/captured streams without ``reconfigure`` are left
    untouched.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            # Embedders may expose a closed, detached or immutable stream.  CLI execution must remain
            # usable there; Click/Rich will apply whatever policy that host supplied.
            continue


class _TotalOutputTyper(typer.Typer):
    """Typer entry point that configures output only when the CLI is actually invoked."""

    def __call__(self, *args, **kwargs):
        _make_cli_streams_total()
        return super().__call__(*args, **kwargs)

# rich_markup_mode="markdown" (not the Typer default "rich"): in "rich" mode square brackets are
# parsed as console style tags, so help text like `pip install 'looplab[ui]'` silently renders as
# `pip install 'looplab'` — the [ui]/[dev]/[otel] extra names vanish. Markdown mode keeps the pretty
# help panels but treats brackets literally, so install hints stay correct.
app = _TotalOutputTyper(
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


def _truthy_env(name: str) -> bool:
    """A LOOPLAB_* deployment env var read as a boolean (1/true/yes/on, case-insensitive). Not a
    Settings field on purpose: it's a property of the FILESYSTEM/deployment, not the run, so it must
    NOT be snapshotted into run_started (a run moved to a lock-capable disk should re-evaluate)."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


@contextmanager
def _engine_singleton(run_dir: Path):
    """Hold an exclusive OS lock on <run_dir>/engine.lock for the engine's whole lifetime, so a second
    `run`/`resume` on the SAME dir can't spawn a concurrent loop (two engines folding+appending the one
    events.jsonl corrupts the log / double-spends the budget). The UI's agentic chat now auto-reopens+
    resumes a finished run per message, so two tabs acting at once is a real race this closes. Yields
    True when the lock was acquired (run), False when another engine already holds it (caller no-ops).
    The OS frees the lock when the process exits (even on crash), so there's no stale-lock problem.
    Where file locking is UNAVAILABLE (FUSE/S3 mounts) single-writer can't be enforced, so it fails
    CLOSED with an actionable error unless LOOPLAB_ALLOW_UNLOCKED_WRITER=1 opts into the risk."""
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
                except OSError as exc:
                    # flock UNSUPPORTED on this filesystem (FUSE/S3 like geesefs, some NFS) raises
                    # ENOTSUP/EINVAL — NOT a held lock, so single-writer CANNOT be enforced here. Two
                    # engines (or the UI server + engine) writing the one events.jsonl unlocked can
                    # interleave appends into a torn line / mint duplicate seq numbers and corrupt the
                    # log (P1-12, doc 17 §6.3). Fail CLOSED by default — but LOUDLY and ACTIONABLY, not
                    # the old silent phantom-"already running" exit that just failed closed with no
                    # explanation. A single operator on such a mount who guarantees one engine per run
                    # dir sets LOOPLAB_ALLOW_UNLOCKED_WRITER=1 to knowingly degrade to a no-op.
                    if _truthy_env("LOOPLAB_ALLOW_UNLOCKED_WRITER"):
                        pass   # explicit operator override -> single-writer assumption, run anyway
                    else:
                        acquired = False   # never acquired the lock -> skip the unlock in `finally`
                        raise RuntimeError(
                            f"Cannot enforce a single writer of the append-only event log: file "
                            f"locking is unavailable on the filesystem holding {run_dir}/engine.lock "
                            f"({type(exc).__name__}: {exc}). Two runs here could corrupt events.jsonl. "
                            f"Move the run dir to a local disk (see LOOPLAB_RUN_ROOT), or set "
                            f"LOOPLAB_ALLOW_UNLOCKED_WRITER=1 if you guarantee only one engine writes "
                            f"this run dir.") from exc
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


def _apply_speculation_calibration_profile(settings: Settings) -> None:
    """Force the source-owned offline measurement profile before any role/client is built."""
    for field, value in SPECULATION_CALIBRATION_PROFILE_SETTINGS.items():
        if field not in settings.__class__.model_fields:
            raise RuntimeError(f"calibration profile references unknown Settings field {field!r}")
        setattr(settings, field, copy.deepcopy(value))


def _make_calibration_roles(task: TaskAdapter, settings: Settings, run_dir: Path):
    researcher, developer = make_roles(task, settings, run_dir)
    # These flags are deliberately not Settings/env/UI knobs.  Engine validates their exact concrete
    # role types and values, including every pair returned by role_factory.
    setattr(researcher, "calibration_concepts", True)
    setattr(developer, "calibration_gpu_probe", True)
    return researcher, developer


def _engine(run_dir: Path, task: TaskAdapter, settings: Settings,
            crash_after: Optional[int], *, speculation_gate_calibration: bool = False) -> Engine:
    from looplab.core.tracing import set_llm_capture
    narrow_speculation_runtime = bool(
        speculation_gate_calibration or settings.speculation_gate_receipt)
    if narrow_speculation_runtime:
        # A public receipt authorizes only the offline source-owned profile that produced it. The
        # helper intentionally preserves max_nodes, treatment depth and receipt placement.
        _apply_speculation_calibration_profile(settings)
    if settings.speculation_gate_receipt:
        # Snapshots must retain the receipt's launch identity even when a later resume starts from a
        # different cwd. Engine repeats the normalization for direct library callers.
        settings.speculation_gate_receipt = str(
            Path(settings.speculation_gate_receipt).expanduser().resolve())
    # Capture LLM prompts/completions into spans (UI per-node trace) unless disabled. Diagnostics
    # only; never read by replay.fold. Honors LOOPLAB_TRACE_LLM_IO via Settings.
    set_llm_capture(settings.trace_llm_io)
    # The runtime-scope primitive is intentionally bounded to the calibration/public-receipt lane
    # (`max_nodes <= 64`).  Ordinary CLI runs may use the product's much larger node budgets and must
    # never cross this rollout-only validator.
    runtime_scope_sha256 = (
        speculation_runtime_scope_digest(settings.masked_snapshot())
        if narrow_speculation_runtime else None
    )
    role_builder = (_make_calibration_roles if narrow_speculation_runtime else make_roles)
    researcher, developer = role_builder(task, settings, run_dir)
    # Agentic-foresight tools: run-introspection (own experiments) + data facts, so the ranker can
    # PULL actual results before deciding. None when foresight_agentic is off -> the one-shot ranker.
    _ftools = None
    if getattr(settings, "foresight_agentic", True) and getattr(settings, "foresight", True):
        try:
            from looplab.agents.agent import CompositeTools
            from looplab.tools.run_tools import DataTools, RunTools
            _ftools = CompositeTools([RunTools(), DataTools(task)])
        except Exception:  # noqa: BLE001 — introspection tools are optional; degrade to one-shot ranking
            _ftools = None
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
        # ideas the numeric surrogate can't. ON by default for the LLM backend. Needs a client (a bare
        # surrogate wrapper exposes none -> falls through). YIELDS to an explicitly-configured numeric
        # `researcher_panel > 1` so opting into the k-NN panel is never silently overridden by the default.
        if (getattr(settings, "foresight", True) and settings.backend == "llm"
                and getattr(settings, "foresight_panel", 2) > 1
                and settings.researcher_panel <= 1
                and getattr(researcher, "client", None) is not None):
            from looplab.search.foresight import ForesightPanelResearcher
            researcher = ForesightPanelResearcher(
            researcher, k=settings.foresight_panel, tools=_ftools,
            min_confidence=getattr(settings, "foresight_min_confidence", 0.0),
            verify_score=getattr(settings, "foresight_verify", False),
            verify_samples=getattr(settings, "foresight_verify_samples", 3))
        # E2 researcher panel: generate K ideas and keep the best by the empirical surrogate.
        elif settings.researcher_panel > 1:
            from looplab.serve.panel import PanelResearcher
            researcher = PanelResearcher(researcher, k=settings.researcher_panel)
    elif (getattr(settings, "foresight", True) and getattr(settings, "foresight_panel", 2) > 1
          and settings.researcher_panel <= 1
          and getattr(researcher, "client", None) is not None):
        # Foresight in UNIFIED mode: the wrappers above are skipped because they'd re-wrap only the
        # researcher handle, but ForesightPanelResearcher now DELEGATES its whole developer surface to
        # the wrapped agent (__getattr__), so wrapping the single unified agent and using it for BOTH
        # handles keeps them identical — predict-before-execute + hypothesis-board prioritization work,
        # implement/repair pass straight through. (Numeric surrogate/panel stay researcher-only, so
        # they remain unified-skipped; only the client-based foresight is safe to share.)
        from looplab.search.foresight import ForesightPanelResearcher
        researcher = ForesightPanelResearcher(
            researcher, k=settings.foresight_panel, tools=_ftools,
            min_confidence=getattr(settings, "foresight_min_confidence", 0.0),
            verify_score=getattr(settings, "foresight_verify", False),
            verify_samples=getattr(settings, "foresight_verify_samples", 3))
        developer = researcher
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
        strat_client = (make_llm_client(settings, temperature=settings.strategist_temperature)
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
    # Deep-Research is researcher-flavored (breadth-seeking ideation), so it honors researcher_temperature.
    deep_researcher = (make_deep_researcher(
        settings, client=make_llm_client(settings, temperature=settings.researcher_temperature), task=task)
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
        sandbox=make_sandbox(settings.trust_mode, image=settings.docker_image,
                             mem=settings.sandbox_memory, cpus=settings.sandbox_cpus,
                             mem_local=settings.sandbox_memory_local,
                             fsize_local=settings.sandbox_fsize_local),
        policy=make_policy(settings.policy, n_seeds=settings.n_seeds,
                           max_nodes=settings.max_nodes, ablate_every=settings.ablate_every,
                           eta=settings.asha_eta,     # forwarded to ASHA (greedy/mcts/evo ignore it)
                           rung_nodes=settings.asha_rung_nodes,
                           debug_depth=settings.debug_depth,
                           operator_bandit=settings.operator_bandit),
        options=EngineOptions.from_settings(settings),
        crash_after=crash_after,
        # Maintainer-only bootstrap path for producing the paired evidence that the public positive
        # speculation path requires.  The CLI admits this flag only for a fresh, bounded offline
        # quadratic run with a real GPU requirement; Engine keeps the seam private so Settings,
        # snapshots, environment variables and the Web UI cannot turn it on accidentally.
        _speculation_gate_calibration=speculation_gate_calibration,
        _speculation_runtime_scope_sha256=runtime_scope_sha256,
        onboarder=onboarder,
        strategist=strategist,
        deep_researcher=deep_researcher,
        report_writer=report_writer,
        developer_factory=dev_factory,
        # a fresh wired pair per concurrent build prevents mutable role state from being
        # shared when the settled build width is >1 (canonical llm_parallel; legacy parallel_build).
        # This is LLM/build isolation, not proof of the later evaluation's CPU/GPU allocation.
        role_factory=(lambda: role_builder(task, settings, run_dir)),
        proxy_scorer=proxy_scorer,
        embedder=_make_embedder(settings),
        # Memora synergy: harmonic recall over the cross-run lessons tier (same abstractor Memora
        # uses for the case/KB index; shares its content-hash cache). None when memora is off; a
        # dead LLM endpoint degrades to the deterministic lexical abstractor inside make_abstractor.
        lesson_abstractor=_make_lesson_abstractor(settings),
    )


def _print_result(state) -> None:
    best = state.best()
    typer.echo(f"run={state.run_id} task={state.task_id} finished={state.finished}")
    typer.echo(f"nodes={len(state.nodes)} evaluated={len(state.evaluated_nodes())}")
    if best is not None:
        m = best.robust_metric
        ms = f"{m:.6g}" if m is not None else "n/a"
        typer.echo(f"BEST node {best.id}: metric={ms} params={best.idea.params}")


# Command registration: importing the command-group modules runs their `@app.command` decorators
# against the `app` above. This block MUST stay at the bottom — the groups import the shared
# builders back from this (still-initializing) package, which is safe only because everything they
# need is already defined by this point.
from looplab.cli import export_cmds, inspect_cmds, run_cmds, ui_cmds  # noqa: E402,F401

# Back-compat re-exports: when `looplab/cli.py` was one flat module, every command was an attribute
# of `looplab.cli` (tests call `cli.stop(...)`/`cli.finalize(...)` directly; tools import `app`).
# Keep that attribute surface identical after the package split.
from looplab.cli.run_cmds import approve, finalize, init, resume, run, stop  # noqa: E402,F401
from looplab.cli.export_cmds import (bench, export_mlflow, export_notebook,  # noqa: E402,F401
                                     harden, smoke)
from looplab.cli.inspect_cmds import inspect, replay, tensorboard, timings  # noqa: E402,F401
from looplab.cli.ui_cmds import build_ui, tui, ui  # noqa: E402,F401
