"""The CONCEPT SHELF — cross-run knowledge (lessons, cases, notes) indexed by the concept tree.

Concepts were a per-run affair: `node_concepts` folds into `RunState`, `serve/concept_frame.py`
projects one run's tree, and `concept_capsules.jsonl` carries one capsule per run. Everything ELSE the
runs learned — `lessons.jsonl`, `cases.jsonl`, `meta_notes.jsonl` — carried a `task_id` and (usually) a
`run_id` and nothing else, so there was no way to ask "what has this lab learned about
`loss/contrastive`" or to sort the Memory panel by the concept tree.

This module is the join. It is deliberately PURE (no I/O, no LLM) and lives beside its sibling
`concept_capsules.py` because cross-run memory is the engine's, not serve's — `serve/routers/misc.py`
calls in, never the reverse.

Two things it fixes, and one it refuses to paper over:

1. **Attribution has a provenance, and it is on the wire.** A row's concepts come either from the row
   itself (`ATTRIBUTION_RECORD` — written at distillation time, when the run's membership was in scope)
   or from its run (`ATTRIBUTION_RUN` — the whole run's folded concepts, inherited through `run_id`).
   These are NOT the same claim: the first says "this lesson is about `loss/contrastive`", the second
   only says "the run that produced this lesson touched `loss/contrastive`". A reader that cannot tell
   them apart will over-trust the second. So the source travels with the concepts.

2. **Coverage is a receipt, not a vibe.** `shelf_coverage` states how many rows in each tier could be
   attributed at all. This is the whole reason the module exists in this shape: on a real portfolio most
   rows predate the durable field and most runs were never tagged, so a concept filter WOULD silently
   return nothing. Silence reads as "no such knowledge" when the truth is "this knowledge was never
   tagged". Every surface that filters by concept must render the receipt beside the result.

3. **It does not invent concepts.** No keyword matching, no embedding nearest-neighbour, no
   task_id->concept guess. An untagged row stays untagged and is counted as such. Deriving a concept
   from a lesson's TEXT is a tagger's job (`search/concept_graph.py::tag_text_llm`), it costs a provider
   call, and its output belongs in the durable field via the write path — not in a read projection that
   would silently re-derive a different answer on every request.

SPELLING. Ids here are `core/concepts.py::normalize_concept_id`-canonical — the same spelling the
per-run ConceptFrame, `events/digest.py::concept_rollup` and `search/concept_graph.py::
project_hierarchy` use. That is deliberately NOT the capsule's spelling: `concept_capsules.jsonl` keys
by `concept_registry.normalize_key` (space-preserving), documented at length in
`engine/lessons.py::store_concept_capsule`. Both choices are right for their reader. The capsule's
reader is the portfolio overview, which joins capsules to capsules. THIS module's reader is an operator
who clicked a concept in the run's concept tree and expects the Memory panel to answer for the SAME id
— so a spelling that agreed with the capsule but disagreed with the tree would break the only
navigation this module exists to provide. Governance aliases/splits are likewise NOT applied here, for
the same agreement reason: the per-run frame applies only the run's own `concept_consolidation`, so
applying portfolio governance here would make the shelf disagree with the tree the operator clicked.
"""
from __future__ import annotations

from typing import Iterable, Optional

from looplab.core.concepts import MAX_MATERIALIZED_CONCEPTS, normalize_concept_id


# The attribution vocabulary is a REGISTRY, not a set of bare strings at the call sites: it reaches the
# HTTP wire and the UI renders a different affordance per value, so a typo would silently degrade a
# record-level claim into an unlabelled one. `tests/test_concept_shelf.py` scans this module and
# `serve/routers/misc.py` for members, the same two-way guard the duck-typed seams in CLAUDE.md use.
ATTRIBUTION_RECORD = "record"
ATTRIBUTION_RUN = "run"
ATTRIBUTION_SOURCES = (ATTRIBUTION_RECORD, ATTRIBUTION_RUN)

# Per-ROW cap. A row is a distilled sentence; 64 concepts on one is already a tagger malfunction, and
# the bound matches the ConceptFrame's per-node cap so one surface cannot show what the other drops.
MAX_ROW_CONCEPTS = MAX_MATERIALIZED_CONCEPTS
# Whole-shelf cap on DISTINCT ids. The tree materializes every ancestor prefix, so the node count is
# super-linear in this; 512 leaves the projection comfortably inside the ConceptFrame's 4096 tree cap.
MAX_SHELF_CONCEPTS = 512


def bounded_row_concepts(raw, *, limit: int = MAX_ROW_CONCEPTS) -> list[str]:
    """One row's durable `concepts` field, healed into bounded canonical ids.

    Untrusted by construction: the field is written by an engine that may be older or newer than this
    reader, and `read_jsonl_lenient` hands over whatever the line contained. A malformed id is DROPPED,
    never coerced — `str()` would launder `7`/`None` into the perfectly valid ids `'7'`/`'None'` and
    publish them as cross-run evidence, the same rule `store_concept_capsule` follows.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out: set[str] = set()
    for value in raw:
        canonical = normalize_concept_id(value)
        if canonical:
            out.add(canonical)
    return sorted(out)[:max(0, int(limit))]


def state_concepts(state, node_ids=None, *, limit: int = MAX_ROW_CONCEPTS) -> list[str]:
    """The WRITE-side helper: the canonical concepts a run — or a chosen subset of its nodes — carries.

    This is what turns `ATTRIBUTION_RECORD` into a stronger claim than `ATTRIBUTION_RUN`. A lesson knows
    the node ids it was distilled from (`evidence`), and a case knows its winner, so both can record the
    concepts of the experiments they actually describe instead of everything the run happened to touch.
    Pass `node_ids=None` for a genuinely run-wide record (the meta-note, which summarizes the run).

    An EMPTY result is returned rather than a run-wide fallback when `node_ids` is given but none of
    those nodes is tagged: writing the run's whole set there would relabel a record-level claim as one,
    and the reader's `run` inheritance already covers that case honestly at read time.

    Never raises — a projection hiccup must not fail a run's finalization.
    """
    try:
        from looplab.events.digest import folded_concepts as _folded
        nodes = getattr(state, "nodes", None) or {}
        if node_ids is None:
            selected = list(nodes.values())
        else:
            selected = [nodes[nid] for nid in node_ids if nid in nodes]
        out: set[str] = set()
        for node in selected:
            out |= _folded(state, node)
        return sorted(out)[:max(0, int(limit))]
    except Exception:  # noqa: BLE001 — cross-run memory tagging is best-effort, never fails a run
        return []


def run_concept_index(summaries: Iterable[dict], *, limit: int = MAX_ROW_CONCEPTS) -> dict:
    """`{run_id: [concept ids]}` from the run-list summaries' `concepts` rollup.

    A run PRESENT with an empty list is a distinct fact from a run that is absent: present-and-empty
    means the run was folded and carries no concept membership (untagged), absent means the run is gone
    from the workspace entirely and nothing can be said about the rows that cite it. Callers that
    collapse the two report deleted runs' lessons as untagged, which is a stronger claim than they hold.
    """
    index: dict[str, list[str]] = {}
    for summary in summaries or ():
        if not isinstance(summary, dict):
            continue
        run_id = summary.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        rollup = summary.get("concepts")
        index[run_id] = bounded_row_concepts(
            list(rollup) if isinstance(rollup, dict) else rollup, limit=limit)
    return index


def attribute_row(row, run_index: Optional[dict] = None) -> tuple[list[str], Optional[str]]:
    """`(concepts, source)` for one memory row. `([], None)` when nothing can be attributed.

    The durable field WINS over the run inheritance even when it is shorter, and — critically — even
    when it is EMPTY-but-present is not expressible here: a row whose durable field healed to nothing
    falls through to its run. That asymmetry is deliberate and differs from `node_concepts`' "explicit
    empty entry is authoritative" rule, because a memory row has no operator-clears-the-tags gesture;
    an empty list on a row only ever means the writer had nothing, not that it decided on nothing.
    """
    if not isinstance(row, dict):
        return [], None
    direct = bounded_row_concepts(row.get("concepts"))
    if direct:
        return direct, ATTRIBUTION_RECORD
    run_id = row.get("run_id")
    if isinstance(run_id, str) and run_id:
        inherited = (run_index or {}).get(run_id)
        if inherited:
            return list(inherited), ATTRIBUTION_RUN
    return [], None


def attribute_rows(rows: Iterable[dict], run_index: Optional[dict] = None) -> list[dict]:
    """Stamp `concepts` + `concept_source` onto a tier's rows, in place, and return them.

    An unattributed row is left WITHOUT either key rather than carrying `concepts: []`. Absence is the
    honest wire shape for "not tagged": an empty list invites a client to render an empty chip row and
    read it as "tagged with nothing", and it makes `"concepts" in row` — the cheapest coverage test a
    client can write — answer the wrong question.
    """
    out = []
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        concepts, source = attribute_row(row, run_index)
        if concepts and source:
            row["concepts"] = concepts
            row["concept_source"] = source
        out.append(row)
    return out


def shelf_coverage(rows: Iterable[dict]) -> dict:
    """The receipt a concept filter must show beside its result: how much of this tier it can classify.

    `by_source` breaks the tagged count down so an operator can see when a tier is carried entirely by
    run-level inheritance — which is the weaker claim, and the one that makes a concept filter feel
    right while being coarse.
    """
    total = tagged = 0
    by_source = {source: 0 for source in ATTRIBUTION_SOURCES}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        total += 1
        source = row.get("concept_source")
        if source in by_source and row.get("concepts"):
            tagged += 1
            by_source[source] += 1
    return {"total": total, "tagged": tagged, "untagged": total - tagged, "by_source": by_source}


def shelf_tree(tiers: dict) -> dict:
    """The concept TREE over every attributed row across every tier, as a display axis.

    Returns `project_hierarchy`'s shape (roots + nodes with parent/depth/children/tagged) plus a
    `counts` map of DIRECTLY-attributed rows per id. Counts are not rolled up the tree here: a reader
    that wants a subtree total sums the descendants it is actually showing, and one that wants the
    direct count must not be handed an inherited one. `tagged` on a node keeps `project_hierarchy`'s
    meaning — the id was really attributed, as opposed to a synthetic ancestor materialized for shape.
    """
    counts: dict[str, int] = {}
    for rows in (tiers or {}).values():
        for row in rows or ():
            if not isinstance(row, dict):
                continue
            for concept in (row.get("concepts") or ()):
                if concept in counts:
                    counts[concept] += 1
                elif len(counts) < MAX_SHELF_CONCEPTS:
                    counts[concept] = 1
    # Deferred: `search` imports `agents` at module scope and this module is reached from the engine's
    # import graph, so the tree projector is pulled in at CALL time to keep the engine's module-level
    # dependency surface unchanged (CLAUDE.md layering).
    from looplab.search.concept_graph import project_hierarchy
    tree = project_hierarchy(sorted(counts))
    tree["counts"] = dict(sorted(counts.items()))
    return tree


def build_shelf(tiers: dict, run_index: Optional[dict] = None) -> dict:
    """Attribute every tier in place and return the shared `concept_index` envelope.

    `runs_indexed`/`runs_tagged` are the OTHER half of the honesty story `shelf_coverage` starts. A tier
    can read as thinly covered for two very different reasons — the rows predate the durable field, or
    the runs behind them were never concept-tagged at all — and only the second is fixed by tagging more
    runs. Reporting both lets the surface say which.
    """
    index = run_index or {}
    coverage = {}
    for tier, rows in (tiers or {}).items():
        coverage[tier] = shelf_coverage(attribute_rows(rows, index))
    return {
        "sources": list(ATTRIBUTION_SOURCES),
        "tree": shelf_tree(tiers),
        "coverage": coverage,
        "runs_indexed": len(index),
        "runs_tagged": sum(1 for ids in index.values() if ids),
    }
