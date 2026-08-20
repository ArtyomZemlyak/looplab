#!/usr/bin/env python3
"""Summarise an AlgoTune campaign: arm A (AlgoTuner) vs arm B (LoopLab), same model.

Reads each arm from the place that arm actually writes, and refuses to invent the rest:

* **arm A** -> ``<algotune>/reports/agent_summary.json``, which ``AlgoTuner/main.py`` updates as
  ``<task>.<normalized-model>.final_speedup`` at the end of a run. That file also carries the 17
  SHIPPED reference models, so their numbers come along for free as context.
* **arm B** -> ``B-<task>.final.json``, the champion's score on the TEST split, produced once after
  the run. Every LoopLab node is evaluated on TRAIN (mirroring AlgoTuner's own agent loop), so the
  run's internal champion metric is a TRAIN number and belongs in the same column as arm A's test
  result only by mistake. Without ``--final-dir`` the tool falls back to that train metric AND says
  so in a warning, because a silently mixed-split comparison is worse than no comparison.
  The champion itself is identified from the FOLD (`extract_champion.py`), never from a directory
  listing: the fold is what decides a run's champion.

WHAT THE COLUMNS MEAN, AND THE TWO THINGS THEY DO NOT
-----------------------------------------------------
``speedup = baseline_ms / optimized_ms``, both timed on this machine in the same pass, so the ratio
self-normalises against hardware. **100 % instance validity is required for any speedup at all** --
a solver wrong on one instance scores 0, not partial credit -- so a `0.0` here means "wrong
somewhere", not "no faster".

It does NOT isolate the loop from the model: the 17 reference arms were produced by *AlgoTuner's*
loop driving other models, so re-timing them compares ARTIFACTS. The only controlled row is
A-vs-B on the same model, produced here. They are printed together and labelled apart.

A MISSING arm is printed as ``--`` and never as ``0``. "Did not finish" and "finished wrong" are
different facts, and averaging the first as a zero is how a campaign that half-ran reports a
result. Means are taken over the PAIRS that have both arms, and the count is stated.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def _arm_a(summary_path: Path, model_fragment: str) -> dict[str, float | None]:
    """`{task: final_speedup}` for the one model whose name contains `model_fragment`."""
    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, float | None] = {}
    for task, models in (data or {}).items():
        if not isinstance(models, dict):
            continue
        for name, row in models.items():
            if model_fragment.lower() not in str(name).lower():
                continue
            raw = (row or {}).get("final_speedup")
            try:
                out[task] = float(raw)
            except (TypeError, ValueError):
                # "N/A" and "Error" are the harness's own words for "no number", and they are not
                # zero -- see the module docstring.
                out[task] = None
    return out


def _reference_models(summary_path: Path, task: str) -> dict[str, float]:
    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for name, row in (data.get(task) or {}).items():
        try:
            out[str(name)] = float((row or {}).get("final_speedup"))
        except (TypeError, ValueError):
            continue
    return out


def _arm_b_final(final_json: Path) -> float | None:
    """Arm B's TEST score: the champion, evaluated once on the graded split after the run.

    This — not the run's own champion metric — is what compares to arm A. Every LoopLab node is
    evaluated on TRAIN (mirroring AlgoTuner's agent loop), so `state.best().metric` is a TRAIN
    number and putting it in the same column as arm A's test result would compare two different
    splits.
    """
    try:
        row = json.loads(final_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = row.get("speedup") if isinstance(row, dict) else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _arm_b_train(run_dir: Path) -> float | None:
    """The LoopLab run's own champion metric — a TRAIN number, reported only as context."""
    if not (run_dir / "events.jsonl").exists():
        return None
    try:
        from looplab.events.eventstore import EventStore
        from looplab.events.replay import fold
        state = fold(EventStore(str(run_dir)).read_all())
    except Exception:                       # noqa: BLE001 - a broken run is "no number", not a crash
        return None
    best = state.best()
    if best is None or best.metric is None:
        return None
    try:
        return float(best.metric)
    except (TypeError, ValueError):
        return None


def _fmt(value: float | None) -> str:
    return "--" if value is None else f"{value:.4f}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--algotune-root", required=True, type=Path)
    ap.add_argument("--runs-root", required=True, type=Path,
                    help="Directory holding <task>/run/ for the LoopLab arm.")
    ap.add_argument("--final-dir", type=Path, default=None,
                    help="Directory holding B-<task>.final.json (the champion's TEST score). "
                         "Without it, arm B's column is its TRAIN metric and is NOT comparable to "
                         "arm A -- the header says so.")
    ap.add_argument("--model-fragment", default="v4-flash",
                    help="Substring identifying OUR model inside agent_summary.json.")
    ap.add_argument("--reference", action="store_true",
                    help="Also print the shipped reference models per task (context, not controls).")
    args = ap.parse_args()

    summary = args.algotune_root.resolve() / "reports" / "agent_summary.json"
    a = _arm_a(summary, args.model_fragment)
    tasks = sorted({p.name for p in args.runs_root.glob("*") if p.is_dir()} | set(a))
    if not tasks:
        print("no campaign output found"); return 1

    rows, paired = [], []
    for task in tasks:
        va = a.get(task)
        vb = (_arm_b_final(args.final_dir / f"B-{task}.final.json") if args.final_dir
              else _arm_b_train(args.runs_root / task / "run"))
        rows.append((task, va, vb))
        if va is not None and vb is not None:
            paired.append((va, vb))

    if args.final_dir is None:
        print("WARNING: --final-dir not given, so arm B's column is its TRAIN metric while "
              "arm A's is a TEST score. Those are different splits and must not be read as "
              "a comparison.")
    width = max(len(t) for t, _, _ in rows)
    print(f"{'task':<{width}}  {'A: AlgoTuner':>13}  {'B: LoopLab':>11}   winner")
    print("-" * (width + 42))
    for task, va, vb in rows:
        if va is None or vb is None:
            winner = "(incomplete)"
        elif va == vb:
            winner = "tie"
        else:
            winner = "A" if va > vb else "B"
        print(f"{task:<{width}}  {_fmt(va):>13}  {_fmt(vb):>11}   {winner}")

    print("-" * (width + 42))
    if paired:
        mean_a = sum(x for x, _ in paired) / len(paired)
        mean_b = sum(y for _, y in paired) / len(paired)
        wins_a = sum(1 for x, y in paired if x > y)
        wins_b = sum(1 for x, y in paired if y > x)
        print(f"{'mean over ' + str(len(paired)) + ' complete pairs':<{width}}  "
              f"{mean_a:>13.4f}  {mean_b:>11.4f}")
        print(f"{'wins':<{width}}  {wins_a:>13}  {wins_b:>11}   "
              f"({len(paired) - wins_a - wins_b} tied)")
    incomplete = sum(1 for _, va, vb in rows if va is None or vb is None)
    if incomplete:
        print(f"\n{incomplete} of {len(rows)} tasks are missing an arm and are EXCLUDED from the "
              f"means above — a missing run is not a zero.")
    print("\nspeedup = baseline_ms / optimized_ms, both timed here. 100% instance validity is "
          "required:\na 0.0000 means the solver was WRONG somewhere, not that it was not faster.")

    if args.reference:
        print("\nShipped reference models (AlgoTuner's loop driving OTHER models — context, not a "
              "control:\nthose rows differ from ours in the model AND were produced elsewhere).")
        for task, _, _ in rows:
            ref = _reference_models(summary, task)
            ours = {k: v for k, v in ref.items() if args.model_fragment.lower() in k.lower()}
            others = {k: v for k, v in ref.items() if k not in ours}
            if not others:
                continue
            best = max(others.items(), key=lambda kv: kv[1])
            print(f"  {task:<{width}}  best reference {best[0]} = {best[1]:.4f}  "
                  f"(median {sorted(others.values())[len(others) // 2]:.4f} over {len(others)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
