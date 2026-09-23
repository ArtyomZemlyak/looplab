"""The shared skeleton of every live cross-run context builder (doc 25 EC-01).

Two builders — the Strategist's observation note (`engine/strategy.py`) and the Researcher's
advisory context pack (`engine/proposal_cues.py`) — ran the same ~190-line pipeline in parallel and
differed only in the middle: an atlas summary versus a rendered context pack. Everything around that
middle was duplicated verbatim, including the parts that are easiest to get subtly wrong and hardest
to notice: the governance re-entry idiom, the row-scoping predicate, and the shape of the v2 audit
receipt.

The receipt is the reason this matters more than the line count. It is what an auditor reads to
decide whether "no cross-run evidence opposed this" meant *nothing opposed it* or *the store could
not be read*. Two independently maintained copies of that schema drift silently — one gains a field,
one changes a bound — and the audit trail stops being comparable across the two agents that shape
the same run.

Everything here is pure or read-only. No LLM, no writes, no engine state; each helper takes what it
needs and returns a value, so both builders keep their own `self` and their own receipt attribute.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Optional

from looplab.engine.memory_stores import governed_source_names
from looplab.trust.cross_run import sanitize_cross_run_projection

# The three governed stores every live cross-run projection reads. Kept here because
# `project_governed_sources` takes them as a tuple and a caller that lists two of the three gets a
# projection governed by a partial ledger — with no error, just quietly weaker guarantees.
# DERIVED from the store registry's `governed_source` flag (review 2026-09-22, ENG3-07), the same flag
# `governance_health._GOVERNED_SOURCE_NAMES` reads, so the list the builders pass and the list the
# ledger accepts can no longer be two hand-kept copies of one trio.
CROSS_RUN_SOURCE_NAMES = governed_source_names()


def advisory_enabled(engine) -> bool:
    """Both builders are off unless `cross_run_advisory` is set AND a memory dir exists.

    The parameter is spelled `engine` because that is the name `tests/test_engine_knob_defaults.py`
    reads an engine handle by: as `owner`, these two knob reads were invisible to the guard that holds
    every knob default to the value a real Engine settles it to (review 2026-09-22, ENG1-03)."""
    return (bool(getattr(engine, "_cross_run_advisory", False))
            and bool(getattr(engine, "memory_dir", None)))


def enter_governed(base: Path, reenter: Callable[[dict], str]) -> str:
    """The governance re-entry idiom: resolve the ledger once, then call back into the builder.

    Written out twice before, and the failure mode of getting it wrong is invisible — a builder that
    forgets `include_concepts` or drops a source name still returns text, just governed by a ledger
    that never saw one of the stores it is projecting.

    This is the fixed-name case of `governance_protocol.governed_projection` (doc 25 EM-08): both
    builders load all three stores themselves every time, so there is nothing for the shared helper's
    `unsupplied` derivation to decide — the trio is a constant. It goes THROUGH the helper anyway so
    the re-entry itself has one implementation across all six governed projections.
    """
    from looplab.engine.governance_protocol import governed_projection

    return governed_projection(
        base, reenter, include_concepts=True, source_names=CROSS_RUN_SOURCE_NAMES)


def load_governed_sources(base: Path) -> tuple[list, list, list]:
    """Read the three stores under the resolved ledger: `(lessons, capsules, research)`.

    The capsule store is guarded by `observed_path_missing` rather than by existence: a path the
    governance ledger reports as OBSERVED-missing is a known empty, while an unobserved one is an
    unknown that must not be read as empty.
    """
    from looplab.engine.claims import load_claim_lessons, load_research_claims
    from looplab.engine.governance_health import observed_path_missing
    from looplab.engine.memory import ConceptCapsuleStore

    capsule_path = base / "concept_capsules.jsonl"
    capsules = ConceptCapsuleStore(capsule_path).all() if not observed_path_missing(
        capsule_path) else []
    return load_claim_lessons(base), capsules, load_research_claims(base)


def corpus_digest(projection: Any, *, max_chars: int, max_items: int,
                  max_total_items: int) -> str:
    """Digest the BOUNDED projection, never the raw store.

    A raw hash over the underlying rows is both a credential oracle and an identity for bytes the
    model was never shown; the digest has to identify what was actually rendered. The bounds differ
    per builder (a note is far smaller than a context pack), so they are arguments rather than
    constants — but the sanitize-then-canonical-JSON pipeline is one implementation.
    """
    sanitized = sanitize_cross_run_projection(
        projection, max_chars=max_chars, max_items=max_items, max_total_items=max_total_items)
    encoded = json.dumps(sanitized, ensure_ascii=False, sort_keys=True, default=str,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_receipt(*, scope_task: str, excluded_run: str, lessons: list, capsules: list,
                  research: list, scope_key: str, scope_value: Any, claim_source: Any,
                  corpus: str, rendered: str) -> dict:
    """The v2 cross-run audit receipt, in ONE place.

    `scope_key`/`scope_value` carry the one field that legitimately differs — the Strategist reports
    a concept SOURCE receipt, the Researcher a concept SCOPE receipt — and it is passed positionally
    into the middle of the dict rather than merged at the end so the key order matches what both
    builders emitted before.
    """
    from looplab.trust.cross_run import cross_run_text

    def _identity(value: str) -> str:
        return cross_run_text(value, max_chars=500, single_line=True, entropy=False)

    return {
        "v": 2,
        "scope_task": _identity(scope_task),
        "excluded_run": _identity(excluded_run),
        "n_lessons": len(lessons), "n_capsules": len(capsules), "n_research": len(research),
        scope_key: scope_value,
        "claim_source": claim_source,
        "corpus_digest": corpus,
        "render_digest": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
    }


def unavailable_receipt(exc) -> dict:
    """Suppressing untrusted policy is safe; erasing its health state is not.

    A closed, content-free receipt is what lets an audit tell "the ledger was unavailable" apart
    from "the store was empty" and from "the feature was off" — three states that all render as the
    empty string to the agent.
    """
    return {"v": 2, "status": "unavailable", "complete": False, "governance": exc.public_receipt()}


def visible_row_predicate(current_direction, *, task_id: str, excluded_run: str,
                          excluded_run_uid: str) -> Callable[[dict], bool]:
    """Row scoping shared by the Strategist note: same live direction, exact task, not this run.

    Direction is checked through `same_live_direction` rather than `==` because a persisted row with
    an invalid or absent direction cannot be interpreted against the current objective at all — it
    is unknown, not merely different.

    "Not this run" is an INCARNATION, not a directory name (review 2026-09-22, ENG3-01): it is
    `trust/cross_run.py::LessonScope.is_current_run`, the predicate the bound agent tools already
    apply to the same stores. Compared on `run_id` alone, a row written by ANOTHER run root that
    happens to share this run's directory name (`demo`, `run_local`, a deleted-and-recreated run)
    was hidden from both live builders while the agent's own `cross_run_*` tools showed it. The
    uid is a REQUIRED keyword so a caller cannot fall back to the name-only rule by omission; an
    empty uid (a legacy run) keeps the name rule, exactly as `is_current_run` does. `excluded_run`
    stays the display name the receipt records.
    """
    from looplab.trust.cross_run import LessonScope, same_live_direction

    current = LessonScope(bound=True, run_uid=str(excluded_run_uid or ""),
                          run_id=str(excluded_run or ""))

    def visible(row: dict) -> bool:
        return (same_live_direction(current_direction, row.get("direction"))
                and bool(task_id) and str(row.get("task_id") or "") == task_id
                and not current.is_current_run(row))

    return visible


def scoped_identity(state, *, default: str = "") -> tuple[str, str]:
    """`(run_id, task_id)` as bounded strings, tolerating a missing state."""
    if state is None:
        return default, default
    return (str(getattr(state, "run_id", "") or default),
            str(getattr(state, "task_id", "") or default))


def empty_after_complete_read(lessons: list, capsules: list, research: list,
                              capsule_source: Optional[dict],
                              claim_source: Optional[dict]) -> bool:
    """True only when every store was read COMPLETELY and all three came back empty.

    The asymmetry is the point: a genuinely empty portfolio yields no guidance, but an incomplete
    read that happens to retain nothing must NOT — that would present an unknown as an authoritative
    absence, which is the one thing these receipts exist to prevent.
    """
    if lessons or capsules or research:
        return False
    return (isinstance(capsule_source, dict) and capsule_source.get("source_complete") is True
            and isinstance(claim_source, dict) and claim_source.get("source_complete") is True)
