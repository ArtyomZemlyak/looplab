"""Shared rendering of standing operator/boss directives (`RunState.pending_hints`) into an LLM
context. Model-generated deep-research advisories share the durable hint event for compatibility,
but are excluded from this operator-authority block. Hints are append-only events folded into
`pending_hints`; a human `hint` event may carry
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


# The literal every deep-research hint's text begins with. Shared with the writer
# (`engine/research_cadence.py`) rather than spelled twice: it is the fallback identity of a row
# whose `source` stamp predates the field, so a reworded writer that left this filter behind would
# put model output back inside the operator-authority block.
DEEP_RESEARCH_HINT_PREFIX = "deep-research directions: "


def render_hint_directives(pending_hints, *, max_shown: int = 6) -> str:
    """A prompt block listing standing directives oldest→newest with explicit precedence, or ""
    when there are none. The most recent directive is flagged as authoritative on conflict; only
    the last `max_shown` are shown (older ones are summarized as a count, not dumped).

    `source="deep_research"` is model output, not operator authority. It remains in replay state
    and reaches proposal planning through the research memo/open-hypothesis channels, but must not
    be relabelled here as an instruction from the operator.

    TWO KEYS, because one of them is a stamp a durable row might predate. `source="deep_research"`
    is the exact marker the writer sets; `DEEP_RESEARCH_HINT_PREFIX` is the text those rows have
    always carried, and it is what catches a row folded from a log written before the stamp
    existed. Without the second key such a row passes the filter and renders INSIDE the
    operator-authority block ("the most recent is authoritative") — model output relabelled as an
    operator instruction, on exactly the resumed and replayed runs this paragraph forbids it for.
    The writer imports the prefix from here so the pair cannot drift."""
    rows = [h for h in (pending_hints or [])
            if isinstance(h, dict) and h.get("source") != "deep_research"
            and not str(h.get("text") or "").lstrip().startswith(DEEP_RESEARCH_HINT_PREFIX)]
    hints = [str(h.get("text", "")).strip() for h in rows if h.get("text")]
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
