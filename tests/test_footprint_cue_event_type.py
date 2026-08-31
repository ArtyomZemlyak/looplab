"""The observed-footprint cue matches the event type by CONSTANT, and its fixture proves it.

`Engine._observed_footprint_note` tells the Researcher what GPU footprints its own predecessors
actually used. It scans the raw event list for `node_created` rows, and it compared the type against
a BARE STRING — CLAUDE.md trap #7, "a typo'd literal silently no-ops".

WHY THIS ONE IS WORSE THAN THE USUAL SHAPE: the cue answers "" on any mismatch by design (silent on a
run with no evaluated node yet), so a drifted spelling does not raise, does not log, and does not
change a single test — it just restores the pre-cue prompt. And the obvious guard cannot see it
either: a fixture that fabricates its rows with the same literal drifts WITH the code.

So these tests build every row from `EV_NODE_CREATED` itself. If the production comparison drifts to
any other spelling, the cue stops seeing rows the registry says are node creations and the evidence
sentence disappears — which is what the first assertion catches.

Every assertion has an input that makes it FAIL; the mutations are named.
"""
from __future__ import annotations

from types import SimpleNamespace

from looplab.events.types import EV_NODE_CREATED


def _row(node_id, gpus, event_type=EV_NODE_CREATED):
    return SimpleNamespace(type=event_type,
                           data={"node_id": node_id,
                                 "idea": {"footprint": {"gpus": gpus}}})


class _Engine:
    """The narrowest host the cue needs: it reads `self.store.read_all()` and nothing else."""

    def __init__(self, rows):
        self.store = SimpleNamespace(read_all=lambda: rows)

    note = None      # bound below from the real mixin


def _note(rows) -> str:
    from looplab.engine.proposal_cues import ProposalCuesMixin

    host = _Engine(rows)
    return ProposalCuesMixin._observed_footprint_note(host)


def test_the_cue_SEES_rows_built_from_the_registry_constant():
    """The whole point of the fixture. Mutation: compare the event type against any other spelling
    (`"node_created "`, `"nodecreated"`, the old bare literal renamed) and the cue sees nothing and
    silently answers "", restoring the pre-cue prompt with no test red anywhere else."""
    text = _note([_row(0, 1), _row(1, 1), _row(2, 2)])
    assert text, ("the cue must report what predecessors actually used; an empty answer here means "
                  "it matched no node_created row at all")
    assert "1" in text and "2" in text, f"both observed footprints must appear, got {text!r}"


def test_a_row_of_ANOTHER_type_is_not_counted():
    """The complement — the match must still be a match. Mutation: drop the type test entirely and
    every event with an `idea.footprint` counts, including a card_added that was never built."""
    assert _note([_row(0, 4, event_type="card_added")]) == ""


def test_no_node_created_row_stays_SILENT():
    """Silence is the designed answer before any node exists, and it is exactly why a drifted
    literal is invisible. Mutation: invent a default footprint and the cue starts asserting a fact
    about a run that has produced no evidence."""
    assert _note([]) == ""


def test_a_malformed_footprint_is_skipped_rather_than_guessed():
    """Mutation: accept a non-int `gpus` and a model-authored "2" or True lands in the evidence the
    Researcher is told its predecessors used."""
    assert _note([_row(0, "2"), _row(1, True), _row(2, None)]) == ""


def test_the_comparison_names_the_CONSTANT_and_not_the_spelling():
    """The one property no behavioural test can reach, and the mutation run is what proved that.

    Replacing `EV_NODE_CREATED` with the literal `"node_created"` SURVIVED every test above — it had
    to, because the two are equal today. The bare literal is not wrong now; it is wrong the day the
    registry spelling moves, and then this cue silently answers "" forever (it is designed to be
    silent when it matches nothing). A behavioural test cannot see a refactor-equivalent, so the
    guard here is a NEGATIVE source pin, which CLAUDE.md keeps as substrings on purpose: what must
    not come back is the TEXT.

    The DRIFT itself — any other spelling — is caught behaviourally by the first test in this file.
    """
    import inspect

    from looplab.engine import proposal_cues

    body = inspect.getsource(proposal_cues.ProposalCuesMixin._observed_footprint_note)
    assert '"node_created"' not in body, (
        "match the registry constant, or a renamed event type leaves this cue matching nothing "
        "with no test red anywhere (CLAUDE.md trap #7)")
    assert "EV_NODE_CREATED" in body
