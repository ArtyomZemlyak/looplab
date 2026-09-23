"""The fold's shared vocabulary: its context object and the lifecycle-generation rules.

`events/replay.py` held the whole fold until review 2026-09-22 (EVT-12) split its handler FAMILIES
into sibling modules (`events/replay_<family>.py`). More than one family needs each thing here, so
they live BELOW all of them — a family may not import `replay.py`, which imports every family:

* `_FoldCtx`, the cross-arm state one fold threads through every handler (selection certificate,
  accounting de-dup keys, finish/report adjacency, concept receipts);
* the ONE reading of an event's lifecycle stamp — `_event_generation` and its `_MISSING` sentinel,
  `event_generation_binds` and the Node-side `_generation_matches`, the control-intent variant
  `_control_generation_matches`, and `_node_for_event`;
* the ONE reading of an event's wall-clock stamp — `event_timestamp`, which the journals family's
  report handler publishes from, beside `run_wall_clock_seconds`, the run-duration reader its
  docstring pairs it with (moved together so that pairing still reads true).

Moved VERBATIM, comments included. `replay.py` re-exports every name, so `from
looplab.events.replay import event_generation_binds` (`engine/evaluate.py`), `event_timestamp` /
`run_wall_clock_seconds` (`cli/`, `engine/finalize.py`) and the tests' imports of the private names
keep resolving to these same objects. A patch aimed at `looplab.events.replay` does NOT reach the
families' calls to these names — `tests/test_replay_families.py` holds every patch the suite makes on
that module to a name `replay.py` itself defines or reads.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

from looplab.core.models import Event, Node, RunState, coerce_node_id as _coerce_node_id


class _FoldCtx:
    """Cross-arm state for selection, accounting de-dup, and finish/report adjacency."""
    __slots__ = (
        "best_confirmed", "best_confirmed_significant", "llm_usage_seen", "llm_usage_ids",
        "charged_terminal_generations", "charged_confirm_seeds", "charged_ablation_ids",
        "charged_noise_seeds", "charged_holdout_keys",
        "pending_finish_report", "concept_subject_invalidated", "concept_mode_untrusted",
        "concept_input_capped", "concept_input_invalid", "run_base_capped",
        "run_base_invalid", "run_base_seen", "event_index",
        "card_enrichment_index", "card_enrichment_omissions", "charged_repair_seqs",
        "repair_ledger_keys", "repair_ledger_per_node", "llm_cost_clean", "literature_ids",
    )

    def __init__(self):
        self.best_confirmed: int | None = None
        # R1-d: whether the confirm certificate found a SIGNIFICANT winner. Only consulted when a
        # best_confirmed is set; defaults True so legacy events / the ci_tie-off path keep the unconditional
        # override (byte-identical). A non-significant confirm under `verifier_ci_tie` must NOT erase best_ci.
        self.best_confirmed_significant: bool = True
        # Legacy summaries are last-write-wins only until the durable delta ledger begins.
        self.llm_usage_seen = False
        # New ledgers retry an ambiguously acknowledged append with the same identity. Replay is
        # first-write-wins for that ID; legacy usage events without an ID remain additive.
        self.llm_usage_ids: set[str] = set()
        # Whether `st.llm_cost` was last written by `_on_llm_usage` and is therefore already clean
        # (review 2026-09-22, EVT-04a: the ledger is sanitized once, not re-sanitized per row).
        self.llm_cost_clean = False
        # Every `lit-` id already in `st.literature` — the ONLY writer of that list is
        # `_on_literature_retrieved`, so this set is exactly its id column, kept incrementally
        # instead of rebuilt from the whole list on every row (review 2026-09-22, EVT-04a).
        self.literature_ids: set = set()
        # First terminal COST wins per (node,lifecycle), independently from whether that lifecycle is
        # still current. A reset may discard its metric/state, but cannot refund compute already spent.
        self.charged_terminal_generations: set[tuple[int, int]] = set()
        self.charged_confirm_seeds: set[tuple[int, int, int]] = set()
        # The same first-write-wins cost key for the eval-NOISE probe's repeats. Its own set and
        # not confirm's: the two phases can run seeds of the SAME (node, generation, seed) triple —
        # confirm at the full profile from `confirm_seed_base`, the probe at the node's own profile
        # from 0 — and sharing the memo would refund whichever ran second.
        self.charged_noise_seeds: set[tuple[int, int, int]] = set()
        # …and for the HOLDOUT phase's evaluator launches (review 2026-09-22, ENG2-15): the withheld
        # scorer / the private grade, one per (node, generation, search_epoch) — the key the phase
        # gates each launch on, so a duplicated row cannot charge twice.
        self.charged_holdout_keys: set[tuple] = set()
        # (node_id, seq) of every `node_repaired` row already charged to the repair epoch. What
        # carries invariant #5 for that counter now that it advances rather than max-ing: a
        # duplicate or re-folded row shares its SEQ, a per-process ordinal restart does not.
        self.charged_repair_seqs: set[tuple[int, int]] = set()
        self.charged_ablation_ids: set[str] = set()
        # `_record_repair_ledger`'s idempotence key for EVERY row it has seen — including the ones
        # the caps DROPPED. Scanning `st.repair_ledger` cannot answer for a dropped row (it is not
        # there), so a duplicate or re-folded `node_repaired` past a cap re-incremented the omission
        # counters and the CLI's "N dropped" over-reported. The per-node tally rides along for the
        # same reason it is cheap here and quadratic there: the cap check used to `sum()` the whole
        # ledger on every row, +18 ms per fold on the repair-heavy corpus run, on the poll path.
        self.repair_ledger_keys: set[tuple] = set()
        self.repair_ledger_per_node: dict[int, int] = {}
        # (physical event seq, physical fold index, content). The index is needed for legacy logs
        # whose envelopes have no meaningful seq but whose report->finish adjacency is still valid.
        self.pending_finish_report: tuple[int, int, dict] | None = None
        # Fold-only receipt boundary for legacy, unstamped node_concepts events. Lifecycle attempts also
        # advance for eval/code retries, but concept evidence becomes ambiguous only after the IDEA changed.
        self.concept_subject_invalidated: set[int] = set()
        # Explicit future/malformed mode values are not legacy absence. Keep the node, but make its
        # concept membership unavailable until a reviewed mode or independent classifier supersedes it.
        self.concept_mode_untrusted: set[int] = set()
        self.concept_input_capped: set[int] = set()
        self.concept_input_invalid: set[int] = set()
        self.run_base_capped = False
        self.run_base_invalid = False
        # A zero-length base is valid and distinct from no base event. Delta roots need this fold-only
        # presence bit because RunState.run_base_concepts alone represents both states as ``[]``.
        self.run_base_seen = False
        self.event_index = -1
        # Index retained enrichment candidates so an attacker-sized set of distinct owners remains
        # O(events), and count rejected candidates so public completeness can fail closed.
        self.card_enrichment_index: dict[tuple, int] = {}
        self.card_enrichment_omissions: dict[tuple, int] = {}


_MISSING = object()


def _event_generation(d: dict, *, legacy_attempt: bool = False):
    """Return an explicitly stamped lifecycle generation, `_MISSING` for a legacy unstamped event,
    or None for an invalid stamp. `node_repaired.data.attempt` predates lifecycle generations and is
    the INLINE-REPAIR ordinal, so callers opt into the terminal-only `attempt` compatibility alias."""
    if "generation" in d:
        raw = d.get("generation")
    elif legacy_attempt and "attempt" in d:
        raw = d.get("attempt")
    else:
        return _MISSING
    generation = _coerce_node_id({"node_id": raw})
    return generation if generation is not None and generation >= 0 else None


def event_generation_binds(d: dict, generation: int, *, legacy_attempt: bool = False) -> bool:
    """Does the lifecycle stamp on RAW event data `d` bind to `generation`?

    PUBLIC because a raw-log reader outside `events/` needs exactly this question and there must be
    one answer to it. `engine/evaluate.py`'s three durable per-node budgets (repair attempts, dep
    rounds, full re-trains) read `node_repaired`/`deps_installed`/`full_retrain_charged` straight off
    the log rather than through the fold — the fold keeps the latest state, they need the trajectory
    — and each of them hand-spelled this rule as `"generation" in d and d.get("generation") !=
    generation` under a comment claiming it keyed "exactly as `replay._generation_matches` keys it".
    It did not: measured over 18 raw values, `generation: true` was admitted by the `!=` and dropped
    by the fold (`bool` subclasses `int`, so `True != 1` is False, while `coerce_node_id` rejects a
    bool on purpose), and `generation: "1"` was the reverse. A budget charged against rows the fold
    does not have is not the log's budget, which is that family's whole premise.

    Exported rather than declared in `tests/test_cross_package_private_seams.py`, per that registry's
    own rule ("the moment to ask whether it should be public instead"): the alternative was to leak
    `_event_generation` AND the `_MISSING` sentinel across the package boundary, and a sentinel is
    not an API. `_generation_matches` is the Node-side twin and now delegates here, so there is one
    implementation and no second copy to drift.
    """
    stamped = _event_generation(d, legacy_attempt=legacy_attempt)
    return stamped is _MISSING or (stamped is not None and stamped == generation)


def _generation_matches(n: Node, d: dict, *, legacy_attempt: bool = False) -> bool:
    return event_generation_binds(d, n.attempt, legacy_attempt=legacy_attempt)


def _control_generation_matches(n: Node, d: dict) -> bool:
    """Match a lifecycle-mutating operator intent while preserving old persisted logs.

    Historical controls were unstamped and can legitimately contain several resets, so a missing
    stamp binds to the lifecycle visible at that point in the append-only replay. Modern producers
    always stamp and the HTTP boundary performs CAS before append; an explicit stale stamp is rejected.
    """
    generation = _event_generation(d)
    if generation is _MISSING:
        return True
    return generation is not None and generation == n.attempt


def _node_for_event(st: RunState, d: dict) -> Node | None:
    nid = _coerce_node_id(d)
    return st.nodes.get(nid) if nid is not None else None


# 9999-12-31T23:59:59Z. Past this an `Event.ts` is corruption (or a unit mix-up — a milliseconds
# timestamp lands here), not a date, and admitting it would let one damaged row define a run's whole
# duration. Paired with the `> 0` floor below because `Event.ts` DEFAULTS to 0.0: a hand-built Event
# or a fixture that never went through `EventStore.append` carries "no timestamp", not 1970.
_MAX_EVENT_TS = 253_402_300_799


def event_timestamp(e) -> Optional[float]:
    """One event's wall-clock timestamp as a usable float, or None when the row does not carry one.

    The ONE spelling of that rule, because two readers need the same answer over the same untrusted
    bytes and used to derive it separately: `_on_report` publishes `published_at` from it, and
    `run_wall_clock_seconds` below measures a run's duration from it. `type(ts) in (int, float)`
    rather than `isinstance` on purpose — `isinstance(True, int)` is True, and a JSON `true` in a
    hand-edited log would otherwise become the epoch second 1.
    """
    ts = getattr(e, "ts", None)
    if type(ts) not in (int, float) or not math.isfinite(ts):
        return None
    return float(ts) if 0 < ts <= _MAX_EVENT_TS else None


def run_wall_clock_seconds(events: Iterable[Event]) -> Optional[float]:
    """How long the RUN took, from its own log: last usable `ts` minus first usable `ts`.

    This is the one duration that survives a process boundary. A run that is stopped and wrapped up
    hours later by `looplab finalize` is finished by a DIFFERENT process, so any `time.time() - start`
    the finalizing process measures describes the wrap-up, not the run — measured, a 274-second run
    reported `budget.elapsed_s = 0.027`. The event log is the only record that spans both processes,
    and it has carried `ts` on every row since the first version of the envelope, so this is exact on
    OLD logs too — nothing had to be recorded for it.

    Order-tolerant (min/max, not first/last position) like everything else that reads the log, and
    reader-tolerant: rows without a usable timestamp are skipped rather than dragging the span to
    1970. Returns None when NO row carries one (a synthetic/hand-built log), so a caller can say
    "unknown" instead of publishing a confident 0.0.

    Note what it deliberately does NOT do: subtract the idle gap while a stopped run waited for its
    `finalize`. That gap is part of how long the run took, and it is exactly the interval the old
    number pretended did not exist. `looplab timings` names the untraced share of it.
    """
    first: Optional[float] = None
    last: Optional[float] = None
    for e in events:
        ts = event_timestamp(e)
        if ts is None:
            continue
        if first is None or ts < first:
            first = ts
        if last is None or ts > last:
            last = ts
    if first is None or last is None:
        return None
    return max(0.0, last - first)
