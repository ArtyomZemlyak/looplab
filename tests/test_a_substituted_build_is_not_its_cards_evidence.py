"""A node whose Developer REPORTED building something else is not a test of its Card's claim.

Measured 2026-09-26 on MiniOneRec inf13: card-2 claimed that one ragged left-padded pass over a whole
batch keeps recall@50 once position_ids and per-request decode K/V lengths are right — the only lead
that had measured 3.3x, broken by an indexing bug. Its build reported `idea_implemented: different`
("instead of the risky single-pass architecture … I keep the byte-exact grouped path"), ran none of
the single pass, scored 1.373x on node 0's idea — and the board read card-2 `supported` on it. The
report already existed (`core/idea_report.py`); nothing that decides a verdict read it.

These drive the real engine and read the real fold and board, and pin the bound that keeps the
return from looping: ONE return per card, as for a never-run discard.
"""
from __future__ import annotations

import json

from looplab.agents import roles as roles_mod
from looplab.core.idea_report import IDEA_REPORT_NAME, idea_report_text
from looplab.core.models import NodeStatus
from looplab.events.replay import fold
from looplab.search import card_selection as cs
from looplab.serve.public_cards import public_cards

from tests.test_card_speculation_engine import (  # noqa: F401 — the receipt fixture is autouse
    _add_ready_draft,
    _admit_unit_speculation_receipt,
    _commit_speculative_node,
    _engine,
    _start,
)

_INSTEAD = "kept the byte-exact grouped path at real cohort size"


def _report(value: str, instead: str = _INSTEAD) -> dict:
    return {IDEA_REPORT_NAME: idea_report_text({"idea_implemented": value, "built_instead": instead})}


def _setup(tmp_path, name: str):
    engine, producer = _engine(tmp_path / name)
    engine._base_max_nodes = 8
    engine.policy.max_nodes = 8
    _start(engine)
    return engine, producer


def _build(engine, producer, card_id: str, files: dict, *, x: float) -> int:
    """Put `card_id` on the board alone, and commit one speculative build whose files are `files`."""
    _add_ready_draft(engine, card_id, x=x)
    producer.last_files = dict(files)
    return _commit_speculative_node(engine)


def _evaluate(engine, node_id: int, metric: float) -> None:
    engine.store.append("node_evaluated", {
        "node_id": node_id, "generation": 0, "metric": metric, "eval_seconds": 1.0,
        "extra_metrics": {}, "stdout_tail": "", "trials": [], "violations": []})


def _board(state):
    return ([c.id for c in state.open_research_beliefs()],
            [c.id for c in roles_mod.attempted_board_prompt_cards(state)],
            "\n".join(roles_mod.board_prompt_lines(state)))


# ------------------------------------------------------------ (1) the measured case, end to end

def test_a_substituted_build_that_beat_the_record_does_not_make_its_card_supported(tmp_path):
    engine, producer = _setup(tmp_path, "inf13-shape")
    # A real experiment sets the record first (toy direction is `min`)...
    first = _build(engine, producer, "card-1", _report("as_proposed", ""), x=0.2)
    _evaluate(engine, first, 1.0)
    # ...then card-2's build runs something ELSE and beats it — node 2 of inf13 exactly.
    sub = _build(engine, producer, "card-2", _report("different"), x=0.3)
    _evaluate(engine, sub, 0.5)

    state = fold(engine.store.read_all())
    node = state.nodes[sub]
    # The node RAN and keeps everything a run owes a real experiment: its metric, its champion title.
    assert node.status is NodeStatus.evaluated and node.metric == 0.5
    assert state.best_node_id == sub

    card = state.cards["card-2"]
    # Without the fix this read `supported`, with `evidence=[sub]`: the board's lie, measured.
    assert card.substituted_nodes == [sub]
    assert card.evidence == []
    assert card.verdict == "open"
    assert card.status == "proposed"
    assert card.best_delta is None
    # ...so the idea is back: electable, and on the claimable untested feed.
    assert card.selection_ready is True and cs._strictly_selection_ready(card) is True
    assert "card-2" in [c.id for c in cs.eligible_cards(state, engine.policy)]
    claimable, attempted, prompt = _board(state)
    assert "card-2" in claimable and "card-2" not in attempted
    # The Researcher reads WHY it is back, on the card's own row.
    row = next(line for line in prompt.splitlines() if "CARD_ID=card-2" in line)
    assert f"NOT TESTED by node {sub} built instead: {_INSTEAD}" in row
    assert "UNTESTED and back on the board" in row
    # The operator's wire carries it beside `discarded_nodes`.
    published = public_cards(state.cards)["card-2"]
    assert published["substituted_nodes"] == [sub] and published["evidence"] == []

    # The honest card beside it is untouched.
    real = state.cards["card-1"]
    assert real.substituted_nodes == [] and real.evidence == [first] and real.verdict == "tested"


def test_the_digest_and_the_novelty_judge_say_it_was_not_a_test(tmp_path):
    from looplab.engine.novelty import _prior_outcome
    engine, producer = _setup(tmp_path, "notes")
    sub = _build(engine, producer, "card-2", _report("not_implemented", "nothing"), x=0.3)
    _evaluate(engine, sub, 0.7)
    node = fold(engine.store.read_all()).nodes[sub]
    assert _prior_outcome(node) == (
        "metric=0.7 [NOT A TEST OF card-2's IDEA (idea not_implemented) — built instead: nothing]")


# ------------------------------------------------------------ (2) what is NOT a substitution

def test_as_proposed_and_partly_are_evidence_as_before(tmp_path):
    for value in ("as_proposed", "partly"):
        engine, producer = _setup(tmp_path, f"counts-{value}")
        node_id = _build(engine, producer, "card-1", _report(value, "the MLP half only"), x=0.2)
        _evaluate(engine, node_id, 1.0)
        card = fold(engine.store.read_all()).cards["card-1"]
        assert card.substituted_nodes == [], value
        assert card.evidence == [node_id] and card.verdict == "tested", value
        assert card.selection_ready is False, value


def test_no_report_at_all_is_evidence_as_before(tmp_path):
    engine, producer = _setup(tmp_path, "no-report")
    node_id = _build(engine, producer, "card-1", {}, x=0.2)
    _evaluate(engine, node_id, 1.0)
    card = fold(engine.store.read_all()).cards["card-1"]
    assert card.substituted_nodes == [] and card.evidence == [node_id]


def test_a_pending_substitution_is_judged_when_it_lands_not_before(tmp_path):
    """Returning the card while its own node runs would let the election build it twice at once."""
    engine, producer = _setup(tmp_path, "pending")
    node_id = _build(engine, producer, "card-2", _report("different"), x=0.3)
    card = fold(engine.store.read_all()).cards["card-2"]
    assert card.substituted_nodes == []
    assert card.evidence == [node_id]
    assert card.selection_ready is False
    _evaluate(engine, node_id, 0.9)
    card = fold(engine.store.read_all()).cards["card-2"]
    assert card.substituted_nodes == [node_id] and card.evidence == []


def test_a_repair_that_built_the_idea_after_all_is_counted_again(tmp_path):
    """The LATEST report wins: `node_repaired.files` replaces the build's."""
    engine, producer = _setup(tmp_path, "repaired")
    node_id = _build(engine, producer, "card-2", _report("different"), x=0.3)
    engine.store.append("node_repaired", {
        "node_id": node_id, "generation": 0, "attempt": 1, "changed": [IDEA_REPORT_NAME],
        "deleted": [], "error_in": "refused", "files": _report("as_proposed", ""),
        "rationale": "fixed the positions", "stages_passed": [], "triage_action": "repair"})
    _evaluate(engine, node_id, 0.9)
    card = fold(engine.store.read_all()).cards["card-2"]
    assert card.substituted_nodes == [] and card.evidence == [node_id]


# ------------------------------------------------------------ (3) the bound: it cannot loop

def test_a_second_substitution_of_the_same_card_retires_it_with_an_honest_verdict(tmp_path):
    engine, producer = _setup(tmp_path, "twice")
    first = _build(engine, producer, "card-2", _report("different"), x=0.3)
    _evaluate(engine, first, 0.9)
    assert fold(engine.store.read_all()).cards["card-2"].selection_ready is True   # returned once…

    producer.last_files = _report("different", "the grouped path again")
    second = _commit_speculative_node(engine)                                          # …and rebuilt
    _evaluate(engine, second, 0.8)

    state = fold(engine.store.read_all())
    card = state.cards["card-2"]
    assert card.substituted_nodes == sorted([first, second])
    # At two, NONE is forgiven (a count, never a choice), and the card retires…
    assert card.evidence == sorted([first, second])
    assert card.selection_ready is False
    assert "work_terminal" in card.selection_blockers
    assert "card-2" not in [c.id for c in cs.eligible_cards(state, engine.policy)]
    claimable, attempted, prompt = _board(state)
    assert "card-2" not in claimable and "card-2" in attempted
    # …but it never claims to have been TESTED: the verdict stays `open`, and the row says why.
    assert card.verdict == "open" and card.best_delta is None
    row = next(line for line in prompt.splitlines() if "CARD_ID=card-2" in line)
    assert "VERDICT=open" in row and "built as something else twice, so retired" in row
    # A third turn buys nothing.
    assert engine._request_card_build() is False


# ------------------------------------------------------------ (4) mixed evidence: the real node decides

def test_in_a_mixed_evidence_set_only_the_real_node_decides_the_verdict():
    """A substitution beside a real experiment stays in `evidence` (the status lane still reads it)
    but never counts: here the real node did not improve and the substitution did, so `supported`
    would come from the substitution alone."""
    from looplab.core.models import Idea, Node
    from looplab.events.card_ledger import _evidence_verdict

    def node(i, metric, parents=()):
        return Node(id=i, operator="draft", idea=Idea(operator="draft", params={}, rationale="r"),
                    status=NodeStatus.evaluated, metric=metric, parent_ids=list(parents))

    nodes = {0: node(0, 1.0), 1: node(1, 1.2, [0]), 2: node(2, 0.5, [0])}
    kwargs = dict(record_establisher=0)
    assert _evidence_verdict([1, 2], nodes, "min", {0, 2}, False, **kwargs)[1] == "supported"
    delta, verdict, supported = _evidence_verdict([1, 2], nodes, "min", {0, 2}, False,
                                                  untested=frozenset({2}), **kwargs)
    assert verdict == "tested" and supported is False and delta is not None and delta < 0
    # all of it substituted -> no usable evidence -> `open`, never `tested`
    assert _evidence_verdict([2], nodes, "min", {0, 2}, False, untested=frozenset({2}),
                             **kwargs)[1] == "open"


def test_the_report_file_is_what_the_fold_reads(tmp_path):
    """Pure function of the log: the node's files come from `node_created`, nothing else."""
    engine, producer = _setup(tmp_path, "fold-only")
    sub = _build(engine, producer, "card-2", _report("different"), x=0.3)
    _evaluate(engine, sub, 0.9)
    created = [e for e in engine.store.read_all() if e.type == "node_created"
               and e.data.get("node_id") == sub][0]
    assert json.loads(created.data["files"][IDEA_REPORT_NAME])["idea_implemented"] == "different"
    first, second = fold(engine.store.read_all()), fold(engine.store.read_all())
    assert first.cards["card-2"].model_dump() == second.cards["card-2"].model_dump()
