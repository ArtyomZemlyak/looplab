"""What a `supported` card verdict rests on, said on the board (doc 67 67.1, 2026-09-26).

`events/card_ledger.py::_evidence_verdict` calls a card supported when ONE measurement of one
experiment beat its parent, or the standing record — strictly, with no look at the replications the
run may already hold (`confirmed_mean` over `confirmed_seeds`, which already decide the champion), at
the >1-SE rule, or at the run's measured eval noise floor. The proposal board shows that verdict to
the Researcher, so a gain inside the noise read as a finding and steered the next proposals.

`verdict_support` classifies the gains the verdict counts — replicated / single_run / within_noise /
not_replicated — and `Settings.card_verdict_support` puts it on a supported card's board row. Driven
here over states the REAL fold built from REAL events, through the real engine stamp and both propose
paths; OFF is pinned against the historical bytes by sha256 (computed on the pre-change tree).
"""
from __future__ import annotations

import ast
import hashlib
import math

import pytest

from looplab.agents.roles import RESEARCHER_HINT_ATTRS, LLMResearcher, _state_brief
from looplab.agents.state_brief import SUPPORT_LEVEL_TEXT, support_legend
from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings, settings_from_snapshot
from looplab.core.models import Idea, durable_idea_payload
from looplab.engine.options import EngineOptions
from looplab.events.card_ledger import (SUPPORT_LEVELS, SUPPORT_NOT_REPLICATED, SUPPORT_REPLICATED,
                                        SUPPORT_SINGLE_RUN, SUPPORT_WITHIN_NOISE, verdict_support)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from tests.factories import make_engine


# Two protocol `profile` facets, as `engine/comparability.py::protocol_record` digests them.
SMOKE, FULL = "a" * 16, "b" * 16


def _provenance(ruler: str) -> dict:
    """A terminal's `metric_provenance` carrying the ruler its number was measured on."""
    return {"comparability": {"version": 1, "authority": "declared", "keys": {"declared": "d" * 16},
                              "protocol": {"profile": ruler}}}


def _run(tmp_path, nodes, *, direction="max", confirmed=(), floor_std=None, floor=None,
         profiles=None, rulers=None):
    """`nodes` is [(id, parents, metric)]; `confirmed` is [(id, mean, std, seeds)]; `floor` adds
    keys to the `eval_noise_floor` row (its `profile`, `protocol_profile`, a `reason`); `profiles` is
    {id: eval_profile}; `rulers` is {id: the protocol facet its terminal recorded}."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g",
                                 "direction": direction})
    for i, parents, metric in nodes:
        idea = Idea(operator="improve" if parents else "draft", params={"lr": 0.001 * (i + 1)},
                    rationale=f"exp {i}", hypothesis=f"h{i}",
                    eval_profile=(profiles or {}).get(i))
        store.append("node_created", {"node_id": i, "parent_ids": parents, "operator": idea.operator,
                                      "idea": durable_idea_payload(idea), "code": "", "files": {},
                                      "generation": 0})
        store.append("node_evaluated", {"node_id": i, "generation": 0, "metric": metric,
                                        "eval_seconds": 1.0, "extra_metrics": {}, "stdout_tail": "",
                                        "trials": [], "violations": [],
                                        **({"metric_provenance": _provenance(rulers[i])}
                                           if rulers and i in rulers else {})})
    for i, mean, std, seeds in confirmed:
        store.append("node_confirmed", {"node_id": i, "generation": 0, "mean": mean, "std": std,
                                        "seeds": seeds})
    if floor_std is not None:
        store.append("eval_noise_floor", {"node_id": 0, "generation": 0, "seeds": [1, 2, 3],
                                          "metrics": [0.7, 0.7, 0.7], "n": 3, "mean": 0.7,
                                          "std": floor_std, "sem": floor_std / math.sqrt(3),
                                          "spread": 2 * floor_std, **(floor or {})})
    return fold(store.read_all())


# ------------------------------------------------------------------ the classification, as a rule
def test_one_measurement_that_beat_its_parent_is_a_single_run(tmp_path):
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.72)])
    assert verdict_support([1], st) == SUPPORT_SINGLE_RUN


def test_a_gain_inside_the_measured_noise_floor_says_so(tmp_path):
    """The floor's `std` is the spread of ONE evaluation of an unchanged candidate, so two single
    measurements differ with sd std·√2: a +0.02 gain is inside std 0.02 (bound 0.028) and outside
    std 0.01 (bound 0.014)."""
    inside = _run(tmp_path / "a", [(0, [], 0.70), (1, [0], 0.72)], floor_std=0.02)
    outside = _run(tmp_path / "b", [(0, [], 0.70), (1, [0], 0.72)], floor_std=0.01)
    assert verdict_support([1], inside) == SUPPORT_WITHIN_NOISE
    assert verdict_support([1], outside) == SUPPORT_SINGLE_RUN


@pytest.mark.parametrize("std,level", [(0.01, SUPPORT_REPLICATED), (0.2, SUPPORT_NOT_REPLICATED)])
def test_a_confirmed_gain_is_held_to_the_confirm_gates_own_rule(tmp_path, std, level):
    """`core/fitness.py::one_se_better` — the rule the confirm gate uses — over two confirmations
    (3 seeds each): mean 0.72 against the parent's confirmed 0.70 clears 1 SE of the difference at
    std 0.01 and does not at std 0.2."""
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.73)],
              confirmed=[(0, 0.70, 0.01, 3), (1, 0.72, std, 3)])
    assert verdict_support([1], st) == level


def test_a_replicated_parent_is_compared_as_a_mean(tmp_path):
    """Both sides replicated: SE of the difference pools both spreads, so a gain (0.045) that
    clears the candidate's own SE (0.006) can still sit inside the pair's (0.058)."""
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.73)],
              confirmed=[(0, 0.70, 0.1, 3), (1, 0.745, 0.01, 3)])
    assert verdict_support([1], st) == SUPPORT_NOT_REPLICATED


def test_a_parents_confirmed_mean_not_its_single_metric_is_what_a_replication_is_held_to(tmp_path):
    """The parent measured 0.70 once and 0.73 over its confirmation seeds; the child's confirmed
    0.725 beats the first and not the second."""
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.72)],
              confirmed=[(0, 0.73, 0.001, 3), (1, 0.725, 0.001, 3)])
    assert verdict_support([1], st) == SUPPORT_NOT_REPLICATED


def test_a_confirmed_candidate_over_an_unconfirmed_incumbent_is_not_a_replication(tmp_path):
    """Critic 2026-09-26, driven: the confirmation's mean is read on the FULL profile over seeds
    disjoint from the search's, the incumbent's number on the search profile at seed 0, and a single
    measurement has a spread of its own. A +0.015 gain inside the floor read `replicated` once the
    candidate was confirmed, and so did a +0.0001 gain with a zero confirmed spread. Both are one
    measurement of each on the search's ruler."""
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.715)], confirmed=[(1, 0.715, 0.02, 3)],
              floor_std=0.02)
    assert verdict_support([1], st) == SUPPORT_WITHIN_NOISE
    tiny = _run(tmp_path / "tiny", [(0, [], 0.70), (1, [0], 0.7001)],
                confirmed=[(1, 0.7001, 0.0, 3)], floor_std=0.05)
    assert verdict_support([1], tiny) == SUPPORT_WITHIN_NOISE
    alone = _run(tmp_path / "alone", [(0, [], 0.70), (1, [0], 0.73)],
                 confirmed=[(1, 0.745, 0.01, 3)])
    assert verdict_support([1], alone) == SUPPORT_SINGLE_RUN


def test_each_side_is_held_to_its_own_seed_count(tmp_path):
    """SE of the difference = sqrt(c_std²/c_n + i_std²/i_n): the incumbent's 10 seeds shrink its
    spread's share to 0.0316, which the +0.05 gain clears; read over the candidate's 2 seeds it
    would be 0.0707, which it does not (critic 2026-09-26: a seed-count swap survived)."""
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.75)],
              confirmed=[(0, 0.70, 0.1, 10), (1, 0.75, 0.001, 2)])
    assert verdict_support([1], st) == SUPPORT_REPLICATED


def test_two_seeds_a_side_is_a_replication(tmp_path):
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.75)],
              confirmed=[(0, 0.70, 0.001, 2), (1, 0.75, 0.001, 2)])
    assert verdict_support([1], st) == SUPPORT_REPLICATED


def test_a_substituted_build_lends_its_card_no_level(tmp_path):
    """A build its Developer reported as something else is never its card's evidence (`untested`
    in `_usable_evidence`, 033ed1c6), so it cannot lend the card a stronger level either."""
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.72), (2, [0], 0.80)],
              confirmed=[(0, 0.70, 0.01, 3), (2, 0.80, 0.01, 3)])
    assert verdict_support([1, 2], st) == SUPPORT_REPLICATED
    assert verdict_support([1, 2], st, untested={2}) == SUPPORT_SINGLE_RUN


def test_one_confirmation_seed_is_not_a_replication(tmp_path):
    """A mean needs a spread: one seed each is two more single measurements."""
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.73)],
              confirmed=[(0, 0.70, 0.0, 1), (1, 0.75, 0.0, 1)])
    assert verdict_support([1], st) == SUPPORT_SINGLE_RUN


@pytest.mark.parametrize("ratio,level", [(1.2, SUPPORT_WITHIN_NOISE), (1.41, SUPPORT_WITHIN_NOISE),
                                         (1.42, SUPPORT_SINGLE_RUN), (1.7, SUPPORT_SINGLE_RUN)])
def test_the_noise_bound_is_the_difference_of_two_single_measurements(tmp_path, ratio, level):
    """std·√2 ≈ 1.4142·std, pinned from both sides: 1.41·std is inside it and 1.42·std is not, so a
    factor of 1.4 moves the first case and 1.5 the second (critic 2026-09-26: with only 1.2 and 1.7,
    every factor in (√2, 1.7) survived)."""
    st = _run(tmp_path, [(0, [], 0.0), (1, [0], ratio * 0.1)], floor_std=0.1)
    assert verdict_support([1], st) == level


def test_a_gain_exactly_on_the_noise_bound_is_inside_it(tmp_path):
    st = _run(tmp_path, [(0, [], 0.0), (1, [0], 0.1 * math.sqrt(2))], floor_std=0.1)
    assert verdict_support([1], st) == SUPPORT_WITHIN_NOISE


def test_a_floor_measured_on_another_profile_is_not_this_gains_noise(tmp_path):
    """Critic 2026-09-26, driven: a floor the probe measured on `full` rated a `smoke` gain
    `within_noise`. The floor counts only when it measured the profile BOTH numbers were read on."""
    nodes = [(0, [], 0.70), (1, [0], 0.72)]
    other = _run(tmp_path / "other", nodes, floor_std=0.02, floor={"profile": "full"})
    assert verdict_support([1], other) == SUPPORT_SINGLE_RUN
    same = _run(tmp_path / "same", nodes, floor_std=0.02, floor={"profile": "full"},
                profiles={0: "full", 1: "full"})
    assert verdict_support([1], same) == SUPPORT_WITHIN_NOISE
    mixed = _run(tmp_path / "mixed", nodes, floor_std=0.02, floor={"profile": "full"},
                 profiles={1: "full"})
    assert verdict_support([1], mixed) == SUPPORT_SINGLE_RUN, "the parent was read on another"
    child = _run(tmp_path / "child", nodes, floor_std=0.02, floor={"profile": "full"},
                 profiles={0: "full"})
    assert verdict_support([1], child) == SUPPORT_SINGLE_RUN, "the child was read on another"
    unnamed = _run(tmp_path / "unnamed", nodes, floor_std=0.02, profiles={0: "full", 1: "full"})
    assert verdict_support([1], unnamed) == SUPPORT_SINGLE_RUN, "an unnamed floor is not a wildcard"


def test_the_floor_counts_on_the_ruler_it_measured_not_on_the_profile_name(tmp_path):
    """Critic 2026-09-26, driven on a repo task: both nodes left `eval_profile` null and were scored
    on `smoke`; the probe ran at the endgame's `full` fidelity and recorded `profile: None` like
    them, so the NAMES matched and a deterministic smoke eval's gains read `within_noise`. The
    recorded rulers do not match."""
    nodes, rulers = [(0, [], 0.70), (1, [0], 0.72)], {0: SMOKE, 1: SMOKE}
    full = _run(tmp_path / "full", nodes, floor_std=0.02, floor={"protocol_profile": FULL},
                rulers=rulers)
    assert verdict_support([1], full) == SUPPORT_SINGLE_RUN
    smoke = _run(tmp_path / "smoke", nodes, floor_std=0.02, floor={"protocol_profile": SMOKE},
                 rulers=rulers)
    assert verdict_support([1], smoke) == SUPPORT_WITHIN_NOISE


def test_null_and_smoke_are_one_ruler_when_the_record_says_so(tmp_path):
    """The same finding's other half: a null profile and `smoke` run the same overrides (the
    Researcher prompt says so), and the name comparison withheld the floor from every such pair."""
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.72)], floor_std=0.02,
              floor={"profile": "smoke", "protocol_profile": SMOKE},
              rulers={0: SMOKE, 1: SMOKE}, profiles={1: "smoke"})
    assert verdict_support([1], st) == SUPPORT_WITHIN_NOISE


@pytest.mark.parametrize("rulers", [{0: SMOKE}, {1: SMOKE}, {0: SMOKE, 1: FULL},
                                    {0: FULL, 1: SMOKE}])
def test_both_numbers_must_have_been_measured_on_the_floors_ruler(tmp_path, rulers):
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.72)], floor_std=0.02,
              floor={"protocol_profile": SMOKE}, rulers=rulers)
    assert verdict_support([1], st) == SUPPORT_SINGLE_RUN


def test_a_floor_that_recorded_no_ruler_is_not_used_for_numbers_that_did(tmp_path):
    """A floor written before the record, over nodes written after it: nothing says it matches."""
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.72)], floor_std=0.02,
              rulers={0: SMOKE, 1: SMOKE})
    assert verdict_support([1], st) == SUPPORT_SINGLE_RUN


def test_a_floor_over_two_rulers_is_no_one_evaluations_spread(tmp_path):
    """No ruler recorded anywhere, names equal — the one flag decides."""
    nodes = [(0, [], 0.70), (1, [0], 0.72)]
    assert verdict_support([1], _run(tmp_path / "a", nodes, floor_std=0.02)) == (
        SUPPORT_WITHIN_NOISE)
    mixed = _run(tmp_path / "b", nodes, floor_std=0.02, floor={"protocol_mixed": True})
    assert verdict_support([1], mixed) == SUPPORT_SINGLE_RUN


def test_the_fold_keeps_the_floors_ruler_only_in_the_shape_its_writer_spells(tmp_path):
    nodes = [(0, [], 0.70), (1, [0], 0.72)]
    kept = _run(tmp_path / "kept", nodes, floor_std=0.02,
                floor={"protocol_profile": SMOKE, "protocol_mixed": True}).eval_noise_floor
    assert kept["protocol_profile"] == SMOKE and kept["protocol_mixed"] is True
    for index, junk in enumerate(({"protocol_profile": 7, "protocol_mixed": "yes"},
                                  {"protocol_profile": "x" * 65, "protocol_mixed": 1},
                                  {"protocol_profile": ""})):
        floor = _run(tmp_path / f"junk{index}", nodes, floor_std=0.02, floor=junk).eval_noise_floor
        assert "protocol_profile" not in floor and "protocol_mixed" not in floor, junk


def test_a_floor_the_probe_marked_is_not_used(tmp_path):
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.72)], floor_std=0.02,
              floor={"reason": "superseded"})
    assert verdict_support([1], st) == SUPPORT_SINGLE_RUN


def test_a_two_parent_node_is_held_against_its_best_parent(tmp_path):
    """Node 2 (0.75) merges 0 (0.70) and 1 (0.74): +0.01 over the better parent is inside the floor;
    +0.05 over the worse one would read `single_run`, and the strongest level would win."""
    st = _run(tmp_path, [(0, [], 0.70), (1, [], 0.74), (2, [0, 1], 0.75)], floor_std=0.02)
    assert verdict_support([2], st) == SUPPORT_WITHIN_NOISE


def test_the_levels_rank_strongest_first(tmp_path):
    """A card whose experiments carry several levels reads the strongest: a gain the re-runs did
    not hold does not outrank one that was never re-run, and a replicated one outranks every other."""
    nodes = [(0, [], 0.70), (1, [0], 0.72), (2, [0], 0.75), (3, [0], 0.80)]
    confirmed = [(0, 0.70, 0.01, 3), (1, 0.705, 0.01, 3), (3, 0.80, 0.01, 3)]
    st = _run(tmp_path / "a", nodes, confirmed=confirmed)
    assert verdict_support([1], st) == SUPPORT_NOT_REPLICATED
    assert verdict_support([2], st) == SUPPORT_SINGLE_RUN
    assert verdict_support([1, 2], st) == SUPPORT_SINGLE_RUN
    assert verdict_support([1, 2, 3], st) == SUPPORT_REPLICATED
    noisy = _run(tmp_path / "b", nodes, confirmed=confirmed, floor_std=0.05)
    assert verdict_support([2], noisy) == SUPPORT_WITHIN_NOISE
    assert verdict_support([1, 2], noisy) == SUPPORT_WITHIN_NOISE
    assert SUPPORT_LEVELS == (SUPPORT_REPLICATED, SUPPORT_SINGLE_RUN, SUPPORT_WITHIN_NOISE,
                              SUPPORT_NOT_REPLICATED)


def test_a_root_that_beat_the_standing_record_is_classified_against_that_record(tmp_path):
    """No parent — the gain the verdict counts is the record it beat (`_record_bases`)."""
    st = _run(tmp_path, [(0, [], 0.70), (1, [], 0.72)], floor_std=0.02)
    assert verdict_support([1], st) == SUPPORT_WITHIN_NOISE
    assert verdict_support([0], st) is None, "the establisher beat nothing"


def test_no_gain_is_no_support_and_the_strongest_gain_is_the_cards(tmp_path):
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.69), (2, [0], 0.71), (3, [0], 0.75)],
              confirmed=[(0, 0.70, 0.001, 3), (3, 0.75, 0.001, 3)], floor_std=0.02)
    assert verdict_support([1], st) is None
    assert verdict_support([2], st) == SUPPORT_WITHIN_NOISE
    assert verdict_support([1, 2, 3], st) == SUPPORT_REPLICATED


def test_min_direction_reads_a_gain_the_other_way(tmp_path):
    st = _run(tmp_path, [(0, [], 0.30), (1, [0], 0.25)], direction="min", floor_std=0.01)
    assert verdict_support([1], st) == SUPPORT_SINGLE_RUN


# ------------------------------------------------------------------ the board
def _board_state(tmp_path):
    """Three experiments, each testing its own hypothesis: the REAL fold derives one card per
    hypothesis and its verdict — node 1's (+0.01 over its parent) `supported`, the other two
    `tested` — and the run measured an eval noise floor of std 0.02."""
    return _run(tmp_path, [(0, [], 0.70), (1, [0], 0.71), (2, [0], 0.69)], floor_std=0.02)


def _row(brief: str, nodes: str) -> str:
    return next(line for line in brief.split("\n") if f" NODES={nodes} " in line)


def test_a_supported_row_says_what_it_rests_on_and_the_board_says_what_that_means(tmp_path):
    st = _board_state(tmp_path)
    on = _state_brief(st, st.nodes[1], verdict_support=True)
    assert "VERDICT=supported SUPPORT=within_noise NODES=[1] " in _row(on, "[1]")
    assert "VERDICT=tested NODES=[2] " in _row(on, "[2]"), "only a supported verdict is qualified"
    assert on.count(support_legend({SUPPORT_WITHIN_NOISE})) == 1


def test_the_legend_is_said_once_and_defines_only_the_levels_the_board_shows(tmp_path):
    """Two supported rows, both inside the floor: one legend line, and it defines `within_noise`
    alone — during the search a board carries one level, and three definitions no row can carry were
    read in every proposal (critic 2026-09-26)."""
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.71), (2, [0], 0.715)], floor_std=0.02)
    on = _state_brief(st, st.nodes[1], verdict_support=True)
    assert "SUPPORT=within_noise NODES=[1] " in _row(on, "[1]")
    assert "SUPPORT=within_noise NODES=[2] " in _row(on, "[2]")
    assert on.count("SUPPORT, on a supported card, says what the verdict rests on") == 1
    assert SUPPORT_LEVEL_TEXT[SUPPORT_WITHIN_NOISE] in on
    for level in (SUPPORT_REPLICATED, SUPPORT_SINGLE_RUN, SUPPORT_NOT_REPLICATED):
        assert SUPPORT_LEVEL_TEXT[level] not in on, level


def test_a_board_with_two_levels_defines_both_in_the_ledgers_order(tmp_path):
    """Node 1 (+0.01) sits inside the floor and node 2 (+0.10) outside it: two supported rows at
    two levels, one legend defining both, strongest first (critic 2026-09-26: a legend of only the
    first level seen survived, because no board carried two)."""
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.71), (2, [0], 0.80)], floor_std=0.02)
    on = _state_brief(st, st.nodes[1], verdict_support=True)
    assert "SUPPORT=within_noise NODES=[1] " in _row(on, "[1]")
    assert "SUPPORT=single_run NODES=[2] " in _row(on, "[2]")
    assert support_legend({SUPPORT_SINGLE_RUN, SUPPORT_WITHIN_NOISE}) in on
    single, noise = (on.index(SUPPORT_LEVEL_TEXT[level])
                     for level in (SUPPORT_SINGLE_RUN, SUPPORT_WITHIN_NOISE))
    assert single < noise


def test_the_board_reads_a_cards_support_without_its_substituted_builds(tmp_path):
    """The board hands `verdict_support` the card's own `substituted_nodes`: a card whose evidence
    holds a replicated substitution and one real single-run gain says `single_run`."""
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.72), (2, [0], 0.80)],
              confirmed=[(0, 0.70, 0.01, 3), (2, 0.80, 0.01, 3)])
    card = next(c for c in st.cards.values() if c.evidence == [1])
    card.evidence, card.substituted_nodes = [1, 2], [2]
    on = _state_brief(st, st.nodes[1], verdict_support=True)
    assert "SUPPORT=single_run NODES=[1, 2] " in _row(on, "[1, 2]")


def test_the_legend_is_keyed_and_ordered_as_the_ledgers_levels():
    """`agents/` reaches `events/` only through a deferred import, so the legend spells the level
    names itself; this pins the two spellings equal, in the same order."""
    assert tuple(SUPPORT_LEVEL_TEXT) == SUPPORT_LEVELS
    assert all(text.startswith(f"{level} = ") for level, text in SUPPORT_LEVEL_TEXT.items())
    legend = support_legend(set(SUPPORT_LEVELS))
    assert [legend.index(SUPPORT_LEVEL_TEXT[level]) for level in SUPPORT_LEVELS] == sorted(
        legend.index(SUPPORT_LEVEL_TEXT[level]) for level in SUPPORT_LEVELS)
    assert "did not hold beyond 1 SE" in SUPPORT_LEVEL_TEXT[SUPPORT_NOT_REPLICATED]


def test_a_board_with_no_supported_card_is_untouched_with_the_switch_on(tmp_path):
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.69), (2, [0], 0.68)], floor_std=0.02)
    assert _state_brief(st, st.nodes[1], verdict_support=True) == _state_brief(st, st.nodes[1])


# The HISTORICAL bytes of `_board_state`, rendered with the digest's verdict cue and the switch
# OFF, measured on the pre-change tree (ed47cfa7, 2026-09-26).
_HISTORICAL_SHA256 = "89450c1f56eb34b02913a4c710655fcdea551d9cf6a01ced143ff5623d10b510"


def test_off_is_the_historical_brief_byte_for_byte(tmp_path):
    st = _board_state(tmp_path)
    text = _state_brief(st, st.nodes[1], memo_verdicts=True)
    assert "VERDICT=supported NODES=[1] " in text, "the state renders a supported card"
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == _HISTORICAL_SHA256


# ------------------------------------------------------------------ the switch and its reach
class _Client:
    def __init__(self):
        self.messages = None

    def complete_tool(self, messages, json_schema=None, **_kw):
        if self.messages is None:
            self.messages = [dict(m) for m in messages]
        return {"operator": "draft", "params": {"x": 1.0}, "rationale": "r"}


def test_the_switch_reaches_both_propose_paths_through_the_engines_stamp(tmp_path, monkeypatch):
    from looplab.agents import agent as agent_mod
    from looplab.agents.agent import ToolUsingResearcher

    assert "_verdict_support" in RESEARCHER_HINT_ATTRS       # so every wrapper forwards it
    state = _board_state(tmp_path / "s")
    for on in (False, True):
        engine = make_engine(tmp_path / f"reach-{on}", card_verdict_support=on,
                             researcher=LLMResearcher(_Client()))
        researcher = engine.researcher
        researcher.client = client = _Client()
        engine._set_complexity_hint(state, state.nodes[1], researcher=researcher)
        researcher.propose(state, state.nodes[1])
        plain = next(m["content"] for m in client.messages if m["role"] == "user")
        seen = {}

        def _fake(client, tools, messages, emit_spec, **kw):
            seen["m"] = [dict(m) for m in messages]
            return Idea(operator="draft", params={}, rationale="ok")

        monkeypatch.setattr(agent_mod, "run_phase", _fake)
        agentic = ToolUsingResearcher(client=object(), tools=None)     # a lane nobody built with
        engine._set_complexity_hint(state, state.nodes[1], researcher=agentic)
        agentic.propose(state, state.nodes[1])
        agentic_user = next(m["content"] for m in seen["m"] if m["role"] == "user")
        for turn in (plain, agentic_user):
            assert ("SUPPORT=within_noise" in turn) is on
            assert (support_legend({SUPPORT_WITHIN_NOISE}) in turn) is on


def test_the_flag_ships_on_resumes_off_and_is_off_at_every_constructor():
    assert Settings().card_verdict_support is True
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["card_verdict_support"] is False
    legacy = {k: v for k, v in Settings().masked_snapshot().items() if k != "card_verdict_support"}
    assert settings_from_snapshot(legacy).card_verdict_support is False
    assert settings_from_snapshot(Settings().masked_snapshot()).card_verdict_support is True
    assert EngineOptions().card_verdict_support is False
    assert EngineOptions.from_settings(Settings()).card_verdict_support is True


def test_only_the_two_propose_paths_pass_the_switch():
    """Triage, the repair critic, the macro chooser, deep research and the Boss read the same
    board builders and keep their historical boards. By AST, so a comment cannot pass a keyword."""
    from tests._source_scan import iter_trees

    passers: dict[str, set[str]] = {}

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, scope + [child.name])
                continue
            if isinstance(child, ast.Call):
                func = child.func
                name = (func.id if isinstance(func, ast.Name)
                        else func.attr if isinstance(func, ast.Attribute) else None)
                keyword = {"_state_brief": "verdict_support", "board_prompt_lines": "support"}.get(name)
                if keyword and any(k.arg == keyword for k in child.keywords):
                    passers.setdefault(".".join(scope[-2:]), set()).add(name)
            visit(child, scope)

    for _path, tree in iter_trees():
        visit(tree, [])
    assert passers == {"LLMResearcher.propose": {"_state_brief"},
                       "ToolUsingResearcher.propose": {"_state_brief"},
                       "_state_brief": {"board_prompt_lines"}}, passers
