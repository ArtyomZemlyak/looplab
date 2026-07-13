"""The permission-mode decision table (Claude-Code-style) — the single source of truth all mutating
tool providers consult."""
from __future__ import annotations

import pytest

from looplab.tools.perm_modes import (
    DEFAULT_PROTECT,
    GRANT_TTL_SECONDS,
    RISK_CONSEQUENTIAL,
    RISK_HIGH,
    RISK_READ,
    RISK_REVERSIBLE,
    RISK_UNKNOWN,
    RememberedGrantStore,
    approval_allows,
    classify_action,
    decide,
    decide_action,
    normalize_mode,
)


def test_reads_always_inline():
    for mode in ("plan", "default", "acceptEdits", "auto"):
        assert decide(mode, "read") == "inline"
        assert decide(mode, "git_ro") == "inline"


def test_plan_denies_all_mutation():
    for kind in ("write", "shell", "git_mut", "create_run"):
        assert decide("plan", kind) == "deny"


def test_default_asks_every_mutation():
    for kind in ("write", "shell", "git_mut", "create_run"):
        assert decide("default", kind) == "ask"


def test_accept_edits_applies_writes_but_asks_shell():
    assert decide("acceptEdits", "write") == "inline"
    for kind in ("shell", "git_mut", "create_run"):
        assert decide("acceptEdits", kind) == "ask"


def test_auto_runs_everything():
    for kind in ("write", "shell", "git_mut", "create_run"):
        assert decide("auto", kind) == "inline"


def test_unknown_mode_falls_back_to_plan():
    assert normalize_mode("nonsense") == "plan"
    assert decide("nonsense", "write") == "deny"


@pytest.mark.parametrize("mode,expected", [
    ("plan", {
        RISK_READ: "inline", RISK_REVERSIBLE: "deny",
        RISK_CONSEQUENTIAL: "deny", RISK_HIGH: "deny", RISK_UNKNOWN: "deny"}),
    ("default", {
        RISK_READ: "inline", RISK_REVERSIBLE: "ask",
        RISK_CONSEQUENTIAL: "ask", RISK_HIGH: "ask", RISK_UNKNOWN: "ask"}),
    ("acceptEdits", {
        RISK_READ: "inline", RISK_REVERSIBLE: "inline",
        RISK_CONSEQUENTIAL: "ask", RISK_HIGH: "ask", RISK_UNKNOWN: "ask"}),
    ("auto", {
        RISK_READ: "inline", RISK_REVERSIBLE: "inline",
        RISK_CONSEQUENTIAL: "inline", RISK_HIGH: "ask", RISK_UNKNOWN: "ask"}),
])
def test_action_risk_by_mode_matrix(mode, expected):
    actions = {
        RISK_READ: {"tool": "read_file", "tool_kind": "read", "path": "a"},
        RISK_REVERSIBLE: {"tool": "write_file", "tool_kind": "write", "path": "a",
                          "recovery_available": True},
        RISK_CONSEQUENTIAL: {
            "tool": "remember", "tool_kind": "knowledge_write", "path": "kb"},
        RISK_HIGH: {"tool": "delete_file", "tool_kind": "write", "path": "a"},
        RISK_UNKNOWN: {"tool": "surprise", "tool_kind": "new_provider"},
    }
    for risk, action in actions.items():
        assert classify_action(action).risk == risk
        assert decide_action(mode, action) == expected[risk]


def test_unknown_and_adversarial_actions_fail_closed_without_policy_crash():
    unknown = {"tool": "surprise", "tool_kind": "new_provider", "scope": {"x": float("nan")}}
    policy = classify_action(unknown)
    assert policy.risk == RISK_UNKNOWN and policy.rememberable is False
    assert len(policy.scope_digest) == 64
    assert decide_action("plan", unknown) == "deny"
    assert decide_action("auto", unknown) == "ask"
    spoofed_read = classify_action({"tool": "delete_everything", "tool_kind": "read"})
    assert spoofed_read.risk == RISK_UNKNOWN and spoofed_read.rememberable is False
    assert decide_action("auto", {"tool": "delete_everything", "tool_kind": "read"}) == "ask"


def test_only_exact_permission_verdicts_authorize():
    assert approval_allows("allow_once") and approval_allows("allow_always")
    assert not approval_allows("allow")
    assert not approval_allows("allow_once_evil")
    assert not approval_allows(True)


def test_write_without_a_recovery_path_is_consequential_not_reversible():
    action = {"tool": "write_file", "tool_kind": "write", "path": "a",
              "recovery_available": False}
    assert classify_action(action).risk == RISK_CONSEQUENTIAL
    assert decide_action("acceptEdits", action) == "ask"


def test_remembered_grant_is_exact_mode_epoch_action_scope_and_expires():
    now = [100.0]
    grants = RememberedGrantStore(clock=lambda: now[0])
    action = classify_action({
        "tool": "extend_budget", "tool_kind": "run_control",
        "scope": {"run_id": "r", "add_nodes": 2, "paths": ["b", "a"]}})
    assert action.rememberable and grants.remember("s", "default", "epoch-a", action)
    assert grants.allows("s", "default", "epoch-a", action)

    variants = [
        ("other", "default", "epoch-a", action),
        ("s", "acceptEdits", "epoch-a", action),
        ("s", "default", "epoch-b", action),
        ("s", "default", "epoch-a", classify_action({
            "tool": "set_directive", "tool_kind": "run_control",
            "scope": {"run_id": "r", "text": "x"}})),
        ("s", "default", "epoch-a", classify_action({
            "tool": "extend_budget", "tool_kind": "run_control",
            "scope": {"run_id": "r", "add_nodes": 3, "paths": ["a", "c"]}})),
    ]
    assert all(not grants.allows(*args) for args in variants)

    now[0] += GRANT_TTL_SECONDS + 0.001
    assert not grants.allows("s", "default", "epoch-a", action)


def test_cancel_invalidation_and_high_reclassification_never_reuse_a_grant():
    grants = RememberedGrantStore()
    reversible = classify_action({"tool": "write_file", "tool_kind": "write", "path": "a",
                                  "recovery_available": True})
    assert grants.remember("s", "default", "epoch", reversible)
    grants.invalidate("s", epoch="epoch")
    assert not grants.allows("s", "default", "epoch", reversible)

    high = classify_action({"tool": "delete_file", "tool_kind": "write", "path": "a"})
    assert not grants.remember("s", "auto", "epoch", high)
    grants._grants[grants._key("s", "auto", "epoch", high)] = grants.clock() + 60
    assert not grants.allows("s", "auto", "epoch", high)


def test_protect_list_covers_integrity_files():
    joined = " ".join(DEFAULT_PROTECT)
    assert ".git/**" in DEFAULT_PROTECT
    assert "events.jsonl" in joined and "grader" in joined


def test_grader_globs_precise_boundary():
    """Architecture review + mega-review: grader/grade files are protected at a filename-COMPONENT
    boundary (name-start, after a _/- separator, or the autograder convention) — so real grader files
    stay read-only while upgrade.py/downgrade.py/upgrader.py (which merely CONTAIN 'grade') are editable
    and autograd.py (the PyTorch lib) is not falsely locked."""
    from looplab.tools.patch import _match

    def prot(path):
        return any(_match(path, glob) for glob in DEFAULT_PROTECT)
    for f in ("grader.py", "grade.py", "grading.py", "mle_grader.py", "grade_submission.py",
              "autograder.py", "autograde.py", "sub/autograder.py"):
        assert prot(f), f + " should be protected"
    for f in ("upgrade.py", "downgrade.py", "upgrader.py", "autograd.py", "gradient.py"):
        assert not prot(f), f + " should be editable"
