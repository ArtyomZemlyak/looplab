"""Typer CLI (I0/I6, ADR-17): run / resume / inspect / replay.

The engine is a *process*, not a server (ADR-18): `LoopLab run task.json` spawns one
async orchestrator that drives the loop to completion (or crash). `resume` re-enters
the same run dir; `replay`/`inspect` are pure read-only folds of the event log.
"""
from __future__ import annotations

import json
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


def _engine(run_dir: Path, task: TaskAdapter, settings: Settings,
            crash_after: Optional[int]) -> Engine:
    researcher, developer = make_roles(task, settings)
    # RepoTask onboarding (Phase 3): if the task can propose its own eval spec, build the
    # onboarder (Researcher proposes + Developer writes the adapter).
    mk = getattr(task, "make_onboarder", None)
    onboarder = mk(settings) if callable(mk) else None
    return Engine(
        run_dir,
        task=task,
        researcher=researcher,
        developer=developer,
        sandbox=make_sandbox(settings.trust_mode, image=settings.docker_image),
        policy=make_policy(settings.policy, n_seeds=settings.n_seeds,
                           max_nodes=settings.max_nodes, ablate_every=settings.ablate_every),
        max_parallel=settings.max_parallel,
        timeout=settings.timeout,
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
    )


def _print_result(state) -> None:
    best = state.best()
    typer.echo(f"run={state.run_id} task={state.task_id} finished={state.finished}")
    typer.echo(f"nodes={len(state.nodes)} evaluated={len(state.evaluated_nodes())}")
    if best is not None:
        typer.echo(f"BEST node {best.id}: metric={best.metric:.6g} params={best.idea.params}")


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
    atomic_write_text(out / "config.snapshot.json",
                      json.dumps(settings.masked_snapshot(), indent=2))
    eng = _engine(out, task, settings, crash_after)
    state = anyio.run(eng.run)
    _print_result(state)


@app.command()
def resume(
    run_dir: Path = typer.Argument(..., help="Existing run directory to resume."),
    task_file: Path = typer.Option(..., help="The same task file used to start the run."),
    max_nodes: Optional[int] = typer.Option(None),
):
    """Resume a crashed/incomplete run by re-entering the loop (replay-based)."""
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
