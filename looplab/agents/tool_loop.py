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
import inspect
import json
import logging
import re
import time
from typing import Optional

from looplab.core import tracing
from looplab.core.llm import BudgetExceeded
from looplab.tools._base import (RESULT_CAP, ToolCapability, ToolResult, collect_inventory,
                                 capability_manifest)
from looplab.core.redact import redact_secrets
# The typed options bundle (doc 25 AG-01). Re-exported here — and, through `agents/agent.py`, under
# every historical spelling — because `loop_opts_from_settings` lives in THIS module and now returns
# one: a caller that imports the factory must be able to name its type from the same place.
from looplab.agents.loop_options import (  # noqa: F401
    EXPLICIT_ONLY_LOOP_ARGS, LOOP_OPTION_FIELDS, UNSET, LoopOptions)
# The StuckDetector's own args canonicalizer, reused by the repeat ledger below so the two repeat
# notions can't drift. (`StuckDetector` itself stays a function-local import in `drive_tool_loop`:
# a FRESH detector is built per call, so nothing about it is module state.)
from looplab.agents.stuck import _canonical


# A configured compressor that cannot be constructed must not silently fall back to the main
# (potentially paid) client. The sentinel lets ``None`` retain its documented legacy meaning
# (no compressor configured -> use the loop client) while selecting deterministic truncation for
# an explicitly configured-but-unavailable compressor.
_SUMMARY_LOCAL_ONLY = object()
_LOG = logging.getLogger(__name__)


class CompositeTools:
    """Merge several tool providers (each with .specs()/.execute()) into one toolset,
    so the Researcher can use knowledge + skills + memory tools together."""

    def __init__(self, providers: list, *, strict_collisions: bool = False,
                 hide_empty_tools: bool = False):
        self.providers = providers
        # Withhold the SPEC of a tool whose provider reports a decisive zero (see `specs`).
        # An UNKNOWN never hides anything: "I could not count it" is not "it is empty",
        # and hiding on it would remove a tool that had something to return.
        self.hide_empty_tools = bool(hide_empty_tools)
        self._route: dict[str, object] = {}
        self._capabilities: dict[str, ToolCapability] = {}
        # De-dup by function name (FIRST provider wins): two providers registering the same tool name
        # otherwise (a) sent DUPLICATE specs to the endpoint — some OpenAI-compatible backends 400 on
        # that — and (b) routed execute() last-wins, silently shadowing the first provider. Dedup makes
        # the toolset well-formed and the shadowing deterministic (and surfaceable).
        self._specs: list[dict] = []
        self.collisions: list[tuple[str, str, str]] = []
        for p in providers:
            declared: dict[str, ToolCapability] = {}
            capability_fn = getattr(p, "capabilities", None)
            if callable(capability_fn):
                try:
                    raw = capability_fn() or ()
                    rows = raw.values() if isinstance(raw, dict) else raw
                    declared = {c.name: c for c in rows if isinstance(c, ToolCapability)}
                except Exception as exc:  # noqa: BLE001 - metadata must not disable a legacy tool
                    _LOG.warning("ignoring invalid capability metadata from %s: %s",
                                 type(p).__name__, exc)
            # `all_specs()` when the provider has one, i.e. when it is itself a CompositeTools.
            # `specs()` on a hide-enabled nested composite is already FILTERED, and routing off a
            # filtered list bakes the filter into THIS object's `_route` -- which would make a
            # withheld tool genuinely undispatchable and break the "the offer is withheld, never the
            # route" invariant `specs()` documents. Measured before this fix: an outer composite
            # over a hide-enabled pilot answered `(unknown tool: read_asset)` instead of running it.
            for spec in (p.all_specs() if callable(getattr(p, "all_specs", None)) else p.specs()):
                fname = (spec.get("function") or {}).get("name")
                if not fname:
                    continue
                if fname in self._route:
                    first = type(self._route[fname]).__name__
                    shadowed = type(p).__name__
                    collision = (fname, first, shadowed)
                    self.collisions.append(collision)
                    message = (
                        f"duplicate tool name {fname!r}: keeping first provider {first}, "
                        f"shadowing {shadowed}"
                    )
                    if strict_collisions:
                        raise ValueError(message)
                    _LOG.warning(message)
                    continue
                self._route[fname] = p
                self._specs.append(spec)
                # Never infer authority from a friendly-looking tool name. A provider that has not
                # opted into the typed contract is explicitly UNKNOWN and policy code can fail closed.
                cap = declared.get(fname)
                if cap is None:
                    fn = spec.get("function") or {}
                    cap = ToolCapability.unknown(
                        fname, input_schema=fn.get("parameters"),
                        source=f"legacy:{type(p).__name__}")
                self._capabilities[fname] = cap
        self._manifest, self.manifest_hash = capability_manifest(
            self._specs, self._capabilities.values())

    def all_specs(self) -> list[dict]:
        """Every spec this composite ROUTES, unfiltered — what a wrapping composite must build from.

        `specs()` is the OFFER and may withhold; `_route` is the reach and never does. A caller that
        composes this object into a bigger one needs the reach, or the outer object inherits an
        offer-time filter as a routing decision.
        """
        return list(self._specs)

    def specs(self) -> list[dict]:
        """The tools to OFFER this turn.

        Filtered live, never at construction, and only ever the OFFER: `_route` keeps every provider
        it ever routed, so a tool withheld here still DISPATCHES if the model calls it from history
        or from a spec it saw a turn ago. Nothing becomes unreachable — a tool only stops being
        advertised while it provably has nothing to say.

        `hide_empty_tools` is off by default and the reason is the caching: this object is built
        ONCE per role, so a filter applied in `__init__` would hide a tool for the whole run on the
        strength of what was true at construction — `list_experiments` would vanish at node 0 and
        never come back. Re-asking here makes the offer track the PHASE.

        NOT the turn, and the difference matters: `drive_tool_loop` computes `tool_specs` once per
        invocation (see its `_compose_loop_tool_specs` call) and reuses that list for every turn, so
        a tool that gains content mid-phase stays withheld until the next phase. An earlier version
        of this docstring and of the `hide_empty_tools` setting claimed "re-evaluated every turn",
        which the code never did. Recomputing per turn would put the whole provider sweep on every
        turn of every phase; the phase boundary is where it is affordable, and the flag is off by
        default partly for this reason.
        """
        if not self.hide_empty_tools:
            return list(self._specs)
        empty = {name for name, value in self.inventory().items()
                 if isinstance(value, int) and value == 0}
        if not empty:
            return list(self._specs)
        return [spec for spec in self._specs
                if (spec.get("function") or {}).get("name") not in empty]

    def inventory(self) -> dict[str, int | str]:
        """Merge the providers' optional `inventory()` receipts (`tools/_base.INVENTORY_CONTRACT`).

        Filtered through `self._route`, which is what makes the merge correct rather than merely
        convenient: a name registered by two providers is DISPATCHED to the first and the second is
        shadowed, so publishing the shadowed provider's count would state a number no call can
        return. Same first-wins rule, one source.

        A name this composite does not route at all is dropped for the same reason -- a provider
        may know about a tool it is not currently offering (`CrossRunTools` answers for all eight of
        its tools whether or not `specs()` published them), and a count for a tool the model cannot
        call is noise in a block whose entire value is that every row names a real tool.
        """
        merged: dict[str, int | str] = {}
        for provider in self.providers:
            for name, value in collect_inventory(provider).items():
                if self._route.get(name) is provider and name not in merged:
                    merged[name] = value
        return merged

    def execute(self, name: str, args: dict) -> str:
        p = self._route.get(name)
        return p.execute(name, args) if p else f"(unknown tool: {name})"

    def execute_result(self, name: str, args: dict, *, cancel_check=None) -> ToolResult:
        """Typed dispatch, additive to the historical string-returning ``execute`` contract.

        Providers may implement ``execute_result(name, args, cancel_check=...)`` to retain
        structured output and cooperative cancellation. Legacy providers are wrapped without
        changing their result bytes. Unknown calls are first-class errors instead of successful
        strings for typed consumers, while ``execute`` keeps its original behaviour.
        """
        p = self._route.get(name)
        if p is None:
            return ToolResult(content=f"(unknown tool: {name})", is_error=True,
                              retryable=False, provenance={"source": "composite"})
        typed = getattr(p, "execute_result", None)
        if callable(typed):
            try:
                signature = inspect.signature(typed)
                accepts_cancel = ("cancel_check" in signature.parameters or any(
                    param.kind == inspect.Parameter.VAR_KEYWORD
                    for param in signature.parameters.values()))
            except (TypeError, ValueError):
                accepts_cancel = False
            return ToolResult.coerce(
                typed(name, args, cancel_check=cancel_check)
                if accepts_cancel else typed(name, args))
        return ToolResult.coerce(p.execute(name, args))

    def capabilities(self) -> list[ToolCapability]:
        return [self._capabilities[name] for name in self._route]

    def capability(self, name: str) -> ToolCapability:
        return self._capabilities.get(name) or ToolCapability.unknown(
            name or "<unknown>", source="composite:unknown")

    def manifest(self) -> dict:
        # The manifest is JSON-shaped; a serialization round-trip prevents callers from mutating
        # the canonical object whose digest is stamped onto subsequent tool observations.
        return json.loads(json.dumps(self._manifest, ensure_ascii=False))

    def bind_state(self, state, parent=None) -> None:
        """Forward the live run to any run-aware provider (RunTools/DataTools); others ignore it."""
        for p in self.providers:
            if hasattr(p, "bind_state"):
                p.bind_state(state, parent)


def compose_tools(providers: list, settings):
    """Turn a provider list into the toolset a role is handed — the ONE place that decision lives.

    A single provider is normally handed over bare. But `Settings.hide_empty_tools` (stop
    advertising a tool whose provider reports it currently holds nothing) is implemented by
    `CompositeTools.specs`, so a bare provider would silently opt that configuration out of the
    filter. Lives here rather than in `agents/factory.py` because the rule is a property of this
    class, and because two call sites spelling it out is how they come to disagree.
    """
    hide = bool(getattr(settings, "hide_empty_tools", False))
    return (providers[0] if len(providers) == 1 and not hide
            else CompositeTools(providers, hide_empty_tools=hide))


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


def _compose_loop_tool_specs(tools, emit_spec: dict, *, self_plan: bool) -> list[dict]:
    """Compose provider and loop-owned tools without duplicate or unreachable schemas.

    The emit function and optional ``update_plan`` are control-plane names: dispatch intercepts
    them before provider execution.  A provider using either name was therefore already shadowed,
    while the endpoint still received duplicate schemas.  Preserve the effective internal-wins
    behavior, make it visible, and send one unambiguous schema per name.
    """
    emit_name = (emit_spec.get("function") or {}).get("name")
    reserved = {emit_name}
    if self_plan:
        reserved.add(_PLAN_TOOL_NAME)
    specs: list[dict] = []
    for spec in tools.specs() if tools is not None else []:
        name = (spec.get("function") or {}).get("name")
        if name and name in reserved:
            _LOG.warning(
                "provider tool name %r collides with loop-owned control tool; "
                "keeping the loop-owned tool and shadowing the provider schema",
                name,
            )
            continue
        specs.append(spec)
    specs.append(emit_spec)
    if self_plan and emit_name != _PLAN_TOOL_NAME:
        specs.append(_plan_spec())
    elif self_plan:
        _LOG.warning(
            "emit tool name %r collides with the self-plan control tool; keeping emit semantics",
            emit_name,
        )
    return specs


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


def _compact_in_place(messages: list, context_budget_chars, auto_summary: bool, summarize) -> None:
    """Bound a growing tool-loop history to `context_budget_chars`, once per turn.

    Compaction happens IN PLACE (slice-assign, same list object): callers like the assistant's
    `run_turn` keep a reference to this list to post-process the trace (stream the final answer
    over it); a rebind would orphan their reference on a compacted turn and they'd re-answer
    BLIND, missing every post-compaction tool result. (Which is why this returns None and takes the
    list rather than returning a new one — the in-place contract is the point, not an accident.)

    `context_budget_chars`: None = unset (fall back to the built-in default), 0 = compaction OFF
    (the documented "0 = off" — the old `or DEFAULT` fallback silently turned 0 into the 120k
    default, i.e. compaction ~8× MORE aggressive than the operator asked for), >0 = the budget.
    """
    budget = context_budget_chars
    if auto_summary and budget is None:
        from looplab.core.context_budget import DEFAULT_SUMMARY_CHARS
        budget = DEFAULT_SUMMARY_CHARS
    if not budget:
        return
    if auto_summary:                    # C2: summarize the stale middle once the history grows long
        from looplab.core.context_budget import compact_history
        messages[:] = compact_history(messages, budget, summarize)
    else:                               # H4: else just middle-truncate stale tool output
        from looplab.core.context_budget import truncate_history
        messages[:] = truncate_history(messages, budget)


def _tool_call_args(tc: dict) -> tuple[str, dict]:
    """`(name, args)` for one model tool call, with the args HARDENED to a dict.

    A small/junk model can emit malformed JSON arguments; never let that crash the
    run — treat an unparseable tool call as empty args (emit then falls back to a
    safe result; a retrieval call just gets {}).
    """
    fn = tc.get("function", {})
    raw = fn.get("arguments") or "{}"
    try:
        args = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (json.JSONDecodeError, TypeError):
        args = {}
    if not isinstance(args, dict):
        # Valid-but-non-object JSON ("[0]", "\"x\"", "3") would otherwise reach finalize()/
        # tools.execute() and blow up on .get(); a junk model must never crash the run.
        args = {}
    return fn.get("name", ""), args


def _run_tool_call(tools, name: str, args: dict, *, repeat_state: dict,
                   on_tool_result=None, cancel_check=None) -> tuple[str, str]:
    """Execute ONE retrieval tool call and return `(capped_result, repeat_note)`.

    Every tool call ALWAYS executes and returns fresh content. The G2 read-dedup cache
    that used to stub an exact repeat ("already ran … use the earlier output") was
    REMOVED by explicit operator decision (P3, docs/PROMPT_REVIEW.md): the stub pointed
    at content the model could no longer see after compaction/phase handoff, and the
    cached copy silently went stale — always read what is asked. The StuckDetector in the
    caller is the loop-safety net now: a model that thrashes on the SAME call with the
    SAME result trips B1 and the loop force-emits instead of spinning; the repeat
    note below covers the 3+-call round-robins B1's 1-/2-cycle window can't see.

    `repeat_state` is the caller's per-invocation ledger (see `_REPEAT_NOTE`), mutated here.
    """
    # First-class TOOL observation (Langfuse-style): input=args, output=result, nested
    # under the active operation span next to the generations that decided the call.
    with tracing.tool(name, _trace_preview(args)) as _tool_obs:
        if tools is None:
            typed_result = ToolResult(content=f"(unknown tool: {name})", is_error=True,
                                      retryable=False)
            capability = ToolCapability.unknown(name or "<unknown>", source="loop:no-provider")
            manifest_hash = ""
        else:
            capability_fn = getattr(tools, "capability", None)
            if callable(capability_fn):
                capability = capability_fn(name)
            else:
                declared_fn = getattr(tools, "capabilities", None)
                try:
                    declared = declared_fn() if callable(declared_fn) else ()
                    rows = declared.values() if isinstance(declared, dict) else declared
                    capability = next((c for c in rows
                                       if isinstance(c, ToolCapability) and c.name == name), None)
                except Exception:  # noqa: BLE001 - metadata cannot disable execution compatibility
                    capability = None
                capability = capability or ToolCapability.unknown(
                    name or "<unknown>", source=f"legacy:{type(tools).__name__}")
            manifest_hash = str(getattr(tools, "manifest_hash", "") or "")
            typed_execute = getattr(tools, "execute_result", None)
            if callable(typed_execute):
                try:
                    signature = inspect.signature(typed_execute)
                    accepts_cancel = ("cancel_check" in signature.parameters or any(
                        param.kind == inspect.Parameter.VAR_KEYWORD
                        for param in signature.parameters.values()))
                except (TypeError, ValueError):
                    accepts_cancel = False
                typed_result = ToolResult.coerce(
                    typed_execute(name, args, cancel_check=cancel_check)
                    if accepts_cancel else typed_execute(name, args))
            else:
                typed_result = ToolResult.coerce(tools.execute(name, args))
        result = typed_result.content
        _tool_obs.output(_trace_preview(result))
        _tool_obs.set("capability", capability.as_dict())
        if manifest_hash:
            _tool_obs.set("capability_manifest_sha256", manifest_hash)
        for key, value in typed_result.trace_attributes().items():
            _tool_obs.set(f"result_{key}", value)
        # Cap once, up front — appending an explicit truncation marker when the cap actually
        # bites (P3) — so the provenance hook receives EXACTLY what the tool message below
        # will carry (a single expression, not two kept-in-sync copies).
        result = _cap_tool_result(str(result))
        # Tag the 3rd+ IDENTICAL-RESULT repeat of this (tool, canonical-args) call (see
        # _REPEAT_NOTE: the round-robin gap the StuckDetector's 1-/2-cycle window can't
        # cover; a changed result — a cursor poll's new chunk, a post-write re-read —
        # resets the streak and never gets the note). The note rides OUTSIDE the cap so it
        # can never be truncated away.
        repeat_note = ""
        sig = f"{name}({_canonical(args)})"
        prev, streak = repeat_state.get(sig, (None, 0))
        streak = streak + 1 if result == prev else 1
        repeat_state[sig] = (result, streak)
        if streak >= 3:
            repeat_note = _REPEAT_NOTE.format(k=streak)
        # THIS BLOCK MOVED INSIDE THE TOOL SPAN so the streak can be STAMPED, and that is the
        # whole change: the note was appended to the model's message and to nothing else, so no
        # span, event or export ever carried it. Measured on `e5small-dr-unified-v10`'s first
        # propose phase — 370 tool calls, 71 repeated (tool, args) pairs, 101 repeats whose
        # output was byte-identical — and the durable record could not say whether the nudge had
        # fired once. Nobody could count its firings on any run, and nobody could answer the only
        # question it exists for: does a nudged model stop repeating? `streak >= 3` was therefore
        # an unvalidated constant. Reading the spans it looks like the note NEVER fires; that is
        # an artifact of where it was appended, and this stamp is what makes the two
        # distinguishable.
        #
        # ADDITIVE trace attributes only (invariant #5): no event type, no fold change, no
        # behaviour change — the message the model receives is byte-identical, which is what
        # `tests/test_tool_repeat_streak_is_traced.py` pins. `repeat_streak` rides on EVERY tool
        # call, because "this call has run once" is the denominator the firings are a rate over;
        # `repeat_note_sent` rides only when the note really went, since a flag on a call that
        # was not nudged would be a claim nobody made.
        _tool_obs.set("repeat_streak", streak)
        if repeat_note:
            _tool_obs.set("repeat_note_sent", True)
    if on_tool_result is not None:      # provenance hook: exceptions propagate
        on_tool_result(name, args, result + repeat_note)
    return result, repeat_note


# Every way this loop can stop WITHOUT the model having emitted an answer of its own accord. The
# `on_budget` observer's `kind`, and a CLOSED vocabulary on purpose: `serve/assistant.py::run_turn`
# turns each one into a sentence the operator reads, so a kind with no row there degrades to a
# generic notice rather than to silence — but a kind that is never reported at all is silence, which
# is the defect this vocabulary exists to close.
#
# It shipped covering only the first two. The other three fell straight through to `fallback(...)`,
# which for the assistant means "the last thing the model said out loud" — so a 300-second turn that
# tripped stuck-detection came back as a bare interstitial narration ("Let me look at the next
# file.") presented as the answer, with nothing anywhere saying the investigation had been cut off.
# That is the operator's "the assistant hangs around 40 tool uses and then a bare tool use arrives as
# the reply", reproduced exactly.
LOOP_CUTOFF_KINDS = ("time", "cost", "turns", "stuck", "stalled", "emit_force")


def _accountant_spend(client) -> float | None:
    """The run's spend so far, or None when this client has no accountant.

    None rather than 0.0 on purpose: 0.0 is a real reading (a run that has not spent yet) and would
    make a missing accountant look like a fresh one, which is how a money ceiling would silently
    become a ceiling on nothing. Every accountant-derived rung in this codebase is opt-out-by-absence
    and none may ever raise — a bookkeeping error must not end a session that is working.
    """
    acct = getattr(client, "accountant", None)
    if acct is None:
        return None
    try:
        spent = float(getattr(acct, "spent", None))
    except (TypeError, ValueError):
        return None
    return spent if spent >= 0 else None


def _session_spend(client, at_start: float | None) -> float | None:
    """What THIS session has spent, or None when it cannot be known."""
    if at_start is None:
        return None
    now = _accountant_spend(client)
    if now is None:
        return None
    return max(0.0, now - at_start)


def _note_budget(on_budget, kind: str, *, turns, seconds, detail: str = "") -> None:
    """Report that the loop stopped WITHOUT a model-chosen emit, and which of the five ways it was.

    An observer is best-effort by construction: this fires on the way to a salvage emit, and a
    broken callback must not turn a rescued answer into a crash.

    `detail` is omitted from the payload when empty — the envelope key set is what a UI branches on,
    so an always-present `"detail": ""` would make every ordinary cutoff look like it carried one.
    """
    if on_budget is None:
        return
    try:
        payload = {"kind": kind, "turns": turns, "seconds": round(float(seconds or 0.0), 3)}
        if detail:
            payload["detail"] = str(detail)[:200]
        on_budget(payload)
    except Exception:  # noqa: BLE001 - an observer may never break the loop it observes
        pass


def drive_tool_loop(client, tools, messages: list, emit_spec: dict, *,
                    max_turns: int = 0, context_budget_chars: int | None = None,
                    time_budget_s: float = 0.0, cost_budget_usd: float = 0.0,
                    finalize=None, fallback=None, on_budget=None,
                    stuck_detection: bool = True,
                    stuck_repeat: int = 4, stuck_alternate: int = 4,
                    self_plan: bool = False, plan_reinject_every: int = 5,
                    auto_summary: bool = False, summary_client=None, on_step=None, on_text=None,
                    cancel_check=None, on_tool_result=None,
                    nudge_prompt: str = "", stuck_prompt: str = "", budget_note=None,
                    validate=None, emit_retries: int = 2, emit_after: int = 0, emit_force: int = 0,
                    terminal_salvage: bool = False):
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
      - `cost_budget_usd` (0 = off): MONEY ceiling for THIS session, measured as spend since the
        session started (not the run's total), on the same "do not start another turn" rule as the
        wall clock. Needs `client.accountant`; without one it is silently off, like every other
        accountant-derived rung in this codebase.

        WHY A THIRD CURRENCY. Measured 2026-08-31 over 7 AlgoTune probes: the two ceilings above are
        denominated in turns and seconds, and what actually ends a run is money. On `pde_heat1d` a
        SINGLE plan step took 48 % of a $1.00 run (72 generations) and was cut by the 1200 s wall at
        1212 s — the wall worked, and half the budget was already gone when it fired. `accPde` the
        same: 1212 s, 28 %. The same ceiling on `edge_expansion` never bit at all (worst step 8-9 %),
        so seconds and dollars are not proxies for each other across tasks, and bounding one does not
        bound the other.

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
    # OPEN[turn-zero-duplicate-budget-reminder] seeding the ledger empty makes the first loop
    # iteration inject a "Reminder — BUDGET: $0.0000 ..." user message that duplicates the budget
    # line both wired callers already lead their opening turn with.
    # proof:`present:_last_budget_note = [""]@looplab/agents/tool_loop.py`
    # REVIEW 2026-08-30 (prompt-noise): reproduced with a stub client — nothing has been spent,
    # `budget_note()` renders the same text as the opener, `"" != note`, and the reminder lands
    # before the first model call, against the budget block's own "a turn that spent nothing adds
    # nothing". Seed with one render before the loop (or let the caller pass the opening figure).
    _last_budget_note = [""]        # last note actually injected; see the budget block below

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
    # loops or phases. `_canonical` (imported at module scope) is the detector's own args
    # canonicalizer, reused so the two repeat notions can't drift. The full previous result string
    # is kept (not a hash): it is already capped at RESULT_CAP, and byte-identity must be exact —
    # no collision caveat. `_run_tool_call` owns the ledger's updates.
    repeat_state: dict[str, tuple[str, int]] = {}
    tool_specs = _compose_loop_tool_specs(tools, emit_spec, self_plan=self_plan)
    current_plan = ""
    started = time.monotonic()
    # D11: history compression runs on the dedicated cheap compressor when configured, else the
    # loop's own client. A configured compressor that failed validation/construction is different:
    # use the deterministic local truncation fallback instead of spending against the main client.
    # Loop-invariant: build once, not per turn.
    if not auto_summary:
        summarize = None
    elif summary_client is _SUMMARY_LOCAL_ONLY:
        summarize = lambda _text: ""
    else:
        summarize = _summarizer(summary_client or client)
    stalls = 0                          # consecutive prose turns we couldn't turn into a forced emit
    emit_rejects = 0                    # bad emits bounced back for a re-emit (validate + emit_retries)
    tool_turns = 0                      # G: investigation turns, for the emit_after soft-convergence nudge
    # ...and every turn that CALLED a tool, investigation or not. The two differ: a turn whose only
    # call was a bounced emit or an `update_plan` retrieved nothing. The nudge measures investigation
    # (its wording states the count); the emit_force ceiling measures turns, because it is the hard
    # termination guarantee for an unlimited-`max_turns` loop and must advance even when the model is
    # bouncing on emit validation or only updating its plan.
    call_turns = 0
    emit_nudged = False
    exhausted = False                    # ran out of turns/time (vs stalled/stuck/cancelled)

    def _accept_forced(forced, *, may_retry: bool):
        """Validate a FORCED emit the same way an in-loop emit is validated, then finalize it. Returns
        (accepted, result, refusal) — `refusal` is the validator's own message, for a caller that
        still has a turn to spend delivering it.

        `may_retry` is the whole rule, and it is why the refusal is not applied uniformly. A `validate`
        bounce exists to buy ONE MORE TURN in which the model fixes what it only described; that trade
        is only available where a turn remains. On the three exits that have none (`emit_force`
        ceiling, `stuck`, budget exhaustion) a rejection cannot produce the edit — it only drops the
        emit, and the caller then falls to `fallback`, which for a repair is `lambda m: ""`. That
        discards the summary AND `rollback_stage` and leaves `repair_verdict` empty, so
        `is_developer_stuck` can never fire: strictly worse than accepting an unverified summary,
        which the durable `inert`/`unmet` verdicts already grade on BYTES downstream.

        So a terminal salvage is ACCEPTED and a retryable one is bounced WITH its reason attached —
        previously every path bounced and every path threw the reason away, so the one chance the
        rung promised was never actually delivered to the model."""
        if forced is None:
            return False, None, ""
        # THE SALVAGE IS THE CALLER'S POLICY, NOT THIS LOOP'S, since 2026-08-30. `drive_tool_loop` is
        # generic and the reasoning above is repair-specific: bouncing a terminal emit there only
        # drops the summary and falls to `lambda m: ""`, which is strictly worse. But the STAGES
        # caller passes `validate` to enforce the operator's wall budget, missing `needs` inputs and
        # manifest collisions, and the Researcher's rejects its own degraded draft — so a blanket
        # `and may_retry` skipped all of those on any exit with no turn left, and `_finalize`
        # persisted whatever was merely shape-valid.
        #
        # DEFAULT FALSE, so every caller keeps its validators on every exit unless it says otherwise;
        # only `repo_developer`'s repair session opts in, where the trade was measured and argued.
        if validate is not None and (may_retry or not terminal_salvage):
            try:
                refusal = validate(forced)    # non-None err string == rejected
                if refusal:
                    return False, None, str(refusal)
            except Exception:  # noqa: BLE001 — a broken validator must not crash the loop
                pass
        return True, finalize(forced), ""

    def _salvage_emit(*, may_retry: bool = False):
        """The forced-emit salvage all FOUR exits share (prose reply / `emit_force` ceiling / stuck /
        budget exhaustion): make ONE forced tool call from everything gathered and validate it like
        an in-loop emit. Returns `(accepted, result, refusal)` — deliberately a tuple rather than a
        `None` sentinel, because `finalize` may legitimately return None (ToolUsingStrategist's
        degrades to the rule baseline, which is `Optional[Strategy]`), so "nothing to accept" and
        "the result is None" have to stay distinguishable. The `_cancelled()` guard stays at each
        call site: what a cancelled loop does next differs per exit, and only three of the four skip
        the paid call.

        `may_retry` defaults to FALSE — the safe direction. Only the prose-reply exit loops back for
        another turn, so only it can honour a bounce; see `_accept_forced`."""
        return _accept_forced(_force_emit(client, messages, emit_spec), may_retry=may_retry)

    # Read once, before the first turn: the ceiling is for THIS session, and the accountant it
    # reads is the RUN's, already carrying whatever earlier phases spent.
    _spend_at_start = _accountant_spend(client)
    turns = itertools.count() if max_turns is None or max_turns <= 0 else range(max_turns)
    for turn_idx in turns:
        if _cancelled():                # user hit stop -> finalize from what we have, promptly
            break
        if time_budget_s and (time.monotonic() - started) > time_budget_s:
            exhausted = True
            # TELL SOMEONE. The salvage below rescues an answer from what was gathered, which is
            # right — but presenting a cut-short investigation as a finished one is how "the
            # assistant hangs around 40 tool uses and then something odd comes back" reads to an
            # operator who was never told the turn ran out of wall clock.
            _note_budget(on_budget, "time", turns=turn_idx, seconds=time.monotonic() - started)
            break                       # out of wall-clock budget -> salvage an emit below
        if cost_budget_usd:
            _sp = _session_spend(client, _spend_at_start)
            if _sp is not None and _sp > cost_budget_usd:
                exhausted = True
                # Same reason the wall clock tells someone: a session cut for money that reports
                # nothing looks exactly like one that finished, and the operator reads the short
                # answer as the model's considered one.
                _note_budget(on_budget, "cost", turns=turn_idx,
                             seconds=time.monotonic() - started,
                             detail=f"${_sp:.4f} of ${cost_budget_usd:.4f} for this session")
                break                   # out of money for THIS session -> salvage an emit below
        _compact_in_place(messages, context_budget_chars, auto_summary, summarize)
        # C1: re-surface the agent's own plan periodically so a long loop can't drift off-goal. A
        # `user`-role reminder, not `system`: the plan is verbatim MODEL output (from update_plan
        # args), so a `system` reinjection would let content the model was steered into by injected
        # tool output re-issue itself with system authority every few turns.
        if current_plan and plan_reinject_every and turn_idx and turn_idx % plan_reinject_every == 0:
            messages.append({"role": "user",
                             "content": "Reminder — your current plan/TODO (update it via update_plan "
                                        "as you make progress):\n" + current_plan})
        # THE BUDGET MOVES INSIDE A SESSION; A PROMPT BUILT AT SESSION START DOES NOT. Measured on
        # dsBN 2026-08-28: `deep_research`'s budget line read "$0.0000 of $1.0000 spent" for all
        # SEVEN generations of its first session and "$0.3210" for all four of its second, because
        # the line is baked into `messages` once and replayed every turn. `plan_step` behaves the
        # same ($0.0935 eight times running). For a stage with no turn cap and no money cap that is
        # exactly the wrong shape: `opus5` spent $1.0204 inside ONE research session, so a
        # session-start figure would have said $0.0000 for all ten of its generations and warned
        # nobody.
        #
        # `user`-role for the same reason the plan reminder above is: this is a reminder, not an
        # instruction carrying system authority. Injected only when the rendered note CHANGES, so a
        # turn that spent nothing adds nothing, and never when the caller supplies no callable --
        # every existing caller keeps a byte-identical message list.
        if budget_note is not None:
            try:
                _note = budget_note() or ""
            except Exception:               # noqa: BLE001 - an extra rung must not end a session
                _note = ""
            if _note and _note != _last_budget_note[0]:
                _last_budget_note[0] = _note
                messages.append({"role": "user", "content": "Reminder — " + _note.strip()})
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
            # Stop can arrive while `chat` above was in flight. `_force_emit` is another PAID provider
            # call, so issuing it after cancellation only adds spend and delays the turn ending — the
            # exhaustion path at the bottom of this loop already guards its forced emit this way.
            if _cancelled():
                break
            # THE ONE EXIT THAT LOOPS BACK, so the only one where a `validate` bounce can buy the
            # extra turn it exists for.
            ok, result, refusal = _salvage_emit(may_retry=True)
            if ok:
                return result
            stalls += 1
            if stalls >= 2:
                # SAY SO. Twice in a row the model answered in prose and the endpoint could not be
                # forced into a structured emit, so `fallback` is about to hand back whatever prose
                # was last said — which reads as a finished answer and is not one.
                _note_budget(on_budget, "stalled", turns=turn_idx,
                             seconds=time.monotonic() - started,
                             detail="the model answered in prose and could not be forced to emit")
                break
            # DELIVER THE REFUSAL. A validator that rejected this emit said WHY, and the generic
            # nudge threw that away — so the repair rung that bounces "you described an edit you
            # never made" spent its one shot on a turn that only ever heard "call emit again".
            messages.append({"role": "user",
                             "content": refusal or nudge_prompt
                             or f"Now call `{emit_name}` with your final answer."})
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
        investigated = False            # did any call this turn actually RUN a tool (see call_turns)
        for tc in calls:
            repeat_note = ""            # per-call: set only when an executed call is a 3rd+ repeat
            name, args = _tool_call_args(tc)     # args HARDENED to a dict (see there)
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
                # `messages` IS LEFT INCONSISTENT HERE, deliberately: an accepted emit returns at
                # once, so the emit's own tool_call_id — and any sibling tool_calls listed AFTER it in
                # the same assistant turn — never receive their role:"tool" answer. Finishing them
                # would mean paying for tool calls whose result nobody will read. (The rejected-emit
                # branch above `continue`s precisely because it does NOT return, so it must not leave
                # a dangling id.) The consequence is a caller obligation: anything that RE-SENDS this
                # transcript to a provider must first strip unanswered tool_call_ids, or a strict
                # OpenAI-compatible backend 400s on it — `serve/assistant.py` does exactly that.
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
                # Surface what the agent is about to do BEFORE the (possibly slow) tool runs, so a
                # live progress view advances turn-by-turn instead of jumping only at the end.
                investigated = True     # a real retrieval — this turn counts as investigation
                _step(turn=turn_idx, tool=name,
                      arg=next((str(v) for v in (args or {}).values() if v), ""))
                # Execute + trace + cap + repeat-ledger + provenance hook — see `_run_tool_call`,
                # which owns the always-execute rule (P3) and the identical-result repeat note.
                result, repeat_note = _run_tool_call(tools, name, args, repeat_state=repeat_state,
                                                     on_tool_result=on_tool_result,
                                                     cancel_check=_cancelled)
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
        # The nudge counts only turns that actually RETRIEVED something: a turn whose only call was a
        # bounced emit or an `update_plan` investigated nothing, so counting it both fired the nudge
        # early and made its stated number ("You have investigated enough (N tool turns)") a lie about
        # work the model never did. The FORCE ceiling deliberately keeps counting every tool-calling
        # turn — it is the termination guarantee for an unlimited-`max_turns` loop, and a model that
        # only ever updates its plan must still be forced to emit.
        # The gate keys on "is there ANY callable tool", not on `tools is not None`. `self_plan`
        # (ON by default via loop_opts_from_settings) exposes `update_plan` even on an emit-only
        # loop, so a `tools is None` check silently disabled the `emit_force` HARD termination
        # guarantee for a loop the model can still call into: `update_plan` with VARYING args
        # defeats the StuckDetector's identical-PAIR check, and under the default unlimited
        # `max_turns=0` / `time_budget_s=0` that loop spun forever. Reachable both ways — an agentic
        # ToolUsingStrategist gets tools=None when build_strategist_tools() finds no providers, and
        # UnifiedAgent's pilot/triage pass pilot_tools=None when researcher_tools=False, both with
        # self_plan on. (DeepResearcher only dodged it by passing a `_NoTools()` sentinel instead of
        # None, which is precisely the accident this makes unnecessary.)
        if (emit_after or emit_force) and (tools is not None or self_plan):
            call_turns += 1
            tool_turns += int(investigated)
            if emit_force and call_turns >= emit_force:
                # Announced BEFORE the salvage, like the wall-clock exit: an answer forced at the
                # convergence ceiling is a salvage from what was gathered either way, and the
                # operator has to be able to tell it from a conclusion.
                _note_budget(on_budget, "emit_force", turns=call_turns,
                             seconds=time.monotonic() - started,
                             detail=f"the soft-convergence ceiling ({emit_force} tool turns) was hit")
                if _cancelled():        # paid call — see the prose-reply force above
                    break
                ok, result, _ = _salvage_emit()
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
            # THE ONE THAT COST THE MOST. This exit is the assistant's most common non-answer: a long
            # investigation that starts circling, and (on an endpoint that ignores tool_choice)
            # falls through to the last thing the model said out loud. Unreported, that is
            # indistinguishable from a finished turn.
            _note_budget(on_budget, "stuck", turns=turn_idx,
                         seconds=time.monotonic() - started, detail=str(stuck_reason))
            messages.append({"role": "user",
                             "content": (stuck_prompt.replace("{reason}", str(stuck_reason)) if stuck_prompt
                                         else f"Stop: you appear to be stuck ({stuck_reason}). "
                                              f"Call `{emit_name}` now with your best answer.")})
            if _cancelled():            # paid call — see the prose-reply force above
                break
            ok, result, _ = _salvage_emit()
            if ok:
                return result
            break
    else:
        exhausted = True                # every turn used without an emit
        _note_budget(on_budget, "turns", turns=max_turns, seconds=time.monotonic() - started)
    if exhausted and not _cancelled():
        # Budget exhaustion (turns or wall-clock) used to fall STRAIGHT to fallback, discarding the
        # whole investigation — the Developer's STAGES phase read a big repo for its full 30-turn
        # budget, never got to `declare_stages`, and silently degraded to "no stages declared".
        # Salvage ONE forced structured emit from everything gathered; only then fall back.
        messages.append({"role": "user",
                         "content": f"Out of turn/time budget. Call `{emit_name}` NOW with your "
                                    "best answer from everything you have gathered."})
        ok, result, _ = _salvage_emit()
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
    # Checked OUTSIDE the try below, on purpose: a bad option name must raise where nothing is
    # catching it, not be swallowed by the containment `except` that turns any agentic failure into
    # a plain completion — which is exactly how the historical duplicate-keyword TypeError hid.
    options = LoopOptions.coerce(loop_opts)
    emit_spec = {"type": "function", "function": {
        "name": "answer", "description": f"Emit {answer_desc}. This ends your turn.",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string", "description": answer_desc}},
                       "required": ["text"]}}}
    try:
        return drive_tool_loop(client, tools, messages, emit_spec,
                               finalize=lambda a: str((a or {}).get("text", "") or ""),
                               fallback=fb, **options)
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
    options = LoopOptions.coerce(loop_opts)      # checked outside the try — see `agentic_text`
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
                               **options)
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


def loop_opts_from_settings(settings) -> LoopOptions:
    """Collect the config-driven tool-loop options (B1 stuck detection + C1 self-plan + C2
    auto-summary) into the typed `LoopOptions` bundle to spread into `drive_tool_loop`. Plain
    scalars only — safe to reuse across calls (the loop builds a FRESH StuckDetector per invocation
    from these thresholds) — plus the optional D11 compression client (stateless, reusable).

    `LoopOptions` is Mapping-shaped, so `**loop_opts_from_settings(s)` spreads exactly the keys the
    dict this replaced carried (doc 25 AG-01); what it adds is that every option has ONE declaration
    point and merging is `.replace()` / `.with_defaults()` instead of a per-call-site `setdefault`.
    """
    g = getattr
    opts = LoopOptions(
        stuck_detection=bool(g(settings, "agent_stuck_detection", True)),
        stuck_repeat=int(g(settings, "agent_stuck_repeat", 4)),
        stuck_alternate=int(g(settings, "agent_stuck_alternate", 4)),
        self_plan=bool(g(settings, "agent_self_plan", True)),
        plan_reinject_every=int(g(settings, "agent_plan_reinject_every", 5)),
        auto_summary=bool(g(settings, "agent_auto_summary", True)),
        emit_after=int(g(settings, "agent_emit_after", 300)),  # G: nudge to emit after N tool turns
        emit_force=int(g(settings, "agent_emit_force", 500)),  # G: force the emit at this many turns
    )
    # C2/H4: the configured context budget must reach EVERY loop, not just the Researcher — the
    # 120k built-in fallback otherwise survives in the Developer's 500-turn implement session (the
    # exact loop the budget raise targeted). Only set when configured, so a bare stub settings
    # object keeps the loop's own unset (None -> built-in default) semantics; an explicit 0 = off.
    cb = g(settings, "context_budget_chars", None)
    if cb is not None:
        opts = opts.replace(context_budget_chars=int(cb))
    # D11 compression model slot (open_deep_research's four-slot pattern): a dedicated CHEAP
    # summarizer for history compression, instead of paying the main model for it. Blank = the
    # loop's own client (byte-identical legacy behavior).
    from looplab.core.llm import make_llm_client_for, role_profile
    # Gated on a compressor model being configured AT ALL — by the legacy field or by a profile bound
    # to the role. Gating on the field alone meant `role_profiles={"compressor": ...}` naming a
    # complete connection validated, passed the startup credential check, and then never built a
    # client: every compression kept paying the main model.
    if g(settings, "compressor_model", None) or role_profile(settings, "compressor").get("model"):
        try:
            # Role-resolved, so the compressor can sit on its own provider WITH its own credential;
            # its own fields still win, so this is the same client as before without profiles.
            opts = opts.replace(summary_client=make_llm_client_for(settings, role="compressor"))
        except Exception:  # noqa: BLE001 - invalid optional config stays local, never bills main
            opts = opts.replace(summary_client=_SUMMARY_LOCAL_ONLY)
    return opts


def resilient(attempt, fallback, *, on_error=None):
    """Run `attempt()`; on any non-budget failure return `fallback()` instead (doc 25 AG-06).

    This is the package's containment rule, written down once. Every agentic entry point needs it and
    it is currently restated at ~15 call sites, each with its own why-comment:

        a hard budget stop PROPAGATES and ends the run; anything else DEGRADES to a caller-specific
        safe value rather than crashing it.

    The asymmetry is the whole point and is easy to get backwards. `BudgetExceeded` is not a failure
    to contain — it is the operator's spend ceiling doing its job, and swallowing it would let a run
    keep billing past the limit that was set to stop it. Everything else (a transport error, an
    endpoint 5xx after retries, a parser giving up) is a *local* problem: the flagship agentic path
    must degrade to a bounded default, because crashing the run loses every node already evaluated.

    Deliberately NOT applied to the existing sites. Doc 25's own recommendation is to adopt this
    opportunistically at new call sites rather than churning fifteen comment-bearing ones, and those
    comments are load-bearing — each records why THAT fallback is safe (e.g. "`_fallback` is itself
    resilient … so it can't re-raise the transport error"). Replacing them with a bare call would
    trade fifteen small duplications for fifteen lost explanations.

    `on_error` receives the contained exception for logging/telemetry. It must not raise; if it does,
    the fallback still runs, because a broken observer must not become a broken agent.
    """
    try:
        return attempt()
    except BudgetExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 - the containment boundary this helper exists to be
        if on_error is not None:
            try:
                on_error(exc)
            except Exception:  # noqa: BLE001 - telemetry must never escalate a contained failure
                pass
        return fallback()


def emit_loop(client, tools, messages: list, model_cls, settings, *, description: str,
              fallback=None, on_step=None):
    """Drive a tool loop whose only terminal is one `emit` call, and return the emitted model.

    Two HTTP surfaces — the genesis planner and the boss command router — each hand-built the same
    scaffolding around `drive_tool_loop`: an `emit` function spec whose parameters are a pydantic
    model's JSON schema, a mutable cell for the result, a finalizer that filters the model's kwargs
    to declared fields and degrades to an EMPTY model on junk, and the settings-driven turn/time
    limits. Only the model, the tools and the prompts differed — and prompts are contracts, so they
    stay verbatim at the call sites (doc 25 SR-11).

    The junk degradation is the part worth having once. A model that emits a plausible-looking but
    wrong-shaped payload must still yield a USABLE empty plan rather than an exception, because the
    caller's next move is to render a card to a human; and unknown keys are dropped rather than
    passed through, so a hallucinated field cannot reach a `model_cls` that permits extras.

    `fallback(messages, emitted)` runs when the loop ends without an emit — the model drove tools
    and stopped, or ignored them entirely. Its answer becomes the result, so a caller that forces a
    final structured call there does not have to write into a cell of its own.
    """
    emitted: dict = {}

    def _finalize(args):
        try:
            emitted["value"] = model_cls(**{key: value for key, value in (args or {}).items()
                                            if key in model_cls.model_fields})
        except Exception:  # noqa: BLE001 - junk emit -> empty model (still a usable card)
            emitted["value"] = model_cls()
        return emitted["value"]

    def _fallback(pending):
        answer = emitted.get("value")
        if fallback is not None:
            answer = fallback(pending, answer)
        emitted["value"] = answer
        return answer

    # Resolve the driver through `agents/agent.py` at CALL time rather than binding this module's
    # own global. That re-export is THE documented monkeypatch seam (CLAUDE.md, and
    # `tests/test_prompt_injection_rule.py` asserts it by name): every prompt-injection and
    # scope-redaction test intercepts agentic loops there. Calling the local name would have quietly
    # retired that seam for the two surfaces this helper serves — and those are exactly the tests
    # that check an untrusted prior report cannot reach a system prompt.
    from looplab.agents import agent as _agent  # deferred: `agent` imports this module

    # The turn/time limits ride IN the bundle rather than beside it (doc 25 AG-01): `max_turns=…,
    # **opts` is the exact shape that raises `TypeError: got multiple values` the day `opts` gains
    # the key — here through a monkeypatched `loop_opts_from_settings`, which the suite does. They
    # are DEFAULTS, not overrides: a bundle that already carries a limit is the operator's configured
    # value and must win over these `getattr` fallbacks.
    options = LoopOptions.coerce(loop_opts_from_settings(settings)).with_defaults(
        max_turns=getattr(settings, "agent_max_turns", 0),
        time_budget_s=getattr(settings, "agent_time_budget_s", 0.0))
    _agent.drive_tool_loop(
        client, tools, messages,
        {"type": "function", "function": {
            "name": "emit", "description": description,
            "parameters": model_cls.model_json_schema()}},
        finalize=_finalize, fallback=_fallback, on_step=on_step,
        **options)
    return emitted.get("value")
