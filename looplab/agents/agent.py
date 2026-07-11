"""Tool-using Researcher (ADR-16): a bounded multi-turn agent loop where the LLM may
call retrieval tools (grep / kb_search / read) before emitting its final structured
Idea. Realizes "the agent chooses lexical-nav vs semantic" — retrieval is a toolset
the model drives, not a fixed pipeline.

Drops in behind the same `Researcher` Protocol as the plain LLMResearcher, so the
orchestrator is unchanged.

The reusable loop machinery (`drive_tool_loop`, `agentic_text`/`agentic_struct`, the
phase-handoff ledger, `CompositeTools`, …) lives in the sibling `agents.tool_loop` and is
re-imported below under its original names, so every historical import/monkeypatch path
through this module holds. `run_phase` stays HERE (see the note above it).
"""
from __future__ import annotations

from typing import Optional

from looplab.core import tracing
from looplab.core.llm import BudgetExceeded
from looplab.core.models import Idea, Node, RunState
from looplab.core.parse import ParseError, parse_structured
from looplab.core.prompts import PromptStore, render
from looplab.agents.roles import (
    _OPERATOR_NOTE, _attention_points, _clamp_fill, _hypothesis_system_suffix,
    _researcher_capability_suffix, _state_brief, collect_hint_cues)
# The tool-loop machinery was split into `agents.tool_loop`. Every moved name is RE-IMPORTED here
# under its original name because callers and tests import AND monkeypatch them THROUGH this
# module — `looplab.agents.agent.agentic_struct` / `.drive_tool_loop` are documented patch seams
# (novelty.py names the former; tests/test_repo_dev_plan.py & tests/test_report.py patch the
# latter), and the flat `looplab.agent.X` alias resolves to this same module — so both paths must
# keep resolving to the SAME objects.
from looplab.agents.tool_loop import (  # noqa: F401
    CompositeTools, _PLAN_TOOL_NAME, _REPEAT_NOTE, _TRUNC_NOTE, _cap_tool_result,
    _flatten_transcript, _force_emit, _handoff_ctx, _plan_spec, _render_plan, _summarizer,
    agentic_struct, agentic_text, drive_tool_loop, handoff_scope, loop_opts_from_settings,
    summarize_phase)


# The "your idea space is the WHOLE experiment / the Developer owns HOW" guidance, as worded for
# ToolUsingResearcher's SYSTEM prompt. A SECOND, deliberately DIFFERENT wording lives in
# roles.py's `_IDEA_SPACE_PLAIN` (that one rides the plain researcher's per-turn user message).
# The two are NOT normalized — prompt strings are contracts and the phrasings have drifted — but
# both are named `_IDEA_SPACE_*` so `grep _IDEA_SPACE` surfaces the pair despite the byte drift.
_IDEA_SPACE_TOOL = ("Your idea space is the WHOLE experiment, not just hyperparameters: you may propose "
                    "changes to the model ARCHITECTURE, the LOSS/objective, the DATA (features, augmentation, "
                    "filtering, negatives, sampling), the TRAINING procedure, or the evaluation — anything "
                    "that could move the metric. Do NOT limit yourself to parameter tuning when a structural "
                    "change is the stronger experiment. Numeric knobs go in `params`; describe any non-numeric "
                    "or structural change (a new loss, an architecture tweak, a data-pipeline change) clearly "
                    "in `rationale` so the Developer can build it.\n"
                    "Propose WHAT to try and WHY (the concept + expected learning). You do not write the code "
                    "yourself — the Developer owns HOW, and is free to edit the repo's code to realise your "
                    "idea — but you ARE free to direct structural, code-level changes when they're warranted. ")


# run_phase deliberately did NOT move to `agents.tool_loop` with the rest of the loop machinery:
# tests monkeypatch `looplab.agents.agent.drive_tool_loop` (e.g. tests/test_repo_dev_plan.py's fake
# loop, driven through the repo Developer's stages/plan/implement phases) and rely on run_phase's
# internal `drive_tool_loop(...)` call resolving through THIS module's (patched) global at call
# time. Defined in tool_loop, that call would resolve tool_loop's UNPATCHED binding and the seam
# would silently break — behavior seams beat file size.
def run_phase(client, tools, messages, emit_spec, *, label: str, next_label: str = "the next phase",
              handoff: bool = True, finalize, fallback, **loop_kwargs):
    """`drive_tool_loop` + cross-phase handoff summaries. When a `handoff_scope` is active it (1)
    injects the briefs accumulated by earlier phases of this node into `messages` — so this phase
    (even a different ROLE) trusts what's already been explored instead of re-reading the repo/data —
    then (2) after the loop, distills THIS phase's transcript into the ledger (one best-effort LLM
    call) for the next phase. Pass `handoff=False` for a TERMINAL phase (nothing downstream reads its
    brief — the single-session implement, the last plan step, a repair) so it doesn't spend a wasted
    summary call. A drop-in for drive_tool_loop: with no active scope it just forwards."""
    ledger = _handoff_ctx.get()
    if ledger:                              # earlier phases produced briefs → inject them up front
        ins = 1 if (messages and messages[0].get("role") == "system") else 0
        messages.insert(ins, {"role": "user", "content": (
            "CONTEXT FROM EARLIER PHASES of this node (a coding agent — possibly a different role — "
            "already explored this; TRUST it and do NOT re-read the same files/dirs, read only what is "
            "genuinely new):\n" + "\n\n".join(ledger))})
    result = drive_tool_loop(client, tools, messages, emit_spec,
                             finalize=finalize, fallback=fallback, **loop_kwargs)
    if handoff and ledger is not None:      # non-terminal phase in an active scope → contribute a brief
        # Wrap the summary call in its OWN operation span so it's a distinct, clearly-labeled band in
        # the UI trace ("handoff-summary") instead of an anonymous complete_text generation buried in
        # the phase — the summarization is visible/auditable, not a silent extra call.
        with tracing.operation("handoff-summary", handoff_from=label, handoff_to=next_label):
            s = summarize_phase(client, messages, phase=label, next_phase=next_label)
        if s:
            ledger.append(f"[{label}]\n{s}")
    return result


class ToolUsingResearcher:
    """Agentic Researcher (same `propose` Protocol as roles.LLMResearcher — see this module's
    docstring): drives a bounded multi-turn tool loop (`drive_tool_loop`, whose docs cover the
    turn/time/context budgets and history compression) in which the model may consult the run
    via retrieval tools before calling `emit` exactly once with its final Idea. Resilient by
    contract: malformed emits are sanitized, and parse/transport failures degrade to a safe
    bounds-filled Idea instead of crashing the run."""

    # P5 (docs/PROMPT_REVIEW.md): name only tools this role may actually have — the default
    # Researcher toolset has NO `read_file` (that's a RepoScoutTools name); its paginating reader
    # is `repo_read`, present on repo tasks only — and reconcile "you HAVE it" with the loop's
    # explicit truncation marker (a marked reply is PARTIAL, so the next range is new content).
    _SYSTEM = ("You are an ML researcher driving experiments to improve the objective. Investigate "
               "PROPERLY, then call `emit` exactly once with your final Idea — that ends your turn.\n"
               "Work FOCUSED, not scattered: pick the most promising direction/hypothesis from the state "
               "brief and RESEARCH THAT — read the relevant code and prior experiments fully enough to "
               "propose a correct, grounded experiment (a half-baked idea from shallow reading wastes a "
               "whole node). But read EFFICIENTLY: read a file ONCE, end to end if needed, and do NOT "
               "re-read a file/grep you already ran — if a read returned content, you HAVE it. Use the "
               "file-reading tools you actually have (on repo tasks `repo_read` paginates); paginated "
               "readers end a truncated reply with a resume marker — if a reply ends with a truncation "
               "marker, request the NEXT range instead of re-reading from the start. When you understand "
               "the change you want and can name its params, STOP and emit (operator, params, rationale, "
               "and a short reusable `theme` slug grouping related experiments, e.g. \"loss-fn\"); you "
               "refine on the NEXT node.\n"
               + _OPERATOR_NOTE + "\n"
               + _IDEA_SPACE_TOOL)

    def __init__(self, client, tools, space_hint: str = "",
                 bounds: Optional[dict] = None, parser: str = "tool_call",
                 max_turns: int = 0, prompts: Optional[PromptStore] = None,
                 context_budget_chars: int | None = None, time_budget_s: float = 0.0,
                 loop_opts: Optional[dict] = None, offer_sweep: bool = True,
                 handoff: bool = True):
        self.client = client
        self.tools = tools          # object with .specs() and .execute(name, args)
        self.space_hint = space_hint
        self.bounds = bounds
        self.parser = parser
        self.max_turns = max_turns          # 0 = unlimited (config-driven via Settings.agent_max_turns)
        self.time_budget_s = time_budget_s  # 0 = no wall-clock cap (Settings.agent_time_budget_s)
        self.prompts = prompts
        self.context_budget_chars = context_budget_chars   # H4: cap the growing tool-call history
        # P6: include the sweep offer only when the active Developer implements `idea.space`
        # (make_roles decides; default True keeps direct constructions byte-compatible).
        self.offer_sweep = offer_sweep
        # P25: contribute the propose→develop handoff brief only when a run_phase-based Developer
        # (the in-house repo developer's stages/plan/implement phases) will actually READ it;
        # False skips the per-node summary LLM call nobody consumes on single-shot developers.
        self.handoff = handoff
        # Collapse the two sources of context_budget_chars to ONE, here, once. loop_opts_from_settings
        # injects it into loop_opts AND it arrives as an explicit ctor kwarg; passing BOTH to run_phase
        # would hand it the keyword twice -> TypeError, caught by propose()'s broad except -> silent
        # fallback (the agentic Researcher DEAD in the default config, where the budget is always set).
        # Merging in __init__ makes the collision impossible by construction for every call site.
        self.loop_opts = dict(loop_opts or {})   # B1/C1/C2 tool-loop options (loop_opts_from_settings)
        self.loop_opts.setdefault("context_budget_chars", context_budget_chars)

    def _emit_spec(self) -> dict:
        return {"type": "function", "function": {
            "name": "emit", "description": "Emit the final Idea for the next experiment.",
            "parameters": Idea.model_json_schema()}}

    @staticmethod
    def _sanitize(args: dict) -> dict:
        """Coerce the model's emit args into a valid Idea shape: params must be numeric, so DROP
        any non-numeric param the model invents (e.g. {"new_metric": "linear"} on a code-edit
        task whose space is free-form) rather than letting it crash the run."""
        out = dict(args) if isinstance(args, dict) else {}
        params = out.get("params")
        if isinstance(params, dict):
            clean: dict = {}
            for k, v in params.items():
                try:
                    clean[k] = float(v)
                except (TypeError, ValueError):
                    pass
            out["params"] = clean
        else:
            out["params"] = {}
        return out

    def _validate_emit(self, args: dict) -> Optional[str]:
        # Pre-accept check for drive_tool_loop: a bad/empty emit is bounced back to the model with THIS
        # message so it re-emits, instead of being silently turned into a no-op idea. Returns an error
        # string to reject, or None to accept.
        try:
            idea = Idea.model_validate(self._sanitize(args))
        except Exception as e:  # noqa: BLE001
            return (f"it didn't parse ({str(e)[:180]}). Emit an object with `operator`, numeric "
                    "`params`, and a `rationale` naming WHAT you change and WHY")
        if not (idea.params or (idea.rationale or "").strip() or (idea.hypothesis or "").strip()):
            return ("it is EMPTY — no params and no rationale. Every experiment must state a concrete "
                    "change and its reason; propose a real one (a param OR a structural change)")
        return None

    def _finalize(self, args: dict) -> Idea:
        # Never let a malformed emit (non-numeric params, bad shape) crash the loop — sanitize,
        # then fall back to a rationale-preserving draft if validation still fails.
        try:
            return _clamp_fill(Idea.model_validate(self._sanitize(args)), self.bounds)
        except Exception:  # noqa: BLE001 - resilience: the run must survive a junk proposal
            rationale = str((args or {}).get("rationale", "") or "")[:500]
            operator = str((args or {}).get("operator") or "draft")
            return _clamp_fill(Idea(operator=operator, params={}, rationale=rationale), self.bounds)

    def _fallback(self, messages: list) -> Idea:
        # Force a structured emit from the accumulated context; if even that fails, return a
        # safe bounds-filled default so the run never crashes.
        try:
            idea = parse_structured(
                self.client, messages + [{"role": "user", "content": "Emit the Idea now."}],
                Idea, self.parser)
        except ParseError:
            idea = Idea(operator="draft", params={}, rationale="fallback (agent parse failed)")
        return _clamp_fill(idea, self.bounds)

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        if hasattr(self.tools, "bind_state"):    # let run-aware tools see the current search
            self.tools.bind_state(state, parent)
        from looplab.agents.hints import render_hint_directives
        hint_block = render_hint_directives(state.pending_hints)
        # A0d breadth-keyed complexity cue + Strategist `prefer_sweep` bias + T5 novelty-gate
        # re-propose feedback (each empty=off). Matches LLMResearcher's cue set exactly, so the
        # agentic path now honors the strategist's sweep nudge just like the plain researcher.
        cue = collect_hint_cues(self, ("_complexity_hint", "_sweep_hint", "_novelty_feedback",
                                       "_novelty_hint"))
        # Hypotheses ledger (P1): honor track_hypotheses on the agentic path too (default on, matching
        # config) — ask for the per-experiment `hypothesis` so the ledger of tested beliefs fills in.
        # Shared `_hypothesis_system_suffix` splices `_HYPOTHESIS_INSTRUCTION` identically to LLMResearcher.
        hyp = _hypothesis_system_suffix(getattr(self, "track_hypotheses", True))
        messages = [
            {"role": "system",
             # P6/P8: the shared capability suffix (sweep offer — gated — + eval_timeout) and the
             # hardware attention points reach the DEFAULT researcher too, appended AFTER the
             # render() so a `tool_researcher_system.md` override keeps them AND the code-owned
             # offer_sweep gate keeps deciding the sweep offer — the pattern now truly shared with
             # LLMResearcher (whose researcher_system default is likewise core-only) / LLMDeveloper.
             "content": render(self.prompts, "tool_researcher_system", self._SYSTEM)
                        + "\n" + _researcher_capability_suffix(getattr(self, "offer_sweep", True))
                        + self.space_hint + hyp
                        + "\n\n" + _attention_points()},
            {"role": "user", "content": _state_brief(state, parent,
                                                     digest_cap=getattr(self, "_digest_cap", 0),
                                                     hyp_order=getattr(self, "_hyp_order", None))
                + hint_block + cue +
                "\nDecide the next experiment — a parameter change OR a structural one (architecture, "
                "loss, data, training) if that's the stronger move. Consult knowledge if useful, then emit."},
        ]
        try:
            # context_budget_chars is folded into self.loop_opts once in __init__ (see there) — pass the
            # merged opts straight through, no per-call re-merge, no double-keyword collision.
            # P25: `handoff` is True only when a run_phase-based (repo) Developer follows — its
            # stages/plan/implement phases read the brief; the single-shot developers never do,
            # so no summary call is spent there and the label names the developer that ACTUALLY runs.
            return run_phase(
                self.client, self.tools, messages, self._emit_spec(),
                label="Researcher·propose",
                next_label=("the Developer (stages → plan → implement)"
                            if getattr(self, "handoff", True)
                            else "the Developer (single-shot implement)"),
                handoff=getattr(self, "handoff", True),
                max_turns=self.max_turns, time_budget_s=self.time_budget_s,
                finalize=self._finalize, fallback=self._fallback,
                validate=self._validate_emit, **self.loop_opts)
        except BudgetExceeded:      # hard budget stop -> propagate and end the run
            raise
        except Exception:  # noqa: BLE001 - a transport/endpoint failure (LLMError after retries) on
            # the flagship agentic path must NOT crash the run: degrade to a safe bounds-filled Idea,
            # the same contract as LLMResearcher / ToolUsingStrategist. `_fallback` is itself resilient
            # (parse_structured swallows LLMError -> draft Idea), so it can't re-raise the transport error.
            return self._fallback(messages)
