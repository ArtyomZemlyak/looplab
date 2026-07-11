"""Export / diagnostics commands: `smoke` / `bench` / `export-mlflow` / `export-notebook` / `harden`.

Split verbatim out of the flat `looplab/cli.py` (docs/15 §P5.2). `bench` keeps its lazy
`looplab.bench` import INSIDE the command body — that is what lets `looplab/bench.py` import the
shared `_engine` builder back from `looplab.cli` at module level without an import cycle.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from looplab.core.atomicio import atomic_write_text
from looplab.core.config import Settings
from looplab.events.replay import fold
from looplab.cli import _BACKENDS, _choice, _require_run_dir, app


def make_llm_client(*args, **kwargs):
    """Late-bound through the package module so a test patching `looplab.cli.make_llm_client`
    (the documented seam, test_cli.py) also stubs `smoke` here — a plain
    `from looplab.cli import make_llm_client` would freeze the pre-patch object."""
    from looplab import cli
    return cli.make_llm_client(*args, **kwargs)


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
                           metric=champ.robust_metric,
                           task_id=state.task_id, run_id=state.run_id)
    dest = out or (run_dir / "champion.ipynb")
    atomic_write_text(dest, json.dumps(nb, indent=1))
    typer.echo(f"wrote {dest}")


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
