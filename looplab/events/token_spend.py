"""Where the TOKENS went, by phase — the token twin of `looplab timings`' wall-clock breakdown.

MEASURED, and the reason this exists: on `rubertlite-dr-unified-v9` the Developer BUILDING one
experiment (`plan` + `stages` + `card_build`) is 61.0 % of 108,534,235 tokens, and on
`rubertlite-dr-unified-v8` it is 63.3 % of 201,041,498. `propose` is the next 18-25 %. Meanwhile
`deep_research` — the phase most recently worked on — is 0.2-0.3 %. Nobody could see any of that:
the durable ledger records TOTALS only and no CLI or UI computed the split, so answering it took an
ad-hoc script.

**THE TOTAL AND THE SPLIT COME FROM DIFFERENT PLACES, AND THAT IS THE DESIGN.** `llm_usage` (the
durable, replayable ledger) carries {cost, calls, priced_calls, prompt_tokens, completion_tokens,
total_tokens, usage_id} — no phase, no role, no node — so it knows the TRUE total and nothing about
where it went. The `generation` spans carry `phase` and `usage` but live in `spans.jsonl`, a sidecar
that replay does not rebuild and `serve/trace_clear.py` can destroy. So the ledger is the
DENOMINATOR and the spans supply the ATTRIBUTION, exactly as `timings` reconciles span durations
against the event log's own wall clock — and the gap between them is PRINTED rather than hidden.

**WHY THE PHASE IS NOT SIMPLY STAMPED ON `llm_usage` INSTEAD.** That was the obvious fix and it is
the wrong one: `engine/costs.py` appends the row from an OUTBOX DRAIN and a reconcile retry loop as
well as inline, so `core/tracing.py::_phase_ctx` at append time is the phase that DRAINED the row,
not the phase that spent the tokens. A confidently wrong attribution on a durable row is worse than
an honest one derived from the span that actually made the call, which carries the phase it was
opened under. Re-open that decision only with a measurement showing every append is inline.

Pure: no I/O, no engine import. The caller reads the files.
"""
from __future__ import annotations

# A generation span with no `phase` is BUCKETED, never dropped. `looplab timings` learned this the
# expensive way — it dropped every span with no `node_id`, which silently hid 143 of one run's 174
# spans and reported 2.7 minutes of a 27.9-minute run. An unattributable call is a fact about the
# record, and a breakdown that omits it reads as complete when it is not.
PHASE_UNATTRIBUTED = "(no phase)"

_GENERATION = "generation"


def _int(value) -> int:
    """A non-negative int, or 0. `isinstance(True, int)` is True, so bools are refused explicitly —
    a `True` token count would silently add 1."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return 0
    return int(value) if value > 0 else 0


def _tokens_of(usage) -> tuple[int, int, int]:
    """(total, prompt, completion) for one generation's `usage` map.

    `total` is the provider's own figure when it gives one and prompt+completion otherwise — NOT the
    sum always, because a provider that bills cached prompt tokens differently reports a `total` its
    two parts do not add up to, and silently re-deriving it would overwrite the billed number.
    """
    if not isinstance(usage, dict):
        return 0, 0, 0
    prompt, completion = _int(usage.get("prompt")), _int(usage.get("completion"))
    total = _int(usage.get("total")) or (prompt + completion)
    return total, prompt, completion


def token_spend_by_phase(spans, ledger_total=None) -> dict:
    """Fold parsed span rows into a per-phase token breakdown, reconciled against the ledger.

    `spans` is any iterable of already-parsed span dicts; `ledger_total` is the durable
    `llm_usage.total_tokens` sum, or None when the event log is unreadable. Returns
    ``{rows, attributed, calls, ledger_total, residual, damaged}`` where `rows` is a list of
    ``{phase, tokens, calls, prompt, completion, share}`` sorted by tokens DESC then phase ASC, so
    two reads of one file cannot disagree about the order.

    `residual` is `ledger_total - attributed` and may be NEGATIVE — that is a real state, not a bug
    to clamp: a retried provider call can open two spans against one billed row. It is None when no
    ledger was given, because "nothing to compare against" is not "zero difference".
    """
    per: dict[str, dict] = {}
    damaged = 0
    for span in spans:
        if not isinstance(span, dict):
            damaged += 1
            continue
        if span.get("kind") != _GENERATION:
            continue                                  # a tool/operation span bills no tokens
        attributes = span.get("attributes")
        if not isinstance(attributes, dict):
            damaged += 1
            attributes = {}
        phase = attributes.get("phase")
        phase = str(phase) if isinstance(phase, str) and phase.strip() else PHASE_UNATTRIBUTED
        total, prompt, completion = _tokens_of(attributes.get("usage"))
        row = per.setdefault(phase, {"phase": phase, "tokens": 0, "calls": 0,
                                     "prompt": 0, "completion": 0})
        row["tokens"] += total
        row["prompt"] += prompt
        row["completion"] += completion
        row["calls"] += 1                             # a call with no usage still HAPPENED
    attributed = sum(r["tokens"] for r in per.values())
    rows = sorted(per.values(), key=lambda r: (-r["tokens"], r["phase"]))
    for row in rows:
        row["share"] = (row["tokens"] / attributed) if attributed else 0.0
    return {
        "rows": rows,
        "attributed": attributed,
        "calls": sum(r["calls"] for r in per.values()),
        "ledger_total": ledger_total,
        "residual": None if ledger_total is None else ledger_total - attributed,
        "damaged": damaged,
    }
