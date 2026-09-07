"""The novelty gates may read the papers the run itself retrieved (doc 52 row 32).

Both gates grade a proposal against THIS RUN's history and nothing else, which is the shape RQ-Bench
named a "novelty mirage": an idea reads as new because nothing here tried it, while the literature
the run retrieved describes it. LoopLab has had the retrieval half durable since doc 52 row 16
(`literature_retrieved` -> `RunState.literature`) and no gate ever read it.

The property these hold is the one that makes the signal safe to ship: the overlap is EVIDENCE and
never a verdict. Nothing is rejected for resembling a paper — running an experiment a paper
describes is often exactly right — and with the flag off the audit rows are byte-identical to what
they always were.
"""
from __future__ import annotations

import pathlib
import tempfile

import pytest

from looplab.core.config import Settings
from looplab.core.models import Idea
from looplab.engine.novelty import (LITERATURE_OVERLAP_FLOOR, LITERATURE_OVERLAP_LIMIT,
                                    literature_overlap)
from tests.factories import make_engine

_PAPERS = [
    {"id": "p1", "title": "Contrastive learning with hard negative mining",
     "snippet": "we mine hard negatives from the batch"},
    {"id": "p2", "title": "A survey of optimizers", "snippet": "adam, sgd, lion"},
    {"id": "p3", "title": "Hard negative mining for dense retrieval",
     "snippet": "negatives mined from an index"},
]


def test_the_overlap_finds_the_paper_that_describes_the_idea():
    rows = literature_overlap("Contrastive learning with hard negative mining for the retriever",
                              _PAPERS)
    assert [row["id"] for row in rows] == ["p1", "p3"]      # both, best first; the survey is not one
    assert rows[0]["similarity"] >= LITERATURE_OVERLAP_FLOOR
    assert all(row["similarity"] >= rows[-1]["similarity"] for row in rows)   # best first


def test_its_recall_is_a_floor_and_the_measure_says_so():
    """No stemming and no synonyms: a paraphrase of the SAME idea can share no content token at all
    and fall under the threshold. A reported overlap is evidence the run's reading describes the
    proposal; an empty result is not evidence that it does not — which is the asymmetry that makes
    it unsafe to ever reject on, and it is stated in the function rather than discovered later."""
    import inspect

    # A PARAPHRASE of exactly what p1 and p3 describe, sharing no content token with either.
    assert literature_overlap("Sample difficult examples during training so the model sees "
                              "tougher pairs", _PAPERS) == []
    assert "RECALL IS A FLOOR" in inspect.getdoc(literature_overlap)


def test_an_unrelated_idea_overlaps_nothing():
    """The floor is what keeps a shared 'the' out of the record."""
    assert literature_overlap("Raise the batch size to 512", _PAPERS) == []
    assert literature_overlap("", _PAPERS) == [] and literature_overlap("anything", []) == []


def test_the_overlap_is_bounded_and_deterministic():
    many = [{"id": f"p{i}", "title": "hard negative mining contrastive retrieval", "snippet": ""}
            for i in range(50)]
    rows = literature_overlap("hard negative mining for contrastive retrieval", many)
    assert len(rows) == LITERATURE_OVERLAP_LIMIT
    assert rows == literature_overlap("hard negative mining for contrastive retrieval", many)


def _engine(**overrides):
    engine = make_engine(pathlib.Path(tempfile.mkdtemp()) / "run", **overrides)
    engine.store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "max"})
    return engine


def test_the_note_is_empty_with_the_flag_off_whatever_the_run_read():
    """OFF is not "no papers" — it is "the gates do not read them", and the audit row must be the
    one it always was."""
    engine = _engine()
    state = type("S", (), {"literature": _PAPERS})()
    idea = Idea(operator="improve", params={}, rationale="hard negative mining for contrastive retrieval")
    assert engine._literature_note(state, idea) == {}
    engine._novelty_literature = True
    note = engine._literature_note(state, idea)
    assert note["literature"] and note["literature"][0]["id"] in {"p1", "p3"}


def test_a_run_that_retrieved_nothing_gets_no_note_even_with_the_flag_on():
    engine = _engine()
    engine._novelty_literature = True
    empty = type("S", (), {"literature": []})()
    idea = Idea(operator="improve", params={}, rationale="hard negative mining")
    assert engine._literature_note(empty, idea) == {}


def test_the_note_never_raises_into_the_proposal_path():
    """An annotation may not be the reason a run stops proposing."""
    engine = _engine()
    engine._novelty_literature = True
    broken = type("S", (), {"literature": [{"title": object()}]})()
    idea = Idea(operator="improve", params={}, rationale="hard negative mining")
    assert engine._literature_note(broken, idea) == {}


def test_the_flag_is_off_by_default_and_reaches_the_engine():
    assert Settings().novelty_literature is False
    assert _engine()._novelty_literature is False
    assert _engine(novelty_literature=True)._novelty_literature is True


def test_the_reproposal_hint_names_the_papers_without_telling_the_agent_to_avoid_them():
    """The prompt half. A paper describing an idea is a reason to say what is different about this
    one — never a reason to drop the direction, which is the instruction that would turn a
    literature signal into a novelty mirage of the opposite kind."""
    import inspect

    from looplab.engine.novelty import NoveltyGateMixin

    source = inspect.getsource(NoveltyGateMixin._reject_and_repropose)
    assert 'audit.get("literature")' in source
    assert "not a reason to drop the direction" in source


def test_the_audit_rows_declare_the_key_they_can_now_carry():
    """The payload contract is the record's documentation: a key a writer can put on an event and
    the contract does not declare is an undocumented field of the run's own log."""
    from looplab.events.types import EVENT_PAYLOAD_KEYS

    for etype in ("novelty_rejected", "novelty_graded", "cross_run_prior"):
        assert "literature" in EVENT_PAYLOAD_KEYS[etype].keys, etype
