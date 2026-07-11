"""Agentic retrieval over the cross-run DISTILLED memory that used to be injection-only.

`lessons.jsonl` (generalizable good/bad findings with a verdict + how many runs back them) and
`meta_notes.jsonl` (per-task causal summaries of WHY a run's winner won) were previously only
*passively injected* into the proposal prompt (fingerprint-matched). This exposes them as TOOLS so the
Researcher can ACTIVELY pull them when it wants — completing "agentic retrieval for everything": cases
+ knowledge notes are already searchable via `kb_search`, skills via `list_skills`/`use_skill`, sibling
runs via the sibling tools, so with these two the agent can reach every memory type on demand.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from looplab.events.eventstore import read_jsonl_lenient
from looplab.tools._base import fn_spec

_WORD = re.compile(r"[a-z0-9@._]+")


def _toks(s: str) -> set:
    return {w for w in _WORD.findall((s or "").lower()) if len(w) > 2}




class MemoryTools:
    """`search_lessons` + `recall_notes` over `<memory_dir>/{lessons,meta_notes}.jsonl`."""

    def __init__(self, memory_dir: str | None):
        self.dir = Path(memory_dir) if memory_dir else None

    def specs(self) -> list[dict]:
        if not self.dir:
            return []
        return [
            fn_spec("search_lessons",
                "Search the cross-run LESSONS ledger — generalizable findings (what worked AND what "
                "did NOT) distilled from past runs, each with a verdict (supported/tested/abandoned/"
                "failed) and how many observations back it. Use it to reuse what reliably helps and to "
                "avoid re-treading known dead ends.",
                {"query": {"type": "string", "description": "What to find lessons about (e.g. 'batch "
                           "size', 'overfitting', 'learning-rate schedule')."},
                 "limit": {"type": "integer", "description": "Max lessons (default 6)."}},
                ["query"]),
            fn_spec("recall_notes",
                "Recall META-NOTES — short CAUSAL summaries of WHY past runs' winners won, per task. "
                "Use it to warm-start: what actually mattered last time on this or a similar task.",
                {"query": {"type": "string", "description": "Task id or keywords to filter "
                           "(blank = most recent)."},
                 "limit": {"type": "integer", "description": "Max notes (default 6)."}},
                []),
        ]

    def _load(self, fname: str) -> list[dict]:
        return read_jsonl_lenient(self.dir / fname, loads=json.loads)

    def execute(self, name: str, args: dict) -> str:
        # ToolProvider contract (_base.py): execute NEVER raises — a junk arg from a small model
        # (e.g. limit="ten") must read as a tool error, not propagate out of drive_tool_loop (which
        # doesn't guard tools.execute) and discard the whole agent phase.
        try:
            return self._execute(name, args)
        except Exception as e:  # noqa: BLE001
            return f"(tool error: {e})"

    def _execute(self, name: str, args: dict) -> str:
        if not self.dir:
            return "(no cross-run memory configured)"
        args = args or {}
        q = str(args.get("query") or "")
        try:
            lim = max(1, int(args.get("limit") or 6))
        except (TypeError, ValueError):
            lim = 6
        qt = _toks(q)
        if name == "search_lessons":
            rows = self._load("lessons.jsonl")
            ranked = sorted(rows, key=lambda o: len(qt & _toks(o.get("statement", ""))), reverse=True)
            hits = [o for o in ranked if (not qt) or (qt & _toks(o.get("statement", "")))][:lim]
            if not hits:
                return "(no matching lessons yet)"
            lines = []
            for o in hits:
                n = int(o.get("evidence_count") or len(o.get("evidence") or []) or 1)
                conf = f", conf {o['confidence']:.2f}" if o.get("confidence") is not None else ""
                lines.append(f"[{o.get('outcome', '?')}] {o.get('statement', '')} "
                             f"(verified across {n} observation{'s' if n != 1 else ''}{conf})")
            return "\n".join(lines)
        if name == "recall_notes":
            rows = self._load("meta_notes.jsonl")
            if qt:
                rows = [o for o in rows
                        if qt & (_toks(o.get("task_id", "")) | _toks(o.get("note", "")))]
            rows = rows[-lim:]                          # most recent first-relevant
            if not rows:
                return "(no matching notes yet)"
            return "\n".join(f"[{o.get('task_id', '?')}] {o.get('note', '')}" for o in rows)
        return f"(unknown tool: {name})"
