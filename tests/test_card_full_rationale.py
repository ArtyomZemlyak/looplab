"""A CLAIMED CARD'S BUILD RUNS THE WHOLE IDEA (doc 69 §3.3, 69.4; `Settings.card_full_rationale`).

A native Card keeps its rationale to 400 characters — its `card_added` row, the ledger and the board
brief all read that bounded text — and a CLAIM of the Card rebuilt the executed Idea from it
(`_rebuilt_claim_idea`), so the Developer ran a cut idea. On the Card-driven lane the proposal lane
builds by claiming (the real run below: OFF, every Card-built node carries the first 400
characters), and so does a re-queue after `developer_stuck`; an inject, a refine, a rerun and an
attach build the proposal's own Idea and were never cut. `minionerec-backbones-v10`: card-4's recipe
went 2,410 -> 400 characters, card-5's reached the plan as "No code change needed".

ON, the mint row also carries `rationale_full` — beside the receipt, OUTSIDE `action` and every
digest, only when the 400-character cut applied, capped at `CARD_RATIONALE_FULL_MAX` with a sentence
when that cut it — and the claim executes it when it extends the Card's own rationale. OFF writes and
executes the historical bytes, for a Card minted while the switch was on as well. The switch is
editable on a stopped run, so the crash-prefix / live-dedupe matcher compares the whole text on its
own terms rather than as part of the writer's exact shape (critic 2026-09-29: a flipped switch
minted a second Card and built an inject twice). These drive the real mint -> real fold -> real claim
of `tests/test_card_concept_round_trip.py`, and then a real Card-driven run.
"""
from __future__ import annotations

import ast
from pathlib import Path

import anyio

from types import SimpleNamespace

from looplab.agents.toy_roles import ToyResearcher
from looplab.core.config import (LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings,
                                 settings_from_snapshot)
from looplab.core.models import Event, Idea
from looplab.engine.card_reservation import (CARD_RATIONALE_FULL_MAX, CARD_RATIONALE_MAX,
                                             CardReservationMixin)
from looplab.engine.options import EngineOptions
from looplab.events.replay import fold
from looplab.events.types import EV_CARD_ADDED, EV_INJECT_FAILED, EV_INJECT_NODE
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
    engine = make_engine(tmp_path / "run", n_seeds=0, max_nodes=4, card_driven_selection=True,
                         card_full_rationale=True)
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
    engine = make_engine(tmp_path / "run", n_seeds=0, max_nodes=4, card_driven_selection=True,
                         card_full_rationale=True)
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
    engine = make_engine(tmp_path / "run", n_seeds=0, max_nodes=4, card_driven_selection=True,
                         card_full_rationale=True)
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


def test_the_crash_prefix_mint_is_recognised_whichever_way_the_switch_now_reads(tmp_path):
    """A run killed between its `card_added` and the claim re-plans the same proposal: the orphan
    row must match, or the planner mints a DUPLICATE Card for it — and the switch is editable on a
    stopped run, so the row it finds may have been written under the other value (critic
    2026-09-29, driven: ON-mint / OFF-replan minted `card-1`). MUTATION: compare `rationale_full`
    as part of the exact writer shape again -> the flipped re-plans mint a second Card."""
    for mint_full, replan_full in ((True, False), (False, True), (True, True), (False, False)):
        engine = make_engine(tmp_path / f"run-{mint_full}-{replan_full}", n_seeds=0, max_nodes=4,
                             card_driven_selection=True)
        plan = _mint(engine, _idea(), full=mint_full)
        events = engine.store.read_all()
        again = engine._plan_native_card(
            events, fold(events), _idea(), parents=[], parent_generations={},
            scored_against=None, source="researcher", at_node=1, full_rationale=replan_full)
        assert (again.disposition, again.card_id) == ("reuse", plan.card_id), (mint_full,
                                                                              replan_full)


def test_a_recorded_whole_text_that_is_another_proposal_s_declines_the_reuse(tmp_path):
    """The matcher admits the row's `rationale_full` only when it is exactly this proposal's: a row
    carrying another text is another author's claim. MUTATION: drop the key from the comparison
    without checking it -> the forged row is reused."""
    engine = make_engine(tmp_path / "run", n_seeds=0, max_nodes=4, card_driven_selection=True)
    plan = _mint(engine, _idea(), full=True)
    row = _row(engine)
    card_action = engine._card_action(_idea(), [], {}, None, None,
                                      scored_against_empty=row["scored_against_empty"])
    assert engine._card_event_matches(row, _idea(), card_action, source="researcher", at_node=1,
                                      implementation_ref=None)
    forged = dict(row, rationale_full=row["rationale"] + " and something else entirely")
    assert not engine._card_event_matches(forged, _idea(), card_action, source="researcher",
                                          at_node=1, implementation_ref=None)
    bare = {key: value for key, value in row.items() if key != "rationale_full"}
    assert engine._card_event_matches(bare, _idea(), card_action, source="researcher",
                                      at_node=1, implementation_ref=None)
    assert plan.card_id == row["id"]


def test_two_identical_injects_across_a_flipped_switch_build_once(tmp_path):
    """The live dedupe is the same matcher: an identical operator inject served after the switch was
    flipped must read as the in-flight twin (`card_duplicate`), not as new work — before the fix it
    minted `card-1` and built the operator's experiment a second time (critic 2026-09-29, driven)."""
    idea = {"operator": "manual", "params": {"x": 1.0}, "rationale": RECIPE,
            "hypothesis": "GRPO after SFT lifts recall"}
    for first, second in ((True, False), (False, True)):
        run_dir = tmp_path / f"inject-{first}-{second}"
        e1 = make_engine(run_dir, n_seeds=1, max_nodes=3, card_full_rationale=first)
        e1.store.append("run_started", {"run_id": "r", "task_id": "toy", "direction": "min"})
        e1.store.append(EV_INJECT_NODE, {"idea": dict(idea)})
        e1.store.append(EV_INJECT_NODE, {"idea": dict(idea)})
        assert anyio.run(e1._serve_forced_requests, fold(e1.store.read_all())) is True
        e2 = make_engine(run_dir, n_seeds=1, max_nodes=3, card_full_rationale=second)
        assert anyio.run(e2._serve_forced_requests, fold(e2.store.read_all())) is True
        events = e2.store.read_all()
        manual = [n.id for n in fold(events).nodes.values() if n.operator == "manual"]
        assert len(manual) == 1, (first, second, manual)
        assert [e.data["id"] for e in events if e.type == EV_CARD_ADDED] == ["card-0"]
        assert [e.data.get("reason") for e in events if e.type == EV_INJECT_FAILED] == [
            "card_duplicate"]


def test_a_claim_reads_the_switch_it_runs_under(tmp_path):
    """OFF executes the historical 400 characters even for a Card minted while the switch was on;
    ON executes the whole text only where the mint row carries it. MUTATION: ignore the switch at
    the claim -> the OFF engine builds the 4,380-character recipe."""
    on = make_engine(tmp_path / "run", n_seeds=0, max_nodes=4, card_driven_selection=True,
                     card_full_rationale=True)
    plan = _mint(on, _idea(), full=True)
    off = make_engine(tmp_path / "run", n_seeds=0, max_nodes=4, card_driven_selection=True)
    executed, _card = _claim(off, plan.card_id)
    assert executed.rationale == RECIPE[:CARD_RATIONALE_MAX]
    executed, _card = _claim(on, plan.card_id)
    assert executed.rationale == RECIPE


def test_the_claim_rule_is_a_prefix_bound_and_a_type_check():
    """MUTATIONS: drop the length bound, accept a non-string, or accept an empty bounded text as a
    prefix -> an oversized, garbled or unanchored row is executed (or the claim raises)."""
    bounded = RECIPE[:CARD_RATIONALE_MAX]
    card = SimpleNamespace(rationale=bounded)
    rule = CardReservationMixin._claim_rationale
    assert rule(card, {"rationale_full": RECIPE}, full_rationale=True) == RECIPE
    too_long = bounded + "z" * (CARD_RATIONALE_FULL_MAX + 200)
    assert rule(card, {"rationale_full": too_long}, full_rationale=True) == bounded
    for garbled in ([RECIPE], 7, None, {"x": RECIPE}):
        assert rule(card, {"rationale_full": garbled}, full_rationale=True) == bounded
    assert rule(SimpleNamespace(rationale=""), {"rationale_full": RECIPE},
                full_rationale=True) == ""
    assert rule(card, {"rationale_full": RECIPE}, full_rationale=False) == bounded


def test_the_row_writes_the_key_exactly_past_the_cut_and_cuts_exactly_past_its_bound(tmp_path):
    """The two boundaries, literally: 400 characters fit (the historical row), 401 do not; 8,000
    ride whole, 8,001 are cut with a sentence. MUTATIONS: `<` for `<=` at the first bound, `>=` for
    `>` at the second -> a redundant key, or a false "cut here"."""
    assert CARD_RATIONALE_MAX == 400 and CARD_RATIONALE_FULL_MAX == 8_000
    field = CardReservationMixin._full_rationale_field
    assert field(_idea("a" * 400)) == {}
    assert field(_idea("a" * 401)) == {"rationale_full": "a" * 401}
    assert field(_idea("b" * 8_000)) == {"rationale_full": "b" * 8_000}
    assert field(_idea("b" * 8_001))["rationale_full"].startswith("b" * 8_000 + "\n[rationale cut")
    engine = make_engine(tmp_path / "run", n_seeds=0, max_nodes=4, card_driven_selection=True,
                         card_full_rationale=True)
    _mint(engine, _idea("c" * 450), full=True)
    assert _row(engine)["rationale"] == "c" * 400


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


def test_the_one_reader_defaults_off_for_an_object_that_never_ran_init():
    """MUTATION: default the reader to True -> a stub with no knob writes and claims the whole text."""
    from looplab.engine.shared import card_full_rationale
    assert card_full_rationale(SimpleNamespace()) is False


# The switch-aware writers, and the ONE call that builds the row WITHOUT the switch on purpose: the
# matcher, which compares the whole rationale on its own terms (`_card_event_matches`).
_SWITCHED = ("_plan_native_card", "_card_added_payload", "_claim_rationale")
_SWITCH_FREE = {("_card_event_matches", "_card_added_payload")}


def _parameters(function) -> set:
    args = function.args
    return {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}


def _reads_the_switch(value, parameters: set) -> bool:
    """`card_full_rationale(self)` — the ONE reader — or a forward of the enclosing function's OWN
    parameter; a bare name that resolves anywhere else (a module global) is neither."""
    if isinstance(value, ast.Name):
        return value.id == "full_rationale" and value.id in parameters
    return (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id == "card_full_rationale"
            and [ast.unparse(a) for a in value.args] == ["self"])


def test_every_card_writer_call_reads_the_one_reader():
    """The row's shape and the executed text are decided at the writer, planner and claim call
    sites; a site that forgets the switch — or spells it `False`, or forwards a name that is not
    its own parameter (a module-level `full_rationale = False` passed the name-only version of this
    guard, critic 2026-09-29) — writes or executes the historical bytes under an ON run. Only one
    lane is driven end to end below, so the rule is held here for all of them: AST, not text.
    MUTATIONS: `full_rationale=False` at any site; a forward of a module global."""
    wrong, free = [], []
    for path in sorted((ROOT / "looplab" / "engine").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            parameters = _parameters(function)
            for node in ast.walk(function):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in _SWITCHED):
                    continue
                values = [k.value for k in node.keywords if k.arg == "full_rationale"]
                where = f"{path.name}:{node.lineno} {function.name} -> {node.func.attr}"
                if (function.name, node.func.attr) in _SWITCH_FREE:
                    free.append(where)
                    if not (len(values) == 1 and isinstance(values[0], ast.Constant)
                            and values[0].value is False):
                        wrong.append(where)
                elif len(values) != 1 or not _reads_the_switch(values[0], parameters):
                    wrong.append(where)
    assert not wrong, wrong
    assert len(free) == 1, free


def test_the_calibration_validator_s_bound_is_the_writer_s():
    """`search/` imports no engine, so the quality layer spells the whole rationale's bound as a
    literal; this pins it to the writer's constant and its cut sentence's room."""
    from looplab.search import speculation_quality as quality
    assert quality._CALIBRATION_RATIONALE_FULL_MAX == CARD_RATIONALE_FULL_MAX + 200
    assert quality._CALIBRATION_CARD_ADDED_OPTIONAL_FIELDS == {"rationale_full"}


def test_a_calibration_replicate_may_carry_the_whole_rationale(tmp_path):
    """Under the default the calibration envelope writes `rationale_full` for a long proposal, and
    the validator refused it as "not one exact native registration" (critic 2026-09-29). It admits
    the key exactly when it extends the row's own rationale. MUTATIONS: the exact key set again ->
    the first run raises; drop the prefix check -> the second passes."""
    import pytest
    from looplab.search import speculation_quality as quality
    from test_speculation_quality_gate import _make_run, _rewrite_first_event_data

    run = _make_run(tmp_path / "whole", treatment=False, seed=0)
    _rewrite_first_event_data(
        run, "card_added",
        lambda data: data.__setitem__("rationale_full", data["rationale"] + " — and the how."),
        where=lambda data: data.get("id") == "card-1")
    quality.analyze_speculation_run(run)
    forged = _make_run(tmp_path / "forged", treatment=False, seed=0)
    _rewrite_first_event_data(
        forged, "card_added",
        lambda data: data.__setitem__("rationale_full", "another proposal's text entirely"),
        where=lambda data: data.get("id") == "card-1")
    with pytest.raises(ValueError, match="ownership/proposal receipt is invalid"):
        quality.analyze_speculation_run(forged)


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
