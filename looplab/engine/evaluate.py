"""The eval task (`_evaluate`: materialize -> eval -> trust scans -> inline repair loop -> ONE
terminal event) — extracted from orchestrator.py as a
MIXIN: `class Engine(EvaluateMixin, …)` inherits it unchanged, so there is ZERO call-site churn
and `self` here IS the engine. The body is a verbatim move and reads engine attributes freely
(~30 of them: `_write_lock`, `proxy_scorer`, `_inline_repair*`, `sandbox`, trust knobs, …); its
helpers (`_materialize`/`_run_eval`/`_triage_crash`/`_repair`/`_safe_reuse_start`/
`_audit_workdir_writes`/…) resolve through `self` — onto the sibling mixins or the Engine
class itself (`_materialize`/`_write_node_files` stay in orchestrator.py).

`_evaluate` was the engine's single largest method until 2026-08-05 (doc 25 ES-03), which named the
decisions its attempt loop was making inline — the intervention watcher (`_eval_intervention_seen`
/`_watch_for_intervention`), the trust surface and its findings (`_trust_scan_surface`
/`_trust_scan_signals`), and the inline-repair pipeline's five verdicts (`_eval_failure_text`,
`_repaired_footprint`, and the module-level `_repair_provider_failure`/`_repair_change_set`
/`_repair_forces_full_retrain`). The 2026-08-06 durability fix added two more of the same kind:
`_durable_repair_ledger` (the repair budget + judge history read back off the EVENT LOG, so a resume
continues a node's repair chain instead of restarting it) and `_effective_repair_cap` (what
`inline_repair_attempts = 0` actually gets).
946 -> 727 lines, the attempt loop 602 -> 420, with every append,
fold, write-lock point and branch order left exactly where it was. What made those blocks worth
naming is not their size: each was reachable ONLY by driving a real sandboxed evaluation that failed
in exactly the right way, so `tests/test_evaluate_named_rules.py` is the first coverage several of
their branches have had. The residue is genuinely a driver — the one-terminal invariant, the
attempt loop's control flow, and the loop-local counters those rules round-trip through.

`fold` is imported from its canonical home here (the orchestrator's module-global `fold` seam —
monkeypatched by two tests — does not reach `_evaluate`: those patches gate node CREATION).
Invariant #2 lives in this file: exactly ONE terminal event per node, emitted at the end of the
attempt loop. Trust scans (reward-hack / code-leakage / critic) stay lazy, method-local imports."""
from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from typing import Optional

import anyio
import orjson

from looplab.core.llm import BudgetExceeded
from looplab.core.models import (DEVELOPER_ERROR_PREFIX, NodeStatus, coerce_node_id,
                                 developer_artifact_footprint,
                                 is_developer_error, normalize_extra_metrics)
from looplab.core.node_evidence import begin_metrics_attempt
from looplab.engine.asha_monitor import extract_resource_curve
from looplab.engine.metric_salvage import (DEFAULT_METRIC_SALVAGE, SALVAGE_CAUSE_TRIAGE_ACTION,
                                           cause_repair_context, salvage_gates,
                                           declaration_actually_corrected,
                                           declaration_only_repair, declaration_repair_provenance,
                                           declared_pipeline_completed, recheck_floor,
                                           recheckable_expect, recheckable_salvage,
                                           salvage as salvage_metric)
from looplab.engine.options import _UNSET
from looplab.engine.train_monitor import eval_log_plan, snapshot_training_logs

# Watchdog/monitor ticks get their OWN thread pool, separate from anyio's shared 40-token default.
# Every `to_thread.run_sync` in the engine draws on that default, and an in-flight eval holds a token
# for its whole (often multi-hour) duration — so at high `eval_parallel` the evals pin the pool and
# every liveness poll (operator abort/reset detection, the train and ASHA kill signals) queues behind
# them, going blind exactly when a kill matters. These ticks are short reads; a small pool is enough,
# and is what keeps them immediately schedulable. Process-wide and lazily built so importing this
# module never touches the event loop.
_WATCH_THREADS = 8
_WATCH_LIMITER: "anyio.CapacityLimiter | None" = None


def _watch_limiter() -> "anyio.CapacityLimiter":
    global _WATCH_LIMITER
    if _WATCH_LIMITER is None:
        _WATCH_LIMITER = anyio.CapacityLimiter(_WATCH_THREADS)
    return _WATCH_LIMITER
from looplab.engine.triage import (_MAX_DEP_ROUNDS, DEFAULT_TRIAGE_ACTION,
                                   UNANSWERABLE_TRIAGE_ACTION, UNREADABLE_TRIAGE_ACTION,
                                   _failure_reason, repair_artifact_defect)
# The repair-verification rung: did this repair do what its rationale said? A LEAF (pure functions
# over bytes the loop already holds — no engine state, no events, no model), imported here rather
# than re-derived, because the same verdict has to be written to the durable row, read back off the
# log by `_durable_repair_ledger` and rendered by `_format_repair_log`, and a second spelling of
# `REPAIR_VERDICTS` would let those three disagree silently.
from looplab.engine.repair_verify import (INERT_REPAIR_LIMIT, REPAIR_VERDICTS, changed_region,
                                          inert_streak, verify_repair)

# How many repair calls may answer with something that is not Python before the loop calls it a
# provider failure rather than a truncation. NOT operator-settable and deliberately small: this is
# not a budget, it is the point at which "the model got cut off" stops being the likelier
# explanation than "the endpoint is answering with prose". Two truncations in a row on one node
# already warrant looking at the provider, and the run-level pause is resumable, so the cost of
# being early here is one operator click while the cost of being late is the whole node budget.
_UNPARSEABLE_REPAIR_LIMIT = 3

# Bounds on the repair history handed to the stop judge. Measured live (deepseek-v4-flash, the
# recorded six-migration chain): the history costs ~66 extra prompt tokens per row and ZERO extra
# calls, so a full chain under the default cap is ~800 tokens — cheap enough that the row cap is
# NOT a cost lever. It exists for `inline_repair_attempts = 0` (no OPERATOR cap, what a pre-existing
# run resumes with), whose only other bound is the far looser `_UNLIMITED_REPAIR_CEILING`. It keeps
# the NEWEST rows, which is the lossy direction for "we already tried this" — acceptable only
# because it binds solely on chains longer than any cap an operator would set.
_JUDGE_HISTORY_ROWS = 12
_JUDGE_ERROR_CHARS = 300

# WHAT AN OPERATOR WITH `inline_repair_attempts: 0` GETS, stated plainly because it is the setting
# most preserved runs actually carry (38 of 46 snapshots under `runs/`, INCLUDING `rubert-dr-0804` —
# the 2345-repair incident this whole redesign was built for).
#
# `0` still means "no OPERATOR cap": the grandfathering decision in
# `core/config.py::LEGACY_CONFIG_SNAPSHOT_DEFAULTS` stands, an operator who chose unlimited in-node
# repair does not silently acquire a 12 mid-run, and the judge remains the primary stop. What it no
# longer means is "unbounded", because that was measured to be exactly nothing: with an always-
# `repair` judge and 0, the loop ran 795 repairs / 796 full evals in 45 seconds and emitted no
# terminal — i.e. on its OWN snapshot, the incident this design exists to prevent was still not
# prevented. So a run with no operator cap gets THIS ceiling instead, and the terminal says which
# bound stopped it.
#
# 50 rather than 12: it must not silently re-cap a run the operator deliberately uncapped, so it sits
# four times the shipped default and six times the longest legitimate chain on record (8 — the six
# stale-dependency migrations plus two repairs on the real research question, see
# `docs/guide/configuration.md`). A node that has made fifty in-place repairs and still has no metric
# is not one repair away from working; whatever the judge believes, that node's remaining value is
# below the cost of continuing to re-eval it, and a terminal returns the budget to the search.
_UNLIMITED_REPAIR_CEILING = 50


def _effective_repair_cap(inline_repair_attempts: int) -> int:
    """The number of in-node repairs this node may actually make.

    A named rule with a truth table because "0 = unlimited" is a THREE-way decision the loop reads in
    three different places (the budget gate, the cap-out message, and the `attempts_left` the judge is
    told), and the three used to disagree: the gate treated 0 as no bound at all while the message
    quoted `self._inline_repair_attempts` and the judge was told `None`."""
    return int(inline_repair_attempts) or _UNLIMITED_REPAIR_CEILING


def _durable_row_belongs(d, node_id: int, generation: int) -> bool:
    """Does this RAW log row belong to `(node_id, generation)` — keyed exactly as the fold keys it?

    The ONE spelling the three durable ledgers below share, and it is a CALL into the fold's own
    rules rather than a re-statement of them. All three used to spell it inline as
    `d.get("node_id") != node_id` / `"generation" in d and d.get("generation") != generation`, each
    with a comment claiming it keyed "exactly as `replay._generation_matches` keys the same event".
    It did not, in both directions — measured over 18 raw values, three disagree:

      * `generation: true` (or `node_id: true`) against lifecycle/node **1**. `bool` is a subclass
        of `int`, so `True != 1` is False and the raw compare ADMITS the row, while
        `core/models.py::coerce_node_id` rejects a bool on purpose (its docstring: `int(True) == 1`
        would spuriously match node 1) and the fold therefore drops it. The inline comment on
        `_durable_repair_ledger` worried about exactly this case — "a corrupt row the fold refuses"
        — and then admitted one.
      * `generation: "1"` / `node_id: " 1 "`. The fold coerces a numeric string and KEEPS the row;
        the raw compare rejects it, so a resume reads a node's repair chain as empty and hands it a
        fresh budget — the very defect the durable ledger exists to fix.

    A budget charged against rows the fold does not have (or refunded for rows it does) is not the
    log's budget, which is this whole family's premise. `_event_generation` is imported rather than
    re-derived for the same reason `agents/unified_agent.py` imports the verdict vocabulary instead
    of re-spelling it: a rule with two copies has two behaviours as soon as one moves.

    `legacy_attempt` stays OFF, matching `replay._on_node_repaired`'s own call: on `node_repaired`
    `attempt` is the inline-repair ordinal, not a generation.
    """
    if not isinstance(d, Mapping):
        return False
    return coerce_node_id(d) == node_id and event_generation_binds(d, generation)


def _durable_int(value, default=0):
    """A durable counter field as an int, or `default` — TOTAL over an untrusted log.

    `int(d.get("unparseable_repairs") or 0)` raised `ValueError` on a string and `TypeError` on a
    list, from inside `_evaluate`'s attempt loop where nothing catches it: the eval dies with NO
    terminal event, so the node is neither evaluated nor failed and every resume re-reads the same
    row and dies again. That is the failure mode `runtime/command_eval.py::READER_PATH_KEYS` exists
    to prevent, one reader over. A counter nobody can parse contributes its default, exactly as the
    `isinstance(n, int)` guards beside it already do for `attempt`/`round`/`spent`.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _durable_dep_rounds(events, node_id: int, generation: int) -> int:
    """How many env-prep install ROUNDS this node has already spent, from the log.

    Same defect and the same fix as `_durable_repair_ledger` below, one counter over: `dep_rounds`
    was a loop local starting at 0, so every resume handed the node a fresh `_MAX_DEP_ROUNDS`. The
    bound exists to stop an offline or misnamed package looping, and a bound that resets on re-entry
    does not bound anything across a flapping provider.

    Reconstructible with no new field, because `deps_installed` already carries `round` — it is a
    DIAGNOSTIC event, so the fold ignores it and reading it here is a raw-log read exactly like the
    repair ledger's. `max` rather than a count: the round number is the authority, and a duplicated
    append (a crash between `store.append` and `continue`) must not inflate it.

    Generation-keyed exactly as `replay._generation_matches` keys it — an absent stamp binds, a
    present-but-mismatched one is rejected — so a `node_reset` genuinely starts a fresh env budget.
    That "exactly" is a CALL, not a claim: see `_durable_row_belongs`, and the three raw values the
    hand-spelled version disagreed with the fold about.
    """
    rounds = 0
    for e in events or []:
        if e.type != EV_DEPS_INSTALLED:
            continue
        d = e.data or {}
        if not _durable_row_belongs(d, node_id, generation):
            continue
        n = _durable_int(d.get("round"), default=None)
        rounds = max(rounds, n) if n is not None else rounds + 1
    return rounds


def _durable_rollbacks(events, node_id: int, generation: int) -> set:
    """Which earlier stages this node has ALREADY been rolled back to, from the log.

    The fourth of the family, and the one whose bound is not a count but a SET: a rollback may be
    tried at most once per suspect stage per node (`engine/eval_stages.py::_rollback_start` rung 3),
    which is what makes the feature bounded by the pipeline's own length rather than by a budget.
    Reading it off `stage_rollback` rows is what carries that across a resume — process-local, a
    restarted node would re-offer every stage it had already re-run, which is precisely the thrash
    the rung exists to stop, and the most expensive kind because each retry is a whole stage.

    ONLY the accepted rows count. A refused request cost nothing and consumed nothing, so holding it
    against the node would spend the one allowance on a decision the engine itself made — and would
    make a typo'd stage name permanently un-nameable.

    Old logs contribute nothing, which is honest: a node that never rolled back has no allowance to
    reconstruct.
    """
    out: set = set()
    for e in events or []:
        if e.type != EV_STAGE_ROLLBACK:
            continue
        d = e.data or {}
        if not _durable_row_belongs(d, node_id, generation):
            continue
        if d.get("accepted") is True and d.get("stage"):
            out.add(str(d["stage"]))
    return out


def _durable_full_retrains(events, node_id: int, generation: int) -> int:
    """Full re-trains already spent on this node, from the log.

    The third of the same family, and the only one that needed a new FIELD: nothing was appended
    when a repair forced a full re-train, so `full_retrains` was reconstructible from nothing and a
    resume restored the whole expensive-compute allowance. `inline_repair_retrain_cap` is a guard on
    GPU hours, which is exactly the budget a resume must not refund.

    It could NOT ride on `node_repaired` the way `changed` and `stages_passed` do, and that is worth
    stating because it is the obvious design and it is wrong: that event is appended BEFORE the loop
    asks `_repair_forces_full_retrain`, so the field would carry the count as of the PREVIOUS repair
    and a resume would refund the most recent re-train — precisely the charge this cap exists to
    hold, since it guards GPU hours rather than attempts. So the charge gets its own diagnostic
    event, appended where it is decided, exactly as `deps_installed` records a dep round.

    A log written before that event existed contributes 0, which is the honest reading: we do not
    know that it re-trained, and inventing a charge for an old log would abandon nodes that never
    spent anything.
    """
    spent = 0
    for e in events or []:
        if e.type != EV_FULL_RETRAIN_CHARGED:
            continue
        d = e.data or {}
        if not _durable_row_belongs(d, node_id, generation):
            continue
        n = _durable_int(d.get("spent"), default=None)
        spent = max(spent, n) if n is not None else spent + 1
    return spent


def _durable_repair_ledger(events, node_id: int, generation: int) -> tuple[int, list[dict], int]:
    """This node's repair ledger as the EVENT LOG records it: (attempts, judge rows, unparseables).

    THE COUNTERS ARE THE LOG'S, NOT THE PROCESS'S. `attempt`, `repair_log` and `unparseable_repairs`
    used to be plain loop locals initialized to 0/[] at the top of `_evaluate`, and nothing folded
    them (`events/replay.py::_on_node_repaired` mutates code and files only). So every re-entry —
    `looplab resume`, a crashed process, and above all the operator-resumed pause this design's own
    recovery from a dead judge PRESCRIBES — restarted the hard budget at zero and handed the judge an
    empty history for a node that already had durable repairs. Measured over four real `Engine.run()`
    resumes at `inline_repair_attempts=4`: 8 durable `node_repaired`, attempts [1,2,1,2,1,2,1,2], no
    terminal, and the judge asked with `history_rows=0` while four durable repairs sat in the log. A
    flapping provider therefore loops pause -> resume -> fresh budget forever, and each resume is a
    fresh 4 repairs. That is invariant #3: the repair budget IS a side effect, so it has to be gated
    on the durable events rather than on a process-local integer.

    Reads `node_repaired` directly rather than through `fold`, because what the judge needs — the
    per-attempt error, what each fix claimed, which files it actually touched — is a TRAJECTORY, and
    `RunState` deliberately keeps only the latest code/files. Generation-scoped exactly like the
    fold: a `node_reset` opens a new lifecycle whose budget genuinely starts fresh, and a row from an
    abandoned generation must not charge it. An unstamped row (a log written before generations were
    stamped) binds to the current lifecycle, mirroring `replay._generation_matches`.

    Rows come back in `_format_repair_log`'s shape. `changed`/`stages_passed` are ADDITIVE fields on
    `node_repaired` (invariant #5) and a row written before they existed simply omits `changed`,
    which the renderer distinguishes from "changed nothing".
    """
    attempts = 0
    rows: list[dict] = []
    unparseable = 0
    for e in events or []:
        if e.type != EV_NODE_REPAIRED:
            continue
        d = e.data or {}
        # Keyed exactly as `replay._generation_matches` keys the same event: an ABSENT stamp is the
        # legacy `_MISSING` case and binds to whichever lifecycle is asking, while a stamp that is
        # present and does not match is rejected — including an explicit null, which replay treats as
        # an invalid stamp rather than as "unstamped". Deliberately NOT `d.get("generation") is None`:
        # that spelling would silently admit a corrupt row the fold refuses. Nor is it the hand-rolled
        # `!=` this used to be, which admitted a different corrupt row (`generation: true`) and
        # dropped a live one (`generation: "1"`) — `_durable_row_belongs` CALLS the fold's rules.
        # `legacy_attempt` is not used here for the reason `_event_generation` documents — on
        # `node_repaired`, `attempt` is the inline-repair ordinal, not a generation.
        if not _durable_row_belongs(d, node_id, generation):
            continue
        n = _durable_int(d.get("attempt"), default=None)
        # A SALVAGE CAUSE FIX IS NOT AN ATTEMPT. It is one Developer call on a node whose metric was
        # already recovered, it never re-ran anything, and the loop that wrote it broke immediately
        # afterwards — so charging it here would take a repair away from a node on RESUME that the
        # same node in-process never lost. The budget bounds re-evaluations (see the field comment on
        # `inline_repair_attempts`: "a re-eval costs the same whichever kind of mistake preceded
        # it"), and this row bought none. Its own bound is invariant #3 — once per lifecycle, gated
        # on this very row by `_repair_salvaged_cause`.
        # The ROW still goes into the judge history: "the declaration was corrected here" is real
        # evidence about the node's trajectory, and hiding it would leave the judge reading a gap.
        if str(d.get("triage_action") or "") != SALVAGE_CAUSE_TRIAGE_ACTION:
            attempts = max(attempts, n) if n is not None else attempts + 1
        unparseable = max(unparseable, _durable_int(d.get("unparseable_repairs")))
        row = {"attempt": n if n is not None else attempts,
               "error": str(d.get("error_in", ""))[-_JUDGE_ERROR_CHARS:],
               "fix": str(d.get("rationale", ""))[:200],
               "stages_passed": d.get("stages_passed")}
        if "changed" in d:
            row["changed"] = list(d.get("changed") or [])
        # The verification columns are read back the same way and for the same reason, with one
        # extra rule: an ABSENT `verified` key must stay absent. `repair_verify.inert_streak` reads
        # "no key" as "not inert" and breaks the streak on it, so a row from before this column
        # existed — or a `salvage_cause_fix` marker row, which never writes one — can never
        # terminalize a node on evidence nobody recorded. Coercing it to a default here would put
        # that decision back in the one place that cannot tell the two apart.
        if d.get("verified") in REPAIR_VERDICTS:
            row["verified"] = str(d.get("verified"))
            row["unmet"] = [str(u) for u in (d.get("unmet") or [])][:12]
        rows.append(row)
    return attempts, rows, unparseable


def _durable_salvage_cause_fix(events, node_id: int, generation: int):
    """Has this node's lifecycle ALREADY had its salvaged metric's cause fixed, per the log?

    Invariant #3 for `_repair_salvaged_cause`: a paid side effect has to be gated on a durable event
    or a resume repeats it. The window is small and completely ordinary — the `node_repaired` row is
    appended, then the loop breaks, then the terminal is written under a different lock acquisition —
    and a process that dies inside it leaves a node with the fix committed, no terminal, and (before
    this) a resume that re-evaluated, re-salvaged and bought a SECOND Developer call to make the same
    edit. The row is keyed exactly as the other durable ledgers key theirs (`_durable_row_belongs`),
    so a `node_reset` genuinely earns a fresh fix for the new lifecycle.

    Returns the ROW, not a bool, because the resumed caller has to answer two different questions
    from it: "was this already paid for?" (any row) and "did it actually change anything?" (whether
    the row carries an edit). A receipt for a call that produced NO change must not read back as a
    corrected declaration — that would make the resumed terminal's `metric_provenance.cause_repaired`
    claim a fix nobody made.
    """
    for e in events or []:
        if e.type != EV_NODE_REPAIRED:
            continue
        d = e.data or {}
        if not _durable_row_belongs(d, node_id, generation):
            continue
        if str(d.get("triage_action") or "") == SALVAGE_CAUSE_TRIAGE_ACTION:
            return d
    return None
from looplab.events.replay import fold
# The fold's OWN generation rule, CALLED rather than re-derived — `_durable_row_belongs` above is
# the single place the durable ledgers key a raw row, and it must agree with `replay` by
# construction. Public on purpose (see its docstring): the alternative was importing the private
# reader plus its `_MISSING` sentinel across the package boundary.
from looplab.events.replay import event_generation_binds
from looplab.runtime.sandbox import GpuPinUnenforceable
from looplab.events.types import (EV_CARD_DROPPED, EV_DEPS_INSTALLED, EV_FULL_RETRAIN_CHARGED, EV_NODE_ABORT,
                                  EV_NODE_EVAL_STARTED,
                                  EV_NODE_EVALUATED, EV_NODE_FAILED, EV_NODE_REPAIRED,
                                  EV_NODE_RESET, EV_PAUSE, EV_PROXY_SCORED,
                                  EV_REWARD_HACK_SUSPECTED,
                                  EV_SPEC_DRIFT, EV_STAGE_FINISHED, EV_STAGE_ROLLBACK)


def _card_identity_spellings(state, raw_card_id) -> frozenset[str]:
    """Return the unambiguous spellings of one folded Card identity.

    Nodes intentionally retain their immutable proposal-time ``card_id`` while replay collapses
    merged Cards onto the canonical row.  Active controls therefore have to compare identities,
    not just the two raw strings.  A spelling owned by more than one row is excluded, and an
    ambiguous node subject resolves to nothing: cancellation must fail closed.
    """
    cards = getattr(state, "cards", None)
    if not isinstance(raw_card_id, str) or not raw_card_id or not isinstance(cards, Mapping):
        return frozenset()

    owners: dict[str, set[str]] = {}
    for canonical, card in cards.items():
        if not isinstance(canonical, str) or not canonical:
            continue
        spellings = {canonical}
        aliases = getattr(card, "aliases", None)
        if isinstance(aliases, list):
            spellings.update(alias for alias in aliases if isinstance(alias, str) and alias)
        for spelling in spellings:
            owners.setdefault(spelling, set()).add(canonical)

    subject_owners = owners.get(raw_card_id, set())
    if len(subject_owners) != 1:
        return frozenset()
    subject = next(iter(subject_owners))
    return frozenset(
        spelling for spelling, spelling_owners in owners.items()
        if spelling_owners == {subject}
    )


def _workdir_manifest_digest(node) -> str:
    """Digest of the node source manifest a workdir was materialized from.

    Module-level on purpose: `_evaluate` takes a lazy `import hashlib` further down, which would make
    `hashlib` an unbound function-local for any closure defined above it.
    """
    return hashlib.sha256(orjson.dumps(
        {"attempt": node.attempt, "code": node.code,
         "files": node.files or {}, "deleted": sorted(node.deleted or [])},
        option=orjson.OPT_SORT_KEYS)).hexdigest()


def _repair_provider_failure(node_code: str, new_code, repaired_files, repaired_deleted,
                             unparseable_repairs: int) -> tuple[Optional[str], int]:
    """Did the repair CALL fail at the provider, and how many not-Python answers has it given?

    A pure rule with a name (doc 25 ES-03) because the property it decides has already cost a real
    run: as ~50 lines in the middle of `_evaluate`'s attempt loop, the only way to observe any of
    its four branches was to drive a whole sandboxed eval against a dead endpoint. It returns the
    provider-failure message (or None) and the UPDATED unparseable counter — the counter round-trips
    through the return value rather than being mutated in place, so a caller that forgets to carry
    it back is a name the caller has to bind, not a silently-frozen count.
    """
    # DOES THE ARTIFACT LOOK LIKE THE THING IT REPLACES? Only asked when the whole-file
    # `code` really is what this repair shipped: a repo/multi-file repair returns "" and
    # carries its work in `files`/`deleted`, and a node whose own code is empty never had
    # a whole-file artifact to begin with. `engine/triage.py::repair_artifact_defect`
    # documents the two answers and why they are treated differently.
    _artifact_defect = ""
    if not repaired_files and not repaired_deleted and (node_code or "").strip():
        _artifact_defect = repair_artifact_defect(new_code)
    # A REPAIR THAT DID NOT PRODUCE A REPAIR. The Developer returns the in-band
    # "(developer error: …)" sentinel when its OWN session failed — an unreachable
    # endpoint, a 401, a 402 "out of credits" — so `new_code` is a provider/transport
    # error message, not code. Nothing downstream could tell the difference: the sentinel
    # was committed as the node's code by `node_repaired`, re-materialized into the
    # workdir, and re-evaluated; the eval then failed with a fresh error, so the loop
    # simply asked again. A dead OpenRouter account produced 2343 such "repairs" on ONE
    # node at ~11/min for 3.5 h, each one a full re-eval.
    #
    # A provider failure is not a code defect, so it must not drive the code-repair loop.
    # No `node_repaired`, no attempt spent, no files written — the loop breaks here and
    # the node terminalizes ONCE below with reason="developer_crash" naming the provider
    # failure, and the run-level circuit breaker fires. (Deliberately NOT committing the
    # sentinel as node.code also keeps the recovery sweep's `_developer_sentinel` scan,
    # which keys on exactly that, from later re-terminalizing this node.)
    #
    # `is_developer_error` recognises exactly ONE shape of this, LoopLab's own sentinel,
    # produced by `adapters/repo_developer.py` alone. Two more shapes reach here:
    #   * a repair that RAISED — normalized into the sentinel at the call above, so it
    #     arrives here already wearing the shape this branch understands;
    #   * a repair that answered with PROSE. When the prose PARSES — a comment-only or
    #     docstring-only answer — the eval exits 0 with no metric, and the node used to
    #     terminalize as `no_metric`, telling the operator "the command printed no metric"
    #     about a provider that is dead, with no pause. `"no_code"` is the engine's own
    #     proof of the same fact the sentinel asserts: an artifact whose module body can
    #     never execute cannot be a repair, whoever wrote it.
    #
    # The remaining answer, `"unparseable"`, keeps today's behaviour of committing the
    # artifact and letting the next eval's SyntaxError inform the next repair — which is
    # how a TRUNCATED generation recovers, and stopping a node on one truncation would be
    # a regression. It is counted DIRECTLY (not inferred from the error text, which can
    # carry a varying provider request id and so looks new every time) and becomes the
    # provider verdict once a repair call has answered with something that is not Python
    # `_UNPARSEABLE_REPAIR_LIMIT` times on one node.
    if _artifact_defect == "unparseable":
        unparseable_repairs += 1
    _dev_err = None
    if is_developer_error(new_code):
        _dev_err = str(new_code)[:400]
    elif _artifact_defect == "no_code":
        _dev_err = ("the repair returned no executable code, only text: "
                    + " ".join(str(new_code).split())[:200])
    elif unparseable_repairs >= _UNPARSEABLE_REPAIR_LIMIT:
        _dev_err = (f"the repair has now returned something that is not valid Python "
                    f"{unparseable_repairs}x — the last one began: "
                    + " ".join(str(new_code).split())[:160])
    return _dev_err, unparseable_repairs


def _repair_change_set(prev_files, prev_deleted, repaired_files,
                       repaired_deleted) -> tuple[set, list]:
    """THIS repair's real change set: files whose content moved, plus its own deletions.

    Named (doc 25 ES-03) because both halves are DELTAS against the pre-repair node and the reason
    is not visible from the expression — a cumulative read of either silently disables checkpoint
    reuse for the rest of the node's life, which is a cost regression no test of the repair loop's
    outcome would notice.
    """
    # The repair's REAL change set = files whose content actually differs from the pre-repair
    # node (last_files is cumulative — see prev_files above), plus THIS repair's deletions.
    changed = {f for f, c in repaired_files.items() if prev_files.get(f) != c}
    # Deletions likewise get the delta, not the cumulative set: a deletion that predates
    # the completed train stage cannot invalidate its checkpoint — the stage already ran
    # (and passed) without that file on disk. Blocking on the cumulative `repaired_deleted`
    # (seeded from node.deleted at repair_from) would permanently disable stage reuse for
    # any node whose implement ever deleted a file; only THIS repair's deletions can
    # invalidate the checkpoint, so only they enter the reuse decision.
    new_deleted = [d for d in repaired_deleted if d not in prev_deleted]
    changed |= set(new_deleted)
    return changed, new_deleted


def _repair_forces_full_retrain(res, next_start, *, rolled_back: bool = False) -> bool:
    """Does this repair discard completed EARLIER-stage work, i.e. does it count against the cap?

    Three conditions with one meaning, which is why they are a named rule (doc 25 ES-03) rather
    than a compound `if` carrying fifteen lines of comment in the middle of the attempt loop.

    `rolled_back` is the FOURTH condition and the reason this rule was worth extending instead of
    giving rollback an accounting of its own. The question the cap asks is "is this repair throwing
    away completed expensive work?", and a rollback is a yes by construction — `_rollback_start` only
    ever accepts a suspect STRICTLY EARLIER than the failed stage, so an accepted rollback always
    discards at least one stage that had already run to completion, plus everything after it. It is
    therefore the same charge on the same ledger (`inline_repair_retrain_cap`, guarding GPU hours),
    and a second counter would let a Developer alternate rollback / full-retrain and pay neither cap.
    It is checked FIRST because the existing three conditions cannot see it: a rollback leaves
    `next_start` set to the suspect's name, so `next_start is None` is False and the historical rule
    reads an accepted rollback as free.
    """
    # Count a full re-train against the cap ONLY when completed EARLIER-stage work is being
    # discarded: a LATER stage failed yet reuse was refused because the repair could
    # have changed an earlier stage. A first-stage failure (nothing to reuse) or a single-
    # command eval is an ordinary retry, bounded by the attempt budget like any other — NOT the
    # retrain cap (mirrors config.py: "only a repair that changes an EARLIER stage's code
    # forces a full re-train ... counted"). The CALLER checks this BEFORE incrementing so cap=N
    # runs exactly N.
    # First-vs-later is judged from the PRE-repair `res.stages` (one record per stage that
    # ran, in order, the failed stage always LAST) — never from the failed stage's index in
    # the POST-repair `_stages`: a repair that renames/drops the failed stage (or a
    # _resolved_stages exception fallback to []) loses that index (-1) for FIRST- and
    # LATER-stage failures alike. A renamed LATER stage still discards completed
    # earlier-stage work on the forced full re-run, so it keeps consuming the cap (the
    # point of counting the renamed case at all — leaving it uncounted let a
    # stage-renaming repair burn unlimited full trains); a renamed FIRST stage never had
    # earlier work to discard, so it must stay an ordinary retry.
    if rolled_back:
        return True
    was_first = len(res.stages or []) <= 1
    return bool(res.failed_stage) and not was_first and next_start is None


class SpeculativeEvaluationInvariantError(AssertionError):
    """A speculative build reached an evaluation without a confirmed selection. See below."""


class EvaluateMixin:
    """The engine's eval-task cluster. See the module docstring for the mixin convention
    (`self` is the Engine)."""

    # METRIC SALVAGE policy — see `engine/metric_salvage.py::METRIC_SALVAGE_MODES` for the three
    # rungs and for why the default is the conservative one.
    #
    # These stay CLASS attributes and are what `Engine.__init__` assigns the settled `_opt` values
    # to — the same shape the mixin was written with, so a test or a resumed Engine subclass can
    # still set them directly without going through Settings.
    #
    # A CLASS DEFAULT IS ALSO WHAT MADE THE WIRING UNTESTABLE, which is worth saying beside it: a
    # test that ASSIGNS the attribute after construction passes whether or not `__init__` ever reads
    # `metric_salvage` — measured, commenting out those two assignments left 29 cases green while
    # `Engine(metric_salvage="off")` silently ran `audit`. Drive the policy through the
    # CONSTRUCTOR (`tests/test_engine_options.py::test_the_salvage_policy_reaches_the_engine_*`,
    # `tests/test_metric_salvage.py::_drive`), never by setting the attribute.
    metric_salvage: str = DEFAULT_METRIC_SALVAGE
    # Whether a salvaged node still asks the Developer to fix the CAUSE (the broken declaration).
    # Its bounds are its OWN and are enumerated in `_repair_salvaged_cause` — the two feature flags,
    # `inline_repair_reasons`, a repair-capable Developer, and once per lifecycle gated on its own
    # event. It deliberately does NOT spend an inline-repair attempt: that budget bounds
    # re-evaluations and this fix buys none.
    metric_salvage_repair: bool = True

    def _assert_speculative_selection_confirmed(self, state, node) -> None:
        """INVARIANT: a speculative build must never consume an evaluation before its selection
        is confirmed.

        This is the load-bearing premise of admitting positive ``speculation_depth`` on real
        Dataset/Repo/Command workloads (see the admission block in `engine/orchestrator.py`): a
        prediction that misses is thrown away BEFORE it runs, so a miss costs one Developer call and
        zero GPU seconds, and its node-budget slot is refunded. Every part of that argument collapses
        the moment a speculative node can reach the sandbox on an unconfirmed selection: the miss
        would then be real GPU time on a real training run, which the argument does not cover and
        which MUST NOT be admitted on it. (The harm did not go to zero when the refund landed — a
        measured A/B put `mean_normalized_regret` at 0.0026 rather than 0.0256 — so the cheap
        regression signal stays too: `speculation_budget_observation` in the run's `budget` receipt.)

        THE SAME PREDICATE, both sides of the lifecycle. Confirmation is the durable
        ``card_build_done`` link (`state.speculative_nodes`) binding this exact attempt-zero lifecycle
        to the Card it was built for — the pre-terminal half of the very fact
        `core/models.py::is_unevaluated_speculative_discard` proves post-terminal before it
        refunds a slot (both go through `_durable_speculative_lifecycle`). This method deliberately
        introduces NO third spelling: it calls `SpeculationMixin._speculative_link_matches`, the
        engine's own one, already used by `_run_card_session`'s admission gate.

        The producer appends the link only after the consumer claims the build as the selection it
        actually wanted, and `_run_card_session` re-runs `speculative_card_is_fresh` immediately
        before dispatch — so the link is the durable, replay-visible half of a confirmation the
        session has already re-checked in memory. Asserted HERE because `_evaluate` is the single
        funnel every evaluation passes through — the card session, the ordinary dispatcher, recovery
        and direct library callers all arrive here — so no future path can reach a sandbox around it.

        Spelled as an explicit raise rather than `assert`: `python -O` strips assert statements, and
        an invariant whose whole purpose is to stop unbudgeted GPU spend must not be optimized out.
        """
        if getattr(node, "speculative", False) is not True or node.attempt != 0:
            return
        if not self._speculative_link_matches(state, node):
            raise SpeculativeEvaluationInvariantError(
                f"speculative node {node.id} reached evaluation without a confirmed selection "
                "(no matching card_build_done link); refusing to spend evaluation budget on an "
                "unconfirmed prediction"
            )

    def _record_eval_start_boundary(self, node) -> bool:
        """Append the durable eval-START boundary for one lifecycle, at most once.

        THE ONE SPELLING, called from two places on purpose. The dispatch decision belongs to the
        MAIN task (`_run_card_session`'s admission), and writing it there is what keeps it out of the
        speculative election's compare-and-swap window: `_request_card_build` reads the log, consults
        the Card scorer, then appends with `expected_last_seq`, and a row appended by a WORKER inside
        that window makes the election lose its CAS. Doing that once per eval defeated the prefetch
        request every single turn — a depth-1 treatment run silently became serial (measured: 17
        nodes built / 5 discarded became 12 / 0, with zero producer/consumer overlap left).
        `_evaluate` still calls it, because `_evaluate` is the single funnel every evaluation passes
        through and the boundary must exist before ANY caller reaches a sandbox — recovery and direct
        library callers included. In a card session that call is already satisfied by the folded flag
        and appends nothing.

        Unlocked, like `_record_card_build_attempt`. That is safe because it is ONE independent
        per-node row the fold keys by (node_id, generation) and applies SET-ONLY, AND because both
        writers are on the main task *after* that node's own `node_created` — not because its position
        is immaterial. This row is NOT splice-neutral, and saying it "pairs with nothing, so its splice
        position cannot change any other event's meaning" (as this docstring did until 2026-08-06) is
        false: `_on_node_eval_started` silently DROPS a row whose node does not exist yet, and
        `Node.eval_started` is one of the durable facts
        `core/models.py::is_unevaluated_speculative_discard` proves the Layer-5 budget refund from —
        which `node_counts_toward_card_budget` reads, which BOTH the L3 budget and the fold's debug
        anchor (`events/card_ledger.py::_card_debug_leaf_children`) read. So splicing it before rather than
        after its own `node_created` measurably flips a DIFFERENT Card's `selection_ready`: measured
        `{budget 2, leafs [2,3], later Card ready}` vs `{budget 3, leafs [3], not ready}`. Not reachable
        today (both writes are main-task and node-created-first), which is exactly why this is written
        down as an ordering PRECONDITION rather than left as a property of the event.

        COST, for whoever re-runs `looplab speculation-gate`: one append is one flush+fsync, which on
        a network/FUSE run dir is not free — measured on this box at ~200 ms against ~1 ms on local
        disk. Against a real evaluation that is noise (a training run is minutes), but it is the same
        order as the calibration harness's ~300 ms toy eval, and there it can flip the
        producer/consumer race the benchmark measures: on a FUSE run dir seed 2 closes prefetches as
        `stale` at commit instead of creating and discarding them, which the clean-protocol validator
        refuses to score. On a local-disk run dir the A/B reproduces its published aggregates exactly
        (`mean_normalized_regret` 0.002590, `mean_hit_rate` 0.751961). Put the calibration run dirs on
        local disk.
        """
        if (getattr(node, "eval_start_boundary", False) is not True
                or getattr(node, "eval_started", False) is True):
            return False
        self.store.append(EV_NODE_EVAL_STARTED, {
            "node_id": node.id, "generation": node.attempt})
        return True

    async def _auto_pause_provider_failure(self, what: str) -> None:
        """RUN-level circuit breaker for "an LLM the repair loop depends on did not answer".

        ONE spelling, because there are now two ways to learn it inside a single eval — the
        Developer's repair call returned/raised a provider failure, and the crash-triage judge could
        not answer — and they must behave identically. Both are RUN-level conditions: every other
        node reaches the same endpoint, so continuing merely re-spends the node budget on nodes that
        cannot be built or cannot be judged. Mirrors the build path's "PAUSE on the FIRST
        developer_crash" breaker (`_create_node`), so the operator learns "your endpoint is out of
        credits" from one pause reason instead of by reading thousands of repair events.

        The pause carries no `node_id`: the fix is to the provider, not to this node, so it must not
        be clearable by a node reset — and a run-level pause folds as a monotone latch
        (`replay.py::_on_pause`), which keeps it order-tolerant against every sibling eval appending
        concurrently. Appended under `_write_lock` with the same already-halting re-check
        `confirm_phase.py::_pace_confirm_refusal` uses for its own auto-pause, so a run that is
        already paused/finished/stopping — or a batch of siblings all hitting the same dead
        endpoint — collects exactly one.
        """
        if self._run_halt_intent():
            return
        async with self._write_lock:
            if self._run_halt_intent():
                return
            self.store.append(EV_PAUSE, {
                "reason": f"auto-paused: {what}. Every other node reaches the same endpoint; fix it "
                          "(credits, key, base URL, or the endpoint itself) and resume."})

    @property
    def _probe_developer(self):
        """Developer used for ablation *probes* (I7): the raw inner developer, bypassing
        any ValidatingDeveloper's retry/fallback. Probes are a measurement harness, not a
        shipped step — routing them through validation would (a) substitute the LLM
        fallback mid-measurement, corrupting impact numbers, and (b) multiply expensive
        external-agent calls by len(params) per ablation (ADR-7 cost rule)."""
        return getattr(self.developer, "inner", self.developer)

    def _trust_gate_signals(self, node, scan_src: str) -> list[dict]:
        """The leakage + critic half of a node's trust findings, as a rule with a NAME.

        These two concatenations were inline in `_evaluate`, ~40 lines inside a `_write_lock` block
        reachable only by finishing a real sandboxed evaluation. A mutation audit (2026-08-05) took
        the measure of what that cost: dropping the `sigs +=` on both calls — leaving the calls
        themselves, so `test_trust_finding_namespaces.py`'s source pins still match — kept 117 tests
        across the eight trust/leakage/critic/signal files green. `sigs` stays empty, `if sigs:`
        never fires, no `reward_hack_suspected` is written, and every downstream gate
        (`is_hard_signal`, `_apply_trust_gate`, the Trust panel, the folded `state.reward_hacks`)
        sees the clean run of a node that was never looked at. `looplab/trust/` exists precisely so
        a run cannot report clean because nothing looked.

        `test_trust_gates_reach_the_ledger.py` catches that end to end, and keeps doing so — this is
        the cheap half of the same guard: a rule a unit test can call directly with a node and a
        source string, instead of one that can only be observed by driving a whole run.

        Returns the findings; it does NOT append. The caller owns the event (the reward-hack
        detectors' own signals concatenate ahead of these, and one `reward_hack_suspected` carries
        the union), so this stays a pure function of `(self._code_leakage_detect, self._critic_check,
        node.idea, scan_src)` and the graded-output name.
        """
        sigs: list[dict] = []
        # Both detectors emit their OWN namespaced signals (doc 25 CT-10). This used to
        # mint `data_leakage:`/`critic:` here, which put the string `is_hard_signal`
        # gates on three files away from the detector that knows what it found.
        if self._code_leakage_detect and scan_src:
            from looplab.trust.leakage import code_leakage_findings
            sigs += code_leakage_findings(scan_src)
        if self._critic_check and scan_src:
            from looplab.trust.critic import critic_findings
            # Host-graded tasks (MLE-bench &c.) score a submission file out-of-process,
            # so the critic's in-code `metric` checks don't apply — hand it the expected
            # submission filename so it checks the right output contract instead.
            sigs += critic_findings(node.idea, scan_src,
                                    submission_file=self._graded_output_name())
        return sigs

    def _trust_scan_surface(self, node) -> str:
        """The exact bytes every trust detector reads for one node — and the bytes `code_digest`
        commits to. A rule with a name because the two are the SAME string by construction: a
        caller that re-derived the surface for the digest could hash something the scans never saw.
        """
        # Scan the WHOLE solution surface, not just solution.py — a patch-gated multi-file
        # agent can hide answer-key access / leakage / the real computation in an in-surface
        # helper module that solution.py imports. Concatenate node.files so the reward-hack /
        # leakage / critic scans cover the imported code too (not only the clean entrypoint).
        return node.code + "".join(
            f"\n\n# --- {fn} ---\n{src}" for fn, src in (node.files or {}).items()
            if str(fn).replace("\\", "/").lower() != "solution.py")

    def _trust_scan_signals(self, node, res, state, workdir, scan_src: str) -> list[dict]:
        """Every trust finding for one evaluated node, in the order the union event carries them.

        The reward-hack half (detectors + the hardened exploit suite + the workdir write audit)
        followed by `_trust_gate_signals`' leakage/critic half. Extracted from `_evaluate` (doc 25
        ES-03) for the reason its sibling was: as ~45 inline lines inside the terminal's
        `_write_lock` block, the only way to observe that any of it ran was to drive a whole run.

        ORDER IS PART OF THE CONTRACT — the reward-hack signals concatenate AHEAD of the gate
        signals, and one `reward_hack_suspected` carries the union, so a reader of the stored
        evidence sees the same sequence it always did.

        Returns the findings; it does NOT append. The caller owns the event, because the payload
        also binds the schema version and the digest of `scan_src` (see the call site).
        """
        sigs: list[dict] = []
        if self.reward_hack_detect:
            from looplab.trust.reward_hack import detect_reward_hacks
            protected = set(self._repo_spec.get("protected_names", [])) | set(self._assets)
            # The grader-IMPORT waiver keys on the task genuinely MATERIALIZING
            # grader.py (an ASSET → calling `grader.score(...)` is the documented
            # grading contract, e.g. the in-workdir mlebench brief). Pass it explicitly
            # instead of letting the detector infer it from `protected`: that union also
            # carries the operator's protect list, and a merely-PROTECTED grader.py
            # (protect=["grader.py"], no asset) means "hands off", not "import me" —
            # inference from the union would wrongly waive the import tells for it.
            sigs += detect_reward_hacks(
                scan_src, res.metric, state.direction,
                protected_names=protected, stdout=res.stdout,
                # Match the asset key NORMALIZED (path separators + case), exactly like
                # the detector normalizes `protected_names` — the inference this call
                # replaced got that normalization for free, so 'Grader.py' or a
                # backslashed key must keep sanctioning the import here too.
                grader_import_ok=any(str(a).replace("\\", "/").lower() == "grader.py"
                                     for a in (self._assets or ())))
            # 4.3: also apply the hardened exploit ruleset grown by `looplab harden`
            # (hacker-fixer-solver) — each previously-discovered exploit stays guarded.
            if self._exploit_suite is not None:
                sigs += self._exploit_suite.scan(scan_src)
            # 4.4 sandbox instrumentation (RewardHackingAgents recipe): flag RUNTIME
            # writes to protected/frozen files — behavioral evidence a static scan of the
            # code can miss (a write via a helper, os.system, a template). Compares the
            # workdir against the assets/protected set the engine placed there.
            if self._workdir_audit:
                sigs += self._audit_workdir_writes(workdir, protected)
        # …and the leakage + critic gates, which are a NAMED rule (`_trust_gate_signals`)
        # rather than two more `sigs +=` lines: as inline concatenations, silencing them
        # was invisible to every trust test that does not drive a whole run. See that
        # method's docstring.
        sigs += self._trust_gate_signals(node, scan_src)
        return sigs

    def _eval_intervention_seen(self, node_id: int, generation: int, start_seq: int,
                                card_id) -> str | None:
        """The ONE post-start intervention this eval's watcher has seen, or None.

        A method rather than a closure inside `_evaluate` (doc 25 ES-03): it needs only the
        lifecycle it is watching, and as a closure it was 58 lines in the middle of the attempt
        loop where nothing could call it directly. `card_id` is passed in rather than read off
        `node` because the caller holds the node — within one attempt `node` is not rebound until
        after the task group closes, so reading it once up front is the same value the closure saw.

        Runs in a worker THREAD (`_watch_for_intervention`'s tick), so it only reads.
        """
        intervention = None
        current_events = self.store.read_all()
        operator_drop_ids: list[str] = []
        for e in current_events:
            if e.seq <= start_seq:
                continue
            if e.type == EV_CARD_DROPPED:
                # Only the explicit operator stop affordance is an active cancel.
                # Engine/freshness drops deliberately burn to terminal as evidence.
                drop_id = e.data.get("id")
                if (isinstance(drop_id, str) and drop_id
                        and e.data.get("dropped_by") == "operator"):
                    operator_drop_ids.append(drop_id)
                continue
            if e.data.get("node_id") != node_id:
                continue
            raw_generation = e.data.get("generation")
            # Controls name the lifecycle they intend to mutate. Missing stamps are
            # legacy generation-0 only; a stale gen-0 click must never cancel a gen-1
            # worker merely because the numeric node id was reused after reset.
            if raw_generation is None:
                if generation != 0:
                    continue
            else:
                if isinstance(raw_generation, bool):
                    continue
                try:
                    event_generation = int(raw_generation)
                except (TypeError, ValueError, OverflowError):
                    continue
                if (isinstance(raw_generation, float)
                        and not raw_generation.is_integer()):
                    continue
                if event_generation != generation:
                    continue
            if e.type == EV_NODE_RESET:
                return "reset"
            if e.type == EV_NODE_ABORT:
                intervention = "abort"
        if (intervention is None and operator_drop_ids
                and isinstance(card_id, str) and card_id):
            # Fold only once an explicit post-start operator drop exists AND this node
            # actually carries a Card identity to match — a card-less worker can never
            # be a drop target (`_card_identity_spellings` returns nothing for a missing
            # id), so skipping the fold there is behaviour-preserving, not just cheaper.
            # This follows merge chains added before or during the eval without making
            # every 300 ms watcher poll replay the complete run. Any corrupt/ambiguous
            # ownership is deliberately a no-op rather than a kill of the wrong worker.
            try:
                active_spellings = _card_identity_spellings(
                    fold(current_events), card_id)
            except Exception:  # noqa: BLE001 - active cancellation must fail closed
                active_spellings = frozenset()
            if any(drop_id in active_spellings for drop_id in operator_drop_ids):
                intervention = "card_drop"
        return intervention

    async def _watch_for_intervention(self, node_id: int, generation: int, start_seq: int,
                                      card_id, cancel, seen: dict) -> None:
        """Poll for a mid-eval intervention and, on the first one, cancel the in-flight eval.

        The verdict travels back to `_evaluate` in `seen["kind"]` rather than through `nonlocal`,
        which is what lets this be a method at all (doc 25 ES-03). Same shape as the watchdogs'
        `kill_signal`, and for the same reason: a sibling task in the eval's task group cannot
        rebind the driver's locals, so the ONE thing it decides is handed over in a dict the driver
        reads after the group closes. Writes `kind` at most once — the loop returns on the first
        non-None verdict — so the driver never has to reconcile two.
        """
        while True:
            await anyio.sleep(0.3)
            if cancel.is_set():
                return
            # Its OWN limiter, never anyio's shared default. Every `run_sync` in the
            # engine draws on that shared 40-token pool, and each in-flight eval's
            # `_run_eval` worker holds a token for the eval's whole (often multi-hour)
            # duration — so at `eval_parallel` near or above 40 (the config allows up
            # to 1024; 0 = AUTO = GPU count) the evals pin every token and this tick
            # queues BEHIND them. Operator abort/reset and both watchdog kills then go
            # blind until an eval finishes on its own, and over-admitted evals sit on
            # reserved GPUs while queued. A tick is a short poll, so a small dedicated
            # pool is always immediately available for it.
            intervention = await anyio.to_thread.run_sync(
                self._eval_intervention_seen, node_id, generation, start_seq, card_id,
                limiter=_watch_limiter())
            if intervention is not None:
                seen["kind"] = intervention
                cancel.set()
                return

    def _eval_failure_text(self, res) -> str:
        """The ONE description of a failed eval — the repair prompt, `node_repaired.error_in`, the
        judge's history rows and the terminal's `error` field are all this string.

        A named rule (doc 25 ES-03) because it is the engine's only account of what went wrong, and
        both of the branches below were retrofitted after a run had already been misdiagnosed by
        the text they replaced. As inline lines in the attempt loop neither could be exercised
        without a real sandbox producing exactly the right stderr.
        """
        # A clean run (exit 0) with no parseable metric is the most confusing failure for the
        # repair agent — the terse "no_metric" gave it nothing to fix, so the debug node just
        # re-ran and failed again. Tell it EXACTLY what the eval reads (the configured metric
        # key + the one line it must print), so a no-metric node can actually be repaired.
        _ms = (self._eval_spec.get("metric") or {}) if isinstance(self._eval_spec, dict) else {}
        _mk = _ms.get("key", "metric")
        _no_metric_hint = (
            f" — the command ran cleanly (exit 0) but printed NO parseable metric. The eval reads"
            f" a stdout JSON line for key {_mk!r}; the entrypoint MUST print exactly one line like"
            f" print(json.dumps({{{_mk!r}: <float>}})) as its last stdout."
            if _ms.get("kind", "stdout_json") == "stdout_json"
            else " — ran cleanly but produced no parseable metric (check the eval's metric reader).")
        # BLANK-BUT-TRUTHY stderr takes the fallback too. `"  \n \t "` is truthy, so it used
        # to survive this `or` and become the node's WHOLE diagnosis: the repair prompt, the
        # `node_repaired.error_in` audit row and the terminal's `error` field were all
        # whitespace. (It also normalized to the empty error signature, which the anti-stuck
        # guard of the time read as an unconditional exemption — 1055 repairs in a 60 s wall
        # with no terminal. That guard is gone, but a failure the engine cannot describe is
        # still the worst thing to hand a judge that decides on the failure text.) Deciding on
        # the STRIPPED text while keeping the unstripped bytes when there is content leaves
        # every non-blank tail byte-identical.
        _stderr_tail = self._redact(res.stderr[-500:])
        _text = (_stderr_tail if _stderr_tail.strip() else "") or (
            f"metric drift: {res.drift}" if res.drift is not None else
            f"exit={res.exit_code} timed_out={res.timed_out} no_metric{_no_metric_hint}"
        )
        # WHICH STAGE BROKE, KEPT WHERE THE READER WILL SEE IT. `command_eval._run_stages` writes its
        # `stage '<name>' failed:` marker at the FRONT of `res.stderr`, and the 500-char TAIL above
        # cuts it off for every failure whose stderr is longer than that — i.e. for every real
        # traceback. So the four things this string IS (the repair prompt, `node_repaired.error_in`,
        # the judge's history rows, the terminal's `error`) stopped naming the failing stage exactly
        # when the pipeline had more than one thing that could have broken. Measured on
        # runs/rubert-dr-0807: 9 of 12 `node_repaired` rows carried no stage name at all, including
        # every `train` failure on a node whose FIRST stage was a Developer-declared `mine`. Nothing
        # else on the repair path carries it — `Developer.repair` receives this text and nothing more
        # (the `failed_stage` column exists only on the TERMINAL, which is written after the repair
        # loop is over), so the model was left to infer the stage from the traceback's filename. That
        # is not the same fact: two stages can run the same script, a stage can fail before printing
        # anything, and a pipeline bug (a wrong argv in `looplab_stages.json`) has no filename at all.
        # Attached AFTER the slice so the tail keeps its whole 500-char budget, and only when the text
        # does not already name the stage — a short stderr, and every single-command eval (no
        # `failed_stage`), stay byte-identical to what they were.
        _failed = str(getattr(res, "failed_stage", "") or "")
        if _failed and f"stage '{_failed}'" not in _text:
            _text = f"[failed stage: {_failed}]\n{_text}"
        return _text

    def _salvage_eval_metric(self, res, reason: str, workdir, since: Optional[float]):
        """The metric this failed eval already produced, or None — see `engine/metric_salvage.py`.

        A thin engine-side seam over the pure rule, for the two reasons the engine keeps such seams:
        it binds the ONE reader spec salvage is allowed to use (`self._eval_spec["metric"]`, the
        operator's own, never the agent's and never a widened one), and it is a single patchable
        name for the tests that drive the property end to end.

        Best-effort in the strict sense: a salvage read walks the candidate's workdir, and a
        diagnostic must never be the thing that turns a failed node into a crashed RUN. Anything
        raised here degrades to "nothing to salvage", which is exactly today's behaviour.

        It also binds the OPERATOR'S OWN stage list, which is what lets the record say whether the
        bytes the reader read came from a stage the operator declared or from the Developer's
        manifest — the difference `metric_salvage.violation_rows` enforces `select` on.
        """
        spec = self._eval_spec if isinstance(getattr(self, "_eval_spec", None), dict) else None
        if not spec:
            return None                 # the solution.py path has no declared reader to salvage with
        try:
            return salvage_metric(res, reason, spec.get("metric"),
                                  self._salvage_reader_root(workdir), since,
                                  mode=getattr(self, "metric_salvage", DEFAULT_METRIC_SALVAGE),
                                  operator_stages=spec.get("stages") or ())
        except Exception:  # noqa: BLE001 — salvage may never be the thing that fails the eval
            return None

    def _salvage_reader_root(self, workdir) -> str:
        """The directory the operator's FILE readers are relative to, for a salvage read.

        `run_command_eval` resolves every reader against its `cwd` — `_sandbox_cwd(workdir,
        eval_spec["cwd"])` — not against the node workdir, and the two differ for any task whose
        `cmd.cwd` is a subdirectory. Salvage handed the reader the node workdir, so on such a task a
        declared `file_json` path was looked for one directory up from where the eval writes it: the
        rung would abstain and the relocation scan would search a wider tree than the eval's own.

        Falls back to the workdir when the engine has no workspace bound (the pure-rule tests
        construct a bare stub), because a salvage that cannot resolve its root must degrade to
        today's behaviour rather than raise.
        """
        spec = getattr(self, "_eval_spec", None)
        cwd_spec = spec.get("cwd", ".") if isinstance(spec, dict) else "."
        try:
            return str(self._sandbox_cwd(workdir, cwd_spec))
        except Exception:  # noqa: BLE001
            return str(workdir)

    def _salvage_qualifying_gates(self, salvaged, res, workdir, since: Optional[float]) -> dict:
        """The operator's CONSTRAINTS / extra readers / drift cross-check, applied to a salvaged
        metric — `engine/metric_salvage.py::salvage_gates` owns the rules; this binds the spec.

        A salvaged metric is a metric, and these are the gates that decide whether a metric
        qualifies. `run_command_eval` computes them in its tail, which every early return that
        produces a salvageable failure skips — so before this a salvaged node reached its terminal
        with an EMPTY `violations` list and, under `metric_salvage="select"`, could become champion
        with the operator's hard constraints never applied and the drift cross-check never run.
        """
        spec = self._eval_spec if isinstance(getattr(self, "_eval_spec", None), dict) else None
        if not spec:
            return {"violations": [], "extra_metrics": {}, "drift": None}
        try:
            return salvage_gates(
                spec, salvaged.metric, getattr(res, "stdout", "") or "",
                self._salvage_reader_root(workdir), since,
                enforce_drift=(getattr(self, "eval_trust_mode", "") == "ratify_freeze_drift"))
        except Exception:  # noqa: BLE001 — see `_salvage_eval_metric`: never the thing that fails
            return {"violations": [], "extra_metrics": {}, "drift": None}

    def _recheck_repaired_contract(self, res, node, workdir, salvaged, fix, err: str):
        """The artifact CHECK, re-asked against the CORRECTED declaration — the `metric_provenance`
        of a node that PASSES it, or None.

        BACKLOG F1e, and the engine-side half of `engine/metric_salvage.py`'s RE-CHECK section, which
        owns every rule — read its four admission rules and, in particular, the note on what they
        jointly admit today. This binds the four things only the engine has: the stage the eval
        failed, the pipeline RE-RESOLVED from the workdir the repair just corrected, the root the
        operator's own declarations resolve against, and the freshness floor the original check used.

        It re-checks, it does not re-run: `verify_stage_artifacts` is a handful of `stat` calls
        against a file that is already on disk, which is the entire economic argument — the measured
        case cost 76 GPU-minutes and re-running the stage to confirm a one-line path fix would spend
        them again.

        Best-effort in the same strict sense as the salvage reads beside it: this walks the
        candidate's own workdir and re-resolves an agent-authored manifest, and neither may ever be
        the thing that turns a failed node into a crashed RUN. Anything raised here degrades to "no
        promotion", i.e. exactly the salvage behaviour that shipped before it.
        """
        from looplab.runtime.command_eval import verify_stage_artifacts
        try:
            stage = str(getattr(res, "failed_stage", "") or "")
            # RE-RESOLVED, not the chain this attempt ran: `_resolved_stages` re-reads
            # `looplab_stages.json` out of the workdir, which `_repair_salvaged_cause` has just
            # re-materialized with the correction. That is what makes this a different question from
            # the one the eval already answered — and when the OPERATOR declared the stages
            # (`eval_spec["stages"]` wins over the manifest), the corrected file is not what the eval
            # reads, the same failing declaration comes back, and the check fails again as it should.
            chain = self._resolved_stages(node, workdir)
            # FIRST, and the most categorical: a node is its WHOLE pipeline. A contract failure
            # aborts the run, so on v6 node 3 the declared `merge` stage and the operator's appended
            # `score` never executed and the recovered number is a plain training run's. A corrected
            # path cannot speak for stages that never ran.
            if not declared_pipeline_completed(chain, getattr(res, "stages", None) or (), stage):
                return None
            if not recheckable_salvage(salvaged):
                return None
            changed = (fix or {}).get("changed") or ()
            if not declaration_only_repair(changed, (fix or {}).get("deleted") or (),
                                           (fix or {}).get("code") or ""):
                return None
            since = recheck_floor(res, stage)
            if since is None:
                return None
            expect = recheckable_expect(chain, stage, changed)
            if not expect.get("files"):
                return None
            # THE REPAIR MUST HAVE REACHED THE DECLARATION THAT FAILED, not merely the file it
            # normally lives in. Without this the gate above is a question about a FILENAME, and in
            # operator `cmd.stages` mode — where the failing declaration is in the task snapshot and
            # the manifest is ignored — it passes on a repair that corrected nothing, leaving the
            # promotion to turn on whether a second stat() of an UNCHANGED path answers differently
            # from the first. See `declaration_actually_corrected`.
            if not declaration_actually_corrected(getattr(res, "stages", None) or (), stage, expect):
                return None
            if verify_stage_artifacts(expect, self._salvage_reader_root(workdir), since,
                                      stage=stage) is not None:
                return None
            return declaration_repair_provenance(salvaged, expect["files"], err)
        except Exception:  # noqa: BLE001 — see `_salvage_eval_metric`: never the thing that fails
            return None

    async def _repair_salvaged_cause(self, node, state, workdir, generation: int,
                                     salvaged, err: str, reason: str, attempt: int, stamp) -> tuple:
        """Ask the Developer to fix the CAUSE of a salvaged node's failure, WITHOUT re-evaluating it.

        THE OTHER HALF OF THE ASK, and the half that stops salvage from becoming a second silent
        failure. A node whose metric was recovered but whose `looplab_stages.json` still declares the
        wrong path reads as successful and walks into the identical contract failure on its next
        attempt — an operator `node_reset`, a stage-scoped re-run — having learnt nothing, and its
        children inherit the same broken declaration.

        WHY IT DOES NOT RE-EVALUATE. That is the entire point of having salvaged: the measured case
        cost 76 GPU-minutes, and paying it again to confirm a one-line path fix is the expense this
        whole design exists to avoid. So the repair is committed and the node terminalizes on the
        metric it already has.

        WHAT THAT COSTS, said plainly rather than left for a reader to discover: the node's recorded
        CODE is then not byte-for-byte the code that produced its recorded METRIC. That is why the
        prompt (`metric_salvage.SALVAGE_CAUSE_DIRECTIVE`) forbids touching anything that could alter
        the result, and why the terminal's `metric_provenance` records `cause_repaired` — a reader
        who cares can see that a correction landed after the measurement.

        WHAT ACTUALLY BOUNDS IT, stated as the list it is, because the first version of this
        docstring claimed the inline-repair loop's bounds and the code checked none of them:

          * `metric_salvage_repair` and `inline_repair` — the two feature flags;
          * `inline_repair_reasons` — the operator's own answer to "may this failure class buy a
            Developer call at all". A run narrowed to `("crash",)` was still paying for a cause fix
            on an `expect_failed` node, which is precisely the spend that setting exists to refuse;
          * a Developer that can repair, and something to repair;
          * ONCE PER LIFECYCLE, gated on its own durable `node_repaired` row (invariant #3). A crash
            between that row and the terminal made a resume pay for a SECOND identical Developer
            call; now the row is read back first and the fix is reported as already landed.

        AND NOT the inline-repair ATTEMPT budget, which is the one bound this deliberately does not
        borrow. That budget bounds re-EVALUATIONS — "a re-eval costs the same whichever kind of
        mistake preceded it" — and this fix buys none: it is one Developer call, at most once per
        lifecycle, on a node that already has its number and is about to terminalize. Refusing it
        because the re-evaluation allowance is spent would leave the broken declaration in place on
        exactly the nodes that struggled most, which is the failure this whole half exists to
        prevent. So `attempt` is neither read nor incremented here, and
        `_durable_repair_ledger` excludes `salvage_cause_fix` rows from the attempt count for the
        same reason — otherwise a resumed node silently lost one repair to a fix it never re-ran.

        Returns `(node, attempt, repaired, fix)`, where `fix` describes WHAT was committed —
        `{"changed": [...], "deleted": [...], "code": "..."}`, and `{}` on every path that committed
        nothing. The caller needs all three because which files a cause fix touched decides whether
        its artifact contract may be re-CHECKED or must be re-RUN (`metric_salvage
        .declaration_only_repair`), and because a declared artifact that is itself a file the REPAIR
        wrote must be refused (`metric_salvage.recheckable_expect`). It is returned rather than
        re-derived from the log because the answer must be about THIS call: a resumed node whose fix
        already landed (the invariant #3 early return below) reports `{}` — nothing was corrected in
        this process, so this attempt may claim no re-check on the strength of it. Its next eval runs
        the corrected manifest for real, which is the stronger answer anyway.

        NEVER raises — including on `BudgetExceeded`, which is
        the one exception this method must swallow rather than propagate: `_evaluate`'s callers wrap
        it in try/FINALLY, not except (`orchestrator.py`'s dispatchers), so a budget stop raised out
        of an OPTIONAL best-effort fix leaves the node with NO TERMINAL AT ALL, discards the metric
        the salvage just recovered, and the run re-dies on resume. A budget that has run out means
        "stop spending", and returning here spends nothing; the loop's own budget checks stop the
        run at the next decision point, one that owns no half-written node.
        It never appends a terminal either — the caller owns the node's single terminal event
        (invariant #2).
        """
        if not (getattr(self, "metric_salvage_repair", True) and self._inline_repair
                and reason in self._inline_repair_reasons
                and callable(getattr(self.developer, "repair", None))
                and (node.code or node.files or self._repo_spec)):
            return node, attempt, False, {}
        # INVARIANT #3, and the cheapest possible spelling of it: the fix's own event. Read BEFORE
        # the paid call, from the durable log rather than from a loop local, because the case it
        # covers is a process that died between appending the row and writing the terminal.
        _prior = _durable_salvage_cause_fix(self.store.read_all(), node.id, generation)
        if _prior is not None:
            # Already paid for on an earlier pass. Whether it REPAIRED anything is a separate fact,
            # read back off the row: a receipt written for a call that proposed no change must not
            # resume as "the declaration was corrected". The fix payload is empty either way — the
            # re-check below is about THIS pass's edits, and there are none to re-check on a resume.
            return node, attempt, bool(_prior.get("changed") or _prior.get("code")
                                       or _prior.get("files") or _prior.get("deleted")), {}
        with self.tracer.span("salvage_cause_repair", node_id=node.id, attempt=attempt + 1):
            try:
                new_code = self._repair(node, cause_repair_context(salvaged, err), state)
            except BudgetExceeded:
                # See the docstring: propagating this out of a best-effort fix costs the node its
                # terminal, because the callers' handler is a `finally`, not an `except`.
                return node, attempt, False, {}
            except Exception:  # noqa: BLE001 — a failed cause fix must not cost the salvaged metric
                return node, attempt, False, {}
        # Snapshot the developer's per-call audit state before the next `await`, for the reason the
        # attempt loop does: the developer instance is SHARED across concurrent evals and the write
        # lock below is a checkpoint, so a sibling's repair would otherwise be recorded as this
        # node's edits.
        repaired_files = dict(getattr(self.developer, "last_files", {}) or {})
        repaired_deleted = list(getattr(self.developer, "last_deleted", []) or [])
        # WAS THIS A REPAIR AT ALL? The same four answers as the attempt loop's, through the same
        # rule. A dead provider must not be committed as the node's code here either — but unlike
        # the loop, it does not pause the run: the node HAS its metric, the terminal is about to be
        # written, and a run-level circuit breaker fired from a path that is not asking for a
        # re-evaluation would stop a run over a fix it did not need.
        _dev_err, _ = _repair_provider_failure(node.code, new_code, repaired_files,
                                               repaired_deleted, 0)
        if _dev_err is not None:
            return node, attempt, False, {}
        prev_files = dict(getattr(node, "files", {}) or {})
        prev_deleted = set(getattr(node, "deleted", []) or [])
        changed, _new_deleted = _repair_change_set(prev_files, prev_deleted,
                                                   repaired_files, repaired_deleted)
        if not changed and not repaired_deleted and not (new_code or "").strip():
            # THE MODEL CHANGED NOTHING — but the call was answered and BILLED, so this exit still
            # owes a durable receipt. Without one, `_durable_salvage_cause_fix` had nothing to read
            # and a process that died before the terminal made the resume re-salvage and buy the
            # identical Developer call again, with nothing in folded state bounding the repeat across
            # successive crashes. This is the ONLY such exit: the three above it must NOT receipt —
            # `BudgetExceeded` and a raised repair bought no answer, and `_repair_provider_failure`
            # fires when the Developer's own SESSION failed (dead endpoint, 401, 402), which a resume
            # should retry once the endpoint is back.
            #
            # The row is deliberately fold-NEUTRAL: no `code` key (so `replay._on_node_repaired`'s
            # `d.get("code", n.code)` leaves the artifact alone) and empty `files`/`deleted` (which
            # the fold skips as falsy), so it records the SPEND without asserting an edit. It charges
            # no attempt either, because `_durable_repair_ledger` excludes `salvage_cause_fix` rows
            # from the attempt count while still passing them to the judge history — where
            # `changed: []` is already the spelling that reads as "proposed nothing".
            async with self._write_lock:
                if fold(self.store.read_all()).nodes[node.id].attempt == generation:
                    self.store.append(EV_NODE_REPAIRED, {
                        "node_id": node.id, "generation": generation, "attempt": attempt,
                        "files": {}, "deleted": [], "error_in": err,
                        "triage_action": SALVAGE_CAUSE_TRIAGE_ACTION,
                        "rationale": (f"metric salvaged ({salvaged.source}); the Developer proposed "
                                      f"no change to the failing declaration"),
                        "changed": [], "stages_passed": None,
                        "salvaged_metric": salvaged.metric})
            return node, attempt, False, {}   # billed, receipted, nothing to commit
        async with self._write_lock:
            # A reset that landed while the repair call was in flight owns the next lifecycle. Skip
            # the commit rather than adopting it — the caller's terminal is already stale-generation
            # and the fold will charge only its eval_seconds.
            if fold(self.store.read_all()).nodes[node.id].attempt != generation:
                return node, attempt, False, {}
            _payload = {
                "node_id": node.id, "generation": generation,
                # The ORDINAL this row FOLLOWS, not a new one. It is not an inline-repair attempt —
                # nothing re-ran — and the ledger keys the budget off `triage_action` rather than off
                # this number, which stays so the judge history reads in order.
                "attempt": attempt,
                "files": repaired_files, "deleted": repaired_deleted,
                "error_in": err,
                # A DISTINCT verdict, not "repair". `triage_action` is what tells a later reader (and
                # `_durable_repair_ledger`'s judge history) what each row was FOR, and a cause fix
                # that was never re-evaluated is not the same event as a repair the loop then tested.
                # It is also what the ledger and the resume gate key on — hence the shared constant.
                "triage_action": SALVAGE_CAUSE_TRIAGE_ACTION,
                "rationale": (f"metric salvaged ({salvaged.source}); fixing the declaration that "
                              f"failed, without re-evaluating"),
                "changed": sorted(changed)[:12] or (["<whole-file solution>"] if new_code else []),
                "stages_passed": None,
                "salvaged_metric": salvaged.metric}
            # `code` is OMITTED when the repair shipped its work in `files` — the multi-file/repo
            # shape, which returns "". `replay._on_node_repaired` reads `d.get("code", n.code)`, so
            # the attempt loop's unconditional `"code": new_code` would BLANK the node's whole-file
            # artifact on any such repair. The loop can live with that (it re-evaluates, and a repo
            # node's `code` is empty anyway); a cause fix cannot, because it runs AFTER the metric
            # was measured and its one promise is that it does not alter the experiment the metric
            # describes. Omitting the key is the fold's own "leave it alone" spelling.
            if (new_code or "").strip():
                _payload["code"] = new_code
            self.store.append(EV_NODE_REPAIRED, _payload)
        node = fold(self.store.read_all()).nodes[node.id]
        if node.attempt != generation:
            return node, attempt, False, {}
        self._write_node_files(node, workdir)
        stamp(node)                      # the workdir now IS the corrected manifest
        return node, attempt, True, {"changed": sorted(changed),
                                    "deleted": list(repaired_deleted),
                                    "code": new_code or ""}

    def _repaired_footprint(self, node, new_code, repaired_files, reservation):
        """The repaired artifact's resource declaration, clamped to the devices already held.

        A named rule (doc 25 ES-03): the property is a SAFETY one — a repair must never grow onto a
        sibling's GPU — and inline it was reachable only from a repo-task repair inside a live
        multi-GPU reservation, which no test drives. Returns None when the artifact declares
        nothing, exactly as `developer_artifact_footprint` does.
        """
        repaired_footprint = developer_artifact_footprint(
            node.idea.footprint, new_code, repaired_files)
        if repaired_footprint is not None:
            repaired_footprint = (
                self._clamp_resource_footprint(repaired_footprint)
                or repaired_footprint)
            # A repair keeps the dispatcher's lifecycle reservation.  It may refine the
            # declaration within those already-held devices, but cannot grow onto GPUs owned
            # by a sibling while the retry loop is live.
            if ((reservation or {}).get("cpu_only")
                    and "gpus" in repaired_footprint):
                repaired_footprint["gpus"] = 0
            elif ((reservation or {}).get("pin")
                  and "gpus" in repaired_footprint):
                repaired_footprint["gpus"] = min(
                    repaired_footprint["gpus"],
                    int(reservation.get("count", 0) or 0))
            held_ids = ((reservation or {}).get("gpu_ids") or [])
            held_mem = [getattr(self, "_gpu_mem", {}).get(gpu)
                        for gpu in held_ids]
            held_mem = [value for value in held_mem if type(value) is int]
            if (held_mem and isinstance(repaired_footprint.get("gpu_mem_mib"), int)):
                repaired_footprint["gpu_mem_mib"] = min(
                    repaired_footprint["gpu_mem_mib"], min(held_mem))
        return repaired_footprint

    async def _evaluate(self, node_id: int, limiter: anyio.CapacityLimiter,
                        max_es: Optional[float] = None) -> None:
        async with limiter:
          with self.tracer.span("evaluate", new_trace=True, node_id=node_id) as sp:
            events_at_start = self.store.read_all()
            state = fold(events_at_start)
            node = state.nodes.get(node_id)
            # The dispatcher checks this before and after resource admission, but _evaluate is also a
            # defensive public seam used by recovery/tests. An operator Card drop that predates this
            # worker must close the pending lifecycle at zero cost; the watcher below intentionally
            # considers only post-start events so it can charge genuinely consumed compute.
            prestart_stop = getattr(self, "_skip_if_aborted", None)
            if node is not None and callable(prestart_stop) \
                    and prestart_stop({"node_id": node_id}, state):
                return
            # A batch is selected from an earlier fold. Before this worker actually starts, reset
            # (especially implement/propose), abort, tombstone, pause or finish may have won. Never
            # evaluate blank/not-yet-rebuilt code or terminalize a superseded lifecycle.
            if (node is None or node.status is not NodeStatus.pending or node.tombstoned
                    or node.id in state.aborted_nodes or node.rerun_from is not None
                    or state.paused or state.finished or state.stop_requested):
                return
            # The one gate that keeps a speculative miss provably free: no unconfirmed prediction may
            # cross into the sandbox. See `_assert_speculative_selection_confirmed`.
            self._assert_speculative_selection_confirmed(state, node)
            generation = node.attempt       # immutable identity of THIS worker's node lifecycle
            # The trace is opened before the fold above so pre-start exits remain observable. Once
            # this worker has an exact lifecycle, stamp the root; the span index uses that root receipt
            # to keep reset attempts disjoint while every nested generation/tool stays in this trace.
            sp.set("generation", generation)
            start_seq = events_at_start[-1].seq if events_at_start else -1
            sp.set("operator", node.operator)
            # The dispatcher owns this reservation for the complete node lifecycle. Keeping the same
            # devices across every inline repair/retry prevents a repaired process from jumping onto a
            # sibling's GPU; the dispatcher releases it exactly once in its worker `finally`.
            _resource_reservation = self._eval_resource_reservation(node_id, generation)
            # The dispatcher registered this reservation under its ADMISSION-time generation, but
            # `generation` here is node.attempt from a fresher fold. An eval-stage node_reset landing in
            # that window (attempt+1, still pending, rerun_from None) passes the prestart guard above and
            # misses the current-generation lookup — and `_resource_eval_env(None)` would yield an
            # UNPINNED env that sees every sibling's GPU. If the dispatcher still holds this node's
            # reservation under the superseded key, fail closed (return without a terminal) so the
            # dispatcher re-admits the reset lifecycle under its current generation and re-pins it,
            # instead of degrading to whole-box visibility. The presence of a stale-generation reservation
            # is the authoritative signal that this node was dispatcher-admitted — a never-admitted
            # recovery/test call holds no stale key and is exempt. Gate on that reservation, NOT on the
            # live mutable `_eval_parallel`: a Strategist/operator that lowers eval_parallel to 1 mid-batch
            # (while pinned siblings are still draining) must not flip this into an unpinned launch. A
            # serial re-admit of a whole-pool-unpinned node is harmless (it just re-runs unpinned).
            if (
                _resource_reservation is None
                and self._eval_reservation_under_other_generation(node_id, generation)
            ):
                return
            try:
                eval_env = self._resource_eval_env(
                    _resource_reservation, inherit_host=True)
            except GpuPinUnenforceable as exc:
                # an explicit positive declaration on a zero-device inventory is a
                # fail-closed, zero-compute terminal — never an unpinned launch and never an endless
                # resource wait. The dispatcher's finally still releases/clears the marker exactly once.
                async with self._write_lock:
                    self.store.append(EV_NODE_FAILED, {
                        "node_id": node_id, "generation": generation,
                        "error": str(exc)[:400], "reason": "gpu_unavailable",
                        "eval_seconds": 0.0})
                    self._maybe_crash()
                return
            # A6 proxy/predictive scoring: cheaply predict this candidate's metric from the observed
            # history and skip a full eval for the doomed bottom fraction (cost lever). Deterministic
            # + replay-safe: the skip is recorded as node_failed reason="proxy_skipped" and a
            # proxy_scored audit event. OFF by default (kill_fraction=0 -> never skips).
            if self.proxy_scorer is not None and self.proxy_kill_fraction > 0:
                pred = self.proxy_scorer.score(state, node)
                if pred is not None:
                    skip = self.proxy_scorer.should_skip(state, node, pred)
                    sp.set_many(proxy_score=round(pred, 6), proxy_skipped=skip)
                    async with self._write_lock:
                        self.store.append(EV_PROXY_SCORED,
                                          {"node_id": node_id, "generation": generation,
                                           "score": round(pred, 6), "skipped": skip})
                        if skip:
                            self.store.append(EV_NODE_FAILED, {
                                "node_id": node_id, "generation": generation,
                                "error": "skipped by proxy scorer (predicted in the doomed bottom fraction)",
                                "reason": "proxy_skipped", "eval_seconds": 0.0})
                            self._maybe_crash()
                    if skip:
                        return
            # DURABLE EVAL-START BOUNDARY (events/types.py::EV_NODE_EVAL_STARTED). This is the LAST
            # instant at which "this build never ran" is still true: every line below writes a node
            # workdir and then executes the node's own code. Both zero-compute exits above (an
            # unenforceable GPU pin, a proxy skip) have already terminalized, so the boundary is never
            # stamped on a lifecycle that really did cost nothing.
            #
            # WHY AN EVENT, weighed against the alternatives. The log records evaluation cost only at
            # the TERMINAL (`events/replay.py::_charge_terminal_cost`) and appends `stage_finished`
            # rows inside that terminal's own write-lock block, so a process killed mid-training left
            # a node byte-identical to one that was never dispatched. The only thing that told them
            # apart was the IN-MEMORY `eval_inflight` set, which a resumed process starts empty — so
            # after a crash the refund fired on 40 GPU-minutes of real work. Persisting that set needs
            # a durable write anyway; a sidecar file is not a function of the log (invariant #4/#5);
            # and no existing durable fact is written between dispatch and the sandbox. Refusing every
            # refund whose lifecycle is unprovable is the remaining option and IS what happens for a
            # node without `eval_start_boundary` — this event is what lets a node that CAN prove it
            # keep the refund the calibration numbers depend on.
            #
            # Scoped to the lifecycles that can ever be refunded (`eval_start_boundary`, stamped on a
            # speculative attempt-zero `node_created`), so this is not a new per-eval append on the
            # ordinary hot path: a run without speculation writes byte-identical logs. Normally
            # already satisfied here — the card session's admission wrote it in the MAIN task, which
            # is what keeps it out of the speculative election's CAS window (see
            # `_record_eval_start_boundary`). This call is the funnel guarantee for every OTHER way
            # into a sandbox: recovery, the legacy dispatcher, a direct library caller.
            async with self._write_lock:
                self._record_eval_start_boundary(node)
            workdir = self.run_dir / "nodes" / f"node_{node_id}"
            # Phase 2 stage-scoped re-run: REUSE the existing workdir (earlier stages' artifacts — the
            # checkpoint `train` wrote) instead of re-seeding it, which would wipe them.
            _superseded_marker = workdir / ".looplab-superseded"
            def _mark_superseded_workdir() -> None:
                try:
                    workdir.mkdir(parents=True, exist_ok=True)
                    _superseded_marker.write_text(str(generation), encoding="ascii")
                except OSError:
                    import shutil
                    shutil.rmtree(workdir, ignore_errors=True)
            # Stage reuse is the ONE path that evaluates a workdir it did not just build, so it must
            # prove the bytes on disk are the manifest the folded node claims. `node_repaired` is
            # appended BEFORE the repaired files are written (both under the same attempt), so a
            # process death in that window leaves state claiming the repair while the workdir still
            # holds the pre-repair source. A later stage-scoped reset then sets `rerun_stage` without
            # any superseded marker (only the live process writes one, and it died), and reuse would
            # skip materialization and score the OLD bytes as if they were the repair.
            # The stamp closes that: it is written only after the files are on disk, so a crash in the
            # gap leaves it naming the previous manifest and reuse is refused. Fail-closed by
            # construction — a missing/unreadable/mismatched stamp just forces the full materialize
            # that every other entry path already does, costing artifacts, never correctness.
            _manifest_stamp = workdir / ".looplab-manifest"
            def _stamp_workdir(n) -> None:
                try:
                    _manifest_stamp.write_text(_workdir_manifest_digest(n), encoding="ascii")
                except OSError:
                    pass          # unstamped => the next reuse check fails closed and rematerializes
            def _workdir_matches(n) -> bool:
                try:
                    return _manifest_stamp.read_text(encoding="ascii").strip() == _workdir_manifest_digest(n)
                except (OSError, ValueError):
                    return False
            _reuse = bool(node.rerun_stage and workdir.exists()
                          and not _superseded_marker.exists() and _workdir_matches(node))
            if not _reuse:
                self._materialize(node, workdir)    # seed tree -> node edits -> task assets
                _stamp_workdir(node)                # the workdir now IS this manifest
                # A stage-scoped re-run whose workdir was GONE has nothing to reuse — the re-seed just
                # wiped any artifacts. Skipping earlier stages now would run the restarted stage against
                # MISSING inputs, so drop the start_stage and re-run the FULL pipeline instead.
                if node.rerun_stage:
                    node.rerun_stage = None
            # Bind mutable TensorBoard/metric sidecars to this exact lifecycle before launching any
            # user code.  A reset can reuse the node directory (stage restart), so the serving layer
            # also filters points by this start time instead of relabelling an older curve.
            try:
                begin_metrics_attempt(workdir, generation)
            except Exception:  # noqa: BLE001 - telemetry must never block the evaluation itself
                pass
            # Hybrid crash repair: each attempt runs the eval (with the mid-eval abort watcher) and,
            # if it CRASHES, the agent triages it and may repair the code IN PLACE and re-run — all
            # within this one node (no new tree node, no max_nodes spent). Exactly ONE terminal event
            # (node_evaluated/node_failed) is emitted at the end so first_terminal budget accounting
            # and resume re-entry are intact; only NON-terminal `node_repaired` events are written
            # mid-loop.
            #
            # WHAT STOPS THE LOOP (redesigned 2026-08-05 — see the ledger note further down for what
            # this replaced and why). Two things, in this order:
            #   1. THE TRIAGE MODEL, consulted once per attempt with this node's whole repair history
            #      (`repair_log`). Its `abandon` is the primary stop: the model that reads the failure
            #      is the only participant that can say "I no longer know how to fix this". This costs
            #      no additional LLM calls — the loop already made exactly one triage call per attempt
            #      to decide repair-vs-reject; it now decides repair-vs-STOP on better evidence.
            #   2. `inline_repair_attempts`, a hard operator backstop (0 = no operator cap, which now
            #      means `_UNLIMITED_REPAIR_CEILING` rather than nothing — see that constant). It
            #      exists because the judge can be wrong in the expensive direction, and because a
            #      judge that cannot ANSWER must not silently mean "keep going" (those verdicts are
            #      `unanswerable`/`unreadable` and are handled below, not here).
            #
            # AND IT IS A LEDGER, NOT A PROCESS COUNTER. Both of those bounds — the budget and the
            # history the judge reads — are seeded from the DURABLE `node_repaired` rows, so a resume
            # continues this node's repair chain instead of starting a new one on top of it. See
            # `_durable_repair_ledger` for the four-resume measurement that made it necessary.
            import threading
            _repair_cap = _effective_repair_cap(self._inline_repair_attempts)
            attempt, _durable_rows, unparseable_repairs = _durable_repair_ledger(
                events_at_start, node_id, generation)
            # Seeded from the log for the same reason `attempt` is: a bound that a resume refunds is
            # not a bound. `_MAX_DEP_ROUNDS` exists so an offline or misnamed package cannot loop,
            # and `inline_repair_retrain_cap` is a guard on GPU hours — both were process-local.
            dep_rounds = _durable_dep_rounds(events_at_start, node_id, generation)
            total_eval = 0.0                 # summed subprocess wall-clock across all attempts (cost)
            async def _record_superseded() -> None:
                async with self._write_lock:
                    self.store.append(EV_NODE_FAILED, {
                        "node_id": node_id, "generation": generation,
                        "error": "superseded by node reset", "reason": "superseded",
                        "eval_seconds": total_eval})
                _mark_superseded_workdir()
            triage_outcome = None            # ("abandon"|"reject_idea", rationale) for the terminal event
            # THE SALVAGED METRIC, when this node's eval failed for something that is not "the
            # metric is absent" and the operator's own declared reader can still find the value the
            # eval produced. Loop-local because salvage is decided per ATTEMPT and consumed by the
            # ONE terminal below — it never becomes a second terminal (invariant #2).
            salvaged = None
            salvage_cause_repaired = False
            # …and the OTHER outcome of the same block: the node whose repaired declaration then
            # PASSED its artifact re-check, which is not a salvage at all (F1e). Its provenance
            # record carries no violation and is consumed by the same single terminal; the two are
            # mutually exclusive by construction — `salvaged` is cleared the moment this is set.
            declaration_repaired = None
            err = ""
            reason = "crash"
            # THE EVIDENCE THE JUDGE DECIDES ON: this node's repair history, newest last. One row per
            # attempt — what failed, what the fix claimed it would do, and which files it actually
            # touched. Rows made in THIS process are appended from loop locals (every field is already
            # in hand); rows from earlier processes are rebuilt from the durable `node_repaired`
            # events above, because `state` is the fold taken at eval START and `RunState` keeps only
            # the latest code, never the trajectory.
            #
            # The judge is handed the trajectory precisely so it can tell "moving" from "circling",
            # and a resume used to hand it an empty history for a node with eight durable repairs —
            # the same defect as the process-local budget, seen from the other side: the model was
            # asked to judge a chain while being shown none of it, and answered `repair` because
            # nothing it could see said otherwise.
            #
            # These three columns are chosen against the two ways the loop failed. A repair going in
            # CIRCLES is visible as a repeating error next to repeating changed-file sets — which is
            # what the deleted signature counter tried to measure with a regex, and which a reader of
            # the actual text does not need a regex for. A repair making real PROGRESS is visible as a
            # moving failure next to fixes that touch different code. The model gets the trajectory,
            # not a scalar someone else already reduced it to.
            repair_log: list[dict] = list(_durable_rows)
            # A repair that returned something that is not Python at all. Counted directly rather than
            # inferred from the SyntaxError it produces: the error text can vary per attempt (a
            # provider request id), the FACT cannot. See `_UNPARSEABLE_REPAIR_LIMIT`. Durable for the
            # same reason as the budget: it bounds a per-NODE condition (a provider answering with
            # prose), so a process-local count let a resume grant three more truncations.
            # `unparseable_repairs` is seeded from the ledger above.
            # Best pipeline depth any attempt has reached (stages passed/reused before the failure) —
            # surfaced to the judge as the other, non-textual evidence that a repair did real work.
            best_depth = -1
            # Multi-stage reuse across repair attempts: `next_start` is the stage to run FROM on the next
            # eval — _UNSET on the first eval (derives node.rerun_stage), then set by the safe-reuse
            # predicate after each repair (a stage name = reuse the completed earlier stages, e.g. skip
            # re-train when only the score script was fixed; None = a full re-run). `full_retrains` counts
            # the EXPENSIVE full re-runs a repair forced, bounded by inline_repair_retrain_cap.
            next_start = _UNSET
            full_retrains = _durable_full_retrains(events_at_start, node_id, generation)
            # ROLLBACK state, both halves durable for the same reason the budget above is. `rolled_to`
            # is the set of suspects already spent (at most one re-run per stage per node);
            # `rollback_refusal` carries the engine's answer to a REFUSED request forward into the next
            # repair prompt, because a Developer told nothing simply re-asserts the same guess — the
            # thrash the rungs exist to stop would then just move up one level.
            rolled_to = _durable_rollbacks(events_at_start, node_id, generation)
            rollback_refusal = ""
            while True:
                _t0 = time.time()
                # repair/retry attempts reuse the workdir and sandbox stage logs append.
                # When either watchdog is enabled, snapshot every existing log before this attempt
                # starts so it cannot rank/classify prior-attempt bytes. Keep the monitor-off path free
                # of extra filesystem work (`off == today`).
                _eval_spec = getattr(self, "_eval_spec", None)
                _watching_logs = (
                    (getattr(self, "_train_monitor", False) and bool(_eval_spec))
                    or (getattr(self, "_asha_live", False) and isinstance(_eval_spec, dict)))
                _log_snapshot = snapshot_training_logs(workdir) if _watching_logs else None
                # Which log each phase of THIS attempt writes. Both watchdogs live across the WHOLE
                # eval — setup, every stage, and the ALWAYS-appended `score` stage — so without the
                # resolved pipeline they can only guess whose bytes they are reading, and the freshest
                # `*.log` is `setup.log` during a pip install and `score.log` after the training has
                # already SUCCEEDED. `_resolved_stages` re-resolves exactly what `_run_eval` will run
                # ([] = the single-command path, whose `eval.log` IS the training log).
                _log_plan = eval_log_plan(self._resolved_stages(node, workdir)) if _watching_logs else None
                # Mid-eval intervention: a watcher polls while the eval runs in a worker thread. An
                # exact node lifecycle mutation or operator drop of THIS node's Card sets the cancel
                # Event, which tree-kills the in-flight subprocess (sandbox._run_argv). The pre-eval
                # skip only catches not-yet-started nodes — this kills a running one.
                cancel = threading.Event()
                # The watcher's verdict, handed back through a dict because a sibling task in the
                # group cannot rebind this frame's locals (see `_watch_for_intervention`). Read
                # into `aborted`/`superseded`/`operator_card_dropped` once the group has closed;
                # nothing inside it consults them.
                _seen: dict = {}
                kill_signal: dict = {}       # filled by the training monitor if it kills a broken run (Phase 3)
                # The Card identity this worker can be dropped through, read while `node` is still
                # the fold this attempt started from — it is not rebound until after the group.
                _card_id = getattr(getattr(node, "idea", None), "card_id", None)
                async with anyio.create_task_group() as _tg:
                    _tg.start_soon(self._watch_for_intervention, node_id, generation, start_seq,
                                   _card_id, cancel, _seen)
                    # Training-log monitor (ON by default in the product Settings since 2026-08-04;
                    # still off in a bare `Engine(...)`/`EngineOptions`): a sibling task that tails this eval's live
                    # training log on a timer while it runs in the worker thread, asks the Developer to
                    # judge its health, and records the verdict (advisory unless kill is enabled).
                    # Cancelled with the eval by `_tg.cancel_scope.cancel()` below. Gated on the
                    # command-eval path (`_eval_spec`): only those write the per-stage `<stage>.log` the
                    # monitor tails — the solution.py path (toy/dataset) has no live log to watch.
                    if getattr(self, "_train_monitor", False) and getattr(self, "_eval_spec", None):
                        _idea = getattr(node, "idea", None)
                        _rationale = (getattr(_idea, "rationale", "") or "")[:400] if _idea else ""
                        _mkey = ((self._eval_spec.get("metric") or {}).get("key", "metric")
                                 if isinstance(self._eval_spec, dict) else "metric")
                        _mon_ctx = f"Optimizing metric {_mkey!r}." + (
                            f" Experiment: {_rationale}" if _rationale else "")
                        _tg.start_soon(self._monitor_training, node_id, generation, workdir, cancel,
                                       _mon_ctx, kill_signal, _log_snapshot, _log_plan)
                    # ASHA live-curve rank watchdog (ON by default in the product Settings since
                    # 2026-08-04; still off in a bare `Engine(...)`): a sibling task that reads the live
                    # log's latest INTERMEDIATE metric and ranks it against finished siblings; advisory
                    # unless asha_live_kill. Same command-eval gate (needs a live log + the metric spec).
                    if getattr(self, "_asha_live", False) and isinstance(getattr(self, "_eval_spec", None), dict):
                        _mspec = self._eval_spec.get("metric") or {}
                        _tg.start_soon(self._monitor_asha, node_id, generation, workdir, cancel,
                                       _mspec, state.direction, kill_signal, _log_snapshot, _log_plan)
                    # The lifecycle reservation selected by the dispatcher stays unchanged through this
                    # retry. CUDA_VISIBLE_DEVICES contains physical ids (logical→physical remap), while
                    # an unspecified serial eval keeps eval_env=None and sees the whole box as before.
                    try:
                        # CODEX AGENT: an evaluator may finish paid/external side effects here, but its
                        # terminal event is appended much later. A process death in that gap makes resume
                        # run the evaluator again. Persist an attempt-scoped outcome/outbox before exposing
                        # success, or require a reconciliable idempotency key at the evaluator boundary.
                        res = await anyio.to_thread.run_sync(
                            self._run_eval, node, str(workdir), eval_env, None, cancel, next_start
                        )
                    except GpuPinUnenforceable as exc:
                        # Fail-closed device pin the Docker daemon/runtime cannot enforce. Terminalize
                        # THIS node instead of letting the raise cancel every in-flight sibling eval in
                        # the batch and re-crash deterministically on every resume; the reservation is
                        # still released by the dispatcher's finally.
                        cancel.set()
                        _tg.cancel_scope.cancel()
                        # Cancelling the task-group scope cancels THIS host task at its next checkpoint,
                        # and the write-lock acquisition below IS such a checkpoint. Without a shield the
                        # pending CancelledError preempts the append: the promised node_failed is skipped,
                        # the task group swallows its own scope's cancellation, and execution falls through
                        # to `ok = (res.metric ...)` with `res` still unbound (UnboundLocalError — NO
                        # terminal written, and a deterministic re-crash on every resume, exactly what this
                        # handler exists to prevent). Shield the terminal so scope cancellation cannot
                        # preempt it; the acquire is bounded (the watcher/monitor siblings only briefly
                        # read or append under the same lock and are already being cancelled).
                        with anyio.CancelScope(shield=True):
                            async with self._write_lock:
                                self.store.append(EV_NODE_FAILED, {
                                    "node_id": node_id, "generation": generation,
                                    "error": str(exc)[:400], "reason": "gpu_unpinnable",
                                    # Add THIS attempt's elapsed (`time.time() - _t0`) — the normal path
                                    # accumulates it only after the task group exits (line 330), which this
                                    # early return skips, so recording the bare accumulator would drop the
                                    # Docker/runtime probe + setup cost from the immutable eval budget.
                                    "eval_seconds": round(total_eval + (time.time() - _t0), 3)})
                                self._maybe_crash()
                        return
                    cancel.set()                  # eval finished on its own …
                    _tg.cancel_scope.cancel()     # … stop the watcher now (no poll-interval latency)
                # Settle the watcher's ONE verdict now the group has closed. Same three questions the
                # watcher used to answer by assigning three `nonlocal`s; asking them here keeps the
                # branch order below (`superseded` -> `aborted` -> watchdog kill) unchanged.
                _intervention = _seen.get("kind")
                superseded = _intervention == "reset"
                operator_card_dropped = _intervention == "card_drop"
                aborted = _intervention in {"abort", "card_drop"}
                total_eval = round(total_eval + (time.time() - _t0), 3)   # cumulative eval cost (#2)
                # STALL SALVAGE: a stage the stall-watchdog tree-killed AFTER it had already printed its
                # metric (a completed train+eval that only hung on teardown — a distributed finalize
                # deadlock / wedged CUDA op) still counts: the metric is real, the non-zero exit is only
                # the kill. Self-gating — `res.metric is not None` on a stall means the value WAS emitted
                # before the silence. NOT for a real deadline timeout (that is still mid-training).
                ok = (res.metric is not None and not res.timed_out
                      and (res.exit_code == 0 or getattr(res, "stalled", False)))
                if superseded:
                    # The reset discards this lifecycle's metric/state, not compute already spent. A
                    # stale-generation terminal is fold-budget-only: replay rejects its state fields
                    # but charges eval_seconds once for this immutable generation.
                    await _record_superseded()
                    return                         # the reset owns the next lifecycle generation
                if aborted and not ok:                       # killed mid-eval by the operator (and the
                    async with self._write_lock:             # eval didn't already finish cleanly first)
                        self.store.append(EV_NODE_FAILED, {
                            "node_id": node_id, "generation": generation,
                            "error": (
                                "Card dropped by operator (killed mid-eval)"
                                if operator_card_dropped
                                else "aborted by operator (killed mid-eval)"
                            ),
                            "reason": "card_dropped" if operator_card_dropped else "aborted",
                            "eval_seconds": total_eval})
                        self._maybe_crash()
                    return
                if kill_signal.get("kill") and not ok:       # a live watchdog tree-killed the run early
                    # ONE terminal event; the watchdog names the reason so the fold/failure-reflection
                    # knows WHY: the training monitor leaves it default ('monitor_broken'), the ASHA
                    # watchdog sets terminal_reason='asha_underperforming'. The advisory record
                    # (EV_TRAIN_MONITOR_ALERT / EV_ASHA_RANK) already ran live; replay reconstructs the
                    # node from this terminal and never re-invokes the watchdog.
                    _kreason = str(kill_signal.get("terminal_reason") or "monitor_broken")
                    async with self._write_lock:
                        self.store.append(EV_NODE_FAILED, {
                            "node_id": node_id, "generation": generation,
                            "error": ("live watchdog stopped the run early: "
                                      + str(kill_signal.get("reason", ""))[:400]),
                            "reason": _kreason, "eval_seconds": total_eval})
                        self._maybe_crash()
                    return
                # THE PIPELINE RECORD, ONCE PER ATTEMPT — not once per NODE. Multi-stage eval
                # (Phase 1): each stage's pass/fail lands BEFORE the terminal so the fold + trace show
                # mine ✓ / train ✗, and a later stage-scoped re-run knows which stages already passed.
                # Empty on the classic single-command eval, so that path appends nothing, as before.
                #
                # This used to be appended ONLY after the attempt loop, from the LAST attempt's `res`,
                # and that made the log lie about every REPAIRED multi-stage node. Once the reuse
                # predicate starts skipping a completed earlier stage, every later attempt reports it
                # as `{"status": "reused", "exit_code": 0, "seconds": 0.0}` — so the ONE record the log
                # ever received was the zero-work marker, and the fold's own defence against exactly
                # that (`events/replay.py::_on_stage_finished`: "a reused marker must NOT clobber that
                # attempt's REAL completion record ... else the node reads as if it trained in 0s") was
                # VACUOUS, because the real record it protects was never written by anyone.
                # `tests/test_events_replay.py::test_fold_reused_stage_marker_does_not_clobber_real_record`
                # drove that guard from hand-built events, so nothing went red.
                # Measured on runs/rubert-dr-0807: node 2 trained for ~6,900 s and its folded stage
                # record read `train reused / exit 0 / 0.0 s`; node 0's Developer-declared `mine` stage
                # ran four times (two crashes, then two successes) and read `mine reused / 0.0 s`.
                # The replayable stage-state authority could not tell "this stage succeeded and its
                # artifacts were reused" from "this stage never ran in this run at all", which is the
                # difference between a healthy pipeline and a silently skipped one.
                #
                # One append per attempt is what the fold was always written to read, and the
                # de-duplication rule stays THERE (last-wins by name, a real record beats a reused
                # marker in either arrival order) rather than being re-derived here — a second,
                # hand-synced copy of that rule in the writer is how the two would drift apart again.
                # COST, stated because it is a real one: `stage_finished` is FOLDED, so under
                # `eval_parallel > 1` these rows can move `speculation.py::_proposal_authority_seq` and
                # discard a concurrently-prepared paid proposal (invariant 1). They could already do
                # that — the terminal block appended the same rows from the same concurrent evals —
                # what changes is the RATE, bounded by attempts x stages. Making them diagnostic
                # instead is not available: the fold reads them.
                async with self._write_lock:
                    for _st in (res.stages or []):
                        self.store.append(EV_STAGE_FINISHED,
                                          {"node_id": node_id, **_st, "generation": generation})
                if ok:
                    break
                reason = _failure_reason(res)
                # The node's whole account of what went wrong — see `_eval_failure_text`, which is
                # where the no-metric hint and the blank-stderr fallback now live.
                err = self._eval_failure_text(res)
                # METRIC SALVAGE. Asked HERE — after the eval has failed, before any repair is
                # considered and long before the terminal — because this is the only point at which
                # the answer can still change which terminal the node gets. `engine/metric_salvage.py`
                # owns every rule; this is the call and its consequences.
                #
                # `_t0` is THIS attempt's wall-clock start, which is the freshness floor a FILE reader
                # is held to: an artifact left by an earlier attempt in the deliberately-reused
                # workdir is older than it, so it cannot be salvaged as this attempt's result.
                #
                # On a hit the loop BREAKS with `ok` true. It does not repair-and-re-evaluate, which
                # is the whole point: the measured case cost 76 GPU-minutes and the number those
                # minutes bought was already on disk. The CAUSE is still fixed — `_repair_salvaged_cause`
                # commits the Developer's correction to the declaration through the ordinary
                # `node_repaired` event, without paying for another evaluation to confirm it — so a
                # salvaged node does not carry its broken manifest into its next attempt.
                salvaged = self._salvage_eval_metric(res, reason, workdir, _t0)
                if salvaged is not None:
                    # THE GATES THAT QUALIFY A METRIC, applied to this one before anything downstream
                    # can treat it as measured. `run_command_eval` runs the operator's constraints,
                    # extra readers and drift cross-check in its TAIL, which every early return that
                    # produces a salvageable failure skips — so a salvaged node used to reach the
                    # terminal with `violations == []` and, under `metric_salvage="select"`, could
                    # become champion with the operator's hard bounds never applied.
                    _gates = self._salvage_qualifying_gates(salvaged, res, workdir, _t0)
                    if _gates["drift"]:
                        # The cross-reader could not corroborate the salvaged value. `drift` is in
                        # NEVER_SALVAGED_REASONS precisely because re-admitting a metric the trust
                        # gate discarded is worse than losing it — the same fact, found one step
                        # later, gets the same answer. The divergence is recorded (the terminal block
                        # appends `spec_drift` from `res.drift`) and the node keeps failing.
                        res.drift = _gates["drift"]
                        err = (err + "\n[metric salvage refused: the drift cross-check could not "
                                     f"corroborate the recovered metric {salvaged.metric!r}]")
                        salvaged = None
                    else:
                        res.metric = salvaged.metric
                        res.violations = list(res.violations or []) + _gates["violations"]
                        res.extra_metrics = {**(res.extra_metrics or {}),
                                             **_gates["extra_metrics"]} or None
                        ok = True
                        node, attempt, salvage_cause_repaired, _fix = (
                            await self._repair_salvaged_cause(
                                node, state, workdir, generation, salvaged, err, reason, attempt,
                                _stamp_workdir))
                        # THE CONTRACT, RE-ASKED AGAINST THE CORRECTED DECLARATION (backlog F1e).
                        # Measured on `rubertlite-dr-unified-v6` node 3: its stage exited 0, WROTE
                        # its checkpoint, printed the number, and failed its declared artifact
                        # contract because the declaration missed the testbed's composed
                        # `<run_name>_<model>` suffix. The best number the run had (0.728113) then
                        # carried a `metric_salvaged` violation, was excluded from
                        # `feasible_nodes()`, and could neither become champion nor be bred from.
                        # ONE node — see `metric_salvage.py`'s RE-CHECK section for why this is
                        # deliberately not a claim about any operator.
                        #
                        # The metric was never actually unmeasured: the pipeline DID write the
                        # artifact, and the fix above has just corrected the sentence that named it.
                        # So the CHECK is re-asked — never the stage, which is the whole economy of
                        # this design — and if it passes the node is recorded as MEASURED. It is
                        # asked HERE, before the terminal constitutes the salvage, so a node that
                        # passes carries no `metric_salvaged` violation, no salvage provenance and
                        # no `salvaged_error`: it never enters the salvage path at all.
                        # `metric_salvage.py`'s RE-CHECK section owns every admission rule, and what
                        # the promotion does NOT prove is written down there too.
                        declaration_repaired = self._recheck_repaired_contract(
                            res, node, workdir, salvaged, _fix, err)
                        if declaration_repaired is not None:
                            # NOT a salvage. `res.metric` and the qualifying gates' violations stay
                            # exactly as computed above — a re-checked contract says nothing about
                            # the operator's CONSTRAINTS, which still ran and still bind.
                            salvaged = None
                        break
                # Environment self-prep (deps.py): a crash that is purely a missing KNOWN library is
                # not a bad idea — install it (trusted_local only) and re-run BEFORE the crash-triage
                # agent can reject the idea. This is what lets torch/XGBoost/CatBoost (e.g. a GRU
                # model) run on a fresh box instead of dying as `idea_rejected`. Bounded by
                # _MAX_DEP_ROUNDS + the `_dep_attempted` cache; does NOT consume a repair attempt (env
                # prep is not a code fix), and the unchanged node is simply re-evaluated.
                if (self._auto_install_deps and reason == "crash" and dep_rounds < _MAX_DEP_ROUNDS):
                    installed = await anyio.to_thread.run_sync(self._prepare_env, res.stderr)
                    if installed:
                        dep_rounds += 1
                        async with self._write_lock:
                            self.store.append(EV_DEPS_INSTALLED, {
                                "node_id": node_id, "generation": generation,
                                "packages": installed, "round": dep_rounds,
                                "resolved": self._drain_dep_receipts(installed)})
                        continue   # re-run now that the library is present (no repair attempt spent)
                # Eval-budget stop: the inline-repair loop re-runs FULL evals with no budget check
                # between attempts — the loop-top / per-eval guards only see `total_eval_seconds` from
                # TERMINAL events, and no terminal is emitted mid-repair, so a node can overshoot the
                # eval budget by multiples inside ONE node. Abandon once this node's cumulative eval
                # time would cross the ceiling.
                # RE-FOLD before comparing. `state` is the fold taken at eval START, so under
                # eval_parallel>1 every terminal a sibling appended since this worker began is
                # invisible to it and the "cumulative ceiling" undercounts run-wide
                # total_eval_seconds — the repair loop kept re-running full evals well past
                # max_eval_seconds whenever siblings burned the remaining budget mid-loop. This is
                # invariant 4 (never carry derived state across loop iterations); one fold is cheap
                # next to the full eval it is guarding.
                if max_es is not None:
                    spent = fold(self.store.read_all()).total_eval_seconds
                    if spent + total_eval >= max_es:
                        triage_outcome = ("abandon", "eval budget exhausted during inline repair")
                        break
                # Pipeline depth reached by THIS attempt (stages passed/reused before the failure).
                # Non-textual evidence of forward progress, handed to the judge alongside the error
                # trajectory: a node whose failure keeps moving to a LATER stage is visibly working,
                # however similar two error strings look.
                _depth = len([s for s in (res.stages or [])
                              if isinstance(s, dict) and s.get("status") in ("ok", "reused")])
                best_depth = max(best_depth, _depth)
                # ONE BUDGET, AND IT IS DURABLE. `inline_repair_attempts` bounds the repairs this node
                # may make, full stop; `attempt` is the count of durable `node_repaired` rows for this
                # lifecycle, so a resume continues the chain rather than restarting it. 0 still means
                # "no OPERATOR cap" (what an existing run resumes with) and is settled to the engine's
                # own `_UNLIMITED_REPAIR_CEILING` by `_effective_repair_cap` — read that constant for
                # what an operator with 0 in their snapshot gets and why it is not simply 12.
                #
                # It used to be two: an `environment` ledger for repairs that only reconciled the code
                # with the installed libraries and an `experiment` ledger for everything else, each
                # bounded by the same number (so 2N worst case). That is removed. A budget is about
                # TIME AND MONEY — a re-eval costs the same whether the previous fix was a library
                # migration or a modelling decision — and "whose fault was this repair" is not a
                # question the operator asked. The real problem it was aimed at (six stale-dependency
                # migrations eating the whole allowance before the node reached its research question)
                # is answered by making the ONE budget big enough to cover the longest real chain on
                # record and letting the judge stop early when there is nothing left to try, rather
                # than by giving the loop a second allowance to spend.
                budget_left = attempt < _repair_cap
                # Inline-repair gate: feature on, repairable reason, budget left, a Developer that can
                # repair, and something to repair (whole-file code, multi-file edits, or a repo).
                if (not self._inline_repair
                        or reason not in self._inline_repair_reasons
                        or not budget_left
                        or not callable(getattr(self.developer, "repair", None))
                        or not (node.code or node.files or self._repo_spec)):
                    if not budget_left and self._inline_repair:
                        # Which bound stopped it, said out loud. An operator whose snapshot says 0
                        # never chose 50 and must not read a terminal that implies they did.
                        triage_outcome = ("abandon", (
                            f"inline repair has spent its hard limit of {_repair_cap} attempt(s) on "
                            "this node (inline_repair_attempts)" if self._inline_repair_attempts else
                            f"inline repair has spent the engine's absolute ceiling of {_repair_cap} "
                            "attempt(s) on this node — this run's inline_repair_attempts is 0, which "
                            "sets no operator cap, so the ceiling is what stopped it"))
                    break
                # THE STOP DECISION. One call per attempt — the same call the loop already made — now
                # carrying this node's repair history, so the model is answering "given everything
                # that has been tried here, do you still know what to change?" instead of judging a
                # single traceback in isolation. `abandon` is its stop.
                #
                # `attempts_left` is now always a real number, including for a run with no operator
                # cap: there IS a bound in that case (the ceiling), and the whole point of telling the
                # judge is that "a stop and a cap-out are not the same surprise". Telling it `None` on
                # exactly the runs that carry the loosest bound was the least useful place to be coy.
                triage = self._triage_crash(state, node, err, attempt + 1, reason=reason,
                                            repair_log=repair_log[-_JUDGE_HISTORY_ROWS:],
                                            depth=_depth, attempts_left=_repair_cap - attempt)
                action = triage.get("action", DEFAULT_TRIAGE_ACTION)
                if action == "abandon":
                    triage_outcome = ("abandon", triage.get("rationale", ""))
                    break
                if action == "reject_idea":   # the idea itself is wrong -> mark the lineage; steer to a new idea
                    reason = "idea_rejected"
                    triage_outcome = ("reject_idea", triage.get("rationale", ""))
                    break
                # A JUDGE THAT PRODUCED NO USABLE VERDICT, in the two shapes that are not the same
                # condition (`engine/triage.py`'s verdict contract owns the distinction). Both have
                # already been re-asked by `_triage_crash`; reaching here means the non-answer
                # persisted, so neither may read as "keep going".
                #
                # THIS BLOCK IS ABOVE THE TRIAGE-DRIVEN INSTALL ON PURPOSE. It used to sit below it,
                # and the install `continue`s on success — so a judge that answered `unanswerable`
                # every round bought itself a full eval per successful install: measured, 7 evals and
                # six packages (faiss-cpu, tensorboardX, fastai, gensim, textblob, umap-learn) pushed
                # into the SHARED eval interpreter before the breaker ever fired. That install exists
                # because the agent's RATIONALE proves it read the traceback and named a library the
                # traceback could not — a premise a non-answer denies outright. A verdict nobody could
                # read is not evidence about anything, least of all about what to pip install.
                if action in (UNANSWERABLE_TRIAGE_ACTION, UNREADABLE_TRIAGE_ACTION):
                    _judge_err = str(triage.get("rationale", ""))[:400] or "no verdict returned"
                    if action == UNANSWERABLE_TRIAGE_ACTION:
                        # THE TRANSPORT FAILED. Not a verdict about this node: the triage model was
                        # wired and the call did not complete — the same dead-provider condition the
                        # circuit breaker exists for, and exactly how the 2345-repair incident began.
                        # Routed to that breaker (terminal + RUN-level pause) rather than to a quiet
                        # per-node abandon the operator would have to infer a provider outage from.
                        triage_outcome = ("abandon", "the repair-stop judge could not be reached — "
                                                     "treating it as a provider failure, not as "
                                                     "permission to keep repairing")
                        reason = "developer_crash"
                        err = (f"crash-triage failed: {_judge_err}\n[the model that decides whether "
                               f"to keep repairing this node could not be reached, so the node was "
                               f"stopped rather than repaired blind. Its last eval error was: "
                               f"{err[-200:]}]")
                        await self._auto_pause_provider_failure(
                            f"the crash-triage model could not be reached while deciding whether to "
                            f"keep repairing node {node_id} — {_judge_err}")
                    else:
                        # THE MODEL ANSWERED SOMETHING UNREADABLE. The endpoint is demonstrably alive
                        # — it produced bytes — so this is a per-NODE stop and NOTHING MORE. Pausing
                        # the run here was a measured defect: one out-of-enum verdict on a SyntaxError
                        # in the agent's own generated code raised a run-level pause carrying
                        # `node_id=None` (not clearable by a node reset) that told the operator to
                        # check credits, key and base URL — using the MODEL's own rationale as the
                        # evidence — and under `eval_parallel > 1` took every healthy in-flight
                        # sibling down with it. It terminalizes like an `abandon`, keeping the eval's
                        # own `reason`, so a node reset re-opens it and the run continues.
                        triage_outcome = ("abandon", f"the repair-stop judge answered something the "
                                                     f"engine could not read as a verdict, so this "
                                                     f"node stopped rather than repairing blind — "
                                                     f"{_judge_err}")
                    break
                # A library the traceback never NAMED. `_prepare_env` above installs only what the
                # crash reports as missing; when a library degrades an absent dependency into a
                # NameError/AttributeError (an `is_x_available()` guard), the agent's diagnosis is
                # the only place the name exists. Considered here rather than after the repair,
                # because an install is not a code repair: it spends no repair attempt at all
                # (exactly like the traceback-driven round above), so an exhausted budget must not
                # be what stops the engine from making the node runnable. Bounded by the same
                # `_MAX_DEP_ROUNDS` + once-per-module `_dep_attempted` cache; the fail-closed
                # conditions live with the extraction (runtime/deps.py).
                # Gated on `reason == "crash"` like its traceback-driven sibling above, which it was
                # not: `inline_repair_reasons` also admits `timeout` and `oom`, whose `err` is
                # whatever the killed process last wrote — so a training run killed at the deadline
                # after logging an early import warning could be read as unresolved-name shaped and
                # drive a pip install into the shared eval interpreter. A too-slow or too-big run is
                # never fixed by installing something.
                if self._auto_install_deps and reason == "crash" and dep_rounds < _MAX_DEP_ROUNDS:
                    installed = await anyio.to_thread.run_sync(
                        self._prepare_env_from_triage, triage, err)
                    if installed:
                        dep_rounds += 1
                        async with self._write_lock:
                            self.store.append(EV_DEPS_INSTALLED, {
                                "node_id": node_id, "generation": generation,
                                "packages": installed, "round": dep_rounds, "source": "triage",
                                "resolved": self._drain_dep_receipts(installed)})
                        continue   # re-run with the library present (no repair attempt spent)
                # action == "repair": fix the code in place and re-eval (no new node, no budget spent).
                # Snapshot the PRE-repair file set now (node is still the pre-repair fold) so we can
                # compute the repair's REAL change set below — `developer.last_files` is the node's whole
                # cumulative solution for the repo developer (repair_from preloads every node file), so a
                # raw key set would always intersect the train stage and defeat checkpoint reuse.
                # Deletions get the same NODE-side baseline: post-repair `last_deleted` is cumulative
                # (repair_from seeds it from node.deleted), so only THIS repair's deletion DELTA may
                # veto checkpoint reuse — and like `prev_files`, the baseline must be read off the
                # NODE, not the shared developer: at this instant `developer.last_deleted` belongs to
                # whatever node it built LAST (see the `_repair` docstring), so a sibling's stale
                # deletions would mask a real repair deletion from the fail-closed reuse guard (or
                # veto reuse for a deletion this node never made).
                prev_files = dict(getattr(node, "files", {}) or {})
                prev_deleted = set(getattr(node, "deleted", []) or [])
                # A REFUSED rollback rides in FRONT of the eval error, not instead of it: the model
                # still has to fix something, and the refusal only tells it which door is shut and
                # why. Cleared on read so one refusal is carried exactly one attempt forward.
                _err_in = f"{rollback_refusal}\n\n{err}" if rollback_refusal else err
                rollback_refusal = ""
                with self.tracer.span("inline_repair", node_id=node_id, attempt=attempt + 1):
                    try:
                        new_code = self._repair(
                            node, self._repair_error_context(reason, _err_in, state=state, node=node),
                            state)
                    except BudgetExceeded:
                        raise      # the hard budget stop propagates, exactly as in `_triage_crash`
                    except Exception as _repair_exc:  # noqa: BLE001 - see below; never escapes an eval
                        # A DEVELOPER THAT RAISES INSTEAD OF RETURNING THE SENTINEL. Only
                        # `adapters/repo_developer.py` converts its own session failure into the
                        # in-band "(developer error: …)" string; `agents/roles.py::LLMDeveloper.repair`
                        # calls `complete_text` uncaught, `ValidatingDeveloper._attempt_loop` does not
                        # catch either, and this was the one `_repair` call site with no handler above
                        # it (the other two are inside `_create_node`, which already terminalizes the
                        # build and requests the build_crash pause). So a 401/402/outage on a NON-repo
                        # task escaped `_evaluate` entirely: measured — zero terminals, zero pauses,
                        # zero repairs, and on the serial path it takes the whole run down, so the
                        # circuit breaker below never engaged for exactly the workloads that need it.
                        # Normalizing the raise into the sentinel routes it through the ONE reviewed
                        # exit for "the repair call failed at the provider" rather than adding a
                        # second, differently-behaved one. `except Exception` deliberately does not
                        # catch `BaseException`, so cancellation and KeyboardInterrupt still travel.
                        new_code = f"{DEVELOPER_ERROR_PREFIX} {_repair_exc})"
                # Snapshot the developer's per-call audit state IMMEDIATELY, before any `await`: under
                # max_parallel>1 the developer instance is SHARED across concurrent _evaluate tasks,
                # and `async with self._write_lock` below is a checkpoint — a sibling task's repair()
                # would overwrite `developer.last_files` in the gap, so reading it after the lock would
                # record (and re-materialize) ANOTHER node's edits as this node's. Capture now.
                # Read BEFORE the not-a-repair gate below (which awaits on the pause path) for that
                # same reason, and because the gate itself has to know whether the whole-file `code`
                # even IS this repair's artifact.
                repaired_files = dict(getattr(self.developer, "last_files", {}) or {})
                repaired_deleted = list(getattr(self.developer, "last_deleted", []) or [])
                # The rollback REQUEST, snapshotted in the same breath and for the same reason: this
                # developer instance is shared across concurrent evals, so reading it after the next
                # `await` would risk picking up a sibling node's answer and re-running an expensive
                # stage on THIS node that nobody asked for.
                _rollback_ask = str(getattr(self.developer, "last_rollback_stage", "") or "").strip()
                # WAS THIS A REPAIR AT ALL, OR A DEAD PROVIDER? The four answers and the incident
                # each one was retrofitted for live in `_repair_provider_failure`. The unparseable
                # counter round-trips through the return value — it is per-NODE, not per-attempt, so
                # losing it here would silently restore the unbounded loop it bounds.
                _dev_err, unparseable_repairs = _repair_provider_failure(
                    node.code, new_code, repaired_files, repaired_deleted, unparseable_repairs)
                if _dev_err is not None:
                    triage_outcome = ("abandon", "the repair CALL failed at the provider — no "
                                                 "repaired code was produced")
                    reason = "developer_crash"
                    err = (f"{_dev_err}\n[the Developer's own session failed, so this node was never "
                           f"repaired. Its last eval error was: {err[-200:]}]")
                    await self._auto_pause_provider_failure(
                        "the Developer's LLM provider failed while repairing node "
                        f"{node_id}, so the repair returned an error instead of code — {_dev_err}")
                    break
                repaired_footprint = self._repaired_footprint(
                    node, new_code, repaired_files, _resource_reservation)
                attempt += 1
                # THE CHANGE SET, COMPUTED BEFORE THE APPEND. It used to be derived after it (a pure
                # function of four locals all in hand here either way), and it is the column that
                # separates a repair chain that is working from one rewriting the same lines — so it
                # has to be IN the durable row, not only in the process-local one, or a resumed
                # judge reads a history with the evidence column blank. Both halves are DELTAS
                # against the pre-repair node, never the cumulative sets the developer hands back —
                # see `_repair_change_set`. `new_deleted` is consumed by the reuse predicate below.
                changed, new_deleted = _repair_change_set(
                    prev_files, prev_deleted, repaired_files, repaired_deleted)
                # The whole-file fallback is gated on the code having actually MOVED, not merely on
                # it being non-empty. A repair that hands back the artifact it was given rendered
                # `it changed: <whole-file solution>` to the judge — the column asserting a change
                # the bytes disprove, on exactly the rows where the truth matters most. `node` is
                # still the pre-repair fold here, so `node.code` is what `new_code` replaced.
                _code_changed = (new_code or "") != (node.code or "")
                _changed_col = sorted(changed)[:12] or (["<whole-file solution>"] if _code_changed
                                                        else [])
                # DID IT DO WHAT IT SAID? The change set above is what the repair DID; the rationale
                # a line below is what it SAID. Nothing ever compared them, and on the shipped corpus
                # ~25 % of explained repairs named a change their diff does not contain — 13 of them
                # changed nothing whatsoever and still bought a full re-evaluation. See
                # `engine/repair_verify.py` for the measurement, the two-tier design and above all
                # why only the byte-anchored verdict is allowed to stop anything.
                #
                # Computed HERE, beside the change set and before the append, for the same reason
                # `changed` is: it belongs in the DURABLE row. A resumed judge that reads the history
                # without this column is back to being told what each fix intended and never what it
                # accomplished. `node` is still the PRE-repair fold at this point (the re-fold is
                # below the append), so `node.code` is the artifact `new_code` replaced.
                _verification = verify_repair(
                    triage.get("rationale", ""), changed=changed, deleted=new_deleted,
                    code_changed=_code_changed,
                    region=changed_region(prev_files, repaired_files, node.code, new_code))
                async with self._write_lock:
                    repair_payload = {
                        "node_id": node_id, "generation": generation,
                        "attempt": attempt, "code": new_code,
                        "files": repaired_files,
                        "deleted": repaired_deleted,
                        "error_in": err, "triage_action": "repair",
                        "rationale": str(triage.get("rationale", ""))[:300],
                        # The judge's evidence columns, made durable (invariant #5: additive, and the
                        # fold ignores them — `_on_node_repaired` reads code/files/deleted/footprint
                        # only). `_durable_repair_ledger` reads exactly these back after a resume;
                        # `unparseable_repairs` rides along because it bounds the same per-NODE
                        # condition and had the same process-local lifetime.
                        "changed": _changed_col,
                        "stages_passed": _depth,
                        # The verification rung's answer, in the same durable row as the evidence it
                        # was derived from. `verified` is a member of `repair_verify.REPAIR_VERDICTS`
                        # and emphatically NOT of `triage.py::TRIAGE_ACTIONS` — it is a fact about
                        # bytes, not a verdict about the node, and no model may emit one (the two
                        # vocabularies are cross-referenced in both modules). `unmet` is capped
                        # because it is model-derived text riding in an event payload.
                        "verified": _verification.verdict,
                        "unmet": list(_verification.unmet[:12]),
                        "unparseable_repairs": unparseable_repairs}
                    if repaired_footprint is not None:
                        repair_payload.update({
                            "idea_footprint": repaired_footprint,
                            "footprint_finalized": True,
                        })
                    # This commits the repaired code to folded state BEFORE the files below are
                    # materialized, so state briefly claims a repair the workdir does not hold. The
                    # window is closed on the READ side rather than by reordering (either order skews
                    # one way or the other): the workdir carries a manifest stamp written only after
                    # its files land, and stage reuse — the one path that evaluates a workdir it did
                    # not just build — refuses to proceed unless that stamp matches the folded node.
                    # See `_stamp_workdir` / `_workdir_matches` at the top of this method.
                    self.store.append(EV_NODE_REPAIRED, repair_payload)
                node = fold(self.store.read_all()).nodes[node_id]   # node.code now == repaired code
                if node.attempt != generation:
                    await _record_superseded()
                    return                   # reset raced the repair; never adopt its newer lifecycle
                self._write_node_files(node, workdir)               # re-materialize before re-eval
                _stamp_workdir(node)     # only NOW does the workdir match the repaired manifest
                if fold(self.store.read_all()).nodes[node_id].attempt != generation:
                    await _record_superseded()
                    return                   # reset raced the filesystem write; force clean next materialize
                # Choose the NEXT eval's start stage: REUSE the completed earlier stages (the train
                # checkpoint is still on disk — _write_node_files overlays, never wipes) when the repair
                # provably didn't touch them, so a fixed score/eval script doesn't pay to re-train. Else
                # a full re-run — bounded by inline_repair_retrain_cap so a repair that keeps rewriting
                # training code can't burn many full trains (the attempt budget bounds the COUNT of
                # repairs, not their cost). The workdir persists across attempts, so a reused
                # checkpoint is valid. (`changed`/`new_deleted` were computed above the append.)
                # THE ROW THE JUDGE WILL READ on the next attempt — the in-process twin of what
                # `_durable_repair_ledger` rebuilds from the event just written, kept because every
                # field is already in hand and re-reading the log per attempt would be a full scan for
                # nothing. "Which files this fix actually touched" is the column that separates a
                # repair chain that is working from one that is rewriting the same lines: the
                # developer's own rationale says what it INTENDED to change, this says what it did.
                repair_log.append({
                    "attempt": attempt,
                    "error": err[-_JUDGE_ERROR_CHARS:],
                    "fix": str(triage.get("rationale", ""))[:200],
                    "changed": _changed_col,
                    "verified": _verification.verdict,
                    "unmet": list(_verification.unmet[:12]),
                    "stages_passed": _depth})
                # AN INERT CHAIN CANNOT MAKE PROGRESS, AND THE ENGINE CAN PROVE IT. `REPAIR_INERT`
                # means the engine compared the bytes and nothing moved: the files this loop is about
                # to re-materialize are the ones already on disk, `_safe_reuse_start` will reuse
                # every completed stage because the change set is empty, and the eval it is about to
                # pay for is the eval that just failed. Repeating that is not a retry, it is a
                # transcription error with a GPU attached — rubertlite-dr-unified-v4 node 6 spent two
                # in a row at ~2.7 h each, and rubertlite-dense-retrieval node 57 three.
                #
                # ONE is allowed: a developer can legitimately burn a turn budget reading before it
                # edits, and stopping a node on that would be a regression. The bound is on the
                # STREAK, so a chain that recovers is never charged for what it already fixed
                # (`inert_streak`), and it is read off `repair_log` — which is seeded from the
                # durable rows — so a resume continues the streak instead of refunding it.
                #
                # This is the only verdict the loop acts on. `REPAIR_UNMET` is model-derived and
                # rides into the judge's history as evidence; see `engine/repair_verify.py`.
                _inert = inert_streak(repair_log)
                if _inert >= INERT_REPAIR_LIMIT:
                    triage_outcome = ("abandon", (
                        f"the last {_inert} repair attempts changed nothing at all — the engine "
                        "compared the repaired files against the ones already on disk and they are "
                        "byte-identical, so re-evaluating would re-run inputs this node has already "
                        "run; abandoning in-node repair (a budgeted inter-node debug node can still "
                        "pick it up)"))
                    break
                _stages = self._resolved_stages(node, workdir)
                # `deleted` and the eval spec's `cwd` ride along so the predicate can fail closed on
                # its blind spots: a deletion is invisible to the reachability closure (the file was
                # unlinked by _write_node_files above), and a non-default cwd re-bases the stage
                # scripts so the changed-vs-reachable intersection would prove nothing.
                _cwd = (self._eval_spec or {}).get("cwd") if isinstance(self._eval_spec, dict) else None
                next_start = self._safe_reuse_start(
                    _stages, res.failed_stage, changed, workdir,
                    deleted=new_deleted, cwd=_cwd)
                # ROLLBACK, asked only when the Developer named a suspect. It OVERRIDES `next_start`
                # in the one direction the reuse predicate structurally cannot express: backwards,
                # onto a stage that already completed. Consulted AFTER `_safe_reuse_start` and never
                # instead of it — the reuse answer is what the run falls back to when the ladder
                # refuses, and computing it first also means a refusal costs the node nothing.
                _rolled_back = False
                if _rollback_ask:
                    _suspect, _refusal = self._rollback_start(
                        _stages, res.failed_stage, _rollback_ask, changed, workdir,
                        already_rolled_back=rolled_to, cwd=_cwd)
                    if _suspect:
                        next_start, _rolled_back = _suspect, True
                        rolled_to = rolled_to | {_suspect}
                    else:
                        rollback_refusal = _refusal or ""
                    async with self._write_lock:
                        # BOTH outcomes are recorded. The refusals are the auditable half: a node
                        # whose every rollback is refused is a Developer stuck on one guess, and that
                        # is invisible if only the accepted ones are written. Diagnostic + fold-
                        # ignored, so this append is splice-neutral by construction (see the event's
                        # own note in events/types.py); on the main task under the write lock like
                        # every other append in this loop.
                        self.store.append(EV_STAGE_ROLLBACK, {
                            "node_id": node_id, "generation": generation, "attempt": attempt,
                            "stage": _rollback_ask, "failed_stage": str(res.failed_stage or ""),
                            "accepted": bool(_suspect),
                            "refusal": str(_refusal or "")[:300]})
                # Which repairs count against the retrain cap, and why a renamed stage still does, is
                # `_repair_forces_full_retrain`. Asked BEFORE incrementing so cap=N runs exactly N.
                if _repair_forces_full_retrain(res, next_start, rolled_back=_rolled_back):
                    if (self._inline_repair_retrain_cap
                            and full_retrains >= self._inline_repair_retrain_cap):
                        # The message names WHICH of the two ways this repair discarded completed
                        # work, because they call for different next moves by whoever reads the
                        # terminal: "keeps rewriting training code" is a Developer circling, while
                        # "rolled the pipeline back" says the pipeline itself was suspected and the
                        # allowance for testing that is now spent.
                        #
                        # The NON-rollback branch is byte-identical to what it has always been, on
                        # purpose. It is an operator-facing string two tests pin, and rewording it to
                        # cover both cases at once ("expensive re-run(s)") stranded both of them on a
                        # substring that no longer existed — a contract change dressed as a tidy-up.
                        # A new case gets a new sentence; it does not get to edit the old one.
                        triage_outcome = ("abandon",
                            (f"repair rolled the pipeline back to stage {next_start!r} — "
                             f"{full_retrains} expensive re-run(s) already spent"
                             if _rolled_back else
                             "repair keeps changing earlier-stage (training) code — "
                             f"{full_retrains} full re-train(s) already spent")
                            + "; abandoning in-node repair to avoid burning compute "
                            "(a budgeted inter-node debug node can still pick it up)")
                        break
                    full_retrains += 1
                    # Recorded HERE, where the compute is actually committed, so a resume reads the
                    # charge back instead of being handed a fresh allowance (`_durable_full_retrains`).
                    # Diagnostic and fold-ignored, so this append is splice-neutral by construction and
                    # needs no BACKGROUND_APPENDABLE membership; it is on the main task under the write
                    # lock like every other append in this loop.
                    async with self._write_lock:
                        self.store.append(EV_FULL_RETRAIN_CHARGED, {
                            "node_id": node_id, "generation": generation,
                            "spent": full_retrains, "attempt": attempt})
                # loop -> re-run the eval with the corrected code (reusing earlier stages when safe)
            sp.set_many(eval_seconds=total_eval, exit_code=res.exit_code, timed_out=res.timed_out,
                        metric=res.metric, ok=ok, repair_attempts=attempt)
            if res.violations:
                sp.set("violations", len(res.violations))
            if res.drift is not None:
                sp.set("drift", True)
            # ASHA past-experiment curve (#7): a bounded per-RUNG [[rung, metric], ...] (canonical
            # geometric rungs) mined from the eval's CAPTURED stdout when the task declares a stdout_json
            # `resource_key`, so a future live node — snapping its sample to the same rung — finds a sibling
            # checkpoint across the whole run. Additive/only-when-present → old logs fold byte-identically.
            # Computed OUTSIDE the write-lock: it parses `res.stdout` (run_argv's bounded ~64 KB tail — for a
            # staged eval, the FINAL stage's output) and depends only on the eval result + `_eval_spec`,
            # nothing the lock guards, so doing it under the global append lock needlessly serialized every
            # other writer once per completed node. (Widening the tail to a teed full-curve accumulator is a
            # follow-up; the fold + reader already degrade safely to the tail.)
            _curve = None
            if ok:
                _spec = getattr(self, "_eval_spec", None)
                _curve = extract_resource_curve(
                    res.stdout, _spec.get("metric") if isinstance(_spec, dict) else None)
            async with self._write_lock:
                # (The `stage_finished` rows are NOT written here any more — they are appended inside
                # the attempt loop, once per attempt, which is the only way a repaired node's log can
                # carry the stage that really ran rather than the last attempt's `reused` marker. See
                # the block above `if ok: break`. Every path that reaches this terminal has already
                # appended THIS attempt's rows, so they still land BEFORE the terminal, as required.)
                if res.drift is not None:               # Phase 4: uncorroborated metric (audit)
                    self.store.append(EV_SPEC_DRIFT,
                                      {"node_id": node_id, **res.drift, "generation": generation})
                if ok:
                    _eval_payload = {
                        "node_id": node_id, "generation": generation,
                        "metric": res.metric,
                        "stdout_tail": self._redact(res.stdout[-500:]), "eval_seconds": total_eval,
                        "extra_metrics": normalize_extra_metrics(res.extra_metrics),   # #5 multi-objective
                        "violations": res.violations or [],
                        # Intra-node sweep: the whole grid's per-trial results, carried on the ONE
                        # node_evaluated event (the sweep is a single atomic eval — eval_seconds is
                        # the whole-sweep wall-clock; per-trial seconds are audit-only). [] normally.
                        "trials": res.trials or [],
                    }
                    if _curve:                     # computed above, outside the write-lock (see the #7 note)
                        _eval_payload["resource_curve"] = _curve
                    if salvaged is not None:
                        # A SALVAGED METRIC IS NEVER SILENTLY EQUAL TO A MEASURED ONE. Two records,
                        # because they answer two different questions and only one of them is read by
                        # anything today:
                        #   * `metric_provenance` is the ACCOUNT — which rung recovered the value,
                        #     out of which declared reader, which stage had failed, and whether the
                        #     cause was then corrected. Additive, so old logs and old readers are
                        #     unaffected (invariant #5).
                        #   * the `metric_salvaged` VIOLATION row is the ENFORCEMENT. The fold's rule
                        #     is `feasible = not violations`, so under the default `audit` mode this
                        #     node keeps its metric and its evaluated status — it counts, it is in the
                        #     budget, the UI and the digest and the lineage all see it — while
                        #     `RunState.feasible_nodes()` excludes it, which is what champion
                        #     selection and breeding read. A provenance field alone would satisfy
                        #     "the selection path CAN tell" and not "does": nothing on that path
                        #     reads an unknown event key. `metric_salvage="select"` is the operator's
                        #     opt-in to a salvaged metric competing on equal terms.
                        _prov = salvaged.as_event()
                        _prov["cause_repaired"] = bool(salvage_cause_repaired)
                        # The failure the salvage overrode, kept verbatim on the SUCCESS terminal.
                        # A node that reads as evaluated must still be able to tell whoever looks
                        # what went wrong, or the salvage has merely moved the silence.
                        #
                        # INSIDE the provenance record, not beside it as its own event key. It was a
                        # top-level `salvaged_error` and the fold ignores unknown keys — so the one
                        # place it was meant to be read (a replayed `RunState`, which is what the UI,
                        # the report and every read-model see) never had it, and `looplab replay`
                        # silently dropped the only account of what the node's failure had been.
                        # `metric_provenance` IS folded, so putting it here is what makes the promise
                        # true rather than adding a second field for the fold to learn.
                        _prov["salvaged_error"] = str(err)[:600]
                        _eval_payload["metric_provenance"] = _prov
                        _eval_payload["violations"] = (
                            list(_eval_payload["violations"])
                            + salvaged.violation_rows(getattr(self, "metric_salvage",
                                                              DEFAULT_METRIC_SALVAGE)))
                    elif declaration_repaired is not None:
                        # A MEASURED metric with provenance — the F1e case. The declared contract
                        # failed, the Developer's fix corrected the declaration, and the artifact
                        # check then PASSED against it, so the pipeline is known to have produced
                        # what it declared and nothing about the number was ever in doubt. NO
                        # violation row and nothing on the selection path: this node competes for
                        # champion and can be bred from, which is the entire point.
                        #
                        # The record is still written (decision (d) in `metric_salvage.py`'s
                        # `declaration_repair_provenance`): "the manifest was wrong and we fixed it"
                        # is worth knowing even when the number is sound — it is the only durable
                        # trace that the node's recorded code is not byte-for-byte what produced its
                        # recorded metric, and the only way an operator sees that every MERGE node
                        # in a run needed the same correction.
                        _eval_payload["metric_provenance"] = declaration_repaired
                    self.store.append(EV_NODE_EVALUATED, _eval_payload)
                    # B5 reward-hacking detector + I3 code-leakage scan emit the shared Trust-panel event.
                    # emission does not rewrite the metric, but the folded trust_gate policy
                    # can exclude high-precision signals from champion/breeding under gate/block.
                    # Both the surface and the findings over it are NAMED rules (doc 25 ES-03) — the
                    # `code_digest` below must be the digest of the exact bytes that were scanned, so
                    # the surface is read once, here, and handed to the scan.
                    scan_src = self._trust_scan_surface(node)
                    sigs = self._trust_scan_signals(node, res, state, workdir, scan_src)
                    if sigs:
                        # P1-7 versioned TrustEvidence: bind the evidence to a schema version + a digest
                        # of the exact scanned surface (provenance — which bytes produced these signals),
                        # so a stored flag isn't a bare {node_id, signals}. Additive; the fold reads the
                        # new fields with defaults, so old logs are unaffected.
                        # (No local `import hashlib` here: it would make `hashlib` a function-local
                        # name for ALL of _evaluate, so any earlier use in this method would raise
                        # UnboundLocalError — the exact trap _workdir_manifest_digest's docstring
                        # records having to move out of this method to dodge. Module-level import.)
                        self.store.append(EV_REWARD_HACK_SUSPECTED,
                                          {"node_id": node_id, "generation": generation,
                                           "signals": sigs,
                                           "evidence_version": 1,
                                           "code_digest": hashlib.sha256(
                                               scan_src.encode("utf-8", "replace")).hexdigest()[:16]})
                else:
                    # `err`/`reason` were computed in the attempt loop (reason may be "idea_rejected"
                    # if the crash-triage agent judged the idea fundamentally wrong).
                    sp.set("error_reason", reason)
                    data = {"node_id": node_id, "generation": generation,
                            "error": err, "reason": reason, "eval_seconds": total_eval}
                    if res.failed_stage:                # Phase 1: pinpoint which pipeline stage broke
                        data["failed_stage"] = res.failed_stage
                    if triage_outcome is not None:
                        data["triage_action"], data["triage_rationale"] = (
                            triage_outcome[0], str(triage_outcome[1])[:300])
                    self.store.append(EV_NODE_FAILED, data)
                self._maybe_crash()
