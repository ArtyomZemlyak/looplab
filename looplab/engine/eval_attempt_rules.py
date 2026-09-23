"""The eval attempt loop's DECISIONS, as pure functions (review 2026-09-22, ENG2-06).

`engine/evaluate.py::_evaluate` is a driver over `EvalAttempt` and nine `_eval_*` phases (doc 52
row 21), and the split that made it one left three decisions where the old method had them: inline,
in the middle of a phase, reachable only by driving a real sandboxed evaluation that failed in
exactly the right way. Each is a rule with a truth table, so each is a function here and the phases
CALL it — beside a fourth that was already a function:

  * `repair_gate` — may this attempt buy a repair? The inline-repair gate DECIDE_REPAIR asks after
    the floors: the feature, the reason, the floors' verdict, a Developer that can repair and
    something to repair — and which bound to name when a floor is what said no.
  * `triage_verdict_outcome` — what the triage judge's ACTION does to the attempt: settle it (and
    with what terminal outcome, reason, failure text and run-level pause), or let it go on to the
    install and the critic.
  * `evaluated_terminal` — what the scored terminal row says about its own number: the violations
    it carries and the `metric_provenance` it records (salvage, a corrected declaration, the
    subject and its `require` row, the host scorer's receipt, the evaluation inputs and
    comparability key, the applied coordinates).
  * `_classify_repair_answer` — was the repair's answer a repair at all (stuck / provider failure /
    edit, in that order). It landed first, as the one ladder the attempt loop and the
    salvage-cause fix share (ENG2-07), and moved here VERBATIM with its two rungs and its constant,
    keeping its private spellings: `engine/evaluate.py` re-exports the names its importers use.

The fifth piece the review named, the judge-history row, landed before this module as
`evaluate.py::repair_ledger_row` (one function over the written payload, doc 66 §5.3) and stays
beside the durable ledgers that read it.

WHAT STAYS IN THE PHASES, deliberately: every append, every `_write_lock` block, every fold, every
await and every paid call — the side effects and their order. A rule here takes VALUES and returns
a verdict; the phase reads the engine, asks, and acts on the answer.
`tests/test_repair_loop_golden.py` holds the loop to the event log it wrote before the move (21
scripted failures, byte for byte), and `tests/test_eval_attempt_rules.py` drives each rule's truth
table, drives each phase with the rule swapped for a double, and pins on real `ast.Call` nodes that
each phase asks once.

A LEAF: `core` and three engine leaves (`triage`, `failure_diagnosis`, `metric_salvage`), no
engine state, no events, no model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from looplab.core.models import developer_stuck_reason, is_developer_error, is_developer_stuck
from looplab.engine.failure_diagnosis import REASON_SOURCE_ENGINE
from looplab.engine.metric_salvage import unbound_subject_violation_rows
from looplab.engine.triage import (UNANSWERABLE_TRIAGE_ACTION, UNREADABLE_TRIAGE_ACTION,
                                   repair_artifact_defect)

# ---------------------------------------------------- was the repair's answer a repair at all?
# Moved VERBATIM from `engine/evaluate.py` (ENG2-06), private spellings and all: the attempt
# loop, the salvage-cause fix and `tests/test_evaluate_named_rules.py` name them there.

# How many repair calls may answer with something that is not Python before the loop calls it a
# provider failure rather than a truncation. NOT operator-settable and deliberately small: this is
# not a budget, it is the point at which "the model got cut off" stops being the likelier
# explanation than "the endpoint is answering with prose". Two truncations in a row on one node
# already warrant looking at the provider, and the run-level pause is resumable, so the cost of
# being early here is one operator click while the cost of being late is the whole node budget.
_UNPARSEABLE_REPAIR_LIMIT = 3


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


# What ONE repair call answered. Three kinds, and the ladder that tells them apart is ORDERED.
# Process-local (never written to a row), so deliberately NOT a registered vocabulary.
_REPAIR_ANSWER_STUCK = "stuck"                      # the Developer's own "(developer stuck: …)"
_REPAIR_ANSWER_PROVIDER_FAILURE = "provider_failure"  # `_repair_provider_failure` fired
_REPAIR_ANSWER_EDIT = "edit"                        # an artifact — which may still move nothing


@dataclass(frozen=True)
class _RepairAnswer:
    """`_classify_repair_answer`'s verdict on one repair call — see it for why there is ONE ladder.

    The collections keep the exact types the attempt loop always computed (a `set`, two `list`s):
    the in-process judge row and the one `_durable_repair_ledger` rebuilds from the log must render
    identically, and a tuple where a list was would not compare equal to `[]`."""
    kind: str
    # The Developer's stuck reason, or the provider-failure message; "" for an edit.
    detail: str = ""
    # The per-NODE not-Python counter, carried back (a stuck answer leaves it untouched).
    unparseable_repairs: int = 0
    # THIS repair's change set: files whose bytes moved + its own deletions (`_repair_change_set`).
    changed: set = field(default_factory=set)
    # THIS repair's own deletions — the delta, never the cumulative set the Developer hands back.
    new_deleted: list = field(default_factory=list)
    # Does the whole-file code the node will CARRY differ from the code it carried?
    code_changed: bool = False
    # The durable `changed` column: the change set, capped, else the whole-file marker when the
    # code moved, else [] — which is the spelling every reader already takes for "changed nothing".
    changed_column: list = field(default_factory=list)

    @property
    def moved(self) -> bool:
        """Did the repair change ANYTHING the node carries? False is the byte-anchored no-op."""
        return bool(self.changed) or self.code_changed


def _classify_repair_answer(node_code, new_code, prev_files, prev_deleted, repaired_files,
                            repaired_deleted, unparseable_repairs: int, *,
                            empty_code_keeps_artifact: bool = False) -> _RepairAnswer:
    """WAS THIS A REPAIR AT ALL — the ONE ladder both repair call sites read.

    Hoisted (review 2026-09-22, ENG2-07) because the salvage-cause fix had re-implemented the
    attempt loop's ladder and drifted from it three ways, each of which let a fix nobody made read
    as `cause_repaired`: it had NO stuck rung, so a "(developer stuck: …)" declaration was
    committed as the node's code; its no-change exit read the CUMULATIVE deletions, so a node whose
    implement once deleted a file could never be a no-op; and its `changed` column fell back on
    "the reply is non-empty" instead of "the code moved". One function is what keeps the two from
    drifting again. The rungs, in order — and the order is the rule:

      1. STUCK first: the declaration is not Python, so `_repair_provider_failure` would count it
         `unparseable` and, three in, pause the RUN over a provider that is answering perfectly
         (`core/models.py::DEVELOPER_STUCK_PREFIX` says why the two sentinels differ).
      2. PROVIDER FAILURE: `_repair_provider_failure`'s four answers, counter round-tripped.
      3. an EDIT, measured as DELTAS against the pre-repair node (`_repair_change_set`) plus
         whether the whole-file code moved. `moved` False is a repair that changed nothing.

    `empty_code_keeps_artifact` states the ONE way the two callers commit differently. The attempt
    loop writes `code` unconditionally, so an empty reply over a non-empty artifact IS a change to
    the code; the cause fix omits the key when the reply is empty (the fold's "leave it alone"
    spelling), so the node keeps its artifact and the code has not moved.
    """
    if is_developer_stuck(new_code):
        return _RepairAnswer(_REPAIR_ANSWER_STUCK, developer_stuck_reason(new_code),
                             unparseable_repairs)
    dev_err, unparseable_repairs = _repair_provider_failure(
        node_code, new_code, repaired_files, repaired_deleted, unparseable_repairs)
    if dev_err is not None:
        return _RepairAnswer(_REPAIR_ANSWER_PROVIDER_FAILURE, dev_err, unparseable_repairs)
    changed, new_deleted = _repair_change_set(prev_files, prev_deleted, repaired_files,
                                              repaired_deleted)
    carried = (node_code if empty_code_keeps_artifact and not (new_code or "").strip()
               else new_code)
    code_changed = (carried or "") != (node_code or "")
    column = sorted(changed)[:12] or (["<whole-file solution>"] if code_changed else [])
    return _RepairAnswer(_REPAIR_ANSWER_EDIT, "", unparseable_repairs, changed, new_deleted,
                         code_changed, column)


# ------------------------------------------------------------ may this attempt buy a repair?

@dataclass(frozen=True)
class RepairGateContext:
    """Everything the inline-repair gate reads, as values the phase has already settled.

    `floor_stop` is the FLOORS' verdict, not an input the gate decides: DECIDE_REPAIR asks
    `repair_judgment.repair_floor_stop` and then — only when that is silent and a retrain cap is in
    force — `repair_judgment.repair_redone_work_stop`, which needs this attempt's resolved pipeline
    (a read of the node's manifest), and hands the gate whichever one spoke."""
    inline_repair: bool          # `Settings.inline_repair`
    reason: str                  # this attempt's ENGINE reason; a diagnosis never reaches the gate
    repair_reasons: tuple        # `Settings.inline_repair_reasons`, as the engine settled it
    floor_stop: Optional[str]    # the bound that stopped the chain, or None
    developer_repairs: bool      # the Developer exposes a callable `repair`
    has_artifact: bool           # whole-file code, multi-file edits, or a repo: something to repair


@dataclass(frozen=True)
class RepairGate:
    """`repair_gate`'s answer. `outcome` is the terminal's `(triage_action, rationale)` when the
    gate has something to SAY about why it closed — only ever a floor; others close silently."""
    buys_repair: bool
    outcome: Optional[tuple] = None


def repair_gate(ctx: RepairGateContext) -> RepairGate:
    """MAY THIS ATTEMPT BUY A REPAIR? The inline-repair gate, as a truth table.

    Five conjuncts, every one required: the feature on, a repairable reason, no floor reached, a
    Developer that can repair, and something to repair. It is asked on the ENGINE's reason, above
    the triage call, so no diagnosed kind can switch inline repair on or off (`_eval_decide_repair`
    says why that ordering is the containment).

    When it says no, it names the bound ONLY for a floor and only with inline repair on — the one
    refusal an operator must be told about in words. Which bound stopped it, said out loud: an
    operator whose snapshot says 0 never chose 50 and must not read a terminal that implies they
    did. Every other refusal (the feature off, a reason the operator narrowed out, no Developer
    `repair`, nothing to repair) settles with no outcome, so the terminal carries no triage columns,
    exactly as a node that was never offered a repair always has.
    """
    if (ctx.inline_repair and ctx.reason in ctx.repair_reasons and ctx.floor_stop is None
            and ctx.developer_repairs and ctx.has_artifact):
        return RepairGate(True)
    if ctx.floor_stop is not None and ctx.inline_repair:
        return RepairGate(False, ("abandon", ctx.floor_stop))
    return RepairGate(False)


# ----------------------------------------------------- what the triage verdict does to the attempt

@dataclass(frozen=True)
class TriageVerdictOutcome:
    """`triage_verdict_outcome`'s answer. On `settles`, the phase rebinds whichever of `reason`,
    `reason_source` and `err` is not None, records `outcome` for the terminal, requests the
    run-level `pause` when one is named, and leaves the attempt loop. Not settling means the
    verdict was `repair` (or a word this table does not know): the attempt goes on to the
    triage-driven install and the critic."""
    settles: bool
    outcome: Optional[tuple] = None
    reason: Optional[str] = None
    reason_source: Optional[str] = None
    err: Optional[str] = None
    pause: Optional[str] = None


def triage_verdict_outcome(action, rationale, *, err: str, node_id) -> TriageVerdictOutcome:
    """WHAT THE JUDGE'S ACTION DOES TO THE ATTEMPT — the four verdicts that end it, as a table.

    `rationale` is the verdict's raw `rationale` (the terminal redacts and caps it when it writes
    the row), `err` the attempt's failure text and `node_id` the node the pause names.

      * `abandon` — the judge's stop: the node ends on the eval's OWN reason.
      * `reject_idea` — the idea itself is wrong: mark the lineage; steer to a new idea.
      * `unanswerable` / `unreadable` — a judge that produced no usable verdict, in the two shapes
        that are not the same condition (`engine/triage.py`'s verdict contract owns the
        distinction). Both have already been re-asked by `_triage_crash`; reaching here means the
        non-answer persisted, so neither may read as "keep going".
    """
    if action == "abandon":
        return TriageVerdictOutcome(True, ("abandon", rationale))
    if action == "reject_idea":
        # Not a classification of the eval — it is the ENGINE's word for "this lineage
        # is wrong", set from the action and not from `failure_kind`. So the attribution
        # goes back to the engine even though a model's verdict is what triggered it:
        # `reason_source` answers "who classified the failure", and nobody did here.
        return TriageVerdictOutcome(True, ("reject_idea", rationale), reason="idea_rejected",
                                    reason_source=REASON_SOURCE_ENGINE)
    if action in (UNANSWERABLE_TRIAGE_ACTION, UNREADABLE_TRIAGE_ACTION):
        _judge_err = str(rationale)[:400] or "no verdict returned"
        if action == UNANSWERABLE_TRIAGE_ACTION:
            # THE TRANSPORT FAILED. Not a verdict about this node: the triage model was
            # wired and the call did not complete — the same dead-provider condition the
            # circuit breaker exists for, and exactly how the 2345-repair incident began.
            # Routed to that breaker (terminal + RUN-level pause) rather than to a quiet
            # per-node abandon the operator would have to infer a provider outage from.
            return TriageVerdictOutcome(
                True, ("abandon", "the repair-stop judge could not be reached — "
                                  "treating it as a provider failure, not as "
                                  "permission to keep repairing"),
                reason="developer_crash",
                err=(f"crash-triage failed: {_judge_err}\n[the model that decides whether "
                     f"to keep repairing this node could not be reached, so the node was "
                     f"stopped rather than repaired blind. Its last eval error was: "
                     f"{err[-200:]}]"),
                pause=(f"the crash-triage model could not be reached while deciding whether to "
                       f"keep repairing node {node_id} — {_judge_err}"))
        # THE MODEL ANSWERED SOMETHING UNREADABLE. The endpoint is demonstrably alive
        # — it produced bytes — so this is a per-NODE stop and NOTHING MORE. Pausing
        # the run here was a measured defect: one out-of-enum verdict on a SyntaxError
        # in the agent's own generated code raised a run-level pause carrying
        # `node_id=None` (not clearable by a node reset) that told the operator to
        # check credits, key and base URL — using the MODEL's own rationale as the
        # evidence — and under `eval_parallel > 1` took every healthy in-flight
        # sibling down with it. It terminalizes like an `abandon`, keeping the eval's
        # own `reason`, so a node reset re-opens it and the run continues.
        return TriageVerdictOutcome(True, ("abandon", f"the repair-stop judge answered something "
                                                      f"the engine could not read as a verdict, so "
                                                      f"this node stopped rather than repairing "
                                                      f"blind — {_judge_err}"))
    return TriageVerdictOutcome(False)


# ------------------------------------------------ what the scored terminal says about its number

@dataclass(frozen=True)
class EvaluatedTerminal:
    """`evaluated_terminal`'s answer: the row's `violations`, and its `metric_provenance` — None
    means the key is OMITTED, which is what every log written before provenance existed carries."""
    violations: list
    metric_provenance: Optional[dict] = None


def evaluated_terminal(*, violations, metric, salvaged, salvage_cause_repaired: bool,
                       failure_text: str, declaration_repaired, metric_salvage: str, subject,
                       metric_subject: str, host_scorer, eval_inputs, comparability,
                       applied_params) -> EvaluatedTerminal:
    """THE VIOLATIONS AND THE PROVENANCE a `node_evaluated` row carries — one decision, in the order
    the terminal has always written it.

    Inputs are values WRITE_TERMINAL already holds: the eval's own `violations`, its `metric`, the
    salvage (`salvaged`, `salvage_cause_repaired`, the attempt's `failure_text`, the operator's
    `metric_salvage` mode), the F1e `declaration_repaired` record, the result's `subject` /
    `host_scorer` / `eval_inputs` / `applied_params` records, the `metric_subject` rung, and the
    `comparability` key the phase computed (`comparability.py::comparability_record` stays the
    phase's call — it reads the run's task snapshot).

    ORDER IS PART OF THE CONTRACT. `metric_provenance` is built in the sequence the rows below state
    — the salvage/declaration record, then the subject merged over it, then the host scorer, the
    evaluation inputs and comparability key, then the applied coordinates — because the row is
    serialized in insertion order and a reordered dict is a different durable record. The
    violations are the eval's own, then the salvage row, then the `require` row, for the same
    reason.
    """
    violations = list(violations or [])
    provenance = None
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
        _prov["salvaged_error"] = str(failure_text)[:600]
        provenance = _prov
        violations = violations + salvaged.violation_rows(metric_salvage)
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
        provenance = declaration_repaired
    # THE SUBJECT — what this number is a claim ABOUT. Folded onto the SAME
    # `metric_provenance` dict rather than beside it, for the reason `salvaged_error`
    # records one branch up: the fold ignores unknown top-level keys, so a second
    # event key would be invisible in every replayed `RunState` — which is what the
    # UI, the report and every read-model see.
    #
    # It MERGES with whatever the salvage/declaration-repair branches already put
    # there. Those answer "which rung produced this number"; this answers "about
    # what", and a salvaged number still has a subject. Merging also means a reader
    # keeps one key to look at, which is the property `metric_provenance` was folded
    # for in the first place.
    #
    # `.get`, not truthiness: `res.metric_subject` is None on the `off` rung and on
    # every path that never reached a metric read, and an old log has no key at all —
    # invariant #5's additive-with-reader-side-defaults rule, which is not optional
    # here because EVERY existing run's log has no provenance.
    if isinstance(subject, dict):
        provenance = {**(provenance or {}), **subject}
    # THE ENFORCEMENT, under `require`: an UNBOUND metric gets the EXISTING
    # `metric_salvaged` violation row, so the fold's `feasible = not violations`
    # keeps it out of `feasible_nodes()` — counted, in the budget, in the UI and
    # the lineage, and never champion and never bred from. A provenance field
    # alone would satisfy "the selection path CAN tell" and not "does": nothing
    # on that path reads an unknown event key. No second exclusion vocabulary is
    # minted — see `unbound_subject_violation_rows` for why the row is the same
    # name and what a new slug would silently cost.
    #
    # ON EVERY SCORED NODE, and not inside the host-scorer branch below, where it
    # sat until review 2026-09-22 (ENG2-03): there it fired only for a node whose
    # number a HOST scorer produced, so a node scored by its own command with an
    # unbound subject — every task that declares no host scorer — kept its metric
    # AND its place in `feasible_nodes()` under the very rung that exists to refuse
    # it. The subject record is set on every eval-spec path that reads a metric
    # (`eval_dispatch.py` records the absent declaration itself), and a path with no
    # record — a toy/dataset eval, the `off` rung — reaches here with None, which
    # `unbound_subject_violation_rows` answers with no row.
    violations = violations + unbound_subject_violation_rows(subject, metric, metric_subject)
    # THE HOST SCORER'S RECEIPT (doc 52 row 10a) — WHAT PRODUCED the number, beside
    # what it is ABOUT: `{argv, program, program_sha256, program_size}`, digested at
    # the score stage's start, merged onto the same provenance dict for the reason
    # the subject is. Two nodes whose receipts differ were not scored by the same
    # program, and that is the fact a "consistent scoring" claim rests on.
    if isinstance(host_scorer, dict):
        provenance = {**(provenance or {}), "host_scorer": host_scorer}
    # THE COMPARABILITY KEY — what this number may be RANKED AGAINST. Merged onto the
    # same `metric_provenance` dict as the subject, for the reason recorded one branch
    # up: the fold ignores unknown TOP-LEVEL keys, so a second event key would be
    # invisible in every replayed `RunState`, which is what the UI, the report, the
    # cross-run panel and `looplab inspect` all read.
    #
    # TWO RECORDS, not one, because they answer different questions and only one of
    # them is an identity: `eval_inputs` is the EVIDENCE (which files, which digests,
    # and the named reason when one did not bind — what an operator debugging a
    # `unknown` key has to look at), `comparability` is the KEY (a digest plus the
    # authority it was decided at — what a ranking surface compares). A surface that
    # had to re-derive the key from the evidence would be a second copy of
    # `comparability_record`, and the first thing to drift.
    #
    # UNCONDITIONAL, and never a violation. This records what a number may be compared
    # with; it does not decide whether the number is sound, so it mints no row, gates
    # nothing and cannot cost a node its terminal. `None` — the answer for every task
    # that declares neither inputs nor a comparison contract — writes NO key at all
    # rather than an empty one, because two empty keys would compare EQUAL and
    # "two runs that recorded nothing are the same evaluation" is the exact statement
    # this mechanism exists to refuse.
    if isinstance(eval_inputs, dict) or comparability is not None:
        _merged = dict(provenance or {})
        if isinstance(eval_inputs, dict):
            _merged["eval_inputs"] = eval_inputs
        if comparability is not None:
            _merged["comparability"] = comparability
        provenance = _merged
    # THE APPLIED COORDINATES — what the configuration that ran said this node's
    # declared `Idea.params` were worth (`runtime/applied_params.py`, bound at the
    # metric read in `eval_dispatch`).
    #
    # MERGED ONTO `metric_provenance` and NOT given a top-level event key, for the
    # reason the subject record already relies on: the fold ignores unknown TOP-LEVEL
    # keys, so a second key would be invisible in every replayed `RunState` — which
    # is what the UI, the report, the exports and `looplab inspect` all read.
    #
    # UNCONDITIONAL AND NEVER A VIOLATION. `Idea.params` is a PROPOSAL under
    # `params_style: "none"`; a node that adjusted for a real constraint (an OOM, a
    # time budget) did the right thing and must still be allowed to win. This says
    # what it ran at; it mints no row, excludes nothing, and cannot cost a node its
    # terminal. Absent when the node declares no comparable coordinate or no carrier
    # could be read — never an empty record, which would be the claim "the
    # configuration was checked and said nothing".
    if isinstance(applied_params, dict):
        provenance = dict(provenance or {}, applied_params=applied_params)
    return EvaluatedTerminal(violations, provenance)
