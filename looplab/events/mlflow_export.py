"""G5 · MLflow bridge, in two halves. `export_run` logs a FINISHED run's champion (params + metrics
+ tags + the solution artifact) to an MLflow tracking server; `LiveTracker`/`autolog` is the
AUTOLOGGING half (2026-09-08, docs/BACKLOG.md §16) that mirrors a run into MLflow WHILE IT RUNS, so
an operator watching a six-hour search sees each node land instead of nothing until a human
remembers to run the export command. MLflow is an OPTIONAL dependency — `available()` reports
whether it's importable, `export_run` raises a clear error if it isn't, `autolog` degrades to doing
nothing, and neither imports it at module load (keeps the core zero-dep).

WHAT "AUTOLOG" MEANS HERE, and what it deliberately does not. MLflow's own `mlflow.autolog()`
monkeypatches training libraries in the process that calls it; LoopLab's engine process trains
nothing — the candidate does, inside a sandbox subprocess the engine must not reach into — so that
call would patch nothing and log nothing. What is autologged instead is the RUN: a follower thread
tails the run's append-only `events.jsonl` (the authoritative record) and mirrors each node terminal
as it lands. That direction is what makes it safe to ship on by configuration: the mirror is a
READER, it holds no lock, it never appends, a dead tracking server cannot stall the search, and a
crash on the mirror side loses a mirror and not a run.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from looplab.core.models import RunState, extra_metric_channel
from looplab.core.redact import redact_secrets

log = logging.getLogger("looplab.mlflow")

POLL_S = 5.0                  # how often the follower re-reads the log's new bytes
_MAX_CONSECUTIVE_FAILURES = 3  # after this many, the mirror gives up rather than spamming a dead URI


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
            _log_solution(mlflow, code)
        return run.info.run_id


def _log_solution(mlflow, code: str) -> None:
    """Publish a node's code as the `solution.py` artifact of the CURRENTLY ACTIVE MLflow run.

    An EXTERNAL tracking server is an egress boundary, so the champion's code is redacted here — the
    same treatment `serve/reviews.py` gives code/files/parent_code before disclosure and
    `core/tracing.py` gives the OTLP exporter ("a DURABLE egress boundary, so redaction has to
    happen here"). Node code is secret-BEARING in practice: a repo-mode Developer that read a
    checked-in .env or token through its tools can echo it straight into the solution.

    ONE spelling of that rule, shared by the finished-run export above and the live mirror below —
    the second egress was the moment a copied `log_text(code)` would have shipped a credential from
    a path no test covered."""
    mlflow.log_text(redact_secrets(code), "solution.py")


def export_run_dir(run_dir, **kwargs) -> str:
    """Convenience: fold a run dir and export it (loads the champion's code from the node detail)."""
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold
    rd = Path(run_dir)
    state = fold(EventStore(rd / "events.jsonl").read_all())
    champ = state.nodes.get(state.champion) if state.champion is not None else state.best()
    return export_run(state, code=(champ.code if champ else None), node=champ, **kwargs)


# --------------------------------------------------------------------- the AUTOLOGGING half (§16)
class LiveTracker:
    """Mirror ONE in-flight run into MLflow by tailing its event log.

    The mirror's shape follows the LOG, not the engine: `sync()` reads the bytes appended since the
    last call (`EventStore.read_all` is incrementally cached) and publishes what is new — a child
    MLflow run per evaluated node carrying that node's params, its metric and its extra metrics with
    the channel tags `export_run` writes — plus, on the parent run, the `node_metric` and
    `best_metric` SERIES stepped by node id, which is the curve an operator watches a search on.

    IDEMPOTENT BY NODE ID, which is what makes it safe on a resumed run: a mirror started over a log
    that already holds forty nodes publishes those forty once and then follows. Nothing here appends
    to the log, takes the engine's write lock, or blocks the loop; `sync()` contains every failure
    and gives up after `_MAX_CONSECUTIVE_FAILURES` rather than retrying a dead tracking URI for the
    length of a run.
    """

    def __init__(self, run_dir, *, tracking_uri: str, experiment: Optional[str] = None):
        self.run_dir = Path(run_dir)
        self.tracking_uri = tracking_uri
        self.experiment = experiment
        self._mlflow = None
        self._store = None
        self._parent = None             # the MLflow run, opened by the first sync that can NAME it
        self._logged: set[int] = set()  # node ids already mirrored
        self._best: Optional[float] = None
        self.failures = 0

    # -- internals -----------------------------------------------------------------------------
    def _events(self):
        from looplab.events.eventstore import EventStore
        if self._store is None:
            self._store = EventStore(self.run_dir / "events.jsonl")
        return self._store.read_all()

    def _open_parent(self, mlflow, state) -> None:
        """Open the parent MLflow run once the log NAMES the LoopLab run. Deferred rather than done
        at construction because the follower starts before the engine writes `run_started`, and a
        parent named after a directory would be the wrong identity on every resumed run."""
        if self._parent is not None or not state.run_id:
            return
        if self.tracking_uri:
            mlflow.set_tracking_uri(self.tracking_uri)
        if self.experiment:
            mlflow.set_experiment(self.experiment)
        self._parent = mlflow.start_run(run_name=state.run_id)
        mlflow.set_tags({
            "looplab.run_id": state.run_id, "looplab.task_id": state.task_id,
            "looplab.direction": state.direction, "looplab.goal": (state.goal or "")[:250],
            "looplab.autologged": "true",   # says this run was MIRRORED live, not exported after
        })

    def _log_node(self, mlflow, state, node) -> None:
        direction = (state.direction or "min").lower()
        metric = node.robust_metric
        with mlflow.start_run(run_name=f"node-{node.id}", nested=True):
            mlflow.set_tags({"looplab.node_id": str(node.id),
                             "looplab.operator": str(node.operator or "")})
            for k, v in (node.idea.params or {}).items():
                try:
                    mlflow.log_param(str(k), v)
                except Exception:  # noqa: BLE001 — a param MLflow's charset refuses costs that param
                    pass
            if metric is not None:
                mlflow.log_metric("metric", float(metric))
            _channels = getattr(node, "extra_metrics_provenance", None)
            for k, v in (node.extra_metrics or {}).items():
                if v is None:
                    continue
                try:                                     # the export's rule above, for its reason:
                    mlflow.log_metric(str(k), float(v))  # an eval-reported value may not be a number
                except (TypeError, ValueError):
                    continue
                try:
                    mlflow.set_tag(f"looplab.extra_metric_channel.{k}",
                                   extra_metric_channel(_channels, k))
                except Exception:  # noqa: BLE001 — a tag key MLflow refuses costs that tag, not the run
                    pass
        if metric is None:
            return
        # The two SERIES on the parent, stepped by node id: what each node measured, and the running
        # best. Derived here rather than read off the state so a mirror that joined mid-run still
        # draws a monotone curve from the point it joined.
        mlflow.log_metric("node_metric", float(metric), step=int(node.id))
        better = (self._best is None or (float(metric) < self._best if direction == "min"
                                         else float(metric) > self._best))
        if better:
            self._best = float(metric)
        mlflow.log_metric("best_metric", float(self._best), step=int(node.id))

    # -- the public surface --------------------------------------------------------------------
    def sync(self) -> int:
        """Publish everything the log gained since the last call; returns how many nodes landed.
        `-1` means the mirror GAVE UP (MLflow absent, or it failed too many times in a row)."""
        if self.failures >= _MAX_CONSECUTIVE_FAILURES:
            return -1
        if self._mlflow is None:
            if not available():
                self.failures = _MAX_CONSECUTIVE_FAILURES
                return -1
            import mlflow
            self._mlflow = mlflow
        mlflow = self._mlflow
        try:
            from looplab.events.replay import fold
            state = fold(self._events())
            self._open_parent(mlflow, state)
            if self._parent is None:
                return 0                       # the log has not named the run yet; try again later
            published = 0
            for node in state.evaluated_nodes():
                if node.id in self._logged:
                    continue
                self._log_node(mlflow, state, node)
                self._logged.add(node.id)
                published += 1
            self.failures = 0
            return published
        except Exception as exc:  # noqa: BLE001 — the mirror is an OBSERVER: a dead tracking server,
            # a log tail read mid-append or an MLflow API change must cost the mirror and never the
            # run. `contain` counts it; three in a row and the follower stops rather than spinning.
            from looplab.core.containment import contain
            contain("mlflow_autolog", exc)
            self.failures += 1
            log.warning("MLflow autolog sync failed (%s/%s): %s",
                        self.failures, _MAX_CONSECUTIVE_FAILURES, exc)
            return 0

    def close(self) -> None:
        """Final sync, publish the champion's code, end the parent run. Safe to call twice."""
        self.sync()
        mlflow, parent = self._mlflow, self._parent
        if mlflow is None or parent is None:
            return
        self._parent = None
        try:
            from looplab.events.replay import fold
            state = fold(self._events())
            champ = (state.nodes.get(state.champion) if state.champion is not None
                     else state.best())
            mlflow.log_metric("nodes", len(state.nodes))
            mlflow.log_metric("evaluated", len(state.evaluated_nodes()))
            if champ is not None:
                mlflow.set_tag("looplab.champion_node_id", str(champ.id))
                if champ.code:
                    _log_solution(mlflow, champ.code)
        except Exception as exc:  # noqa: BLE001 — same containment as `sync`; the run is already over
            from looplab.core.containment import contain
            contain("mlflow_autolog_close", exc)
        finally:
            try:
                mlflow.end_run()
            except Exception as exc:  # noqa: BLE001 — an un-endable run leaks a mirror, not a result
                from looplab.core.containment import contain
                contain("mlflow_autolog_end_run", exc)


def follow_run_dir(run_dir, *, tracking_uri: str, experiment: Optional[str] = None,
                   poll_s: float = POLL_S, stop: Optional[threading.Event] = None) -> LiveTracker:
    """Tail `run_dir` into MLflow until `stop` is set, then close the mirror. Runs on the CALLER's
    thread — `autolog` below is what puts it on its own."""
    tracker = LiveTracker(run_dir, tracking_uri=tracking_uri, experiment=experiment)
    stop = stop if stop is not None else threading.Event()
    while not stop.is_set():
        if tracker.sync() < 0:
            break                              # gave up: MLflow absent, or the URI is dead
        stop.wait(max(0.1, float(poll_s)))
    tracker.close()
    return tracker


@contextmanager
def autolog(run_dir, *, tracking_uri: str, experiment: Optional[str] = None,
            poll_s: float = POLL_S):
    """Mirror `run_dir` into MLflow for as long as the block runs; yields the `LiveTracker` or None.

    A NO-OP and not an error when `tracking_uri` is empty — the shipped default, because shipping a
    run to a tracking server is egress and only an operator naming one may start it — and when
    MLflow is not installed, since an optional dependency's absence must never fail a run. The
    mirror lives on a daemon thread and is stopped, joined (bounded) and closed at exit, so the
    final sync lands before the command returns."""
    if not tracking_uri or not available():
        yield None
        return
    tracker = LiveTracker(run_dir, tracking_uri=tracking_uri, experiment=experiment)
    stop = threading.Event()

    def _loop():
        while not stop.is_set():
            if tracker.sync() < 0:
                return
            stop.wait(max(0.1, float(poll_s)))

    thread = threading.Thread(target=_loop, name="looplab-mlflow-autolog", daemon=True)
    thread.start()
    try:
        yield tracker
    finally:
        stop.set()
        thread.join(timeout=max(1.0, float(poll_s)))
        tracker.close()
