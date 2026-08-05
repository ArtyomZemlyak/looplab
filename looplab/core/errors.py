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


class LLMCredentialError(LLMError):
    """A credential refusal that carries its ROOT CAUSE, not just the role that tripped over it.

    One wrong variable is checked once per role that consumes it, so a single half-override used to
    raise — and `core/llm.py::validate_bound_profiles` used to PRINT — the identical sentence seven
    times, once per role, prefixed only by the role name. Seven copies of one cause tell the operator
    nothing the first copy did not, and they bury the one thing that would have helped: which knob is
    actually wrong. `cause_detail` is the same refusal stated WITHOUT a role in it, so the aggregator
    can group every role under one diagnosis and print it once.

    `str(exc)` stays whatever the raising site wants a DIRECT (single-role) caller to read — a
    `make_llm_client_for(settings, role="embed")` failure still names `embed` in its message. The two
    audiences differ, which is exactly why the role-neutral form is a separate attribute rather than
    a reworded message.
    """

    def __init__(self, message: str, *, cause_detail: str | None = None):
        super().__init__(message)
        self.cause_detail = message if cause_detail is None else cause_detail


def credential_cause(exc: BaseException) -> str:
    """The role-neutral cause of a credential refusal, for grouping; its message otherwise.

    Every credential gate raises through `LLMCredentialError`, but the gates also re-raise plain
    `LLMError`s from their neighbours (a malformed base URL, for instance), and an aggregator must
    not lose those. Defaulting to `str(exc)` keeps a non-credential failure its own group rather than
    silently merging it with an unrelated one.
    """
    return str(getattr(exc, "cause_detail", None) or exc)
