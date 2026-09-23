"""The fold's JOURNALS family: the records the fold keeps for readers and cadence gates, never for
selection.

Split out of `events/replay.py` (review 2026-09-22, EVT-12). Every handler here writes a record that
a reader, a projection or a replay GATE (a cadence that must not re-buy a paid step) consumes, and
none of them touches a node's lifecycle, a metric or the champion:

* the durable LLM spend ledger — `_on_llm_usage` over `_on_llm_cost`'s legacy summary, with the one
  set of sanitizers (`_clean_llm_totals`, `_clean_llm_delta`, `_row_priced_calls`);
* the advisory payloads a model authored — the Deep-Research memo, the literature it read and the run
  report — re-sanitized on the way in under `_FOLD_REDACTION_ENV`, the empty environment that keeps
  the fold a function of the log (EVT-02);
* the audit/steering journals — data profile/provenance/leakage, host grading, workspace/env drift,
  the diversity archive, coverage snapshots, policy/strategy/plan decisions, rung promotions, agent
  decisions, novelty audits, cross-run priors, research attempts, lesson distillations.

`_on_report_generated` hands a FINISH report to `replay.py::_on_run_finished` through
`_FoldCtx.pending_finish_report` — the one cross-family edge, carried by the context object exactly as
before the split. Moved VERBATIM, comments included; `replay.py` re-exports the names tests read off
it and merges `HANDLERS` into its dispatch table.
"""
from __future__ import annotations

from types import MappingProxyType

from looplab.core.fitness import is_usable_metric
from looplab.core.jsonutil import bounded_int
from looplab.core.models import Event, RunState
from looplab.events.replay_ctx import _FoldCtx, event_timestamp
from looplab.events.types import (
    EV_AGENT_DECISION, EV_COVERAGE_SNAPSHOT, EV_CROSS_RUN_PRIOR, EV_DATA_LEAKAGE, EV_DATA_PROFILED,
    EV_DATA_PROVENANCE, EV_DIVERSITY_ARCHIVE, EV_ENV_CHANGED, EV_HOST_GRADING,
    EV_LESSONS_DISTILLED, EV_LESSONS_REFRESHED, EV_LITERATURE_RETRIEVED, EV_LLM_COST, EV_LLM_USAGE,
    EV_NOVELTY_GRADED, EV_NOVELTY_REJECTED, EV_PLAN, EV_POLICY_DECISION, EV_REPORT_GENERATED,
    EV_RESEARCH_ATTEMPTED, EV_RESEARCH_COMPLETED, EV_RUNG_PROMOTED, EV_STRATEGY_DECISION,
    EV_WORKSPACE_CHANGED,
)


def _on_data_profiled(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.data_profile = d.get("columns")

def _on_data_provenance(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # ALIASES the live `Event.data` (as do host_grading, leakage, archive, spec_proposed,
    # hypothesis_ranking, the `append(d)` audit journals, and Node.files/deleted). The fold runs on
    # EVERY loop iteration over the whole log, so copying each of these would be real per-iteration
    # cost for a hazard none of them carry: they are read-only PROJECTIONS — no consumer writes back
    # through them. `_on_fork`/`_on_inject_node` are the exception and DO copy, because those dicts
    # are REQUEST records the engine consumes, and `EventStore` caches parsed Events across
    # `read_all()`, so an in-place edit there would silently diverge every later fold in the process
    # from the bytes on disk. The rule for a new handler: alias a projection, copy a request.
    st.data_provenance = d   # D4: pinned dataset/asset content hashes

def _on_host_grading(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.host_grading = d      # out-of-process host-side grading active (audit; no labels)


def _on_data_leakage(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.leakage = d


def _on_workspace_changed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.workspace_changed = True                 # resume saw the source repo/data change


def _on_env_changed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.env_changed = True                       # resume saw the Python/lib environment drift (F18)

def _on_diversity_archive(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.archive = d

def _on_coverage_snapshot(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # direct-best-neutral, not behaviorally inert: Strategist/proposal cues read it.
    st.coverage_snapshots.append(d)   # at_node/projection gates dedup and reject stale snapshots

_MAX_LLM_COUNTER = (1 << 63) - 1
_MAX_LLM_COST = 1.7976931348623157e308


def _llm_counter(value) -> int:
    # The bool rejection this used to spell separately is inside `bounded_int` — `type(x) is int` is
    # False for `True`, so a hand-edited `{"tokens": true}` still folds to 0 rather than arithmeticing
    # as 1. Keeping the two-step form here was what let this site and its siblings drift into two
    # spellings of one rule (doc 25 EV-04).
    return value if bounded_int(value, 0, _MAX_LLM_COUNTER) else 0


def _llm_cost_value(value) -> float:
    if not is_usable_metric(value):
        return 0.0
    out = float(value)
    return out if out >= 0.0 else 0.0


def _clean_llm_totals(d: dict | None) -> dict:
    try:
        raw = dict(d) if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001 - a corrupt event must not poison every replay
        raw = {}
    out = dict(raw)
    out.update({
        "cost": _llm_cost_value(raw.get("cost")),
        "calls": _llm_counter(raw.get("calls")),
        # How many of `calls` the provider actually stated an amount for (`core/llm.py::
        # cost_is_reported`). Plain sanitizer only — the reader-side default for a log written
        # before this field existed belongs to `_row_priced_calls`, which still has the raw row.
        "priced_calls": _llm_counter(raw.get("priced_calls")),
        "prompt_tokens": _llm_counter(raw.get("prompt_tokens")),
        "completion_tokens": _llm_counter(raw.get("completion_tokens")),
        "total_tokens": _llm_counter(raw.get("total_tokens")),
    })
    return out


def _row_priced_calls(raw: object, clean: dict) -> int:
    """Priced-call count for ONE usage/summary row, with the pre-counter reader-side default.

    `priced_calls` is additive (invariant 5), so every log written before it existed omits it, and
    the default chosen there decides what the UI says about ~every historical run. Neither constant
    works: 0 reports runs with a real invoice as unpriced, `calls` reports the unpriced ones as
    fully priced. The row settles it itself — a nonzero `cost` on that row IS the provider having
    stated an amount, and a zero one is exactly the evidence that it did not
    (`core/llm.py::cost_is_reported`). Measured against `runs/rubert-dr-0804`, whose gateway started
    reporting prices mid-run, this recovers the true 209-priced-of-313 split from the existing log
    with no migration; `runs/rubert-dr-0805` stays 0-of-354.

    A modern row always carries the field, so this branch cannot mislabel a new run.
    """
    if isinstance(raw, dict) and "priced_calls" in raw:
        return int(clean["priced_calls"])
    # Deliberately a COPY of `core/llm.py::inferred_priced_calls` rather than an import of it: that
    # module pulls in the openai/httpx transport (measured 0.5 s, ~4x this module's whole import) and
    # `fold` is on every state read. The rule is one comparison and its rationale lives at the shared
    # definition — change both together, and prefer the import if that weight ever goes away.
    return int(clean["calls"]) if float(clean["cost"]) > 0.0 else 0


def _on_llm_cost(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    if not ctx.llm_usage_seen:
        # Compatibility base: latest legacy summary before the new ledger. Once a usage delta is
        # present, later summaries are derived snapshots and may not overwrite durable totals.
        st.llm_cost = _clean_llm_totals(d)
        st.llm_cost["priced_calls"] = _row_priced_calls(d, st.llm_cost)


_LLM_LEDGER_COUNTERS = ("calls", "priced_calls", "prompt_tokens", "completion_tokens",
                        "total_tokens")


def _clean_llm_delta(d: dict) -> dict:
    """The six ledger columns ONE usage row contributes — `_clean_llm_totals` minus its copy of
    every other payload key, which a delta never adds to the ledger — plus the row's own
    `_row_priced_calls` default. Same sanitizers, so the same numbers."""
    clean = {"cost": _llm_cost_value(d.get("cost"))}
    for key in _LLM_LEDGER_COUNTERS:
        clean[key] = _llm_counter(d.get(key))
    clean["priced_calls"] = _row_priced_calls(d, clean)
    return clean


def _on_llm_usage(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    usage_id = d.get("usage_id")
    if isinstance(usage_id, str) and usage_id:
        if usage_id in ctx.llm_usage_ids:
            ctx.llm_usage_seen = True
            return
        ctx.llm_usage_ids.add(usage_id)
    # THE LEDGER IS CLEANED ONCE, NOT ON EVERY ROW (review 2026-09-22, EVT-04a). Each row used to
    # re-run `_clean_llm_totals` over the WHOLE accumulated ledger and over a full copy of its own
    # payload, on the most frequent event of an LLM run, folded ~14x per node: measured 6.5 us a
    # row for this handler alone, 3.3 us after. What
    # this handler writes is already clean (every column below is a sanitized sum) and
    # `_clean_llm_totals` is the identity on a clean ledger, so only the FIRST accumulation — whose
    # base is the empty default or a legacy `llm_cost` summary — needs the pass; `_on_llm_cost`
    # cannot replace the ledger afterwards (it yields once `llm_usage_seen`). Behaviour-identical:
    # `tests/test_fold_fast_paths_are_exact.py` folds against the verbatim old handler.
    base = dict(st.llm_cost) if ctx.llm_cost_clean else _clean_llm_totals(st.llm_cost)
    delta = _clean_llm_delta(d)
    base["cost"] = min(_MAX_LLM_COST, float(base["cost"]) + float(delta["cost"]))
    for key in _LLM_LEDGER_COUNTERS:
        base[key] = min(_MAX_LLM_COUNTER, int(base[key]) + int(delta[key]))
    st.llm_cost = base
    ctx.llm_usage_seen = True
    ctx.llm_cost_clean = True


def _on_policy_decision(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    _scores = {}
    _raw = d.get("scores")
    # A non-dict `scores` (a list/str/number from a corrupt or hand-edited log) has no `.items()`
    # and would raise an uncaught AttributeError that bricks the ENTIRE fold — the same corrupt-log
    # class the per-key try/except below already guards. Skip a non-dict container the same way.
    for k, v in (_raw.items() if isinstance(_raw, dict) else ()):
        try:
            _scores[int(k)] = v                 # a non-integer key (corrupt log) is skipped
        except (TypeError, ValueError):
            continue
    st.policy_scores = _scores
    st.policy_chosen = d.get("chosen")
    st.policy_reason = d.get("reason") or ""

def _on_strategy_decision(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # A7 Strategist behavioral replay state: rebuild the chosen Strategy without re-calling the LLM;
    # engine re-entry applies active_strategy before the next decision/evaluation boundary.
    st.active_strategy = d.get("strategy")
    history = {"strategy": d.get("strategy"), "at_node": d.get("at_node"),
               "ctx": d.get("ctx")}
    if d.get("developer_application") is not None:
        history["developer_application"] = d["developer_application"]
    st.strategy_history.append(history)

def _on_plan(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # doc 52 row 18: the run's plan artifact. Latest wins; the history keeps every re-cut. The row
    # is validated by its writer (`engine/plan.py::build_plan`) and read back defensively here.
    if not isinstance(d, dict) or not isinstance(d.get("phases"), list):
        return
    st.plan = dict(d)
    st.plan_history.append({"at_node": d.get("at_node"), "reason": d.get("reason"),
                            "endgame_start": d.get("endgame_start"), "reserve": d.get("reserve")})


def _on_rung_promoted(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.rungs.append({"rung": d.get("rung"), "survivors": d.get("survivors", [])})

def _on_agent_decision(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Self-driving unified agent (audit-only): records WHICH legal macro action the agent
    # chose and why. NEVER drives selection — the effect is the subsequent node_created,
    # folded as usual. Additive & non-load-bearing: an old log without it folds identically.
    st.agent_decisions.append(d)


def _on_novelty_rejected(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.novelty_events.append(d)   # E1: a near-duplicate proposal nudged off (audit)

def _on_novelty_graded(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.novelty_grades.append(d)   # D3: a graded-ALLOW (level-4/5) the flat gate would reject (audit)

def _on_cross_run_prior(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.cross_run_priors.append(d)   # §21.20 Step 2: concept tried in a SIMILAR earlier run (audit; surface)


def _on_research_attempted(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Paid-attempt receipt appended BEFORE the Deep-Research provider call. Selection-neutral: only
    # the research trigger gates read it, exactly like the memo row below. Ignore a row without a
    # usable identity — an attempt nothing can ever reconcile would strand the manual queue.
    attempt_id = d.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        return
    at_node = d.get("at_node")
    st.research_attempts.append({
        "attempt_id": attempt_id,
        "trigger": str(d.get("trigger") or ""),
        "at_node": at_node if type(at_node) is int and at_node >= 0 else None,
        "manual": bool(d.get("manual")),
    })

# THE FOLD SCREENS NO ENVIRONMENT (review 2026-09-22, EVT-02). The three advisory handlers below
# re-sanitize what their writers already sanitized, and that pass read the REPLAYING process's
# `os.environ` (`core/redact.py::redact_persisted_text` -> `redact_env_values`): one log folded to
# a different `RunState` on a box that happened to hold a matching secret — driven, a shapeless
# `MY_DB_PASSWORD` value in a memo summary and a report headline folded verbatim in one process
# and as `***REDACTED_ENV***` in the next, `model_dump_json()` unequal. Invariant 5 is that the
# fold is a function of the log. So the writers keep the identity screen (their `env` is the
# default: the process that owns the secret, at the moment the bytes become durable), and the fold
# keeps every screen that IS a function of the bytes — shapes, entropy, controls, caps — and
# passes an empty, read-only mapping for the one that is a property of the box. A log written
# before 2026-09-08 (when that screen reached the memo writer) can therefore fold a raw env value
# it always carried: the durable row holds it either way, and the fold was never the place that
# could take it back.
_FOLD_REDACTION_ENV = MappingProxyType({})


def _on_research_completed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Deep-Research memo: never re-ranks current nodes/best; later proposal context and cross-run
    # evidence may read it. `served_manual` also prevents replay from re-serving the request.
    from looplab.core.advisory_payloads import sanitize_research_memo_payload
    # old events predate D8 omission receipts. Preserve their replay shape (and unknown authority)
    # instead of manufacturing a complete receipt from an already-truncated legacy projection.
    memo = sanitize_research_memo_payload(d.get("memo") or d, add_receipts=False,
                                          env=_FOLD_REDACTION_ENV)
    st.research.append(memo)
    # THE DURABLE RESEARCH RECORD (doc 52 row 16): the latest memo's plan is the run's current
    # ResearchPlan / ProgressLedger, and every memo's exact-span evidence accrues by id. Both are
    # sanitized above and read by nothing that selects; an old row carries neither.
    if isinstance(memo.get("plan"), dict):
        st.research_plan = memo["plan"]
    for item in memo.get("evidence") or ():
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
            st.research_evidence.setdefault(item["id"], item)
    # `research_served` indexes `research_requests`: the engine only sets `served_manual` while
    # serving `research_requests[research_served]` (engine/research_cadence.py::normalized_belief_key). Counting every
    # such row unconditionally let a duplicate/orphan completion push the cursor PAST the queue, so a
    # `deep_research` request appended afterwards sat at an index the manual trigger would never reach
    # — the operator's "go think hard now" was silently dropped. Clamping to the queue is a no-op on
    # any log a sanctioned producer wrote. There is no per-request identity to compare (the memo
    # carries none), so the head clamp IS the bind here.
    if d.get("served_manual") and st.research_served < len(st.research_requests):
        st.research_served += 1
    # Close this memo's paid attempt so the trigger gates stop counting it as still outstanding.
    # Order-tolerant: the attempt row may be folded before or after this one — the engine only ever
    # asks "which attempt ids are completed", never "in what order".
    attempt_id = d.get("attempt_id")
    if isinstance(attempt_id, str) and attempt_id:
        st.research_attempts_completed.add(attempt_id)

def _on_literature_retrieved(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # The papers a Deep-Research pass read (doc 52 row 16), sanitized on the way in like the memo
    # they rode beside. Selection-neutral: nothing but the record reads `st.literature`.
    from looplab.core.advisory_payloads import sanitize_literature_items
    # The ids already folded live on the ctx, kept in step with the ONE writer of `st.literature`
    # below — rebuilding the set from the whole list on every row made the literature fold
    # quadratic in papers read (review 2026-09-22, EVT-04a; measured on a loaded box, 1,500 rows
    # carrying 6,000 papers folded in 563-976 ms before and 235-260 ms after).
    seen = ctx.literature_ids
    at_node = d.get("at_node") if type(d.get("at_node")) is int else None
    for item in sanitize_literature_items(d.get("items"), env=_FOLD_REDACTION_ENV):
        if item["id"] not in seen:
            seen.add(item["id"])
            st.literature.append({**item, "at_node": at_node})


def _on_lessons_distilled(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # M6 does not re-rank current nodes/best; at_node + pair ids are behavioral replay gates that
    # prevent paid re-distillation, while the shared lesson output can steer later proposals.
    st.lessons_distilled.append(d)

def _on_lessons_refreshed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.lessons_refreshed.append(d)   # M6 shared-store re-read cadence/replay gate

def _on_report_generated(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Agent-authored run report (selection-neutral; NEVER touches nodes/best). Latest wins; the cadence
    # and manual-refresh paths both append this, and the receipt also gates future regeneration.
    from looplab.core.advisory_payloads import sanitize_report_payload
    content = sanitize_report_payload(d.get("content") or d, env=_FOLD_REDACTION_ENV)
    # The event envelope is the publication authority. Model/provider content must not forge which
    # node-count/trigger the writer bound, nor the physical receipt that made the narrative durable.
    # Preserve inner at_node/trigger only for historical events whose outer payload omitted them.
    if "at_node" in d or "trigger" in d:
        envelope = sanitize_report_payload({
            "at_node": d.get("at_node"), "trigger": d.get("trigger"),
        }, env=_FOLD_REDACTION_ENV)
        if "at_node" in d:
            content["at_node"] = envelope["at_node"]
        if "trigger" in d:
            content["trigger"] = envelope["trigger"]
    content["published_seq"] = (e.seq if type(e.seq) is int
                                and 0 <= e.seq <= (1 << 53) - 1 else None)
    content["published_at"] = event_timestamp(e)   # the shared rule; see `event_timestamp`
    if "trigger" in d and content["trigger"] == "finish":
        # Publish only if the immediately-adjacent run_finished accepts this report's CAS chain.
        ctx.pending_finish_report = (e.seq, ctx.event_index, content)
        return
    st.report = content


# This family's rows of the fold's dispatch table. `replay.py::_HANDLERS` is assembled from every
# family's table and refuses a type two of them claim, so a journal handler is registered HERE,
# beside its body, and nowhere else.
HANDLERS = {
    EV_DATA_PROFILED: _on_data_profiled,
    EV_DATA_PROVENANCE: _on_data_provenance,
    EV_HOST_GRADING: _on_host_grading,
    EV_DATA_LEAKAGE: _on_data_leakage,
    EV_WORKSPACE_CHANGED: _on_workspace_changed,
    EV_ENV_CHANGED: _on_env_changed,
    EV_DIVERSITY_ARCHIVE: _on_diversity_archive,
    EV_COVERAGE_SNAPSHOT: _on_coverage_snapshot,
    EV_LLM_COST: _on_llm_cost,
    EV_LLM_USAGE: _on_llm_usage,
    EV_POLICY_DECISION: _on_policy_decision,
    EV_STRATEGY_DECISION: _on_strategy_decision,
    EV_PLAN: _on_plan,
    EV_RUNG_PROMOTED: _on_rung_promoted,
    EV_AGENT_DECISION: _on_agent_decision,
    EV_NOVELTY_REJECTED: _on_novelty_rejected,
    EV_NOVELTY_GRADED: _on_novelty_graded,
    EV_CROSS_RUN_PRIOR: _on_cross_run_prior,
    EV_RESEARCH_ATTEMPTED: _on_research_attempted,
    EV_RESEARCH_COMPLETED: _on_research_completed,
    EV_LITERATURE_RETRIEVED: _on_literature_retrieved,
    EV_LESSONS_DISTILLED: _on_lessons_distilled,
    EV_LESSONS_REFRESHED: _on_lessons_refreshed,
    EV_REPORT_GENERATED: _on_report_generated,
}
