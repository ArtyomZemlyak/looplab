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

_WORD = re.compile(r"[a-z0-9@._]+")


def _toks(s: str) -> set:
    return {w for w in _WORD.findall((s or "").lower()) if len(w) > 2}


def _fn(name: str, desc: str, props: dict, required: list) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


class MemoryTools:
    """`search_lessons` + `recall_notes` over `<memory_dir>/{lessons,meta_notes}.jsonl`."""

    def __init__(self, memory_dir: str | None):
        self.dir = Path(memory_dir) if memory_dir else None

    def specs(self) -> list[dict]:
        if not self.dir:
            return []
        return [
            _fn("search_lessons",
                "Search the cross-run LESSONS ledger — generalizable findings (what worked AND what "
                "did NOT) distilled from past runs, each with a verdict (supported/tested/abandoned/"
                "failed) and how many observations back it. Use it to reuse what reliably helps and to "
                "avoid re-treading known dead ends.",
                {"query": {"type": "string", "description": "What to find lessons about (e.g. 'batch "
                           "size', 'overfitting', 'learning-rate schedule')."},
                 "limit": {"type": "integer", "description": "Max lessons (default 6)."}},
                ["query"]),
            _fn("recall_notes",
                "Recall META-NOTES — short CAUSAL summaries of WHY past runs' winners won, per task. "
                "Use it to warm-start: what actually mattered last time on this or a similar task.",
                {"query": {"type": "string", "description": "Task id or keywords to filter "
                           "(blank = most recent)."},
                 "limit": {"type": "integer", "description": "Max notes (default 6)."}},
                []),
        ]

    def _load(self, fname: str) -> list[dict]:
        p = self.dir / fname
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                out.append(d)
        return out

    def execute(self, name: str, args: dict) -> str:
        if not self.dir:
            return "(no cross-run memory configured)"
        args = args or {}
        q = str(args.get("query") or "")
        lim = max(1, int(args.get("limit") or 6))
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
