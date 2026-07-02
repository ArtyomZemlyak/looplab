"""H4 · Context budgeting for long agent traces. A propose->implement->repair lifecycle with inline
tool calls grows the message history; cap it so a long run doesn't blow the model's context window.
Truncates the MIDDLE of long intermediate messages (keeping the system prompt and the most recent
turns intact), which is where stale tool output accumulates. Pure + deterministic; off when
`max_chars <= 0`.
"""
from __future__ import annotations

# High-water mark (chars) at which auto-summary compacts a long tool-loop history when no explicit
# `context_budget_chars` is set. ~120k chars ≈ ~30k tokens: short loops never hit it; a genuinely
# long agent run gets its stale middle summarized before it can crowd the context window.
DEFAULT_SUMMARY_CHARS = 120_000


def truncate_history(messages: list[dict], max_chars: int, *, keep_last: int = 2,
                     per_msg_cap: int = 400) -> list[dict]:
    """Return a copy of `messages` whose total content size is reduced toward `max_chars` by
    middle-truncating long intermediate messages. The system message and the last `keep_last`
    messages are never truncated (the model needs the task + the immediate context)."""
    if max_chars <= 0:
        return messages
    total = sum(len(str(m.get("content") or "")) for m in messages)
    if total <= max_chars:
        return messages
    n = len(messages)
    out: list[dict] = []
    for i, m in enumerate(messages):
        content = str(m.get("content") or "")
        protected = m.get("role") == "system" or i >= n - keep_last
        # Stop once the running total is back under budget: max_chars is a TARGET, not just a
        # trigger — keep truncating oldest long messages only until we're under, then leave the
        # rest intact (over-truncating would discard far more context than the budget requires).
        if protected or len(content) <= per_msg_cap or total <= max_chars:
            out.append(m)
            continue
        head = per_msg_cap // 2
        trimmed = (content[:head] + f"\n…[truncated {len(content) - 2 * head} chars]…\n"
                   + content[-head:])
        # For a message only marginally over the cap the marker overhead can make `trimmed` LONGER
        # than the original; replacing it would grow the history the budget exists to shrink. Skip
        # unless truncation actually saves bytes.
        if len(trimmed) >= len(content):
            out.append(m)
            continue
        total -= len(content) - len(trimmed)
        out.append({**m, "content": trimmed})
    return out


def compact_history(messages: list[dict], max_chars: int, summarize, *, keep_last: int = 3):
    """C2 · Auto-summary upgrade over `truncate_history`: when the history exceeds `max_chars`,
    LLM-summarize the STALE MIDDLE (everything except the system messages at the front and the last
    `keep_last` turns) into a single compact note, rather than just middle-truncating it. `summarize`
    is a ``callable(text) -> str``. Defensive: on an empty/failed summary it falls back to
    deterministic `truncate_history`, so a flaky summarizer never loses the loop's context.

    Returns a NEW message list (input untouched). Off when `max_chars <= 0` or nothing to compact."""
    if max_chars <= 0:
        return messages
    total = sum(len(str(m.get("content") or "")) for m in messages)
    if total <= max_chars:
        return messages
    n = len(messages)
    # Front: leading system messages (task/goal) — never summarized away.
    head = 0
    while head < n and messages[head].get("role") == "system":
        head += 1
    tail = max(head, n - keep_last)     # keep the last `keep_last` turns verbatim
    # Never start the kept tail on a `tool` message whose owning assistant(tool_calls) turn is about
    # to be summarized away — an orphaned role:tool is rejected by OpenAI-compatible endpoints (HTTP
    # 400 "messages with role 'tool' must be a response to a preceding message with tool_calls").
    # Pull the boundary back so the owning assistant rides into the tail with its tool replies.
    while tail > head and messages[tail].get("role") == "tool":
        tail -= 1
    middle = messages[head:tail]
    if len(middle) < 2:                 # not enough stale context to be worth a summary call
        return truncate_history(messages, max_chars)
    body = "\n".join(f"[{m.get('role', '?')}] {str(m.get('content') or '')}" for m in middle)
    try:
        summary = summarize(body)
    except Exception:                   # noqa: BLE001 - a flaky summarizer must never break the loop
        summary = ""
    if not summary:
        return truncate_history(messages, max_chars)
    note = {"role": "system", "content": "Summary of earlier steps:\n" + summary}
    return messages[:head] + [note] + messages[tail:]
