"""The run-start PINS and the RE-ENTRY that honours them (review 2026-09-22, ENG1-04 step 2).

Engine invariant #6 — settings recorded in `run_started` win over live config on resume — is ONE
contract with two halves, and `orchestrator.py` kept them apart, with the setup phase and the build
circuit breakers between: the values the setup phase WRITES into `run_started`, and the checks a
re-entry READS them back with. A pin and its reader drift apart exactly when they are edited apart,
so this mixin holds both:

* WHAT `run_started` PINS. `_run_start_pinned_values` — the settings whose run-start record, not a
  later snapshot, owns re-entry semantics: holdout, the verifier tie-break, the HITL gate, and the
  Card-speculation lane's evidence identities (each of those absent unless its treatment is on, so a
  default payload stays byte-identical) — and `_run_start_settled_widths`, the RESOLVED concurrency
  widths, never their AUTO sentinel. Their one caller is `_setup_phase`, which appends the row.
* WHAT A RE-ENTRY DOES WITH THEM, before any append, because a refusal must leave the log it declined
  to trust untouched: `_repin_settled_widths` (over `_recorded_settled_width`, the width THIS log runs
  at) adopts or refuses the widths, `_repin_declared_env` adopts the declared environment, and
  `_require_pinned_speculation_receipt` fails closed on a speculative prefix this process cannot
  re-authorize. `cli/run_cmds.py` asks the first and the last again at its own boundaries, BEFORE its
  lifecycle writes (`_preflight_settled_widths`, `_preflight_speculation_authority`).
* After setup, `_reentry_repin` re-reads the tail (a resume re-reads what another writer may have
  extended), repeats those two checks and re-applies the rest of the recorded treatment: Card
  authority, the EFFECTIVE speculation depth, the active Strategy, the verifier and HITL gates, the
  holdout split — and it loads the run-start cross-run priors. It is this module's one fold, reached
  through `shared.py::engine_fold`, so a test that patches `orchestrator.fold` still steers it.
* The REFUSAL TYPES those checks raise — `RunStartPinError`, `SpeculationAuthorizationError`,
  `SettledWidthPinError` — moved with them. `orchestrator.py` imports all three back, so they are the
  SAME class objects under the spelling `cli/run_cmds.py` and the tests import them by, and nothing
  that raises or catches one changed.

WHAT STAYS IN `orchestrator.py`. `_enter_run` SEQUENCES these checks with build recovery, the command
ACK and setup, and it is an exact cut of `_run_with_llm_broker` (doc 25 XP-06) — the spine's prologue,
not a pin. `_ack_commands` runs on every loop turn, not only at re-entry. `_recover_interrupted_builds`
closes the build-reservation ledger, and the inject lane and the CLI's fatal-error recovery run it too.
The STARTUP resolvers (`_resolve_llm_parallel`, `_resolve_speculation_depth`) stay beside
`Engine.__init__`, their only caller: they resolve the AUTO sentinels this module then pins or adopts.
`_setup_phase`, which appends the `run_started` these values ride in, is `setup_phase.py`'s
(`SetupPhaseMixin`, ENG1-04 step 3): it writes the row, and this module decides what the row pins.

The bodies are byte-for-byte what `orchestrator.py` held (moved by AST line range and asserted
verbatim), and in a mixin `self` IS the Engine, so no call site changed. The observable differences are
the kind step 1 accepted: the re-entry WARNINGs are logged under `looplab.engine.reentry` (every mixin
logs under its own `__name__`), and the three refusal classes report it as their `__module__`.
"""
from __future__ import annotations

import logging
from typing import Optional

from looplab.core.config import RUN_START_PINNED_FIELDS
from looplab.core.errors import OperatorRefusal
from looplab.core.models import RunState
from looplab.engine.finalize import incomplete_finalize_scope, is_guarded_abort
from looplab.engine.shared import engine_fold as fold
from looplab.engine.widths import EVAL_WIDTH_MAX, LLM_WIDTH_MAX, settled_width_refusal
from looplab.events.types import EV_LESSONS_STORE_UNAVAILABLE
from looplab.search.speculation_calibration import (SPECULATION_CALIBRATION_PROFILE_DIGEST,
                                                    SPECULATION_POLICY_SCOPE)

_LOG = logging.getLogger(__name__)


class RunStartPinError(OperatorRefusal, RuntimeError):
    """A re-entry contradicts a value this run's own ``run_started`` pinned (engine invariant #6).

    This is deliberately distinct from an ordinary fatal engine error.  CLI fatal-error recovery
    writes terminal events, while a refused re-entry must return without changing the log it refused
    to trust — so `cli/run_cmds.py::_run_engine_guarded` re-raises this family untouched.

    ``OperatorRefusal`` is the SECOND thing that distinctness has to buy, and it was missing: the
    re-raise kept the log clean and then handed the operator a 33-frame traceback whose last line
    was the carefully written remedy (`engine/widths.py::settled_width_refusal` names the file to
    edit and the two ways to change the width durably).  `RuntimeError` stays the base, so every
    existing `except RuntimeError` / `pytest.raises(RuntimeError)` is unaffected.
    """


class SpeculationAuthorizationError(RunStartPinError):
    """A durable speculation prefix cannot be re-entered under the current evidence authority."""


class SettledWidthPinError(RunStartPinError):
    """A resume explicitly spells a concurrency width other than the one ``run_started`` pinned."""


class ReentryMixin:
    """Invariant #6 at both ends: what `run_started` pins, and how a re-entry adopts or refuses it."""

    def _run_start_pinned_values(self) -> dict:
        """The config values whose run-start record, not a later snapshot, owns re-entry semantics."""
        values = {
            "holdout_fraction": self._holdout_fraction,
            "holdout_select": self._holdout_select,
            "select_verifier": self._select_verifier,
            "select_verifier_samples": self._select_verifier_samples,
            "verifier_ci_tie": self._verifier_ci_tie,
            # The HITL gate, ALWAYS written (both values are a record: a recorded False must win
            # over a snapshot edited to True as much as the reverse). This is the one key the
            # default payload gained after calibration receipts were issued, and
            # `search/speculation_quality.py::_CALIBRATION_PINS_ADDED_AFTER_RECEIPTS` is what keeps
            # those receipts valid — see there before adding another.
            "require_approval": bool(self.require_approval),
        }
        legacy_fields = RUN_START_PINNED_FIELDS - {"card_driven_selection", "speculation_depth"}
        if values.keys() != legacy_fields:
            raise RuntimeError("run-start pinned settings contract drifted")
        # Keep the default run_started payload byte-identical. Replay treats an absent key as false;
        # only the opt-in path needs an additive durable marker.
        if self.card_driven_selection:
            values["card_driven_selection"] = True
        if self._speculation_implementation_digest:
            values["speculation_implementation_digest"] = (
                self._speculation_implementation_digest
            )
        if self._speculation_runtime_scope_sha256:
            values["speculation_runtime_scope_sha256"] = (
                self._speculation_runtime_scope_sha256
            )
        # Preserve the default run_started bytes just like the Card selector flag. Replay supplies
        # zero for an absent key, while an enabled overlap treatment must be durable across resume.
        if ((self.card_driven_selection and self.speculation_depth)
                or self._speculation_gate_calibration):
            values["speculation_depth"] = self.speculation_depth
        if self._speculation_gate_calibration:
            if (
                not self._speculation_gate_admitted
                or not self._speculation_implementation_digest
                or not self._speculation_runtime_scope_sha256
                or self._speculation_calibration_profile_digest
                != SPECULATION_CALIBRATION_PROFILE_DIGEST
                or not self._speculation_calibration_gpu_inventory
                or type(self._speculation_calibration_seed) is not int
                or self._speculation_policy_scope != SPECULATION_POLICY_SCOPE
            ):
                raise RuntimeError("calibration reached run start outside its exact profile envelope")
            values.update({
                "speculation_calibration_profile_digest": (
                    self._speculation_calibration_profile_digest),
                "speculation_calibration_gpu_inventory": list(
                    self._speculation_calibration_gpu_inventory),
                "speculation_calibration_seed": self._speculation_calibration_seed,
                "speculation_policy_scope": self._speculation_policy_scope,
            })
        elif self.card_driven_selection and self.speculation_depth:
            # Neither a runtime-scope pin nor an implementation digest is required here: both are
            # EVIDENCE identities for the calibrated lane, which measured one exact
            # Settings/policy/sandbox envelope under one exact source tree. The product lane measured
            # no such envelope and claims no evidence, so minting either would be a pin that means
            # nothing (and, for the source digest, one that any later edit would revoke). The lane
            # token below is what a resume compares, and it differs between the two lanes.
            if (
                not self._speculation_gate_admitted
                or not self._speculation_gate_receipt_digest
                or not (self._speculation_implementation_digest
                        or getattr(self, "_speculation_product_lane", False))
            ):
                raise RuntimeError("positive Card speculation reached run start without gate evidence")
            values["speculation_gate_receipt_digest"] = (
                self._speculation_gate_receipt_digest
            )
            values["speculation_policy_scope"] = self._speculation_policy_scope
        return values

    def _run_start_settled_widths(self) -> dict:
        """The RESOLVED concurrency widths this run is executing at, for the ``run_started`` record.

        Both Settings fields ship ``0`` = AUTO, a sentinel resolved off the LIVE BOX
        (`_detect_gpu_ids`). `config.snapshot.json` therefore stores the operator's INTENT, not the
        treatment the log was written under, and re-entry re-derives from whatever hardware it lands
        on: a 1-GPU run resumed on a 2-GPU host doubles its eval concurrency and flips the build spine
        from the serial one to the concurrent-append seam (invariant #1) MID-LOG, with nothing
        recorded either way. Pin the settled INTEGERS — the same fix, for the same reason, that
        `_resolve_speculation_depth` already applies to its own AUTO sentinel (invariant #6).

        These deliberately stay OUT of `RUN_START_PINNED_FIELDS`. That contract is the HTTP config
        editor's refuse-list ("start a new run to use different semantics"), and both widths remain
        operator-mutable mid-run through the durable `budget_extend` control event — exactly the
        reason `trust_gate` is excluded from it too. What re-entry owes them is narrower and lives in
        `_repin_settled_widths`: adopt the pin when the axis was launched AUTO, refuse a differently
        spelled explicit width, and stand aside once a control event has taken the axis over.
        """
        return {"eval_parallel": self._eval_parallel, "llm_parallel": self._llm_parallel}

    def _repin_declared_env(self, entry: RunState) -> None:
        """Restore the RUN-LEVEL DECLARED ENVIRONMENT `run_started` recorded (engine invariant #6).

        This is the strict reading of that invariant and not a convenience: a declared variable is
        the reason a node read one corpus rather than another (`VS_LOCAL_DATA_ROOT` is the measured
        case), so a resume that took a DIFFERENT value from live config would keep appending nodes
        to a log whose earlier nodes were evaluated under other conditions — and nothing in the run
        would say so. The log wins, always, for the whole rest of the run.

        Called from `_enter_run` beside `_repin_settled_widths`, and BEFORE any append, for the same
        reason: what the log recorded has to be in force before this invocation decides anything.

        ADOPT, never refuse. The widths refuse a contradicting re-entry because a width is a
        LAUNCH knob the operator re-spells on the resume command line and a silent adoption there
        would ignore something they typed; this value is normally spelled once, in a config file the
        resume re-reads on its own, so refusing would turn an unchanged file into a hard stop. The
        operator IS told at WARNING when the two disagree, and the way to run under a different
        environment is a new run — which is the honest answer, because the comparison the old nodes
        belong to no longer holds.

        A run that recorded NOTHING (an old log, or one launched with no declaration) keeps this
        process's own launch value untouched: inventing "the log said empty, so drop yours" would
        make the very first resume of a run started before this field existed silently lose an
        environment the operator has since declared, which is a regression rather than a pin.
        """
        recorded = getattr(entry, "eval_env", None)
        if not isinstance(recorded, dict) or not recorded:
            return
        live = dict(getattr(self, "_eval_env", None) or {})
        if live != recorded:
            # Name the DISAGREEING variables with BOTH values, not the two key sets: the ordinary
            # case is one variable whose VALUE changed (a data root re-pointed at a different
            # corpus), and two identical-looking key lists is a warning that reports a conflict
            # while hiding it. Printing values is safe precisely because a secret-shaped one was
            # refused at declaration time — that refusal is what lets this message be useful.
            names = sorted(set(recorded) | set(live))
            detail = "; ".join(f"{n}: log={recorded.get(n)!r} launch={live.get(n)!r}"
                               for n in names if recorded.get(n) != live.get(n))
            _LOG.warning(
                "resume: this run's declared eval_env disagrees with the launch config (%s). The "
                "RECORD wins (engine invariant #6) — every node in this log was evaluated under the "
                "recorded environment and results have to stay comparable. Start a NEW run to "
                "evaluate under a different one.", detail)
        self._eval_env = dict(recorded)

    def _repin_settled_widths(self, entry: RunState, *, source: Optional[str] = None) -> None:
        """Restore the widths ``run_started`` pinned, or refuse a re-entry that contradicts them.

        Called at the same re-entry boundaries as `_require_pinned_speculation_receipt` and, like it,
        BEFORE any append — a refusal must leave the log it declined to trust untouched.  "Before any
        append" is a promise only the CALLER can keep: reaching `Engine.run` is already past the
        CLI's own reopen/resume writes, so `cli/run_cmds.py::_preflight_settled_widths` runs this at
        the same four command-level boundaries the speculation receipt is authorized at.  This
        in-engine call stays as the backstop for every other entry point.

        ``source`` names the surface whose knob the operator must actually change, and is passed only
        by the CLI, which is the only layer that knows: `run` writes `config.snapshot.json` from its
        launch settings and never reads it back, while `resume` restores the run's settings FROM that
        snapshot.  Naming the wrong one sends the operator to edit a file with no effect on the
        command they ran, so `engine/widths.py::SETTLED_WIDTH_SOURCES` owns the mapping and ``None``
        keeps the generic phrasing a library ``Engine(...)`` caller gets.

        Deliberately NOT called per loop iteration: `_apply_control_overrides` re-applies an
        operator's `budget_extend` widths on every turn, so a per-iteration re-pin would either undo
        the operator's own live retune or refuse the run over it.

        THREE LAYERS, resolved here and nowhere else (docs/29 F1). `run_started` pins what the run
        LAUNCHED at; a `run_width_settled` row re-pins what the run's own PROPOSALS moved it to; a
        `budget_extend` is what a HUMAN said last. The order is pin < proposals < operator, and the
        top layer is applied by `_apply_control_overrides` on every turn rather than here — which is
        also why the two lower layers must be resolved BEFORE it runs, and why a `budget_extend` on an
        axis still makes this method stand aside entirely for that axis.

        This is what makes the width reconstructible BY REPLAY ALONE (invariant #6). A resumed process
        adopts the last recorded re-pin; it does NOT re-derive one, because the derivation reads the
        LIVE GPU pool and the resuming box may have a different one. Re-deriving here would reproduce,
        one layer up, exactly the defect pinning the widths was introduced to close.
        """
        for axis, upper, recorded, resolved, auto in (
            ("eval_parallel", EVAL_WIDTH_MAX,
             self._recorded_settled_width(entry, "eval_parallel", EVAL_WIDTH_MAX),
             self._eval_parallel, self._eval_parallel_startup_auto),
            ("llm_parallel", LLM_WIDTH_MAX,
             self._recorded_settled_width(entry, "llm_parallel", LLM_WIDTH_MAX),
             self._llm_parallel, self._llm_parallel_startup_auto),
        ):
            # 0 = the key is absent or malformed = a log written before widths were pinned. Keep this
            # process's own startup resolution: that is byte-identical to the pre-pin behaviour, and
            # inventing a width for a legacy log would be the very re-derivation this pin prevents.
            if type(recorded) is not int or not 1 <= recorded <= upper:
                continue
            if auto:
                # AUTO asked the BOX to decide. On re-entry the run's own log outranks a different
                # box — including a SMALLER one: continuing at the pinned width keeps one search
                # treatment across the whole log, and a width above what the hardware can serve is
                # bounded by the resource scheduler, not by silently rewriting the treatment.
                setattr(self, f"_{axis}", recorded)
                continue
            if recorded == resolved:
                continue
            # An operator who already retuned this axis through a durable control event owns it: the
            # override is re-applied by `_apply_control_overrides` on every turn, so the launch flag
            # has no effect on the running width and refusing the resume over it would be a false
            # alarm about a value that does nothing. Either spelling of the axis counts.
            if any(key in (getattr(entry, "budget_overrides", None) or {})
                   for key in (("max_parallel", "eval_parallel") if axis == "eval_parallel"
                               else ("parallel_build", "llm_parallel"))):
                continue
            raise SettledWidthPinError(
                settled_width_refusal(
                    axis, resolved=resolved, recorded=recorded, source=source,
                    repinned=(getattr(entry, f"{axis}_settled", None) == recorded)))

    @staticmethod
    def _recorded_settled_width(entry: RunState, axis: str, upper: int) -> int:
        """The width THIS LOG is running at: the `run_started` pin, re-pinned by the proposals.

        docs/29 F1. Returns `0` for "not recorded", which every caller reads as "keep this process's
        own startup resolution" — the byte-identical pre-pin behaviour a legacy log must retain.

        The re-pin wins over the pin whenever it is present and valid, and that direction is the whole
        contract: the pin says what the run was launched at, the re-pin says what the run's own
        proposals moved it to, and it is the SECOND that later events in this log were produced under.
        Guarded rather than trusted (`Optional[int]` reaching here as a bool or a string means a
        hand-edited log) so a malformed re-pin degrades to the pin instead of to `0` — losing the pin
        as well would hand a hand-edited row the power to re-derive the width off the resuming box,
        which is more than it should be able to do.
        """
        repin = getattr(entry, f"{axis}_settled", None)
        if type(repin) is int and 1 <= repin <= upper:
            return repin
        recorded = getattr(entry, axis, 0)
        return recorded if type(recorded) is int and 1 <= recorded <= upper else 0

    def _require_pinned_speculation_receipt(self, entry: RunState) -> None:
        """Fail closed on positive-depth or calibration re-entry before any log mutation."""
        profile_digest = str(getattr(
            entry, "speculation_calibration_profile_digest", "") or "")
        calibration_gpu = getattr(entry, "speculation_calibration_gpu_inventory", None)
        calibration_seed = getattr(entry, "speculation_calibration_seed", None)
        # TWO DIFFERENT DEPTH FACTS, and reading only one of them is what made an adaptively settled
        # run unresumable through the engine's OWN printed advice. `speculation_depth_pinned` is what
        # `run_started` recorded — the LAUNCH treatment invariant #6 owns, and the only thing an
        # operator's spelled depth may be compared against. `speculation_depth` is that pin narrowed
        # by every `speculation_depth_settled` row (`replay.py::_on_speculation_depth_settled`) — a
        # measurement THIS RUN made about itself, which the run is allowed to make and the operator is
        # not. Until 2026-08-06 the single folded field carried both, so a resume that spelled exactly
        # the depth `run_started` pinned was refused with "speculation_depth was pinned at 0" for a
        # log whose run_started said 1.
        recorded_depth = getattr(entry, "speculation_depth_pinned", 0)
        adaptive_depth = getattr(entry, "speculation_depth", 0)
        recorded_impl = str(getattr(
            entry, "speculation_implementation_digest", "") or "")
        recorded_scope = str(getattr(entry, "speculation_policy_scope", "") or "")
        recorded_receipt = str(getattr(
            entry, "speculation_gate_receipt_digest", "") or "")
        recorded_runtime_scope = str(getattr(
            entry, "speculation_runtime_scope_sha256", "") or "")
        recorded_calibration = bool(
            profile_digest or calibration_gpu or calibration_seed is not None)
        # Treat every durable speculation authority/prefix as gated, even when another field was
        # corrupted or omitted.  In particular card=false must not turn a receipt/implementation/
        # policy/depth prefix into an inert-looking log that recovery or command ACK may mutate.
        recorded_marker = bool(
            recorded_calibration
            or recorded_impl
            or recorded_scope
            or recorded_receipt
            or recorded_runtime_scope
            or (type(recorded_depth) is int and recorded_depth > 0)
        )
        if not recorded_marker:
            return

        def reject(*causes: str) -> None:
            # NAME THE CAUSE. This message used to list every pin the check knows about and let the
            # operator guess which one moved — and the one that actually fired most often (a
            # whole-source implementation digest revoked by an unrelated edit) was not even in the
            # list, so the text pointed at a receipt/profile/seed/GPU mismatch that had not happened.
            detail = "; ".join(causes) if causes else "a run-start speculation pin does not match"
            raise SpeculationAuthorizationError(
                f"cannot resume this run's Card speculation/calibration: {detail}. The run-start "
                "record owns these values (engine invariant #6) — re-run with the launch settings "
                "the log pinned rather than editing them on the resume command."
            )

        # ONE re-entry rule for the depth, and it is stated here once. It used to be two that
        # contradicted each other: this AUTO-only adoption, and `_reentry_repin`'s unconditional
        # `self.speculation_depth = _entry.speculation_depth`.
        #
        #   * AUTO is a STARTUP resolution off the live box (`_resolve_speculation_depth`), exactly
        #     like eval_parallel/llm_parallel — the operator asked the BOX to decide, so on re-entry
        #     the run's own log outranks a different box. Adopt the run's EFFECTIVE depth, its own
        #     ratchet included: a run narrowing itself is not an operator disagreement and must never
        #     refuse. This is what keeps a resume on a differently-sized box continuing the run's own
        #     search treatment.
        #   * An EXPLICITLY spelled depth is never adopted, and it is compared against the LAUNCH PIN
        #     below — a changed explicit treatment must still fail closed. Note what that means for a
        #     run that ratcheted: spelling the pin is ACCEPTED and still runs at the settled depth,
        #     because the ratchet is a durable one-way fact about this run that no resume flag can
        #     un-record (`engine/speculation.py::_settle_speculation_depth` says so in the warning it
        #     prints, which used to advise the opposite).
        auto_depth = bool(getattr(self, "_speculation_depth_auto", False))
        if (
            auto_depth
            and type(adaptive_depth) is int
            and 0 <= adaptive_depth <= 64
        ):
            self.speculation_depth = adaptive_depth
        # Which recorded depth THIS process has to agree with, per the rule above. Computed once so
        # the legacy adoption below and the refusal further down cannot drift apart.
        authoritative_depth = adaptive_depth if auto_depth else recorded_depth
        depth_agrees = (type(authoritative_depth) is int
                        and authoritative_depth == self.speculation_depth)

        # LEGACY PRODUCT-LANE ADOPTION (invariant #6 again). A build before the receipt-lane fix
        # carried a supplied receipt's identity into `run_started` even on a workload the receipt
        # never measured, so those logs pin a whole-source `speculation_implementation_digest` that no
        # later process can reproduce once anything is edited or upgraded. They are the exact runs the
        # product lane exists to keep resumable, and refusing them forever punishes the operator for a
        # bug in the writer. Adopt what the log recorded — but ONLY for that precise legacy shape:
        # this process must itself be in the product lane, and the log must carry no calibration
        # fields and no runtime-scope pin, so a calibrated lane's envelope can never be adopted away.
        if (
            getattr(self, "_speculation_product_lane", False)
            and not self._speculation_gate_calibration
            and not recorded_calibration
            and not recorded_runtime_scope
            and recorded_impl
            and recorded_receipt
            and recorded_scope == SPECULATION_POLICY_SCOPE
            and getattr(entry, "card_driven_selection", False) is True
            and depth_agrees
        ):
            self._speculation_implementation_digest = recorded_impl
            self._speculation_gate_receipt_digest = recorded_receipt

        causes: list[str] = []
        if (
            not isinstance(getattr(entry, "run_id", None), str)
            or not entry.run_id.strip()
            or entry.run_id != self.run_dir.name
        ):
            causes.append(
                f"the log was written for run id {getattr(entry, 'run_id', None)!r}, "
                f"not {self.run_dir.name!r}")
        if not self._speculation_gate_admitted:
            causes.append(
                "this process did not admit Card speculation at all (card_driven_selection off, "
                "depth 0, or a policy other than "
                f"{SPECULATION_POLICY_SCOPE!r}), but the log records a speculative prefix")
        # EQUALITY, not "must be present", for BOTH evidence identities: the calibrated lane pins an
        # implementation digest and a runtime scope, the product lane deliberately pins neither (see
        # `speculation_product_authority_digest` for why a whole-source digest must not gate a real
        # run's resume). Empty-vs-empty is the product lane agreeing with itself; either lane meeting
        # the other's log still fails closed, in both directions.
        if recorded_impl != self._speculation_implementation_digest:
            causes.append(
                "the run started in the "
                f"{'calibrated' if recorded_impl else 'product'} lane but is being resumed in the "
                f"{'calibrated' if self._speculation_implementation_digest else 'product'} one "
                "(speculation_implementation_digest differs)")
        if recorded_runtime_scope != self._speculation_runtime_scope_sha256:
            causes.append(
                "the calibrated runtime-scope pin differs — the Settings/roles/sandbox envelope the "
                "receipt was measured under is not the one this process is launching")
        if not getattr(self, "_speculation_product_lane", False) and not recorded_impl:
            causes.append(
                "the log carries no evidence identity, so it cannot be resumed under a receipt")
        if getattr(entry, "card_driven_selection", False) is not True:
            causes.append("the log did not pin card_driven_selection=true")
        if not depth_agrees:
            # NAME BOTH FACTS when they differ. The old text said "pinned at <settled value>", which
            # was not a value `run_started` ever carried, so it sent the operator to re-run with a
            # launch setting the log does not record — and the depth it printed was the one the
            # ratchet had already overridden.
            settled_note = (
                f" (and settled by this run to {adaptive_depth!r})"
                if adaptive_depth != recorded_depth else "")
            causes.append(
                f"speculation_depth was pinned at run start to {recorded_depth!r}{settled_note}, "
                f"and this process resolved {self.speculation_depth!r}")
        if recorded_scope != SPECULATION_POLICY_SCOPE:
            causes.append(
                f"the log pinned policy scope {recorded_scope!r}, not {SPECULATION_POLICY_SCOPE!r}")
        if self._speculation_policy_scope != SPECULATION_POLICY_SCOPE:
            causes.append(
                f"this process resolved policy scope {self._speculation_policy_scope!r}, "
                f"not {SPECULATION_POLICY_SCOPE!r}")
        if causes:
            reject(*causes)

        # The hidden evidence bootstrap is immutable: any control would invalidate the paired
        # measurement.  A public receipt, by contrast, admits the measured launch envelope and keeps
        # explicit Stage-6 operator controls available.  Those interventions remain in the event log
        # and the quality evidence reader rejects such a run as future calibration evidence.
        if self._speculation_gate_calibration and (
            self._policy_name != SPECULATION_POLICY_SCOPE
            or bool(getattr(entry, "budget_overrides", None))
            or getattr(entry, "pending_strategy", None) is not None
            or bool(getattr(entry, "active_strategy", None))
        ):
            reject(
                "this is the hidden calibration bootstrap, whose paired measurement admits no "
                "policy swap, budget override or Strategy — and the log records one")

        if recorded_calibration:
            if (
                self._speculation_gate_calibration is not True
                or profile_digest != SPECULATION_CALIBRATION_PROFILE_DIGEST
                or self._speculation_calibration_profile_digest != profile_digest
                or not isinstance(calibration_gpu, list)
                or calibration_gpu != self._speculation_calibration_gpu_inventory
                or type(calibration_seed) is not int
                or calibration_seed != self._speculation_calibration_seed
                # Calibration never serializes its internal admission token as a public receipt.
                or bool(getattr(entry, "speculation_gate_receipt_digest", ""))
            ):
                reject(
                    "the log is a calibration bootstrap and its exact profile digest, GPU inventory "
                    "and seed are not the ones this process resolved")
            return

        if self._speculation_gate_calibration:
            reject("this process is a calibration bootstrap but the log is not one")
        if not recorded_receipt:
            causes.append("the log records no speculation lane token")
        elif recorded_receipt != self._speculation_gate_receipt_digest and (
            recorded_receipt not in getattr(
                self, "_speculation_product_authority_tokens", frozenset())
        ):
            causes.append(
                "the speculation lane token differs — on the product lane it is derived from the "
                "policy scope and the TASK KIND, so a resume that names a different task kind (or a "
                "receipt-authorized log met by a receiptless process) lands here")
        if causes:
            reject(*causes)

    def _reentry_repin(self) -> bool:
        _events = self.store.read_all()
        _entry = fold(_events)
        # Re-pin after setup for the same reason the receipt check repeats here: a FRESH run's own
        # run_started was appended by `_setup_phase` a few lines ago (a no-op re-pin), while a resume
        # re-reads a tail another writer may have extended.
        self._repin_settled_widths(_entry)
        self._require_pinned_speculation_receipt(_entry)
        self._pending_finalize_scope = incomplete_finalize_scope(_events)
        # A failed finalize attempt is recorded as a guarded-abort finish (`error`, or the ceiling's
        # `budget_exhausted`) by the CLI guard, but its durable stop is still pending. Treat that as
        # NOT already finalized so the retry below can write run_finished(aborted) and re-run
        # budget/archive/case/cost wrap-up exactly once. The CLASS predicate, not the literal —
        # see `events/finalize_scope.py::GUARDED_ABORT_REASONS`.
        entry_finished = bool(_entry.finished and self._pending_finalize_scope is None and not (
            _entry.stop_requested and is_guarded_abort(_entry.stop_reason)))
        # Restore Card authority before replaying the active Strategy: its conditional governance
        # grant for card_scoring depends on this run-start-pinned value, not the ambient snapshot.
        if _entry.run_id:
            self.card_driven_selection = _entry.card_driven_selection
            # THE LOG'S OWN TREATMENT WINS (invariant #6), and the value adopted is the EFFECTIVE
            # depth — the launch pin narrowed by every settle row this run wrote. Same single rule
            # `_require_pinned_speculation_receipt` states, and it ran at the top of this method,
            # where it has ALREADY failed closed on a spelled depth that disagrees with the launch pin.
            #
            # SAY SO WHERE IT CANNOT. A log whose `run_started` recorded no speculative prefix at all
            # never reaches that refusal (the guard returns on `recorded_marker`), so a resume
            # spelling `-s speculation_depth=2` over a run that pinned none was accepted there and
            # then silently clamped to 0 right here — the operator's explicit flag doing nothing, with
            # nothing said, which is the shape of the two rules disagreeing. It still cannot take
            # effect (turning the treatment on mid-run would write a speculative prefix into a log
            # whose run_started carries no receipt authorizing one, and the run's own next re-entry
            # would then have to refuse it), but it is no longer silent.
            # Gated on `_speculation_gate_admitted` so this says only what it means. A depth spelled
            # with Layer 3 OFF never entered the lane in the first place (`admit_speculation_lane`
            # requires `card_driven_selection`), so it is not re-entry that is ignoring it — and
            # `_run_start_pinned_values` omits the key in that case, which would otherwise make every
            # FRESH `-s card_driven_selection=false -s speculation_depth=2` run warn about its own
            # run_started.
            if (not getattr(self, "_speculation_depth_auto", False)
                    and getattr(self, "_speculation_gate_admitted", False)
                    and self.speculation_depth != _entry.speculation_depth):
                _LOG.warning(
                    "ignoring speculation_depth=%d on re-entry: this run's log is the authority for "
                    "its search treatment (engine invariant #6) and records %d — run_started pinned "
                    "%d. A depth can only be chosen at LAUNCH; start a new run to use a different "
                    "one.",
                    self.speculation_depth, _entry.speculation_depth,
                    getattr(_entry, "speculation_depth_pinned", 0))
            self.speculation_depth = _entry.speculation_depth
        # A7 Strategist: re-apply the last-decided strategy on (re)entry so a resumed run continues
        # with it WITHOUT re-consulting the Strategist (the decision lives in the event log).
        if _entry.active_strategy:
            # A recorded Developer backend is part of this run's treatment. If today's credential or
            # endpoint cannot reconstruct it, refuse re-entry instead of silently continuing on the
            # constructor's backend and making fold/live disagree.
            self._apply_strategy(_entry.active_strategy, _strict_developer=True)
        # R1-c resume-safety (invariant #6): the fold applies the RECORDED tie-break rule
        # (`st.select_verifier_tiebreak`, folded from run_started); re-pin the engine's live-verify gate
        # to match so `_maybe_verify_ties` produces atomic group scores consistently with what the fold
        # reads — not a possibly-changed live `LOOPLAB_SELECT_VERIFIER`. Its direct peer `holdout_select`
        # is re-pinned the same way below. Guard on `run_id` (set only by run_started): on a path where
        # setup hasn't recorded run_started yet, keep the live value rather than zero it from an empty fold.
        if _entry.run_id:
            self._select_verifier = _entry.select_verifier_tiebreak
            self._verifier_ci_tie = _entry.verifier_ci_tie   # R1-d: re-pin the recorded CI-tie rule
            self._select_verifier_samples = _entry.select_verifier_samples
            # The HITL gate (invariant #6, pinned 2026-09-06). Adopt only a RECORDED value: a log
            # written before the pin folds None and keeps the live (snapshot) value, so the fix
            # that exists to stop an unapproved finish cannot itself finish an older
            # approval-pending run unapproved. Say so when the record overrides the snapshot —
            # the operator edited a value the run will not honour.
            if _entry.require_approval is not None:
                if bool(self.require_approval) != _entry.require_approval:
                    _LOG.warning(
                        "ignoring require_approval=%s on re-entry: this run's log records %s "
                        "(engine invariant #6 — the approval gate is chosen at launch; start a "
                        "new run to use a different one)",
                        bool(self.require_approval), _entry.require_approval)
                self.require_approval = _entry.require_approval
        # Pinned by tests/test_holdout.py::test_a_resume_honours_the_recorded_split_not_a_changed_live
        # _setting, which resumes with every one of these settings CHANGED and asserts the recorded
        # values win (both this block and the verifier re-pin above).
        # D1 resume-safety: honor the holdout split the run ORIGINALLY committed to (recorded in
        # run_started), not a possibly-changed live `holdout_fraction` — otherwise nodes evaluated
        # before vs. after a config change would be scored on different splits and the champion pick
        # would mix incomparable metrics. Recorded holdout_select likewise wins on resume.
        if _entry.holdout_fraction is not None:
            self._holdout_fraction = _entry.holdout_fraction
            self._holdout_select = _entry.holdout_select
            # P0-2 freshly-hidden per-epoch holdout: rebuild the partition for the CURRENT search
            # epoch. A run reopened after finishing (search_epoch>=1) then scores its new candidates
            # on a never-disclosed split instead of the one revealed at the prior finish ('already-
            # seen exam'). Epoch 0 rebuilds the byte-identical original partition, so a normal
            # single-epoch run (and every replay of an existing log) is unchanged.
            self._holdout_idx = self._build_holdout_idx(self._holdout_fraction, _entry.search_epoch)
            self._apply_search_split()
            self._holdout_epoch = _entry.search_epoch
        # E4: cross-run meta-learned priors. Excluding THIS run's id matters on resume: a run that
        # already mid-run-distilled its own comparative lessons (M6) must not read them back as if
        # they were another run's experience — its own results are already in the digest. The stamp
        # is taken BEFORE the read (a write landing in between is re-read next refresh — safe).
        self._lessons_seen_stamp = self._lessons_store_stamp()
        # §role-split: the RESEARCHER prior carries only R&D lessons; the DEVELOPER prior only its own
        # code-fix lessons (routed into the idea handed to the Developer via `_directed_idea`). One
        # scan builds both — the two role pools share every untagged lesson, so re-reading/re-embedding
        # the store per role is wasted work.
        _rid = _entry.run_id or None
        _ruid = _entry.run_uid or None
        # BEST-EFFORT, exactly like the refresh path (`lessons.maybe_refresh_lessons`) this mirrors.
        # `_load_reflection_priors_both` reads the SHARED store through `read_jsonl_lenient`, which
        # RAISES OSError on an unreadable lessons.jsonl / meta_notes.jsonl (permissions, a transient
        # FS fault) — while `_lessons_store_stamp` one line up already swallows the same OSError.
        # Unguarded, that failed the run during DETERMINISTIC setup, before the first node, on every
        # start AND every resume: a true crash-loop, strictly worse than the mid-run refresh case the
        # sibling guard was written for. The stamp is reset to None so the first refresh cadence
        # retries the store instead of reading the pre-read stamp as "already seen, unchanged".
        try:
            self._prior_note_text, self._dev_prior_note_text = \
                self._load_reflection_priors_both(
                    exclude_run_id=_rid, exclude_run_uid=_ruid)
            # THE RECORD of what was just spliced into both role prompts (doc 52 row 17): main
            # task, diagnostic, one row per role — the citation instrument's first half.
            if self._prior_note_text or self._dev_prior_note_text:
                self.lessons.record_prior_injection(
                    at_node=len(getattr(_entry, "nodes", None) or {}), phase="run_start")
        except (OSError, ValueError) as e:  # noqa: BLE001 - an advisory prior cannot fail the run
            self._lessons_seen_stamp = None
            self.store.append(EV_LESSONS_STORE_UNAVAILABLE, {
                "mode": "read", "phase": "run_start", "error": str(e)[:300]})
        return entry_finished
