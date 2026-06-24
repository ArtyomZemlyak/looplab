"""H4 · Context budgeting for long agent traces. A propose->implement->repair lifecycle with inline
tool calls grows the message history; cap it so a long run doesn't blow the model's context window.
Truncates the MIDDLE of long intermediate messages (keeping the system prompt and the most recent
turns intact), which is where stale tool output accumulates. Pure + deterministic; off when
`max_chars <= 0`.
"""
from __future__ import annotations


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
        if protected or len(content) <= per_msg_cap:
            out.append(m)
            continue
        head = per_msg_cap // 2
        trimmed = (content[:head] + f"\n…[truncated {len(content) - 2 * head} chars]…\n"
                   + content[-head:])
        out.append({**m, "content": trimmed})
    return out
