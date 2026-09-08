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


def test_a_non_ascii_idea_is_measured_and_not_silently_zero():
    """THE TOKENIZER IS THE SHARED ONE (`core/text.py::tokenize`), and this is why. A local
    `[a-z0-9_]+` class reduces a Cyrillic or CJK idea to NO tokens, so the overlap is empty — and
    an empty overlap is not "nothing similar was retrieved", it is the answer the caller's own
    docstring warns must never be read as evidence of absence. The Russian idea below matched
    nothing at all against a Russian paper on its own subject.

    The `[a-z0-9_]+` class also glued identifiers together; the shared rule splits on underscore,
    so `train_loss` in an idea meets `training loss` in a title."""
    russian = [{"id": "p1", "title": "Обучение с подкреплением для отбора признаков",
                "snippet": "отбор признаков градиентный бустинг"}]
    hits = literature_overlap("отбор признаков с помощью градиентного бустинга", russian)
    assert [row["id"] for row in hits] == ["p1"], hits

    from looplab.engine.novelty import _content_tokens
    assert _content_tokens("train_loss schedule") == {"train", "loss", "schedule"}
    # …and the ASCII behaviour it replaced is unchanged where it already worked.
    ascii_hits = literature_overlap("feature selection via gradient boosting",
                                    [{"id": "p2", "title": "Feature selection with gradient boosting"}])
    assert [row["id"] for row in ascii_hits] == ["p2"]


# ---------------------------------------------------------------- the GRADE reads it (2026-09-08)
#
# `_literature_note` recorded the overlap on the audit rows and `grade_novelty` graded levels 0-5
# from the concept graph and this run's nodes alone, so a proposal the run's own reading describes
# could still be graded `novel`. The level rubric now takes prior art as an input — at ONE terminal
# and in ONE direction, which is what keeps a floor-bounded signal from manufacturing a verdict.

def _graph():
    from looplab.search.concept_graph import Concept, ConceptGraph
    return ConceptGraph([Concept("negatives/mining", "Hard negatives", ("negatives",),
                                 ("hard negative", "negative mining"))],
                        task_type="dense-retrieval")


def _run_state():
    from looplab.core.models import RunState
    return RunState(run_id="r", task_id="t", direction="max")


def test_a_proposal_the_runs_own_reading_describes_is_not_graded_a_new_region():
    """The novelty mirage, closed at the one terminal that asserts the space is new. It is still an
    ALLOW-shaped grade (`surface_prior`), never a rejection: running an experiment a paper describes
    is often exactly right."""
    from looplab.search.graded_novelty import grade_novelty

    idea = Idea(operator="improve", params={"lr": 0.1},
                rationale="contrastive learning with hard negative mining for the retriever")
    blind = grade_novelty(_run_state(), idea, _graph())
    assert (blind.level, blind.name, blind.recommendation) == (0, "novel", "allow")
    assert blind.prior_art == []

    informed = grade_novelty(_run_state(), idea, _graph(),
                             literature=literature_overlap(idea.rationale, _PAPERS))
    assert informed.level == 3 and informed.name == "described_in_retrieved_literature"
    assert informed.recommendation == "surface_prior", "surface the prior art; never reject on it"
    assert [row["id"] for row in informed.prior_art] == ["p1", "p3"]
    assert "hard negative mining" in informed.rationale, "the rationale names the paper it read"


def test_an_empty_overlap_never_moves_a_grade_because_its_recall_is_a_floor():
    """THE ASYMMETRY, driven: the paraphrase `test_its_recall_is_a_floor_and_the_measure_says_so`
    proves the overlap misses is fed through the whole rubric, and the grade is byte-identical to
    the one computed with no literature at all. Reading silence as novelty would spend a floor as
    if it were a ceiling."""
    from looplab.search.graded_novelty import grade_novelty

    idea = Idea(operator="improve", params={"lr": 0.1},
                rationale="sample difficult examples during training so the model sees tougher pairs")
    missed = literature_overlap(idea.rationale, _PAPERS)
    assert missed == [], "the paraphrase is exactly what this measure cannot see"
    blind = grade_novelty(_run_state(), idea, _graph())
    informed = grade_novelty(_run_state(), idea, _graph(), literature=missed)
    assert informed == blind and informed.level == 0


def test_prior_art_never_touches_a_grade_that_is_about_this_runs_own_history():
    """Levels 1-5 assert something about experiments THIS run ran, and a paper neither strengthens
    nor weakens those claims — so the overlap rides along as evidence and the level does not move."""
    from looplab.core.models import Idea as _Idea, Node, NodeStatus
    from looplab.search.graded_novelty import grade_novelty

    tried = _Idea(operator="improve", params={"lr": 0.1},
                  rationale="contrastive learning with hard negative mining")
    state = _run_state()
    state.nodes[0] = Node(id=0, operator="improve", idea=tried, status=NodeStatus.evaluated,
                          metric=0.5, attempt=0)
    papers = literature_overlap(tried.rationale, _PAPERS)
    assert papers, "the fixture must actually overlap or this proves nothing"

    same = grade_novelty(state, tried, _graph(), literature=papers)
    assert same.level == 1 and same.name == "identical" and same.prior_art == papers

    variant = _Idea(operator="improve", params={"lr": 0.9},
                    rationale="contrastive learning with hard negative mining, mined offline")
    allowed = grade_novelty(state, variant, _graph(),
                            literature=literature_overlap(variant.rationale, _PAPERS))
    assert allowed.level == 4 and allowed.recommendation == "allow"


def test_the_precheck_defers_on_prior_art_exactly_as_it_defers_on_novel():
    """The whole safety argument in one assertion: the renamed terminal is a level the live pre-gate
    already returns None for, so no proposal's ADMISSION can move. Level 3 short-circuits nothing —
    the flat dedup gate decides, as it always did."""
    import inspect

    from looplab.engine.novelty import NoveltyGateMixin

    source = inspect.getsource(NoveltyGateMixin._graded_novelty_precheck)
    assert "if grade.level not in (4, 5):" in source and "return None" in source


def test_the_engine_hands_the_grade_the_same_rows_the_audit_row_carries():
    """ONE derivation, two consumers: a grade whose rationale named a paper the row beside it did
    not carry would be a receipt about a different measurement."""
    engine = _engine()
    engine._novelty_literature = True
    state = type("S", (), {"literature": _PAPERS})()
    idea = Idea(operator="improve", params={},
                rationale="hard negative mining for contrastive retrieval")
    assert engine._literature_rows(state, idea) == engine._literature_note(state, idea)["literature"]
    engine._novelty_literature = False
    assert engine._literature_rows(state, idea) == [], "off means the grade sees nothing either"
