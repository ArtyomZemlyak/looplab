"""The finalization PROTOCOL vocabulary: one spelling for the writer and every reader.

`engine/finalize.py` WRITES the finalization checklist; `events/finalize_scope.py` and
`search/speculation_quality.py` READ it back and refuse evidence that does not match. Until doc 25
SE-01 those were hand-synced copies of the same literals — the step names, the `budget` receipt's
field set, and the exact ordered event suffix a quiet finalization emits — so any engine finalize
change broke the calibration gate in lockstep, silently: the gate does not report "the protocol
moved", it reports that six legitimately-calibrated GPU runs are not quality evidence.

It lives in `events/` and not in the engine because `search` may not import `engine`
(`tests/test_calibration_profile_home.py::test_the_search_to_engine_edge_is_gone` asserts that edge
stays at ZERO, and doc 25 XP-07 moved the finalize-scope read side DOWN here for exactly this
reason). `events` imports only `core`, so writer and readers all import this DOWNWARD.

Constants and one pure constructor only — no state, no I/O, no engine import.
"""
from __future__ import annotations

from typing import Any

from looplab.events.types import (
    EV_BUDGET,
    EV_DIVERSITY_ARCHIVE,
    EV_FINALIZATION_FINISHED,
    EV_FINALIZE_STEP,
    EV_RUN_FINISHED,
)

# ------------------------------------------------------------------ the step vocabulary
# Every `finalize_step` payload carries `{"scope": <scope>, "step": <one of these>}`. A step name is
# a bare string in the log, so a typo does not fail — it writes a marker nothing recognizes, and the
# idempotent checklist then re-runs its (sometimes PAID) effect on every finalize pass.
FINALIZE_STEP_BEGUN = "begun"
FINALIZE_STEP_REPORT_BEGUN = "report_begun"
FINALIZE_STEP_REPORT = "report"
FINALIZE_STEP_BUDGET = "budget"
FINALIZE_STEP_DIVERSITY = "diversity"
FINALIZE_STEP_CASE = "case"
FINALIZE_STEP_REFLECTION_BEGUN = "reflection_begun"
FINALIZE_STEP_REFLECTION = "reflection"
FINALIZE_STEP_LLM_COST = "llm_cost"
FINALIZE_STEP_COMPLETE = "complete"
FINALIZE_STEP_ABANDONED = "abandoned"

# ------------------------------------------------------------------ the budget receipt
# The EXACT key set of the `budget` finalization receipt. The reader compares against this set (not
# a subset), so a field added on the writer side without adding it here refuses every run.
FINALIZE_BUDGET_FIELDS = frozenset({
    "elapsed_s", "process_s", "eval_s", "nodes", "speculation", "finalize_scope", "finish_seq",
})


def budget_receipt(
    *,
    elapsed_s: float,
    process_s: float,
    eval_s: float,
    nodes: int,
    speculation: dict[str, Any],
    finalize_scope: str,
    finish_seq: int,
) -> dict[str, Any]:
    """Mint the one `budget` finalization receipt payload, rounding included.

    `engine/finalize.py` publishes exactly this dict and `search/speculation_quality.py` recomputes
    every field of it from the run's own fold. Both rounding and the key set therefore have to be
    one spelling: a reader that rounded differently would reject honest evidence, and `set(payload)
    != FINALIZE_BUDGET_FIELDS` is a hard refusal rather than a tolerated extra field.
    """
    return {
        "elapsed_s": round(elapsed_s, 3),
        "process_s": round(process_s, 3),
        "eval_s": round(eval_s, 3),
        "nodes": nodes,
        "speculation": speculation,
        "finalize_scope": finalize_scope,
        "finish_seq": finish_seq,
    }


# ------------------------------------------------------------------ the quiet terminal suffix
# The exact ordered `(event type, finalize step)` sequence a finalization emits when NOTHING
# optional is configured: no planned finish report, reflection disabled (`memory_dir` unset /
# `reflection_priors` off) and cross-run curation off — i.e. the offline calibration profile. It
# starts at the finalization's OWN `begun` claim, which is appended immediately before `run_finished`,
# and ends with the `complete` marker that closes the scope.
#
# `None` marks a row that is not a `finalize_step`, so its payload is the effect's own receipt
# rather than a step marker.
#
# This is the shape the speculation quality gate demands of calibration evidence. It is a property
# of the WRITER, so `tests/test_finalize_protocol.py` drives a real toy run to completion and
# compares what the engine actually appended against this table — a constant that only the reader
# consults would be the same two hand-synced copies under one name.
QUIET_FINALIZATION_SUFFIX: tuple[tuple[str, str | None], ...] = (
    (EV_FINALIZE_STEP, FINALIZE_STEP_BEGUN),
    (EV_RUN_FINISHED, None),
    (EV_BUDGET, None),
    (EV_FINALIZE_STEP, FINALIZE_STEP_BUDGET),
    (EV_DIVERSITY_ARCHIVE, None),
    (EV_FINALIZE_STEP, FINALIZE_STEP_DIVERSITY),
    (EV_FINALIZE_STEP, FINALIZE_STEP_CASE),
    (EV_FINALIZE_STEP, FINALIZE_STEP_REFLECTION_BEGUN),
    (EV_FINALIZE_STEP, FINALIZE_STEP_REFLECTION),
    (EV_FINALIZE_STEP, FINALIZE_STEP_LLM_COST),
    (EV_FINALIZATION_FINISHED, None),
    (EV_FINALIZE_STEP, FINALIZE_STEP_COMPLETE),
)

__all__ = [
    "FINALIZE_BUDGET_FIELDS",
    "FINALIZE_STEP_ABANDONED",
    "FINALIZE_STEP_BEGUN",
    "FINALIZE_STEP_BUDGET",
    "FINALIZE_STEP_CASE",
    "FINALIZE_STEP_COMPLETE",
    "FINALIZE_STEP_DIVERSITY",
    "FINALIZE_STEP_LLM_COST",
    "FINALIZE_STEP_REFLECTION",
    "FINALIZE_STEP_REFLECTION_BEGUN",
    "FINALIZE_STEP_REPORT",
    "FINALIZE_STEP_REPORT_BEGUN",
    "QUIET_FINALIZATION_SUFFIX",
    "budget_receipt",
]
