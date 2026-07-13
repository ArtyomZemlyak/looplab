"""Claude-Code-style permission modes for the assistant's mutating tools.

The MODE the user selects in the UI is enforced by the tool PROVIDERS via one small decision table
here (single source of truth, imported by write/shell/git). `decide(mode, tool_kind)` returns:
  - "inline": run the action now and return its real result;
  - "ask":    do NOT run — hand the action to the approver (a confirm-card in the UI); run it only if
              the user allows, else return a "declined" note to the model;
  - "deny":   refuse outright (read-only `plan` mode blocks all mutation).

Modes (mirroring Claude Code):
  - plan        — read-only: inspect + propose, no mutation.
  - default     — ask before every mutation.
  - acceptEdits — file edits apply inline; shell / git-mutation / launching a run still ask.
  - auto        — everything runs inline (bypass); the user opted into this explicitly.

`tool_kind` ∈ {read, write, shell, git_ro, git_mut, create_run}. Reads and read-only git are always
inline regardless of mode.
"""
from __future__ import annotations

MODES = ("plan", "default", "acceptEdits", "auto")
DEFAULT_MODE = "plan"

READONLY_KINDS = frozenset({"read", "git_ro"})
MUTATING_KINDS = frozenset({"write", "shell", "git_mut", "create_run"})


def normalize_mode(mode) -> str:
    return mode if mode in MODES else DEFAULT_MODE


def decide(mode, tool_kind) -> str:
    """Return "inline" | "ask" | "deny" for a tool of `tool_kind` under permission `mode`."""
    if tool_kind in READONLY_KINDS:
        return "inline"
    mode = normalize_mode(mode)
    if mode == "auto":
        return "inline"
    if mode == "plan":
        return "deny"
    if mode == "acceptEdits" and tool_kind == "write":
        return "inline"
    return "ask"     # default (all mutations) + acceptEdits (shell/git_mut/create_run)


# Hard-protected paths: NEVER writable/removable, in ANY mode, because clobbering them would corrupt a
# run's source-of-truth event log, break git internals, or overwrite a held-out grader/answer (the
# scoring integrity guarantee). Deliberately does NOT protect LoopLab's own source — editing/repairing
# LoopLab is an explicit goal — only run-data + integrity files. Matched case-insensitively against the
# root-relative POSIX path (see patch._match semantics: a leading `**/` also matches root files).
DEFAULT_PROTECT = [
    # BOTH forms: the writable-target check resolves a path relative to its FIRST containing root
    # (usually $HOME, which the repo lives under), so the bare ".git/**" never matched the repo's
    # .git seen as "data/…/.git/…" — leaving .git internals (config, hooks/pre-commit) writable.
    ".git/**", "**/.git/**",
    "**/events.jsonl", "**/spans.jsonl", "**/readmodel.sqlite", "**/engine.lock",
    "**/task.snapshot.json", "**/config.snapshot.json",
    "**/answers/**", "**/answers.csv", "**/held_out/**", "**/private/**",
    # grader / grade / grading files — protected BROADLY (any name containing "grade"/"grader"/"grading")
    # so no-separator forms like `mygrader.py` / `pregrader.py` / `finalgrader.py` are caught too (F11),
    # not just the separated `mle_grader.py`. Migration scripts that merely CONTAIN "grade"
    # (upgrade.py / downgrade.py / upgrader.py) — which no glob can distinguish from a real grader — are
    # carved back out by DEFAULT_PROTECT_EXCEPTIONS below. (`autograd.py`, the PyTorch lib, has no
    # "grade" substring — g-r-a-d, no trailing 'e' — so it stays editable.)
    "**/*grade*.py", "**/*grader*.py", "**/*grading*.py",
]

# Editable EXCEPTIONS that OVERRIDE the broad grader protection above (F11): migration scripts that
# contain "grade"/"grader" but are NOT graders. Threaded into SurfacePolicy(allow_exceptions=...) only
# on the DEFAULT protect path (WriteTools); a repo task's OWN manifest protect list is never overridden.
DEFAULT_PROTECT_EXCEPTIONS = [
    "**/upgrade.py", "**/downgrade.py", "**/upgrader.py", "**/downgrader.py",
    "**/upgrade_*.py", "**/downgrade_*.py",       # alembic-style migration scripts (e.g. upgrade_003.py)
]


def default_approver(action: dict) -> str:
    """Safe default when no interactive approver is wired: DENY. The server injects a real approver
    that blocks on a UI confirm-card; tests inject an auto-allow/deny stub."""
    return "deny"
