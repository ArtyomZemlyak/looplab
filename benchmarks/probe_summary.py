#!/usr/bin/env python3
"""The standing per-probe checklist, computed instead of hand-rolled.

Sweep item 9 asks the same seven questions of every finished probe -- champion, test against train,
money by phase, spend AFTER the last evaluated node, `eval_train` count, whether the model used the
reference module -- and for three sweeps running I answered them by writing the same throwaway
script each time. Each rewrite is a chance to compute a slightly different thing and call it the
same number, which is the failure this whole document keeps recording.

So it lives here, once. Two of the columns are not on the checklist and are here because the corpus
put them there:

  * SPEND BEFORE THE FIRST EVALUATED NODE. §72 found the waste metric pointed at the wrong end of
    the run: `remPde` read 11 % by "spend after the last node" while being the worst run on its
    task, having spent 91 % before its first. The pair is only legible together.
  * NODES. Across the ten scored probes on this box, the five that reached a second evaluated node
    hold the top score of every task they belong to, with one exception (`accPde`, 120.76 on one
    node). That is five points and an exception, not a rule -- printed so the next probe can
    refute it rather than so it can be believed.

Usage:  probe_summary.py [ROOT ...]        (default: BENCH_ROOT and the runs-archive)
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


def _meter_by_arm(path: str | None = None) -> dict:
    """Which INSTRUMENT each probe ran on, from the meter ledger — the only place it is recorded.

    A probe tree says what the run did and nothing about the gateway it did it through. Measured
    2026-09-01: `accEE`, `accPde` and `remEE` ran entirely UNSTREAMED, before the bench profile was
    made to set `LOOPLAB_LLM_STREAM` rather than default it. Without streaming the gateway's nginx
    times the whole generation against a 300 s window, and `remEE` lost 9 calls to it — 45 minutes
    of wall clock returning nothing, on a $1.00 budget.

    That is invisible in every score, card and run log, and §73 had built a "controlled pair" on
    `remEE` against a streamed run before anyone looked (§80). So it belongs in the summary that
    every comparison is drawn from.
    """
    p = Path(path or os.environ.get("LOOPLAB_METER_LOG")
             or (os.environ.get("BENCH_ROOT") or "/var/tmp/looplab-bench") + "/meter/meter.jsonl")
    out: dict = {}
    if not p.is_file():
        return out
    with open(p, errors="replace") as fh:
        for line in fh:
            if '"arm"' not in line:
                continue
            try:
                r = json.loads(line)
            except Exception:       # noqa: BLE001 - a torn tail is normal on a LIVE meter
                continue
            arm = r.get("arm")
            if not arm:
                continue
            d = out.setdefault(arm, {"streamed": 0, "unstreamed": 0, "ceiling": 0})
            # PREFLIGHTS ARE EXCLUDED. `agents/preflight.py` sets stream=False by design and sends
            # ten tokens; counting them would put every probe on "mostly streamed, some not" and
            # hide the three that are genuinely on the other instrument.
            if (r.get("prompt_tokens") or 0) > 1000:
                d["streamed" if r.get("stream") else "unstreamed"] += 1
            if r.get("status") == 504:
                d["ceiling"] += 1
    return out


def _roots(argv: list[str]) -> list[Path]:
    if argv:
        return [Path(a) for a in argv if Path(a).is_dir()]
    cands = [os.environ.get("BENCH_ROOT") or "/var/tmp/looplab-bench",
             os.environ.get("SNAPSHOT_RUNS_ARCHIVE")
             or "/home/jovyan/data/looplab-bench/runs-archive"]
    return [Path(c) for c in cands if Path(c).is_dir()]


def _load(path: Path) -> list[dict]:
    rows = []
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:       # noqa: BLE001 - a torn last line is normal on a LIVE run
                    continue
    except OSError:
        return []
    return rows


def _test_score(probe_dir: Path) -> float | None:
    """The champion's TEST speedup, from the probe's own final.json."""
    for name in ("final.json",):
        f = probe_dir / name
        if not f.is_file():
            continue
        try:
            j = json.loads(f.read_text(errors="replace"))
        except Exception:               # noqa: BLE001
            continue
        sp = j.get("speedup")
        return float(sp) if isinstance(sp, (int, float)) else None
    return None


def _why_no_test(probe_dir: Path) -> str:
    """Why this probe has no TEST score, in its own words, or "".

    A missing score is not one fact. `accEE` had none because its champion extraction died on
    `ModuleNotFoundError: No module named 'looplab'` (fixed 2.5 hours after that run, in d3d41531)
    on top of a run that had AUTO-PAUSED on a crashed Developer session -- and finding that out took
    six commands, when both sentences were sitting in the probe's own two log files. Same shape as
    the zeros section in `bench_runs_report.sh`: the diagnosis exists, in a file nothing read.

    `run_finished` is deliberately NOT the discriminator. It is absent from accPde, remDL3, remEE
    and remEE2, all of which scored a test perfectly well, so its absence means nothing on its own --
    which is the sort of thing that reads as a signal until you count it.
    """
    for name, hunt in (("probe.log", ("could not fold", "чемпион: НЕТ", "champion: NONE")),
                       ("run.log", ("PAUSED", "pause reason", "finished=False"))):
        f = probe_dir / name
        if not f.is_file():
            continue
        lines = f.read_text(errors="replace").splitlines()
        for needle in hunt:
            for line in lines:
                if needle in line:
                    return line.strip()[:120]
    return ""


def summarise(run_dir: Path) -> dict | None:
    events = _load(run_dir / "events.jsonl")
    spans = _load(run_dir / "spans.jsonl")
    if not events:
        return None
    gens = [r for r in spans if r.get("name") == "generation"]
    total = sum(float((r.get("attributes") or {}).get("cost") or 0) for r in gens)
    nodes = [r for r in events if r.get("type") == "node_evaluated"]
    stamps = [float(r.get("ts") or 0) for r in nodes]
    first, last = (min(stamps), max(stamps)) if stamps else (None, None)

    def spend(pred) -> float:
        return sum(float((r.get("attributes") or {}).get("cost") or 0)
                   for r in gens if pred(float(r.get("start") or 0)))

    before = spend(lambda t: first is not None and t < first)
    after = spend(lambda t: last is not None and t > last)

    by_phase: dict[str, float] = defaultdict(float)
    calls: Counter = Counter()
    for r in gens:
        a = r.get("attributes") or {}
        ph = a.get("phase") or "?"
        by_phase[ph] += float(a.get("cost") or 0)
        calls[ph] += 1

    # TIME TO THE FIRST BUILD STEP. Measured 2026-09-01 across eight runs, this is the one quantity
    # that separates cleanly, and it separates by TASK rather than by run: discrete_log reaches its
    # first `plan_step` at 64-74 minutes, edge_expansion at 18-21, pde_heat1d at 29-33. Two live
    # discrete_log probes at 55 minutes with no build looked stuck until this was computed; they were
    # on schedule for their task. Three summary statistics in three sweeps failed to order the
    # scores (§74.3); this one is not offered as a predictor of score, it is offered because a sweep
    # that cannot tell "slow task" from "stuck run" wastes an investigation every time.
    starts = [float(r.get("start") or 0) for r in spans if r.get("start") is not None]
    t0 = min(starts) if starts else None
    build_ts = [float(r.get("start") or 0) for r in gens
                if (r.get("attributes") or {}).get("phase") == "plan_step"]
    to_build = ((min(build_ts) - t0) / 60.0) if (build_ts and t0 is not None) else None

    tools_all = [r for r in spans if r.get("name") == "tool"]
    tools = tools_all
    dev = [r for r in tools
           if str((r.get("attributes") or {}).get("tool") or "") == "run_dev_command"]
    eval_train = sum(1 for r in dev if "eval_train" in json.dumps(r.get("attributes") or {}))

    blob = json.dumps(spans)
    ref_imports = len(re.findall(r"(?:from|import)\s+reference_\w+", blob))
    ref_calls = len(re.findall(r"\b(?:is_solution|generate_problem)\s*\(", blob))

    # AND THE SAME THING IN THE UNITS THE ACCEPTANCE CRITERION USES. §69.1 pinned the comparison
    # before its data arrived -- "against 4.9-8.3 %, not against 3.0 %" -- and that band is a share
    # of `run_probe` CALLS, not a raw count of regex hits. Reporting counts against a percentage
    # baseline is the same different-denominators mistake this file keeps catching elsewhere, and it
    # sat in this tool for three sweeps.
    probes = [r for r in tools_all
              if str((r.get("attributes") or {}).get("tool") or "") == "run_probe"]
    ref_pct = ref_call_pct = None
    if probes:
        hit_i = sum(1 for r in probes
                    if re.search(r"(?:from|import)\s+reference_\w+", json.dumps(r.get("attributes") or {})))
        hit_c = sum(1 for r in probes
                    if re.search(r"\b(?:is_solution|generate_problem)\s*\(", json.dumps(r.get("attributes") or {})))
        ref_pct = 100.0 * hit_i / len(probes)
        ref_call_pct = 100.0 * hit_c / len(probes)

    probe_dir = Path(str(run_dir).split("/runs/", 1)[0]) if "/runs/" in str(run_dir) else run_dir
    champ = probe_dir / "champion_solver.py"
    kernel = False
    champ_lines = 0
    if champ.is_file():
        body = champ.read_text(errors="replace")
        champ_lines = body.count("\n")
        kernel = bool(re.search(r"import numba|@njit|cimport|import cython", body))
    if not kernel and probe_dir.is_dir():
        kernel = kernel or any(p.suffix == ".pyx" for p in probe_dir.glob("*"))

    return {
        "probe": probe_dir.name,
        "task": run_dir.parent.name,
        "spent": total,
        "test": _test_score(probe_dir),
        "nodes": [round(float((r.get("data") or {}).get("metric") or 0), 4) for r in nodes],
        "before_pct": (100 * before / total) if total else 0.0,
        "after_pct": (100 * after / total) if total else 0.0,
        "eval_train": eval_train,
        "dev_commands": len(dev),
        "ref_imports": ref_imports,
        "ref_calls": ref_calls,
        "run_probe": len(probes),
        "ref_pct": ref_pct,
        "ref_call_pct": ref_call_pct,
        "champion_lines": champ_lines,
        "kernel": kernel,
        "to_build_min": to_build,
        "probe_dir": str(probe_dir),
        "why_no_test": "" if _test_score(probe_dir) is not None else _why_no_test(probe_dir),
        "phases": sorted(by_phase.items(), key=lambda kv: -kv[1])[:4],
        "calls": calls,
    }


def main(argv: list[str]) -> int:
    roots = _roots(argv)
    if not roots:
        print("no bench roots on this box", file=sys.stderr)
        return 1
    meter = _meter_by_arm()
    seen: dict[str, dict] = {}
    for root in roots:
        for ev in sorted(root.rglob("events.jsonl")):
            s = summarise(ev.parent)
            # The live tree and the archive hold the SAME run; keep whichever has more spend, which
            # is the fresher copy. Reporting one probe twice is how the zeros section went wrong.
            if s and (s["probe"] not in seen or s["spent"] > seen[s["probe"]]["spent"]):
                seen[s["probe"]] = s
    if not seen:
        print("no probes on this box")
        return 0

    print(f"{'probe':10s}{'task':16s}{'$':>8}{'TEST':>10}{'nodes':>7}"
          f"{'before%':>9}{'after%':>8}{'eval_tr':>8}{'->build':>9}  champion")
    for s in sorted(seen.values(), key=lambda x: (x["task"], -(x["test"] or -1))):
        test = f"{s['test']:.4f}" if s["test"] is not None else "-"
        champ = (f"{s['champion_lines']}L {'kernel' if s['kernel'] else 'plain python'}"
                 if s["champion_lines"] else "(none)")
        tb = f"{s['to_build_min']:.0f}m" if s["to_build_min"] is not None else "-"
        print(f"{s['probe']:10s}{s['task']:16s}{s['spent']:>8.4f}{test:>10}"
              f"{len(s['nodes']):>7}{s['before_pct']:>8.0f}%{s['after_pct']:>7.0f}%"
              f"{s['eval_train']:>8}{tb:>9}  {champ}")

    unscored = [s for s in seen.values() if s["test"] is None and s["why_no_test"]]
    if unscored:
        print("\nprobes with NO test score, and why (from the probe's own logs):")
        for s in sorted(unscored, key=lambda x: x["probe"]):
            print(f"  {s['probe']:10s} {s['why_no_test']}")
            # RECOVERABLE FOR FREE, and say so with the command. A run that spent its budget and
            # reached an evaluated node has already paid for everything expensive; extraction and
            # the test pass cost CPU and nothing else. `accEE` sat unscored for twenty hours after
            # an import bug that was fixed the same morning, and recovering it on 2026-09-01 took
            # two commands and $0 -- the score came back 224.8846, within 0.2 % of the figure the
            # operator brief had been carrying with no evidence behind it on this box.
            #
            # NOT `looplab resume`: that continues the RUN and spends more money, and accEE had
            # already spent $1.0042 of its $1.00, so resuming would break the budget contract that
            # makes it comparable. The cheap half is the only half that is missing.
            if s["nodes"]:
                print(f"             recoverable for $0 -- it has {len(s['nodes'])} evaluated "
                      f"node(s) and its budget is already spent:")
                print(f"               cd {s['probe_dir']} && python "
                      f"benchmarks/algotune/extract_champion.py --run-dir runs/{s['task']}/run "
                      f"--all-files --out champion_solver.py")
                print(f"               ALGOTUNE_BASELINE_CACHE_DIR=<.baseline_times> "
                      f"ALGOTUNE_EVAL_WORKERS=auto looplab_eval.py --task {s['task']} "
                      f"--solver champion_solver.py --subset test")

    odd = []
    for s in seen.values():
        m = meter.get(s["probe"])
        if not m:
            continue
        if m["unstreamed"] and not m["streamed"]:
            odd.append((s["probe"], m, "UNSTREAMED — nginx timed whole generations at 300 s"))
        elif m["ceiling"]:
            odd.append((s["probe"], m, "streamed, but hit the gateway ceiling"))
    if odd:
        print("\nprobes NOT on the current instrument (from the meter, not the probe tree):")
        for probe, m, why in sorted(odd):
            lost = f", {m['ceiling']} call(s) killed at the 300 s ceiling" if m["ceiling"] else ""
            print(f"  {probe:10s} {m['unstreamed']} unstreamed / {m['streamed']} streamed"
                  f"{lost} — {why}")

    print("\nper-probe detail:")
    for s in sorted(seen.values(), key=lambda x: (x["task"], x["probe"])):
        pct = ("—" if s["ref_pct"] is None
               else f"{s['ref_pct']:.1f}% import / {s['ref_call_pct']:.1f}% is_solution")
        print(f"  {s['probe']} ({s['task']}) nodes(train)={s['nodes']}  "
              f"reference over {s['run_probe']} run_probe calls: {pct}   "
              f"(§69.1 baseline 4.9-8.3 %)")
        for ph, cost in s["phases"]:
            share = 100 * cost / s["spent"] if s["spent"] else 0
            print(f"      {ph:16s}{s['calls'][ph]:>5} calls  ${cost:.4f}  {share:4.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
