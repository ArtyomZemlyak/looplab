"""Agentic retrieval + curation toolset (ADR-16) for the LLM agents: lexical (grep), file
(list/read/tree), semantic (kb_search), and WRITE (kb_write/append/edit, memory_*) tools over the
markdown knowledge base and cross-run memory. The model chooses which to call; file access is
restricted to the store roots (no arbitrary reads/writes). Both stores are hierarchical markdown —
see `mdstore.MarkdownStore` — so the agent can read what exists and EDIT it, not just append blindly.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .mdstore import MarkdownStore
from .retrieval import glob_files, grep, read_file
from .vectorstore import hash_embed


def _fn_spec(name: str, desc: str, props: dict, required: list) -> dict:
    """Build one OpenAI-format function/tool schema. Shared by every tool provider so the
    schema shape lives in one place."""
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


class RepoTools:
    """Read-only view of the editable repo(s) for the LLM Researcher (item #3): grep / list /
    read over the source tree, path-restricted to the mounted repos. The proposer can SEE the
    code it suggests changing instead of proposing blind. It never writes — editing the repo
    stays the Developer's job (the trust/role boundary)."""

    def __init__(self, mounts: list[dict], max_bytes: int = 4000):
        # mounts: [{"name": ".|subdir", "path": "<repo>"}]; "." is shown as the repo root.
        # expanduser/expandvars so a `~/repo` mount (e.g. from an older snapshot) still resolves.
        self.roots = {(m["name"] or "."): Path(os.path.expanduser(os.path.expandvars(m["path"]))).resolve()
                      for m in mounts}
        self.max_bytes = max_bytes

    def specs(self) -> list[dict]:
        names = ", ".join(self.roots)
        return [
            _fn_spec("repo_grep", f"Regex search across the editable repo source ({names}). "
                     "Returns matching <repo>/<path>:<line> hits.",
                     {"pattern": {"type": "string"}, "glob": {"type": "string"}}, ["pattern"]),
            _fn_spec("repo_list", f"List source files in an editable repo ({names}).",
                     {"repo": {"type": "string"}, "glob": {"type": "string"}}, []),
            _fn_spec("repo_read", "Read a file from an editable repo, given a <repo>/<path> "
                     "(or just <path> for the root repo).", {"path": {"type": "string"}}, ["path"]),
        ]

    def _resolve(self, rel: str):
        """Map a '<repo>/<path>' (or '<path>' for root '.') to an absolute path, restricted to
        within that repo's root. Returns None on an unknown repo or an escape attempt."""
        rel = (rel or "").replace("\\", "/").lstrip("/")
        head, _, tail = rel.partition("/")
        if head in self.roots and head != ".":
            root, sub = self.roots[head], tail
        elif "." in self.roots:
            root, sub = self.roots["."], rel
        else:
            return None
        target = (root / sub).resolve()
        if root != target and root not in target.parents:   # escape (.. / absolute)
            return None
        return target

    def execute(self, name: str, args: dict) -> str:
        try:
            if name == "repo_grep":
                glob = args.get("glob") or "*"
                out = []
                for label, root in self.roots.items():
                    pre = "" if label == "." else label + "/"
                    for h in grep(args.get("pattern", ""), str(root), glob=glob, max_hits=20):
                        hp = Path(h.path).resolve()
                        if root != hp and root not in hp.parents:
                            continue            # a hit outside the root (symlink) -> skip, not crash
                        out.append(f"{pre}{hp.relative_to(root).as_posix()}:{h.lineno}: {h.line}")
                return "\n".join(out[:40]) or "(no matches)"
            if name == "repo_list":
                repo = args.get("repo") or ("." if "." in self.roots else next(iter(self.roots)))
                root = self.roots.get(repo)
                if root is None:
                    return f"(no such repo: {repo}; have: {', '.join(self.roots)})"
                glob = args.get("glob") or "*"
                files = [Path(p).resolve().relative_to(root).as_posix()
                         for p in glob_files(glob, str(root)) if ".git" not in Path(p).parts]
                return "\n".join(sorted(files)[:100]) or "(empty)"
            if name == "repo_read":
                target = self._resolve(args.get("path", ""))
                if target is None or not target.is_file():
                    return f"(no such file: {args.get('path')})"
                return read_file(str(target))[:self.max_bytes]
        except Exception as e:  # noqa: BLE001 — tool errors are fed back to the model
            return f"(tool error: {e})"
        return f"(unknown tool: {name})"


def _case_extras(cases_path: Path | None) -> list[tuple[str, str]]:
    """Past-best-solution CASES (I19) rendered as searchable (id, text) snippets — folded into the
    semantic index so the agent can recall what worked on a similar task. Read-only (never edited as
    files; they're appended automatically at run end). One malformed line never kills the rest."""
    out: list[tuple[str, str]] = []
    if not (cases_path and cases_path.exists()):
        return out
    for i, line in enumerate(cases_path.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(c, dict):
            continue
        text = (f"PAST CASE — task {c.get('task_id')}: {c.get('goal','')}\n"
                f"best params={c.get('params')} metric={c.get('metric')}\n"
                f"{c.get('rationale','')}")
        out.append((f"case:{c.get('task_id') or i}", text))
    return out


class KnowledgeTools:
    """Read + write tools over the agent's two persistent markdown stores.

    `knowledge_dir` is the KNOWLEDGE BASE — a hierarchy of folders + markdown notes (expert/domain
    knowledge) the agent can search, read, and grow/edit. `memory_dir` is cross-run MEMORY — topic
    markdown files (lessons, recurring mistakes) plus the auto-appended `cases.jsonl` of past best
    solutions (read-only). When `writable` is true the agent also gets the write/edit tools, so an
    operator can tell the Boss "research X and add it to the KB", "consolidate this report", or "you
    keep making this mistake — remember it". `kb_search` spans BOTH stores + the cases."""

    def __init__(self, knowledge_dir: str | None = None,
                 cases_path: str | None = None, k: int = 3,
                 memory_dir: str | None = None, writable: bool = True):
        self.cases_path = Path(cases_path) if cases_path else None
        self.writable = writable
        self.k = k
        # Past-best-solution cases (I19) are folded into exactly ONE store's index so kb_search
        # surfaces them without double-indexing any directory: the KB if present (kb_search has
        # always carried cases), else memory, else a tiny cases-only store rooted at the cases file.
        cases_extra = _case_extras(self.cases_path)
        self.kb = MarkdownStore(knowledge_dir, extra=cases_extra) if knowledge_dir else None
        self.mem = MarkdownStore(memory_dir, extra=(cases_extra if self.kb is None else None)) \
            if memory_dir else None
        self._cases_only = None
        if self.kb is None and self.mem is None and self.cases_path:
            self._cases_only = MarkdownStore(self.cases_path.parent, extra=cases_extra)

    # ---- the index kb_search/grep span (KB + memory notes + cases) -----------------------------
    def _search(self, query: str) -> list[tuple[str, str]]:
        seen: dict[str, str] = {}
        for store in (self.kb, self.mem, self._cases_only):
            if store is None:
                continue
            for label, text in store.search(query, self.k):
                seen.setdefault(label, text)
        # Re-rank the merged set by the same hash-embedding similarity used inside a store, so the
        # top-k across both stores is coherent (not just "KB first, then memory").
        qv = hash_embed(query or "")

        def _sim(t: str) -> float:
            from .vectorstore import _cosine
            return _cosine(qv, hash_embed(t))
        ranked = sorted(seen.items(), key=lambda kv: -_sim(kv[1]))
        return ranked[: self.k]

    # ---- tool schemas (OpenAI function format) ----
    def specs(self) -> list[dict]:
        specs = [
            _fn_spec("kb_search", "Semantic search across the knowledge base + memory; returns relevant "
                     "note snippets (and past solved-case notes).", {"query": {"type": "string"}}, ["query"]),
            _fn_spec("grep", "Regex search across knowledge-base notes (*.md, recursive). Matching lines.",
                     {"pattern": {"type": "string"}}, ["pattern"]),
            _fn_spec("list_notes", "List knowledge-base note paths (relative, includes sub-folders).", {}, []),
            _fn_spec("kb_tree", "Show the knowledge-base folder/file hierarchy, so you can see what "
                     "already exists before adding to it.", {}, []),
            _fn_spec("read_note", "Read a knowledge-base note by its (relative) path, e.g. "
                     "'nlp/tokenization.md'.", {"name": {"type": "string"}}, ["name"]),
        ]
        if self.writable and self.kb is not None:
            specs += [
                _fn_spec("kb_write", "Create or overwrite a knowledge-base note (markdown). `name` is a "
                         "relative path and MAY include folders (they're created), e.g. "
                         "'cv/augmentation/mixup.md'. Use to add domain knowledge, a structured report, "
                         "or a researched topic. Read the tree/existing note first to avoid duplication.",
                         {"name": {"type": "string"}, "content": {"type": "string"}}, ["name", "content"]),
                _fn_spec("kb_append", "Append a markdown section to a knowledge-base note (creates it if "
                         "absent). Extend a note without rewriting it.",
                         {"name": {"type": "string"}, "content": {"type": "string"}}, ["name", "content"]),
                _fn_spec("kb_edit", "Revise a knowledge-base note in place: replace the exact (unique) "
                         "substring `old` with `new`. Use to FIX or update existing knowledge. Read the "
                         "note first to copy an exact snippet.",
                         {"name": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
                         ["name", "old", "new"]),
            ]
        if self.mem is not None:
            specs += [
                _fn_spec("memory_list", "List cross-run memory note paths (relative markdown files).", {}, []),
                _fn_spec("memory_read", "Read a memory note by its (relative) path.",
                         {"name": {"type": "string"}}, ["name"]),
            ]
            if self.writable:
                specs += [
                    _fn_spec("memory_write", "Create or overwrite a memory note (markdown topic file), e.g. "
                             "'pitfalls.md'. Memory is for dev-process lessons learned across runs.",
                             {"name": {"type": "string"}, "content": {"type": "string"}}, ["name", "content"]),
                    _fn_spec("memory_edit", "Revise a memory note in place (replace unique `old` with `new`).",
                             {"name": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
                             ["name", "old", "new"]),
                    _fn_spec("remember", "Quickly save a lesson to cross-run memory — a recurring mistake to "
                             "avoid or a tip for future runs. Appended as a dated bullet to a topic file "
                             "(default 'lessons.md'; pass `topic` to group it). Becomes searchable.",
                             {"text": {"type": "string"}, "topic": {"type": "string"}}, ["text"]),
                ]
        return specs

    # ---- dispatch ----
    def execute(self, name: str, args: dict) -> str:
        try:
            if name == "kb_search":
                hits = self._search(args.get("query", ""))
                return "\n---\n".join(f"{label}:\n{text[:600]}" for label, text in hits) or "(no notes)"
            if name == "grep":
                if self.kb is None:
                    return "(no knowledge base configured)"
                return "\n".join(self.kb.grep(args.get("pattern", ""), max_hits=20)) or "(no matches)"
            if name == "list_notes":
                if self.kb is None:
                    return "(no knowledge base configured)"
                return "\n".join(self.kb.list()) or "(empty)"
            if name == "kb_tree":
                if self.kb is None:
                    return "(no knowledge base configured)"
                return self.kb.tree()
            if name == "read_note":
                if self.kb is None:
                    return "(no knowledge base configured)"
                text = self.kb.read(args.get("name", ""))
                return text if text is not None else f"(no such note: {args.get('name')})"
            if name in ("kb_write", "kb_append", "kb_edit"):
                if not (self.writable and self.kb is not None):
                    return "(knowledge base is read-only or not configured)"
                return self._mutate(self.kb, name.split("_", 1)[1], args, "knowledge-base note")
            if name == "memory_list":
                return ("\n".join(self.mem.list()) or "(empty)") if self.mem else "(no memory configured)"
            if name == "memory_read":
                if self.mem is None:
                    return "(no memory configured)"
                text = self.mem.read(args.get("name", ""))
                return text if text is not None else f"(no such note: {args.get('name')})"
            if name in ("memory_write", "memory_edit"):
                if not (self.writable and self.mem is not None):
                    return "(memory is read-only or not configured)"
                return self._mutate(self.mem, name.split("_", 1)[1], args, "memory note")
            if name == "remember":
                if not (self.writable and self.mem is not None):
                    return "(memory is read-only or not configured)"
                text = (args.get("text", "") or "").strip()
                if not text:
                    return "(nothing to remember: empty text)"
                # Sanitize the topic to a clean filename: strip any path, then inspect the base name
                # (without a trailing '.md'). Fall back to 'lessons' for a degenerate value — empty,
                # a bare extension like '.md', or a dotfile — so the store's forced '.md' suffix can
                # never build a '.md.md' or hidden-file note name.
                topic = Path((args.get("topic") or "lessons")).name
                base = topic[:-3] if topic.endswith(".md") else topic
                if not base or base.startswith(".") or not any(c.isalnum() for c in base):
                    topic = "lessons"
                rel = self.mem.append(topic, f"- {text}")
                return f"(remembered in {rel})" if rel else "(could not write memory note)"
        except Exception as e:  # noqa: BLE001 — tool errors are fed back to the model
            return f"(tool error: {e})"
        return f"(unknown tool: {name})"

    @staticmethod
    def _mutate(store: MarkdownStore, op: str, args: dict, label: str) -> str:
        """Shared write/append/edit dispatch for either store (KB or memory)."""
        if op == "edit":
            return store.edit(args.get("name", ""), args.get("old", ""), args.get("new", ""))
        fn = store.write(args.get("name", ""), args.get("content", "")) if op == "write" \
            else store.append(args.get("name", ""), args.get("content", ""))
        if fn is None:
            return f"(bad {label} path: {args.get('name')!r})"
        return f"({'wrote' if op == 'write' else 'appended to'} {fn})"
