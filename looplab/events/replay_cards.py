"""The fold's CARDS family: every handler that feeds the hypothesis-card board (docs/23).

Split out of `events/replay.py` (review 2026-09-22, EVT-12). These handlers change for the board's
reasons — Card identity, the operator's overlays, the enrichment journal, the speculative build queue
— and they only COLLECT: each folds a bounded receipt onto `RunState`, and `card_ledger.py::
derive_cards` (called once by `replay.py::_finalize_fold`) derives the board from them after the whole
log is folded. What lives here:

* the belief journal — `hypothesis_added` / `_merged` / `_updated` / `_ranked`;
* the Card receipts — added, merged, dropped (both spellings), reopened, reprioritized, edited,
  resource-pinned, enriched, ranked;
* the speculative Card-build queue — requested, attempted, done.

Moved VERBATIM, comments included. The card-journal cap `CARD_ENRICHMENT_JOURNAL_MAX` is read HERE
now, so a test that lowers it patches THIS module (`tests/test_card_enrichment_writers.py`), and
`replay.py` no longer binds the name at all: a patch still aimed at `looplab.events.replay` fails
loudly instead of lowering a cap no fold code reads (`tests/test_replay_families.py`). `replay.py`
re-exports the names tests import off it and merges `HANDLERS` into its dispatch table.
"""
from __future__ import annotations

import math

from looplab.core.concepts import bounded_raw_concept_values
from looplab.core.jsonutil import bounded_int, valid_digest_ref
from looplab.core.models import (CARD_STATEMENT_MAX_UTF8_BYTES as _CARD_REPLAY_STATEMENT_MAX_BYTES,
                                 Event, RunState, hypothesis_id, normalize_steering_context)
# The derived Card ledger's bounds (doc 25 EV-01), and ONLY the names these handlers call — the rule
# `replay.py`'s own ledger import states: a re-export of a helper the ledger calls internally would
# look like a patch seam while a monkeypatch through it silently missed the fold.
from looplab.events.card_ledger import (
    CARD_ENRICHMENT_JOURNAL_MAX,
    _CARD_REPLAY_NODE_ID_MAX,
    _CARD_REPLAY_STATEMENT_MAX,
    _bounded_card_added_receipt,
    _bounded_card_cross_run_enrichment,
    _bounded_card_drop_receipt,
    _bounded_card_enrichment,
    _bounded_card_footprint_enrichment,
    _bounded_card_merge_receipt,
    _bounded_card_novelty_enrichment,
    _bounded_card_ref,
    _card_replay_id,
    _card_replay_node_id,
    _card_replay_text,
    _digest_ref,
)
from looplab.events.replay_ctx import (_MISSING, _FoldCtx, _event_generation, _generation_matches,
                                       _node_for_event)
from looplab.events.types import (
    EV_CARD_ADDED, EV_CARD_AUTO_DROPPED, EV_CARD_BUILD_ATTEMPTED, EV_CARD_BUILD_DONE,
    EV_CARD_BUILD_REQUESTED, EV_CARD_DROPPED, EV_CARD_EDITED, EV_CARD_ENRICHED, EV_CARD_MERGED,
    EV_CARD_RANKED, EV_CARD_REOPENED, EV_CARD_REPRIORITIZED, EV_CARD_RESOURCE_PINNED,
    EV_HYPOTHESIS_ADDED, EV_HYPOTHESIS_MERGED, EV_HYPOTHESIS_RANKED, EV_HYPOTHESIS_UPDATED,
)


def _on_hypothesis_ranked(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # FOREAGENT board prioritization: latest wins. The order does not re-rank evaluated nodes; the sole
    # board derivation `_derive_cards` uses it to stamp Card.priority (the compatibility priority
    # fallback when no native card_ranked receipt exists).
    n = _node_for_event(st, d)
    generation = _event_generation(d)
    if generation is not _MISSING and (
            n is None or n.id in st.aborted_nodes or not _generation_matches(n, d)):
        return
    # `_derive_cards` iterates `(...).get("order") or []` unguarded, so a truthy SCALAR `order`
    # raised TypeError out of the fold and bricked the run. The native twin `_on_card_ranked`
    # already bounds this and says why: "a malformed/future order is an honest empty ranking, never
    # an iterable assumption that can brick replay". Same treatment here.
    raw_order = d.get("order")
    st.hypothesis_ranking = ({**d, "order": raw_order} if isinstance(raw_order, list)
                             else {**d, "order": []})


def _on_hypothesis_merged(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P1+: engine-written agentic merge — fold alias beliefs into a canonical. Collected
    # here, APPLIED deterministically in `_derive_cards` (no LLM in the fold). A malformed
    # entry is tolerated there; unknown on old logs -> skipped by the outer dispatch.
    receipt = _bounded_card_merge_receipt(d)
    if receipt is not None:
        receipt["_event_index"] = ctx.event_index
        st.hypotheses_merged.append(receipt)

def _on_hypothesis_added(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P1: an explicitly-registered hypothesis (human `add_hypothesis`, or a deep-research
    # direction) — may have no evidence yet. Evidence + verdict are DERIVED post-loop.
    #
    # FOLD DELTA ON OLD LOGS, stated: the bounds below drop an over-cap or non-string LEGACY row
    # ENTIRELY — not just its journal entry but also its card-identity registration and the
    # abandoned-reopen side effect that the pre-bounds handler executed for any truthy statement.
    # That is a fold-output change on old logs (a previously reopened hypothesis stays abandoned;
    # ambiguity edges can differ), which invariant 5's additive-only rule says needs reader-side
    # tolerance rather than silence. Real-log exposure is small (Card construction refused over-cap
    # statements anyway), but the delta is undocumented here and should be — or the reopen/identity
    # halves should keep accepting the legacy shape with the journal row alone bounded.
    statement = d.get("statement")
    clean_statement = statement.strip() if isinstance(statement, str) else ""
    try:
        statement_bytes = len(clean_statement.encode("utf-8"))
    except UnicodeError:
        statement_bytes = _CARD_REPLAY_STATEMENT_MAX_BYTES + 1
    if (clean_statement and len(clean_statement) <= _CARD_REPLAY_STATEMENT_MAX
            and statement_bytes <= _CARD_REPLAY_STATEMENT_MAX_BYTES):
        receipt = {"statement": clean_statement}
        # `parent_belief_id` carries the QUESTION-UNDER-QUESTION edge, resolved at the append site
        # by `research_cadence.question_parent_rows` (statement of a same-memo sibling, or an id
        # already on the board). Bounded exactly like `id` because it IS one; absent leaves the key
        # out entirely, so every log on disk folds byte-identically and a writer that said nothing
        # is never turned into a writer that claimed "no parent".
        for key, limit in (("id", 256), ("parent_belief_id", 256),
                           ("source", 64), ("rationale", 400)):
            value = d.get(key)
            if isinstance(value, str) and value.strip() and len(value.strip()) <= limit:
                receipt[key] = value.strip()
        at_node = d.get("at_node")
        if bounded_int(at_node, 0, (1 << 31) - 1):
            receipt["at_node"] = at_node
        # THE CONCEPTS THE QUESTION IS ABOUT, and until now this handler dropped them on the floor.
        # A question registered here becomes a board row that owns no action, and it carried NO
        # concept membership at all — measured on `runs/e5small-dr-unified-v5`, all five questions
        # had `concept_tags=[]` while the run's one experiment carried four. So the concept
        # hierarchy and the question board were disjoint taxonomies over the same run: an operator
        # grouping by concept saw the experiments and none of the questions they answer.
        #
        # Bounded by the SAME rule every other concept membership goes through
        # (`bounded_raw_concept_values`), so a question cannot introduce a slug shape a node could
        # not. Absent/malformed leaves the receipt without the key entirely, which is what keeps
        # every log on disk folding byte-identically — an empty list would be an authored claim of
        # "no concepts", and that is a different statement from "this writer said nothing".
        raw_concepts = d.get("concepts", d.get("concept_tags"))
        if isinstance(raw_concepts, list):
            values, _overflow, _invalid = bounded_raw_concept_values(raw_concepts)
            if values:
                receipt["concepts"] = values
        st.hypotheses_added.append(receipt)
        # Re-adding an abandoned statement reopens it (last write wins).
        try:
            hid = str(receipt.get("id") or hypothesis_id(receipt["statement"]))
            if hid in st.hypotheses_abandoned:
                st.hypotheses_abandoned.remove(hid)
        except Exception:  # noqa: BLE001 — an unreadable receipt cannot reopen a hypothesis; the fold stays total
            pass


def _on_card_added(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Hypothesis-card Kanban (docs/23): bounded registration plus an optional immutable ownership
    # receipt. Unreceipted historical rows remain visible shadows; only `_derive_cards` may validate
    # native identity/readiness. Evidence/verdict/status are derived.
    receipt = _bounded_card_added_receipt(d)
    if receipt is not None:
        # RunState is deep-copied on every incremental snapshot. Never retain Event.data
        # here: one unknown megabyte field would otherwise be multiplied by every live state read.
        st.cards_added.append(receipt)

def _on_card_merged(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Engine-written agentic merge — fold alias cards into a canonical. Collected here, APPLIED
    # deterministically in `_derive_cards` (no LLM in the fold), order-tolerant, back-compat on old logs.
    receipt = _bounded_card_merge_receipt(d)
    if receipt is not None:
        # aliases are identity-bearing, so cap the durable prefix before RunState owns it;
        # unknown merge metadata has no replay semantics and remains only in the append-only log.
        receipt["_event_index"] = ctx.event_index
        st.cards_merged.append(receipt)

def _on_card_dropped(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Canonical engine effect (`card_auto_dropped`) or operator intent (`card_dropped`):
    # {id, reason, dropped_by}. Historical engine-authored `card_dropped` rows intentionally share this
    # handler so old logs retain byte-for-byte replay semantics after the event namespace split.
    receipt = _bounded_card_drop_receipt(d)
    if receipt is not None:
        if e.type == EV_CARD_AUTO_DROPPED:
            # THE EVENT TYPE IS THE AUTHORITY, not the payload's claim about itself (review
            # 2026-09-22, EV-06). `card_auto_dropped` is the ENGINE's retirement by definition, and
            # `card_ledger._apply_card_drops` lets a reopen undo only an OPERATOR drop — so a row of
            # this type carrying `dropped_by: "operator"` (a forged, foreign or mis-parameterised
            # writer: `_drop_card_once` takes the author as an argument) published
            # `reopenable=True` and could be reopened, laundering an engine retirement back onto
            # the board. Every engine writer already stamps "engine", so its receipts are unchanged;
            # an unattributed one gains the key its reader already defaulted to.
            receipt["dropped_by"] = "engine"
        # keep a typed lifecycle receipt, not the raw control payload. This also prevents
        # arbitrary objects from becoming enormous strings later in `_derive_cards`.
        receipt["_event_index"] = ctx.event_index
        st.cards_dropped.append(receipt)


def _on_card_reopened(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """An operator putting a stopped card back on the board.

    MIRRORS `_on_card_dropped` deliberately, down to reusing its bounded receipt: the two are one
    lifecycle switch and a second, subtly different bound is how they come to disagree about which
    ids are admissible. `_event_index` is what resolves them — last receipt wins — so drop, reopen
    and drop again is expressible and replays identically.

    The drop receipt is NOT removed. The log is append-only and who stopped the work and why is
    history the reopened card still owes its reader; `_apply_card_drops` simply stops APPLYING a drop
    that a later reopen supersedes.
    """
    receipt = _bounded_card_drop_receipt(d)
    if receipt is not None:
        receipt["_event_index"] = ctx.event_index
        st.cards_reopened.append(receipt)


def _on_card_reprioritized(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Fold one server-stamped operator priority pin into its last-write-wins map."""
    card_id = _card_replay_id(d.get("id"))
    priority = d.get("priority")
    if (card_id is not None and d.get("source") == "operator" and d.get("pinned") is True
            and bounded_int(priority, 0, 255)):
        # Reinsert so dict iteration preserves GLOBAL last-event order even when aliases later merge
        # several raw ids onto one canonical Card. Plain assignment would retain first-insertion order.
        st.card_priority_pins.pop(card_id, None)
        st.card_priority_pins[card_id] = priority


def _on_card_edited(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Fold a display-only edit; the immutable seed/action receipt remains untouched."""
    card_id = _card_replay_id(d.get("id"))
    statement = _card_replay_text(
        d.get("statement"), max_chars=_CARD_REPLAY_STATEMENT_MAX, strip=True)
    if card_id is not None and statement is not None and d.get("source") == "operator":
        st.card_operator_edits.pop(card_id, None)
        edit = {"statement": statement, "source": "operator"}
        # Legacy/in-memory Event objects may not carry a durable sequence. Never manufacture an
        # acknowledgement for those rows; modern EventStore records always take this exact branch.
        if type(e.seq) is int and e.seq >= 0:
            edit["event_seq"] = e.seq
        st.card_operator_edits[card_id] = edit


def _on_card_resource_pinned(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Fold a quantitative operator override without rewriting receipt-owned ``footprint``."""
    card_id = _card_replay_id(d.get("id"))
    if card_id is None or d.get("source") != "operator" or d.get("pinned") is not True:
        return
    pin: dict[str, int | str] = {"pinned_by": "operator"}
    for key in ("gpus", "gpu_mem_mib"):
        value = d.get(key)
        if bounded_int(value, 0, _CARD_REPLAY_NODE_ID_MAX):
            pin[key] = value
        elif key in d:
            return
    if len(pin) > 1:
        st.card_resource_pins.pop(card_id, None)
        st.card_resource_pins[card_id] = pin

# The identity half of every folded enrichment candidate, in the order `rec` below is built.
_CARD_ENRICHMENT_IDENTITY_ORDER = ("id", "node_id", "generation", "proposal_ref", "_seq",
                                   "_event_index")


def _on_card_enriched(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Layer 1b: a delta onto a card (novelty verdict, cross-run prior, footprint-finalize, steering cues).
    # Collected here; APPLIED last-write-by-envelope-order in `_derive_cards`.
    raw_id = d.get("id")
    if isinstance(raw_id, str) and raw_id.strip() and len(raw_id.strip()) <= 256:
        rec = {"id": raw_id.strip()}
        fence_keys = {"node_id", "generation", "proposal_ref"}
        modern = bool(fence_keys & set(d))
        if modern:
            # Current engine deltas are bound to the exact Node lifecycle and proposal. A partial or
            # malformed fence is not a legacy row; dropping it prevents an enrichment from following a
            # numeric node slot after reset/re-proposal.
            node_id = d.get("node_id")
            generation = d.get("generation")
            proposal_ref = d.get("proposal_ref")
            digest = proposal_ref.get("digest") if isinstance(proposal_ref, dict) else None
            if (not bounded_int(node_id, 0, (1 << 31) - 1)
                    or not bounded_int(generation, 0, (1 << 31) - 1)
                    or not isinstance(proposal_ref, dict)
                    or set(proposal_ref) != {"v", "digest"} or proposal_ref.get("v") != 1
                    or not valid_digest_ref(digest, prefix="idea:v1:")):
                return
            rec.update({
                "node_id": node_id,
                "generation": generation,
                "proposal_ref": {"v": 1, "digest": digest},
            })
        allowed = (
            "novelty_verdict", "cross_run_prior", "footprint", "steering_context",
            "concept_tags", "lesson_refs", "claim_refs", "research_origin",
            "foresight_rank", "confidence",
        )
        # bound each allow-listed sibling independently. A huge lexically-early unknown
        # field must not consume a shared budget and erase id or a later valid field.
        for key in allowed:
            if key not in d:
                continue
            if key == "concept_tags":
                # keep enough derived receipt data to say that a node-less enrichment was
                # lossy.  The caller-provided provenance_tier is intentionally not copied: a free-form
                # delta must never promote its own tags to classifier/operator truth.
                if not isinstance(d[key], list):
                    continue
                values, overflow, invalid = bounded_raw_concept_values(d[key])
                rec[key] = values
                rec["_concept_tags_overflow"] = overflow
                rec["_concept_tags_invalid"] = invalid
                continue
            value = d[key]
            if key == "steering_context":
                bounded = normalize_steering_context(value)
                if bounded is not None:
                    rec[key] = bounded
                continue
            if key == "footprint":
                bounded = _bounded_card_footprint_enrichment(value)
                if bounded is not None:
                    rec[key] = bounded
                continue
            if key == "novelty_verdict":
                bounded = _bounded_card_novelty_enrichment(value)
                if bounded is not None:
                    rec[key] = bounded
                continue
            if key == "cross_run_prior":
                bounded = _bounded_card_cross_run_enrichment(value)
                if bounded is not None:
                    rec[key] = bounded
                continue
            if key == "research_origin":
                bounded = _bounded_card_ref(value)
                if bounded is not None and (not modern or _digest_ref(bounded, "memo")):
                    rec[key] = bounded
                continue
            if key in {"lesson_refs", "claim_refs"}:
                if not isinstance(value, list):
                    continue
                namespace = "lesson" if key == "lesson_refs" else "claim"
                refs: list[str] = []
                for item in value[:64]:
                    bounded = _bounded_card_ref(item)
                    if (bounded is not None and bounded not in refs
                            and (not modern or _digest_ref(bounded, namespace))):
                        refs.append(bounded)
                rec[key] = refs
                continue
            valid, bounded = _bounded_card_enrichment(value)
            if valid:
                rec[key] = bounded
        # envelope seq is authoritative; physical order is the deterministic tie-break for
        # legacy/default envelopes. Assign both after copying so payload fields can never spoof ordering.
        rec["_seq"] = e.seq if type(e.seq) is int else -1
        rec["_event_index"] = ctx.event_index if type(ctx.event_index) is int else -1
        # Keep one LWW candidate per raw Card id, exact lifecycle fence, and semantic field. Full
        # history remains in events.jsonl; RunState/FoldCursor retain only projection candidates.
        identity_keys = {"id", "node_id", "generation", "proposal_ref", "_seq", "_event_index"}
        semantic_keys = [key for key in rec if key not in identity_keys and not key.startswith("_concept_tags_")]
        fence = (
            rec["id"], rec.get("node_id"), rec.get("generation"),
            (rec.get("proposal_ref") or {}).get("digest"),
        )
        order = (rec["_seq"], rec["_event_index"])
        for key in semantic_keys:
            # Built from the fixed `_CARD_ENRICHMENT_IDENTITY_ORDER`, never by iterating the SET
            # above: the candidate is folded state, and a set of strings iterates in the process's
            # hash-seed order — one log dumped these rows with differently-ordered keys in two
            # processes (review 2026-09-22, found proving EVT-04a against the review's run logs).
            candidate = {name: rec[name] for name in _CARD_ENRICHMENT_IDENTITY_ORDER
                         if name in rec}
            candidate[key] = rec[key]
            if key == "concept_tags":
                for flag in ("_concept_tags_overflow", "_concept_tags_invalid"):
                    if flag in rec:
                        candidate[flag] = rec[flag]
            candidate_key = (*fence, key)
            replace_at = ctx.card_enrichment_index.get(candidate_key)
            if replace_at is not None:
                prior = st.cards_enriched[replace_at]
                prior_order = (
                    prior.get("_seq") if type(prior.get("_seq")) is int else -1,
                    prior.get("_event_index")
                    if type(prior.get("_event_index")) is int else -1,
                )
                if order >= prior_order:
                    st.cards_enriched[replace_at] = candidate
            elif len(st.cards_enriched) < CARD_ENRICHMENT_JOURNAL_MAX:
                ctx.card_enrichment_index[candidate_key] = len(st.cards_enriched)
                st.cards_enriched.append(candidate)
            else:
                # Handler-level LWW means repeated values for the same rejected window are still one
                # missing projection candidate, not an ever-growing count of audit-log events.
                #
                # A NEW key refused here is refused FOREVER: the fold replays the same log prefix,
                # so the same 4,096 keys win on every fold. That is fine for the fold — it is the
                # bound doing its job — but it makes the omission a DURABLE fact the writer has to
                # see, which is what `card_enrichment_omissions` carries out to `derive_cards` and
                # what `Card._card_enrichment_complete` publishes to a READER.
                #
                # The WRITER does not gate on that flag: it is set for ANY omission and never
                # clears, so using it would freeze every later enrichment of the card, including
                # keys this journal would still accept. `research_cadence` instead memoizes the
                # exact (card, subject, delta) it has already appended and watched not take — which
                # is what stops the re-append loop this bound would otherwise create (measured:
                # appends-per-pass 0,1,2,3,4… versus a control converging at one).
                ctx.card_enrichment_omissions.setdefault(candidate_key, 1)


def _on_card_ranked(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Layer 1b: FOREAGENT board prioritization for cards — latest wins (mirrors `_on_hypothesis_ranked`).
    raw_order = d.get("order")
    order: list[str] = []
    seen: set[str] = set()
    if isinstance(raw_order, list):
        for raw in raw_order[:256]:
            if not isinstance(raw, str):
                continue
            cid = raw.strip()
            if not cid or len(cid) > 256 or cid in seen:
                continue
            seen.add(cid)
            order.append(cid)
    # a malformed/future order is an honest empty ranking, never an iterable assumption
    # that can brick replay. Preserve metadata while replacing only the bounded, deduplicated order.
    metadata: dict = {"order": order}
    raw_at_node = d.get("at_node")
    if bounded_int(raw_at_node, 0, (1 << 31) - 1):
        metadata["at_node"] = raw_at_node
    raw_confidence = d.get("confidence")
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError, OverflowError):
        confidence = math.nan
    if (not isinstance(raw_confidence, bool) and math.isfinite(confidence)
            and 0.0 <= confidence <= 1.0):
        metadata["confidence"] = confidence
    if isinstance(d.get("reason"), str):
        metadata["reason"] = d["reason"][:400]
    if isinstance(d.get("ranked"), list):
        valid, ranked = _bounded_card_enrichment(d["ranked"])
        if valid:
            metadata["ranked"] = ranked
    st.card_ranking = metadata

def _on_hypothesis_updated(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Carries a status override (human/agent drops — or reopens — a line of inquiry).
    # Last write wins: "deleted" removes the card entirely (sticky); "abandoned" adds the
    # abandoned override; any other status clears the abandoned override (reopen).
    hid = d.get("id")
    if hid:
        status = d.get("status")
        if status == "deleted":
            if hid not in st.hypotheses_deleted:
                st.hypotheses_deleted.append(hid)
        elif status == "abandoned":
            if hid not in st.hypotheses_abandoned:
                st.hypotheses_abandoned.append(hid)
        elif hid in st.hypotheses_abandoned:
            st.hypotheses_abandoned.remove(hid)


def _on_card_build_requested(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Fold one main-task speculative build election.

    The request generation is the search epoch observed under ``_id_lock``. A late producer from an
    earlier epoch may still be given up by a matching done record, but a request itself cannot enter
    the queue with a stale/forged epoch. The compact normalized record is the durable buffer key.
    """
    card_id = _card_replay_id(d.get("card_id"))
    generation = d.get("generation")
    if (card_id is None or not bounded_int(generation, 0, _CARD_REPLAY_NODE_ID_MAX)
            or generation != st.search_epoch):
        return
    st.card_build_requests.append({"card_id": card_id, "generation": generation})


def _on_card_build_attempted(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Fold one PHYSICAL producer attempt against a request head.

    Selection-neutral by construction: nothing here touches nodes, cards or the request queue — the
    row exists so a resume can see that a provider call for this exact request identity may already
    have been paid for. Unlike the request above it is NOT epoch-filtered: an attempt from a since-
    superseded epoch is still an attempt that may have been billed, and dropping it would erase the
    very evidence this record exists to keep.

    `index` is the queue POSITION the attempt was made against, the same discipline `card_build_done`
    uses. Without it a card that was closed and later re-elected at the same epoch would carry the
    identical (card_id, generation) key, and the old — already reconciled — attempt would quarantine
    a brand-new head forever. The fold stays dumb: it records the position, the engine decides whether
    an attempt still belongs to the open head.
    """
    card_id = _card_replay_id(d.get("card_id"))
    generation = d.get("generation")
    index = d.get("index")
    if (card_id is None or not bounded_int(generation, 0, _CARD_REPLAY_NODE_ID_MAX)
            or type(index) is not int or index < 0):
        return
    st.card_build_attempts.append(
        {"card_id": card_id, "generation": generation, "index": index})


def _on_card_build_done(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Advance exactly one positional Card-build request and retain its successful node link.

    A valid skipped result deliberately advances the counter so producer failure cannot wedge every
    resume. Unlike the older fork pair, the async producer can race a search-epoch change, so the writer
    must duplicate the exact request identity and replay advances only the matching current head.
    Orphan/malformed/mismatched done rows are inert and can never skip a later real request.
    """
    # AN INDEXED CLOSE names the request position it completes; without `index` the row closes the
    # head, exactly as every log written before several producers existed. The index must name a
    # position still OPEN (at or past the cursor, not already closed ahead of it), so a replayed or
    # forged row can never close a request twice or skip one it does not name.
    named = d.get("index")
    if named is None:
        request_index = st.card_builds_done
    elif (type(named) is not int or named < st.card_builds_done
          or named in st.card_builds_done_ahead):
        return
    else:
        request_index = named
    request = (st.card_build_requests[request_index]
               if request_index < len(st.card_build_requests) else None)
    card_id = _card_replay_id(d.get("card_id"))
    generation = d.get("generation")
    if (request is None or card_id != request["card_id"]
            or type(generation) is not int or generation != request["generation"]):
        return
    skipped = d.get("skipped")
    # `isinstance` FIRST: set membership hashes the raw event value, so a forged/corrupt `skipped`
    # that is a list/dict raised TypeError out of the fold and bricked every replay/resume of the run
    # (the fold loop has no per-event try/except). Every other field this handler reads is
    # shape-guarded; this one is now too.
    if isinstance(skipped, str) and skipped in {"producer_failed", "stale"}:
        _close_card_build_request(st, request_index, skipped)
        if skipped == "producer_failed" and card_id not in st.card_build_producer_failed:
            st.card_build_producer_failed.append(card_id)
        return
    if skipped is not None or d.get("speculative") is not True:
        return
    node_id = _card_replay_node_id(d.get("node_id"))
    if node_id is None:
        return
    node = st.nodes.get(node_id)
    if (node is None or node.idea.card_id != card_id
            or getattr(node, "speculative", False) is not True
            or getattr(node, "card_build_generation", None) != generation):
        return
    # Keep only the exact bounded receipt consumed by depth/freshness recovery. Last write for a node
    # is harmless and deterministic; first-terminal lifecycle rules still own its actual node state.
    _close_card_build_request(st, request_index, "committed")
    st.speculative_nodes[node_id] = dict(request)


def _close_card_build_request(st: RunState, request_index: int, outcome: str) -> None:
    """Close one request position and record its outcome IN POSITION ORDER.

    `card_build_outcomes[i]` is read as the outcome of request `i` once the queue is closed
    (`search/speculation_quality.py`), so an out-of-order close is inserted after every earlier
    position already closed rather than appended. The cursor then advances over every position
    closed ahead of it. With in-order closes both steps reduce to the historical `append` and `+= 1`.
    """
    earlier = st.card_builds_done + sum(1 for i in st.card_builds_done_ahead if i < request_index)
    st.card_build_outcomes.insert(earlier, outcome)
    if request_index == st.card_builds_done:
        st.card_builds_done += 1
        while st.card_builds_done in st.card_builds_done_ahead:
            st.card_builds_done_ahead.remove(st.card_builds_done)
            st.card_builds_done += 1
    else:
        st.card_builds_done_ahead.append(request_index)


# This family's rows of the fold's dispatch table. `replay.py::_HANDLERS` is assembled from every
# family's table and refuses a type two of them claim, so a board handler is registered HERE,
# beside its body, and nowhere else. Both drop spellings share one handler, as they always did.
HANDLERS = {
    EV_HYPOTHESIS_RANKED: _on_hypothesis_ranked,
    EV_HYPOTHESIS_MERGED: _on_hypothesis_merged,
    EV_HYPOTHESIS_ADDED: _on_hypothesis_added,
    EV_HYPOTHESIS_UPDATED: _on_hypothesis_updated,
    EV_CARD_ADDED: _on_card_added,
    EV_CARD_BUILD_REQUESTED: _on_card_build_requested,
    EV_CARD_BUILD_ATTEMPTED: _on_card_build_attempted,
    EV_CARD_BUILD_DONE: _on_card_build_done,
    EV_CARD_MERGED: _on_card_merged,
    EV_CARD_AUTO_DROPPED: _on_card_dropped,
    EV_CARD_DROPPED: _on_card_dropped,
    EV_CARD_REPRIORITIZED: _on_card_reprioritized,
    EV_CARD_EDITED: _on_card_edited,
    EV_CARD_REOPENED: _on_card_reopened,
    EV_CARD_RESOURCE_PINNED: _on_card_resource_pinned,
    EV_CARD_ENRICHED: _on_card_enriched,
    EV_CARD_RANKED: _on_card_ranked,
}
