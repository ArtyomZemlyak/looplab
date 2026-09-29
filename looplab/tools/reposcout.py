"""Read-only filesystem scout tools for the pre-run genesis BOSS.

So the boss can actually INSPECT a repo on this machine (list dirs, read text files, glob) before
authoring a `repo` task spec + an adaptation checklist — instead of only promising to. It drops in
behind the same tool-provider protocol as RunTools (`.specs()` / `.execute(name, args)`), so it runs
in the shared `agent.drive_tool_loop`.

Trusted-local only: the operator points the boss at their OWN repo via the localhost UI (the genesis
endpoint is also behind the optional UI token). Defensively bounded, because the tool RESULTS are fed
to the model (possibly a REMOTE provider):
  - every path is resolved and must live under an allowed root (home + the run-root); a `..`/symlink
    escape resolves out and is refused;
  - read is an ALLOWLIST — only known source/doc/config extensions (and a few safe extensionless
    names like Makefile/Dockerfile/README) are returned; anything else is "exists, not read", so an
    unrecognized dotfile can't be slurped;
  - on TOP of that, credential files (.env, secrets/keys, ~/.ssh, ~/.aws, ~/.kube, ~/.docker, gcloud,
    and any name containing secret/credential/password/api_key/private/id_rsa) are refused AND hidden
    from list_dir/find_files — so a secret (incl. the LLM API key in the server env) can't reach the
    model via contents OR via a revealed filename.
"""
from __future__ import annotations

import io
from fnmatch import fnmatch, fnmatchcase
from pathlib import Path

from looplab.core import _pathsafe
from looplab.core.node_evidence import read_bounded_regular_file
from looplab.tools._base import (   # shared schema builder, bounded renderers, the loop's cap
    RESULT_CAP, capabilities_for_specs, fit_rows, fn_spec)

# Path/secret guards now live in _pathsafe (shared with the write/shell/git providers so every tool
# enforces the same rules). Bound under the historical private names because this module's own call
# sites still use them (no external importer — these are NOT a back-compat re-export contract).
_looks_secret = _pathsafe.looks_secret
_readable = _pathsafe.readable

# The agent loop hard-caps EVERY tool result at RESULT_CAP chars (agents/agent.py drive_tool_loop),
# and anything longer loses its TAIL there — including a resume pointer appended at the end. So one
# read_file page (window header + body + resume marker) must fit comfortably UNDER that cap, or the
# pagination contract silently breaks and the model acts on code it never saw (mega-review P3; the
# old 16KB page lost both its tail and its pointer at the cap). Derived, not hard-coded: the -400
# headroom covers the window header + resume marker so page+header+marker ≤ RESULT_CAP.
_MAX_READ = RESULT_CAP - 400   # chars of file content returned per read_file page
# Hard host-RAM ceiling for ONE `read_file`. `_readable` gates on extension, not size, and
# TEXT_EXT covers .csv/.jsonl/.log — so without this a multi-GB repo data file is decoded whole
# (twice, counting the splitlines copy) into the shared engine process before any page bound.
_MAX_FILE_BYTES = 64 * 1024 * 1024   # 64 MiB
_MAX_ENTRIES = 200         # entries per list_dir / find_files
# Every way `_read_file` can decline to return content. It answers with a parenthesized REASON (so
# the agent learns why instead of seeing an empty result), and `read_file_checked` recognizes one by
# this closed vocabulary — never by "looks parenthesized", which a real one-line file can also be.
REFUSAL_PREFIXES = (
    "(deleted this session:", "(path not allowed", "(no such file:", "(refused:",
    "(unsupported/binary type", "(file too large to page:", "(could not read:")


def _fit_rows(header: str, rows: list[str], receipt: str = "", cap: int = RESULT_CAP) -> str:
    """`_base.fit_rows` with this module's header shape (doc 25 TO-08).

    `read_file` already sizes its page so the loop's head-cut cannot eat the trailing receipt;
    list_dir, find_files and grep did not, so a long listing arrived looking complete. `cap` is
    lowered only by a caller that puts a line of its own above the rows (`RepoTools`' view header),
    so the rows and their receipt still fit under the loop's cut together with it.
    """
    return fit_rows(header, rows, receipt=receipt, cap=cap)


# ---------------------------------------------------------------------------------------------
# The FILE FILTER of a content grep (WP-TOOLS T1, 2026-09-29).
#
# `glob` used to be matched against a file's BASENAME only, so a glob with a `/` in it could never
# match anything: measured on MiniOneRec inf13, 250 `repo_grep` calls carried one (`service/
# latency_engine.py`, `**/*.py`), 0 of the 250 ever hit, and every one answered `(no matches)` — which
# the model read as "the symbol does not exist" (`def infer`, "not found" in a file that defines it).
#
# A glob WITHOUT a `/` (once a leading `./` is gone) keeps the basename rule, byte for byte. A glob
# WITH one is matched SEGMENT by segment against the key the hit is SHOWN under (`_disp`, the staged
# overlay's key — never a path relative to whatever directory the walk started in, so a staged file
# and its disk copy, and a glob copied off a hit label, agree): `*` stays inside one segment, `**`
# spans zero or more whole segments, and the match is RIGHT-anchored, so `service/x.py` also finds
# `pkg/service/x.py`. A leading `/` anchors it at the key's start instead; a trailing `/` means
# everything under that directory. Python >= 3.11 has no `PurePath.match` with a real `**` (that
# arrived in 3.13), hence the matcher below.
def _path_glob(glob: str) -> tuple[str, list[str] | None, bool]:
    """`(normalized glob, its segments or None for a bare file-name glob, anchored at the start?)`."""
    g = str(glob or "*").replace("\\", "/")
    while g.startswith("./"):
        g = g[2:]
    if "/" not in g:
        return g or "*", None, False
    anchored = g.startswith("/")
    if g.endswith("/"):
        g += "**"
    return g, [seg for seg in g.split("/") if seg], anchored


def _parts_match(pattern: list[str], parts: list[str], match) -> bool:
    """Do the path segments `parts` match the glob segments `pattern` IN FULL? `**` consumes zero or
    more whole segments, every other pattern segment exactly one (through `match`, a fnmatch)."""
    reach = [True] + [False] * len(parts)        # reach[j]: the pattern so far consumed parts[:j]
    for seg in pattern:
        if seg == "**":
            seen, nxt = False, []
            for ok in reach:
                seen = seen or ok
                nxt.append(seen)
        else:
            nxt = [False] + [reach[j] and match(parts[j], seg) for j in range(len(parts))]
        reach = nxt
    return reach[-1]


def glob_admits(glob: str, key: str) -> bool:
    """Does a grep's `glob` admit the file SHOWN as `key` (see the block above)?"""
    g, pattern, anchored = _path_glob(glob)
    shown = str(key or "").replace("\\", "/")
    if pattern is None:
        return fnmatch(shown.rsplit("/", 1)[-1], g)
    parts = [seg for seg in shown.split("/") if seg]
    return _parts_match(pattern if anchored else ["**", *pattern], parts, fnmatch)


def no_file_receipt(where: str, glob: str) -> str:
    """The answer of a grep that searched NO file at all — never `(no matches)`, which is a claim
    about the PATTERN. Counted after every gate, so a glob naming only a credential file reads
    exactly like one naming nothing (the existence of a secret is not disclosed, as in `_list_dir`)."""
    return (f"(grep: no searchable file under {where} matches glob {glob!r} — a glob is a file-name "
            "pattern like *.py or a repo-relative path pattern like service/*.py)")


class GrepResult(str):
    """`_grep`'s answer: exactly the text every caller has always received, plus WHAT KIND of
    answer it is, so a caller merging several searches decides by kind and not by the text's shape.

    `RepoTools.repo_grep` used to drop every block starting `(grep:` — which folded an empty pattern
    and a glob no file matched into the same `(no matches)` a real miss gets. Kinds: `hits` (rows,
    possibly capped), `stopped` (the file budget ran out), `not_found` (files were searched, no line
    matched), `no_file` (no file was searched at all), `refused` (the named `root` cannot be
    searched, with the reason) and `error` (the pattern itself was refused)."""

    kind: str

    def __new__(cls, text: str, kind: str) -> "GrepResult":
        out = super().__new__(cls, text)
        out.kind = kind
        return out


# Directories that are never worth walking for a content grep — model weights / checkpoints / caches
# that a trainer repo carries by the GB (walking them stalls a grep on a FUSE mount).
_SKIP_DIRS = {".git", "__pycache__", ".ipynb_checkpoints", "node_modules", ".mypy_cache",
              ".pytest_cache", ".venv", "venv", "wandb", "lightning_logs", "ckpt", "checkpoints"}
# Paths `find_files` may look at before it gives up, mirroring `_grep`'s 4000-file budget. Set FAR
# above the 200-entry display cap on purpose: an ordinary repo (this one is ~4.7k files) is still
# enumerated WHOLE, so the 200 shown stay the deterministic alphabetically-first ones rather than
# whatever the walk happened to reach first. Only a pathological tree trades that for a bounded
# walk — and then the result says it did.
_FIND_SCAN_BUDGET = 20000


class RepoScoutTools:
    def __init__(self, roots, default_root=None, overlay=None, deleted=None, named_roots=None):
        self._roots = _pathsafe.resolve_roots(roots)
        # (name, resolved_root) for each editable, MIRRORING RepoWriteTools._roots. When set, a disk path
        # is shown/deduped PREFIXED with its owning editable's name (`<name>/train.py`) — the SAME key
        # shape the write tools + overlay use in a MULTI-editable repo, so a grep/find hit round-trips
        # into edit_file and dedups against the staged overlay. Empty (boss / single unnamed root) =>
        # fall back to the plain default_root-relative rendering below.
        self._named_roots = [(n or "", _pathsafe.resolve_roots([p])[0])
                             for (n, p) in (named_roots or []) if p]
        # A repo-RELATIVE path (e.g. "train.py") resolves against this root, so a caller whose write
        # tools already use repo-relative paths (the repo Developer) can read/grep with the SAME paths
        # instead of switching to absolutes. None => relative paths resolve against CWD (the boss case).
        self._default_root = _pathsafe.resolve_roots([default_root])[0] if default_root else None
        # STAGED overlay: {repo-relative-path: content} that WINS over disk. This is the whole point for
        # the repo Developer — the code it is CURRENTLY EDITING (its own writes this session, or a
        # pre-seeded base) is what it needs to read/grep, not the pristine on-disk repo. Pass the SAME
        # live dict the write tools mutate, so a read reflects the latest edit. Empty for the boss (disk
        # only). NOT secret-filtered — the caller authored these files itself; disk reads still are.
        self._overlay = overlay if overlay is not None else {}
        # STAGED deletions: repo-relative paths the caller removed this session. They still sit on the
        # editable-root disk, so read/grep/list must HIDE them to reflect the staged tree, not the
        # pristine repo. Live list => a later delete takes effect immediately.
        self._deleted = deleted if deleted is not None else []

    def _is_deleted(self, rel: str) -> bool:
        rel = str(rel or "").replace("\\", "/").lstrip("./")
        return any(str(d).replace("\\", "/").lstrip("./") == rel for d in self._deleted)

    def _is_deleted_abs(self, p) -> bool:
        """Is an ABSOLUTE path a staged deletion? Maps it back to a repo-relative path first.

        Through `_disp` too, because in a MULTI-editable tree a deletion is keyed `<name>/rel` (the
        write tools' shape) and the plain `relative_to(first root)` below can never produce that —
        so a file deleted under a named editable stayed listed by `find_files` and `list_dir`."""
        if not self._deleted:
            return False
        if self._named_roots and self._is_deleted(self._disp(p)):
            return True
        base = self._default_root or (self._roots[0] if self._roots else None)
        try:
            return base is not None and self._is_deleted(str(Path(p).relative_to(base)))
        except ValueError:
            return False

    def _disp(self, p) -> str:
        """How a DISK path is shown to the caller. For the repo Developer (`default_root` set) render it
        REPO-RELATIVE (e.g. "train.py") — the SAME path shape its write_file/edit_file expects, so a
        grep/find hit ROUND-TRIPS back into an edit (an absolute path is rejected by the write tools'
        _safe_rel, and mixing it with the staged overlay's relative hits confuses the model). For the
        boss (no default_root, multiple unrelated roots like ~/ + the repo) an absolute path is
        unambiguous, so keep it verbatim."""
        # MULTI-editable: key by the OWNING editable's name (`<name>/rel`), exactly as RepoWriteTools does,
        # so a hit under a SECONDARY root round-trips too (relative_to(default_root=roots[0]) would raise
        # for those and leak an absolute path, and would drop the name prefix for the first root).
        for name, root in self._named_roots:
            try:
                rel = str(Path(p).relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            return f"{name}/{rel}" if name and name != "." else rel
        if not self._default_root:
            return str(p)
        try:
            return str(Path(p).relative_to(self._default_root)).replace("\\", "/")
        except ValueError:
            return str(p)     # outside the repo root (a secondary root) — absolute is the honest form

    def _resolve(self, path: str):
        """Resolve a user/model-supplied path and confirm it's inside an allowed root (else None).
        A relative path is tried against `default_root` first (repo-relative), then CWD."""
        import os as _os
        if self._default_root and path and not _os.path.isabs(_os.path.expanduser(str(path))):
            hit = _pathsafe.resolve_within(self._roots, str(self._default_root / path))
            if hit is not None:
                return hit
        return _pathsafe.resolve_within(self._roots, path)

    def specs(self) -> list[dict]:
        return [
            fn_spec("list_dir",
                     "List files and subdirectories under a directory on this machine (read-only; the "
                     "first 200 entries — an overflow ends with an '… (+K more)' line). Use to explore "
                     "a repo's structure.",
                     {"path": {"type": "string", "description": "Directory path (absolute or ~-relative)."}},
                     ["path"]),
            fn_spec("read_file",
                     "Read a text file on this machine (read-only). Returns ONE page of at most ~3600 "
                     "chars. Use for README, the train/eval entry script (e.g. test.py), configs, "
                     "requirements. Read a WINDOW with start_line + lines (like an editor's 'go to line "
                     "N, show M lines'); omit both to read page 1. A page with more file below it ENDS "
                     "with the marker '… (more below — continue with start_line=N)' — continue from "
                     "exactly that N (a single line longer than one page is cut mid-line — the marker "
                     "says so and resumes at the NEXT line); a reply WITHOUT that marker IS the end of "
                     "the file. Never re-read from the top.",
                     {"path": {"type": "string", "description": "File path (absolute or ~-relative)."},
                      "start_line": {"type": "integer",
                                     "description": "1-based line to start from (default 1/top). Use the "
                                     "N from the previous page's 'continue with start_line=N' marker."},
                      "lines": {"type": "integer",
                                "description": "How many lines to return from start_line (a bounded window). "
                                "Omit for as many as fit in one ~3600-char page."}},
                     ["path"]),
            fn_spec("find_files",
                     "Recursively find files matching a glob under a directory (read-only; capped at "
                     "200 matches — narrow the pattern if the list ends without your file). A "
                     "**/... pattern skips caches and weight dirs (.git, node_modules, venv, "
                     "checkpoints, ckpt, wandb, lightning_logs, __pycache__) — search those by "
                     "naming the directory in `root`. Any cut is stated on the last line.",
                     {"root": {"type": "string"},
                      "pattern": {"type": "string", "description": "glob, e.g. **/*.py or **/README*"}},
                     ["root"]),
            fn_spec("grep",
                     "Search file CONTENTS for a regex across a repo (read-only) — find where a CLI arg "
                     "is parsed (grep 'add_argument'), a config key is read, a function is defined. "
                     "Returns file:line snippets. Use this to CONFIRM an exact flag/name in the real "
                     "code instead of guessing it.",
                     {"pattern": {"type": "string", "description": "regex (or a plain substring)"},
                      "root": {"type": "string", "description": "a directory to search under, or ONE "
                               "file to search (optional; defaults to the repo)"},
                      "glob": {"type": "string", "description": "file filter (optional): a file-name "
                               "glob like *.py, or a repo-relative path glob like src/*.py (* stays "
                               "inside one directory, **/ spans any)"},
                      "max_hits": {"type": "integer", "description": "cap on hits (optional, default 40)"}},
                     ["pattern"]),
        ]

    def capabilities(self):
        return capabilities_for_specs(
            self.specs(), effect="read", risk="low", idempotency="idempotent",
            concurrency_safe=True, cancellable=False, approval="never",
            source="looplab.tools.reposcout")

    def execute(self, name: str, args: dict) -> str:
        args = args or {}
        try:
            if name == "list_dir":
                return self._list_dir(args.get("path", ""))
            if name == "read_file":
                return self._read_file(args.get("path", ""), args.get("start_line", 0), args.get("lines", 0))
            if name == "find_files":
                return self._find_files(args.get("root", ""), args.get("pattern", "*"))
            if name == "grep":
                return self._grep(str(args.get("pattern", "")), args.get("root", ""),
                                  args.get("glob") or "*", args.get("max_hits"))
        except Exception as e:  # noqa: BLE001 - tools are advisory; never crash the loop
            return f"(error: {e})"
        return f"(unknown tool: {name})"

    def _list_dir(self, path: str) -> str:
        p = self._resolve(path)
        if not p:
            return f"(path not allowed or outside permitted roots: {path})"
        if not p.exists():
            return f"(no such path: {path})"
        if not p.is_dir():
            return f"(not a directory: {path})"
        # Hide credential files/dirs from the listing too — not just from read_file — so a secret's
        # existence + name never reaches the model. Staged deletions are hidden too (reflect the tree).
        children = [c for c in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
                    if not _looks_secret(c) and not self._is_deleted_abs(c)]
        rows = []
        for c in children[:_MAX_ENTRIES]:
            if c.is_dir():
                rows.append(f"DIR  {c.name}/")
            else:
                try:
                    sz = c.stat().st_size
                except OSError:
                    sz = "?"
                rows.append(f"FILE {c.name}  ({sz}b)")
        if not rows:
            return f"{p}:\n(empty)"
        over = len(children) - _MAX_ENTRIES
        return _fit_rows(f"{p}:\n", rows, f"… (+{over} more)" if over > 0 else "")

    def _overlay_get(self, path: str):
        """The staged content for a path, if the caller has one overlaid (else None). Matches by exact
        key, by normalized repo-relative key, AND by trailing-suffix: an ABSOLUTE sandbox path the agent
        used to read its OWN just-written file (`…/nodes/node_59/test_looplab.py`) ends with the overlay's
        repo-relative key (`test_looplab.py`), so it now resolves to the staged content instead of missing
        to disk — the read/write 'split' that left an agent unable to read what it had just written."""
        key = self._overlay_key(path)
        return None if key is None else self._overlay[key]

    def _overlay_key(self, path: str):
        """The overlay KEY `_overlay_get` resolves `path` to, or None — the same rule, returning
        where it landed, so a caller that labels what it found (a single-file `grep` root) names the
        staged file by the key a hit round-trips through."""
        if not self._overlay or not path:
            return None
        norm = str(path).replace("\\", "/")
        key = norm.lstrip("./")
        if norm in self._overlay:
            return norm
        if key in self._overlay:
            return key
        # Suffix match ONLY for an ABSOLUTE / workdir-prefixed request (norm starts with "/" or a
        # Windows drive): strip the workdir prefix to reach the repo-relative overlay key. A RELATIVE
        # request (e.g. "src/test.py") must NOT suffix-match a SHORTER key ("test.py") — that returned a
        # DIFFERENT file's staged content on any repo with duplicate basenames (test.py / __init__.py),
        # so the Developer edited a file it never actually read.
        if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":"):
            # INSIDE a known root the repo-relative key is EXACT, so resolve it and stop. Suffix
            # matching from in here is what handed back a DIFFERENT file: an absolute
            # `<root>/src/train.py` also ends with the staged root-level `train.py`, and any repo with
            # a duplicated basename (train.py, config.py, __init__.py) could therefore show the agent
            # one file's content and then have it edit another. A miss on the exact key means "not
            # staged", never "try a shorter key".
            for name, root in ([("", r) for r in self._roots]
                               + [(n, r) for (n, r) in self._named_roots]):
                rp = str(root).replace("\\", "/").rstrip("/")
                if rp and (norm == rp or norm.startswith(rp + "/")):
                    rel = norm[len(rp) + 1:]
                    # Multi-editable overlays are keyed `<name>/<rel>` (mirroring RepoWriteTools).
                    staged = f"{name}/{rel}" if name else rel
                    return staged if staged in self._overlay else None
            # OUTSIDE every root: the sandbox/workdir COPY case this branch exists for (the agent
            # reads its own just-written file through an absolute node-workdir path). Keep matching by
            # suffix, but take the LONGEST matching key so the most specific staged path wins instead
            # of whichever one the dict happened to yield first.
            best_key = None
            for k in self._overlay:
                kk = str(k).replace("\\", "/")
                if kk and norm.endswith("/" + kk) and (best_key is None or len(kk) > len(str(best_key))):
                    best_key = k
            return best_key
        return None

    def _overlay_key_path(self, key: str):
        """Where a staged KEY sits in the tree on disk (whether or not a file is there), or None when
        no root owns it — the inverse of `_disp`: `<name>/rel` under that named editable, anything
        else under the default root. With named roots and no default one (every mount named), an
        unprefixed key — the workspace's own `looplab_stages.json` — lives under no mount, so it is
        None rather than a guess."""
        k = str(key or "").replace("\\", "/")
        while k.startswith("./"):
            k = k[2:]
        for name, root in self._named_roots:
            if name and name != "." and (k == name or k.startswith(name + "/")):
                return root / k[len(name) + 1:] if k != name else root
        base = self._default_root or (self._roots[0] if self._roots and not self._named_roots
                                      else None)
        return base / k if base is not None and k else None

    def _key_within(self, key: str, base: Path) -> bool:
        """Is the staged `key` inside the directory `base` — i.e. part of what a walk of `base`
        covers? `_grep` asks this so a `root`-scoped search does not report staged files from
        outside the root it was given."""
        where = self._overlay_key_path(key)
        return where is not None and (where == base or base in where.parents)

    def read_file_checked(self, path: str, start_line=0, lines=0) -> tuple[bool, str]:
        """`(ok, text)` — the STRUCTURED form of `_read_file` for callers that must tell a refusal
        from content. `_read_file` renders a refusal as a parenthesized line so the AGENT reads a
        reason rather than an empty result, but a caller cannot recover that fact by shape: a
        genuine one-line file whose whole content is parenthesized — a stub `(placeholder)` — looks
        identical, and the assistant's `@file` expansion silently dropped it with no grounding and
        no reason shown. Matched against the CLOSED refusal vocabulary below instead
        (`tests/test_reposcout.py` pins that every refusal return in `_read_file` is in it)."""
        text = self._read_file(path, start_line, lines)
        return (not text.startswith(REFUSAL_PREFIXES), text)

    def _read_file(self, path: str, start_line=0, lines=0) -> str:
        staged = self._overlay_get(path)
        if staged is not None:               # the code the caller is EDITING wins over the pristine disk
            return self._paginate(staged, start_line, lines)
        if self._is_deleted(path):           # reflect the STAGED tree: a file deleted this session is gone
            return f"(deleted this session: {path} — not read)"
        p = self._resolve(path)
        if not p:
            return f"(path not allowed or outside permitted roots: {path})"
        if not p.is_file():
            return f"(no such file: {path})"
        if _looks_secret(p):
            return f"(refused: {p.name} looks like a credential/secret file — not read)"
        if not _readable(p):
            try:
                sz = p.stat().st_size
            except OSError:
                sz = "?"
            return f"(unsupported/binary type {p.suffix or '<none>'}; {sz}b — exists, not read)"
        # Size-fence BEFORE the read. `_readable` gates on EXTENSION only, and TEXT_EXT includes
        # `.csv`/`.jsonl`/`.log`, so a repo task's multi-GB training CSV or a run's events.jsonl passed
        # every earlier check — then `read_text` slurped the whole file and `_paginate`'s
        # `splitlines(keepends=True)` built a SECOND full copy (a per-line str object each, well over 2x
        # the file size at peak) before `_MAX_READ` was applied to the returned page. This is agent-
        # reachable during a run (`tools/asset_brief.py` hands RepoScoutTools the task repo root), so one
        # `read_file("data/train.csv")` could OOM the engine host. Same host-RAM rule the eval reader
        # applies via `_MAX_METRIC_FILE_BYTES`; the read-whole-then-paginate contract is unchanged below.
        try:
            if p.stat().st_size > _MAX_FILE_BYTES:
                return (f"(file too large to page: {p.stat().st_size}b > {_MAX_FILE_BYTES}b — "
                        f"use grep/find_files to locate the region instead)")
        except OSError as e:
            return f"(could not read: {e})"
        try:
            data = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as e:
            return f"(could not read: {e})"
        return self._paginate(data, start_line, lines)

    @staticmethod
    def _paginate(data: str, start_line=0, lines=0, max_chars: int = _MAX_READ) -> str:
        """Return a WINDOW of `data`: `lines` lines starting at 1-based `start_line`, capped at
        `max_chars` chars (default _MAX_READ; env_inspect passes a reduced budget so its origin-path
        prefix still fits the loop cap). start_line 0/None/'' = from the top; `lines` 0/None = as many
        as fit; stringy '180'/'40' coerce. When more remains (line window ran into the char cap, or no
        `lines` given and the file is bigger than one page) the reply ENDS with the resume marker naming
        the exact start_line to continue from; a reply without the marker IS the end of the file — that
        asymmetry is the tool's documented contract, so EVERY continuing page (the mid-line case
        included) must end with the same '… (more below — continue with start_line=N)' stem, the marker
        text must stay stable, and it must always fit UNDER the agent loop's RESULT_CAP (see _MAX_READ).
        (Before this, read_file ignored start_line entirely and always returned the first 16KB — agents
        re-read the same file head 13-21× and burned their whole budget, per the node 56/59/61/62
        traces.)"""
        def _int(v):
            try:
                return int(v) if v else 0
            except (TypeError, ValueError):
                return 0
        start = max(0, _int(start_line) - 1)
        want = max(0, _int(lines))
        all_lines = data.splitlines(keepends=True)
        n = len(all_lines)
        window = all_lines[start: (start + want) if want else n]
        body = "".join(window)
        mid_line = False
        if len(body) > max_chars:                      # window ran into the char cap — truncate on a line boundary
            # Accumulate WHOLE splitlines() entries (the SAME line model `all_lines` used) until the next
            # would exceed the char cap. Counting/trimming the cut by `\n` alone (body.count("\n") /
            # rfind("\n")) desynced from splitlines() whenever a NON-`\n` line boundary — \f, \v, lone \r,
            # \x1c-\x1e, \x85, U+2028/U+2029 — fell inside the truncated window: `shown` and the resume
            # start_line then disagreed with the split, re-serving or mislabeling lines across pages (and a
            # boundary-only window counted 0 `\n`, falsely flagging an ordinary short line as "longer than
            # one page"). Work over the entries directly so `shown` and the next start_line always agree.
            kept: list[str] = []
            used = 0
            for _ln in window:
                if used + len(_ln) > max_chars:
                    break
                kept.append(_ln)
                used += len(_ln)
            if kept:
                body = "".join(kept)                   # the whole lines that fit; the rest is the next page
                shown = len(kept)
            else:
                # A single line longer than one page: NO whole line fits, so a whole-line marker would
                # point at the SAME start_line and the model would loop on identical pages forever
                # (reproduced). Guarantee progress: show the truncated head of that one line, count it
                # shown, and resume at the NEXT line — the marker says so honestly (its tail is skipped).
                body = window[0][:max_chars]
                shown = 1
                mid_line = True
            more = True
        else:                                          # the full line-window was returned
            shown = len(window)
            more = (start + shown) < n                 # anything after the window?
        # Header AFTER the cap, from the actual `shown` count — the pre-cap window would overstate the
        # range and disagree with the resume marker when the char cap cut the window short.
        head = f"(lines {start + 1}-{start + shown} of {n})\n" if (start > 0 or want) else ""
        if mid_line:
            # The explanation PRECEDES the canonical stem — the marker must still END with the
            # documented '… (more below — continue with start_line=N)' so "a reply without the
            # marker IS the end" stays true for a stem-matching reader.
            tail = (f"\n… (line {start + 1} is longer than one page — its remainder is NOT reachable "
                    f"by line windows) … (more below — continue with start_line={start + 2})")
        elif more:
            tail = f"\n… (more below — continue with start_line={start + shown + 1})"
        else:
            tail = ""
        return head + body + tail

    @staticmethod
    def _iter_glob(base: Path, pattern: str):
        """Lazily yield `(path, matched)` for every path LOOKED AT resolving `base.glob(pattern)`.

        The `matched` flag is what lets the caller's budget bound the WALK rather than just the
        answer: counting only matches would leave `**/*.py` free to stat a million-file checkpoint
        tree that happens to hold no `.py`, which is the exact runaway this budget exists to stop.

        pathlib has no prune hook, so `base.glob("**/…")` stats every byte of the GB-scale
        checkpoint/venv dirs `_SKIP_DIRS` exists to avoid before any caller-side budget can act.
        `**/<name-glob>` is both the shape that can run away and the one models actually send, so
        walk it here with the same prune `_grep` uses, and report every entry examined. Every other
        shape is depth-bounded by its own literal segments (`*.py`, `sub/*.py`, `*/*/x.py`) and
        falls through to pathlib with its semantics untouched — there only matches are observable,
        so for those the budget bounds MEMORY (the Path objects held) and not the stat walk.

        Deliberately matched to pathlib, not to `_grep`: HIDDEN entries are yielded (`.github/*.yml`
        is a legitimate find), a `**/*` yields directories as well as files, and matching is
        case-SENSITIVE (`fnmatchcase`, since `fnmatch` would fold case on a Windows/macOS host while
        pathlib's POSIX flavour does not). Symlinked dirs are not descended, which is os.walk's
        default and pathlib's `**` behaviour both."""
        tail = pattern[3:] if pattern.startswith("**/") else None
        if tail is None or not tail or "/" in tail or "**" in tail:
            for m in base.glob(pattern):
                yield m, True
            return
        import os as _os
        from fnmatch import fnmatchcase as _fnmatchcase
        for dp, dirs, files in _os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
            # `dirs` is already the PRUNED list, so a skipped dir is neither descended nor yielded —
            # the same "this subtree does not exist for search" rule `_grep` applies.
            for name in list(dirs) + sorted(files):
                yield Path(dp) / name, _fnmatchcase(name, tail)

    def _find_files(self, root: str, pattern: str, *, with_overlay: bool = False,
                    reserve: int = 0) -> str:
        """`with_overlay` lists the STAGED files under `root` beside the disk walk (a staged key the
        disk also has is one row), matched against `pattern` the way pathlib matches it relative to
        `root` (`*` inside a segment, `**` any depth, case-sensitive). Off for this tool's own
        callers, whose listing has always been the disk's; `RepoTools` turns it on for a node's
        view, where a file only that node recorded exists as surely as one on disk. `reserve` is
        the room a caller's own header line takes above the rows (`_fit_rows`' `cap`)."""
        p = self._resolve(root)
        if not p:
            return f"(root not allowed or outside permitted roots: {root})"
        if not p.is_dir():
            return f"(not a directory: {root})"
        hits = []
        scanned = 0
        stopped = False
        try:
            # Iterate LAZILY under a scan budget. `sorted(p.glob(...))` materialized EVERY match
            # before the 200-entry cap applied, so a `**/*` pattern built and sorted the whole tree
            # first; one of this tool's roots is `Path.home()` (serve/assistant.py), where that is a
            # full-tree stat walk plus hundreds of MB of Path objects inside the server process, to
            # return 200 lines. `_iter_glob` prunes the recursive shape and reports every path it
            # LOOKS AT, so `scanned` bounds the walk itself, not just the answer.
            for m, matched in self._iter_glob(p, pattern or "*"):
                scanned += 1
                if scanned > _FIND_SCAN_BUDGET:
                    stopped = True
                    break
                if not matched:
                    continue
                # pathlib glob accepts `..` segments and follows symlinks, so a pattern like
                # "../../etc/*" escapes the allowed roots — re-validate every hit against the roots
                # (and run the secret filter on the RESOLVED path so a symlinked secret is caught).
                rm = _pathsafe.resolve_within(self._roots, str(m))
                if rm is None or _looks_secret(rm) or self._is_deleted_abs(rm):
                    continue
                hits.append(self._disp(rm))   # repo-relative for the Developer so a hit round-trips
        except (OSError, ValueError) as e:
            return f"(bad pattern: {e})"
        if with_overlay and self._overlay:
            want = [seg for seg in str(pattern or "*").replace("\\", "/").split("/") if seg]
            for key in self._overlay:
                where = self._overlay_key_path(key)
                if (where is None or not self._key_within(key, p) or where == p
                        or _looks_secret(Path(str(key))) or self._is_deleted(key)):
                    continue
                if _parts_match(want, list(where.relative_to(p).parts), fnmatchcase):
                    hits.append(str(key))
            hits = list(dict.fromkeys(hits))
        if not hits:
            # "(no matches)" would be a LIE about a walk that never finished — the model would cross
            # the file off and stop looking for it. Say which of the two happened.
            if stopped:
                return (f"(no matches for {pattern!r} in the first {_FIND_SCAN_BUDGET} paths under "
                        f"{root}; the walk stopped there — narrow `root`)")
            return f"(no matches for {pattern!r} under {root})"
        # ALWAYS say when the list is partial. `_list_dir` prints "... (+K more)" and `_grep` prints
        # "(capped at N hits)"; this one printed nothing, so a capped result was byte-identical to a
        # complete one and the model read the first 200 matches as the whole match set.
        notes = []
        shown = sorted(hits)
        if len(shown) > _MAX_ENTRIES:
            notes.append(f"showing {_MAX_ENTRIES} of {len(shown)} matches")
            shown = shown[:_MAX_ENTRIES]
        if stopped:
            notes.append(f"stopped after scanning {_FIND_SCAN_BUDGET} paths")
        budget = RESULT_CAP - max(0, int(reserve))
        return _fit_rows("", shown,
                         f"... ({'; '.join(notes)} — narrow `pattern`/`root` for the rest)"
                         if notes else "", cap=budget)

    def _grep_target(self, root: str):
        """What a `grep` ROOT names, resolved EXACTLY as `_read_file` resolves a path (WP-TOOLS T2).

        `root` had to be a directory, so a model that knew WHICH file it wanted — the usual case —
        was told `(grep: X is not a searchable directory)` and retried on the parent: 219 turns on
        MiniOneRec inf13, 123 of them in `plan_step`. A file is now searched as itself, and in
        `_read_file`'s ORDER, because the Developer's scout overlays the code it is editing: a
        disk-first resolution would grep the pristine base copy of `service/latency_engine.py` — 102
        of the 175 Developer roots, a file every lineage node had rewritten — and hand its stale
        line numbers to `edit_file`, while the 39 roots that exist only staged would still fail.
        Staged key → staged deletion → `_resolve` → regular file → secret → readable type → size. A
        `.git` component is refused first: the walk prunes `.git`, and a NAMED root bypasses the
        prune. Returns `("staged", key)`, `("file", path)`, `("dir", path)` or `("refused",
        receipt)` — each refusal says what the root IS, never "not a directory" about a file."""
        internals = f"(grep: {root} is inside repository internals (.git) — not searched)"
        if ".git" in Path(str(root).replace("\\", "/")).parts:
            return "refused", internals
        key = self._overlay_key(root)
        if key is not None:
            return "staged", key
        if self._is_deleted(root):
            return "refused", f"(grep: {root} was deleted this session — there is nothing to search)"
        p = self._resolve(root)
        if p is None:
            return "refused", (f"(grep: {root} is outside the searchable roots — pass a directory "
                               "or a file inside the repo)")
        if ".git" in Path(self._disp(p)).parts:
            return "refused", internals
        if p.is_dir():
            return "dir", p
        if not p.exists():
            return "refused", f"(grep: no such file or directory: {root})"
        if not p.is_file():
            return "refused", f"(grep: {root} is not a regular file or a directory — not searched)"
        if _looks_secret(p):
            return "refused", (f"(grep: refused: {p.name} looks like a credential/secret file — "
                               "not searched)")
        if not _readable(p):
            return "refused", f"(grep: {root} is an unsupported/binary file type — not searched)"
        try:
            size = p.stat().st_size
        except OSError as e:
            return "refused", f"(grep: could not read {root}: {e})"
        if size > 2_000_000:
            return "refused", (f"(grep: {root} is {size}b, over grep's 2000000b per-file limit — "
                               "read it in windows with read_file instead)")
        return "file", p

    def _grep_one(self, rx, cap: int, pattern: str, kind: str, target, glob: str, where: str,
                  budget: int) -> GrepResult:
        """`_grep` over the ONE file `_grep_target` admitted: a staged key, or a regular file read
        through `core/node_evidence.py::read_bounded_regular_file` — the one reader of a file a
        candidate can write, which a triage scout rooted at a node's workdir is looking at. `glob`
        is ignored unless it EXCLUDES this file, and then the answer is the no-file receipt."""
        shown = str(target) if kind == "staged" else self._disp(target)
        if not glob_admits(glob, shown):
            return GrepResult(no_file_receipt(where, glob), "no_file")
        if kind == "staged":
            lines = str(self._overlay[target]).splitlines()
        else:
            data = read_bounded_regular_file(target, 2_000_001)
            if data is None:
                return GrepResult(f"(grep: could not read {where} — not a readable regular file)",
                                  "refused")
            if len(data) > 2_000_000:
                return GrepResult(f"(grep: {where} is over grep's 2000000b per-file limit — read it "
                                  "in windows with read_file instead)", "refused")
            # The walk's own line model (a text-mode file iterator), so a hit's line number is the
            # one the same file gets when a directory walk reaches it.
            lines = list(io.StringIO(data.decode("utf-8", errors="replace"), newline=None))
        hits: list[str] = []
        for i, line in enumerate(lines, 1):
            if rx.search(line):
                hits.append(f"{shown}:{i}: {line.strip()[:200]}")
                if len(hits) >= cap:
                    return GrepResult(_fit_rows("", hits, f"(capped at {cap} hits)", budget), "hits")
        if hits:
            return GrepResult(_fit_rows("", hits, "", budget), "hits")
        return GrepResult(f"(grep: {pattern!r} not found in {shown})", "not_found")

    def _grep(self, pattern: str, root: str, glob: str, max_hits, *,
              skip_hidden: bool = True, label: str | None = None,
              reserve: int = 0) -> GrepResult:
        """`skip_hidden=False` keeps DOTTED directories in the walk (doc 25 TO-06).

        This tool's own audience wants them pruned — `~/` is one of its roots, where `.cache`/`.venv`
        dwarf the repo. A repo Researcher grepping ONE mounted repo wants `.github/workflows/*.yml`,
        which is ordinary source. `_SKIP_DIRS` (which holds `.git`) is pruned either way, so the
        credential surface is closed in both modes. `_find_files` already yields hidden entries and
        says so — this makes the divergence a parameter instead of a silent difference between the
        two walkers.

        WHAT IT ANSWERS (WP-TOOLS, 2026-09-29) is a `GrepResult`: the text every caller has always
        read, and its KIND. Three things changed in it. `glob` is path-aware (`glob_admits`, T1). A
        search that searched NO file says so (`no_file_receipt`) instead of "not found" — counted
        over the files actually searched, after the symlink, secret and deletion gates and staged
        files included, so a glob naming only a credential file is indistinguishable from one
        naming nothing. And `root` may name ONE file (`_grep_target`, T2), while a named directory
        now scopes the STAGED files too, which used to be searched whatever `root` said. `label` is
        how a receipt names the searched place when the caller's `root` is a path the model never
        saw (`RepoTools` hands over absolute mount roots); `reserve` is the room a caller's own
        header line takes under the loop's cut.
        """
        import os as _os
        import re as _re
        pattern = (pattern or "").strip()
        if not pattern or len(pattern) > 1000:      # cheap ReDoS guard (Python re has no match timeout)
            return GrepResult("(grep: give a (short) pattern to search for)", "error")
        try:
            rx = _re.compile(pattern)
        except _re.error:
            rx = _re.compile(_re.escape(pattern))   # not a valid regex -> treat as a literal substring
        cap = max(1, min(int(max_hits) if max_hits else 40, 200))   # clamp: a model-supplied max can't disable the cap
        budget = RESULT_CAP - max(0, int(reserve))
        glob = glob or "*"
        where = label if label is not None else (root or "repo")
        within = None               # a NAMED directory: the staged files searched are the ones in it
        if root:
            kind, target = self._grep_target(root)
            if kind == "refused":
                return GrepResult(target, "refused")
            if kind != "dir":
                return self._grep_one(rx, cap, pattern, kind, target, glob, where, budget)
            base = within = target
        else:
            base = self._default_root or (self._roots[0] if self._roots else None)
            if base is None or not base.is_dir():
                return GrepResult(f"(grep: {root or 'repo'} is not a searchable directory)", "refused")
        bare, path_glob, _anchored = _path_glob(glob)
        hits: list[str] = []
        # STAGED overlay first — the code the caller is EDITING wins over disk, and its paths dedup the
        # disk walk (so a patched file isn't grepped in both its edited and pristine form).
        staged_rel = set()
        searched = 0
        for rel, content in sorted(self._overlay.items()):
            if within is not None and not self._key_within(rel, within):
                continue                            # staged, but outside the `root` asked for
            if not glob_admits(glob, rel):
                continue
            staged_rel.add(rel)
            searched += 1
            for i, line in enumerate(str(content).splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(hits) >= cap:
                        return GrepResult(_fit_rows("", hits, f"(capped at {cap} hits)", budget),
                                          "hits")
        scanned = 0
        for dp, dirs, files in _os.walk(base):
            dirs[:] = [d for d in dirs
                       if d not in _SKIP_DIRS and not (skip_hidden and d.startswith("."))]
            for fn in sorted(files):
                if scanned >= 4000:                 # file budget so a huge repo can't stall the grep
                    return GrepResult(_fit_rows("", hits, "(stopped after 4000 files; narrow "
                                                "`root`/`glob`)", budget), "stopped")
                if path_glob is None and not fnmatch(fn, bare):
                    continue
                fp = Path(dp) / fn
                # skip a file STAGED (grepped above) or DELETED this session. Key it exactly as the overlay
                # does (`_disp` == the write-tool path shape, prefixed per editable) so the dedup HITS in a
                # multi-editable repo — else an already-edited file is re-grepped from PRISTINE disk and the
                # model is shown the old content it already changed. A PATH glob is matched against this
                # same key, so a staged file and its disk copy can never answer a glob differently.
                _rel = self._disp(fp)
                if path_glob is not None and not glob_admits(glob, _rel):
                    continue
                if _rel in staged_rel or self._is_deleted(_rel):
                    continue
                # Resolve the (possibly symlinked) path and RE-VALIDATE on the resolved target — exactly as
                # find_files does. os.walk + open() follow symlinks, so an innocuously-named link
                # (configs/data.json -> ~/.aws/credentials) would slip past _looks_secret (which sees only
                # the link's OWN name/parts) and leak an off-sandbox file into the hits fed to a remote model.
                rp = _pathsafe.resolve_within(self._roots, str(fp))
                if rp is None or _looks_secret(rp) or not _readable(rp):
                    continue                        # out-of-root symlink, credential file, or a binary
                fp = rp
                try:
                    if fp.stat().st_size > 2_000_000:
                        continue
                except OSError:
                    continue
                scanned += 1
                try:
                    with open(fp, encoding="utf-8", errors="replace") as fh:
                        for i, line in enumerate(fh, 1):
                            if rx.search(line):
                                # repo-relative label for the Developer (matches the staged-overlay hits
                                # above + write_file's path shape, so a hit round-trips into an edit).
                                hits.append(f"{self._disp(fp)}:{i}: {line.strip()[:200]}")
                                if len(hits) >= cap:
                                    return GrepResult(_fit_rows("", hits, f"(capped at {cap} hits)",
                                                                budget), "hits")
                except OSError:
                    continue
        if hits:
            return GrepResult(_fit_rows("", hits, "", budget), "hits")
        if not searched + scanned:
            return GrepResult(no_file_receipt(where, glob), "no_file")
        return GrepResult(f"(grep: {pattern!r} not found)", "not_found")
