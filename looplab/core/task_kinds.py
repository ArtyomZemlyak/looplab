"""Shared task-kind launch defaults used by every config and launch surface."""
from __future__ import annotations

from typing import Optional


# These task kinds need a code-writing/reasoning backend to do useful work. Keeping the vocabulary in
# core prevents generated configs, CLI Genesis and web launch from silently choosing different defaults.
GENERATIVE_KINDS = frozenset({"dataset", "code_regression", "mlebench", "mlebench_real", "repo"})


def default_backend(kind: Optional[str], *, chosen: bool) -> Optional[str]:
    """Return ``llm`` for an unpinned generative task; otherwise preserve the configured default."""
    return "llm" if (not chosen and kind in GENERATIVE_KINDS) else None
