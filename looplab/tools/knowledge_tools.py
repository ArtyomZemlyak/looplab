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
from datetime import datetime, timezone
from pathlib import Path

from looplab.core.atomicio import atomic_write_text
from looplab.core.memory_window import read_memory_jsonl_window
from looplab.core.redact import redact_persisted_text
from looplab.core import _pathsafe
from looplab.tools._base import clip, fn_spec
from looplab.tools.perm_modes import (
    DEFAULT_MODE, authorize, default_approver)
from looplab.tools.retrieval import glob_files, grep, read_file
from looplab.tools.vectorstore import InMemoryVectorStore, Item, cosine, hash_embed
from looplab.trust.cross_run import LessonScope

#: How much of ONE `kb_search` hit is delivered. Unchanged from the bare `[:600]` it replaces — this
#: is a bound on WHAT is kept, not on how much: `kb_search` returns up to `k` hits plus up to `k`
#: anchor-expansions, and six 600-char hits plus the header already sit just under the tool loop's
#: `RESULT_CAP`. What changed is that the cut now SAYS it cut (doc 25 TO-08 — five providers wrote
#: their own silent clip; a hit that stops mid-recipe and looks whole is the case-params defect one
#: layer up), and that each record orders itself so its payload is above the line.
_KB_HIT_CHARS = 600


def _kb_hit(text: str) -> str:
    """One retrieved record, bounded and honest about it."""
    return clip(str(text), _KB_HIT_CHARS, note="\n…[+{n} chars of this record not shown]")


def _abstraction_of(payload: dict):
    """Rebuild the `Abstraction` a harmonic payload carries (for merging during a consolidating build)."""
    from looplab.tools.memora import Abstraction
    return Abstraction(str(payload.get("abstraction", "")), list(payload.get("anchors", [])))


def _readable_repo_path(p: Path) -> bool:
    """Is this repo path safe to stream back into the (possibly REMOTE) model context?

    `_pathsafe.looks_secret` is not enough on its own here. It knows `.ssh`/`.aws`/`.env` but NOT
    `.git`, and a clone made with a credentialed HTTPS remote parks that credential in plain text
    in `.git/config` (`url = https://user:ghp_…@github.com/org/repo`) — a routine pattern for
    private repos in CI/hub environments. `repo_list` already filtered `.git` out; `repo_grep` and
    `repo_read` did not, so the Researcher could reach the credential with an ordinary
    `repo_grep(pattern="http")` or `repo_read(".git/config")` and it would ride into the prompt.
    The unconditional `redact_secrets` in `agents/tool_loop.py` protects only the durable trace
    PREVIEW, not the model-bound message, so the filter has to happen here.

    Two gates, matching what `RepoScoutTools._read_file` has always applied to the same class of
    content: no `.git` internals, and a known source/doc/config extension. `.gitignore` and
    `.dockerignore` are in `_pathsafe.SAFE_NAMES` and stay readable — only paths that live INSIDE
    a `.git` directory are excluded.
    """
    return ".git" not in p.parts and _pathsafe.readable(p)


def _recursive_glob(pattern: str) -> str:
    """Make a bare `*.py` recursive (doc 25 TO-06).

    `repo_list` has always walked the whole tree (`retrieval.glob_files` is rglob-shaped), while
    `RepoScoutTools._find_files` runs a pathlib glob where `*.py` matches ONE level. Handing the
    pattern over unchanged would silently stop showing the Researcher every file in a subdirectory —
    the loudest possible regression from a refactor that was supposed to change nothing.
    """
    pattern = pattern or "*"
    return pattern if ("**" in pattern or "/" in pattern) else f"**/{pattern}"




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
        # ONE walker, one secret gate, one budget (doc 25 TO-06). This class used to re-implement the
        # walk, and its guards had drifted: no file budget at all, no skip-dirs, no per-file size
        # skip, and a silent 40-hit cut where `_grep` clamps and says `(capped at N hits)`. The
        # `<repo>/<path>` mount prefix stays OURS — `RepoScoutTools._resolve` knows `default_root`
        # and CWD, not named mounts — so `_resolve` below still maps the model's path, and the scout
        # is handed the ABSOLUTE result. `named_roots` makes its `_disp` render exactly the
        # `<name>/<rel>` labels this tool has always emitted.
        from looplab.tools.reposcout import RepoScoutTools
        self._scout = RepoScoutTools(
            list(self.roots.values()), default_root=self.roots.get("."),
            named_roots=list(self.roots.items()))

    def specs(self) -> list[dict]:
        names = ", ".join(self.roots)
        return [
            fn_spec("repo_grep", f"Regex search across the editable repo source ({names}). "
                     "Returns matching <repo>/<path>:<line> hits.",
                     {"pattern": {"type": "string"}, "glob": {"type": "string"}}, ["pattern"]),
            fn_spec("repo_list", f"List source files in an editable repo ({names}).",
                     {"repo": {"type": "string"}, "glob": {"type": "string"}}, []),
            fn_spec("repo_read", "Read a file from an editable repo, given a <repo>/<path> "
                     "(or just <path> for the root repo). Returns ONE page of at most ~3600 chars; "
                     "window with start_line (+ optional lines). A page with more file below it ENDS "
                     "with '… (more below — continue with start_line=N)' — continue from exactly that "
                     "N (a single line longer than one page is cut mid-line — the marker says so and "
                     "resumes at the NEXT line); a reply WITHOUT that marker IS the end of the file. "
                     "Never re-read from the top.",
                     {"path": {"type": "string"},
                      "start_line": {"type": "integer", "description": "1-based line to start from (default top)"},
                      "lines": {"type": "integer", "description": "how many lines to return (optional window)"}},
                     ["path"]),
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
                # One block per mount, each carrying the scout's OWN receipt (`(capped at N hits)`,
                # `(stopped after 4000 files…)`). Merging the hit lines into a single 40-line cut is
                # what made an overflowing search read as an exhaustive one — a per-mount block keeps
                # every partial answer labelled as partial. `skip_hidden=False`: `.github/*.yml` is
                # ordinary repo source for a Researcher, and `.git` is pruned by `_SKIP_DIRS` anyway.
                blocks = []
                for root in self.roots.values():
                    block = self._scout._grep(args.get("pattern", ""), str(root), glob, 40,
                                              skip_hidden=False)
                    if block and not block.startswith("(grep:"):
                        blocks.append(block)
                return "\n".join(blocks) or "(no matches)"
            if name == "repo_list":
                repo = args.get("repo") or ("." if "." in self.roots else next(iter(self.roots)))
                root = self.roots.get(repo)
                if root is None:
                    return f"(no such repo: {repo}; have: {', '.join(self.roots)})"
                return self._scout._find_files(str(root), _recursive_glob(args.get("glob") or "*"))
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
                if not _readable_repo_path(target):
                    # KEPT here, not delegated: `_pathsafe.looks_secret` (and therefore the scout's
                    # own gate) does not know `.git`, so a credentialed clone's `.git/config` would
                    # pass every check the scout makes. See the module-level helper.
                    return (f"(refused: {target.name} is not a readable source file — "
                            "repository internals and binaries are not returned)")
                # The scout owns the read: its size fence, its full-file-then-paginate contract (M9 —
                # a blind [:max_bytes] head made the agent re-read the same file 8×, and a 200KB cut
                # reported EOF for a larger file), and its `(more below — continue with start_line=N)`
                # marker. It is handed the ABSOLUTE path `_resolve` already confined to a mount.
                return self._scout._read_file(str(target), args.get("start_line", 0),
                                              args.get("lines", 0))
        except Exception as e:  # noqa: BLE001 — tool errors are fed back to the model
            return f"(tool error: {e})"
        return f"(unknown tool: {name})"


class KnowledgeWriteTools:
    """Lets an agent SAVE a distilled note into the shared knowledge base (`knowledge_dir`) so FUTURE
    runs' Researchers find it via `kb_search`. Deliberately narrow + benign — it only appends a single
    markdown file under the KB dir (no arbitrary path, no shell, no git). It still mutates shared
    cross-run state: plan mode omits/denies it and other
    modes apply the centralized permission policy. This is the write half of the knowledge base whose
    read half is `KnowledgeTools`."""

    def __init__(self, knowledge_dir: str | None = None, *, mode: str = DEFAULT_MODE,
                 approver=None):
        self.dir = Path(knowledge_dir).resolve() if knowledge_dir else None
        self.mode = mode
        self.approver = approver or default_approver

    def specs(self) -> list[dict]:
        if not self.dir or self.mode == "plan":
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
            action = {
                "tool": "remember", "tool_kind": "knowledge_write",
                "label": f"remember {title[:80]}", "verb": "save a shared knowledge note",
                "path": str(self.dir), "preview": title[:4000],
                "scope": {
                    "knowledge_dir": str(self.dir),
                    "note_digest": hashlib.sha256(json.dumps(
                        {"title": title, "note": note, "tags": tags}, sort_keys=True,
                        ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest(),
                },
            }
            refusal = authorize(
                self.mode, self.approver, action,
                denied=("(remember is disabled in read-only plan mode. Switch to "
                        "default/acceptEdits/auto to save shared knowledge.)"),
                declined=f"remember {title[:80]}")
            if refusal:
                return refusal
            self.dir.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or "note"
            # content-hash id: re-saving the same note overwrites (idempotent) instead of piling duplicates.
            sid = hashlib.sha1((title + "\n" + note).encode("utf-8")).hexdigest()[:8]
            path = self.dir / f"{slug}-{sid}.md"
            body = ("---\n"
                    "v: 1\n"
                    "actor: owner-assistant\n"
                    "surface: remember\n"
                    f"created_at: {datetime.now(timezone.utc).isoformat()}\n"
                    "source_refs: unknown\n"
                    "---\n\n"
                    f"# {title}\n\n{note}\n")
            if tags:
                body += "\n_tags: " + ", ".join(tags) + "_\n"
            atomic_write_text(path, body)
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
        self._scope = LessonScope()
        self._index = InMemoryVectorStore()
        self._index_revision = ""
        self._case_window_health = None
        self._build_index()

    def bind_state(self, state, parent=None) -> None:
        """Bind case retrieval to the same live scope as lessons and cross-run tools.

        Knowledge Markdown remains operator-authored portfolio knowledge.  Cases are run-authored
        outcome memory and must be authorized/scoped *before* embedding or Memora consolidation;
        filtering after a lossy merge cannot recover a compatible member that was discarded.
        """
        next_scope = LessonScope.of(state)
        current = (self._scope.bound, self._scope.run_uid, self._scope.run_id,
                   self._scope.task_id, self._scope.direction, self._scope.goal_terms)
        updated = (next_scope.bound, next_scope.run_uid, next_scope.run_id,
                   next_scope.task_id, next_scope.direction, next_scope.goal_terms)
        self._scope = next_scope
        if current != updated:
            self._build_index()

    def _source_revision(self) -> str:
        """Stable identity of the files feeding the in-memory index; unavailable files stay explicit."""
        identities: list[tuple[str, int, int]] = []
        paths = [Path(p) for p in glob_files("*.md", str(self.dir))] if self.dir else []
        if self.cases_path:
            paths.append(self.cases_path)
        for path in sorted(set(paths), key=lambda item: str(item)):
            try:
                stat = path.stat()
                identities.append((str(path), int(stat.st_size), int(stat.st_mtime_ns)))
            except OSError:
                identities.append((str(path), -1, -1))
        return hashlib.sha256(
            json.dumps(identities, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _records(self):
        """(id, index_source, payload) triples for every note + case, before embedding — so the raw
        vs. harmonic build paths share one collection pass."""
        recs = []
        self._case_window_health = None
        if self.dir:
            for p in glob_files("*.md", str(self.dir)):
                text = read_file(p)
                recs.append((p, Path(p).name + " " + text, {"path": p, "text": text}))
        # Cross-run memory (I19): past best solutions become searchable knowledge.
        if self.cases_path:
            from looplab.engine.memory import valid_case_record

            case_rows, self._case_window_health = read_memory_jsonl_window(self.cases_path)
            for i, c in case_rows:
                # valid JSON is not necessarily a valid case. Apply the writer/search schema
                # fence here too so a poisoned goal/params/metric cannot crash or enter agent retrieval.
                if c is None or not valid_case_record(c):
                    self._case_window_health["skipped"] += 1
                    continue
                if c.get("active") is False:
                    continue
                if not self._scope.allows(c):
                    continue
                goal = c.get("goal") if isinstance(c.get("goal"), str) else ""
                rationale = c.get("rationale") if isinstance(c.get("rationale"), str) else ""
                # `valid_case_record` requires `params` to be a dict but places no bound on its size
                # or content, and this text is sent to the embedding/abstractor PROVIDER and returned
                # verbatim by `kb_search` — to a different run than the one that wrote it. Stringifying
                # it raw therefore shipped whatever a past solution happened to put in its params
                # (credentials, injected instructions, unbounded blobs) across that boundary. Route it
                # through the same always-on persisted-boundary sanitizer the memo/trace writers use.
                # Preserve objective identity in both the scope gate and rendered evidence.  This
                # prevents identical goals with opposite min/max objectives from becoming equivalent
                # semantic hits after consolidation.
                params = redact_persisted_text(c.get("params"), max_chars=2000, single_line=True)
                # THE PAYLOAD LEADS. A hit is delivered head-clipped at `_KB_HIT_CHARS`, and the
                # goal used to be first — so on the only real case in the shared store
                # (`rubertlite-dr-unified-v8`) `best params=` began at char 691 of a 1,610-char
                # record and NEVER fit: what a role received was 600 chars of the task prompt it was
                # already holding, since the scope gate above admits a case only on an exact task id
                # or a strict goal-fingerprint overlap. The toy cases fit (two parameters), which is
                # exactly why nobody saw it. The params are the one thing a case holds that the
                # meta-note beside it does not, so they go first and the goal — the discriminator
                # that says WHICH problem this was measured on, which matters because `repo_task` is
                # one task id over several repos — goes last, where a clip can take it.
                text = (f"PAST CASE ({c.get('task_id')}, objective={c.get('direction')}) "
                        f"metric={c.get('metric')}, run {c.get('run_id') or 'unknown'}:\n"
                        f"params={params}\n"
                        f"why: {rationale}\n"
                        f"measured on this goal: {goal}")
                recs.append((f"case:{i}", goal + " " + text,
                             {"path": f"case:{c.get('task_id')}", "text": text,
                              "source_kind": "case", "task_id": c.get("task_id"),
                              "direction": c.get("direction"), "run_id": c.get("run_id"),
                              "run_uid": c.get("run_uid"), "member_ids": [f"case:{i}"]}))
        return recs

    def _build_index(self) -> None:
        # OPEN[knowledge-index-re-embeds-every-record] this re-embeds every KB doc and case from
        # scratch on each rebuild, and a rebuild fires whenever `_source_revision` changes — i.e.
        # every append to the case store — so an unchanged record is paid for again on every write.
        # proof:absent:_vector_memo@looplab/tools/knowledge_tools.py
        # The spend became VISIBLE on 2026-09-02 (`LLMEmbedder` now carries a `CostAccountant`, so
        # these calls reach `llm_usage` and `looplab tokens`); what is still open is not paying it.
        # `InMemoryVectorStore` has no persistence by design, but the embeddings are a pure function
        # of (model, text) and could be memoized by content digest across rebuilds within a process
        # — the same shape `make_abstractor`'s content-hash cache already uses one layer over.
        # CLOSE IT WITH A NUMBER, not with the cache: nobody has measured records-per-rebuild or
        # rebuilds-per-run on a real corpus (`runs/` is empty on the box this was written on), and a
        # cache sized without that is the unmeasured policy this repo refuses elsewhere. The meter
        # to read it off now exists.
        self._index = InMemoryVectorStore()
        self._index_revision = self._source_revision()
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
                # Scope/authorization has already run.  Keep unlike source/semantic partitions
                # separate anyway: a richer operator note must never replace a case, and two task
                # contracts must not collapse merely because their prose is similar.
                partition = (pl.get("source_kind", "knowledge"), pl.get("direction"),
                             pl.get("task_id"))
                prior_partition = (it.payload.get("source_kind", "knowledge"),
                                   it.payload.get("direction"), it.payload.get("task_id"))
                if partition != prior_partition:
                    continue
                if cosine(vec, it.vector) >= self.consolidate_threshold:
                    prev = _abstraction_of(it.payload)
                    m = prev.merge(ab)
                    if len(pl["text"]) > len(it.payload["text"]):
                        it.payload["text"] = pl["text"]     # keep the richer memory value
                        it.payload["path"] = pl["path"]
                    it.payload["abstraction"] = m.primary
                    it.payload["anchors"] = list(m.anchors)
                    it.payload["merged"] = int(it.payload.get("merged", 1)) + 1
                    members = list(it.payload.get("member_ids") or [it.id])
                    for member in pl.get("member_ids") or [rid]:
                        if member not in members:
                            members.append(member)
                    it.payload["member_ids"] = members
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
                revision = self._source_revision()
                if revision != self._index_revision:
                    self._build_index()
                q = args.get("query", "")
                # Embed the query in the SAME space as the index. When a HARMONIC (abstraction-keyed)
                # index is in use (self.abstract set — _build_index keys each entry by
                # embed(abstract(src).index_text())), abstract the query too: scoring a RAW query vector
                # against abstraction+anchor keys lives in a different textual space, dampening cosine
                # and losing the anchor weighting. Mirrors retrieve_lessons_harmonic (abstracts both sides).
                qvec = (self.embed(self.abstract(q).index_text())
                        if self.abstract is not None else self.embed(q))
                hits = self._index.search("kb", qvec, self.k)
                out = [f"{Path(h.payload['path']).name}"
                       + (f" [members={','.join(h.payload.get('member_ids') or [h.id])}]"
                          if h.payload.get("member_ids") else "")
                       + f":\n{_kb_hit(h.payload['text'])}" for h in hits]
                # Anchor-expansion (Memora): follow the top hits' cue anchors to related-but-not-
                # similar notes the plain query missed. No-op on a legacy (no-anchor) index.
                if self.expand and self.abstract is not None:
                    from looplab.tools.memora import expand_by_anchors
                    for h in expand_by_anchors(self._index, "kb", hits, self.embed, k=self.k):
                        out.append(f"[related via anchors] {Path(h.payload['path']).name}:\n"
                                   f"{_kb_hit(h.payload['text'])}")
                mode = ("hash" if self.embed is hash_embed else "semantic")
                header = (f"[KB_INDEX: revision={self._index_revision[:16]}; mode={mode}; "
                          f"scope={'run' if self._scope.bound else 'portfolio'}]")
                if self._case_window_health is not None:
                    health = self._case_window_health
                    header += (f"\n[CASE_SOURCE_SNAPSHOT: sha256={health['window_digest']}; "
                               f"rows={health['source_rows']}; "
                               f"truncated={'true' if health['source_window_truncated'] else 'false'}; "
                               f"skipped={health['skipped']}; "
                               f"unavailable={'true' if health['unavailable'] else 'false'}.]")
                return header + "\n" + ("\n---\n".join(out) or "(no notes in this index scope)")
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
                # Same secret gate every sibling reader applies (RepoTools.repo_read,
                # RepoScoutTools._read_file). `.name` already blocks traversal, but knowledge_dir is
                # OPERATOR-CONFIGURABLE and is not .md-only — it already holds `.memora_cache.json` —
                # so pointing it at a directory that also contains an `.env`/`id_rsa` let
                # `read_note(".env")` stream that file to the (possibly remote) model, even though
                # `list_notes` (*.md only) never advertised it. Defense in depth, not traversal
                # defense: the reachable set is one directory, and this is what keeps a credential in
                # it out of the prompt.
                if _pathsafe.looks_secret(Path(target.name)):
                    return f"(refused: {target.name} looks like a secret/credential)"
                return read_file(str(target))[:4000]
        except Exception as e:  # noqa: BLE001 — tool errors are fed back to the model
            return f"(tool error: {e})"
        return f"(unknown tool: {name})"
