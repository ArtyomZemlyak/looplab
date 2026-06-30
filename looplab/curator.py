"""Knowledge Curator: a dedicated, GOAL-driven agentic session that populates and maintains the
markdown memory + knowledge base — not a single tool call.

The difference matters. A one-shot `kb_write` blindly drops a file; the curator runs a real tool
loop: it surveys the existing structure (`kb_tree`/`list_notes`), reads the relevant notes, decides
where new material belongs (extending/editing an existing note instead of duplicating a topic),
creates the folders/files it needs, and only then reports what it changed. It works toward a high
level goal — "research X and add it to the KB", "consolidate this report into the KB, structured",
"you keep making this mistake — record it in memory" — the same way the rest of LoopLab's agents
work from a goal rather than a fixed script.

Grounded by the same toolset the Researcher uses: web/arXiv search (when enabled) for "go learn it
online", plus full read/edit access to both stores. Degrades cleanly with no model (returns a note
saying so) — it never crashes a caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel, Field


class _CurateOut(BaseModel):
    """Structured result the LLM fills via `emit` after it has finished editing the stores."""
    summary: str = ""                                   # what it did, in one short paragraph
    changes: list[str] = Field(default_factory=list)    # relative paths it created/edited
    followups: list[str] = Field(default_factory=list)  # suggested next curation steps


@dataclass
class CurateResult:
    ok: bool
    summary: str = ""
    changes: list[str] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)
    error: str = ""


_SYSTEM = (
    "You are the KNOWLEDGE CURATOR for an autonomous ML research system. You maintain two persistent "
    "markdown stores:\n"
    "  • the KNOWLEDGE BASE (kb_* tools) — a structured HIERARCHY of folders + notes holding expert, "
    "domain knowledge (techniques, datasets, pitfalls, how-tos). Organize it into sensible "
    "sub-folders, e.g. 'cv/augmentation/mixup.md', 'tabular/gbdt/xgboost.md'.\n"
    "  • cross-run MEMORY (memory_* / remember) — short topic files of DEV-PROCESS lessons learned "
    "across runs (recurring mistakes to avoid, gotchas, tips).\n\n"
    "Work toward the GOAL like a librarian, not a dump truck:\n"
    "  1. FIRST survey what already exists — call kb_tree / list_notes (and memory_list) and read "
    "the notes that look related. Do NOT create a duplicate of a topic that already exists.\n"
    "  2. If a relevant note exists, EXTEND or EDIT it (kb_append / kb_edit) rather than making a "
    "second one. If none fits, create a new note at a well-chosen path (kb_write).\n"
    "  3. When the goal says to research/learn a topic, use the web/search tools first (if "
    "available), then write the distilled knowledge — concise, markdown, with concrete specifics "
    "(hyperparameters, ranges, pitfalls), not fluff.\n"
    "  4. Keep memory and the knowledge base separate: process lessons → memory; durable domain "
    "knowledge → the KB.\n\n"
    "Make the actual edits with the tools — do not just describe them. When everything the goal "
    "asks for is written, call `emit` exactly once with a `summary`, the list of `changes` (paths "
    "you created/edited), and any `followups`."
)


class Curator:
    """Runs the curation tool loop. Holds the toolset + client; `run(goal)` drives one session."""

    def __init__(self, client, tools, *, parser: str = "tool_call",
                 max_turns: int = 0, time_budget_s: float = 0.0, context_budget_chars: int = 0):
        self.client = client
        self.tools = tools
        self.parser = parser
        self.max_turns = max_turns
        self.time_budget_s = time_budget_s
        self.context_budget_chars = context_budget_chars

    def run(self, goal: str, *, context: str = "") -> CurateResult:
        from .agent import drive_tool_loop
        if self.client is None:
            return CurateResult(False, error="no LLM client configured (curation needs a model)")
        goal = (goal or "").strip()
        if not goal:
            return CurateResult(False, error="empty goal")
        emit_spec = {"type": "function", "function": {
            "name": "emit", "description": "Report what you curated: a summary, the changed paths, "
            "and any follow-ups.", "parameters": _CurateOut.model_json_schema()}}
        user = f"GOAL: {goal}"
        if context:
            user += f"\n\nCONTEXT (material to file/structure):\n{context}"
        box: dict = {}

        def _fin(args):
            try:
                box["c"] = _CurateOut(**{k: v for k, v in (args or {}).items()
                                         if k in _CurateOut.model_fields})
            except Exception:  # noqa: BLE001 - junk emit -> empty (the writes already happened)
                box["c"] = _CurateOut(summary="(curation finished; emit was malformed)")
            return box["c"]

        try:
            drive_tool_loop(
                self.client, self.tools,
                [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
                emit_spec, max_turns=self.max_turns, time_budget_s=self.time_budget_s,
                context_budget_chars=self.context_budget_chars,
                finalize=_fin, fallback=lambda _m: box.get("c"))
        except Exception as e:  # noqa: BLE001 - transport/model failure -> soft fail, not a crash
            return CurateResult(False, error=str(e))
        out = box.get("c")
        if out is None:
            return CurateResult(False, error="the curator made no edits (model did not drive the tools)")
        return CurateResult(True, summary=out.summary, changes=out.changes, followups=out.followups)


def make_curator(settings, *, client=None) -> Optional["Curator"]:
    """Build a Curator from config: requires a client and at least one of the stores enabled. The
    toolset is read+write KnowledgeTools over the resolved memory + KB dirs, plus web/arXiv search
    when those are enabled (so "research X and add it" can actually go look it up). None when no
    client is available (toy/offline) — the caller then reports curation is unavailable."""
    if client is None:
        return None
    kb_dir = settings.resolved_knowledge_dir()
    mem_dir = settings.resolved_memory_dir()
    if not (kb_dir or mem_dir):
        return None
    from pathlib import Path

    from .knowledge_tools import KnowledgeTools
    cases = str(Path(mem_dir) / "cases.jsonl") if mem_dir else None
    providers = [KnowledgeTools(kb_dir, cases_path=cases, memory_dir=mem_dir, writable=True)]
    if getattr(settings, "web_search", False):
        from .web import WebTools
        providers.append(WebTools(enabled=True))
    if getattr(settings, "literature_search", False):
        from .literature import LiteratureTools
        providers.append(LiteratureTools(enabled=True))
    from .agent import CompositeTools
    tools = providers[0] if len(providers) == 1 else CompositeTools(providers)
    return Curator(client, tools, parser=getattr(settings, "llm_parser", "tool_call"),
                   max_turns=getattr(settings, "agent_max_turns", 0),
                   time_budget_s=getattr(settings, "agent_time_budget_s", 0.0),
                   context_budget_chars=getattr(settings, "context_budget_chars", 0))
