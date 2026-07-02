"""C1 · Fault localization (ADR-7, Agentless recipe phase 1). Before editing/repairing, rank the
repo's source files by relevance to a failure (the error/traceback) + the idea, so the Developer
edits the RIGHT place instead of guessing. Dependency-free heuristic over the file tree: files named
in a traceback score highest, then files sharing identifiers with the error/idea text.

Pure read-only over the given roots; deterministic. Surfaced into the repair/proposal prompt for
repo tasks (the documented failure mode it fixes: "missing relevant files across multiple locations").
"""
from __future__ import annotations

import re
from pathlib import Path

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_PYFILE = re.compile(r'File "([^"]+\.py)"|([\w./\\-]+\.py)')
_COMMON = {
    "Error", "Traceback", "File", "line", "self", "return", "import", "from", "None", "True",
    "False", "def", "class", "print", "the", "and", "for", "not", "with", "value", "object",
    "module", "most", "recent", "call", "last", "Exception", "raise", "args", "kwargs",
}


def _symbols(error_text: str, idea_text: str = "") -> tuple[set[str], set[str]]:
    """Extract (filenames named in a traceback, candidate identifiers) from the error + idea text."""
    files: set[str] = set()
    for m in _PYFILE.finditer(error_text or ""):
        f = m.group(1) or m.group(2)
        if f:
            files.add(Path(f.replace("\\", "/")).name)
    idents = {w for w in _IDENT.findall((error_text or "") + " " + (idea_text or ""))
              if w not in _COMMON}
    return files, idents


def localize(error_text: str, repo_roots, idea_text: str = "", top: int = 5) -> list[dict]:
    """Rank `*.py` files under `repo_roots` by relevance to the failure. Returns
    [{file, score, hits}] (score desc) — files named in the traceback get a strong boost, then
    files sharing identifiers with the error/idea."""
    files, idents = _symbols(error_text, idea_text)
    scored: list[dict] = []
    for root in repo_roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.py")):
            if any(part in (".git", "__pycache__", ".venv", "node_modules") for part in p.parts):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = p.relative_to(root).as_posix()
            shared = idents & set(_IDENT.findall(text))
            score = (10 if p.name in files else 0) + len(shared)
            if score > 0:
                hits = (["named-in-traceback"] if p.name in files else []) + sorted(shared)[:8]
                scored.append({"file": rel, "score": score, "hits": hits})
    scored.sort(key=lambda s: (-s["score"], s["file"]))
    return scored[:top]
