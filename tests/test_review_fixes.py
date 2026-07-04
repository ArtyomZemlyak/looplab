"""Regression tests for the code-review findings fixed in this pass. Each test would FAIL against
the pre-fix code — they pin the corrected behavior so the bugs can't silently return."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from looplab.runtime.command_eval import host_score, read_metric
from looplab.core.context_budget import truncate_history
from looplab.trust.critic import critique
from looplab.core.models import Idea
from looplab.core.parse import _coerce_value
from looplab.search.policy import ASHAPolicy, make_policy
from looplab.trust.redact import redact_secrets
from looplab.trust.reward_hack import detect_reward_hacks


# --- command_eval.host_score: label equality + no candidate array-shopping ------------------------

def test_host_score_accuracy_matches_numeric_encodings():
    # Class labels encoded as JSON strings / floats must still match the integer answer key.
    assert host_score("accuracy", ["1", "0", "1"], [1, 0, 1]) == 1.0
    assert host_score("accuracy", [1.0, 0.0, 1.0], [1, 0, 1]) == 1.0
    # A genuine non-label (a probability) must NOT count as correct.
    assert host_score("accuracy", [0.999, 0.0, 1.0], [1, 0, 1]) == pytest.approx(2 / 3)


def test_host_score_predictions_cannot_key_shop():
    # The candidate ships a payload with several arrays; only a bare list or the canonical
    # "predictions" key is scored — a candidate can't smuggle a favorable array under "values".
    preds = {"values": [1, 1, 1], "predictions": [1, 0, 1]}
    assert host_score("accuracy", preds, [1, 0, 1]) == 1.0       # uses "predictions", not "values"
    # No recognized predictions key -> no metric (can't fall through to a candidate-chosen array).
    assert host_score("accuracy", {"values": [1, 0, 1]}, [1, 0, 1]) is None


def test_host_score_labels_path_must_be_outside_workspace(tmp_path):
    # read_metric(kind=host_score) must refuse a labels file inside the candidate workspace (it would
    # be mounted/writable by an untrusted candidate). Place predictions + labels both under workdir.
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "predictions.json").write_text(json.dumps([1, 0, 1]), encoding="utf-8")
    inside = wd / "labels.json"
    inside.write_text(json.dumps([1, 0, 1]), encoding="utf-8")
    spec = {"kind": "host_score", "scorer": "accuracy",
            "predictions": "predictions.json", "labels": str(inside)}
    with pytest.raises(ValueError, match="inside the candidate workspace"):
        read_metric("", str(wd), spec)
    # An out-of-workspace labels file is accepted and scored.
    outside = tmp_path / "labels.json"
    outside.write_text(json.dumps([1, 0, 1]), encoding="utf-8")
    spec["labels"] = str(outside)
    assert read_metric("", str(wd), spec) == 1.0


# --- reward_hack: answer-key tells are case-sensitive and must still fire -------------------------

def test_reward_hack_detects_uppercase_answer_key_access():
    for code in ("from grader import _Y", "print(grader._Y)", "leak = _Y[0]"):
        sigs = detect_reward_hacks(code, metric=1.0, direction="max")
        assert any(s["signal"] == "grader_access" for s in sigs), code


# --- context_budget: max_chars is a target, not just a trigger -----------------------------------

def test_truncate_history_stops_once_under_budget():
    big = "x" * 5000
    small = "y" * 401
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": big},
            {"role": "assistant", "content": small},
            {"role": "user", "content": small},
            {"role": "assistant", "content": "recent-a"},
            {"role": "user", "content": "recent-b"}]
    out = truncate_history(msgs, max_chars=2000, keep_last=2, per_msg_cap=400)
    # Trimming the one giant message already drops us under budget, so the 401-char messages survive.
    assert "[truncated" in out[1]["content"]
    assert out[2]["content"] == small and out[3]["content"] == small


# --- parse: boolean coercion doesn't silently flip an intended-on flag to False ------------------

def test_coerce_bool_recognizes_on_and_rejects_garbage():
    assert _coerce_value("on", bool) is True
    assert _coerce_value("off", bool) is False
    # An unrecognized string is returned as-is (so model validation rejects it) — not silently False.
    assert _coerce_value("maybe", bool) == "maybe"


# --- critic: a computed metric must not be flagged as hard-coded ----------------------------------

def test_critic_no_false_positive_on_computed_metric():
    idea = Idea(operator="draft", params={"k": 3.0}, rationale="x")
    computed = ("k = 3\nscore = sum(range(k)) / k\n"
                "import json; print(json.dumps({'metric': score}))")
    assert not any(i["issue"] == "hardcoded_metric" for i in critique(idea, computed))
    # A genuinely hard-coded literal with no computation is still flagged.
    hard = "import json\nprint(json.dumps({'metric': 0.99}))\nk = 3\n"
    assert any(i["issue"] == "hardcoded_metric" for i in critique(idea, hard))


# --- redact: compound credential key names are masked --------------------------------------------

def test_redact_masks_compound_secret_keys():
    out = redact_secrets("env={'AWS_SECRET_ACCESS_KEY': 'abcdefabcdefabcd'}", entropy=False)
    assert "abcdefabcdefabcd" not in out and "***" in out
    out2 = redact_secrets("db_password=supersecretvalue", entropy=False)
    assert "supersecretvalue" not in out2


# --- ASHA rung-0 width knob actually takes effect -------------------------------------------------

def test_asha_rung_nodes_overrides_rung0_width():
    p = make_policy("asha", n_seeds=4, max_nodes=20, eta=3, rung_nodes=8)
    assert isinstance(p, ASHAPolicy) and p.rung0 == 8
    # 0 falls back to n_seeds (default, preserving prior behavior).
    assert make_policy("asha", n_seeds=4, max_nodes=20, eta=3, rung_nodes=0).rung0 == 4
    # policy_params colliding with explicit make_policy kwargs must not crash via the strategist path.
    assert make_policy("asha", n_seeds=4, max_nodes=20, rung_nodes=6, eta=2).rung0 == 6


# --- a mid-run strategy switch to BOHB wires the surrogate (not bare ASHA) -------------------------

def test_strategy_switch_to_bohb_wires_surrogate(tmp_path):
    from looplab.engine.orchestrator import Engine
    from looplab.search.policy import GreedyTree
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.surrogate import SurrogateResearcher
    from pydantic import BaseModel

    class _T(BaseModel):
        kind: str = "t"; id: str = "t"; goal: str = "g"; direction: str = "min"

    class _R:
        bounds = {"k": (1.0, 9.0)}
        def propose(self, state, parent):
            return Idea(operator="draft", params={"k": 3.0})

    class _D:
        def implement(self, idea):
            return "print('{\"metric\": 0.0}')"

    eng = Engine(tmp_path / "b", task=_T(), researcher=_R(), developer=_D(),
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1))
    assert not isinstance(eng.researcher, SurrogateResearcher)
    eng._apply_strategy({"policy": "bohb", "policy_params": {"n_seeds": 4}})  # collision must not crash
    assert isinstance(eng.researcher, SurrogateResearcher)   # surrogate now wired
    assert eng._policy_name == "bohb"
