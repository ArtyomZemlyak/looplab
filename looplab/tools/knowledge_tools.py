"""Agentic retrieval toolset (ADR-16) for the LLM Researcher: lexical (grep), file
(list/read), and semantic (kb_search) tools over a knowledge directory of markdown
notes. The model chooses which to call. File access is restricted to the knowledge
directory (no arbitrary reads).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from looplab.core import _pathsafe
from looplab.tools._base import fn_spec
from looplab.tools.retrieval import glob_files, grep, read_file
from looplab.tools.vectorstore import InMemoryVectorStore, Item, cosine, hash_embed


def _abstraction_of(payload: dict):
    """Rebuild the `Abstraction` a harmonic payload carries (for merging during a consolidating build)."""
    from looplab.tools.memora import Abstraction
    return Abstraction(str(payload.get("abstraction", "")), list(payload.get("anchors", [])))




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
            fn_spec("repo_grep", f"Regex search across the editable repo source ({names}). "
                     "Returns matching <repo>/<path>:<line> hits.",
                     {"pattern": {"type": "string"}, "glob": {"type": "string"}}, ["pattern"]),
            fn_spec("repo_list", f"List source files in an editable repo ({names}).",
                     {"repo": {"type": "string"}, "glob": {"type": "string"}}, []),
            fn_spec("repo_read", "Read a file from an editable repo, given a <repo>/<path> "
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


class KnowledgeWriteTools:
    """Lets an agent SAVE a distilled note into the shared knowledge base (`knowledge_dir`) so FUTURE
    runs' Researchers find it via `kb_search`. Deliberately narrow + benign — it only appends a single
    markdown file under the KB dir (no arbitrary path, no shell, no git) — so it's safe to expose even
    in the assistant's read-only 'plan' mode, unlike the general write tools. This is the write half of
    the knowledge base whose read half is `KnowledgeTools`."""

    def __init__(self, knowledge_dir: str | None = None):
        self.dir = Path(knowledge_dir).resolve() if knowledge_dir else None

    def specs(self) -> list[dict]:
        if not self.dir:
            return []
        return [fn_spec(
            "remember",
            "Save a distilled note to the shared KNOWLEDGE BASE so FUTURE runs' Researchers can find it "
            "(via kb_search). Use it whenever the user shares experiment results, lessons, recipes, or "
            "domain facts worth keeping across runs. Distill to the essentials: what was tried, the "
            "result/metric, and the takeaway or lesson — write it so a future run benefits.",
            {"title": {"type": "string", "description": "Short descriptive title for the note."},
             "note": {"type": "string", "description": "The knowledge in markdown: what was tried, the "
                      "result/metric, and the takeaway. Be specific and self-contained."},
             "tags": {"type": "array", "items": {"type": "string"},
                      "description": "Optional keywords to aid retrieval (e.g. task/domain/method)."}},
            ["title", "note"])]

    def execute(self, name: str, args: dict) -> str:
        if name != "remember":
            return f"(unknown tool: {name})"
        if not self.dir:
            return "error: no knowledge base configured (set knowledge_dir) — cannot save the note."
        try:
            title = str((args or {}).get("title") or "note").strip()
            note = str((args or {}).get("note") or "").strip()
            if not note:
                return "error: `note` is empty — nothing to remember."
            raw_tags = (args or {}).get("tags") or []
            if not isinstance(raw_tags, (list, tuple)):   # a junk model may pass a scalar
                raw_tags = [raw_tags]
            tags = [str(t) for t in raw_tags if str(t).strip()]
            self.dir.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or "note"
            # content-hash id: re-saving the same note overwrites (idempotent) instead of piling duplicates.
            sid = hashlib.sha1((title + "\n" + note).encode("utf-8")).hexdigest()[:8]
            path = self.dir / f"{slug}-{sid}.md"
            body = f"# {title}\n\n{note}\n"
            if tags:
                body += "\n_tags: " + ", ".join(tags) + "_\n"
            path.write_text(body, encoding="utf-8")
            return (f"saved to the knowledge base as {path.name} — future runs will find it via "
                    f"kb_search (KB: {self.dir}).")
        except Exception as e:  # noqa: BLE001 - a full/read-only KB disk must not kill the whole turn
            return f"(error saving to the knowledge base: {e})"


class KnowledgeTools:
    def __init__(self, knowledge_dir: str | None = None,
                 cases_path: str | None = None, k: int = 3, embed=None,
                 abstract=None, expand: bool = True, consolidate_threshold: float = 0.86):
        self.dir = Path(knowledge_dir).resolve() if knowledge_dir else None
        self.cases_path = Path(cases_path) if cases_path else None
        self.k = k
        # T4: one embedder builds AND queries the index (consistent dim). Defaults to the lexical
        # hash_embed; `make_embedder(settings)` supplies a real LLM embedder when configured.
        self.embed = embed or hash_embed
        # Memora (opt-in): an `abstract` callable (see tools.memora.make_abstractor) switches the index
        # from raw-text to abstraction+anchor keying, CONSOLIDATES near-duplicate notes/cases at build
        # time, and lets `kb_search` EXPAND through anchors. None -> byte-identical legacy indexing.
        self.abstract = abstract
        self.expand = expand
        self.consolidate_threshold = consolidate_threshold
        self._index = InMemoryVectorStore()
        self._build_index()

    def _records(self):
        """(id, index_source, payload) triples for every note + case, before embedding — so the raw
        vs. harmonic build paths share one collection pass."""
        recs = []
        if self.dir:
            for p in glob_files("*.md", str(self.dir)):
                text = read_file(p)
                recs.append((p, Path(p).name + " " + text, {"path": p, "text": text}))
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
                recs.append((f"case:{i}", c.get("goal", "") + " " + text,
                             {"path": f"case:{c.get('task_id')}", "text": text}))
        return recs

    def _build_index(self) -> None:
        recs = self._records()
        if not recs:
            return
        if self.abstract is None:                        # legacy: embed raw text, no anchors/merge
            self._index.upsert("kb", [Item(id=rid, vector=self.embed(src), payload=pl)
                                      for rid, src, pl in recs])
            return
        # Harmonic build: key each entry by its abstraction+anchors and CONSOLIDATE near-duplicates
        # (same abstraction) into one entry, keeping the richer text — so the index carries roughly
        # half the entries of a flat store instead of a chain of partial duplicates.
        kept: list[Item] = []
        for rid, src, pl in recs:
            ab = self.abstract(src)
            vec = self.embed(ab.index_text())
            merged = False
            for it in kept:
                if cosine(vec, it.vector) >= self.consolidate_threshold:
                    prev = _abstraction_of(it.payload)
                    m = prev.merge(ab)
                    if len(pl["text"]) > len(it.payload["text"]):
                        it.payload["text"] = pl["text"]     # keep the richer memory value
                        it.payload["path"] = pl["path"]
                    it.payload["abstraction"] = m.primary
                    it.payload["anchors"] = list(m.anchors)
                    it.payload["merged"] = int(it.payload.get("merged", 1)) + 1
                    it.vector = self.embed(m.index_text())
                    merged = True
                    break
            if not merged:
                kept.append(Item(id=rid, vector=vec,
                                 payload={**pl, "abstraction": ab.primary, "anchors": list(ab.anchors)}))
        self._index.upsert("kb", kept)

    # ---- tool schemas (OpenAI function format) ----
    def specs(self) -> list[dict]:
        return [
            fn_spec("kb_search", "Semantic search over the knowledge base; returns relevant note snippets.",
                     {"query": {"type": "string"}}, ["query"]),
            fn_spec("grep", "Regex search across knowledge notes (*.md). Returns matching lines.",
                     {"pattern": {"type": "string"}}, ["pattern"]),
            fn_spec("list_notes", "List available knowledge note filenames.", {}, []),
            fn_spec("read_note", "Read a knowledge note by filename.",
                     {"name": {"type": "string"}}, ["name"]),
        ]

    # ---- dispatch ----
    def execute(self, name: str, args: dict) -> str:
        try:
            if name == "kb_search":
                hits = self._index.search("kb", self.embed(args.get("query", "")), self.k)
                out = [f"{Path(h.payload['path']).name}:\n{h.payload['text'][:600]}" for h in hits]
                # Anchor-expansion (Memora): follow the top hits' cue anchors to related-but-not-
                # similar notes the plain query missed. No-op on a legacy (no-anchor) index.
                if self.expand and self.abstract is not None:
                    from looplab.tools.memora import expand_by_anchors
                    for h in expand_by_anchors(self._index, "kb", hits, self.embed, k=self.k):
                        out.append(f"[related via anchors] {Path(h.payload['path']).name}:\n"
                                   f"{h.payload['text'][:600]}")
                return "\n---\n".join(out) or "(no notes)"
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
