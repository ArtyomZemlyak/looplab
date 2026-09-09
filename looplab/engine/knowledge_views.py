"""The PUBLIC cross-run knowledge read model — the one import site for `tools/`, `cli/` and
`serve/` (doc 25 XP-01 / TO-09, the shape §6.6 prescribes).

Until 2026-09-08 the cross-run tools, the governance CLI and the portfolio routes reached into the
engine by its PRIVATE names: `_capsule_rows`, `_dedup_valid_capsules`, `_claim_source_rows`,
`_filter_claim_assessments`, `_portfolio_concept_overview_data`, `_TOMBSTONE` and six more —
underscore names of a lower-layer package, imported FUNCTION-LOCALLY (deliberately, to keep the
import graph acyclic) from a package that sits ABOVE them. That combination is the silent-rename
failure class the registries in CLAUDE.md exist to prevent: an underscore normally licenses the
owning module to rename freely, nothing fails at import time because the imports are lazy, and
`CrossRunTools.execute` swallows the resulting ImportError into the generic "(cross-run tool
unavailable)" string — so the affected tools simply stop answering.

Two things changed together, and neither is sufficient alone:

* the thirteen read-model functions and the purge sentinel are now PUBLIC in the modules that own
  them (`concept_capsules.py`, `claims_health.py`, `concept_registry.py`) — the underscore is gone,
  so the freedom it was claiming (rename at will) is no longer being claimed by names four packages
  depend on;
* this module is the SURFACE those consumers import, so an engine-internal reshuffle — `memory.py`
  splitting again, a view moving between `claims_health` and `claims` — is one edit here instead of
  an edit in every consumer package. `tests/test_knowledge_views.py` pins it BOTH ways.

Deliberately NOT a facade with wrappers: every name below IS the owning module's object, so there
is exactly ONE implementation and one docstring per view, and `looplab.engine.memory` /
`looplab.engine.claims` keep re-exporting the same objects for their in-package callers.

What this module is NOT: it is not `read_models.py` — `events/readmodel.py` already owns that term
for the per-run SQLite artifact — and it grants no WRITE surface. Cross-run truth is written by the
engine (facts) or ratified by the operator (§22.4); everything here reads.
"""
from __future__ import annotations

from looplab.engine.claims_health import (  # noqa: F401 — the claim/lesson evidence views
    claim_source_rows,
    filter_claim_assessments,
    filter_claim_source_rows,
    load_claim_source_path,
    safe_claim_source_summary,
    safe_research_source_summary,
)
from looplab.engine.concept_capsules import (  # noqa: F401 — the capsule / portfolio views
    capsule_completeness,
    capsule_fingerprint_scope_complete,
    capsule_rows,
    capsule_source_summary,
    dedup_valid_capsules,
    filter_capsule_rows,
    portfolio_concept_overview_data,
)
from looplab.engine.concept_registry import CONCEPT_TOMBSTONE  # noqa: F401 — the purge sentinel

#: THE DECLARED SURFACE, in one place because that is the whole point of the module: a consumer
#: outside `engine/` reads the cross-run stores through exactly these names, and
#: `tests/test_knowledge_views.py` checks both directions (every name resolves to the owning
#: module's object; no consumer outside `engine/` imports the read model any other way).
KNOWLEDGE_VIEWS: tuple[str, ...] = (
    "CONCEPT_TOMBSTONE",
    "capsule_completeness",
    "capsule_fingerprint_scope_complete",
    "capsule_rows",
    "capsule_source_summary",
    "claim_source_rows",
    "dedup_valid_capsules",
    "filter_capsule_rows",
    "filter_claim_assessments",
    "filter_claim_source_rows",
    "load_claim_source_path",
    "portfolio_concept_overview_data",
    "safe_claim_source_summary",
    "safe_research_source_summary",
)

__all__ = list(KNOWLEDGE_VIEWS)
