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
from looplab.tools._base import fn_spec   # shared OpenAI function-schema builder (one schema shape)

# Path/secret guards now live in _pathsafe (shared with the write/shell/git providers so every tool
# enforces the same rules). Re-exported under the historical private names for back-compat.
_looks_secret = _pathsafe.looks_secret
_readable = _pathsafe.readable

_MAX_READ = 16000          # bytes returned from one read_file
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
                     "List files and subdirectories under a directory on this machine (read-only). "
                     "Use to explore a repo's structure.",
                     {"path": {"type": "string", "description": "Directory path (absolute or ~-relative)."}},
                     ["path"]),
            fn_spec("read_file",
                     "Read a text file on this machine (first ~16KB, read-only). Use for README, the "
                     "train/eval entry script (e.g. test.py), configs, requirements.",
                     {"path": {"type": "string", "description": "File path (absolute or ~-relative)."}},
                     ["path"]),
            fn_spec("find_files",
                     "Recursively find files matching a glob under a directory (read-only).",
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
                return self._read_file(args.get("path", ""))
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
        """The staged content for a repo-relative path, if the caller has one overlaid (else None)."""
        if not self._overlay or not path:
            return None
        key = str(path).replace("\\", "/").lstrip("./")
        return self._overlay.get(path) or self._overlay.get(key)

    def _read_file(self, path: str) -> str:
        staged = self._overlay_get(path)
        if staged is not None:               # the code the caller is EDITING wins over the pristine disk
            return staged[:_MAX_READ] + ("\n… (truncated)" if len(staged) > _MAX_READ else "")
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
        return data[:_MAX_READ] + ("\n… (truncated)" if len(data) > _MAX_READ else "")

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
