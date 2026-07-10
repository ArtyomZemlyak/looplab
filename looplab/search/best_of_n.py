"""C2 · Best-of-N candidate selection (ADR-7). Generate N independent implementations and keep the
best by an EXECUTION-FREE reward (static validity + a metric-print signal) — the single most reliable
SWE-bench lever for weak local models (SWE-RM best-of-k: +10 pts), without spending an eval per
candidate. Wraps any Developer; forwards repair/audit hooks so it composes with the loop unchanged.

Applied only to the in-house LLM developer path (not expensive external coding agents), consistent
with the ADR-7 cost rule. N=1 is a transparent pass-through (== today).
"""
from __future__ import annotations

from looplab.agents.roles import WrapsDeveloper
from looplab.core.models import Idea
from looplab.core.prompts import render
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


def _listwise_pick(client, idea, candidates: list[str], parser: str = "tool_call",
                   prompts=None) -> int:
    """D10 (OPPO arXiv:2506.12928): comparative LLM selection over candidates presented TOGETHER —
    +~3 pts over independent pointwise scoring on GAIA, and beats majority voting. Used only to
    break a TIE among the top static-scorers (the execution-free score stays the primary filter;
    the LLM is a weak comparative prior, never the sole oracle — the eval still decides). Returns
    the index of the chosen candidate, or 0 on any failure."""
    try:
        from pydantic import BaseModel

        from looplab.core.parse import parse_structured
        from looplab.agents.agent import agentic_struct

        class _Pick(BaseModel):
            choice: int = 0
            reason: str = ""

        blocks = "\n\n".join(f"--- CANDIDATE {i} ---\n{c[:2000]}" for i, c in enumerate(candidates))
        msgs = [
            {"role": "system", "content": render(
                prompts, "bestofn_judge_system",
                "You are selecting the single best ML solution implementation from several candidates "
                "for the SAME task. Compare them side by side; prefer correct, complete, robust code "
                "that faithfully realizes the idea and avoids obvious bugs/leakage. Call `emit` with "
                "`choice` = the 0-based index of the best candidate.")},
            {"role": "user", "content":
             f"Idea: {getattr(idea, 'rationale', '') or ''}\n\n{blocks}\n\n"
             f"Pick the best candidate (0..{len(candidates) - 1})."},
        ]
        # Use the run's configured parser (threaded from settings.llm_parser) — a non-tool_call
        # backend (baml/json/guided) must not be forced through tool_call, or the selection
        # silently no-ops to top[0].
        # AGENTIC: upgrade to `agentic_struct` so the ranker MAY read the real experiments/code
        # (read_experiment/read_code via RunTools) before emitting its pick, instead of judging from
        # the truncated candidate blocks alone. No RunState reaches this selection path (the Developer
        # protocol's `implement(idea)` carries no state, and `WrapsDeveloper` forwards only
        # brief/client/prompts) — so `tools=None`, which makes `agentic_struct` degrade to the exact
        # `parse_structured` call below. The fallback preserves the old behavior on any agentic failure.
        out = agentic_struct(
            client, None, msgs, _Pick, parser=(parser or "tool_call"),
            loop_opts={"max_turns": 15},
            fallback=lambda m: parse_structured(client, m, _Pick, parser or "tool_call"))
        if isinstance(out.choice, int) and 0 <= out.choice < len(candidates):
            return out.choice
    except Exception:  # noqa: BLE001 — selection is advisory; fall back to the first top-scorer
        pass
    return 0


class BestOfNDeveloper(WrapsDeveloper):
    """Generate `n` candidates from `inner.implement` and return the best. The EXECUTION-FREE static
    score is the primary filter; when `listwise` is on and the top scorers TIE, an LLM comparative
    selection (D10) breaks the tie — the LLM as a weak comparative prior, never the sole oracle.
    Deterministic given a deterministic inner (toy); with an LLM at temperature>0 the candidates
    vary, so best-of-N actually explores. `repair` delegates to inner (single attempt).

    Forwarding (brief/client/prompts/is_code_generating/last_report) comes from `WrapsDeveloper`."""

    def __init__(self, inner, n: int = 3, listwise: bool = True, parser: str = "tool_call",
                 foresight: bool = True, direction: str = "min", goal: str = "",
                 min_confidence: float = 0.0):
        self.inner = inner
        self.n = max(1, n)
        self.listwise = listwise
        # §1 confidence gate: below this predicted confidence the foresight pick ABSTAINS (leaves
        # last_foresight_pick=None so the D10 tie-break runs), rather than committing a low-confidence
        # choice. 0.0 (default) = off — byte-identical to the historical behavior.
        self.min_confidence = max(0.0, float(min_confidence))
        # Run objective, threaded into the FOREAGENT ranker so its predict-before-execute world model
        # optimizes for the RIGHT direction. `foresight.rank` defaults to direction="min"; without this
        # a max-direction task (accuracy/AUC/F1) would be told to prefer the LOWEST-predicted candidate.
        self.direction = direction or "min"
        self.goal = goal or ""
        # FOREAGENT predict-before-execute (search/foresight.py): rank the statically-runnable
        # candidates with the LLM world model — a real predictor, not just the D10 tie-break — before
        # spending an eval. ON by default; a no-op without a client or with <2 distinct candidates.
        self.foresight = foresight
        self.parser = parser or "tool_call"
        self.last_files: dict = {}
        self.last_deleted: list = []
        self.last_n_scores: list[float] = []
        # The predictive pick for THIS call (order/confidence/reason) or None when the ranker didn't
        # decide the pick — the engine reads it to emit `foresight_selected`, and `audit_extra`/the D10
        # guard derive "did foresight decide?" from `is not None` (one source of truth, no stale bool).
        self.last_foresight_pick: dict | None = None
        self._last_candidates: list[tuple[str, dict, list]] = []   # (code, files, deleted) per N

    def audit_extra(self) -> dict:
        extra = super().audit_extra()
        extra["best_of_n"] = self.n
        extra["foresight"] = self.last_foresight_pick is not None
        return extra

    def implement(self, idea: Idea) -> str:
        if self.n == 1:
            code = self.inner.implement(idea)
            self._sync_audit()
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
        chosen = top[0]
        self.last_foresight_pick = None
        # FOREAGENT: predict-before-execute (arXiv:2601.05930). Among the top static-scorers — the
        # validity-tied candidates the execution-free score can't separate — the LLM world model
        # predicts which will score best WITHOUT running any, promoting the LLM from D10 tie-break-only
        # to a genuine ranker primed with the task/data brief (the Verified Data Analysis Report). The
        # static score stays the VALIDITY FLOOR (`top` excludes broken/no-metric candidates), so a
        # hunch can never beat a valid candidate with a likely-invalid one. Fails open: on abstain
        # (no client / <2 distinct / malformed output) `chosen` stays top[0] and the D10 tie-break runs.
        if self.foresight and self.client is not None and len({c[0] for c in top}) > 1:
            # Call the ranker directly (not rank_solutions) to keep the FULL prediction — order,
            # confidence, and the model's reason — for the `foresight_selected` audit event, not just
            # the winning index.
            from looplab.search.foresight import rank, verified_report
            r = rank(self.client, verified_report(brief=getattr(self, "brief", "") or ""),
                     [c[0] for c in top], goal=self.goal, direction=self.direction,
                     parser=self.parser, prompts=self.prompts)
            if r is not None:
                order, conf, reason = r
                # §1 confidence gate: only let the prediction DECIDE (and be recorded as a committed
                # pick) when it's confident enough; below the threshold leave last_foresight_pick=None
                # so the D10 tie-break runs, exactly as on a ranker abstain. 0.0 default = off.
                if conf is None or conf >= self.min_confidence:
                    chosen = top[order[0]]
                    self.last_foresight_pick = {
                        "kind": "solution", "method": "foresight", "n": len(top),
                        "chosen": order[0], "order": order, "confidence": conf, "reason": reason}
        # D10: break a tie among the top static-scorers with a list-wise LLM comparison (advisory).
        # Only when the predictor abstained, there are >1 DISTINCT candidates (a temperature-0 inner
        # developer yields N identical strings — a full LLM comparison of identical code is wasted),
        # and a client is available.
        if (self.last_foresight_pick is None and self.listwise and self.client is not None
                and len({c[0] for c in top}) > 1):
            # Pass the prompt store only when one is configured: callers/tests monkeypatch
            # `_listwise_pick` with its historical 4-arg signature, so the default (no-store)
            # path must keep that call shape unchanged.
            kw = {"prompts": self.prompts} if self.prompts is not None else {}
            idx = _listwise_pick(self.client, idea, [c[0] for c in top], parser=self.parser, **kw)
            chosen = top[idx]
        self.last_files, self.last_deleted = chosen[1], chosen[2]
        return chosen[0]

    def repair(self, idea: Idea, code: str, error: str) -> str:
        repair = getattr(self.inner, "repair", None)
        if callable(repair):
            self.last_foresight_pick = None   # repair uses no predictive ranker: clear the prior pick
            out = repair(idea, code, error)   # so this node's audit/`foresight_selected` isn't stale
            self._sync_audit()                # else last_files stale from prior implement()
            return out
        return self.implement(idea)
