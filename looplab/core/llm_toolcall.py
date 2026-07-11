"""Assistant-message / native tool-call parsing for the LLM clients (split out of `core.llm`).

Recovery of tool calls that models leak into `content` in their own template
(`_extract_native_tool_calls` / `_apply_native_tool_calls`), slot-merging of streamed tool-call
deltas (`_tool_call_slot` / `_args_complete`), and reasoning/<think> extraction
(`_reasoning_of` / `_clean_thinking`). `core.llm` re-imports every name under its original name,
so `looplab.core.llm._apply_native_tool_calls` (and the flat `looplab.llm.…`) keep resolving to
the SAME objects — tests and callers import and monkeypatch through those paths.
"""
from __future__ import annotations

import json
import re

# Safe top-level import (no cycle): parse imports only from looplab.core.errors now.
from looplab.core.parse import split_think


def _reasoning_of(msg: dict) -> str:
    """The dedicated reasoning field of an OpenAI-shaped assistant message: `reasoning` (OpenRouter/
    Ollama) or `reasoning_content` (newer OpenAI/SGLang), whichever is present. '' when absent."""
    return msg.get("reasoning") or msg.get("reasoning_content") or ""


def _clean_thinking(content: str, reasoning: str = "") -> tuple[str, str]:
    """(thinking, answer) for an assistant turn. Reasoning models surface their chain-of-thought one
    of two ways: a DEDICATED field (`reasoning`/`reasoning_content` — newer Ollama/OpenAI) or INLINE
    <think>…</think> tags in `content`. Prefer the dedicated field (content is then already clean);
    otherwise split the inline tags. Either way the answer is the clean conclusion the UI surfaces."""
    if reasoning:
        _, answer = split_think(content or "")   # strip any stray inline tags too, for safety
        return str(reasoning), (answer or content or "")
    return split_think(content or "")


# Native tool-call recovery. Some models (glm-5.1 / DeepSeek served via litellm) emit tool calls in
# their OWN template as plain CONTENT instead of OpenAI `tool_calls` — e.g.
# `<｜DSML｜invoke name="emit"><｜DSML｜parameter name="arguments" ...>{...}</…></…>`. When that leaks
# into content the agent loop sees no tool call and the raw markup reaches the user. These lift the
# leaked call back into OpenAI-shaped tool_calls (name + JSON arguments) and clean the content.
#
# Every pattern REQUIRES a genuine OPENING tag `<…invoke…` (negative lookahead `(?!/)` after the `<`
# so a bare CLOSER `</invoke>` never counts as an opener): otherwise prose that merely QUOTES the
# syntax un-fenced — 'write invoke name="delete_file" … and close with </invoke>' — would be lifted
# into a real, EXECUTED tool call (the docstring's stated worst case). Weak local models routinely
# omit code fences, so the code-span guard alone is not enough — the tag anchor is what makes it safe.
_NATIVE_INVOKE_RE = re.compile(r'<(?!/)[^>]*?invoke\s+name="([^"]+)"(.*?)</[^>]*?invoke>', re.DOTALL)
_NATIVE_PARAM_RE = re.compile(r'parameter\s+name="([^"]+)"[^>]*?>(.*?)</[^>]*?parameter>', re.DOTALL)
_NATIVE_OPEN_RE = re.compile(r'<(?!/)[^>]*?(?:DSML|tool_calls|\binvoke\b)')


_CODE_SPAN_RE = re.compile(r"```.*?(?:```|$)|`[^`\n]*`", re.DOTALL)


def _code_spans(text: str) -> list[tuple[int, int]]:
    """Spans of fenced blocks and inline code — markup QUOTED there is the model talking about
    tool calls (docs, examples, explaining this very file), never a leaked call to recover."""
    return [m.span() for m in _CODE_SPAN_RE.finditer(text)]


def _extract_native_tool_calls(content: str):
    """(tool_calls | None, cleaned_content). Parse a leaked native tool-call block out of `content`.
    Deliberately conservative: only tag-anchored markup OUTSIDE code spans counts — recovering a
    merely-quoted example would truncate the reply and, worse, execute text as a real tool call."""
    if not content or "invoke name=" not in content:
        return None, content
    spans = _code_spans(content)

    def _quoted(pos: int) -> bool:
        return any(a <= pos < b for a, b in spans)

    calls = []
    for m in _NATIVE_INVOKE_RE.finditer(content):
        if _quoted(m.start()):
            continue
        name, body = m.group(1), m.group(2)
        params = {p.group(1): p.group(2).strip() for p in _NATIVE_PARAM_RE.finditer(body)}
        args = params.get("arguments")
        if args is None:
            args = json.dumps(params or {})
        else:
            try:
                json.loads(args)                 # already valid JSON string -> keep as-is
            except (ValueError, TypeError):
                args = json.dumps(params or {})   # fall back to the param map
        calls.append({"id": f"call_{len(calls)}", "type": "function",
                      "function": {"name": name, "arguments": args}})
    if not calls:
        return None, content
    m0 = next((m for m in _NATIVE_OPEN_RE.finditer(content) if not _quoted(m.start())), None)
    if m0 is None:                # invoke text without any tag-anchored opener — quoted, not leaked
        return None, content
    clean = content[:m0.start()].strip()
    return calls, clean


_FINAL_NAMES = {"emit", "finalanswer", "answer", "reply", "respond", "finish", "submit", "final", "done"}
_ANSWER_FIELDS = ("answer", "reply", "text", "response", "summary", "content", "message")


def _apply_native_tool_calls(msg: dict) -> dict:
    """If `msg` has no OpenAI tool_calls but its content carries a leaked native tool-call block,
    recover it. A FINAL-ANSWER-style call (emit / final_answer / answer / …) becomes the clean visible
    content (its answer text) so the loop finalizes on it; any other call is lifted into real
    tool_calls. Either way the raw markup never reaches the user. Mutates + returns `msg`."""
    if not isinstance(msg, dict) or msg.get("tool_calls"):
        return msg
    calls, clean = _extract_native_tool_calls(msg.get("content") or "")
    if not calls:
        return msg
    first = calls[0]
    name = re.sub(r"[_\s-]", "", (first["function"]["name"] or "").lower())
    try:
        args = json.loads(first["function"]["arguments"])
    except (ValueError, TypeError):
        args = {}
    if name in _FINAL_NAMES:
        ans = ""
        if isinstance(args, dict):
            for k in _ANSWER_FIELDS:
                if isinstance(args.get(k), str) and args[k].strip():
                    ans = args[k]
                    break
            if not ans:
                ans = json.dumps(args)
        else:
            ans = str(args)
        msg["content"] = (clean + "\n" + ans).strip() if clean else ans
    else:
        msg["tool_calls"] = calls
        msg["content"] = clean
    return msg


def _assistant_text(msg: dict) -> str:
    """Display text for a tool-calling assistant turn: its content, plus a compact note of any
    tool calls it made (so the trace shows the model chose to call a tool, not an empty reply)."""
    content = msg.get("content") or ""
    calls = msg.get("tool_calls") or []
    if calls:
        names = ", ".join(c.get("function", {}).get("name", "?") for c in calls)
        note = f"[tool_calls: {names}]"
        return f"{content}\n{note}" if content else note
    return content


def _args_complete(slot: dict) -> bool:
    """True when a slot's accumulated `arguments` already form a COMPLETE JSON value — i.e. that call
    is finished, so a following delta belongs to a NEW call, not this one's continuation."""
    joined = "".join(slot["function"]["arguments"])
    if not joined.strip():
        return False
    try:
        json.loads(joined)
        return True
    except (ValueError, TypeError):
        return False


def _tool_call_slot(tcs: dict, tc: dict) -> int:
    """Pick the merge slot for a streamed tool-call delta. When the provider supplies `index` (the
    OpenAI spec), use it verbatim. When it OMITS `index` (several Ollama builds / OpenAI-compat
    gateways emit one WHOLE call per delta with no index), a blind `.get("index", 0)` collapses every
    call into slot 0 — the later call overwrites the earlier's id/name and their argument fragments
    concatenate into invalid JSON. So without an index we START A NEW SLOT when the delta begins a
    new call and otherwise keep appending to the open slot (so a single call streamed in fragments
    stays merged). A new call is signalled by a NEW id, or — for a provider that ECHOES `function.name`
    on every continuation delta while omitting ids — by a repeated name ONLY once the current slot's
    arguments already parse as complete JSON (so an echoed name mid-fragment doesn't split one call)."""
    idx = tc.get("index")
    if idx is not None:
        return idx
    if not tcs:
        return 0
    cur = max(tcs)
    slot = tcs[cur]
    fn = tc.get("function") or {}
    new_id = tc.get("id") and slot.get("id") and tc["id"] != slot["id"]
    new_named = fn.get("name") and slot["function"]["name"] and _args_complete(slot)
    return cur + 1 if (new_id or new_named) else cur
