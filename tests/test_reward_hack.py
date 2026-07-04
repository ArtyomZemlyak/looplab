"""Reward-hack detection: answer-key access tells fire regardless of identifier casing.

Regression from the code-review findings pass — would FAIL against the pre-fix code."""
from __future__ import annotations

from looplab.trust.reward_hack import detect_reward_hacks


# --- reward_hack: answer-key tells are case-sensitive and must still fire -------------------------

def test_reward_hack_detects_uppercase_answer_key_access():
    for code in ("from grader import _Y", "print(grader._Y)", "leak = _Y[0]"):
        sigs = detect_reward_hacks(code, metric=1.0, direction="max")
        assert any(s["signal"] == "grader_access" for s in sigs), code
