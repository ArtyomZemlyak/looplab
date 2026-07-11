"""Self-contained E2E snapshot: render a finished LoopLab run (events.jsonl + spans.jsonl) into one
openable dark-themed HTML with inline-SVG charts (DAG, best-so-far trajectory, ablation sensitivity,
gantt timeline) + trust/robustness, diversity, and a node table. Reuses the canonical projections
(replay.fold + traceview), so it's true to the real run — a reliable visual when a live screenshot
isn't available. Usage: python -m tools.e2e_report runs/e2e [out.html]
"""
from __future__ import annotations

import html
import sys
from pathlib import Path

from looplab.eventstore import EventStore
from looplab.replay import fold
from looplab.traceview import build_trace_view, load_spans

C = dict(bg="#0c0e12", bg1="#12151c", bg2="#181c25", line="#2a3038", fg="#e6e9ef",
         dim="#9aa3b2", mut="#6b7480", ok="#2ecc71", fail="#ef4444", work="#f0b429",
         best="#ffd54a", accent="#4aa3ff", infeasible="#7a6b9a", violet="#9a6bff")


def esc(s):
    return html.escape(str(s))


def fmt(v, p=4):
    if v is None:
        return "—"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return esc(v)
    a = abs(v)
    if a and (a < 1e-3 or a >= 1e6):
        return f"{v:.2e}"
    return f"{float(f'{v:.{p}g}')}"


def layout(nodes):
    depth = {}
    def d(i):
        if i in depth:
            return depth[i]
        ps = [p for p in nodes[i].parent_ids if p in nodes]
        depth[i] = 1 + max((d(p) for p in ps), default=-1) if ps else 0
        return depth[i]
    for i in nodes:
        d(i)
    by = {}
    for i in nodes:
        by.setdefault(depth[i], []).append(i)
    pos, XS, YS = {}, 150, 90
    for dp, arr in by.items():
        arr.sort()
        off = -(len(arr) - 1) / 2
        for k, i in enumerate(arr):
            pos[i] = ((off + k) * XS, dp * YS)
    return pos


def svg_dag(st):
    nodes = st.nodes
    if not nodes:
        return "<p class=mut>no nodes</p>"
    pos = layout(nodes)
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    W, H, pad = (maxx - minx) + 200, (maxy - miny) + 120, 80
    def X(x): return x - minx + pad
    def Y(y): return y - miny + 50
    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" style="max-height:420px">']
    for n in nodes.values():
        for p in n.parent_ids:
            if p in pos:
                col = C["fail"] if n.operator == "debug" else C["line"]
                parts.append(f'<line x1="{X(pos[p][0])}" y1="{Y(pos[p][1])+22}" x2="{X(pos[n.id][0])}" '
                             f'y2="{Y(pos[n.id][1])-22}" stroke="{col}" stroke-width="1.5"/>')
    for n in nodes.values():
        x, y = X(pos[n.id][0]), Y(pos[n.id][1])
        border = (C["best"] if n.id == st.best_node_id else
                  C["fail"] if n.status == "failed" else
                  C["infeasible"] if not n.feasible else C["ok"] if n.status == "evaluated" else C["mut"])
        m = n.robust_metric
        crown = " ♚" if n.id == st.best_node_id else ""
        parts.append(
            f'<g transform="translate({x-58},{y-22})">'
            f'<rect width="116" height="44" rx="8" fill="{C["bg2"]}" stroke="{border}" stroke-width="2"/>'
            f'<text x="8" y="17" fill="{C["fg"]}" font-size="12" font-weight="700">#{n.id}{crown}'
            f'<tspan fill="{C["dim"]}" font-weight="400"> {esc(n.operator)}</tspan></text>'
            f'<text x="8" y="34" fill="{C["fg"]}" font-size="13" font-weight="600">{fmt(m)}</text></g>')
    parts.append("</svg>")
    return "".join(parts)


def svg_trajectory(st):
    ev = sorted([n for n in st.nodes.values() if n.metric is not None], key=lambda n: n.id)
    if not ev:
        return "<p class=mut>no evaluated nodes</p>"
    W, H, pad = 760, 240, 40
    ids = [n.id for n in ev]
    ys = [n.robust_metric for n in ev]
    miny, maxy = min(ys), max(ys)
    def X(i): return pad + (i - min(ids)) / max(1, (max(ids) - min(ids))) * (W - pad - 12)
    def Y(v): return H - pad - (v - miny) / max(1e-9, maxy - miny) * (H - pad - 14)
    best, pts = None, []
    for n in ev:
        v = n.robust_metric
        if n.feasible and (best is None or (v < best if st.direction == "min" else v > best)):
            best = v
        if best is not None:
            pts.append((X(n.id), Y(best)))
    line = " ".join(("L" if k else "M") + f"{x:.1f} {y:.1f}" for k, (x, y) in enumerate(pts))
    dots = "".join(f'<circle cx="{X(n.id):.1f}" cy="{Y(n.robust_metric):.1f}" '
                   f'r="4" fill="{C["infeasible"] if not n.feasible else C["accent"]}" opacity=".85"/>' for n in ev)
    grid = "".join(f'<line x1="{pad}" x2="{W-12}" y1="{pad/2+t*(H-pad-14)}" y2="{pad/2+t*(H-pad-14)}" stroke="{C["bg2"]}"/>'
                   for t in (0, .25, .5, .75, 1))
    return (f'<svg viewBox="0 0 {W} {H}" width="100%">{grid}{dots}'
            f'<path d="{line}" fill="none" stroke="{C["ok"]}" stroke-width="2"/>'
            f'<text x="{pad}" y="14" fill="{C["mut"]}" font-size="11">best so far: {fmt(best)} · node id →</text></svg>')


def svg_bars(data, color):
    if not data:
        return "<p class=mut>no ablation events (none ran)</p>"
    mx = max(abs(v) for _, v in data) or 1e-9
    W, bh, gap, lab = 760, 26, 10, 150
    H = len(data) * (bh + gap) + 8
    rows = []
    for i, (k, v) in enumerate(data):
        y = i * (bh + gap) + 4
        bw = abs(v) / mx * (W - lab - 70)
        rows.append(f'<text x="{lab-8}" y="{y+bh/2+4}" fill="{C["fg"]}" font-size="12" text-anchor="end">{esc(k)}</text>'
                    f'<rect x="{lab}" y="{y}" width="{bw:.1f}" height="{bh}" rx="3" fill="{color}" opacity=".85"/>'
                    f'<text x="{lab+bw+6:.1f}" y="{y+bh/2+4}" fill="{C["mut"]}" font-size="11">{fmt(v)}</text>')
    return f'<svg viewBox="0 0 {W} {H}" width="100%">{"".join(rows)}</svg>'


def svg_gantt(tv):
    flat = []
    def walk(arr, nid):
        for s in arr:
            flat.append((nid, s.get("name"), s.get("start", 0), s.get("duration_s", 0) or 0, s.get("status") == "ERROR"))
            walk(s.get("children", []), nid)
    for nid, arr in (tv.get("nodes") or {}).items():
        walk(arr, nid)
    if not flat:
        return "<p class=mut>no spans</p>"
    t0 = min(s[2] for s in flat)
    t1 = max(s[2] + s[3] for s in flat)
    span = max(1e-6, t1 - t0)
    W, rh, lab = 760, 13, 150
    H = len(flat) * rh + 24
    pal = {"evaluate": C["ok"], "implement": C["accent"], "propose": C["violet"],
           "repair": C["fail"], "ablate": C["work"], "confirm_seed": C["ok"], "create_node": C["dim"]}
    def X(t): return lab + (t - t0) / span * (W - lab - 20)
    rows = []
    for i, (nid, name, start, dur, err) in enumerate(sorted(flat, key=lambda s: s[2])):
        y = i * rh + 4
        bw = max(2, dur / span * (W - lab - 20))
        rows.append(f'<text x="{lab-6}" y="{y+9}" fill="{C["dim"]}" font-size="9" text-anchor="end">{esc(nid)}:{esc(name)}</text>'
                    f'<rect x="{X(start):.1f}" y="{y}" width="{bw:.1f}" height="{rh-4}" rx="2" '
                    f'fill="{C["fail"] if err else pal.get(name, C["accent"])}" opacity=".85"/>')
    return (f'<svg viewBox="0 0 {W} {H}" width="100%">{"".join(rows)}'
            f'<text x="{lab}" y="{H-4}" fill="{C["mut"]}" font-size="11">{fmt(span)}s total · {len(flat)} spans</text></svg>')


def build(run_dir: Path) -> str:
    st = fold(EventStore(run_dir / "events.jsonl").read_all())
    tv = build_trace_view(st, load_spans(run_dir / "spans.jsonl"))
    best = st.best()
    ev = [n for n in st.nodes.values() if n.status == "evaluated"]
    failed = [n for n in st.nodes.values() if n.status == "failed"]
    # ablation impacts (latest per param)
    impacts = {}
    for a in st.ablations:
        for k, v in (a.get("impacts") or {}).items():
            impacts[k] = abs(v)
    bars = sorted(impacts.items(), key=lambda kv: -kv[1])
    # confirmed (robustness)
    conf = [n for n in st.nodes.values() if n.confirmed_mean is not None]
    chooser = min if st.direction == "min" else max
    naive = chooser((n for n in ev if n.feasible), key=lambda n: (n.metric, n.id), default=None)

    def card(title, body):
        return f'<div class="card"><div class="h">{title}</div>{body}</div>'

    conf_tbl = "".join(
        f'<tr><td>#{n.id}</td><td>{fmt(n.metric)}</td><td>{fmt(n.confirmed_mean)} ± {fmt(n.confirmed_std)}</td>'
        f'<td>{n.confirmed_seeds}</td></tr>' for n in conf) or '<tr><td colspan=4 class=mut>none</td></tr>'
    arch = st.archive or {}
    arch_tbl = "".join(f'<tr><td>#{e["node_id"]}</td><td>{fmt(e["metric"])}</td><td class=mut>{esc(e.get("params"))}</td></tr>'
                       for e in (arch.get("elites") or [])) or '<tr><td colspan=3 class=mut>—</td></tr>'
    node_tbl = "".join(
        f'<tr><td>#{n.id}{" ♚" if n.id==st.best_node_id else ""}</td><td>{esc(n.operator)}</td>'
        f'<td>{esc(",".join(map(str,n.parent_ids)) or "—")}</td>'
        f'<td style="color:{C["ok"] if n.status=="evaluated" else C["fail"] if n.status=="failed" else C["mut"]}">{esc(n.status)}</td>'
        f'<td>{fmt(n.metric)}</td><td>{fmt(n.eval_seconds)}</td></tr>' for n in sorted(st.nodes.values(), key=lambda n: n.id))

    seedluck = (f'<div class="kv"><span class="k">naive single-eval leader</span><b>#{naive.id} · {fmt(naive.metric)}</b>'
                f'<span class="k">selected robust winner</span><b>#{best.id} · {fmt(best.robust_metric)}</b></div>'
                + (f'<div class="flag">↳ demotion: single-eval leader #{naive.id} was corrected by multi-seed confirmation</div>'
                   if naive and best and naive.id != best.id else '')) if (naive and best) else '<span class=mut>n/a</span>'

    cost = st.llm_cost
    head = (f'<div class="kv4">'
            f'<div><div class="big">{fmt(best.metric) if best else "—"}</div><div class="l">best metric (#{best.id if best else "—"})</div></div>'
            f'<div><div class="big">{len(st.nodes)}</div><div class="l">nodes · {len(ev)} eval · {len(failed)} fail</div></div>'
            f'<div><div class="big">{len(conf)}</div><div class="l">confirmed (robust)</div></div>'
            f'<div><div class="big">{fmt(st.total_eval_seconds,3)}s</div><div class="l">eval compute'
            + (f' · {cost["total_tokens"]} tok' if cost else '') + '</div></div></div>')

    return f"""<!doctype html><html><head><meta charset=utf-8><title>LoopLab E2E — {esc(st.run_id)}</title>
<style>
:root{{color-scheme:dark}}
body{{margin:0;background:{C['bg']};color:{C['fg']};font:14px ui-sans-serif,system-ui,'Segoe UI',Roboto,sans-serif}}
.wrap{{max-width:980px;margin:0 auto;padding:24px}}
h1{{font-size:20px;margin:0 0 2px}} .goal{{color:{C['dim']};margin-bottom:16px}}
.card{{background:{C['bg1']};border:1px solid {C['line']};border-radius:12px;padding:14px 16px;margin:12px 0}}
.card .h{{font-weight:700;margin-bottom:8px;color:{C['fg']}}}
.kv4{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:8px 0 4px}}
.big{{font-size:26px;font-weight:700}} .l{{color:{C['mut']};font-size:12px}}
.kv{{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;align-items:center}}
.kv .k{{color:{C['mut']}}} .flag{{color:{C['fail']};margin-top:6px}}
table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}
th,td{{text-align:left;padding:5px 8px;border-bottom:1px solid {C['line']}}} th{{color:{C['mut']};font-weight:500}}
.mut{{color:{C['mut']}}} .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.badge{{display:inline-block;font-size:11px;padding:1px 8px;border-radius:999px;border:1px solid {C['line']};color:{C['dim']}}}
</style></head><body><div class="wrap">
<h1>◉ LoopLab — E2E run snapshot <span class="badge">{esc(st.run_id)}</span></h1>
<div class="goal">{esc(st.goal)} <span class="badge">direction: {esc(st.direction)}</span> <span class="badge">{esc(st.task_id)}</span></div>
{card("Summary", head)}
{card("Research DAG", svg_dag(st))}
<div class="grid2">
{card("Best-metric trajectory (feasible-aware)", svg_trajectory(st))}
{card("Parameter sensitivity (ablation |Δmetric|)", svg_bars(bars, C['violet']))}
</div>
{card("Execution timeline (spans)", svg_gantt(tv))}
<div class="grid2">
{card("Trust — robustness &amp; seed-luck", seedluck + '<table><tr><th>node</th><th>single</th><th>robust mean±std</th><th>seeds</th></tr>'+conf_tbl+'</table>')}
{card("Diversity archive ("+str(arch.get("niches","?"))+" niches)", '<table><tr><th>node</th><th>metric</th><th>params</th></tr>'+arch_tbl+'</table>')}
</div>
{card("All nodes", '<table><tr><th>node</th><th>operator</th><th>parents</th><th>status</th><th>metric</th><th>eval s</th></tr>'+node_tbl+'</table>')}
<div class="mut" style="margin-top:18px;font-size:12px">Generated from {esc(run_dir.name)}/events.jsonl + spans.jsonl via replay.fold + traceview — the same projections the live React UI consumes.</div>
</div></body></html>"""


if __name__ == "__main__":
    rd = Path(sys.argv[1] if len(sys.argv) > 1 else "runs/e2e")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else rd / "e2e_report.html"
    out.write_text(build(rd), encoding="utf-8")
    print(f"wrote {out}")
