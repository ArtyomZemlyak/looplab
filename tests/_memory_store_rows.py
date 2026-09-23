"""A row for EVERY store `engine/memory_stores.py::MEMORY_STORES` registers, for the drives that have
to cover all of them (review 2026-09-22, ENG3-07).

The defect those drives exist for was a store in NO deletion tier: the cascade and the orphan sweep
walked a hand-kept five-store list, so `lesson_utility.jsonl` and `regime_contrast.jsonl` kept a
deleted run's rows steering later runs. A drive that plants rows in a hand-kept list of its own would
repeat that exact failure one level down, so the cascaded rows here are keyed BY the registry and
`CASCADED_ROWS` is held equal to its cascaded set — a store that becomes cascaded without a row here
fails the drive instead of being silently skipped by it.

Each cascaded row is the shape its real writer emits, chosen so the store's keep-predicate
(`serve/memory_cascade.py`) DELETES it when the row names the deleted run: a lesson nothing was
consolidated into, a capsule whose concepts no curation merged, a claim in a pool no other run
curated, an unseeded regime row.
"""
from __future__ import annotations

from pathlib import Path

import orjson

from looplab.engine.memory_stores import CASCADED, MEMORY_STORES, PRESERVED


def _identity(run_id: str, run_uid: str) -> dict:
    return {"run_id": run_id, **({"run_uid": run_uid} if run_uid else {})}


# name -> (run_id, run_uid) -> row
CASCADED_ROWS = {
    "lessons.jsonl": lambda rid, uid: {
        **_identity(rid, uid), "task_id": "t", "statement": f"{rid} alone", "outcome": "won"},
    "meta_notes.jsonl": lambda rid, uid: {
        **_identity(rid, uid), "task_id": "t", "note": "finished", "direction": "max"},
    "cases.jsonl": lambda rid, uid: {
        **_identity(rid, uid), "task_id": "t", "direction": "max", "goal": "g", "metric": 0.5},
    "research_claims.jsonl": lambda rid, uid: {
        **_identity(rid, uid), "task_id": "t", "record_kind": "source_receipt"},
    "concept_capsules.jsonl": lambda rid, uid: {
        **_identity(rid, uid), "task_id": "t", "concepts": ["loss/contrastive"]},
    # `events/prior_citations.py::utility_rows` is the writer: one row per lesson a run's prior
    # showed. "Shown 8, cited 0" is the row that makes `filter_useless` forget a lesson.
    "lesson_utility.jsonl": lambda rid, uid: {
        "lesson_id": "les-0123456789ab", "run_id": rid, "run_uid": uid, "shown": 8, "cited": 0,
        "ts": 1.0},
    # `engine/regime_contrast.py::run_contrast` plus the stamp `write_reflection_note` appends.
    "regime_contrast.jsonl": lambda rid, uid: {
        "regimes": {"plain": {"n": 3, "median": 1.0, "max": 1.2, "min": 0.8}}, "nodes": 3,
        "task_id": "t", "direction": "max", "run_id": rid, "run_uid": uid, "finish_seq": 7},
}


def cascaded_names() -> set[str]:
    return {store.name for store in MEMORY_STORES if store.policy == CASCADED}


def preserved_stores() -> list:
    return [store for store in MEMORY_STORES if store.policy == PRESERVED]


def rows_of(memory: Path, name: str) -> list[dict]:
    return [orjson.loads(line) for line in (memory / name).read_bytes().splitlines()
            if line.strip()]


def snapshot_preserved(memory: Path) -> dict:
    """Every byte of every preserved store, so "never touched" is compared, not assumed."""
    out = {}
    for store in preserved_stores():
        path = memory / store.name
        if path.is_dir():
            out[store.name] = {p.relative_to(path).as_posix(): p.read_bytes()
                               for p in sorted(path.rglob("*")) if p.is_file()}
        elif path.exists():
            out[store.name] = path.read_bytes()
    return out


def plant_every_store(memory: Path, *, run_id: str, run_uid: str,
                      survivor_id: str, survivor_uid: str) -> None:
    """One row of `run_id` and one of the survivor in every CASCADED store, and content in every
    PRESERVED one — naming `run_id` wherever that store's rows name a run at all."""
    assert set(CASCADED_ROWS) == cascaded_names(), (
        "a cascaded store with no row here would be silently skipped by every drive that uses this "
        "helper — add its writer's row shape to CASCADED_ROWS")
    for name, row in CASCADED_ROWS.items():
        (memory / name).write_bytes(
            orjson.dumps(row(run_id, run_uid)) + b"\n"
            + orjson.dumps(row(survivor_id, survivor_uid)) + b"\n")
    for store in preserved_stores():
        path = memory / store.name
        if not Path(store.name).suffix:            # a directory store: skills/, .curation_invocations/
            path.mkdir(exist_ok=True)
            (path / "entry.md").write_text(f"planted for {run_id}\n", encoding="utf-8")
        elif store.name.endswith(".jsonl"):
            row = ({**_identity(run_id, run_uid), "task_id": "t", "proposals": {}}
                   if store.names_run else {"task_id": "t", "by": "operator"})
            path.write_bytes(orjson.dumps(row) + b"\n")
        else:
            path.write_text("{}", encoding="utf-8")
