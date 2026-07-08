"""Tool-using Researcher (ADR-16): a bounded multi-turn agent loop where the LLM may
call retrieval tools (grep / kb_search / read) before emitting its final structured
Idea. Realizes "the agent chooses lexical-nav vs semantic" — retrieval is a toolset
the model drives, not a fixed pipeline.

Drops in behind the same `Researcher` Protocol as the plain LLMResearcher, so the
orchestrator is unchanged.
"""
from __future__ import annotations

import itertools
import json
import time
from typing import Optional

from looplab.core import tracing
from looplab.core.llm import BudgetExceeded
from looplab.core.models import Idea, Node, RunState
from looplab.core.parse import ParseError, parse_structured
from looplab.core.prompts import PromptStore, render
from looplab.agents.roles import (
    _clamp_fill, _hypothesis_system_suffix, _state_brief, collect_hint_cues)


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


class CompositeTools:
    """Merge several tool providers (each with .specs()/.execute()) into one toolset,
    so the Researcher can use knowledge + skills + memory tools together."""

    def __init__(self, providers: list):
        self.providers = providers
        self._route: dict[str, object] = {}
        for p in providers:
            for spec in p.specs():
                self._route[spec["function"]["name"]] = p

    def specs(self) -> list[dict]:
        return [s for p in self.providers for s in p.specs()]

    def execute(self, name: str, args: dict) -> str:
        p = self._route.get(name)
        return p.execute(name, args) if p else f"(unknown tool: {name})"

    def bind_state(self, state, parent=None) -> None:
        """Forward the live run to any run-aware provider (RunTools/DataTools); others ignore it."""
        for p in self.providers:
            if hasattr(p, "bind_state"):
                p.bind_state(state, parent)


def _force_emit(client, messages: list, emit_spec: dict) -> Optional[dict]:
    """Force the model to return the structured emit via a forced `tool_choice`, returning the parsed
    args dict — or None when the client/endpoint can't force a tool call (a fake without
    `complete_tool`, an endpoint that ignores tool_choice, or a transport blip). Used when the model
    answered in prose instead of calling a tool: rather than nudge-and-hope (a reasoning model often
    keeps replying in prose — the bug that left the boss "talking but not acting"), we make ONE
    forced-emit call so we deterministically get a structured result. `complete_tool` always names
    the forced tool `emit` and returns `calls[0].arguments`, so it works for any emit schema
    regardless of `emit_spec`'s function name."""
    schema = (emit_spec.get("function") or {}).get("parameters") or {}
    try:
        out = client.complete_tool(list(messages), schema)
    except BudgetExceeded:                 # a hard budget stop must propagate, never be swallowed here
        raise
    except Exception:  # noqa: BLE001 - no complete_tool / endpoint ignored tool_choice / transport
        return None
    if out is None:                        # a client that RETURNS None means "couldn't force" — keep
        return None                        # it None so the caller nudges + retries, not finalize({})
    # Coerce a valid-but-non-object emit ("[…]", "\"x\"", "3") to {} so finalize()'s `.get()` can't
    # AttributeError — the same guard the in-loop emit path applies.
    return out if isinstance(out, dict) else {}


_PLAN_TOOL_NAME = "update_plan"

# G2 read-dedup classification (exact tool names, not substrings — a substring blocklist classified
# revert_file/git_checkout/reset_node as "read-only" and dedup-froze cursor-polls like read_output).
# _DEDUP_SAFE: readers whose (name,args)->result is STABLE within one loop unless a tool from the
# "everything else" class runs — safe to suppress an exact repeat AND to keep cached across turns.
_DEDUP_SAFE = frozenset((
    "read_file", "grep", "find_files", "list_dir",              # repo scouts / assistant fs readers
    "repo_read", "repo_list", "repo_grep",                      # cross-run repo scouts
    "read_note", "list_notes", "recall_notes", "kb_search",     # knowledge base
    "pkg_info", "py_api", "read_installed", "grep_installed",   # env inspector (static packages)
    "list_themes", "search_lessons",                            # lesson store (frozen within a loop)
))
# _READ_ONLY_VOLATILE: read-only but legitimately TIME-VARYING (polls, git state, live-run readers,
# hardware) — never deduped (each call may see new data: dedup here broke background-job polling and
# tripped the StuckDetector on evolving observations) and never invalidates the cache (mutates nothing).
_READ_ONLY_VOLATILE = frozenset((
    "read_output", "list_background",                           # background-command cursor polls
    "git_status", "git_diff", "git_log",                        # working-tree state
    "gpu_info",                                                 # hardware utilization varies
    "list_runs", "list_all_runs", "read_run", "read_run_code", "read_run_experiment",
    "read_run_logs", "read_run_trace", "list_experiments", "list_sibling_runs",
    "read_sibling_code", "read_sibling_experiment", "read_code", "read_experiment",
    "read_asset", "read_logs", "data_profile", "data_schema",
    "find_analogous", "find_analogous_across_runs",
))


def _plan_spec() -> dict:
    """C1 (TodoWrite-style) self-plan tool: the agent records/updates its OWN working TODO so it
    keeps the goal in view across a long tool-loop. Recording a plan never finishes the task."""
    return {"type": "function", "function": {
        "name": _PLAN_TOOL_NAME,
        "description": ("Record or update your working TODO/plan for THIS task so you don't lose "
                        "track across turns. Call it whenever your plan changes. It does NOT finish "
                        "the task — you still emit your final answer separately."),
        "parameters": {"type": "object", "properties": {
            "plan": {"type": "string", "description": "Short free-form plan / next steps."},
            "todos": {"type": "array", "description": "Checklist items with a status.",
                      "items": {"type": "object", "properties": {
                          "item": {"type": "string"},
                          "status": {"type": "string",
                                     "enum": ["pending", "in_progress", "done"]}},
                          "required": ["item"]}}}}}}


def _render_plan(args: dict) -> str:
    """Flatten an update_plan call into a compact human-readable TODO block."""
    args = args or {}
    parts: list[str] = []
    plan = str(args.get("plan") or "").strip()
    if plan:
        parts.append(plan)
    todos = args.get("todos")
    if isinstance(todos, list):
        marks = {"done": "[x]", "in_progress": "[~]", "pending": "[ ]"}
        for t in todos:
            if not isinstance(t, dict):
                continue
            item = str(t.get("item") or "").strip()
            if not item:
                continue
            parts.append(f"{marks.get(str(t.get('status') or 'pending'), '[ ]')} {item}")
    return "\n".join(parts).strip()


def drive_tool_loop(client, tools, messages: list, emit_spec: dict, *,
                    max_turns: int = 0, context_budget_chars: int | None = None,
                    time_budget_s: float = 0.0, finalize=None, fallback=None,
                    stuck_detection: bool = True,
                    stuck_repeat: int = 4, stuck_alternate: int = 4,
                    self_plan: bool = False, plan_reinject_every: int = 5,
                    auto_summary: bool = False, summary_client=None, on_step=None, on_text=None,
                    cancel_check=None, on_tool_result=None,
                    nudge_prompt: str = "", stuck_prompt: str = "",
                    validate=None, emit_retries: int = 2, emit_after: int = 0, emit_force: int = 0):
    """Multi-turn tool loop shared by every tool-using agent (Researcher, unified-agent pilot/triage,
    Boss, genesis scout, cross-run report). The model MAY call the provided retrieval tools across
    turns; when it calls the emit function (named in `emit_spec`), `finalize(args)` is returned. If
    the loop ends without an emit, `fallback(messages)` is returned. `tools` may be None (emit-only).

    Limits are caller-supplied (and ultimately config-driven, NOT hardcoded), and default to
    UNLIMITED so the agent is never cut off mid-reasoning:
      - `max_turns` (0 = unlimited): max number of tool turns before falling through to `fallback`.
      - `time_budget_s` (0 = off): WALL-CLOCK ceiling across turns — a new turn is not started once
        exceeded (a turn already in flight isn't interrupted — that's the LLM client's per-call
        timeout's job). Set it to bound an interactive request behind a proxy gateway timeout.

    Safe-by-default unlimited operation (the point of "the agents may loop forever in their own
    loop"): `max_turns`/`time_budget_s` are only BACKSTOPS. What actually stops a stuck loop is the
    `StuckDetector` (B1, default ON via `stuck_detection`): when the model repeats the SAME call (or
    ping-pongs between two, or keeps hitting the SAME error) with no progress, we force the final
    emit and finish instead of spinning forever. Thresholds are config-driven (`stuck_repeat` /
    `stuck_alternate`); a FRESH detector is built per call so state never leaks across loops.

    Optional long-horizon aids:
      - `self_plan` (C1): expose a TodoWrite-style `update_plan` tool so the agent keeps its OWN
        TODO; the current plan is re-injected as a reminder every `plan_reinject_every` turns.
      - `auto_summary` (C2): when the history exceeds `context_budget_chars`, LLM-summarize the
        stale middle instead of only middle-truncating it (falls back to truncation on any error).
      - `on_step(event)` (optional): a best-effort PROGRESS callback so a long agentic loop is not an
        opaque "thinking" spinner. Called with a small dict — {"turn", "tool", "arg"} as the model
        invokes a retrieval tool — so a caller (e.g. the genesis endpoint) can surface "reading
        README.md" / "listing /repo" live to the UI. Never affects control flow; any exception it
        raises is swallowed (transparency must not change behaviour).
      - `on_tool_result(name, args, result)` (optional): a per-tool-call DATA hook invoked after a
        retrieval tool actually EXECUTES, with the parsed args and the 4000-char-capped result
        string (exactly what the tool message will carry) — so a caller can record provenance
        (the DeepResearcher's consulted-sources ledger) without re-implementing the loop. Not
        called for the emit, the `update_plan` tool, or a cancel-stubbed call. Unlike
        `on_step`/`on_text` this is data collection, not transparency, so exceptions PROPAGATE
        (the caller's error handling owns them, same as a raising `tools.execute`).
      - `nudge_prompt` / `stuck_prompt` (optional): caller-supplied wording for the two mid-loop
        user nudges (the prose-stall retry, and the stuck-detector stop). Prompt strings are
        contracts, so a caller folded onto this loop keeps its historical wording byte-identical
        via these instead of inheriting the generic default. `stuck_prompt` may contain a literal
        `{reason}` placeholder (substituted via `str.replace`, NOT `str.format`, so prompt wording
        with other literal braces — JSON examples etc. — is safe); empty ("") = the default wording.

    Termination under "unlimited": when the model answers WITHOUT calling a tool (it considers
    itself done), we FORCE the structured emit immediately (`_force_emit`) and finish — so a prose
    reply becomes a real result instead of looping forever. If the client can't force a tool call,
    we fall back to a bounded nudge-and-retry (two consecutive prose turns ⇒ stop) so the loop
    always terminates regardless of `max_turns`.

    Pure mechanics: callers own prompt construction, the emit schema, and result coercion —
    so the SAME loop drives an Idea emit, a code emit, an action choice, or a strategy emit.
    """
    emit_name = emit_spec["function"]["name"]
    def _step(**ev):                    # best-effort progress ping; never let it perturb the loop
        if on_step is None:
            return
        try:
            on_step(ev)
        except Exception:               # noqa: BLE001 - transparency must not change behaviour
            pass
    def _text(content):                 # interstitial assistant prose (a message written BEFORE a tool
        if on_text is None:             # round) — surfaced live so the chat reads like Claude Desktop
            return                      # (what the agent is thinking out loud between tool calls).
        try:
            s = (content or "").strip()
            if s:
                on_text(s)
        except Exception:               # noqa: BLE001 - transparency must not change behaviour
            pass
    def _cancelled() -> bool:           # guarded probe — a broken cancel_check must not wedge the loop
        if cancel_check is None:
            return False
        try:
            return bool(cancel_check())
        except Exception:               # noqa: BLE001
            return False
    stuck = None
    if stuck_detection:                 # a FRESH detector per call — never share state across loops
        from looplab.agents.stuck import StuckDetector
        stuck = StuckDetector(repeat_threshold=stuck_repeat, alternate_threshold=stuck_alternate)
    tool_specs = ((tools.specs() if tools is not None else []) + [emit_spec])
    if self_plan:
        tool_specs = tool_specs + [_plan_spec()]
    current_plan = ""
    started = time.monotonic()
    # D11: history compression runs on the dedicated cheap compressor when configured, else the
    # loop's own client. Loop-invariant: build once, not per turn.
    summarize = _summarizer(summary_client or client) if auto_summary else None
    stalls = 0                          # consecutive prose turns we couldn't turn into a forced emit
    emit_rejects = 0                    # bad emits bounced back for a re-emit (validate + emit_retries)
    tool_turns = 0                      # G: investigation turns, for the emit_after soft-convergence nudge
    emit_nudged = False
    read_seen: dict = {}                 # G2: (tool,args) -> FIRST capped result, for read-dedup + stuck parity
    turns = itertools.count() if max_turns is None or max_turns <= 0 else range(max_turns)
    for turn_idx in turns:
        if _cancelled():                # user hit stop -> finalize from what we have, promptly
            break
        if time_budget_s and (time.monotonic() - started) > time_budget_s:
            break                       # out of wall-clock budget -> finalize from what we have
        # Compaction happens IN PLACE (slice-assign, same list object): callers like the assistant's
        # `run_turn` keep a reference to this list to post-process the trace (stream the final answer
        # over it); a rebind would orphan their reference on a compacted turn and they'd re-answer
        # BLIND, missing every post-compaction tool result.
        # `context_budget_chars`: None = unset (fall back to the built-in default), 0 = compaction OFF
        # (the documented "0 = off" — the old `or DEFAULT` fallback silently turned 0 into the 120k
        # default, i.e. compaction ~8× MORE aggressive than the operator asked for), >0 = the budget.
        _budget = context_budget_chars
        if auto_summary and _budget is None:
            from looplab.core.context_budget import DEFAULT_SUMMARY_CHARS
            _budget = DEFAULT_SUMMARY_CHARS
        if _budget:
            _pre_compact_len = len(messages)
            if auto_summary:            # C2: summarize the stale middle once the history grows long
                from looplab.core.context_budget import compact_history
                messages[:] = compact_history(messages, _budget, summarize)
            else:                       # H4: else just middle-truncate stale tool output
                from looplab.core.context_budget import truncate_history
                messages[:] = truncate_history(messages, _budget)
            if len(messages) < _pre_compact_len and read_seen:
                # Compaction summarized away the very outputs the dedup stubs point at ("use the
                # earlier output") — the model could never recover them. Forget the cache so the
                # next re-read executes for real.
                read_seen.clear()
        # C1: re-surface the agent's own plan periodically so a long loop can't drift off-goal. A
        # `user`-role reminder, not `system`: the plan is verbatim MODEL output (from update_plan
        # args), so a `system` reinjection would let content the model was steered into by injected
        # tool output re-issue itself with system authority every few turns.
        if current_plan and plan_reinject_every and turn_idx and turn_idx % plan_reinject_every == 0:
            messages.append({"role": "user",
                             "content": "Reminder — your current plan/TODO (update it via update_plan "
                                        "as you make progress):\n" + current_plan})
        # NB: a transport failure (LLMError after the client's retries) PROPAGATES out of the loop by
        # design — the caller decides how to degrade. The assistant's `run_turn` surfaces it as an
        # error dict; the engine's agentic callers (ToolUsingResearcher.propose /
        # UnifiedAgent.choose_action / triage_crash) wrap this loop and fall back to a safe default,
        # the same way ToolUsingStrategist.decide does. BudgetExceeded likewise propagates (hard stop).
        msg = client.chat(messages, tool_specs, tool_choice="auto")
        calls = msg.get("tool_calls") or []
        if not calls:
            # Model replied in prose instead of calling a tool — it's done exploring. Force the
            # emit now so we always get a structured result; only if that's unsupported do we nudge
            # and retry (bounded, so an unlimited loop can't spin forever on a model that won't emit).
            messages.append({"role": "assistant", "content": msg.get("content") or ""})
            forced = _force_emit(client, messages, emit_spec)
            if forced is not None:
                return finalize(forced)
            stalls += 1
            if stalls >= 2:
                break
            messages.append({"role": "user",
                             "content": nudge_prompt or f"Now call `{emit_name}` with your final answer."})
            continue
        stalls = 0
        # Surface interstitial prose live — but NOT on the final turn where the model pairs prose with
        # the emit/final-answer call, since that same prose is regenerated as the streamed answer (it
        # would show twice). Only genuine between-tool-rounds prose reaches the UI here.
        if not any((c.get("function") or {}).get("name") == emit_name for c in calls):
            # Surface the model's between-tool "thinking out loud". Many models (minimax-m3 via
            # OpenRouter, SGLang) put it in the dedicated `reasoning`/`reasoning_content` field and leave
            # `content` empty on a tool-calling turn — without this fallback the chat showed only tool
            # steps and NO intermediate assistant prose. content wins when present (the real prose).
            from looplab.core.llm import _reasoning_of
            _text(msg.get("content") or _reasoning_of(msg))
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
        stuck_reason = None
        for tc in calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            raw = fn.get("arguments") or "{}"
            # A small/junk model can emit malformed JSON arguments; never let that crash the
            # run — treat an unparseable tool call as empty args (emit then falls back to a
            # safe result; a retrieval call just gets {}).
            try:
                args = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (json.JSONDecodeError, TypeError):
                args = {}
            if not isinstance(args, dict):
                # Valid-but-non-object JSON ("[0]", "\"x\"", "3") would otherwise reach finalize()/
                # tools.execute() and blow up on .get(); a junk model must never crash the run.
                args = {}
            stuck_obs = None      # what StuckDetector sees this turn (dedup hit overrides with the 1st result)
            if name == emit_name:
                # Bounce a malformed emit BACK to the model with the concrete error instead of silently
                # accepting a degraded/empty idea (the "fallback (agent parse failed)" no-op nodes that
                # tested nothing and polluted the experiment history). `validate(args) -> err|None`; on
                # an error we re-inject it and let the model re-emit, up to `emit_retries` times, then
                # accept whatever we have so the loop still always terminates.
                if validate is not None and emit_rejects < emit_retries:
                    err = None
                    try:
                        err = validate(args)
                    except Exception:  # noqa: BLE001 — a broken validator must never crash the loop
                        err = None
                    if err:
                        # The assistant turn (with this tool_call) is already in `messages`; just answer
                        # the emit call with the error and `continue` so any sibling calls this turn still
                        # get their tool results (no dangling tool_call_id) and the NEXT turn re-prompts.
                        emit_rejects += 1
                        messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                         "content": f"Your `{emit_name}` was NOT accepted: {err}. Fix it "
                                                    "and call it again with a valid, COMPLETE idea — "
                                                    "never an empty one."})
                        continue
                return finalize(args)
            if _cancelled():
                # Stop pressed while this turn's calls were executing: do NOT run the remaining
                # (possibly slow/mutating) tools. Stub the result so no tool_call_id dangles in the
                # trace; the top-of-turn check then ends the loop.
                result = "(cancelled by the user: tool not executed)"
            elif self_plan and name == _PLAN_TOOL_NAME:
                current_plan = _render_plan(args) or current_plan
                result = "plan updated"
            else:
                # G2: read-dedup anti-thrash. A model (esp. high-reasoning, on a big repo) re-issues the
                # SAME read/list/grep dozens of times — live rubert node 0: 92% of 434 reads were re-reads of
                # just 35 files, burning the whole turn budget re-ingesting identical content. Suppress an
                # EXACT repeat of a call from the _DEDUP_SAFE allowlist (stable file/kb/env readers ONLY —
                # a substring blocklist misclassified revert_file/git_checkout as read-only and froze
                # cursor-polls like read_output): return a short stub instead of re-executing + re-sending
                # the same ≤4KB. Loop-local, so a fresh propose/node reads normally. Volatile read-only
                # tools (polls, git state, run readers) are never deduped AND never invalidate; every
                # other tool (writes, unknown/MCP names) conservatively clears the cache.
                _dedupable = name in _DEDUP_SAFE
                _dk = (name, json.dumps(args, sort_keys=True, default=str)) if _dedupable else None
                # The observation the StuckDetector sees. On a dedup hit it's the FIRST (real) result, not
                # the stub — so a genuinely-stuck model repeating one call still trips stuck at the SAME
                # count (the stub is only what we send the model to save tokens; it must not offset B1).
                stuck_obs = None
                if _dk is not None and _dk in read_seen:
                    stuck_obs = read_seen[_dk]
                    _step(turn=turn_idx, tool=name, arg="(repeat suppressed)")
                    result = (f"(already ran `{name}` with these exact args earlier this turn — result "
                              f"unchanged, not re-sent. Use the earlier output; to learn more read a "
                              f"DIFFERENT file/range, else call `{emit_name}` with your best idea.)")
                    if on_tool_result is not None:
                        on_tool_result(name, args, result)
                else:
                    # Surface what the agent is about to do BEFORE the (possibly slow) tool runs, so a
                    # live progress view advances turn-by-turn instead of jumping only at the end.
                    _step(turn=turn_idx, tool=name,
                          arg=next((str(v) for v in (args or {}).values() if v), ""))
                    # First-class TOOL observation (Langfuse-style): input=args, output=result, nested
                    # under the active operation span next to the generations that decided the call.
                    with tracing.tool(name, args) as _tool_obs:
                        result = tools.execute(name, args) if tools is not None else f"(unknown tool: {name})"
                        _tool_obs.output(result)
                    if read_seen and not _dedupable and name not in _READ_ONLY_VOLATILE:
                        # A write/edit/revert/checkout/… (or an unknown/MCP tool — assume the worst) may
                        # have CHANGED files on disk (the Developer edits then re-reads its own code).
                        # Invalidate the read-dedup so a subsequent re-read returns the FRESH content,
                        # never a stale "already read" stub. Volatile READ-ONLY tools (git_status, polls)
                        # mutate nothing, so they neither dedup nor invalidate.
                        read_seen.clear()
                    # Cap once, up front, so the provenance hook receives EXACTLY what the tool message
                    # below will carry (a single expression, not two kept-in-sync copies).
                    result = str(result)[:4000]
                    if _dk is not None:               # remember the first result for dedup + stuck parity
                        read_seen[_dk] = result
                    if on_tool_result is not None:      # provenance hook: exceptions propagate
                        on_tool_result(name, args, result)
            result = str(result)[:4000]
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "name": name, "content": result})
            if stuck is not None:       # B1: flag no-progress on the cheapest signal (a repeat)
                stuck_reason = stuck.push(name, args, stuck_obs if stuck_obs is not None else result) or stuck_reason
        # G: soft convergence. A model that keeps issuing DIFFERENT tool calls never trips the
        # StuckDetector (it keys on repeats) and, with max_turns unlimited, investigates until the budget
        # runs out (live GLM node 63: one idea's worth of intent, then ~200 more reads). Nudge it to
        # nudge at `emit_after` tool turns; FORCE the emit at `emit_force` if it still hasn't committed.
        if (emit_after or emit_force) and tools is not None:
            tool_turns += 1
            if emit_force and tool_turns >= emit_force:
                forced = _force_emit(client, messages, emit_spec)
                if forced is not None:
                    return finalize(forced)
            elif emit_after and tool_turns == emit_after and not emit_nudged:
                emit_nudged = True
                messages.append({"role": "user",
                                 "content": f"You have investigated enough ({tool_turns} tool turns). STOP "
                                            f"exploring and call `{emit_name}` NOW with your best idea — "
                                            "you can refine on the next node."})
        if stuck_reason:
            # No progress — stop gracefully WITH a result instead of spinning forever. Nudge once,
            # then force the structured emit; if the client can't force it, fall through to fallback.
            messages.append({"role": "user",
                             "content": (stuck_prompt.replace("{reason}", str(stuck_reason)) if stuck_prompt
                                         else f"Stop: you appear to be stuck ({stuck_reason}). "
                                              f"Call `{emit_name}` now with your best answer.")})
            forced = _force_emit(client, messages, emit_spec)
            if forced is not None:
                return finalize(forced)
            break
    return fallback(messages)


def agentic_text(client, tools, messages, *, loop_opts=None, fallback=None,
                 answer_desc="your final answer") -> str:
    """`client.complete_text(messages)` upgraded to AGENTIC: the model MAY first call the provided
    read-only tools (run introspection, repo scouts, …) to GROUND its answer in the real experiments/
    code, then emits the text. Any single-shot text call becomes tool-using just by passing `tools`.
    Degrades to a plain completion when `tools` is falsy or the loop yields nothing — so callers keep
    their exact old behavior with no client/tools. Returns the emitted text (str)."""
    fb = fallback or (lambda m: str(client.complete_text(m) or ""))
    if not tools:
        return fb(messages)
    emit_spec = {"type": "function", "function": {
        "name": "answer", "description": f"Emit {answer_desc}. This ends your turn.",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string", "description": answer_desc}},
                       "required": ["text"]}}}
    try:
        return drive_tool_loop(client, tools, messages, emit_spec,
                               finalize=lambda a: str((a or {}).get("text", "") or ""),
                               fallback=fb, **(loop_opts or {}))
    except Exception:  # noqa: BLE001 — an agentic-path failure must never break a best-effort step
        return fb(messages)


def agentic_struct(client, tools, messages, model_cls, *, parser="tool_call",
                   loop_opts=None, fallback=None):
    """`parse_structured(client, messages, model_cls, parser)` upgraded to AGENTIC: the model MAY first
    call the provided read-only tools to GROUND its structured emit in the real experiments/code, then
    emits the object. Returns a validated `model_cls` instance. Degrades to plain `parse_structured` when
    `tools` are absent or the loop yields nothing invalid — so callers keep their exact old behavior."""
    from looplab.core.parse import parse_structured
    fb = fallback or (lambda m: parse_structured(client, m, model_cls, parser))
    if not tools:
        return fb(messages)
    emit_spec = {"type": "function", "function": {
        "name": "emit", "description": "Emit the final structured result. This ends your turn.",
        "parameters": model_cls.model_json_schema()}}

    def _final(args):
        try:
            return model_cls.model_validate(args or {})
        except Exception:  # noqa: BLE001 — a malformed emit falls back to the plain structured path
            return fb(messages)
    try:
        return drive_tool_loop(client, tools, messages, emit_spec, finalize=_final, fallback=fb,
                               **(loop_opts or {}))
    except Exception:  # noqa: BLE001 — the agentic path must never break a best-effort step
        return fb(messages)


def _summarizer(client):
    """Build a `summarize(text) -> str` callable from an LLM client for `compact_history` (C2).
    Best-effort: any failure makes the caller fall back to deterministic truncation."""
    msgs_for = lambda text: [
        {"role": "system",
         "content": "Summarize the earlier agent steps below into a few tight bullet points: "
                    "what was tried, what was learned, and any decisions. Keep only what future "
                    "turns need."},
        {"role": "user", "content": text}]

    def _summarize(text: str) -> str:
        # Prefer the tool-free text completion: a `chat(..., tools=[], tool_choice="none")` is
        # rejected by some OpenAI-compatible backends (vLLM/older Ollama) when tools is empty.
        complete_text = getattr(client, "complete_text", None)
        if callable(complete_text):
            return str(complete_text(msgs_for(text)) or "").strip()
        msg = client.chat(msgs_for(text), [], tool_choice="none")
        return str((msg or {}).get("content") or "").strip()
    return _summarize


def loop_opts_from_settings(settings) -> dict:
    """Collect the config-driven tool-loop options (B1 stuck detection + C1 self-plan + C2
    auto-summary) into a dict to spread into `drive_tool_loop`. Plain scalars only — safe to reuse
    across calls (the loop builds a FRESH StuckDetector per invocation from these thresholds) —
    plus the optional D11 compression client (stateless, reusable)."""
    g = getattr
    opts = {
        "stuck_detection": bool(g(settings, "agent_stuck_detection", True)),
        "stuck_repeat": int(g(settings, "agent_stuck_repeat", 4)),
        "stuck_alternate": int(g(settings, "agent_stuck_alternate", 4)),
        "self_plan": bool(g(settings, "agent_self_plan", True)),
        "plan_reinject_every": int(g(settings, "agent_plan_reinject_every", 5)),
        "auto_summary": bool(g(settings, "agent_auto_summary", True)),
        "emit_after": int(g(settings, "agent_emit_after", 300)),  # G: nudge to emit after N tool turns
        "emit_force": int(g(settings, "agent_emit_force", 500)),  # G: force the emit at this many turns
    }
    # C2/H4: the configured context budget must reach EVERY loop, not just the Researcher — the
    # 120k built-in fallback otherwise survives in the Developer's 500-turn implement session (the
    # exact loop the budget raise targeted). Only set when configured, so a bare stub settings
    # object keeps the loop's own unset (None -> built-in default) semantics; an explicit 0 = off.
    cb = g(settings, "context_budget_chars", None)
    if cb is not None:
        opts["context_budget_chars"] = int(cb)
    # D11 compression model slot (open_deep_research's four-slot pattern): a dedicated CHEAP
    # summarizer for history compression, instead of paying the main model for it. Blank = the
    # loop's own client (byte-identical legacy behavior).
    if g(settings, "compressor_model", None):
        from looplab.adapters.tasks import make_llm_client
        try:
            opts["summary_client"] = make_llm_client(
                settings, model=settings.compressor_model,
                base_url=g(settings, "compressor_base_url", None) or None)
        except Exception:  # noqa: BLE001 — a bad compressor config degrades to the main client
            pass
    return opts


class ToolUsingResearcher:
    """Agentic Researcher (same `propose` Protocol as roles.LLMResearcher — see this module's
    docstring): drives a bounded multi-turn tool loop (`drive_tool_loop`, whose docs cover the
    turn/time/context budgets and history compression) in which the model may consult the run
    via retrieval tools before calling `emit` exactly once with its final Idea. Resilient by
    contract: malformed emits are sanitized, and parse/transport failures degrade to a safe
    bounds-filled Idea instead of crashing the run."""

    _SYSTEM = ("You are an ML researcher driving experiments to improve the objective. Investigate "
               "PROPERLY, then call `emit` exactly once with your final Idea — that ends your turn.\n"
               "Work FOCUSED, not scattered: pick the most promising direction/hypothesis from the state "
               "brief and RESEARCH THAT — read the relevant code and prior experiments fully enough to "
               "propose a correct, grounded experiment (a half-baked idea from shallow reading wastes a "
               "whole node). But read EFFICIENTLY: read_file paginates (start_line/lines) — read a file "
               "ONCE, end to end if needed, and do NOT re-read a file/grep you already ran; if a read "
               "returned content, you HAVE it. When you understand the change you want and can name its "
               "params, STOP and emit (operator, params, rationale, and a short reusable `theme` slug "
               "grouping related experiments, e.g. \"loss-fn\"); you refine on the NEXT node.\n"
               + _IDEA_SPACE_TOOL)

    def __init__(self, client, tools, space_hint: str = "",
                 bounds: Optional[dict] = None, parser: str = "tool_call",
                 max_turns: int = 0, prompts: Optional[PromptStore] = None,
                 context_budget_chars: int | None = None, time_budget_s: float = 0.0,
                 loop_opts: Optional[dict] = None):
        self.client = client
        self.tools = tools          # object with .specs() and .execute(name, args)
        self.space_hint = space_hint
        self.bounds = bounds
        self.parser = parser
        self.max_turns = max_turns          # 0 = unlimited (config-driven via Settings.agent_max_turns)
        self.time_budget_s = time_budget_s  # 0 = no wall-clock cap (Settings.agent_time_budget_s)
        self.prompts = prompts
        self.context_budget_chars = context_budget_chars   # H4: cap the growing tool-call history
        self.loop_opts = loop_opts or {}    # B1/C1/C2 tool-loop options (loop_opts_from_settings)

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
             "content": render(self.prompts, "tool_researcher_system", self._SYSTEM)
                        + self.space_hint + hyp},
            {"role": "user", "content": _state_brief(state, parent,
                                                     digest_cap=getattr(self, "_digest_cap", 0),
                                                     hyp_order=getattr(self, "_hyp_order", None))
                + hint_block + cue +
                "\nDecide the next experiment — a parameter change OR a structural one (architecture, "
                "loss, data, training) if that's the stronger move. Consult knowledge if useful, then emit."},
        ]
        try:
            return drive_tool_loop(
                self.client, self.tools, messages, self._emit_spec(),
                max_turns=self.max_turns, context_budget_chars=self.context_budget_chars,
                time_budget_s=self.time_budget_s,
                finalize=self._finalize, fallback=self._fallback,
                validate=self._validate_emit, **self.loop_opts)
        except BudgetExceeded:      # hard budget stop -> propagate and end the run
            raise
        except Exception:  # noqa: BLE001 - a transport/endpoint failure (LLMError after retries) on
            # the flagship agentic path must NOT crash the run: degrade to a safe bounds-filled Idea,
            # the same contract as LLMResearcher / ToolUsingStrategist. `_fallback` is itself resilient
            # (parse_structured swallows LLMError -> draft Idea), so it can't re-raise the transport error.
            return self._fallback(messages)
