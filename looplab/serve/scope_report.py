"""Cross-run aggregate reports (UI-only, on-demand): synthesize a portfolio-level report over a SET
of runs — a project folder, a task, or a super-task. Each run's OWN per-run report is the unit of
evidence (cheap by default); a bounded tool loop lets the agent drill into any run when the digest
isn't enough (full access on demand). Mirrors report.py's contract: degrades offline, never raises,
returns a content dict the UI renders unconditionally.

The server resolves a scope → run briefs (+ a `drill` callback into any run's experiments) and calls
generate_scope_report(); this module stays free of run-root / event-store details so it's unit-testable
with plain dicts.
"""
from __future__ import annotations

from typing import Callable, Optional

from pydantic import BaseModel


class _AggReport(BaseModel):
    """Portfolio-level findings across a set of runs (the cross-run analogue of report._ReportOut).
    Every field has a default so an offline/partial generation still renders."""
    headline: str = ""
    verdict: str = ""                 # which approach / model / config wins across the runs, and why
    best_runs: list = []              # [{"run_id","metric","why"}] — the standout runs, ranked
    what_worked: list = []            # techniques that recurred in the winners
    what_didnt: list = []             # dead ends seen across runs (don't repeat these)
    learnings: list = []              # cross-run insights (model/policy/feature patterns)
    next_directions: list = []        # what to try next, informed by the whole portfolio
    caveats: list = []


def _fmt_metric(m) -> str:
    if m is None:
        return "—"
    return f"{m:.5g}" if isinstance(m, (int, float)) else str(m)


def run_brief_line(b: dict, full: bool = False) -> str:
    """One markdown block per run. The compact form (digest) shows headline/verdict/what-worked/etc.;
    `full=True` (the read_run drill tool) additionally surfaces the report's learnings + caveats, which
    the digest omits — so calling read_run actually returns signal the agent didn't already have."""
    rep = b.get("report") if isinstance(b.get("report"), dict) else None
    out = [f"### run {b['run_id']}" + (f" ({b['label']})" if b.get("label") else "")]
    out.append(f"task={b.get('task_id')} · model={b.get('model') or '?'} · policy={b.get('policy') or '?'} "
               f"· best={_fmt_metric(b.get('best_metric'))} ({b.get('direction') or '?'}) "
               f"· {b.get('phase') or ''} · {b.get('nodes')} nodes")
    if b.get("goal"):
        out.append(f"goal: {b['goal']}")
    if rep:
        for k in ("headline", "verdict", "champion_summary"):
            if rep.get(k):
                out.append(f"{k}: {rep[k]}")
        extra = ("learnings", "caveats") if full else ()
        for k in ("what_worked", "what_didnt", "next_directions", *extra):
            v = rep.get(k)
            if v:
                items = v if isinstance(v, (list, tuple)) else [v]
                out.append(f"{k.replace('_', ' ')}: " + "; ".join(str(x) for x in items))
    else:
        out.append("(no per-run report — metrics/config only)")
    return "\n".join(out)


def _has_content(d) -> bool:
    """True when an agg-report dict carries SOME substantive content — used to reject an all-default
    'blank' emit (a weak model calling emit_report with {}) so we fall through to the metrics rollup
    instead of persisting/showing an empty report."""
    if not isinstance(d, dict):
        return False
    return bool((d.get("headline") or "").strip() or (d.get("verdict") or "").strip()
                or d.get("best_runs") or d.get("what_worked") or d.get("what_didnt")
                or d.get("learnings") or d.get("next_directions"))


def build_digest(scope_label: str, briefs: list) -> str:
    head = (f"Cross-run portfolio for {scope_label}: {len(briefs)} run(s). Synthesize what the WHOLE "
            "set teaches — recurring winners, dead ends, the best runs, and where to go next.")
    return head + "\n\n" + "\n\n".join(run_brief_line(b) for b in briefs)


def _ranked(briefs: list) -> list:
    """Runs with a metric, ordered best-first by EACH run's OWN direction. A project / super-task scope
    can mix min-objective (RMSE/loss) and max-objective (accuracy/AUC) runs, so a single set-wide
    direction would rank the minority backwards (a 0.95-accuracy run sorted as 'worst' among loss runs).
    Metrics across different tasks aren't directly comparable, but a per-run direction key at least never
    ranks a max-objective run as if lower were better."""
    rated = [b for b in briefs if b.get("best_metric") is not None]
    return sorted(rated, key=lambda b: (b["best_metric"] if b.get("direction") != "max" else -b["best_metric"]))


def _deterministic(scope_label: str, briefs: list) -> dict:
    """Offline / no-model fallback: an honest metrics-only rollup so the panel still shows something."""
    n_rep = sum(1 for b in briefs if isinstance(b.get("report"), dict) and b["report"])
    best = _ranked(briefs)[:5]
    return _AggReport(
        headline=f"{len(briefs)} runs in {scope_label} · {n_rep} with reports",
        verdict="(model unavailable — deterministic metrics rollup)",
        best_runs=[{"run_id": b["run_id"], "metric": b.get("best_metric"),
                    "why": f"{b.get('model') or '?'} / {b.get('policy') or '?'}"} for b in best],
        learnings=[f"{b['run_id']}: best {_fmt_metric(b.get('best_metric'))} "
                   f"({b.get('model') or '?'}, {b.get('policy') or '?'})" for b in briefs[:12]],
        caveats=["Generated without an LLM — only metrics/config, no synthesis."],
    ).model_dump()


class _CrossRunTools:
    """Cross-run access for the aggregate-report agent: list the runs, read any run's full per-run
    report, and drill into one experiment of one run (via the server-provided `drill` callback —
    'full access to all runs in scope'). Same .specs()/.execute() shape as RunTools/DataTools."""

    def __init__(self, briefs: list, drill: Optional[Callable[[str, int], str]] = None):
        self._briefs = {b["run_id"]: b for b in briefs}
        self._drill = drill

    def specs(self) -> list:
        return [
            {"type": "function", "function": {
                "name": "list_runs",
                "description": "List every run in this scope with model, policy, best metric and phase.",
                "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {
                "name": "read_run",
                "description": "Read one run's full per-run report + config (model/policy/best).",
                "parameters": {"type": "object",
                               "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]}}},
            {"type": "function", "function": {
                "name": "inspect_experiment",
                "description": "Drill into ONE experiment (node) of ONE run: params, metric, code, "
                               "sweep trials. Use when a run's report isn't specific enough.",
                "parameters": {"type": "object",
                               "properties": {"run_id": {"type": "string"}, "node_id": {"type": "integer"}},
                               "required": ["run_id", "node_id"]}}},
        ]

    def execute(self, name: str, args: dict) -> str:
        try:
            if name == "list_runs":
                return "\n".join(
                    f"{b['run_id']}: model={b.get('model') or '?'} policy={b.get('policy') or '?'} "
                    f"best={_fmt_metric(b.get('best_metric'))} ({b.get('direction') or '?'}) {b.get('phase') or ''}"
                    for b in self._briefs.values()) or "(no runs)"
            if name == "read_run":
                b = self._briefs.get(str(args.get("run_id")))
                return run_brief_line(b, full=True) if b else f"(no such run in scope: {args.get('run_id')!r})"
            if name == "inspect_experiment":
                run_id = str(args.get("run_id"))
                if run_id not in self._briefs:
                    return f"(no such run in scope: {args.get('run_id')!r})"
                if not self._drill:
                    return "(deep experiment access unavailable here)"
                return self._drill(run_id, int(args.get("node_id")))
            return f"(unknown tool: {name})"
        except Exception:  # noqa: BLE001 - model/tool payloads must never enter persisted reports
            return "(tool request invalid)"

    def bind_state(self, *a, **k) -> None:  # drive_tool_loop may call this; cross-run tools are stateless
        pass


def generate_scope_report(scope: dict, briefs: list, client, *, parser: str = "tool_call",
                          drill: Optional[Callable[[str, int], str]] = None,
                          max_turns: int = 0, time_budget_s: float = 0.0) -> dict:
    """Synthesize a cross-run report. `scope` = {type,id,label}; `briefs` = per-run dicts (run_id,
    label, task_id, goal, direction, model, policy, best_metric, phase, nodes, report). `drill(run_id,
    node_id) -> str` optionally exposes deep experiment access. Returns a content dict; never raises."""
    label = scope.get("label") or f"{scope.get('type')}:{scope.get('id')}"
    if not briefs:
        return _AggReport(headline=f"No runs in {label}", verdict="nothing to summarize yet").model_dump()
    if client is None:
        return _deterministic(label, briefs)
    try:
        from looplab.agents.agent import drive_tool_loop
        digest = build_digest(label, briefs)
        sys_prompt = (
            "You are a principal ML researcher writing a CROSS-RUN report over a portfolio of "
            f"autonomous runs ({label}). Synthesize the WHOLE set, not any single run: which approach / "
            "model / policy / features won and why, what recurred in the winners, which dead ends to "
            "avoid, the standout runs (ranked, with their metric), and the most promising next "
            "directions for this task. Ground every claim in the runs. The digest below has each run's "
            "own report; call read_run / inspect_experiment ONLY when you need a detail it doesn't show. "
            "Then call emit_report exactly once. Be specific and terse.")
        emit_spec = {"type": "function", "function": {
            "name": "emit_report", "description": "Emit the final cross-run report.",
            "parameters": _AggReport.model_json_schema()}}
        messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": digest}]
        box: dict = {}

        def _fin(args):
            try:
                box["r"] = _AggReport(**{k: v for k, v in (args or {}).items()
                                         if k in _AggReport.model_fields}).model_dump()
            except Exception:  # noqa: BLE001 - malformed emit -> fall back to the metrics rollup
                box["r"] = _deterministic(label, briefs)
            return box["r"]

        def _force(_messages):
            """The tool loop exhausted without an emit (a weaker model may keep calling tools or never
            emit). Force ONE structured synthesis over the digest — it already carries every run's
            report, so this still yields a real agent-authored report rather than the metrics rollup."""
            try:
                from looplab.core.parse import parse_structured
                r = parse_structured(client, messages, _AggReport, parser)
                return r.model_dump() if hasattr(r, "model_dump") else None
            except Exception:  # noqa: BLE001 - no synthesis either -> deterministic below
                return None

        result = drive_tool_loop(client, _CrossRunTools(briefs, drill), messages, emit_spec,
                                 max_turns=max_turns, time_budget_s=time_budget_s,
                                 finalize=_fin, fallback=_force)
        # Prefer a SUBSTANTIVE agent report; a blank/all-default emit (or empty forced synthesis) drops
        # through to the honest metrics rollup rather than persisting an empty report.
        for cand in (result, box.get("r")):
            if _has_content(cand):
                return cand
        return _deterministic(label, briefs)
    except Exception:  # noqa: BLE001 - any model/loop failure -> deterministic, still useful
        return _deterministic(label, briefs)
