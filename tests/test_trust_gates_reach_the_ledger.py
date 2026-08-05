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
"""
from __future__ import annotations

import anyio
import pytest

from factories import make_engine
from looplab.agents.roles import ToyObjectiveDeveloper
from looplab.core.models import developer_artifact_footprint
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
