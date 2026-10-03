"""Concept-authoring prompt lines and the schema-aware filter, shared unchanged."""

# THE TWO CONCEPT-AUTHORING LINES `_state_brief` writes after the recorded concept data — exactly one
# of them per brief, chosen by whether inherited membership is delta-safe. Hoisted VERBATIM out of
# `_state_brief` (the bytes are unchanged) so the one caller that must NOT be handed them can name
# them instead of re-spelling them: they are an AUTHORING contract (`concept_mode`, `concepts`,
# `concepts_added`/`concepts_removed`), and only a caller whose answer is an `Idea` has those
# fields. `for_proposal=False` already drops the board's CLAIM contracts for that reason; these two
# were missed, and they reach the pilot, the triage judge and the repair critic, none of whose emit
# schemas has a concept field (review 2026-09-22, Q-1 — `drop_concept_authoring` below and
# `Settings.prompt_truths_judges`). The concept DATA line above them is context and stays.
CONCEPT_AUTHORING_UNSAFE_LINE = (
    "Concept authoring safety: inherited membership is UNAVAILABLE or PARTIAL. "
    "You MUST set `concept_mode=\"full\"`, provide the exact complete set in `concepts`, leave "
    "`concepts_added` and `concepts_removed` empty, and MUST NOT use delta mode for this proposal.")
CONCEPT_AUTHORING_CONTEXT_LINE = (
    "Concept membership context only: use delta mode only when a separate trusted run cue "
    "explicitly enables it; a root inherits the run base and a merge inherits all actual parents.")


def drop_concept_authoring(brief: str) -> str:
    """`brief` without the concept-AUTHORING line `_state_brief` wrote into it — for a reader whose
    emit schema has no concept field (the facade's three judges).

    Removes whole LINES equal to one of the two constants above and nothing else, so every other
    byte of the brief — the concept data line, the board, a hint directive appended after the brief
    — reaches the judge exactly as it was built. A brief that carries neither line comes back
    unchanged."""
    if not brief:
        return brief
    lines = brief.split("\n")
    kept = [line for line in lines
            if line not in (CONCEPT_AUTHORING_UNSAFE_LINE, CONCEPT_AUTHORING_CONTEXT_LINE)]
    return brief if len(kept) == len(lines) else "\n".join(kept)
