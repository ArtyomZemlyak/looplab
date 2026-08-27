"""A memo that fills only `open_questions` must still reach the board.

The deep-research memo grew a QUESTION/EXPERIMENT split: `open_questions` is what becomes a board
row, and `recommended_directions` stayed behind as the legacy hint projection. But both appends sat
under one `if directions:` gate, so a schema-valid memo that filled the new field and left the old
one empty — it is optional, defaults to `[]`, and the prompt asks for it only as a redundant union
of the two new lists — suppressed EVERY `EV_HYPOTHESIS_ADDED` append. The run paid for a think-hard
deep-research pass and its whole board output was discarded on delivery.

Driven through the real `_record_deep_research` rather than asserted about the source: the defect was
a control-flow coupling between two channels, and the only thing that shows it is counting the rows
that actually land in the log.
"""
from __future__ import annotations

import pathlib
import tempfile

from looplab.core.models import ResearchMemo
from looplab.events.types import EV_HINT, EV_HYPOTHESIS_ADDED
from tests.factories import make_engine


def _deliver(**memo_kwargs) -> tuple[int, int]:
    """Record one memo and return (hypothesis_added rows, hint rows)."""
    engine = make_engine(pathlib.Path(tempfile.mkdtemp()) / "run")
    engine.store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "max"})
    engine._record_deep_research(
        ResearchMemo(summary="s", at_node=0, **memo_kwargs), trigger="cadence", manual=False)
    types = [event.type for event in engine.store.read_all()]
    return types.count(EV_HYPOTHESIS_ADDED), types.count(EV_HINT)


def test_questions_alone_still_register_on_the_board():
    """THE DEFECT. Before the split of the two gates this was (0, 0)."""
    registered, hints = _deliver(open_questions=[
        "does distilling from a stronger teacher help here",
        "is the in-batch negative pool the bottleneck",
    ])
    assert registered == 2, (
        "MUTATION: put the hypothesis append back under `if directions:` and this is 0 — the "
        "board stays empty for a memo that answered exactly what it was asked for")
    assert hints == 0, (
        "and NO legacy hint: that projection is keyed on `recommended_directions` for replay "
        "compatibility, and widening it would put new text into old logs' channel for no reader")


def test_a_legacy_memo_folds_exactly_as_it_did():
    """The compatibility half. `questions` falls back to `directions` when a memo drew no
    distinction, so every log already on disk delivers byte-identically."""
    assert _deliver(recommended_directions=["try a bigger batch", "raise the temperature"]) == (2, 1)


def test_a_split_memo_registers_the_questions_and_hints_the_experiments():
    """Both filled: the question becomes the board row, the experiment stays a hint. The fallback
    must not fire here — registering the union would put an unbuildable single-change experiment on
    the board, which is what the split exists to stop."""
    assert _deliver(open_questions=["does a stronger teacher help"],
                    recommended_directions=["try a bigger batch"]) == (1, 1)


def test_an_empty_memo_appends_neither():
    assert _deliver() == (0, 0)
