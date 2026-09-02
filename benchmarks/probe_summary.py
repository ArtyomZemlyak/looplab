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
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path


def _meter_by_arm(path: str | None = None) -> dict:
    """Which INSTRUMENT each probe ran on, from the meter ledger — the only place it is recorded.

    A probe tree says what the run did and nothing about the gateway it did it through. Measured
    2026-09-01: `accEE`, `accPde`, `remEE` and the abandoned `remDL` ran entirely UNSTREAMED,
    before the bench profile was made to set `LOOPLAB_LLM_STREAM` rather than default it.
    Without streaming the gateway's nginx times the whole generation against a 300 s window.
    `remEE` lost 9 calls to it (45 minutes returning nothing on a $1.00 budget) and `remDL`
    lost ELEVEN -- more than any other run, and it is the one that produced no evaluated node
    at all. An earlier version of this paragraph named only the first three, which is how the
    worst-hit run stayed out of the sections that cite it.

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
            d = out.setdefault(arm, {"streamed": 0, "unstreamed": 0, "ceiling": 0, "unknown": 0})
            # PREFLIGHTS ARE EXCLUDED. `agents/preflight.py` sets stream=False by design and sends
            # ten tokens; counting them would put every probe on "mostly streamed, some not" and
            # hide the three that are genuinely on the other instrument.
            # A KILLED CALL IS STILL A CALL. All 21 status-504 rows in the ledger carry
            # `prompt_tokens: null` -- the usage frame never arrived -- so the >1000 filter dropped
            # them from BOTH counters while the ceiling counter, outside it, still fired. A probe
            # whose real calls ALL died read "0 unstreamed / 0 streamed ... streamed, but hit the
            # gateway ceiling": the verdict backwards. It understated the damage elsewhere too --
            # remEE's "309 unstreamed" excluded the 9 that died.
            big = (r.get("prompt_tokens") or 0) > 1000
            killed = r.get("status") == 504
            if big or killed:
                # `stream` ABSENT is unknown, not false: reading a missing key as "unstreamed"
                # manufactures a verdict out of a gap in the data.
                if "stream" not in r:
                    d["unknown"] += 1
                else:
                    d["streamed" if r.get("stream") else "unstreamed"] += 1
            if killed:
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
    # A DESTROYED result first, because it looks exactly like a run that has not finished and it is
    # the opposite. `remEEctl1` scored 35.0981 on 2026-09-01, printed it, and then had its
    # final.json truncated to ZERO BYTES by the run_probe.sh offset hazard (dcdf1f29): the shell,
    # resuming at a stale offset after the file grew under it, re-parsed the scoring block and
    # applied its `> "$OUT/final.json"` redirect to nothing. The summary called it "STILL RUNNING
    # (no stated reason)" -- a finished, fully paid probe filed under "not done yet", which is how a
    # lost dollar goes unnoticed. The champion survives, so the score is recoverable by re-scoring
    # it; saying so here is the difference between a recovery and a rerun.
    fin = probe_dir / "final.json"
    if fin.is_file() and fin.stat().st_size == 0:
        champ = probe_dir / "champion_solver.py"
        how = ("re-score it: the champion is preserved" if champ.is_file()
               else "and the champion is gone too")
        return f"final.json is ZERO BYTES -- the score was written and then destroyed; {how}"

    # The needle list is a convenience, not the gate -- every unscored probe is listed whether or
    # not one matches (see the caller). These are the phrasings seen on this box, and `remDL`'s is
    # here because it was the one the first six needles missed: nine attempts of exponential
    # backoff against a gateway returning 504, which is a diagnosis in as many words.
    for name, hunt in (("probe.log", ("could not fold", "чемпион: НЕТ", "champion: NONE",
                                      "ОТКАЗ", "Traceback")),
                       ("run.log", ("PAUSED", "pause reason", "finished=False",
                                    "answered HTTP", "attempt 9 of 9", "Traceback"))):
        f = probe_dir / name
        if not f.is_file():
            continue
        lines = f.read_text(errors="replace").splitlines()
        for needle in hunt:
            for line in lines:
                if needle in line:
                    return line.strip()[:120]
    return ""


def _card_of(probe: str, roots) -> tuple:
    """What the probe RECORDED about the card it was given, or `unrecorded`.

    Never inferred. A probe from before INSTRUMENT.txt existed cannot be shown to have run today's
    default card, and quietly pooling it with the ones that did is how two arms become one number.
    """
    for root in roots:
        rec = Path(root) / "model-probes" / probe / "INSTRUMENT.txt"
        if not rec.exists():
            rec = Path(root) / probe / "INSTRUMENT.txt"
        try:
            text = rec.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        args = sha = ""
        for line in text.splitlines():
            if line.startswith("card_args:"):
                args = line.split(":", 1)[1].strip()
            elif line.startswith("card_sha256:"):
                sha = line.split(":", 1)[1].strip()
        if args or sha:
            # THE FLAGS ARE THE GROUPING KEY, the hash is evidence about it. Keyed on the hash,
            # remEEctl1/2 and remEEctl3/4 landed in DIFFERENT rows on 2026-09-01 -- one arm, four
            # dollars, split in half because the record gained `card_sha256` between the second
            # probe and the third. An instrument that improves mid-arm must not silently partition
            # the arm it is measuring; that is the failure `card_sha256` was added to catch, wearing
            # the other hat.
            return (args or "(shipped card)", sha[:12])
    return ("unrecorded (pre-INSTRUMENT.txt)", "")


def summarise(run_dir: Path) -> dict | None:
    events = _load(run_dir / "events.jsonl")
    try:
        age_s = time.time() - (run_dir / "events.jsonl").stat().st_mtime
    except OSError:
        age_s = None
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

    # WAS THE GRADED CODE THE CHECKED CODE? Reconstructed from the tool spans, per evaluated node:
    # the last `check` before that node's evaluation, and whether any file WRITE landed after it.
    # See docs/56 §104 for the measurement this counts (33 % of such nodes score zero, against
    # 3.0 % of the rest, p = 0.0024) and for why this is reported rather than acted on.
    tool_spans = [r for r in spans if r.get("kind") == "tool"]
    checks, writes = [], []
    for r in tool_spans:
        attrs = r.get("attributes") or {}
        tool = attrs.get("tool")
        start = float(r.get("start") or 0)
        if tool == "run_dev_command":
            try:
                named = json.loads(attrs.get("input") or "{}").get("name")
            except (ValueError, TypeError):
                named = None
            if named == "check":
                checks.append(start)
        elif tool in ("write_file", "edit_surface", "apply_patch", "write"):
            writes.append(start)
    graded_unchecked = 0
    for ts in stamps:
        prior_check = max((c for c in checks if c < ts), default=None)
        if prior_check is None:
            continue                      # never checked at all is a different fact, not this one
        if any(prior_check < w < ts for w in writes):
            graded_unchecked += 1

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

    # AND HOW LONG THE BUILD ITSELF TAKES. The pair answers the question a sweep actually asks of a
    # quiet probe — "is this run stuck, or is this task slow?" — and neither half answers it alone.
    # Measured 2026-09-01 over 21 runs, first `plan_step` to first `node_evaluated`:
    #
    #     edge_expansion  5, 6, 7, 9, 11, 13, 14 min
    #     pde_heat1d      14, 25, 27, 33, 34, 35, 38, 38, 44 min
    #     discrete_log    23, 26, 27, 53, 54 min
    #
    # ROUGH, NOT A BAND. On 19 runs this read "edge_expansion 5-14, pde 25-44, DL 23-54, nothing
    # between 14 and 23", and that sentence stood in this comment for thirty minutes before
    # `remPde8` built in 14.0 and landed exactly in the gap. Verified at the source, and its plan was
    # 3 steps with 2 and 3 both no-ops — but that explains nothing: across all 21 runs the count of
    # steps that actually wrote correlates +0.02 with duration, and `remDL3` wrote in ZERO of its
    # three steps and took the longest build on record.
    #
    # Sixth summary statistic in six sweeps to look clean and then not be (§74.3, §76.1). The others
    # died on a larger sample; this one died on the next run, half an hour after being written down.
    # It does not predict the score either — within task r = -0.01 (EE), -0.12 (pde), -0.59 (DL).
    #
    # What it is still good for is the only thing it was added for: a probe 42 minutes into a build
    # on discrete_log has company, and one 42 minutes in on edge_expansion has none. Read it as
    # "has anything ever taken this long here", not as a bound.
    build_min = None
    if build_ts and stamps:
        first_node = min(stamps)
        b0 = min(build_ts)
        if first_node > b0:
            build_min = (first_node - b0) / 60.0

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
        "before_usd": before,
        # The ABSOLUTE figure beside the percentage, because they answer different questions and
        # only one of them is stable. `before_pct` moves for the whole life of a run: the same
        # $0.24 is 100 % at the first node and 24 % at the last. `before_usd` is fixed the moment
        # the first node lands, which is what makes an arm readable while it is still in flight.
        "before_pct": (100 * before / total) if total else 0.0,
        # Kept in the table rather than dropped: a run that NEVER evaluates is the outcome the
        # "measure early" clause is about, and excluding it censors the comparison in the direction
        # that flatters whichever arm fails to evaluate. See docs/56 §87.
        "reached_a_node": bool(nodes),
        "graded_unchecked": graded_unchecked,
        "graded_nodes": len(nodes),
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
        "build_min": build_min,
        "age_s": age_s,
        "probe_dir": str(probe_dir),
        # HOW OFTEN THE PROPOSER REPEATED ITSELF. Every one of the 43 rejections in the corpus
        # names `near_node` 0 or 1 -- the proposer, having produced the first node, proposed it
        # again. Measured 2026-09-02: 35 of 46 probes hit this at least once, and those probes
        # spend a median 10.2 % of their dollar on `repropose` against 2.0 % for the eleven that
        # did not. Not a defect -- the novelty check catches it every time, which is what it is
        # for -- but a per-probe number nobody could see.
        "novelty_rejected": sum(1 for r in events if r.get("type") == "novelty_rejected"),
        "trust_flags": sum(1 for r in events if r.get("type") == "reward_hack_suspected"),
        "trust_signals": [str((sig or {}).get("signal") or "")
                          for r in events if r.get("type") == "reward_hack_suspected"
                          for sig in ((r.get("data") or {}).get("signals") or [])],
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
          f"{'before%':>9}{'after%':>8}{'eval_tr':>8}{'->build':>9}{'build':>7}  champion")
    for s in sorted(seen.values(), key=lambda x: (x["task"], -(x["test"] or -1))):
        test = f"{s['test']:.4f}" if s["test"] is not None else "-"
        champ = (f"{s['champion_lines']}L {'kernel' if s['kernel'] else 'plain python'}"
                 if s["champion_lines"] else "(none)")
        tb = f"{s['to_build_min']:.0f}m" if s["to_build_min"] is not None else "-"
        bm = f"{s['build_min']:.0f}m" if s["build_min"] is not None else "-"
        # `after%` MEANS TWO DIFFERENT THINGS depending on whether the run is over, and the table
        # used to print them identically. For a finished probe it is waste: money spent after the
        # last node it will ever evaluate. For a RUNNING one it is just "time since the last node",
        # which grows until the next one lands and then collapses. On 2026-09-01 the live
        # `remEEctl5` showed 47 % beside `accEE`'s 41 % -- and accEE's 41 % is the real §75 waste
        # pattern on a finished run, while remEEctl5 had merely evaluated eight minutes earlier.
        # A `+` marks the figure as still accumulating; the legend below says so once.
        running = not (Path(s["probe_dir"]) / "champion_solver.py").is_file()
        after = f"{s['after_pct']:.0f}%" + ("+" if running else " ")
        print(f"{s['probe']:10s}{s['task']:16s}{s['spent']:>8.4f}{test:>10}"
              f"{len(s['nodes']):>7}{s['before_pct']:>8.0f}%{after:>8}"
              f"{s['eval_train']:>8}{tb:>9}{bm:>7}  {champ}")

    # The legend once, and only when it applies: a `+` on every row would be noise, and a legend
    # for a marker nothing carries teaches a reader to ignore legends.
    if any(not (Path(x["probe_dir"]) / "champion_solver.py").is_file() for x in seen.values()):
        print("  (after% marked + is STILL ACCUMULATING: the run has no champion yet, so the figure "
              "is time since the last node, not waste)")

    # EVERY unscored probe, not only those whose logs match one of six phrases. The needle list
    # matched NONE of the five probes carrying no TEST on 2026-09-01 -- including `remDL`, dead
    # since 13:46, whose run.log says `answered HTTP 504 (overloaded) -- waiting 30s before attempt
    # 7 of 9`. A section written because "the diagnosis is in a file nobody reads" was silent
    # because the diagnosis was phrased differently.
    unscored = [s for s in seen.values() if s["test"] is None]
    if unscored:
        print("\nprobes with NO test score, and why (from the probe's own logs):")
        for s in sorted(unscored, key=lambda x: x["probe"]):
            why = s["why_no_test"] or "(no stated reason in probe.log or run.log)"
            # A FRESH EVENT LOG IS NOT A RUNNING PROBE. `remEEctl2` finished at 15:56 with its
            # score destroyed, and this line called it "STILL RUNNING final.json is ZERO BYTES" --
            # the two halves contradicting each other in one sentence, six minutes after the run
            # ended. The age of events.jsonl says only that something happened recently, and a
            # probe that has produced a champion has by definition stopped producing nodes.
            finished = (Path(s["probe_dir"]) / "champion_solver.py").is_file()
            live = ("" if finished
                    else " -- STILL RUNNING" if s["age_s"] is not None and s["age_s"] < 2400
                    else "")
            print(f"  {s['probe']:10s}{live} {why}")
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
            # NOT for a run still writing: "its budget is already spent" was printed
            # unconditionally, and extracting a champion from a live run hands back an intermediate
            # result dressed as a final one.
            if s["nodes"] and not (s["age_s"] is not None and s["age_s"] < 2400):
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

    # THE CHAMPION LEDGER. docs/56 §84 measured this by hand and `remPde10` finished thirty
    # minutes later and moved every figure in it. A statistic that has to be re-typed after each
    # run is a statistic that will be quoted stale; this prints it from the corpus every time.
    # Ties (last node WAS the best) are counted and shown, because the sign test's denominator is
    # the non-ties and a reader who cannot see the ties cannot check the p.
    # SPEND BEFORE THE FIRST EVALUATED NODE, by card. The `KEEP_BEST` clause ends "Measure
    # early, measure often", so this is the quantity it names, and the arm that removes it is the
    # only way to find out whether saying so changes anything. Grouped by what the probe RECORDED
    # about its card (INSTRUMENT.txt); probes older than that record are pooled as "unrecorded",
    # which is honest about what can and cannot be compared rather than assuming they match the
    # default of today.
    by_card, hashes = {}, {}
    for s2 in seen.values():
        args, sha = _card_of(s2["probe"], roots)
        key = (s2["task"], args)
        by_card.setdefault(key, []).append(s2)
        if sha:
            hashes.setdefault(key, {}).setdefault(sha, []).append(s2["probe"])
    if len(by_card) > 1:
        print("\nspend before the FIRST evaluated node, by card "
              "(the quantity KEEP_BEST's 'measure early' names):")
        for (task, card), group in sorted(by_card.items()):
            got = sorted(g["before_usd"] for g in group if g["reached_a_node"])
            never = [g["probe"] for g in group if not g["reached_a_node"]]
            med = f"${statistics.median(got):.4f}" if got else "—"
            rng = f"${got[0]:.4f}-${got[-1]:.4f}" if got else "—"
            print(f"  {task:16s} {card:34s} n={len(got):2d} median {med:>9s}  range {rng}")
            seen_hashes = hashes.get((task, card), {})
            if len(seen_hashes) > 1:
                # Same flags, DIFFERENT card text: the flags did not change but the card did, so
                # this row pools runs that were not given the same thing. Loud, because it is
                # exactly the confound a flag column cannot see.
                print(f"  {'':16s} {'':34s} ! this row pools {len(seen_hashes)} DIFFERENT card "
                      f"texts under one set of flags: "
                      + "; ".join(f"{h}={','.join(sorted(v))}" for h, v in sorted(seen_hashes.items())))
            if never:
                # NAMED, not silently absent: "has not evaluated yet" and "never will" look
                # identical here, and both would otherwise leave the arm looking thriftier.
                print(f"  {'':16s} {'':34s} + {len(never)} run(s) with NO node yet, excluded "
                      f"and censoring this row: {', '.join(sorted(never))}")
            # AND THE SAME QUANTITY IN MINUTES. Time to the first BUILD step is the other unit
            # "measure early" comes in, and it is less censored than the dollars: a run that has
            # started building has a number even before it evaluates anything. NOT an independent
            # confirmation of the row above — one construct, two units — so it is printed beside
            # it rather than under a second heading.
            built = sorted(g["to_build_min"] for g in group
                           if isinstance(g.get("to_build_min"), (int, float)))
            if built:
                print(f"  {'':16s} {'':34s} to first build: n={len(built):2d} "
                      f"median {statistics.median(built):5.1f}m  range {built[0]:.0f}-{built[-1]:.0f}m")
            # REFERENCE USE, by card, with §69.1's band beside it. This is the PRE-REGISTERED
            # outcome of the `--no-reference-affordance` arm launched 2026-09-01, and it is printed
            # BEFORE that arm has any data on purpose: §84's lesson was that a figure typed by hand
            # after the fact is quoted stale, and the cheapest moment to make it a command is while
            # it is still empty. Runs with no `run_probe` call at all are excluded and counted --
            # a probe that never probed has no rate, and averaging it in as 0 % would push any arm
            # toward the floor for a reason unrelated to the clause.
            # SPLIT BY WHETHER THE RUN IS OVER, like `after%` two commits earlier. A rate over a
            # probe still working is a rate over the calls it has made SO FAR: the same four
            # probes read 2.1 % and then 1.7 % an hour apart, from nobody's edit. §93 computed
            # p = 0.0430 over three unfinished runs and one finished one against ten finished ones
            # and did not say so.
            #
            # NOT because a partial rate is biased -- I assumed that and measured otherwise. Over
            # the 18 finished edge_expansion runs, the rate across the first fifteen `run_probe`
            # calls has a median of 6.7 % against a final 7.8 %, and only 6 of the 18 understate.
            # It is NOISIER, not lower, over a denominator a third the size. Mixing the two is
            # still mixing two precisions in one median, which is what the marker makes visible.
            done_g = [g for g in group if (Path(g["probe_dir"]) / "champion_solver.py").is_file()]
            rates = sorted(g["ref_pct"] for g in done_g if g.get("ref_pct") is not None)
            partial = sorted(g["ref_pct"] for g in group
                             if g not in done_g and g.get("ref_pct") is not None)
            silent = [g["probe"] for g in group if g.get("ref_pct") is None]
            if rates:
                print(f"  {'':16s} {'':34s} reference use: n={len(rates):2d} "
                      f"median {statistics.median(rates):5.1f}%  range {rates[0]:.1f}-{rates[-1]:.1f}%"
                      f"   (§69.1 pre-clause band 4.9-8.3 %)")
            if partial:
                print(f"  {'':16s} {'':34s} + {len(partial)} run(s) STILL RUNNING, rate so far "
                      f"{', '.join(f'{x:.1f}%' for x in partial)} — not in the median above")
            if silent:
                print(f"  {'':16s} {'':34s} + {len(silent)} run(s) with NO run_probe call, so no "
                      f"rate: {', '.join(sorted(silent))}")

    # FINISHED RUNS ONLY. "Ended on a node that was not their best" is a sentence about a run that
    # ENDED, and a probe still working has a `last` that is only the last SO FAR -- one more node
    # can turn a move into a tie or a tie into a move. Measured 2026-09-01: exactly one of the 33
    # rows was live and it was a tie, so the headline p was untouched (it counts non-ties, all of
    # them finished) -- but the denominator said "runs" about a run that had not ended, and the
    # direction of that error is not fixed. Held-out rows are counted below rather than dropped
    # silently.
    multi = [s for s in seen.values() if len(s["nodes"]) >= 2]
    live_multi = [s for s in multi
                  if not (Path(s["probe_dir"]) / "champion_solver.py").is_file()]
    pairs = [(s["probe"], s["task"], max(s["nodes"]), s["nodes"][-1])
             for s in multi if s not in live_multi]
    if pairs:
        moved = [r for r in pairs if r[2] > r[3] * 1.001]
        zeros = [r for r in pairs if r[3] == 0]
        print("\nthe champion rule, over every run with more than one evaluated node:")
        for probe, task, best, last in sorted(pairs, key=lambda r: -(r[2] / r[3] if r[3] else 1e18)):
            ratio = "     inf" if not last else f"{best / last:8.2f}"
            flag = "  <- last node scored ZERO" if last == 0 else ""
            print(f"  {probe:10s} {task:16s} best {best:9.4f}  last {last:9.4f}  {ratio}x{flag}")
        # 0.5 ** n, spelled out: every non-tie moves the same way, so the one-sided sign test is
        # exactly the chance of n coin flips agreeing. No run can end above its own best.
        p_val = 0.5 ** len(moved) if moved else 1.0
        print(f"  {len(moved)} of {len(pairs)} runs ended on a node that was NOT their best "
              f"({len(pairs) - len(moved)} ties, {len(zeros)} ended on a ZERO)")
        if live_multi:
            print(f"  + {len(live_multi)} multi-node run(s) STILL RUNNING, held out because their "
                  f"last node is not final: {', '.join(sorted(s['probe'] for s in live_multi))}")
        print(f"  paired sign test over the {len(moved)} non-ties: one-sided p = {p_val:.6g}"
              f" = 1/{2 ** len(moved)}")
        print("  NOTE: this is the rule's PROTECTIVE value given the nodes these runs produced.")
        print("        Whether STATING the rule changes which nodes appear is a different question")
        print("        and needs the --no-unteachable-rules control arm; see docs/56 §83, §84.")

    # WAS THE GRADED CODE THE CHECKED CODE? Measured 2026-09-02 over 111 evaluated nodes whose
    # tool order could be reconstructed from the spans: a node whose last file WRITE came after its
    # last `check` scores ZERO four times in twelve; a node whose last write came before it scores
    # zero three times in ninety-nine. 33 % against 3.0 %, exact one-sided Fisher p = 0.0024.
    #
    # This is REPORTED, not acted on. Telling the Developer "you have edited since your last check"
    # is a behaviour change, and §92 is the standing answer to behaviour changes proposed off an
    # observational split: its effect is unmeasurable without an arm that lacks it. What a sweep can
    # do without an arm is make the quantity visible, so the next reader is not counting it by hand.
    unchecked = [(s2["probe"], s2["graded_unchecked"], s2["graded_nodes"])
                 for s2 in seen.values() if s2.get("graded_unchecked")]
    if unchecked:
        tot_u = sum(u for _, u, _ in unchecked)
        print(f"\nnodes graded on code written AFTER their last `check` ({tot_u} across "
              f"{len(unchecked)} probes; those nodes score zero 11x more often -- docs/56 §104):")
        for name, u, tot in sorted(unchecked, key=lambda r: -r[1]):
            print(f"  {name:11} {u} of {tot} evaluated node(s)")

    # TRUST FLAGS, which no sweep had ever looked at. The event log carries `reward_hack_suspected`
    # and it appears in no summary: found 2026-09-02 by counting event types rather than reading the
    # ones already named. Four in the corpus, all `critic:params_ignored` -- "none of the proposed
    # params are referenced in the code" -- and all on `discrete_log`: BOTH nodes of remDL2 and BOTH
    # nodes of remDL7, and remDL7's 16.7799 is the best discrete_log number this bench has.
    #
    # NOT a defect: those runs carry `trust_gate: audit`, the shipped default, under which a flag is
    # advisory and the node stays eligible to win. The engine did what it was configured to do. What
    # was wrong is that the standing brief calls discrete_log "the corpus's finest load-bearing
    # number" and nothing told a reader that its best run was flagged twice by the loop's own critic.
    flagged = {}
    for s2 in seen.values():
        n = s2.get("trust_flags") or 0
        if n:
            flagged[s2["probe"]] = (n, s2.get("trust_signals") or [])
    if flagged:
        print("\ntrust flags the loop raised on its own nodes (trust_gate=audit: advisory, "
              "the node still competes):")
        for probe, (n, sigs) in sorted(flagged.items()):
            print(f"  {probe:10s} {n} flag(s): {', '.join(sorted(set(sigs))) or '(unnamed)'}")

    print("\nper-probe detail:")
    for s in sorted(seen.values(), key=lambda x: (x["task"], x["probe"])):
        pct = ("—" if s["ref_pct"] is None
               else f"{s['ref_pct']:.1f}% import / {s['ref_call_pct']:.1f}% is_solution")
        nov = f"; proposer repeated itself {s['novelty_rejected']}x" if s.get("novelty_rejected") else ""
        print(f"  {s['probe']} ({s['task']}) nodes(train)={s['nodes']}  "
              f"reference over {s['run_probe']} run_probe calls: {pct}   "
              f"(§69.1 baseline 4.9-8.3 %){nov}")
        for ph, cost in s["phases"]:
            share = 100 * cost / s["spent"] if s["spent"] else 0
            print(f"      {ph:16s}{s['calls'][ph]:>5} calls  ${cost:.4f}  {share:4.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
