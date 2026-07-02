"""Agentic retrieval toolset (ADR-16) for the LLM Researcher: lexical (grep), file
(list/read), and semantic (kb_search) tools over a knowledge directory of markdown
notes. The model chooses which to call. File access is restricted to the knowledge
directory (no arbitrary reads).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import _pathsafe
from .retrieval import glob_files, grep, read_file
from .vectorstore import InMemoryVectorStore, Item, hash_embed


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
                        rp = hp.relative_to(root)
                        if _pathsafe.looks_secret(rp):
                            continue            # don't stream secret-file contents into the LLM prompt
                        out.append(f"{pre}{rp.as_posix()}:{h.lineno}: {h.line}")
                return "\n".join(out[:40]) or "(no matches)"
            if name == "repo_list":
                repo = args.get("repo") or ("." if "." in self.roots else next(iter(self.roots)))
                root = self.roots.get(repo)
                if root is None:
                    return f"(no such repo: {repo}; have: {', '.join(self.roots)})"
                glob = args.get("glob") or "*"
                files = [Path(p).resolve().relative_to(root).as_posix()
                         for p in glob_files(glob, str(root))
                         if ".git" not in Path(p).parts
                         and not _pathsafe.looks_secret(Path(p).resolve().relative_to(root))]
                return "\n".join(sorted(files)[:100]) or "(empty)"
            if name == "repo_read":
                target = self._resolve(args.get("path", ""))
                if target is None or not target.is_file():
                    return f"(no such file: {args.get('path')})"
                # Refuse to read credential files back into the (possibly remote) model context.
                for r in self.roots.values():
                    try:
                        if _pathsafe.looks_secret(target.relative_to(r)):
                            return f"(refused: {target.name} looks like a secret/credential)"
                    except ValueError:
                        continue
                return read_file(str(target))[:self.max_bytes]
        except Exception as e:  # noqa: BLE001 — tool errors are fed back to the model
            return f"(tool error: {e})"
        return f"(unknown tool: {name})"


class KnowledgeTools:
    def __init__(self, knowledge_dir: str | None = None,
                 cases_path: str | None = None, k: int = 3):
        self.dir = Path(knowledge_dir).resolve() if knowledge_dir else None
        self.cases_path = Path(cases_path) if cases_path else None
        self.k = k
        self._index = InMemoryVectorStore()
        self._build_index()

    def _build_index(self) -> None:
        items = []
        if self.dir:
            for p in glob_files("*.md", str(self.dir)):
                text = read_file(p)
                items.append(Item(id=p, vector=hash_embed(Path(p).name + " " + text),
                                  payload={"path": p, "text": text}))
        # Cross-run memory (I19): past best solutions become searchable knowledge.
        if self.cases_path and self.cases_path.exists():
            for i, line in enumerate(self.cases_path.read_text(encoding="utf-8").splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:                       # a single malformed case line must not kill indexing
                    c = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(c, dict):
                    continue
                text = (f"PAST CASE — task {c.get('task_id')}: {c.get('goal','')}\n"
                        f"best params={c.get('params')} metric={c.get('metric')}\n"
                        f"{c.get('rationale','')}")
                items.append(Item(id=f"case:{i}", vector=hash_embed(c.get("goal", "") + " " + text),
                                  payload={"path": f"case:{c.get('task_id')}", "text": text}))
        if items:
            self._index.upsert("kb", items)

    # ---- tool schemas (OpenAI function format) ----
    def specs(self) -> list[dict]:
        return [
            _fn_spec("kb_search", "Semantic search over the knowledge base; returns relevant note snippets.",
                     {"query": {"type": "string"}}, ["query"]),
            _fn_spec("grep", "Regex search across knowledge notes (*.md). Returns matching lines.",
                     {"pattern": {"type": "string"}}, ["pattern"]),
            _fn_spec("list_notes", "List available knowledge note filenames.", {}, []),
            _fn_spec("read_note", "Read a knowledge note by filename.",
                     {"name": {"type": "string"}}, ["name"]),
        ]

    # ---- dispatch ----
    def execute(self, name: str, args: dict) -> str:
        try:
            if name == "kb_search":
                hits = self._index.search("kb", hash_embed(args.get("query", "")), self.k)
                return "\n---\n".join(
                    f"{Path(h.payload['path']).name}:\n{h.payload['text'][:600]}" for h in hits
                ) or "(no notes)"
            if name == "grep":
                if not self.dir:
                    return "(no notes directory)"
                hits = grep(args.get("pattern", ""), str(self.dir), glob="*.md", max_hits=20)
                return "\n".join(f"{Path(h.path).name}:{h.lineno}: {h.line}" for h in hits) or "(no matches)"
            if name == "list_notes":
                if not self.dir:
                    return "(no notes directory)"
                return "\n".join(Path(p).name for p in glob_files("*.md", str(self.dir))) or "(empty)"
            if name == "read_note":
                if not self.dir:
                    return "(no notes directory)"
                target = (self.dir / Path(args.get("name", "")).name)  # restrict to kb dir
                if not target.exists():
                    return f"(no such note: {args.get('name')})"
                return read_file(str(target))[:4000]
        except Exception as e:  # noqa: BLE001 — tool errors are fed back to the model
            return f"(tool error: {e})"
        return f"(unknown tool: {name})"
