"""The proposal brief, FITTED (`Settings.propose_brief_fit`; Q-3, the Researcher's context audit).

WHAT WAS RENDERED. A scripted toy run through the real `cli/__init__.py::_engine` + `Engine.run`
(recording client, shipped Settings) and repo-shaped states built from REAL events and folded by the
real `replay.fold`. The Researcher's working set failed it four ways, each measured:

1. THE BOUND CUT THE WRONG THING AND SAID NOTHING. `experiments_digest` joined every row and cut the
   STRING at its budget (1,200 chars for any run under twenty nodes) with a bare ` …`. On a repo-shaped
   run (eight dotted params per node) that held three and a half "Strongest" rows: a 12-node run lost
   10 of 13 lines and a 20-node run 10 of 13 — EVERY "Weakest / failures (avoid repeating)" row among
   them — and a 40-node run (budget 2,400) still lost every failure row.
2. A LIST OF EVERY SCORED NODE WAS CALLED "STRONGEST". With five or fewer scored nodes the far corner
   (metric 100, twenty times the baseline) sat under "Strongest:" and the avoid section was empty.
3. FACTS TWICE, OR IN TWO SPELLINGS. The header's raw `metric=0.08000000000000004` beside the digest's
   `0.08`; "Refine from node N" repeating "Best so far" on every champion improve; the concept JSON
   twice; the repo time budget twice (~1,000 duplicate characters per proposal).
4. A FAILURE WITH NO TRIAGE VERDICT HAD NO WHY. The failure cue kept a traceback's HEAD
   ("Traceback (most recent call last):" + a path) and the digest row kept nothing; a board row showed
   one of a belief's two failed attempts.

Tier 1 throughout: every assertion reads a render a real builder produced — a real Engine stamping a
real Researcher where the path goes through one. OFF is pinned against the HISTORICAL bytes by sha256
(computed on the pre-change tree), not merely against another call of the same code.
"""
from __future__ import annotations

import ast
import hashlib

import pytest

from looplab.agents.roles import RESEARCHER_HINT_ATTRS, LLMResearcher, _state_brief
from looplab.core.cards import Card
from looplab.core.config import (LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings,
                                 settings_from_snapshot)
from looplab.core.models import Idea, RunState, durable_idea_payload
from looplab.engine.options import EngineOptions
from looplab.events.digest import auto_char_cap, experiments_digest
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from tests.factories import make_engine

TRACEBACK = ("Traceback (most recent call last):\n  File \"/runs/x/nodes/node_1/solution.py\", "
             "line 41, in <module>\n    model.fit(X, y)\n  File \"/usr/lib/python3/site-packages/"
             "sklearn/base.py\", line 9, in fit\n    raise ValueError('Input X contains NaN')\n"
             "ValueError: Input X contains NaN. LinearRegression does not accept missing values.")


def _append_node(store, i, parents, params, *, metric=None, fail=None, triage="", hyp=None):
    idea = Idea(operator="improve" if parents else "draft", params=params, concept_mode="full",
                concepts=["loss/contrastive", "training/schedule"], rationale=f"experiment {i}",
                hypothesis=hyp)
    store.append("node_created", {"node_id": i, "parent_ids": parents, "operator": idea.operator,
                                  "idea": durable_idea_payload(idea), "code": "", "files": {},
                                  "generation": 0})
    if fail:
        payload = {"node_id": i, "generation": 0, "reason": fail, "error": TRACEBACK,
                   "eval_seconds": 60.0}
        if triage:
            payload["triage_rationale"] = triage
        store.append("node_failed", payload)
    elif metric is not None:
        store.append("node_evaluated", {"node_id": i, "generation": 0, "metric": metric,
                                        "eval_seconds": 60.0, "extra_metrics": {},
                                        "stdout_tail": "", "trials": [], "violations": []})


def _repo_shaped(tmp_path, n_nodes=20) -> RunState:
    """A retrieval run's shape: eight dotted params a node, a failure every other node from #4."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"goal": "maximize recall@10", "direction": "max",
                                 "task_id": "e5small", "run_id": "repo-shaped"})
    fails = set(range(4, 16, 2))
    for i in range(n_nodes):
        params = {"train.learning_rate": 1e-4 * (1 + i % 5), "train.batch_size": 1024 * (1 + i % 4),
                  "train.n_epochs": 1 + i % 3, "loss.temperature": 0.02 + 0.01 * (i % 4),
                  "train.gradient_accumulation_steps": 1 + i % 2, "model.dropout": 0.1,
                  "train.warmup_ratio": 0.05, "train.max_seq_length": 128 * (1 + i % 2)}
        _append_node(store, i, [] if i < 3 else [i - 3], params,
                     metric=0.70 + 0.004 * i + (0.01 if i % 5 == 0 else 0),
                     fail=("oom" if i % 4 == 0 else "crash") if i in fails else None,
                     triage="batch 4096 at seq 256 does not fit on one H200; halve the batch",
                     hyp=f"belief {i % 7}")
    return fold(store.read_all())


def _toy(tmp_path, metrics, *, fail_untriaged=False) -> RunState:
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"goal": "minimize (x-3)^2 + (y+1)^2", "direction": "min",
                                 "task_id": "toy", "run_id": "toy"})
    for i, metric in enumerate(metrics):
        _append_node(store, i, [], {"x": 0.1 * (i + 1), "y": -0.5}, metric=metric, hyp=f"h{i}")
    if fail_untriaged:
        _append_node(store, len(metrics), [], {"x": 9.9, "y": 0.0}, fail="crash", hyp="edge")
    return fold(store.read_all())


# ------------------------------------------------------------------ (1) the bound cuts whole rows
def test_the_historical_bound_lost_every_avoid_row_on_a_repo_shaped_run(tmp_path):
    """The defect, pinned so a reader can see what the fitted bound fixes."""
    state = _repo_shaped(tmp_path)
    off = experiments_digest(state)
    assert off.endswith(" …") and "FAILED" not in off and "Weakest" not in off


def test_the_fitted_bound_keeps_whole_rows_the_avoid_rows_and_says_what_it_cut(tmp_path):
    state = _repo_shaped(tmp_path)
    cap = auto_char_cap(state)
    full_rows = {line for line in experiments_digest(state, char_cap=10**9, fit=True).split("\n")}
    fitted = experiments_digest(state, fit=True)
    assert len(fitted) <= cap
    lines = fitted.split("\n")
    rows = [line for line in lines if line.startswith("  #")]
    # Whole rows only: each is a row of the uncut render, or that row clipped with its own marker.
    for row in rows:
        assert row in full_rows or (row.endswith("…") and any(r.startswith(row[:-1])
                                                              for r in full_rows)), row
    assert "Weakest / failures (avoid repeating):" in lines
    assert any("FAILED" in row for row in rows), "the avoid-repeating set was cut again"
    receipt = lines[-1]
    assert receipt.startswith(f"[not shown within this {cap}-char budget: ")
    assert "list_experiments" in receipt and "weakest/failed" in receipt


def test_a_render_that_fits_is_whole_and_carries_no_receipt(tmp_path):
    state = _toy(tmp_path, [10.0, 0.5, 100.0, 3.0, 2.0, 1.0, 4.0])
    fitted = experiments_digest(state, fit=True)
    assert "[not shown" not in fitted and " …" not in fitted
    assert all(f"#{i} " in fitted for i in range(7))


# ------------------------------------------------------------------ (2) "Strongest" means strongest
def test_a_list_of_every_scored_node_is_not_called_the_strongest(tmp_path):
    state = _toy(tmp_path, [10.0, 0.5, 100.0])
    off = experiments_digest(state)
    assert "Strongest:" in off and "metric=100" in off.split("Strongest:")[1]
    fitted = experiments_digest(state, fit=True)
    assert "Scored so far, best first:" in fitted and "Strongest:" not in fitted


def test_with_more_scored_nodes_than_the_list_holds_it_is_the_strongest_again(tmp_path):
    fitted = experiments_digest(_toy(tmp_path, [10.0, 0.5, 100.0, 3.0, 2.0, 1.0]), fit=True)
    assert "Strongest:" in fitted and "Scored so far" not in fitted


# ------------------------------------------------------------------ (3) each fact once, one format
def test_the_header_states_each_number_once_in_the_digests_format(tmp_path):
    state = _toy(tmp_path, [10.0, 0.1 + 0.2, 100.0])       # 0.30000000000000004, the float noise
    best = state.best()
    off = _state_brief(state, best)
    assert "metric=0.30000000000000004" in off
    assert off.count("params={'x': 0.2, 'y': -0.5}") == 2   # Best so far + Refine from, verbatim
    fitted = _state_brief(state, best, fit=True)
    assert f"Best so far: node {best.id} metric=0.3 params=x=0.2, y=-0.5" in fitted
    assert f"Refine from node {best.id} (the best so far)." in fitted
    assert "0.30000000000000004" not in fitted


def test_a_repo_task_hears_its_time_budget_once(tmp_path):
    engine = make_engine(tmp_path / "repo", propose_brief_fit=True)
    engine._repo_spec = {"editables": [{"path": str(tmp_path)}]}
    engine._eval_spec = {"timeout": 21600.0}
    state = _toy(tmp_path / "s", [0.71])
    state.nodes[0].eval_seconds = 3900.0
    researcher = LLMResearcher(object())
    engine._set_complexity_hint(state, None, researcher=researcher)
    assert "Experiment TIME BUDGET" not in researcher._complexity_hint
    assert ("Experiment wall-clock measured so far (the TIME BUDGET below is the ceiling each stage "
            "runs under): node 0: 65 min (completed).") in researcher._complexity_hint
    assert "21600s (~6.0h)" in researcher._time_budget_hint
    assert {"kind": "experiment_time_budget", "seconds": 21600.0} in researcher._steering_context
    engine._propose_brief_fit = False
    engine._set_complexity_hint(state, None, researcher=researcher)
    assert "must finish within ~21600s" in researcher._complexity_hint     # the historical twin


def test_the_concept_json_reaches_the_prompt_once(tmp_path):
    turns = {}
    for fit in (False, True):
        engine = make_engine(tmp_path / f"concepts-{fit}", propose_brief_fit=fit,
                             concept_run_base=True, researcher=LLMResearcher(_Client()))
        state = _toy(tmp_path / f"s-{fit}", [1.0])
        state.run_base_concepts = ["loss/contrastive"]
        turns[fit] = _propose(engine, state)
    assert turns[False].count("UNTRUSTED_RECORDED_CONCEPT_DATA=") == 2
    assert turns[True].count("UNTRUSTED_RECORDED_CONCEPT_DATA=") == 1
    assert "Concept authoring — delta mode is enabled" in turns[True]


# ------------------------------------------------------------------ (4) a failure keeps its why
def test_an_untriaged_failure_carries_its_own_last_error_line(tmp_path):
    state = _toy(tmp_path, [1.0], fail_untriaged=True)
    off = experiments_digest(state)
    assert "FAILED (crash)" in off and "error:" not in off
    fitted = experiments_digest(state, fit=True)
    assert "FAILED (crash)" in fitted
    assert "— error: ValueError: Input X contains NaN." in fitted


def test_the_failure_cue_reads_the_last_line_not_the_tracebacks_head(tmp_path):
    engine = make_engine(tmp_path / "reflect", failure_reflection=True)
    state = _toy(tmp_path / "s", [1.0], fail_untriaged=True)
    off, _ = engine._cue_failure_reflection(state, None, None)
    assert "Traceback (most recent call last):" in off           # the historical head cut
    engine._propose_brief_fit = True
    fitted, _ = engine._cue_failure_reflection(state, None, None)
    assert "ValueError: Input X contains NaN" in fitted
    assert "Traceback" not in fitted


def test_a_board_row_carries_every_node_of_its_belief():
    state = RunState(goal="g", direction="min")
    for cid, status, nodes in (("card-3", "failed", [3]), ("card-4", "running", [4])):
        state.cards[cid] = Card(id=cid, statement="large x keeps improving",
                                seed_statement="large x keeps improving", status=status,
                                verdict="open", evidence=nodes, belief_id="belief-x")
    off = _state_brief(state, None)
    assert "CARD_ID=card-3" in off and "NODES=[3] " in off and "card-4" not in off
    fitted = _state_brief(state, None, fit=True)
    assert "CARD_ID=card-4 BELIEF_ID=belief-x STATUS=running VERDICT=open NODES=[3, 4] " in fitted
    assert fitted.count("BELIEF_ID=belief-x") == 1


# ------------------------------------------------------------------ the switch and its reach
class _Client:
    def __init__(self):
        self.messages = None

    def complete_tool(self, messages, json_schema=None, **_kw):
        if self.messages is None:
            self.messages = [dict(m) for m in messages]
        return {"operator": "draft", "params": {"x": 1.0}, "rationale": "r"}


def _propose(engine, state, parent=None) -> str:
    researcher = engine.researcher
    researcher.client = client = _Client()
    engine._set_complexity_hint(state, parent, researcher=researcher)
    researcher.propose(state, parent)
    return next(m["content"] for m in client.messages if m["role"] == "user")


def test_the_switch_reaches_both_propose_paths_through_the_engines_stamp(tmp_path, monkeypatch):
    from looplab.agents import agent as agent_mod
    from looplab.agents.agent import ToolUsingResearcher

    assert "_brief_fit" in RESEARCHER_HINT_ATTRS              # so every wrapper forwards it
    state = _toy(tmp_path / "s", [10.0, 0.5, 100.0])
    for fit in (False, True):
        engine = make_engine(tmp_path / f"reach-{fit}", propose_brief_fit=fit,
                             researcher=LLMResearcher(_Client()))
        plain = _propose(engine, state, state.best())
        seen = {}

        def _fake(client, tools, messages, emit_spec, **kw):
            seen["m"] = [dict(m) for m in messages]
            return Idea(operator="draft", params={}, rationale="ok")

        monkeypatch.setattr(agent_mod, "run_phase", _fake)
        agentic = ToolUsingResearcher(client=object(), tools=None)     # a lane nobody built with
        engine._set_complexity_hint(state, state.best(), researcher=agentic)
        agentic.propose(state, state.best())
        agentic_user = next(m["content"] for m in seen["m"] if m["role"] == "user")
        for turn in (plain, agentic_user):
            assert ("(the best so far)." in turn) is fit
            assert ("Scored so far, best first:" in turn) is fit


# The HISTORICAL bytes, measured on the pre-change tree (origin/master 2026-09-23) over the
# repo-shaped state above: OFF must reproduce them exactly, whatever this module does ON.
_HISTORICAL_SHA256 = {
    "digest": "bc91a53873985196e36190cc7f4640241ffb803ce1208b1a43c02a12b919c120",
    "brief": "fff336b22c01678e2b2cf6fe6c9f7cbc6af8b19e6b1b70b52d0eb844ea70fe94",
}


def test_off_is_the_historical_brief_byte_for_byte(tmp_path):
    state = _repo_shaped(tmp_path)
    renders = {"digest": experiments_digest(state),
               "brief": _state_brief(state, state.nodes[1], memo_verdicts=True)}
    for key, text in renders.items():
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == _HISTORICAL_SHA256[key], key


def test_the_flag_ships_on_resumes_off_and_is_off_at_every_constructor():
    assert Settings().propose_brief_fit is True
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["propose_brief_fit"] is False
    legacy = {k: v for k, v in Settings().masked_snapshot().items() if k != "propose_brief_fit"}
    assert settings_from_snapshot(legacy).propose_brief_fit is False
    assert settings_from_snapshot(Settings().masked_snapshot()).propose_brief_fit is True
    assert EngineOptions().propose_brief_fit is False
    assert EngineOptions.from_settings(Settings()).propose_brief_fit is True


# The builders that take the switch, and the keyword each takes it by. A call that passes it is
# either one of the two propose paths or one builder forwarding it to the next.
_SWITCH_KEYWORDS = {"_state_brief": "fit", "experiments_digest": "fit", "sibling_digest": "fit",
                    "board_prompt_lines": "fit", "_node_line": "fit", "node_params_brief": "compact"}
_MAY_PASS_THE_SWITCH = {"LLMResearcher.propose", "ToolUsingResearcher.propose",
                        "_state_brief", "experiments_digest", "sibling_digest", "_node_line"}


def _switch_passers() -> dict[str, set[str]]:
    """`{enclosing qualname: {builder}}` for every call in `looplab/` that passes the switch, by
    AST (a comment cannot pass a keyword)."""
    from tests._source_scan import iter_trees

    found: dict[str, set[str]] = {}

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, scope + [child.name])
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, scope + [child.name])
            else:
                if isinstance(child, ast.Call):
                    func = child.func
                    name = (func.id if isinstance(func, ast.Name)
                            else func.attr if isinstance(func, ast.Attribute) else None)
                    keyword = _SWITCH_KEYWORDS.get(name)
                    if keyword and any(k.arg == keyword for k in child.keywords):
                        found.setdefault(".".join(scope[-2:]) if len(scope) > 1 else
                                         (scope[0] if scope else "<module>"), set()).add(name)
                visit(child, scope)

    for _path, tree in iter_trees():
        visit(tree, [])
    return found


def test_only_the_two_propose_paths_turn_the_brief_fitted():
    """Triage, the repair critic, the macro chooser, deep research, the report and the Boss read
    the same builders and keep their historical briefs: none of them passes the switch, so each
    gets the builders' OFF default whatever the run's setting. MUTATION: pass `fit=` from
    `_ask_triage` -> red; drop it from either propose path -> red."""
    passers = _switch_passers()
    assert set(passers) <= _MAY_PASS_THE_SWITCH, sorted(set(passers) - _MAY_PASS_THE_SWITCH)
    for path in ("LLMResearcher.propose", "ToolUsingResearcher.propose"):
        assert passers.get(path) == {"_state_brief"}, (path, passers.get(path))
