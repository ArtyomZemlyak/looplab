"""Auto-skill promotion is per CARD and mid-run, not only at the wrap-up pass (BACKLOG §0.17).

`memory.write_auto_skill` had exactly one caller — `lessons_distill.py::write_reflection_note`,
whose contract is the FINAL state — so a technique a run established at node 6 of 40 reached the
shared store only when the run finished, and a KILLED run (no pause, no finish) never wrote it at
all. The phase was never the problem (`looplab finalize` reaches the identical pass on a stopped
run); the TRIGGER was. `lessons.py::maybe_promote_skills` is the trigger.

Four properties, each driven rather than pinned:

  * `settled_skill_cards` is a statable truth table — which board rows are ripe, and none of the
    ones whose verdict can still move;
  * the mid-run pass really writes an `auto-*.md` card mid-run, on the `lessons_every` pace;
  * it is REPLAY-SAFE: a second pass at the same node count, and a fresh engine folded over the
    same log, promote nothing again — counted at the real writer, not asserted about the gate;
  * the run-end pass then SKIPS what the mid-run pass already promoted, so the classifier is still
    paid once per card, and says how many it skipped.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from looplab.adapters.toytask import ToyTask
from looplab.core.models import Card, Node, NodeStatus, Idea, RunState
from looplab.engine import memory as memory_mod
from looplab.engine.lessons_distill import (UNSETTLED_CARD_STATUSES, promoted_skill_keys,
                                            settled_skill_cards)
from looplab.engine.orchestrator import Engine
from looplab.events.replay import fold
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

ROOT = Path(__file__).resolve().parents[1]
# A statement the deterministic portability prefilter accepts, so the whole pass runs offline with
# no reflection client (`classify_skill_candidate`'s free rung).
STATEMENT = "Use hard-negative mining in contrastive retrieval training"


def _state_with_card(**card_kwargs) -> RunState:
    state = RunState(run_id="r", task_id="t", goal="g", direction="max")
    state.nodes[0] = Node(id=0, parent_ids=[], operator="draft",
                          idea=Idea(operator="draft", params={}, rationale=""),
                          status=NodeStatus.evaluated, metric=0.5)
    fields = {"id": "c1", "statement": STATEMENT, "verdict": "supported", "best_delta": 0.25,
              "evidence": [0], "status": "evaluated"}
    fields.update(card_kwargs)
    state.cards[fields["id"]] = Card(**fields)
    return state


def test_settled_skill_cards_admits_only_a_card_whose_verdict_cannot_move():
    assert [c.id for c in settled_skill_cards(_state_with_card(), set())] == ["c1"]

    # Not the run's own judgement yet.
    for verdict in ("open", "testing", "tested", "abandoned"):
        assert settled_skill_cards(_state_with_card(verdict=verdict), set()) == []
    # A record set over nothing is not support — rung 0 of the run-end pass, and mid-run it also
    # keeps a delta that could still become positive from being receipted as refused.
    for delta in (None, 0.0, -0.1):
        assert settled_skill_cards(_state_with_card(best_delta=delta), set()) == []
    # Work is still arriving for it: the lane says so…
    for status in UNSETTLED_CARD_STATUSES:
        assert settled_skill_cards(_state_with_card(status=status), set()) == []
    # …or an evidence node is still pending, which the lane check does NOT subsume (`evidence`
    # deliberately excludes the `node_building` marker the `building` lane is derived from).
    running = _state_with_card()
    running.nodes[1] = Node(id=1, parent_ids=[0], operator="tweak",
                            idea=Idea(operator="tweak", params={}, rationale=""),
                            status=NodeStatus.pending)
    running.cards["c1"].evidence = [0, 1]
    assert settled_skill_cards(running, set()) == []

    # Already promoted at this exact statement.
    digest = memory_mod.skill_source_digest(STATEMENT)
    assert settled_skill_cards(_state_with_card(), {("c1", digest)}) == []
    # …but the pair is the key, so another card making the same claim is still its own row, and an
    # edited statement is a claim that has not been assessed.
    assert [c.id for c in settled_skill_cards(_state_with_card(), {("other", digest)})] == ["c1"]


def _engine(tmp_path, *, lessons_every: int) -> Engine:
    task = ToyTask.load(ROOT / "examples" / "toy_task.json")
    researcher, developer = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=4),
                 reflection_priors=True, memory_dir=str(tmp_path / "mem"),
                 lessons_every=lessons_every)
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    eng.store.append("node_evaluated", {"node_id": 0, "metric": 0.5})
    eng._causal_meta_note = lambda *_args: "note"          # type: ignore[method-assign]
    eng._reflect_lessons = lambda *_args: []               # type: ignore[method-assign]
    eng._comparative_lessons_on = False
    return eng


def _folded_with_card(eng: Engine) -> RunState:
    state = fold(eng.store.read_all())
    state.cards["c1"] = Card(id="c1", statement=STATEMENT, verdict="supported",
                             best_delta=0.25, evidence=[0], status="evaluated")
    return state


def _promotion_rows(eng: Engine) -> list[dict]:
    return [e.data for e in eng.store.read_all() if e.type == "skills_promoted"]


def test_a_settled_card_reaches_the_shared_store_while_the_run_is_still_going(tmp_path,
                                                                             monkeypatch):
    writes: list = []
    real_write = memory_mod.write_auto_skill
    monkeypatch.setattr(memory_mod, "write_auto_skill",
                        lambda *a, **k: (writes.append(a[1]), real_write(*a, **k))[1])

    eng = _engine(tmp_path, lessons_every=1)
    state = eng._maybe_promote_skills(_folded_with_card(eng))

    cards = sorted((tmp_path / "mem" / "skills").glob("auto-*.md"))
    assert len(cards) == 1 and len(writes) == 1, "the technique must be on disk before the run ends"
    body = cards[0].read_text(encoding="utf-8")
    assert "status: candidate" in body and "provenance: auto" in body

    rows = _promotion_rows(eng)
    assert len(rows) == 1
    assert rows[0]["at_node"] == 1 and rows[0]["trigger"] == "cadence" and rows[0]["count"] == 1
    assert rows[0]["cards"] == ["c1"]
    assert rows[0]["promoted"] == [["c1", memory_mod.skill_source_digest(STATEMENT)]]
    assert promoted_skill_keys(eng.store.read_all()) == {
        ("c1", memory_mod.skill_source_digest(STATEMENT))}
    # The pass returns the RE-FOLDED state (it appended), the way every cadence here does.
    assert isinstance(state, RunState) and state.nodes[0].metric == 0.5

    # Replay-safety, counted at the writer. Two separate gates, and the second is the one that
    # matters: the cadence stops a second pass at the SAME node count, but the run keeps creating
    # nodes, so the window reopens with the same card still settled on the board. What must stop it
    # then is the durable `promoted` ledger — mutate `promoted_skill_keys(rows)` to `set()` in
    # `lessons.py` and only the grown-window assertion below turns red.
    eng._maybe_promote_skills(_folded_with_card(eng))
    assert len(writes) == 1 and len(_promotion_rows(eng)) == 1

    eng.store.append("node_created", {
        "node_id": 1, "parent_ids": [0], "operator": "tweak",
        "idea": {"operator": "tweak", "params": {}, "rationale": ""}})
    eng.store.append("node_evaluated", {"node_id": 1, "metric": 0.9})
    eng._maybe_promote_skills(_folded_with_card(eng))
    rows = _promotion_rows(eng)
    assert len(rows) == 2 and rows[1]["at_node"] == 2, "the cadence window did reopen"
    assert rows[1]["cards"] == [] and rows[1]["promoted"] == []
    assert len(writes) == 1, "a card already promoted at this statement is not judged again"

    # A resumed run folds the same durable log and reaches the same answer.
    resumed = _engine(tmp_path, lessons_every=1)
    resumed._maybe_promote_skills(_folded_with_card(resumed))
    assert len(writes) == 1, "a resumed run must not re-pay the classifier for a promoted card"

    # …and the ledger is per CARD, not a run-wide latch: a second settled card still promotes.
    second = _folded_with_card(eng)
    second.cards["c2"] = Card(id="c2", statement="Warm up the learning rate over the first epoch",
                              verdict="supported", best_delta=0.1, evidence=[0],
                              status="evaluated")
    resumed_engine_rows = len(_promotion_rows(eng))
    eng.store.append("node_created", {
        "node_id": 2, "parent_ids": [0], "operator": "tweak",
        "idea": {"operator": "tweak", "params": {}, "rationale": ""}})
    eng.store.append("node_evaluated", {"node_id": 2, "metric": 0.95})
    grown = fold(eng.store.read_all())
    grown.cards.update(second.cards)
    eng._maybe_promote_skills(grown)
    assert len(_promotion_rows(eng)) == resumed_engine_rows + 1
    assert len(writes) == 2 and _promotion_rows(eng)[-1]["cards"] == ["c2"]


def test_the_run_end_pass_skips_what_the_mid_run_pass_already_promoted(tmp_path, monkeypatch):
    writes: list = []
    real_write = memory_mod.write_auto_skill
    monkeypatch.setattr(memory_mod, "write_auto_skill",
                        lambda *a, **k: (writes.append(a[1]), real_write(*a, **k))[1])

    eng = _engine(tmp_path, lessons_every=1)
    eng._maybe_promote_skills(_folded_with_card(eng))
    assert len(writes) == 1

    eng.store.append("run_finished", {"reason": "done", "finalization_required": True})
    final = _folded_with_card(eng)
    eng._write_reflection_note(final)

    note = [e.data for e in eng.store.read_all() if e.type == "reflection_note"][-1]
    assert len(writes) == 1, "the classifier is paid once per card, whichever pass got there first"
    assert note["n_skills"] == 0 and note["skill_candidates"] == []
    assert note["n_skills_promoted_earlier"] == 1, (
        "'this run promoted nothing at the end' must be readable apart from 'promoted nothing'")


def test_with_the_cadence_off_promotion_stays_exactly_where_it_was(tmp_path):
    """`lessons_every: 0` — the `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` value, so every resumed pre-field
    run — leaves the mid-run pass inert and the run-end pass doing all of the work."""
    eng = _engine(tmp_path, lessons_every=0)
    eng._maybe_promote_skills(_folded_with_card(eng))
    assert _promotion_rows(eng) == []
    assert not (tmp_path / "mem" / "skills").exists()

    eng.store.append("run_finished", {"reason": "done", "finalization_required": True})
    eng._write_reflection_note(_folded_with_card(eng))
    note = [e.data for e in eng.store.read_all() if e.type == "reflection_note"][-1]
    assert note["n_skills"] == 1 and note["n_skills_promoted_earlier"] == 0
    assert len(sorted((tmp_path / "mem" / "skills").glob("auto-*.md"))) == 1


def test_the_pass_does_not_fire_while_an_evaluation_is_in_flight(tmp_path):
    """`at_creation_boundary` is the precondition every node-count consumer shares (F1i)."""
    eng = _engine(tmp_path, lessons_every=1)
    eng.store.append("node_created", {
        "node_id": 1, "parent_ids": [0], "operator": "tweak",
        "idea": {"operator": "tweak", "params": {}, "rationale": ""}})
    state = _folded_with_card(eng)
    assert state.pending_nodes(), "the fixture must actually have a pending node"
    eng._maybe_promote_skills(state)
    assert _promotion_rows(eng) == []


@pytest.mark.parametrize("attribute", ["_maybe_promote_skills"])
def test_the_pass_runs_in_the_capped_enrichment_lane(attribute):
    """A capped lane's producers are a registry (`core/llm_broker.py::BACKGROUND_LANE_PRODUCERS`):
    a paid cadence outside it runs uncapped and reads as an unexplained stall."""
    from looplab.core.llm_broker import BACKGROUND_LANE_PRODUCERS

    assert f"orchestrator.py::{attribute}" in BACKGROUND_LANE_PRODUCERS["enrichment"]
