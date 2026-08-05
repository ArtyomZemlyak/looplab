"""D2 · Capability self-benchmark harness. Run a suite of tasks end-to-end and report best-metric,
eval-seconds-to-result, and reward-hack flags — a regression test for *capability* (does the engine
still solve these?), not just code. Seeded from `tools/e2e_report.py`; exposed as `looplab bench`.

Pure orchestration over the existing engine builder, so it benchmarks exactly what `looplab run`
does. Offline by default.

REPRODUCIBILITY, stated as measured rather than as advertised. On the toy backend every SCIENTIFIC
field of `benchmark.json` — champion, `best_metric`, `nodes`/`evaluated`/`failed`, `reward_hack_flags`,
`stop_reason` — is identical run to run, and so is the folded `RunState`. Two narrower claims that
used to be spelled as one blanket "deterministic for the toy backend", and are not:

* `benchmark.json` is NOT byte-identical, ever: `eval_seconds` and `wall_seconds` are wall-clock
  measurements. (Measured over 8 identical suites: 8 distinct files, ONE distinct projection once
  those two fields are dropped.)
* The event log's BYTE ORDER is deterministic only for a run whose cross-run memory store is in the
  same state. It is CLAUDE.md engine invariant #1 that makes the log byte-reproducible at a settled
  build width of 1 — and that holds here, since AUTO settles a toy build to 1 — but the run's CONTENT
  still depends on what it reads: the FIRST run into an empty `LOOPLAB_MEMORY_DIR` has no prior
  lessons to reconcile and therefore lacks one `lessons_reconciled` event that every later run has.
  Measured over 8 suites sharing one initially-empty memory dir: exactly 2 order signatures per task,
  split 1/7, diverging at index 5 on exactly that event; 8 suites with a per-suite memory dir give 1.
  That is cross-run LEARNING working, not a defect, and it is deliberately not suppressed here — the
  harness exists to benchmark what `looplab run` actually does. Pin `LOOPLAB_MEMORY_DIR` to a fresh
  directory per suite (or to a pre-warmed one) when you need log-byte comparability across suites.
"""
from __future__ import annotations

import time
from pathlib import Path

import anyio
import orjson

from looplab.core.config import Settings
from looplab.core.latebind import late_bound
from looplab.adapters.tasks import load_task
# Top-level on purpose (this was a lazy in-function import "to avoid an import cycle"): the cycle is
# gone — the CLI's `bench` command imports looplab.bench lazily inside its own body
# (looplab/cli/export_cmds.py), so nothing in the looplab.cli package imports this module at import
# time, in either direction. Importing the shared engine builder here retires that load-bearing lazy
# import (docs/15 §P5.2b).
# Late-binding shim matching the cli package's own command-module shims — so a test patching
# `cli._engine` (to stub the engine for an offline bench) is seen here too. `looplab.core.latebind`
# names it by STRING, so this module still does not import the Typer command surface.
_engine = late_bound("looplab.cli", "_engine")


def run_benchmark(task_files, settings: Settings, out_dir) -> list[dict]:
    """Run each task to completion and return a capability summary per task. Writes
    `<out_dir>/benchmark.json` with the full report."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    seen_stems: dict[str, int] = {}
    for tf in task_files:
        tf = Path(tf)
        # Unique run dir per task: two suites with a same-named task file (suiteA/task.json,
        # suiteB/task.json) both map to out/task otherwise, and Engine.run would FOLD the first
        # task's event log and "resume" the wrong run. Suffix a counter on collision.
        stem = tf.stem
        if stem in seen_stems:
            seen_stems[stem] += 1
            rd = out_dir / f"{stem}_{seen_stems[stem]}"
        else:
            seen_stems[stem] = 1
            rd = out_dir / stem
        t0 = time.time()
        try:
            task = load_task(tf)
            state = anyio.run(_engine(rd, task, settings, None).run)
            best = state.best()
            results.append({
                "task": tf.stem, "task_id": state.task_id, "direction": state.direction,
                "finished": state.finished,
                "best_metric": (best.robust_metric if best else None),
                "best_node": (best.id if best else None),
                "nodes": len(state.nodes), "evaluated": len(state.evaluated_nodes()),
                "failed": sum(1 for n in state.nodes.values() if n.status.value == "failed"),
                "eval_seconds": round(state.total_eval_seconds, 3),
                "wall_seconds": round(time.time() - t0, 3),
                "reward_hack_flags": len(state.reward_hacks),
                "stop_reason": state.stop_reason,
            })
        except Exception as e:  # noqa: BLE001 — one bad task shouldn't sink the whole suite
            results.append({"task": tf.stem, "error": str(e), "finished": False})
    report = {"n_tasks": len(results), "results": results,
              "solved": sum(1 for r in results if r.get("finished") and r.get("best_metric") is not None)}
    (out_dir / "benchmark.json").write_bytes(orjson.dumps(report, option=orjson.OPT_INDENT_2))
    return results
