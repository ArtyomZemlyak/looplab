"""A MEMO IS QUOTED AS CURRENT LONG AFTER THE RESULT IT DENIES LANDED (doc 53 §4a, third half).

`spectral_clustering` is the specimen and the whole timeline is in its own event log:

    seq 128   2052.8s  node_eval_started   node=0
    seq 130   2052.9s  research_attempted  trigger=cadence at_node=1
    seq 144   2109.1s  node_evaluated      node=0 metric=0.0
    seq 155   2365.7s  research_completed  trigger=cadence at_node=1

The memo was COMPUTED from the state at 2052.9 s and RECORDED at 2365.7 s — **256 seconds after
node 0's result was on disk** — and it opens: *"experiment #0 (deterministic-baseline-replication)
is still pending, so there are no measured results yet — the memo's 6 claims are all UNSUPPORTED
because they cite no experiment."* `agents/roles.py::_state_brief` then pushed that sentence into
every later prompt as the "Latest deep-research takeaway", directly beneath a working set showing
the result it denied. The prompt contradicted itself, and the arm answered the 0.0 with a SPEED
hypothesis over a solver its own `score.log` called 98/100 valid.

MEASURED over the thirty run dirs (`runs-B` + `model-probes` + `fullctx-probe`): **78 of 119**
completed memos (65.5 %) were appended after at least one `node_evaluated` their snapshot could not
contain — 78 results in all, on 28 of the 30 runs.

WHAT DOES NOT WORK, measured rather than assumed. §4a proposed "build the memo from state at
generation time". Over 131 `research_attempted` receipts the snapshot's own node count disagrees
with the log **0 times** — the snapshot is already fresh when the provider call STARTS, and goes
stale while it runs. Re-folding one line earlier recovers nothing. A memo cannot see the future;
what the record can do is stop presenting it as current, which is what these tests pin.
"""
from __future__ import annotations

import threading

import pytest

from looplab.agents.roles import _state_brief
from looplab.core.models import ResearchMemo
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold

_BOUND = 10.0


def _seed(store: EventStore, *, evaluated: int = 0, created: int = 1) -> None:
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    for node_id in range(created):
        store.append("node_created", {
            "node_id": node_id, "parent_ids": [], "operator": "draft",
            "idea": {"operator": "draft", "params": {}}, "code": ""})
    for node_id in range(evaluated):
        store.append("node_evaluated", {"node_id": node_id, "metric": 0.0, "eval_seconds": 0.1})


class _SlowResearcher:
    """The provider call outlives the eval — which is the entire point of overlapping the two."""

    def __init__(self, started: threading.Event, release: threading.Event):
        self.started, self.release = started, release

    def research(self, state, *, trigger: str) -> ResearchMemo:
        self.started.set()
        assert self.release.wait(_BOUND), "harness never released the provider call"
        return ResearchMemo(
            at_node=len(state.nodes), trigger=trigger,
            summary="experiment #0 is still pending, so there are no measured results yet")


def _engine(store: EventStore, researcher) -> Engine:
    eng = Engine.__new__(Engine)
    eng.store = store
    eng.deep_researcher = researcher
    eng._research_verify = False
    eng._track_hypotheses = False
    return eng


def _run_think_while(store: EventStore, eng: Engine, researcher, landing) -> None:
    """Drive one paid think and let `landing(store)` happen WHILE the provider is answering."""
    snapshot = fold(store.read_all())
    done = threading.Event()

    def _think():
        try:
            eng._research_attempt_step(snapshot, "cadence")
        finally:
            done.set()

    worker = threading.Thread(target=_think, daemon=True)
    worker.start()
    assert researcher.started.wait(_BOUND), "the provider call never started"
    landing(store)
    researcher.release.set()
    assert done.wait(_BOUND), "the think never returned"
    worker.join(timeout=_BOUND)


def _memo(store: EventStore) -> dict:
    return fold(store.read_all()).research[-1]


# ------------------------------------------------------------------ the record

def test_the_memo_records_the_result_it_could_not_see(tmp_path):
    """The specimen: node 0's result lands mid-think and the memo says so on itself."""
    store = EventStore(tmp_path / "events.jsonl")
    _seed(store)
    started, release = threading.Event(), threading.Event()
    researcher = _SlowResearcher(started, release)
    eng = _engine(store, researcher)

    _run_think_while(
        store, eng, researcher,
        lambda s: s.append("node_evaluated", {"node_id": 0, "metric": 0.0, "eval_seconds": 0.1}))

    receipt = _memo(store).get("snapshot_superseded")
    assert receipt == {"nodes": [0], "count": 1}, receipt


def test_a_think_that_superseded_nothing_leaves_the_memo_byte_identical(tmp_path):
    """The clause appears only where it is true — a serial cadence pass between nodes, which is 41
    of the corpus's 119 memos, must record exactly what it recorded before."""
    store = EventStore(tmp_path / "events.jsonl")
    _seed(store, created=1, evaluated=1)
    started, release = threading.Event(), threading.Event()
    researcher = _SlowResearcher(started, release)
    eng = _engine(store, researcher)

    _run_think_while(store, eng, researcher, lambda s: None)

    assert "snapshot_superseded" not in _memo(store)


def test_a_result_the_snapshot_ALREADY_HAD_is_not_counted_as_superseded(tmp_path):
    """The falsifier for "count every evaluated node". The receipt is the DELTA against the memo's
    own snapshot; counting the board would make every memo on a mature run claim to be stale."""
    store = EventStore(tmp_path / "events.jsonl")
    _seed(store, created=3, evaluated=2)          # nodes 0 and 1 are already in the snapshot
    started, release = threading.Event(), threading.Event()
    researcher = _SlowResearcher(started, release)
    eng = _engine(store, researcher)

    _run_think_while(
        store, eng, researcher,
        lambda s: s.append("node_evaluated", {"node_id": 2, "metric": 7.0, "eval_seconds": 0.1}))

    assert _memo(store).get("snapshot_superseded") == {"nodes": [2], "count": 1}


# ------------------------------------------------------------------ the prompt

def _takeaway(state) -> str:
    return [ln for ln in _state_brief(state, None).splitlines()
            if ln.startswith("Latest deep-research takeaway")][0]


def test_the_prompt_that_quotes_the_memo_says_it_predates_the_board(tmp_path):
    """The half that actually steered the run. Without this the same prompt carries "there are no
    measured results yet" and a working set holding the result, one above the other."""
    store = EventStore(tmp_path / "events.jsonl")
    _seed(store)
    started, release = threading.Event(), threading.Event()
    researcher = _SlowResearcher(started, release)
    eng = _engine(store, researcher)
    _run_think_while(
        store, eng, researcher,
        lambda s: s.append("node_evaluated", {"node_id": 0, "metric": 0.0, "eval_seconds": 0.1}))

    line = _takeaway(fold(store.read_all()))
    assert "WRITTEN BEFORE 1 experiment result(s) (#0) landed" in line, line
    # …and the memo's own sentence is still there, unedited: the clause annotates, it never withholds.
    assert "no measured results yet" in line, line


def test_the_historical_takeaway_line_is_unchanged_when_nothing_was_superseded(tmp_path):
    """A prompt is a contract; the new words appear only where they are the truth."""
    store = EventStore(tmp_path / "events.jsonl")
    _seed(store, created=1, evaluated=1)
    started, release = threading.Event(), threading.Event()
    researcher = _SlowResearcher(started, release)
    eng = _engine(store, researcher)
    _run_think_while(store, eng, researcher, lambda s: None)

    line = _takeaway(fold(store.read_all()))
    assert line.startswith("Latest deep-research takeaway: experiment #0 is still pending"), line


# ------------------------------------------------------------------ bounds

def test_a_hostile_receipt_cannot_spend_the_prompt():
    """The receipt is engine-derived, but it round-trips through the same sanitizer every legacy and
    tool-authored memo does, so it must be bounded there rather than at the writer."""
    from looplab.core.advisory_payloads import (memo_snapshot_cue,
                                                sanitize_research_memo_payload)

    payload = sanitize_research_memo_payload({
        "summary": "s",
        "snapshot_superseded": {"nodes": list(range(10_000)) + ["x", None], "count": 10 ** 9},
    })
    receipt = payload["snapshot_superseded"]
    assert receipt["nodes"] == list(range(8)) and receipt["count"] == 10 ** 9
    assert len(memo_snapshot_cue(payload)) < 250, memo_snapshot_cue(payload)


def test_a_malformed_receipt_reads_as_no_claim_rather_than_a_false_one():
    """Degrading to silence is the safe direction: the clause exists to stop a memo asserting more
    than it knows, so an unreadable receipt must not invent one."""
    from looplab.core.advisory_payloads import memo_snapshot_cue

    assert memo_snapshot_cue({"summary": "s"}) == ""
    assert memo_snapshot_cue({"snapshot_superseded": "not a dict"}) == ""
    assert memo_snapshot_cue({"snapshot_superseded": {"nodes": [], "count": 0}}) == ""
    assert memo_snapshot_cue(None) == ""


def test_the_count_can_exceed_the_ids_it_names():
    """A long overlap window truncates the sample and keeps the total honest -- the alternative is a
    clause that silently under-reports exactly when the memo is most stale."""
    from looplab.core.advisory_payloads import memo_snapshot_cue, snapshot_superseded_receipt

    receipt = snapshot_superseded_receipt({"nodes": list(range(20)), "count": 20})
    assert receipt == {"nodes": list(range(8)), "count": 20}
    assert "WRITTEN BEFORE 20 experiment result(s)" in memo_snapshot_cue(
        {"snapshot_superseded": receipt})
    assert ", …" in memo_snapshot_cue({"snapshot_superseded": receipt})
