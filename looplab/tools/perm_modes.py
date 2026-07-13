"""Central, fail-closed action policy for assistant tools.

Concrete providers submit an action identity and canonical scope to ``decide_action``. READ actions
are inline; REVERSIBLE and CONSEQUENTIAL actions follow the selected mode; HIGH and unregistered
UNKNOWN actions require explicit approval even in Auto and can never receive a remembered grant.
The kind-only ``decide`` remains a compatibility helper, not an authorization seam.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from typing import Callable, Optional

MODES = ("plan", "default", "acceptEdits", "auto")
DEFAULT_MODE = "plan"

READONLY_KINDS = frozenset({"read", "git_ro"})
MUTATING_KINDS = frozenset({
    "write", "knowledge_write", "shell", "git_mut", "create_run", "run_control", "mcp"})

RISK_READ = "READ"
RISK_REVERSIBLE = "REVERSIBLE"
RISK_CONSEQUENTIAL = "CONSEQUENTIAL"
RISK_HIGH = "HIGH"
RISK_UNKNOWN = "UNKNOWN"
RISKS = (RISK_READ, RISK_REVERSIBLE, RISK_CONSEQUENTIAL, RISK_HIGH, RISK_UNKNOWN)

# A remembered grant is intentionally short AND turn-scoped. Keep this a code constant, not an env
# knob: a deployment must not accidentally stretch a transient approval into a long-lived bypass.
GRANT_TTL_SECONDS = 600.0
APPROVAL_ALLOW_ONCE = "allow_once"
APPROVAL_ALLOW_ALWAYS = "allow_always"


@dataclass(frozen=True)
class ActionPolicy:
    risk: str
    action_id: str
    scope: dict
    scope_digest: str
    consequence: str
    rememberable: bool


_ACTION_RISK = {
    # Read is also explicit: an unknown provider must not self-declare `tool_kind=read` and bypass
    # the deny-by-default registry. Most read providers never need this gate; these are the concrete
    # identities that do reach it (plus the small policy-contract fixture).
    ("read", "read_file"): RISK_READ,
    ("git_ro", "git"): RISK_READ,
    ("write", "write_file"): RISK_REVERSIBLE,
    ("write", "edit_file"): RISK_REVERSIBLE,
    ("write", "apply_patch"): RISK_REVERSIBLE,
    ("write", "delete_file"): RISK_HIGH,
    ("write", "revert_file"): RISK_HIGH,
    ("knowledge_write", "remember"): RISK_CONSEQUENTIAL,
    # Arbitrary commands (including tests, which execute repo code) are HIGH until a future parser or
    # sandbox proof can classify a concrete argv more narrowly. Auto must still ask.
    ("shell", "run_command"): RISK_HIGH,
    ("shell", "run_tests"): RISK_HIGH,
    ("shell", "read_output"): RISK_READ,
    ("shell", "list_background"): RISK_READ,
    ("shell", "kill_background"): RISK_HIGH,
    ("git_mut", "git_add"): RISK_REVERSIBLE,
    ("git_mut", "git_branch"): RISK_REVERSIBLE,
    ("git_mut", "git_commit"): RISK_CONSEQUENTIAL,
    ("git_mut", "git_checkout"): RISK_HIGH,
    ("run_control", "finalize_run"): RISK_CONSEQUENTIAL,
    ("run_control", "stop_run"): RISK_CONSEQUENTIAL,
    ("run_control", "resume_run"): RISK_CONSEQUENTIAL,
    ("run_control", "reset_node"): RISK_CONSEQUENTIAL,
    ("run_control", "extend_budget"): RISK_CONSEQUENTIAL,
    ("run_control", "set_directive"): RISK_CONSEQUENTIAL,
    ("run_control", "delete_node"): RISK_HIGH,
    ("run_control", "delete_run"): RISK_HIGH,
    ("run_control", "set_trust_gate"): RISK_HIGH,
}

_CONSEQUENCE = {
    RISK_READ: "Reads scoped local state without changing it.",
    RISK_REVERSIBLE: "Changes scoped local state with a practical local recovery path.",
    RISK_CONSEQUENTIAL: "Changes durable state or runs a command with observable side effects.",
    RISK_HIGH: "May delete, overwrite, terminate, or weaken safeguards; explicit approval is required.",
    RISK_UNKNOWN: "Unclassified action; capabilities and side effects are unknown.",
}
_ACTION_CONSEQUENCE = {
    ("write", "write_file"): "Creates or overwrites the scoped file with the reviewed content.",
    ("write", "edit_file"): "Replaces one exact match in the scoped file.",
    ("write", "apply_patch"): "Applies the reviewed patch to the listed workspace files.",
    ("write", "delete_file"): "Deletes the scoped file from disk.",
    ("write", "revert_file"): "Overwrites the scoped file from its previous snapshot.",
    ("knowledge_write", "remember"): "Writes a durable note into the shared cross-run knowledge base.",
    ("shell", "run_command"): "Executes the reviewed argument vector in the scoped working directory.",
    ("shell", "run_tests"): "Executes repository test code in a local Python process.",
    ("shell", "kill_background"): "Terminates the scoped background task.",
    ("git_mut", "git_add"): "Changes the repository index for the reviewed paths.",
    ("git_mut", "git_branch"): "Creates the reviewed local Git branch.",
    ("git_mut", "git_commit"): "Creates a durable Git commit from the current staged index.",
    ("git_mut", "git_checkout"): "Switches the working tree and index to the reviewed Git ref.",
    ("run_control", "finalize_run"): "Stops active work and finalizes the scoped run.",
    ("run_control", "stop_run"): "Pauses the scoped run.",
    ("run_control", "resume_run"): "Resumes execution of the scoped run.",
    ("run_control", "reset_node"): "Re-runs the scoped node from the reviewed stage.",
    ("run_control", "extend_budget"): "Increases the scoped run budget by the reviewed amounts.",
    ("run_control", "set_directive"): "Writes or replaces the reviewed directive on the scoped run.",
    ("run_control", "delete_node"): "Permanently removes the reviewed node subtree from the run.",
    ("run_control", "delete_run"): "Permanently deletes the scoped run directory.",
    ("run_control", "set_trust_gate"): "Changes the scoped run's trust-enforcement policy.",
}
_NON_REMEMBERABLE_ACTIONS = frozenset({("git_mut", "git_commit")})
_SCOPE_KEYS = ("path", "paths", "cwd", "run_id", "node_id", "task_id", "preview", "verb")


def _canonical_scope(action: dict) -> dict:
    """Return a bounded JSON-safe scope used for display and exact grant matching."""
    scope = {}
    explicit = action.get("scope")
    if isinstance(explicit, dict):
        for key, value in explicit.items():
            if not isinstance(key, str):
                continue
            if isinstance(value, str):
                scope[key[:80]] = value[:4000]
            elif value is None or isinstance(value, (int, bool)):
                scope[key[:80]] = value
            elif isinstance(value, float) and math.isfinite(value):
                scope[key[:80]] = value
            elif isinstance(value, (list, tuple)):
                scope[key[:80]] = sorted(
                    {str(item)[:1000] for item in value
                     if isinstance(item, (str, int, float, bool))})[:200]
    for key in _SCOPE_KEYS:
        value = action.get(key)
        if isinstance(value, str):
            scope[key] = value[:4000]
        elif isinstance(value, int) and not isinstance(value, bool):
            scope[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            scope[key] = value
        elif isinstance(value, (list, tuple)):
            scope[key] = sorted({str(item)[:1000] for item in value if item is not None})[:200]
    return scope


def classify_action(action: object) -> ActionPolicy:
    """Classify one concrete action; malformed/unregistered identities become UNKNOWN."""
    raw = action if isinstance(action, dict) else {}
    kind = raw.get("tool_kind") if isinstance(raw.get("tool_kind"), str) else ""
    tool = raw.get("tool") if isinstance(raw.get("tool"), str) else ""
    kind = kind.strip()[:80]
    tool = tool.strip()[:160]
    risk = _ACTION_RISK.get((kind, tool), RISK_UNKNOWN)
    # File edits are only REVERSIBLE when the concrete provider has a recovery-receipt path. A
    # provider without one may still be allowed by Auto, but Accept edits must not silently treat it
    # like an undoable edit.
    if risk == RISK_REVERSIBLE and kind == "write" and raw.get("recovery_available") is not True:
        risk = RISK_CONSEQUENTIAL
    action_id = f"{kind}:{tool}" if kind and tool else "unknown"
    try:
        scope = _canonical_scope(raw)
        encoded = json.dumps(scope, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                             allow_nan=False).encode("utf-8")
    except Exception:  # noqa: BLE001 - adversarial provider data becomes UNKNOWN, never a policy crash
        risk = RISK_UNKNOWN
        scope = {"invalid": True}
        encoded = b'{"invalid":true}'
    digest = hashlib.sha256(encoded).hexdigest()
    return ActionPolicy(
        risk=risk,
        action_id=action_id,
        scope=scope,
        scope_digest=digest,
        consequence=_ACTION_CONSEQUENCE.get((kind, tool), _CONSEQUENCE[risk]),
        rememberable=(risk in {RISK_REVERSIBLE, RISK_CONSEQUENTIAL}
                      and (kind, tool) not in _NON_REMEMBERABLE_ACTIONS),
    )


_RISK_MODE_DECISION = {
    RISK_READ: {mode: "inline" for mode in MODES},
    RISK_REVERSIBLE: {
        "plan": "deny", "default": "ask", "acceptEdits": "inline", "auto": "inline"},
    RISK_CONSEQUENTIAL: {
        "plan": "deny", "default": "ask", "acceptEdits": "ask", "auto": "inline"},
    RISK_HIGH: {"plan": "deny", "default": "ask", "acceptEdits": "ask", "auto": "ask"},
    RISK_UNKNOWN: {"plan": "deny", "default": "ask", "acceptEdits": "ask", "auto": "ask"},
}


def normalize_mode(mode) -> str:
    return mode if mode in MODES else DEFAULT_MODE


def decide_action(mode, action: object) -> str:
    """Return ``inline`` | ``ask`` | ``deny`` for a fully described action."""
    policy = classify_action(action)
    return _RISK_MODE_DECISION[policy.risk][normalize_mode(mode)]


def approval_allows(verdict: object) -> bool:
    """Only the two wire-protocol approval values authorize an effect; prefix lookalikes fail closed."""
    return verdict in {APPROVAL_ALLOW_ONCE, APPROVAL_ALLOW_ALWAYS}


def decide(mode, tool_kind) -> str:
    """Compatibility kind-only matrix; concrete providers must prefer :func:`decide_action`."""
    if tool_kind in READONLY_KINDS:
        return "inline"
    mode = normalize_mode(mode)
    if mode == "plan":
        return "deny"
    if mode == "acceptEdits" and tool_kind == "write":
        return "inline"
    if mode == "auto" and tool_kind in MUTATING_KINDS:
        return "inline"
    return "ask"


class RememberedGrantStore:
    """Short-lived exact-action grants, bound to one session/mode/turn-cancel epoch."""

    def __init__(self, *, ttl_seconds: float = GRANT_TTL_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.ttl_seconds = min(GRANT_TTL_SECONDS, max(0.0, float(ttl_seconds)))
        self.clock = clock
        self._grants: dict[tuple[str, str, str, str, str], float] = {}

    @staticmethod
    def _key(session: str, mode: str, epoch: str, policy: ActionPolicy) -> tuple:
        return (str(session), normalize_mode(mode), str(epoch),
                policy.action_id, policy.scope_digest)

    def _purge(self) -> None:
        now = self.clock()
        for key, deadline in list(self._grants.items()):
            if deadline <= now:
                self._grants.pop(key, None)

    def remember(self, session: str, mode: str, epoch: str, policy: ActionPolicy) -> bool:
        self._purge()
        if not policy.rememberable or not session or not epoch or self.ttl_seconds <= 0:
            return False
        self._grants[self._key(session, mode, epoch, policy)] = self.clock() + self.ttl_seconds
        return True

    def allows(self, session: str, mode: str, epoch: str, policy: ActionPolicy) -> bool:
        self._purge()
        if not policy.rememberable:
            return False
        return self._key(session, mode, epoch, policy) in self._grants

    def invalidate(self, session: str, *, epoch: Optional[str] = None) -> None:
        for key in list(self._grants):
            if key[0] == str(session) and (epoch is None or key[2] == str(epoch)):
                self._grants.pop(key, None)


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
