"""WHO MAY SAY WHAT A FAILED EVAL FAILED OF — the ownership split, the diagnostician's contract,
and the one value that means "nobody could say".

THE DEFECT THIS MODULE EXISTS FOR IS NOT THAT `_failure_reason` USED REGEXES. It is that in that
one function **text got the LAST WORD**. Nothing downstream ever re-checks a `reason`, yet the
reason chooses the repair directive (`crash_repair._repair_error_context`), gates the
triage-driven install, gates metric salvage (`metric_salvage.NEVER_SALVAGED_REASONS`) and lands on
the durable terminal a whole run is later audited from. Everywhere ELSE in this tree where text
touches a decision, this codebase had already converged on the opposite discipline and written it
down: `runtime/deps.py::triage_install_candidates` lets a traceback and a model's prose NOMINATE a
distribution — "Free rationale text alone can NEVER mint a candidate" — and then
`runtime/deps.py::is_present` DECIDES, by spawning the eval interpreter and asking `find_spec`.
The measured cost of the version without that probe is in `engine/crash_repair.py::_prepare_env`:
a regex reduced `pytorch_lightning.utilities.cloud_io` to `pytorch_lightning`, pip answered
"Requirement already satisfied" rc=0 in 2.19 s, and the engine wrote a `deps_installed` receipt
saying the environment had just been repaired — a FALSE RECEIPT, after which the identical failure
that followed read as the agent's code being wrong.

So the rule this module applies, and the one to apply to any new reason before routing it:

    **Text may NOMINATE. It may never DECIDE. Ask first whether an OUT-OF-BAND CHANNEL EXISTS.**

    * If one exists, USE IT. Inventing a text sentinel for a fact you are holding is strictly
      worse than reading the fact — that is what `setup` was doing (see below).
    * If one CANNOT exist — the engine did not cause the exit, nothing observed it, and the dead
      process's own bytes are the only witness — that is precisely where a diagnostician earns its
      cost, and its verdict has to be made checkable some other way (see EVIDENCE, below).

Note the scope this rule does NOT cover, said out loud so the boundary is deliberate rather than
accidental: `runtime/deps.py`'s regexes STAY. They nominate; `is_present` decides. Nothing in this
module touches them.

-----------------------------------------------------------------------------------------------
THE SPLIT, applied reason by reason. The question asked of each is "does an out-of-band channel
exist?", NOT "is this text trustworthy" and NOT "how confident is the classifier".

ENGINE-FINAL — the engine caused it, ran it, or measured it, and REMEMBERS DOING SO. The candidate
cannot produce these bytes, a model is never asked about one, and `diagnosed_failure_reason` will
not read an answer about one if it arrives:

    timeout        `res.timed_out` — the engine's own clock.
    diverged  )    `res.diverged` / `res.stalled`, filled by `runtime/command_eval.py` from
    stalled   )    `run_argv`'s out-of-band `signals` dict. NEVER the stderr sentinel beside them,
                   which is mixed into the candidate's own output and is forgeable.
    not_learning   the training watchdog's own kill (`train_monitor.MONITOR_REPAIR_REASON`),
                   reaching `_evaluate` through `kill_signal["terminal_reason"]`. A kill the
                   ENGINE issued.
    drift          `res.drift` — the independent cross-reader's refusal.
    setup          `res.setup_failed`, a flag on the branch where `run_command_eval` has just
                   observed `rc != 0 or timed_out` from the setup command IT ran.
    needs_failed )  `res.stages[-1]["status"]`, written by `_run_stages` after IT stat'ed the
    expect_failed)  filesystem: `verify_stage_inputs` for a declared input that is not there,
                    `verify_stage_artifacts` for a declared output that was not (re)written. Both
                    exit 0, so there is no text involved in either direction.

  `setup` IS THE CASE WORTH SPELLING OUT, because it was on the wrong side of this line until
  2026-08-20 and it shows the anti-pattern in its purest form. `run_command_eval` KNEW: it had just
  read `rc` from the setup step. It then threw that knowledge into `stderr` as the literal
  `"setup failed:\\n"` and `_failure_reason` read it back with `.startswith()`. The knowledge made a
  round trip through a channel the CANDIDATE also writes — and `setup` is in
  `metric_salvage.NEVER_SALVAGED_REASONS`, so a candidate whose training script happened to begin
  its stderr with those twelve characters had a metric it really produced SUPPRESSED. Low
  probability; exactly the forgeability the watchdog branch one line above it warns about in
  writing. The fix is not to move `setup` to a model — it is to stop discarding the fact:
  `RunResult.setup_failed`, the same shape `timed_out`/`diverged`/`stalled` already have. `setup`
  then stays engine-final for a STRONGER reason than before. What a diagnostician may add here is
  WHY setup failed, as repair direction — never WHETHER it failed.

DIAGNOSABLE — no out-of-band channel exists, or the "channel" is itself a model:

    crash      exit_code != 0. STRUCTURAL as far as it goes ("the process died non-zero") and it
               stays the engine's residual — but it says nothing about the CAUSE, and nothing
               observed the cause. The two text rules that used to refine it are deleted: the
               `-9/137 + "Traceback" absent` kernel-OOM signature and `_is_torch_oom`'s marker scan.
    oom        the allocator ran out. The engine did NOT cause this exit, the candidate's own
               process raised and died, and nothing out of band saw it. Device-level free memory is
               not a substitute: it is sampled after the process is gone and the allocation is
               already released. This is the textbook case for a diagnostician.
    no_metric  exit 0, no contract failure, no reader could parse a number. Structural, and equally
               silent about the cause.
    check_failed  THE ONE CONTRACT STATUS THAT MOVES, and it moves because it is not an engine
               computation at all: `_run_stages`' `check_failed` row is written from
               `_call_stage_check`, i.e. from ANOTHER MODEL's reading of the candidate's own
               stdout. Treating one model's prose as an authenticated fact is the same error as
               treating the candidate's stderr as one. The engine's own deterministic reading in
               that neighbourhood (`epoch_floor_acquits`) already respects this: it may only
               ACQUIT, never fail a stage.

  MEASURED, and this is the hole the split exists to close. `check_failed` names the stage that
  refused, never the failure behind it, and the corpus shows it hiding at least three different
  ones. `runs/rubertlite-dense-retrieval` has SIXTEEN terminals whose recorded error is
  "stage 'train' failed verification: <the checker saying the loss never moved>" — "Loss stagnant
  at 13.3 throughout epoch 19", "Loss constant at 14 throughout training", "Loss is constant at
  14.8 … suggesting training may not be learning". Every one of them is `not_learning`. (Those
  sixteen rows are RECORDED `no_metric`; that run predates the `check_failed` reason. Under today's
  classifier they are `check_failed`, which is what makes them the right measurement for this
  change and is a correction to the brief that commissioned it.) Two more, in
  `rubertlite-dr-unified-v8/v9`, are a different failure each: node 9's own repair rationale reads
  "the train stage simply times out because 10 epochs at bs 8192/acc 2 needs ~5.4h vs the ~4h
  budget", and v9 node 0's reads "The run completed training and evaluation (RECALL@100: 0.697972)
  but failed verification because training stopped at epoch 14.87" — a node failed on a checker's
  reading while holding a number.

  THE CONTRAST IS THE ARGUMENT, and it is why `needs_failed`/`expect_failed` did NOT move with it.
  All 8 `expect_failed` rows in the corpus are the same real cause — the stage wrote its artifact
  to a path the manifest does not declare — and the repair rationale AGREES with the label in 8 of
  8 ("wrote negatives.parquet to a path with a doubled `_e5-small-en-ru` suffix", "trained fully
  and saved a real model, but at …/checkpoint-3177/…"). Zero mislabels, because a stat of the
  filesystem is not a reading of anything. `needs_failed` occurs 0 times and has the same shape.
  Moving them would widen the trusted set for no measured gain, which is the trade `docs/36`
  refuses.

-----------------------------------------------------------------------------------------------
EVIDENCE — the diagnostician's `is_present`, and the honest limit of the analogy.

`is_present` works because a checkable out-of-band answer EXISTS for its question: spawn the
interpreter, ask `find_spec`. **For a failure KIND no such probe exists, and pretending otherwise
would be the whole defect again.** Every candidate check is either the text rule being deleted
(scan the stderr for allocator markers), or a fact already known to be false of the case that
motivated this (the exit code and the presence of a traceback — `torch.OutOfMemoryError` RAISES,
so it exits 1 with a full traceback and every conjunct of the old kernel signature is false), or
unavailable (device memory after the process is gone). So the conclusion cannot be re-checked.

What CAN be checked, cheaply and locally, is whether the diagnostician actually LOOKED: it must
cite the evidence it stands on — which file and line, or which log — and the engine re-resolves
that citation and records whether it resolved. That is not a proof the verdict is right. It is
what makes a wrong verdict AUDITABLE afterwards, which is the property the brief asked for and the
strongest one available here. An agent whose verdict cannot be re-checked at all is just a more
expensive regex.

IT RECORDS, IT DOES NOT REFUSE, and that is a cost decision rather than a taste one. A refusal
would demote an uncited-but-correct diagnosis to `unclassified`, i.e. lose it. The rate at which a
live model mis-formats a citation is not known here — no corpus row carries one yet, because the
field is new — and a corroboration requirement that fires on 2% of cases while blocking 20% of
real diagnoses is a bad trade. So the check lands on the durable row as
`evidence_resolved: true|false`, the number becomes countable, and the refusal is a decision for
whoever reads that number. Do NOT promote it to a gate without one.

-----------------------------------------------------------------------------------------------
WHY THE DIAGNOSTICIAN IS THE TRIAGE CALL AND NOT A SECOND AGENT — argued from the meter.

Measured over the three modern runs that carry `spans.jsonl` (2026-08-20):

    run                        triage decisions   provider calls   wall s    median s   as % of
                               (1 per failure)    inside them      total     per call   run's calls
    rubertlite-dr-unified-v8         20                187          3,328      115.8       3.6%
    rubertlite-dr-unified-v9          9                 78          1,548      174.6       2.8%
    e5small-dr-unified-v3             9                 70          2,021      276.2       3.5%

    ------------------------------------------------------------------------------------------
    pooled                           38                335          6,898       —          3.3%

**The triage call is ALREADY a multi-turn tool-carrying agent that spends 8.82 provider calls per
failure** (9.35 / 8.67 / 7.78 per run) — it drives `agents/tool_loop.py::drive_tool_loop`
over the pilot tools plus `train_monitor.repair_log_tools`, it is already handed exactly the
evidence this question needs (the error text, the repair history, the pipeline depth, the dead
eval's own stage logs), and it fires exactly once per failed attempt. A separate 9-turn
diagnostician would roughly DOUBLE the failure-path agentic cost — +335 provider calls across
those three runs, 3.3% of a run's generations to ~6.6% — and add a second 116-276 s median wait to
every failed attempt (+6,898 s in total across the three), on the EVAL-BLOCKING thread, at the
worst possible moment: after a multi-hour node has already died, with the GPU idle behind it.

It would also be able to CONTRADICT ITSELF. The action and the kind are not independent: the
directive `_repair_error_context` renders is BUILT from the kind, so a diagnostician answering
`oom` while a separate triage answers `repair` from a `crash` reading produces a repair pointed at
a bug that is not there — which is the v3 incident, reintroduced through the other door. One agent
answering both questions from one reading cannot disagree with itself.

So the classification RIDES the emit that is already paid for, and what this change buys is spent
on EVIDENCE instead of on calls: the same call, plus the code the eval actually ran
(`diagnosis_code_tools`, rooted at the workdir, +3 turns), plus a citation it must stand on.
Marginal cost measured in calls: ZERO — the classification and the evidence ride an emit that was
already being paid for. Marginal cost in turns: at most 3 of ~8.8, and only when the model chooses
to spend them.
"""
from __future__ import annotations

from pathlib import Path

# `FAILURE_REASONS` is the closed vocabulary every tuple below partitions or draws from. It is
# DEFINED in `core/models.py` because `core/config.py` needs it for the `inline_repair_reasons`
# default and core may not import from `engine`; it is imported here so the split reads beside the
# thing it splits.
from looplab.core.models import FAILURE_REASONS  # noqa: F401

# --- The ownership split -----------------------------------------------------------------------
# See the module docstring for the per-reason argument. These three tuples are a REGISTRY in
# CLAUDE.md's sense — a duck-typed seam across the classifier, the agent's emit schema and the
# engine's coercion — and `tests/test_failure_ownership_split.py` derives them from the code rather
# than restating them, because a reason that drifts to the wrong side of this line does not fail,
# it just silently lets a model contradict a fact or silently keeps a diagnosis unasked.

# What the engine CAUSED, RAN or MEASURED and remembers doing. Never asked; never overridable.
ENGINE_FINAL_REASONS: tuple[str, ...] = (
    "timeout", "diverged", "stalled", "not_learning", "drift", "setup",
    "needs_failed", "expect_failed")

# The deterministic answers `_failure_reason` still PRODUCES that are handed to the diagnostician as
# evidence rather than kept as answers. Every one is a reason no out-of-band channel witnessed
# (`crash`/`no_metric`) or one whose "channel" is itself a model (`check_failed`).
#
# `oom` IS DELIBERATELY ABSENT, and its absence is the sharpest statement of what this change did.
# Both of its producers were text rules — the `-9/137 + no-Traceback` kernel signature and
# `_is_torch_oom`'s allocator marker list — so deleting them left the engine with NO way to say
# `oom` at all. It is not a deterministic answer under review; it is a kind only the diagnostician
# can ever name, which is exactly right for the one failure class where no out-of-band channel can
# exist: the engine did not cause the exit, the candidate's own allocator raised, nothing observed
# it, and device-level free memory is sampled after the process is gone.
#
# So this tuple is what gets ASKED ABOUT and `DIAGNOSED_FAILURE_REASONS` is what may be ANSWERED,
# and they are not the same set in either direction — `oom` and `not_learning` are answer-only.
DIAGNOSABLE_ENGINE_REASONS: tuple[str, ...] = ("crash", "no_metric", "check_failed")

# The CLOSED vocabulary the diagnostician may answer with. A value outside it is REFUSED, and the
# refusal is not cosmetic: `Settings.inline_repair_reasons` selects on these exact strings, so an
# invented kind would silently make a failure class unrepairable with nothing red anywhere — the
# drift `tests/test_inline_repair_reason_coverage.py` exists for.
#
# THE ONE MEMBER THAT IS ALSO ENGINE-FINAL IS `not_learning`, AND IT IS DELIBERATE. Stating it as a
# registered exception rather than letting it hide inside a set operation, because a reviewer's
# first instinct — and the brief's — is that the two vocabularies must be disjoint:
#
#   * WITHOUT IT THIS CHANGE CANNOT FIX ITS OWN MOTIVATING CASE. The sixteen
#     `runs/rubertlite-dense-retrieval` terminals are `check_failed` whose real cause is "the loss
#     never moved". If the diagnostician may not say `not_learning`, the best it can do for them is
#     `no_metric`, and the record still does not say what happened.
#   * DISJOINTNESS IS THE WRONG PROPERTY, and asking for it conflates a CAUSE vocabulary with a
#     PRODUCER partition. "The loss never descended" is one cause with two possible witnesses: the
#     training watchdog killed the run over it (engine-final, `MONITOR_REPAIR_REASON`), or nobody
#     killed anything and a model read it off the log afterwards. The property that actually keeps
#     the engine safe is the ASYMMETRY, and it is what the guard test drives: whenever the ENGINE's
#     own answer is engine-final, the diagnostician is not consulted at all. A diagnostician
#     answering `not_learning` about a `check_failed` contradicts nothing the engine observed.
#   * IT BUYS NOTHING PRIVILEGED. `not_learning` is absent from
#     `metric_salvage.NEVER_SALVAGED_REASONS`, so it can neither suppress a metric nor admit one;
#     `triage._rule_triage` bounds it by the same `max_attempts` as a crash, so it buys no extra
#     attempt; and the gate that reads it (`inline_repair_reasons`) is evaluated ABOVE the
#     diagnostician on the deterministic answer. A wrong `not_learning` therefore costs exactly one
#     repair round pointed at the objective instead of at the real bug — the same thing every other
#     wrong kind costs.
#
# `timeout` is the near-miss that shows the line is real and is not just "whatever the model might
# usefully say". v8 node 9's failure genuinely WAS a run that could not fit its budget, and the
# diagnostician still may not answer `timeout`: the engine's clock did not fire, so answering it
# would be a model asserting that an engine mechanism it cannot observe did something. It says
# `check_failed` or `crash` and puts "ran out of budget" in its EVIDENCE, where a reader can weigh
# it. `timeout` is also in `NEVER_SALVAGED_REASONS`, so admitting it here would hand a model the
# power to suppress a metric — the direction `docs/36` refuses outright.
DIAGNOSED_FAILURE_REASONS: tuple[str, ...] = (
    "crash", "oom", "no_metric", "check_failed", "not_learning")

# The two ANSWER-ONLY kinds, i.e. the members no classifier produces. `oom` because both of its
# producers were the deleted text rules (see `DIAGNOSABLE_ENGINE_REASONS`), `not_learning` because
# its only engine producer is a watchdog KILL and the diagnostician's answer is about a run nothing
# killed. Spelled so the guard test can assert the two vocabularies' relationship exactly instead of
# asserting "they overlap somehow".
DIAGNOSED_ONLY_REASONS: tuple[str, ...] = ("oom", "not_learning")

# The registered overlap with ENGINE-FINAL, spelled once so the guard test can assert it EXACTLY
# rather than assert "some overlap is allowed". Adding a member here means arguing the three bullets
# above for it.
DIAGNOSED_ENGINE_FINAL_OVERLAP: tuple[str, ...] = ("not_learning",)

# --- "Nobody could say" ------------------------------------------------------------------------
# THE FALLBACK, AND IT IS NOT A REGEX. When the diagnostician was WIRED and ASKED and could not
# produce a readable kind after `triage._TRIAGE_REASK_LIMIT` has already been spent, the honest
# answer is not the residual it was handed — that residual is a NOMINATION that never got a
# decision, and recording it as `crash` makes a diagnostician that failed indistinguishable from
# one that agreed. It is `unclassified`, and its four properties are load-bearing:
#
#   1. IT ROUTES TO A BOUNDED REPAIR. It is in `core/models.py::FAILURE_REASONS`, therefore in the
#      default `Settings.inline_repair_reasons`, so the node is still repaired rather than thrown
#      away over a flapping provider — and `triage._rule_triage` gives it the BLIND bound
#      (`_RULE_BLIND_CRASH_ATTEMPTS`), never the unbounded path, because a failure nobody could
#      name is the definition of repairing blind.
#   2. IT IS NOT IN `metric_salvage.NEVER_SALVAGED_REASONS`. A diagnostician being unreachable must
#      never suppress a metric the eval really produced.
#   3. IT BUYS NO EXTRA ATTEMPT. Same `max_attempts` as a crash; the hard cap is untouched.
#   4. IT IS COUNTABLE. `REASON_SOURCE_UNDIAGNOSED` on the durable row, so "how often was the
#      diagnostician unavailable" is a query rather than a guess, and `engine_reason` still carries
#      the deterministic column beside it so no audit loses the structural answer.
#
# WHAT IT IS NOT: the answer when no diagnostician is WIRED AT ALL. That is a configuration, not a
# failure — the same distinction `triage.py`'s verdict contract draws between `unanswerable` (the
# judge was there and the transport died) and the `_rule_triage` path (no judge was ever there).
# An offline run, a toy backend and every `unified_agent=false` run keep the engine's structural
# residual with `REASON_SOURCE_ENGINE`, byte for byte as before. `DIAGNOSIS_UNAVAILABLE_KEY` is how
# the two are told apart, and it is unforgeable by construction — see below.
UNCLASSIFIED_REASON = "unclassified"

# WHO CHOSE THE `reason` ON A DURABLE ROW. Stamped by the ENGINE beside the value, never by the
# model. This is the `at_pending` SHAPE rather than the `TrainingVerdict.fault` one: `fault` is a
# field the MODEL emits, so it can describe its own judgement but cannot witness that it made one,
# while this is a column the engine writes recording what IT independently held at that instant.
# The row keeps BOTH `reason` and `engine_reason`, so the structural column is never destroyed and
# any audit — including the corpus replay that motivated this change — can still be run against it.
# An ABSENT value on an old row means "nobody looked", which is not the same fact as `engine`.
REASON_SOURCE_ENGINE = "engine"
# The diagnostician answered. Spelled `triage` and not `diagnostician` because the diagnostician IS
# the triage call (see the cost argument in the module docstring) and because rows written on
# 2026-08-20 already carry this literal — renaming it would split one fact across two spellings in
# the durable record for no reader's benefit.
REASON_SOURCE_TRIAGE = "triage"
# The diagnostician was wired, was asked, and did not produce a readable kind. Pairs with
# `UNCLASSIFIED_REASON` and exists so that unavailability is countable rather than papered over as
# agreement.
REASON_SOURCE_UNDIAGNOSED = "undiagnosed"
REASON_SOURCES: tuple[str, ...] = (REASON_SOURCE_ENGINE, REASON_SOURCE_TRIAGE,
                                   REASON_SOURCE_UNDIAGNOSED)

# The key an ENGINE-SIDE caller stamps to report that NO diagnostician was wired, so the rule path's
# verdict is not mistaken for a diagnostician's non-answer. Unforgeable from the wire BY
# CONSTRUCTION, exactly like `triage.TRIAGE_TRANSPORT_FAILURE_KEY`: the model answers through a
# JSON-schema tool call, and `crash_repair._ask_triage` REBUILDS the returned dict from a fixed list
# of keys, so no model output can ever set this one. Its only writer is `triage._rule_triage`.
DIAGNOSIS_UNAVAILABLE_KEY = "diagnosis_unavailable"

# --- The evidence contract ----------------------------------------------------------------------
# WHERE THE DIAGNOSTICIAN LOOKED, in the two shapes it can have. A closed vocabulary rather than
# free text because the ENGINE re-resolves the citation and has to know what kind of thing it is
# being handed; an unrecognised source is dropped to `EVIDENCE_SOURCE_NONE` rather than guessed at.
EVIDENCE_SOURCE_CODE = "code"       # a file in the node's workdir, optionally `path:line`
EVIDENCE_SOURCE_LOG = "log"         # a stage log the eval wrote
EVIDENCE_SOURCE_ERROR = "error"     # the captured stderr/stdout tail it was handed
EVIDENCE_SOURCE_NONE = "none"       # it cited nothing readable
EVIDENCE_SOURCES: tuple[str, ...] = (EVIDENCE_SOURCE_CODE, EVIDENCE_SOURCE_LOG,
                                     EVIDENCE_SOURCE_ERROR, EVIDENCE_SOURCE_NONE)

# Durable-row bounds. The locator is a path (possibly `path:line`) and the quote is one line of
# what was there; both land on `node_failed`/`node_repaired`, so both are capped at the row-sized
# 300 every other model-authored string on those rows wears.
EVIDENCE_LOCATOR_CAP = 300
EVIDENCE_QUOTE_CAP = 300

# The ADDITIONAL turn grant that comes with the code scouts, over and above
# `unified_agent.UnifiedAgent._REPAIR_LOOK_TURNS` (4, which is the log tools' grant). THREE, and the
# number is `train_monitor._MONITOR_LOOK_TURNS`' own 2026-08-18 argument applied one role over: that
# budget moved 6 -> 9 when the same code scouts were added to the live watchdog, because attributing
# a cause to the implementation means locating the file that sets the parameter (a `grep`), reading
# it (a `read_file`, possibly a second page), and doing it WITHOUT giving up the log evidence the
# verdict is primarily about. A budget that forces the diagnostician to choose between looking at
# the failure and looking at the code produces exactly the guess this whole module exists to
# replace.
#
# ADDITIVE and only over a FINITE budget, for `_pilot_emit`'s stated reason: `max_turns=0` means
# unlimited and `0 + n` would silently turn an operator's "no turn cap" into a cap of n.
DIAGNOSIS_CODE_LOOK_TURNS = 3


def coerce_failure_kind(value, fallback: str) -> str:
    """Normalize a diagnostician-supplied failure kind to a member of `DIAGNOSED_FAILURE_REASONS`,
    failing closed to `fallback`.

    The classification twin of `triage.coerce_triage_action`, and it takes an explicit `fallback`
    because the two callers want different ones: `diagnosed_failure_reason` below passes `""` so it
    can tell "no readable kind" from "a kind", while a caller that merely wants a safe string can
    pass the engine's own answer. Pure and total over junk — a non-string, a `None`, a list, a
    number all answer `fallback`."""
    v = str(value or "").strip().lower()
    return v if v in DIAGNOSED_FAILURE_REASONS else str(fallback)


def coerce_evidence(verdict) -> dict:
    """The diagnostician's citation, normalized to `{source, locator, quote}` and never raising.

    Total over junk on purpose — this runs on the eval loop's failure path, where a raise costs the
    terminal being written. An unrecognised source, an absent key, a non-dict verdict and a model
    that cited nothing all collapse to `EVIDENCE_SOURCE_NONE` with empty strings, which is a
    perfectly good durable row meaning "it did not say where it looked"."""
    if not isinstance(verdict, dict):
        return {"source": EVIDENCE_SOURCE_NONE, "locator": "", "quote": ""}
    src = str(verdict.get("evidence_source", "") or "").strip().lower()
    if src not in EVIDENCE_SOURCES:
        src = EVIDENCE_SOURCE_NONE
    loc = str(verdict.get("evidence_locator", "") or "").strip()[:EVIDENCE_LOCATOR_CAP]
    quote = str(verdict.get("evidence_quote", "") or "").strip()[:EVIDENCE_QUOTE_CAP]
    # A source with nothing to point at is not a citation. Collapsing it here rather than at the
    # sink means the durable row and the re-check agree about what "cited" means.
    if src in (EVIDENCE_SOURCE_CODE, EVIDENCE_SOURCE_LOG) and not loc:
        src = EVIDENCE_SOURCE_NONE
    return {"source": src, "locator": loc, "quote": quote}


def evidence_citation_resolves(evidence, workdir) -> bool | None:
    """Does the cited file actually exist inside the node's workdir? `None` when there is nothing
    checkable to resolve (no citation, or a citation into the error text it was handed anyway).

    THE DIAGNOSTICIAN'S `is_present`, AND ONLY AS FAR AS THAT ANALOGY REALLY GOES. It does not
    check that the verdict is RIGHT — the module docstring explains why no such probe exists for a
    failure kind — it checks that the thing the verdict says it read is a thing that is there. A
    citation to a file the workdir does not contain is a diagnosis nobody can re-derive, which is
    the property that makes a wrong answer auditable.

    CONFINED TO THE WORKDIR, and that is the safety half. The locator is model-authored text
    reaching a filesystem call, so it is resolved against the workdir root and REFUSED if it
    escapes — `..`, an absolute path, a symlink out. A refusal answers False (it did not resolve),
    never a raise and never a read outside the fence. Nothing is opened: `exists()` on a resolved
    path answers the question without paging in a checkpoint on a geesefs mount.

    Three answers, not two, because "it cited nothing" and "it cited something that is not there"
    are different facts about the diagnostician and a durable row that conflates them cannot be
    counted."""
    if not isinstance(evidence, dict):
        return None
    src = str(evidence.get("source", "") or "")
    if src not in (EVIDENCE_SOURCE_CODE, EVIDENCE_SOURCE_LOG):
        return None                       # nothing filesystem-shaped was cited
    loc = str(evidence.get("locator", "") or "").strip()
    if not loc:
        return None
    # `path:line` — the shape the prompt asks for. Split from the RIGHT and only on an all-digit
    # tail, so a Windows-ish `C:\...` or a colon inside a directory name is not mistaken for a line
    # number. This is text SHAPING, not text DECIDING: whatever it produces is then resolved against
    # the real filesystem, which is what answers.
    head, sep, tail = loc.rpartition(":")
    path_text = head if (sep and tail.isdigit() and head) else loc
    try:
        root = Path(workdir).resolve()
        if not root.is_dir():
            return None                   # no workdir to resolve against — unchecked, not failed
        cand = Path(path_text)
        if cand.is_absolute():
            # An absolute path is only admissible if it is already inside the fence; anything else
            # is refused rather than reinterpreted.
            resolved = cand.resolve()
        else:
            resolved = (root / cand).resolve()
        if not (resolved == root or root in resolved.parents):
            return False                  # escaped the fence: unresolvable BY POLICY
        return bool(resolved.exists())
    except (OSError, ValueError, RuntimeError):
        # A malformed path, a NUL byte, a symlink loop, a dead mount. All mean the same thing to the
        # record: the citation did not resolve.
        return False


def diagnosed_failure_reason(deterministic: str, verdict) -> tuple[str, str]:
    """`(reason, source)` — the classification after the diagnostician has been consulted.

    THE WHOLE OWNERSHIP RULE, in one pure function, so that "an engine-final fact is never put to a
    model" is a property of one readable expression rather than of a condition spread across the
    eval loop. The branches, in order, and each is a different fact:

      1. The engine's own answer is ENGINE-FINAL -> returned unchanged, and the verdict is not even
         looked at. That is the override, and it is spelled as "the model is never ASKED to
         contradict a fact" rather than "the model's answer is discarded if it contradicts a fact".
         The two behave identically and only the first is checkable by reading this function.
      2. NO DIAGNOSTICIAN WAS WIRED (`DIAGNOSIS_UNAVAILABLE_KEY`, set only by
         `triage._rule_triage`) -> the engine keeps its structural residual with
         `REASON_SOURCE_ENGINE`. A configuration is not a failure, and this branch is what keeps
         every offline/toy/`unified_agent=false` run byte-identical to before.
      3. The verdict is not a readable dict, or its `action` is not an AGENT verdict — a transport
         failure, an unreadable emit, a non-dict — or it carries no kind inside the vocabulary ->
         `UNCLASSIFIED_REASON` with `REASON_SOURCE_UNDIAGNOSED`. It was asked and it could not
         answer, and that is a fact worth recording as itself. See `UNCLASSIFIED_REASON` for the
         four properties that make this safe to route.
      4. Otherwise the diagnostician's kind is the reason and the source says so — INCLUDING when
         it agrees with the engine. That is a 2026-08-20 change from `judged_failure_reason`, which
         stamped `engine` on agreement: under ownership-by-decision the diagnostician DID decide,
         and `engine_reason` on the same row is what lets a reader recover whether the two agreed.
         Attributing its agreement to the engine made a confirmed diagnosis uncountable.

    Total over junk on purpose — this runs on the eval loop's failure path, where a raise would cost
    the terminal that is being written."""
    det = str(deterministic)
    if det not in DIAGNOSABLE_ENGINE_REASONS:
        return det, REASON_SOURCE_ENGINE
    if not isinstance(verdict, dict):
        # Not "the diagnostician failed" — a caller that has no verdict object at all never reached
        # one, which is the no-judge shape. Fail to the engine's own answer, as before.
        return det, REASON_SOURCE_ENGINE
    if verdict.get(DIAGNOSIS_UNAVAILABLE_KEY) is True:
        return det, REASON_SOURCE_ENGINE
    # Deferred (function-local) import: `triage` imports THIS module at module scope so the split
    # reads beside the classifier it splits, and a module-scope import back would close the cycle.
    from looplab.engine.triage import AGENT_TRIAGE_ACTIONS
    if str(verdict.get("action", "")).strip().lower() not in AGENT_TRIAGE_ACTIONS:
        return UNCLASSIFIED_REASON, REASON_SOURCE_UNDIAGNOSED
    kind = coerce_failure_kind(verdict.get("failure_kind"), "")
    if not kind:
        return UNCLASSIFIED_REASON, REASON_SOURCE_UNDIAGNOSED
    return kind, REASON_SOURCE_TRIAGE


def diagnosis_code_tools(engine, workdir):
    """The read-only CODE scouts the diagnostician may look with, rooted at the NODE WORKDIR — or
    None when the tools are off or there is no workdir to root them at.

    THE SAME CONSTRUCTION `train_monitor.monitor_code_tools` MAKES, and deliberately the same
    argument, because it is the same question one role over. The live watchdog got these scouts
    because `fault` splits a `broken` verdict into "the code is wrong" and "the idea is wrong" and
    a frozen loss looks identical either way from the outside; the diagnostician is asked a harder
    version — `oom` vs `crash` vs `not_learning` — from a dead process instead of a live one.

    ROOTED AT THE WORKDIR, which is the whole safety argument as well as the accuracy one:

    - it is the code that ACTUALLY RAN. The pilot's own `read_code` is rooted at the editable
      SOURCE, a different filesystem from the one the eval saw — a distinction that already cost a
      run (`runs/rubert-dr-0807` node 2 died on a missing `<workdir>/looplab_eval.py` while the
      repair session's `read_file` cheerfully returned it). A diagnostician reading the source tree
      would be answering about a program that is not the one on trial.
    - it is the one region that provably holds only what THIS node produced. No other node's
      workspace, no operator secret outside it, no engine source.
    - the direction of harm is favourable, and it is the same bound the whole feature wears:
      everything read here is the candidate's own text, exactly like the stderr tail this judge has
      always been handed, and the widest thing that text can now buy is a repair directive plus a
      word on a durable row. It cannot mint a metric, move a champion, clear a violation or change
      a selection — `_salvage_eval_metric` and the `inline_repair_reasons` gate both run ABOVE this
      call on the engine's own answer.

    GATED ON `Settings.repair_log_tools`, the diagnostician's existing surface, rather than on a
    new switch. The two are one paid look at one moment — the operator who allowed this judge to
    read the dead eval's logs has already allowed it to read that eval's own text — and a second
    switch would let a run reach the state "may read the log that says a parameter is absurd, may
    not read the line that sets it", which is the exact half-blindness `_MONITOR_LOOK_TURNS`' 6 -> 9
    move was made to end. `getattr` is total over a partially-built engine.
    """
    if not getattr(engine, "_repair_log_tools", False):
        return None
    try:
        root = Path(workdir)
        if not root.is_dir():
            return None
    except (OSError, ValueError, TypeError):
        return None
    from looplab.tools.reposcout import RepoScoutTools
    return RepoScoutTools(roots=[str(root)], default_root=str(root))


def diagnosis_tools(engine, workdir, log_plan=None, log_snapshot=None):
    """Everything the diagnostician may look with: the dead eval's own stage logs AND the code that
    wrote them. `None` when neither is available, which is what `triage_crash` reads as "no extra
    tools" and is the historical stderr-tail-only ask byte for byte.

    Composed HERE rather than at the call site for `train_monitor.monitor_tools`' reason — the roles
    that may look must not be able to come to disagree about what looking MEANS — and with the same
    ordering rule: `CompositeTools` de-dups by tool NAME with the first provider winning, and the
    LOGS go first deliberately, so a name collision can never silently shadow `read_log` (which
    knows about this attempt's byte floor) with a general file reader that does not.

    It is called off the event loop by its one caller (`engine/evaluate.py`), because
    `monitor_log_sources` globs the workdir and opens + probes every stage log, and on the
    geesefs/S3 mounts a run root usually lives on a missed directory lookup costs 105-950 ms."""
    from looplab.engine.train_monitor import repair_log_tools
    providers = [p for p in (repair_log_tools(engine, workdir, log_plan, log_snapshot),
                             diagnosis_code_tools(engine, workdir)) if p is not None]
    if not providers:
        return None
    if len(providers) == 1:
        # Not wrapped in a one-element composite: that would be a different object with a different
        # `specs()` ordering, and the single-provider path must stay byte-identical to what the log
        # tools alone have always produced.
        return providers[0]
    from looplab.agents.tool_loop import CompositeTools
    return CompositeTools(providers)
