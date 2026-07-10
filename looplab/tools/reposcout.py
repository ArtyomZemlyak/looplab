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

from pathlib import Path

from looplab.core import _pathsafe
from looplab.tools._base import RESULT_CAP, fn_spec   # shared schema builder + the loop's result cap

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
_MAX_ENTRIES = 200         # entries per list_dir / find_files


# Directories that are never worth walking for a content grep — model weights / checkpoints / caches
# that a trainer repo carries by the GB (walking them stalls a grep on a FUSE mount).
_SKIP_DIRS = {".git", "__pycache__", ".ipynb_checkpoints", "node_modules", ".mypy_cache",
              ".pytest_cache", ".venv", "venv", "wandb", "lightning_logs", "ckpt", "checkpoints"}


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
        """Is an ABSOLUTE path a staged deletion? Maps it back to a repo-relative path first."""
        if not self._deleted:
            return False
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
                     "200 matches — narrow the pattern if the list ends without your file).",
                     {"root": {"type": "string"},
                      "pattern": {"type": "string", "description": "glob, e.g. **/*.py or **/README*"}},
                     ["root"]),
            fn_spec("grep",
                     "Search file CONTENTS for a regex across a repo (read-only) — find where a CLI arg "
                     "is parsed (grep 'add_argument'), a config key is read, a function is defined. "
                     "Returns file:line snippets. Use this to CONFIRM an exact flag/name in the real "
                     "code instead of guessing it.",
                     {"pattern": {"type": "string", "description": "regex (or a plain substring)"},
                      "root": {"type": "string", "description": "dir to search under (optional; "
                               "defaults to the repo)"},
                      "glob": {"type": "string", "description": "filename glob to restrict (optional, e.g. *.py)"},
                      "max_hits": {"type": "integer", "description": "cap on hits (optional, default 40)"}},
                     ["pattern"]),
        ]

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
        if len(children) > _MAX_ENTRIES:
            rows.append(f"… (+{len(children) - _MAX_ENTRIES} more)")
        return f"{p}:\n" + ("\n".join(rows) if rows else "(empty)")

    def _overlay_get(self, path: str):
        """The staged content for a path, if the caller has one overlaid (else None). Matches by exact
        key, by normalized repo-relative key, AND by trailing-suffix: an ABSOLUTE sandbox path the agent
        used to read its OWN just-written file (`…/nodes/node_59/test_looplab.py`) ends with the overlay's
        repo-relative key (`test_looplab.py`), so it now resolves to the staged content instead of missing
        to disk — the read/write 'split' that left an agent unable to read what it had just written."""
        if not self._overlay or not path:
            return None
        norm = str(path).replace("\\", "/")
        key = norm.lstrip("./")
        if norm in self._overlay:
            return self._overlay[norm]
        if key in self._overlay:
            return self._overlay[key]
        # Suffix match ONLY for an ABSOLUTE / workdir-prefixed request (norm starts with "/" or a
        # Windows drive): strip the workdir prefix to reach the repo-relative overlay key. A RELATIVE
        # request (e.g. "src/test.py") must NOT suffix-match a SHORTER key ("test.py") — that returned a
        # DIFFERENT file's staged content on any repo with duplicate basenames (test.py / __init__.py),
        # so the Developer edited a file it never actually read.
        if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":"):
            for k, v in self._overlay.items():
                kk = str(k).replace("\\", "/")
                if kk and norm.endswith("/" + kk):
                    return v
        return None

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
        if len(body) > max_chars:                      # window ran into the char cap — truncate mid-window
            body = body[:max_chars]
            shown = body.count("\n")                   # whole lines that survived the cut
            if shown == 0:
                # A single line longer than one page: zero whole lines survived, so the generic marker
                # would point at the SAME start_line and the model would loop on identical pages forever
                # (reproduced). Guarantee progress: count the truncated line as shown and resume at the
                # NEXT line — the marker says so honestly (the line's tail past the cap is skipped).
                shown = 1
                mid_line = True
            else:
                # Drop the partial trailing fragment (chars past the last complete line): the header
                # and the resume marker count WHOLE lines, and the next page re-serves that line in
                # full from start_line — keeping the fragment would double-serve its head and show a
                # half-line the stated range doesn't cover.
                body = body[: body.rfind("\n")]
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

    def _find_files(self, root: str, pattern: str) -> str:
        p = self._resolve(root)
        if not p:
            return f"(root not allowed or outside permitted roots: {root})"
        if not p.is_dir():
            return f"(not a directory: {root})"
        hits = []
        try:
            for m in sorted(p.glob(pattern or "*")):
                # pathlib glob accepts `..` segments and follows symlinks, so a pattern like
                # "../../etc/*" escapes the allowed roots — re-validate every hit against the roots
                # (and run the secret filter on the RESOLVED path so a symlinked secret is caught).
                rm = _pathsafe.resolve_within(self._roots, str(m))
                if rm is None or _looks_secret(rm) or self._is_deleted_abs(rm):
                    continue
                hits.append(self._disp(rm))   # repo-relative for the Developer so a hit round-trips
                if len(hits) >= _MAX_ENTRIES:
                    break
        except (OSError, ValueError) as e:
            return f"(bad pattern: {e})"
        return "\n".join(hits) if hits else f"(no matches for {pattern!r} under {root})"

    def _grep(self, pattern: str, root: str, glob: str, max_hits) -> str:
        import os as _os
        import re as _re
        from fnmatch import fnmatch as _fnmatch
        pattern = (pattern or "").strip()
        if not pattern or len(pattern) > 1000:      # cheap ReDoS guard (Python re has no match timeout)
            return "(grep: give a (short) pattern to search for)"
        base = self._resolve(root) if root else (self._default_root or (self._roots[0] if self._roots else None))
        if base is None or not base.is_dir():
            return f"(grep: {root or 'repo'} is not a searchable directory)"
        try:
            rx = _re.compile(pattern)
        except _re.error:
            rx = _re.compile(_re.escape(pattern))   # not a valid regex -> treat as a literal substring
        cap = max(1, min(int(max_hits) if max_hits else 40, 200))   # clamp: a model-supplied max can't disable the cap
        hits: list[str] = []
        # STAGED overlay first — the code the caller is EDITING wins over disk, and its paths dedup the
        # disk walk (so a patched file isn't grepped in both its edited and pristine form).
        staged_rel = set()
        for rel, content in sorted(self._overlay.items()):
            if not _fnmatch(rel.rsplit("/", 1)[-1], glob):
                continue
            staged_rel.add(rel)
            for i, line in enumerate(str(content).splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(hits) >= cap:
                        return "\n".join(hits) + f"\n(capped at {cap} hits)"
        scanned = 0
        for dp, dirs, files in _os.walk(base):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            for fn in sorted(files):
                if scanned >= 4000:                 # file budget so a huge repo can't stall the grep
                    return "\n".join(hits) + "\n(stopped after 4000 files; narrow `root`/`glob`)"
                if not _fnmatch(fn, glob):
                    continue
                fp = Path(dp) / fn
                # skip a file STAGED (grepped above) or DELETED this session. Key it exactly as the overlay
                # does (`_disp` == the write-tool path shape, prefixed per editable) so the dedup HITS in a
                # multi-editable repo — else an already-edited file is re-grepped from PRISTINE disk and the
                # model is shown the old content it already changed.
                _rel = self._disp(fp)
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
                                    return "\n".join(hits) + f"\n(capped at {cap} hits)"
                except OSError:
                    continue
        return "\n".join(hits) if hits else f"(grep: {pattern!r} not found)"
