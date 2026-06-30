"""Hierarchical markdown store: a directory tree of `.md` notes the agent can list, read, search,
write AND edit — the shared backend for the two persistent stores the agent maintains itself:

  * cross-run **memory** — flat-ish topic files (lessons, recurring mistakes, dev-process notes);
  * the **knowledge base** — a structured hierarchy of folders + files (expert domain knowledge).

Everything is markdown. Paths are RELATIVE and restricted to the store root (no `..` escape), so a
note can live at `nlp/tokenization/bpe.md` as easily as `general.md`. Mutations are atomic and the
semantic index is rebuilt after each write, so a follow-up search sees the change immediately.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .atomicio import atomic_write_text
from .retrieval import glob_files
from .retrieval import grep as _grep
from .retrieval import read_file
from .vectorstore import InMemoryVectorStore, Item, hash_embed


class MarkdownStore:
    """Read/edit/grow a directory tree of markdown notes, path-restricted to `root`.

    `extra` is a list of (id, text) pairs folded into the semantic index alongside the files — used
    by memory to make past-solution CASES searchable next to the curated markdown notes, without
    those cases being editable files."""

    def __init__(self, root: str | Path, extra: Optional[list[tuple[str, str]]] = None):
        self.root = Path(root).resolve()
        self._extra = list(extra or [])
        self._index = InMemoryVectorStore()
        self.reindex()

    # ---- path safety ---------------------------------------------------------------------------
    def resolve(self, rel: str, *, for_write: bool = False) -> Optional[Path]:
        """Map a relative note path to an absolute one inside the store. Returns None on an empty
        path or a `..`/absolute escape. For writes, force a `.md` suffix so notes stay markdown."""
        rel = (rel or "").replace("\\", "/").strip().lstrip("/")
        if not rel or rel in (".", ".."):
            return None
        target = (self.root / rel).resolve()
        if target != self.root and self.root not in target.parents:   # traversal / absolute escape
            return None
        if for_write and target.suffix != ".md":
            target = target.parent / (target.name + ".md")
        return target

    def _rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root).as_posix()

    # ---- read ----------------------------------------------------------------------------------
    def list(self) -> list[str]:
        """Every note as a root-relative posix path (recursive), sorted."""
        return sorted(self._rel(Path(p)) for p in glob_files("*.md", str(self.root)))

    def tree(self) -> str:
        """An indented folder→file view of the store, so the agent can see the existing structure
        before deciding where new material belongs (and avoid duplicating a topic that exists)."""
        paths = self.list()
        if not paths:
            return "(empty store)"
        lines: list[str] = []
        seen_dirs: set[str] = set()
        for rel in paths:
            parts = rel.split("/")
            for d in range(len(parts) - 1):
                prefix = "/".join(parts[: d + 1])
                if prefix not in seen_dirs:
                    seen_dirs.add(prefix)
                    lines.append("  " * d + parts[d] + "/")
            lines.append("  " * (len(parts) - 1) + parts[-1])
        return "\n".join(lines)

    def read(self, rel: str, max_bytes: int = 8000) -> Optional[str]:
        """Read a note. Tolerates a missing `.md` suffix (read 'foo' or 'foo.md')."""
        target = self.resolve(rel)
        if target is None:
            return None
        if not target.exists():
            alt = self.resolve(rel, for_write=True)    # resolve again with the .md suffix forced on
            if alt is None or not alt.exists():
                return None
            target = alt
        if not target.is_file():
            return None
        return read_file(str(target))[:max_bytes]

    def grep(self, pattern: str, max_hits: int = 30) -> list[str]:
        hits = _grep(pattern, str(self.root), glob="*.md", max_hits=max_hits)
        return [f"{self._rel(Path(h.path))}:{h.lineno}: {h.line}" for h in hits]

    def search(self, query: str, k: int = 4) -> list[tuple[str, str]]:
        """Semantic search → [(label, snippet)]. Labels are relative paths for files, or the
        extra-content id (e.g. a past-case marker)."""
        hits = self._index.search("md", hash_embed(query or ""), k)
        return [(h.payload.get("label", "?"), h.payload.get("text", "")) for h in hits]

    # ---- write / edit --------------------------------------------------------------------------
    def write(self, rel: str, content: str) -> Optional[str]:
        """Create or overwrite a note (markdown), making parent folders as needed. Returns the
        relative path written, or None on a bad path."""
        target = self.resolve(rel, for_write=True)
        if target is None:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        body = (content or "").rstrip("\n") + "\n"
        atomic_write_text(target, body)
        self._index_one(self._rel(target), body)
        return self._rel(target)

    def append(self, rel: str, content: str) -> Optional[str]:
        """Append a section to a note (creating it if absent). Returns the relative path."""
        target = self.resolve(rel, for_write=True)
        if target is None:
            return None
        prev = target.read_text(encoding="utf-8").rstrip("\n") + "\n\n" if target.exists() else ""
        target.parent.mkdir(parents=True, exist_ok=True)
        body = prev + (content or "").rstrip("\n") + "\n"
        atomic_write_text(target, body)
        self._index_one(self._rel(target), body)
        return self._rel(target)

    def edit(self, rel: str, old: str, new: str) -> str:
        """Precise in-place edit: replace the unique substring `old` with `new` in an existing note,
        so the agent can REVISE knowledge (fix a stale fact, extend a section) instead of only
        appending. Returns a status string (the tool feeds it back to the model)."""
        target = self.resolve(rel, for_write=True)
        if target is None:
            return "(bad path)"
        if not target.exists():
            return f"(no such note: {rel} — use write to create it)"
        text = target.read_text(encoding="utf-8")
        if not old:
            return "(edit needs a non-empty `old` string to locate)"
        count = text.count(old)
        if count == 0:
            return "(the `old` text was not found in the note — read it first to copy an exact snippet)"
        if count > 1:
            return f"(the `old` text appears {count}× — make it longer/unique so the edit is unambiguous)"
        body = text.replace(old, new, 1)
        atomic_write_text(target, body)
        self._index_one(self._rel(target), body)
        return f"(edited {self._rel(target)})"

    # ---- indexing ------------------------------------------------------------------------------
    def _index_one(self, rel: str, text: str) -> None:
        """Upsert a SINGLE note into the live index after a write/append/edit. Item ids are the
        relative path, so this replaces the prior version of that note in place — no need to re-read
        and re-embed the whole tree on every mutation (the curator does many writes per session)."""
        self._index.upsert("md", [Item(id=rel, vector=hash_embed(rel + " " + text),
                                       payload={"label": rel, "text": text})])

    def reindex(self) -> None:
        items: list[Item] = []
        for p in glob_files("*.md", str(self.root)):
            rel = self._rel(Path(p))
            text = read_file(p)
            items.append(Item(id=rel, vector=hash_embed(rel + " " + text),
                              payload={"label": rel, "text": text}))
        for eid, text in self._extra:
            items.append(Item(id=eid, vector=hash_embed(text),
                              payload={"label": eid, "text": text}))
        self._index = InMemoryVectorStore()       # rebuild from scratch so deletes/edits propagate
        if items:
            self._index.upsert("md", items)
