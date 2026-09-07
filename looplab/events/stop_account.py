"""Why did this run stop, and is it owed more work? — answered from the durable record alone.

THE GAP THIS CLOSES, measured rather than argued. Over `/var/tmp/looplab-bench/runs-B` — 20 real
AlgoTune runs of this engine, 2026-08-22/23 — **8 ended with no `run_finished` event at all**, and
`looplab inspect` reported every one of them with the same two words: `finished=False`. They are
three different things:

* **4 auto-paused after a Developer crash** (`discrete_log`, `integer_factorization`, `pagerank`,
  `spectral_clustering`): each wrote `node_failed{reason: "developer_crash"}` and then a `pause`
  carrying "auto-paused: a Developer session crashed (LLM unreachable or a hard error, unresolved
  within the node) — resume once it's fixed".
* **1 auto-paused on the Researcher's fallback** (`count_riemann_zeta_zeros`): a `pause` naming the
  provider failure, the fact that nothing was proposed, and the remedy.
* **3 were killed from outside** (`pde_heat1d`, `sparse_eigenvectors_complex`,
  `max_weighted_independent_set`): the campaign harness's own `timeout 14400` sent SIGTERM at a
  four-hour wall. Nothing in the log says so, because nothing in the process survived to write it.

A paused run correctly has no `run_finished` — it is RESUMABLE, not over — and the reason it is
paused was durable in `events.jsonl` the whole time. The fold threw it away and no reader asked.
That is the first half of this module: state the disposition and quote the reason.

THE OTHER TWELVE WERE NO BETTER, and that was not in the brief. Every run in the corpus that DID
finish folds to `stop_reason="error"` — which reads as a crash, and not one of them crashed: all
twelve stopped on the operator's own `llm_budget_usd` ceiling, and the `run_finished` row said so on
the same row, in the `error` field, in a sentence naming the amount spent, the setting and the
remedy. The fold kept the class and dropped the sentence. That is a worse failure than the paused
half: `finished=False` at least prompts a question, while `finished (reason=error)` answers it
wrongly, and `compare_arms.py` had to re-scan the raw log line-by-line for `"run_finished"` AND
`"spend ceiling reached"` to recover a fact the fold was holding one field away.

WHAT THE ENGINE MAY NOT SAY, and why this module refuses to say it. It may not name the SIGTERM.
Nothing of ours observed that signal: there is no handler (grep `SIGTERM` — every hit is about a
process the engine SUPERVISED, never about itself), and the default disposition runs no `finally`,
no `atexit` and no append. The house rule for this is already written down twice —
`docs/44-text-may-nominate-never-decide-2026-08-20.md`'s ownership test ("the engine classifies what
it DID or COMPUTED"), and `engine/failure_diagnosis.py::engine_observed_facts`, which names
`SIGTERM` for a child whose exit STATUS the engine holds and refuses to name a memory kill it merely
suspects. Applied here: the engine holds no status for its own death, so `no_boundary` is the whole
of what it may claim — *the record has no end in it*. Which of the many ways a process can die
happened is a question for the supervisor that killed it, and this campaign's supervisor answers it
(`campaign.sh::record_done` writes `state=wall_cut` / `rc=124`, read by `compare_arms.py`).

WHY A READ-TIME ACCOUNT AND NOT A HEARTBEAT. A heartbeat needs the dying process to have cooperated,
and it buys less than it looks. Its whole content is a better estimate of WHEN writing stopped, and
the corpus says that estimate is already free and already misleading: the last durable row lands
36 s before the kill in `pde_heat1d`, 72 s in `sparse_eigenvectors_complex` — and **26 minutes** in
`max_weighted_independent_set`, which was alive and paying for LLM calls the whole time. Whatever
the period, a heartbeat still cannot say WHY, because "the writer stopped writing" is exactly what
the absent boundary already says. Against that it costs a periodic append to a log whose readers key
on POSITION: engine invariant #1 records the same shape twice, where `train_monitor_alert` /
`asha_rank` fired on a timer into a reservation's CAS window and cost 17 builds / 5 discards -> 12 / 0.
A derivation needs nothing of the dead process, adds no bytes, and cannot be wrong-but-durable — if
the run is resumed and finishes, the account changes because the FACTS changed.

THIS IS A RECORD AND NOT A VERDICT (invariant: text may nominate, never decide). Nothing here is a
gate. `cli/run_cmds.py::classify_prior_run` is the sole decider of what a re-entering command does
with a lifecycle boundary, it reads `finished`/`paused`/`stop_requested` off the fold, and it must
stay that way: `tests/test_stop_account.py` pins that it does not read this module. The account is
deliberately prose plus three plain fields — there is no `owed: bool` here to branch on, because the
one caller that would want it already has `paused`.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Optional

# Invariant 7: an event type is a REGISTERED constant, never a literal. This module is the
# reason the rule exists in its purest form — a `phase_progress` beacon has no reader that
# fails loudly, so a literal that stopped matching would not raise: `_open_phase` would
# return None for every run and `looplab inspect` would print the FALSE sentence "no phase
# beacon was left open — the run was between phases" about a run that died mid-`propose`,
# forever, with nothing red. `stop_account` lives in `events/`, so naming its own package's
# registry costs no layering.
from looplab.events.types import EV_PHASE_PROGRESS, PROGRESS_STARTED

# The four dispositions, each PROVABLE from the durable record and jointly exhaustive:
#   finished     — a `run_finished` is folded. The run ended on its own terms.
#   paused       — a `pause` is in effect. Resumable, not over; the reason is quoted.
#   no_boundary  — a `run_started` is folded and neither of the above. The engine never recorded an
#                  end, so it is either still running or the process writing it is gone. The log
#                  alone cannot tell those apart, and this module does not pretend otherwise.
#   no_log       — the fold saw no `run_started`. Nothing to account for.
#
# Deliberately NOT drawn from `core/models.py::FAILURE_REASONS`, and not extended into it. That
# registry's own rule is stated in the two comments around it: every member is a verdict about ONE
# candidate's evaluation ("a crash, a missing dependency, a metric printed one directory over"), and
# `REPAIRABLE_REASONS` derives from it by asking whether an inline repair could fix that candidate's
# code. A run stopping is not a node failing — there is no code to repair, no `node_failed` to carry
# it, and no repair budget it belongs to — so a member added here would be a member every consumer
# of that registry (`options.py`, `failure_diagnosis`'s three sibling tuples, the Developer's own
# emit enum) would have to be taught to ignore. Different question, different vocabulary.
STOP_DISPOSITIONS: tuple[str, ...] = ("finished", "paused", "no_boundary", "no_log")


@dataclass(frozen=True)
class StopAccount:
    """One run's stop account. `line` is the whole of it; the other two are for a structured reader.

    No boolean. See the module docstring: "is it owed more work?" is answered in `line`, in words,
    precisely so that no caller can quietly promote this record into a gate.
    """

    disposition: str
    reason: Optional[str]
    line: str


_NOBODY_SAID = (
    "STOPPED WITHOUT A BOUNDARY — this run recorded neither a `run_finished` nor a `pause`, so the "
    "engine never wrote down an end for it. That means one of two things and the log cannot "
    "distinguish them: it is still running, or whatever was writing it is gone (a wall-clock kill, "
    "an OOM kill, a lost session, a power cut). Nothing of ours observed the second, so nothing of "
    "ours may name it — ask whoever supervises the process, and check whether anything still holds "
    "the run dir's engine.lock. Either way the run is OWED more work: nothing has been finalized and "
    "`looplab resume` picks it up from the last durable event."
)


def stop_account(state) -> StopAccount:
    """The stop account for a folded `RunState`. Total — never raises, and never touches the disk.

    IT NAMES THE LOCK AND DOES NOT TAKE IT, which is the one deliberate omission here. "Is a writer
    still alive?" is answerable out of band — `<run_dir>/engine.lock` is freed by the OS on any exit,
    crash included — and question 1 of docs/44 says to use an out-of-band fact rather than mint a
    signal for it. It is not taken because `flock` on a live run's lock is EXCLUSIVE in both
    directions: a probe here opens a window in which a genuine `cli/__init__.py::_engine_singleton`
    acquisition sees contention and the operator's `run`/`resume` silently no-ops, which is precisely
    the phantom "already running" that file's own branches document having fixed. Liveness is also a
    fact about NOW rather than about the run, so a record of what the run WROTE is the wrong place to
    store it. The sentence names the path and hands the question to the reader.
    """
    if not getattr(state, "run_id", ""):
        return StopAccount("no_log", None,
                           "no `run_started` in this log — there is no run here to account for.")
    if getattr(state, "finished", False):
        reason = _text(getattr(state, "stop_reason", None))
        detail = _text(getattr(state, "stop_detail", None))
        # Stated for BOTH answers, like `inspect`'s comparability line and for the same reason: a
        # sentence printed only when a key exists makes its absence invisible on exactly the runs
        # where it matters. A finish with no reason is a legacy/markerless finish, not a clean one.
        if not reason:
            return StopAccount("finished", None,
                               "finished, and the `run_finished` row names no reason — an old log, "
                               "or a finish written before reasons were recorded.")
        line = f"finished (reason={reason})"
        if detail:
            # THE COARSE CLASS IS NOT THE ACCOUNT, and on this corpus it is actively misleading: all
            # 12 finished runs in `runs-B` carry `reason=error`, and every one of them stopped on the
            # operator's own spend ceiling with the sentence saying so on the same row. Printed on its
            # own line, unabbreviated, because that sentence is what a reader came for.
            line += f"\n  {detail}"
        elif reason == "error":
            line += " — and the row carries no sentence saying WHAT went wrong. `error` is the class, "\
                    "not the account; look at the last `node_failed` rows."
        return StopAccount("finished", reason, line)
    if getattr(state, "paused", False):
        reason = _text(getattr(state, "pause_reason", None))
        scope = ("the whole run" if getattr(state, "pause_node_id", None) is None
                 else f"node {state.pause_node_id}")
        head = (f"PAUSED ({scope}) — resumable, NOT finished, so the absent `run_finished` is "
                f"correct rather than missing. It is OWED more work: `looplab resume`.")
        # The pause's OWN words, verbatim and unabbreviated. The producers already bound their text
        # at the append site and this is the one place a reader is meant to see all of it: the five
        # paused runs in the corpus each named their cause AND their remedy in that string, and
        # truncating it here would re-create the investigation it exists to prevent.
        body = (f"{head}\n  pause reason: {reason}" if reason else
                f"{head}\n  the `pause` row names no reason — nobody can say why.")
        return StopAccount("paused", reason, body + _unserved_finalize(state))
    return StopAccount("no_boundary", None, _NOBODY_SAID + _unserved_finalize(state))


def _unserved_finalize(state) -> str:
    """The clause for a `run_abort` recorded on a run that has NO finish at all, else "".

    A separate fact from the disposition and stated separately: `stop_requested` means somebody ASKED
    for a wrap-up — report, cross-run lessons, cost roll-up — and the run stopped before producing
    one. A reader who resumes without knowing that gets a fresh search epoch stacked on an unfinished
    finalization.

    CALLED ONLY FROM THE TWO BRANCHES WHERE NO `run_finished` EXISTS, and that is the whole reason
    this needs no predicate of its own. On a run that HAS finished, "is a finalize still outstanding?"
    is a genuinely subtle question — `cli/run_cmds.py::classify_prior_run` answers it with a stop
    request NEWER than the accepted finish, or a finish whose own reason is `error` — and this module
    deliberately does not answer it a second time. Doc 25 §0.8 measured four implementations of one
    claim/verdict join and every drift was between the copies; a fifth spelling of the pending-finalize
    rung, living in a RECORD where nothing would exercise it, is that finding volunteering to recur.
    Here `paused`/`no_boundary` have already established that no finish exists, so a truthy
    `stop_requested` means "asked, never served" with nothing left to decide.

    Not observed in any of the 20 corpus runs, and stated anyway: it needs an operator `finalize`, so
    absence in one automated campaign says nothing about the shape — and whoever needs this line will
    not have a log of it to compare against.
    """
    asked = _text(getattr(state, "stop_requested", None))
    if not asked:
        return ""
    return (f"\n  a finalize was requested (`run_abort` reason={asked}) and never served — the "
            f"wrap-up it asked for was not written. `looplab finalize` completes it.")


def last_record_line(events) -> Optional[str]:
    """What the log's last rows say the run was doing — the evidence half, or None on an empty log.

    Separate from `stop_account` because it needs the EVENTS and the account needs only the folded
    state, and because it is evidence rather than disposition: `summary` and `means` are for READING,
    `quote` and `locator` are for CHECKING (`docs/44`, on the diagnostician's own record).

    Two facts, both read straight off rows nobody had to add:

    * the LAST row of any type, with its seq and timestamp. No skip list — "the last thing written"
      needs no definition and cannot be argued with.
    * the last OPEN phase beacon: a `phase_progress` with `status="started"` that no later
      `finished` for the same phase closes. That is the run's own answer to "what was I doing", and
      it was already in the log — `pde_heat1d` died inside `node 3 improve build/propose`, which is
      the sentence its investigation was missing. Absent (a run that died between phases) is a real
      answer and is said, never left blank.
    """
    rows = [e for e in (events or ()) if getattr(e, "type", None)]
    if not rows:
        return None
    last = rows[-1]
    bits = [f"last record: `{last.type}` seq={getattr(last, 'seq', '?')} "
            f"ts={_iso(getattr(last, 'ts', None))}"]
    open_phase = _open_phase(rows)
    if open_phase is None:
        bits.append("no phase beacon was left open — the run was between phases, so its own log "
                    "does not say what it was working on")
    else:
        phase, since = open_phase
        quiet = ""
        ts = getattr(last, "ts", None)
        if isinstance(ts, (int, float)) and isinstance(since, (int, float)) and ts >= since:
            quiet = f", and nothing closed it in the {(ts - since) / 60:.1f} min before that last row"
        bits.append(f"last OPEN phase: {phase} (started {_iso(since)}){quiet}")
    return "\n  ".join(bits)


def _open_phase(rows):
    """`(label, started_ts)` for the newest unclosed `phase_progress`, or None.

    Keyed by `(node_id, stage, phase)` — the triple the beacon itself carries — so a `finished` for
    one node's `propose` does not close another node's. Order-tolerant in the only way that matters:
    it scans forward and lets the last write per key win, exactly as the fold does.
    """
    open_by_key: dict = {}
    for e in rows:
        if getattr(e, "type", None) != EV_PHASE_PROGRESS:
            continue
        d = getattr(e, "data", None) or {}
        key = (d.get("node_id"), d.get("stage"), d.get("phase"))
        if str(d.get("status") or "") == PROGRESS_STARTED:
            label = " ".join(str(part) for part in (
                f"node {d['node_id']}" if d.get("node_id") is not None else None,
                d.get("operator"), d.get("stage"), d.get("phase")) if part)
            open_by_key[key] = (label or "an unnamed phase", getattr(e, "ts", None))
        else:
            open_by_key.pop(key, None)
    if not open_by_key:
        return None
    return max(open_by_key.values(), key=lambda pair: pair[1] if isinstance(pair[1], (int, float))
               else float("-inf"))


def _text(value) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _iso(ts) -> str:
    if not isinstance(ts, (int, float)):
        return "unknown"
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(timespec="seconds")
