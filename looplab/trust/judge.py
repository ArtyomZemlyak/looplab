"""One structured-judge invocation, shared by the two verifiers (doc 25 CT-09).

`trust/memo_verify.py` (the D8 memo-claim verifier) and `trust/verifier.py` (the advisory criteria
scorer) do different jobs, and a full merge would be wrong. But both reach the model the same way:

    agentic_struct(client, tools, msgs, Model, parser=parser, loop_opts={"max_turns": 15},
                   fallback=lambda m: parse_structured(client, m, Model, parser))
    # ...or plain parse_structured when there are no tools

Written twice, a change to the judge-call CONTRACT — the turn budget, the fallback, the parser — has
to be found by grep in two places, and the two are three files apart with near-identical names. The
contract lives here; what each verifier decides on its own is its prompt, its output model, and what
it does with a failed sample.
"""
from __future__ import annotations

from typing import Any, Optional

# These judges READ the run and then emit; they do not investigate for 300 turns. The same budget
# `reflect_lessons` uses, and the reason it is a constant rather than two literals: a turn budget
# that drifts between the two verifiers changes how much evidence each one can afford to look at.
JUDGE_MAX_TURNS = 15


def structured_judge(client, msgs: list, model: type, *, parser: str,
                     tools: Any = None, max_turns: int = JUDGE_MAX_TURNS,
                     tool_result_label: str = "") -> Optional[Any]:
    """Ask the model for `model`-shaped output, agentically when `tools` are available.

    With `tools`, the judge reads the run through them first and the plain structured parse becomes
    the FALLBACK — so a tool loop that yields nothing valid still produces a verdict instead of
    nothing. Without them it is the plain parse, byte-identically to what both callers did inline.

    Raises whatever the model layer raises; each caller already owns its own failure policy
    (`verify_memo` keeps its deterministic verdicts, `verify` drops the sample), and moving that
    decision in here would flatten two deliberately different contracts into one.

    `tool_result_label` is the untrusted-evidence FENCE on every tool result (`core/evidence.py`),
    carried to the loop (review 2026-09-22, TAT-02): the two live-log watchdog judges read the
    candidate's own training log through their tools, and without this parameter no caller could
    ask for it. Forwarded only when non-empty — the historical call byte for byte otherwise, and
    moot without `tools` (there is no tool result to fence).
    """
    from looplab.core.parse import parse_structured

    if tools is None:
        return parse_structured(client, msgs, model, parser)
    # Imported inside the call, not at module scope: `search` imports `agents` at module level, so a
    # module-level `agents` import in a package `agents` can reach would close the cycle.
    from looplab.agents.agent import agentic_struct

    return agentic_struct(
        client, tools, msgs, model, parser=parser, loop_opts={"max_turns": max_turns},
        fallback=lambda m: parse_structured(client, m, model, parser),
        **({"tool_result_label": tool_result_label} if tool_result_label else {}))
