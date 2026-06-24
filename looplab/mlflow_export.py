"""G5 · MLflow export bridge. Log a finished run's champion (params + metrics + tags + the solution
artifact) to an MLflow tracking server, so LoopLab plugs into existing MLOps stacks. MLflow is an
OPTIONAL dependency — `available()` reports whether it's importable, and `export_run` raises a clear
error if it isn't, never at import time (keeps the core zero-dep).
"""
from __future__ import annotations

from pathlib import Path

from .models import RunState


def available() -> bool:
    try:
        import mlflow  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def export_run(state: RunState, *, tracking_uri: str | None = None,
               experiment: str | None = None, code: str | None = None) -> str:
    """Log the run's champion to MLflow and return the MLflow run id. Raises RuntimeError if MLflow
    isn't installed (install the optional `mlflow` extra)."""
    if not available():
        raise RuntimeError(
            "MLflow export needs the optional `mlflow` package: pip install mlflow")
    import mlflow

    if tracking_uri:
        mlflow.set_tracking_uri(tracking_uri)
    if experiment:
        mlflow.set_experiment(experiment)
    best = state.best()
    with mlflow.start_run(run_name=state.run_id) as run:
        mlflow.set_tags({
            "looplab.run_id": state.run_id, "looplab.task_id": state.task_id,
            "looplab.direction": state.direction, "looplab.goal": (state.goal or "")[:250],
        })
        if best is not None:
            for k, v in (best.idea.params or {}).items():
                try:
                    mlflow.log_param(str(k), v)
                except Exception:  # noqa: BLE001
                    pass
            metric = best.confirmed_mean if best.confirmed_mean is not None else best.metric
            if metric is not None:
                mlflow.log_metric("best_metric", float(metric))
            for k, v in (best.extra_metrics or {}).items():
                if v is not None:
                    try:                              # extra_metrics is eval-reported: a non-numeric
                        mlflow.log_metric(str(k), float(v))   # value must not abort the whole export
                    except (TypeError, ValueError):
                        pass
        mlflow.log_metric("nodes", len(state.nodes))
        mlflow.log_metric("evaluated", len(state.evaluated_nodes()))
        if code:
            mlflow.log_text(code, "solution.py")
        return run.info.run_id


def export_run_dir(run_dir, **kwargs) -> str:
    """Convenience: fold a run dir and export it (loads the champion's code from the node detail)."""
    from .eventstore import EventStore
    from .replay import fold
    rd = Path(run_dir)
    state = fold(EventStore(rd / "events.jsonl").read_all())
    champ = state.nodes.get(state.champion) if state.champion is not None else state.best()
    return export_run(state, code=(champ.code if champ else None), **kwargs)
