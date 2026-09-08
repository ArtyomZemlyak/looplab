"""A zero at 900 seconds is not the solver's failure, and the stopwatch said it was.

`looplab_eval` classifies every refusal by name and `compare_arms` keeps a partition of that
vocabulary into the solver's fault and the arena's. None of it reached `pulse`, the tool an operator
watches live: the reason travels in the node's `stdout_tail`, and `pulse` inferred "refusal" from
`eval_seconds < 5`. That is right for the refusals which cost no time and exactly wrong for the one
that costs the most -- `evaluator_timeout` returns its zero AFTER the full timeout.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import pulse  # noqa: E402


def _log(tmp, nodes):
    path = Path(tmp) / "events.jsonl"
    rows = []
    for node_id, metric, secs, tail in nodes:
        rows.append({"type": "node_evaluated",
                     "data": {"node_id": node_id, "metric": metric, "eval_seconds": secs,
                              "violations": None, "stdout_tail": tail}})
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(path)


REFUSED = json.dumps({"speedup": None, "eval_seconds": 903.0,
                      "no_speedup": {"reason": "evaluator_timeout", "detail": "our --timeout fired"}})
SCORED_ZERO = json.dumps({"speedup": 0.0, "eval_seconds": 44.0})


def test_a_refusal_that_took_the_full_timeout_is_still_a_refusal():
    with tempfile.TemporaryDirectory() as tmp:
        got = pulse.pulse(_log(tmp, [("n1", None, 903.0, REFUSED)]))
        assert got["zeros"] == 1
        bad = got["bad"][0]
        assert bad["reason"] == "evaluator_timeout", bad
        assert bad["refusal"] is True, bad          # the stopwatch alone would have said False


def test_a_zero_the_bridge_did_not_explain_still_uses_the_stopwatch():
    """A node whose stdout the record did not keep is the case the heuristic was written for, and
    it must survive: 12 corpus zeros are real evaluations at 41-47 s carrying violations."""
    with tempfile.TemporaryDirectory() as tmp:
        got = pulse.pulse(_log(tmp, [("n2", 0.0, 44.0, SCORED_ZERO),
                                     ("n3", 0.0, 0.4, SCORED_ZERO)]))
        slow, fast = got["bad"]
        assert slow["reason"] is None and slow["refusal"] is False, slow
        assert fast["reason"] is None and fast["refusal"] is True, fast


def test_the_reason_is_read_from_the_bridge_and_not_from_any_word_in_the_log():
    """`no_speedup` appears in prose in these logs; only the bridge's own nested object counts."""
    with tempfile.TemporaryDirectory() as tmp:
        prose = "the model wrote about no_speedup and reason and quit"
        got = pulse.pulse(_log(tmp, [("n4", 0.0, 44.0, prose)]))
        assert got["bad"][0]["reason"] is None, got["bad"][0]
        assert got["bad"][0]["refusal"] is False, got["bad"][0]


def test_the_printer_names_the_reason():
    src = (BENCH / "pulse.py").read_text(encoding="utf-8")
    assert 'RULER REFUSAL ({z["reason"]})' in src, "the reason is read but not shown"
