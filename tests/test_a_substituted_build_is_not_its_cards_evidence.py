"""A node whose Developer REPORTED building something else is not a test of its Card's claim.

Measured 2026-09-26 on MiniOneRec inf13: card-2 claimed that one ragged left-padded pass over a whole
batch keeps recall@50 once position_ids and per-request decode K/V lengths are right — the only lead
that had measured 3.3x, broken by an indexing bug. Its build reported `idea_implemented: different`
("instead of the risky single-pass architecture … I keep the byte-exact grouped path"), ran none of
the single pass, scored 1.373x on node 0's idea — and the board read card-2 `supported` on it. The
report already existed (`core/idea_report.py`); nothing that decides a verdict read it.

These drive the real engine and read the real fold and board, and pin the bound that keeps the
return from looping: ONE return per card, as for a never-run discard — and the return that is
withheld (section 7): a substitution a later build on it has beaten, unless a rebuild is under way.
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
    _build_result,
    _commit_speculative_node,
    _engine,
    _request,
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


def test_the_novelty_judges_prior_outcome_says_it_was_not_a_test(tmp_path):
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
    """The report the fold judges is the one `node_created` carried (not a side file), and folding
    the same log twice derives the same card. The digest/novelty/reader surfaces are driven in
    `tests/test_substituted_build_readers.py`."""
    engine, producer = _setup(tmp_path, "fold-only")
    sub = _build(engine, producer, "card-2", _report("different"), x=0.3)
    _evaluate(engine, sub, 0.9)
    created = [e for e in engine.store.read_all() if e.type == "node_created"
               and e.data.get("node_id") == sub][0]
    assert json.loads(created.data["files"][IDEA_REPORT_NAME])["idea_implemented"] == "different"
    first, second = fold(engine.store.read_all()), fold(engine.store.read_all())
    assert first.cards["card-2"].model_dump() == second.cards["card-2"].model_dump()


# ------------------------------------------------------------ (5) the return survives its own rebuild

def test_a_returned_cards_rebuild_is_not_superseded_by_the_freshness_gate(tmp_path):
    """Evidence is re-linked from `idea.card_id` on every fold, so the rebuild put the forgiven node
    back beside it (`[n1 terminal, n2 pending]`, owner `mixed`), the speculative freshness gate refused
    the pair, and `_drop_stale_speculation` superseded the rebuild it had just paid for — the return
    retired its card unbuilt, every time. UNPATCHED here: the real gate decides."""
    import anyio

    engine, producer = _setup(tmp_path, "rebuild")
    first = _build(engine, producer, "card-2", _report("different"), x=0.3)
    _evaluate(engine, first, 0.9)
    producer.last_files = _report("as_proposed", "")
    second = _commit_speculative_node(engine)

    card = fold(engine.store.read_all()).cards["card-2"]
    assert card.evidence == [second] and card.substituted_nodes == [first]
    assert card.selection_provenance.owner_state == "in_flight"
    assert card.selection_blockers == ["work_in_flight"]
    assert anyio.run(engine._drop_stale_speculation) is False
    assert fold(engine.store.read_all()).nodes[second].status is NodeStatus.pending

    # …and when the rebuild lands as proposed, IT decides the verdict, beside the forgiven node.
    _evaluate(engine, second, 0.4)
    card = fold(engine.store.read_all()).cards["card-2"]
    assert card.evidence == sorted([first, second]) and card.substituted_nodes == [first]
    assert card.verdict != "open" and card.status == "evaluated"
    assert "work_terminal" in card.selection_blockers


def test_a_discard_then_a_substitution_is_two_forgiven_builds_not_three(tmp_path, monkeypatch):
    """The bound counts BOTH kinds: a discard returned, rebuilt as a substitution, must retire."""
    import anyio

    from looplab.engine import speculation as speculation_module

    engine, producer = _setup(tmp_path, "mixed-bound")
    _add_ready_draft(engine, "card-2", x=0.3)
    producer.last_files = _report("as_proposed", "")
    discarded = _commit_speculative_node(engine)
    monkeypatch.setattr(speculation_module, "speculative_card_is_fresh", lambda *_a, **_k: False)
    assert anyio.run(engine._drop_stale_speculation) is True
    monkeypatch.undo()
    assert fold(engine.store.read_all()).cards["card-2"].selection_ready is True    # returned once

    producer.last_files = _report("different")
    rebuilt = _commit_speculative_node(engine)
    assert anyio.run(engine._drop_stale_speculation) is False                        # not superseded
    _evaluate(engine, rebuilt, 0.8)

    state = fold(engine.store.read_all())
    card = state.cards["card-2"]
    assert card.discarded_nodes == [discarded] and card.substituted_nodes == [rebuilt]
    assert card.evidence == sorted([discarded, rebuilt])
    assert card.verdict == "open" and card.status == "failed"
    assert card.selection_ready is False and "work_terminal" in card.selection_blockers
    assert "card-2" not in [c.id for c in cs.eligible_cards(state, engine.policy)]


def test_a_gated_substitution_is_not_returned(tmp_path):
    """An infeasible (or trust-excluded) build that says "different" keeps its card in `gated`: a
    return would carry it OUT of the one lane that excludes and buy it a second build."""
    engine, producer = _setup(tmp_path, "gated")
    sub = _build(engine, producer, "card-2", _report("different"), x=0.3)
    engine.store.append("node_evaluated", {
        "node_id": sub, "generation": 0, "metric": 0.5, "eval_seconds": 1.0, "extra_metrics": {},
        "stdout_tail": "", "trials": [],
        "violations": [{"name": "mem", "value": 2.0, "max": 1.0, "min": None}]})
    state = fold(engine.store.read_all())
    assert state.nodes[sub].feasible is False
    card = state.cards["card-2"]
    assert card.substituted_nodes == [sub] and card.evidence == [sub]
    assert card.status == "gated" and card.selection_ready is False
    assert "card_terminal" in card.selection_blockers and card.verdict == "open"


def test_a_report_copied_from_the_parent_does_not_move_the_childs_card(tmp_path):
    """The fold-level half of `core/idea_report.py::inherited_report`: a log written before the
    Developer dropped the preloaded report carries the parent's `different` in the child's files —
    byte for byte — and the child's OWN card must not be returned on the parent's word."""
    engine, producer = _setup(tmp_path, "inherited")
    parent = _build(engine, producer, "card-1", _report("different"), x=0.2)
    _evaluate(engine, parent, 1.0)
    _add_ready_draft(engine, "card-2", x=0.3)
    state = fold(engine.store.read_all())
    child = max(state.nodes) + 1
    engine.store.append("node_created", {
        "node_id": child, "parent_ids": [parent], "operator": "improve",
        "idea": {"operator": "improve", "params": {"x": 0.3}, "rationale": "tune it",
                 "card_id": "card-2"},
        "files": dict(state.nodes[parent].files)})
    _evaluate(engine, child, 0.9)
    state = fold(engine.store.read_all())
    assert state.nodes[child].files[IDEA_REPORT_NAME] == state.nodes[parent].files[IDEA_REPORT_NAME]
    assert state.cards["card-1"].substituted_nodes == [parent]     # the parent's own report counts
    card = state.cards["card-2"]
    assert child in card.evidence and card.substituted_nodes == []
    assert card.verdict != "open"


def test_the_rebuild_of_a_returned_card_is_told_why_it_is_back(tmp_path):
    """The Developer (only — `node_created` keeps the Researcher's idea) hears that the earlier build
    ran something else; the first build of a card hears nothing new."""
    engine, producer = _setup(tmp_path, "told")
    seen: list = []
    implement = producer.implement
    producer.implement = lambda idea: (seen.append(idea.rationale or ""), implement(idea))[1]
    first = _build(engine, producer, "card-2", _report("different"), x=0.3)
    _evaluate(engine, first, 0.9)
    producer.last_files = _report("as_proposed", "")
    second = _commit_speculative_node(engine)
    assert "HAS NOT BEEN TESTED YET" not in seen[0]
    assert f"node {first} built instead: {_INSTEAD}" in seen[-1]
    assert "HAS NOT BEEN TESTED YET" not in (fold(engine.store.read_all()).nodes[second].idea.rationale
                                             or "")


# ------------------------------------------------------------ (6) the fold's other clauses, driven

def _raw_node(engine, card_id: str, files: dict, *, parents=()) -> int:
    """A node under `card_id` appended the way a log carries one — for shapes the election can't
    reach (a second build of a card that already has evidence)."""
    node_id = max(fold(engine.store.read_all()).nodes) + 1
    engine.store.append("node_created", {
        "node_id": node_id, "parent_ids": list(parents), "operator": "draft", "files": dict(files),
        "idea": {"operator": "draft", "params": {"x": 0.9}, "rationale": "r", "card_id": card_id}})
    return node_id


def test_in_a_mixed_set_the_substitution_stays_in_evidence_and_the_real_node_decides(tmp_path):
    engine, producer = _setup(tmp_path, "mixed-fold")
    real = _build(engine, producer, "card-2", _report("as_proposed", ""), x=0.3)
    _evaluate(engine, real, 1.0)
    before = fold(engine.store.read_all()).cards["card-2"]
    sub = _raw_node(engine, "card-2", _report("different"))
    _evaluate(engine, sub, 0.1)                   # far better (min), and NOT a test of the idea
    card = fold(engine.store.read_all()).cards["card-2"]
    assert card.substituted_nodes == [sub] and card.evidence == sorted([real, sub])
    assert (card.verdict, card.best_delta) == (before.verdict, before.best_delta)
    assert card.status == "evaluated" and "work_terminal" in card.selection_blockers


def test_a_tombstoned_substitution_is_not_stamped(tmp_path):
    engine, producer = _setup(tmp_path, "tombstoned")
    sub = _build(engine, producer, "card-2", _report("different"), x=0.3)
    _evaluate(engine, sub, 0.5)
    engine.store.append("node_tombstoned", {"node_ids": [sub]})
    card = fold(engine.store.read_all()).cards["card-2"]
    assert card.substituted_nodes == []


def test_a_discarded_build_is_forgiven_once_as_a_discard_never_also_as_a_substitution(
        tmp_path, monkeypatch):
    import anyio

    from looplab.engine import speculation as speculation_module

    engine, producer = _setup(tmp_path, "discard-not-sub")
    _add_ready_draft(engine, "card-2", x=0.3)
    producer.last_files = _report("different")
    node_id = _commit_speculative_node(engine)
    monkeypatch.setattr(speculation_module, "speculative_card_is_fresh", lambda *_a, **_k: False)
    assert anyio.run(engine._drop_stale_speculation) is True
    card = fold(engine.store.read_all()).cards["card-2"]
    assert card.discarded_nodes == [node_id] and card.substituted_nodes == []
    assert card.evidence == [] and card.selection_ready is True


def test_a_returned_cards_research_origin_survives_a_substitution_return(tmp_path):
    """Attribution is not evidence: the returned card still names the memo its build came from."""
    from looplab.events import card_ledger

    engine, producer = _setup(tmp_path, "origin")
    sub = _build(engine, producer, "card-2", _report("different"), x=0.3)
    _evaluate(engine, sub, 0.9)
    state = fold(engine.store.read_all())
    state.nodes[sub].idea.footprint = {"gpus": 1}
    state.nodes[sub].research_origin = {"memo_id": "memo:sha256:" + "d" * 64}
    card_ledger.derive_cards(state)
    card = state.cards["card-2"]
    assert card.evidence == [] and card.substituted_nodes == [sub]
    assert card.research_origin == "memo:sha256:" + "d" * 64
    assert card.footprint == {"gpus": 1, "proposed_by": "researcher"}


def test_a_struck_rebuild_is_not_work_the_return_waits_on(tmp_path):
    """The return keeps the forgiven node out only beside a rebuild that is IN FLIGHT by the ledger's
    own rule — a tombstoned pending rebuild is not, so the card is judged on both, as before."""
    engine, producer = _setup(tmp_path, "struck")
    first = _build(engine, producer, "card-2", _report("different"), x=0.3)
    _evaluate(engine, first, 0.9)
    producer.last_files = _report("as_proposed", "")
    second = _commit_speculative_node(engine)
    assert fold(engine.store.read_all()).cards["card-2"].evidence == [second]
    engine.store.append("node_tombstoned", {"node_ids": [second]})
    card = fold(engine.store.read_all()).cards["card-2"]
    assert card.evidence == sorted([first, second])
    assert "work_terminal" in card.selection_blockers


def test_the_deprecated_hypotheses_rows_list_only_the_nodes_that_tested_the_card(tmp_path):
    """The old Hypothesis `evidence` meant "the nodes that TESTED it". A card retired on two builds
    its Developer reported as something else keeps them in `cards[].evidence` (the audit set), but
    the `/state` compat row — same old shape, `tests/test_server.py` pins it — must not call them
    tests."""
    from fastapi.testclient import TestClient

    from looplab.serve.server import make_app

    engine, producer = _setup(tmp_path, "subrun")
    first = _build(engine, producer, "card-2", _report("different"), x=0.3)
    _evaluate(engine, first, 0.9)
    producer.last_files = _report("different", "again")
    second = _commit_speculative_node(engine)
    _evaluate(engine, second, 0.8)
    state = TestClient(make_app(tmp_path)).get("/api/runs/subrun/state").json()["state"]
    assert state["cards"]["card-2"]["evidence"] == sorted([first, second])
    assert state["hypotheses"]["card-2"]["evidence"] == []


# ------------------------------------------------------------ (7) a substitution a later build beat
#
# Measured 2026-09-27 on MiniOneRec inf13: card-2's node 2 (1.3726, `different`) was returned and
# selection-ready although node 5 — card-6, built on node 2 `as_proposed` — had tested card-2's very
# claim and beaten it (4.1658). `_apply_card_returns` now keeps such a card off the automatic lane
# (`core/idea_report.py::surpassed_by`), except over a rebuild already under way. The toy run below
# MINIMIZES, so a later build "beats" the substitution with a LOWER metric.

def _child(engine, parent: int, files: dict, *, card_id: str, metric=None) -> int:
    """A later build that took `parent`'s code as its base, filed under ANOTHER card — inf13's node
    5 (card-6, an improve on node 2). Its id comes from the engine's own allocator, so it never lands
    on an id a claim has reserved."""
    events = engine.store.read_all()
    node_id = engine._node_id_ceiling(events, fold(events))
    engine.store.append("node_created", {
        "node_id": node_id, "parent_ids": [parent], "operator": "improve",
        "idea": {"operator": "improve", "params": {"x": 0.5}, "rationale": "a later idea",
                 "card_id": card_id},
        "files": dict(files)})
    if metric is not None:
        _evaluate(engine, node_id, metric)
    return node_id


def _row(state, card_id: str) -> str:
    return next(line for line in _board(state)[2].splitlines() if f"CARD_ID={card_id}" in line)


def test_a_substitution_another_cards_build_on_it_beat_is_not_returned(tmp_path):
    """The inf13 shape end to end: returned while nothing beat it, kept off the board once a later
    build that tested its own idea did — verdict `open`, and the row names the nodes."""
    engine, producer = _setup(tmp_path, "inf13-beaten")
    sub = _build(engine, producer, "card-2", _report("different"), x=0.3)
    _evaluate(engine, sub, 0.9)
    running = _child(engine, sub, _report("as_proposed", ""), card_id="card-6")
    # a later build still running has measured nothing: the card is returned, as before
    card = fold(engine.store.read_all()).cards["card-2"]
    assert card.evidence == [] and card.selection_ready is True
    _evaluate(engine, running, 0.7)                                   # it tested its idea and WON

    state = fold(engine.store.read_all())
    card = state.cards["card-2"]
    assert card.substituted_nodes == [sub] and card.evidence == [sub]
    assert card.verdict == "open" and card.best_delta is None        # never tested, never supported
    assert card.status == "failed" and card.status_nodes == [sub]
    assert card.selection_ready is False and "work_terminal" in card.selection_blockers
    assert "card-2" not in [c.id for c in cs.eligible_cards(state, engine.policy)]
    claimable, attempted, _prompt = _board(state)
    assert "card-2" not in claimable and "card-2" in attempted
    row = _row(state, "card-2")
    assert "VERDICT=open" in row
    assert (f"NOT TESTED by node {sub} built instead: {_INSTEAD} — not returned: node(s) "
            f"[{running}] built on node {sub} and beat it; its idea was never tested — propose it "
            "again only if none of those tested it." in row)
    assert "UNTESTED and back on the board" not in row
    # …and no Developer build is bought for it: nothing else on this board is electable.
    assert engine._request_card_build() is False
    # The winner's own card is untouched by any of this.
    assert state.cards["card-6"].evidence == [running]
    assert state.cards["card-6"].substituted_nodes == []


def test_a_later_build_that_did_not_beat_the_substitution_leaves_the_card_returned(tmp_path):
    """inf13 seq 1849: node 3 — card-5's unrelated improve on node 2 — scored 1.2382 against node 2's
    1.3726. "Any evaluated descendant" would have taken card-2 off the board there, before card-6
    (the card that did test its claim) existed; only a descendant that BEAT the substitution does.
    A winner that is itself a substitution tested nothing, so it does not block either."""
    engine, producer = _setup(tmp_path, "not-beaten")
    sub = _build(engine, producer, "card-2", _report("different"), x=0.3)
    _evaluate(engine, sub, 0.9)
    _child(engine, sub, _report("as_proposed", ""), card_id="card-5", metric=1.2)       # lost
    _child(engine, sub, _report("different", "its own substitute"), card_id="card-7",
           metric=0.1)                                                                  # not a test
    state = fold(engine.store.read_all())
    card = state.cards["card-2"]
    assert card.evidence == [] and card.selection_ready is True
    assert "card-2" in [c.id for c in cs.eligible_cards(state, engine.policy)]
    assert "UNTESTED and back on the board" in _row(state, "card-2")


def test_a_rebuild_in_flight_when_a_later_build_beats_the_substitution_is_committed(tmp_path):
    """The critic's in-flight defect, driven through the real request, producer, claim and gate. A
    speculative rebuild has no node until `_claim_requested_card_build` commits it, and the claim
    gives the build up unless the card is still selected. A later build beating the substitution
    WHILE the rebuild was in hand would have withdrawn the return (`work_terminal`) and closed the
    finished, paid build `stale:not_selected_now`; the open request counts as the rebuild instead."""
    import anyio

    engine, producer = _setup(tmp_path, "in-flight")
    sub = _build(engine, producer, "card-2", _report("different"), x=0.3)
    _evaluate(engine, sub, 0.9)
    request = _request(engine)                                   # the rebuild is elected…
    assert request["card_id"] == "card-2"
    producer.last_files = _report("as_proposed", "")
    result = _build_result(engine, request)                      # …and built; it has no node yet
    assert result.success is True
    beat = _child(engine, sub, _report("as_proposed", ""), card_id="card-6", metric=0.7)

    state = fold(engine.store.read_all())
    card = state.cards["card-2"]
    assert card.evidence == [] and card.substituted_nodes == [sub]
    assert card.selection_ready is True and state.card_status_now(card) == "building"

    engine._ensure_speculation_state()
    engine._spec_builds[result.key] = result
    assert engine._serve_card_builds() is True
    state = fold(engine.store.read_all())
    rebuild = max(state.nodes)
    assert rebuild not in (sub, beat) and state.nodes[rebuild].idea.card_id == "card-2"
    assert state.card_build_outcomes[-1] == "committed"            # not `stale`
    assert state.nodes[rebuild].status is NodeStatus.pending
    assert state.cards["card-2"].evidence == [rebuild]
    assert anyio.run(engine._drop_stale_speculation) is False      # and the real gate keeps it
    assert fold(engine.store.read_all()).nodes[rebuild].status is NodeStatus.pending

    # When it lands, the rebuild decides the card; the forgiven node rejoins beside it, as before.
    _evaluate(engine, rebuild, 0.8)
    card = fold(engine.store.read_all()).cards["card-2"]
    assert card.evidence == sorted([sub, rebuild]) and card.verdict != "open"


def test_a_request_that_closed_without_a_node_is_judged_again(tmp_path):
    """Only a build ANSWERING the card holds the return. Once its request closes with nothing built,
    nothing of the run's is in flight for it, and the card is judged as it would have been."""
    engine, producer = _setup(tmp_path, "closed")
    sub = _build(engine, producer, "card-2", _report("different"), x=0.3)
    _evaluate(engine, sub, 0.9)
    request = _request(engine)
    _child(engine, sub, _report("as_proposed", ""), card_id="card-6", metric=0.7)
    assert fold(engine.store.read_all()).cards["card-2"].evidence == []
    assert engine._append_card_build_done(request, skipped="stale",
                                          skipped_reason="not_selected_now") is True
    card = fold(engine.store.read_all()).cards["card-2"]
    assert card.evidence == [sub] and card.selection_ready is False


def test_a_claimed_rebuild_is_a_rebuild_too(tmp_path):
    """The serial lane claims the card (`node_building` names it) before its Developer call; a later
    build beating the substitution during that call does not take the card back either."""
    from looplab.search.card_selection import card_action

    engine, producer = _setup(tmp_path, "claimed")
    sub = _build(engine, producer, "card-2", _report("different"), x=0.3)
    _evaluate(engine, sub, 0.9)
    card = fold(engine.store.read_all()).cards["card-2"]
    reservation = engine._claim_existing_card_build(card_action(card))
    assert reservation is not None and reservation.card_id == "card-2"
    _child(engine, sub, _report("as_proposed", ""), card_id="card-6", metric=0.7)
    card = fold(engine.store.read_all()).cards["card-2"]
    assert card.evidence == [] and card.status == "building"
    assert card.status_nodes == [reservation.node_id]


def test_a_request_for_a_card_since_merged_away_is_not_the_survivors_rebuild(tmp_path):
    """A request is committed under its OWN card id: `_claim_requested_card_build` looks the card up
    by it, and a card merged into another is folded out of `cards`. So card-2's open request, once
    card-2 is merged into card-9, can never land on card-9 — it is no rebuild of card-9's, and does
    not hold card-9's return (markers are different: a reserved node links into the survivor)."""
    engine, producer = _setup(tmp_path, "merged")
    sub = _build(engine, producer, "card-2", _report("different"), x=0.3)
    _evaluate(engine, sub, 0.9)
    assert _request(engine)["card_id"] == "card-2"
    _add_ready_draft(engine, "card-9", x=0.4)
    engine.store.append("card_merged", {"canonical": "card-9", "aliases": ["card-2"],
                                        "statement": "queued proposal card-9 improves the objective"})
    _child(engine, sub, _report("as_proposed", ""), card_id="card-6", metric=0.7)
    state = fold(engine.store.read_all())
    assert "card-2" not in state.cards and state.open_card_build_request_ids() == {"card-2"}
    card = state.cards["card-9"]
    assert card.substituted_nodes == [sub] and card.evidence == [sub]


def test_the_substituted_nodes_own_open_request_is_not_a_rebuild(tmp_path):
    """The one open request that is NOT a rebuild: the substituted build's own, whose
    `card_build_done` has not landed (committed, then stopped before the close). Counting it would
    hold the return for a card nothing is rebuilding."""
    engine, producer = _setup(tmp_path, "own-request")
    _add_ready_draft(engine, "card-2", x=0.3)
    producer.last_files = _report("different")
    request = _request(engine)
    outcome, sub = engine._claim_requested_card_build(request, _build_result(engine, request))
    assert outcome == "created"                                  # committed; the request is open
    _evaluate(engine, sub, 0.9)
    state = fold(engine.store.read_all())
    assert state.open_card_build_request_ids() == {"card-2"} and sub not in state.speculative_nodes
    assert state.cards["card-2"].evidence == []                   # returned: nothing beat it yet
    _child(engine, sub, _report("as_proposed", ""), card_id="card-6", metric=0.7)
    card = fold(engine.store.read_all()).cards["card-2"]
    assert card.substituted_nodes == [sub] and card.evidence == [sub]


def test_what_surpasses_a_substitution_is_a_statable_rule():
    """`core/idea_report.py::surpassed_by` over inf13's shape: node 2 (1.37, card-2, substituted) and
    what was later built on it, one row per clause of the rule."""
    from looplab.core.idea_report import surpassed_by
    from looplab.core.models import Idea, Node

    def node(i, metric, parents=(), *, card, files=None, status=NodeStatus.evaluated, **kw):
        op = "improve" if parents else "draft"
        return Node(id=i, operator=op, parent_ids=list(parents), status=status, metric=metric,
                    files=dict(files or {}), idea=Idea(operator=op, params={}, card_id=card), **kw)

    nodes = {
        2: node(2, 1.37, card="card-2", files=_report("different")),
        3: node(3, 1.24, [2], card="card-5"),                    # lost to it
        5: node(5, 4.17, [2], card="card-6"),                    # beat it (as proposed, no report)
        7: node(7, 1.33, [2], card="card-7"),                    # lost to it…
        10: node(10, 1.66, [7], card="card-11"),                 # …and ITS child beat it
        11: node(11, 9.0, [2], card="card-12", files=_report("different", "its own substitute")),
        12: node(12, 9.0, [2], card="card-2"),                   # the same card: its own rebuild
        13: node(13, 9.0, [2], card="card-13", tombstoned=True),
        14: node(14, None, [2], card="card-14", status=NodeStatus.pending),
        15: node(15, 9.0, [2], card="card-15", feasible=False),
        16: node(16, 9.0, [], card="card-16"),                   # not built on it at all
    }
    assert surpassed_by(2, nodes, direction="max") == [5, 10]
    assert surpassed_by(2, nodes, direction="min") == [3, 7]
    # `partly` still exercised its claim, so it counts; only a substitution does not
    nodes[5].files = _report("partly", "the MLP half only")
    assert surpassed_by(2, nodes, direction="max") == [5, 10]
    # the champion's own exclusions: a trust-flagged or an aborted number beat nothing
    assert surpassed_by(2, nodes, direction="max", excluded={5}, aborted={10}) == []
    # a substitution that produced no number is surpassed by nothing
    nodes[2] = node(2, None, card="card-2", files=_report("different"), status=NodeStatus.failed)
    assert surpassed_by(2, nodes, direction="max") == []


def test_the_withheld_return_is_a_function_of_the_log(tmp_path):
    """Replay determinism, driven over a log that has all three moments — the return, a rebuild
    request that holds it while a later build wins, and that request closing with no node. The
    engine's own read and a fresh read of the file fold to the same Cards, and folding every PREFIX
    of the log keeps the card returned up to the close and takes it off the board exactly there."""
    from looplab.events.eventstore import EventStore

    engine, producer = _setup(tmp_path, "replay")
    sub = _build(engine, producer, "card-2", _report("different"), x=0.3)
    _evaluate(engine, sub, 0.9)
    request = _request(engine)
    _child(engine, sub, _report("as_proposed", ""), card_id="card-6", metric=0.7)
    assert engine._append_card_build_done(request, skipped="stale",
                                          skipped_reason="not_selected_now") is True

    events = engine.store.read_all()
    live = fold(events)
    again = fold(EventStore(engine.store.path).read_all())
    assert ([(cid, c.model_dump(mode="json")) for cid, c in live.cards.items()]
            == [(cid, c.model_dump(mode="json")) for cid, c in again.cards.items()])

    returned_at = next(i for i, e in enumerate(events)
                       if e.type == "node_evaluated" and e.data["node_id"] == sub)
    closed_at = next(i for i, e in enumerate(events)
                     if e.type == "card_build_done" and e.data.get("skipped") == "stale")
    for cut in range(returned_at + 1, len(events) + 1):
        card = fold(events[:cut]).cards["card-2"]
        want = [sub] if cut > closed_at else []
        assert card.evidence == want, (cut, events[cut - 1].type, card.evidence)
        assert card.selection_ready is (cut <= closed_at), (cut, events[cut - 1].type)
