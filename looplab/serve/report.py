"""Run-report writer: an agent-authored, conclusion-first summary of a run that grows as the search
proceeds. Mirrors the Deep-Research stage (`deep_research.py`) but is a pure synthesis step (no
external tools): it reads the whole `RunState` — champion, improvement story, trust caveats, themes,
the latest research memo — and emits a structured, conclusion-first report.

Recorded as an audit-only `report_generated` event (folded into `RunState.report`, latest wins),
NEVER into the search DAG, so best-selection/policies are untouched. Degrades gracefully: any
transport/parse failure (or no model) yields a minimal report rather than crashing the run. The UI
always renders the deterministic analysis from the node set; this narrative layers on top.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from looplab.events.digest import experiments_digest, node_metric
from looplab.core.models import NodeStatus, RunState


class _ReportOut(BaseModel):
    """Structured, conclusion-first report the LLM fills (validated, then stored as state.report)."""
    headline: str = ""                                   # one-sentence bottom line (the takeaway)
    verdict: str = ""                                    # short paragraph: improved? robust? trustworthy?
    champion_summary: str = ""                           # what the winning solution is, in plain words
    what_worked: list[str] = Field(default_factory=list)
    learnings: list[str] = Field(default_factory=list)
    what_didnt: list[str] = Field(default_factory=list)
    next_directions: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)     # trust caveats stated plainly


_SYSTEM = (
    "You are a senior ML researcher writing the RUN REPORT for an automated experiment loop, read by "
    "the human who launched it. Lead with the conclusion. Be concrete and grounded ONLY in the "
    "results given — never invent numbers. Produce: a one-sentence `headline` (the single most "
    "important takeaway), a short `verdict` paragraph (did the metric improve and by how much, is the "
    "best result robust across seeds, is it trustworthy or are there red flags), a plain-words "
    "`champion_summary`, and the short lists `what_worked`, `learnings`, `what_didnt`, "
    "`next_directions`, and `caveats` (state any reward-hack / leakage / drift / single-seed / "
    "infeasibility flags plainly). Keep every list item to one short line."
)


def _report_context(state: RunState) -> str:
    """A compact, conclusion-grade brief of the whole run for the report prompt: status, champion +
    robustness, the improvement story, trust flags, the latest research conclusion, and the
    strongest/weakest experiments (via the shared digest)."""
    direction = state.direction
    best = state.best()
    n_fail = sum(1 for n in state.nodes.values() if n.status is NodeStatus.failed)
    dir_note = "lower is better" if direction == "min" else "higher is better"
    lines = [
        f"Goal: {state.goal or state.task_id}",
        f"Direction: {direction} ({dir_note})",
        f"Status: {'finished' if state.finished else 'running'}"
        + (f" ({state.stop_reason})" if state.stop_reason else ""),
        f"Nodes: {len(state.nodes)} — {len(state.evaluated_nodes())} evaluated, {n_fail} failed.",
    ]
    if best is not None:
        m = node_metric(best)
        rob = ""
        if best.confirmed_mean is not None:
            rob = (f", confirmed {best.confirmed_mean:.4g} ±{(best.confirmed_std or 0.0):.2g} "
                   f"over {best.confirmed_seeds or 0} seed(s)")
        theme = f", {best.idea.theme}" if best.idea.theme else ""
        lines.append(f"Champion: #{best.id} metric={_g(m)} ({best.operator}{theme}){rob}; "
                     f"params={best.idea.params}")
        feas = sorted(state.feasible_nodes(), key=lambda n: n.id)
        if feas:
            base = node_metric(feas[0])
            if base is not None and m is not None:
                lines.append(f"Improvement: baseline #{feas[0].id} {_g(base)} → best {_g(m)} "
                             f"(Δ {m - base:+.4g}).")
    else:
        lines.append("Champion: none yet (no feasible evaluated node).")
    # Trust flags — the conclusion must not bury these.
    flags: list[str] = []
    if best is not None and any(h.get("node_id") == best.id for h in state.reward_hacks):
        flags.append("the champion is flagged as a POSSIBLE reward-hack")
    elif state.reward_hacks:
        flags.append(f"{len(state.reward_hacks)} node(s) flagged as possible reward-hacks")
    if state.leakage and state.leakage.get("leak"):
        flags.append("a data-leakage scan flagged this run")
    if state.drifts:
        flags.append(f"{len(state.drifts)} metric-drift divergence(s) caught")
    infeasible = [n for n in state.evaluated_nodes() if not n.feasible]
    if infeasible:
        flags.append(f"{len(infeasible)} evaluated node(s) violated a constraint (excluded from best)")
    if best is not None and best.confirmed_mean is None:
        flags.append("the champion is single-seed (not multi-seed confirmed)")
    if flags:
        lines.append("Trust flags: " + "; ".join(flags) + ".")
    if state.research:
        memo = state.research[-1]
        if isinstance(memo, dict) and memo.get("summary"):
            lines.append("Latest deep-research conclusion: " + str(memo["summary"])[:400])
    dig = experiments_digest(state, top_k=6, worst_n=3)
    if dig:
        lines.append(dig)
    return "\n".join(lines)


def _g(v: Optional[float]) -> str:
    return "?" if v is None else f"{v:.4g}"


def _report_tools(state: RunState):
    """Read-only run-introspection tools so the report is GROUNDED by reading the real experiments
    (read_experiment / read_code / read_logs / list_experiments) instead of synthesizing blind from the
    aggregate summary in the prompt. None on any failure => plain parse_structured (old behaviour)."""
    try:
        from looplab.tools.run_tools import RunTools
        from looplab.agents.agent import CompositeTools
        rt = RunTools()
        rt.bind_state(state, None)
        return CompositeTools([rt])
    except Exception:  # noqa: BLE001 — grounding is best-effort; degrade to the non-agentic path
        return None


def generate_report(state: RunState, client, *, parser: str = "tool_call", trigger: str = "") -> dict:
    """Synthesize one conclusion-first report dict from the run state. Best-effort: a transport/parse
    failure (or no usable model) returns a minimal report rather than raising — the caller records it
    as a `report_generated` event regardless, so the UI always has the deterministic analysis."""
    from looplab.core.parse import parse_structured
    from looplab.agents.agent import agentic_struct
    try:
        # Build the context INSIDE the try too — a malformed state must degrade to a minimal report,
        # not propagate out of the (un-try'd) _write_report and kill the run.
        messages = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _report_context(state) + "\n\nWrite the run report now."},
        ]
        # AGENTIC: the model MAY first read the real experiments (RunTools) to ground the report,
        # then emit the structured _ReportOut. Degrades to plain parse_structured when tools/loop
        # yield nothing (or no client), preserving the offline minimal-report contract below.
        out = agentic_struct(client, _report_tools(state), messages, _ReportOut,
                             parser=parser, loop_opts={"max_turns": 15},
                             fallback=lambda m: parse_structured(client, m, _ReportOut, parser))
        content = out.model_dump(mode="json")
    except Exception as e:  # noqa: BLE001 — report is best-effort; never crash the run
        content = _ReportOut(headline="(report unavailable)",
                             verdict=f"(report generation failed: {e})").model_dump(mode="json")
    content["at_node"] = len(state.nodes)
    content["trigger"] = trigger
    return content


class ReportWriter:
    """Thin wrapper holding the LLM client + parser so the engine/server can call `.generate(state)`
    symmetrically with the DeepResearcher."""

    def __init__(self, client, parser: str = "tool_call"):
        self.client = client
        self.parser = parser

    def generate(self, state: RunState, trigger: str = "") -> dict:
        return generate_report(state, self.client, parser=self.parser, trigger=trigger)


def make_report_writer(settings, *, client=None) -> Optional[ReportWriter]:
    """Build a ReportWriter when an LLM client is wired; None in toy/offline mode (the engine then
    never runs the cadence and the UI shows the deterministic report only)."""
    if client is None:
        return None
    return ReportWriter(client, parser=getattr(settings, "llm_parser", "tool_call"))
