"""Reusable multi-turn tool-loop machinery (split out of `agents.agent`): `drive_tool_loop` — the
bounded agent loop shared by every tool-using persona — its agentic text/struct upgrades
(`agentic_text` / `agentic_struct`), the phase-handoff ledger (`handoff_scope` /
`summarize_phase`), and the `CompositeTools` toolset merger. `agents.agent` re-imports every name
under its original name, so the documented patch seams (`looplab.agents.agent.drive_tool_loop` /
`.agentic_struct` — novelty.py names them — and the flat `looplab.agent.…`) keep resolving to the
SAME objects.

`run_phase` deliberately STAYS in `agents.agent`: tests monkeypatch `agent.drive_tool_loop` and
rely on run_phase's internal `drive_tool_loop(...)` call resolving through THAT module's (patched)
global — which only holds while run_phase's module globals are agent's, not this module's.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import itertools
import json
import re
import time
from typing import Optional

from looplab.core import tracing
from looplab.core.llm import BudgetExceeded
from looplab.tools._base import RESULT_CAP
from looplab.trust.redact import redact_secrets


class CompositeTools:
    """Merge several tool providers (each with .specs()/.execute()) into one toolset,
    so the Researcher can use knowledge + skills + memory tools together."""

    def __init__(self, providers: list):
        self.providers = providers
        self._route: dict[str, object] = {}
        # De-dup by function name (FIRST provider wins): two providers registering the same tool name
        # otherwise (a) sent DUPLICATE specs to the endpoint — some OpenAI-compatible backends 400 on
        # that — and (b) routed execute() last-wins, silently shadowing the first provider. Dedup makes
        # the toolset well-formed and the shadowing deterministic (and surfaceable).
        self._specs: list[dict] = []
        for p in providers:
            for spec in p.specs():
                fname = (spec.get("function") or {}).get("name")
                if not fname or fname in self._route:
                    continue
                self._route[fname] = p
                self._specs.append(spec)

    def specs(self) -> list[dict]:
        return list(self._specs)

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

# The hard per-result size bound every tool observation passes through before it reaches the
# model — the SHARED `tools._base.RESULT_CAP`, so the loop cap and every provider's own page/tail
# budget move together. When it actually truncates, an EXPLICIT marker replaces the tail (P3,
# docs/PROMPT_REVIEW.md): the silent head-cut destroyed every paginating tool's resume pointer and
# left the model acting on code it never saw. `{n}` = exact number of characters cut.
_TRUNC_NOTE = ("\n…[truncated by the tool-result cap — {n} chars omitted; "
               "re-request a narrower range]")

# Appended to the 3rd+ consecutive IDENTICAL-RESULT repeat of an exact (tool, canonical-args) call
# within ONE loop invocation. The G2 read-dedup removal (P3 — see the always-execute comment in
# drive_tool_loop) left a B1 gap: a 3+-call read ROUND-ROBIN (A B C A B C …) never trips the
# StuckDetector, which only catches 1- and 2-cycles. No caching, no suppression — the repeated call
# still fully executes and returns fresh, complete content (the operator's always-re-read decision
# stands); we only TELL the model it is repeating itself so it can stop on its own. Keyed on the
# RESULT too, not just the call: a cursor tool (read_output) legitimately repeats the same args and
# returns NEW output each poll — a call-count-only note ('the result is identical unless a write
# changed it') was FALSE there and contradicted the tool's own '(more output pending)' marker. Now
# the note fires only when the new result is byte-identical to the previous one for that call, so
# it is always true. `{k}` = length of the identical-result streak.
_REPEAT_NOTE = ("\n(note: this exact call has now run {k}× this phase with an IDENTICAL result)")


def _cap_tool_result(result: str, cap: int = RESULT_CAP) -> str:
    """Bound a tool result to `cap` chars, appending `_TRUNC_NOTE` (inside the cap) when it actually
    truncates — so the model KNOWS the reply is partial and can re-request a narrower range instead
    of trusting a silently amputated page. Idempotent: an already-capped string passes through, so
    the loop can apply it as a final belt-and-braces bound too. The tiny fixed-point loop settles the
    marker's own length (the omitted-count digits shift the split by a char or two)."""
    if len(result) <= cap:
        return result
    keep = cap
    while True:
        note = _TRUNC_NOTE.format(n=len(result) - keep)
        new_keep = max(0, cap - len(note))
        if new_keep == keep:
            return result[:keep] + note
        keep = new_keep


def _trace_preview(value, cap: int = RESULT_CAP) -> str:
    """Bound/redact a trace observation before durable serialization, retaining size + digest."""
    secret = re.compile(r"(?:api[_-]?key|authorization|password|secret|token)", re.IGNORECASE)

    def _redact(obj, depth=0):
        if depth > 5:
            return "<depth-limited>"
        if isinstance(obj, dict):
            # Do not materialize an untrusted mapping merely to retain its first
            # fields. A diagnostic preview must not allocate a second full-sized tool result.
            return {str(k): ("<redacted>" if secret.search(str(k)) else _redact(v, depth + 1))
                    for k, v in itertools.islice(obj.items(), 128)}
        if isinstance(obj, (list, tuple)):
            return [_redact(v, depth + 1) for v in itertools.islice(obj, 128)]
        return obj

    try:
        rendered = (json.dumps(_redact(value), ensure_ascii=False, sort_keys=True, default=str,
                               separators=(",", ":")) if isinstance(value, (dict, list, tuple))
                    else str(value))
    except Exception:  # noqa: BLE001 — tracing must never perturb tool execution
        rendered = "<trace preview unavailable>"
    # Values can contain credentials even when their enclosing key is harmless (for example a
    # command's plain-text stdout containing ``Authorization: Bearer ...``).  Apply the canonical
    # redactor to the fully rendered observation before either hashing or persisting it.  Trace
    # previews always enable the conservative entropy pass because they are durable diagnostics,
    # not byte-exact evaluator output.
    rendered = redact_secrets(rendered, entropy=True)
    cap = max(0, int(cap))
    if len(rendered) <= cap:
        return rendered[:cap]
    digest = hashlib.sha256(rendered.encode("utf-8", errors="replace")).hexdigest()
    marker = f"\n…[trace preview: original_chars={len(rendered)} sha256={digest}]"
    if len(marker) >= cap:
        return marker[-cap:] if cap else ""
    return rendered[:cap - len(marker)] + marker


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
        string (exactly what the tool message will carry, truncation marker and any repeat note
        included) — so a caller
        can record provenance
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
    # STATELESS per-loop repeat ledger (see _REPEAT_NOTE): for each exact (tool, canonical-args)
    # call, the previous CAPPED result and the length of the current identical-result streak — for
    # THIS invocation only, a fresh dict per call, like the StuckDetector, so nothing leaks across
    # loops or phases. `_canonical` is the detector's own args canonicalizer, reused so the two
    # repeat notions can't drift. The full previous result string is kept (not a hash): it is
    # already capped at RESULT_CAP, and byte-identity must be exact — no collision caveat.
    from looplab.agents.stuck import _canonical
    repeat_state: dict[str, tuple[str, int]] = {}
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
    exhausted = False                    # ran out of turns/time (vs stalled/stuck/cancelled)

    def _accept_forced(forced):
        """Validate a FORCED emit the same way an in-loop emit is validated, then finalize it. Returns
        (True, result) when acceptable, else (False, None) so the caller can fall through to a nudge /
        fallback instead of accepting an empty/malformed emit — the very no-op node the `validate`
        bounce exists to prevent, which the forced-emit paths otherwise re-created."""
        if forced is None:
            return False, None
        if validate is not None:
            try:
                if validate(forced):      # non-None err string == rejected
                    return False, None
            except Exception:  # noqa: BLE001 — a broken validator must not crash the loop
                pass
        return True, finalize(forced)

    turns = itertools.count() if max_turns is None or max_turns <= 0 else range(max_turns)
    for turn_idx in turns:
        if _cancelled():                # user hit stop -> finalize from what we have, promptly
            break
        if time_budget_s and (time.monotonic() - started) > time_budget_s:
            exhausted = True
            break                       # out of wall-clock budget -> salvage an emit below
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
            if auto_summary:            # C2: summarize the stale middle once the history grows long
                from looplab.core.context_budget import compact_history
                messages[:] = compact_history(messages, _budget, summarize)
            else:                       # H4: else just middle-truncate stale tool output
                from looplab.core.context_budget import truncate_history
                messages[:] = truncate_history(messages, _budget)
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
            ok, result = _accept_forced(_force_emit(client, messages, emit_spec))
            if ok:
                return result
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
            repeat_note = ""            # per-call: set only when an executed call is a 3rd+ repeat
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
                # Every tool call ALWAYS executes and returns fresh content. The G2 read-dedup cache
                # that used to stub an exact repeat ("already ran … use the earlier output") was
                # REMOVED by explicit operator decision (P3, docs/PROMPT_REVIEW.md): the stub pointed
                # at content the model could no longer see after compaction/phase handoff, and the
                # cached copy silently went stale — always read what is asked. The StuckDetector
                # below is the loop-safety net now: a model that thrashes on the SAME call with the
                # SAME result trips B1 and the loop force-emits instead of spinning; the repeat
                # note below covers the 3+-call round-robins B1's 1-/2-cycle window can't see.
                # Surface what the agent is about to do BEFORE the (possibly slow) tool runs, so a
                # live progress view advances turn-by-turn instead of jumping only at the end.
                _step(turn=turn_idx, tool=name,
                      arg=next((str(v) for v in (args or {}).values() if v), ""))
                # First-class TOOL observation (Langfuse-style): input=args, output=result, nested
                # under the active operation span next to the generations that decided the call.
                with tracing.tool(name, _trace_preview(args)) as _tool_obs:
                    result = tools.execute(name, args) if tools is not None else f"(unknown tool: {name})"
                    _tool_obs.output(_trace_preview(result))
                # Cap once, up front — appending an explicit truncation marker when the cap actually
                # bites (P3) — so the provenance hook receives EXACTLY what the tool message below
                # will carry (a single expression, not two kept-in-sync copies).
                result = _cap_tool_result(str(result))
                # Tag the 3rd+ IDENTICAL-RESULT repeat of this (tool, canonical-args) call (see
                # _REPEAT_NOTE: the round-robin gap the StuckDetector's 1-/2-cycle window can't
                # cover; a changed result — a cursor poll's new chunk, a post-write re-read —
                # resets the streak and never gets the note). The note rides OUTSIDE the cap so it
                # can never be truncated away.
                sig = f"{name}({_canonical(args)})"
                prev, streak = repeat_state.get(sig, (None, 0))
                streak = streak + 1 if result == prev else 1
                repeat_state[sig] = (result, streak)
                if streak >= 3:
                    repeat_note = _REPEAT_NOTE.format(k=streak)
                if on_tool_result is not None:      # provenance hook: exceptions propagate
                    on_tool_result(name, args, result + repeat_note)
            result = _cap_tool_result(str(result))   # idempotent final bound (cancel/plan stubs too)
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "name": name, "content": result + repeat_note})
            if stuck is not None:       # B1: flag no-progress on the cheapest signal (a repeat).
                # Push the UN-noted result: the note's incrementing count would otherwise make every
                # repeat look like a NEW observation and blind the identical-pair check.
                stuck_reason = stuck.push(name, args, result) or stuck_reason
        # G: soft convergence. A model that keeps issuing DIFFERENT tool calls never trips the
        # StuckDetector (it keys on repeats) and, with max_turns unlimited, investigates until the budget
        # runs out (live GLM node 63: one idea's worth of intent, then ~200 more reads). Nudge it to
        # nudge at `emit_after` tool turns; FORCE the emit at `emit_force` if it still hasn't committed.
        # The nudge wording is ROLE-NEUTRAL: this loop also drives the strategist/pilot/triage emits
        # (via loop_opts_from_settings), where "your best idea / next node" would be nonsense.
        if (emit_after or emit_force) and tools is not None:
            tool_turns += 1
            if emit_force and tool_turns >= emit_force:
                ok, result = _accept_forced(_force_emit(client, messages, emit_spec))
                if ok:
                    return result
                break   # force unsupported/rejected: fall to fallback, don't re-attempt every turn
            elif emit_after and tool_turns == emit_after and not emit_nudged:
                emit_nudged = True
                messages.append({"role": "user",
                                 "content": f"You have investigated enough ({tool_turns} tool turns). STOP "
                                            f"exploring and call `{emit_name}` NOW with your best final "
                                            "output."})
        if stuck_reason:
            # No progress — stop gracefully WITH a result instead of spinning forever. Nudge once,
            # then force the structured emit; if the client can't force it, fall through to fallback.
            messages.append({"role": "user",
                             "content": (stuck_prompt.replace("{reason}", str(stuck_reason)) if stuck_prompt
                                         else f"Stop: you appear to be stuck ({stuck_reason}). "
                                              f"Call `{emit_name}` now with your best answer.")})
            ok, result = _accept_forced(_force_emit(client, messages, emit_spec))
            if ok:
                return result
            break
    else:
        exhausted = True                # every turn used without an emit
    if exhausted and not _cancelled():
        # Budget exhaustion (turns or wall-clock) used to fall STRAIGHT to fallback, discarding the
        # whole investigation — the Developer's STAGES phase read a big repo for its full 30-turn
        # budget, never got to `declare_stages`, and silently degraded to "no stages declared".
        # Salvage ONE forced structured emit from everything gathered; only then fall back.
        messages.append({"role": "user",
                         "content": f"Out of turn/time budget. Call `{emit_name}` NOW with your "
                                    "best answer from everything you have gathered."})
        ok, result = _accept_forced(_force_emit(client, messages, emit_spec))
        if ok:
            return result
    return fallback(messages)


# SEAM NOTE: agentic_text/agentic_struct call `drive_tool_loop` through THIS module's globals.
# Pre-split (one module) a patch on `looplab.agents.agent.drive_tool_loop` intercepted them; now
# it does not — patch `looplab.agents.tool_loop.drive_tool_loop` to intercept these two. Every
# existing test patches seams that still resolve (run_phase stayed in agent.py for exactly this
# reason — see its why-comment).
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
    except BudgetExceeded:  # a HARD budget stop must propagate — degrading to fb() runs ANOTHER LLM
        raise                # call after the budget tripped (every sibling loop caller re-raises first)
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
    except BudgetExceeded:  # a HARD budget stop must propagate, not degrade to another LLM call
        raise
    except Exception:  # noqa: BLE001 — the agentic path must never break a best-effort step
        return fb(messages)


def _summarizer(client):
    """Build a `summarize(text) -> str` callable from an LLM client for `compact_history` (C2).
    Best-effort: any failure makes the caller fall back to deterministic truncation."""
    def msgs_for(text):
        return [
            {"role": "system",
             "content": "Summarize the earlier agent steps below into a few tight bullet points: "
                        "what was tried, what was learned, and any decisions. Keep only what future "
                        "turns need."},
            {"role": "user", "content": text},
        ]

    def _summarize(text: str) -> str:
        # Prefer the tool-free text completion: a `chat(..., tools=[], tool_choice="none")` is
        # rejected by some OpenAI-compatible backends (vLLM/older Ollama) when tools is empty.
        complete_text = getattr(client, "complete_text", None)
        if callable(complete_text):
            return str(complete_text(msgs_for(text)) or "").strip()
        msg = client.chat(msgs_for(text), [], tool_choice="none")
        return str((msg or {}).get("content") or "").strip()
    return _summarize


def _flatten_transcript(messages) -> str:
    """Render a tool-loop's messages into a plain-text transcript for summarization: role-tagged
    lines, tool calls named, tool results labeled. Drops the (huge, non-carryable) system prompt and
    caps the total so one over-long phase can't blow the summary call's context."""
    parts = []
    for m in messages or []:
        role = m.get("role")
        if role == "system":
            continue                       # the phase's own instructions aren't context to hand off
        content = str(m.get("content") or "")
        tcs = m.get("tool_calls") or []
        if tcs:
            names = ", ".join((tc.get("function") or {}).get("name", "") for tc in tcs)
            content = (content + f" [tool calls: {names}]").strip()
        if role == "tool":
            content = f"[tool result] {content}"
        if content:
            parts.append(f"{role}: {content}")
    return "\n".join(parts)[:60_000]


# Node-scoped PHASE-HANDOFF ledger (contextvar, like tracing._node_ctx). The engine opens a
# `handoff_scope()` around each node build; every LLM phase that runs through `run_phase` inside it
# reads the accumulated briefs (so it trusts what earlier phases — even a different ROLE — already
# explored) and appends its own. None = no active scope → run_phase is a plain drive_tool_loop
# (unit tests, aux single-shot loops outside a node build). Isolated per node: each build gets a
# fresh list, and a parallel build runs in its own contextvars context.
_handoff_ctx: contextvars.ContextVar = contextvars.ContextVar("LOOPLAB_handoff", default=None)
# NOTE: the node-scoped READ CACHE that used to accompany this ledger (a (tool,args)->result map
# shared across a node's phases) was removed with the loop's read-dedup (P3, docs/PROMPT_REVIEW.md):
# every read now executes for real, in every phase — the brief above only *discourages* re-reading.


@contextlib.contextmanager
def handoff_scope(enabled: bool = True):
    """Open the per-node phase-coordination scope: a handoff ledger (briefs flow phase→phase).
    `enabled=False` is a no-op (the master switch, `Settings.phase_handoff_summary`), so run_phase /
    drive_tool_loop behave exactly as before — no briefs."""
    if not enabled:
        yield
        return
    tok = _handoff_ctx.set([])
    try:
        yield
    finally:
        _handoff_ctx.reset(tok)


def summarize_phase(client, messages, *, phase: str, next_phase: str, min_chars: int = 2_000) -> str:
    """ONE LLM call that distills a COMPLETED phase's transcript into a handoff brief for the NEXT
    phase — so the next phase trusts what was already explored instead of re-reading the same repo /
    data (the tool-call explosion this cuts). Best-effort: returns '' on any client error, and skips
    the call entirely when there's too little to distill (a phase that barely read anything). The
    caller injects the returned brief into the next phase's prompt."""
    try:
        blob = _flatten_transcript(messages)
        if len(blob) < min_chars:          # nothing meaningful explored — a summary call would be waste
            return ""
        sys = (f"You are handing off from the '{phase}' phase to the '{next_phase}' phase of a coding "
               "agent working on a repo. Distill the transcript below into a TIGHT brief the next phase "
               "needs so it does NOT have to re-read what this phase already explored. Cover: the repo "
               "structure + KEY files and their roles, the entry point / eval flow, data & model paths "
               "CONFIRMED to exist, library APIs/versions already checked, and the concrete DECISIONS "
               "made. Bullet points, facts only — omit anything the next phase can't act on.")
        msgs = [{"role": "system", "content": sys}, {"role": "user", "content": blob}]
        ct = getattr(client, "complete_text", None)
        if callable(ct):
            return str(ct(msgs) or "").strip()
        return str((client.chat(msgs, [], tool_choice="none") or {}).get("content") or "").strip()
    except BudgetExceeded:  # a hard budget stop must propagate — never masked by the optional summary
        raise
    except Exception:  # noqa: BLE001 — otherwise a handoff summary is best-effort; never crash the phase
        return ""


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
        from looplab.core.llm import make_llm_client
        try:
            opts["summary_client"] = make_llm_client(
                settings, model=settings.compressor_model,
                base_url=g(settings, "compressor_base_url", None) or None)
        except Exception:  # noqa: BLE001 — a bad compressor config degrades to the main client
            pass
    return opts
