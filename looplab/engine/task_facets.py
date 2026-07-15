"""AGENTIC task FACETS (§21.20.2) — the LLM faceting the deterministic passport deliberately does NOT do.

`scope_profile` (cross_run_index.py) is an honest, universal, DETERMINISTIC passport: task fingerprint +
goal terms, no hardcoded domain/language/modality buckets (that classification would be a guess). This
module adds the classification agentically: an LLM reads the task's goal/kind and proposes a small set of
FACETS (domain, language, modality, interaction, objective) that describe what KIND of problem it is.

Kept strictly OFF the deterministic index path (CR0 gate): facets live in their OWN append-only
`task_facets.jsonl` keyed by task_id, and `scope_profile` only carries them when EXPLICITLY passed — so
`build_index`/`rebuild_index_from_run_root` (which never pass facets) stay byte-identical rebuildable. The
facets are an advisory OVERLAY: used as an ADDITIONAL cross-task scope-match signal (two differently-named
retrieval tasks can now recognize each other) and surfaced to the operator; they never gate the fingerprint.

LLM proposes -> recorded via `record_task_facets`. Degrades to no facets on no client / any failure.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

# The controlled facet AXES — a small, fixed vocabulary so facets from different tasks are comparable. The
# VALUES are free-form (the LLM's short slug), but the axes are fixed (an unknown axis is dropped).
FACET_AXES = ("domain", "language", "modality", "interaction", "objective")


def propose_task_facets(goal: str, kind: str, client, *, parser: str = "tool_call") -> dict:
    """Ask an LLM to classify a task (its `goal` + `kind`) into the FACET_AXES — a short slug per axis
    (e.g. {domain: "information-retrieval", language: "russian", modality: "text", interaction: "pairwise",
    objective: "ranking"}). Returns `{axis: value}` for the axes the model filled (unknown axes dropped,
    values normalized/truncated). Empty on no client / any failure; never raises."""
    if client is None or not str(goal or "").strip():
        return {}
    try:
        from pydantic import BaseModel, Field

        from looplab.core.parse import parse_structured
        from looplab.engine.concept_registry import normalize_key

        class _Facets(BaseModel):
            domain: str = ""
            language: str = ""
            modality: str = ""
            interaction: str = ""
            objective: str = ""

        class _Out(BaseModel):
            facets: _Facets = Field(default_factory=_Facets)

        # PROMPT CONTRACT (CLAUDE.md): a compact classifier — ONE short slug per axis, "" when unknown. Fixed
        # axes so facets are comparable across tasks; the model must not invent axes (schema enforces it).
        system = (
            "You classify a machine-learning task into a small set of FACETS so the system can recognize "
            "when two differently-worded tasks are the same KIND of problem. Fill each axis with ONE short "
            "lowercase slug (hyphenated), or leave it \"\" if unknown/not-applicable:\n"
            "- domain: the problem area (e.g. information-retrieval, image-classification, tabular-regression)\n"
            "- language: natural language if text (e.g. russian, english, multilingual), else \"\"\n"
            "- modality: text / image / tabular / audio / graph / ...\n"
            "- interaction: pointwise / pairwise / listwise / generative / ... (how examples are scored)\n"
            "- objective: ranking / classification / regression / generation / ...\n"
            "Call `emit` ONCE with the `facets` object.")
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": f"KIND: {kind or '(unknown)'}\nGOAL: {goal}\n\nClassify it."}]
        out = parse_structured(client, msgs, _Out, parser)
        raw = out.facets.model_dump()
        facets = {ax: normalize_key(raw.get(ax))[:60] for ax in FACET_AXES if normalize_key(raw.get(ax))}
        return facets
    except Exception:  # noqa: BLE001 — agentic faceting is best-effort; never block the caller
        return {}


def record_task_facets(memory_dir, *, task_id: str, facets: dict, by: str = "steward", at: str = "") -> dict:
    """Persist a task's facets (append-only, last-write-wins per task_id) to `task_facets.jsonl`. Only
    known FACET_AXES with a non-empty value are kept. Returns the stored record. Raises on no task_id/dir."""
    from looplab.engine.concept_registry import _append_governance, normalize_key
    tid = str(task_id or "").strip()
    if not tid:
        raise ValueError("empty task_id")
    if not memory_dir:
        raise ValueError("no memory_dir")
    clean = {ax: normalize_key((facets or {}).get(ax))[:60] for ax in FACET_AXES
             if normalize_key((facets or {}).get(ax))}
    rec = {"task_id": tid, "facets": clean, "by": str(by or "steward"), "at": str(at or "")}
    path = Path(memory_dir) / "task_facets.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    _append_governance(path, rec)
    return rec


def load_task_facets(memory_dir) -> dict:
    """`{task_id -> {axis: value}}` from `task_facets.jsonl` (last write per task wins). {} when none."""
    import json

    from looplab.events.eventstore import read_jsonl_lenient
    if not memory_dir:
        return {}
    path = Path(memory_dir) / "task_facets.jsonl"
    if not path.exists():
        return {}
    out: dict = {}
    for r in read_jsonl_lenient(path, loads=json.loads, dicts_only=True):
        tid = str(r.get("task_id") or "").strip()
        if tid and isinstance(r.get("facets"), dict):
            out[tid] = {ax: str(v) for ax, v in r["facets"].items() if ax in FACET_AXES and v}
    return out


def facet_overlap(a: dict, b: dict) -> int:
    """How many facet axes two tasks share a VALUE on — the additional cross-task scope-match signal."""
    return sum(1 for ax in FACET_AXES if a.get(ax) and a.get(ax) == b.get(ax))


def steward_task_facets(memory_dir, client, *, task_id: str, goal: str, kind: str = "", apply: bool = False,
                        by: str = "steward", at: str = "") -> dict:
    """One-call agentic faceting: classify the task and (when `apply`) record its facets. Returns
    `{"facets", "recorded"}`."""
    facets = propose_task_facets(goal, kind, client)
    recorded = None
    if apply and facets and task_id:
        recorded = record_task_facets(memory_dir, task_id=task_id, facets=facets, by=by, at=at)
    return {"facets": facets, "recorded": recorded}
