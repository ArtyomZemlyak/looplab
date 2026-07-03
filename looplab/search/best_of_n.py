"""C2 · Best-of-N candidate selection (ADR-7). Generate N independent implementations and keep the
best by an EXECUTION-FREE reward (static validity + a metric-print signal) — the single most reliable
SWE-bench lever for weak local models (SWE-RM best-of-k: +10 pts), without spending an eval per
candidate. Wraps any Developer; forwards repair/audit hooks so it composes with the loop unchanged.

Applied only to the in-house LLM developer path (not expensive external coding agents), consistent
with the ADR-7 cost rule. N=1 is a transparent pass-through (== today).
"""
from __future__ import annotations

from looplab.core.models import Idea
from looplab.core.validate import validate_agent_code


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


def _listwise_pick(client, idea, candidates: list[str]) -> int:
    """D10 (OPPO arXiv:2506.12928): comparative LLM selection over candidates presented TOGETHER —
    +~3 pts over independent pointwise scoring on GAIA, and beats majority voting. Used only to
    break a TIE among the top static-scorers (the execution-free score stays the primary filter;
    the LLM is a weak comparative prior, never the sole oracle — the eval still decides). Returns
    the index of the chosen candidate, or 0 on any failure."""
    try:
        from pydantic import BaseModel

        from looplab.core.parse import parse_structured

        class _Pick(BaseModel):
            choice: int = 0
            reason: str = ""

        blocks = "\n\n".join(f"--- CANDIDATE {i} ---\n{c[:2000]}" for i, c in enumerate(candidates))
        msgs = [
            {"role": "system", "content":
             "You are selecting the single best ML solution implementation from several candidates "
             "for the SAME task. Compare them side by side; prefer correct, complete, robust code "
             "that faithfully realizes the idea and avoids obvious bugs/leakage. Call `emit` with "
             "`choice` = the 0-based index of the best candidate."},
            {"role": "user", "content":
             f"Idea: {getattr(idea, 'rationale', '') or ''}\n\n{blocks}\n\n"
             f"Pick the best candidate (0..{len(candidates) - 1})."},
        ]
        parser = getattr(getattr(client, "parser", None), "__str__", lambda: "tool_call")
        out = parse_structured(client, msgs, _Pick, "tool_call")
        if isinstance(out.choice, int) and 0 <= out.choice < len(candidates):
            return out.choice
    except Exception:  # noqa: BLE001 — selection is advisory; fall back to the first top-scorer
        pass
    return 0


class BestOfNDeveloper:
    """Generate `n` candidates from `inner.implement` and return the best. The EXECUTION-FREE static
    score is the primary filter; when `listwise` is on and the top scorers TIE, an LLM comparative
    selection (D10) breaks the tie — the LLM as a weak comparative prior, never the sole oracle.
    Deterministic given a deterministic inner (toy); with an LLM at temperature>0 the candidates
    vary, so best-of-N actually explores. `repair` delegates to inner (single attempt)."""

    def __init__(self, inner, n: int = 3, listwise: bool = True):
        self.inner = inner
        self.n = max(1, n)
        self.listwise = listwise
        self.last_files: dict = {}
        self.last_deleted: list = []
        self.last_n_scores: list[float] = []
        self._last_candidates: list[tuple[str, dict, list]] = []   # (code, files, deleted) per N

    # T8/A0b: capability follows the inner developer (merge_mode="auto" resolution)
    @property
    def is_code_generating(self) -> bool:
        return bool(getattr(self.inner, "is_code_generating", False))

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
        self.last_n_scores = []          # per-node telemetry: reset so it holds only THIS node's N
        cands: list[tuple[str, dict, list, float]] = []
        for _ in range(self.n):
            code = self.inner.implement(idea)
            sc = _score(code)
            self.last_n_scores.append(sc)
            cands.append((code, getattr(self.inner, "last_files", {}) or {},
                          getattr(self.inner, "last_deleted", []) or [], sc))
        best_score = max(c[3] for c in cands)
        top = [c for c in cands if c[3] >= best_score - 1e-9]
        # D10: break a tie among the top static-scorers with a list-wise LLM comparison (advisory).
        # Only when it would actually change the outcome (>1 tied) and a client is available.
        chosen = top[0]
        if self.listwise and len(top) > 1 and self.client is not None:
            idx = _listwise_pick(self.client, idea, [c[0] for c in top])
            chosen = top[idx]
        self.last_files, self.last_deleted = chosen[1], chosen[2]
        return chosen[0]

    def repair(self, idea: Idea, code: str, error: str) -> str:
        repair = getattr(self.inner, "repair", None)
        if callable(repair):
            out = repair(idea, code, error)
            self.last_files = getattr(self.inner, "last_files", {}) or {}
            self.last_deleted = getattr(self.inner, "last_deleted", []) or []   # else stale from prior implement()
            return out
        return self.implement(idea)
