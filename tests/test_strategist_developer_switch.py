"""The Developer-backend switch has producers at both ends.

Everything BELOW the switch already existed: `validate_strategy` whitelisted `developer`,
`_prepare_strategy_developer` has four refusal arms, and `developer_application` is a durable
receipt shape. Nothing could reach any of it. `_StrategyOut` declares `extra="forbid"`, so a model
naming a backend lost its ENTIRE decision — the policy, the widths, the rationale — and
`_normalize_set_strategy`'s closed key set refused an operator's `developer` with a 400. So the
capability was neither exposed nor retired, which is the state the open item recorded.

The two halves refuse DIFFERENTLY and that asymmetry is the design (`core/appconfig.py`'s rule):
the operator is present at the HTTP boundary and can fix a typo, so an unknown name is a 400 naming
the valid set; the model is not, and a hallucinated name must not take a decision — or a run — down,
so `validate_strategy` drops it. What the drop now also does is SAY SO.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from looplab.agents.strategist import (
    StrategyContext, _assemble_strategy, _StrategyOut, validate_strategy)
from looplab.core.config import developer_switch_names


def _ctx(**over) -> StrategyContext:
    fields = {"available_policies": ["greedy", "mcts"],
              "available_developers": ["default", "codex", "llm"]}
    fields.update(over)
    return StrategyContext(**fields)


def test_the_model_can_now_PROPOSE_a_developer():
    """THE DEFECT, half one. MUTATION: drop the field -> `extra="forbid"` rejects the whole tool
    call, so a Strategist that mentions a backend loses its policy and its rationale too."""
    out = _StrategyOut(policy="mcts", developer="codex", rationale="external agent for this repo")
    assert _assemble_strategy(out)["developer"] == "codex"


def test_a_decision_with_NO_developer_is_byte_identical():
    """The overwhelmingly common case: nothing may change for a Strategist that never mentions one."""
    assert "developer" not in _assemble_strategy(_StrategyOut(policy="mcts", rationale="r"))


def test_assemble_copies_it_VERBATIM_and_does_not_filter():
    """`validate_strategy` is the whitelist and the only thing entitled to refuse a name. Filtering
    here would hide a hallucinated backend from the receipt that exists to record exactly that."""
    assert _assemble_strategy(_StrategyOut(developer="made-up", rationale="r"))["developer"] == "made-up"


def test_a_registered_backend_survives_validation():
    strat = validate_strategy({"policy": "mcts", "developer": "codex"}, _ctx())
    assert strat["developer"] == "codex" and "developer_refused" not in strat


def test_an_UNREGISTERED_backend_is_dropped_and_SAID():
    """THE INVISIBLE DROP. The refusal happens before `_prepare_strategy_developer` runs, so its
    `refused` receipt cannot fire for a name it never receives — the durable decision then carried
    the rationale ("switch developer to agentless") with no `developer` and no receipt at all, i.e.
    a history that reads as a switch that happened.

    MUTATION: drop the `elif` -> the record is silent again, now that something can produce the
    field for it to be silent about.
    """
    strat = validate_strategy(
        {"policy": "mcts", "developer": "agentless", "rationale": "switch developer to agentless"},
        _ctx())
    assert "developer" not in strat, "a refused name must never look like a switch"
    assert strat["developer_refused"] == "agentless"
    assert strat["policy"] == "mcts", "the rest of the decision still applies"


def test_the_refusal_reaches_the_SAME_receipt_the_factory_refusal_uses(tmp_path):
    """One reader answers "what happened to the developer this decision asked for", for every arm.

    MUTATION: leave `developer_refused` in the strategy and write no receipt -> the durable record
    carries a key no consumer knows and still says nothing about the refusal.
    """
    from factories import make_engine

    engine = make_engine(tmp_path)
    effective, prepared, receipt = engine._prepare_strategy_developer(
        {"policy": "mcts", "developer_refused": "agentless"})

    assert receipt == {
        "status": "refused", "requested_backend": "agentless", "applied_backend": "default",
        "reason_code": "unknown_backend",
        "reason": "'agentless' is not an available Developer backend"}
    assert "developer_refused" not in effective, "the marker must not reach the durable strategy"
    assert effective["policy"] == "mcts"


def test_a_decision_naming_NO_developer_writes_no_receipt(tmp_path):
    from factories import make_engine

    engine = make_engine(tmp_path)
    assert engine._prepare_strategy_developer({"policy": "mcts"})[2] is None


def test_the_operator_can_POST_one(tmp_path):
    """THE DEFECT, half two, over the wire."""
    from looplab.serve.server import make_app

    run = tmp_path / "demo"
    run.mkdir()
    (run / "events.jsonl").write_text('{"seq":0,"type":"run_started","data":{}}\n', encoding="utf-8")
    client = TestClient(make_app(tmp_path))
    name = sorted(developer_switch_names())[0]

    response = client.post("/api/runs/demo/control",
                           json={"type": "set_strategy", "data": {"strategy": {"developer": name}}})
    assert response.status_code < 400, response.text


def test_an_unknown_backend_is_REFUSED_over_the_wire_and_the_message_names_the_set(tmp_path):
    """Not dropped: the operator is present and can fix a typo, so a 400 naming the valid set is
    strictly better than silently keeping the current backend."""
    from looplab.serve.server import make_app

    run = tmp_path / "demo"
    run.mkdir()
    (run / "events.jsonl").write_text('{"seq":0,"type":"run_started","data":{}}\n', encoding="utf-8")
    client = TestClient(make_app(tmp_path))

    response = client.post("/api/runs/demo/control",
                           json={"type": "set_strategy", "data": {"strategy": {"developer": "agentless"}}})
    assert response.status_code == 400
    body = response.text
    assert "strategy.developer" in body
    for name in developer_switch_names():
        assert name in body, name


def test_both_ends_read_the_SAME_vocabulary():
    """MUTATION: hard-code either list -> the operator and the model are told different things are
    switchable, which is the disagreement `developer_switch_names` was created to end."""
    import ast
    from pathlib import Path

    from looplab.serve import control_validation

    source = Path(control_validation.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "developer_switch_names" in called, "the HTTP validator derives its own list"
