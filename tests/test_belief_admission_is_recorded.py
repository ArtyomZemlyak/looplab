"""What the board did with a memo's directions must survive the console.

MEASURED on e5small-dr-unified-v12 before this shipped. To establish that the cap refused 394 of
413 proposed directions — and that all 55 requests for the seed-replicate experiment (#131) were
among them — I had to reconstruct the open board memo by memo and re-run
`classify_research_beliefs` against it by hand. None of it was in `events.jsonl`:

    research_cadence.py   verdict = classify_research_beliefs(open_statements, directions, ...)
                          if verdict.dropped:  ->  _LOG.warning(...)

The counts already existed; their only consumer was a log line. A console line is not a record —
not in the run directory, not surviving a restart, not countable across runs — and the memo
receipt (`research_completed`) is appended in a different method BEFORE the classification, so it
cannot carry the verdict. The cap value is an operator decision and this is the number it needs.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import types as _types

import pytest

from looplab.engine.research_cadence import (BeliefAdmission, ResearchCadenceMixin,
                                             classify_research_beliefs)
from looplab.events.types import (BACKGROUND_APPENDABLE, DIAGNOSTIC_EVENTS, EV_BELIEF_ADMISSION)


def _cadence(fail_append: bool = False):
    rows: list[tuple[str, dict]] = []

    def append(kind, data):
        if fail_append:
            raise RuntimeError("the store refused this row")
        rows.append((kind, data))

    return _types.SimpleNamespace(store=_types.SimpleNamespace(append=append)), rows


def test_the_verdict_reaches_the_run_log():
    cadence, rows = _cadence()
    verdict = classify_research_beliefs(["already open"], ["a", "b"])
    ResearchCadenceMixin._record_belief_admission(cadence, verdict, 2, True)
    assert len(rows) == 1
    kind, data = rows[0]
    assert kind == EV_BELIEF_ADMISSION
    assert data["proposed"] == 2
    assert data["admitted"] == len(verdict.admitted)
    assert data["board_read"] is True


def test_every_refusal_cause_is_carried_separately():
    # `reasons()` renders only the causes that fired; the ROW must carry all four, or an operator
    # cannot tell "the cap refused 394" from "the board already had them".
    cadence, rows = _cadence()
    verdict = BeliefAdmission(admitted=["kept"], blank=1, repeated=2, restated=3, capped=4)
    ResearchCadenceMixin._record_belief_admission(cadence, verdict, 11, True)
    data = rows[0][1]
    assert (data["admitted"], data["blank"], data["repeated"], data["restated"], data["capped"]) \
        == (1, 1, 2, 3, 4)
    assert data["proposed"] == 11


def test_a_memo_that_lost_nothing_is_recorded_too():
    # Not gated on `dropped`, unlike the warning beside it. A run where the cap never bites must be
    # distinguishable from a run nobody instrumented, and only a row present at 0 does that.
    cadence, rows = _cadence()
    verdict = classify_research_beliefs([], ["one", "two"])
    ResearchCadenceMixin._record_belief_admission(cadence, verdict, 2, True)
    assert len(rows) == 1
    assert rows[0][1]["capped"] == 0 and rows[0][1]["admitted"] == 2


def test_a_degraded_board_read_says_so():
    # When the board read fails the classifier sees NO open statements, so `restated`/`capped`
    # describe nothing real. The flag is what stops a later reader averaging those rows in.
    cadence, rows = _cadence()
    ResearchCadenceMixin._record_belief_admission(
        cadence, classify_research_beliefs([], ["x"]), 1, False)
    assert rows[0][1]["board_read"] is False


def test_a_store_that_refuses_the_row_never_costs_the_memo():
    # Best-effort by construction: the append is a receipt, and the memo's steering must not
    # depend on it. Without the guard this raises straight through the research task.
    cadence, _ = _cadence(fail_append=True)
    ResearchCadenceMixin._record_belief_admission(
        cadence, classify_research_beliefs([], ["x"]), 1, True)


def test_the_event_is_diagnostic_and_deliberately_not_background_appendable():
    # The two seams are not interchangeable. BACKGROUND_APPENDABLE holds FOLDED events the
    # research task may write; this row is not folded at all, and DIAGNOSTIC_EVENTS is excluded
    # wholesale from `_proposal_authority_seq` — the stronger form of the same licence.
    assert EV_BELIEF_ADMISSION in DIAGNOSTIC_EVENTS
    assert EV_BELIEF_ADMISSION not in BACKGROUND_APPENDABLE


def test_the_classification_site_actually_calls_it():
    # A receipt nobody writes is the defect one level up — the same shape as a health snapshot
    # nobody reads. AST, not a substring: a call named in a comment must not satisfy this.
    src = pathlib.Path(inspect.getsourcefile(ResearchCadenceMixin)).read_text()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "_record_belief_admission"]
    assert calls, "the classification site does not record its verdict"

    classifications = [n for n in ast.walk(tree)
                       if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                       and n.func.id == "classify_research_beliefs"]
    assert classifications, "classify_research_beliefs is no longer called there"
    # And the receipt must follow the classification it describes.
    assert min(c.lineno for c in calls) > min(c.lineno for c in classifications)
