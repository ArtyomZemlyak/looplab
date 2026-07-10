"""Prompt store (I18, ADR-8): role prompt bodies live as editable Markdown files and
are re-read on every use (hot-reload), so they can be tuned without code changes / a
restart. Templates use ``$var`` (string.Template) so JSON braces in prompts don't clash.
Missing file or no store -> the built-in default is used.
"""
from __future__ import annotations

import re
import string
from pathlib import Path
from typing import Optional

# Anchored to the START of the string (\A), NOT ^…MULTILINE: a prompt body may use `---` as Markdown
# horizontal rules, and a MULTILINE `^---` matches between ANY two of them, silently deleting the
# section in between (Section A vanishes from a body like "intro\n---\nA\n---\nB"). Only a genuine
# leading YAML frontmatter block (the file's FIRST line is `---`) is stripped.
_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---[ \t]*\r?\n", re.DOTALL)


def _strip_frontmatter(text: str) -> str:
    return _FRONTMATTER.sub("", text, count=1)


class PromptStore:
    def __init__(self, directory: Optional[str] = None):
        self.dir = Path(directory) if directory else None

    def get(self, name: str, default: str = "", **vars) -> str:
        text = default
        if self.dir is not None:
            f = self.dir / f"{name}.md"
            if f.exists():  # re-read each call -> hot reload
                # utf-8-sig strips a BOM so a Windows-edited prompt's frontmatter still matches ^---.
                text = _strip_frontmatter(f.read_text(encoding="utf-8-sig", errors="replace")).strip()
        return string.Template(text).safe_substitute(vars)


# The overridable prompt-key REGISTRY (docs/15 §P4.7): every `render(prompts, "<key>", …)` call
# site must use a key listed here — `tests/test_prompt_keys.py` source-scans both directions
# (the same discipline as event types / hints / signals). Why: an override lands as
# `<prompt_dir>/<key>.md`, so a typo'd KEY at a call site (or a renamed key with a stale
# override file) silently falls back to the built-in default — no error, the operator's tuned
# prompt just stops applying.
PROMPT_KEYS: tuple[str, ...] = (
    "researcher_system", "tool_researcher_system",
    "developer_system", "developer_repair_prefix",
    "repo_developer_system_intro", "repo_developer_system_body",
    "strategist_system", "tool_strategist_system",
    "pilot_system", "triage_system",
    "deep_research_system", "foresight_system", "merge_system",
)


def render(store: Optional[PromptStore], name: str, default: str, **vars) -> str:
    """Resolve a prompt via the store (if any) or the inline default; render $vars."""
    if store is not None:
        return store.get(name, default=default, **vars)
    return string.Template(default).safe_substitute(vars)
