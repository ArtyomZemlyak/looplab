"""The permission-mode decision table (Claude-Code-style) — the single source of truth all mutating
tool providers consult."""
from __future__ import annotations

from looplab.tools.perm_modes import DEFAULT_PROTECT, decide, normalize_mode


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
    prot = lambda p: any(_match(p, g) for g in DEFAULT_PROTECT)
    for f in ("grader.py", "grade.py", "grading.py", "mle_grader.py", "grade_submission.py",
              "autograder.py", "autograde.py", "sub/autograder.py"):
        assert prot(f), f + " should be protected"
    for f in ("upgrade.py", "downgrade.py", "upgrader.py", "autograd.py", "gradient.py"):
        assert not prot(f), f + " should be editable"
