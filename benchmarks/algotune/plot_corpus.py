#!/usr/bin/env python3
"""Corpus-wide slices over the AlgoTune A/B bench: metric vs money, nodes, calls, tokens,
wall-clock, measurements, phase mix, and the difficulty hypothesis.

RUN IT (free cores only -- 0-43 and 48-91 carry live runs):

    taskset -c 44-47,92-95 python3 \
        /var/tmp/looplab-bench/looplab/benchmarks/algotune/plot_corpus.py

Reads only. Writes only into /var/tmp/looplab-bench/plots/. Makes no LLM calls.

WHAT IT READS, AND THE TRAPS IT ALREADY AVOIDS
----------------------------------------------
* `spans.jsonl`  -- `duration_s` / `start` (NOT duration_ms/start_time), `attributes.cost` on
  `generation` spans. Cost is read from the span, never estimated from tokens.
* `events.jsonl` -- a line is NOT an event: a crash-atomic batch carries `type` as a LIST
  `["__looplab_event_batch_v1__"]` and the real events sit in `data.events`. `iter_events`
  unrolls them; a naive reader silently loses whole nodes.
* prompts       -- reconstructed via `span_input.py` (`input_carry` is an INTEGER prefix length
  and `input` is only the SUFFIX when `input_from` is set). We only need lengths here, but we
  use the same resolver so the numbers are on the same ruler as doc 56.
* `score.log`   -- append-only, several JSON objects per file. Take the LAST with a non-null
  speedup, else the last object of any kind (same rule as `score_row.py`).
* test metric   -- `final.json` beside the probe, NOT the tail of `probe.log`: sol10's probe.log
  tail still says 0.0 while final.json carries 259.677 (doc 56 sec 15).
* arm A money   -- the log's own `You have $Y remaining` line is AlgoTuner's internal accounting;
  the gateway ledger `meter/meter.jsonl` (field `arm`, not `path`) is what was really paid.
  Both are reported; they disagree by up to 2.4x, see `A_SELFREPORT_VS_METER`.
* arm A ran 2-5 ATTEMPTS per task (`campaign-final/A-*.attempts`). The surviving `.log` is the
  LAST attempt, so meter rows are filtered to `ts >= last attempt epoch`.
* `camp-runs/` and `invalidated/` are EXCLUDED: 2026-08-20/21 arm B was measured on a broken
  ruler and must be discarded, not rescaled.

HONESTY RULES BAKED IN
----------------------
* every axis label carries n; no trend line is drawn for n < 3 (a NOT-A-RESULT note is stamped
  on the panel instead).
* ruler noise: ~10 % at high speedups, ~2 % at low ones (doc 56 sec 14). `NOISE_BAND` draws it
  and the text report refuses to call anything smaller an effect.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import textwrap
from collections import Counter, defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import span_input  # noqa: E402

ROOT = "/var/tmp/looplab-bench"
PLOTS = os.path.join(ROOT, "plots")
DATA = os.path.join(ROOT, "AlgoTune/.hf_datasets/oripress__AlgoTune/data")
BASELINES = os.path.join(ROOT, "looplab/benchmarks/algotune/.baseline_times")
TASKSRC = os.path.join(ROOT, "AlgoTune/AlgoTuneTasks")

# doc 56 sec 14: the +-2 % figure came from a pair at speedup ~2.2 and does not generalise up.
NOISE_HIGH, NOISE_LOW, NOISE_SPLIT = 0.10, 0.02, 10.0

# Corpus traps that are facts about the data, not about the reader. Kept as data so the
# report can print them rather than quietly applying them.
TRAPS = {
    "sol10": ("probe.log tail holds a stale 0.0 from the pre-fix extractor; final.json carries "
              "259.677 and an independent re-score gives 285.58 -- a 10 % spread on the same "
              "solver, which is what set NOISE_HIGH."),
    "ds3": ("test 0.0 is REAL: the solver fails on a data shape that exists only in test "
            "(eval_seconds 35.5, so the ruler ran). Not a ruler refusal."),
    "dsNew2": ("stopped early on a session crash, not on budget -- NOT a valid control run."),
}
SOL10_INDEPENDENT_RESCORE = 285.58

MEASURE_TOOLS = {"run_dev_command", "run_probe"}   # LoopLab: agent-initiated measurements
CODE_PHASES = {"plan", "plan_step"}                # doc 56 sec 1: money that writes code


# --------------------------------------------------------------------------- helpers
def jload(path, default=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return json.load(fh)
    except Exception:
        return default


def iter_events(path):
    """Yield real events. Crash-atomic packets carry `type` as a one-element LIST and hide the
    events in data.events; unrolling is mandatory or whole nodes vanish from the count."""
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec.get("type"), list):
                for ev in (rec.get("data") or {}).get("events") or []:
                    yield ev
            else:
                yield rec


def read_score_log(path):
    """LAST record with a usable speedup, else the last record of any kind (score_row.py rule)."""
    best = last = None
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            last = rec
            if rec.get("speedup") is not None:
                best = rec
    return best or last


# --------------------------------------------------------------------------- task features
def task_features():
    feats = {}
    for task in sorted(os.listdir(DATA)):
        d = os.path.join(DATA, task)
        if not os.path.isdir(d):
            continue
        n = tgt = None
        for fn in os.listdir(d):
            m = re.match(r"^.+_T(\d+)ms_n(\d+)_size(\d+)_(train|test)\.jsonl$", fn)
            if m:
                tgt, n = int(m.group(1)), int(m.group(2))
                break
        base = os.path.join(BASELINES, f"{task}__test__w22x1r3.json")
        if not os.path.exists(base):
            cands = [f for f in os.listdir(BASELINES) if f.startswith(task + "__test__")]
            base = os.path.join(BASELINES, sorted(cands)[0]) if cands else None
        bt = jload(base, {}) if base else {}
        times = [v for v in bt.values() if isinstance(v, (int, float))]
        src = os.path.join(TASKSRC, task, f"{task}.py")
        loc = 0
        if os.path.exists(src):
            with open(src, encoding="utf-8", errors="replace") as fh:
                loc = sum(1 for ln in fh if ln.strip() and not ln.strip().startswith("#"))
        feats[task] = dict(
            task=task, n_param=n, target_ms=tgt,
            baseline_ms=float(np.mean(times)) if times else None,
            baseline_ms_med=float(np.median(times)) if times else None,
            n_instances=len(times),
            # how long ONE full reference pass over the test split takes: this, not the
            # champion's eval_seconds, is the task's intrinsic "long evaluation" cost --
            # eval_seconds also contains however fast the submitted solver happened to be.
            ref_eval_s=(float(np.sum(times)) / 1000.0) if times else None,
            ref_loc=loc,
        )
    return feats


# --------------------------------------------------------------------------- LoopLab runs
def scan_looplab_run(rundir):
    """One LoopLab run directory -> a flat row. All money from spans, never from tokens."""
    spans_path = os.path.join(rundir, "spans.jsonl")
    if not os.path.exists(spans_path):
        return None
    spans, by_id = span_input.load(spans_path)

    cost = 0.0
    gens = 0
    ptok = ctok = rtok = 0
    models = Counter()
    phase_cost = defaultdict(float)
    phase_gens = Counter()
    dur = defaultdict(float)
    tools = Counter()
    tool_time = defaultdict(float)
    t0, t1 = math.inf, 0.0

    for sp in spans:
        name = sp.get("name")
        a = sp.get("attributes") or {}
        st, du = sp.get("start"), sp.get("duration_s") or 0.0
        if isinstance(st, (int, float)):
            t0, t1 = min(t0, st), max(t1, st + du)
        dur[name] += du
        if name == "generation":
            gens += 1
            c = a.get("cost") or 0.0
            cost += c
            ph = a.get("phase") or "?"
            phase_cost[ph] += c
            phase_gens[ph] += 1
            models[a.get("model")] += 1
            u = a.get("usage") or {}
            # LoopLab writes usage as {prompt, completion, total}; older/other writers use
            # the OpenAI names. Accept both rather than silently reporting zero tokens.
            ptok += u.get("prompt") or u.get("prompt_tokens") or u.get("input_tokens") or 0
            ctok += u.get("completion") or u.get("completion_tokens") or u.get("output_tokens") or 0
            rtok += u.get("reasoning") or u.get("reasoning_tokens") or 0
        elif name == "tool":
            tools[a.get("tool")] += 1
            tool_time[a.get("tool")] += du

    dev_cmds = Counter()
    for sp in spans:
        a = sp.get("attributes") or {}
        if sp.get("name") == "tool" and a.get("tool") == "run_dev_command":
            try:
                dev_cmds[json.loads(a.get("input") or "{}").get("name")] += 1
            except Exception:
                dev_cmds["?"] += 1

    nodes = []
    ndir = os.path.join(rundir, "nodes")
    if os.path.isdir(ndir):
        for nm in sorted(os.listdir(ndir), key=lambda s: int(re.sub(r"\D", "", s) or -1)):
            rec = read_score_log(os.path.join(ndir, nm, "score.log"))
            if rec is None:
                continue
            nodes.append(dict(node=nm, speedup=rec.get("speedup"),
                              eval_seconds=rec.get("eval_seconds"), subset=rec.get("subset")))

    ev = Counter()
    for e in iter_events(os.path.join(rundir, "events.jsonl")):
        ev[e.get("type")] += 1

    wall = (t1 - t0) if t0 < math.inf else 0.0
    return dict(
        cost=cost, gens=gens, prompt_tokens=ptok, completion_tokens=ctok,
        reasoning_tokens=rtok, model=(models.most_common(1)[0][0] if models else None),
        phase_cost=dict(phase_cost), phase_gens=dict(phase_gens),
        wall_s=wall, llm_s=dur.get("generation", 0.0),
        eval_s=dur.get("evaluate", 0.0) + dur.get("score", 0.0),
        tool_s=dur.get("tool", 0.0),
        tools=dict(tools), tool_time=dict(tool_time), dev_cmds=dict(dev_cmds),
        nodes=nodes, n_nodes=len(nodes),
        node_evaluated=ev.get("node_evaluated", 0),
        measure_calls=sum(tools.get(t, 0) for t in MEASURE_TOOLS),
        measure_s=sum(tool_time.get(t, 0.0) for t in MEASURE_TOOLS),
        n_events=sum(ev.values()),
    )


def collect_looplab():
    rows = []
    # -- single-task probes ------------------------------------------------------------
    pdir = os.path.join(ROOT, "model-probes")
    for label in sorted(os.listdir(pdir)):
        base = os.path.join(pdir, label)
        runs = os.path.join(base, "runs")
        if not os.path.isdir(runs):
            continue
        for task in sorted(os.listdir(runs)):
            rundir = os.path.join(runs, task, "run")
            row = scan_looplab_run(rundir)
            if row is None:
                continue
            fin = jload(os.path.join(base, "final.json"))
            plog = os.path.join(base, "probe.log")
            budget = None
            champ_node = None
            if os.path.exists(plog):
                txt = open(plog, encoding="utf-8", errors="replace").read()
                m = re.search(r"(?:бюджет|лимит(?: расхода записи \S+)?)\s*\$([0-9.]+)", txt)
                if m:
                    budget = float(m.group(1))
                m = re.search(r"champion node (\d+)", txt)
                if m:
                    champ_node = int(m.group(1))
            row.update(arm="LoopLab", source="probe", label=label, task=task,
                       budget=budget, champion_node=champ_node,
                       test_speedup=(fin or {}).get("speedup"),
                       test_eval_s=(fin or {}).get("eval_seconds"),
                       has_final=fin is not None,
                       trap=TRAPS.get(label))
            rows.append(row)
    # -- the 20-task arm-B campaign ----------------------------------------------------
    cdir = os.path.join(ROOT, "runs-B")
    for task in sorted(os.listdir(cdir)):
        rundir = os.path.join(cdir, task, "run")
        row = scan_looplab_run(rundir)
        if row is None:
            continue
        fin = jload(os.path.join(ROOT, "campaign-final", f"B-{task}.final.json"))
        res = jload(os.path.join(ROOT, "rescored", f"{task}.json"))
        row.update(arm="LoopLab", source="campaign-B", label="B", task=task, budget=1.0,
                   champion_node=None,
                   test_speedup=(fin or res or {}).get("speedup"),
                   test_eval_s=(fin or res or {}).get("eval_seconds"),
                   rescored=(res or {}).get("speedup"),
                   has_final=(fin is not None or res is not None), trap=None)
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- arm A
A_EVAL_RE = re.compile(r"Running full dataset evaluation on '(\w+)' subset for command '(\w+)'")


def collect_algotuner(meter_rows):
    rows = []
    cf = os.path.join(ROOT, "campaign-final")
    for fn in sorted(os.listdir(cf)):
        if not (fn.startswith("A-") and fn.endswith(".log")):
            continue
        task = fn[2:-4]
        path = os.path.join(cf, fn)
        msgs = spend_self = test = None
        cmds = Counter()
        eval_input = profile = 0
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "You have sent " in line:
                    m = re.search(r"You have sent (\d+) messages and have used up \$([0-9]+(?:\.[0-9]+)?)", line)
                    if m:
                        msgs, spend_self = int(m.group(1)), float(m.group(2))
                if "Running full dataset evaluation" in line:
                    m = A_EVAL_RE.search(line)
                    if m:
                        cmds[f"{m.group(2)}/{m.group(1)}"] += 1
                if "Running _runner_eval_input" in line:
                    eval_input += 1
                if "Running _runner_profile" in line:
                    profile += 1
                if "Using test dataset speedup for summary:" in line:
                    v = line.rsplit(":", 1)[1].strip()
                    test = None if v in ("None", "N/A") else float(v)
        att = os.path.join(cf, f"A-{task}.attempts")
        epochs = []
        if os.path.exists(att):
            epochs = [int(x) for x in re.findall(r"epoch=(\d+)", open(att).read())]
        last = epochs[-1] if epochs else 0
        mrows = [r for r in meter_rows
                 if r.get("arm") == "A" and r.get("task") == task and r.get("ts", 0) >= last]
        allrows = [r for r in meter_rows if r.get("arm") == "A" and r.get("task") == task]
        ts = [r["ts"] for r in mrows if r.get("ts")]
        rows.append(dict(
            arm="AlgoTuner", source="campaign-A", label="A", task=task,
            model="deepseek-v4-flash", budget=1.0,
            cost=sum(r.get("cost") or 0.0 for r in mrows),
            cost_all_attempts=sum(r.get("cost") or 0.0 for r in allrows),
            cost_selfreported=spend_self, attempts=len(epochs),
            gens=len(mrows), messages=msgs,
            prompt_tokens=sum(r.get("prompt_tokens") or 0 for r in mrows),
            completion_tokens=sum(r.get("completion_tokens") or 0 for r in mrows),
            reasoning_tokens=0,
            wall_s=(max(ts) - min(ts)) if len(ts) > 1 else 0.0,
            llm_s=sum((r.get("latency_ms") or 0) for r in mrows) / 1000.0,
            eval_s=0.0, tool_s=0.0,
            measure_calls=sum(cmds.values()) + eval_input + profile,
            measure_s=0.0,
            a_cmds=dict(cmds), a_eval_input=eval_input, a_profile=profile,
            n_nodes=None, nodes=[], phase_cost={}, phase_gens={}, tools={}, dev_cmds={},
            test_speedup=test, has_final=test is not None, trap=None,
        ))
    # the one arm-A probe on a stronger model (doc 55/56: 338.26 on edge_expansion)
    solA = os.path.join(ROOT, "algotuner-probes/solA/run.log")
    if os.path.exists(solA):
        msgs = spend_self = test = None
        cmds = Counter()
        eval_input = profile = 0
        with open(solA, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "You have sent " in line:
                    m = re.search(r"You have sent (\d+) messages and have used up \$([0-9]+(?:\.[0-9]+)?)", line)
                    if m:
                        msgs, spend_self = int(m.group(1)), float(m.group(2))
                if "Running full dataset evaluation" in line:
                    m = A_EVAL_RE.search(line)
                    if m:
                        cmds[f"{m.group(2)}/{m.group(1)}"] += 1
                if "Running _runner_eval_input" in line:
                    eval_input += 1
                if "Running _runner_profile" in line:
                    profile += 1
                if "Using test dataset speedup for summary:" in line:
                    v = line.rsplit(":", 1)[1].strip()
                    test = None if v in ("None", "N/A") else float(v)
        mrows = [r for r in meter_rows if r.get("arm") == "solA"]
        ts = [r["ts"] for r in mrows if r.get("ts")]
        rows.append(dict(
            arm="AlgoTuner", source="probe-A", label="solA", task="edge_expansion",
            model="openai/gpt-5.6-sol", budget=1.0,
            cost=sum(r.get("cost") or 0.0 for r in mrows) or None,
            cost_all_attempts=None, cost_selfreported=spend_self, attempts=1,
            gens=len(mrows), messages=msgs,
            prompt_tokens=sum(r.get("prompt_tokens") or 0 for r in mrows),
            completion_tokens=sum(r.get("completion_tokens") or 0 for r in mrows),
            reasoning_tokens=0,
            wall_s=(max(ts) - min(ts)) if len(ts) > 1 else 0.0,
            llm_s=sum((r.get("latency_ms") or 0) for r in mrows) / 1000.0,
            eval_s=0.0, tool_s=0.0,
            measure_calls=sum(cmds.values()) + eval_input + profile, measure_s=0.0,
            a_cmds=dict(cmds), a_eval_input=eval_input, a_profile=profile,
            n_nodes=None, nodes=[], phase_cost={}, phase_gens={}, tools={}, dev_cmds={},
            test_speedup=test, has_final=test is not None, trap=None,
        ))
    return rows


def load_meter():
    rows = []
    for fn in ("meter/meter.jsonl", "meter/meter-gemini.jsonl"):
        p = os.path.join(ROOT, fn)
        if not os.path.exists(p):
            continue
        for line in open(p, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


# =========================================================================== plotting
# Palette: categorical slots 1-3 of the validated default (blue / orange / aqua). Only the
# first three slots clear the all-pairs CVD floors, and every panel here is a scatter, so the
# series count is capped at three by construction. Identity is never colour-alone: every
# series is also in the legend and the notable points carry direct text labels.
SURFACE = "#fcfcfb"
INK, INK2, INK3 = "#0b0b0b", "#52514e", "#8a8880"
C_LOOP, C_ALGO, C_THIRD = "#2a78d6", "#eb6834", "#1baf7a"
C_STATUS_BAD = "#e34948"
ARM_COLOR = {"LoopLab": C_LOOP, "AlgoTuner": C_ALGO}


def style():
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": "#d9d8d2", "axes.labelcolor": INK2, "text.color": INK,
        "xtick.color": INK2, "ytick.color": INK2, "font.size": 9,
        "axes.titlesize": 11, "axes.titleweight": "bold", "axes.titlecolor": INK,
        "grid.color": "#eceae4", "grid.linewidth": 0.8, "legend.frameon": False,
        "axes.spines.top": False, "axes.spines.right": False, "figure.dpi": 130,
    })


def finish(ax, title, xlabel, ylabel, note=None, legend=True, legend_loc="best", pad=0.20):
    """Titles, a recessive grid, a legend whenever there is more than one labelled series, and
    a wrapped footnote. The note is WRAPPED to the axes width -- an unwrapped matplotlib text
    runs off the canvas and silently overlaps the neighbouring panel."""
    ax.set_title(title, loc="left", pad=10)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.grid(True, alpha=0.9, zorder=0)
    ax.set_axisbelow(True)
    if legend and ax.get_legend_handles_labels()[0]:
        if legend_loc == "below":
            ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=3,
                      fontsize=7.6, labelcolor=INK2)
        else:
            ax.legend(loc=legend_loc, fontsize=7.6, labelcolor=INK2)
    if note:
        fig = ax.get_figure()
        inches = ax.get_position().width * fig.get_figwidth()
        width = max(40, int(inches * 21))
        txt = textwrap.fill(" ".join(note.split()), width=width)
        ax.text(0.0, -pad, txt, transform=ax.transAxes, fontsize=7.0, color=INK3,
                va="top", ha="left", linespacing=1.45)


def trend(ax, xs, ys, color, logx=False, logy=False):
    """Least-squares line, but ONLY at n >= 3. Below that we stamp the panel instead --
    a two-point 'trend' is a line through noise and this corpus has plenty of n=2 slices."""
    xs, ys = np.asarray(xs, float), np.asarray(ys, float)
    ok = np.isfinite(xs) & np.isfinite(ys)
    if logx:
        ok &= xs > 0
    if logy:
        ok &= ys > 0
    xs, ys = xs[ok], ys[ok]
    if len(xs) < 3:
        ax.text(0.02, 0.94, f"n={len(xs)} — NO trend line drawn, this is not a result",
                transform=ax.transAxes, fontsize=7.5, color=C_STATUS_BAD, va="top")
        return None
    X = np.log10(xs) if logx else xs
    Y = np.log10(ys) if logy else ys
    b, a = np.polyfit(X, Y, 1)
    r = float(np.corrcoef(X, Y)[0, 1])
    gx = np.linspace(X.min(), X.max(), 50)
    ax.plot(10 ** gx if logx else gx, 10 ** (a + b * gx) if logy else a + b * gx,
            color=color, lw=1.6, ls="--", alpha=0.75, zorder=2,
            label=f"fit n={len(xs)}, r={r:+.2f}")
    return r


def noise_note(ys):
    ys = [y for y in ys if y is not None]
    hi = sum(1 for y in ys if y >= NOISE_SPLIT)
    return (f"ruler noise ~{NOISE_HIGH:.0%} above speedup {NOISE_SPLIT:.0f} "
            f"({hi}/{len(ys)} points), ~{NOISE_LOW:.0%} below (doc 56 §14): "
            f"differences smaller than that are not effects.")


ZERO_FLOOR = 0.45   # where a genuine 0.0 is drawn on a log axis, always annotated as such


def _split_zero(rows, ykey="test_speedup"):
    live = [r for r in rows if r.get(ykey)]
    dead = [r for r in rows if r.get(ykey) == 0.0]
    return live, dead


def _mark_zeros(ax, dead, xkey):
    for i, r in enumerate(sorted(dead, key=lambda r: r.get(xkey) or 0)):
        x = r.get(xkey)
        if x is None:
            continue
        ax.scatter(x, ZERO_FLOOR, s=44, facecolors="none", edgecolors=C_STATUS_BAD,
                   linewidths=1.4, zorder=4)
        ax.annotate(f"{r['label']}/{r['task'][:12]} = 0.0", (x, ZERO_FLOOR),
                    textcoords="offset points", xytext=(6, -10 - 9 * (i % 2)), fontsize=6.2,
                    color=C_STATUS_BAD)
    if dead:
        ax.scatter([], [], s=44, facecolors="none", edgecolors=C_STATUS_BAD, linewidths=1.4,
                   label=f"invalid solver, metric 0.0 (n={len(dead)}), drawn at {ZERO_FLOOR}")


def fig_metric_vs(rows, xkey, xlabel, fname, title, logx=True, extra_note=""):
    scored = [r for r in rows if r.get("test_speedup") is not None and r.get(xkey)]
    fig, ax = plt.subplots(figsize=(7.6, 5.0))
    live, dead = _split_zero(scored)
    for arm in ("LoopLab", "AlgoTuner"):
        sub = [r for r in live if r["arm"] == arm]
        if not sub:
            continue
        ax.scatter([r[xkey] for r in sub], [r["test_speedup"] for r in sub], s=38,
                   c=ARM_COLOR[arm], alpha=0.85, edgecolors=SURFACE, linewidths=0.9,
                   zorder=3, label=f"{arm} (n={len(sub)})")
        trend(ax, [r[xkey] for r in sub], [r["test_speedup"] for r in sub],
              ARM_COLOR[arm], logx=logx, logy=True)
    for i, r in enumerate(sorted(live, key=lambda r: -r["test_speedup"])):
        if r["test_speedup"] >= 100:
            ax.annotate(f"{r['label']}/{r['task'][:14]}", (r[xkey], r["test_speedup"]),
                        textcoords="offset points", xytext=(6, 4 if i % 2 else -9),
                        fontsize=6.2, color=INK2)
    _mark_zeros(ax, dead, xkey)
    if logx:
        ax.set_xscale("log")
    ax.set_yscale("log")
    ax.axhline(1.0, color=INK3, lw=0.9, ls=":", zorder=1)
    ax.text(ax.get_xlim()[0], 1.02, " speedup 1.0 = the reference implementation",
            fontsize=6.5, color=INK3, va="bottom")
    ax.set_ylim(bottom=ZERO_FLOOR * 0.55)
    finish(ax, title, xlabel, f"test speedup (log), n={len(scored)} runs",
           note=noise_note([r["test_speedup"] for r in scored]) + " " + extra_note,
           legend_loc="below", pad=0.30)
    fig.tight_layout()
    fig.savefig(os.path.join(PLOTS, fname), bbox_inches="tight")
    plt.close(fig)


def fig_arm_summary(rows, fname):
    fig, axes = plt.subplots(1, 2, figsize=(10.5, 4.6))
    ax = axes[0]
    groups, labels, colors = [], [], []
    for arm in ("LoopLab", "AlgoTuner"):
        v = [r["test_speedup"] for r in rows
             if r["arm"] == arm and r.get("test_speedup") is not None]
        if v:
            groups.append(v)
            labels.append(f"{arm}\nn={len(v)}")
            colors.append(ARM_COLOR[arm])
    pos = np.arange(len(groups))
    for i, (g, c) in enumerate(zip(groups, colors)):
        ax.scatter(np.full(len(g), i) + np.random.RandomState(0).uniform(-.13, .13, len(g)),
                   np.maximum(g, ZERO_FLOOR), s=30, c=c, alpha=0.75, edgecolors=SURFACE,
                   linewidths=0.8, zorder=3)
        ax.hlines(np.median(g), i - .28, i + .28, color=INK, lw=2.2, zorder=4)
        ax.text(i + .32, np.median(g), f"median {np.median(g):.2f}", fontsize=7, color=INK2,
                va="center")
    ax.set_xticks(pos)
    ax.set_xticklabels(labels)
    ax.set_yscale("log")
    ma = [r["test_speedup"] for r in rows if r["source"] == "campaign-A"
          and r.get("test_speedup") is not None]
    mb = [r["test_speedup"] for r in rows if r["source"] == "campaign-B"
          and r["task"] in {x["task"] for x in rows if x["source"] == "campaign-A"
                            and x.get("test_speedup") is not None}
          and r.get("test_speedup") is not None]
    finish(ax, "Every scored run, by arm", "", "test speedup (log)",
           note="Black bar = median. Zeros are drawn at %.2f. These two medians are on "
                "DIFFERENT task mixes and are NOT comparable — LoopLab's set is dominated by "
                "edge_expansion and kcenters probes. On the 10 tasks both arms actually "
                "finished, the medians are AlgoTuner %.2f and LoopLab %.2f."
                % (ZERO_FLOOR, np.median(ma), np.median(mb)), legend=False)

    ax = axes[1]
    # The MATCHED comparison: one campaign run per arm per task, same model, same nominal $1.
    # Best-of-corpus would silently put a $10 gpt-5.6-sol probe against a $1 deepseek run.
    common, pa, pb, probe = [], [], [], []
    for t in sorted({r["task"] for r in rows if r["source"] == "campaign-A"}):
        a = next((r for r in rows if r["source"] == "campaign-A" and r["task"] == t
                  and r.get("test_speedup") is not None), None)
        b = next((r for r in rows if r["source"] == "campaign-B" and r["task"] == t
                  and r.get("test_speedup") is not None), None)
        if not a or not b:
            continue
        common.append(t)
        pa.append(a["test_speedup"])
        pb.append(b["test_speedup"])
        probe.append(max((r["test_speedup"] for r in rows if r["source"] == "probe"
                          and r["task"] == t and r.get("test_speedup") is not None), default=None))
    y = np.arange(len(common))
    ax.barh(y - 0.19, np.maximum(pa, ZERO_FLOOR), height=0.34, color=C_ALGO,
            label=f"AlgoTuner, $1 deepseek (n={len(pa)})", zorder=3)
    ax.barh(y + 0.19, np.maximum(pb, ZERO_FLOOR), height=0.34, color=C_LOOP,
            label=f"LoopLab, $1 deepseek (n={len(pb)})", zorder=3)
    pv = [(v, i) for i, v in enumerate(probe) if v]
    if pv:
        ax.scatter([v for v, _ in pv], [i + 0.19 for _, i in pv], marker="D", s=26,
                   color=C_THIRD, edgecolors=SURFACE, linewidths=0.8, zorder=5,
                   label=f"best LoopLab probe, other budget/model (n={len(pv)})")
    ax.set_yticks(y)
    ax.set_yticklabels(common, fontsize=7)
    ax.set_xscale("log")
    finish(ax, f"Matched pair: one $1 deepseek run per arm (n={len(common)} tasks)",
           "test speedup (log)", "",
           note="This is the only apples-to-apples slice in the corpus. The 10 tasks missing "
                "from it are the ones AlgoTuner never produced a number on. Diamonds are "
                "LoopLab probes at a DIFFERENT budget or model and are context, not part of "
                "the comparison. Bars at %.2f are a genuine 0.0." % ZERO_FLOOR)
    fig.tight_layout()
    fig.savefig(os.path.join(PLOTS, fname), bbox_inches="tight")
    plt.close(fig)


def fig_measurements(rows, fname):
    L = [r for r in rows if r["arm"] == "LoopLab"]
    A = [r for r in rows if r["arm"] == "AlgoTuner"]
    fig, axes = plt.subplots(2, 2, figsize=(11, 8.4))

    ax = axes[0][0]
    for sub, arm in ((L, "LoopLab"), (A, "AlgoTuner")):
        xs = [r["wall_s"] / 60 for r in sub if r.get("wall_s")]
        ys = [r["measure_calls"] for r in sub if r.get("wall_s")]
        ax.scatter(xs, ys, s=34, c=ARM_COLOR[arm], alpha=0.85, edgecolors=SURFACE,
                   linewidths=0.8, zorder=3, label=f"{arm} (n={len(xs)})")
        trend(ax, xs, ys, ARM_COLOR[arm])
    finish(ax, "Measurements vs wall-clock", "wall clock (minutes)",
           "measurement calls",
           note="LoopLab = run_probe + run_dev_command (check/eval_train/profile). AlgoTuner = "
                "dataset evals (incl. the automatic one after every edit) + eval_input + profile. "
                "AlgoTuner wall clock is the gateway-call span of its FINAL attempt only.")

    ax = axes[0][1]
    for sub, arm in ((L, "LoopLab"), (A, "AlgoTuner")):
        xs = [(r["prompt_tokens"] + r["completion_tokens"]) / 1e6 for r in sub]
        ys = [r["measure_calls"] for r in sub]
        ax.scatter(xs, ys, s=34, c=ARM_COLOR[arm], alpha=0.85, edgecolors=SURFACE,
                   linewidths=0.8, zorder=3, label=f"{arm} (n={len(xs)})")
        trend(ax, xs, ys, ARM_COLOR[arm])
    finish(ax, "Measurements vs tokens burned", "prompt+completion tokens (millions)",
           "measurement calls")

    ax = axes[1][0]
    xs = [r["measure_calls"] for r in L]
    ys = [r["cost"] / max(r["measure_calls"], 1) for r in L]
    ax.scatter(xs, ys, s=34, c=C_LOOP, alpha=0.85, edgecolors=SURFACE, linewidths=0.8,
               zorder=3, label=f"LoopLab (n={len(xs)})")
    xa = [r["measure_calls"] for r in A if r.get("cost")]
    ya = [r["cost"] / max(r["measure_calls"], 1) for r in A if r.get("cost")]
    ax.scatter(xa, ya, s=34, c=C_ALGO, alpha=0.85, edgecolors=SURFACE, linewidths=0.8,
               zorder=3, label=f"AlgoTuner (n={len(xa)})")
    ax.set_yscale("log")
    finish(ax, "Amortised money per measurement", "measurement calls in the run",
           "run cost / measurements ($, log)",
           note="AMORTISED, not attributed: nothing in the span schema charges a dollar to a "
                "measurement. This is run spend divided by measurement count — it says how much "
                "money each measurement had to justify, not what one cost.")

    ax = axes[1][1]
    names = ["LLM\n(generation)", "measurement\ntools", "node\nevaluation", "other"]
    tot = np.zeros(4)
    for r in L:
        other = max(r["wall_s"] - r["llm_s"] - r["measure_s"] - r["eval_s"], 0)
        tot += np.array([r["llm_s"], r["measure_s"], r["eval_s"], other])
    share = tot / tot.sum() * 100
    # One ordered magnitude (share of time), not four identities -- so a single-hue ramp, and
    # deliberately NOT the arm colours, which mean LoopLab/AlgoTuner everywhere else here.
    bars = ax.bar(names, share, color=["#184f95", "#2a78d6", "#6da7ec", "#c9c7bf"],
                  zorder=3, width=0.62)
    for b, s, t in zip(bars, share, tot):
        ax.text(b.get_x() + b.get_width() / 2, s + 1.2, f"{s:.1f}%\n{t/3600:.1f} h",
                ha="center", fontsize=7.5, color=INK2)
    ax.set_ylim(0, max(share) * 1.3)
    ax.tick_params(axis="x", labelsize=7.5)
    finish(ax, f"Where LoopLab wall-clock goes (n={len(L)} runs pooled)", "",
           "share of summed span time (%)",
           note="Spans overlap (a tool runs inside a phase), so shares are of summed span time, "
                "not of a partition of the wall clock. The point stands regardless: the loop is "
                "LLM-bound, and parallelising evaluation cannot buy back time not spent there.",
           legend=False)
    fig.tight_layout(h_pad=6.5, w_pad=3.0)
    fig.savefig(os.path.join(PLOTS, fname), bbox_inches="tight")
    plt.close(fig)


def fig_phases(rows, fname):
    # A run that has barely started has a meaningless phase mix; 5 cents is roughly one
    # generation on the cheapest model here, so below that there is nothing to describe.
    L = sorted([r for r in rows if r["arm"] == "LoopLab" and r["phase_cost"] and r["cost"] > 0.05],
               key=lambda r: -r["cost"])
    fig, axes = plt.subplots(1, 2, figsize=(13, 6.2),
                             gridspec_kw={"width_ratios": [1.55, 1]})
    ax = axes[0]
    agg = Counter()
    for r in L:
        for k, v in r["phase_cost"].items():
            agg[k] += v
    top = [k for k, _ in agg.most_common(7)]
    order = top + ["rest"]
    # sequential blue ramp: this is ONE ordered magnitude (phase share), not eight identities
    ramp = ["#0d366b", "#184f95", "#256abf", "#2a78d6", "#3987e5", "#6da7ec", "#9ec5f4", "#cde2fb"]
    y = np.arange(len(L))
    left = np.zeros(len(L))
    for ph, col in zip(order, ramp):
        vals = []
        for r in L:
            c = (r["phase_cost"].get(ph, 0.0) if ph != "rest"
                 else sum(v for k, v in r["phase_cost"].items() if k not in top))
            vals.append(100 * c / max(r["cost"], 1e-9))
        vals = np.array(vals)
        ax.barh(y, vals, left=left, color=col, height=0.72, zorder=3,
                label=f"{ph} (${agg[ph] if ph!='rest' else sum(agg[k] for k in agg if k not in top):.2f} total)"
                if ph != "rest" else
                f"rest (${sum(agg[k] for k in agg if k not in top):.2f} total)",
                edgecolor=SURFACE, linewidth=0.7)
        left += vals
    ax.set_yticks(y)
    ax.set_yticklabels([f"{r['label']}/{r['task'][:13]} ${r['cost']:.2f}" for r in L], fontsize=6)
    ax.set_xlim(0, 100)
    finish(ax, f"Share of each run's money by phase (n={len(L)} LoopLab runs)",
           "% of that run's generation cost", "")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.09), fontsize=6.8, ncol=4,
              labelcolor=INK2)

    ax = axes[1]
    pts = [(100 * sum(r["phase_cost"].get(p, 0) for p in CODE_PHASES) / max(r["cost"], 1e-9),
            r["test_speedup"], r) for r in L if r.get("test_speedup") is not None]
    live = [(x, y_, r) for x, y_, r in pts if y_]
    ax.scatter([p[0] for p in live], [p[1] for p in live], s=38, c=C_LOOP, alpha=0.85,
               edgecolors=SURFACE, linewidths=0.9, zorder=3, label=f"LoopLab (n={len(live)})")
    for x, y_, r in live:
        if y_ >= 100 or x > 68 or x < 12:
            ax.annotate(f"{r['label']}/{r['task'][:12]}", (x, y_), textcoords="offset points",
                        xytext=(5, 4), fontsize=6.3, color=INK2)
    for x, y_, r in pts:
        if y_ == 0.0:
            ax.scatter(x, ZERO_FLOOR, s=44, facecolors="none", edgecolors=C_STATUS_BAD,
                       linewidths=1.4, zorder=4, label=None)
            ax.annotate(f"{r['label']}/{r['task'][:12]} = 0.0", (x, ZERO_FLOOR),
                        textcoords="offset points", xytext=(5, -9), fontsize=6.2,
                        color=C_STATUS_BAD)
    trend(ax, [p[0] for p in live], [p[1] for p in live], C_LOOP, logy=True)
    ax.set_yscale("log")
    finish(ax, "Does spending more on code-writing pay?",
           "share of run spend in plan + plan_step (%)", "test speedup (log)",
           note="Phases plan/plan_step are the ones that write code (doc 56 §1). "
                + noise_note([p[1] for p in pts]))
    fig.tight_layout()
    fig.savefig(os.path.join(PLOTS, fname), bbox_inches="tight")
    plt.close(fig)


def build_pairs(rows, feats):
    """The apples-to-apples pairing: campaign arm B vs campaign arm A -- same 20 tasks, same
    model (deepseek-v4-flash), same nominal $1 budget. Probes are a different budget/model and
    are carried alongside as `probe_best`, never mixed into the ratio."""
    pairs = []
    for t in sorted(feats):
        a = next((r for r in rows if r["arm"] == "AlgoTuner" and r["source"] == "campaign-A"
                  and r["task"] == t), None)
        b = next((r for r in rows if r["source"] == "campaign-B" and r["task"] == t), None)
        probes = [r for r in rows if r["source"] == "probe" and r["task"] == t
                  and r.get("test_speedup") is not None]
        f = feats[t]
        pairs.append(dict(
            task=t, A=a, B=b,
            a_sp=(a or {}).get("test_speedup"), b_sp=(b or {}).get("test_speedup"),
            a_cost=(a or {}).get("cost"), b_cost=(b or {}).get("cost"),
            probe_best=max((r["test_speedup"] for r in probes), default=None),
            n_probes=len(probes),
            eval_s=(b or {}).get("test_eval_s") or (a or {}).get("test_eval_s"),
            **{k: f[k] for k in ("n_param", "target_ms", "baseline_ms", "ref_loc",
                                 "ref_eval_s", "n_instances")}))
    # HEADROOM: the best speedup anyone in the whole corpus ever got on that task. Not a
    # difficulty proxy -- an opportunity proxy, and the one the data actually responds to.
    for p_ in pairs:
        cand = [v for v in (p_["a_sp"], p_["b_sp"], p_["probe_best"]) if v]
        p_["headroom"] = max(cand) if cand else None
    return pairs


DIFF_AXES = [
    ("n_param", "instance size parameter N (log)", True, "instance size N"),
    ("baseline_ms", "reference runtime per instance, ms (log)", True, "reference ms/instance"),
    ("ref_loc", "reference solver, non-blank non-comment lines", False, "reference solver LOC"),
    ("ref_eval_s", "one full REFERENCE pass over the test split, seconds", False,
     "length of one reference evaluation"),
    ("headroom", "best speedup anyone reached on this task (log)", True,
     "headroom (not a difficulty proxy)"),
]


def fig_difficulty(pairs, fname):
    usable = [p for p in pairs if p["a_sp"] is not None and p["b_sp"] is not None]
    ratio_ok = [p for p in usable if p["a_sp"] > 0]
    fig, axes = plt.subplots(2, 3, figsize=(16.5, 8.8))
    for ax, (key, xlabel, logx, human) in zip(axes.ravel(), DIFF_AXES):
        pts = [(p[key], p["b_sp"] / p["a_sp"], p) for p in ratio_ok if p.get(key)]
        ax.axhline(1.0, color=INK3, lw=1.0, ls="-", zorder=2)
        ax.axhspan(1 - NOISE_HIGH, 1 + NOISE_HIGH, color="#eceae4", zorder=1)
        ax.scatter([p[0] for p in pts], [p[1] for p in pts], s=42, c=C_LOOP, alpha=0.9,
                   edgecolors=SURFACE, linewidths=0.9, zorder=4,
                   label=f"LoopLab / AlgoTuner, both $1 deepseek (n={len(pts)})")
        for x, y, p in pts:
            ax.annotate(p["task"][:16], (x, y), textcoords="offset points", xytext=(5, 3),
                        fontsize=6.0, color=INK2)
        if logx:
            ax.set_xscale("log")
        ax.set_yscale("log")
        r = trend(ax, [p[0] for p in pts], [p[1] for p in pts], C_LOOP, logx=logx, logy=True)
        finish(ax, f"Ratio vs {human}", xlabel,
               "LoopLab / AlgoTuner test speedup (log)", legend=False)
        ax.tick_params(axis="x", labelsize=7.5)
    axes.ravel()[-1].axis("off")
    axes.ravel()[-1].scatter([], [], s=42, c=C_LOOP,
                             label=f"LoopLab / AlgoTuner, both $1 deepseek (n={len(ratio_ok)})")
    axes.ravel()[-1].axhspan(0, 0, color="#eceae4", label="±10 % ruler noise around parity")
    axes.ravel()[-1].plot([], [], color=C_LOOP, ls="--", lw=1.6, label="least-squares fit")
    axes.ravel()[-1].legend(loc="upper left", fontsize=8.5, labelcolor=INK2)
    axes.ravel()[-1].text(
        0.0, 0.55,
        textwrap.fill("A point inside the grey band is a tie, not a win — the ruler carries "
                      "~10 % noise at these magnitudes. Tasks where AlgoTuner scored 0.0 or "
                      f"produced no metric at all are excluded: {len(usable) - len(ratio_ok)} "
                      f"zero + {len(pairs) - len(usable)} missing of 20. Every cell is n=1 per "
                      "arm per task; there are no replicates in this corpus.", 46),
        transform=axes.ravel()[-1].transAxes, fontsize=8, color=INK2, va="top", linespacing=1.5)
    fig.tight_layout(h_pad=5.0, w_pad=2.5)
    fig.savefig(os.path.join(PLOTS, fname), bbox_inches="tight")
    plt.close(fig)


def fig_difficulty_levels(pairs, fname):
    """Same hypothesis, but on the raw metric rather than the ratio -- a ratio hides which arm
    moved. Both arms against the same difficulty proxy."""
    fig, axes = plt.subplots(2, 3, figsize=(16.5, 8.8))
    for ax, (key, xlabel, logx, human) in zip(axes.ravel(), DIFF_AXES[:-1]):
        for arm, sp_key, col in (("AlgoTuner", "a_sp", C_ALGO), ("LoopLab", "b_sp", C_LOOP)):
            pts = [(p[key], p[sp_key], p) for p in pairs
                   if p.get(key) and p.get(sp_key)]
            ax.scatter([p[0] for p in pts], [p[1] for p in pts], s=38, c=col, alpha=0.85,
                       edgecolors=SURFACE, linewidths=0.9, zorder=3,
                       label=f"{arm} (n={len(pts)})")
            trend(ax, [p[0] for p in pts], [p[1] for p in pts], col, logx=logx, logy=True)
        zeros = [(p[key], p) for p in pairs if p.get(key) and p.get("b_sp") == 0.0]
        for x, p in zeros:
            ax.scatter(x, ZERO_FLOOR, s=44, facecolors="none", edgecolors=C_STATUS_BAD,
                       linewidths=1.4, zorder=4)
        if logx:
            ax.set_xscale("log")
        ax.set_yscale("log")
        ax.axhline(1.0, color=INK3, lw=0.9, ls=":", zorder=1)
        finish(ax, f"Test speedup vs {human}", xlabel, "test speedup (log)", legend=False)
        ax.tick_params(axis="x", labelsize=7.5)
    for spare in axes.ravel()[len(DIFF_AXES) - 1:]:
        spare.axis("off")
    spare = axes.ravel()[len(DIFF_AXES) - 1]
    spare.scatter([], [], s=38, c=C_ALGO, label="AlgoTuner, $1 deepseek (n=10 tasks)")
    spare.scatter([], [], s=38, c=C_LOOP, label="LoopLab, $1 deepseek (n=20 tasks)")
    spare.scatter([], [], s=44, facecolors="none", edgecolors=C_STATUS_BAD, linewidths=1.4,
                  label="genuine metric 0.0 (invalid solver)")
    spare.plot([], [], color=INK3, ls="--", lw=1.6, label="least-squares fit, per arm")
    spare.legend(loc="upper left", fontsize=8.5, labelcolor=INK2)
    axes.ravel()[-1].text(
        0.0, 0.55,
        textwrap.fill("Dotted line = the reference implementation (speedup 1.0). Red rings are "
                      "a genuine metric of 0.0 — an invalid solver, not a ruler refusal. "
                      "LoopLab has a number on all 20 tasks, AlgoTuner on 10, so the two "
                      "series are not drawn on the same task set: read the ratio figure for "
                      "the paired comparison.", 46),
        transform=axes.ravel()[-1].transAxes, fontsize=8, color=INK2, va="top", linespacing=1.5)
    fig.tight_layout(h_pad=5.0, w_pad=2.5)
    fig.savefig(os.path.join(PLOTS, fname), bbox_inches="tight")
    plt.close(fig)


def fig_trajectory(rows, fname):
    L = [r for r in rows if r["arm"] == "LoopLab" and len(r["nodes"]) >= 1]
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 5.0))
    ax = axes[0]
    for r in L:
        ys = [n["speedup"] for n in r["nodes"]]
        if not any(y for y in ys):
            continue
        xs = list(range(len(ys)))
        ys = [max(y or 0.0, ZERO_FLOOR) for y in ys]
        col = C_THIRD if len(ys) >= 7 else C_LOOP
        ax.plot(xs, ys, color=col, lw=1.3, alpha=0.55, marker="o", ms=3.2, zorder=3)
        if len(ys) >= 7:
            ax.annotate(f"{r['label']}/{r['task'][:12]}", (xs[-1], ys[-1]),
                        textcoords="offset points", xytext=(4, 0), fontsize=6.4, color=INK2)
    ax.plot([], [], color=C_LOOP, lw=1.3, marker="o", ms=3.2,
            label=f"< 7 nodes (n={sum(1 for r in L if len(r['nodes'])<7)})")
    ax.plot([], [], color=C_THIRD, lw=1.3, marker="o", ms=3.2,
            label=f">= 7 nodes (n={sum(1 for r in L if len(r['nodes'])>=7)})")
    ax.set_yscale("log")
    finish(ax, "Search trajectory: TRAIN score of every node, in order",
           "node index", "train speedup (log)", legend_loc="below", pad=0.28,
           note="One line per run. Train, not test — this is the number the search itself sees. "
                "Zeros floored at %.2f." % ZERO_FLOOR)

    ax = axes[1]
    pts = []
    for r in L:
        ys = [n["speedup"] or 0.0 for n in r["nodes"]]
        if len(ys) < 2 or ys[0] <= 0:
            continue
        pts.append((len(ys), max(ys) / ys[0], r))
    ax.axhline(1.0, color=INK3, lw=1.0, zorder=2)
    ax.axhspan(1 - NOISE_HIGH, 1 + NOISE_HIGH, color="#eceae4", zorder=1)
    ax.scatter([p[0] for p in pts], [p[1] for p in pts], s=42, c=C_LOOP, alpha=0.9,
               edgecolors=SURFACE, linewidths=0.9, zorder=4,
               label=f"LoopLab runs with >=2 evaluated nodes (n={len(pts)})")
    for n, g, r in pts:
        if g > 1.5 or n >= 7:
            ax.annotate(f"{r['label']}/{r['task'][:12]}", (n, g), textcoords="offset points",
                        xytext=(5, 3), fontsize=6.3, color=INK2)
    ax.set_yscale("log")
    med = np.median([p[1] for p in pts]) if pts else float("nan")
    finish(ax, "What the whole search buys over its own first node",
           "evaluated nodes in the run", "best node / node 0 (log)",
           legend_loc="below", pad=0.28,
           note=f"median gain over node 0 = {med:.2f}x across n={len(pts)} runs. "
                "Grey band is the ±10 % ruler noise: inside it the search bought nothing.")
    fig.tight_layout(w_pad=3.0)
    fig.savefig(os.path.join(PLOTS, fname), bbox_inches="tight")
    plt.close(fig)


def fig_accounting(rows, fname):
    A = [r for r in rows if r["arm"] == "AlgoTuner" and r.get("cost_selfreported")]
    fig, ax = plt.subplots(figsize=(7.6, 5.0))
    ax.plot([0, 1.3], [0, 1.3], color=INK3, lw=1.0, ls=":", zorder=2, label="perfect agreement")
    for model, col in (("deepseek-v4-flash", C_ALGO), ("openai/gpt-5.6-sol", C_THIRD)):
        sub = [r for r in A if r["model"] == model]
        if not sub:
            continue
        ax.scatter([r["cost_selfreported"] for r in sub], [r["cost"] for r in sub], s=46,
                   c=col, alpha=0.9, edgecolors=SURFACE, linewidths=0.9, zorder=3,
                   label=f"{model} (n={len(sub)})")
    for i, r in enumerate(sorted(A, key=lambda r: -r["cost"])):
        ax.annotate(f"{r['label']}/{r['task'][:18]}", (r["cost_selfreported"], r["cost"]),
                    textcoords="offset points", xytext=(6, 3 if i % 2 else -9), fontsize=6.2,
                    color=INK2)
    finish(ax, "AlgoTuner's own budget line vs the gateway ledger",
           "'You have used up $X' at the end of the run", "metered spend, same attempt window ($)",
           note="Both axes are the SAME attempt (meter rows filtered to ts >= the last entry in "
                "campaign-final/A-*.attempts). The gap tracks completion tokens: on deepseek "
                "AlgoTuner stops at ~$1.00 by its own count while the gateway bills up to "
                "$2.41, and on gpt-5.6-sol the error runs the other way ($0.996 self-reported, "
                "$0.508 metered). Arm A's '$1 budget' is not comparable to arm B's $1 without "
                "this correction, in either direction.")
    fig.tight_layout()
    fig.savefig(os.path.join(PLOTS, fname), bbox_inches="tight")
    plt.close(fig)


# --------------------------------------------------------------------------- report
def rank(v):
    order = np.argsort(np.argsort(np.asarray(v, float)))
    return order.astype(float)


def spearman(x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 3:
        return None, int(ok.sum())
    return float(np.corrcoef(rank(x[ok]), rank(y[ok]))[0, 1]), int(ok.sum())


def fmt_rho(x, y, name):
    r, n = spearman(x, y)
    if r is None:
        return f"  {name}: n={n} — too few points, no correlation reported"
    verdict = "no relation" if abs(r) < 0.3 else ("weak" if abs(r) < 0.55 else "clear")
    return f"  {name}: Spearman rho = {r:+.2f} (n={n}) — {verdict}"


def report(rows, pairs, feats, meter):
    out = []
    P = out.append
    L = [r for r in rows if r["arm"] == "LoopLab"]
    A = [r for r in rows if r["arm"] == "AlgoTuner"]
    Ls = [r for r in L if r.get("test_speedup") is not None]
    As = [r for r in A if r.get("test_speedup") is not None]

    P("# AlgoTune corpus: what the accumulated runs actually show")
    P("")
    P("Regenerate with (free cores only — 0-43 and 48-91 carry live runs):")
    P("")
    P("```")
    P("taskset -c 44-47,92-95 python3 \\")
    P("    /var/tmp/looplab-bench/looplab/benchmarks/algotune/plot_corpus.py")
    P("```")
    P("")
    P("The script is read-only against the corpus and makes no LLM calls. It re-reads live "
      "run directories on every invocation, so numbers move as in-flight probes finish.")
    P("")
    P(f"Generated by `looplab/benchmarks/algotune/plot_corpus.py`. "
      f"{len(L)} LoopLab runs ({len(Ls)} with a test number) and {len(A)} AlgoTuner runs "
      f"({len(As)} with a test number). `rescored/*.json` was checked against "
      f"`campaign-final/B-*.final.json` for all 8 tasks it covers and agrees to the digit, so "
      f"only one of the two is read. Money is read from `attributes.cost` on generation "
      f"spans for LoopLab and from the gateway ledger for AlgoTuner; nothing is estimated "
      f"from token counts.")
    P("")
    P("EXCLUDED ON PURPOSE: `camp-runs/` and `invalidated/` (the 2026-08-20/21 arm B was "
      "measured on a broken ruler — discard, do not rescale), and every probe with no "
      "`final.json` (still in flight or crashed before scoring): "
      + ", ".join(sorted(r["label"] for r in L if not r["has_final"])) + ".")
    P("")

    P("## Money")
    P("")
    tot_l = sum(r["cost"] for r in L)
    tot_a = sum((r.get("cost") or 0) for r in A)
    tot_all = sum((r.get("cost") or 0) for r in meter)
    P(f"* LoopLab runs in scope: **${tot_l:.2f}** over {len(L)} runs "
      f"(median ${np.median([r['cost'] for r in L]):.2f}).")
    P(f"* AlgoTuner final attempts in scope: **${tot_a:.2f}** over {len(A)} runs.")
    P(f"* Whole ledger, every arm and every discarded attempt: **${tot_all:.2f}**.")
    ds = [r for r in A if r["source"] == "campaign-A" and r.get("cost_selfreported")
          and r["cost_selfreported"] > 0.5]
    if ds:
        ratios = sorted(r["cost"] / r["cost_selfreported"] for r in ds)
        worst = max(ds, key=lambda r: r["cost"] / r["cost_selfreported"])
        P(f"* **AlgoTuner's own budget line is not the bill.** On the {len(ds)} deepseek runs "
          f"that reached the ceiling it stops at a self-reported "
          f"${np.median([r['cost_selfreported'] for r in ds]):.3f} while the gateway charges "
          f"${np.median([r['cost'] for r in ds]):.3f} median and up to "
          f"${worst['cost']:.2f} ({worst['task']}) — metered/self-reported spans "
          f"{ratios[0]:.2f}x to {ratios[-1]:.2f}x. The excess is completion tokens: the four "
          f"worst offenders (pde_heat1d, sparse_eigenvectors_complex, rbf_interpolation, "
          f"kcenters) are exactly the runs with millions of completion tokens. So 'arm A at $1' "
          f"is arm A at $1.00-$2.41 and every $-for-$ comparison must use the metered column.")
        solA = next((r for r in A if r["label"] == "solA"), None)
        if solA:
            P(f"* The error runs the OTHER way on a different model: the gpt-5.6-sol arm-A probe "
              f"self-reported ${solA['cost_selfreported']:.3f} and was metered at "
              f"${solA['cost']:.3f}. AlgoTuner's price table is simply not the gateway's.")
        P(f"* Arm A also burned {sum(r.get('attempts') or 1 for r in A)} attempts across "
          f"{len(A)} tasks; counting the abandoned ones its true campaign cost is "
          f"${sum((r.get('cost_all_attempts') or r.get('cost') or 0) for r in A):.2f}.")
    P("")

    ca = [r for r in rows if r["source"] == "campaign-A" and r.get("test_speedup") is not None]
    cb = [r for r in rows if r["source"] == "campaign-B"
          and r["task"] in {x["task"] for x in ca} and r.get("test_speedup") is not None]
    P(f"* On the {len(ca)} tasks both campaign arms finished, the paid spend was "
      f"AlgoTuner median ${np.median([r['cost'] for r in ca]):.2f} against LoopLab median "
      f"${np.median([r['cost'] for r in cb]):.2f} — LoopLab came in UNDER its cap, arm A over "
      f"its own estimate of it. Median test speedup on that same set: AlgoTuner "
      f"{np.median([r['test_speedup'] for r in ca]):.2f}, LoopLab "
      f"{np.median([r['test_speedup'] for r in cb]):.2f}.")
    P("")

    P("## Metric against spend, nodes, calls, tokens, wall clock")
    P("")
    P("Spearman on LoopLab runs with a test number "
      f"(n={len(Ls)}; a rho this corpus can support at all needs n>=3):")
    P(fmt_rho([r["cost"] for r in Ls], [r["test_speedup"] for r in Ls], "test speedup vs $"))
    P(fmt_rho([r["n_nodes"] for r in Ls], [r["test_speedup"] for r in Ls],
              "test speedup vs evaluated nodes"))
    P(fmt_rho([r["gens"] for r in Ls], [r["test_speedup"] for r in Ls],
              "test speedup vs LLM calls"))
    P(fmt_rho([r["prompt_tokens"] + r["completion_tokens"] for r in Ls],
              [r["test_speedup"] for r in Ls], "test speedup vs total tokens"))
    P(fmt_rho([r["wall_s"] for r in Ls], [r["test_speedup"] for r in Ls],
              "test speedup vs wall clock"))
    P("  (the wall-clock sign is NEGATIVE in both arms — long runs are struggling runs, not "
      "winning ones. Do not read it as 'stop early'; it is selection, not causation.)")
    P(fmt_rho([r["measure_calls"] for r in Ls], [r["test_speedup"] for r in Ls],
              "test speedup vs measurement calls"))
    P("")
    same = [r for r in Ls if r["task"] == "edge_expansion" and r["model"] == "deepseek-v4-flash"]
    if len(same) >= 3:
        P(f"Held-fixed slice — deepseek-v4-flash on edge_expansion, n={len(same)}, "
          f"budget ${min(r['cost'] for r in same):.2f}-${max(r['cost'] for r in same):.2f}:")
        P(fmt_rho([r["cost"] for r in same], [r["test_speedup"] for r in same],
                  "  test speedup vs $ (one task, one model)"))
        P("")

    P("## Measurements")
    P("")
    dev = Counter()
    for r in L:
        for k, v in r["dev_cmds"].items():
            dev[k] += v
    probe_calls = sum(r["tools"].get("run_probe", 0) for r in L)
    P(f"* LoopLab, pooled: {probe_calls} free-form `run_probe` calls and "
      f"{sum(dev.values())} pinned developer commands ({dict(dev)}).")
    acmd = Counter()
    for r in A:
        for k, v in (r.get("a_cmds") or {}).items():
            acmd[k] += v
    P(f"* AlgoTuner, pooled: {sum(acmd.values())} dataset evaluations "
      f"({dict(acmd)}) plus {sum(r.get('a_eval_input', 0) for r in A)} `eval_input` and "
      f"{sum(r.get('a_profile', 0) for r in A)} `profile` calls.")
    P(f"* Per run: AlgoTuner takes a median of "
      f"{np.median([r['measure_calls'] for r in A]):.0f} measurements, LoopLab "
      f"{np.median([r['measure_calls'] for r in L]):.0f}.")
    tl = sum(r["llm_s"] for r in L)
    tm = sum(r["measure_s"] for r in L)
    te = sum(r["eval_s"] for r in L)
    P(f"* Time: LoopLab burned {tl/3600:.1f} h inside `generation`, {tm/3600:.1f} h inside "
      f"measurement tools and {te/3600:.1f} h inside engine node evaluation — "
      f"{100*tl/(tl+tm+te):.0f} % of accounted span time is the model thinking. "
      f"Parallelising evaluation cannot buy back time that is not spent there.")
    P("* Money per measurement can only be AMORTISED, never attributed: no span in this schema "
      "charges a dollar to a measurement. Amortised, a LoopLab measurement has to justify a "
      f"median of ${np.median([r['cost']/max(r['measure_calls'],1) for r in L]):.3f} and an "
      f"AlgoTuner one ${np.median([r['cost']/max(r['measure_calls'],1) for r in A if r.get('cost')]):.3f}.")
    P("")

    P("## Where the money goes by phase")
    P("")
    agg = Counter()
    gens = Counter()
    for r in L:
        for k, v in r["phase_cost"].items():
            agg[k] += v
        for k, v in r["phase_gens"].items():
            gens[k] += v
    tot = sum(agg.values())
    for k, v in agg.most_common(9):
        P(f"* `{k}`: ${v:.2f} ({100*v/tot:.1f} %), {gens[k]} generations")
    code = sum(agg[p] for p in CODE_PHASES)
    P(f"* Code-writing (`plan` + `plan_step`) is **${code:.2f}, {100*code/tot:.0f} %**; "
      f"everything else is search scaffolding.")
    cs = [(100 * sum(r["phase_cost"].get(p, 0) for p in CODE_PHASES) / max(r["cost"], 1e-9),
           r["test_speedup"]) for r in Ls if r["phase_cost"]]
    P(fmt_rho([c for c, _ in cs], [s for _, s in cs], "code-share vs test speedup"))
    P(f"  code share itself ranges {min(c for c,_ in cs):.0f} %-{max(c for c,_ in cs):.0f} % "
      f"across runs, so the corpus average is an aggregate, not a per-run constant.")
    P("")

    P("## The search's own payoff")
    P("")
    gains = []
    for r in L:
        ys = [n["speedup"] or 0.0 for n in r["nodes"]]
        if len(ys) >= 2 and ys[0] > 0:
            gains.append((len(ys), max(ys) / ys[0], r["label"]))
    if gains:
        P(f"* Over runs with >=2 evaluated nodes (n={len(gains)}), the median gain of the whole "
          f"search over its own node 0 is **{np.median([g for _, g, _ in gains]):.2f}x**.")
        small = [g for n, g, _ in gains if n < 7]
        big = [g for n, g, _ in gains if n >= 7]
        P(f"* Runs with <7 nodes (n={len(small)}): median {np.median(small):.2f}x. "
          f"Runs with >=7 nodes (n={len(big)}): median "
          f"{np.median(big):.2f}x." if big else "")
        P("* At a $1 ceiling a run buys 2-4 nodes, and at 2-4 nodes the scaffolding is paid "
          "for and never used. The wins all live in the double-digit-node runs, which cost $10.")
    P("")
    return out, pairs


def report_hypothesis(pairs, out):
    P = out.append
    P("## THE CUSTOMER'S HYPOTHESIS: does LoopLab win on HARD tasks and on tasks with a LONG "
      "evaluation?")
    P("")
    usable = [p for p in pairs if p["a_sp"] is not None and p["b_sp"] is not None]
    ratio_ok = [p for p in usable if p["a_sp"] > 0]
    missing = [p["task"] for p in pairs if p["a_sp"] is None or p["b_sp"] is None]
    P(f"Pairing: campaign arm B vs campaign arm A — the only apples-to-apples slice "
      f"(same 20 tasks, same model deepseek-v4-flash, same nominal $1). "
      f"{len(usable)} tasks have a number on both sides; {len(missing)} do not "
      f"({', '.join(missing) if missing else 'none'}).")
    P("")
    P(f"**Before any ratio: coverage.** LoopLab produced a scored test number on "
      f"{sum(1 for p in pairs if p['b_sp'] is not None)}/20 tasks; AlgoTuner on "
      f"{sum(1 for p in pairs if p['a_sp'] is not None)}/20. Five arm-A tasks have no log at "
      f"all, three exhausted their attempts without reaching a final evaluation "
      f"(min_dominating_set, multi_dim_knapsack, set_cover_conflicts) and two ran the final "
      f"evaluation but produced no metric (pagerank, spectral_clustering, `missing_metrics` in "
      f"agent_failures.json). That 20-vs-10 completion gap is the largest single difference in "
      f"this corpus and it is NOT what the hypothesis is about — record it separately.")
    P("")
    P("| task | N | ref ms/inst | ref pass s | ref LOC | AlgoTuner | LoopLab | ratio |")
    P("|---|---|---|---|---|---|---|---|")
    for p in sorted(usable, key=lambda p: -(p["b_sp"] / p["a_sp"] if p["a_sp"] else 0)):
        rat = f"{p['b_sp']/p['a_sp']:.2f}x" if p["a_sp"] else "A=0.0"
        P(f"| {p['task']} | {p['n_param']} | {p['baseline_ms']:.1f} | "
          f"{(p['ref_eval_s'] or 0):.1f} | {p['ref_loc']} | "
          f"{p['a_sp']:.3f} | {p['b_sp']:.3f} | {rat} |")
    P("")
    wins = [p for p in ratio_ok if p["b_sp"] / p["a_sp"] > 1 + NOISE_HIGH]
    ties = [p for p in ratio_ok if 1 - NOISE_HIGH <= p["b_sp"] / p["a_sp"] <= 1 + NOISE_HIGH]
    loss = [p for p in ratio_ok if p["b_sp"] / p["a_sp"] < 1 - NOISE_HIGH]
    P(f"Counting only differences bigger than the ±10 % ruler noise: LoopLab wins "
      f"**{len(wins)}**, ties {len(ties)}, loses **{len(loss)}** of {len(ratio_ok)}.")
    P("")
    for key, label in (("n_param", "instance size N"),
                       ("baseline_ms", "reference runtime per instance (ms)"),
                       ("ref_eval_s", "seconds of one full reference pass over the test split"),
                       ("ref_loc", "reference solver length (LOC)"),
                       ("headroom", "headroom: best speedup anyone reached on the task")):
        xs = [p[key] for p in ratio_ok if p.get(key)]
        ys = [p["b_sp"] / p["a_sp"] for p in ratio_ok if p.get(key)]
        P(fmt_rho(xs, ys, f"ratio vs {label}"))
    P("")
    P("What the ratio DOES respond to — not difficulty, but whether the reference agent had "
      "already found the win:")
    P(fmt_rho([p["a_sp"] for p in ratio_ok], [p["b_sp"] / p["a_sp"] for p in ratio_ok],
              "ratio vs AlgoTuner's own score on that task"))
    P(fmt_rho([p["b_sp"] for p in ratio_ok], [p["b_sp"] / p["a_sp"] for p in ratio_ok],
              "ratio vs LoopLab's own score on that task"))
    P("")
    for key, label in (("n_param", "instance size N"),
                       ("baseline_ms", "reference runtime per instance (ms)"),
                       ("ref_loc", "reference solver length (LOC)")):
        xs = [p[key] for p in pairs if p.get(key) and p.get("b_sp") is not None]
        ys = [p["b_sp"] for p in pairs if p.get(key) and p.get("b_sp") is not None]
        P(fmt_rho(xs, ys, f"LoopLab absolute speedup vs {label}"))
        xs = [p[key] for p in pairs if p.get(key) and p.get("a_sp") is not None]
        ys = [p["a_sp"] for p in pairs if p.get(key) and p.get("a_sp") is not None]
        P(fmt_rho(xs, ys, f"AlgoTuner absolute speedup vs {label}"))
    P("")
    P("### Verdict")
    P("")
    rhos = []
    for key in ("n_param", "baseline_ms", "ref_eval_s", "ref_loc"):
        r, n = spearman([p[key] for p in ratio_ok if p.get(key)],
                        [p["b_sp"] / p["a_sp"] for p in ratio_ok if p.get(key)])
        if r is not None:
            rhos.append(abs(r))
    P(f"**The hypothesis is NOT confirmed by this corpus.** On the {len(ratio_ok)} tasks where "
      f"both arms produced a number, the LoopLab/AlgoTuner ratio has no monotone relation to "
      f"any difficulty proxy: |rho| <= {max(rhos):.2f} on instance size N, reference runtime per "
      f"instance, total reference-pass seconds and reference-solver length, all at n=10. At "
      f"n=10 a |rho| of 0.24 is not distinguishable from zero, so this is 'no signal', not "
      f"'a small negative signal'.")
    P("")
    P("Two concrete counter-examples rather than a summary statistic: the single largest "
      "instance in the suite, convex_hull at N=267021, is LoopLab's **worst** result "
      "(0.25x), and the second-longest reference pass, sparse_eigenvectors_complex at "
      "175.7 ms/instance, is a 0.91x loss. The two clear wins are edge_expansion (24.5x) and "
      "count_riemann_zeta_zeros (5.8x), which sit at N=4408 and N=15849 — mid-pack — and whose "
      "reference solvers are 176 and 31 lines, i.e. among the SHORTEST.")
    P("")
    P("What separates wins from losses is not difficulty but **whether AlgoTuner had already "
      "found the win**. Every LoopLab win is a task where AlgoTuner scored ~1.0-1.2, i.e. "
      "barely beat the reference at all; every LoopLab loss is a task where AlgoTuner scored "
      "1.5-16.4. That is consistent with a ceiling effect on the shared model, not with a "
      "difficulty advantage, and it is the honest reading of the same ten points.")
    P("")
    P("What WOULD test the hypothesis properly and is missing from the corpus: arm A numbers "
      "on the ten tasks it never finished (five have no log at all), and replicates — every "
      "cell here is n=1 per arm per task, on a ruler with ~10 % noise at high speedup, so no "
      "single row below ~1.1x or above ~0.9x is an effect on its own.")
    P("")
    return wins, ties, loss, ratio_ok


def main():
    style()
    os.makedirs(PLOTS, exist_ok=True)
    meter = load_meter()
    feats = task_features()
    rows = collect_looplab() + collect_algotuner(meter)
    pairs = build_pairs(rows, feats)

    scored = [r for r in rows if r.get("test_speedup") is not None]
    fig_metric_vs(scored, "cost", "run spend, $ (log) — metered", "01-metric-vs-money.png",
                  "Test speedup against money actually paid",
                  extra_note="Read the AlgoTuner fit with care: every one of those runs was "
                             "GIVEN the same $1 ceiling, so its x-spread is an artefact of "
                             "AlgoTuner's own accounting missing completion tokens, not a "
                             "budget the experiment chose. The downward slope is that artefact "
                             "correlating with reasoning-heavy tasks, not evidence that money "
                             "hurts.")
    fig_arm_summary(scored, "02-metric-by-arm-and-task.png")
    fig_metric_vs([r for r in scored if r["arm"] == "LoopLab"], "n_nodes",
                  "evaluated nodes in the run", "03-metric-vs-nodes.png",
                  "Test speedup against node count (LoopLab only — AlgoTuner has no nodes)",
                  logx=False,
                  extra_note="AlgoTuner's loop has no node concept: it edits one file in one "
                             "session, so this axis exists for LoopLab alone.")
    fig_metric_vs(scored, "gens", "LLM calls in the run (log)", "04-metric-vs-llm-calls.png",
                  "Test speedup against number of LLM calls")
    for r in rows:
        r["total_tokens"] = (r.get("prompt_tokens") or 0) + (r.get("completion_tokens") or 0)
    fig_metric_vs([r for r in rows if r.get("test_speedup") is not None], "total_tokens",
                  "prompt + completion tokens (log)", "05-metric-vs-tokens.png",
                  "Test speedup against tokens burned")
    for r in rows:
        r["wall_min"] = (r.get("wall_s") or 0) / 60.0
    fig_metric_vs([r for r in rows if r.get("test_speedup") is not None], "wall_min",
                  "wall clock, minutes (log)", "06-metric-vs-wallclock.png",
                  "Test speedup against wall clock",
                  extra_note="The two arms' wall clocks are measured differently and are not "
                             "strictly comparable: LoopLab's is first-to-last span in its own "
                             "trace, AlgoTuner's is first-to-last gateway call of its final "
                             "attempt (its log carries no timestamps). Both slopes are "
                             "NEGATIVE — inside each arm, the runs that took longest are the "
                             "ones that were struggling, not the ones that were winning.")
    fig_measurements(rows, "07-measurements.png")
    fig_phases(rows, "08-phase-money.png")
    fig_difficulty(pairs, "09-hypothesis-ratio-vs-difficulty.png")
    fig_difficulty_levels(pairs, "10-hypothesis-levels-vs-difficulty.png")
    fig_trajectory(rows, "11-search-trajectory.png")
    fig_accounting(rows, "12-armA-budget-vs-ledger.png")

    out, _ = report(rows, pairs, feats, meter)
    wins, ties, loss, ratio_ok = report_hypothesis(pairs, out)
    out.append("## Corpus traps deliberately not stepped on")
    out.append("")
    for k, v in sorted(TRAPS.items()):
        out.append(f"* **{k}** — {v}")
    out.append(f"* sol10's independent re-score is {SOL10_INDEPENDENT_RESCORE} against "
               f"final.json's 259.677; the plots use final.json and the 10 % gap between the two "
               f"is exactly the noise figure the panels shade.")
    out.append("")
    out.append("## Figures")
    out.append("")
    for fn in sorted(os.listdir(PLOTS)):
        if fn.endswith(".png"):
            out.append(f"* `{fn}`")
    with open(os.path.join(PLOTS, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    print("\n".join(out))
    print(f"\n[wrote {PLOTS}]")


if __name__ == "__main__":
    main()
