"""Metric SALVAGE — recover a metric a failed eval had ALREADY produced, deterministically.

THE RUN THIS EXISTS FOR. `runs/rubertlite-dr-unified-v5` node 0 trained for 76 minutes on two
H200s, exited 0, wrote a complete SentenceTransformer checkpoint, ran the scorer at the end of its
own `train` stage and printed `RECALL@100: 0.743250` — the EXACT line the operator's declared
metric reader matches (`{"kind":"stdout_regex","pattern":"RECALL@100: ([0-9.]+)"}`). The node was
failed with `reason: no_metric` because the stage manifest declared the checkpoint at
`.../unified-baseline_rubert-tiny-lite/final/model.safetensors` while the testbed composes its
output dir as `<run_name>_<model>`. A one-line path error threw away 76 GPU-minutes AND the number
those minutes bought — a number that was sitting in the captured stdout of the very stage that
failed.

TWO INDEPENDENT GATES had to hold for that to happen, and both are in `command_eval._run_stages`:

  1. the `expect_failed` early return hard-codes `metric=None` — it never asks the reader at all;
  2. the crash branch's `_salvaged = read_metric(...)` is gated on `_i == len(stages) - 1`, and
     `train` is not the last stage (the operator's protected `score` stage is appended after it).

So this module asks the question those two branches skip, at the ONE place that can still act on
the answer: `engine/evaluate.py`, after the attempt loop has decided the eval is not `ok` and
BEFORE the node's single terminal event is written. Salvage is never a second terminal — invariant
#2 is not negotiable — it changes WHICH terminal is written, once.

--------------------------------------------------------------------------------------------------
DETERMINISTIC FIRST, AND HERE, DETERMINISTIC ONLY.

The brief asked whether an LLM should read the number out of the output when no declared reader
can. It should not, and the measured case is the argument: the DECLARED reader finds this metric.
The value is in `train.log`/the captured stdout in exactly the operator's own format, so the whole
76 minutes is recoverable with a regex the operator already wrote.

The reason not to add the model tier is the trust boundary CLAUDE.md and
`adapters/repo_developer.py` both name: the operator's `cmd` is the protected final `score` stage
PRECISELY so the agent cannot rewrite how a run is scored. The agent writes the training script,
which means the agent writes the text an extractor would read. A declared reader is spoofable only
in the way the operator ALREADY accepted when they chose `stdout_regex` — an LLM extractor widens
that to "any number, anywhere, in any file the agent wrote", which is a scoring path the operator
never agreed to and cannot audit. Two further costs: `fold` must stay deterministic (invariant #5)
and a terminal that depends on a provider call is a terminal a dead endpoint can withhold — the
exact shape of the 2345-repair incident.

If a model tier is ever added it must (a) go through THIS module's provenance record, (b) be
restricted to files the stage itself declared in `expect.files`, and (c) be a separate `source`
value so a salvaged-by-model metric is distinguishable from a salvaged-by-reader one. The hook is
`SALVAGE_SOURCES`; nothing in this module calls a model.

--------------------------------------------------------------------------------------------------
WHAT MAKES A SALVAGED METRIC TRUSTWORTHY. Four conditions, all enforced below:

  * DECLARED READER. The value comes from `eval_spec["metric"]` — the operator's own spec, byte for
    byte, the same object `run_command_eval` reads with. Never a spec the agent authored, never a
    widened one. `kind == "adapter"` is REFUSED outright: it EXECs an agent-authored module, which
    is the same rule `run_command_eval` already applies to `metrics`/`constraints` readers and
    `validate_cross_check` applies to the drift reader. (Refusing it also means salvage never needs
    the docker `wrap`, since every remaining reader is in-process over host paths.)
  * THIS ATTEMPT'S OWN FRESH OUTPUT. The stdout is the failing eval's captured stdout and the
    workdir is this node's; FILE readers get `since = this attempt's start`, so a prior attempt's
    artifact in a deliberately-reused workdir, or a foreign experiment's, cannot be salvaged. That
    is the same `_file_is_fresh` gate the primary read uses and it is the one that stops the
    workdir-reuse trap from becoming a false promotion.
  * THE FAILURE WAS NOT A FAILURE OF MEASUREMENT OR OF TRUST. A closed allow-list
    (`salvage_condition`), not "anything that is not no_metric". Drift, a timed-out eval and a
    failed `expect.assert`/`check` are refused, each for its own reason — see that function.
  * IT IS RECORDED AS SALVAGED. `SalvagedMetric.as_event()` rides on `node_evaluated` as
    `metric_provenance`, and under the default policy the node also carries a `metric_salvaged`
    VIOLATION row, which the fold turns into `feasible = False` — so `RunState.feasible_nodes()`,
    and therefore champion selection and breeding, structurally cannot mistake it for a measured
    metric. A provenance field alone would satisfy "can tell" and not "does tell": nothing on the
    selection path reads an unknown event key.

`METRIC_SALVAGE_MODES` is the operator's lever over that last condition, defaulting to the
conservative rung.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# WHAT A SALVAGED METRIC IS ALLOWED TO DO, as a closed, ordered vocabulary.
#
#   off    — never salvage. Today's behaviour, byte-identical: the node fails and the number is lost.
#   audit  — salvage. The terminal is `node_evaluated`, the metric and its provenance are on the
#            event, the node is EVALUATED (it counts, it is in the budget, the UI and the digest and
#            the lineage all see it) — and it carries a `metric_salvaged` violation, so it is not
#            `feasible` and therefore cannot be the reported champion or be bred from.
#   select — salvage as a first-class metric: no violation row, fully feasible. The operator has
#            accepted salvaged metrics as selectable.
#
# DEFAULT IS `audit`, and the trade-off is real and worth stating rather than hiding: a salvaged
# baseline is NOT bred from, so a run whose only node was salvaged proposes fresh ideas instead of
# improving that one. The conservative rung is still the right default for a scoring path — the
# whole point of the protected `score` stage is that "this number is comparable to the others" is a
# claim only the operator's own measurement gets to make — and `select` is one setting away for an
# operator who has looked at the provenance and disagrees.
METRIC_SALVAGE_MODES = ("off", "audit", "select")
DEFAULT_METRIC_SALVAGE = "audit"

# Where a salvaged value came from. A CLOSED set so a reader of the event can tell the rungs apart,
# and so an added rung (an LLM extraction, say) has to declare itself rather than inherit the
# credibility of the deterministic one.
#   eval_result     — the RunResult already carried it (`_run_stages`' own last-stage salvage read);
#                     nothing was re-read, this is a relabelling of a value the eval produced.
#   declared_reader — the operator's `eval_spec["metric"]` reader, re-asked over the failing
#                     attempt's own stdout + workdir.
#   relocated_file  — the operator's FILE reader, re-asked at the fresh same-basename path the stage
#                     actually wrote (see `_relocated`). Still the operator's spec; only the path
#                     moved, and only inside the workdir.
SALVAGE_SOURCES = ("eval_result", "declared_reader", "relocated_file")

# The failure conditions under which a recovered metric may be admitted. The slug rides on the
# provenance record, so an operator reading a salvaged node learns which of these it was.
SALVAGE_CONDITIONS = ("artifact_contract", "stage_failed", "eval_failed")

# Failure REASONS (`engine/triage.py::_failure_reason`) a metric is never salvaged under. Each is a
# separate argument, not a list of similar things:
#
#   drift  — the metric WAS read and then deliberately discarded because an independent reader could
#            not corroborate it (`command_eval._drift`). Salvaging it re-admits precisely what the
#            trust gate rejected, which is worse than losing it.
#   setup  — `setup failed:` short-circuits before the eval's own work starts, so any value the
#            readers can see predates this attempt entirely.
#   timeout — the hard deadline fired, which means the run was still doing work when it was killed.
#            A training loop that prints its metric line every epoch is the COMMON case, not an
#            exotic one, so "the number in the output" and "the number this experiment achieved" are
#            not the same fact here and nothing deterministic tells them apart. The brief asked for
#            "a timeout after scoring" to be salvageable; the case where that IS distinguishable is
#            already handled and shipped — the STALL watchdog fires when the process went SILENT,
#            i.e. when it had finished talking, and `evaluate.py`'s `ok` gate has salvaged that since
#            before this module existed (`res.stalled`). A hard deadline carries no such evidence.
NEVER_SALVAGED_REASONS = frozenset({"drift", "setup", "timeout"})

# Stage statuses that VETO salvage even when a reader can find a number.
#
# `check_failed` is the load-bearing one and the distinction it draws is the whole reason `expect`
# has two halves (`command_eval`'s STAGE SUCCESS CONTRACT block). `expect.files` is a claim about
# WHERE the stage wrote; it fails on a path typo, which is the measured v5 defect and says nothing
# about whether the work happened. `expect.assert` / `check` is a claim about WHAT the stage did —
# the half that caught "hard negatives for 9,364 of 764,676 queries" — and salvaging past it would
# re-create exactly the defect that contract exists to end, one field over.
#
# The symmetric worry, that `expect_failed` can ALSO be substantive ("the checkpoint was never
# written at all"), is answered by the reader rather than by a rule: if the training never produced
# a checkpoint then the scorer never ran, so there is no fresh value for the declared reader to
# find and salvage abstains on its own. The deterministic read IS the discriminator, which is
# another reason not to put a model in that seat.
VETO_STAGE_STATUSES = frozenset({"check_failed"})


def settle_mode(mode) -> str:
    """The effective salvage mode for an arbitrary configured value.

    Total over junk on purpose: this is read off an engine attribute an operator, a Strategist or a
    resumed snapshot can set, and an unrecognised value must not silently mean the PERMISSIVE rung.
    Anything not in the vocabulary settles to the conservative default rather than to `select`.
    """
    return mode if mode in METRIC_SALVAGE_MODES else DEFAULT_METRIC_SALVAGE


def salvage_condition(res, reason: str) -> Optional[str]:
    """Which salvage condition this failed eval is (a `SALVAGE_CONDITIONS` slug), or None.

    A rule with a truth table rather than a compound `if` in the attempt loop, because every branch
    is one an operator has to be able to argue with, and because the inline version would only be
    reachable by driving a whole sandboxed evaluation that failed in exactly the right way — the
    same cost `engine/evaluate.py`'s other named rules were extracted for (doc 25 ES-03).

    THE PROCESS MUST HAVE FINISHED. A non-zero exit is not salvageable, and that is a deliberate
    refusal of one of the cases the brief named ("a late-stage crash"). A process the OS or the
    runtime stopped did not complete, so a metric line in its output is a line it printed on the way
    somewhere — a per-epoch `RECALL@100:` from epoch 4 of 20 reads identically to a final one, and
    promoting it would make the search compare a partial run against complete ones. That is the
    false promotion this module must not create, and it is not a hypothetical shape: intermediate
    per-epoch metric lines are what `asha_monitor` exists to read.
    The ONE non-zero exit that IS evidence of completion is already carved out and shipped:
    `RunResult.stalled` is the authenticated verdict that the process went SILENT while alive, i.e.
    that it had finished talking before the watchdog killed it (`_salvageable_stall`, which also
    subtracts the DIVERGED case). `evaluate.py`'s `ok` gate has salvaged that since before this
    module existed; admitting it here too is what lets a stalled stage's FILE artifact be read, not
    just the value it happened to have already printed.

    Reads only `res`'s own fields and the classifier's `reason`; no I/O, no model.
    """
    if reason in NEVER_SALVAGED_REASONS or getattr(res, "timed_out", False):
        return None
    if getattr(res, "exit_code", 0) != 0 and not getattr(res, "stalled", False):
        return None
    stages = [s for s in (getattr(res, "stages", None) or []) if isinstance(s, dict)]
    if not stages:
        # Single-command eval: there is no per-stage record to consult. `read_metric` has already
        # run over this exact stdout in that path, so rung 1 is very nearly a no-op here — what can
        # still differ is a FILE reader whose relocated twin exists (rung 2).
        return "eval_failed"
    status = str(stages[-1].get("status") or "")
    if status in VETO_STAGE_STATUSES:
        return None
    if status == "expect_failed":
        return "artifact_contract"
    if status == "fail":
        # Only reachable under the `stalled` carve-out above — a plain crashed stage returned None
        # already.
        return "stage_failed"
    if status in ("ok", "reused"):
        # Every stage passed and the reader looked at this exact output and found nothing: the
        # honest `no_metric`. Re-asking the SAME reader over the SAME bytes cannot answer
        # differently, so the only thing left to try is the relocated-file rung.
        return "eval_failed"
    return None                          # `timeout`, and any status a future writer adds


@dataclass(frozen=True)
class SalvagedMetric:
    """One recovered metric and the complete account of how it was recovered.

    Frozen and event-shaped because this record is the ONLY thing standing between a salvaged
    number and a measured one everywhere downstream — the terminal event, the fold, the champion
    path, the UI and whoever reads the run six months later. A salvage that could not describe
    itself would be a salvage nobody can refuse.
    """
    metric: float
    condition: str            # a SALVAGE_CONDITIONS slug — what the eval was doing when it failed
    source: str               # a SALVAGE_SOURCES slug — which rung recovered it
    reader: str               # the DECLARED reader kind the value came out of
    stage: str = ""           # the stage that failed, when the eval was staged
    detail: str = ""          # rung-specific evidence (the relocated path, …)

    def as_event(self) -> dict:
        """The `metric_provenance` payload for `node_evaluated`.

        `salvaged: True` is spelled out rather than implied by the record's presence: a reader that
        checks one key must not have to know that this dict only ever exists for salvaged metrics,
        because the natural next change is to write provenance for MEASURED metrics too.
        """
        row = {"salvaged": True, "condition": self.condition, "source": self.source,
               "reader": self.reader}
        if self.stage:
            row["stage"] = self.stage
        if self.detail:
            row["detail"] = self.detail[:300]
        return row

    def violation_rows(self, mode: str) -> list:
        """The `violations` rows this salvage contributes under `mode` — the ENFORCED half.

        Shaped like `command_eval._violations`' rows ({name, value, max, min}) because that is what
        `events/replay.py::_on_node_evaluated` folds and what the UI renders; `salvage` carries the
        provenance a second time so the exclusion can explain itself at the point it is applied,
        without a reader having to join two keys of the same event.

        `max`/`min` are None: this is not a numeric bound that was breached. The row exists to make
        `n.feasible` False — the fold's rule is `feasible = not violations`, so any row does it —
        and to say WHY in the same breath.
        """
        if settle_mode(mode) != "audit":
            return []
        return [{"name": "metric_salvaged", "value": self.metric, "max": None, "min": None,
                 "salvage": self.as_event()}]


def _usable(value) -> Optional[float]:
    """`value` as a finite float, or None. A NaN/inf salvage would fold into `Node.metric` and poison
    every metric-keyed sort the search runs; `_finite_metric` drops it at the fold, but abstaining
    here is what keeps the node's TERMINAL honest rather than leaving it claiming a metric the fold
    then silently discards."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _relocated(metric_spec: dict, workdir: str, since: Optional[float]) -> Optional[tuple]:
    """RUNG 2: the operator's FILE reader, re-pointed at the fresh same-basename file the stage
    actually wrote — `(spec, found_path)` or None.

    This is the metric-reader twin of the diagnostic `verify_stage_artifacts` already emits: the
    v5 defect was a declared path that disagreed with disk by one directory name, and a run whose
    metric reader names a FILE has exactly the same exposure as one whose `expect.files` does. It
    stays inside the declaration in both senses that matter — it is still the operator's spec, still
    the operator's basename, and `_artifact_written_elsewhere` searches only INSIDE the workdir, only
    files this attempt wrote, and only up to a bounded number of directories.

    Deliberately NOT extended to `stdout_*` readers: there is no path to relocate, and a stdout
    reader that found nothing found nothing.
    """
    from looplab.runtime import command_eval
    if command_eval.spec_kind(metric_spec) not in command_eval.READERS_REQUIRING_PATH:
        return None
    rel = metric_spec.get("path")
    if not isinstance(rel, str) or not rel.strip():
        return None
    found = command_eval._artifact_written_elsewhere(workdir, rel, since)
    if not found:
        return None
    return dict(metric_spec, path=found), found


def read_salvageable_metric(metric_spec, stdout: str, workdir: str,
                            since: Optional[float]) -> Optional[tuple]:
    """Ask the OPERATOR'S declared reader for a metric this failed eval already produced.

    Returns `(value, source, reader_kind, detail)` or None. Pure with respect to the run: it reads
    the node's own workdir and the captured stdout and nothing else, and it appends nothing.

    `wrap` is deliberately not a parameter. Every reader that survives the `adapter` refusal below
    parses HOST files or the already-captured stdout in-process — under the container tier the
    workdir is the bind mount, so the host paths are the same bytes — and the ONE reader that needs
    a container is the one salvage must never run.
    """
    from looplab.runtime import command_eval
    if not isinstance(metric_spec, dict):
        return None
    kind = command_eval.spec_kind(metric_spec)
    if kind == "adapter":
        # An agent-authored module, EXECd. `run_command_eval` already refuses it for the
        # metrics/constraints gates and `validate_cross_check` for the drift reader, both with the
        # same one-line reason: an agent-authored reader defeats the trust boundary. A salvage
        # reader decides whether a FAILED node counts as successful, so it is the last place that
        # rule should be relaxed.
        return None
    if kind not in command_eval.METRIC_READERS:
        return None
    value = _usable(command_eval.read_metric(stdout, str(workdir), metric_spec,
                                             wrap=None, since=since))
    if value is not None:
        return value, "declared_reader", kind, ""
    moved = _relocated(metric_spec, str(workdir), since)
    if moved is not None:
        spec, found = moved
        value = _usable(command_eval.read_metric(stdout, str(workdir), spec, wrap=None, since=since))
        if value is not None:
            return value, "relocated_file", kind, f"read from {found!r} (declared {metric_spec.get('path')!r})"
    return None


def salvage(res, reason: str, metric_spec, workdir: str, since: Optional[float],
            mode: str = DEFAULT_METRIC_SALVAGE) -> Optional[SalvagedMetric]:
    """THE salvage decision for one failed eval attempt: a `SalvagedMetric`, or None.

    Order is the argument. The CONDITION is settled first — from the eval's own record, with no
    filesystem access at all — so a refused failure class costs nothing and, more importantly, so
    the refusal cannot be talked out of by a value that happens to be readable. Only then is the
    declared reader asked.

    `since` is the ATTEMPT's start wall-clock, not the eval's. It is deliberately the looser of the
    two available floors (it precedes `run_command_eval`'s own `_eval_started`, which is taken after
    `setup`), and it is still strictly sufficient for the property that matters: every artifact of
    every PREVIOUS attempt of this node is older than it, because the workdir persists across
    attempts and each attempt stamps a fresh start. Threading the exact `_eval_started` out of
    `run_command_eval` would need a new `RunResult` field; the write-up in the report says so.
    """
    if settle_mode(mode) == "off":
        return None
    condition = salvage_condition(res, reason)
    if condition is None:
        return None
    stage = str(getattr(res, "failed_stage", "") or "")
    # The eval already carried a value (the last-stage crash salvage in `command_eval._run_stages`,
    # or a host grader that scored a failed run). Relabel rather than re-read: re-asking the reader
    # could answer differently, and the number the eval reported is the one the trace already shows.
    #
    # NOT REACHABLE FROM `_evaluate` TODAY, and saying so is cheaper than leaving a decoy. Its `ok`
    # gate is `metric is not None and not timed_out and (exit == 0 or stalled)`, and
    # `salvage_condition` above admits exactly `not timed_out and (exit == 0 or stalled)` — so any
    # result that carries a metric AND passes the condition already took the `ok` path and never got
    # here. It is kept because `salvage` is a callable rule with its own truth table, and a rule that
    # abstains on a value it was handed would be wrong on its own terms; if either gate is ever
    # widened this is the branch that stops a carried metric from being silently re-derived.
    carried = _usable(getattr(res, "metric", None))
    if carried is not None:
        return SalvagedMetric(metric=carried, condition=condition, source="eval_result",
                              reader=_spec_kind(metric_spec), stage=stage)
    found = read_salvageable_metric(metric_spec, getattr(res, "stdout", "") or "", workdir, since)
    if found is None:
        return None
    value, source, reader, detail = found
    return SalvagedMetric(metric=value, condition=condition, source=source, reader=reader,
                          stage=stage, detail=detail)


def _spec_kind(metric_spec) -> str:
    from looplab.runtime import command_eval
    return command_eval.spec_kind(metric_spec)


# --- THE CAUSE, WHICH IS THE OTHER HALF OF THE ASK -----------------------------------------------
# Salvaging the metric and leaving the broken declaration in place would trade one silent failure
# for another: the node reads as successful and its next attempt (an operator `node_reset`, a
# stage-scoped re-run) walks into the identical contract failure, having learnt nothing. So the
# engine still asks the Developer to fix what broke — it just does not pay for a re-evaluation to
# find out whether the fix worked, which is the entire point of having salvaged the metric.
#
# The prompt is a CONTRACT (CLAUDE.md): it has to tell the model three things the ordinary repair
# prompt would get wrong here — that the experiment SUCCEEDED, that the metric is already recorded,
# and that the fix must be to the DECLARATION rather than to the training code, because rewriting
# the training code after the metric has been measured would leave the node's recorded result and
# its recorded code describing two different experiments.
SALVAGE_CAUSE_DIRECTIVE = (
    "[the experiment SUCCEEDED and its metric has already been recorded — do NOT re-run it]\n"
    "This node's evaluation produced a real metric ({metric}) via the run's declared metric reader, "
    "and the engine has salvaged it: the node is recorded as EVALUATED with that value. What failed "
    "was the DECLARATION around it, not the experiment:\n\n{error}\n\n"
    "Fix ONLY that declaration so the next attempt of this node does not hit the same failure — "
    "typically the path in `looplab_stages.json` (`expect.files`), or the eval spec's metric path. "
    "Do NOT change training/modelling code, hyperparameters, or anything that would alter the "
    "result: the metric above was measured against the code as it stands, and a repair that changes "
    "the experiment would make the recorded number describe code that never produced it. Return the "
    "corrected declaration and nothing else."
)


def cause_repair_context(salvaged: SalvagedMetric, error: str) -> str:
    """The repair prompt for a node whose metric was salvaged. See `SALVAGE_CAUSE_DIRECTIVE`."""
    return SALVAGE_CAUSE_DIRECTIVE.format(metric=salvaged.metric, error=(error or "").strip())
