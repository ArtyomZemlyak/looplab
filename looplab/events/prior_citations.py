"""The prior CITATION-RATE instrument (doc 52 row 17): did the priors a proposal was shown reach
its rationale?

Nothing measured whether an injected prior (a lesson, a note, a skill, a memory read) was CITED by
the proposal that followed, so memory growth was unbounded by any utility signal. The two records
this reads are the whole instrument: `prior_injected` (which lesson rows a role's prompt prior was
built from, written by the main task at every load) and `memory_read` (which rows a cross-run /
memory / skill tool call showed, written through the engine sink). Both are DIAGNOSTIC rows, so
this is a pure projection over `events.jsonl` — no fold change, no model, no I/O.

THE JOIN IS BY LOG ORDER, and that is a stated approximation, not a hidden one: a `node_created`
row is a proposal; the prior it was shown is the latest `prior_injected` of each role before it,
and the tool reads it was shown are the `memory_read` rows since the previous proposal. A record
does not know which model turn consumed it — the proposal that follows is the best available
reading, and the report says so.

THE CITATION RULE IS LEXICAL and deliberately modest: a lesson is cited when at least
`CITATION_CONTAINMENT` of its content tokens (len >= 3, no stopwords, no bare numbers) appear in
the proposal's rationale + hypothesis + params, or when its id literal does. Containment rather
than Jaccard because a rationale is long and a statement short — quoting the whole statement
inside a paragraph is exactly the case a Jaccard bar would miss. Named a CITATION rate to keep it
apart from HASTE's keep-fraction sense of "hit rate".

`utility_rows` turns a report into the per-(run, lesson) rows `lesson_utility.jsonl` accumulates;
`engine/lessons_priors.py` folds those onto each lesson as `utility` for the read-side rank term
and the forgetting rung (`lesson_hygiene.lesson_utility` / `filter_useless`).
"""
from __future__ import annotations

import json
import time
from typing import Iterable, Optional

from looplab.core.text import tokenize

CITATION_CONTAINMENT = 0.6
_STOPWORDS = frozenset(
    "the a an and or of to in on for with by is are was were be been this that it its as at from "
    "than into over under not no but if then so we our you your they their which who what when "
    "where how all any each more most other some such only own same too very can will just "
    "should would could may might must do does did done have has had having use used using "
    "make made makes set sets run runs try tried also than".split())


def citation_tokens(text) -> frozenset[str]:
    return frozenset(t for t in tokenize(str(text or ""))
                     if len(t) >= 3 and t not in _STOPWORDS and not t.isdigit())


def cites(statement: str, text: str, *, lesson_id: str = "") -> bool:
    """Whether `text` cites `statement` under the containment rule (or names its id)."""
    if lesson_id and lesson_id in str(text or ""):
        return True
    wanted = citation_tokens(statement)
    if not wanted:
        return False
    have = citation_tokens(text)
    return len(wanted & have) / len(wanted) >= CITATION_CONTAINMENT


def proposal_text(data: dict) -> str:
    """The text a proposal is judged on: the idea's rationale, hypothesis and params."""
    idea = data.get("idea") if isinstance(data, dict) else None
    idea = idea if isinstance(idea, dict) else {}
    parts = [str(idea.get("rationale") or ""), str(idea.get("hypothesis") or ""),
             str(idea.get("description") or "")]
    params = idea.get("params")
    if isinstance(params, dict):
        try:
            parts.append(json.dumps(params, sort_keys=True, default=str))
        except (TypeError, ValueError):
            parts.append(str(params))
    return " ".join(p for p in parts if p)


def _rows(data: dict) -> list[dict]:
    rows = data.get("rows") if isinstance(data, dict) else None
    return [r for r in (rows or []) if isinstance(r, dict) and isinstance(r.get("id"), str)]


def prior_citation_report(events: Iterable) -> dict:
    """The report over one run's events (`Event` objects or `(type, data)` pairs)."""
    lessons: dict[str, dict] = {}
    latest_prior: dict[str, list[dict]] = {}
    pending_reads: list[tuple[str, list[dict]]] = []
    proposals = injections = reads = shown_pairs = cited_pairs = 0

    def slot(row: dict) -> dict:
        return lessons.setdefault(row["id"], {
            "statement": str(row.get("statement") or "")[:160], "shown": 0, "cited": 0,
            "shown_tool": 0, "cited_tool": 0, "roles": []})

    for ev in events:
        etype = getattr(ev, "type", None) if not isinstance(ev, tuple) else ev[0]
        data = getattr(ev, "data", None) if not isinstance(ev, tuple) else ev[1]
        if not isinstance(data, dict):
            continue
        if etype == "prior_injected":
            injections += 1
            latest_prior[str(data.get("role") or "all")] = _rows(data)
        elif etype == "memory_read":
            reads += 1
            pending_reads.append((str(data.get("tool") or ""), _rows(data)))
        elif etype == "node_created":
            proposals += 1
            text = proposal_text(data)
            for role, rows in latest_prior.items():
                for row in rows:
                    entry = slot(row)
                    entry["shown"] += 1
                    shown_pairs += 1
                    if role not in entry["roles"]:
                        entry["roles"].append(role)
                    if cites(entry["statement"], text, lesson_id=row["id"]):
                        entry["cited"] += 1
                        cited_pairs += 1
            for _tool, rows in pending_reads:
                for row in rows:
                    entry = slot(row)
                    entry["shown_tool"] += 1
                    if cites(entry["statement"], text, lesson_id=row["id"]):
                        entry["cited_tool"] += 1
            pending_reads = []
    return {
        "proposals": proposals, "injections": injections, "reads": reads,
        "shown_pairs": shown_pairs, "cited_pairs": cited_pairs,
        "citation_rate": (cited_pairs / shown_pairs) if shown_pairs else None,
        "lessons": lessons,
        "rule": (f"a lesson is cited when >= {CITATION_CONTAINMENT:.0%} of its content tokens appear "
                 "in the proposal's rationale/hypothesis/params, or its id does; a proposal is shown "
                 "the latest prior of each role before it and the tool reads since the previous "
                 "proposal (joined by log order)"),
    }


def utility_rows(report: dict, *, run_id: str, run_uid: str = "",
                 now: Optional[float] = None) -> list[dict]:
    """One `lesson_utility.jsonl` row per lesson this run's prompt prior showed (tool reads are
    reported, not credited — a read the model asked for is not a prior the store pushed)."""
    out = []
    for lesson_id, entry in (report.get("lessons") or {}).items():
        if not lesson_id.startswith("les-") or entry.get("shown", 0) <= 0:
            continue
        out.append({"lesson_id": lesson_id, "run_id": str(run_id or ""), "run_uid": str(run_uid or ""),
                    "shown": int(entry["shown"]), "cited": int(entry["cited"]),
                    "ts": float(time.time() if now is None else now)})
    return out
