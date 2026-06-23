"""Validate how an external coding agent performed (ADR-7 companion).

The external Developer (`cli_agent.CliAgentDeveloper`) edits a solution file
*out-of-process* via an opaque CLI agent (OpenCode/aider/…). We can't trust it to have
done its job: it may no-op (leave the seed), emit unparseable Python, crash, or time
out. This module turns the agent's output **plus** the captured process signal into a
structured `AgentReport` of pass/fail checks — cheaply and statically (no code
execution) — *before* the orchestrator spends a sandbox evaluation on it.

This is complementary to, not a duplicate of, node evaluation: the dynamic verdict
("does the code run and produce a metric?") is the existing `node_evaluated` /
`node_failed`. The static verdict here catches failures the eval can't attribute to the
*agent* (no edit, syntax error, agent process died/timed out) and lets the
`ValidatingDeveloper` retry the agent before wasting a sandbox run. Together they answer
"did the external agent actually do the work?".
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AgentRun:
    """Process-level signal captured from a CLI agent subprocess (`cli_agent`)."""
    launched: bool = True          # False if the launcher was missing (OSError)
    exit_code: Optional[int] = None
    timed_out: bool = False
    stdout_tail: str = ""
    stderr_tail: str = ""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    severity: str = "error"        # "error" fails the report; "warn" is advisory only


@dataclass
class AgentReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Pass iff no error-severity check failed (warnings don't fail the report)."""
        return all(c.ok for c in self.checks if c.severity == "error")

    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def feedback(self) -> str:
        """One-line, human/LLM-readable reason(s) the agent's output was rejected —
        fed back into the next attempt's prompt by `ValidatingDeveloper`. Returns "" for
        a passing report (only ever called on the failure path)."""
        return "; ".join(f"{c.name} ({c.detail})" if c.detail else c.name
                         for c in self.failures())

    def summary(self) -> dict:
        """Compact, JSON-serializable form for the `agent_validated` event."""
        return {
            "ok": self.ok,
            "checks": [
                {"name": c.name, "ok": c.ok, "severity": c.severity, "detail": c.detail}
                for c in self.checks
            ],
        }


def _parses(code: str) -> tuple[bool, str]:
    try:
        ast.parse(code or "")
        return True, ""
    except SyntaxError as e:
        return False, f"SyntaxError: {e.msg} (line {e.lineno})"


def validate_agent_code(
    code: str,
    *,
    seed: Optional[str] = None,
    run: Optional[AgentRun] = None,
    patch: Optional[dict] = None,
    files: Optional[dict] = None,
    metric_key: str = "metric",
) -> AgentReport:
    """Build an `AgentReport` for one agent invocation.

    `code`   — what the agent left in the solution file (single-file mode).
    `seed`   — the file content handed to the agent; if given, we check the agent
               actually changed it (a no-op means the edit silently failed).
    `run`    — process-level signal (exit/timeout/launched); checks added when present.
    `patch`  — surface-gate verdict {ok,paths,rejected}; a check is added when present.
    `files`  — when not None, REPO mode: validate the accepted changed-file SET (RepoTask)
               instead of a single `solution.py` — the eval runs a command over these files.
    `metric_key` — the contract token a single-file solution must print (advisory).
    """
    checks: list[Check] = []

    # --- edit-surface gate (patch-gated agents) ------------------------------------
    if patch is not None:
        checks.append(Check("edit_in_surface", bool(patch.get("ok")),
                            f"out-of-surface edits rejected: {patch.get('rejected')}"
                            if patch.get("rejected") else "no in-surface changes"))

    # --- process-level signal (only when we have it: CLI-agent developers) ---------
    if run is not None:
        checks.append(Check("agent_launched", run.launched,
                            "" if run.launched else "launcher not found / failed to start"))
        checks.append(Check("agent_not_timed_out", not run.timed_out,
                            "agent hit its timeout" if run.timed_out else ""))
        if run.exit_code is not None:
            checks.append(Check("agent_exit_ok", run.exit_code == 0,
                                f"exit={run.exit_code}", severity="warn"))

    if files is not None:
        # --- repo mode: validate the changed-file set -----------------------------
        checks.append(Check("produced_files", bool(files),
                            "agent made no accepted in-surface edits"))
        bad = [n for n, c in files.items() if n.endswith(".py") and not _parses(c)[0]]
        checks.append(Check("parses", not bad,
                            f"syntax errors in {bad}" if bad else ""))
    else:
        # --- single-file mode -----------------------------------------------------
        stripped = (code or "").strip()
        checks.append(Check("produced_code", bool(stripped), "empty output"))
        if seed is not None:
            checks.append(Check("modified_seed", stripped != seed.strip(),
                                "output identical to the input file (no change made)"))
        parses, detail = _parses(code)
        checks.append(Check("parses", parses, detail))
        checks.append(Check("emits_metric", metric_key in (code or ""),
                            f"no reference to {metric_key!r}", severity="warn"))

    return AgentReport(checks)
