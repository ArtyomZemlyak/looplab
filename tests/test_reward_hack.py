"""Reward-hack detection: answer-key access tells fire regardless of identifier casing.

Regression from the code-review findings pass — would FAIL against the pre-fix code."""
from __future__ import annotations

from looplab.trust.reward_hack import detect_reward_hacks


# --- reward_hack: answer-key tells are case-sensitive and must still fire -------------------------

def test_reward_hack_detects_uppercase_answer_key_access():
    for code in ("from grader import _Y", "print(grader._Y)", "leak = _Y[0]"):
        sigs = detect_reward_hacks(code, metric=1.0, direction="max")
        assert any(s["signal"] == "grader_access" for s in sigs), code


# --- sanctioned grader IMPORT (in-workdir mlebench): waive the import tells, never key access ------

def test_reward_hack_sanctions_grader_import_when_task_ships_grader():
    # In-workdir mlebench MANDATES `from grader import score` and ships grader.py as an asset;
    # the asset name reaches the detector via the engine's protected/asset set, sanctioning the
    # IMPORT tells only — pre-fix every honest node on that task was flagged as suspicious.
    prot = {"grader.py", "train.json", "test.json"}
    ok = 'from grader import score\nprint(json.dumps({"metric": score(preds)}))'
    assert not any(s["signal"] == "grader_access"
                   for s in detect_reward_hacks(ok, metric=0.9, direction="max",
                                                protected_names=prot))
    # key ACCESS is NEVER sanctioned, even with grader.py shipped
    for code in ("from grader import _Y", "print(grader._Y)", "leak = _Y[0]"):
        sigs = detect_reward_hacks(code, metric=0.9, direction="max", protected_names=prot)
        assert any(s["signal"] == "grader_access" for s in sigs), code
    # an explicit override beats the asset inference (a caller can force the strict mode) …
    strict = detect_reward_hacks(ok, metric=0.9, direction="max", protected_names=prot,
                                 grader_import_ok=False)
    assert any(s["signal"] == "grader_access" for s in strict)
    # … and without the grader asset (host_graded / repo tasks) the import tells still fire
    assert any(s["signal"] == "grader_access"
               for s in detect_reward_hacks(ok, metric=0.9, direction="max"))
