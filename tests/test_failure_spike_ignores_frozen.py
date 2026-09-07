"""A speculative build the ENGINE froze is not a failure of the experiment — deliberately.

The failure-spike filter and `serve/attention.py`'s owner-alert filter answer one question ("this
node ended for a reason that says nothing about the experiment") and were hand-written twice. The
unification onto `core/models.py::BENIGN_TERMINAL_REASONS` justified itself by the dead word
`cancelled`, which no terminal writer mints — true, and only half of what it did: `attention.py`
also carried `frozen` and the fold did not, so the shared set ADDED a live reason to the fold.

Keeping it is right. `frozen` is minted by `engine/speculation.py::_fail_reserved_build` when a
speculative build is terminalized by a transient pause/stop/budget crossing — the engine's own
doing, at a moment the run is already stopping. Counting that toward the consecutive-failure breaker
means the engine's response to a pause is to conclude the run is failing.

What is NOT free is that this changes FOLDED state on every preserved log carrying such a terminal:
`current_failure_count`, `failure_spike_level` and `failure_spike_seq` all move. All three are
`Field(exclude=True)`, so a corpus check that digests `model_dump()` is blind to it — which is why
it needs a test of its own rather than a line in a docstring.
"""
from __future__ import annotations

from looplab.core.models import BENIGN_TERMINAL_REASONS, NodeStatus
from looplab.events.replay import _FAILURE_SPIKE_IGNORED_REASONS, fold
from looplab.events.eventstore import EventStore


def _log(tmp_path, terminals):
    store = EventStore(tmp_path / "events.jsonl")
    for nid, reason in enumerate(terminals):
        store.append("node_created", {
            "node_id": nid, "parent_ids": [], "operator": "draft",
            "idea": {"operator": "draft", "params": {}, "rationale": "r"}, "code": "print(1)"})
        store.append("node_failed", {"node_id": nid, "generation": 0, "reason": reason})
    return fold(store.read_all())


def test_a_frozen_speculative_build_does_not_count_toward_the_spike(tmp_path):
    """MUTATION: `BENIGN_TERMINAL_REASONS - {"frozen"}` here -> the count is 3 and a run that was
    merely paused starts tripping its own consecutive-failure breaker."""
    state = _log(tmp_path, ["crash", "frozen", "crash"])
    assert state.current_failure_count == 2, (
        "the two real crashes count; the build the engine itself froze does not")


def test_the_two_readers_of_this_judgement_agree_exactly(tmp_path):
    """They were written twice and drifted; the point of deriving both from one set is that they
    cannot again. A reader that needs a DIFFERENT set says so at its own site and explains why —
    neither of these does."""
    from looplab.serve import attention

    assert _FAILURE_SPIKE_IGNORED_REASONS == set(BENIGN_TERMINAL_REASONS)
    assert "frozen" in BENIGN_TERMINAL_REASONS, (
        "the reason the unification actually moved — see the note at the fold's set")
    assert "cancelled" not in BENIGN_TERMINAL_REASONS, (
        "the word both copies carried and no terminal writer mints")
    assert attention.BENIGN_TERMINAL_REASONS is BENIGN_TERMINAL_REASONS


def test_an_ordinary_failure_still_counts(tmp_path):
    """The non-vacuous half: the filter must not have swallowed the signal it exists to pass."""
    state = _log(tmp_path, ["crash", "no_metric", "timeout"])
    assert state.current_failure_count == 3
    for reason in BENIGN_TERMINAL_REASONS:
        assert _log(tmp_path / reason, [reason]).current_failure_count == 0, reason


def test_a_frozen_build_is_a_reason_the_engine_really_mints():
    """A registered word nothing writes is the `cancelled` defect again. This one has a writer."""
    import inspect

    from looplab.engine import speculation

    assert '"frozen"' in inspect.getsource(speculation), (
        "`_fail_reserved_build` is the writer; if it stops being, this membership is dead weight")
