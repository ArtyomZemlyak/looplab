"""The Researcher's OWN noticed-but-unpursued questions reach the open board.

THE DEFECT (doc 27 / the marker on `core/models.py::Idea.open_questions`). The carrier shipped and
nothing read it: `open_questions` (with the positional `question_concepts` beside it) rode
`durable_idea_payload` -> `node_created` -> `Idea(**d["idea"])` intact — `tests/test_open_questions_
ask.py` pins that crossing — and then stopped. No engine path turned one into a board row, so a
Researcher that answered the ask got a field in an event and no consequence: the "stamped and
nothing consumes it" shape this repo has paid for before.

DRIVEN, NOT PINNED (CLAUDE.md tier 1). Every test below writes real `node_created` rows to a real
`EventStore`, folds the real log, calls the real method and re-folds — so the assertions are about
what a run's board actually contains, and a change that stops splicing the read fails here whatever
constant it stops splicing. The one double is the Engine itself (`SimpleNamespace` + bound mixin
methods, the harness `tests/test_direction_board_cap.py` established), because `self` on a mixin is
the Engine and this method reads exactly two things off it.
"""
from __future__ import annotations

import types

from looplab.core.models import (Card, CardSelectionProvenance, Idea, RunState,
                                 durable_idea_payload)
from looplab.engine import research_cadence as rc
from looplab.engine.research_cadence import (DEEP_RESEARCH_OPEN_BELIEF_CAP,
                                             idea_registered_questions,
                                             open_belief_populations)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def _engine(store, *, track: bool = True):
    """The Engine as this method sees it: a store and `_track_hypotheses`, nothing else."""
    engine = types.SimpleNamespace(store=store, _track_hypotheses=track)
    engine._register_idea_questions = types.MethodType(
        rc.ResearchCadenceMixin._register_idea_questions, engine)
    return engine


def _run(tmp_path, name="events.jsonl"):
    store = EventStore(tmp_path / name)
    store.append("run_started", {"goal": "g", "direction": "max"})
    return store


def _propose(store, node_id, questions, concepts=None, *, operator="draft"):
    """One real `node_created` row whose idea carries the questions — the durable payload the
    Researcher's own emission produces, not a hand-built dict."""
    idea = Idea(operator=operator, params={"x": float(node_id)}, rationale="r",
                open_questions=list(questions),
                question_concepts=[list(c) for c in (concepts or [])])
    store.append("node_created", {"node_id": node_id, "parent_ids": [], "operator": operator,
                                  "idea": durable_idea_payload(idea), "code": "print(1)",
                                  "files": {}})


def _board_statements(store):
    state = fold(store.read_all())
    return {c.seed_statement for c in state.cards.values()}


def _rows(store, etype="hypothesis_added"):
    return [e.data for e in store.read_all() if e.type == etype]


# ------------------------------------------------------------------ the pure read of the carrier


def test_the_carrier_read_joins_each_question_to_its_own_concept_row():
    """POSITIONAL, through the ONE shared `question_concept_rows`: a join spelled a second time here
    files a question under a concept set belonging to a different question. Mutation: filter the
    blanks BEFORE reading the index and `q3` inherits `q1`'s concepts."""
    state = RunState(goal="g", direction="max")
    state.nodes = {0: types.SimpleNamespace(idea=Idea(
        operator="draft", params={}, rationale="r",
        open_questions=["q1", "", "q3"],
        question_concepts=[["loss/contrastive"], ["training/negative-mining"], ["data/dedup"]]))}
    assert idea_registered_questions(state) == [
        (0, "q1", ["loss/contrastive"]), (0, "q3", ["data/dedup"])]


def test_the_carrier_read_is_in_node_order_and_keeps_a_question_with_no_concepts():
    state = RunState(goal="g", direction="max")
    state.nodes = {
        2: types.SimpleNamespace(idea=Idea(operator="draft", params={}, rationale="r",
                                           open_questions=["later"])),
        0: types.SimpleNamespace(idea=Idea(operator="draft", params={}, rationale="r",
                                           open_questions=["earlier"])),
        1: types.SimpleNamespace(idea=None),
    }
    assert idea_registered_questions(state) == [(0, "earlier", []), (2, "later", [])]


def test_a_node_with_no_questions_or_no_idea_contributes_nothing():
    state = RunState(goal="g", direction="max")
    state.nodes = {0: types.SimpleNamespace(idea=Idea(operator="draft", params={}, rationale="r"))}
    assert idea_registered_questions(state) == []
    assert idea_registered_questions(RunState(goal="g", direction="max")) == []


# ------------------------------------------------------------------ the writer, end to end


def test_a_registered_question_becomes_a_board_row(tmp_path):
    """THE DEFECT ITSELF: before this, the question rode `node_created` and became no board row.

    Mutation: delete the `_register_idea_questions` call (or the append inside it) and the board is
    empty while the question sits in the log with nothing reading it — which is exactly the state
    the marker described.
    """
    store = _run(tmp_path)
    _propose(store, 0, ["does a stronger teacher help at this batch size?"],
             [["loss/distillation"]])

    engine = _engine(store)
    state = engine._register_idea_questions(fold(store.read_all()))

    assert _board_statements(store) == {"does a stronger teacher help at this batch size?"}
    assert {c.seed_statement for c in state.cards.values()} == _board_statements(store), (
        "the method returns the RE-FOLDED state when it wrote — a caller handed the stale one "
        "would decide the rest of its iteration against a board missing what it just added")
    row = _rows(store)[0]
    assert row["source"] == "researcher", (
        "the CHANNEL is what separates a question the proposal noticed from a deep-research "
        "recommended direction; both writers fill the same board")
    assert row["at_node"] == 0
    assert row["concepts"] == ["loss/distillation"], (
        "a question registered with no concept membership makes the concept hierarchy and the "
        "question board disjoint taxonomies over one run")


def test_a_question_with_no_concepts_is_registered_without_the_key(tmp_path):
    """Absent leaves the key OUT entirely — an empty list is an authored claim of 'no concepts',
    which is a different statement from 'this writer said nothing'."""
    store = _run(tmp_path)
    _propose(store, 0, ["is recall@100 saturated on this corpus?"])
    _engine(store)._register_idea_questions(fold(store.read_all()))
    assert "concepts" not in _rows(store)[0]
    assert _board_statements(store) == {"is recall@100 saturated on this corpus?"}


def test_running_the_sweep_again_registers_nothing_more(tmp_path):
    """REPLAY-SAFE WITH NO COUNTER PAIR: the gate is the BOARD. A question that reached it is
    refused as `restated` by the same rule that makes a re-run memo idempotent — including across a
    fresh process, which is what a resume is.

    Mutation: give the sweep a private dedup instead of `open_belief_populations` and this passes
    in-process while a resumed run doubles every row.
    """
    store = _run(tmp_path)
    _propose(store, 0, ["does a stronger teacher help?"])
    _engine(store)._register_idea_questions(fold(store.read_all()))
    assert len(_rows(store)) == 1

    for _ in range(3):
        _engine(store)._register_idea_questions(fold(store.read_all()))
    assert len(_rows(store)) == 1, "a second offer of an open question is a restatement"

    # …and a genuinely new question from a LATER node still lands, or "idempotent" would just mean
    # "the sweep stopped working after the first node".
    _propose(store, 1, ["does a stronger teacher help?", "is the corpus deduped?"])
    _engine(store)._register_idea_questions(fold(store.read_all()))
    assert {r["statement"] for r in _rows(store)} == {"does a stronger teacher help?",
                                                      "is the corpus deduped?"}
    assert [r["at_node"] for r in _rows(store)] == [0, 1]


def test_the_same_question_from_two_nodes_is_one_row_at_the_FIRST_asker(tmp_path):
    """Two ideas noticing the same thing is one question. `normalized_belief_key` decides what "the
    same" means (case and spacing are not semantics), and the row names where it was first seen."""
    store = _run(tmp_path)
    _propose(store, 0, ["Is the corpus deduped?"])
    _propose(store, 1, ["  is the corpus   DEDUPED?  "])
    _engine(store)._register_idea_questions(fold(store.read_all()))
    rows = _rows(store)
    assert len(rows) == 1 and rows[0]["at_node"] == 0
    assert rows[0]["statement"] == "Is the corpus deduped?"


def test_a_question_restating_an_OPEN_DIRECTION_is_refused(tmp_path):
    """The two writers share one dedup universe, so the Researcher cannot open a second row for a
    question deep research already put on the board."""
    store = _run(tmp_path)
    store.append("hypothesis_added", {"statement": "does a stronger teacher help?",
                                      "source": "deep_research", "at_node": 0})
    _propose(store, 0, ["Does a stronger teacher help?"])
    _engine(store)._register_idea_questions(fold(store.read_all()))
    assert [r["source"] for r in _rows(store)] == ["deep_research"]


def _direction_card(cid, statement, **kw):
    return Card(id=cid, statement=statement, seed_statement=statement,
                selection_provenance=CardSelectionProvenance(), **kw)


def _asking(questions, cards):
    """A folded board built directly, the `tests/test_direction_board_cap.py` shape.

    The BOARD is hand-built here and nowhere else in this file, because minting a work-item child
    through the log needs an ownership receipt this test has no opinion about — and the property
    under test is the cap, not the mint. The questions still arrive on a real `Idea`.
    """
    state = RunState(goal="g", direction="max")
    state.cards = {c.id: c for c in cards}
    state.nodes = {0: types.SimpleNamespace(idea=Idea(
        operator="draft", params={}, rationale="r", open_questions=list(questions)))}
    return state


def test_the_cap_binds_and_a_taken_up_direction_frees_room(tmp_path):
    """The SAME cap and the SAME two populations as the memo path — driven here rather than
    asserted, because a private cap would bound a different board."""
    full = [_direction_card(f"d{i}", f"direction {i}")
            for i in range(DEEP_RESEARCH_OPEN_BELIEF_CAP)]
    store = _run(tmp_path)
    _engine(store)._register_idea_questions(_asking(["a question with no room"], full))
    assert _rows(store) == [], "the cap binds"

    # A refused question is RE-OFFERED, so the moment a direction is taken up the room it frees is
    # room this sweep can use — the memo path gets one attempt per memo, this one gets every
    # iteration, and that difference is the whole reason it does not write a receipt.
    child = Card(id="child-1", statement="a concrete experiment",
                 seed_statement="a concrete experiment", parent_card_id="d0",
                 selection_provenance=CardSelectionProvenance(
                     action_source="card_added", action_owner_count=1))
    _engine(store)._register_idea_questions(_asking(["a question with no room"], full + [child]))
    assert [r["statement"] for r in _rows(store)] == ["a question with no room"], (
        "a direction somebody is already answering stops competing for board room, and the "
        "re-offer is what lets this sweep use it")


def test_track_hypotheses_off_writes_nothing(tmp_path):
    store = _run(tmp_path)
    _propose(store, 0, ["a question"])
    _engine(store, track=False)._register_idea_questions(fold(store.read_all()))
    assert _rows(store) == []


def test_a_run_with_no_questions_never_touches_the_store(tmp_path):
    """The steady state on essentially every run today (0 of 155 `node_created` rows carried one):
    the sweep must cost a fold it already has and nothing else — no append, and no `read_all()` for
    a no-op."""
    store = _run(tmp_path)
    _propose(store, 0, [])
    state = fold(store.read_all())
    reads = []
    watched = types.SimpleNamespace(
        read_all=lambda: (reads.append(1), store.read_all())[1],
        append=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("appended for a run with no "
                                                                    "questions")))
    out = _engine(watched)._register_idea_questions(state)
    assert out is state and reads == []


def test_the_board_is_read_off_the_STATE_HANDED_IN_and_never_re_folded(tmp_path):
    """`_admissible_beliefs` folds the log itself and therefore contains that read; this sweep is
    handed the fold its caller already holds, so it has no I/O to contain and none to pay for.

    Mutation: re-fold here and every loop iteration buys a second `read_all()` of a multi-megabyte
    log — and worse, the two decisions in one iteration read two different boards, which is the
    exact defect `test_ONE_FOLD_PER_MEMO_and_both_decisions_read_the_SAME_board` was written for.
    """
    store = _run(tmp_path)
    _propose(store, 0, ["a question"])
    state = fold(store.read_all())

    reads = []
    watched = types.SimpleNamespace(read_all=lambda: (reads.append(1), store.read_all())[1],
                                    append=store.append)
    _engine(watched)._register_idea_questions(state)
    assert [r["statement"] for r in _rows(store)] == ["a question"]
    assert reads == [1], "exactly one read_all(), the RE-FOLD after the append and nothing before it"

    # The board it classified against really is this state's: a question already open on the handed
    # board is refused without the method ever looking at the log to find that out.
    reads.clear()
    store2 = _run(tmp_path, "second.jsonl")
    watched2 = types.SimpleNamespace(read_all=lambda: (reads.append(1), store2.read_all())[1],
                                     append=store2.append)
    _engine(watched2)._register_idea_questions(fold(store.read_all()))
    assert _rows(store2) == [] and reads == [], (
        "the empty second log would admit the question if the board came from there")


def test_the_engine_reaches_the_sweep_with_the_deep_research_stage_SWITCHED_OFF(tmp_path):
    """THE CALL SITE, driven on a real `Engine`. Every test above calls the method directly, so all
    of them stay green if nothing ever calls it — which is the shape of the defect being closed.

    `_maybe_deep_research` is the research cluster's one main-task entry point and the sweep runs
    before any of its three triggers, so a run with NO deep researcher wired — the offline default —
    still puts the Researcher's questions on the board. Mutation: delete the call and this is the
    only test in the file that goes red.
    """
    from looplab.engine.orchestrator import Engine

    store = _run(tmp_path)
    _propose(store, 0, ["should the negatives be mined per epoch?"])

    engine = Engine.__new__(Engine)
    engine.store = store
    engine._track_hypotheses = True
    engine.deep_researcher = None            # the stage is OFF
    engine.deep_research_every = 0

    out = engine._maybe_deep_research(fold(store.read_all()))
    assert [r["statement"] for r in _rows(store)] == ["should the negatives be mined per epoch?"]
    assert [r["source"] for r in _rows(store)] == ["researcher"]
    assert "should the negatives be mined per epoch?" in {
        c.seed_statement for c in out.cards.values()}, (
        "and the state the cadence hands on carries the row it just wrote")


# ------------------------------------------------ the populations, spelled once for both writers


def test_both_writers_read_ONE_derivation_of_the_two_populations():
    """`open_belief_populations` was hoisted out of `_admissible_beliefs` so the memo path and this
    sweep cannot disagree about what is already open. A dedup universe spelled at two sites is two
    universes, and the writer that loses is whichever ran last."""
    def _direction(cid, statement, **kw):
        return Card(id=cid, statement=statement, seed_statement=statement,
                    selection_provenance=CardSelectionProvenance(), **kw)

    child = Card(id="child", statement="an experiment", seed_statement="an experiment",
                 parent_card_id="d0",
                 selection_provenance=CardSelectionProvenance(
                     action_source="card_added", action_owner_count=1))
    state = RunState(goal="g", direction="max")
    state.cards = {c.id: c for c in [_direction("d0", "taken up"),
                                     _direction("d1", "unanswered"), child]}

    open_statements, unanswered = open_belief_populations(state)
    assert open_statements == ["taken up", "unanswered"], (
        "the DEDUP universe keeps a taken-up direction — restating a question somebody is already "
        "answering is precisely the duplicate to refuse")
    assert unanswered == ["unanswered"], (
        "the CAP population drops it — a direction with children no longer competes for room")
