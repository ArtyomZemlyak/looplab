"""A CLAIMED CARD'S BUILD RUNS THE WHOLE IDEA (doc 69 §3.3, 69.4; `Settings.card_full_rationale`).

A native Card keeps its rationale to 400 characters — its `card_added` row, the ledger and the board
brief all read that bounded text — and a CLAIM of the Card rebuilt the executed Idea from it
(`_rebuilt_claim_idea`), so the Developer ran a cut idea. On the Card-driven lane every build is a
claim (the real run below: OFF, every Card-built node carries the first 400 characters), and so is a
re-queue after `developer_stuck`. `minionerec-backbones-v10`: card-4's recipe
went 2,410 -> 400 characters, card-5's reached the plan as "No code change needed".

ON, the mint row also carries `rationale_full` — beside the receipt, OUTSIDE `action` and every
digest, only when the 400-character cut applied, capped at `CARD_RATIONALE_FULL_MAX` with a sentence
when that cut it — and the claim executes it when it extends the Card's own rationale. OFF writes and
executes the historical bytes. These drive the real mint -> real fold -> real claim of
`tests/test_card_concept_round_trip.py`, and then a real Card-driven run.
"""
from __future__ import annotations

import ast
from pathlib import Path

import anyio

from looplab.agents.toy_roles import ToyResearcher
from looplab.core.config import (LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings,
                                 settings_from_snapshot)
from looplab.core.models import Event, Idea
from looplab.engine.card_reservation import CARD_RATIONALE_FULL_MAX, CARD_RATIONALE_MAX
from looplab.engine.options import EngineOptions
from looplab.events.replay import fold
from looplab.events.types import EV_CARD_ADDED
from looplab.search.card_selection import card_action as projected_card_action
from tests.factories import make_engine

ROOT = Path(__file__).resolve().parents[1]
# A recipe the way card-4's was: the first 400 characters are the premise, the rest is the how.
RECIPE = ("Swap the backbone to Qwen2.5-0.5B and run GRPO after SFT. " * 7)[:400] + (
    "HOW: build the prefix CSV once with `scripts/prefix.py --out data/prefix.csv`, then point "
    "`rl.dataset.prefix_path` at it; keep `kl_coef=0.04`; the reward is FilteredUnseenRecall@20 on "
    "the dev split. " * 20)


def _idea(rationale: str = RECIPE, **overrides) -> Idea:
    base = dict(operator="draft", params={"x": 1.0}, rationale=rationale,
                hypothesis="GRPO after SFT lifts recall")
    base.update(overrides)
    return Idea(**base)


def _mint(engine, idea, *, full: bool):
    events = engine.store.read_all()
    plan = engine._plan_native_card(
        events, fold(events), idea, parents=[], parent_generations={},
        scored_against=None, source="researcher", at_node=1, full_rationale=full)
    assert plan.disposition == "mint", plan.disposition
    engine.store.append(EV_CARD_ADDED, plan.payload)
    return plan


def _claim(engine, card_id):
    events = engine.store.read_all()
    state = fold(events)
    card = state.cards[card_id]
    action = projected_card_action(card)
    assert action is not None
    engine._card_claim_refusal = None
    reservation = engine._prepare_existing_card_claim(events, state, action, card, node_id=1)
    assert reservation is not None, engine._card_claim_refusal or "anonymous refusal"
    return reservation.idea, card


def _row(engine):
    return next(e for e in engine.store.read_all() if e.type == EV_CARD_ADDED).data


# ----------------------------------------------------------------------------- mint + claim
def test_a_claim_executes_the_whole_rationale_the_researcher_wrote(tmp_path):
    engine = make_engine(tmp_path / "run", n_seeds=0, max_nodes=4, card_driven_selection=True)
    plan = _mint(engine, _idea(), full=True)
    row = _row(engine)
    assert row["rationale"] == RECIPE[:CARD_RATIONALE_MAX]          # the Card stays bounded
    assert row["rationale_full"] == RECIPE                            # the whole idea rides beside
    assert "rationale_full" not in row["idea"], "never inside the digested action block"
    executed, card = _claim(engine, plan.card_id)
    assert card.rationale == RECIPE[:CARD_RATIONALE_MAX]
    assert executed.rationale == RECIPE                               # the defect: was 400 chars
    assert card.selection_ready is True


def test_off_writes_and_executes_the_historical_bytes(tmp_path):
    """MUTATION: write the key regardless of the switch -> the OFF row gains it."""
    engine = make_engine(tmp_path / "run", n_seeds=0, max_nodes=4, card_driven_selection=True)
    plan = _mint(engine, _idea(), full=False)
    assert "rationale_full" not in _row(engine)
    executed, _card = _claim(engine, plan.card_id)
    assert executed.rationale == RECIPE[:CARD_RATIONALE_MAX]


def test_a_rationale_that_fits_writes_the_historical_row_byte_for_byte(tmp_path):
    """A proposal the cut never touched is the same row either way — so a run's short rationales
    cost no crash-prefix match and no row shape. MUTATION: write the key whenever ON."""
    short = "Try a cosine schedule."
    rows = []
    for full in (True, False):
        engine = make_engine(tmp_path / f"run{full}", n_seeds=0, max_nodes=4,
                             card_driven_selection=True)
        _mint(engine, _idea(short), full=full)
        rows.append(_row(engine))
    assert rows[0] == rows[1] and "rationale_full" not in rows[0]


def test_a_rationale_past_the_whole_bound_is_cut_with_a_sentence_never_silently(tmp_path):
    huge = "z" * (CARD_RATIONALE_FULL_MAX + 5_000)
    engine = make_engine(tmp_path / "run", n_seeds=0, max_nodes=4, card_driven_selection=True)
    plan = _mint(engine, _idea(huge), full=True)
    full = _row(engine)["rationale_full"]
    assert full.startswith("z" * CARD_RATIONALE_FULL_MAX)
    assert full.endswith(f"the Researcher's was {len(huge):,} characters; the Card keeps the "
                         f"first {CARD_RATIONALE_FULL_MAX:,}]")
    executed, _card = _claim(engine, plan.card_id)
    assert executed.rationale == full


def test_a_row_whose_whole_text_is_not_this_card_s_is_ignored(tmp_path):
    """The claim trusts `rationale_full` only when it EXTENDS the Card's own rationale: a row
    edited in place (or written by anything else) falls back to the bounded text. MUTATION: drop
    the prefix check -> the foreign text is executed."""
    engine = make_engine(tmp_path / "run", n_seeds=0, max_nodes=4, card_driven_selection=True)
    plan = _mint(engine, _idea(), full=True)
    events = engine.store.read_all()
    forged = []
    for event in events:
        data = dict(event.data)
        if event.type == EV_CARD_ADDED:
            data["rationale_full"] = "something else entirely " * 40
        forged.append(Event(seq=event.seq, type=event.type, data=data, ts=event.ts))
    state = fold(forged)
    card = state.cards[plan.card_id]
    engine._card_claim_refusal = None
    reservation = engine._prepare_existing_card_claim(
        forged, state, projected_card_action(card), card, node_id=1)
    assert reservation is not None
    assert reservation.idea.rationale == RECIPE[:CARD_RATIONALE_MAX]


def test_the_crash_prefix_mint_is_still_recognised_under_the_same_switch(tmp_path):
    """A run killed between its `card_added` and the claim re-plans the same proposal: the orphan
    row must match the writer's shape, or the upgrade mints a DUPLICATE Card for it. Both halves read
    the same switch, so the shape agrees. MUTATION: build the matcher's expected row without
    `full_rationale` -> the orphan is declined."""
    engine = make_engine(tmp_path / "run", n_seeds=0, max_nodes=4, card_driven_selection=True)
    plan = _mint(engine, _idea(), full=True)
    row = _row(engine)
    card_action = engine._card_action(_idea(), [], {}, None, None,
                                      scored_against_empty=row["scored_against_empty"])
    assert engine._card_event_matches(row, _idea(), card_action, source="researcher", at_node=1,
                                      implementation_ref=None, full_rationale=True)
    assert not engine._card_event_matches(row, _idea(), card_action, source="researcher",
                                          at_node=1, implementation_ref=None,
                                          full_rationale=False)


# -------------------------------------------------------------------------------- settings
def test_on_for_new_runs_off_for_a_pre_field_snapshot_and_off_at_every_constructor():
    """MUTATION: drop the LEGACY row -> the pre-field snapshot reads ON."""
    assert Settings().card_full_rationale is True
    assert EngineOptions().card_full_rationale is False
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["card_full_rationale"] is False
    legacy = Settings().masked_snapshot()
    for field in LEGACY_CONFIG_SNAPSHOT_DEFAULTS:
        legacy.pop(field, None)
    legacy.pop("config_snapshot_schema", None)
    assert settings_from_snapshot(legacy).card_full_rationale is False
    assert EngineOptions.from_settings(Settings()).card_full_rationale is True


def _names_the_one_reader(value) -> bool:
    """`card_full_rationale(self)` — the ONE reader — or a bare `full_rationale` forward."""
    if isinstance(value, ast.Name):
        return value.id == "full_rationale"
    return (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id == "card_full_rationale"
            and [ast.unparse(a) for a in value.args] == ["self"])


def test_every_card_writer_call_reads_the_one_reader():
    """The row's shape is decided at SIX call sites plus the forwards inside the planner and the
    matcher; a site that forgets the switch — or spells it `False` — writes the historical row under
    an ON run, and its crash-prefix orphan then stops matching the sites that did not. Only one lane
    is driven end to end below, so the rule is held here for all of them: AST, not text, every call
    of the three writers in the engine package passes `full_rationale=` as the one reader or as a
    forward of its own parameter. MUTATION: `full_rationale=False` at any site."""
    wrong = []
    for path in sorted((ROOT / "looplab" / "engine").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("_plan_native_card", "_card_added_payload",
                                           "_card_event_matches")):
                values = [k.value for k in node.keywords if k.arg == "full_rationale"]
                if len(values) != 1 or not _names_the_one_reader(values[0]):
                    wrong.append(f"{path.name}:{node.lineno} {node.func.attr}")
    assert not wrong, wrong


# ----------------------------------------------------------------------------- a real run
class _LongRationaleResearcher(ToyResearcher):
    """The toy proposals with a recipe-length rationale and a hypothesis of their own — a Card's
    statement falls back to the rationale when there is no hypothesis, and a 4,000-character one is
    no statement at all (the proposal is then refused as a Card, whatever this switch says)."""

    def propose(self, state, parent):
        idea = super().propose(state, parent)
        return idea.model_copy(update={
            "rationale": RECIPE,
            "hypothesis": f"GRPO after SFT at x={idea.params.get('x', 0.0):.4f}"})


def _run(tmp_path, *, on: bool):
    run_dir = tmp_path / ("on" if on else "off")
    engine = make_engine(run_dir, researcher=_LongRationaleResearcher({"x": (-5.0, 5.0),
                                                                        "y": (-5.0, 5.0)}),
                         n_seeds=1, max_nodes=3, card_driven_selection=True, speculation_depth=0,
                         card_full_rationale=on)
    anyio.run(engine.run)
    return fold(engine.store.read_all())


def test_a_real_card_driven_run_builds_its_claimed_cards_from_the_whole_idea(tmp_path):
    """End to end, over the folded read model: every node built from a Card carries the idea its
    Researcher wrote — OFF, the same run's claimed nodes carry the first 400 characters."""
    for on, expect in ((True, RECIPE), (False, RECIPE[:CARD_RATIONALE_MAX])):
        state = _run(tmp_path, on=on)
        claimed = [n for n in state.nodes.values() if n.idea.card_id is not None]
        assert claimed, "the run took no Card-driven build, so it proves nothing"
        assert {n.idea.rationale for n in claimed} == {expect}, on
