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
from looplab.agents.state_brief import SUPPORT_LEGEND
from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings, settings_from_snapshot
from looplab.core.models import Idea, durable_idea_payload
from looplab.engine.options import EngineOptions
from looplab.events.card_ledger import (SUPPORT_NOT_REPLICATED, SUPPORT_REPLICATED,
                                        SUPPORT_SINGLE_RUN, SUPPORT_WITHIN_NOISE, verdict_support)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from tests.factories import make_engine


def _run(tmp_path, nodes, *, direction="max", confirmed=(), floor_std=None):
    """`nodes` is [(id, parents, metric)]; `confirmed` is [(id, mean, std, seeds)]."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g",
                                 "direction": direction})
    for i, parents, metric in nodes:
        idea = Idea(operator="improve" if parents else "draft", params={"lr": 0.001 * (i + 1)},
                    rationale=f"exp {i}", hypothesis=f"h{i}")
        store.append("node_created", {"node_id": i, "parent_ids": parents, "operator": idea.operator,
                                      "idea": durable_idea_payload(idea), "code": "", "files": {},
                                      "generation": 0})
        store.append("node_evaluated", {"node_id": i, "generation": 0, "metric": metric,
                                        "eval_seconds": 1.0, "extra_metrics": {}, "stdout_tail": "",
                                        "trials": [], "violations": []})
    for i, mean, std, seeds in confirmed:
        store.append("node_confirmed", {"node_id": i, "generation": 0, "mean": mean, "std": std,
                                        "seeds": seeds})
    if floor_std is not None:
        store.append("eval_noise_floor", {"node_id": 0, "generation": 0, "seeds": [1, 2, 3],
                                          "metrics": [0.7, 0.7, 0.7], "n": 3, "mean": 0.7,
                                          "std": floor_std, "sem": floor_std / math.sqrt(3),
                                          "spread": 2 * floor_std})
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
    """`core/fitness.py::one_se_better` — the rule the confirm gate uses — over the node's own
    confirmation (3 seeds): mean 0.72 against the parent's 0.70 clears 1 SE at std 0.01 and does
    not at std 0.2."""
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.73)], confirmed=[(1, 0.72, std, 3)])
    assert verdict_support([1], st) == level


def test_a_replicated_parent_is_compared_as_a_mean(tmp_path):
    """Both sides replicated: SE of the difference pools both spreads, so a gain (0.045) that
    clears the candidate's own SE (0.006) can still sit inside the pair's (0.058)."""
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.73)],
              confirmed=[(0, 0.70, 0.1, 3), (1, 0.745, 0.01, 3)])
    assert verdict_support([1], st) == SUPPORT_NOT_REPLICATED
    alone = _run(tmp_path / "alone", [(0, [], 0.70), (1, [0], 0.73)],
                 confirmed=[(1, 0.745, 0.01, 3)])
    assert verdict_support([1], alone) == SUPPORT_REPLICATED, "the candidate's own SE clears it"


def test_a_root_that_beat_the_standing_record_is_classified_against_that_record(tmp_path):
    """No parent — the gain the verdict counts is the record it beat (`_record_bases`)."""
    st = _run(tmp_path, [(0, [], 0.70), (1, [], 0.72)], floor_std=0.02)
    assert verdict_support([1], st) == SUPPORT_WITHIN_NOISE
    assert verdict_support([0], st) is None, "the establisher beat nothing"


def test_no_gain_is_no_support_and_the_strongest_gain_is_the_cards(tmp_path):
    st = _run(tmp_path, [(0, [], 0.70), (1, [0], 0.69), (2, [0], 0.71), (3, [0], 0.75)],
              confirmed=[(3, 0.75, 0.001, 3)], floor_std=0.02)
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
    assert on.count(SUPPORT_LEGEND) == 1


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
            assert (SUPPORT_LEGEND in turn) is on


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
