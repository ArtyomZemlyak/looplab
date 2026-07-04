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


def render(store: Optional[PromptStore], name: str, default: str, **vars) -> str:
    """Resolve a prompt via the store (if any) or the inline default; render $vars."""
    if store is not None:
        return store.get(name, default=default, **vars)
    return string.Template(default).safe_substitute(vars)
