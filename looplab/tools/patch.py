"""Patch application with an out-of-surface gate (I4, ADR-14).

When a Developer backend edits files via a unified diff (rather than whole-file
rewrites), the diff is double-gated before it touches disk:
  1. parse the target paths and **reject** (not strip) the whole patch if any path is
     outside the edit-surface allow-list or escapes it (`..`, absolute, drive letter);
  2. apply with `git apply --check` (dry-run) then for real — kernel-grade enforcement
     so a parser bug can't leak.
A rejected forbidden-file touch is a *signal* (returned to the caller), not noise.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import tempfile
from pathlib import Path

_DRIVE = re.compile(r"^[A-Za-z]:")


def _unquote_git_path(p: str) -> str:
    r"""git C-quotes a path containing non-ASCII / control / quote bytes (core.quotePath, ON by
    default): `café.py` -> `"caf\303\251.py"` (octal-escaped UTF-8). Decode it back so the surface
    gate sees the real path; a non-quoted path is returned unchanged."""
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        try:
            return (p[1:-1]
                    .encode("latin-1", "backslashreplace")
                    .decode("unicode_escape")
                    .encode("latin-1")
                    .decode("utf-8", "replace"))
        except (UnicodeDecodeError, UnicodeEncodeError):
            return p[1:-1]
    return p


def changed_paths(diff_text: str) -> list[str]:
    """Target paths referenced by a unified diff (a/ b/ prefixes stripped, /dev/null ignored).
    Decodes git's C-quoting (core.quotePath) so a non-ASCII filename gates correctly. The +++/---
    header lines are AUTHORITATIVE — they carry the whole path including spaces — so the ambiguous
    `diff --git a/X b/X` line (which `split()` mangles when X has spaces) is used only as a FALLBACK
    for a block with no +++/--- header (a pure rename / mode change / binary file)."""
    paths: set[str] = set()
    git_fallback: list[str] = []      # a/ b/ paths from the current `diff --git` block's line
    block_has_header = True           # suppress the fallback once a +++/--- header is seen
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if not block_has_header:
                paths.update(git_fallback)            # previous block had no +++/--- header
            block_has_header = False
            git_fallback = [(t[2:] if t[:2] in ("a/", "b/") else t)
                            for t in (_unquote_git_path(tok) for tok in line.split()[2:])]
        elif line.startswith("+++ ") or line.startswith("--- "):
            block_has_header = True
            p = _unquote_git_path(line[4:].strip().split("\t")[0])
            if p == "/dev/null":
                continue
            if p[:2] in ("a/", "b/"):
                p = p[2:]
            paths.add(p)
    if not block_has_header:
        paths.update(git_fallback)                    # last block had no +++/--- header
    return sorted(paths)


def _escapes(path: str) -> bool:
    pp = path.replace("\\", "/")
    # leading slash = POSIX-absolute (os.path.isabs misses this on Windows); drive =
    # Windows-absolute; ".." = traversal.
    if pp.startswith("/") or _DRIVE.match(path) or os.path.isabs(path):
        return True
    return ".." in pp.split("/")


def _ci(s: str) -> str:
    return s.replace("\\", "/").lower()   # case-insensitive, forward-slash (cross-platform)


def _match(path: str, glob: str, _fn=fnmatch.fnmatch) -> bool:
    """fnmatch, but treat a leading `**/` as 'zero OR more directories' so a surface like
    `**/*.py` also matches a ROOT-level file (`train.py`) — plain fnmatch requires the literal
    slash and would silently reject root files. `_fn=fnmatch.fnmatchcase` gives the case-
    SENSITIVE twin (exact-protect mode; the default callers pre-fold case via `_ci`)."""
    if _fn(path, glob):
        return True
    if glob.startswith("**/") and _fn(path, glob[3:]):
        return True
    # A `**/` segment anywhere means 'zero OR more directories' (e.g. a namespaced
    # `model/**/*.py` must also match the ROOT file `model/train.py`). plain fnmatch has no
    # `**` semantics, so collapse one `**/` and retry.
    return "**/" in glob and _fn(path, glob.replace("**/", "", 1))


def _in_surface(p: str, allow: list[str], prefixes: list[str]) -> bool:
    """Is `p` within the edit surface? With multi-editable `prefixes` (named repo subdirs), a
    path UNDER a named repo is governed ONLY by that repo's prefixed globs, and a root path only
    by the non-prefixed globs — so a broad root glob (`**/*.py`) can't widen a named repo's
    narrower surface (fnmatch's `*` crosses `/`)."""
    pl = p.replace("\\", "/")
    owner = next((pre for pre in prefixes if pl.startswith(pre + "/")), None)
    if owner:
        globs = [g for g in allow if g.startswith(owner + "/")]
    elif prefixes:
        globs = [g for g in allow if not any(g.startswith(pre + "/") for pre in prefixes)]
    else:
        globs = allow
    return any(_match(pl, g) for g in globs)


class SurfacePolicy:
    """The write-gate trust boundary as ONE value object (BACKLOG §4 "SurfacePolicy"): may this
    repo-relative path be edited? Combines the three checks the write paths used to re-implement
    independently — escape (absolute / drive letter / `..`), the edit-surface allow globs, and
    the protect-list — and `check()` returns a REASON CODE (None = editable), so each consumer
    keeps its OWN user-visible refusal wording (those strings are per-site contracts).

    The sites' semantics are deliberately NOT identical; the differences are explicit
    constructor parameters — do not "simplify" them away:
      * `surface=None` disables the surface check entirely (WriteTools gates containment via
        resolved roots instead); an EMPTY list keeps diff-gate semantics (no allow-list =>
        nothing is editable).
      * `protected_exact=True` matches the protect-list by EXACT, case-SENSITIVE string
        membership (RepoWriteTools' historical semantics — its protect entries are already
        normalized workspace-relative names). The default is case-insensitive equality-or-glob
        (`_ci`/`_match`) — the diff-gate/WriteTools semantics, which also catches an NTFS
        case-variant of a protected file.
      * `check_escapes=False` skips the escape test for a caller that already canonicalized the
        path under its own rules (RepoWriteTools._safe_rel also strips `./`, rejects `~`, and —
        unlike `_escapes` — accepts a drive-letter path on POSIX; re-checking here would change
        what that gate accepts).
      * `prefixes` are passed to `_in_surface` VERBATIM — the diff gate rstrips trailing
        slashes before constructing the policy (as it always did); RepoWriteTools historically
        does not, and with a trailing-slash prefix the two resolve the owning repo differently.

    Check order is fixed escapes -> protected -> outside_surface: RepoWriteTools reports
    'protected' over 'outside your surface' when both apply, and the diff gate only tests
    None-vs-not-None, so this order preserves every site's behavior."""

    ESCAPES = "escapes"
    PROTECTED = "protected"
    OUTSIDE_SURFACE = "outside_surface"

    def __init__(self, surface: list[str] | None, protected=None, prefixes: list[str] | None = None,
                 *, protected_exact: bool = False, check_escapes: bool = True,
                 allow_exceptions: list[str] | None = None):
        self.surface = None if surface is None else list(surface)
        self.prefixes = list(prefixes or [])
        self.protected_exact = protected_exact
        # Exact mode keeps the raw names (case-sensitive set membership); glob mode pre-folds to
        # the case-insensitive/forward-slash canonical form once, like gate() always did.
        self.protected = (set(protected or []) if protected_exact
                          else [_ci(g) for g in (protected or [])])
        # Explicit editable EXCEPTIONS that OVERRIDE the protect-list (F11). A protect glob broad enough
        # to catch every grader (`**/*grader*.py`) also catches migration scripts that merely CONTAIN
        # "grade" (upgrade.py / downgrade.py / upgrader.py) — which no glob can distinguish from a real
        # `pregrader.py`. An explicit exception list carves those back out. EMPTY by default, so every
        # existing site is byte-for-byte unchanged; only the DEFAULT_PROTECT write path opts in. Matched
        # case-insensitively like the glob-mode protect list. NOT applied to a manifest's explicit
        # protect entries — those callers pass no exceptions, so their intent always wins.
        self.allow_exceptions = [_ci(g) for g in (allow_exceptions or [])]
        self.check_escapes = check_escapes

    def _is_protected(self, path: str) -> bool:
        if self.allow_exceptions:
            pc_x = _ci(path)
            if any(pc_x == g or _match(pc_x, g) for g in self.allow_exceptions):
                return False   # an explicit editable exception overrides the protect-list (F11)
        if self.protected_exact:
            if path in self.protected:
                return True
            # Exact mode's protect entries are MOSTLY pre-normalized literal names, but glob forms
            # legitimately reach it too: the `dir/**` tree guard RepoTask emits for a read-only data
            # mount, AND operator-authored protect globs like `*.ckpt` (the SAME protected_names list
            # feeds the diff gate, which glob-matches them) — an exact membership test silently
            # ignored those, so the two enforcement sites disagreed about what is protected and a
            # write the diff gate would reject sailed through here. Glob entries match with the
            # shared `_match` semantics but case-SENSITIVELY (fnmatchcase), preserving exact mode's
            # documented case contract; plain names keep the O(1) set-membership fast path above.
            for g in self.protected:
                if g.endswith("/**") and path == g[:-3]:
                    return True    # the mount dir itself (`dir/**` needs ≥1 char after the slash)
                if any(ch in g for ch in "*?[") and _match(path, g, _fn=fnmatch.fnmatchcase):
                    return True
            return False
        pc = _ci(path)
        return any(pc == g or _match(pc, g) for g in self.protected)

    def check(self, path: str) -> str | None:
        """None if `path` is editable, else the first failing reason code
        (ESCAPES | PROTECTED | OUTSIDE_SURFACE)."""
        if self.check_escapes and _escapes(path):
            return self.ESCAPES
        if self._is_protected(path):
            return self.PROTECTED
        if self.surface is not None and not _in_surface(path, self.surface, self.prefixes):
            return self.OUTSIDE_SURFACE
        return None


def gate(diff_text: str, allow: list[str], protect: list[str] | None = None,
         prefixes: list[str] | None = None,
         allow_exceptions: list[str] | None = None) -> dict:
    """Check every target path against the allow-list (globs), the protect-list, and escape
    rules. `ok` iff the patch is non-empty and every path is within the surface AND none is
    protected. A protected path is rejected (reject-not-strip) even when it matches the surface
    — the agent must never edit the eval/grader/metric/adapter files, case-insensitively (the
    surface gate + NTFS are case-insensitive, so a case-variant must not slip through).
    `prefixes` (named multi-editable repo dirs) scopes each repo's surface to its own subdir."""
    policy = SurfacePolicy(
        allow, protect, [x.rstrip("/") for x in (prefixes or [])],
        allow_exceptions=allow_exceptions,
    )
    paths = changed_paths(diff_text)
    rejected = [p for p in paths if policy.check(p) is not None]
    return {"ok": bool(paths) and not rejected, "paths": paths, "rejected": rejected}


def apply_patch(diff_text: str, repo_dir: str, allow: list[str],
                protect: list[str] | None = None, prefixes: list[str] | None = None,
                allow_exceptions: list[str] | None = None) -> dict:
    """Gate, then `git apply --check` then apply, inside `repo_dir`. Never applies a
    patch that fails the surface gate or the dry-run. `protect` (reject, don't strip — for the
    eval/metric/adapter/grader files) MUST be threaded through so this entry point can't be used to
    overwrite the score source; the live cli_agent path passes it and so should every caller."""
    g = gate(diff_text, allow, protect, prefixes, allow_exceptions)
    if not g["ok"]:
        return {"applied": False, "paths": g["paths"], "rejected": g["rejected"],
                "error": "out-of-surface" if g["rejected"] else "empty patch"}
    repo = Path(repo_dir)
    # Unique temp name so two concurrent apply_patch calls on the same repo can't clobber each other's
    # patch between --check and apply (or unlink a sibling's file mid-apply).
    fd, patch_path = tempfile.mkstemp(dir=str(repo), prefix=".LoopLab.", suffix=".patch")
    patch_name = Path(patch_path).name
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as pf:
            pf.write(diff_text)
        chk = subprocess.run(["git", "apply", "--check", patch_name],
                             cwd=str(repo), capture_output=True, text=True)
        if chk.returncode != 0:
            return {"applied": False, "paths": g["paths"], "rejected": [],
                    "error": chk.stderr.strip()}
        ap = subprocess.run(["git", "apply", patch_name],
                            cwd=str(repo), capture_output=True, text=True)
        return {"applied": ap.returncode == 0, "paths": g["paths"], "rejected": [],
                "error": "" if ap.returncode == 0 else ap.stderr.strip()}
    finally:
        Path(patch_path).unlink(missing_ok=True)
