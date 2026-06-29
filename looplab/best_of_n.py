"""C2 · Best-of-N candidate selection (ADR-7). Generate N independent implementations and keep the
best by an EXECUTION-FREE reward (static validity + a metric-print signal) — the single most reliable
SWE-bench lever for weak local models (SWE-RM best-of-k: +10 pts), without spending an eval per
candidate. Wraps any Developer; forwards repair/audit hooks so it composes with the loop unchanged.

Applied only to the in-house LLM developer path (not expensive external coding agents), consistent
with the ADR-7 cost rule. N=1 is a transparent pass-through (== today).
"""
from __future__ import annotations

from .models import Idea
from .validate import validate_agent_code


def _score(code: str) -> float:
    """Execution-free quality score for a candidate (higher = better): static validity (compiles +
    passes the agent-output checks) plus a signal that it emits the required JSON metric line."""
    if not code or not code.strip():
        return -1.0
    s = 0.0
    try:
        compile(code, "<candidate>", "exec")
        s += 1.0
    except SyntaxError:
        return 0.0          # un-runnable: worst usable score
    if validate_agent_code(code).ok:
        s += 2.0
    if "metric" in code:
        s += 0.5
    return s


class BestOfNDeveloper:
    """Generate `n` candidates from `inner.implement` and return the highest execution-free score.
    Deterministic given a deterministic inner (toy); with an LLM at temperature>0 the candidates
    vary, so best-of-N actually explores. `repair` delegates to inner (single attempt)."""

    def __init__(self, inner, n: int = 3):
        self.inner = inner
        self.n = max(1, n)
        self.last_files: dict = {}
        self.last_deleted: list = []
        self.last_n_scores: list[float] = []

    # forward the hooks make_roles / the engine poke at, to the wrapped developer
    @property
    def brief(self) -> str:
        return getattr(self.inner, "brief", "")

    @property
    def client(self):
        return getattr(self.inner, "client", None)

    @client.setter
    def client(self, value) -> None:        # H3 per-role client rewiring reaches the inner developer
        if hasattr(self.inner, "client"):
            self.inner.client = value

    @property
    def prompts(self):
        return getattr(self.inner, "prompts", None)

    @prompts.setter
    def prompts(self, value) -> None:
        if hasattr(self.inner, "prompts"):
            self.inner.prompts = value

    @property
    def last_report(self):
        return getattr(self.inner, "last_report", None)

    def audit_extra(self) -> dict:
        fn = getattr(self.inner, "audit_extra", None)
        extra = fn() if callable(fn) else {}
        extra["best_of_n"] = self.n
        return extra

    def implement(self, idea: Idea) -> str:
        if self.n == 1:
            code = self.inner.implement(idea)
            self.last_files = getattr(self.inner, "last_files", {}) or {}
            self.last_deleted = getattr(self.inner, "last_deleted", []) or []
            self.last_n_scores = [_score(code)]
            return code
        best_code, best_score = "", None
        self.last_n_scores = []          # per-node telemetry: reset so it holds only THIS node's N
        for _ in range(self.n):
            code = self.inner.implement(idea)
            sc = _score(code)
            self.last_n_scores.append(sc)
            if best_score is None or sc > best_score:
                best_code, best_score = code, sc
                self.last_files = getattr(self.inner, "last_files", {}) or {}
                self.last_deleted = getattr(self.inner, "last_deleted", []) or []
        return best_code

    def repair(self, idea: Idea, code: str, error: str) -> str:
        repair = getattr(self.inner, "repair", None)
        if callable(repair):
            out = repair(idea, code, error)
            self.last_files = getattr(self.inner, "last_files", {}) or {}
            self.last_deleted = getattr(self.inner, "last_deleted", []) or []   # else stale from prior implement()
            return out
        return self.implement(idea)
