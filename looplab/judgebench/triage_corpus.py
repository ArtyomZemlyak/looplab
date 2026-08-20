"""Turn `runs/` into a labelled bench for the FAILURE CLASSIFIER — `engine/triage.py::_failure_reason`
and whatever replaces it.

This is the second bench in this package and it exists for a specific reason: an agent-driven
diagnostician is replacing the substring rules in `_failure_reason`, and without a corpus the only
evidence the swap helped would be that the next run felt better. That is the failure mode the whole
package was built to kill. `judge_corpus.py` benches the training-log monitor; this benches the
question one layer down — **why did this eval produce no usable metric?**

## The row

One row per FAILURE CLASSIFICATION EVENT: one eval attempt that ended with no metric, and therefore
exactly one call of `_failure_reason`. They are enumerated from the durable event log, not from
spans, so a run whose spans were pruned still contributes:

* every `node_repaired` — the attempt that preceded the repair failed, and was classified;
* every `node_failed` whose reason is a real classification (`superseded` / `developer_crash` are
  dropped: the node was never run against its own idea, or the DEVELOPER's session died, and neither
  is a reading of an eval);
* a terminal that follows its own repair with **no intervening eval** is the SAME failure as that
  repair and is merged into it. Byte-identical error text is NOT the merge test and must not be:
  `e5small-dr-unified-v3` node 2 failed its artifact contract four times with the same message to
  the byte, and merging on text would have collapsed four distinct classifications into one.

## The evidence, split by who could see it

`evidence.at_classification` is what `_failure_reason` itself had: the stderr tail as the engine
recorded it (~500 chars — that IS the durable record, see the honesty note below), the failed
stage's exit code, the attempt's `stage_finished` rows and their statuses. `evidence.on_demand` is
what a TOOL-USING diagnostician could fetch and the substring rule structurally could not: the
`read_log` tool outputs the triage agent actually pulled at that moment, and the failed stage's own
log file where its mtime pairs it to this attempt. The split is not cosmetic — it is how the bench
can say whether a candidate's win comes from reading better or from looking further.

**The 500-character honesty note.** `res.stderr` was clamped at 64,000 bytes per stream when
`_failure_reason` read it; what SURVIVED to disk is `node_repaired.error_in`, 500 characters. So a
replay over the stored tail is not always the answer the engine gave. Measured on this corpus:
**not one of the 122 stored tails contains a `_TORCH_OOM_MARKERS` string** — 5 are a launcher's
opaque `Root Cause … exitcode: 1` block and 2 are nothing but a progress bar — so `_is_torch_oom`
replayed over `at_classification` alone scores 0 of 23 OOMs, and over `on_demand` as well it scores
16. The recorded reason is therefore kept AS the incumbent's answer and never recomputed for the
primary matrix; `--arm frozen` and `--arm frozen-widened` are what separate the RULE from the
WINDOW, and `--arm live` is today's classifier. The frozen arms read a SNAPSHOT of the classifier
and of its vocabulary (`TORCH_OOM_MARKERS` here, `_frozen_failure_reason_v1` in `triage_score.py`,
both echoed into the dataset header) rather than production, because the classifier they describe
was replaced hours after this corpus was cut and an arm that followed production would have gone on
reporting a different program's score under the label "the incumbent".

## The label

**The regex's own answer cannot be the label** — that measures agreement, not accuracy. Every label
here rests on a fact `_failure_reason` did not author, and `LABEL_BASES` is the closed list of what
those facts are, each with the confidence it earns:

| basis | fact | confidence |
|---|---|---|
| `reused_stage_later_scored` | the operator reset the node, the SAME stage output was `reused`, and it scored a healthy metric — so the stage did not fail | high |
| `oom_marker_in_evidence` | a `_TORCH_OOM_MARKERS` string in the log the triage agent read or in the paired stage log | high |
| `allocator_message_in_stderr` | the allocator's own message body (`Tried to allocate … GiB … free`) in the recorded tail | high |
| `watchdog_sentinel` | the ENGINE's own `‼ LOOPLAB health-check:` line, which only the engine writes | high |
| `stage_timeout` | `stage_finished.status == "timeout"`, the engine's own clock | high |
| `nonfinite_loss_in_log` | repeated `loss=nan` / `loss=inf` / `loss=-2e+10` in the paired stage log | high |
| `artifact_contract` | `expect_failed`: the engine compared the declared path's mtime to the stage start | high |
| `declared_condition_violated` | the check quotes the epoch the log itself reports against the epoch the manifest declared | high |
| `terminal_exception` | a named non-OOM Python exception is the last line of the traceback — the careful reader's read | high |
| `logged_fatal_error` | the program's own logger printed the fatal condition and then exited non-zero | high |
| `check_concern_nonfinite` | the stage checker quotes a non-finite or exploded loss it read out of the log | medium |
| `check_concern_no_learning` | the stage checker read the log and called the training dead, with no reuse-score to acquit it and nothing non-finite to convict the numerics | medium |
| `reviewed` | a case the rules above cannot reach, read by hand, carrying the exact evidence string it was read from | medium |

Anything else is `unknown` and is EXCLUDED from the matrix rather than guessed. A padded corpus is
worse than a thin one, and the count of `unknown` is printed with every report.

**No label here rests on a model's prose, and one rule was deleted to keep it that way.** The
obvious eleventh basis is the triage agent's own rationale: it HAD the log tools, it read the stage
log, and it repeatedly contradicts the reason it was handed ("Genuine CUDA OOM at step 0, 139.48 /
139.80 GiB in use" under a `[failure kind: crash]` header). That rule was written, run, and
removed. It fired on exactly ONE row in 122, and on that row it was WRONG: the rationale for
`rubertlite-dr-unified-v7` node 1 attempt 3 recites a failure HISTORY — "the failure is moving (OOM
-> faiss GPU error -> no-traceback crash at step 50)" — and a substring rule over prose read the
history as the diagnosis. Every other OOM it would have caught was already caught by the
allocator's own words. So all 24 `oom` labels rest on strings torch itself printed, the deleted
rule bought nothing, and the row it got wrong is now `unknown`.

## What this corpus refuted

The operator's starting premise was that the 16 terminals in `runs/rubertlite-dense-retrieval` are
really `not_learning` and were classified `check_failed`. Both halves are wrong and the corpus says
so from evidence:

* they were recorded `no_metric`, not `check_failed` — the reason did not exist yet; replayed at
  HEAD they DO come out `check_failed`, which is that fix working;
* **10 of the 16 were not failures at all.** The operator later reset each node from the `score`
  stage, the train stage was `reused` (seconds=0.0 — the very checkpoint the checker condemned) and
  the node scored 0.805 – 0.8662 against a run best of 0.8835. The stage-check had read only the
  last 4,000 characters of the log, seen a flat loss inside the final epoch, and called a converged
  training "no learning progress". `rubertlite-dense-retrieval` node 1's loss went 33.9 → 13.3
  monotonically and it scored 0.805.
* **the window that produced those ten is since fixed.** The stage checker read `run.out[-4000:]`;
  the trajectory veto (`engine/eval_stages.py`, 2026-08-20) widened what it is judged on, so a
  rerun of those nodes today would not be condemned. No label moves — what acquits them is the
  operator's own re-run, not the checker's later repair — but a reader must not take "10 of 16 were
  false refusals" as a property of the checker that ships. `CORPUS_LIMITS` says so in the header,
  so the caveat travels with the number instead of living only here.
* 4 of the 16 are `diverged` (`loss=inf` for all 20 epochs, `loss=nan`, `loss=-1.5e+10`,
  `-2.35e+08`) — caught by the stage CHECKER because the diverge watchdog did not exist yet;
* exactly ONE — node 12 — is genuinely `not_learning`: its loss fell 0.986 → 0.0195 monotonically
  while validation recall@100 stayed at 0.0028. That is the case the word was added for, and it is
  1 of 122 rows, not 16.
* the last is node 40, whose `soup` stage exited 0 having printed nothing at all.

## A cause the vocabulary cannot name

`Idea.params` is a PROPOSAL, not what ran. On 4 of these 122 rows the stage's own argument parser
REFUSED the parameters the engine substituted into its command, so the hyperparameters the node
existed to test never reached a line of code — and `crash` is still the honest classification,
because no member of `FAILURE_REASONS` says "the experiment's parameters were never applied". Those
rows carry `cause_notes.params_rejected_by_stage` (the refused `--names`) and
`cause_notes.declared_params` beside the label, never as one: a thirteenth reason would break the
single property that lets this corpus claim the classifier is wrong at all, which is that its
vocabulary is the classifier's own. The annotation is what a diagnostician can be asked to NAME.

The silent twin is larger and cannot appear here at all. Measured across every run on disk
(2026-08-20): 457 comparisons of declared params against the node's own `config.yaml`, **41 diverged
(9.0 %)**, 18 of them on nodes that PRODUCED a metric — the e5 champion at 0.793426 is recorded as
batch 8192 / accum 2 / 15 epochs and ran batch 512 / accum 32 / 3 epochs. A corpus of failures is
structurally blind to a defect whose whole shape is a node that succeeds at the wrong experiment.

## Redaction

Same rule as `judge_corpus.py`, for the same reason: a committed dataset is an egress boundary, and
the stored text is a candidate's own stderr and log output. Everything goes through
`core/redact.py::redact_output_tail`, the SAME screen `engine/audit.py::Engine._redact` applies to
persisted tails.

## Scope

Seven preserved runs of ONE task family (ESCI dense retrieval, two backbones), ONE operator, one
box. A run still being appended to is REFUSED (`LIVE_RUN_GRACE_S`) rather than half-labelled.
`CORPUS_LIMITS` travels in the dataset header so the caveat cannot be separated from the number.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable, Iterator, Optional

from looplab.core.models import FAILURE_REASONS
from looplab.core.redact import redact_output_tail

# Bumped when the ROW SHAPE or a LABEL RULE changes: a candidate scored against v1 and one scored
# against v2 are not comparable, and the header carries the version so a stale comparison is visible.
DATASET_SCHEMA = "looplab.judgebench.failure_triage.v1"

JUDGE_FAILURE_TRIAGE = "failure_triage"

LABEL_UNKNOWN = "unknown"
# The label vocabulary IS `FAILURE_REASONS` plus `unknown`. Deliberately imported rather than
# copied: a bench whose vocabulary drifts from the classifier's cannot say the classifier is wrong,
# only that it disagrees with a stale list. `tests/test_triage_bench.py` pins that every label used
# is a member.
LABELS: tuple[str, ...] = FAILURE_REASONS + (LABEL_UNKNOWN,)

# A node terminal that says nothing about an eval: the card gate replaced the node before it ran
# (`superseded`), or the DEVELOPER's own session crashed (`developer_crash`). Neither is a reading
# of a failed eval and neither may enter the corpus.
NON_CLASSIFICATION_REASONS = frozenset({"superseded", "developer_crash"})

# `idea_rejected` is NOT in that set. It is the triage judge's verdict on the IDEA, recorded over
# the top of a real classification — the underlying `_failure_reason` answer is on the triage span
# for that attempt and is recovered from there.
VERDICT_OVERWRITES_REASON = frozenset({"idea_rejected"})

# The closed list of things a label may rest on, most-trusted first. Stored on every row, printed
# with every report, and asserted by the test — a label whose basis is not a member is a defect.
LABEL_BASES = (
    "reused_stage_later_scored", "oom_marker_in_evidence", "allocator_message_in_stderr",
    "watchdog_sentinel", "stage_timeout", "nonfinite_loss_in_log", "artifact_contract",
    "declared_condition_violated", "terminal_exception", "logged_fatal_error",
    "check_concern_nonfinite", "check_concern_no_learning", "reviewed", "none",
)
HIGH_CONFIDENCE_BASES = frozenset({
    "reused_stage_later_scored", "oom_marker_in_evidence", "allocator_message_in_stderr",
    "watchdog_sentinel", "stage_timeout", "nonfinite_loss_in_log", "artifact_contract",
    "declared_condition_violated", "terminal_exception", "logged_fatal_error"})

# A FROZEN SNAPSHOT of `triage._TORCH_OOM_MARKERS` as it stood on 2026-08-20, and it must stay
# frozen even though that name no longer exists in production — the rule was deleted the same day,
# because this corpus is what showed both of its producers were reading the failure's own text.
#
# It is frozen because it decides LABELS. A label rule that followed production would mean the
# corpus said something different every time the classifier changed, and then no two scores taken
# months apart would be comparable — which is the one thing a bench must never lose. The label
# vocabulary is imported (`FAILURE_REASONS`) because it is the space of ANSWERS; this list is part
# of the rule that decides TRUTH, and truth was cut once.
#
# `tests/test_triage_bench.py::test_the_historical_arm_reads_no_production_name` pins that this
# stays a snapshot and that production has not quietly grown the name back.
TORCH_OOM_MARKERS = (
    "OutOfMemoryError", "CUDA out of memory", "HIP out of memory", "XPU out of memory",
)

# The allocator's own message BODY, which survives truncation the marker line does not: torch prints
# "Tried to allocate X GiB. GPU N has a total capacity of ... of which Y is free" and closes with the
# alloc-conf documentation link. 18 of the corpus's recorded tails are cut off after the marker and
# still carry one of these, so this is what rescues them.
_ALLOCATOR_BODY = (
    re.compile(r"Tried to allocate [\d.]+ [KMG]iB"),
    re.compile(r"PYTORCH_CUDA_ALLOC_CONF"),
    re.compile(r"optimizing-memory-usage-with-pytorch-cuda-alloc-conf"),
)

# The ENGINE's own watchdog sentinels. They are read HERE (and never in `_failure_reason`, which
# reads the authenticated `signals` flags instead) because on a preserved run the flag is gone and
# the sentinel is the only surviving witness that the engine — not the candidate — issued the kill.
# Engine-authored, so a candidate cannot mint one into the record retroactively.
DIVERGE_SENTINEL = "LOOPLAB health-check: training DIVERGED"
STALL_SENTINEL = "LOOPLAB health-check: stage STALLED"

# A non-finite or exploded loss in the stage's own log. The exponent floor is 1e+6 rather than
# "non-finite only" because the corpus's real divergences print `-2.35e+08` and `-1.5e+10` — finite
# floats that are unambiguously a blown objective — while its healthy runs top out at loss 33.9.
_NONFINITE_LOSS = re.compile(r"loss=(?:nan|inf|-inf|[-+]?\d+(?:\.\d+)?e[+]0*[6-9]|"
                             r"[-+]?\d+(?:\.\d+)?e[+][1-9]\d)", re.IGNORECASE)
_NONFINITE_MIN_HITS = 3

# The last `SomeError: message` line of a traceback — the careful reader's read, and the one thing
# a human does first. Only accepted as a label when it names an exception that is NOT an OOM.
_TERMINAL_EXCEPTION = re.compile(r"^(?:\[rank\d+\]:\s*)?([A-Za-z_][\w.]*(?:Error|Exception|Exit))"
                                 r": (.*)$", re.MULTILINE)
_NOT_A_CRASH_EXCEPTIONS = frozenset({"OutOfMemoryError", "torch.OutOfMemoryError",
                                     "torch.cuda.OutOfMemoryError", "MemoryError"})

# An argparse/usage refusal: the interpreter exited 2 before any of the candidate's own code ran.
# Not an exception and so invisible to the pattern above, but every bit as definite a crash.
_ARGPARSE_ERROR = re.compile(r"^\S+: error: (unrecognized arguments|argument |the following)",
                             re.MULTILINE)

# The arguments an argparse-based stage REFUSED. This is the visible half of a defect the reason
# vocabulary has no word for: `Idea.params` is a PROPOSAL, and what actually ran is whatever the
# repo did with it. Measured across every run on disk by the coordinator on 2026-08-20: 457
# comparisons of declared params against the node's own `config.yaml`, **41 diverged (9.0 %)**, 18
# of them on nodes that produced a metric — the e5 champion (0.793426) is recorded as batch 8192 /
# accum 2 / 15 epochs and RAN batch 512 / accum 32 / 3 epochs. That silent majority produces no
# failure at all and so cannot appear in a corpus of failures; what CAN appear is the loud half,
# where the engine substituted `%params%` into a command whose parser rejected them and the stage
# died at argv parsing before a line of the experiment ran.
#
# It is recorded as an ANNOTATION and deliberately NOT as a label. `crash` is the honest
# classification — the process exited non-zero at parse time and there is no other member of
# `FAILURE_REASONS` that fits — but "the hyperparameters this node was supposed to test were never
# applied" is the CAUSE, and it is what a diagnostician should be able to say and a substring rule
# cannot. Storing it lets the bench slice on the class without inventing a thirteenth reason, which
# would break the one property that lets this corpus claim the classifier is wrong at all: its
# vocabulary is the classifier's own.
_PARAMS_REJECTED = re.compile(r"error: unrecognized arguments: (.+)$", re.MULTILINE)

# The program's own logger stating the fatal condition on its way out — a deliberate `sys.exit(1)`
# with a diagnosis attached, which is a crash with a better error message and not a missing metric.
_LOGGED_FATAL = re.compile(r"\|\s*ERROR\s*\||^ERROR:|CRITICAL", re.MULTILINE)

# The stage checker quoting a number it read out of the log. A model wrote the sentence, but the
# NUMBER is the log's — which is why this is a label at medium confidence and the checker's other
# readings are not labels at all.
_NONFINITE_CONCERN = re.compile(
    r"\b(?:nan|inf|non-finite|nonfinite|diverg)\w*|loss=?\s*-?\d+(?:\.\d+)?e[+]\d", re.I)

# `metric >= HEALTHY_FRACTION * run_best` is a healthy score, not a rescued corpse. 0.8 is wide on
# purpose: the ten rescued nodes land at 0.91x - 0.98x of their run's best and the corpus holds
# nothing between 0.05x and 0.91x, so the constant cannot be tuned to flatter a result.
HEALTHY_FRACTION = 0.8

# All `stage_finished` rows of one eval attempt are appended in a single burst (measured spread
# < 0.15 s even when a stage ran for an hour), so this groups an attempt without trusting `seconds`.
ATTEMPT_BURST_S = 60.0

# A stage log file belongs to the attempt whose `stage_finished` lands within this of its mtime.
# Measured on `e5small-dr-unified-v3`: every log's mtime matches its stage's row to the second, and
# a node's workspace holds ONLY the last run of each stage — so without this pairing an earlier
# attempt would be labelled from a LATER attempt's log, which is exactly the fabrication this bench
# exists to prevent. Rows that do not pair carry no log and say so.
# A run still in flight is REFUSED, and the reason is the label rather than tidiness: every label
# here rests on what happened NEXT — the repair that followed, the reset that reused the stage, the
# metric the artefact eventually scored — and a run that has not finished has not produced its own
# "next" yet. Measured while building this corpus: `runs/e5small-dr-unified-v4` grew from 292 to 853
# events between two extractions minutes apart and contributed one unlabellable row to the second,
# so a rebuild would not have been byte-identical and nobody would have seen why.
# `run_finished` is deliberately NOT the test: three of the seven preserved runs never wrote one.
LIVE_RUN_GRACE_S = 3600.0

LOG_PAIR_TOLERANCE_S = 5.0
LOG_TAIL_BYTES = 64_000        # what `res.stderr` was clamped to; the window the engine really had
STORED_LOG_TAIL = 6_000        # what is COMMITTED, so the artefact stays small
STORED_TOOL_READS = 3
STORED_TOOL_READ_CHARS = 4_000

# The label-side twin of `triage_score.HISTORICAL_AUTHENTICATED_REASONS`, spelled here so the header
# can carry it without importing the scorer (the corpus module must be readable on its own).
_HISTORICAL_AUTHENTICATED_REASONS = frozenset({
    "drift", "timeout", "setup", "diverged", "stalled",
    "needs_failed", "expect_failed", "check_failed"})

CORPUS_LIMITS = (
    "SCOPE: seven preserved runs of ONE task family (ESCI dense retrieval, two backbones), ONE "
    "operator, one box. A score measured here is evidence about THIS deployment; it is not a "
    "general claim about failure classification. "
    "THE RECORDED REASON IS THE INCUMBENT, NOT THE TRUTH — agreement with it is churn, not "
    "accuracy, and `triage_score.py` never averages the two. "
    "THE INCUMBENT THIS CORPUS MEASURES CONTAINS A DEFECT SINCE REPAIRED. Ten of the sixteen "
    "`rubertlite-dense-retrieval` terminals were condemned by a stage checker that could see only "
    "the last 4,000 characters of the training log, so it read a flat loss inside the final epoch "
    "as 'no learning progress' on runs that had converged. The trajectory veto "
    "(`engine/eval_stages.py`, 2026-08-20) widens what it is judged on, and those nodes would not "
    "be condemned today. NOT ONE LABEL MOVES: the rows record what happened, and what acquits them "
    "is the operator's own reused-and-scored re-run, not the checker's later repair. But 10-of-16 "
    "is a property of a checker with a broken window, not of the checker that ships, and any rate "
    "quoted from these rows about STAGE CHECKING is a historical rate. The failure-CLASSIFICATION "
    "scores are unaffected, because they replay a recorded `res` whose stage statuses were written "
    "at the time - verified: `--arm live` is 88/118 both before and after the veto landed. "
    "THE STORED STDERR IS THE 500-CHARACTER DURABLE TAIL, not the 64,000-byte stream "
    "`_failure_reason` actually read: no stored tail contains a torch OOM marker, so a candidate "
    "replayed over `evidence.at_classification` alone is strictly worse informed than the engine "
    "was, and `evidence.on_demand` is what closes that gap."
)


class LiveRunRefused(Exception):
    """A run whose event log is still being appended to cannot be labelled from its own outcomes."""


def _redact(text: str) -> str:
    """The one egress screen, and deliberately the SAME one persisted tails already go through."""
    if not text:
        return text or ""
    return redact_output_tail(text, entropy=True)


def _iter_jsonl(path: Path) -> Iterator[dict]:
    """Stream a JSONL, skipping torn lines (a partially-flushed tail is not a corpus defect)."""
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _iter_spans(path: Path, want: tuple) -> Iterator[dict]:
    """Stream `spans.jsonl` — 307 MB on one run — parsing only the lines that can matter.

    The span `name` is the first key of every line, so the prefilter is a string compare and the
    288 MB of `generation` spans (whole prompts and completions) are never parsed. `tool` lines are
    parsed only when `want` asks for them, because their `output` is where the triage agent's log
    reads live and that is the strongest evidence this corpus has.
    """
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            if line.startswith('{"name":"generation"'):
                continue
            if line.startswith('{"name":"tool"') and "tool" not in want:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _attempt_stage_rows(rows: list, ts: float) -> list:
    """The `stage_finished` burst that belongs to the eval that ended at or just before `ts`."""
    before = [(t, d) for t, d in rows if t <= ts + 2.0]
    if not before:
        return []
    last = before[-1][0]
    return [d for t, d in before if last - t < ATTEMPT_BURST_S]


def _failed_stage_name(data: dict, error: str) -> Optional[str]:
    """The stage the engine says failed, from the record where it is explicit and from the error
    text's own engine-authored prefixes where it is not (older runs did not carry the field)."""
    name = data.get("failed_stage")
    if name:
        return str(name)
    for pattern in (r"^\[failed stage: ([^\]]+)\]", r"^stage '([^']+)'"):
        found = re.match(pattern, error or "")
        if found:
            return found.group(1)
    return None


def _oom_evidence(texts: Iterable[str]) -> dict:
    """Which OOM witnesses are present, kept apart by strength rather than OR-ed into a boolean."""
    markers, body = [], []
    for text in texts:
        if not text:
            continue
        markers += [m for m in TORCH_OOM_MARKERS if m in text and m not in markers]
        body += [p.pattern for p in _ALLOCATOR_BODY if p.search(text) and p.pattern not in body]
    return {"markers": markers, "allocator_body": body}


def _terminal_exception(text: str) -> Optional[dict]:
    """The last named exception in a traceback — what a careful reader reads first."""
    found = _TERMINAL_EXCEPTION.findall(text or "")
    if not found:
        return None
    name, message = found[-1]
    return {"exception": name, "message": message[:200]}


def _rejected_params(text: str) -> list:
    """The `--name` arguments a stage's parser refused — the hyperparameters that never ran."""
    found = _PARAMS_REJECTED.search(text or "")
    if not found:
        return []
    return sorted({token.split("=", 1)[0] for token in found.group(1).split()
                   if token.startswith("--")})


def _first_line_matching(text: str, pattern, *, last: bool = False) -> Optional[str]:
    """The whole matching LINE, not the match — evidence a reader can read without the file."""
    lines = [line.strip() for line in (text or "").splitlines() if pattern.search(line)]
    if not lines:
        return None
    return (lines[-1] if last else lines[0])[:300]


def _nonfinite_hits(text: str) -> int:
    return len(_NONFINITE_LOSS.findall(text or ""))


def _log_tail(path: str, size: int) -> str:
    with open(path, "rb") as handle:
        handle.seek(max(0, size - LOG_TAIL_BYTES))
        return handle.read().decode("utf-8", "replace")


def _stage_logs(run_dir: Path) -> dict:
    """`(node_id, stage) -> (path, stat_result)` for every stage log left in the workspaces.

    The `stat_result` is carried whole rather than unpacked into a `(path, mtime, size)` tuple, and
    that is not a style choice: `tests/test_file_identity_tiers.py` reads a tuple built out of two
    or more `st_*` fields as a HAND-ROLLED FILE-IDENTITY SIGNATURE and counts it against a ledger
    that may not grow. It would be reading this one wrong — the two fields are used for two
    unrelated jobs, `st_mtime` to decide WHICH ATTEMPT a log belongs to and `st_size` to seek its
    tail — but a shape that has to be explained to a guard every time it is read is the wrong shape.
    Neither is an identity claim, so neither `core/atomicio.file_identity` nor a tuple of stat
    fields is what this wants."""
    found: dict = {}
    nodes = run_dir / "nodes"
    if not nodes.is_dir():
        return found
    for node in nodes.iterdir():
        if not node.name.startswith("node_"):
            continue
        try:
            node_id = int(node.name.split("_", 1)[1])
        except ValueError:
            continue
        for log in node.glob("*.log"):
            try:
                stat = log.stat()
            except OSError:
                continue
            found[(node_id, log.stem)] = (str(log), stat)
    return found


def _later_reused_score(events: list, node_id: int, after_seq: int, stage: Optional[str],
                        run_best: float) -> Optional[dict]:
    """Did this node's SAME stage output later score a healthy metric?

    The strongest fact in this corpus and the one that refutes the premise the bench was handed. It
    requires all three: a `stage_finished` for the failed stage with status `reused` (so nothing was
    recomputed — `seconds` is 0.0 on every one of them), a `node_evaluated` after it, and a metric
    at or above `HEALTHY_FRACTION` of the run's best. A rescued-but-dead metric is not evidence the
    stage was healthy, so the fraction gate is not optional.
    """
    if not stage:
        return None
    reused_at = None
    for event in events:
        data = event.get("data") or {}
        if data.get("node_id") != node_id or (event.get("seq") or 0) <= after_seq:
            continue
        if (event.get("type") == "stage_finished" and data.get("name") == stage
                and data.get("status") == "reused"):
            reused_at = event.get("seq")
        if event.get("type") == "node_evaluated" and reused_at is not None:
            metric = data.get("metric")
            if isinstance(metric, (int, float)) and run_best > 0:
                return {"metric": float(metric), "run_best": run_best,
                        "fraction_of_best": round(float(metric) / run_best, 4),
                        "reused_seq": reused_at, "scored_seq": event.get("seq")}
    return None


# --- the label rules ---------------------------------------------------------------------------
# Ordered, and the order IS the argument: an authenticated engine fact outranks a text read, and a
# text read outranks a model's prose. `derive_label` is a PURE function of the facts stored on the
# row, so `tests/test_triage_bench.py` re-derives every label offline on a machine with no `runs/`
# — which is what makes a hand-edited label a red test rather than an undetectable lie.

# Cases no rule above can reach, read by hand, each carrying the exact string it was read from. The
# evidence string is asserted to be PRESENT in the row's own stored evidence, so an override cannot
# drift away from what it claims to have read. Kept small on purpose: 3 of 122.
REVIEWED: dict = {
    # The `soup` stage exited 0 having printed NOTHING, so the checker could not confirm anything
    # and said so. Nothing failed a contract: the eval ran clean and produced no parseable metric,
    # which is what `no_metric` means. At HEAD `_stage_check_outcome` would read "cannot confirm"
    # as INCONCLUSIVE and never reach this branch at all.
    "rubertlite-dense-retrieval/n40/s593": (
        "no_metric", "No output provided — cannot confirm stage completion"),
    # Exit -15: SIGTERMed with no traceback, no watchdog sentinel, a bare progress bar for a tail,
    # and no surviving log read. The triage agent's rationale names an OOM, but it names it inside
    # a HISTORY of this node's three failures ("OOM -> faiss GPU error -> no-traceback crash"), so
    # it is a recital and not a diagnosis — the one row that made the rationale rule untenable.
    # Kept as a row precisely so a candidate is scored on admitting it does not know.
    "rubertlite-dr-unified-v7/n1/s2394": (LABEL_UNKNOWN, ""),
}

_NO_LEARNING_CONCERN = re.compile(r"no learning|not learning|stagnant|stuck|flat|constant", re.I)
_DECLARED_CONDITION = "declared_condition_violated"
_CONTRACT_STATUSES = ("needs_failed", "expect_failed", "check_failed")


def derive_label(facts: dict) -> dict:
    """`{"reason", "basis", "confidence", "evidence"}` from the row's own stored facts."""
    case_id = facts.get("case_id")
    if case_id in REVIEWED:
        reason, quote = REVIEWED[case_id]
        if reason == LABEL_UNKNOWN:
            return {"reason": LABEL_UNKNOWN, "basis": "none", "confidence": "none",
                    "evidence": "no diagnosis survives in any recorded evidence"}
        return {"reason": reason, "basis": "reviewed", "confidence": "medium", "evidence": quote}

    # 1. The stage output the engine condemned was later REUSED and scored. Nothing else in this
    #    corpus is this strong: the artefact itself answered.
    reused = facts.get("reused_stage_later_scored")
    if reused:
        return {"reason": facts.get("failed_stage_status") or LABEL_UNKNOWN,
                "basis": "reused_stage_later_scored", "confidence": "high",
                "evidence": "same stage output reused and scored %.4f (%.2fx run best)"
                            % (reused["metric"], reused["fraction_of_best"])}
    # 2. The allocator's own words, from anywhere in the evidence.
    oom = facts.get("oom_evidence") or {}
    if oom.get("markers"):
        return {"reason": "oom", "basis": "oom_marker_in_evidence", "confidence": "high",
                "evidence": "allocator marker %r in the recorded evidence" % oom["markers"][0]}
    # 3. The ENGINE's own watchdog line. Above the exit-code reads for the reason `_failure_reason`
    #    puts the flags there: both watchdogs tree-kill, so both LOOK like a kernel OOM.
    if facts.get("diverge_sentinel"):
        return {"reason": "diverged", "basis": "watchdog_sentinel", "confidence": "high",
                "evidence": DIVERGE_SENTINEL}
    if facts.get("stall_sentinel"):
        return {"reason": "stalled", "basis": "watchdog_sentinel", "confidence": "high",
                "evidence": STALL_SENTINEL}
    # 4. The engine's own clock.
    if facts.get("failed_stage_status") == "timeout":
        return {"reason": "timeout", "basis": "stage_timeout", "confidence": "high",
                "evidence": "stage_finished.status == 'timeout'"}
    # 5. A blown objective in the stage's own log, for the failures the watchdog never saw because
    #    it did not exist yet — the stage CHECKER caught them instead and had no word for it.
    if facts.get("nonfinite_loss_hits", 0) >= _NONFINITE_MIN_HITS and facts.get("log_paired"):
        return {"reason": "diverged", "basis": "nonfinite_loss_in_log", "confidence": "high",
                "evidence": "%d non-finite/exploded loss records in the paired stage log"
                            % facts["nonfinite_loss_hits"]}
    # 6. The two structural stage contracts. Both are engine comparisons over the filesystem and
    #    over the log's own numbers, not readings of the candidate's prose.
    if facts.get("failed_stage_status") == "expect_failed":
        return {"reason": "expect_failed", "basis": "artifact_contract", "confidence": "high",
                "evidence": "engine compared the declared artifact's mtime to the stage start"}
    if facts.get("failed_stage_status") == "needs_failed":
        return {"reason": "needs_failed", "basis": "artifact_contract", "confidence": "high",
                "evidence": "declared input was absent when the stage was about to start"}
    if _DECLARED_CONDITION in (facts.get("check_concern") or ""):
        return {"reason": "check_failed", "basis": "declared_condition_violated",
                "confidence": "high",
                "evidence": (facts.get("check_concern") or "")[:160]}
    # 7. The allocator's message body, for the tails truncated past the marker line.
    if oom.get("allocator_body"):
        return {"reason": "oom", "basis": "allocator_message_in_stderr", "confidence": "high",
                "evidence": "allocator message body (%s) in the recorded stderr tail"
                            % oom["allocator_body"][0]}
    # 8. A named non-OOM exception is the last line of the traceback, or an argparse refusal, or
    #    the program's own logger naming the fatal condition on its way to a non-zero exit.
    terminal = facts.get("terminal_exception")
    if terminal and terminal.get("exception") not in _NOT_A_CRASH_EXCEPTIONS:
        return {"reason": "crash", "basis": "terminal_exception", "confidence": "high",
                "evidence": "%s: %s" % (terminal["exception"], terminal["message"][:120])}
    if facts.get("argparse_error"):
        return {"reason": "crash", "basis": "terminal_exception", "confidence": "high",
                "evidence": facts["argparse_error"][:160]}
    if facts.get("logged_fatal") and facts.get("failed_stage_status") not in (
            None, "ok", "reused", *_CONTRACT_STATUSES):
        return {"reason": "crash", "basis": "logged_fatal_error", "confidence": "high",
                "evidence": facts["logged_fatal"][:160]}
    # 9. The stage checker's own reading of the log, which is a MODEL's sentence around a number the
    #    log printed. Medium, and last, so it can never outrank the log itself.
    concern = facts.get("check_concern") or ""
    if facts.get("failed_stage_status") == "check_failed" and _NONFINITE_CONCERN.search(concern):
        return {"reason": "diverged", "basis": "check_concern_nonfinite", "confidence": "medium",
                "evidence": concern[:160]}
    if facts.get("failed_stage_status") == "check_failed" and _NO_LEARNING_CONCERN.search(concern):
        return {"reason": "not_learning", "basis": "check_concern_no_learning",
                "confidence": "medium", "evidence": concern[:160]}
    return {"reason": LABEL_UNKNOWN, "basis": "none", "confidence": "none",
            "evidence": "no fact independent of the classifier's own answer"}


def extract_run(run_dir) -> list:
    """Every failure classification in one run, with its evidence and its label."""
    run_dir = Path(run_dir)
    events_path, spans_path = run_dir / "events.jsonl", run_dir / "spans.jsonl"
    if not events_path.exists():
        return []
    events = list(_iter_jsonl(events_path))
    if not events:
        return []
    if time.time() - events_path.stat().st_mtime < LIVE_RUN_GRACE_S:
        raise LiveRunRefused(run_dir.name)
    run = run_dir.name

    stage_rows: dict = {}
    for event in events:
        if event.get("type") == "stage_finished":
            data = event.get("data") or {}
            stage_rows.setdefault(data.get("node_id"), []).append((event.get("ts") or 0.0, data))
    metrics = [(event.get("data") or {}).get("metric") for event in events
               if event.get("type") == "node_evaluated"]
    run_best = max([m for m in metrics if isinstance(m, (int, float))] or [0.0])

    # The triage spans carry the reason for the runs whose `node_repaired` rows predate the field,
    # and the agent's `read_log` tool outputs are the strongest per-attempt evidence on disk.
    triage: dict = {}
    triage_by_span: dict = {}
    tool_reads: dict = {}
    if spans_path.exists():
        for span in _iter_spans(spans_path, ("operation",)):
            if span.get("name") == "triage":
                attrs = span.get("attributes") or {}
                triage[(attrs.get("node_id"), attrs.get("attempt"))] = span
                triage_by_span[span.get("span_id")] = (attrs.get("node_id"), attrs.get("attempt"))
        if triage_by_span:
            for span in _iter_spans(spans_path, ("tool",)):
                if span.get("name") != "tool":
                    continue
                key = triage_by_span.get(span.get("parent_id"))
                if key is None:
                    continue
                attrs = span.get("attributes") or {}
                if not str(attrs.get("tool") or "").startswith("read_log"):
                    continue
                tool_reads.setdefault(key, []).append(str(attrs.get("output") or ""))

    logs = _stage_logs(run_dir)

    # --- enumerate the failures ------------------------------------------------------------
    attempts: dict = {}
    failures: list = []
    for event in events:
        kind, data = event.get("type"), (event.get("data") or {})
        if kind == "node_repaired":
            node = data.get("node_id")
            attempt = data.get("attempt") or attempts.get(node, 0) + 1
            attempts[node] = attempt
            failures.append({"event": event, "node": node, "attempt": attempt, "terminal": False})
        elif kind == "node_failed":
            node = data.get("node_id")
            if data.get("reason") in NON_CLASSIFICATION_REASONS:
                continue
            mine = [f for f in failures if f["node"] == node]
            if mine:
                between = [e for e in events
                           if mine[-1]["event"]["ts"] < (e.get("ts") or 0.0) <= (event.get("ts") or 0)
                           and e.get("type") in ("stage_finished", "node_eval_started")
                           and (e.get("data") or {}).get("node_id") == node]
                if not between:
                    mine[-1]["terminal"] = True      # the same failure, now terminal
                    continue
            attempt = attempts.get(node, 0) + 1
            attempts[node] = attempt
            failures.append({"event": event, "node": node, "attempt": attempt, "terminal": True})

    declared: dict = {}
    for event in events:
        if event.get("type") == "node_created":
            idea = (event.get("data") or {}).get("idea") or {}
            declared[(event.get("data") or {}).get("node_id")] = idea.get("params") or {}

    rows = []
    for failure in failures:
        event, node, attempt = failure["event"], failure["node"], failure["attempt"]
        data = event.get("data") or {}
        seq = event.get("seq")
        error = str(data.get("error_in") or data.get("error") or "")
        span = triage.get((node, attempt))
        span_attrs = (span or {}).get("attributes") or {}
        recorded = data.get("reason")
        if recorded in VERDICT_OVERWRITES_REASON or recorded is None:
            recorded = span_attrs.get("reason") or (
                None if recorded in VERDICT_OVERWRITES_REASON else recorded)
        stage = _failed_stage_name(data, error)
        rows_for_attempt = _attempt_stage_rows(stage_rows.get(node, []), event.get("ts") or 0.0)
        stage_row = next((r for r in reversed(rows_for_attempt) if r.get("name") == stage), None)
        if stage_row is None and rows_for_attempt:
            stage_row = rows_for_attempt[-1]

        reads = [r for r in tool_reads.get((node, attempt), []) if r]
        # Prefer the reads that actually carry a diagnosis; a bench that stores the first three
        # reads stores three progress bars.
        reads.sort(key=lambda t: (not any(m in t for m in TORCH_OOM_MARKERS),
                                  not _TERMINAL_EXCEPTION.search(t), -len(t)))
        reads = [r[-STORED_TOOL_READ_CHARS:] for r in reads[:STORED_TOOL_READS]]

        log_block = None
        if stage and (node, stage) in logs:
            path, info = logs[(node, stage)]
            size = info.st_size
            paired = any(abs((ts or 0.0) - info.st_mtime) <= LOG_PAIR_TOLERANCE_S
                         for ts, row in stage_rows.get(node, [])
                         if row.get("name") == stage and row in rows_for_attempt)
            tail = _log_tail(path, size)
            log_block = {
                "paired_to_this_attempt": bool(paired),
                "path": os.path.relpath(path, run_dir.parent.parent) if paired else None,
                "bytes": size,
                # The tail is stored only when it PROVABLY belongs to this attempt. A workspace
                # holds one log per stage and every re-run truncates it, so an unpaired log is a
                # LATER attempt's output and putting it on this row would be fabrication.
                "tail": _redact(tail[-STORED_LOG_TAIL:]) if paired else None,
                "nonfinite_loss_hits": _nonfinite_hits(tail) if paired else 0,
                "oom_markers": [m for m in TORCH_OOM_MARKERS if m in tail] if paired else [],
            }

        declared_params = sorted(declared.get(node) or {})
        rationale = str(data.get("rationale") or data.get("triage_rationale") or "")
        concern = str((stage_row or {}).get("concern") or "")
        at_classification = _redact(error)
        on_demand = [_redact(r) for r in reads]
        oom = _oom_evidence([at_classification] + on_demand
                            + ([log_block["tail"]] if log_block and log_block["tail"] else []))
        facts = {
            "case_id": "%s/n%s/s%s" % (run, node, seq),
            "recorded_reason": recorded,
            "failed_stage_status": (stage_row or {}).get("status"),
            "check_concern": concern,
            "reused_stage_later_scored": _later_reused_score(
                events, node, seq or 0, stage, run_best),
            "oom_evidence": oom,
            "diverge_sentinel": DIVERGE_SENTINEL in at_classification,
            "stall_sentinel": STALL_SENTINEL in at_classification,
            "nonfinite_loss_hits": (log_block or {}).get("nonfinite_loss_hits", 0),
            "log_paired": bool((log_block or {}).get("paired_to_this_attempt")),
            "terminal_exception": _terminal_exception(
                "\n".join([at_classification] + on_demand)),
            "argparse_error": _first_line_matching(at_classification, _ARGPARSE_ERROR),
            "params_rejected": _rejected_params(at_classification),
            "logged_fatal": _first_line_matching(at_classification, _LOGGED_FATAL, last=True),
            "rationale": _redact(rationale),
        }
        # A healthy reuse-score only acquits a CONTRACT refusal. If the stage crashed outright, a
        # later reuse means the operator re-ran an older artefact, not that this attempt was fine.
        if facts["reused_stage_later_scored"] and (stage_row or {}).get("status") not in (
                "check_failed", "expect_failed", "needs_failed"):
            facts["reused_stage_later_scored"] = None
        elif (facts["reused_stage_later_scored"]
                and facts["reused_stage_later_scored"]["fraction_of_best"] < HEALTHY_FRACTION):
            facts["reused_stage_later_scored"] = None

        label = derive_label(facts)
        rows.append({
            "case_id": facts["case_id"],
            "evidence": {
                # What `_failure_reason` itself had.
                "at_classification": {
                    "stderr_tail": at_classification,
                    "stderr_tail_chars": len(error),
                    "exit_code": (stage_row or {}).get("exit_code"),
                    "failed_stage": stage,
                    "failed_stage_status": (stage_row or {}).get("status"),
                    "stages": [{k: v for k, v in row.items()
                                if k in ("name", "status", "exit_code", "seconds", "concern")}
                               for row in rows_for_attempt],
                    "stages_recorded": bool(rows_for_attempt),
                    "stages_passed": data.get("stages_passed"),
                    "eval_seconds": data.get("eval_seconds"),
                },
                # What a TOOL-USING diagnostician could fetch and a substring rule could not.
                "on_demand": {
                    "triage_log_reads": on_demand,
                    "stage_log": log_block,
                },
            },
            # THE INCUMBENT, NOT THE LABEL. Named `recorded` for the same reason it is in
            # `judge_corpus.py`: every reader who conflates the two invents an accuracy number.
            "recorded": {
                "reason": recorded,
                "reason_from": ("event" if data.get("reason") not in (None, *VERDICT_OVERWRITES_REASON)
                                else ("triage_span" if span_attrs.get("reason") else "unrecoverable")),
                "triage_action": data.get("triage_action"),
                "rationale": facts["rationale"][:1200],
                "node_terminal_reason": data.get("reason") if failure["terminal"] else None,
            },
            # A CAUSE the closed reason vocabulary has no word for, recorded beside the label and
            # never as one. See `_PARAMS_REJECTED` for the measurement and for why it stays out.
            "cause_notes": {
                "params_rejected_by_stage": facts["params_rejected"],
                "declared_params": declared_params,
            },
            "label": {**label, **{k: facts[k] for k in (
                "failed_stage_status", "check_concern", "reused_stage_later_scored",
                "oom_evidence", "diverge_sentinel", "stall_sentinel", "nonfinite_loss_hits",
                "log_paired", "terminal_exception", "argparse_error", "logged_fatal")}},
            "provenance": {
                "run": run, "node_id": node, "attempt": attempt, "seq": seq,
                "ts": event.get("ts"), "event": event.get("type"),
                "terminal": failure["terminal"], "triage_span": (span or {}).get("span_id"),
                "run_best_metric": run_best,
            },
        })
    return rows


def build_dataset(run_dirs: Iterable) -> dict:
    """`{"header": {...}, "rows": [...]}` — the whole regenerable artefact."""
    rows: list = []
    sources = []
    skipped = []
    for run_dir in run_dirs:
        try:
            run_rows = extract_run(run_dir)
        except LiveRunRefused as refused:
            skipped.append(str(refused))
            sys.stderr.write("skipping %s: its event log was appended to within the last %d s, so "
                             "the outcomes every label rests on are not final yet\n"
                             % (refused, LIVE_RUN_GRACE_S))
            continue
        if not run_rows:
            continue
        labelled = sum(1 for r in run_rows if r["label"]["reason"] != LABEL_UNKNOWN)
        last = max((r["provenance"]["seq"] or 0) for r in run_rows)
        sources.append({"run": Path(run_dir).name, "rows": len(run_rows), "labelled": labelled,
                        # The event-log position this run was read at. A rebuild over a run that
                        # has since grown produces a different header, so the drift is a visible
                        # diff instead of a silent one.
                        "last_seq": last})
        rows.extend(run_rows)
    rows.sort(key=lambda r: (r["provenance"]["run"], r["provenance"]["seq"] or 0, r["case_id"]))
    labelled = sum(1 for r in rows if r["label"]["reason"] != LABEL_UNKNOWN)
    return {"header": {
        "schema": DATASET_SCHEMA,
        "judge": JUDGE_FAILURE_TRIAGE,
        "rows": len(rows),
        "labelled": labelled,
        "unlabelled": len(rows) - labelled,
        "sources": sources,
        "skipped_live_runs": skipped,
        "labels": list(LABELS),
        "label_bases": list(LABEL_BASES),
        # THE FROZEN VOCABULARY, IN THE ARTEFACT. Whoever reads this file in six months gets the
        # rule that decided its labels and the partition its historical scores were reported under,
        # without having to find the commit that was HEAD when it was cut. Production's copies of
        # both have already moved once (the 2026-08-20 ownership split) and will move again.
        "frozen_vocabulary": {
            "as_of": "2026-08-20",
            "torch_oom_markers": list(TORCH_OOM_MARKERS),
            "authenticated_reasons": sorted(_HISTORICAL_AUTHENTICATED_REASONS),
            "note": "a SNAPSHOT of production as it stood when this corpus was cut, not a mirror. "
                    "The historical arms read these; the live arm reads production. See "
                    "judgebench/triage_score.py::_frozen_failure_reason_v1.",
        },
        "high_confidence_bases": sorted(HIGH_CONFIDENCE_BASES),
        "healthy_fraction": HEALTHY_FRACTION,
        "redaction": "core.redact.redact_output_tail(entropy=True)",
        "limits": CORPUS_LIMITS,
    }, "rows": rows}


def write_dataset(dataset: dict, path) -> Path:
    """Header line first, then one row per line, gzipped. Sorted + `sort_keys` so a rebuild from the
    same runs is BYTE-IDENTICAL and the committed file's diff is only ever real change."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(
        [json.dumps(dataset["header"], sort_keys=True, ensure_ascii=False)]
        + [json.dumps(r, sort_keys=True, ensure_ascii=False) for r in dataset["rows"]]) + "\n"
    with gzip.GzipFile(filename="", mode="wb", fileobj=path.open("wb"), compresslevel=9,
                       mtime=0) as handle:
        handle.write(payload.encode("utf-8"))
    return path


def read_dataset(path) -> dict:
    """The inverse of `write_dataset`. Raises on an empty or truncated file rather than returning a
    dataset with no rows, which would score as a vacuous pass."""
    with gzip.open(Path(path), "rt", encoding="utf-8") as handle:
        lines = [line for line in handle.read().splitlines() if line.strip()]
    if not lines:
        raise ValueError("triage-bench dataset is empty: %s" % path)
    header = json.loads(lines[0])
    rows = [json.loads(line) for line in lines[1:]]
    if header.get("rows") != len(rows):
        raise ValueError("triage-bench dataset header claims %s rows, file has %s"
                         % (header.get("rows"), len(rows)))
    return {"header": header, "rows": rows}


def rederive_label(row: dict) -> dict:
    """Recompute a row's label from the facts stored ON the row — no `runs/` needed.

    This is what makes the committed file auditable: `tests/test_triage_bench.py` calls it on every
    row and compares, so a label edited by hand in the artefact goes red on a machine that has never
    seen the operator's disk.
    """
    facts = {"case_id": row["case_id"], "recorded_reason": (row.get("recorded") or {}).get("reason"),
             "rationale": (row.get("recorded") or {}).get("rationale")}
    facts.update({k: v for k, v in (row.get("label") or {}).items()
                  if k not in ("reason", "basis", "confidence", "evidence")})
    return derive_label(facts)


DEFAULT_DATASET = (Path(__file__).resolve().parents[2]
                   / "tests" / "data" / "judge_bench" / "failure_triage.v1.jsonl.gz")
