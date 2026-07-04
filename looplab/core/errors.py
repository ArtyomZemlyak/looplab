"""Shared LLM-layer exception types.

Lives below both `llm` (the clients) and `parse` (structured-output parsing) so either can
raise/catch these without importing the other — this module is what breaks the old
parse↔llm import cycle. `looplab.core.llm` re-exports both names for backward compatibility.
"""
from __future__ import annotations


class BudgetExceeded(Exception):
    pass


class LLMError(RuntimeError):
    """A reachable LLM transport/protocol failure (network down, HTTP error, non-JSON, no choices).
    Raised instead of leaking a raw urllib/JSON exception so the role layer's retry+fallback treats
    it like any other bad response and the run degrades to a safe default rather than crashing."""
