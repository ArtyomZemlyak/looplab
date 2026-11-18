"""Which standing hints carry OPERATOR authority — one filter for every reader.

`RunState.pending_hints` holds the operator's directives AND model-written deep-research advisories
(they share the durable `hint` event for compatibility). Two readers must agree on which is which:
the internal roles' prompt block (`agents/hints.py::render_hint_directives`) and the external
agent's progress read (`harness/progress.py`). It lives in `core` because `harness` may not import
`agents` (`tests/test_package_layering.py`); `agents/hints.py` re-exports both names.
"""
from __future__ import annotations


# The literal every deep-research hint's text begins with. Shared with the writer
# (`engine/research_cadence.py`) rather than spelled twice: it is the fallback identity of a row
# whose `source` stamp predates the field, so a reworded writer that left this filter behind would
# put model output back inside the operator-authority block.
DEEP_RESEARCH_HINT_PREFIX = "deep-research directions: "


def operator_directive_rows(pending_hints) -> list[dict]:
    """Current operator rows, in native order, shared by text and source-reference readers."""
    return [h for h in (pending_hints or [])
            if isinstance(h, dict) and h.get("source") != "deep_research"
            and not str(h.get("text") or "").lstrip().startswith(DEEP_RESEARCH_HINT_PREFIX)
            and h.get("status", "active") == "active"
            and str(h.get("text") or "").strip()]


def operator_directive_texts(pending_hints) -> list[str]:
    """The standing OPERATOR directives, oldest first — never model-written advisories."""
    return [str(h["text"]).strip() for h in operator_directive_rows(pending_hints)]
