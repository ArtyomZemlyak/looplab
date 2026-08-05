"""The leakage and reward-hack gates actually EMIT — end to end, through a real run.

`looplab/trust/` exists so a run cannot report clean because nothing looked. `engine/evaluate.py`
concatenates two of those detectors into the signals that become `reward_hack_suspected`, and
`test_trust_finding_namespaces.py` guards the wiring with source pins:

    assert "code_leakage_findings(scan_src)" in source
    assert "critic_findings(node.idea, scan_src" in source

A mutation audit on 2026-08-05 walked through both. Making the two calls contribute nothing —

    code_leakage_findings(scan_src)  # sigs += code_leakage_findings(scan_src)

— keeps every pinned substring, keeps the detectors' own unit tests green (they are called, and
they still return the right findings), and leaves all nine trust/leakage/critic/signal test files
passing. What it removes is the only thing that matters: `sigs` stays empty, `if sigs:` never fires,
no `reward_hack_suspected` event is ever written, and every downstream gate — `is_hard_signal`,
`_apply_trust_gate`, the Trust panel, the folded `state.reward_hacks` — sees a clean run.

So this file asserts the LEDGER, not the wiring: a node whose code carries a leakage tell and a
critic tell produces a `reward_hack_suspected` event carrying both namespaces. Deliberately a
separate file from the namespace guards, which are about who MINTS the namespace; this is about
whether anything is emitted at all.

The two concatenations are now the named rule `EvaluateMixin._trust_gate_signals`, so the same
property is ALSO checkable without driving a run: two of the last four tests call the rule directly
and die on the identical mutation in milliseconds, and the last two pin the chain that carries its
result to the event — `_trust_gate_signals` -> `_trust_scan_signals` -> `sigs` in `_evaluate` (doc
25 ES-03 split the middle link out of `_evaluate`, so there are now two places the `+=`/`=` can be
lost instead of one). Both halves stay — the rule proves the findings are produced, the run proves
they reach the ledger.
"""
from __future__ import annotations

import ast
from types import SimpleNamespace

import anyio
import pytest

from _source_scan import function_tree
from factories import make_engine
from looplab.agents.roles import ToyObjectiveDeveloper
from looplab.core.models import Idea, developer_artifact_footprint
from looplab.engine.evaluate import EvaluateMixin
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold

# A runnable solution that is ALSO a positive control for both detectors:
#   * `.fit(X_test, ...)` -> `code_leakage_scan` flags `fit_on_test`, namespaced `data_leakage:`;
#   * a LITERAL metric with nothing computing it -> `critique` flags `hardcoded_metric`, namespaced
#     `critic:`, which is the one critic issue that is a HARD gate.
# The leaky call sits under `if False:` so the script still runs and still reports a metric — the
# scanners are static, and a node that CRASHES never reaches the trust block at all.
_LEAKY_SOLUTION = '''import json
if False:                       # never executes; a static tell, so the eval still succeeds
    model.fit(X_test, y_test)
print(json.dumps({"metric": 0.5}))
'''


class _LeakyDeveloper(ToyObjectiveDeveloper):
    """The toy Developer, but every node it writes carries both tells."""

    def implement(self, idea):
        self.last_footprint = developer_artifact_footprint(idea.footprint, _LEAKY_SOLUTION)
        return _LEAKY_SOLUTION


def _signals(run_dir) -> list[dict]:
    return [signal
            for event in EventStore(run_dir / "events.jsonl").read_all()
            if event.type == "reward_hack_suspected"
            for signal in (event.data.get("signals") or [])]


@pytest.fixture(scope="module")
def gated_run(tmp_path_factory):
    """One real run with both gates on. Module-scoped: it is a full subprocess-sandbox run, and the
    three assertions below are three questions about the SAME event log."""
    run_dir = tmp_path_factory.mktemp("trust-gates") / "run"
    engine = make_engine(run_dir, developer=_LeakyDeveloper(), n_seeds=1, max_nodes=1,
                         code_leakage_detect=True, critic_check=True)
    state = anyio.run(engine.run)
    assert state.finished
    return run_dir, state


def test_a_leaking_node_reaches_the_ledger_with_the_leakage_namespace(gated_run):
    """`code_leakage_findings` -> `sigs` -> the durable event. The gate that fires on train->test
    information flow is worthless if its findings are computed and dropped."""
    run_dir, _state = gated_run
    leakage = [signal for signal in _signals(run_dir)
               if str(signal.get("signal", "")).startswith("data_leakage:")]
    assert leakage, (
        "no data_leakage signal was recorded for a node that fits on test data — the leakage gate "
        "ran and its findings went nowhere")
    assert any(signal["signal"] == "data_leakage:fit_on_test" for signal in leakage)


def test_the_critic_findings_reach_the_same_ledger(gated_run):
    """`critic_findings` is the second concatenation, and `critic:hardcoded_metric` is the narrow
    HARD signal that can exclude a node from selection and breeding."""
    run_dir, _state = gated_run
    critic = [signal for signal in _signals(run_dir)
              if str(signal.get("signal", "")).startswith("critic:")]
    assert critic, "no critic signal was recorded for a node with a hard-coded metric"
    assert any(signal["signal"] == "critic:hardcoded_metric" for signal in critic)


def test_the_fold_sees_the_gate_so_a_run_cannot_report_clean(gated_run):
    """The consequence, one layer out: `sigs` is what `if sigs:` gates the append on, so a silenced
    detector does not produce a weaker event — it produces NO event, and `state.reward_hacks` is
    empty exactly as it is for an honest run."""
    run_dir, _state = gated_run
    state = fold(EventStore(run_dir / "events.jsonl").read_all())
    assert state.reward_hacks, "the folded run reports clean while both detectors had something to say"
    namespaces = {str(signal.get("signal", "")).split(":")[0]
                  for flag in state.reward_hacks
                  for signal in (flag.get("signals") or [])}
    assert {"data_leakage", "critic"} <= namespaces


def test_both_gates_stay_silent_on_an_honest_node(tmp_path):
    """The other half of a positive control: the detectors above fired because the code was leaky,
    not because the wiring flags everything. The stock toy solution computes its metric and touches
    no test data, so the same run with the same gates on emits nothing."""
    run_dir = tmp_path / "clean"
    engine = make_engine(run_dir, n_seeds=1, max_nodes=1,
                         code_leakage_detect=True, critic_check=True)
    state = anyio.run(engine.run)
    assert state.finished
    assert _signals(run_dir) == []
    assert state.reward_hacks == []


# ------------------------------------------------- the same property, without driving a whole run

# `_trust_gate_signals` reads exactly one attribute off the node, and saying so here is the point:
# a rule narrow enough to call with a bare Idea is a rule a reviewer can check.
_NODE = SimpleNamespace(idea=Idea(operator="draft"))


def _gate_engine(tmp_path, **gates):
    return make_engine(tmp_path / "rule", n_seeds=1, max_nodes=1, **gates)


def _namespaces(engine, src=None):
    return {row["signal"].split(":")[0]
            for row in engine._trust_gate_signals(_NODE, _LEAKY_SOLUTION if src is None else src)}


def test_the_named_rule_produces_both_namespaces(tmp_path):
    """The mutation the module docstring describes, killed in milliseconds instead of a run.

    Dropping either `sigs +=` inside `_trust_gate_signals` — the exact edit that survives all eight
    trust/leakage/critic/signal files — drops the namespace from THIS list. That is the whole reason
    the two concatenations became a named rule: the property is now reachable from a caller."""
    engine = _gate_engine(tmp_path, code_leakage_detect=True, critic_check=True)
    signals = {row["signal"] for row in engine._trust_gate_signals(_NODE, _LEAKY_SOLUTION)}
    assert "data_leakage:fit_on_test" in signals, "the leakage gate contributed nothing"
    assert "critic:hardcoded_metric" in signals, "the critic gate contributed nothing"


def test_the_named_rule_keeps_both_gates_optional(tmp_path):
    """Each half is opt-in (`code_leakage_detect` / `critic_check`), and the extraction has to keep
    a gate that is OFF silent — otherwise the rule would start flagging runs that never enabled it."""
    both = _gate_engine(tmp_path, code_leakage_detect=True, critic_check=True)
    leakage_only = _gate_engine(tmp_path, code_leakage_detect=True, critic_check=False)
    critic_only = _gate_engine(tmp_path, code_leakage_detect=False, critic_check=True)
    neither = _gate_engine(tmp_path, code_leakage_detect=False, critic_check=False)
    assert _namespaces(leakage_only) == {"data_leakage"}
    assert _namespaces(critic_only) == {"critic"}
    assert _namespaces(neither) == set()
    # …and an empty surface is not something to have an opinion about (both gates test `scan_src`).
    assert _namespaces(both, src="") == set()


def test_the_scan_still_concatenates_the_named_rule():
    """Naming the rule moved one failure mode rather than removing it: `sigs +=
    self._trust_gate_signals(...)` can lose its `sigs +=` exactly as the two calls it replaced could,
    and the three unit tests above would not notice. The end-to-end tests at the top of this file are
    what CATCH that; this states it as well, on the AST — a comment naming the method is not an
    `ast.AugAssign`, and neither is a bare call whose result is dropped.

    The concatenation moved OUT of `_evaluate` on 2026-08-05 (doc 25 ES-03): the reward-hack half and
    this half are now the one named rule `_trust_scan_signals`, which `_evaluate` calls. So the same
    assertion is made about its new home, and the test below adds the link the split created — that
    `_evaluate` still BINDS what the rule returns."""
    concatenations = [
        node for node in ast.walk(function_tree(EvaluateMixin._trust_scan_signals))
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name)
        and node.target.id == "sigs" and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "_trust_gate_signals"]
    assert len(concatenations) == 1, (
        "`_trust_scan_signals` must concatenate `_trust_gate_signals` into `sigs` exactly once — "
        "the trust gates are computed and dropped otherwise")


def test_the_evaluator_binds_what_the_scan_returns():
    """The seam the extraction created. `_trust_scan_signals` returns the findings and appends
    nothing, so `_evaluate` calling it without binding the result — `self._trust_scan_signals(...)`
    on a line of its own, the exact mutation the rule above is about, one level out — computes every
    detector and drops the lot. Pinned as a real `ast.Assign` to `sigs`, so a comment carrying the
    call expression does not satisfy it."""
    bindings = [
        node for node in ast.walk(function_tree(EvaluateMixin._evaluate))
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "_trust_scan_signals"
        and [target.id for target in node.targets if isinstance(target, ast.Name)] == ["sigs"]]
    assert len(bindings) == 1, (
        "`_evaluate` must bind `self._trust_scan_signals(...)` to `sigs` exactly once — the scan "
        "appends nothing itself, so an unbound call runs every detector and discards the findings")
    # …and the surface it hands the scan must be the SAME string `code_digest` commits to, which is
    # what `_trust_scan_surface` exists to guarantee: the digest below is of `scan_src`, so a caller
    # that re-derived the surface for one of the two would hash bytes the detectors never saw.
    surfaces = [
        node for node in ast.walk(function_tree(EvaluateMixin._evaluate))
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "_trust_scan_surface"
        and [target.id for target in node.targets if isinstance(target, ast.Name)] == ["scan_src"]]
    assert len(surfaces) == 1, "`scan_src` must come from `_trust_scan_surface`, derived once"
    assert "scan_src" in [ast.unparse(arg) for arg in bindings[0].value.args], (
        "the scan must be handed the same `scan_src` the digest is taken over")
