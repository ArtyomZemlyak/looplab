"""A stopped run must be legible from its own log.

Every shape below is copied from `/var/tmp/looplab-bench/runs-B` — 20 real AlgoTune runs of this
engine, 2026-08-22/23 — and named by the run it came from, but nothing here reads that directory:
the logs are synthesized in `tmp_path` so the guard is real on any box. The measurement those runs
produced, which is what these tests hold:

* **8 of the 20 ended with no `run_finished` event**, and `looplab inspect` reported all 8 with the
  same two words, `finished=False`. They are three unrelated things — 4 auto-paused after a
  `developer_crash`, 1 auto-paused on a Researcher fallback, and 3 were killed from outside by the
  campaign harness's `timeout 14400` (SIGTERM at a four-hour wall; `campaign-paired/B-*.done` records
  `rc=124` for exactly those three).
* **All 12 that DID finish fold to `stop_reason="error"`**, which reads as a crash. None crashed:
  every one stopped on the operator's own `llm_budget_usd` ceiling and said so, in full, in the
  `run_finished.error` field the fold discarded.

Both halves are the same defect — durable text the fold dropped — and the tests are paired
accordingly. The third class gets a NEGATIVE guard instead: the engine may not name the signal that
killed it, because nothing of ours observed one.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from looplab.core.models import FAILURE_REASONS
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.stop_account import (STOP_DISPOSITIONS, last_record_line,
                                         stop_account)

REPLAY_SRC = Path(__file__).resolve().parents[1] / "looplab" / "events" / "replay.py"

# `runs-B/discrete_log` seq 317-318, byte for byte. The pause that cost hours to investigate.
DEVELOPER_CRASH_PAUSE = ("auto-paused: a Developer session crashed (LLM unreachable or a hard error, "
                         "unresolved within the node) — resume once it's fixed")
# `runs-B/convex_hull` seq 476's `error` field, abridged only where it repeats itself.
SPEND_CEILING = ("LLM spend ceiling reached: $1.0084 of the $1.0000 set by `llm_budget_usd`. The run "
                 "stops here rather than spending more. To continue, raise `llm_budget_usd` in this "
                 "run's `config.snapshot.json` (0 = no limit) and resume")


def _started(store: EventStore) -> None:
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})


def _crashed_node(store: EventStore, node_id: int = 3) -> None:
    """The exact prefix an auto-pause needs, because `_on_pause` refuses a scoped pause otherwise.

    Driven rather than stubbed: the fold takes a node-scoped `pause` ONLY when that node is `failed`
    with `error_reason == "developer_crash"`, so a test that skipped this would be pausing through a
    door the production path never opens.
    """
    store.append("node_created", {"node_id": node_id, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}}, "code": ""})
    store.append("node_failed", {"node_id": node_id, "generation": 0, "reason": "developer_crash",
                                 "error": "(developer error: LLM request failed)"})


def _fold(path: Path):
    return fold(EventStore(path).read_all())


# --------------------------------------------------------------- the paused half (5 of the 8)

def test_an_auto_pause_reaches_the_reader_with_its_reason(tmp_path):
    """`runs-B/discrete_log`: the sentence was durable all along and no reader could reach it."""
    p = tmp_path / "events.jsonl"
    store = EventStore(p)
    _started(store)
    _crashed_node(store)
    store.append("pause", {"node_id": 3, "generation": 0, "reason": DEVELOPER_CRASH_PAUSE})

    state = _fold(p)
    assert state.paused is True
    assert state.finished is False
    # The fold now carries it…
    assert state.pause_reason == DEVELOPER_CRASH_PAUSE
    account = stop_account(state)
    # …and the account a reader is handed quotes it in full, unabbreviated. Truncating it would
    # re-create the investigation this exists to end: the remedy is at the END of that sentence.
    assert account.disposition == "paused"
    assert account.reason == DEVELOPER_CRASH_PAUSE
    assert DEVELOPER_CRASH_PAUSE in account.line
    assert "node 3" in account.line
    # A pause is not a failure to report — it is work still owed.
    assert "OWED" in account.line and "resume" in account.line


def test_an_operator_pause_with_no_reason_says_nobody_can_say(tmp_path):
    """An absent reason is stated, never rendered as silence — `inspect`'s comparability rule."""
    p = tmp_path / "events.jsonl"
    store = EventStore(p)
    _started(store)
    store.append("pause", {"reason": "   "})

    account = stop_account(_fold(p))
    assert account.disposition == "paused" and account.reason is None
    assert "names no reason" in account.line


@pytest.mark.parametrize("lift", ["resume", "run_reopened", "node_reset", "node_abort",
                                  "node_tombstoned"])
def test_every_lift_of_a_pause_takes_its_reason_with_it(tmp_path, lift):
    """A stale reason on a run that is no longer paused is a lie one `getattr` away from a reader.

    Parametrized over all five events that clear `paused` in the fold, because the reason is cleared
    at four separate call sites and a fifth added later is exactly how this drifts. The AST guard
    below catches a NEW site; this catches the ones that exist.
    """
    p = tmp_path / "events.jsonl"
    store = EventStore(p)
    _started(store)
    _crashed_node(store)
    store.append("pause", {"node_id": 3, "generation": 0, "reason": DEVELOPER_CRASH_PAUSE})
    assert _fold(p).pause_reason == DEVELOPER_CRASH_PAUSE

    payload = {"resume": {}, "run_reopened": {},
               # the writer computes the subtree; the fold stays a pure set op over it
               "node_tombstoned": {"node_ids": [3]}}.get(lift, {"node_id": 3, "generation": 0})
    store.append(lift, payload)

    state = _fold(p)
    assert state.paused is False, f"{lift} did not lift the pause — the fixture is wrong, not the code"
    assert state.pause_reason is None, f"{lift} lifted the pause and left its reason behind"
    assert stop_account(state).disposition != "paused"


def test_pause_reason_is_cleared_wherever_the_pause_is_lifted():
    """AST, not substrings: every `st.paused = False` in the fold has an `st.pause_reason = None`.

    The behavioural test above proves the four sites that exist today. This one is what makes a FIFTH
    site go red the day it is written, and it is an AST scan because the cheapest way to satisfy a
    text pin here is a comment (CLAUDE.md's guard-test ladder, tier 3). It is a COUNT equality rather
    than a per-site adjacency check on purpose — the four sites sit at four different indentations
    inside four different handlers, and a shape assertion over that is a test about formatting.
    """
    tree = ast.parse(REPLAY_SRC.read_text(encoding="utf-8-sig", errors="replace"))
    lifts, cleared = 0, 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                and target.value.id == "st"):
            continue
        value = node.value
        if target.attr == "paused" and isinstance(value, ast.Constant) and value.value is False:
            lifts += 1
        if target.attr == "pause_reason" and isinstance(value, ast.Constant) and value.value is None:
            cleared += 1
    assert lifts >= 4, "the fold stopped lifting pauses — re-derive this guard before relaxing it"
    assert cleared == lifts, (
        f"{lifts} sites set `st.paused = False` but {cleared} clear `st.pause_reason`. A run that is "
        f"no longer paused must not still carry the reason it was paused for.")


# --------------------------------------------------------------- the finished half (12 of the 20)

def test_a_budget_finish_says_which_budget_and_by_how_much(tmp_path):
    """`runs-B/convex_hull` and eleven siblings: `reason=error` on a run that did not crash."""
    p = tmp_path / "events.jsonl"
    store = EventStore(p)
    _started(store)
    store.append("run_finished", {"reason": "error", "error": SPEND_CEILING})

    state = _fold(p)
    assert state.stop_reason == "error"
    assert state.stop_detail == SPEND_CEILING
    account = stop_account(state)
    assert account.disposition == "finished"
    # The COARSE class is kept — it is what `classify_prior_run` decides on — and the sentence is
    # printed beside it. A reader who sees only "error" concludes the run crashed; twelve did not.
    assert "reason=error" in account.line
    assert "$1.0084" in account.line and "llm_budget_usd" in account.line


def test_an_error_finish_with_no_sentence_says_the_class_is_not_the_account(tmp_path):
    p = tmp_path / "events.jsonl"
    store = EventStore(p)
    _started(store)
    store.append("run_finished", {"reason": "error"})

    account = stop_account(_fold(p))
    assert account.disposition == "finished" and account.reason == "error"
    assert "`error` is the class" in account.line


def test_reopening_a_finished_run_drops_its_finish_sentence(tmp_path):
    p = tmp_path / "events.jsonl"
    store = EventStore(p)
    _started(store)
    store.append("run_finished", {"reason": "error", "error": SPEND_CEILING})
    assert _fold(p).stop_detail == SPEND_CEILING
    store.append("run_reopened", {})

    state = _fold(p)
    assert state.finished is False and state.stop_reason is None and state.stop_detail is None


# --------------------------------------------------- the killed half (3 of the 8), and its limits

def _killed_log(tmp_path) -> Path:
    """`runs-B/pde_heat1d`: a domain event, an open phase beacon, then 22.6 min of paid LLM calls.

    That tail is the whole shape of a wall-clock kill in this corpus, and it is why a heartbeat was
    refused: the process was ALIVE and spending for the entire silence, so "it stopped writing" was
    never the missing fact. What was missing is that nothing said the log had no end in it.
    """
    p = tmp_path / "events.jsonl"
    store = EventStore(p)
    _started(store)
    store.append("node_created", {"node_id": 3, "parent_ids": [], "operator": "improve",
                                  "idea": {"operator": "improve", "params": {}}, "code": ""})
    store.append("phase_progress", {"node_id": 3, "operator": "improve", "stage": "build",
                                    "phase": "propose", "status": "started"})
    for _ in range(12):
        store.append("llm_usage", {"cost": 0.006, "calls": 1, "prompt_tokens": 22087,
                                   "completion_tokens": 10880, "total_tokens": 32967})
    return p


def test_a_run_that_was_killed_is_named_as_having_no_boundary(tmp_path):
    state = _fold(_killed_log(tmp_path))
    assert state.finished is False and state.paused is False
    account = stop_account(state)
    assert account.disposition == "no_boundary"
    assert account.reason is None, "nobody wrote a reason; the account must not invent one"
    assert "OWED" in account.line and "resume" in account.line


def test_the_engine_refuses_to_name_the_signal_that_killed_it(tmp_path):
    """The ownership test, applied to the engine's own death (docs/44).

    There is no SIGTERM handler in this package — every `SIGTERM` in `looplab/` is about a process the
    engine SUPERVISED and holds an exit status for. So the engine observed nothing about its own kill,
    and a record that named one would be a conclusion drawn from an absence. The account must say the
    record has no end in it, name BOTH live possibilities, and hand the question to the supervisor.

    A negative pin, deliberately on the TEXT (CLAUDE.md: what must not come back is the text).
    """
    line = stop_account(_fold(_killed_log(tmp_path))).line
    for forbidden in ("SIGTERM", "SIGKILL", "killed by", "was killed", "timed out", "wall clock"):
        assert forbidden not in line, f"the account claims {forbidden!r}; nothing of ours observed it"
    # …and it must not silently collapse the two live readings into the dead one.
    assert "still running" in line
    assert "engine.lock" in line, "the out-of-band channel is named, and left for the reader to use"


def test_an_unserved_finalize_request_is_stated_beside_the_disposition(tmp_path):
    """A `run_abort` with no `run_finished` after it: somebody asked for the wrap-up and never got it.

    Not observed in any of the 20 corpus runs — it needs an operator `finalize` — which is exactly
    why it is guarded rather than left to be noticed: the person who needs this line will not have a
    log of it to compare against.
    """
    p = _killed_log(tmp_path)
    EventStore(p).append("run_abort", {"reason": "operator"})

    account = stop_account(_fold(p))
    assert account.disposition == "no_boundary"
    assert "a finalize was requested" in account.line
    assert "looplab finalize" in account.line


def test_the_account_leaves_the_pending_finalize_question_to_its_owner(tmp_path):
    """A run that HAS finished never gets the clause, however its stop request sits.

    "Is a finalize still outstanding on a FINISHED run?" is `classify_prior_run`'s question — a stop
    request newer than the accepted finish, or a finish whose reason is `error` — and this module
    deliberately does not answer it a second time (doc 25 §0.8: four implementations of one join, and
    every drift was between the copies). The guard is that the clause is scoped to the branches where
    no finish exists, so there is no second spelling to drift.
    """
    p = tmp_path / "events.jsonl"
    store = EventStore(p)
    _started(store)
    store.append("run_abort", {"reason": "operator"})
    store.append("run_finished", {"reason": "error", "error": SPEND_CEILING})

    state = _fold(p)
    # …the fact itself is still on the fold, untouched, for the decider that owns it.
    assert state.stop_requested == "operator"
    assert "a finalize was requested" not in stop_account(state).line


def test_the_last_open_phase_survives_the_silence_that_follows_it(tmp_path):
    """The evidence half, read off beacons that were already being written.

    `pde_heat1d` died inside `node 3 improve build/propose` and its log said so the whole time — the
    beacon is a `DIAGNOSTIC_EVENT`, so the fold never reads it and no reader had gone looking. No
    event was added to make this sayable.
    """
    evidence = last_record_line(EventStore(_killed_log(tmp_path)).read_all())
    assert "last record: `llm_usage`" in evidence
    assert "node 3 improve build propose" in evidence


def test_a_closed_phase_is_not_reported_as_open(tmp_path):
    """`runs-B/max_weighted_independent_set` — killed at the wall with its last beacon CLOSED.

    2 of the 3 wall-cut runs look like this, so an "open phase" that reported the last STARTED beacon
    regardless of its `finished` twin would be wrong more often than right. Absence is a real answer
    and is said out loud rather than left blank.
    """
    p = tmp_path / "events.jsonl"
    store = EventStore(p)
    _started(store)
    store.append("phase_progress", {"node_id": 2, "operator": "draft", "stage": "build",
                                    "phase": "propose", "status": "started"})
    store.append("phase_progress", {"node_id": 2, "operator": "draft", "stage": "build",
                                    "phase": "propose", "status": "finished", "seconds": 277.176,
                                    "ok": True})
    store.append("llm_usage", {"cost": 0.0028, "calls": 1})

    evidence = last_record_line(EventStore(p).read_all())
    assert "no phase beacon was left open" in evidence
    assert "propose" not in evidence


def test_one_nodes_finished_phase_does_not_close_anothers(tmp_path):
    """The beacon is keyed by `(node_id, stage, phase)` — the triple it carries — not by phase name.

    Under a settled build width above 1 two nodes propose concurrently, so a name-keyed reader would
    let node 2's completion close node 3's open beacon and report a killed run as quiescent.
    """
    p = tmp_path / "events.jsonl"
    store = EventStore(p)
    _started(store)
    store.append("phase_progress", {"node_id": 3, "operator": "improve", "stage": "build",
                                    "phase": "propose", "status": "started"})
    store.append("phase_progress", {"node_id": 2, "operator": "draft", "stage": "build",
                                    "phase": "propose", "status": "finished", "ok": True})

    assert "node 3 improve build propose" in last_record_line(EventStore(p).read_all())


# --------------------------------------------------------------- the reader, driven end to end

def _inspect(run_dir: Path) -> str:
    """`looplab inspect` over the real `app`, because the READER is what was missing.

    Every property above is about a value; this is about whether anyone ever sees it. The defect
    being closed was never that the fact was unavailable — it was durable in `events.jsonl` for the
    whole four hours — but that the one command an operator runs to ask "what did this run do?"
    printed `finished=False` and stopped.
    """
    from typer.testing import CliRunner

    from looplab.cli import app

    result = CliRunner().invoke(app, ["inspect", str(run_dir)])
    assert result.exit_code == 0, result.output
    return result.output


def test_inspect_prints_the_pause_reason_it_used_to_swallow(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store = EventStore(run_dir / "events.jsonl")
    _started(store)
    _crashed_node(store)
    store.append("pause", {"node_id": 3, "generation": 0, "reason": DEVELOPER_CRASH_PAUSE})

    output = _inspect(run_dir)
    assert "finished=False" in output, "the old line stays — this adds an account, it replaces nothing"
    assert "stop: PAUSED" in output
    assert DEVELOPER_CRASH_PAUSE in output


def test_inspect_prints_the_account_and_the_evidence_for_a_run_with_no_boundary(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    log = _killed_log(run_dir)
    assert log.parent == run_dir

    output = _inspect(run_dir)
    assert "stop: STOPPED WITHOUT A BOUNDARY" in output
    assert "stop evidence:" in output
    assert "node 3 improve build propose" in output


def test_inspect_states_the_account_on_a_run_that_finished_cleanly(tmp_path):
    """Printed for EVERY disposition. A line that appears only when there is something to say makes
    its own absence invisible on exactly the runs where it matters most — `inspect`'s own
    `comparability:` argument, one line up in the same command."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store = EventStore(run_dir / "events.jsonl")
    _started(store)
    store.append("run_finished", {"reason": "budget", "error": ""})

    output = _inspect(run_dir)
    assert "stop: finished (reason=budget)" in output
    assert "stop evidence:" in output


# ------------------------------------------------------- it is a RECORD, and it stays one

def test_the_stop_vocabulary_is_not_a_failure_reason():
    """`FAILURE_REASONS` is a verdict about ONE candidate's evaluation; a run stopping is not that.

    Its own rule, from the comment beneath it: `REPAIRABLE_REASONS` derives from this tuple by asking
    whether an inline repair could fix that candidate's code. `no_boundary` has no code to repair, no
    `node_failed` to travel on and no repair budget it belongs to, so a member added there would be
    one every consumer of the registry — `engine/options.py`, `failure_diagnosis`'s three sibling
    tuples, the Developer's own emit enum — would have to be taught to ignore.
    """
    assert set(STOP_DISPOSITIONS).isdisjoint(FAILURE_REASONS)
    assert "no_boundary" not in FAILURE_REASONS


def test_no_decision_path_reads_the_stop_account():
    """The lifecycle DECIDER must keep deciding on the fold's own facts, never on this record.

    `cli/run_cmds.py::classify_prior_run` picks which event a re-entering command appends. It reads
    `finished` / `paused` / `stop_requested` / `stop_reason`, and it must not learn to read
    `pause_reason`, `stop_detail` or the account — those are prose a model or a remote writer can
    shape, and "text may nominate, never decide" is the whole of why they are separated.

    AST over the real function, so a commented-out read cannot satisfy it and a live one cannot hide.
    """
    from looplab.cli import run_cmds

    tree = ast.parse(textwrap.dedent(inspect.getsource(run_cmds.classify_prior_run)))
    read = {node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)}
    assert "finished" in read and "paused" in read, "the fixture no longer names the decider"
    for prose in ("pause_reason", "stop_detail", "line", "disposition"):
        assert prose not in read, (
            f"classify_prior_run reads `{prose}` — that turns a record into a gate")


def test_the_engine_does_not_import_the_stop_account():
    """No engine module may reach for it: a record that reaches the loop becomes an input to it.

    The importers are `cli/__init__.py` (the printed summary) and `cli/inspect_cmds.py` (the evidence
    half). Both are readers. `events/` may import it freely — it lives there.
    """
    from tests._source_scan import iter_sources

    pkg = Path(__file__).resolve().parents[1] / "looplab"
    offenders = sorted(str(path.relative_to(pkg)) for path, text in iter_sources(pkg)
                       if "stop_account" in text and path.parts[-2] in ("engine", "search", "agents"))
    assert offenders == [], f"a decision layer imports the stop account: {offenders}"
