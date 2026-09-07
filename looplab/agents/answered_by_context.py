"""The one place a prompt tells a model which of its tools this turn's context already answered.

WHY THIS EXISTS
---------------
An agent turn opens with a snapshot of the run and closes with a tool surface, and nothing joined
them. Measured 2026-08-19 over six cold-start runs, **138 of 227 tool calls returned nothing at
all**, and the empty tail was dominated by tools whose emptiness was knowable before the call:
``read_asset`` 20/20, ``cross_run_search`` 12/12, ``read_concept_tree`` 10/10, ``data_schema`` 9/9,
``list_themes`` 9/9, ``list_notes`` 8/8. Every one of those is a store the process could see was
empty while it was building the prompt.

INSTRUCTION VS DATA — the measurement that decided the shape
------------------------------------------------------------
A prompt RULE asking the model to read its context before reaching for a tool was A/B'd over three
models x three runs and moved nothing (deepseek 21.0 -> 20.3 calls, gemini 4.0 -> 4.0, glm
17.7 -> 19.3). This block — the same knowledge as DATA in the user turn — was A/B'd over six live
runs and did: 41.3 -> 34.3 tool calls, exact duplicates 6.7 -> 3.7, LLM calls 14.3 -> 12.0.

Keep that difference in mind before "simplifying" this into prose. The rule and the block are not
two spellings of one idea; only one of them changed behaviour.

Three RENDERINGS of the same rows were then A/B'd live, 3 runs each on one task/model/provider:

    flat (`tool=count` pairs, shipped)   17.7 tool calls   5.3 empty   0.7 duplicates
    grouped by answer (EMPTY / HAS / ?)  19.3              5.3         3.0
    terse (every clause removed)         24.0              5.7         3.3

against 41.3 / 25.0 / 6.7 with no block at all. The three are within the run-to-run spread of each
other at n=3, so the honest reading is that PUBLISHING the counts is what matters and the layout does
not — but terse being worst is at least consistent with the trailer's clauses being load-bearing
rather than filler, which is why they survive. Do not re-litigate the layout without more runs than
that; do not delete the trailer on the theory that it is padding.

WHERE THE NUMBERS COME FROM
---------------------------
Every row is the PROVIDER's own answer (``tools/_base.py::INVENTORY_CONTRACT``), never a second
derivation from ``RunState``. That is deliberate: a brief that re-derives "how many experiments are
there" is a second reader of the same question, and the two go out of sync in exactly the direction
that matters — the brief publishes a count the tool then contradicts. Asking the provider also means
each row carries that provider's own scope rules (``SiblingRunTools``'s fail-closed task boundary,
``CrossRunTools``'s store filter) instead of a looser restatement of them.

WHAT A NUMBER CLAIMS, AND WHAT IT DOES NOT
------------------------------------------
A number is the SIZE of what the tool can read, not a promise about content: ``cross_run_search=41``
says the store holds 41 rows, not that any answers a query. So a non-zero row is an upper bound and
only a ZERO is decisive — a tool with nothing to read cannot return anything. The asymmetry is the
safety property: an over-count costs at most the call that would have happened anyway, while an
under-count would suppress a call that had an answer.

``UNKNOWN(reason)`` is therefore a first-class value and never collapses to ``0``. "I looked and
there are none" and "I could not look" are different claims with opposite consequences, and the
concept readers already draw this line by hand — ``run_tools._concept_tree`` answers "recorded
fallback [] is NOT a known-empty taxonomy" when its projection is unavailable. Publishing a ``0``
there would assert exactly what that reader refuses to assert.

THE BLOCK IS NOT THE WHOLE FIX, AND THE OTHER HALF IS IN THE TOOLS
------------------------------------------------------------------
A correct count is defeated by an answer the model can read as a near-miss. Measured 2026-08-19
with this block already live and publishing ``read_asset=0``, one deep-research phase still spent
NINE ``read_asset`` calls walking ``solver.py``, ``reference_svm.py``, ``reference``, ``train``,
``test`` — because the answer was "(this task has no data assets)", which reads as *not that one*.
``cross_run_search=0`` lost the same way to five rephrasings against an empty store. Both answers
are now TERMINAL at the tool (``run_tools.DataTools._NO_ASSETS``, ``cross_run_tools``'s empty-store
branch): they state the CLASS of the emptiness and that no argument will change it. Publishing a
count and leaving a retry-inviting answer underneath is half a fix.
"""
from __future__ import annotations

from looplab.tools._base import collect_inventory, render_inventory

# The trailer — the surviving text of the three renderings above. Three sentences, each
# earning its place:
#
#  1. what a row MEANS (a number is what the tool would read) — without it the model has to guess
#     whether `read_asset=0` is a count, an id or a limit;
#  2. what UNKNOWN means, in the direction that protects the model from the block — it is an
#     invitation to call, not a discouragement;
#  3. WHEN the block goes stale. This is not decoration: with concurrent evaluation a sibling's
#     result can land between two turns of one session, so a snapshot is a point in time and a
#     re-read after something HAPPENED is correct. The clause names the events that make it
#     correct so it cannot be read as "never call these again".
_TRAILER = (
    " — a number is how much that tool has to read right now, so 0 means a call returns nothing; "
    "UNKNOWN means this snapshot could not count it and the tool may still have something. "
    "The run keeps moving: re-read one after something HAPPENED (an experiment finished, an "
    "evaluation landed), not because an earlier answer looked empty."
)

# The lead. It states what the rows ARE, and deliberately does NOT claim that calling any of them
# is pointless: only a ZERO is decisive, and the first version's "a call returns the same, no need
# to spend one" said otherwise about every row -- including `cross_run_search=41`, where the tool
# holds 41 records the prompt never carried. Saying "a call returns the same" there is the
# under-count direction this module exists to prevent, one sentence earlier than the trailer that
# corrects it.
_LEAD = "how much each of your tools holds right now, measured while this turn was assembled: "


def answered_by_context(tools) -> str:
    """One block naming what `tools` already holds, or `""` when there is nothing to say.

    Returns the empty string for a caller with no tool surface (the non-agentic roles) and for a
    provider that declines the hook — silence costs at most the call that would have happened
    anyway, while a fabricated row costs a call that would have found something.
    """
    rows = collect_inventory(tools) if tools is not None else {}
    rows = {name: value for name, value in rows.items() if name in _offered(tools)}
    if not rows:
        return ""
    # Leading blank line: `_state_brief` ends mid-sentence without one, so without this the block is
    # glued onto "...a merge inherits all actual parents." and stops reading as its own section --
    # which is the whole of its measured value. Every comparable code-owned splice in `roles.py`
    # (`_UNTRUSTED_MEMORY_RULE`, `_CONTEXT_BEFORE_TOOLS_RULE`) opens the same way.
    return chr(10) * 2 + _LEAD + render_inventory(rows) + _TRAILER


def _offered(tools) -> frozenset:
    """The tool names this turn's request will actually carry.

    The block must never name a tool the model was not offered. Under `hide_empty_tools` the offer
    is filtered on the SAME zeros this block publishes, so an unfiltered block described a surface
    the endpoint never received -- measured on a cold-start repo run, `specs()` offered nothing
    while the block named `read_asset`, `data_schema` and `data_profile`, and the trailer then told
    the model to re-read them. A provider with no `specs()` at all is treated as offering
    everything it reports, which is the pre-existing behaviour for a bare provider.
    """
    getter = getattr(tools, "specs", None)
    if not callable(getter):
        return frozenset(collect_inventory(tools))
    try:
        return frozenset((spec.get("function") or {}).get("name") for spec in getter() or ())
    except Exception:  # noqa: BLE001 - a prompt must never fail on an optional receipt
        # OPEN[offered-fallback-names-unoffered-tools] on a specs() error the fallback names every
        # INVENTORIED tool — including ones the failed offer never carried — which is the exact
        # defect this function's docstring exists to prevent, restored on the error path.
        # proof:absent:frozenset()@looplab/agents/answered_by_context.py
        # REVIEW 2026-08-30 (rule-inversion): the module's own "silence costs at most the call"
        # rule prescribes failing toward the EMPTY frozenset (block suppressed) here; the
        # no-specs()-at-all branch above is a different case (a bare provider offers everything it
        # reports) and keeps its behaviour.
        return frozenset(collect_inventory(tools))
