"""Static-HTML lineage view (I6, ADR-1). A decoupled *reader* of files-as-truth:
takes a RunState and renders a standalone HTML page (no server, no JS framework).
"""
from __future__ import annotations

import html

from looplab.core.models import NodeStatus, RunState


def _span_li(span: dict) -> str:
    """One span as a nested <li>: name · duration · status, recursing into children."""
    status = span.get("status", "OK")
    color = "#b42318" if status == "ERROR" else "#555"
    dur = span.get("duration_s")
    dur_s = f" · {dur*1000:.0f}ms" if isinstance(dur, (int, float)) else ""
    attrs = span.get("attributes", {})
    # show a couple of the most useful attributes inline
    extra = []
    for k in ("metric", "exit_code", "error_reason", "drift", "tokens", "cost_usd"):
        if k in attrs and attrs[k] is not None:
            extra.append(f"{k}={attrs[k]}")
    extra_s = (" <span style='color:#888'>(" + html.escape(", ".join(extra)) + ")</span>") if extra else ""
    err = ""
    for ev in span.get("events", []):
        if ev.get("name") == "exception":
            err = f"<div style='color:#b42318;font-size:11px'>{html.escape(str(ev.get('error',''))[:200])}</div>"
    kids = "".join(_span_li(c) for c in span.get("children", []))
    kids_ul = f"<ul>{kids}</ul>" if kids else ""
    return (f"<li><span style='color:{color}'>{html.escape(span.get('name',''))}</span>"
            f"{dur_s}{extra_s}{err}{kids_ul}</li>")


def _span_forest_html(forest: list[dict]) -> str:
    if not forest:
        return "<span style='color:#999'>—</span>"
    return "<ul style='margin:.2rem 0;padding-left:1.1rem'>" + \
           "".join(_span_li(s) for s in forest) + "</ul>"

_STATUS_COLOR = {
    NodeStatus.evaluated: "#1a7f37",
    NodeStatus.failed: "#b42318",
    NodeStatus.pending: "#9a6700",
}


def _agent_badge(report: dict | None) -> str:
    """External-agent audit badge (ADR-7): how the coding agent performed on this node."""
    if not report:
        return "<span style='color:#999'>—</span>"
    if report.get("fell_back"):
        return "<span style='color:#9a6700' title='agent failed validation; LLM fallback used'>↩ fallback</span>"
    if report.get("ok"):
        att = report.get("attempts")
        suffix = f" ×{att}" if att and att > 1 else ""
        return f"<span style='color:#1a7f37' title='agent output validated'>✓ agent{suffix}</span>"
    return "<span style='color:#b42318' title='agent output invalid, no fallback'>✗ agent</span>"


def render_html(state: RunState, trace_view: dict | None = None) -> str:
    trace_nodes = (trace_view or {}).get("nodes", {})
    rows = []
    for n in sorted(state.nodes.values(), key=lambda n: n.id):
        best = " ⭐" if n.id == state.best_node_id else ""
        color = _STATUS_COLOR.get(n.status, "#444")
        parents = ", ".join(map(str, n.parent_ids)) or "—"
        metric = "" if n.metric is None else f"{n.metric:.6g}"
        params = html.escape(str(n.idea.params))
        status_cell = n.status.value
        if n.status is NodeStatus.failed and n.error_reason:
            status_cell += f" <span style='color:#888;font-weight:400'>({html.escape(n.error_reason)})</span>"
        if not n.feasible:
            status_cell += " <span style='color:#b42318' title='constraint violated'>⚠ infeasible</span>"
        secs = "" if n.eval_seconds is None else f"{n.eval_seconds:.2f}s"
        trace_cell = _span_forest_html(trace_nodes.get(str(n.id), []))
        rows.append(
            f"<tr>"
            f"<td>{n.id}{best}</td>"
            f"<td>{parents}</td>"
            f"<td>{html.escape(n.operator)}</td>"
            f"<td><code>{params}</code></td>"
            f"<td style='color:{color};font-weight:600'>{status_cell}</td>"
            f"<td style='text-align:right'>{metric}</td>"
            f"<td style='text-align:right'>{secs}</td>"
            f"<td>{_agent_badge(n.agent_report)}</td>"
            f"<td>{trace_cell}</td>"
            f"</tr>"
        )
    best = state.best()
    if best:
        bm = best.robust_metric
        bm_s = "—" if bm is None else f"{bm:.6g}"
        best_line = f"<b>Best:</b> node {best.id} — metric {bm_s} — params {html.escape(str(best.idea.params))}"
    else:
        best_line = "<b>Best:</b> (none yet)"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>LoopLab — {html.escape(state.run_id)}</title>
<style>
 body{{font-family:system-ui,Segoe UI,sans-serif;margin:2rem;color:#1c1c1c}}
 table{{border-collapse:collapse;width:100%;margin-top:1rem}}
 th,td{{border:1px solid #ddd;padding:.4rem .6rem;font-size:14px}}
 th{{background:#f5f5f5;text-align:left}}
 code{{font-size:12px}}
 .meta{{color:#555}}
</style></head><body>
<h1>LoopLab run <code>{html.escape(state.run_id)}</code></h1>
<p class="meta">Task: <b>{html.escape(state.task_id)}</b> · goal: {html.escape(state.goal)} ·
 direction: {state.direction} · config: <code>{html.escape(state.config_hash)}</code> ·
 finished: {state.finished}</p>
<p>{best_line}</p>
<table>
 <thead><tr><th>node</th><th>parents</th><th>operator</th><th>params</th><th>status</th><th>metric</th><th>eval</th><th>agent</th><th>trace</th></tr></thead>
 <tbody>{''.join(rows)}</tbody>
</table>
</body></html>
"""
