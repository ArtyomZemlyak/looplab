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
    """Grader/grade files are protected BROADLY — any name containing 'grade'/'grader'/'grading',
    including no-separator forms like mygrader.py / pregrader.py (F11) — while the migration scripts that
    merely CONTAIN 'grade' (upgrade.py/downgrade.py/upgrader.py) stay editable via the explicit
    DEFAULT_PROTECT_EXCEPTIONS override, and autograd.py (PyTorch) / gradient.py are not falsely locked.
    Checks the FULL protection decision (broad glob AND the exception carve-out), as WriteTools uses it."""
    from looplab.tools.patch import SurfacePolicy
    from looplab.tools.perm_modes import DEFAULT_PROTECT_EXCEPTIONS
    prot = lambda p: SurfacePolicy(None, DEFAULT_PROTECT, check_escapes=False,
                                   allow_exceptions=DEFAULT_PROTECT_EXCEPTIONS).check(p) == "protected"
    for f in ("grader.py", "grade.py", "grading.py", "mle_grader.py", "grade_submission.py",
              "autograder.py", "autograde.py", "sub/autograder.py",
              "mygrader.py", "pregrader.py", "finalgrader.py", "src/xgrader.py"):   # F11: no-separator
        assert prot(f), f + " should be protected"
    for f in ("upgrade.py", "downgrade.py", "upgrader.py", "upgrade_003.py",
              "autograd.py", "gradient.py"):
        assert not prot(f), f + " should be editable"
    # An operator/manifest that passes its OWN protect list gets NO exception override (intent wins).
    assert SurfacePolicy(None, ["**/upgrade.py"], check_escapes=False).check("upgrade.py") == "protected"
