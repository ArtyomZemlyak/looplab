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
    except (SyntaxError, ValueError):
        # SyntaxError/IndentationError = malformed; ValueError = e.g. a NUL byte in the source. Both mean
        # un-runnable — score it worst, never let a bad candidate raise out of the best-of-N ranking.
        return 0.0
    if validate_agent_code(code).ok:
        s += 2.0
    if "metric" in code:
        s += 0.5
    return s


def refuse_unrankable_best_of_n(developer, n: int) -> None:
    """Refuse `best_of_n > 1` for a Developer whose answer is not the code this module ranks.

    **ASK THE DEVELOPER, don't infer it from the backend** — the same rule
    `agents/factory.py::_offer_sweep` learned the hard way. `_score` above ranks the STRING
    `implement()` returns; `adapters/repo_developer.py::LLMRepoDeveloper.implement` returns a
    SENTINEL (`""` = "the files are the answer" — the artifact travels on `last_files`). So on every
    repo task the operator's `best_of_n` bought N full builds and then discarded the selection
    entirely: all N candidates score -1.0, `top` holds them all, the FOREAGENT ranker and the D10
    tie-break are both skipped because `len({"", ""}) == 1`, and `chosen = top[0]` — candidate 0 wins
    even when it is the one with the syntax error. That is not a degraded selection, it is a paid
    coin flip with a fixed outcome: measured over the 52 real repo builds in `runs/`, a build costs
    7.37M prompt tokens, so `best_of_n=3` was +14.7M tokens per node for an answer the setting had no
    influence over (`docs/BACKLOG.md` §0.18).

    It REFUSES rather than silently dropping to N=1, on this repo's own precedent
    (`core/config.py::DEVELOPER_BACKEND_ALIASES` is deliberately wider than the launch set "because
    admitting it at launch would be the silent downgrade the closed set exists to stop"): an operator
    who typed `best_of_n=3` and got one build with no message cannot tell the knob from a no-op.
    `ConfigRefusal` because it is a fact about the operator's own input, so `cli/__init__.py`'s
    refusal boundary prints it as one line at exit 2 — and on a LIVE Strategist developer swap the
    same raise is caught by `engine/strategy.py::_prepare_strategy_developer` and recorded as the
    durable `developer_application: refused` receipt, which is the right answer there too.

    Teaching `_score` to read `last_files` instead was measured and NOT built: the only
    execution-free discriminator docs/36 permits (does it parse — a selector may REFUSE a candidate,
    never ELECT one on a model's opinion) scores 683 of 683 authored `.py` files in `runs/` as valid,
    i.e. it would have separated nothing. A selector with measured discrimination of zero is the
    unverified claim `docs/BACKLOG.md` §0.18 exists to end.

    `answers_with_code` is a POSITIVE marker (absent means NO — `agents/roles.py`, forwarded
    read-through by `WrapsDeveloper`), so a third-party or templated Developer is fail-closed by
    omission rather than silently billed.
    """
    if n > 1 and not getattr(developer, "answers_with_code", False):
        from looplab.core.errors import ConfigRefusal
        raise ConfigRefusal(
            f"best_of_n={n} cannot be honoured by the active Developer "
            f"({type(developer).__name__}): best-of-N ranks the code `implement()` returns, and this "
            "Developer answers on `last_files` instead — every candidate would score identically and "
            "candidate 0 would always win, after N full builds were paid for. Set best_of_n=1.")


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

    Forwarding (brief/client/prompts/is_code_generating/last_report) and one-candidate output sync
    come from `WrapsDeveloper`; the N-candidate path restores the winning files/deletions/footprint."""

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
        self.last_footprint: dict | None = None
        self.last_n_scores: list[float] = []
        # The predictive pick for THIS call (order/confidence/reason) or None when the ranker didn't
        # decide the pick — the engine reads it to emit `foresight_selected`, and `audit_extra`/the D10
        # guard derive "did foresight decide?" from `is not None` (one source of truth, no stale bool).
        self.last_foresight_pick: dict | None = None
        # Candidate snapshots are (code, files, deleted, footprint). The footprint belongs to the
        # selected code just as strongly as its multi-file patch; restoring the inner's final call
        # would otherwise attach candidate N's resources to a different winning implementation.
        self._last_candidates: list[tuple[str, dict, list, object]] = []

    def audit_extra(self) -> dict:
        extra = super().audit_extra()
        extra["best_of_n"] = self.n
        extra["foresight"] = self.last_foresight_pick is not None
        return extra

    def implement(self, idea: Idea) -> str:
        return self._best_of(idea, lambda: self.inner.implement(idea))

    def implement_from(self, idea: Idea, parent) -> str:
        """Parent-aware best-of-N (arch-review §4 P1-9): forward the inner developer's `implement_from`
        so an IMPROVE/REFINE still starts from the parent's actual solution through the BestOfN wrapper.
        Without exposing this, the engine's `getattr(developer, 'implement_from')` capability check saw
        only BestOfN's plain `implement` and regenerated every child from the pristine baseline."""
        impl_from = getattr(self.inner, "implement_from", None)
        if not callable(impl_from):
            return self.implement(idea)     # inner has no parent-aware path -> plain best-of-N
        return self._best_of(idea, lambda: impl_from(idea, parent))

    def repair_from(self, idea: Idea, node, error: str) -> str:
        """Parent-aware repair, forwarded like `implement_from` — the fix is seeded from the FAILING
        node's own files. Single-shot (repair is not best-of-N'd), mirroring `repair`."""
        rf = getattr(self.inner, "repair_from", None)
        if not callable(rf):
            return self.repair(idea, getattr(node, "code", ""), error)
        self.last_foresight_pick = None     # repair uses no predictive ranker: clear the prior pick
        out = rf(idea, node, error)
        self._sync_audit()                  # else last_files stale from a prior implement()
        return out

    def _best_of(self, idea: Idea, gen_one) -> str:
        """Run `gen_one()` (one inner implement / implement_from candidate) N times and pick the best by
        the execution-free static score, then the FOREAGENT predictor / D10 list-wise tie-break. Shared
        by `implement` and `implement_from` so both get identical best-of-N + parent-aware behavior."""
        if self.n == 1:
            code = gen_one()
            self._sync_audit()
            self.last_n_scores = [_score(code)]
            return code
        self.last_n_scores = []          # per-node telemetry: reset so it holds only THIS node's N
        cands: list[tuple[str, dict, list, object, float]] = []
        for _ in range(self.n):
            code = gen_one()
            sc = _score(code)
            self.last_n_scores.append(sc)
            raw_footprint = getattr(self.inner, "last_footprint", None)
            footprint = dict(raw_footprint) if isinstance(raw_footprint, dict) else raw_footprint
            cands.append((code, getattr(self.inner, "last_files", {}) or {},
                          getattr(self.inner, "last_deleted", []) or [], footprint, sc))
        best_score = max(c[4] for c in cands)
        top = [c for c in cands if c[4] >= best_score - 1e-9]
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
        self.last_files, self.last_deleted, self.last_footprint = chosen[1], chosen[2], chosen[3]
        return chosen[0]

    def repair(self, idea: Idea, code: str, error: str) -> str:
        repair = getattr(self.inner, "repair", None)
        if callable(repair):
            self.last_foresight_pick = None   # repair uses no predictive ranker: clear the prior pick
            out = repair(idea, code, error)   # so this node's audit/`foresight_selected` isn't stale
            self._sync_audit()                # else last_files stale from prior implement()
            return out
        return self.implement(idea)
