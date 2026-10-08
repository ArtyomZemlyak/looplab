"""`card_filed`: the operator files an experiment under a research question — on the record.

THE GAP, measured on `minionerec-backbones-v11` (2026-10-08): 45 experiment cards, 39 of them
operator injects, and THREE carried a `parent_card_id` — every inject path (`ll_inject.py`, the UI
form, the Assistant tool) names none and nothing after the mint could add one; `core/cards.py` said
so outright ("THERE IS NO CORRECTION PATH YET"). The Research view since `e41329bb` DRAWS most of
them under a question by concept overlap, display only — but the run's own record (the question
board the Researcher reads, the open-question cap's "taken up" test, the questions' rollups) still
saw 42 unfiled experiments and 20 of 23 questions unanswered, which is exactly the cap that then
refused 32 deep-research questions.

What is pinned: the operator's filing replaces the authored edge (and un-filing clears it), last
write wins, only an operator-stamped row counts, the forest refusals still apply, a filed question
stops occupying an unanswered cap slot, and the control intake refuses what the fold would ignore.
"""
from __future__ import annotations

import pytest

from looplab.core.models import Event, Idea
from looplab.events.replay import fold
from looplab.events.types import EV_CARD_FILED


def _log(*, authored: str | None = None):
    """Two questions and one experiment; `authored` = the edge the proposal itself wrote."""
    idea = Idea(operator="draft", hypothesis="a concrete experiment",
                **({"parent_card_id": authored} if authored else {}))
    return [
        Event(seq=0, ts=0.0, type="run_started",
              data={"run_id": "r", "task_id": "t", "direction": "max"}),
        Event(seq=1, ts=0.0, type="hypothesis_added",
              data={"statement": "does x help?", "source": "deep_research"}),
        Event(seq=2, ts=0.0, type="hypothesis_added",
              data={"statement": "does y help?", "source": "deep_research"}),
        Event(seq=3, ts=0.0, type="node_created",
              data={"node_id": 0, "generation": 0, "operator": "draft", "parent_ids": [],
                    "idea": idea.model_dump(mode="json")}),
    ]


def _ids(st):
    questions = sorted(cid for cid, c in st.cards.items() if c.card_kind == "direction")
    experiment = next(cid for cid, c in st.cards.items() if c.card_kind == "experiment")
    return questions, experiment


def _filed(seq, card_id, parent, **extra):
    return Event(seq=seq, ts=0.0, type=EV_CARD_FILED,
                 data={"id": card_id, "parent_card_id": parent, "source": "operator", **extra})


def test_an_operator_filing_puts_the_experiment_under_the_question_and_says_who():
    base = _log()
    (qx, qy), exp = _ids(fold(base))
    st = fold(base + [_filed(4, exp, qx)])
    card = st.cards[exp]
    assert card.parent_card_id == qx, (
        "MUTATION: drop the `card_filings` loop in `_apply_card_operator_overlays` and this is None "
        "— the operator's decision never reaches the board")
    assert (card.filed_by, card.filed_seq) == ("operator", 4)
    assert st.cards[qx].child_card_ids == [exp] and st.cards[qx].child_rollup["children"] == 1, (
        "the question's rollup must see the filed experiment — it is a real edge, not a display hint")


def test_last_write_wins_and_an_un_filing_clears_even_an_AUTHORED_edge():
    base = _log()
    (qx, qy), exp = _ids(fold(base))
    assert fold(base + [_filed(4, exp, qx), _filed(5, exp, qy)]).cards[exp].parent_card_id == qy
    # The proposal authored qx; the operator corrects it — the correction path the Card lacked.
    authored = _log(authored=qx)
    assert fold(authored).cards[exp].parent_card_id == qx, "precondition: the authored edge lands"
    st = fold(authored + [_filed(4, exp, None)])
    assert st.cards[exp].parent_card_id is None and st.cards[exp].filed_by == "operator", (
        "an operator's 'not under any question' must be distinguishable from nobody having looked — "
        "the Research view keys its refusal to re-infer a concept filing on `filed_by`")
    assert fold(authored + [_filed(4, exp, qy)]).cards[exp].parent_card_id == qy


@pytest.mark.parametrize("data", [
    {"source": "engine"},                       # not the operator's decision
    {"source": None},
    {"drop_parent_key": True},                  # neither a question nor an explicit null
    {"parent_card_id": 7},                      # not a card id
])
def test_rows_that_are_not_an_operator_filing_are_ignored(data):
    base = _log()
    (qx, _qy), exp = _ids(fold(base))
    row = {"id": exp, "parent_card_id": qx, "source": "operator"}
    row.update({k: v for k, v in data.items() if k != "drop_parent_key"})
    if data.get("drop_parent_key"):
        row.pop("parent_card_id")
    st = fold(base + [Event(seq=4, ts=0.0, type=EV_CARD_FILED, data=row)])
    assert st.cards[exp].parent_card_id is None and st.cards[exp].filed_by is None
    assert st.card_filings == {}


def test_the_forest_refusals_apply_to_an_operator_filing_too():
    """A filing naming a card that does not exist is refused by `_apply_card_lineage`, like any
    authored edge — written BEFORE that phase so there is one set of refusals, not two."""
    base = _log()
    _qs, exp = _ids(fold(base))
    st = fold(base + [_filed(4, exp, "no-such-question")])
    assert st.cards[exp].parent_card_id is None


def test_a_filed_question_stops_occupying_an_unanswered_cap_slot():
    """The engine-side consequence the display inference could never have: the open-question cap
    counts CHILDLESS questions (`research_cadence.py::open_belief_populations`)."""
    from looplab.engine.research_cadence import open_belief_populations

    base = _log()
    (qx, _qy), exp = _ids(fold(base))
    _open, unanswered = open_belief_populations(fold(base))
    assert len(unanswered) == 2
    _open, unanswered = open_belief_populations(fold(base + [_filed(4, exp, qx)]))
    assert unanswered == ["does y help?"], "the question the operator filed work under is taken up"


def test_a_log_without_a_filing_folds_exactly_as_before():
    base = _log(authored=None)
    st = fold(base)
    assert st.card_filings == {}
    assert all(c.filed_by is None and c.filed_seq is None for c in st.cards.values())


def test_the_wire_publishes_who_filed_and_keeps_the_journal_internal():
    from looplab.serve.public_cards import INTERNAL_CARD_STATE_FIELDS, public_cards

    base = _log()
    (qx, _qy), exp = _ids(fold(base))
    st = fold(base + [_filed(4, exp, qx)])
    wire = public_cards(st.cards)[exp]
    assert (wire["parent_card_id"], wire["filed_by"], wire["filed_seq"]) == (qx, "operator", 4)
    assert "card_filings" in INTERNAL_CARD_STATE_FIELDS


# ------------------------------------------------------------------------- the control intake

class _Ctx:
    def __init__(self, st, data):
        self._st, self.data = st, data

    def state(self):
        return self._st

    def card(self):
        cid = self.data["id"]
        return cid, self._st.cards[cid]

    def text(self, name, *, required=True, limit=20_000):
        value = self.data.get(name)
        return value.strip() if isinstance(value, str) else None


def test_the_intake_stamps_the_operator_and_refuses_what_the_fold_would_not_honour():
    from fastapi import HTTPException

    from looplab.serve.control_validation import _normalize_card_filed, _precondition_card

    base = _log()
    st = fold(base)
    (qx, qy), exp = _ids(st)
    out = _normalize_card_filed(_Ctx(st, {"id": exp, "parent_card_id": qx}))
    assert out == {"id": exp, "parent_card_id": qx, "source": "operator"}

    def refused(data, state=st):
        with pytest.raises(HTTPException) as err:
            _normalize_card_filed(_Ctx(state, data))
        return err.value.status_code, (err.value.detail or {}).get("code") if isinstance(
            err.value.detail, dict) else err.value.detail

    assert refused({"id": exp})[0] == 400, "an absent key is malformed, never an un-filing"
    assert refused({"id": exp, "parent_card_id": "nope"}) == (400, "card_filing_target_invalid")
    assert refused({"id": exp, "parent_card_id": exp}) == (400, "card_filing_target_invalid"), (
        "an experiment is not a question")
    assert refused({"id": qx, "parent_card_id": qy}) == (400, "card_filing_not_experiment")
    assert refused({"id": exp, "parent_card_id": None}) == (409, "card_filing_unchanged"), (
        "nothing to un-file")
    filed = fold(base + [_filed(4, exp, qx)])
    assert refused({"id": exp, "parent_card_id": qx}, filed) == (409, "card_filing_unchanged")
    assert _normalize_card_filed(_Ctx(filed, {"id": exp, "parent_card_id": None}))[
        "parent_card_id"] is None

    # Append-time recheck on a fresh fold: the same rule.
    assert _precondition_card(filed, EV_CARD_FILED,
                              {"id": exp, "parent_card_id": qx}, None)["code"] == "card_filing_unchanged"
    assert _precondition_card(st, EV_CARD_FILED, {"id": exp, "parent_card_id": qx}, None) is None


def test_the_real_intake_closes_the_payload_and_null_un_files(tmp_path):
    """Through `normalize_control` (the allow-list + `_ControlIntake`), not a stand-in context."""
    from fastapi import HTTPException

    from looplab.events.eventstore import EventStore
    from looplab.serve.control_validation import CONTROL_SPECS, EnginePolicy, normalize_control
    from looplab.serve.protocol import COLLABORATION_EVENTS

    rd = tmp_path / "run"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    for ev in _log():
        store.append(ev.type, ev.data)

    class _Srv:
        def state(self, rd):
            return fold(EventStore(rd / "events.jsonl").read_all())

    st = _Srv().state(rd)
    (qx, _qy), exp = _ids(st)
    assert EV_CARD_FILED in COLLABORATION_EVENTS, "command-only and generation-fenced"
    assert CONTROL_SPECS[EV_CARD_FILED].engine_policy is EnginePolicy.NO_SPAWN
    assert normalize_control(_Srv(), rd, EV_CARD_FILED, {"id": exp, "parent_card_id": qx}) == {
        "id": exp, "parent_card_id": qx, "source": "operator"}
    with pytest.raises(HTTPException) as forged:
        normalize_control(_Srv(), rd, EV_CARD_FILED,
                          {"id": exp, "parent_card_id": qx, "source": "engine"})
    assert forged.value.status_code == 400, "provenance is stamped, never accepted"
    store.append(EV_CARD_FILED, {"id": exp, "parent_card_id": qx, "source": "operator"})
    assert normalize_control(_Srv(), rd, EV_CARD_FILED, {"id": exp, "parent_card_id": None})[
        "parent_card_id"] is None
