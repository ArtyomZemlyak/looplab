"""The MLE-bench Lite campaign's aggregator (doc 52 row 23): ≥3 seeds per competition, mean ± SEM,
the percentile rank, the raw and Mislead-adjusted numbers, and the two extras' verdicts — one table.

    python -m looplab.adapters.mlebench_campaign runs/spooky-s1 runs/spooky-s2 runs/spooky-s3 [--json]

The README's submission rule is ≥3 seeds reported as mean ± SEM, and until this module nothing in
the tree aggregated anything: `docs/MLEBENCH.md` named no seed count and no aggregation. Every
number here is read off a run's OWN record — the champion's finish-time private grade
(`holdout_evaluated`, `protocol: private_grade`, doc 52 row 3) and its official report
(`mlebench_report.json`: medal flags, `above_median`, the leaderboard `percentile`), the run row's
Mislead pair (`engine/champion_caveats.py::mislead_gap`, row 22), the extras sidecar
(`mlebench_extras.json`, row 22) and the seed the run was launched with — and nothing is computed
that a reviewer could not recompute from the bundle (`looplab export-bundle`). No model, no
network, no write: this prints.

What "Mislead-adjusted" means here: the champion's number minus the run's own `mislead_gap.gap` —
i.e. `S_intended`, the best number the intended protocol supports — reported BESIDE the raw one and
never instead of it, which is Protocol Validity's shape. A run whose gap is `null` (nothing survived
the filter) has no adjusted number, and the table says so.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Optional


def run_facts(run_dir) -> dict:
    """One run's campaign row, off its own record; every field `None` when the record lacks it."""
    from looplab.engine.champion_caveats import mislead_gap
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold

    run_dir = Path(run_dir)
    events = EventStore(run_dir / "events.jsonl").read_all()
    state = fold(events)
    best = state.best()
    row: dict = {"run_id": state.run_id, "run_dir": str(run_dir), "task_id": state.task_id,
                 "direction": state.direction, "finished": bool(state.finished),
                 "nodes": len(state.nodes), "seed": _seed_of(run_dir),
                 "champion": best.id if best is not None else None,
                 "search_metric": best.metric if best is not None else None,
                 "private_grade": None, "gap_to_private": None, "protocol": None,
                 "report": None, "mislead": mislead_gap(state), "extras": _extras_of(run_dir)}
    if best is not None:
        for e in events:
            d = e.data if isinstance(e.data, dict) else {}
            if e.type == "holdout_evaluated" and d.get("node_id") == best.id:
                row["private_grade"] = d.get("metric")
                row["gap_to_private"] = d.get("gap")
                row["protocol"] = d.get("protocol", "holdout")
        report_path = run_dir / "nodes" / f"node_{best.id}" / "mlebench_report.json"
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if isinstance(report, dict):
                row["report"] = {k: report.get(k) for k in (
                    "score", "percentile", "leaderboard_size", "any_medal", "gold_medal",
                    "silver_medal", "bronze_medal", "above_median", "valid_submission")}
        except (OSError, ValueError):
            pass
    gap = (row["mislead"] or {}).get("gap") if isinstance(row["mislead"], dict) else None
    number = row["private_grade"] if row["private_grade"] is not None else row["search_metric"]
    row["raw"] = number
    row["adjusted"] = (number - gap if state.direction == "max" else number + gap) \
        if (number is not None and gap is not None) else None
    return row


def _seed_of(run_dir: Path):
    """The seed the run was launched with: `LOOPLAB_EVAL_SEED` from the eval env when the config
    snapshot declares one, else `confirm_seed_base`; `None` when neither is recorded."""
    try:
        snap = json.loads((run_dir / "config.snapshot.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(snap, dict):
        return None
    env = snap.get("eval_env") if isinstance(snap.get("eval_env"), dict) else {}
    if env.get("LOOPLAB_EVAL_SEED") is not None:
        return str(env["LOOPLAB_EVAL_SEED"])
    return snap.get("confirm_seed_base")


def _extras_of(run_dir: Path) -> Optional[dict]:
    try:
        doc = json.loads((run_dir / "mlebench_extras.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    rv, pl = doc.get("rule_violation") or {}, doc.get("plagiarism") or {}
    return {"rule_violation": rv.get("verdict") or rv.get("status"),
            "plagiarism": pl.get("max_similarity") if pl.get("status") == "ok" else pl.get("status")}


def mean_sem(values) -> dict:
    """Mean ± SEM over the finite values, with the count; SEM is `None` below two values."""
    xs = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)
          and math.isfinite(v)]
    if not xs:
        return {"n": 0, "mean": None, "sem": None}
    mean = sum(xs) / len(xs)
    if len(xs) < 2:
        return {"n": 1, "mean": round(mean, 6), "sem": None}
    var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
    return {"n": len(xs), "mean": round(mean, 6), "sem": round(math.sqrt(var / len(xs)), 6)}


def summarize(run_dirs) -> dict:
    """Rows per run and one aggregate per competition (`task_id`): mean ± SEM of the raw and the
    adjusted number, medal / above-median rates, the mean percentile, the seed count."""
    rows = [run_facts(rd) for rd in run_dirs]
    groups: dict = {}
    for row in rows:
        groups.setdefault(row["task_id"], []).append(row)
    per_competition = []
    for task_id, members in sorted(groups.items()):
        reports = [m["report"] for m in members if m["report"]]
        seeds = {m["seed"] for m in members if m["seed"] is not None}
        per_competition.append({
            "task_id": task_id, "runs": len(members), "seeds": len(seeds),
            "seed_protocol_met": len(seeds) >= 3,
            "raw": mean_sem([m["raw"] for m in members]),
            "adjusted": mean_sem([m["adjusted"] for m in members]),
            "private_grade": mean_sem([m["private_grade"] for m in members]),
            "percentile": mean_sem([r.get("percentile") for r in reports]),
            "any_medal_rate": _rate([r.get("any_medal") for r in reports]),
            "above_median_rate": _rate([r.get("above_median") for r in reports]),
            "rule_violations": sum(1 for m in members
                                   if (m["extras"] or {}).get("rule_violation") == "violation"),
        })
    return {"version": 1, "runs": rows, "competitions": per_competition}


def _rate(flags) -> Optional[float]:
    known = [bool(f) for f in flags if f is not None]
    return round(sum(known) / len(known), 4) if known else None


def _fmt(ms: dict) -> str:
    if ms["mean"] is None:
        return "—"
    return f"{ms['mean']:.4f}" + (f" ± {ms['sem']:.4f}" if ms["sem"] is not None else "") + f" (n={ms['n']})"


def render(summary: dict) -> str:
    """The campaign table, one line per competition (the survey's Table 10 columns the protocol
    page names), then one line per run."""
    lines = ["competition | runs/seeds | raw mean ± SEM | Mislead-adjusted | private grade | "
             "percentile | medal rate | above-median | rule violations"]
    for c in summary["competitions"]:
        lines.append(f"{c['task_id']} | {c['runs']}/{c['seeds']}{'' if c['seed_protocol_met'] else ' (<3 seeds)'} | "
                     f"{_fmt(c['raw'])} | {_fmt(c['adjusted'])} | {_fmt(c['private_grade'])} | "
                     f"{_fmt(c['percentile'])} | {c['any_medal_rate']} | {c['above_median_rate']} | "
                     f"{c['rule_violations']}")
    lines.append("")
    for r in summary["runs"]:
        gap = (r["mislead"] or {}).get("gap") if r["mislead"] else None
        lines.append(f"  {r['run_id']:28s} seed={r['seed']} champion={r['champion']} raw={r['raw']} "
                     f"adjusted={r['adjusted']} gap={gap} private={r['private_grade']} "
                     f"protocol={r['protocol']} extras={r['extras']}")
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m looplab.adapters.mlebench_campaign",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("run_dirs", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true", help="print the whole summary as JSON")
    args = parser.parse_args(argv)
    missing = [str(rd) for rd in args.run_dirs if not (rd / "events.jsonl").is_file()]
    if missing:
        sys.stderr.write("no events.jsonl under: " + ", ".join(missing) + "\n")
        return 2
    summary = summarize(args.run_dirs)
    print(json.dumps(summary, indent=2) if args.json else render(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
