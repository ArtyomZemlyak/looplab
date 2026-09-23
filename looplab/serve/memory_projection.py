"""The Memory panel's READ MODEL: `GET /api/memory`'s bounded, allow-listed projection of the three
cross-run memory tiers (`cases` / `lessons` / `notes`) and the concept shelf published beside them.

Review 2026-09-22, SRV2-13 (doc 50 SR-04). This lived in `serve/routers/misc.py`, the grab-bag
router, as module helpers plus a route body, so the only way to drive the projection was to build
the ASGI app. The bodies are verbatim moves; the one edit is the route body becoming
`memory_view(srv, run_id)`, with `srv` threaded explicitly where the closure captured it. The route
keeps its declaration and its ORDER comment (it must be registered before the authoring catch-all
`GET /api/{kind}`), and nothing else.

THE BOUNDS ARE THIS MODULE'S. `_MEMORY_TIER_LIMIT`, `_MEMORY_SOURCE_BYTES`, `_MEMORY_SOURCE_ROWS`,
`_MEMORY_ROW_BYTES` and `_MEMORY_EVIDENCE_MAX` are module globals read at CALL time, so a test
narrows them by patching THIS module (`tests/test_memory_endpoint.py`). The router imports
`memory_view` and no bound, so there is no second binding for a patch to miss.

A pure read side: it writes no store, calls no model and takes no lock. The source window is
`core/memory_window.py`'s (the one bounded-tail rule the agents' own readers share), the redaction
`core/redact.py`'s, and the concept join `engine/concept_shelf.py`'s.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Optional

from looplab.core.memory_window import (
    MEMORY_SOURCE_BYTES, MEMORY_SOURCE_ROW_BYTES, MEMORY_SOURCE_ROWS,
    read_memory_jsonl_window,
)
from looplab.core.redact import bounded_redacted_tree, redact_persisted_text
from looplab.engine.concept_shelf import bounded_row_concepts, build_shelf, run_concept_index


_MEMORY_TIER_LIMIT = 200
_MEMORY_SOURCE_BYTES = MEMORY_SOURCE_BYTES
_MEMORY_SOURCE_ROWS = MEMORY_SOURCE_ROWS
_MEMORY_ROW_BYTES = MEMORY_SOURCE_ROW_BYTES


def _memory_text(value, maximum: int, *, entropy: bool = True) -> str:
    if not isinstance(value, (str, int, float, bool)):
        return ""
    return " ".join(redact_persisted_text(
        value, max_chars=maximum, entropy=entropy, single_line=True).split())


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


# See the genesis sibling for why the character cell is derived from the node budget rather than
# chosen: this projection only ever meant to bound NODES.
_MEMORY_NODES = 96
_MEMORY_STR_CAP = 500


def _bounded_json_value(value):
    """Bound one case param tree by depth, fanout and a shared scalar-item budget.

    The walk is `core/redact.py::bounded_redacted_tree`, shared with the span/trace sanitizer, the
    advisory payloads and the genesis evidence projection (doc 25 SR-06). Masking a secret-NAMED key
    is why sharing matters here: `redact_persisted_text`'s entropy pass only catches high-entropy
    strings, so a short wordlike credential like `{"api_key": "tok-abcd"}` would otherwise reach the
    memory-browser response intact — and the classification happens on the ORIGINAL key (8d1bcda),
    which is a rule that had to hold in every copy and did not.
    """
    truncated = [False]
    projected = bounded_redacted_tree(
        value, [_MEMORY_NODES * _MEMORY_STR_CAP], [_MEMORY_NODES],
        max_items=32, max_depth=2, str_cap=_MEMORY_STR_CAP, key_cap=80,
        truncated=truncated)
    return projected, truncated[0]


_MEMORY_EVIDENCE_MAX = 32
# `engine/lessons_reconcile.py::_evidence_sig_map` writes `evidence_sig` as `v2:a=<attempt>:t=<0|1>:x=<0|1>:...`.
# Only the ATTEMPT is republished: the tombstone/abort flags and the outcome are a staleness fence the
# engine re-derives for itself, and mirroring them onto the wire would invite a client to re-implement
# `_lesson_evidence_stale` from a projection that is one field short of it.
_EVIDENCE_SIG_ATTEMPT = re.compile(r"\Av[0-9]+:a=(\d{1,9}):")


def _lesson_evidence_refs(row: dict) -> tuple[list[int], dict[str, int]]:
    """The node ids a lesson row credits, and each one's node ATTEMPT where the row records it.

    Returned separately rather than as one list of pairs because they have different completeness:
    every distilling writer sets `evidence`, but a row written before `evidence_sig` — or one that
    survived a consolidation that took its base from another run — can carry ids with no attempt at
    all. A consumer must be able to tell "node 7, attempt 0" from "node 7, attempt unrecorded", and a
    pair list with a null half invites reading the second as the first.
    """
    evidence: list[int] = []
    for value in row.get("evidence") or []:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        if value not in evidence:
            evidence.append(value)
        if len(evidence) >= _MEMORY_EVIDENCE_MAX:
            break
    generations: dict[str, int] = {}
    signatures = row.get("evidence_sig")
    if isinstance(signatures, dict):
        for node_id in evidence:
            match = _EVIDENCE_SIG_ATTEMPT.match(str(signatures.get(str(node_id), "")))
            if match is not None:
                generations[str(node_id)] = int(match.group(1))
    return evidence, generations


def _project_memory_row(tier: str, row) -> Optional[dict]:
    if not isinstance(row, dict):
        return None
    if tier == "cases":
        task_id = _memory_text(row.get("task_id"), 500, entropy=False)
        if not task_id:
            return None
        params, params_truncated = _bounded_json_value(row.get("params", {}))
        out = {"task_id": task_id, "goal": _memory_text(row.get("goal"), 1000),
               "direction": row.get("direction") if row.get("direction") in ("min", "max") else "min",
               "metric": _finite_number(row.get("metric")), "params": params}
        rationale = _memory_text(row.get("rationale"), 1000)
        if rationale:
            out["rationale"] = rationale
        if params_truncated:
            out["params_truncated"] = True
        # A case is the only tier whose historical rows carry no `run_id` at all, so run-level
        # inheritance cannot reach them; carrying it forward is what lets a NEW case be attributed.
        run_id = _memory_text(row.get("run_id"), 500, entropy=False)
        if run_id:
            out["run_id"] = run_id
        _carry_row_concepts(row, out)
        return out
    if tier == "lessons":
        statement = _memory_text(row.get("statement"), 1000)
        if not statement:
            return None
        out = {"statement": statement}
        _carry_row_concepts(row, out)
        for key, maximum in (("run_id", 500), ("run_uid", 500), ("task_id", 500), ("role", 40),
                             ("kind", 80), ("outcome", 48), ("claim_stance", 24)):
            value = _memory_text(row.get(key), maximum, entropy=key not in ("run_id", "task_id"))
            if value:
                out[key] = value
        for key in ("delta", "confidence"):
            value = _finite_number(row.get(key))
            if value is not None:
                out[key] = value
        evidence_count = row.get("evidence_count")
        if isinstance(evidence_count, int) and not isinstance(evidence_count, bool):
            out["evidence_count"] = max(0, evidence_count)
        for key in ("evidence_traceable_count", "evidence_untraceable_count"):
            value = row.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                out[key] = value
        lineage = []
        for ref in row.get("evidence_refs") or []:
            if not isinstance(ref, dict) or len(lineage) >= _MEMORY_EVIDENCE_MAX:
                continue
            node_id = ref.get("node_id")
            if isinstance(node_id, bool) or not isinstance(node_id, int) or node_id < 0:
                continue
            run_id = _memory_text(ref.get("run_id"), 500, entropy=False)
            run_uid = _memory_text(ref.get("run_uid"), 500, entropy=False)
            if not (run_id or run_uid):
                continue
            item = {"node_id": node_id}
            if run_id:
                item["run_id"] = run_id
            if run_uid:
                item["run_uid"] = run_uid
            generation = ref.get("generation")
            if isinstance(generation, int) and not isinstance(generation, bool) and generation >= 0:
                item["generation"] = generation
            lineage.append(item)
        if lineage:
            out["evidence_refs"] = lineage
        # The node-level provenance a lesson row genuinely carries, restored to the wire. It was
        # dropped here while `evidence_count` (a scalar the CONSOLIDATION pass writes) survived, which
        # left the UI able to say "3 experiments supported this" and unable to say WHICH — so no
        # surface could answer "what did this experiment teach us". `evidence` is the writers' own
        # field (`engine/lessons_distill.py::skill_source_digest`, `engine/lessons_reconcile.py::_evidence_sig_map`: "`evidence`
        # [child, parent] IS the credited pair"), and `evidence_sig` binds each id to the exact node
        # ATTEMPT it was distilled against — which is what stops a re-run node from inheriting a
        # lesson drawn from its previous life. Both are bounded identifiers, not prose, so neither
        # goes through the entropy redactor.
        evidence, generations = _lesson_evidence_refs(row)
        if evidence:
            out["evidence"] = evidence
            if generations:
                out["evidence_generations"] = generations
        return out
    note = _memory_text(row.get("note") or row.get("statement"), 4000)
    if not note:
        return None
    out = {"note": note}
    for key, maximum in (("run_id", 500), ("task_id", 500), ("at", 120)):
        value = _memory_text(row.get(key), maximum, entropy=key not in ("run_id", "task_id", "at"))
        if value:
            out[key] = value
    _carry_row_concepts(row, out)
    return out


def _carry_row_concepts(row: dict, out: dict) -> None:
    """Carry a row's DURABLE `concepts` field into its projection, healed and bounded.

    Deliberately not routed through `_memory_text`: a concept id is a closed-vocabulary identifier, not
    prose, and `bounded_row_concepts` already refuses anything outside `normalize_concept_id`'s grammar
    — so the redactor's entropy pass would only ever mangle a legitimate id (a long
    `deployment/end-to-end` path reads as high-entropy) while adding no protection a stricter grammar
    has not already given. The field is ABSENT when the row has none, so run-level inheritance in
    `build_shelf` can still claim the row; writing `[]` here would pin it as durably-untagged.
    """
    concepts = bounded_row_concepts(row.get("concepts"))
    if concepts:
        out["concepts"] = concepts


def _read_memory_tier(path: Path, tier: str, run_id: str = "") -> tuple[list[dict], dict]:
    """Project one memory tier's bounded recent tail, optionally narrowed to ONE run.

    `run_id` filters on the row's own durable `run_id` and is applied INSIDE the source-window scan,
    before the `_MEMORY_TIER_LIMIT` cap. That ordering is the whole point: filtering the already-capped
    200-row result would silently answer "this run contributed nothing" whenever 200 newer rows from
    other runs sat in front of it. Rows excluded by the filter are counted as `filtered`, NOT as
    `skipped` — `skipped` means a row was unreadable, and a caller reasoning about store health must
    not have deliberate narrowing folded into that number. The window receipt is unchanged and still
    load-bearing: `source_window_truncated` means older rows for this run were never read, so an empty
    filtered result is "not in the recent tail", never "does not exist".
    """
    receipt = {"limit": _MEMORY_TIER_LIMIT, "returned": 0, "skipped": 0, "filtered": 0,
               "superseded": 0,
               "source_window_truncated": False, "unavailable": False,
               "source_rows": 0, "source_size": 0, "window_digest": ""}
    decoded, source = read_memory_jsonl_window(
        path, max_bytes=_MEMORY_SOURCE_BYTES, max_rows=_MEMORY_SOURCE_ROWS,
        max_row_bytes=_MEMORY_ROW_BYTES)
    for key in ("source_window_truncated", "unavailable", "source_rows", "source_size",
                "window_digest", "skipped"):
        receipt[key] = source[key]
    projected = []
    for _index, row in decoded:
        if tier == "cases" and isinstance(row, dict) and row.get("active") is False:
            receipt["superseded"] += 1
            continue
        safe = _project_memory_row(tier, row)
        if safe is None:
            receipt["skipped"] += 1
            continue
        if run_id and safe.get("run_id") != run_id:
            receipt["filtered"] += 1
            continue
        projected.append(safe)
    if len(projected) > _MEMORY_TIER_LIMIT:
        projected = projected[-_MEMORY_TIER_LIMIT:]
        receipt["source_window_truncated"] = True
    receipt["returned"] = len(projected)
    return projected, receipt


def memory_view(srv, run_id: str = "") -> dict:
    """`GET /api/memory`'s whole payload: the three tiers, a receipt per tier, the concept shelf.

    `srv` supplies exactly two things: `global_settings()` (for `memory_dir`) and
    `run_summaries(only=...)` (the concept index's run list, bounded to the runs the rows cite).
    `run_id` narrows all three tiers INSIDE the source-window scan (`_read_memory_tier`); the empty
    string is the whole-store projection.
    """
    s = srv.global_settings()
    out = {"dir": None, "cases": [], "lessons": [], "notes": []}
    if not s.memory_dir:
        return out
    md = Path(s.memory_dir)
    out["dir"] = str(md)
    receipts = {}
    # allow-list only the three UI tiers. Every tier gets an independent bounded
    # recent source window and result cap; governance/capsule ledgers are not accidental "cases".
    for tier, filename in (("cases", "cases.jsonl"), ("lessons", "lessons.jsonl"),
                           ("notes", "meta_notes.jsonl")):
        out[tier], receipts[tier] = _read_memory_tier(md / filename, tier, run_id=run_id)
    # The concept SHELF: stamp every row with its concepts + attribution source, and publish the
    # tree plus the coverage receipt beside them. This is a read projection — it never writes back,
    # and it never invents a concept — so a tier whose rows predate the durable field simply reads
    # as untagged, which is the fact the UI has to show rather than an empty filter result.
    # Run-level inheritance needs the run list; when it is unavailable the shelf still ships with
    # whatever DURABLE tags the rows carry, and `runs_indexed: 0` says why the rest went untagged.
    # Bounded to the runs the rows THEMSELVES cite. Unbounded, this folded every run in the
    # workspace on the request thread — the whole portfolio on a cold cache, and every live run
    # on every open of the panel (the summary cache is keyed on `file_identity`, which changes
    # on each append). The shelf only ever looks a row's own `run_id` up, so a run no row
    # mentions contributes nothing to `concept_index` whether it was folded or not.
    cited = {row.get("run_id") for tier in ("cases", "lessons", "notes") for row in out[tier]
             if isinstance(row, dict) and isinstance(row.get("run_id"), str) and row.get("run_id")}
    concept_index_available = True
    try:
        index = run_concept_index(srv.run_summaries(only=cited))
    except Exception:  # noqa: BLE001 — a half-written run must not empty the Memory panel
        index = {}
        concept_index_available = False
    out["concept_index"] = build_shelf(
        {tier: out[tier] for tier in ("cases", "lessons", "notes")}, index)
    out["projection"] = "bounded_recent_tail"
    out["concept_index_available"] = concept_index_available
    out["page"] = {"tiers": receipts,
                   "truncated": any(row["source_window_truncated"] for row in receipts.values()),
                   "unavailable": any(row["unavailable"] for row in receipts.values()),
                   "partial": (not concept_index_available or any(
                       row["source_window_truncated"] or row["skipped"] or row["unavailable"]
                       for row in receipts.values()))}
    return out
