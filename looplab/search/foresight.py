"""FOREAGENT-adapted predict-before-execute (arXiv:2601.05930, "Can We Predict Before Executing
Machine Learning Agents?"). The Generate-Execute-Feedback loop has an EXECUTION BOTTLENECK: every
hypothesis is judged only by an expensive sandbox run. FOREAGENT internalizes execution priors — it
uses the LLM as an IMPLICIT WORLD MODEL to predict, WITHOUT executing, which candidate will score
best, primed with a "Verified Data Analysis Report", then verifies only the top pick
(Predict-then-Verify). On AI4Science that buys ~6x faster convergence and a wider search at a fixed
execution budget.

Adapted to LoopLab's two existing "predict before you spend an eval" seams so the mechanism
COMPOSES with what's here instead of replacing it:

* code candidates — best-of-N (search/best_of_n.py) calls `rank` over the statically-tied top
  candidates, upgrading from an execution-free STATIC score (+ a tie-break) into a real predictive
  ranker; the static score stays a cheap validity PRE-FILTER.
* hypotheses / ideas — `ForesightPanelResearcher` ranks K candidate ideas the numeric k-NN
  surrogate (serve/panel.py) is BLIND to (structural / text ideas), priming the predictor with the
  data profile AND the accumulated experiment memory (the EvoScientist-style synergy: memory feeds
  the world model, the world model turns memory into a pre-execution prediction). It ALSO
  prioritizes the OPEN-hypothesis board — the batch of untested beliefs that arrives from deep
  research, a human "+ Add", or the strategist — predicting which to test first instead of the
  arbitrary insertion order `_state_brief` shows today.

Replay-safety: the predictor only chooses WHICH already-generated candidate/idea becomes the node;
that choice is recorded in `node_created` (the code / the idea), so replay re-folds it and never
re-invokes the predictor — exactly like best-of-N and the researcher panel do today. The predictor
is purely advisory and FAILS OPEN: any malformed/absent LLM output makes it abstain, and the caller
falls back to its prior behavior.
"""
from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, Field

from looplab.agents.roles import RESEARCHER_HINT_ATTRS
from looplab.core.parse import parse_structured
from looplab.core.prompts import render

_REPORT_CAP = 2000     # per-source char bound for the priming "Verified Data Analysis Report"
_ITEM_CAP = 2000       # max chars rendered per candidate
_MAX_ITEMS = 20        # cap on candidates presented together in one predictive call (prompt-size bound;
                       # beyond it `rank` appends the remainder in input order rather than scoring them)


class _Ranking(BaseModel):
    """The predictor's output: candidate indices ordered best->worst, plus a calibrated confidence."""
    order: list[int] = Field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""


_SYSTEM = (
    "You are an ML research world-model. PREDICT, WITHOUT EXECUTING any code, which of several "
    "candidates will achieve the BEST metric on the task — internalizing execution priors the way an "
    "experienced researcher does. Reason from the Verified Data Analysis Report and the prior results "
    "about likely correctness, data-fit, overfitting / leakage risk, and the expected metric; do NOT "
    "assume anything you cannot infer from what you are given. Call `emit` with `order` = the "
    "candidate indices from BEST to WORST (0-based) and `confidence` in [0,1] = how sure you are "
    "(well-calibrated: low when the candidates look equivalent).")


def verified_report(*, brief: str = "", data_profile: Optional[dict] = None, memory: str = "",
                    cap: int = _REPORT_CAP) -> str:
    """Assemble the FOREAGENT priming context — the "Verified Data Analysis Report" the predictor
    reasons from: the task / data contract (`brief`), a compact view of the profiled data
    (`data_profile`), and the accumulated experiment memory (`memory`). Each source is bounded and
    any may be empty; returns "" when nothing is available (the caller then abstains)."""
    parts: list[str] = []
    if brief and brief.strip():
        parts.append("TASK / DATA CONTRACT:\n" + brief.strip()[:cap])
    if data_profile:
        try:
            prof = json.dumps(data_profile, default=str, sort_keys=True)
        except (TypeError, ValueError):
            prof = str(data_profile)
        parts.append("DATA PROFILE:\n" + prof[: cap // 2])
    if memory and memory.strip():
        parts.append("PRIOR RESULTS (experiment memory):\n" + memory.strip()[:cap])
    return "\n\n".join(parts)[: cap * 2]


def rank(client, report: str, items: list[str], *, goal: str = "", direction: str = "min",
         parser: str = "tool_call", prompts=None) -> Optional[tuple[list[int], float, str]]:
    """Predict a best->worst ordering over `items` (already-rendered candidate strings) with ONE LLM
    call. Returns `(order, confidence, reason)` where `order` is DISTINCT valid indices best-first (a
    partial order is tolerated — any index the model omitted is appended in input order) and `reason`
    is the model's one-line justification (the analysis trace, "" if none), or None on any failure /
    abstention. Never raises — the predictor is advisory and fails open."""
    if client is None or len(items) < 2:
        return None
    items = items[:_MAX_ITEMS]
    try:
        blocks = "\n\n".join(f"--- CANDIDATE {i} ---\n{c[:_ITEM_CAP]}" for i, c in enumerate(items))
        rep = (report or "").strip()
        user = ((("VERIFIED DATA ANALYSIS REPORT\n" + rep + "\n\n") if rep else "")
                + f"Goal: {goal or '(unspecified)'} | optimize direction: {direction}.\n\n"
                + blocks + "\n\n"
                + f"Predict the best->worst order of candidates 0..{len(items) - 1} and your confidence.")
        msgs = [{"role": "system", "content": render(prompts, "foresight_system", _SYSTEM)},
                {"role": "user", "content": user}]
        out = parse_structured(client, msgs, _Ranking, parser or "tool_call")
    except Exception:  # noqa: BLE001 — advisory predictor: fall back on ANY error (parse/transport)
        return None
    return _sanitize_ranking(out, len(items))


def _sanitize_ranking(out, n: int) -> Optional[tuple[list[int], float, str]]:
    """Turn a raw `_Ranking` into (order, confidence, reason): DISTINCT valid indices best-first (any
    the model dropped are appended in input order), confidence clamped to [0,1]. None if unusable."""
    if out is None:
        return None
    seen: set[int] = set()
    order: list[int] = []
    for idx in getattr(out, "order", None) or []:
        if isinstance(idx, int) and 0 <= idx < n and idx not in seen:
            seen.add(idx)
            order.append(idx)
    if not order:
        return None
    order.extend(i for i in range(n) if i not in seen)
    conf = out.confidence if isinstance(getattr(out, "confidence", None), (int, float)) else 0.0
    return order, max(0.0, min(1.0, float(conf))), (getattr(out, "reason", "") or "").strip()[:600]


def _rank_user_msg(report: str, items: list[str], goal: str, direction: str) -> str:
    blocks = "\n\n".join(f"--- CANDIDATE {i} ---\n{c[:_ITEM_CAP]}" for i, c in enumerate(items))
    rep = (report or "").strip()
    return ((("VERIFIED DATA ANALYSIS REPORT\n" + rep + "\n\n") if rep else "")
            + f"Goal: {goal or '(unspecified)'} | optimize direction: {direction}.\n\n"
            + blocks + "\n\n"
            + f"Predict the best->worst order of candidates 0..{len(items) - 1} and your confidence.")


def rank_agentic(client, tools, report: str, items: list[str], *, goal: str = "", direction: str = "min",
                 parser: str = "tool_call", prompts=None, max_turns: int = 4
                 ) -> Optional[tuple[list[int], float, str]]:
    """AGENTIC variant of `rank`: the world-model runs a TOOL-USING loop (`drive_tool_loop`) so it can
    PULL specific evidence — actual experiment results, data facts — via `tools` before committing to
    an order, instead of reasoning only from a pre-baked report. Emits the same `_Ranking`; returns
    the same `(order, confidence, reason)`. Falls back to the single-call `rank` when there are no
    tools, and on ANY loop error (advisory — never raises)."""
    if client is None or len(items) < 2:
        return None
    if tools is None:
        return rank(client, report, items, goal=goal, direction=direction, parser=parser, prompts=prompts)
    items = items[:_MAX_ITEMS]
    try:
        from looplab.agents.agent import drive_tool_loop
        emit_spec = {"type": "function", "function": {
            "name": "emit", "description": "Emit the predicted best->worst order + confidence.",
            "parameters": _Ranking.model_json_schema()}}
        msgs = [{"role": "system", "content": render(prompts, "foresight_system", _SYSTEM)
                 + " You MAY consult tools to check actual results before deciding; then call `emit`."},
                {"role": "user", "content": _rank_user_msg(report, items, goal, direction)}]
        box: dict = {}

        def _finalize(args):
            try:
                box["out"] = _Ranking.model_validate(args)
            except Exception:  # noqa: BLE001
                box["out"] = None
            return box.get("out")

        drive_tool_loop(client, tools, msgs, emit_spec, max_turns=max_turns,
                        finalize=_finalize, fallback=lambda _m: None,
                        nudge_prompt="Now call `emit` with the order.",
                        stuck_prompt="Stop ({reason}). Call `emit` with the order now.")
        got = _sanitize_ranking(box.get("out"), len(items))
        return got if got is not None else rank(client, report, items, goal=goal,
                                                direction=direction, parser=parser, prompts=prompts)
    except Exception:  # noqa: BLE001 — agentic path is best-effort; fall back to the one-shot predictor
        return rank(client, report, items, goal=goal, direction=direction, parser=parser, prompts=prompts)


def _idea_text(idea) -> str:
    """Render an idea for the predictor: the HYPOTHESIS (the belief under test) first, then the
    rationale + params/grid — so the ranking is over WHAT each experiment tests, not just its
    numbers (which is exactly where the numeric surrogate is blind)."""
    parts: list[str] = []
    hyp = getattr(idea, "hypothesis", None)
    if hyp:
        parts.append("Hypothesis: " + str(hyp))
    if getattr(idea, "rationale", ""):
        parts.append("Rationale: " + idea.rationale)
    if getattr(idea, "params", None):
        parts.append("Params: " + ", ".join(f"{k}={v}" for k, v in idea.params.items()))
    if getattr(idea, "space", None):
        parts.append("Sweep grid: " + "; ".join(f"{k} in {v}" for k, v in idea.space.items()))
    return "\n".join(parts) or (getattr(idea, "operator", "") or "idea")


def _novelty_rank_directive(stance: str) -> str:
    """The novelty clause appended to the predictor's report for the K->1 idea pick (slice 3). Empty
    for the default "balanced" stance (ranking stays a pure predicted-metric choice); "explore" tells
    the world-model to break near-ties toward the MORE DIVERGENT candidate so breadth isn't silently
    discarded; "exploit" reinforces backing the safest predicted improvement."""
    if stance == "explore":
        return ("\n\nNOVELTY DIRECTIVE (strategist stance = explore): the search is narrowing. When "
                "two candidates are close on predicted metric, PREFER the one that explores a more "
                "DIFFERENT direction / theme (broaden the hypothesis space); do not rank a near-"
                "duplicate of an already-tried idea first.")
    if stance == "exploit":
        return ("\n\nNOVELTY DIRECTIVE (strategist stance = exploit): back the candidate with the "
                "safest predicted improvement to the current leader; novelty is not a priority now.")
    return ""


def _memory_brief(state, parent) -> str:
    """The accumulated experiment memory that primes the hypothesis predictor (EvoScientist-style):
    the whole-search working set + the lineage lessons under the node being refined. Best-effort;
    "" when there's nothing yet. Local import keeps `search` import-time free of `events.digest`."""
    try:
        from looplab.events.digest import experiments_digest, lineage_lessons
        parts = [experiments_digest(state, char_cap=1200), lineage_lessons(state, parent)]
        return "\n".join(p for p in parts if p)
    except Exception:  # noqa: BLE001 — priming is best-effort; never break a proposal
        return ""


class ForesightPanelResearcher:
    """FOREAGENT-adapted predict-before-execute panel for HYPOTHESES / ideas. Generate K candidate
    ideas from the wrapped Researcher, then predict — WITHOUT executing — which will most improve the
    objective, using the LLM as an implicit world model primed with a Verified Data Analysis Report
    (`state.data_profile`) AND the accumulated experiment memory (the synergy). This ranks the
    STRUCTURAL / text ideas the numeric k-NN surrogate (serve/panel.py) cannot compare; on abstain
    (no client / malformed output / no signal) it returns the first proposal, so it degrades to a
    plain proposer rather than blocking the search.

    Behind the same `Researcher` Protocol, so it drops into `_engine`'s researcher-wrapper chain with
    no orchestrator change (parity with `PanelResearcher`). K=1 or a missing client is a transparent
    pass-through. Replay-safe: the chosen idea is recorded in `node_created`; replay never re-ranks.
    """

    def __init__(self, base, k: int = 2, *, client=None, bounds=None,
                 parser: str = "tool_call", prompts=None, tools=None):
        self.base = base
        self.k = max(1, k)
        self.client = client if client is not None else getattr(base, "client", None)
        self.bounds = bounds if bounds is not None else getattr(base, "bounds", None)
        self.parser = parser
        self.prompts = prompts if prompts is not None else getattr(base, "prompts", None)
        # When `tools` is wired, ranking runs in AGENTIC mode (a drive_tool_loop that can pull actual
        # experiment/data evidence before deciding); else it's the one-shot predictor. Optional.
        self.tools = tools
        self.last_foresight: Optional[dict] = None       # telemetry: last idea ranking + confidence
        self.last_hyp_priority: Optional[dict] = None     # telemetry: last board prioritization

    def _rank(self, report: str, items: list[str], *, goal: str, direction: str, kind: str = "idea"):
        """Dispatch to the agentic ranker when tools are wired, else the one-shot predictor. Runs in its
        OWN named trace so the ranking's LLM spans are isolated from the node's propose/implement trace —
        the captured (trace_id, span_id) rides on the telemetry dict and stamps the hypothesis_ranked /
        foresight_selected event, scoping its trace in the UI to JUST the ranking.

        `kind` names the span so the two DISTINCT ranking steps don't collapse into look-alike bands:
        board prioritization (`kind="board"`, runs BEFORE propose — picks which open hypothesis to
        pursue) traces as `hyp_prioritize`; idea predict-before-execute (`kind="idea"`, runs AFTER
        propose — scores the chosen proposal) traces as `foresight_rank`. Without this the UI showed
        two identical "Researcher · foresight" bands and the first read as a superfluous duplicate."""
        import contextlib
        from looplab.core import tracing
        tr = tracing._current_tracer.get()
        span_name = "hyp_prioritize" if kind == "board" else "foresight_rank"
        cm = tr.span(span_name, new_trace=True) if tr is not None else contextlib.nullcontext()
        with cm:
            self._last_rank_ids = tracing.current_ids() if tr is not None else (None, None)
            if self.tools is not None:
                return rank_agentic(self.client, self.tools, report, items, goal=goal, direction=direction,
                                    parser=self.parser, prompts=self.prompts)
            return rank(self.client, report, items, goal=goal, direction=direction,
                        parser=self.parser, prompts=self.prompts)

    def __getattr__(self, name):
        """Delegate every attribute NOT defined here (implement / repair / choose_action / assets /
        last_files / …) to the wrapped agent. This lets the panel wrap a UNIFIED agent (where the
        researcher IS the developer): `propose` is intercepted for predict-before-execute + board
        prioritization, while the whole DEVELOPER surface passes straight through to the SAME object,
        so the two handles never diverge (the R1 hazard that made the engine skip wrappers in unified
        mode). Only reached for names missing on the instance — `base`/`client`/`propose`/… are real
        attrs and never hit this. Guard dunders so copy/pickle/introspection don't misfire."""
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "base"), name)

    @property
    def space_hint(self) -> str:
        return getattr(self.base, "space_hint", "")

    def _forward_hints(self) -> None:
        """Mirror the engine-set steering hints onto the base Researcher so the panel stays
        transparent to novelty-gate feedback / complexity / sweep cues (the engine setattrs them on
        THIS wrapper — the active researcher — after construction)."""
        for attr in RESEARCHER_HINT_ATTRS:
            if hasattr(self, attr):
                setattr(self.base, attr, getattr(self, attr))

    def _prioritize_board(self, state, parent) -> None:
        """FOREAGENT prioritization of the OPEN-hypothesis board. Untested beliefs arrive in batches
        from many sources — deep-research `recommended_directions`, a human "+ Add", the strategist —
        and all land as `open` hypotheses in `state.hypotheses`; today `_state_brief` shows them in
        arbitrary insertion order and truncates to 5, so which gets tested (and which is silently
        dropped) is luck. Here the world model PREDICTS which is most likely to pay off (primed with
        the data profile + experiment memory) and steers the base Researcher to test it first, by
        setting `_hyp_order` (read by `_state_brief` to order the board best-first). No-op with <2
        open hypotheses or on abstain; the resulting node's `idea.hypothesis` is what's recorded, so
        replay never re-ranks."""
        self.last_hyp_priority = None
        hyps = [h for h in (state.hypotheses or {}).values()
                if getattr(h, "status", "") == "open" and not getattr(h, "evidence", None)]
        if len(hyps) < 2:
            setattr(self.base, "_hyp_order", None)
            return
        r = self._rank(
                 verified_report(data_profile=state.data_profile, memory=_memory_brief(state, parent)),
                 ["Hypothesis: " + h.statement for h in hyps],
                 goal=state.goal, direction=state.direction, kind="board")
        if r is None:
            setattr(self.base, "_hyp_order", None)
            return
        order, conf, reason = r
        ids = [hyps[i].id for i in order]
        setattr(self.base, "_hyp_order", ids)
        # Telemetry the ENGINE reads after propose() to emit the `hypothesis_ranked` audit event
        # (engine = sole event writer): the predicted board order + confidence + the model's analysis
        # trace (`reason`). `ranked` pairs id->statement so the event/UI needn't re-resolve ids.
        _tid, _sid = getattr(self, "_last_rank_ids", (None, None))
        self.last_hyp_priority = {
            "order": ids, "confidence": conf, "reason": reason, "n": len(hyps),
            "ranked": [{"id": hyps[i].id, "statement": hyps[i].statement} for i in order],
            "_trace_id": _tid, "_span_id": _sid}   # stamped onto the hypothesis_ranked event by the engine

    def propose(self, state, parent):
        if self.client is None:
            return self.base.propose(state, parent)     # transparent pass-through
        self._forward_hints()
        if self.tools is not None and hasattr(self.tools, "bind_state"):
            self.tools.bind_state(state)                 # let the agentic ranker read the live run
        self._prioritize_board(state, parent)            # rank the open-hypothesis board, steer the base
        if self.k == 1:
            return self.base.propose(state, parent)      # board prioritized; single proposal
        ideas = [self.base.propose(state, parent) for _ in range(self.k)]
        # Slice 3: the Strategist's novelty stance biases the K->1 pick. "balanced" (default) leaves
        # the ranking a pure predicted-metric choice — byte-identical to today; "explore" appends a
        # directive so that when candidates are close the ranker PREFERS the more novel/divergent one
        # (breadth is otherwise silently discarded here). Injected via the report, so `_SYSTEM` and
        # the ranker signatures are untouched.
        stance = getattr(self, "_novelty_stance", "balanced")
        report = verified_report(data_profile=state.data_profile, memory=_memory_brief(state, parent))
        report += _novelty_rank_directive(stance)
        r = self._rank(report, [_idea_text(i) for i in ideas],
                       goal=state.goal, direction=state.direction)
        if r is None:
            self.last_foresight = None
            return ideas[0]
        order, conf, reason = r
        best = ideas[order[0]]
        # Telemetry the engine reads after propose() to emit `foresight_selected` (engine = sole event
        # writer): WHICH of the K generated ideas won + the discarded alternatives + confidence + the
        # model's analysis trace + the novelty stance in force. Without it only the winner survives.
        _tid, _sid = getattr(self, "_last_rank_ids", (None, None))
        self.last_foresight = {
            "kind": "idea", "method": "foresight", "n": len(ideas), "k": self.k,
            "chosen": order[0], "order": order, "confidence": conf, "reason": reason,
            "novelty_stance": stance,
            "candidates": [" ".join(_idea_text(i).split())[:160] for i in ideas],
            "_trace_id": _tid, "_span_id": _sid}   # stamped onto the foresight_selected event by the engine
        best.rationale = (best.rationale
                          + f" [foresight: predicted best of {self.k} pre-execution]").strip()
        return best
