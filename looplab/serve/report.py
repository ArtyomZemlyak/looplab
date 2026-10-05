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
from looplab.core.output_language import current_output_language
from looplab.engine.champion_caveats import (CHAMPION_CAVEAT_MERGED_COORDINATES,
                                             CHAMPION_CAVEAT_MIXED_COMPARABILITY,
                                             CHAMPION_CAVEAT_PARAMS_OVERRIDDEN,
                                             CHAMPION_CAVEAT_RETARGETED_OBJECTIVE,
                                             CHAMPION_CAVEAT_SALVAGED,
                                             CHAMPION_CAVEAT_TRUST_FLAGGED,
                                             champion_metric_caveats)
from looplab.events.digest import (concept_rollup, experiments_digest, metric_scored_invalid, node_metric,
                                   node_theme)
from looplab.core.models import NodeStatus, RunState, activation_unverified


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
    "infeasibility flags plainly). Keep every list item to one short line. "
    "Claim improvement only for the eligible same-ruler primary-score comparisons below; "
    "never infer it from the first/selected numbers or confirmation means. Name node IDs and "
    "limits. Failed evaluations show execution problems, not that an idea was ineffective. "
    "Put untested deep-research proposals in next_directions, never in learnings or what_worked. "
    "Concept association alone is not an ablation or a causal effect. Say when evidence is unknown."
)


def _report_context(state: RunState) -> str:
    """A compact, conclusion-grade brief of the whole run for the report prompt: status, champion +
    robustness, eligible parent comparisons, trust flags, hypothesis-search proposals, and the
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
    objective = getattr(state, "objective_key", None)
    lines = [
        f"Goal: {state.goal or state.task_id}",
        f"Direction: {direction} ({dir_note})",
        # doc 68 68.2: only under an operator retarget — every number below is then that metric's,
        # and a report that did not say so published it as the task's own (critic 2026-09-27).
        *([f"Objective: the declared extra metric {objective!r}, which an operator retarget made the "
           "run's objective — every metric below is its value, not the task's own metric."]
          if objective else []),
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
                lines.append(f"Recorded first/selected values: #{feas[0].id} {_g(base)} → "
                             f"#{best.id} {_g(m)}. These selection values can include confirmation "
                             "means; they alone do not establish improvement or a causal effect.")
    else:
        lines.append("Champion: none yet (no feasible evaluated node).")
    lines.extend(_parent_score_evidence(state))
    concepts = concept_rollup(state)
    if concepts:
        lines.append("Concept contrasts: matched complete other-concept sets and evaluation rulers; "
                     "observational with/without evidence, not causal ablations. Positive gain "
                     "means better in the run direction. Missing estimates do not mean no effect.")
        for cid, row in sorted(concepts.items(), key=lambda item: (-item[1]["count"], item[0]))[:6]:
            effect = row["effect"]
            lines.append(f"Concept {cid}: status={effect['status']}, reason={effect['reason']}, "
                         f"gain={_g(effect['estimate'])}, pairs={effect['n_pairs']}, "
                         f"contexts={effect['n_contexts']}, warnings={effect['has_advisory_warnings']}; "
                         f"pair preview={effect['pairs'][:2]}")
        if len(concepts) > 6:
            lines.append(f"{len(concepts) - 6} other concept summaries omitted; inspect concept tools.")
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
    # AN UNVERIFIED ACTIVATION (the graded check's WARN, minionerec-lora-v1 node 2, 2026-10-01): the
    # node's metric STANDS — it is feasible, and under `activation_unverified_gate=gate` only barred
    # from best and breeding — but nothing proved its declared change took effect: a config-only
    # change whose markers no code prints. Said beside the exclusions because it is the same kind of
    # fact about a number the operator is about to trust.
    unverified = [n for n in state.evaluated_nodes() if activation_unverified(n)]
    if unverified:
        champion = best is not None and any(n.id == best.id for n in unverified)
        flags.append(f"{len(unverified)} evaluated node(s) settled with their ACTIVATION "
                     "UNVERIFIED — a config-only change whose declared markers no code prints, so "
                     "nothing proved the change took effect"
                     + (" (the champion among them)" if champion else ""))
    if best is not None and best.confirmed_mean is None:
        flags.append("the champion is single-seed (not multi-seed confirmed)")
    # THE CHAMPION'S OWN CAVEATS (doc 69 69.16): the engine's receipt on the number this report
    # leads with (`engine/champion_caveats.py::champion_metric_caveats`, the one derivation the run
    # row, the reviewer bundle and the git export read). Unread here, a real run's report called
    # every number "directly comparable" beside `mixed_comparability`. The retarget is the
    # Objective line above.
    flags += _champion_caveat_clauses(state, objective_line=True)
    if flags:
        lines.append("Trust flags: " + "; ".join(flags) + ".")
    if state.research:
        memo = state.research[-1]
        if isinstance(memo, dict) and memo.get("summary"):
            lines.append("Latest hypothesis-search memo (untested proposals, not measured learnings): "
                         + str(memo["summary"])[:400])
    dig = experiments_digest(state, top_k=6, worst_n=3)
    if dig:
        lines.append(dig)
    return "\n".join(lines)


def _parent_score_evidence(state: RunState) -> list[str]:
    """Use the completion receipt's authority, including creation-bound parent attempts."""
    from collections import Counter

    from looplab.engine.comparability import comparability_status, record_of
    from looplab.events.replay import flagged_node_ids
    from looplab.serve.node_comparison import completion_score_comparison
    from looplab.serve.run_result_summary import current_trust_signals

    flagged = set(flagged_node_ids(state))
    advisory = set(current_trust_signals(state)) - flagged
    counts = Counter()
    rows = []
    for node in sorted(state.nodes.values(), key=lambda n: n.id):
        if node.tombstoned or node.status != NodeStatus.evaluated:
            continue
        parents = []
        for pid in node.parent_ids[:8]:
            parent = state.nodes.get(pid)
            if (parent is not None and not parent.tombstoned
                    and parent.status == NodeStatus.evaluated and pid not in state.aborted_nodes
                    and node.parent_generations.get(str(pid)) == parent.attempt):
                parents.append({"node_id": pid, "attempt": parent.attempt,
                                "comparability": comparability_status(record_of(node), record_of(parent))})
        status = completion_score_comparison(node, parents, state, flagged)["status"]
        counts[status] += 1
        if status == "same":
            parent = state.nodes[parents[0]["node_id"]]
            delta = node.metric - parent.metric
            gain = delta if state.direction == "max" else -delta
            outcome = "better" if gain > 0 else "worse" if gain < 0 else "unchanged"
            warning = " (trust advisory remains)" if {node.id, parent.id} & advisory else ""
            rows.append(f"#{parent.id}/attempt {parent.attempt} → #{node.id}/attempt {node.attempt}: "
                        f"primary scores {_g(parent.metric)} → {_g(node.metric)}, "
                        f"direction-normalized gain {gain:+.4g}, {outcome}{warning}")
    lines = ["Primary-score parent comparison coverage: " + str(dict(sorted(counts.items()))) + "."]
    if rows:
        lines.append(f"Eligible same-ruler primary-score comparisons (latest {min(12, len(rows))} "
                     f"of {len(rows)}; observational, not isolated ablations):")
        lines.extend(rows[-12:])
    else:
        lines.append("No eligible same-ruler primary-score comparison establishes improvement.")
    return lines


# One trust-flag clause per champion caveat, on the meaning `ui/src/runIndex.js::
# bestMetricCaveatNotice` gives the same slug.
_CHAMPION_CAVEAT_FLAGS = {
    CHAMPION_CAVEAT_SALVAGED: (
        "the champion's metric was NOT measured: its evaluation failed and the run's own declared "
        "reader recovered the number, which `metric_salvage` lets compete"),
    CHAMPION_CAVEAT_TRUST_FLAGGED: (
        "the champion carries a high-precision reward-hack or leakage signal this run's trust_gate "
        "did not enforce"),
    # Either source the engine raises it from (critic 2026-09-27, driven): the committed code
    # (`repair_verify.py::declared_param_overrides`) or the configuration the evaluation resolved
    # (`runtime/applied_params.py`) — a clause naming the code alone was false for the second.
    CHAMPION_CAVEAT_PARAMS_OVERRIDDEN: (
        "the champion's own code, or the configuration its evaluation resolved, assigns a different "
        "value to a parameter its experiment declares (or two of its own carriers disagree), so its "
        "declared params are not the configuration that produced the number — the metric itself "
        "was measured normally; what is in question is what it measures"),
    CHAMPION_CAVEAT_MIXED_COMPARABILITY: (
        "this run's nodes were NOT all measured against the same evaluation (their comparability "
        "keys, source trees or evaluation protocols provably differ): the champion won a mixed "
        "field, so the numbers are not directly comparable"),
    CHAMPION_CAVEAT_MERGED_COORDINATES: (
        "the champion is a mean-merge: its params are the average of its parents' and no "
        "experiment ever trained that configuration"),
}


def _champion_caveat_clauses(state: RunState, *, objective_line: bool) -> list[str]:
    """One clause per champion caveat (`engine/champion_caveats.py::champion_metric_caveats`); a slug
    this build words nothing for is named as it is, never dropped. The retarget is said by the
    Objective line where the text has one (`objective_line`), and here where it has none."""
    out: list[str] = []
    for slug in champion_metric_caveats(state):
        if slug == CHAMPION_CAVEAT_RETARGETED_OBJECTIVE:
            if not objective_line:
                out.append(f"the champion's metric is the declared extra metric "
                           f"{getattr(state, 'objective_key', None)!r}, which an operator retarget "
                           "made the objective — not the task's own metric")
            continue
        out.append(_CHAMPION_CAVEAT_FLAGS.get(
            slug, f"the engine records the champion caveat {slug!r}"))
    return out


def _g(v: Optional[float]) -> str:
    return "?" if v is None else f"{v:.4g}"


def _report_tools(state: RunState):
    """Read-only run-introspection tools so the report is GROUNDED by reading the real experiments
    (read_experiment / read_code / read_logs / list_experiments) instead of synthesizing blind from the
    aggregate summary in the prompt. None on any failure => plain parse_structured (old behaviour)."""
    from looplab.tools.run_tools import readonly_run_tools
    return readonly_run_tools(state)


def generate_report(state: RunState, client, *, parser: str = "tool_call", trigger: str = "",
                    raise_on_failure: bool = False, evidence_envelope: bool = False,
                    output_language: str = "auto") -> dict:
    """Synthesize one conclusion-first report dict from the run state.

    Engine-owned cadence/finalization calls keep the best-effort default and receive deterministic
    fallback content. A manual paid refresh passes ``raise_on_failure=True`` so provider failure can
    be recorded as a distinct durable terminal without replacing the last known-good report.

    ``evidence_envelope`` fences what the report's run tools return — the candidates' own code, logs
    and output — between `UNTRUSTED_RUN_EVIDENCE` and its closing marker (`core/evidence.py`; review
    2026-09-22, TAT-02). OFF here, as at every constructor, because it changes a prompt: both
    callers pass the RUN's `Settings.evidence_envelope` through `envelope_enabled` — the engine's
    writer via `make_report_writer`, the manual refresh via the run's own snapshot — so a run
    launched before the field keeps its historical report request byte for byte.
    """
    from looplab.core.evidence import fence_kwargs
    from looplab.core.parse import parse_structured
    from looplab.agents.agent import agentic_struct
    language = current_output_language(client, output_language)
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
                             fallback=lambda m: parse_structured(client, m, _ReportOut, parser),
                             **fence_kwargs(evidence_envelope))
        content = out.model_dump(mode="json")
    except Exception as e:  # noqa: BLE001 — report is best-effort; never crash the run
        if raise_on_failure:
            raise
        from looplab.serve.assistant import safe_provider_failure
        failure = safe_provider_failure(e)
        try:
            content = _deterministic_report(state, failure["message"], output_language=language)
        except Exception:  # noqa: BLE001 — a malformed state must still yield a report
            content = _ReportOut(
                headline="(отчёт недоступен)" if language == "ru" else "(report unavailable)",
                verdict="(не удалось составить отчёт: ошибка модели)" if language == "ru"
                else f"(report generation failed: {failure['message']})").model_dump(mode="json")
    content["at_node"] = len(state.nodes)
    content["trigger"] = trigger
    return sanitize_report_payload(content)



def _deterministic_report(state: "RunState", message: str, *, output_language="auto") -> dict:
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
        headline = ("No eligible champion; written without the model." if evaluated
                    else "No node was evaluated; written without the model.")
        champion = ""
    summary = (f"{len(state.nodes)} node(s) — {evaluated} evaluated, {n_fail} failed"
               + (f", {n_invalid} scored but INVALID" if n_invalid else "")
               + f". Stop reason: {state.stop_reason or 'not recorded'}.")
    out = _ReportOut(
        headline=headline,
        champion_summary=champion,
        verdict=f"(report generation failed: {message})",
        # The champion's own caveats, as the paid report's context carries them (doc 69 69.16): an
        # empty list here read as "no caveats" beside a `mixed_comparability` champion, on exactly
        # the runs that end on the budget ceiling (critic 2026-09-27, driven).
        caveats=_champion_caveat_clauses(state, objective_line=False)[:32],
    ).model_dump(mode="json")
    # `summary` is not a field of `_ReportOut` — it is the LEGACY single-field shape that
    # `sanitize_report_payload` still reads and that older logs and finalization receipts render.
    # Set it directly, because it is where the counts belong and because the verdict cannot carry
    # them: `advisory_payloads._report_verdict` collapses anything opening with the failure marker
    # down to its canonical phrase, by design, to keep a raw provider exception out of storage.
    out["summary"] = summary
    if output_language == "ru":
        out.update(
            headline=(f"Лучший узел — #{best.id}, метрика={_g(node_metric(best))} "
                      f"({best.operator}); описание составлено без модели." if best is not None
                      else ("Нет допустимого лучшего узла; описание составлено без модели." if evaluated
                            else "Нет оценённых узлов; описание составлено без модели.")),
            champion_summary=(f"Узел #{best.id}, оператор {best.operator}, params={best.idea.params}, "
                              f"метрика={_g(node_metric(best))} ({state.direction}: "
                              f"{'меньше' if state.direction == 'min' else 'больше'} — лучше)."
                              if best is not None else ""),
            verdict="(не удалось составить отчёт: ошибка модели)",
            summary=(f"Узлов: {len(state.nodes)}; оценено: {evaluated}; ошибок: {n_fail}"
                     + (f"; недействительных оценок: {n_invalid}" if n_invalid else "")
                     + f". Причина остановки: {state.stop_reason or 'не записана'}."),
            caveats=_russian_champion_caveats(state),
        )
    return out


def _russian_champion_caveats(state):
    """Authored caveats for a NEW offline report; replay and measured data stay raw."""
    labels = {
        CHAMPION_CAVEAT_SALVAGED: "Метрика лучшего узла восстановлена после ошибки оценки; "
        "защищённая процедура оценки её не измерила. metric_salvage разрешает её отбор.",
        CHAMPION_CAVEAT_TRUST_FLAGGED: "У лучшего узла есть сигнал обхода оценки или утечки; "
        "trust_gate этого запуска не исключил его из отбора.",
        CHAMPION_CAVEAT_PARAMS_OVERRIDDEN: "Код или конфигурация лучшего узла переопределяет "
        "заявленные параметры, либо источники параметров расходятся. Число измерено, но "
        "не подтверждает именно заявленную конфигурацию.",
        CHAMPION_CAVEAT_MIXED_COMPARABILITY: "Узлы оценивались по разным исходным деревьям "
        "или протоколам; их числа нельзя считать непосредственно сопоставимыми.",
        CHAMPION_CAVEAT_MERGED_COORDINATES: "Параметры лучшего узла усреднены по родителям; "
        "ни один эксперимент не обучал эту конфигурацию.",
        CHAMPION_CAVEAT_RETARGETED_OBJECTIVE: f"Цель оценки изменена оператором на "
        f"дополнительную метрику {getattr(state, 'objective_key', None)!r}; это не исходная "
        "метрика задачи.",
    }
    return [labels.get(slug, f"Движок записал оговорку: {slug!r}")
            for slug in champion_metric_caveats(state)][:32]


class ReportWriter:
    """Thin wrapper holding the LLM client + parser so the engine/server can call `.generate(state)`
    symmetrically with the DeepResearcher."""

    def __init__(self, client, parser: str = "tool_call", evidence_envelope: bool = False):
        self.client = client
        self.parser = parser
        # The untrusted-evidence fence on the report's run tools (`generate_report`); OFF at the
        # constructor like every prompt flag, filled by `make_report_writer` from the run's Settings.
        self.evidence_envelope = bool(evidence_envelope)

    def generate(self, state: RunState, trigger: str = "") -> dict:
        return generate_report(state, self.client, parser=self.parser, trigger=trigger,
                               evidence_envelope=self.evidence_envelope)


def make_report_writer(settings, *, client=None) -> Optional[ReportWriter]:
    """Build a ReportWriter when an LLM client is wired; None in toy/offline mode (the engine then
    never runs the cadence and the UI shows the deterministic report only)."""
    if client is None:
        return None
    from looplab.core.evidence import envelope_enabled
    return ReportWriter(client, parser=getattr(settings, "llm_parser", "tool_call"),
                        evidence_envelope=envelope_enabled(settings))
