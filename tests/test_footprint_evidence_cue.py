"""What this run's own nodes asked for — the evidence the GPU BUDGET cue never carried.

The budget paragraph told the Researcher the pool and the per-experiment ceiling and never what the
run's existing nodes RAN on. Measured on `runs/e5small-dr-unified-v4`: the Researcher wrote 123
cards proposing to extend node #3's recipe and declared `{"gpus": 2}` on every one, while node #3
was created with `{"gpus": 1}` and ran on a single card. The widest of those declarations settled
the run's width to 1, so one card carried the work and the other idled for a nine-hour evaluation.

Evidence, NOT a clamp. One of those cards justified itself — "using 2 GPUs to halve wall-clock" —
which is a real reason for a 30-epoch run. The scheduler-side answer to an idle card is backfill;
this note only makes the declaration an informed one.
"""
from looplab.engine.proposal_cues import ProposalCuesMixin


class _Event:
    def __init__(self, type_, data): self.type, self.data = type_, data


class _Store:
    def __init__(self, events): self._events = events
    def read_all(self): return self._events


class _Probe(ProposalCuesMixin):
    def __init__(self, events): self.store = _Store(events)


def _node(nid, gpus):
    idea = {"name": "n", "rationale": "r", "params": {}}
    if gpus is not None:
        idea["footprint"] = {"gpus": gpus}
    return _Event("node_created", {"node_id": nid, "idea": idea})


def test_it_reports_what_the_run_s_nodes_declared():
    note = _Probe([_node(0, 1), _node(1, 1), _node(2, 2)])._observed_footprint_note()
    assert "2 node(s) on 1 GPU(s)" in note
    assert "1 node(s) on 2 GPU(s)" in note


def test_a_run_with_no_evidence_says_nothing():
    """Silence beats a default. A run with no node yet has nothing to report, and inventing one
    would be exactly the guess this note exists to replace."""
    assert _Probe([])._observed_footprint_note() == ""
    assert _Probe([_node(0, None)])._observed_footprint_note() == ""


def test_a_node_counts_once_however_many_times_it_was_created():
    """`node_created` repeats across resets and re-runs; a node that was rebuilt three times is
    still ONE node's worth of evidence, not three."""
    note = _Probe([_node(0, 1), _node(0, 1), _node(0, 1)])._observed_footprint_note()
    assert "1 node(s) on 1 GPU(s)" in note


def test_the_latest_creation_of_a_node_wins():
    """A node re-created with a different footprint reports the CURRENT one — the record's last
    word about it, which is what the next proposal will actually be compared against."""
    note = _Probe([_node(0, 2), _node(0, 1)])._observed_footprint_note()
    assert "1 node(s) on 1 GPU(s)" in note and "2 GPU(s)" not in note


def test_malformed_footprints_are_ignored_rather_than_counted():
    note = _Probe([_node(0, 1), _Event("node_created", {"node_id": 1, "idea": {"footprint": {"gpus": "two"}}}),
                   _Event("node_created", {"node_id": 2}),
                   _Event("card_added", {"id": "c1"})])._observed_footprint_note()
    assert "1 node(s) on 1 GPU(s)" in note


def test_it_never_fails_the_build_it_decorates():
    """A prompt cue may not take down a proposal. An unreadable store answers empty."""
    class _Broken:
        def read_all(self): raise RuntimeError("log unreadable")
    probe = ProposalCuesMixin.__new__(ProposalCuesMixin)
    probe.store = _Broken()
    assert probe._observed_footprint_note() == ""


def test_it_names_the_consequence_not_just_the_count():
    """The number alone does not tell the Researcher why it matters: the widest OPEN declaration is
    what sets the run's width, which is the mechanism that idled a card."""
    note = _Probe([_node(0, 1)])._observed_footprint_note()
    assert "width" in note and "legitimate choice" in note
