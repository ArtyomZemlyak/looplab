"""Shared exception types: the LLM layer's, and the REFUSAL marker every surface reads.

Lives below both `llm` (the clients) and `parse` (structured-output parsing) so either can
raise/catch these without importing the other — this module is what breaks the old
parse↔llm import cycle. `looplab.core.llm` re-exports both names for backward compatibility.

`core` imports nothing above itself, which is exactly why the refusal marker belongs HERE: the
engine, the search policies, the sandbox factory and the CLI all have to agree on one predicate,
and this is the only module all four may import.
"""
from __future__ import annotations



class OperatorRefusal(Exception):
    """Marker: LoopLab raised this ON PURPOSE, at a boundary the operator controls.

    THE PROBLEM IT SOLVES. A deliberate refusal and a genuine bug used to be indistinguishable at
    the CLI boundary, so both were rendered as a Rich traceback with the message on the last line —
    `looplab run … -s policy=mcts -s speculation_depth=1` printed 42 frames to say
    "Card speculation requires policy='greedy', got 'mcts'". That presentation tells the operator
    their command CRASHED the program and buries the one line that says what to change.
    `cli/__init__.py` now prints this family as a message with a non-zero exit and leaves every
    other exception its full traceback, which is how a real bug gets diagnosed.

    WHAT MAY CARRY THE MARKER — the bar is deliberately high, because a bug printed as a tidy
    one-line message is far worse than a refusal printed as a traceback:

    * the exception is constructed at a `raise` whose whole job is to REFUSE, never as a side
      effect of Python semantics (no `int("x")`, no attribute/type slip, no library call);
    * its message names what was refused AND what to change, because that message is now the
      entire operator-facing output;
    * the condition is a property of the operator's INPUT (settings, flags, task file, the run's
      own pinned history, the reachability of the endpoint they configured) — not of LoopLab's
      internal state. `Engine.__init__`'s calibrated-role-factory envelope checks deliberately do
      NOT carry it: a role factory that returns the wrong shape is a defect in the code, and it
      must keep its traceback.

    Concrete flavours below preserve each site's historical base class, so every existing
    `except ValueError` / `except RuntimeError` and every `pytest.raises(...)` keeps working — the
    marker only ADDS a way to recognize the family.
    """


class BudgetExceeded(OperatorRefusal, RuntimeError):
    """The run reached the LLM spend ceiling the operator set (`Settings.llm_budget_usd`).

    It wears the marker for the three reasons the docstring above requires: it is constructed at a
    `raise` whose whole job is to refuse (`llm.py::CostAccountant.add`), the condition is a property
    of the operator's own INPUT rather than of LoopLab's internal state, and the message names both
    what was refused and the knob to change.

    It was a bare `Exception` until 2026-08-20 and nobody noticed, because the ceiling had always
    been settable only in code -- so no run had ever actually hit it. The first campaign that did
    got 342 lines of Rich traceback and exit 1 to say "spent 0.0209 >= budget 0.0200": the exact
    presentation this family exists to remove, and worse than cosmetic downstream, because exit 1
    is indistinguishable from a crash. A harness cannot then tell "this task is FINISHED, it spent
    its allowance" from "this task DIED, retry it" -- and retrying is strictly wrong here, since the
    next attempt spends the same allowance and stops at the same wall.

    The ten `except BudgetExceeded` sites in `agents/` are unaffected: they catch by name, and the
    rule they enforce -- a hard budget stop PROPAGATES, it never degrades to a fallback that would
    make another paid call -- is unchanged.
    """


def is_run_ending(exc: BaseException) -> bool:
    """Is this refusal the run REACHING ITS END, rather than something going wrong?

    Its five `OperatorRefusal` siblings are not alike, and treating them alike cost real money.
    `LLMError`, `LLMCredentialError`, `ConfigRefusal` and `EnvironmentRefusal` are all FAULTS: the
    developer session normalises them into its crash sentinel on purpose, the orchestrator pauses
    the run, and "resume once it's fixed" is exactly the right sentence — an outage or a bad key is
    something an operator repairs and continues from.

    `BudgetExceeded` is the odd one out. Nothing is broken; the run has done what it was told to do
    until the money ran out, and it has a champion. Dressing it as a crash paused **16 of the 105
    probe runs that reached full budget** (§228) — every one at or past its $1.00 ceiling, every one
    within 0.2 s of its last call — under "a Developer session crashed (LLM unreachable or a hard
    error)", leaving finished runs marked OWED WORK. Acting on that message cost $0.1056 past a cap
    (§213).

    So the distinction is named here rather than spelled out at each catch site, and the test pins
    all five: the ending is re-raised, the faults keep their sentinel.
    """
    return isinstance(exc, BudgetExceeded)


class ConfigRefusal(OperatorRefusal, ValueError):
    """A refusal about the configuration this command was handed (settings, flags, the task file).

    `ValueError` is the base every one of these sites already raised, so widening them to this type
    is invisible to callers that catch or assert on `ValueError` — `cli/run_cmds.py::run` still maps
    the task-validation ones to a `BadParameter`, and the ones that escape to the boundary are now
    printed as refusals instead of tracebacks.
    """


class EnvironmentRefusal(OperatorRefusal, RuntimeError):
    """A refusal because the machine this command needs is not equipped for what was asked.

    Distinct from `ConfigRefusal` in the remedy, not in the handling: nothing about the run's config
    is *wrong*, the box simply lacks a tool the configuration requires (`trust_mode='untrusted'`
    with no docker CLI on PATH). `RuntimeError` is the historical base at those sites.
    """


class LLMError(OperatorRefusal, RuntimeError):
    """A reachable LLM transport/protocol failure (network down, HTTP error, non-JSON, no choices).
    Raised instead of leaking a raw urllib/JSON exception so the role layer's retry+fallback treats
    it like any other bad response and the run degrades to a safe default rather than crashing.

    Carries `OperatorRefusal` because every one of its ~35 raise sites is a hand-written,
    operator-facing sentence — the credential/endpoint preflights in `agents/preflight.py` and
    `core/llm.py` are outright REFUSALS ("Refusing to start: the roles would degrade to empty
    fallback proposals"), and the transport ones name the URL and the remedy. When one of these
    escapes to the CLI it is never a Python slip, so its message is the whole story; the frames
    that produced it are `urllib`/`httpx` boilerplate. `LOOPLAB_TRACEBACK=1` brings them back.
    """


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


def budget_stop_leaf(exc: BaseException | None, _depth: int = 0) -> BaseException | None:
    """The `BudgetExceeded` inside `exc`, however deeply a task group wrapped it — or None.

    ONE definition, because two callers now have to agree on it and they are on opposite sides of
    the run. `cli/run_cmds.py::_run_engine_guarded` asks it at the very END, to record the ceiling
    as `run_finished {"reason": "budget_exhausted"}` instead of a crash; `engine/orchestrator.py::
    Engine._drain_inflight_evaluation` asks it one frame INSIDE `Engine.run`, to let an evaluation
    that is already burning land its terminal before the ceiling tears the eval task group down. A
    second, hand-synced copy would drift, and the direction it would drift in is the expensive one:
    the engine-side copy failing to recognise a wrapped leaf silently reinstates the very defect
    the drain exists to remove, with nothing going red.

    It lives HERE rather than in either caller because `core` imports nothing above itself, so
    this is the only module both the CLI and the engine may import (the same argument the
    `OperatorRefusal` marker at the top of this file is placed on).

    Bounded depth: an exception group can nest, and a cycle in `__cause__`/`__context__` (which a
    caller can construct) must not turn a terminal-event handler into a hang.
    """
    if _depth > 8 or exc is None:
        return None
    if isinstance(exc, BudgetExceeded):
        return exc
    for inner in list(getattr(exc, "exceptions", ()) or ()):
        found = budget_stop_leaf(inner, _depth + 1)
        if found is not None:
            return found
    for attr in ("__cause__", "__context__"):
        inner = getattr(exc, attr, None)
        if inner is not None and inner is not exc:
            found = budget_stop_leaf(inner, _depth + 1)
            if found is not None:
                return found
    return None
