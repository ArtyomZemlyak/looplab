"""Let a role LOOK at a running experiment's log instead of being handed a slice of it.

WHY THIS EXISTS (measured, `runs/rubertlite-dr-unified-v7`, 2026-08-14)
-----------------------------------------------------------------------
The training monitor decides whether to KILL a node. What it was handed was
`train_monitor.training_log_digest` — 40 records bounded to 4000 chars — and a tqdm bar line on the
real logs is ~330 characters, so the 40 kept records became about TEN. The judge saw ten loss values,
roughly **30 seconds of a five-hour run**, and at `train_monitor_interval_s=600` consecutive tails do
not even overlap. It answered correctly for what it was shown and called a node that had gone
27.69 -> 22.92 "pinned with no learning trend".

`LossTrajectoryTracker` (engine/train_monitor.py) was the right FIRST move and it stays exactly where
it is: a kill is RECORD-adjacent, so the veto over it must be deterministic and engine-side. This
module is the other half — it gives the role the ability to LOOK, instead of the engine choosing what
it is shown. A role that can ask "what has the loss done over the whole run at ten-minute
granularity" is answering a question the tail cannot state, and it is asking it about the same bytes.

WHAT IS AND IS NOT WIDENED (docs/36-agent-driven-decisions-2026-08-13.md)
-------------------------------------------------------------------------
Everything reachable through these tools is text **the candidate's own training script wrote**. So
this is `engine/metric_salvage.py`'s rule one package over: what a role reads here may inform what
happens NEXT and must never become the FACT a record rests on. Concretely — the tools widen the
judge's CONTEXT; they do not touch `should_monitor_kill`'s conjuncts, the trajectory veto stays
deterministic, and nothing here reaches a metric, a champion or a selection record. Doc 36's second
corollary applied literally: a wider action space must not widen the trusted set.

THE BOUNDARY — four universal rules, no allow-list of paths
------------------------------------------------------------
`tools/dev_probe.py` is this week's precedent: a tool is bounded by rules that a new case inherits,
never by a table somebody has to remember to extend. The four here:

1. **It names a LOG, never a path.** The `log` argument is matched against a source map the CALLER
   supplies; a name that is not in it is refused by listing what is. No path is ever constructed from
   model input, so there is no `..` to reject, no absolute path to confine, no symlink to resolve —
   the traversal question does not arise one rung earlier than where `read_fence` would answer it.
2. **The source map is the engine's own resolved stage plan.** `engine/train_monitor.py::eval_log_plan`
   already derives, from the resolved pipeline, exactly which `<stage>.log` this eval writes in its
   own workdir; `monitor_log_sources` turns that into the map. So the reach is a SUBSET of the node's
   own output — which is the one region of the filesystem that provably holds nothing but what this
   node produced. `runtime/read_allowlist.py` grants the workdir and run dir readwrite and
   `runtime/read_fence.py::fence_inputs` never fences them, so this tool asks for nothing the eval
   itself was not already granted, and it can reach the operator's editable source tree only by
   naming a file it has no way to name.
3. **Every read is bounded, and a bounded answer SAYS SO with the call that continues it.**
   Seek-and-read only; a per-call ceiling that is a PARAMETER with a maximum, never a silent
   truncation. `core/trace_files.py::iter_bounded_trace_jsonl_lines` is the discipline being reused:
   its over-ceiling stop is a specified prefix rule rather than a bug, and so is every stop here.
4. **It reads, and only reads.** No write, no execution, no second file, no network. There is nothing
   for engine invariant #3 to gate, which is why — like `run_probe` — a call is an ordinary `tool`
   span and not a domain event.

THE TWO SURFACES
----------------
* ``read_log`` — the log as a FILE: tail, head, a line range, or a search with context. Records are
  split on ``\\n`` **and** ``\\r``, because a tqdm bar writes its whole multi-hour life into ONE
  newline-delimited line and a line-oriented reader is useless on it. A search is not a window over
  the file, it is a SWEEP of it — see the section on the search reach below.
* ``metric_series`` — the numeric series the log emits (`loss`, `grad_norm`, `learning_rate`,
  `epoch`, the step counter, `it/s`), answered over a TIME OR STEP WINDOW AT A GRANULARITY: the last
  10 seconds, the whole run bucketed hourly, first-vs-last, min/max. **Downsampling is by
  AGGREGATION and never by dropping** — median, spread, min, max and count per bucket — because a
  sampling artifact is the exact failure this module exists to end.

COST, measured on the real logs (2026-08-14, page-cache warm, mean of 5)
------------------------------------------------------------------------
| call | bytes touched | v7 node 1 (5.9 MB) | v7 node 0 (15.7 MB) |
|---|---|---|---|
| `read_log` tail/head (default) | 256 KiB | 2.4 ms | 2.6 ms |
| `read_log` search (default window) | 256 KiB | 3.4 ms | 3.3 ms |
| `metric_series` default, and `last_s=10` | 256 KiB | 14 ms | 13 ms |
| `metric_series` whole run, hourly | the whole file | 703 ms | 1,269 ms |

The DEFAULT is a tail, so the common call is the cheap one, and it costs about what one `train_monitor`
tick already pays to build its digest. A whole-run scan is what the caller explicitly asked for
(`whole_run=true`, or a window wide enough to need it) and its receipt states the bytes it cost.
Scan time is dominated by the record split + regex over the tqdm fill, not by I/O — which is why
`read_log` is ~5x cheaper than `metric_series` over the same bytes.

(That table's `read_log search` row is superseded: a search is no longer a window read. See the
search section and its own table below.)

WHAT THAT TABLE DID NOT SAY, and what a whole-run scan really costs (2026-08-15)
--------------------------------------------------------------------------------
The paragraph above used to end "the ceiling (`_MAX_SCAN_BYTES`) is above the largest log in `runs/`,
so 'the whole run' is answerable rather than quietly clipped". `_MAX_SCAN_BYTES` was above it; the
answer was clipped anyway, because `_read_window` clamped EVERY read by `_MAX_READ_BYTES` — the
READER's ceiling, a different bound answering a different question. So the escalation ladder walked
up to a ceiling it could not spend, re-read the same 33.5 MB twice, and no byte below
`size - _MAX_READ_BYTES` was reachable at any parameter. Measured on the corpus:
`rubertlite-dr-unified-v2` node 3 (88 MB) — the head **61.8 %** unreadable, so `whole_run=true`
answered with a series that began 4 hours into a 4h17m run and called it the whole run;
`rubert-dr-0807` node 1 (45 MB) — the first 11.4 MB. That is the exact failure this module exists to
end, one layer down from the digest that produced it: a series which silently starts most of the way
in shows the wrong trend, and the watchdog's question is "is this run learning?".

The two bounds are now what their names say — `_read_window` takes the CALLER's ceiling — and the
real numbers, measured on the 88 MB log (`runs/rubertlite-dr-unified-v2/nodes/node_3/train.log`) with
`runs/` on its geesefs/S3 mount:

| | |
|---|---|
| geesefs sequential read, cold | 101 MB/s (88 MB in 0.87 s) |
| geesefs sequential read, warm | ~300 MB/s (88 MB in 0.25-0.28 s) |
| the record split + regex, per MB | **51 ms** (88 MB in 4.5 s) |
| whole-run hourly scan of that log, AFTER this fix | 1 read, ~5.4 s cold, and it covers 100 % |
| the same call BEFORE this fix | 6 reads, 88 MB read and parsed, 4.7 s, and it covered 38 % |

So the head was not expensive — it was free. The broken ladder already paid for the whole file; it
just spent the bytes re-reading a tail. **I/O is 5-16 % of a scan**; what a scan costs is the regex
over the tqdm fill, and that is why the ceiling is a bound on BYTES PARSED and not on bytes fetched.

WHY `_MAX_SCAN_BYTES` STAYS AT 128 MiB, and what happens above it
------------------------------------------------------------------
It is not "the largest log plus room" — that number moves every time a run gets longer, and the
88 MB log is 1.6x the previous largest. It is a bound on the JUDGE'S REQUEST PATH: this judge fires
on a timer up to `_MAX_MONITOR_LLM_CALLS` times per node with `_MONITOR_LOOK_TURNS=6` tool turns
each, so the number that matters is the worst case one call can cost. At the measured 51 ms/MB,
128 MiB is ~6.9 s of CPU — ~1 % of the `train_monitor_interval_s=600` cadence it fires on, and it
covers every log in `runs/` (largest 88 MB) with 1.5x of headroom.

Above it the head IS unreachable, and the receipt SAYS SO in the words that matter to the reader —
"this series does not start at the run's start; N bytes (P %) are below it, so the first bucket is a
mid-run value" — rather than the old text, which told a caller who had just passed `whole_run=true`
to pass `whole_run=true`. An answer that announces its own truncation is a different object from one
that silently misleads a judge, and that is the property to keep if this ceiling ever moves again.

`_MAX_SEARCH_BYTES` starts at the same number for the same derivation, and is a SEPARATE name because
it bounds a different call: a sweep is 9.8 ms/MB, so 128 MiB is ~1.3 s there rather than ~6.9 s. A
search above it does better than announce its truncation — it names the byte to resume at.

A SEARCH IS A SWEEP OF THE LOG, NOT A WINDOW OF IT (2026-08-15)
----------------------------------------------------------------
The same defect as the one above, one surface over, and it survived that fix because it was hiding
behind a different bound. `mode="search"` was `_read_window(where="tail")` plus a regex over the
records that came back — a search of the LAST `max_bytes`, ceilinged by `_MAX_READ_BYTES`. So on a
log larger than that ceiling the head was unsearchable at every parameter. Re-derived on the corpus
before it was touched:

| log | size | searchable region | head unsearchable |
|---|---|---|---|
| `rubertlite-dr-unified-v2` node 3 | 87,949,008 | bytes 54,394,576-end | **61.8 %** |
| `rubert-dr-0807` node 1 | 44,976,734 | bytes 11,422,302-end | 25.4 % |
| `rubertlite-dr-unified-v7` node 0 | 31,386,860 | all of it | 0 % |

`mode="head"` was not the rescue the receipt said it was: head is not a search, it returns the first
N records — 33.5 MB is ~120,000 of them at 60 per call — and it reads UP from the floor by the same
32 MiB, so on the 88 MB log there was a **20.8 MB band (23.7 %) no mode reached at all**.

What that cost is one number on that log. `CUDA-enabled jaxlib` is printed once per process start, so
a count of it is a count of this node's RESTARTS; it is at bytes 109 / 18.8 M / 48.7 M / 78.5 M. The
windowed search reported **1 match** — "this node started once" — to a judge asking whether the run is
healthy. On `rubert-dr-0807` node 1 the equivalent count was 2 of 3. And the DEFAULT search window is
256 KiB, which on `rubertlite-dr-unified-v7` node 0 answers "no record matches 'Traceback' in the
262,144 bytes read" about a log whose byte 0 IS a traceback.

**The answer was not silent about this** — it printed the byte range and "54,394,576 earlier bytes not
read" — and that is exactly why it took a second look to see. What it got wrong was the REMEDY: it
offered `raise max_bytes` to a caller already at the cap and `mode="head"` for a mode that cannot
search. A remedy that cannot be spent reads, to anything that believes its receipts, as "you have seen
it all" — the identical failure `_scan_reach` was rewritten for on the metric side the same week.

So a search now SWEEPS: `_search_scan` streams forward from the attempt floor (or `from_byte`),
matching record by record, stopping at a chunk boundary when it has all the matches it can show or
when it has spent `_MAX_SEARCH_BYTES`. Three things make that affordable and honest:

* the answer's size is bounded by the ANSWER (`_MAX_HITS` groups under `_CAP`), never by how far it
  looked, so reaching further costs CPU and not context;
* it STOPS once it has its matches, so the common search is still about one chunk; and
* every stop names a byte — `from_byte=<n>` — that the sweep really examined up to, so paging past
  the ceiling is a call the caller can actually make. `tests/test_log_tools.py` spends that remedy
  rather than asserting its wording.

The ceiling is deliberately `_MAX_SCAN_BYTES`'s number and NOT `_MAX_READ_BYTES` — a sweep and a
whole-run scan are the same kind of thing (a bound on bytes PARSED on the judge's request path) and a
window bound is not. Raising the window bound instead would have been the same defect further out.

COST of the sweep, measured 2026-08-15 (page-cache warm, mean of 5, `runs/` on its geesefs/S3 mount)
----------------------------------------------------------------------------------------------------
| call | v7 node 1 (15.0 MB) | 0807 node 1 (45.0 MB) | v2 node 3 (87.9 MB) |
|---|---|---|---|
| `read_log` tail (default) — unchanged | 3.5 ms | 3.1 ms | 4.0 ms |
| `read_log` search, match found early | 15.1 ms | 13.3 ms | 11.7 ms |
| `read_log` search, NO match (full sweep) | 136 ms | 521 ms | 860 ms |
| ...of which is fetching the bytes | 35 ms | 82 ms | 239 ms |
| `metric_series` whole run, hourly | 775 ms | 3,090 ms | 4,746 ms |

A sweep is **9.8 ms/MB** against the metric scan's ~54 ms/MB over the same bytes (one regex per record
instead of four), and I/O is 16-28 % of it. The worst case one search can cost is therefore
`_MAX_SEARCH_BYTES` x 9.8 ms/MB = **~1.3 s**, i.e. ~0.2 % of the `train_monitor_interval_s=600`
cadence this judge fires on and a fifth of what the whole-run scan at the same ceiling already costs.
A search that FINDS something is ~13 ms whatever the file's size, because it stops; the sweep price is
paid by the search that has to prove a NEGATIVE, and proving that negative is the entire point.

THE CLOCK, and why it is derived rather than assumed
-----------------------------------------------------
"The last 10 seconds" needs a time per sample, and the real logs do not carry one per line. Measured
on v7 node 1: 22 absolute timestamps, ALL inside the first 34 KB, and then 3.5 hours of pure tqdm.
So the clock is the best available LOWER BOUND from two independent sources, combined and monotone by
construction (`_Clock`):

* the log's own absolute timestamps (`YYYY-MM-DD HH:MM:SS`), as a delta from the first one seen; and
* the tqdm bars' elapsed field, tracked PER BAR TOTAL — a `10/100` bar and a `2377/17650` bar are
  different bars, and one restarting means the previous one finished while the other is a nested
  concurrent bar that finished nothing. Summing them blindly reported v7 node 1 as 12.5 hours (real:
  3.5); ignoring restarts reported `rubertlite-dense-retrieval` node 36 as 108 seconds (real: ~1.2 h).
  Per-lane accumulation gets both right — 3.56 h and 1.15 h, and the derived end-of-run wall clock
  lands within 8 minutes of the file's own mtime on a run that is still appending.

Neither source is authoritative about the other, so the clock is their MAXIMUM, and the receipt names
which one is carrying it. A window is expressed against that clock, and the header states its origin
so a reader can never mistake it for a wall clock the log did not contain.

WHY THE NUMBER PATTERNS ARE NOT SHARED WITH `train_monitor`
------------------------------------------------------------
`train_monitor._LOSS_POINT_RE` reads exactly `loss` and feeds a DETERMINISTIC veto over a kill.
`_value_re` here reads any key a role asks for and feeds a role's context. They are two trust tiers
(doc 36 again), and a shared regex would mean a widened reader here silently moved what the veto
measures. They are deliberately two, and this paragraph is why.
"""
from __future__ import annotations

import datetime
import os
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

from looplab.tools._base import RESULT_CAP, clip, fit_rows, fn_spec

# ---------------------------------------------------------------------------------------- the bounds
#
# Each is a PARAMETER with a ceiling, never a fixed truncation, and each over-ceiling stop is stated
# in the answer alongside the exact call that continues past it (rule 3).

# What one `read_log` reads by default, and the most it may ever read. The default is twice the
# training monitor's own 128 KiB tail so a first look already sees more than the digest ever did.
#
# The ceiling bounds ONE ANSWER's read, and that is the whole of its job: a `read_log` answer is at
# most `_MAX_LINES` records under `_CAP`, so bytes past what fills that budget buy the reader nothing
# and cost the judge parse time. It is anchored at the END the mode asks for — `head`/`range` read up
# from the floor, `tail` down from the newest — so no WINDOW mode is reaching for the far end of a
# large file through this number. It stopped applying to `search` on 2026-08-15: a search is not a
# window, and this number is why its reach used to end 54 MB into an 88 MB log (see the docstring).
# (Its original comment said the ceiling "covers the largest single
# stage log in `runs/` (14.7 MB) with room to spare"; the corpus now holds an 88 MB and a 45 MB log,
# which is a fact about the corpus and not a reason to raise a per-answer bound. What it DID break
# was the scan below, because that clamp was applied to both — see the module docstring.)
_DEFAULT_READ_BYTES = 262_144
_MAX_READ_BYTES = 33_554_432

# The same pair for `metric_series`, and DELIBERATELY A DIFFERENT NUMBER — a metric query over "the
# whole run" must be able to REACH the whole run, which is the point of the tool, so this bounds a
# whole-file SCAN and not one answer's read. `_DEFAULT_SCAN_BYTES` is only the STARTING window:
# `_scan_for_window` escalates a tail read until the requested window is covered or the ceiling is
# hit (and an UNBOUNDED window skips the ladder entirely — it is one read of the ceiling).
#
# 128 MiB is a bound on the judge's REQUEST PATH, derived rather than picked: at the measured
# 51 ms/MB of record-split + regex it is ~6.9 s of CPU for the worst case one call can cost, against
# a `train_monitor_interval_s=600` cadence, and it covers the largest log in `runs/` (88 MB) 1.5x
# over. Above it the head is genuinely unreachable and `_scan_reach` says so in the answer.
_DEFAULT_SCAN_BYTES = 262_144
_MAX_SCAN_BYTES = 134_217_728

# Records per `read_log` answer. The result cap usually binds first; this stops a search over a log
# of one-character lines from building a 100k-element list before the cap has anything to say.
_DEFAULT_LINES = 60
_MAX_LINES = 2_000

# Search bounds. `context` is records either side of a hit.
_MAX_HITS = 100
_MAX_CONTEXT = 20

# A search STREAMS, so this is its working-set unit and NOT a bound on its reach: `_search_scan` reads
# the log a chunk at a time from the floor forward and stops at a chunk boundary, so what a search
# holds in memory is one chunk plus at most `_MAX_HITS` hit groups — independent of the file's size.
#
# The chunk is also the granularity of "how much did an early stop overshoot": a sweep that has found
# its last showable match finishes the chunk it is in rather than stopping mid-buffer, because the
# byte it names in `from_byte=` has to be a byte it really examined up to. 1 MiB is ~12 ms of parse at
# the measured rate — small enough that a search for a common word costs about one chunk, large enough
# that a full sweep of the 88 MB log is 84 reads and not 21,000.
_SEARCH_CHUNK = 1_048_576

# The most bytes ONE search may parse. Deliberately the SCAN ceiling and not the reader's: a search is
# a whole-file sweep, exactly like a `metric_series` scan, and `_MAX_READ_BYTES` bounds a different
# thing (one answer's window, where bytes past what fills the answer buy the reader nothing). Binding
# a sweep with the window bound is what made the head of a big log unsearchable — see the module
# docstring. Above this ceiling a search STOPS and names the byte to resume at; it does not clip.
_MAX_SEARCH_BYTES = _MAX_SCAN_BYTES

# Buckets per `metric_series` answer. Above this the caller is asked for a coarser granularity rather
# than being handed a silently thinned series — dropping buckets is exactly the sampling artifact this
# module exists to end, so the refusal names the bucket size that would fit.
_MAX_BUCKETS = 200

# Per-record character cap in `read_log`. A tqdm bar record is ~330 chars of fill; `_squeeze` takes it
# to ~60 without losing a single number, and this bounds whatever squeezing did not.
_MAX_RECORD_CHARS = 400

# Our own budget under the agent loop's per-result cap, leaving room for the header + receipt that
# say what was and was not covered (`_base.clip`'s `reserve` rationale, one layer up).
_CAP = RESULT_CAP - 200

_MODES = ("tail", "head", "range", "search")

# The metric keys a caller may name without spelling a regex. `step`/`total`/`it_s` come from the
# progress bar rather than from a `key: value` pair, so they are read by `_BAR` instead of `_value_re`.
_BAR_METRICS = ("step", "progress", "it_s")


# ------------------------------------------------------------------------------------ the source map
@dataclass(frozen=True)
class LogSource:
    """ONE log a role may read, as the CALLER declares it. Rule 1's whole surface.

    `name` is what the model types and the only thing it can type; `path` is never derived from model
    input. `floor` is the byte offset below which the file's bytes belong to a PREVIOUS eval attempt
    (`train_monitor.attempt_byte_floor`) — bytes below it are never returned and never parsed, so a
    resumed node cannot read its own predecessor's curve as its own.
    """

    name: str
    path: Path
    role: str = ""
    floor: int = 0


SourceSpec = Union[Sequence[LogSource], Callable[[], Sequence[LogSource]]]


# ------------------------------------------------------------------------------------- record shapes
# A record is delimited by `\n` OR `\r`. Both, because a tqdm/rich progress bar overwrites ONE line in
# place with carriage returns and never emits a newline until it finishes — on `runs/rubertlite-dr-
# unified-v7` node 1 the entire 3.5-hour training run is a SINGLE newline-delimited line. A
# line-oriented reader answers "the last 60 lines" with one line, and it is the wrong line.
_RECORD_SPLIT = re.compile(r"[\r\n]")

# A run of 8+ identical characters collapses to that character + an ellipsis. This is what makes a
# progress-bar record readable without dropping any of it: `45%|███…███▍     …     | 8004/17650
# [3:24:37<4:01:19, 1.50s/it]` keeps every number, every percentage and the rate, and loses only the
# bar's fill. Squeezing INSIDE a record is the same principle as bucketing a series: reduce, never
# drop. `raw=true` turns it off.
_FILL_RUN = re.compile(r"(.)\1{7,}")

# An absolute timestamp anywhere in a record (loguru's default, `logging`'s default, ISO-8601).
_ABS_TS = re.compile(r"(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)")

# A tqdm/Keras/rich progress render: `8004/17650 [3:24:37<4:01:19,  1.50s/it]`. The counter and the
# elapsed field must appear TOGETHER — a bare `a/b` is a ratio, a date or a version somewhere else on
# the line, and `train_monitor._PROGRESS_RE` learned that the hard way. Groups: done, total, h, m, s.
_BAR = re.compile(r"(?<![\d./])(\d{1,9})/(\d{1,9})\s*\[(?:(\d+):)?(\d{1,2}):(\d\d)<")

# The rate a progress bar reports, in either spelling tqdm uses.
_RATE = re.compile(r"([0-9.]+)(s/it|it/s)")

# A numeric literal, including the non-finite spellings. Non-finite values are KEPT here (unlike
# `train_monitor.parse_loss_points`, which counts them): this surface REPORTS what the log says, and a
# role asking "what is grad_norm doing" must be able to see `nan` where it is. They are excluded from
# every aggregate instead, and counted in the bucket row.
_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|[-+]?nan|[-+]?inf(?:inity)?"


def _value_re(key: str) -> re.Pattern:
    """`key: value` / `key=value` / `'key': value`, case-insensitively — the three spellings every
    trainer in `runs/` actually emits (HF Trainer prints a dict, Lightning a `k=v` postfix, a
    hand-rolled loop a `k: v` line). See the module docstring for why this is deliberately NOT
    `train_monitor._LOSS_POINT_RE`."""
    return re.compile(r"(?<![A-Za-z0-9_])['\"]?" + re.escape(key) + r"['\"]?\s*[:=]\s*(" + _NUMBER + ")",
                      re.IGNORECASE)


def _squeeze(text: str) -> str:
    """Collapse fill runs so a progress-bar record is readable. Reduce, never drop — see `_FILL_RUN`."""
    return _FILL_RUN.sub(lambda m: m.group(1) + "…", text)


def _records(text: str) -> list:
    """Split on `\\n` and `\\r`, dropping blanks. See `_RECORD_SPLIT` for why both."""
    return [r for r in (rec.rstrip() for rec in _RECORD_SPLIT.split(text or "")) if r.strip()]


def _int_arg(args: dict, key: str, default: int) -> int:
    """One integer argument, where a legitimate ZERO is not silently promoted to the default.
    `int(args.get("context") or 1)` reads naturally and makes `context=0` — "just the hits, no
    neighbours" — impossible to ask for."""
    value = (args or {}).get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _fmt_num(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.6g}"


def _fmt_clock(seconds: Optional[float]) -> str:
    """`h:mm:ss` on the series clock. Not a wall clock and never rendered as one — the header says
    what the origin is."""
    if seconds is None:
        return "?"
    total = int(max(0.0, seconds))
    return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _median(values) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def _quantile(ordered, q: float) -> float:
    """Nearest-rank quantile over an already-sorted list. Deliberately not interpolated: a bucket's
    spread is a description of the samples that are there, and an interpolated value is a number no
    step ever logged."""
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


# ------------------------------------------------------------------------------------------ the clock
class _Clock:
    """The per-record time, as the best available LOWER BOUND from the log's two independent clocks.

    Monotone by construction: both terms are non-decreasing and the answer is their maximum, so a
    window query can never see time run backwards. See the module docstring for the two measured
    failures the per-lane bar accounting fixes.
    """

    def __init__(self) -> None:
        self.abs_first: Optional[float] = None   # wall clock of the first absolute timestamp seen
        self.abs_delta = 0.0                     # seconds since it
        self.bar_origin: Optional[float] = None  # `abs_delta` when the first bar render appeared
        self.lanes: dict = {}                    # bar total -> (accumulated seconds, last elapsed)
        self.bar_peak = 0.0
        self.step: Optional[int] = None
        self.total: Optional[int] = None
        self.rate: Optional[float] = None        # iterations per second

    def observe(self, record: str) -> None:
        match = _ABS_TS.search(record)
        if match:
            try:
                stamp = datetime.datetime(*(int(g) for g in match.groups())).timestamp()
            except (ValueError, OverflowError, OSError):
                stamp = None
            if stamp is not None:
                if self.abs_first is None:
                    self.abs_first = stamp
                self.abs_delta = max(self.abs_delta, stamp - self.abs_first)
        for bar in _BAR.finditer(record):
            try:
                done, total = int(bar.group(1)), int(bar.group(2))
                elapsed = float(int(bar.group(3) or 0) * 3600 + int(bar.group(4)) * 60
                                + int(bar.group(5)))
            except (TypeError, ValueError, OverflowError):
                continue
            if total <= 0 or done > total:
                continue
            accum, last = self.lanes.get(total, (0.0, 0.0))
            if elapsed < last:
                # THIS lane's bar restarted, so the previous one finished and its time is real
                # elapsed. A different lane's bar is a nested concurrent bar and adds nothing —
                # which is the whole reason lanes are keyed by total (see the module docstring).
                accum += last
            self.lanes[total] = (accum, elapsed)
            self.bar_peak = max(self.bar_peak, accum + elapsed)
            if self.bar_origin is None:
                self.bar_origin = self.abs_delta
            self.step, self.total = done, total
        rate = _RATE.search(record)
        if rate:
            try:
                value = float(rate.group(1))
                self.rate = (1.0 / value) if (rate.group(2) == "s/it" and value > 0) else (
                    value if rate.group(2) == "it/s" else None)
            except (TypeError, ValueError, ZeroDivisionError, OverflowError):
                pass

    @property
    def t(self) -> float:
        return max(self.abs_delta, (self.bar_origin or 0.0) + self.bar_peak)

    def source(self) -> str:
        """Which clock is actually carrying the answer — part of every receipt, because a reader who
        does not know this cannot tell a real 3-hour span from a bar that never restarted."""
        if self.lanes and self.abs_first is not None:
            return "log timestamps + progress-bar elapsed"
        if self.lanes:
            return "progress-bar elapsed (the log carries no absolute timestamp here)"
        if self.abs_first is not None:
            return "log timestamps"
        return "none (no timestamp and no progress bar in the scanned region)"

    def wall(self, t: float) -> str:
        """`t` rendered as an absolute wall clock, or '' when the log gave us no anchor for one."""
        if self.abs_first is None:
            return ""
        try:
            return datetime.datetime.fromtimestamp(self.abs_first + t).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OverflowError, OSError):
            return ""


@dataclass(frozen=True)
class _Sample:
    t: float
    value: float
    step: Optional[int]
    total: Optional[int]
    finite: bool


def scan_series(text: str, key: str) -> tuple:
    """Every `key` reading in `text`, in order, each carrying the record's clock. Pure — no I/O — so
    the whole time model has a truth table instead of being reachable only through a live eval.

    Returns `(samples, clock)`. A non-finite reading is kept with `finite=False`: this surface reports
    what the log SAYS, and `grad_norm: nan` is the single most useful thing a role can be shown about
    a blown-up run. Aggregates exclude them and every bucket row counts them.
    """
    clock = _Clock()
    samples: list = []
    if key in _BAR_METRICS:
        for record in _RECORD_SPLIT.split(text or ""):
            clock.observe(record)
            if not _BAR.search(record):
                continue
            value = {"step": clock.step, "progress": clock.step, "it_s": clock.rate}.get(key)
            if value is None:
                continue
            samples.append(_Sample(t=clock.t, value=float(value), step=clock.step,
                                   total=clock.total, finite=True))
        return samples, clock
    pattern = _value_re(key)
    for record in _RECORD_SPLIT.split(text or ""):
        clock.observe(record)
        for match in pattern.finditer(record):
            try:
                value = float(match.group(1))
            except (TypeError, ValueError, OverflowError):
                continue
            samples.append(_Sample(t=clock.t, value=value, step=clock.step, total=clock.total,
                                   finite=value == value and abs(value) != float("inf")))
    return samples, clock


def bucket_series(samples, *, start: float, end: float, width: float) -> list:
    """Aggregate `samples` into fixed-width buckets over `[start, end]`. Pure.

    **The rule this module exists for**: a bucket REDUCES its samples (count, median, spread, min,
    max, first, last) and never drops any of them. A thinned series is what produced a confident
    "pinned, no learning trend" about a curve that had fallen 4.8, and no amount of extra buckets
    fixes a reduction that discards.

    So this function is TOTAL over `[start, end]` — every sample in the window lands in exactly one
    bucket, and `sum(row["n"]) == len(samples in window)` is a driven property
    (`tests/test_log_tools.py`). It deliberately does NOT clamp to `_MAX_BUCKETS`: a clamp here would
    cover the window's HEAD and silently drop its tail, which is the identical defect one layer down.
    The cap lives at the CALL SITE, where it is a stated refusal naming a width that fits.
    """
    if width <= 0:
        width = max(1e-9, end - start)
    rows: list = []
    count = int(max(1, ((end - start) / width) + 1))
    for index in range(count):
        lo = start + index * width
        hi = lo + width
        # The last bucket is closed on the right so the newest sample is never orphaned by float error.
        inside = [s for s in samples
                  if s.t >= lo and (s.t < hi or (index == count - 1 and s.t <= hi + 1e-6))]
        if not inside:
            continue
        finite = sorted(s.value for s in inside if s.finite)
        rows.append({
            "lo": lo, "hi": hi, "n": len(inside), "nonfinite": sum(1 for s in inside if not s.finite),
            "median": _median(finite) if finite else None,
            "spread": (_quantile(finite, 0.75) - _quantile(finite, 0.25)) if finite else None,
            "min": finite[0] if finite else None, "max": finite[-1] if finite else None,
            "first": next((s.value for s in inside if s.finite), None),
            "last": next((s.value for s in reversed(inside) if s.finite), None),
            "step": next((s.step for s in reversed(inside) if s.step is not None), None),
            "total": next((s.total for s in reversed(inside) if s.total is not None), None),
        })
    return rows


# ------------------------------------------------------------------------------------- the file reads
def _read_window(source: LogSource, *, want: int, where: str, start: Optional[int],
                 ceiling: int) -> tuple:
    """Read a bounded byte window of `source`. Returns `(text, lo, hi, size)`.

    Seek-and-read only, and never below `source.floor` — the previous attempt's bytes are not this
    node's evidence (rule 1's `floor`). `where` is `"tail"`, `"head"` or `"at"` (from `start`).

    `ceiling` is REQUIRED and belongs to the CALLER, because the two callers bound different things:
    `read_log` bounds one answer's read (`_MAX_READ_BYTES`) and `metric_series` bounds a whole-run
    SCAN (`_MAX_SCAN_BYTES`). This function clamped both by the reader's ceiling until 2026-08-15,
    which silently made every byte below `size - _MAX_READ_BYTES` unreachable at any parameter — the
    head 61.8 % of the corpus's largest log, answered as if it were the whole run. A default here
    would restore exactly that: the bound a read is subject to is never this function's to assume.
    """
    path = Path(source.path)
    size = os.path.getsize(path)
    floor = max(0, min(int(source.floor or 0), size))
    want = max(1, min(int(want), max(1, int(ceiling))))
    if where == "head":
        lo = floor
    elif where == "at":
        lo = max(floor, min(int(start or 0), size))
    else:
        lo = max(floor, size - want)
    with open(path, "rb") as fh:
        fh.seek(lo)
        raw = fh.read(want)
    return raw.decode("utf-8", "replace"), lo, lo + len(raw), size


def _search_scan(source: LogSource, rx: re.Pattern, *, start: Optional[int], ceiling: int,
                 context: int, max_hits: int) -> tuple:
    """Sweep the log FORWARD from `start` (default: the attempt floor), collecting the first
    `max_hits` matching records with `context` records either side.

    Returns `(rows, hits, records, lo, hi, size, torn, stopped)`, where `rows` is
    `[(record_number, is_hit, record)]` in file order, `hits` counts EVERY match in `[lo, hi)` — not
    just the shown ones — and `stopped` is `""` (reached the end), `"hits"` (has all it can show) or
    `"ceiling"` (spent its byte budget).

    WHY THIS IS A SWEEP AND NOT A WINDOW. Search used to be `_read_window(where="tail")` + a regex over
    the records that came back, i.e. a search of the last `max_bytes` of the file, ceilinged by the
    READER's `_MAX_READ_BYTES`. On a log bigger than that ceiling the head was unsearchable at every
    parameter: measured on `runs/rubertlite-dr-unified-v2/nodes/node_3/train.log` (88 MB), 61.8 % of
    it, and `CUDA-enabled jaxlib` — printed once per restart, so a count of it is a count of the
    node's restarts — reported **1 match** where the file holds 4 (bytes 109, 18.8 M, 48.7 M, 78.5 M);
    on `runs/rubert-dr-0807/nodes/node_1/train.log` (45 MB), 25.4 %, and 2 matches where there are 3.
    `mode="head"` did not rescue it — head is not a search, it returns the first N records — and above
    2x the ceiling there was a 20.8 MB band of that 88 MB log (23.7 %) no mode reached at all.

    So the reach is the whole log, and the bound is BYTES PARSED with a stated resume point. Three
    properties this shape has that a bigger window would not:

    * the answer is bounded by the ANSWER's size (`max_hits` groups), never by how far it looked, so
      reaching further costs CPU and not context;
    * an early stop happens at a CHUNK boundary, so `hi` is a byte the sweep genuinely examined up to
      and `from_byte=hi` is a call that resumes without a gap; and
    * the floor is enforced here exactly as in `_read_window` — `start` below `source.floor` is raised
      to it, so no `from_byte` a model can type reads a dead attempt's curve as the live one's.
    """
    path = Path(source.path)
    size = os.path.getsize(path)
    floor = max(0, min(int(source.floor or 0), size))
    lo = floor if start is None else max(floor, min(int(start), size))
    ceiling = max(1, int(ceiling))
    rows: list = []
    # The trailing `context` records, for the NEXT hit's leading context. A deque with a maxlen rather
    # than a re-sliced list: this runs once per record over a whole file, and `maxlen=0` (context=0,
    # "just the hits") is a legitimate ask that costs nothing here.
    before: deque = deque(maxlen=max(0, context))
    hits = 0                     # every match in [lo, hi), including ones past `max_hits`
    index = 0                    # record number, 1-based from `lo`
    emitted = 0                  # highest record number already in `rows` (dedupes overlapping context)
    after = 0                    # trailing context still owed to the last shown hit
    carry = b""                  # the bytes of a record straddling the chunk boundary
    scanned = 0
    pos = lo
    torn = lo > floor            # the first record at a mid-file seek is half a record
    first = True
    stopped = ""
    with open(path, "rb") as fh:
        fh.seek(lo)
        while pos < size and scanned < ceiling:
            chunk = fh.read(min(_SEARCH_CHUNK, ceiling - scanned, size - pos))
            if not chunk:
                break
            pos += len(chunk)
            scanned += len(chunk)
            buf = carry + chunk
            # Cut at the last record delimiter so a record never straddles a chunk: the delimiters are
            # ASCII, so this is also what keeps the decode from splitting a UTF-8 sequence and spending
            # a replacement char per boundary. The backstop is for a log with no delimiter at all —
            # 8 MiB of one record is not a record any more, and carrying it forever is a memory leak.
            at_end = pos >= size
            cut = len(buf) if at_end else max(buf.rfind(b"\n"), buf.rfind(b"\r")) + 1
            if cut <= 0:
                if len(buf) < _SEARCH_CHUNK * 8:
                    carry = buf
                    continue
                cut = len(buf)
            carry = buf[cut:]
            for record in _RECORD_SPLIT.split(buf[:cut].decode("utf-8", "replace")):
                record = record.rstrip()
                if not record.strip():
                    continue
                if first:
                    first = False
                    if torn:
                        continue     # never show half a record as if it were a record
                index += 1
                matched = bool(rx.search(record))
                if matched:
                    hits += 1
                if matched and hits <= max_hits:
                    for number, text in before:
                        if number > emitted:
                            rows.append((number, False, text))
                            emitted = number
                    rows.append((index, True, record))
                    emitted = index
                    after = context
                elif after > 0:
                    rows.append((index, False, record))
                    emitted = index
                    after -= 1
                before.append((index, record))
            if hits >= max_hits and after == 0:
                stopped = "hits"
                break
            if scanned >= ceiling and pos < size:
                stopped = "ceiling"
                break
    hi = lo + scanned - len(carry)
    return rows, hits, index, lo, hi, size, torn, stopped


def _search_reach(source: LogSource, lo: int, hi: int, size: int, *, ceiling: int, stopped: str,
                  torn: bool) -> str:
    """What a SEARCH did not look at, and the exact call that looks at it (rule 3).

    The sibling of `_scan_reach`, and it exists for the same reason that one does: the receipt this
    replaced offered a caller at the top of the ladder two remedies it could not spend. `raise
    max_bytes` was already at its cap, and `mode="head" for the run's start` names a mode that does
    not search — it returns the first N records, so on a 33 MB head it is 120,000 records the caller
    would have to page through 60 at a time. A remedy that cannot be spent reads, to anything that
    believes its receipts, as "you have seen it all".

    Every remedy below is one call away and takes a byte this sweep really reached.
    """
    floor = max(0, min(int(source.floor or 0), size))
    parts = []
    if lo > floor:
        parts.append(f"{lo - floor:,} earlier bytes were skipped by from_byte — omit it to sweep from "
                     "this attempt's start")
    elif floor > 0:
        parts.append(f"the {floor:,} bytes below this belong to a PREVIOUS attempt of this node and "
                     "are never searched")
    if torn:
        parts.append("the first record at from_byte was torn by the seek and was dropped")
    if hi >= size:
        parts.append("this sweep reached the END of the log")
    elif stopped == "ceiling":
        body = max(1, size - floor)
        parts.append(f"**this sweep did NOT reach the end of the log** — {size - hi:,} of {body:,} "
                     f"bytes ({100.0 * (size - hi) / body:.0f} %) are above this tool's {ceiling:,}-byte "
                     f"search ceiling, so a match after byte {hi:,} is NOT ruled out. Continue with "
                     f"from_byte={hi}")
    elif stopped == "hits":
        parts.append(f"stopped at the match limit with {size - hi:,} bytes still unsearched — "
                     f"from_byte={hi} continues past them")
    else:
        # The read ended before EOF for neither reason — a short read on a file being appended to,
        # or one rotated under us. Say the byte and nothing about why, rather than borrowing a reason.
        parts.append(f"stopped at byte {hi:,} with {size - hi:,} bytes unsearched — "
                     f"from_byte={hi} continues")
    return "; " + "; ".join(parts) if parts else ""


def _scan_for_window(source: LogSource, key: str, *, need_s: Optional[float],
                     ceiling: int) -> tuple:
    """Read the smallest TAIL that covers the requested time window, escalating up to `ceiling`.

    This is what keeps the default cheap while leaving "the whole run" answerable: `need_s=10` is
    served by one 256 KiB read, `need_s=86400` on a five-hour run escalates to the whole file and the
    receipt states what that cost. A doubling ladder rather than a stat-and-guess because the
    relationship between bytes and seconds is entirely up to what the candidate's script prints.

    An UNBOUNDED window (`whole_run=true`, i.e. `need_s=inf`) is the one case the ladder has nothing
    to discover — the answer is every byte the ceiling allows — so it is ONE read. Escalating to it
    re-READ and, far more expensively, re-PARSED the same prefix at every rung: measured on the 88 MB
    log, six reads parsing 88 MB to cover 33.5 MB of it, where one read parses 88 MB and covers all
    of it. The parse is 51 ms/MB against 10 ms/MB of cold geesefs I/O, so a rung is not cheap.

    Returns `(samples, clock, lo, hi, size, reads)`.
    """
    ceiling = max(1, min(int(ceiling), _MAX_SCAN_BYTES))
    want = ceiling if (need_s is not None and need_s == float("inf")) else min(ceiling,
                                                                              _DEFAULT_SCAN_BYTES)
    reads = 0
    while True:
        text, lo, hi, size = _read_window(source, want=want, where="tail", start=None,
                                          ceiling=ceiling)
        samples, clock = scan_series(text, key)
        reads += 1
        covered = (samples[-1].t - samples[0].t) if len(samples) > 1 else 0.0
        # Enough when the window is covered, when we already hold the whole readable file, or when
        # the ceiling is spent. `need_s=None` means "whatever this read holds" — the cheap default.
        if (need_s is None or covered >= need_s or lo <= max(0, int(source.floor or 0))
                or want >= ceiling):
            return samples, clock, lo, hi, size, reads
        want = min(ceiling, want * 4)


def _scan_reach(source: LogSource, lo: int, size: int, *, whole: bool, ceiling: int) -> str:
    """What a series does NOT cover, in the words its READER needs (rule 3).

    Three cases, and the third is the one this helper exists for. When the scan reached the floor
    there is nothing unread, and a non-zero floor is stated as what it is rather than as bytes
    somebody could go and fetch. When it did not and the caller asked for a TAIL, the fix is the
    parameter they did not pass. When it did not and the caller ALREADY passed `whole_run=true`, the
    ceiling is the binding constraint and the answer has to say the thing that changes how the series
    is read — that its first bucket is a MID-RUN value and not the run's opening one. The text this
    replaced said "earlier bytes not scanned — whole_run=true reads from the start" to a caller who
    had just passed `whole_run=true`: it named a remedy that was already spent, which reads as "you
    are seeing the start" to anything that believes its receipts.
    """
    floor = max(0, min(int(source.floor or 0), size))
    if lo <= floor:
        if floor > 0:
            return (f". The {floor:,} bytes below this belong to a PREVIOUS attempt of this node and "
                    "are never read.")
        return "."
    unread = lo - floor
    body = max(1, size - floor)
    if not whole:
        return f"; {unread:,} earlier bytes not scanned — whole_run=true reads from the start."
    return (f"; **this series does NOT start at the run's start** — {unread:,} of {body:,} bytes "
            f"({100.0 * unread / body:.0f} %) are below it, above this tool's {ceiling:,}-byte scan "
            "ceiling, so the first bucket is a MID-RUN value and the net change is only over the part "
            "read. Narrow the question (last_s) rather than reading this as the whole run.")


# ------------------------------------------------------------------------------------- the provider
class LogQueryTools:
    """ToolProvider (`specs()`/`execute()`) giving a role TWO tools over a bounded set of logs:
    `read_log` (the log as a file) and `metric_series` (its numeric series over a window at a
    granularity).

    `sources` is either a sequence of `LogSource` or a CALLABLE returning one. A callable is the
    normal case and the reason it is one: the active stage log MOVES during an eval (setup -> train ->
    score) and a file can rotate under a long-lived provider, so the map is re-derived per call rather
    than frozen at construction. `default` names the log an omitted `log` argument means; when it is
    None the sole source is used, or — with several — the caller is asked to name one.
    """

    def __init__(self, sources: SourceSpec, *, default: Optional[str] = None,
                 max_read_bytes: int = _MAX_READ_BYTES, max_scan_bytes: int = _MAX_SCAN_BYTES,
                 max_search_bytes: int = _MAX_SEARCH_BYTES):
        self._sources = sources
        self.default = default
        # THREE bounds because there are three questions, and this module's whole thesis is that
        # answering one of them with another's number is a silent truncation: `max_read_bytes` is one
        # ANSWER's window (tail/head/range), `max_scan_bytes` is one metric SCAN, `max_search_bytes`
        # is how far one search SWEEP may look. The last two start at the same value because the
        # derivation is the same (worst-case CPU on the judge's request path) and they are still two
        # names, because `read_log` search was bounded by `max_read_bytes` — a fourth conflation of
        # exactly this kind — until 2026-08-15.
        self.max_read_bytes = max(1, min(int(max_read_bytes), _MAX_READ_BYTES))
        self.max_scan_bytes = max(1, min(int(max_scan_bytes), _MAX_SCAN_BYTES))
        self.max_search_bytes = max(1, min(int(max_search_bytes), _MAX_SEARCH_BYTES))

    def bind_state(self, state=None, parent=None) -> None:
        return None

    # ---------------------------------------------------------------------------------- resolution
    def sources(self) -> list:
        """The source map, re-derived per call. A resolver that raises yields an EMPTY map (and
        therefore a stated refusal), never an exception out of `execute` — providers soft-fail."""
        try:
            raw = self._sources() if callable(self._sources) else self._sources
        except Exception:  # noqa: BLE001 — a resolver hiccup is "no logs right now", not a crash
            return []
        return [s for s in (raw or []) if isinstance(s, LogSource)]

    def _resolve(self, name: Optional[str]) -> Union[LogSource, str]:
        """One log, or a refusal STRING naming what exists. Rule 1: the only thing a caller can say
        about which file to open is a name from this list."""
        available = self.sources()
        if not available:
            return "(no logs available yet — this eval has not written one, or none it may read)"
        wanted = str(name or self.default or "").strip()
        if not wanted:
            if len(available) == 1:
                return available[0]
            return ("(name a log: " + ", ".join(s.name for s in available) + ")")
        for source in available:
            if source.name.lower() == wanted.lower():
                return source
        return (f"(no log named {wanted!r}; this eval writes: "
                + ", ".join(f"{s.name}" + (f" [{s.role}]" if s.role else "") for s in available)
                + ". Logs are named, not paths — you cannot open anything else.)")

    def _header(self, source: LogSource, lo: int, hi: int, size: int) -> str:
        role = f" [{source.role}]" if source.role else ""
        return (f"{source.name}{role}: {size:,} bytes total; "
                f"this answer covers bytes {lo:,}-{hi:,}.")

    # --------------------------------------------------------------------------------------- specs
    def specs(self) -> list[dict]:
        names = ", ".join(s.name for s in self.sources()) or "(none yet)"
        return [
            fn_spec("read_log",
                    "READ THE LIVE LOG LIKE A FILE — tail it, read its head, read a line range, or "
                    "SEARCH it with context. Use this instead of reasoning from the excerpt you were "
                    "given: the excerpt is a short tail (often ~30 seconds of a multi-hour run) and "
                    "the answer to 'did this ever work', 'what did it say at the start', 'is there a "
                    "traceback' is somewhere else in the file.\n"
                    f"Logs this eval writes: {names}. You name a LOG, not a path — nothing else is "
                    "openable.\n"
                    "A progress bar rewrites one line with carriage returns, so a 'line' here is a "
                    "record split on newline OR carriage return, and bar fill is collapsed (every "
                    "number kept) unless you pass raw=true. Every answer says which bytes it covered "
                    "and how to read further.\n"
                    "SEARCH SWEEPS THE WHOLE LOG from the run's start, not a window of it: it reports "
                    "the first matches with context and then says either that it reached the END (so "
                    "'no match' means nowhere in this log) or the exact from_byte= that continues it. "
                    "tail/head/range read a bounded WINDOW instead — that is what max_bytes sizes for "
                    "them.",
                    {"log": {"type": "string", "description": f"which log ({names}); omit for the "
                             "one being judged"},
                     "mode": {"type": "string", "enum": list(_MODES),
                              "description": "tail (default, newest records) | head (the run's "
                                             "START — setup, config, the first losses) | range "
                                             "(records from `start`) | search (sweep the WHOLE log "
                                             "for `pattern`, with context)"},
                     "lines": {"type": "integer", "description": f"how many records "
                               f"(default {_DEFAULT_LINES}, max {_MAX_LINES})"},
                     "start": {"type": "integer", "description": "range mode: the 1-based record "
                               "number to start at"},
                     "pattern": {"type": "string", "description": "search mode: a regular "
                                 "expression (plain text works too), matched case-insensitively"},
                     "context": {"type": "integer", "description": f"search mode: records to show "
                                 f"either side of a hit (default 1, max {_MAX_CONTEXT})"},
                     "from_byte": {"type": "integer", "description": "search mode: resume the sweep "
                                   "at this byte — the number a previous search's receipt gave you. "
                                   "Omit to sweep from this attempt's start"},
                     "max_bytes": {"type": "integer", "description": f"how many bytes this call may "
                                   f"read: a WINDOW for tail/head/range (default "
                                   f"{_DEFAULT_READ_BYTES}, max {self.max_read_bytes}), the SWEEP "
                                   f"budget for search (default and max {self.max_search_bytes})"},
                     "raw": {"type": "boolean", "description": "keep progress-bar fill verbatim "
                             "instead of collapsing it"}},
                    []),
            fn_spec("metric_series",
                    "ASK THE LOG'S NUMBERS A QUESTION over a time or step window, at a granularity "
                    "you choose: the last 10 seconds, the last hour at 1-minute buckets, the whole "
                    "run bucketed hourly, first-vs-last, min/max. This is how you find out whether a "
                    "loss is actually descending — a short tail cannot tell 'converged' from 'still "
                    "descending slowly', because inside any short window that difference is below "
                    "the step-to-step noise.\n"
                    "Each bucket reports count, median, spread, min, max, first and last, so nothing "
                    "is thinned away — coarser buckets aggregate MORE samples, never fewer.\n"
                    "Time is the log's own clock (progress-bar elapsed and/or its timestamps); every "
                    "answer states which, its origin, and how many bytes it read.",
                    {"metric": {"type": "string", "description": "the key to read: loss, grad_norm, "
                                "learning_rate, epoch, or anything else the log prints as `key: "
                                "value` / `key=value`. Also `step` and `it_s` from the progress bar."},
                     "log": {"type": "string", "description": f"which log ({names}); omit for the "
                             "one being judged"},
                     "last_s": {"type": "number", "description": "window: only the last N SECONDS "
                                "of the run's clock. Omit for everything the scan covered."},
                     "bucket_s": {"type": "number", "description": "granularity: bucket width in "
                                  "seconds (3600 = hourly). Omit for one bucket per sample, up to "
                                  f"{_MAX_BUCKETS}."},
                     "whole_run": {"type": "boolean", "description": "scan from the beginning of the "
                                   "log instead of a tail. Costs a full read — say so when you need "
                                   "the run's opening values."},
                     "max_bytes": {"type": "integer", "description": f"ceiling for this call's read "
                                   f"(default escalates from {_DEFAULT_SCAN_BYTES} to what the "
                                   f"window needs, max {self.max_scan_bytes})"}},
                    ["metric"]),
        ]

    # ------------------------------------------------------------------------------------- dispatch
    def execute(self, name: str, args: dict) -> str:
        args = args or {}
        try:
            if name == "read_log":
                return self._read_log(args)
            if name == "metric_series":
                return self._metric_series(args)
        except Exception as e:  # noqa: BLE001 — a provider returns an error string, never raises
            return f"({name} error: {type(e).__name__}: {e})"
        return f"(unknown tool: {name})"

    # ------------------------------------------------------------------------------------- read_log
    def _read_log(self, args: dict) -> str:
        source = self._resolve(args.get("log"))
        if isinstance(source, str):
            return source
        mode = str(args.get("mode") or "tail").strip().lower()
        if mode not in _MODES:
            return f"(mode must be one of {', '.join(_MODES)})"
        lines = max(1, min(_int_arg(args, "lines", _DEFAULT_LINES), _MAX_LINES))
        raw_mode = bool(args.get("raw"))
        if mode == "search":
            # A search does not go through `_read_window` at all: it is a SWEEP of the log, bounded by
            # bytes PARSED with a stated resume point, not a window of it bounded by the reader's
            # per-answer ceiling. See `_search_scan` for the measurement that made that the shape.
            return self._search(source, args, raw_mode, lines)
        want = max(1, min(_int_arg(args, "max_bytes", _DEFAULT_READ_BYTES), self.max_read_bytes))
        where = "head" if mode in ("head", "range") else "tail"
        text, lo, hi, size = _read_window(source, want=want, where=where, start=None,
                                          ceiling=self.max_read_bytes)
        records = _records(text)
        # A tail that does not start at the floor has a torn leading record; drop it rather than
        # showing half a line as if it were a line. Same rule the monitor's own tail read applies.
        truncated_head = lo > max(0, int(source.floor or 0))
        if truncated_head and records:
            records = records[1:]
        header = self._header(source, lo, hi, size)
        if not records:
            return header + "\n(nothing in this window yet)"

        if mode == "head":
            chosen, first_index = records[:lines], 1
        elif mode == "range":
            start = max(1, _int_arg(args, "start", 1))
            chosen, first_index = records[start - 1:start - 1 + lines], start
            if not chosen:
                return (header + f"\n(record {start} is past the {len(records)} records in the "
                        f"{hi - lo:,} bytes read; raise max_bytes, or use mode=\"tail\")")
        else:
            chosen = records[-lines:]
            first_index = len(records) - len(chosen) + 1
        body = [self._render(r, raw_mode) for r in chosen]
        receipt = self._reach_receipt(mode, lo, hi, size, len(records), first_index, truncated_head,
                                      floor=max(0, min(int(source.floor or 0), size)))
        return fit_rows([header, f"records {first_index}-{first_index + len(chosen) - 1} "
                                 f"of {len(records)} in this window:"], body, receipt=receipt,
                        cap=_CAP, omitted="... ({receipt}{n} record(s) dropped to fit the result "
                                          "cap — ask for fewer `lines`)")

    def _render(self, record: str, raw_mode: bool) -> str:
        return clip(record if raw_mode else _squeeze(record), _MAX_RECORD_CHARS,
                    keep="head", note="…({n} more chars in this record; raw=true keeps them)")

    def _reach_receipt(self, mode: str, lo: int, hi: int, size: int, records: int,
                       first_index: int, truncated_head: bool, *, floor: int) -> str:
        """What this answer did NOT cover, and the exact call that covers it (rule 3).

        The unread head is counted from the FLOOR, not from byte 0. Bytes below `LogSource.floor`
        belong to a previous attempt of this node and no parameter reaches them by design (rule 1), so
        counting them as "not read — mode=head for the run's start" both overstates the loss and names
        a call that cannot deliver it. A resumed node's floor is the whole of its predecessor's log,
        which is exactly when that number is largest and the advice most wrong.
        """
        parts = []
        floor = max(0, min(int(floor or 0), size))
        if lo > floor:
            parts.append(f"{lo - floor:,} earlier bytes not read — mode=\"head\" for the run's start, "
                         "or raise max_bytes")
        elif floor > 0:
            parts.append(f"this answer starts at the current attempt's boundary; the {floor:,} bytes "
                         "below it are a previous attempt's and are never read")
        if hi < size:
            parts.append(f"{size - hi:,} later bytes not read — mode=\"tail\" for the newest")
        if truncated_head:
            parts.append("the first record in this window was torn by the seek and was dropped")
        return "; ".join(parts)

    def _search(self, source, args, raw_mode, lines) -> str:
        """Search = sweep the log from this attempt's start (or `from_byte`) and report the first
        matches with context, plus what the sweep did and did not reach.

        Record numbers are the sweep's own, 1-based from `lo` — so with the default `from_byte` they
        are the log's record numbers from the attempt floor, the same numbering `mode="range"` uses,
        and a hit at record 412 can be re-read in full with `mode="range", start=412`.
        """
        pattern = str(args.get("pattern") or "").strip()
        if not pattern:
            return "(search needs a `pattern`)"
        context = max(0, min(_int_arg(args, "context", 1), _MAX_CONTEXT))
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"(pattern is not a valid regular expression: {e})"
        # `max_bytes` on a search bounds the SWEEP, so its ceiling is the scan bound and not the
        # reader's — the two are different questions and binding this one with the wrong number is the
        # defect `_search_scan`'s docstring measures. Default: sweep as far as the ceiling allows.
        ceiling = max(1, min(_int_arg(args, "max_bytes", self.max_search_bytes),
                             self.max_search_bytes))
        start = _int_arg(args, "from_byte", -1)
        rows, hits, records, lo, hi, size, torn, stopped = _search_scan(
            source, rx, start=(None if start < 0 else start), ceiling=ceiling, context=context,
            max_hits=min(lines, _MAX_HITS))
        header = self._header(source, lo, hi, size)
        reach = _search_reach(source, lo, hi, size, ceiling=ceiling, stopped=stopped, torn=torn)
        if not hits:
            # The whole point of the sweep: when it reached the end, "no match" is a statement about
            # the LOG and not about a window, and it is allowed to say so. When it did not, `reach`
            # carries the byte that continues it.
            whole = hi >= size and lo <= max(0, min(int(source.floor or 0), size))
            scope = (f"ANYWHERE in this log — {records:,} record(s), all {hi - lo:,} bytes read"
                     if whole else f"in the {hi - lo:,} bytes read ({records:,} record(s))")
            return header + f"\n(no record matches {pattern!r} {scope}{reach})"
        shown = sum(1 for _n, is_hit, _r in rows if is_hit)
        receipt = f"{hits} match(es)" + (f", showing the first {shown}" if shown < hits else "")
        body = [f"{'>' if is_hit else ' '}{number}: {self._render(record, raw_mode)}"
                for number, is_hit, record in rows]
        return fit_rows([header, f"search {pattern!r} — {receipt}{reach}"], body, cap=_CAP,
                        omitted="... ({receipt}{n} context line(s) dropped to fit the result cap — "
                                "lower `context` or `lines`)")

    # -------------------------------------------------------------------------------- metric_series
    def _metric_series(self, args: dict) -> str:
        source = self._resolve(args.get("log"))
        if isinstance(source, str):
            return source
        metric = str(args.get("metric") or "").strip()
        if not metric:
            return "(metric_series needs a `metric` — e.g. loss, grad_norm, learning_rate, epoch)"
        last_s = args.get("last_s")
        last_s = float(last_s) if isinstance(last_s, (int, float)) and last_s else None
        whole = bool(args.get("whole_run"))
        ceiling = _int_arg(args, "max_bytes", self.max_scan_bytes)
        ceiling = max(1, min(ceiling, self.max_scan_bytes))
        # `whole_run` and a window are two different asks: the window says how much to KEEP, this says
        # how much to READ. `whole_run` reads to the ceiling in one go; otherwise the tail escalates
        # only as far as the window needs, which is what keeps "the last 10 seconds" a 256 KiB read.
        need = float("inf") if whole else last_s
        samples, clock, lo, hi, size, reads = _scan_for_window(
            source, metric, need_s=(None if need is None else need), ceiling=ceiling)
        if not samples:
            # The remedy has to be one the caller has not already spent: `whole_run=true` is not
            # advice to give somebody who passed `whole_run=true` and got nothing back.
            more = ("Try whole_run=true, or read_log with mode=\"search\" to find what this log "
                    "actually names." if not whole else
                    "This was already a whole-run scan"
                    + (f" (it covered {hi - lo:,} of {size:,} bytes — the rest is above this tool's "
                       f"{ceiling:,}-byte ceiling)" if lo > max(0, int(source.floor or 0)) else
                       " over every byte of the log")
                    + ", so this log does not print that key. Use read_log with mode=\"search\" to "
                      "find what it actually names.")
            return (self._header(source, lo, hi, size)
                    + f"\n(no `{metric}` reading in the {hi - lo:,} bytes read. {more})")

        end = samples[-1].t
        begin = samples[0].t
        start = max(begin, end - last_s) if last_s else begin
        inside = [s for s in samples if s.t >= start - 1e-6]
        bucket_s = args.get("bucket_s")
        bucket_s = float(bucket_s) if isinstance(bucket_s, (int, float)) and bucket_s else None
        span = max(0.0, end - start)
        if bucket_s and span > 0 and (span / bucket_s) > _MAX_BUCKETS:
            fits = span / _MAX_BUCKETS
            return (f"(that window is {_fmt_clock(span)} at a {_fmt_num(bucket_s)}s bucket = "
                    f"{int(span / bucket_s):,} buckets, above the {_MAX_BUCKETS} cap. Buckets are "
                    "never silently dropped — they would stop being an aggregate. Use bucket_s>="
                    f"{fits:.0f}, or narrow last_s.)")
        width = bucket_s or (span / _MAX_BUCKETS if span > 0 and len(inside) > _MAX_BUCKETS else 0.0)
        rows = (bucket_series(inside, start=start, end=end, width=width) if width > 0
                else [{"lo": s.t, "hi": s.t, "n": 1, "nonfinite": 0 if s.finite else 1,
                       "median": s.value if s.finite else None, "spread": 0.0,
                       "min": s.value if s.finite else None, "max": s.value if s.finite else None,
                       "first": s.value if s.finite else None, "last": s.value if s.finite else None,
                       "step": s.step, "total": s.total} for s in inside[-_MAX_BUCKETS:]])
        return self._render_series(source, metric, rows, inside, clock,
                                   start=start, end=end, begin=begin, width=width,
                                   lo=lo, hi=hi, size=size, reads=reads, last_s=last_s,
                                   whole=whole, ceiling=ceiling)

    def _render_series(self, source, metric, rows, inside, clock, *, start, end, begin, width,
                       lo, hi, size, reads, last_s, whole, ceiling) -> str:
        finite = [s.value for s in inside if s.finite]
        nonfinite = sum(1 for s in inside if not s.finite)
        head = [
            f"{source.name}: `{metric}` — {len(inside):,} reading(s) over "
            f"{_fmt_clock(end - start)} of the run's clock"
            + (f" (the last {_fmt_num(last_s)}s you asked for)" if last_s else "")
            + (f", {nonfinite} of them non-finite" if nonfinite else ""),
            f"clock: {clock.source()}; t=0 is the start of the SCANNED region"
            + (f", wall {clock.wall(0.0)}" if clock.wall(0.0) else "")
            + f". Read {hi - lo:,} of {size:,} bytes"
            + (f" in {reads} reads" if reads > 1 else "")
            + _scan_reach(source, lo, size, whole=whole, ceiling=ceiling),
        ]
        if finite:
            head.append(f"window: first={_fmt_num(finite[0])} last={_fmt_num(finite[-1])} "
                        f"min={_fmt_num(min(finite))} max={_fmt_num(max(finite))} "
                        f"net={_fmt_num(finite[-1] - finite[0])}")
        head.append(f"buckets of {_fmt_clock(width)}:" if width > 0 else "every reading:")
        body = []
        for row in rows:
            cell = (f"+{_fmt_clock(row['lo'])} n={row['n']:<5d} med={_fmt_num(row['median'])} "
                    f"iqr={_fmt_num(row['spread'])} min={_fmt_num(row['min'])} "
                    f"max={_fmt_num(row['max'])} first={_fmt_num(row['first'])} "
                    f"last={_fmt_num(row['last'])}")
            if row["step"] is not None and row["total"]:
                cell += f" step={row['step']}/{row['total']}"
            if row["nonfinite"]:
                cell += f" NONFINITE={row['nonfinite']}"
            body.append(cell)
        receipt = ("readings are text the TRAINING SCRIPT printed — evidence about what it says, "
                   "not a measured metric")
        return fit_rows(head, body, receipt=receipt, cap=_CAP,
                        omitted="... ({receipt}{n} bucket(s) dropped to fit the result cap — ask "
                                "for a LARGER bucket_s so every reading still lands in one)")
