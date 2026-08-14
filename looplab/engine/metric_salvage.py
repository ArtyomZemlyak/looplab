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
WHAT MAKES A SALVAGED METRIC TRUSTWORTHY. Five conditions, all enforced below:

  * DECLARED READER. The value comes from `eval_spec["metric"]` — the operator's own spec, byte for
    byte, the same SPEC OBJECT `run_command_eval` reads with (the same object; NOT the same input —
    see the next condition, which is the half this sentence used to be read as covering). Never a
    spec the agent authored, never a widened one. `kind == "adapter"` is REFUSED outright: it EXECs an agent-authored module, which
    is the same rule `run_command_eval` already applies to `metrics`/`constraints` readers and
    `validate_cross_check` applies to the drift reader. (Refusing it also means salvage never needs
    the docker `wrap`, since every remaining reader is in-process over host paths.)
  * AND THE BYTES IT READ ARE ACCOUNTED FOR. The SPEC is the operator's; the OUTPUT it is asked
    about is not necessarily. `run_command_eval`'s primary read runs over the FINAL command's stdout
    — the operator's protected `score` stage — while the `expect_failed`/`check_failed` early return
    sets `stdout` to the FAILING stage's output, which in Developer-manifest mode is a script the
    AGENT wrote. Running the operator's regex over agent-authored text is not the same trust
    statement as running it over the scorer's, and this module refused to say so until it recorded
    which it was: a Developer whose training script prints `RECALL@100: 0.999` would otherwise have
    it admitted every time one of its own stages failed a contract.
    So `SalvagedMetric.producer` names the stage that produced the bytes (`stage_producer`), it
    rides on the provenance record, and it is ENFORCED: `select` — the rung that lets a salvaged
    metric compete on equal terms — is honoured only for OPERATOR-declared output. Agent-produced
    bytes stay at the `audit` rung whatever the operator set, i.e. they keep the `metric_salvaged`
    violation and stay out of champion selection and breeding.
    The alternative was to restrict salvage to the protected `score` stage outright. That is
    refused because it deletes the case this module exists for: v5's number was printed by the
    `train` stage, and the `score` stage never ran. Recovering it and REFUSING to let it compete is
    the honest position; recovering it silently is not, and not recovering it is the 76 GPU-minutes.
  * THIS ATTEMPT'S OWN FRESH OUTPUT. The stdout is the failing eval's captured stdout and the
    workdir is this node's; FILE readers get `since = this attempt's start`, so a prior attempt's
    artifact in a deliberately-reused workdir, or a foreign experiment's, cannot be salvaged. That
    is the same `_file_is_fresh` gate the primary read uses and it is the one that stops the
    workdir-reuse trap from becoming a false promotion.
  * THE FAILURE WAS NOT A FAILURE OF MEASUREMENT OR OF TRUST. A closed allow-list
    (`salvage_condition`), not "anything that is not no_metric". Drift, a timed-out eval and a
    failed `expect.assert`/`check` are refused, each for its own reason — see that function.
  * THE GATES THAT QUALIFY A METRIC STILL RUN. A salvaged metric is a metric, so the operator's
    CONSTRAINTS, extra readers and drift cross-check must be applied to it exactly as
    `run_command_eval`'s tail applies them to a measured one — `salvage_gates` is that tail, re-asked
    over the failed attempt's own output. Without it a salvaged node reached the terminal with an
    EMPTY `violations` list, so under `select` it was `feasible=True` with the operator's hard
    constraints never applied and the drift cross-check never run, and could become champion.
  * IT IS RECORDED AS SALVAGED. `SalvagedMetric.as_event()` rides on `node_evaluated` as
    `metric_provenance`, and under the default policy the node also carries a `metric_salvaged`
    VIOLATION row, which the fold turns into `feasible = False` — so `RunState.feasible_nodes()`,
    and therefore champion selection and breeding, structurally cannot mistake it for a measured
    metric. A provenance field alone would satisfy "can tell" and not "does tell": nothing on the
    selection path reads an unknown event key.

`METRIC_SALVAGE_MODES` is the operator's lever over that last condition, defaulting to the
conservative rung.

--------------------------------------------------------------------------------------------------
AND ONE CASE THAT IS NOT A SALVAGE AT ALL. When the failure was an artifact CONTRACT, the Developer's
cause fix corrected the manifest, and the artifact check then PASSES against the corrected
declaration, the metric was never unmeasured — the pipeline produced the artifact and only the
sentence describing where it lived was wrong. Such a node is recorded as MEASURED: no violation, no
salvage provenance. See the RE-CHECK section at the bottom of this module for the three admission
rules and for what that promotion does NOT prove.
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

# The `violations` row name that carries the exclusion. A CONSTANT because it is now read as well as
# written: `violation_rows` mints it and `metric_unmeasured` (below) is the reader every knowledge
# writer asks, and a typo'd literal on either side is silent — the fold's rule is
# `feasible = not violations`, so a misspelled row still excludes the node from selection while the
# reader that has to keep it out of a cross-run LESSON stops recognising it.
SALVAGE_VIOLATION = "metric_salvaged"

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

# WHO WROTE THE BYTES the declared reader was asked about — a CLOSED two-value vocabulary, because
# the answer decides whether `select` applies (see `violation_rows`).
#
#   operator_stage — the output of a stage the OPERATOR declared, or of the single-command eval,
#                    which is the operator's own `cmd`. This is the same trust statement
#                    `run_command_eval`'s primary read makes: the operator chose which command
#                    scores, and the agent may not change that choice.
#   agent_stage    — the output of a stage from the Developer's `looplab_stages.json`. The operator's
#                    SPEC still selected the number, but the agent wrote the text it selected from.
#
# The distinction is not paranoia about a hypothetical: the reason the protected `score` stage
# exists is that the agent authors the training script, and the `expect_failed` / `check_failed`
# early returns hand salvage exactly that script's stdout.
OPERATOR_PRODUCED = "operator_stage"
AGENT_PRODUCED = "agent_stage"
SALVAGE_PRODUCERS = (OPERATOR_PRODUCED, AGENT_PRODUCED)

# The name `engine/eval_stages.py` appends the operator's protected scoring command under when the
# Developer supplies the pipeline. `validate_stages(reserved=("score",))` keeps a Developer manifest
# from claiming it, which is what makes the name a reliable owner marker here.
OPERATOR_PROTECTED_STAGE = "score"


# The violation row an UNBOUND metric contributes — an unsalvaged, fully measured node whose number
# has no proven referent (`runtime/metric_subject.py`, `Settings.metric_subject="require"`).
#
# IT IS THE SAME `metric_salvaged` ROW, ON PURPOSE, and inventing a second exclusion vocabulary here
# would be the mistake. The enforcement is `feasible = not violations`, so any row excludes the node;
# what a second NAME would cost is every reader that already knows this one — `metric_unmeasured`
# (the cross-run knowledge boundary), `serve/report.py`'s salvaged panel, `lessons_distill`'s row
# note, `speculation_quality`'s calibration filter. A new slug would silently stop being recognised
# by all four while still excluding the node, which is the exact half-wired shape this module's own
# `SALVAGE_VIOLATION` comment warns about.
#
# The two cases are also genuinely the same claim: "this node has a number that nobody has shown to
# be a measurement of what it says it measures". Salvage reaches it because the eval failed and the
# number was recovered; this reaches it because the eval succeeded and nothing binds the number to an
# artifact. `condition` distinguishes them for anyone who needs to.
UNBOUND_SUBJECT_CONDITION = "metric_subject_unbound"


def unbound_subject_violation_rows(prov, metric, mode: str) -> list:
    """The `violations` rows an unbound metric contributes under the `metric_subject` rung.

    `[]` under `off`/`audit` — those rungs RECORD and do not enforce; `[]` when the subject bound.
    Under `require`, one `metric_salvaged` row carrying the provenance, shaped exactly like
    `SalvagedMetric.violation_rows`' ({name, value, max, min} + the account) so the fold, the UI and
    every existing reader need to learn nothing.

    Note there is no `select`-shaped escape hatch here and there must not be. `select` exists because
    an operator can look at a salvaged value's provenance and decide the recovery was sound; there is
    nothing to look at for an unbound one — the whole content of the finding is that the record does
    not say what the number is about. The escape hatch is declaring a subject.
    """
    if str(mode or "") != "require" or not isinstance(prov, dict) or prov.get("subject_bound"):
        return []
    from looplab.runtime.metric_subject import unbound_message
    account = {"salvaged": False, "condition": UNBOUND_SUBJECT_CONDITION,
               "unbound_reason": str(prov.get("unbound_reason") or "unreadable"),
               "detail": unbound_message(prov)[:300]}
    return [{"name": SALVAGE_VIOLATION, "value": metric, "max": None, "min": None,
             "salvage": account}]

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
#
# `diverged` is here for the same reason `command_eval._salvageable_stall` subtracts it from the
# stall carve-out ("Divergence is the stronger, fail-closed verdict"). Without it the refusal of a
# diverged training rested ENTIRELY on `exit_code != 0`, and the kill/exit race defeats that: the
# non-finite losses drain, the process exits naturally before the tree-kill lands, and the
# divergence verdict is recorded beside exit 0. The node reaches `eval_failed`, salvage re-admits a
# number produced by a numerically broken run, and under `metric_salvage="select"` that number is
# selectable. `res.diverged` is not consulted anywhere else in this module, so this set is the
# whole rule.
NEVER_SALVAGED_REASONS = frozenset({"drift", "setup", "timeout", "diverged"})

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

# …and the same veto for a stage the checker looked at and COULD NOT JUDGE.
#
# Added 2026-08-13 with the verdict vocabulary (doc 39 §2.1). That change let an unreadable /
# out-of-enum / explicitly-inconclusive checker answer stop failing the node, because "I cannot tell"
# is not "it failed" and killing a 15-hour training on one is what cost 46.6 GPU-hours. That is a
# decision about what happens NEXT and it is the right one. This is the OTHER half of doc 36's
# corollary 2 — *a wider action space must not widen the trusted set* — and without it the change
# would quietly have done both: a pipeline whose `check`ed stage drew a doubt used to end at that
# stage, so it could never reach salvage at all, and now it can.
#
# So the DOING moves and the RECORDING does not. A stage nobody could vouch for still may not
# contribute a number to the record on a node that failed for some other reason. Note this is a
# scan over EVERY stage row, not `stages[-1]` like the status veto above: an inconclusive stage is
# no longer terminal, so the doubt is generally NOT on the last row — it is upstream of it, which is
# exactly the case the status check cannot see.
VETO_STAGE_KEYS = frozenset({"check_inconclusive"})


def _stage_carries_veto_key(stages) -> bool:
    """Did any stage in this pipeline record a doubt that vetoes salvage? Pure and total."""
    return any(isinstance(s, dict) and any(s.get(k) for k in VETO_STAGE_KEYS) for s in stages)


def settle_mode(mode) -> str:
    """The effective salvage mode for an arbitrary configured value.

    Total over junk on purpose: this is read off an engine attribute an operator, a Strategist or a
    resumed snapshot can set, and an unrecognised value must not silently mean the PERMISSIVE rung.
    Anything not in the vocabulary settles to the conservative default rather than to `select`.
    """
    return mode if mode in METRIC_SALVAGE_MODES else DEFAULT_METRIC_SALVAGE


def stage_producer(operator_stages, stage) -> str:
    """Who produced the output a salvage read is about — an `SALVAGE_PRODUCERS` slug.

    `operator_stages` is `eval_spec["stages"]` (the operator's own declared pipeline, dicts or bare
    names); `stage` is `RunResult.failed_stage`, i.e. the stage whose captured stdout the failing
    `RunResult` carries.

    Total over junk and CONSERVATIVE in the one direction that matters: anything this cannot
    positively attribute to the operator is `agent_stage`, because the consequence of the two
    answers is asymmetric — calling agent output "operator" lets an agent-printed number compete for
    champion, while calling operator output "agent" costs only that a salvaged node stays excluded
    under `select`.

    An EMPTY stage name is the single-command eval (`_run_single`), whose stdout is the operator's
    own `cmd` — the same bytes `run_command_eval`'s primary read uses — and is attributed to the
    operator for exactly that reason.
    """
    name = str(stage or "").strip()
    if not name:
        return OPERATOR_PRODUCED
    if name == OPERATOR_PROTECTED_STAGE:
        return OPERATOR_PRODUCED
    declared = set()
    for row in (operator_stages or ()):
        if isinstance(row, dict):
            declared.add(str(row.get("name") or ""))
        elif isinstance(row, str):
            declared.add(row)
    return OPERATOR_PRODUCED if name in declared else AGENT_PRODUCED


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
    # `res.diverged` as well as the reason, and deliberately not only through it: the divergence
    # verdict is fail-closed and can land beside ANY classification (the kill/exit race records it
    # next to a natural exit 0, where `_failure_reason` sees a clean completion). Reading the flag
    # directly is what makes "a diverged training is never salvaged" a property of the RESULT rather
    # than of whichever label the classifier happened to reach for.
    if getattr(res, "diverged", False):
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
    if _stage_carries_veto_key(stages):
        # An upstream stage the checker could not vouch for — see `VETO_STAGE_KEYS`. Checked before
        # every admitting branch below, and over ALL rows rather than the last one, because an
        # inconclusive stage no longer ends the pipeline and so is never the row `status` describes.
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
    # WHO WROTE THE OUTPUT the reader read (a SALVAGE_PRODUCERS slug). Defaults to the conservative
    # answer so a caller that does not know cannot accidentally buy `select`.
    producer: str = AGENT_PRODUCED

    def __post_init__(self):
        """Bind the three declared vocabularies to the ONE place a salvage record is minted.

        `SALVAGE_SOURCES`/`SALVAGE_CONDITIONS`/`SALVAGE_PRODUCERS` declared themselves closed and
        were enforced by nothing — every slug was a hand-written literal at its construction site, so
        a typo'd `"relocated-file"` shipped silently: the record still wrote, `as_event()` still rode
        on `node_evaluated`, and the operator reading a salvaged node got a source slug no reader
        recognises. That is the failure the registry convention exists to prevent (CLAUDE.md), and a
        registry with no consumer only looks like one.

        Raising is safe here and deliberately loud: both entry points that reach a construction
        (`_salvage_eval_metric`, `_salvage_qualifying_gates`) are already wrapped in
        `except Exception -> None`, so an unregistered slug degrades to "this node was not salvaged"
        — the same outcome as any other salvage read failure — instead of crashing the eval.
        """
        for value, allowed, field in ((self.condition, SALVAGE_CONDITIONS, "condition"),
                                      (self.source, SALVAGE_SOURCES, "source"),
                                      (self.producer, SALVAGE_PRODUCERS, "producer")):
            if value not in allowed:
                raise ValueError(f"SalvagedMetric.{field}={value!r} is not one of {allowed!r}")

    def as_event(self) -> dict:
        """The `metric_provenance` payload for `node_evaluated`.

        `salvaged: True` is spelled out rather than implied by the record's presence: a reader that
        checks one key must not have to know that this dict only ever exists for salvaged metrics,
        because the natural next change is to write provenance for MEASURED metrics too.
        """
        row = {"salvaged": True, "condition": self.condition, "source": self.source,
               "reader": self.reader, "producer": self.producer}
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

        `select` IS NOT UNCONDITIONAL, and this is the one place that fact is enforced. The operator
        setting it accepted "a metric recovered by MY declared reader may compete"; they did not
        accept "a number the agent's own training script printed may compete", which is what the
        setting silently bought whenever the failing stage came out of the Developer's manifest (the
        `expect_failed` early return hands salvage that stage's stdout). So agent-produced output
        keeps the row under every rung except `off` — see `OPERATOR_PRODUCED` and `stage_producer`.
        """
        settled = settle_mode(mode)
        if settled == "off":                 # `salvage` never returns a record under `off` at all
            return []
        if settled == "select" and self.producer == OPERATOR_PRODUCED:
            return []
        return [{"name": SALVAGE_VIOLATION, "value": self.metric, "max": None, "min": None,
                 "salvage": self.as_event()}]


def metric_unmeasured(node) -> bool:
    """Is this node's METRIC one nobody measured — i.e. salvaged AND not admitted to selection?

    THE KNOWLEDGE BOUNDARY, stated once so every writer asks the same question. The
    `metric_salvaged` violation was designed as SELECTION machinery: the fold turns any violation
    row into `feasible = False`, which keeps the node out of `feasible_nodes()` and therefore out of
    champion selection and breeding. But selection is not the only path a number travels. The
    cross-run knowledge writers — the M6 comparative pair lessons, the whole-run reflection — read
    `state.nodes` directly, so a value that could not become champion could still become a LESSON
    asserting something about the metric, stamped with that node as its evidence, appended to the
    shared `lessons.jsonl` and retrieved by a later run as if it were measurement. The violation
    gated breeding and not knowledge; this predicate is what closes that half.

    It reads the VIOLATION ROW, not `metric_provenance`, because the row is the enforcement and the
    provenance is only the account — under `select` with operator-produced bytes the operator has
    explicitly accepted the value as comparable, no row is minted, and such a node must stay in
    every population it was in before (annotated, never hidden — see
    `engine/lessons_distill.py::_SALVAGED_ROW_NOTE`). Keying on `metric_provenance.salvaged` alone
    would silently overrule that setting; keying on `feasible` alone would also catch a
    CONSTRAINT-violating node, whose number is real and whose exclusion is a fact about the bound
    rather than about the measurement.

    WHAT THIS DOES NOT SAY. It is not "ignore this node". A salvaged node ran, and what it OBSERVED
    — which concepts it touched, that its declaration failed and how — is real knowledge that is
    independent of the unmeasured number, and both halves are still recorded: `store_concept_capsule`
    keeps the concept TAG while withholding the numeric outcome, and `reflect_lessons` shows the node
    as an observation with its salvage condition and no metric. Only claims ABOUT THE METRIC are
    refused, which is the same distinction the violation already makes for selection.

    Total over junk (a hand-edited log, a `violations` list of strings): anything that is not a dict
    row naming this violation is not one.
    """
    for row in (getattr(node, "violations", None) or ()):
        if isinstance(row, dict) and row.get("name") == SALVAGE_VIOLATION:
            return True
    return False


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
    the operator's basename, and `_artifacts_written_elsewhere` searches only INSIDE the workdir, only
    files this attempt wrote, and only up to a bounded number of directories.

    Deliberately NOT extended to `stdout_*` readers: there is no path to relocate, and a stdout
    reader that found nothing found nothing.

    AMBIGUITY IS A REFUSAL, not a coin toss. `_artifacts_written_elsewhere` returns every fresh
    same-basename file it found, sorted; two of them means the workdir holds two candidate answers
    and nothing deterministic says which one the declaration meant. The diagnostic half of this scan
    (`verify_stage_artifacts`' near-miss line) can name the first and be useful, because a human
    reads it; a salvage cannot, because it would promote a number chosen by directory-walk order —
    the same value could be recovered differently on a re-run of the identical workdir.
    """
    from looplab.runtime import command_eval
    if command_eval.spec_kind(metric_spec) not in command_eval.READERS_REQUIRING_PATH:
        return None
    rel = metric_spec.get("path")
    if not isinstance(rel, str) or not rel.strip():
        return None
    found = command_eval.artifacts_written_elsewhere(workdir, rel, since)
    if len(found) != 1:
        return None
    return dict(metric_spec, path=found[0]), found[0]


def read_salvageable_metric(metric_spec, stdout: str, workdir: str,
                            since: Optional[float]) -> Optional[tuple]:
    """Ask the OPERATOR'S declared reader for a metric this failed eval already produced.

    Returns `(value, source, reader_kind, detail)` or None. Pure with respect to the run: it reads
    the node's own workdir and the captured stdout and nothing else, and it appends nothing.

    THE SPEC IS THE OPERATOR'S; `stdout` IS WHATEVER THE FAILING STAGE PRINTED. Those are two
    different claims and this function makes only the first. Who wrote the bytes is decided by
    `stage_producer` and enforced by `SalvagedMetric.violation_rows` — read the module docstring's
    second condition before treating a value that comes back from here as measured.

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
            mode: str = DEFAULT_METRIC_SALVAGE, operator_stages=()) -> Optional[SalvagedMetric]:
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

    `operator_stages` is the operator's own `eval_spec["stages"]`, and it is what lets the record say
    whether the salvaged bytes came from a stage the operator declared or from the Developer's
    manifest. An omitted argument therefore settles to the CONSERVATIVE answer (`agent_stage`), not
    to a permissive one.
    """
    if settle_mode(mode) == "off":
        return None
    condition = salvage_condition(res, reason)
    if condition is None:
        return None
    stage = str(getattr(res, "failed_stage", "") or "")
    producer = stage_producer(operator_stages, stage)
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
                              reader=_spec_kind(metric_spec), stage=stage, producer=producer)
    found = read_salvageable_metric(metric_spec, getattr(res, "stdout", "") or "", workdir, since)
    if found is None:
        return None
    value, source, reader, detail = found
    return SalvagedMetric(metric=value, condition=condition, source=source, reader=reader,
                          stage=stage, detail=detail, producer=producer)


def _spec_kind(metric_spec) -> str:
    from looplab.runtime import command_eval
    return command_eval.spec_kind(metric_spec)


# The reason an operator-owned reader that would EXEC agent code cannot simply be skipped here.
# `run_command_eval` RAISES on an `adapter` in `metrics`/`constraints`, but it raises in its TAIL —
# the code path a salvaged eval never reached — so a spec salvage sees may still contain one. A
# skipped constraint is a silent pass on an operator's hard bound; an EXECd one is the trust
# boundary. Neither is acceptable, so it counts as unverifiable, which `_violations` already treats
# as a violation ("an unverifiable constraint is a violation, never a silent pass").
_ADAPTER_CONSTRAINT = "constraint reader is `adapter` (agent-authored code is never EXECd on a salvage)"


def salvage_gates(eval_spec, metric: float, stdout: str, workdir: str,
                  since: Optional[float], *, enforce_drift: bool = False) -> dict:
    """The QUALIFYING reads `run_command_eval`'s tail would have made about this metric, re-asked.

    Returns `{"violations": [...], "extra_metrics": {...}, "drift": {...}|None}`.

    A salvaged metric is still a metric, and the operator's constraints, extra readers and drift
    cross-check are the things that decide whether a metric qualifies. They are computed in
    `run_command_eval`'s TAIL (`viol = _violations(...) if constraints and m is not None`), which the
    `expect_failed` / `check_failed` / crashed-stage early returns skip entirely — so every salvaged
    node reached its terminal with `violations == []`, and under `select` that read as
    "no constraint was breached" when the truth was "no constraint was ever evaluated". A node like
    that could become champion with the operator's hard bounds never applied.

    THREE DELIBERATE DIFFERENCES from the tail, each because the situation is not the same:
      * FAIL CLOSED ON FRESHNESS. `since` is this attempt's start for every reader, where the tail
        relaxes secondary readers under a stage-scoped re-run
        (`command_eval.attempt_freshness_floor`, which since 2026-08-14 is also what the metric
        SUBJECT binds with). A stale constraint file therefore reads as unverifiable -> violation.
        That is the safe direction: the node is already excluded under the default rung, so the cost
        is confined to `select`, where the alternative is admitting an unmeasured metric under an
        unchecked bound. KNOW WHAT IT COSTS THERE, because it is the same off-by-one-attempt shape
        the subject binding had: on a stage-scoped re-run the reused stages' artifacts CANNOT be
        newer than this attempt, so under `select` + an operator-produced stage a readable constraint
        reads unverifiable and re-excludes a node the operator admitted. Deliberate and unchanged —
        this is the FAILED-eval path, where exclusion is the conservative direction and admitting a
        number under an unchecked bound is not — but it is a policy choice, not an oversight, and
        flipping it needs its own evidence rather than a symmetry argument with the tail.
      * NO `wrap`. Same argument as `read_salvageable_metric`: `adapter` is refused, and every other
        reader parses host files or the captured stdout in-process.
      * DRIFT IS A REFUSAL, NOT A RECORD. The caller must abandon the salvage when this returns a
        `drift` — `NEVER_SALVAGED_REASONS` already refuses a metric the drift gate discarded, and a
        salvaged value that its cross-reader cannot corroborate is the same fact discovered one step
        later.

    Best-effort in the same strict sense as the rest of this module: it never raises. A reader that
    blows up contributes an unverifiable constraint (fail closed), never a dead eval worker.
    """
    from looplab.runtime import command_eval
    spec = eval_spec if isinstance(eval_spec, dict) else {}
    wd = str(workdir)
    out: dict = {"violations": [], "extra_metrics": {}, "drift": None}

    constraints = [c for c in (spec.get("constraints") or []) if isinstance(c, dict)]
    safe: list = []
    for c in constraints:
        if command_eval.spec_kind(c) == "adapter":
            out["violations"].append({"name": c.get("name", "constraint"), "value": None,
                                      "max": c.get("max"), "min": c.get("min"),
                                      "unverifiable": _ADAPTER_CONSTRAINT})
        else:
            safe.append(c)
    if safe:
        try:
            out["violations"].extend(
                command_eval.constraint_violations(stdout, wd, safe, None, since=since))
        except Exception:  # noqa: BLE001 — an unreadable constraint is a violation, not a dead run
            out["violations"].extend([{"name": c.get("name", "constraint"), "value": None,
                                       "max": c.get("max"), "min": c.get("min"),
                                       "unverifiable": "constraint reader raised"} for c in safe])

    for name, reader in (spec.get("metrics") or {}).items():
        if not isinstance(reader, dict) or command_eval.spec_kind(reader) == "adapter":
            continue                      # audit-only values: a missing one is absent, not a gate
        try:
            value = command_eval.read_metric(stdout, wd, reader, wrap=None, since=since)
        except Exception:  # noqa: BLE001
            value = None
        if value is not None:
            out["extra_metrics"][str(name)] = value

    cross = spec.get("cross_check")
    if enforce_drift and isinstance(cross, dict):
        tolerance = spec.get("drift_tolerance", 1e-6)
        try:
            tolerance = float(tolerance)
        except (TypeError, ValueError):
            tolerance = 1e-6
        try:
            command_eval.validate_cross_check(cross)
            value = command_eval.read_metric(stdout, wd, cross, wrap=None, since=since)
        except Exception:  # noqa: BLE001 — an unusable cross reader corroborates nothing
            value = None
        if command_eval.metric_drift_exceeds(metric, value, tolerance):
            out["drift"] = {"primary": metric, "cross": value, "tolerance": tolerance}
    return out


# --- THE CAUSE, WHICH IS THE OTHER HALF OF THE ASK -----------------------------------------------
# The `triage_action` a cause fix writes on its `node_repaired` row. A CONSTANT because two sites key
# on it and they must agree: `engine/evaluate.py::_repair_salvaged_cause` writes it (and reads it
# back to make the fix idempotent across a resume — invariant #3), and
# `engine/evaluate.py::_durable_repair_ledger` reads it to keep a fix that bought no re-evaluation
# out of the inline-repair ATTEMPT budget. A typo'd literal in either place is silent: the resume
# pays for a second Developer call, or the node loses a repair attempt it never spent.
SALVAGE_CAUSE_TRIAGE_ACTION = "salvage_cause_fix"
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


# --- THE RE-CHECK: A CORRECTED DECLARATION IS RE-ASKED, NOT RE-RUN (backlog F1e) -----------------
# THE CASE THIS EXISTS FOR, measured on `runs/rubertlite-dr-unified-v6` node 3 on 2026-08-13. Its
# Developer authored a fresh config with a new `run_name` and declared the checkpoint at a path
# missing the testbed's composed `<run_name>_<model>` suffix. The stage exited 0, WROTE the
# checkpoint, printed the number, and failed its declared artifact contract on the path alone — the
# near-miss diagnostic named the exact file it had written. The metric (0.728113) was recovered from
# its stdout, and under the default `audit` rung a salvaged metric carries a `metric_salvaged`
# violation and is excluded from `feasible_nodes()`, so the best number the run had at that point
# could neither become champion nor be bred from.
#
# ONE NODE, NOT A PATTERN, and the distinction is load-bearing because the first write-up of this
# claimed otherwise. "Every DRAFT is measured, every MERGE is salvaged" was n=1 plus a prediction
# that turned out wrong: node 4 was another merge, declared `unified-baseline_rubert-tiny-lite/
# final/...`, the repo composed exactly that, and it measured normally. So nothing here — and
# nothing anywhere else — may special-case an operator or assume merges get paths wrong more often.
# The change stands on the single honest case: a manifest-only repair that makes an artifact the
# pipeline ALREADY produced satisfy its contract. Let the frequency be whatever it turns out to be.
#
# WHY THIS IS NOT ANSWERED BY `select`. That rung admits agent-produced bytes wholesale, which is
# the boundary salvage exists to keep (see the module docstring's second condition). The answer here
# is narrower and is a fact about THIS eval rather than a policy: the metric was never actually
# unmeasured. The pipeline DID produce the artifact — `command_eval._artifact_written_elsewhere`
# proves it and names the exact file in the failure `concern` — and `metric_salvage_repair` already
# corrects the manifest in the SAME attempt. So once the declaration is corrected, the artifact
# CHECK is re-asked against it (`verify_stage_artifacts`, a handful of stats, no stage re-run); if it
# now passes, the contract is satisfied by the artifact the pipeline really produced and the node is
# recorded as MEASURED — no violation, no salvage provenance — because nothing about the number was
# ever in doubt, only the sentence describing where it lived.
#
# WHAT THE RE-CHECK STILL DOES NOT PROVE, stated because promoting a number is exactly the place to
# say what the promotion is not. The failing stage is usually the Developer's `train`, so the
# operator's protected `score` stage never ran: the value comes out of the operator's DECLARED
# reader over an AGENT-authored stage's output, which a fully successful staged eval would have read
# from the score stage instead. The re-check answers "did this stage produce what it declared", not
# "did the operator's scorer run". That is why the record this returns keeps `producer` and says the
# contract was RE-checked rather than satisfied first time — a reader who weighs those differently
# still has both facts.
#
# FOUR ADMISSION RULES, each with its own argument and each fail-closed:
#   * THE DECLARED PIPELINE MUST HAVE RUN (`declared_pipeline_completed`) — first, because it is the
#     cheapest and the most categorical. A contract failure ABORTS the pipeline, so on v6 node 3 the
#     manifest declared `train` → `merge`, `train` failed its contract, and `merge` and the
#     operator's appended `score` never executed at all (`nodes/node_3/` has `train.log` and no
#     `score.log`). The recovered 0.728113 is therefore a plain training run's number filed under a
#     node whose declared pipeline was train-then-merge-then-score. Promoting it on the strength of a
#     corrected path would put a train-only result into champion selection wearing that node's label,
#     which is strictly worse than leaving it salvaged — salvage at least MARKS it. A corrected
#     declaration says one stage's output was where it said; it says nothing about the stages that
#     never ran. So: every stage of the resolved chain must have run, and the contract failure must be
#     on the LAST of them.
#   * FRESHNESS — the re-check keeps `since` at the STAGE's own start (`recheck_floor`), the same
#     floor the original check used. An earlier attempt's leftover in the deliberately-reused workdir
#     must not be able to satisfy the corrected path.
#   * WHICH REPAIRS QUALIFY — only a repair that touched EXACTLY the stage manifest
#     (`declaration_only_repair`). A repair that touched CODE changed what the stage would produce,
#     so its artifact must be re-RUN, not re-checked.
#   * WHICH SALVAGES QUALIFY — only an `artifact_contract` failure whose value came out of the
#     operator's spec UNMODIFIED (`recheckable_salvage`). A `relocated_file` rung already tells us a
#     second declaration (the metric reader's own path) is still wrong, which is not a node whose
#     only defect has been corrected.
# The re-check is asked BEFORE the salvage is constituted on the terminal, so a node that passes
# carries no provenance of salvage, no `metric_salvaged` violation and no `salvaged_error` at all.
#
# WHAT THE FOUR RULES TOGETHER ADMIT, AND WHY NOTHING SHIPPED REACHES IT YET — measured against
# `engine/eval_stages.py::_resolve_stages`, and stated here because a rule set nobody can reach is a
# thing a reader must be TOLD, not left to discover:
#   * completion + "the failure is on the last stage" leaves exactly one admissible shape — a
#     pipeline that ran end to end whose FINAL stage exited 0, printed the number, and misnamed its
#     own artifact;
#   * in Developer-manifest mode the final stage is the operator's APPENDED `score`, which
#     `_resolve_stages` builds with `name`/`command`/`timeout` and no `expect` at all, so it cannot
#     fail an artifact contract;
#   * in operator `cmd.stages` mode the final stage CAN declare `expect.files`, but that declaration
#     lives in the task spec and `looplab_stages.json` is ignored entirely — so the manifest-only
#     repair the second rule requires cannot be what corrected it.
# So today the two halves are mutually exclusive and no live node is promoted; v6 node 3 stays
# salvaged, which is the intended answer for it. Making the promotion reachable is a DESIGN decision
# with an owner — letting the appended `score` stage carry an operator-declared `expect` and deciding
# who may repair it — not something this module should route around.

# The Developer's stage manifest — the ONE file a re-checkable repair may touch. `engine/eval_stages
# .py::_resolve_stages` reads it at exactly this name in the workdir ROOT, which is why this is an
# exact match and not the BASENAME rule `_safe_reuse_start` uses one file over: there, matching more
# paths refuses more reuse (the conservative direction); here, matching more paths would admit more
# re-checks, so the same looseness would point the wrong way.
DECLARATION_MANIFEST = "looplab_stages.json"

# The salvage rungs whose value may be promoted by a re-check. `declared_reader` only: the operator's
# spec read exactly what it declared, so the corrected manifest is the last broken sentence about
# this node. `relocated_file` found the number somewhere other than where the operator's own metric
# path says it lives — a second wrong declaration the manifest fix did not touch — and `eval_result`
# is a relabelling of a value the eval carried, not a contract this check can speak for.
RECHECKABLE_SALVAGE_SOURCES = ("declared_reader",)


def _decl_key(path) -> str:
    """A repair's changed-file key as the manifest name would be spelled.

    `command_eval.normalize_declared_path` and nothing else: this used to strip a `./` prefix ONCE
    while the manifest validator stripped it repeatedly, so `././looplab_stages.json` canonicalized
    on the validated side and did not here — and the F1e re-check then compared two spellings of one
    path forever, refusing a promotion the validator had accepted. Fail-closed, but permanently
    unreachable and indistinguishable from "the repair didn't correct anything"."""
    from looplab.runtime import command_eval
    return command_eval.normalize_declared_path(path)


def declaration_only_repair(changed, deleted=(), code: str = "") -> bool:
    """Did this repair touch ONLY the stage manifest — i.e. is it a repair to the DECLARATION?

    THE RULE DECISION F1e NAMES, as a rule with a truth table rather than a condition buried in the
    attempt loop. A repair that touched CODE has changed what the stage would produce, so what it
    declares must be re-RUN and never re-checked against an artifact the OLD code wrote: re-checking
    it would attach the previous code's number to a node whose recorded code is different, which is
    the one thing the whole salvage-cause design refuses (`SALVAGE_CAUSE_DIRECTIVE`).

    FAIL CLOSED on everything ambiguous, and each clause is a real shape rather than a defensive
    reflex:
      * a DELETION is invisible in `changed` and can remove a module an earlier stage trained
        through — the same blind spot `_safe_reuse_start` refuses reuse over;
      * a whole-file `code` artifact IS the experiment's code;
      * an EMPTY change set means nothing was corrected, so there is no corrected declaration to
        re-check and a pass would be the ORIGINAL contract passing on a second look;
      * anything beside the manifest — a config, a data file, a second manifest under a subdirectory
        the eval does not read — is not a declaration fix this can speak for.
    """
    if deleted:
        return False
    if (code or "").strip():
        return False
    return {_decl_key(c) for c in (changed or ())} == {DECLARATION_MANIFEST}


def declared_pipeline_completed(stages, rows, stage) -> bool:
    """Did EVERY stage the resolved pipeline declares actually RUN, with the contract failure on the
    LAST of them? The first and most categorical of the four admission rules.

    A NODE IS ITS WHOLE PIPELINE, not its first stage. `runs/rubertlite-dr-unified-v6` node 3
    declared `train` → `merge` (plus the operator's appended `score`); `train` failed its artifact
    contract, `_run_stages` returned early, and `merge` and `score` never executed — the workdir has
    `train.log` and no `score.log`. Its recovered metric is a plain training run's number. Correcting
    the path proves the file was where the fixed declaration says; it proves nothing about two stages
    that never ran, and promoting on it would file a train-only result under a node claiming to be
    something else. A pipeline that aborted early has not produced the thing the node claims to be,
    whatever the contract now says about one stage's output.

    `stages` is the resolved chain (`Engine._resolved_stages`) and `rows` is `RunResult.stages` — the
    per-stage records the run really produced, where `reused` is a genuine "ran on an earlier attempt
    and its artifacts are on disk" (the stage-scoped re-run) and therefore counts.

    FAIL CLOSED on every shape it cannot establish: an empty or unresolvable chain, a stage with no
    name, a duplicate name (two rows could not be told apart), a declared stage with no record at
    all, and any status that is not `ok`/`reused` on a stage before the last.
    """
    names = [str(s.get("name") or "") for s in (stages or ()) if isinstance(s, dict)]
    if not names or "" in names or len(names) != len(set(names)):
        return False
    if str(stage or "") != names[-1]:
        return False                     # the failure aborted the pipeline somewhere before the end
    status = {str(r.get("name") or ""): str(r.get("status") or "")
              for r in (rows or ()) if isinstance(r, dict)}
    if any(status.get(n) not in ("ok", "reused") for n in names[:-1]):
        return False
    return status.get(names[-1]) == "expect_failed"


def recheckable_salvage(salvaged) -> bool:
    """May this recovered value be PROMOTED by a re-check, if the contract now passes?

    The condition must be `artifact_contract` — the only failure class in which a declared contract
    is what failed, and therefore the only one a re-check of that contract can answer. See
    `RECHECKABLE_SALVAGE_SOURCES` for the rung rule.

    The PRODUCER gate is the third rule, and without it this promotion was strictly more permissive
    than `metric_salvage="select"` — the most permissive rung an operator can choose. A promotion
    drops the `metric_salvaged` violation, its provenance and the `feasible_nodes()` exclusion
    outright, whereas `violation_rows` keeps all three for AGENT_PRODUCED output under every rung
    except `off` (see its own paragraph: the operator who set `select` accepted "a metric recovered
    by MY declared reader may compete", not "a number the agent's own training script printed may
    compete"). A fully Developer-declared pipeline is exactly the shape that reaches here, so
    without this line a node could be recorded as MEASURED, with no violation, under the DEFAULT
    `audit` rung — while the same node under an explicit `select` still carried the violation."""
    return (salvaged is not None
            and getattr(salvaged, "condition", "") == "artifact_contract"
            and getattr(salvaged, "source", "") in RECHECKABLE_SALVAGE_SOURCES
            and getattr(salvaged, "producer", AGENT_PRODUCED) == OPERATOR_PRODUCED)


def recheck_floor(res, stage: str) -> Optional[float]:
    """The freshness floor a re-check of `stage`'s corrected contract must use, or None to REFUSE.

    DECISION (a) OF F1e. It is the stage's OWN wall-clock start, recorded by the writer that ran the
    original check (`command_eval.EXPECT_SINCE_KEY`) rather than re-derived here — the two must be
    the same number or the re-check is answering a different question from the check it replaces.
    The looser floors in reach are both wrong in the direction that matters: the ATTEMPT's start
    (what `salvage` itself uses) precedes every stage in the attempt, so an artifact an EARLIER stage
    of this same attempt wrote could satisfy a corrected path that names a LATER stage's output.

    None when the row is missing, is not this stage's, did not fail its artifact contract, or carries
    an unusable floor — a re-check with no floor would degrade to existence-only, which is precisely
    how a leftover from a previous attempt gets promoted.
    """
    from looplab.runtime import command_eval
    name = str(stage or "")
    if not name:
        return None
    for row in (getattr(res, "stages", None) or []):
        if not isinstance(row, dict) or str(row.get("name") or "") != name:
            continue
        if str(row.get("status") or "") != "expect_failed":
            return None                  # some other stage outcome; there is no contract to re-ask
        floor = row.get(command_eval.EXPECT_SINCE_KEY)
        if isinstance(floor, bool) or not isinstance(floor, (int, float)):
            return None
        return float(floor) if math.isfinite(floor) and floor > 0 else None
    return None


def recheckable_expect(stages, stage: str, changed) -> dict:
    """The CORRECTED `expect` for `stage` out of the re-resolved chain, or `{}` to refuse.

    `stages` is `Engine._resolved_stages(node, workdir)` re-run AFTER the repair landed in the
    workdir, so its `expect` is the corrected declaration — the whole point of the exercise is that
    the check is re-asked against a different sentence, not against the one that already failed.

    THE ONE ADVERSARIAL CASE, refused here rather than left to trust: a declared artifact that is
    itself one of the files the REPAIR wrote. `_write_node_files` materializes the corrected manifest
    AFTER the stage start, so a "fix" that declared `looplab_stages.json` as the stage's own output
    would pass the freshness gate on a file the stage never produced. An artifact the repair wrote is
    not an artifact the stage produced, and the repair is authored by the agent whose declaration was
    wrong in the first place.
    """
    name = str(stage or "")
    if not name:
        return {}
    # LATENT GAP in the guard below, unreachable today: `written` is only the CHANGED
    # set, but the repair's commit path (`_repair_salvaged_cause` -> `_write_node_files`)
    # re-materializes EVERY `node.files` entry with a fresh mtime after the stage start — so a
    # corrected `expect.files` naming an UNCHANGED node source file (e.g. `train.py`) would pass
    # the freshness floor on a file the stage never produced. Unreachable today only because the
    # promotion path never sees an operator-declared expect on the appended score stage; if that
    # documented design option ever lands, `written` must widen to everything the commit rewrote,
    # not just the diff.
    written = {_decl_key(c) for c in (changed or ())}
    for row in (stages or ()):
        if not isinstance(row, dict) or str(row.get("name") or "") != name:
            continue
        expect = row.get("expect") if isinstance(row.get("expect"), dict) else {}
        files = [f for f in (expect.get("files") or []) if isinstance(f, str) and f.strip()]
        if not files:
            return {}                    # nothing declared any more: a dropped contract is not a fix
        if any(_decl_key(f) in written for f in files):
            return {}
        return {"files": files}
    return {}


def declaration_actually_corrected(rows, stage: str, expect) -> bool:
    """Did the repair reach the DECLARATION THAT FAILED — not merely the file it usually lives in?

    `declaration_only_repair` answers a question about a FILENAME: did this change set touch only
    `looplab_stages.json`. That is necessary and it is not sufficient, because a repair can write
    that file without altering the sentence the contract failed on — and in operator `cmd.stages`
    mode it cannot alter it at all, since the failing declaration lives in the task snapshot and the
    manifest is ignored, while `cause_repair_context` still directs the Developer at the manifest.
    The file-set gate then passes on a repair that corrected nothing, and everything downstream
    reduces to whether `verify_stage_artifacts` answers differently on a second look at an UNCHANGED
    declaration — across a window that contains a full Developer LLM round trip, on a network
    filesystem. That is a race deciding whether a node is champion-eligible, and it was reachable:
    driven end to end on a real `Engine`, the node was promoted with the still-wrong path recorded as
    its corrected `expect_files`.

    So the rule is stated as what it actually needs to be: the post-repair declaration must DIFFER
    from the one the contract was held to. `rows` is `RunResult.stages`, where the failing row
    carries `command_eval.EXPECT_DECLARED_KEY`; `expect` is the corrected block from
    `recheckable_expect`.

    FAIL CLOSED, in the two ways that matter:
      * NO recorded pre-repair declaration — an older log, a row the writer did not stamp, a caller
        that hand-built rows — means the question cannot be answered, and an unanswerable question is
        not a pass. This is the branch that keeps every pre-2026-08-13 run out of the promotion path
        entirely, which is correct: nothing about those logs establishes what changed.
      * An UNCHANGED declaration is the "original contract passing on a second look" that
        `declaration_only_repair`'s empty-change-set clause already refuses in the other direction.
        Order-insensitive and path-normalized, so a reordered or `./`-prefixed rewrite of the same
        set is correctly read as no correction at all.
    """
    from looplab.runtime import command_eval        # deferred, like every command_eval read here

    name = str(stage or "")
    files = [f for f in ((expect or {}).get("files") or []) if isinstance(f, str)]
    if not name or not files:
        return False
    for row in (rows or ()):
        if not isinstance(row, dict) or str(row.get("name") or "") != name:
            continue
        before = row.get(command_eval.EXPECT_DECLARED_KEY)
        if not isinstance(before, list) or not before:
            return False
        was = {_decl_key(f) for f in before if isinstance(f, str)}
        now = {_decl_key(f) for f in files}
        return bool(was) and was != now
    return False


def declaration_repair_provenance(salvaged, expect_files, error: str) -> dict:
    """The `metric_provenance` for a node whose repaired contract PASSED the re-check.

    DECISION (d), taken deliberately: the node is measured — no violation row, nothing on the
    selection path — and it STILL records that its declaration was repaired. "The manifest was wrong
    and we fixed it" is worth knowing even when the number is sound: it is the difference between a
    node that ran cleanly and one that needed a correction after the fact, it is the only durable
    trace that the node's recorded code is not byte-for-byte what produced its recorded metric (the
    cost `_repair_salvaged_cause` documents), and it is the evidence an operator would need to notice
    that every MERGE node in a run has been through this. A provenance field with no violation says
    exactly that and nothing more.

    `salvaged: False` is spelled out rather than omitted for the reason `SalvagedMetric.as_event`
    already gives — a reader that checks one key must not have to know the dict only ever exists for
    salvaged metrics, and this IS the "provenance for a MEASURED metric" that docstring anticipated.
    Every reader in the tree keys on its truthiness (`serve/report.py`, `engine/lessons_distill.py`,
    `search/speculation_quality.py`), so a False here reads as measured everywhere.
    """
    return {"salvaged": False,
            "declaration_repaired": True,
            "stage": getattr(salvaged, "stage", "") or "",
            "reader": getattr(salvaged, "reader", "") or "",
            # WHO WROTE THE BYTES the declared reader read, kept verbatim from the recovery record.
            # The re-check says the stage produced what it declared; it does not say the operator's
            # protected scorer ran, and this is the field that keeps those two apart.
            "producer": getattr(salvaged, "producer", AGENT_PRODUCED),
            "expect_files": [str(f) for f in (expect_files or [])][:12],
            # The contract failure that was corrected, kept on the SUCCESS terminal for the same
            # reason `salvaged_error` is kept on a salvaged one: a node that reads as measured must
            # still be able to tell whoever looks what had been wrong.
            "declaration_error": str(error or "")[:600]}
