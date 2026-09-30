"""A ranking made in the CARD-STAGING lane is published when the Card's node is created.

MiniOneRec inf13 (2026-09-29): 20 `foresight_rank` spans, 2 `foresight_selected` rows, 0
`hypothesis_ranked` / `card_ranked` rows. 17 of the 20 rankings ran in the card-staging lane
(`engine/card_reservation.py::_stage_card_creates`), which proposes through the primary Researcher —
the foresight panel — before any node exists, and whose `finally` discards that researcher's
telemetry. The node the Card was later built into (`speculation.py::_create_precoded_node`, or the
serial Card claim in `orchestrator.py::_handle_create_actions`) had nothing left to publish, so the
foresight track record (`search/foresight.py::foresight_scoreboard`) and the board priority lost
nearly every pick the run paid for.

The rankings are now snapshotted where they are made, recorded against the staged `card_id` as a
diagnostic `card_ranking_staged` row, and published by `engine/audit.py::AuditMixin.
_emit_staged_card_ranking` when the Card's FIRST node is created, on the MAIN task, with that node's
id — on both creation paths. Driven through the real lanes, not a call-presence pin.
"""
from __future__ import annotations

import functools

import anyio
import pytest

from looplab.adapters.toytask import ToyTask
from looplab.core.models import Event, Idea
from looplab.engine.orchestrator import Engine
from looplab.events.replay import fold
from looplab.events.types import (EV_CARD_RANKED, EV_CARD_RANKING_STAGED, EV_FORESIGHT_SELECTED,
                                  EV_HYPOTHESIS_RANKED, EV_NODE_CREATED)
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.card_selection import META_CARD_ID
from looplab.search.policy import GreedyTree
# The receipt fixture is AUTOUSE in its own module and stays autouse when imported here.
from tests.test_card_speculation_engine import (  # noqa: F401  (imported for its autouse effect)
    _admit_unit_speculation_receipt,
    _build_result,
    _engine,
    _request,
    _start,
)

_RANKING_EVENTS = (EV_FORESIGHT_SELECTED, EV_HYPOTHESIS_RANKED, EV_CARD_RANKED)


class _RankingPanel:
    """What `ForesightPanelResearcher.propose` leaves on itself: an idea pick and a board order.

    The board order names `card-<n-1>` — the id the n-th staged Card is minted under on an empty
    board — so the Card projection resolves and `card_ranked` is written beside `hypothesis_ranked`."""

    def __init__(self):
        self.calls = 0
        self.last_foresight = None
        self.last_hyp_priority = None
        self._steering_context = []

    def propose(self, _state, _parent):
        self.calls += 1
        n = self.calls
        self.last_hyp_priority = {"order": [f"card-{n - 1}"], "confidence": 0.6, "n": 1,
                                  "reason": f"board {n}", "_trace_id": None, "_span_id": None}
        self.last_foresight = {"kind": "idea", "method": "foresight", "n": 2, "k": 2,
                               "chosen": 1, "order": [1, 0], "confidence": 0.7,
                               "reason": f"pick {n}", "candidates": ["a", "b"],
                               "_trace_id": None, "_span_id": None}
        return Idea(operator="draft", params={"x": 0.1 * n, "y": -1.0},
                    rationale=f"staged proposal {n}", hypothesis=f"staged hypothesis {n}")


class _Developer:
    def __init__(self):
        self.last_files: dict = {}
        self.last_deleted: list = []

    def implement(self, _idea):
        return "print(1)"


def _stage(engine) -> list[str]:
    state = fold(engine.store.read_all())
    return anyio.run(functools.partial(engine._stage_card_creates, [{"kind": "draft"}], state))


def _rows(engine, etype):
    return [event.data for event in engine.store.read_all() if event.type == etype]


def _published(engine) -> dict:
    return {etype: _rows(engine, etype) for etype in _RANKING_EVENTS}


def test_staging_records_the_ranking_against_the_card_and_publishes_nothing_yet(tmp_path):
    """No node exists at staging, so nothing node-keyed is written — but the ranking is no longer
    thrown away: it is held by `card_id`, and the primary researcher is left clean for the next
    action. MUTATION: drop `_record_staged_card_ranking` from the staging loop -> no row."""
    engine, _producer = _engine(tmp_path / "run")
    _start(engine)
    panel = engine.researcher = _RankingPanel()

    assert _stage(engine) == ["card-0"]

    rows = _rows(engine, EV_CARD_RANKING_STAGED)
    assert [row["card_id"] for row in rows] == ["card-0"]
    assert rows[0]["foresight"]["reason"] == "pick 1"
    assert rows[0]["hyp_priority"]["order"] == ["card-0"]
    assert _published(engine) == {etype: [] for etype in _RANKING_EVENTS}
    assert panel.last_foresight is None and panel.last_hyp_priority is None


def test_the_precoded_commit_publishes_the_staged_ranking_with_its_node_id(tmp_path):
    """The Layer-5 path: the producer builds on a POOLED pair that never proposed, and the main task
    commits. MUTATION: remove the `_emit_staged_card_ranking` call from `_create_precoded_node` ->
    the node lands with no `foresight_selected` / `hypothesis_ranked` / `card_ranked` at all."""
    engine, _producer = _engine(tmp_path / "run")
    _start(engine)
    engine.researcher = _RankingPanel()
    assert _stage(engine) == ["card-0"]

    result = _build_result(engine, _request(engine))
    assert result.success is True and result.card_id == "card-0"
    engine._ensure_speculation_state()
    engine._spec_builds[result.key] = result
    assert engine._serve_card_builds() is True

    created = [row for row in _rows(engine, EV_NODE_CREATED)
               if (row.get("idea") or {}).get("card_id") == "card-0"]
    assert len(created) == 1
    node_id = created[0]["node_id"]
    published = _published(engine)
    assert [(p["node_id"], p["card_id"], p["reason"])
            for p in published[EV_FORESIGHT_SELECTED]] == [(node_id, "card-0", "pick 1")]
    assert [r["node_id"] for r in published[EV_HYPOTHESIS_RANKED]] == [node_id]
    assert [(c["at_node"], c["order"]) for c in published[EV_CARD_RANKED]] == [(node_id, ["card-0"])]
    # …and the fold takes the pick into the track record the world model is primed with.
    assert [p["node_id"] for p in fold(engine.store.read_all()).foresight_selected] == [node_id]


def _serial_engine(run_dir) -> Engine:
    engine = Engine(run_dir, task=ToyTask(), researcher=_RankingPanel(), developer=_Developer(),
                    sandbox=SubprocessSandbox(),
                    policy=GreedyTree(n_seeds=0, max_nodes=4, debug_depth=0),
                    n_seeds=0, max_nodes=4, card_driven_selection=True, speculation_depth=0)
    engine._novelty_mode = "off"
    engine.store.append("run_started", {"run_id": engine.run_dir.name, "task_id": "toy",
                                        "goal": "g", "direction": "min",
                                        **engine._run_start_pinned_values()})
    return engine


def test_the_serial_card_claim_publishes_the_staged_ranking_on_the_main_task(tmp_path):
    """The serial path: the Card is claimed and built in a worker (`_offload_node_build`), where a
    board-wide row may not be appended, so the ranking is published by the MAIN task after the
    build returns — the foresight pick AND the board rows, against the claimed node's id.
    MUTATION: remove the call after `_offload_node_build(a, reserved=reservation)` -> nothing."""
    engine = _serial_engine(tmp_path / "run")
    assert _stage(engine) == ["card-0"]
    state = fold(engine.store.read_all())
    creates = engine._select_actions(state)
    assert creates == [{"kind": "draft", META_CARD_ID: "card-0"}], creates
    before = engine.store.read_all()[-1].seq

    anyio.run(lambda: engine._handle_create_actions(
        creates, state, created_no_terminal=0, no_mint_turns=0, decision_seq=before,
        max_es=None, max_s=None, start=0.0))

    created = [row for row in _rows(engine, EV_NODE_CREATED)
               if (row.get("idea") or {}).get("card_id") == "card-0"]
    assert len(created) == 1
    node_id = created[0]["node_id"]
    published = _published(engine)
    assert [(p["node_id"], p["card_id"]) for p in published[EV_FORESIGHT_SELECTED]] == [
        (node_id, "card-0")]
    assert [r["node_id"] for r in published[EV_HYPOTHESIS_RANKED]] == [node_id]
    assert [c["at_node"] for c in published[EV_CARD_RANKED]] == [node_id]


def test_only_the_cards_first_node_after_the_row_publishes_it(tmp_path):
    """A pure reading of the log: the ranking belongs to the build of the proposal that staged the
    Card. A LATER node of the same Card made no ranking, and a node created before the row was built
    from an older proposal — neither may publish it."""
    engine = Engine.__new__(Engine)

    def _node(nid, card):
        return Event(type=EV_NODE_CREATED, data={"node_id": nid, "idea": {"card_id": card}})

    row = Event(type=EV_CARD_RANKING_STAGED,
                data={"card_id": "card-3", "at_node": 5, "foresight": {"reason": "r"}})
    log = [_node(1, "card-3"), row, _node(5, "card-9"), _node(6, "card-3"), _node(7, "card-3")]
    assert engine._staged_card_ranking("card-3", 6, events=log) == (row.data, True)
    assert engine._staged_card_ranking("card-3", 7, events=log) is None, "node 6 took it first"
    assert engine._staged_card_ranking("card-3", 1, events=log) is None, "built before the row"
    assert engine._staged_card_ranking("card-9", 5, events=log) is None, "no row for card-9"
    # A node the log does not show as created publishes nothing (a build that minted no node).
    assert engine._staged_card_ranking("card-3", 8, events=log) is None
    # A RE-STAGING of the same Card starts a new claim: its first node after the new row takes it.
    restaged = log + [row, _node(8, "card-3")]
    assert engine._staged_card_ranking("card-3", 8, events=restaged) == (row.data, True)


def test_a_newer_board_decision_supersedes_the_staged_board_order():
    """The board pair is a last-write-wins register, so the staged board order is published only
    while it is the LATEST board decision in the log: a later staged row that carries a board order,
    or a `hypothesis_ranked` already published, supersedes it. A later row with only an idea pick
    does not — it decided nothing about the board."""
    engine = Engine.__new__(Engine)
    board = {"order": ["card-3"], "confidence": 0.6}
    row = Event(type=EV_CARD_RANKING_STAGED,
                data={"card_id": "card-3", "at_node": 5, "hyp_priority": board})
    built = Event(type=EV_NODE_CREATED, data={"node_id": 6, "idea": {"card_id": "card-3"}})
    newer_row = Event(type=EV_CARD_RANKING_STAGED,
                      data={"card_id": "card-4", "at_node": 6, "hyp_priority": {"order": []}})
    pick_only = Event(type=EV_CARD_RANKING_STAGED,
                      data={"card_id": "card-5", "at_node": 7, "foresight": {"reason": "r"}})
    ranked = Event(type=EV_HYPOTHESIS_RANKED, data={"node_id": 2, "order": []})

    assert engine._staged_card_ranking("card-3", 6, events=[row, built]) == (row.data, True)
    assert engine._staged_card_ranking("card-3", 6, events=[row, pick_only, built]) == (
        row.data, True)
    assert engine._staged_card_ranking("card-3", 6, events=[row, newer_row, built]) == (
        row.data, False)
    assert engine._staged_card_ranking("card-3", 6, events=[row, ranked, built]) == (
        row.data, False)
    assert engine._staged_card_ranking("card-3", 6, events=[ranked, row, built]) == (
        row.data, True), "a decision BEFORE the row is older than it"


class _TwoBoardsPanel(_RankingPanel):
    """Proposal n ranks the board as T_n: T1 = [card-0] at 0.6, then the NEWER T2 = [card-1, card-0]
    at 0.7 (the critic's repro of the stale-register defect, 2026-09-29)."""

    def propose(self, state, parent):
        idea = super().propose(state, parent)
        n = self.calls
        self.last_hyp_priority = {"order": ["card-0"] if n == 1 else ["card-1", "card-0"],
                                  "confidence": 0.5 + 0.1 * n, "n": n, "reason": f"board T{n}",
                                  "_trace_id": None, "_span_id": None}
        return idea


def _build_next(engine) -> str:
    """One create turn of the serial Card lane; returns the Card it built."""
    state = fold(engine.store.read_all())
    creates = [a for a in engine._select_actions(state) if META_CARD_ID in a]
    before = engine.store.read_all()[-1].seq
    anyio.run(lambda: engine._handle_create_actions(
        creates, state, created_no_terminal=0, no_mint_turns=0, decision_seq=before,
        max_es=None, max_s=None, start=0.0))
    return creates[0][META_CARD_ID]


def _close(engine, card_id: str) -> None:
    """Terminalize the node a Card was just built into, so the next turn may build."""
    node = next(r["node_id"] for r in _rows(engine, EV_NODE_CREATED)
                if (r.get("idea") or {}).get("card_id") == card_id)
    engine.store.append("node_failed", {"node_id": node, "generation": 0,
                                        "error": "closed by the test", "reason": "crash"})


def test_a_card_built_after_a_newer_staging_never_rolls_the_board_register_back(tmp_path):
    """The critic's repro: card-0 staged with T1, card-1 with the NEWER T2; the operator pins card-1,
    so card-1 is built FIRST. Publishing each Card's board order at its node's creation left the
    OLDER T1 in the register the Card selector reads (0.6) — in build order, the order selection
    chose. Now card-1's node publishes T2 and card-0's publishes only its idea pick.
    MUTATION: publish the board pair unconditionally -> the final register is T1 at 0.6."""
    engine = _serial_engine(tmp_path / "run")
    engine.researcher = _TwoBoardsPanel()
    assert _stage(engine) == ["card-0"] and _stage(engine) == ["card-1"]
    engine.store.append("card_reprioritized", {"id": "card-1", "priority": 0, "pinned": True,
                                               "source": "operator"})

    assert _build_next(engine) == "card-1"
    assert fold(engine.store.read_all()).card_ranking["order"] == ["card-1", "card-0"]
    _close(engine, "card-1")
    assert _build_next(engine) == "card-0"

    final = fold(engine.store.read_all())
    assert final.card_ranking["order"] == ["card-1", "card-0"]
    assert final.card_ranking["confidence"] == pytest.approx(0.7)
    assert [r["reason"] for r in _rows(engine, EV_HYPOTHESIS_RANKED)] == ["board T2"]
    assert sorted(p["card_id"] for p in _rows(engine, EV_FORESIGHT_SELECTED)) == [
        "card-0", "card-1"], "every Card's own idea pick is still published"


def test_the_batch_staging_lane_holds_each_rolls_ranking_for_its_own_card(tmp_path):
    """The shipped default-width path stages several drafts in ONE shared-Researcher pass
    (`_await_batch_proposal`), whose rolls each snapshot their own ranking
    (`novelty.py::_snapshot_role_telemetry`). Each Card gets ITS roll's pick, and only the newest
    board order is published. MUTATION: carry no ranking in the batch lane (the per-action lane's
    snapshot only) -> no row is recorded and neither node publishes its pick."""
    engine = _serial_engine(tmp_path / "run")
    engine.researcher = _TwoBoardsPanel()
    state = fold(engine.store.read_all())
    staged = anyio.run(functools.partial(engine._stage_card_creates,
                                         [{"kind": "draft"}, {"kind": "draft"}], state))
    assert staged == ["card-0", "card-1"]
    rows = _rows(engine, EV_CARD_RANKING_STAGED)
    assert [(r["card_id"], r["foresight"]["reason"], r["hyp_priority"]["reason"]) for r in rows] == [
        ("card-0", "pick 1", "board T1"), ("card-1", "pick 2", "board T2")]

    first = _build_next(engine)
    _close(engine, first)
    second = _build_next(engine)
    assert {first, second} == {"card-0", "card-1"}

    picks = {p["card_id"]: (p["node_id"], p["reason"]) for p in _rows(engine, EV_FORESIGHT_SELECTED)}
    nodes = {(r.get("idea") or {}).get("card_id"): r["node_id"] for r in _rows(engine, EV_NODE_CREATED)}
    assert picks == {"card-0": (nodes["card-0"], "pick 1"), "card-1": (nodes["card-1"], "pick 2")}
    assert [r["reason"] for r in _rows(engine, EV_HYPOTHESIS_RANKED)] == ["board T2"]
    assert fold(engine.store.read_all()).card_ranking["order"] == ["card-1", "card-0"]
