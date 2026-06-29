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

from .atomicio import atomic_write_text
from .config import Settings
from .eventstore import EventStore
from .orchestrator import Engine
from .policy import make_policy
from .replay import fold
from .sandbox import make_sandbox
from .tasks import TaskAdapter, load_task, make_llm_client, make_roles

app = typer.Typer(add_completion=False, help="LoopLab — autonomous ML research engine (P0).")


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
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            acquired = False        # another engine holds it -> caller will skip running
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
        memory_dir=settings.memory_dir,
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
    task_file: Path = typer.Argument(..., help="Path to a toy task JSON file."),
    out: Path = typer.Option(Path("runs/run_local"), help="Run directory."),
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
    """Start a new run (or continue if the run dir already has events)."""
    task = load_task(task_file)
    settings = Settings()
    if max_nodes is not None:
        settings.max_nodes = max_nodes
    if backend is not None:
        settings.backend = backend
    if developer_backend is not None:
        settings.developer_backend = developer_backend
    if agent_cmd is not None:
        settings.agent_cmd = agent_cmd
    if validate_agent is not None:
        settings.validate_agent = validate_agent
    if agent_patch_gate is not None:
        settings.agent_patch_gate = agent_patch_gate
    if agent_surface is not None:
        settings.agent_surface = [g.strip() for g in agent_surface.split(",") if g.strip()]
    if model is not None:
        settings.llm_model = model
    if knowledge_dir is not None:
        settings.knowledge_dir = knowledge_dir
    if memory_dir is not None:
        settings.memory_dir = memory_dir
    if max_seconds is not None:
        settings.max_seconds = max_seconds
    if ablate_every is not None:
        settings.ablate_every = ablate_every
    if require_approval:
        settings.require_approval = True
    if confirm_top_k is not None:
        settings.confirm_top_k = confirm_top_k
    if confirm_seeds is not None:
        settings.confirm_seeds = confirm_seeds
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
        # Self-describing run: keep a verbatim copy of the task so `resume` (CLI or UI) can re-enter the
        # loop from the run dir alone — no need to remember the original task-file path.
        try:
            atomic_write_text(out / "task.snapshot.json",
                              Path(task_file).read_text(encoding="utf-8"))
        except OSError:
            pass
        state = anyio.run(eng.run)
    _print_result(state)


@app.command()
def resume(
    run_dir: Path = typer.Argument(..., help="Existing run directory to resume."),
    task_file: Optional[Path] = typer.Option(
        None, help="The task file used to start the run. Defaults to the run's task.snapshot.json."),
    max_nodes: Optional[int] = typer.Option(None),
):
    """Resume a crashed/incomplete run by re-entering the loop (replay-based)."""
    # Fall back to the verbatim task snapshot `run` wrote into the run dir, so a run can be resumed
    # from the dir alone (the UI relies on this to continue a finished run without ui_meta.json).
    snap = run_dir / "task.snapshot.json"
    if task_file is None:
        if not snap.exists():
            raise typer.BadParameter(
                "no --task-file given and no task.snapshot.json in the run dir")
        task_file = snap
    task = load_task(task_file)
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
        state = anyio.run(eng.run)
    _print_result(state)


@app.command()
def smoke(model: Optional[str] = typer.Option(None, help="Override model id.")):
    """Ping the configured LLM endpoint (a startup self-test, per ADR-11)."""
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


@app.command()
def approve(run_dir: Path = typer.Argument(..., help="Run dir awaiting approval."),
            node_id: Optional[int] = typer.Option(None, help="Node to approve (default: best).")):
    """HITL: ratify whatever the paused run is waiting on — an onboarding eval spec (Phase 3)
    or the final-best node (I21) — by appending the matching event so resume continues."""
    store = EventStore(run_dir / "events.jsonl")
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
    """D2: capability self-benchmark — run each task e2e and report best-metric / eval-seconds /
    reward-hack flags (a regression test for capability, not just code)."""
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
    """G5: log the run's champion (params/metrics/solution) to MLflow (needs the optional mlflow pkg)."""
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
    """I4: export the run's champion solution as a runnable Jupyter notebook (.ipynb)."""
    from .notebook import champion_notebook
    store = EventStore(run_dir / "events.jsonl")
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
def replay(run_dir: Path = typer.Argument(...)):
    """Pure fold of the event log -> current state (read-only)."""
    store = EventStore(run_dir / "events.jsonl")
    state = fold(store.read_all())
    typer.echo(orjson.dumps(state.model_dump(mode="json"),
                            option=orjson.OPT_INDENT_2).decode())


@app.command()
def inspect(run_dir: Path = typer.Argument(...)):
    """Show the resolved config snapshot + the run's best result."""
    snap = run_dir / "config.snapshot.json"
    if snap.exists():
        typer.echo(snap.read_text(encoding="utf-8"))
    store = EventStore(run_dir / "events.jsonl")
    _print_result(fold(store.read_all()))


@app.command()
def ui(run_root: Path = typer.Option(Path("runs"), help="Directory containing run subdirs."),
       host: str = typer.Option("127.0.0.1", help="Bind host."),
       port: int = typer.Option(8765, help="Bind port.")):
    """Serve the live React UI over the run dirs (needs the [ui] extra: pip install 'looplab[ui]').

    A separate read/control process (ADR-18): tails events.jsonl -> SSE, serves the built React
    app, and turns UI actions into appended control events. Does not change the engine."""
    try:
        from .server import serve  # lazy: keeps the core import-free of fastapi/uvicorn
    except ModuleNotFoundError as e:
        typer.echo(f"UI extra not installed: {e}")
        raise typer.Exit(1)
    typer.echo(f"LoopLab UI on http://{host}:{port}  (run-root={run_root})")
    serve(run_root, host=host, port=port)


if __name__ == "__main__":
    app()
