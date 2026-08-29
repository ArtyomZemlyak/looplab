#!/usr/bin/env python3
"""Corrected corpus slices for the AlgoTune A/B bench. A COPY of plot_corpus.py's job, not a
replacement -- plot_corpus.py is left untouched. Text report only, no figures.

    taskset -c 44-47,92-95 python3 \
        /var/tmp/looplab-bench/looplab/benchmarks/algotune/plot_corpus_v2.py

Read-only. No LLM calls. See plots/review-of-corpus-analysis.md for the findings this encodes.

WHAT THIS FIXES RELATIVE TO plot_corpus.py
------------------------------------------
1. MONEY. plot_corpus reads `attributes.cost` on `generation` spans. That stream lost 12-25 % of
   its generation spans in the 20 `runs-B/` campaign runs (e.g. edge_expansion: 320 spans vs 379
   `llm_usage` events), so it understates arm-B spend by a median of 11 % and up to 28 %. The
   `llm_usage` events in `events.jsonl` agree with the gateway ledger to the cent on all 20 tasks.
   We read events, and print the span figure beside it so the gap stays visible.
2. LEDGER. There are THREE active meter files; plot_corpus opens two. `meter/meter-8803.jsonl`
   is the only one carrying `arm=dsN3b`.
3. SPEARMAN. plot_corpus's `rank()` is `argsort(argsort(v))`, which breaks ties by POSITION IN THE
   ARRAY -- and the array is in corpus (roughly chronological) order. On `n_nodes` (17 runs tied at
   2, 16 at 3) that manufactures rho -0.18 where the tie-corrected value is -0.07. We average ties,
   print a permutation 5 % threshold beside every rho, and print the leave-one-out worst case.
4. THE X AXIS. Every run burns its whole budget, so "metric vs total wall clock / total $" measures
   budget exhaustion, not loop speed. We compute time / $ / tokens to the FIRST evaluated node, to
   the CHAMPION node, and BETWEEN adjacent evaluated nodes, from `node_evaluated` timestamps against
   `run_started` with `llm_usage` integrated up to each node.
5. EPOCHS. 2026-08-27 10:36 the card stopped reading as a ban on compilation; 2026-08-28 09:05 pip
   landed in the arena venv so `setup.py` candidates stopped scoring 0.0. Nothing is pooled across
   those boundaries without saying so.
6. RATIO ARTEFACTS. rho(b/a, a) is negative by construction. Any correlation between a ratio and its
   own denominator is reported against a permutation null, never bare.

TRAPS INHERITED UNCHANGED FROM plot_corpus.py (they were right)
---------------------------------------------------------------
* `events.jsonl`: a crash-atomic packet carries `type` as a one-element LIST with the real events in
  `data.events`. Unrolling is mandatory.
* `score.log`: append-only, take the LAST record with a non-null speedup. It is also the BETTER node
  source than `node_evaluated` events -- five arm-B runs died after scoring their last node but
  before the event flushed.
* test metric: `final.json`, never the tail of `probe.log`. sol10 is the only run where they differ.
* `camp-runs/` and `invalidated/` stay excluded.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

import numpy as np

ROOT = "/var/tmp/looplab-bench"
DATA = os.path.join(ROOT, "AlgoTune/.hf_datasets/oripress__AlgoTune/data")
BASELINES = os.path.join(ROOT, "looplab/benchmarks/algotune/.baseline_times")
TASKSRC = os.path.join(ROOT, "AlgoTune/AlgoTuneTasks")
METERS = ("meter/meter.jsonl", "meter/meter-gemini.jsonl", "meter/meter-8803.jsonl")

BREAK_CARD = 1787826960.0   # 2026-08-27 10:36 UTC
BREAK_PIP = 1787907900.0    # 2026-08-28 09:05 UTC
FRESH = {"dsBud", "dsBud2", "dsBud3", "dsNoDR", "dsChkKc", "dsChk49",
         "dsKcRep", "dsN3", "dsN3b", "dsPyx"}
CODE_PHASES = {"plan", "plan_step"}

# doc 56 sec 14 set NOISE_HIGH from a 259.677 vs 285.5765 pair whose second measurement left no
# artefact on disk (grep -rn "285.57" matches only doc 56). Treated as ASSUMED until reproduced.
NOISE_LOW = 0.02      # measured: fxSpectral scored node_0 twice, 2.1766 then 2.2244
NOISE_HIGH = 0.10     # assumed, unverified


# ----------------------------------------------------------------- readers
def jload(p, d=None):
    try:
        return json.load(open(p, encoding="utf-8", errors="replace"))
    except Exception:
        return d


def iter_jsonl(path):
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for ln in fh:
            ln = ln.strip()
            if not ln.startswith("{"):
                continue
            try:
                yield json.loads(ln)
            except ValueError:
                continue


def iter_events(path):
    """A LINE IS NOT AN EVENT: crash-atomic packets carry `type` as a list."""
    for rec in iter_jsonl(path):
        if isinstance(rec.get("type"), list):
            for ev in (rec.get("data") or {}).get("events") or []:
                yield ev
        else:
            yield rec


def read_score_log(path):
    best = last = None
    for rec in iter_jsonl(path):
        last = rec
        if rec.get("speedup") is not None:
            best = rec
    return best or last


def load_meter():
    return [r for fn in METERS for r in iter_jsonl(os.path.join(ROOT, fn))]


# ----------------------------------------------------------------- statistics
def rankdata(a):
    """Ranks with TIES AVERAGED. plot_corpus.py's argsort(argsort(.)) does not do this."""
    a = np.asarray(a, float)
    order = np.argsort(a, kind="mergesort")
    r = np.empty(len(a), float)
    r[order] = np.arange(len(a), dtype=float)
    sa = a[order]
    i = 0
    while i < len(sa):
        j = i
        while j + 1 < len(sa) and sa[j + 1] == sa[i]:
            j += 1
        if j > i:
            r[order[i:j + 1]] = (i + j) / 2.0
        i = j + 1
    return r


def spearman(x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    if len(x) < 3:
        return None, len(x)
    rx, ry = rankdata(x), rankdata(y)
    if rx.std() == 0 or ry.std() == 0:
        return None, len(x)
    return float(np.corrcoef(rx, ry)[0, 1]), len(x)


_CRIT = {}


def critical(n, draws=4000, seed=1):
    """Two-sided 5 % |rho| under the permutation null. Printed beside every rho so a reader never
    has to guess whether -0.24 at n=10 means anything (it does not)."""
    if n < 3:
        return float("nan")
    if n not in _CRIT:
        rng = np.random.RandomState(seed)
        x = np.arange(n, dtype=float)
        _CRIT[n] = float(np.percentile(
            [abs(spearman(x, rng.permutation(n).astype(float))[0]) for _ in range(draws)], 95))
    return _CRIT[n]


def loo(x, y):
    """rho after dropping the single most influential point, and which point that was."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    ok = np.isfinite(x) & np.isfinite(y)
    idx = np.where(ok)[0]
    x, y = x[ok], y[ok]
    r0, n = spearman(x, y)
    if r0 is None or n < 4:
        return r0, None, None
    worst = None
    for i in range(n):
        m = np.ones(n, bool)
        m[i] = False
        r, _ = spearman(x[m], y[m])
        if r is None:
            continue
        if worst is None or abs(r - r0) > abs(worst[0] - r0):
            worst = (r, i)
    return r0, worst[0], idx[worst[1]]


def rho_line(name, x, y, labels=None):
    r, n = spearman(x, y)
    if r is None:
        return f"  {name}: n={n} — too few points, no correlation reported"
    c = critical(n)
    r0, rl, wi = loo(x, y)
    verdict = "SIGNIFICANT at 5 %" if abs(r) >= c else "not distinguishable from zero"
    s = f"  {name}: rho={r:+.2f} (n={n}, 5 % bar {c:.2f}) — {verdict}"
    if rl is not None:
        who = labels[wi] if labels else f"#{wi}"
        s += f"; drop {who} -> {rl:+.2f}"
    return s


def ratio_null(a, b, seed=0, draws=5000):
    """rho(b/a, a) is negative BY CONSTRUCTION. Returns (observed, null mean, p). Never report a
    ratio-vs-denominator correlation without this."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    obs, _ = spearman(a, b / a)
    rng = np.random.RandomState(seed)
    null = []
    for _ in range(draws):
        bp = rng.permutation(b)
        r, _ = spearman(a, bp / a)
        if r is not None:
            null.append(r)
    null = np.asarray(null)
    return obs, float(null.mean()), float((null <= obs).mean())


# ----------------------------------------------------------------- run scan
def scan(rundir):
    sp_path = os.path.join(rundir, "spans.jsonl")
    if not os.path.exists(sp_path):
        return None
    cost_spans = 0.0
    gens = 0
    ptok = ctok = 0
    phase_cost = defaultdict(float)
    phase_gens = Counter()
    dur = defaultdict(float)
    tools = Counter()
    tool_time = defaultdict(float)
    dev = Counter()
    models = Counter()
    t0, t1 = math.inf, 0.0
    for sp in iter_jsonl(sp_path):
        name = sp.get("name")
        a = sp.get("attributes") or {}
        st, du = sp.get("start"), sp.get("duration_s") or 0.0
        if isinstance(st, (int, float)):
            t0, t1 = min(t0, st), max(t1, st + du)
        dur[name] += du
        if name == "generation":
            gens += 1
            c = a.get("cost") or 0.0
            cost_spans += c
            ph = a.get("phase") or "?"
            phase_cost[ph] += c
            phase_gens[ph] += 1
            models[a.get("model")] += 1
            u = a.get("usage") or {}
            ptok += u.get("prompt") or u.get("prompt_tokens") or 0
            ctok += u.get("completion") or u.get("completion_tokens") or 0
        elif name == "tool":
            tools[a.get("tool")] += 1
            tool_time[a.get("tool")] += du
            if a.get("tool") == "run_dev_command":
                # the developer command's NAME lives in attributes.input as {"name": "check"}
                try:
                    dev[json.loads(a.get("input") or "{}").get("name")] += 1
                except Exception:
                    dev["?"] += 1

    usage, nodes_ev = [], []
    run_started = run_finished = finish_reason = None
    for e in iter_events(os.path.join(rundir, "events.jsonl")):
        t, ts, d = e.get("type"), e.get("ts"), e.get("data") or {}
        if t == "llm_usage":
            usage.append((ts, d.get("cost") or 0.0,
                          d.get("prompt_tokens") or 0, d.get("completion_tokens") or 0))
        elif t == "node_evaluated":
            nodes_ev.append((ts, d.get("node_id"), d.get("metric")))
        elif t == "run_started":
            run_started = ts
        elif t == "run_finished":
            run_finished, finish_reason = ts, d.get("reason")
    usage.sort(key=lambda r: r[0] or 0)
    nodes_ev.sort(key=lambda r: r[0] or 0)

    nodes = []
    ndir = os.path.join(rundir, "nodes")
    if os.path.isdir(ndir):
        for nm in sorted(os.listdir(ndir), key=lambda s: int(re.sub(r"\D", "", s) or -1)):
            rec = read_score_log(os.path.join(ndir, nm, "score.log"))
            if rec is not None:
                nodes.append(dict(node=nm, speedup=rec.get("speedup"),
                                  eval_seconds=rec.get("eval_seconds")))

    start = run_started or (t0 if t0 < math.inf else None)
    tl = []
    for ts, nid, metric in nodes_ev:
        c = p = q = 0.0
        for u_ts, u_c, u_p, u_q in usage:
            if u_ts is None or ts is None or u_ts > ts:
                break
            c, p, q = c + u_c, p + u_p, q + u_q
        tl.append(dict(ts=ts, node=nid, metric=metric, cost_at=c, tok_at=p + q,
                       t_rel=(ts - start) if (ts and start) else None))

    row = dict(
        rundir=rundir, cost_spans=cost_spans, cost=sum(u[1] for u in usage),
        gens=gens, gens_usage=len(usage), prompt_tokens=ptok, completion_tokens=ctok,
        model=(models.most_common(1)[0][0] if models else None),
        phase_cost=dict(phase_cost), phase_gens=dict(phase_gens),
        wall_s=(t1 - t0) if t0 < math.inf else 0.0, t0=start,
        obs_s=((run_finished or t1) - start) if start else 0.0,
        llm_s=dur.get("generation", 0.0),
        eval_s=dur.get("evaluate", 0.0) + dur.get("score", 0.0),
        tools=dict(tools), dev_cmds=dict(dev), nodes=nodes, n_nodes=len(nodes),
        measure_calls=tools.get("run_probe", 0) + tools.get("run_dev_command", 0),
        measure_s=tool_time.get("run_probe", 0.0) + tool_time.get("run_dev_command", 0.0),
        run_finished=run_finished, finish_reason=finish_reason, timeline=tl,
    )
    _timeline_metrics(row)
    return row


def _timeline_metrics(r):
    """THE CORRECTED X AXES. Not total budget -- time / money / tokens to the first node, to the
    champion, and between adjacent nodes."""
    tl, start = r["timeline"], r["t0"]
    for k in ("t_first", "t_best", "c_first", "c_best", "tok_first", "tok_best",
              "gap_s", "gap_usd", "gap_tok", "best_idx", "train_best"):
        r[k] = None
    if not tl or start is None:
        return
    r["t_first"], r["c_first"], r["tok_first"] = tl[0]["t_rel"], tl[0]["cost_at"], tl[0]["tok_at"]
    ms = [(e["metric"] if e["metric"] is not None else -1) for e in tl]
    bi = int(np.argmax(ms))
    r["best_idx"], r["train_best"] = bi, ms[bi]
    r["t_best"], r["c_best"], r["tok_best"] = tl[bi]["t_rel"], tl[bi]["cost_at"], tl[bi]["tok_at"]
    if len(tl) > 1:
        r["gap_s"] = float(np.median([tl[i + 1]["ts"] - tl[i]["ts"] for i in range(len(tl) - 1)]))
        r["gap_usd"] = float(np.median([tl[i + 1]["cost_at"] - tl[i]["cost_at"]
                                        for i in range(len(tl) - 1)]))
        r["gap_tok"] = float(np.median([tl[i + 1]["tok_at"] - tl[i]["tok_at"]
                                        for i in range(len(tl) - 1)]))


def epoch(r):
    if r["t0"] is None:
        return "?"
    return "E0" if r["t0"] < BREAK_CARD else ("E1" if r["t0"] < BREAK_PIP else "E2")


def collect():
    rows = []
    pdir = os.path.join(ROOT, "model-probes")
    for label in sorted(os.listdir(pdir)):
        base = os.path.join(pdir, label)
        runs = os.path.join(base, "runs")
        if not os.path.isdir(runs):
            continue
        for task in sorted(os.listdir(runs)):
            r = scan(os.path.join(runs, task, "run"))
            if r is None:
                continue
            fin = jload(os.path.join(base, "final.json"))
            r.update(source="probe", label=label, task=task,
                     test_speedup=(fin or {}).get("speedup"), has_final=fin is not None)
            rows.append(r)
    cdir = os.path.join(ROOT, "runs-B")
    for task in sorted(os.listdir(cdir)):
        r = scan(os.path.join(cdir, task, "run"))
        if r is None:
            continue
        fin = jload(os.path.join(ROOT, "campaign-final", f"B-{task}.final.json"))
        res = jload(os.path.join(ROOT, "rescored", f"{task}.json"))
        r.update(source="campaign-B", label="B", task=task,
                 test_speedup=(fin or res or {}).get("speedup"),
                 has_final=(fin is not None or res is not None))
        rows.append(r)
    return rows


def collect_armA(meter):
    """Arm A ran 2-5 attempts per task; the surviving log is the LAST one. plot_corpus filters the
    meter by `ts >= last attempt epoch`. The meter rows also carry an explicit `attempt` LABEL, so
    we filter by that instead -- it is exact rather than inferred. (They agree to the cent on all
    20 tasks; this is belt and braces, not a correction.)"""
    out = []
    cf = os.path.join(ROOT, "campaign-final")
    for fn in sorted(os.listdir(cf)):
        if not (fn.startswith("A-") and fn.endswith(".log")):
            continue
        task = fn[2:-4]
        msgs = selfrep = test = None
        cmds = Counter()
        for ln in open(os.path.join(cf, fn), encoding="utf-8", errors="replace"):
            m = re.search(r"You have sent (\d+) messages and have used up \$([0-9]+(?:\.[0-9]+)?)", ln)
            if m:
                msgs, selfrep = int(m.group(1)), float(m.group(2))
            m = re.search(r"Running full dataset evaluation on '(\w+)' subset for command '(\w+)'", ln)
            if m:
                cmds[f"{m.group(2)}/{m.group(1)}"] += 1
            if "Using test dataset speedup for summary:" in ln:
                v = ln.rsplit(":", 1)[1].strip()
                test = None if v in ("None", "N/A") else float(v)
        att = os.path.join(cf, f"A-{task}.attempts")
        labs = re.findall(r"^(a\d+) ", open(att).read(), re.M) if os.path.exists(att) else []
        last = labs[-1] if labs else None
        mr = [r for r in meter if r.get("arm") == "A" and r.get("task") == task
              and r.get("attempt") == last]
        ts = [r["ts"] for r in mr if r.get("ts")]
        out.append(dict(task=task, test=test, selfrep=selfrep, msgs=msgs, attempts=len(labs),
                        cost=sum(r.get("cost") or 0.0 for r in mr),
                        cost_all=sum(r.get("cost") or 0.0 for r in meter
                                     if r.get("arm") == "A" and r.get("task") == task),
                        calls=len(mr), wall_s=(max(ts) - min(ts)) if len(ts) > 1 else 0.0,
                        measure_calls=sum(cmds.values())))
    return out


def task_features():
    feats = {}
    for task in sorted(os.listdir(DATA)):
        d = os.path.join(DATA, task)
        if not os.path.isdir(d):
            continue
        n = None
        for fn in os.listdir(d):
            m = re.match(r"^.+_T(\d+)ms_n(\d+)_size(\d+)_(train|test)\.jsonl$", fn)
            if m:
                n = int(m.group(2))
                break
        b = os.path.join(BASELINES, f"{task}__test__w22x1r3.json")
        if not os.path.exists(b):
            c = [f for f in os.listdir(BASELINES) if f.startswith(task + "__test__")]
            b = os.path.join(BASELINES, sorted(c)[0]) if c else None
        bt = jload(b, {}) if b else {}
        t = [v for v in bt.values() if isinstance(v, (int, float))]
        src = os.path.join(TASKSRC, task, f"{task}.py")
        loc = sum(1 for l in open(src, encoding="utf-8", errors="replace")
                  if l.strip() and not l.strip().startswith("#")) if os.path.exists(src) else 0
        feats[task] = dict(n_param=n, ref_loc=loc,
                           baseline_ms=float(np.mean(t)) if t else None,
                           ref_eval_s=(float(np.sum(t)) / 1000.0) if t else None)
    return feats


# ----------------------------------------------------------------- report
def main():
    meter = load_meter()
    rows = collect()
    A = collect_armA(meter)
    feats = task_features()
    P = print

    P("# AlgoTune corpus v2 — corrected slices")
    P("")
    P(f"{len(rows)} LoopLab runs, {sum(1 for r in rows if r['test_speedup'] is not None)} with a "
      f"test number; {len(A)} arm-A logs, {sum(1 for a in A if a['test'] is not None)} with one.")
    P("")

    P("## Epochs")
    for e in ("E0", "E1", "E2"):
        sub = [r for r in rows if epoch(r) == e]
        nodes = sum(len(r["timeline"]) for r in sub)
        P(f"* {e}: {len(sub)} runs started, {nodes} nodes evaluated inside them")
    post = [(r["label"], e["node"], e["metric"]) for r in rows for e in r["timeline"]
            if e["ts"] and e["ts"] >= BREAK_PIP]
    P(f"* node_evaluated events after the pip fix ({BREAK_PIP:.0f}): {len(post)} — {post}")
    P("")

    P("## Money: spans vs events vs ledger")
    sp = sum(r["cost_spans"] for r in rows)
    ev = sum(r["cost"] for r in rows)
    P(f"* corpus: spans ${sp:.2f}, events/ledger **${ev:.2f}** — the span stream is ${ev-sp:.2f} short")
    cb = [r for r in rows if r["source"] == "campaign-B"]
    P(f"* arm-B campaign: spans ${sum(r['cost_spans'] for r in cb):.2f}, events "
      f"${sum(r['cost'] for r in cb):.2f}; per-run shortfall median "
      f"{100*np.median([1 - r['cost_spans']/r['cost'] for r in cb]):.1f} %, max "
      f"{100*max(1 - r['cost_spans']/r['cost'] for r in cb):.1f} %")
    P(f"* whole ledger, all three files: ${sum(r.get('cost') or 0 for r in meter):.2f}")
    common = [a for a in A if a["test"] is not None]
    bmap = {r["task"]: r for r in cb}
    pairA = [a for a in common if a["task"] in bmap and bmap[a["task"]]["test_speedup"] is not None]
    P(f"* on the {len(pairA)} tasks both arms finished: AlgoTuner median "
      f"${np.median([a['cost'] for a in pairA]):.3f} vs LoopLab median "
      f"${np.median([bmap[a['task']]['cost'] for a in pairA]):.3f} "
      f"(the span stream would say ${np.median([bmap[a['task']]['cost_spans'] for a in pairA]):.3f} "
      f"— that is the artefact, not a run under its cap)")
    P("")

    P("## THE CORRECTED X AXES: metric against time / money / tokens to a node")
    Ls = [r for r in rows if r["test_speedup"] is not None and r["t_first"] is not None]
    lab = [f"{r['label']}/{r['task'][:10]}" for r in Ls]
    y = [r["test_speedup"] for r in Ls]
    for name, k in (("time to FIRST evaluated node (s)", "t_first"),
                    ("time to BEST (champion) node (s)", "t_best"),
                    ("median seconds between evaluated nodes", "gap_s"),
                    ("$ before FIRST node", "c_first"),
                    ("$ before BEST node", "c_best"),
                    ("median $ between evaluated nodes", "gap_usd"),
                    ("tokens before FIRST node", "tok_first"),
                    ("tokens before BEST node", "tok_best"),
                    ("median tokens between evaluated nodes", "gap_tok"),
                    ("[superseded] total wall clock", "obs_s"),
                    ("[superseded] total $", "cost")):
        P(rho_line(name, [r[k] if r[k] is not None else np.nan for r in Ls], y, lab))
    P(f"  descriptive: median run spends "
      f"{100*np.median([r['t_first']/r['obs_s'] for r in Ls if r['obs_s']]):.0f} % of its wall clock "
      f"and {100*np.median([r['c_first']/r['cost'] for r in Ls if r['cost']]):.0f} % of its money "
      f"before its FIRST evaluated node.")
    P("")

    P("## Search payoff over node 0, by epoch (score.log nodes, the complete source)")
    def payoff(sub, name):
        g = []
        for r in sub:
            ys = [n["speedup"] or 0.0 for n in r["nodes"]]
            if len(ys) >= 2 and ys[0] > 0:
                g.append((max(ys) / ys[0], int(np.argmax(ys)) == 0, r["label"]))
        if not g:
            P(f"* {name}: n=0")
            return
        P(f"* {name}: n={len(g)}, median gain {np.median([x[0] for x in g]):.2f}x, "
          f"champion IS node 0 in {sum(1 for x in g if x[1])}/{len(g)}, "
          f"gain beyond +-{NOISE_HIGH:.0%} in {sum(1 for x in g if x[0] > 1+NOISE_HIGH)}/{len(g)}")
    payoff(rows, "all runs")
    for e in ("E0", "E1"):
        payoff([r for r in rows if epoch(r) == e], e)
    payoff([r for r in rows if r["label"] in FRESH], "fresh 10 probes")
    payoff([r for r in rows if r["label"] in FRESH and r["test_speedup"] is not None],
           "fresh, finished only")
    P("")

    P("## Code share by epoch (plan + plan_step)")
    for name, sub in (("E0", [r for r in rows if epoch(r) == "E0"]),
                      ("E1", [r for r in rows if epoch(r) == "E1"]),
                      ("fresh 10", [r for r in rows if r["label"] in FRESH])):
        cs = [100 * sum(r["phase_cost"].get(p, 0) for p in CODE_PHASES) / max(r["cost_spans"], 1e-9)
              for r in sub]
        P(f"* {name}: n={len(sub)}, median {np.median(cs):.0f} % (range {min(cs):.0f}-{max(cs):.0f} %)")
    Ss = [r for r in rows if r["test_speedup"] is not None]
    for name, sub in (("all scored", Ss), ("E0 scored", [r for r in Ss if epoch(r) == "E0"]),
                      ("E1 scored", [r for r in Ss if epoch(r) == "E1"])):
        cs = [100 * sum(r["phase_cost"].get(p, 0) for p in CODE_PHASES) / max(r["cost_spans"], 1e-9)
              for r in sub]
        P(rho_line(f"code-share vs test speedup [{name}]", cs,
                   [r["test_speedup"] for r in sub], [r["label"] for r in sub]))
    P("")

    P("## Difficulty hypothesis (arm B vs arm A, all of it epoch E0)")
    ok = [(a, bmap[a["task"]]) for a in pairA if a["test"] > 0]
    labs = [a["task"][:12] for a, _ in ok]
    rat = [b["test_speedup"] / a["test"] for a, b in ok]
    for k, nm in (("n_param", "instance size N"), ("baseline_ms", "reference ms/instance"),
                  ("ref_eval_s", "one full reference pass (s)"), ("ref_loc", "reference LOC")):
        P(rho_line(f"ratio vs {nm}", [feats[a['task']][k] for a, _ in ok], rat, labs))
    obs, nul, p = ratio_null([a["test"] for a, _ in ok], [b["test_speedup"] for _, b in ok])
    P(f"  ratio vs AlgoTuner's OWN score: rho={obs:+.2f} — but the ratio has that score in its "
      f"DENOMINATOR. Permutation null (B shuffled, no real link): mean {nul:+.2f}, "
      f"p={p:.2f}. NOT a finding.")
    P("")

    P("## Developer commands (attributes.input on tool/run_dev_command)")
    for r in rows:
        if r["dev_cmds"].get("check"):
            P(f"* {r['label']:9s} {r['task'][:12]:12s} {r['dev_cmds']}"
              + ("   [LIVE snapshot]" if r["test_speedup"] is None else ""))
    pooled = Counter()
    for r in rows:
        for k, v in r["dev_cmds"].items():
            pooled[k] += v
    P(f"* pooled across the corpus: {dict(pooled)}; run_probe "
      f"{sum(r['tools'].get('run_probe', 0) for r in rows)}")
    P("")
    P("See plots/review-of-corpus-analysis.md for what each of these overturns.")


if __name__ == "__main__":
    sys.exit(main())
