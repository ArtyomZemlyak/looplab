"""Run-report writer: an agent-authored, conclusion-first summary of a run that grows as the search
proceeds. Mirrors the Deep-Research stage (`deep_research.py`) but is a pure synthesis step (no
external tools): it reads the whole `RunState` — champion, improvement story, trust caveats, themes,
the latest research memo — and emits a structured, conclusion-first report.

Recorded as a selection-neutral `report_generated` event (folded into `RunState.report`, latest wins).
It never enters the search DAG or changes best-selection/policies, although the durable receipt gates
the report's own refresh cadence. Degrades gracefully: any
transport/parse failure (or no model) yields a minimal report rather than crashing the run. The UI
always renders the deterministic analysis from the node set; this narrative layers on top.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from looplab.core.advisory_payloads import sanitize_report_payload
from looplab.events.digest import (experiments_digest, metric_scored_invalid, node_metric,
                                   node_theme)
from looplab.core.models import NodeStatus, RunState


class _ReportOut(BaseModel):
    """Structured, conclusion-first report the LLM fills (validated, then stored as state.report)."""
    headline: str = Field(default="", max_length=800)     # one-sentence bottom line
    verdict: str = Field(default="", max_length=4_000)    # improved? robust? trustworthy?
    champion_summary: str = Field(default="", max_length=4_000)
    what_worked: list[str] = Field(default_factory=list, max_length=32)
    learnings: list[str] = Field(default_factory=list, max_length=32)
    what_didnt: list[str] = Field(default_factory=list, max_length=32)
    next_directions: list[str] = Field(default_factory=list, max_length=32)
    caveats: list[str] = Field(default_factory=list, max_length=32)


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
    # The second half of that count, for the reason `digest.metric_scored_invalid` states: a node the
    # eval REFUSED to score is `evaluated` with a real 0.0 and was folded into the healthy total, so
    # this line told the report writer a run of nothing-but-invalid solvers had zero failures. Same
    # omit-when-zero rule as the working set's headline.
    n_invalid = sum(1 for n in state.nodes.values() if metric_scored_invalid(n))
    dir_note = "lower is better" if direction == "min" else "higher is better"
    lines = [
        f"Goal: {state.goal or state.task_id}",
        f"Direction: {direction} ({dir_note})",
        f"Status: {'finished' if state.finished else 'running'}"
        + (f" ({state.stop_reason})" if state.stop_reason else ""),
        f"Nodes: {len(state.nodes)} — {len(state.evaluated_nodes())} evaluated, {n_fail} failed"
        + (f", {n_invalid} scored but INVALID (the eval refused to time them)." if n_invalid
           else "."),
    ]
    if best is not None:
        m = node_metric(best)
        rob = ""
        if best.confirmed_mean is not None:
            rob = (f", confirmed {best.confirmed_mean:.4g} ±{(best.confirmed_std or 0.0):.2g} "
                   f"over {best.confirmed_seeds or 0} seed(s)")
        theme_label = node_theme(best, state)   # primary canonical axis (folded concepts, else legacy theme/first authored)
        theme = f", {theme_label}" if theme_label else ""
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
    # WHY a node is excluded is two different facts, and the report is the artifact an operator reads
    # when they cannot ask anyone. A node whose metric was SALVAGED
    # (`engine/metric_salvage.py`) carries a `metric_salvaged` row in `violations` — that is what
    # makes `feasible` False and keeps an unmeasured number out of champion selection — but it
    # breached no bound and its experiment did not misbehave. Reporting it as "violated a constraint"
    # accuses the run of something that did not happen, and it is the accusation the operator acts
    # on. `ui/src/trustSemantics.js` draws exactly this distinction in the browser; this is the
    # server half of the same rule, and the one the generated run report reads.
    # A node can be BOTH (a salvaged metric that then failed a real bound — the constraint gates run
    # on a salvaged metric too), so these are two overlapping counts read off the rows, not a split.
    infeasible = [n for n in state.evaluated_nodes() if not n.feasible]
    salvaged = [n for n in infeasible if (n.metric_provenance or {}).get("salvaged")]
    breached = [n for n in infeasible
                if any((v or {}).get("name") != "metric_salvaged" for v in (n.violations or []))
                or not n.violations]
    if breached:
        flags.append(f"{len(breached)} evaluated node(s) violated a constraint (excluded from best)")
    if salvaged:
        flags.append(f"{len(salvaged)} evaluated node(s) carry a SALVAGED metric — recovered by the "
                     "run's own declared reader from an eval that failed, not measured by the "
                     "scoring path (excluded from best)")
    # A THIRD exclusion, and it needs its own sentence for the same reason the first two do. An
    # UNBOUND metric (`runtime/metric_subject.py`, `metric_subject="require"`) rides the SAME
    # `metric_salvaged` row on purpose — inventing a second exclusion vocabulary would silently cost
    # it every existing reader — but it is neither of the two facts above: the eval SUCCEEDED and the
    # number was measured by the scoring path; what is missing is any record of what the number is
    # ABOUT. Without this branch such a node falls out of both lists (its provenance is not
    # `salvaged`, and its only row IS named `metric_salvaged`) and the report says nothing at all
    # about a node it has excluded — which is the counted-but-invisible failure this whole mechanism
    # exists to end.
    unbound = [n for n in infeasible
               if any((v or {}).get("name") == "metric_salvaged"
                      and ((v or {}).get("salvage") or {}).get("condition") == "metric_subject_unbound"
                      for v in (n.violations or []))]
    if unbound:
        flags.append(f"{len(unbound)} evaluated node(s) recorded a metric that is bound to NO "
                     "subject — nothing says which artifact the number is about, so it cannot be "
                     "checked and is excluded from best. Declare `eval.metric.subject`")
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
    from looplab.tools.run_tools import readonly_run_tools
    return readonly_run_tools(state)


def generate_report(state: RunState, client, *, parser: str = "tool_call", trigger: str = "",
                    raise_on_failure: bool = False) -> dict:
    """Synthesize one conclusion-first report dict from the run state.

    Engine-owned cadence/finalization calls keep the best-effort default and receive deterministic
    fallback content. A manual paid refresh passes ``raise_on_failure=True`` so provider failure can
    be recorded as a distinct durable terminal without replacing the last known-good report.
    """
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
        if raise_on_failure:
            raise
        from looplab.serve.assistant import safe_provider_failure
        failure = safe_provider_failure(e)
        try:
            content = _deterministic_report(state, failure["message"])
        except Exception:  # noqa: BLE001 — a malformed state must still yield a report
            content = _ReportOut(
                headline="(report unavailable)",
                verdict=f"(report generation failed: {failure['message']})").model_dump(mode="json")
    content["at_node"] = len(state.nodes)
    content["trigger"] = trigger
    return sanitize_report_payload(content)



def _deterministic_report(state: "RunState", message: str) -> dict:
    """The run's own facts when the writer could not be paid for — not an empty placeholder.

    EVERY run that ends on the budget ceiling loses its report, because finalization runs AFTER the
    ceiling has fired and the writer's call is refused. Measured 2026-08-27 across the probe corpus:
    three of three ceiling-terminated runs recorded `headline: "(report unavailable)"`, and the
    report is the one artefact a human reads to learn what a run found. The runs that went the whole
    distance are exactly the ones that lose it.

    Reflection already solves this the right way — `finalize.py` notes that `write_reflection_note`
    "degrades to a deterministic meta-note when the provider is exhausted, which on this path it
    usually is". The report had no such path and discarded facts `_report_context` had computed one
    frame earlier.

    The verdict still OPENS with the legacy failure marker, so `advisory_payloads._report_verdict`
    keeps collapsing a raw exception into its canonical phrase and anything watching for a failed
    report still sees one. What changes is that the other fields carry the run instead of nothing.
    """
    best = state.best()
    evaluated = len(state.evaluated_nodes())
    n_fail = sum(1 for n in state.nodes.values() if n.status is NodeStatus.failed)
    n_invalid = sum(1 for n in state.nodes.values() if metric_scored_invalid(n))
    if best is not None:
        headline = (f"#{best.id} is the champion at metric={_g(node_metric(best))} "
                    f"({best.operator}); written without the model.")
        champion = (f"Node #{best.id}, operator {best.operator}, params={best.idea.params}, "
                    f"metric={_g(node_metric(best))} ({state.direction}: "
                    f"{'lower' if state.direction == 'min' else 'higher'} is better).")
    else:
        headline = "No node was evaluated; written without the model."
        champion = ""
    summary = (f"{len(state.nodes)} node(s) — {evaluated} evaluated, {n_fail} failed"
               + (f", {n_invalid} scored but INVALID" if n_invalid else "")
               + f". Stop reason: {state.stop_reason or 'not recorded'}.")
    out = _ReportOut(
        headline=headline,
        champion_summary=champion,
        verdict=f"(report generation failed: {message})",
    ).model_dump(mode="json")
    # `summary` is not a field of `_ReportOut` — it is the LEGACY single-field shape that
    # `sanitize_report_payload` still reads and that older logs and finalization receipts render.
    # Set it directly, because it is where the counts belong and because the verdict cannot carry
    # them: `advisory_payloads._report_verdict` collapses anything opening with the failure marker
    # down to its canonical phrase, by design, to keep a raw provider exception out of storage.
    out["summary"] = summary
    return out


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
