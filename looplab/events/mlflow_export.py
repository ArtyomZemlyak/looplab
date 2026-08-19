"""G5 · MLflow export bridge. Log a finished run's champion (params + metrics + tags + the solution
artifact) to an MLflow tracking server, so LoopLab plugs into existing MLOps stacks. MLflow is an
OPTIONAL dependency — `available()` reports whether it's importable, and `export_run` raises a clear
error if it isn't, never at import time (keeps the core zero-dep).
"""
from __future__ import annotations

from pathlib import Path

from looplab.core.models import RunState, extra_metric_channel
from looplab.core.redact import redact_secrets


def available() -> bool:
    try:
        import mlflow  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def export_run(state: RunState, *, tracking_uri: str | None = None,
               experiment: str | None = None, code: str | None = None,
               node=None) -> str:
    """Log the run's champion to MLflow and return the MLflow run id. Raises RuntimeError if MLflow
    isn't installed (install the optional `mlflow` extra). `node` overrides which node's params/
    metrics are logged (defaults to state.best()); pass the SAME node whose `code` is exported so the
    logged params/metrics and the solution.py artifact describe ONE node (a pinned champion may
    differ from the metric-best node)."""
    if not available():
        raise RuntimeError(
            "MLflow export needs the optional `mlflow` package: pip install mlflow")
    import mlflow

    if tracking_uri:
        mlflow.set_tracking_uri(tracking_uri)
    if experiment:
        mlflow.set_experiment(experiment)
    best = node if node is not None else state.best()
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
            metric = best.robust_metric
            if metric is not None:
                mlflow.log_metric("best_metric", float(metric))
            _channels = getattr(best, "extra_metrics_provenance", None)
            for k, v in (best.extra_metrics or {}).items():
                if v is not None:
                    logged = False
                    try:                              # extra_metrics is eval-reported: a non-numeric
                        mlflow.log_metric(str(k), float(v))   # value must not abort the whole export
                        logged = True
                    except (TypeError, ValueError):
                        pass
                    # AND THE CHANNEL IT CAME THROUGH, as a tag beside it. MLflow's metric surface
                    # is a bare `name -> value` series with nowhere to hang provenance, so an
                    # auto-captured number the candidate printed landed in the same table as the
                    # protected `best_metric` with nothing separating them — an operator comparing
                    # runs in MLflow could not tell. A TAG rather than a renamed metric key: the key
                    # is what makes a series comparable across runs and across the 3 preserved runs
                    # that already exported these names, so renaming would break the comparison this
                    # export exists for. `unknown` is exported too, and says so — silence there
                    # would read as "declared" to exactly the reader this is for. Same containment
                    # as the metric above: a key MLflow's tag charset refuses must not abort the run.
                    # SET ONLY WHERE THE METRIC LANDED. The `log_metric` above has its own
                    # containment, and the case it exists for is a non-numeric extra value — so
                    # tagging on `v is not None` published provenance for a number that is not in
                    # the run's metric table at all, which is worse than no provenance: the tag is
                    # what an operator reads to tell an auto-captured value from the protected
                    # `best_metric`, and a channel naming a missing metric answers a question about
                    # nothing. Gated on `logged` (see the metric branch above).
                    if not logged:
                        continue
                    try:
                        mlflow.set_tag(f"looplab.extra_metric_channel.{k}",
                                       extra_metric_channel(_channels, k))
                    except Exception:  # noqa: BLE001
                        pass
        mlflow.log_metric("nodes", len(state.nodes))
        mlflow.log_metric("evaluated", len(state.evaluated_nodes()))
        if code:
            # An EXTERNAL tracking server is an egress boundary, so the champion's code is redacted
            # here — the same treatment `serve/reviews.py` gives code/files/parent_code before
            # disclosure and `core/tracing.py` gives the OTLP exporter ("a DURABLE egress boundary,
            # so redaction has to happen here"). Node code is secret-BEARING in practice: a repo-mode
            # Developer that read a checked-in .env or token through its tools can echo it straight
            # into the solution.
            mlflow.log_text(redact_secrets(code), "solution.py")
        return run.info.run_id


def export_run_dir(run_dir, **kwargs) -> str:
    """Convenience: fold a run dir and export it (loads the champion's code from the node detail)."""
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold
    rd = Path(run_dir)
    state = fold(EventStore(rd / "events.jsonl").read_all())
    champ = state.nodes.get(state.champion) if state.champion is not None else state.best()
    return export_run(state, code=(champ.code if champ else None), node=champ, **kwargs)
