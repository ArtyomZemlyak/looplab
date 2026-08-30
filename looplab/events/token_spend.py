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

from looplab.events.traceview import _safe_token_count

# A generation span with no `phase` is BUCKETED, never dropped. `looplab timings` learned this the
# expensive way — it dropped every span with no `node_id`, which silently hid 143 of one run's 174
# spans and reported 2.7 minutes of a 27.9-minute run. An unattributable call is a fact about the
# record, and a breakdown that omits it reads as complete when it is not.
PHASE_UNATTRIBUTED = "(no phase)"

_GENERATION = "generation"


# A token count read off an untrusted span row, in ONE spelling. `traceview._safe_token_count` has
# owned this exact projection ("Signed-int64, non-negative token projection shared by normalization
# and roll-ups") for the same `attributes.usage` field of the same `spans.jsonl`, and a private copy
# here disagreed with it on three real inputs — a numeric string "1234" (it accepts, the copy read 0),
# a non-integral 1.9 (it refuses, the copy truncated to 1) and an over-int64 count (it clamps to 0,
# the copy's `in (inf, -inf)` test is False for a large int, so it returned the number and drove
# `attributed` — and therefore `residual`, the one figure this command exists to print — arbitrarily
# far off). Two readers of one field that disagree make `looplab tokens` and the trace/UI roll-up
# print different totals for one run with nothing to say which is wrong.
# `events` importing `events` is no new edge: traceview imports only `core`.
_int = _safe_token_count


def _states_zero(raw) -> bool:
    """Did the provider explicitly report a total of ZERO?

    `_int` maps both "no figure at all" and "a figure of 0" onto 0, so truthiness cannot tell them
    apart — which is the whole reason the caller cannot use `or`. Anything that is not a real
    number equal to zero (absent, negative, NaN, inf, junk) is NOT a stated figure and correctly
    falls back to `prompt + completion`. Numeric strings are accepted because `_safe_token_count`
    accepts them, so the two readers agree about what counts as a stated number.
    """
    if raw is None or isinstance(raw, bool):
        return False
    if isinstance(raw, int):            # exact, and never overflows: `float(10**400)` RAISES
        return raw == 0
    try:
        return float(raw) == 0.0
    except (TypeError, ValueError, OverflowError):
        return False


def _tokens_of(usage) -> tuple[int, int, int]:
    """(total, prompt, completion) for one generation's `usage` map.

    `total` is the provider's own figure when it gives one and prompt+completion otherwise — NOT the
    sum always, because a provider that bills cached prompt tokens differently reports a `total` its
    two parts do not add up to, and silently re-deriving it would overwrite the billed number.
    """
    if not isinstance(usage, dict):
        return 0, 0, 0
    prompt, completion = _int(usage.get("prompt")), _int(usage.get("completion"))
    stated = usage.get("total")
    counted = _int(stated)
    # PRESENCE, not truthiness. `_int(...) or (prompt + completion)` re-derives the sum whenever the
    # provider's own figure is ZERO — and a stated zero is a figure, not an absence: a fully
    # cache-served turn legitimately bills 0 against a 12,000-token prompt. That is precisely the
    # substitution the docstring above forbids, and it is not cosmetic: it inflates `attributed`,
    # drives `residual` negative, and makes `looplab tokens` print "spans over-attribute" — blaming
    # a retried provider call for a number this reader invented.
    # OPEN[stated-zero-rule-reads-a-writer-normalized-field] the zero this rule preserves cannot
    # reach it from the shipped writer, and the zero it CAN see is manufactured from junk.
    # proof:`present:(counted or _states_zero(stated))@looplab/events/token_spend.py+present:_token_int(t.get("total_tokens")@looplab/core/tracing.py`
    # REVIEW 2026-08-29 (P2 correctness): `core/tracing.py::_norm_usage` runs a TRUTHINESS chain of
    # its own at write time, so a provider-stated total of 0 beside real parts is replaced by their
    # sum before any span exists — the cache-served case above is unwritable — while a junk or
    # negative `total_tokens` ('n/a', -1) beside real parts IS stored as 0 (`_token_int` maps
    # unparseable to 0). This reader then treats that manufactured 0 as "provider explicitly
    # reported ZERO" and drops prompt+completion from `attributed`, opening a per-call span-vs-
    # ledger gap the CLI reports as spans under-attributing — the ledger's own `_normalize_usage`
    # records the sum for the same payload. Driven with the real functions on both sides. Fix
    # direction: make the writer honor presence the way this reader does (keep a stated in-range
    # zero, store junk as ABSENT), or accept a stored zero here only when the parts are zero too;
    # either way the cache-served claim moves to the site that decides it.
    total = counted if (counted or _states_zero(stated)) else (prompt + completion)
    return total, prompt, completion


# The bucket for a generation whose ancestry names no card — the per-card twin of
# `PHASE_UNATTRIBUTED`, and it exists for the same reason: an unattributable call is a fact about the
# record.
#
# A `propose` GENERATION DOES NOT LAND HERE, and this comment claimed the opposite until 2026-08-30.
# `orchestrator.py::stamp_proposal_span` stamps the card id INSIDE the open `propose` span the moment
# `_link` mints the card, and spans are written on CLOSE, so the id is on the row `_owning_card`
# walks to. MEASURED on `runs/rubertlite-dr-unified-v9` by folding its real spans twice, with and
# without the propose phase: ALL 27,436,262 propose tokens resolve to a real card and `(no card)`
# gains exactly 0 of them. Per card the propose share is 18.2 %-62.0 % of the row (card-5 is 62 %),
# so the old sentence was not a rounding error about a corner — it was backwards about a quarter of
# the run.
#
# THE ATTRIBUTION IS RIGHT AND THE LABEL WAS WRONG, which is why this was fixed by rewording rather
# than by narrowing `card_of` to `card_build` spans: the proposal that minted a card is money spent
# on THAT experiment's behalf, and moving 25.3 % of the run into `(no card)` would make this bucket
# the largest row in the table and tell the operator less than it does now. What a card's row prices
# is the whole experiment — the propose that minted it AND the build that followed.
CARD_UNATTRIBUTED = "(no card)"

# How far up a parent chain to look for a `card_id` before giving up. A build's generations sit two
# or three hops under `card_build` (`generation <- stages <- card_build`), so this is slack, not a
# limit anyone reaches; it is a TERMINATION bound, because a torn sidecar can present a parent chain
# that cycles and this fold must not hang on a file it is only reporting about.
_CARD_ANCESTRY_HOPS = 60


def token_spend_by_card(spans, card_nodes=None, ledger_total=None) -> dict:
    """Fold parsed span rows into a per-CARD token breakdown — what each experiment COST.

    NOT the build alone, and the docstring said "BUILD cost" until 2026-08-30. A card's row includes
    the `propose` that minted it, because `stamp_proposal_span` puts the card id on the open propose
    span and `_owning_card` walks to it — measured at 18.2 %-62.0 % of a row on
    `runs/rubertlite-dr-unified-v9`. An operator reconciling this table against the phase table's
    plan+stages+card_build read that difference as attribution error; it is the propose.

    MEASURED, and the reason this exists beside the per-phase fold: on `e5small-dr-unified-v9` at
    17.6 h, cards 3 and 4 cost 35.3M + 33.6M = 68.9M tokens — 21.0 % of the run's 327.6M — and every
    node either card ever owned was thrown away by the Card freshness gate before dispatch
    (`node_failed reason=superseded`, `never_evaluated: True`, `eval_seconds: 0.0`). Those four nodes
    are TWO cards each built TWICE, and the rebuilds cost MORE than the originals (40.9M of the
    68.9M). No shipped reader could say any of it: `token_spend_by_phase` stops at the phase, and the
    durable ledger carries no phase, no role and no node — so "what did this experiment cost, and was
    it ever evaluated" had no answer at all.

    **THE IDENTITY IS ALREADY IN THE RECORD; only the roll-up was missing.** A generation span does
    not carry `card_id` itself — the `card_build` span above it does — so this walks each
    generation's PARENT CHAIN to the nearest ancestor that names one. Measured on that run: 4,478 of
    4,511 build generations resolve, 97 % of all generation tokens.

    `spans` is any iterable of parsed span dicts. Unlike the per-phase fold this one MATERIALIZES it:
    a parent chain can only be walked once every span's id is known, and a generator cannot be read
    twice. `card_nodes` is `{card_id: {"nodes": [...], "discarded": [...]}}` supplied by the caller,
    which is the only side that can fold the event log; `discarded` is the subset of `nodes` that
    `core/models.py::is_unevaluated_speculative_discard` PROVED never reached a sandbox.

    Returns ``{rows, attributed, calls, ledger_total, residual, damaged, torn_attributes}`` with
    `rows` a list of ``{card, tokens, calls, prompt, completion, share, nodes, discarded,
    wholly_discarded}`` sorted by tokens DESC then card ASC, exactly as the per-phase fold sorts, so
    two reads of one file cannot disagree about the order.

    **`wholly_discarded` is the load-bearing field and its rule is narrow on purpose**: a card is
    wholly discarded only when it owns at least one node and EVERY node it owns was proven an
    unevaluated discard. A card with no nodes is not "wholly discarded" — it is a card whose build is
    still in flight or was refused before minting one, and calling that a loss would report the
    engine's normal forward motion as waste. A card with one discarded node and one that ran is not
    either: the idea WAS evaluated, and the discarded sibling is the prefetch machinery working.
    """
    rows_in = [s for s in spans]
    parents: dict = {}
    card_of: dict = {}
    for span in rows_in:
        if not isinstance(span, dict):
            continue
        span_id = span.get("span_id")
        if span_id is None:
            continue
        parents[span_id] = span.get("parent_id")
        attributes = span.get("attributes")
        if isinstance(attributes, dict):
            card = attributes.get("card_id")
            if isinstance(card, str) and card.strip():
                card_of[span_id] = card

    def _owning_card(span_id):
        seen = 0
        while span_id is not None and seen < _CARD_ANCESTRY_HOPS:
            if span_id in card_of:
                return card_of[span_id]
            span_id = parents.get(span_id)
            seen += 1
        return None

    per: dict[str, dict] = {}
    damaged = 0
    torn_attributes = 0
    for span in rows_in:
        if not isinstance(span, dict):
            damaged += 1
            continue
        if span.get("kind") != _GENERATION:
            continue
        attributes = span.get("attributes")
        if not isinstance(attributes, dict):
            torn_attributes += 1                      # a billed call whose attribution is lost
            attributes = {}
        card = _owning_card(span.get("span_id")) or CARD_UNATTRIBUTED
        total, prompt, completion = _tokens_of(attributes.get("usage"))
        row = per.setdefault(card, {"card": card, "tokens": 0, "calls": 0,
                                    "prompt": 0, "completion": 0})
        row["tokens"] += total
        row["prompt"] += prompt
        row["completion"] += completion
        row["calls"] += 1
    attributed = sum(r["tokens"] for r in per.values())
    lineage = card_nodes if isinstance(card_nodes, dict) else {}
    rows = sorted(per.values(), key=lambda r: (-r["tokens"], r["card"]))
    for row in rows:
        row["share"] = (row["tokens"] / attributed) if attributed else 0.0
        owned = lineage.get(row["card"]) if isinstance(lineage.get(row["card"]), dict) else {}
        nodes = [n for n in (owned.get("nodes") or [])]
        discarded = [n for n in (owned.get("discarded") or [])]
        row["nodes"] = nodes
        row["discarded"] = discarded
        row["wholly_discarded"] = bool(nodes) and set(discarded) >= set(nodes)
    return {
        "rows": rows,
        "attributed": attributed,
        "calls": sum(r["calls"] for r in per.values()),
        "ledger_total": ledger_total,
        "residual": None if ledger_total is None else ledger_total - attributed,
        "damaged": damaged,
        "torn_attributes": torn_attributes,
    }


def token_spend_by_build(spans, builds) -> dict:
    """Price each BUILD WINDOW, and separate the ones that minted nothing.

    MEASURED on `e5small-dr-unified-v9`, and it is a DIFFERENT loss from the one
    `token_spend_by_card` reports: three of that run's twelve builds finished with
    `card_build_done.skipped == "stale"` and NO `node_id` — card-2 31.6M, card-5 8.5M, card-3 1.2M,
    41.4M together, 11.8 % of the run. Those builds never minted a node, so the freshness gate never
    saw them; they are not in the 68.9M the per-card fold flags, except card-3's 1.2M.

    **THE PER-CARD FOLD IS BLIND HERE IN BOTH DIRECTIONS AND THAT IS WHY THIS EXISTS.** Its
    `wholly_discarded` needs the card to OWN a node, so card-5 — whose only build was skipped, and
    which has been `selection_ready` ever since — shows no nodes and no flag; and card-2 reads as a
    healthy 97.6M row while a third of that spend bought a build the engine threw away. The narrow
    rule is still correct for the question it answers ("was every experiment this card produced
    discarded before it ran"), so it is NOT widened: a build that minted nothing is a different
    fact and gets its own line.

    `builds` is `[{card, start, end, skipped, node_id}, ...]` from the caller, which is the only side
    that can read the durable log — the window is a `card_build_requested` -> `card_build_done` pair
    and `skipped` is that terminal's own field. A generation is charged to a window when its card
    matches AND its start lies inside it; the FIRST matching window wins, stated because two windows
    of one card could in principle overlap and a fold must be deterministic about it either way.

    Returns `{rows, skipped_tokens, skipped_builds, builds, attributed, unwindowed}`. `unwindowed`
    is every generation token that fell outside every window — `propose`, research and enrichment
    all legitimately live there — and it is REPORTED rather than dropped, the same rule the phase
    fold applies to `(no phase)`.

    **This shares the span clock with the event log and that is an assumption, not a proof.**
    `span["start"]` and an event's `ts` are both wall-clock seconds from the same process, which is
    what makes the join possible at all; a tracer that ever moved to a monotonic origin would make
    every window empty rather than wrong, which is the failure direction to prefer.
    """
    rows_in = [s for s in spans]
    parents, card_of = {}, {}
    for span in rows_in:
        if not isinstance(span, dict):
            continue
        span_id = span.get("span_id")
        if span_id is None:
            continue
        parents[span_id] = span.get("parent_id")
        attributes = span.get("attributes")
        if isinstance(attributes, dict):
            card = attributes.get("card_id")
            if isinstance(card, str) and card.strip():
                card_of[span_id] = card

    def _owning_card(span_id):
        seen = 0
        while span_id is not None and seen < _CARD_ANCESTRY_HOPS:
            if span_id in card_of:
                return card_of[span_id]
            span_id = parents.get(span_id)
            seen += 1
        return None

    windows = []
    for b in (builds or []):
        if not isinstance(b, dict):
            continue
        start, end = b.get("start"), b.get("end")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue                                   # a window with no bounds prices nothing
        windows.append({"card": b.get("card"), "start": float(start), "end": float(end),
                        "skipped": b.get("skipped"), "node_id": b.get("node_id"),
                        "tokens": 0, "calls": 0})

    attributed = 0
    unwindowed = 0
    for span in rows_in:
        if not isinstance(span, dict) or span.get("kind") != _GENERATION:
            continue
        attributes = span.get("attributes")
        if not isinstance(attributes, dict):
            attributes = {}
        total, _prompt, _completion = _tokens_of(attributes.get("usage"))
        attributed += total
        start = span.get("start")
        if not isinstance(start, (int, float)):
            unwindowed += total                        # untimed: never guessed into a window
            continue
        card = _owning_card(span.get("span_id"))
        hit = None
        for w in windows:
            if w["card"] == card and w["start"] <= float(start) <= w["end"]:
                hit = w
                break                                  # FIRST match wins, deterministically
        if hit is None:
            unwindowed += total
            continue
        hit["tokens"] += total
        hit["calls"] += 1
    skipped = [w for w in windows if w["skipped"]]
    return {
        "rows": windows,
        "skipped_tokens": sum(w["tokens"] for w in skipped),
        "skipped_builds": len(skipped),
        "builds": len(windows),
        "attributed": attributed,
        "unwindowed": unwindowed,
    }


def token_spend_by_phase(spans, ledger_total=None) -> dict:
    """Fold parsed span rows into a per-phase token breakdown, reconciled against the ledger.

    `spans` is any iterable of already-parsed span dicts; `ledger_total` is the durable
    `llm_usage.total_tokens` sum, or None when the event log is unreadable. Returns
    ``{rows, attributed, calls, ledger_total, residual, damaged, torn_attributes}`` where `rows` is
    a list of ``{phase, tokens, calls, prompt, completion, share}`` sorted by tokens DESC then phase
    ASC, so two reads of one file cannot disagree about the order.

    `damaged` and `torn_attributes` are DIFFERENT populations and a caller must not add them into
    one sentence: `damaged` rows were not spans at all and contributed nothing, while a
    `torn_attributes` row is a real generation span counted in `calls` whose attribution — and only
    whose attribution — was lost.

    `residual` is `ledger_total - attributed` and may be NEGATIVE — that is a real state, not a bug
    to clamp: a retried provider call can open two spans against one billed row. It is None when no
    ledger was given, because "nothing to compare against" is not "zero difference".
    """
    per: dict[str, dict] = {}
    damaged = 0
    torn_attributes = 0
    for span in spans:
        if not isinstance(span, dict):
            damaged += 1
            continue
        if span.get("kind") != _GENERATION:
            continue                                  # a tool/operation span bills no tokens
        attributes = span.get("attributes")
        if not isinstance(attributes, dict):
            # COUNTED AS A CALL, and deliberately not skipped: `kind` read cleanly, so a billed
            # generation really did happen and only its attribution is lost. Dropping it would be
            # the very omission this module opens by refusing ("an unattributable call is a fact
            # about the record, and a breakdown that omits it reads as complete when it is not").
            # It lands in `(no phase)` with zero tokens, where the operator can see it.
            #
            # `torn_attributes` is therefore a SEPARATE counter from `damaged`, which counts rows
            # that were not spans at all and contributed nothing. One number for both made the CLI
            # report this row as "stepped over" while the same row was inside `calls` — one torn
            # span described two contradictory ways. Two populations, two counters, one true
            # sentence each.
            torn_attributes += 1
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
        "torn_attributes": torn_attributes,
    }
