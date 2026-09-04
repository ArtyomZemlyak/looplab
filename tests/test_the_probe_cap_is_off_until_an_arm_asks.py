"""A count cap on `run_probe`, off by default, because §189 found the only lever worth an arm.

MEASURED over the 69 `edge_expansion` runs with a champion: of eleven process variables, only
`run_probe` separates the top thirteen from the bottom thirteen — 20 calls against 29, p = 0.037 —
while evaluated nodes (3 vs 3), `eval_train` calls (12 vs 12), file reads, generations and every
phase share are flat. Split at the corpus median of 24 probes, the champion is 221.81 against
177.84: **+43.97, two-sided p = 0.0077**, and **+50.03 (p = 0.0097)** restricted to the fifty runs
that evaluated exactly three nodes.

It is a correlation — a run that probes twenty-nine times may be probing BECAUSE it is lost — and
the cap is the only way to tell that apart from probing making it lost. §187 prices the arm at 48
probes for power 0.83, so the cap must exist before the arm and must change nothing until then.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from looplab.tools.dev_probe import DevProbeTools  # noqa: E402


def test_the_default_is_uncapped():
    """Every probe in the corpus ran uncapped; the default has to keep doing that."""
    tools = DevProbeTools()
    assert tools.max_calls == 0
    for _ in range(6):
        got = tools.execute_result("run_probe", {"code": "print(1)"})
        assert "refused" not in (got.content or ""), got.content


def test_the_cap_refuses_only_after_it_is_reached():
    tools = DevProbeTools(max_calls=2)
    first = [tools.execute_result("run_probe", {"code": "print(1)"}) for _ in range(2)]
    assert all(not r.is_error for r in first), [r.content for r in first]
    blocked = tools.execute_result("run_probe", {"code": "print(1)"})
    assert blocked.is_error and "run_probe refused" in blocked.content
    assert blocked.structured.get("refused") == "probe_cap"
    assert blocked.structured.get("cap") == 2


def test_the_refusal_names_the_cheaper_instrument():
    """A cap that only says no teaches nothing; the card's own answer is `eval_train`."""
    tools = DevProbeTools(max_calls=1)
    tools.execute_result("run_probe", {"code": "print(1)"})
    text = tools.execute_result("run_probe", {"code": "print(1)"}).content
    assert 'run_dev_command("eval_train")' in text, text
    assert "graded number" in text, text


def test_a_refused_probe_does_not_consume_more_of_the_cap():
    """The counter must not run away past the cap -- the message quotes it."""
    tools = DevProbeTools(max_calls=1)
    tools.execute_result("run_probe", {"code": "print(1)"})
    a = tools.execute_result("run_probe", {"code": "print(1)"}).content
    b = tools.execute_result("run_probe", {"code": "print(1)"}).content
    assert a == b, (a, b)


def test_an_unknown_tool_is_still_unknown_under_a_cap():
    tools = DevProbeTools(max_calls=1)
    got = tools.execute_result("nope", {})
    assert got.is_error and "unknown tool" in got.content
