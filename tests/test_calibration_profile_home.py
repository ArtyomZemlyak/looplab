"""The calibration profile identity lives in `search/`, not the engine (doc 25 SE-07).

`search/speculation_calibration.py` opens by saying the scope identity lives there specifically "to
avoid importing the engine from the quality layer (and the resulting import cycle)". Two of the three
constants the quality reader needed had nevertheless stayed in `engine/orchestrator.py`, so
`search/speculation_quality.py` imported the orchestrator to reach them — the cycle existed anyway,
merely deferred to call time, and the module docstring was describing an intention rather than the
code.

Two things need pinning. The DIGEST must not have moved (it gates receipt acceptance: change its
value and every previously-issued calibration receipt stops verifying), and the profile must stay
derivable from `Settings`' declared defaults alone — the whole reason it can live below the engine.
"""
from __future__ import annotations

import inspect

import pytest

from looplab.core.config import Settings
from looplab.search import speculation_calibration
from looplab.search.speculation_calibration import (SPECULATION_CALIBRATION_PROFILE_DIGEST,
                                                    SPECULATION_CALIBRATION_PROFILE_SETTINGS,
                                                    SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS)


# The digest the profile currently produces, together with the schema it is derived FROM. Pinning
# both is what makes this guard able to tell the two causes of a shift apart:
#
#   * the FIELD SET is unchanged but the digest moved  -> a refactor changed the derivation. That is
#     the bug this test was written for (doc 25 SE-07 moved the profile between modules and was
#     verified byte-identical); every already-issued receipt silently stops verifying.
#   * the FIELD SET changed too                        -> `Settings` legitimately gained or lost a
#     knob, so the calibration envelope really is different and old receipts SHOULD be invalidated.
#     Re-pin both values deliberately and note the field below.
#
# Pinning the digest ALONE could not distinguish these, so every ordinary settings addition failed
# with a message about a module move that never happened (observed 2026-08-04 when
# `asha_live_kill_confidence` was added).
#
# Field-set history:
#   2026-08-04  + asha_live_kill_confidence  (ASHA live-kill LLM judge)
#   2026-08-05  - inline_repair_stuck_repeat (see the second history block below)
#   2026-08-06  + concept_tidy               (cross-run concept ratification; see below)
#   2026-08-09  + task_facets_finalize       (separate paid facet-steward schedule; see below)
#   2026-08-11  + systemic_failure_stop      (run-level 'nothing has ever worked' stop; see below)
#   2026-08-11  concept_retag_every 30 -> 5  (intentional cadence-default change; field set unchanged,
#               but the complete settings envelope and therefore old receipts genuinely changed)
#   2026-08-13  + developer_probe, developer_probe_timeout_s  (F2, the Developer's probe)
#   2026-08-13  + eval_env                   (run-level declared environment; see below)
#   2026-08-13  + proposal_width             (the proposal-derived run width, F1; see below)
# A LITERAL, measured on the tree. Both halves must stay literals: an earlier attempt at this guard
# wrote `_EXPECTED_DIGEST = SPECULATION_CALIBRATION_PROFILE_DIGEST`, which compares the constant to
# ITSELF and can never fail — proven by changing only the derivation (the profile schema string
# v1->v2), which moved the digest and still reported 12 passed.
#   2026-08-14  + sandbox_readonly_rootfs (the untrusted/hostile Docker tier's container ROOT
#               FILESYSTEM: "" = the historical writable image, a size = `--read-only` plus a tmpfs
#               scratch of that size). See the second history block below for why.
#   2026-08-15  redact_output False -> True  (intentional SECURITY-default change; field set
#               unchanged, but the complete settings envelope and therefore old receipts genuinely
#               changed). See the second history block below.
#   2026-08-18  inline_repair_reasons + not_learning  (intentional BEHAVIOUR-default change; field
#               set unchanged, third cause — see the message below. The live training watchdog can
#               now stop a stage whose fault its judge attributed to the IMPLEMENTATION, and that
#               stop terminalizes with `not_learning`, which this default is what makes repairable.
#               A replicate calibrated before it would abandon a class of node this one repairs, so
#               the envelope genuinely moved and old receipts SHOULD stop verifying.)
#   2026-08-18  MERGE: `not_learning` (a changed DEFAULT) and `cadence_while_evaluating` (a new
#               FIELD) landed on separate branches, each re-pinned against a tree without the
#               other — the same shape as the 2026-08-13 merge recorded below, and with the same
#               answer: neither branch's digest was ever correct for the shipped tree, so this is
#               re-measured ONCE over the merged map rather than picked from a side.
#   2026-08-20  + train_monitor_contract  (a NEW FIELD, so this is the "field set changed too"
#               branch and both pins move together). It shows the live training watchdog the
#               watched stage's own declared contract plus the engine's reading of the
#               schedule the trainer configured. It buys no paid call and no authority —
#               it is text in a message the monitor already sends — but a calibration
#               REPLICATE is an assertion about the whole envelope, and a replicate run with
#               the contract in the judge's prompt can reach a different verdict on the same
#               node than one run without it. Old receipts SHOULD stop verifying, and the
#               revocation costs nothing that was not already spent:
#               `speculation_implementation_digest` hashes every shipped `.py` and this
#               change moves it regardless.
#   2026-08-21  A DEFAULT MOVED, and the field set did NOT — so this is branch (2) of the
#               assertion below, not a derivation change. `inline_repair_reasons` gained a
#               fourteenth member, `check_false_positive`, because `check_failed` names the
#               stage that REFUSED and says nothing about why: of 22 `check_failed` rows on
#               the durable bench, five were the diagnostician refuting the checker with
#               validation numbers out of the same log and having nowhere to put the finding.
#               A replicate calibrated before it is GENUINELY different — the same failure now
#               reaches a directive pointed at the check rather than at the experiment, so a
#               repair chain can diverge from the first attempt onward. Old receipts should
#               stop verifying. (This is the second time this same field has moved the digest;
#               the first is in the list two paragraphs up, and both are the same kind of
#               deliberate widening rather than a refactor.)
#   2026-08-22  A DEFAULT MOVED AGAIN, on the branch side this time, and the two changes MEET
#               here: `inline_repair_reasons` now excludes `rules_violation` (an arena's own
#               submission validator refused the candidate before scoring, so there is nothing a
#               repair could fix that would not be a way AROUND the rule) while master's
#               `check_false_positive` was added to it. The digest below is measured over BOTH,
#               so it matches neither side's value and re-deriving it is the merge, not a
#               rubber stamp. Old receipts should stop verifying, for the same reason as above.
_EXPECTED_DIGEST = "sha256:d1a4daa186318e67cf0f54fa9a6d245b986f272e51ba863cef3aa0a9248979f7"
# The field set the digest above was measured over. Pinning it as a literal COUNT + a sorted digest
# of the names is what lets the assertion below name the CAUSE of a shift instead of just reporting
# one. Re-pin both, together, when Settings legitimately gains or loses a knob.
#   2026-08-04  + asha_live_kill_confidence   (ASHA live-kill LLM judge)
#   2026-08-05  - inline_repair_stuck_repeat  (the error-signature anti-stuck guard was replaced by
#               the triage model's own stop decision, so the knob had nothing left to tune), and
#               inline_repair_attempts 0 -> 12. This is the "field set changed too" branch: the
#               calibration envelope really is different — the inline-repair loop that a speculative
#               node's eval runs under is bounded differently now — so previously-issued receipts
#               SHOULD stop verifying, and BOTH pins are re-set deliberately.
#   2026-08-06  + concept_tidy                (the cross-run concept RATIFICATION stage). Also the
#               "field set changed too" branch, though this knob is OFF in the calibration profile
#               and cannot execute during a calibration run: it needs a `memory_dir`, which the
#               profile pins to None. The envelope is still a different envelope — the profile is a
#               COMPLETE settings map, so a new field changes it whatever its value — and the guard
#               is deliberately not clever enough to exempt an inert knob. Both pins re-set.
#   2026-08-09  + task_facets_finalize       (the fresh default stops a paid finalize-time steward;
#               missing-field snapshots preserve the old all-three schedule). The calibration
#               profile pins it false alongside `cross_run_curation=false`, so it is inert for the
#               toy calibration run, just like `concept_tidy` above. It is nevertheless a real
#               Settings/snapshot treatment field, and this digest intentionally binds the complete
#               non-variant envelope. Both pins re-set rather than exempting one control ad hoc.
#   2026-08-11  + systemic_failure_stop      (the run-level bound that stops a run in which NOTHING
#               has ever produced a metric). The 'field set changed too' branch again, and here the
#               knob is NOT inert: a calibration run whose every replicate crashed would now end on
#               this terminal rather than grinding, which is a different envelope in the direction
#               that matters — so previously-issued receipts SHOULD stop verifying. Both pins re-set.
#   2026-08-12  + metric_salvage, metric_salvage_repair  (deterministic recovery of a metric the eval
#               already produced, for a node that failed for some other reason). The 'field set
#               changed too' branch, and this knob is emphatically NOT inert for calibration: a
#               replicate whose stage failed its declared contract but had printed its metric now
#               ends `node_evaluated` (infeasible, under the default `audit`) instead of
#               `node_failed`, which changes the population a calibration pair is measured over.
#               `speculation_quality` additionally refuses evidence containing a salvaged node, so a
#               receipt issued before this could not describe the same envelope. Both pins re-set.
#   2026-08-13  + read_fence, and a CHANGED default for inline_repair_reasons (+ diverged, stalled,
#               needs_failed). Two real envelope changes in one day, and neither is inert for a
#               calibration replicate. `read_fence` decides what a node's eval process may READ: at
#               its `deny` default a replicate that reads outside its own workdir now FAILS where it
#               used to succeed on a foreign file's contents. The three new failure reasons are
#               classifications a replicate can actually receive — a watchdog kill that used to be
#               reported as `oom` is now `diverged`, and a stage refused for a missing declared
#               input is `needs_failed` — and each is repair-eligible by default, so the number of
#               attempts a failing replicate buys changes too. Old receipts SHOULD stop verifying.
#   2026-08-13  + eval_deadline_grace_s (doc 39 site #2: the one-shot, judge-granted extension a
#               stage may receive at its wall-clock deadline). The 'field set changed too' branch.
#               INERT AT ITS DEFAULT and re-pinned anyway, on purpose: the default is 0.0, which is
#               the historical unconditional tree-kill byte-for-byte, so no replicate's behaviour
#               moves unless an operator sets it. But the profile's whole job is to describe the
#               envelope a receipt was issued under, and an operator who DOES set it changes how long
#               a replicate may run — which is exactly the kind of difference a paired calibration is
#               measuring. A knob that can change a replicate's wall clock does not get to be
#               invisible to the envelope just because its default is off.
#   2026-08-13  + developer_probe (ON) and developer_probe_timeout_s — the Developer's PROBE (F2,
#               tools/dev_probe.py). INERT for a calibration replicate, like `concept_tidy` and
#               `task_facets_finalize` above and unlike `read_fence`: the profile's workload scope
#               is `quadratic_toy`, which declares no editable source tree, and `make_roles` only
#               builds the `LLMRepoDeveloper` that carries the probe when `repo_spec()` names one —
#               so the tool cannot be composed, cannot be called, and cannot change what a
#               replicate does. Both pins are re-set anyway rather than exempting the two knobs:
#               the profile is a COMPLETE settings map and this digest binds the whole non-variant
#               envelope, which is exactly the rule that keeps the guard from needing to be clever
#               about which knobs are reachable from which workload.
#   2026-08-13  MERGE: eval_deadline_grace_s and the developer_probe pair landed on separate
#               branches, each re-pinning against a tree without the other, so both pins were
#               measured over an incomplete settings map. Re-measured once over the merged map:
#               200 fields. Neither branch's digest was ever correct for the shipped tree, which is
#               why this is re-derived here rather than picked from one side.
#   2026-08-13  + repair_critic_after, and a CHANGED default for inline_repair_attempts (12 -> 0).
#               F8/F5: the in-node repair bound stopped being a count. The 'field set changed too'
#               branch, and it is emphatically NOT inert for a calibration replicate — this is the
#               same knob whose 0 -> 12 move on 2026-08-05 was recorded above as a real envelope
#               change, taken back the other way for a different reason. A failing replicate now
#               buys repairs until a JUDGMENT stops it (bounded by the engine's ceiling of 50, the
#               eval-time budget and the money ceiling) rather than at attempt 12, and the critic is
#               a second model that can end the loop earlier. Both directions change how many evals
#               a replicate spends, so old receipts SHOULD stop verifying. Both pins re-set.
#               NOTE the OTHER receipt this day revokes, which is not visible from this file:
#               `search/scorer_fidelity.py`'s `forced_debug` case became `no_forced_debug` when the
#               Debug node was deleted, and that row is part of `speculation_quality`'s derivation.
#   2026-08-13  MERGE of five branches. Each re-pinned this digest against a tree carrying only its
#               own new knob, so every one of the five was measured over an incomplete settings map
#               and none was ever correct for the shipped tree. Re-measured ONCE over the merged
#               map: 201 fields. A plain union of the branches also left several `_EXPECTED_DIGEST`
#               and `_EXPECTED_FIELD_COUNT` statements in this file, where Python silently uses the
#               last — the vacuous-guard shape the comment above warns about, now collapsed to one.
#   2026-08-13  + eval_env                   (the RUN-LEVEL DECLARED ENVIRONMENT, backlog F1d). The
#               'field set changed too' branch. The profile pins it to `{}`, so a calibration
#               replicate runs under no declaration and this knob is INERT for the toy workload —
#               like `concept_tidy` and `task_facets_finalize` before it, and the guard is
#               deliberately not clever enough to exempt an inert knob, because the digest binds the
#               COMPLETE non-variant envelope. What makes re-pinning right rather than merely
#               necessary is what the field IS: a value that changes what every eval process in the
#               run can read. An envelope that can no longer state that is not the envelope a later
#               receipt would be compared against. Both pins re-set.
#   2026-08-13  + metric_subject, landlock  (metric PROVENANCE: what a recorded number is a claim
#               ABOUT, and the kernel read allow-list that bounds what produced it). The 'field set
#               changed too' branch. `landlock` IS inert for a calibration replicate — it ships
#               `off` and the toy profile declares no mounts — but `metric_subject` is not: at its
#               `audit` default every replicate's `node_evaluated` now carries a
#               `metric_provenance` record, which is folded state a receipt describes, and the
#               protected `score` stage's `needs` is derived from the declared subject, so a
#               replicate can now fail `needs_failed` where it used to run. Old receipts SHOULD stop
#               verifying. Both pins re-set.
#   2026-08-13  + proposal_width  (the run's WIDTH derived from the proposals, backlog F1). The
#               'field set changed too' branch. It is INERT for a calibration replicate twice over —
#               the profile spells all four widths as `1`, so no axis is AUTO, and
#               `_settle_proposal_width` refuses the calibration lane outright — but the digest binds
#               the COMPLETE non-variant envelope and is deliberately not clever enough to exempt an
#               inert knob. What makes re-pinning right rather than merely necessary is that the
#               field decides whether a run may CHANGE its execution width mid-log at all, which is
#               precisely the property a receipt asserts about its replicates; `run_width_settled` is
#               a named forbidden calibration lifecycle event for the same reason. Both pins re-set.
#               Note this change moves `speculation_implementation_digest` regardless (it hashes
#               every shipped `.py`), so every previously issued receipt is revoked either way.
#   2026-08-14  + train_monitor_tools (the watchdog judges get `read_log`/`metric_series` instead of
#               a fixed slice of the tail). The 'field set changed too' branch again, and verified
#               that way rather than from the count: diffing the Settings field set against the
#               pre-merge tree reports exactly `['train_monitor_tools']` added and nothing removed,
#               so a +2/-1 cannot be hiding behind the +1. Re-pinning is right and not merely
#               necessary, because the field decides whether a replicate's kill judge may issue
#               PAID tool calls and read arbitrary windows of the training log — i.e. it changes
#               what evidence the stop decision was made on, which is exactly what a receipt
#               asserts about its replicates. `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` pins it OFF, so a
#               resumed pre-2026-08-14 run replays on the old evidence and its receipt still
#               describes what happened. Both pins re-set.
#   2026-08-14  + auto_extra_metrics (may an UNDECLARED numeric key on the candidate's own stdout be
#               RECORDED as one of the node's metrics). The 'field set changed too' branch, verified
#               that way and not from the count: diffing the Settings field set against the
#               pre-merge tree reports exactly `['auto_extra_metrics']` added and nothing removed.
#               Re-pinning is right and not merely necessary, and this field is the sharpest case on
#               this list so far: a calibration replicate's own CUDA-probe proof
#               (`speculation_cuda_probe_v`/`device_count`/`alloc_bytes`/`device_ordinal`, which
#               `speculation_quality._validate_cuda_probe_artifact` requires as an EXACT schema on
#               `node.extra_metrics`) travels through precisely the channel this field gates — so at
#               `false` a replicate records no probe metrics at all and the artifact check refuses
#               it. The shipped default is `true`, i.e. the pre-field behaviour, so nothing about a
#               default run moves; but a receipt asserts what its replicates recorded, and this is a
#               field that can empty the very evidence the gate re-derives. Both pins re-set.
#   2026-08-14  + sandbox_readonly_rootfs (container ROOT-FILESYSTEM hardening for the untrusted and
#               hostile Docker tiers). The 'field set changed too' branch, verified that way and NOT
#               from the count: diffing the `Settings` field set against the pre-merge tree by AST
#               reports exactly `['sandbox_readonly_rootfs']` added and nothing removed, so a +2/-1
#               cannot be hiding behind the +1. It is INERT for a calibration replicate twice over —
#               the profile pins `trust_mode` to the subprocess tier, which has no container
#               filesystem at all, and the shipped default is `""`, i.e. the historical writable
#               image byte for byte — and both pins are re-set anyway, on the same rule every inert
#               knob above was re-pinned under: the digest binds the COMPLETE non-variant envelope
#               and the guard is deliberately not clever enough to exempt a knob it can prove
#               unreachable. What makes re-pinning right rather than merely necessary is what the
#               field IS: it decides what an eval process may WRITE outside its own workdir, which
#               is the write-side twin of `read_fence`/`landlock` — an operator who sets it changes
#               whether a replicate that installs a system package, or writes a cache under $HOME,
#               succeeds or fails. An envelope that cannot state that is not the envelope a later
#               receipt would be compared against.
#   2026-08-15  redact_output False -> True. **The 'field set UNCHANGED' branch**, which this file's
#               own assertion message tells you not to re-pin — so read why this is the second
#               legitimate instance rather than the bug that message is written for, and the
#               precedent is one line up in the first history block: `concept_retag_every 30 -> 5`
#               (2026-08-11), recorded there as "field set unchanged, but the complete settings
#               envelope and therefore old receipts genuinely changed". The DERIVATION did not move
#               — `_declared_settings_json_defaults` reads `Settings`' declared defaults, and a
#               declared default is exactly what changed — so the two causes the guard separates are
#               still separated; what this case shows is that "field set unchanged" is necessary but
#               not sufficient evidence of a refactor, and the tie-breaker is whether a REVIEWER
#               deliberately moved a default in the same change.
#               VERIFIED BY DIFFING THE FIELD SET AGAINST MASTER, NOT FROM THE COUNT, exactly as the
#               2026-08-14 entries above prescribe: an AST scan of `Settings`' annotated assignments
#               in both trees reports [] added and [] removed, with the ORDER identical too — so
#               `_EXPECTED_FIELD_COUNT` STAYS at 208 and a +1/-1 cannot be hiding behind an
#               unchanged integer. Only the digest is re-pinned.
#               It is NOT inert for a calibration replicate, and that is what makes re-pinning right
#               rather than merely necessary: the profile inherits the new `True`, so every
#               replicate's persisted `stdout_tail`/`stderr_tail`/`error`/`reason` now runs the
#               ENTROPY pass as well as the always-on shape/env passes. A receipt asserts what its
#               replicates RECORDED, and this changes the bytes that reach `events.jsonl` — the same
#               kind of claim `auto_extra_metrics` was re-pinned for on 2026-08-14, in the mirror
#               direction (that field can empty a record, this one can mask one). Measured, the
#               entropy pass changes 0 of 1,652 persisted tails across the whole preserved corpus,
#               so no replicate's DIAGNOSTICS are expected to move; the envelope moves regardless,
#               because the digest binds the complete non-variant map and is deliberately not clever
#               enough to exempt a knob whose measured effect is currently zero.
#               Old receipts SHOULD stop verifying. On this box exactly one exists
#               (`.looplab/speculation-quality.receipt.json`) and it was ALREADY dead before this
#               change on four independent axes — `calibration_profile_digest`,
#               `implementation_digest`, `runtime_scope_sha256`, and the `forced_debug` ->
#               `no_forced_debug` scorer-fidelity rename — so the revocation this line records costs
#               nothing that was not already spent.
#   2026-08-15  + repair_log_tools  (may the crash/timeout TRIAGE judge query the failed eval's
#               stage logs instead of diagnosing from `res.stderr[-500:]`). The 'field set changed
#               too' branch: an AST scan reports exactly this one field ADDED and none removed, so
#               `_EXPECTED_FIELD_COUNT` goes 208 -> 209 and both pins are re-set.
#               INERT for a calibration replicate, twice over — the profile's toy workload is the
#               `solution.py` path, which writes no per-stage `<stage>.log`, so `repair_log_tools`
#               resolves to `None` at `needs_log_snapshot` before any provider is built; and a
#               replicate that never fails never reaches `_triage_crash` at all. The guard is
#               deliberately not clever enough to exempt an inert knob, and re-pinning is right
#               rather than merely necessary for the usual reason: this field decides what EVIDENCE
#               a paid stop decision is made on, and an envelope that cannot state that is not the
#               envelope a later receipt would be compared against. Old receipts SHOULD stop
#               verifying; the one on this box was already dead on four independent axes.
#   2026-08-16  + memo_verdict_cue  (does the deep-research takeaway PUSHED into every
#               Researcher / crash-triage / repair-critic prompt carry the memo's own verifier
#               tally). The 'field set changed too' branch: an AST scan of `Settings` against
#               `d7d4b436` reports exactly this one field ADDED and none removed, so
#               `_EXPECTED_FIELD_COUNT` goes 209 -> 210 and both pins are re-set.
#               INERT for a calibration replicate, and inert twice over: the profile's toy
#               workload records no `research_completed` row, so `state.research` is empty and
#               `_state_brief` emits no takeaway line at all — the cue has nothing to qualify —
#               and the replicate's roles are `cli/__init__.py::_make_calibration_roles`, whose
#               researcher is the GPU probe rather than an `LLMResearcher`. The guard is
#               deliberately not clever enough to exempt an inert knob, and re-pinning is right
#               rather than merely necessary for the reason `repair_log_tools` above records:
#               this field decides what a paid role is TOLD, and an envelope that cannot state
#               that is not the envelope a later receipt would be compared against.
#               Old receipts SHOULD stop verifying, and the revocation costs nothing that was
#               not already spent: the one receipt on this box
#               (`.looplab/speculation-quality.receipt.json`) carries
#               `calibration_profile_digest: sha256:e3fd5e10…`, which is neither the digest
#               being replaced nor the new one — it was already dead before this change, as the
#               two entries above it also record.
#   2026-08-18  + cadence_while_evaluating  (may the node-count cadences fire at a creation
#               decision point that still has evaluations IN FLIGHT — backlog F1i). The 'field set
#               changed too' branch, and verified that way rather than from the count: diffing the
#               `Settings` field set against `fbbab725` reports exactly `['cadence_while_evaluating']`
#               added and nothing removed, so a +2/-1 cannot be hiding behind the +1.
#               `_EXPECTED_FIELD_COUNT` goes 210 -> 211 and both pins are re-set.
#               INERT for a calibration replicate, and inert three times over rather than by
#               accident: the profile already pins `coverage_context: False`, `concept_pivot: False`
#               and `strategist_backend: "off"`, which is EVERY consumer of the predicate this knob
#               governs — so no cadence it could release is wired at all, and the replicate's toy
#               evals are instantaneous besides. It is therefore left at the shipped `True` rather
#               than pinned False: a False here would assert an intent about behaviour that cannot
#               occur, and the three overrides above are what actually holds the line. The guard is
#               deliberately not clever enough to exempt an inert knob, and re-pinning is right
#               rather than merely necessary for this list's usual reason — the field decides
#               whether a run may spend on a Strategist consult or a classifier pass WHILE a GPU is
#               busy, and an envelope that cannot state that is not the envelope a later receipt
#               would be compared against. Old receipts SHOULD stop verifying, and the revocation
#               costs nothing that was not already spent: `speculation_implementation_digest` hashes
#               every shipped `.py` and this change moves it regardless.
#   2026-08-19  + gpu_footprint_cue  (do the two Researcher prompts state what a LARGER
#               `footprint.gpus` actually does, against a shipped wording the scheduler
#               contradicts — docs/BACKLOG.md §0.15). The 'field set changed too' branch, and
#               verified that way rather than from the count: diffing the `Settings` field set
#               against `086ca5b4` reports exactly `['gpu_footprint_cue']` added and nothing
#               removed, so a +2/-1 cannot be hiding behind the +1. `_EXPECTED_FIELD_COUNT` goes
#               211 -> 212 and both pins are re-set.
#               INERT for a calibration replicate, and inert twice over: the profile's researcher
#               is `cli/__init__.py::_make_calibration_roles`' GPU probe rather than an
#               `LLMResearcher`, so no footprint clause is ever composed; and the engine cue it
#               also governs is silent unless `_repo_spec` and a GPU pool are both present, which
#               the toy calibration workload has not. Left at the shipped `True` for
#               `cadence_while_evaluating`'s reason — a `False` here would assert an intent about
#               behaviour that cannot occur. Re-pinning is right rather than merely necessary for
#               this list's usual reason: the field decides what a PAID role is told about the
#               hardware it may ask for, and an envelope that cannot state that is not the
#               envelope a later receipt would be compared against. Old receipts SHOULD stop
#               verifying, and the revocation costs nothing that was not already spent —
#               `speculation_implementation_digest` hashes every shipped `.py` and this change
#               moves it regardless.
#   2026-08-20  inline_repair_reasons gained `unclassified` (12 -> 13 members). **The 'field set
#               UNCHANGED' branch again**, and the SECOND time this exact default has moved it —
#               the first is in the first history block above — so the precedent for reading it as
#               case (2) rather than as the refactor the assertion message warns about is this
#               field's own. The DERIVATION did not move: `_declared_settings_json_defaults` reads
#               `Settings`' declared defaults, the default is bound BY IDENTITY to
#               `core/models.py::FAILURE_REASONS`, and that registry is what gained a member.
#               VERIFIED BY DIFFING THE FIELD SET AGAINST MASTER, NOT FROM THE COUNT, as the
#               2026-08-14 entries prescribe: an AST scan of `Settings`' annotated assignments in
#               both trees reports [] added and [] removed, so `_EXPECTED_FIELD_COUNT` STAYS at 212
#               and a +1/-1 cannot be hiding behind an unchanged integer. Only the digest moves.
#               IT IS NOT INERT FOR A CALIBRATION REPLICATE, and that is what makes re-pinning right
#               rather than merely necessary. `unclassified` is what `engine/evaluate.py` records
#               when the failure DIAGNOSTICIAN was wired, asked, and could not answer, and its
#               presence in this list is what keeps such a node repairable at all — under a TIGHTER
#               bound than the reason it replaces (`triage._RULE_BLIND_CRASH_ATTEMPTS`, 12, rather
#               than the caller's cap). So a replicate calibrated before it and one calibrated after
#               can spend a different number of EVALUATIONS on the same failing node, which is
#               precisely what a speculation receipt asserts about. Old receipts SHOULD stop
#               verifying, and the revocation costs nothing that was not already spent:
#               `speculation_implementation_digest` hashes every shipped `.py` and this change moves
#               it regardless.
#   2026-08-20  + train_monitor_contract (212 -> 213). The "field set changed too" branch, and
#               verified by DIFFING THE FIELD SET rather than reading the count, as the
#               2026-08-14 entries prescribe: an AST scan of `Settings`' annotated
#               assignments against master reports exactly [train_monitor_contract] added
#               and [] removed, so no +1/-1 pair is hiding behind the new integer.
#   2026-08-21  REBASE onto master, and BOTH pins re-measured over the rebased tree rather than
#               carried from either side. The branch adds three fields to master's set
#               (`llm_budget_usd`, `hide_empty_tools`, `developer_probe_confine`) — 213 -> 216 —
#               and the digest hashes every shipped `.py`, so 45 commits of master move it whatever
#               the field set does. Old receipts SHOULD stop verifying: `developer_probe_confine`
#               ships ON and FAILS CLOSED, so a replicate calibrated where the kernel offers
#               Landlock and one calibrated where it does not can spend a different number of
#               Developer turns on the same node.
#   2026-08-27  + developer_step_feedback_command (216 -> 217). The "field set changed too" branch,
#               and verified by DIFFING THE FIELD SET rather than reading the count, as the
#               2026-08-14 entries prescribe: an AST scan of `Settings`' annotated assignments
#               against HEAD reports exactly ['developer_step_feedback_command'] added and []
#               removed, so no +2/-1 is hiding behind the new integer. The knob names an
#               operator-pinned developer command the plan loop runs BETWEEN steps, handing its
#               output to the next step (doc 53 item 10a). INERT for a calibration replicate twice
#               over: the profile's toy workload is the `solution.py` path, which declares no
#               `developer_commands` at all, so `_step_feedback_command_name` resolves to "" before
#               anything is executed; and the replicate's roles come from
#               `cli/__init__.py::_make_calibration_roles`, which builds no `LLMRepoDeveloper`. The
#               guard is deliberately not clever enough to exempt an inert knob, and re-pinning is
#               right rather than merely necessary for the reason `repair_log_tools` above records:
#               this field decides what a paid role is TOLD, and it also decides whether a node
#               build spends an extra ~40 s of wall clock per editing step — so a replicate
#               calibrated with it set and one without are genuinely different. Old receipts SHOULD
#               stop verifying; `speculation_implementation_digest` hashes every shipped `.py` and
#               this change moves it regardless.
_EXPECTED_FIELD_COUNT = 217


def test_the_digest_did_not_change_when_the_profile_moved():
    """A receipt gate. If the digest shifts for any reason OTHER than a real schema change, every
    calibration receipt issued earlier stops verifying and the gate refuses runs that were
    legitimately calibrated — with no error that says why.

    Pinning the digest AND the field set separates the two causes:
      * field set unchanged, digest moved -> EITHER a refactor changed the DERIVATION (the bug this
        test exists for: no already-issued receipt would verify again, for nothing), OR a default
        VALUE moved deliberately. The digest binds the complete settings map, so the second is a
        real envelope change and is resolved by re-pinning WITH a history line, not by hunting.
      * field set changed too              -> `Settings` legitimately grew or shrank; the calibration
        envelope really is different, old receipts SHOULD be invalidated, and both pins get re-set
        deliberately with a line in the history above.
    """
    covered = frozenset(Settings.model_fields) - set(SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS)
    assert frozenset(SPECULATION_CALIBRATION_PROFILE_SETTINGS) == covered, (
        "the profile no longer covers exactly the non-variant Settings fields")
    schema_changed = len(SPECULATION_CALIBRATION_PROFILE_SETTINGS) != _EXPECTED_FIELD_COUNT
    assert SPECULATION_CALIBRATION_PROFILE_DIGEST == _EXPECTED_DIGEST, (
        f"the calibration profile digest moved to {SPECULATION_CALIBRATION_PROFILE_DIGEST}. "
        + (f"Settings also changed size ({_EXPECTED_FIELD_COUNT} -> "
           f"{len(SPECULATION_CALIBRATION_PROFILE_SETTINGS)}), so this is real schema growth: re-pin "
           "BOTH constants above and add a line to the field-set history."
           if schema_changed else
           "The field set is UNCHANGED, so one of TWO things happened and they need opposite "
           "answers. (1) A refactor changed the DERIVATION — that silently invalidates every "
           "already-issued speculation receipt for no reason at all. Do not re-pin; find the "
           "change. (2) A DEFAULT VALUE moved on purpose — the digest binds the complete "
           "non-variant settings map, not just its field NAMES, so a deliberate default change "
           "really does move the envelope and old receipts SHOULD stop verifying. Three are "
           "already recorded in the history above (concept_retag_every, redact_output, "
           "inline_repair_reasons). If yours is (2), re-pin and add a line saying WHICH default "
           "moved and why a replicate calibrated before it is genuinely different."))


def test_the_engine_still_re_exports_the_identity_it_no_longer_derives():
    """The engine, the CLI and the tests all spell these on `engine.orchestrator`. Moving the
    derivation must not have moved the NAME out from under them."""
    from looplab.engine import orchestrator

    assert orchestrator.SPECULATION_CALIBRATION_PROFILE_DIGEST is (
        SPECULATION_CALIBRATION_PROFILE_DIGEST)
    assert orchestrator.SPECULATION_CALIBRATION_PROFILE_SETTINGS is (
        SPECULATION_CALIBRATION_PROFILE_SETTINGS)


def test_the_quality_reader_no_longer_reaches_UP_into_the_engine_for_the_profile():
    """The finding's headline. `speculation_quality` is the module the calibration module's docstring
    was written to keep engine-free."""
    source = inspect.getsource(speculation_calibration)
    assert "looplab.engine" not in source, (
        "the calibration module now imports the engine — the cycle it exists to prevent")

    from looplab.search import speculation_quality

    quality = inspect.getsource(speculation_quality)
    assert "from looplab.engine.orchestrator import" not in quality, (
        "speculation_quality imports the orchestrator again")


def test_the_search_to_engine_edge_is_gone():
    """This asserted exactly ONE remaining `search` → `engine` import and named it: the cluster of
    event-log helpers behind `engine.finalize.incomplete_finalize_scope`, recorded as still open
    under SE-07. Its own note said "when it goes, this test is what says the layer is finally
    clean". It has gone (doc 25 XP-07) — the five helpers moved DOWN to `events/finalize_scope.py`,
    which `search` may import, with `engine/finalize.py` keeping re-exports for its serve consumers.

    So this now asserts ZERO, and is kept rather than deleted: a count of one was the thing worth
    watching while the edge existed, and a count of zero is the thing worth watching now. Nothing
    else in the suite fails if a NEW upward import appears in a `search` module — the sibling guard
    in `test_speculation_quality_gate` covers only `speculation_quality.py`, and
    `test_agents_search_direction` covers the other direction."""
    from pathlib import Path

    search = Path(__file__).resolve().parents[1] / "looplab" / "search"
    edges = sorted(
        f"{path.name}:{index}"
        for path in search.glob("*.py")
        for index, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1)
        if "looplab.engine" in line and line.lstrip().startswith(("from ", "import ")))
    assert not edges, f"search reaches up into the engine again: {edges}"


# ------------------------------------------------------------------ the profile stays derivable

def test_the_profile_covers_every_settings_field_except_the_declared_variants():
    """The profile is "every Settings field except the experiment inputs". A field added to Settings
    and not to the profile would silently leave part of the runtime envelope out of the digest."""
    expected = set(Settings.model_fields) - set(SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS)
    assert set(SPECULATION_CALIBRATION_PROFILE_SETTINGS) == expected


def test_no_variant_field_leaks_into_the_profile():
    """`max_nodes` and friends vary per calibration run BY DESIGN; including one would make every
    run's digest unique and the gate would accept nothing."""
    assert not (set(SPECULATION_CALIBRATION_PROFILE_SETTINGS)
                & set(SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS))


def test_the_profile_is_canonical_snapshot_JSON():
    """The quality reader compares it after `json.loads()`. A tuple or a non-JSON scalar would make
    the persisted receipt un-reproducible by any reader that went through JSON."""
    import orjson

    assert orjson.loads(orjson.dumps(SPECULATION_CALIBRATION_PROFILE_SETTINGS,
                                     option=orjson.OPT_SORT_KEYS)) == (
        SPECULATION_CALIBRATION_PROFILE_SETTINGS)


def test_the_profile_ignores_the_launcher_environment(monkeypatch):
    """`BaseSettings()` is forbidden in the derivation: its env precedence would make a
    source-OWNED profile depend on whose machine built it, so two honest calibrations would disagree.

    Driven by RE-DERIVING under a poisoned environment rather than by `importlib.reload`. Reloading
    this module would hand every already-imported holder — `engine/orchestrator.py` re-exports both
    constants — a stale object, which is a suite-wide state leak in exchange for testing the same
    property one function call away.
    """
    monkeypatch.setenv("LOOPLAB_MAX_NODES", "999")
    monkeypatch.setenv("LOOPLAB_TIMEOUT", "12345")
    monkeypatch.setenv("LOOPLAB_LLM_MODEL", "some-other-model")

    declared = speculation_calibration._declared_settings_json_defaults()
    for field, poisoned in (("max_nodes", 999), ("timeout", 12345.0),
                            ("llm_model", "some-other-model")):
        assert declared.get(field) != poisoned, (
            f"the profile read {field} from the environment instead of the schema default")


def test_the_derivation_refuses_a_required_settings_field():
    """A required field has no declared default to read, so the profile cannot be inferred at all.
    Failing loudly at import beats emitting a digest that silently omits it."""
    source = inspect.getsource(speculation_calibration._declared_settings_json_defaults)
    assert "is_required()" in source and "raise RuntimeError" in source


@pytest.mark.parametrize("name", ["_declared_settings_json_defaults",
                                  "SPECULATION_CALIBRATION_PROFILE_SETTINGS",
                                  "SPECULATION_CALIBRATION_PROFILE_DIGEST"])
def test_the_orchestrator_no_longer_derives_the_profile_itself(name):
    """A re-derivation in the engine would be a second answer to a receipt question — and the two
    could disagree without either being obviously wrong."""
    from looplab.engine import orchestrator

    source = inspect.getsource(orchestrator)
    assert f"{name} = " not in source, f"engine/orchestrator.py re-derives {name}"
