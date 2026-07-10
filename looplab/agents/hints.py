"""Shared rendering of standing operator/boss directives (`RunState.pending_hints`) into an LLM
context. Hints are append-only events folded into `pending_hints`; a `hint` event may carry
`replace: true` to SUPERSEDE earlier ones (mirrors the set_strategy/pending_strategy pin), so the
boss can rewrite the single standing directive instead of piling up contradictory ones.

Every LLM stage that can act on a directive renders it through this one helper so recency and
precedence are conveyed identically everywhere (the agent must be able to tell which directive is
newest and know that the newest wins on conflict): the Researcher (proposals), the Strategist
(policy/operator mix), the pilot (macro-action choice), the crash-triage agent (repair-vs-reject),
and the Developer (the built CODE — the engine folds the directives into the idea handed to
`implement`, see `engine/node_build.py::_directed_idea`). Signal-delivery §1: a directive that only reached
the proposal used to steer WHAT to try but not HOW it was built or which action ran next.
"""
from __future__ import annotations


def render_hint_directives(pending_hints, *, max_shown: int = 6) -> str:
    """A prompt block listing standing directives oldest→newest with explicit precedence, or ""
    when there are none. The most recent directive is flagged as authoritative on conflict; only
    the last `max_shown` are shown (older ones are summarized as a count, not dumped)."""
    hints = [str(h.get("text", "")).strip() for h in (pending_hints or []) if h.get("text")]
    hints = [h for h in hints if h]
    if not hints:
        return ""
    if len(hints) == 1:
        return "\nOperator directive (follow it): " + hints[0]
    shown = hints[-max_shown:]
    dropped = len(hints) - len(shown)
    lines = [f"  (+{dropped} older directive(s) superseded/omitted)"] if dropped else []
    for i, h in enumerate(shown):
        newest = i == len(shown) - 1
        lines.append(f"  {i + 1}. {h}" + ("   <-- MOST RECENT, follow this when they conflict"
                                          if newest else ""))
    return ("\nOperator directives, oldest first, newest last (follow them; the most recent takes "
            "precedence when they conflict):\n" + "\n".join(lines))
