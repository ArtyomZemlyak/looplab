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

_FRONTMATTER = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


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
                text = _strip_frontmatter(f.read_text(encoding="utf-8", errors="replace")).strip()
        return string.Template(text).safe_substitute(vars)


def render(store: Optional[PromptStore], name: str, default: str, **vars) -> str:
    """Resolve a prompt via the store (if any) or the inline default; render $vars."""
    if store is not None:
        return store.get(name, default=default, **vars)
    return string.Template(default).safe_substitute(vars)
