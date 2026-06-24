"""D2 · Capability self-benchmark harness. Run a suite of tasks end-to-end and report best-metric,
eval-seconds-to-result, and reward-hack flags — a regression test for *capability* (does the engine
still solve these?), not just code. Seeded from `tools/e2e_report.py`; exposed as `looplab bench`.

Pure orchestration over the existing engine builder, so it benchmarks exactly what `looplab run`
does. Deterministic for the toy backend; offline by default.
"""
from __future__ import annotations

import time
from pathlib import Path

import anyio
import orjson

from .config import Settings
from .replay import fold
from .tasks import load_task


def run_benchmark(task_files, settings: Settings, out_dir) -> list[dict]:
    """Run each task to completion and return a capability summary per task. Writes
    `<out_dir>/benchmark.json` with the full report."""
    from .cli import _engine   # lazy to avoid an import cycle (cli imports bench in its command)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for tf in task_files:
        tf = Path(tf)
        rd = out_dir / tf.stem
        t0 = time.time()
        try:
            task = load_task(tf)
            state = anyio.run(_engine(rd, task, settings, None).run)
            best = state.best()
            results.append({
                "task": tf.stem, "task_id": state.task_id, "direction": state.direction,
                "finished": state.finished,
                "best_metric": (best.confirmed_mean if best and best.confirmed_mean is not None
                                else (best.metric if best else None)),
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
