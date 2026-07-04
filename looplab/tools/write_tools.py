"""Write/edit tool provider for the assistant: create, string-edit, patch and delete files — gated by
the SAME path/secret rules as the read scout, plus a protect-list and the permission MODE.

Same `.specs()`/`.execute(name, args)` shape as `RepoScoutTools`/`RunTools`, so it drops into the
shared `agent.drive_tool_loop` via `CompositeTools`. Every mutation is:
  1. GATED — resolve within allowed roots, refuse secrets (`_pathsafe.looks_secret`), refuse
     protected paths (run event logs / graders / .git — see `perm_modes.DEFAULT_PROTECT`);
  2. AUTHORIZED by the mode — `plan` refuses, `default`/`acceptEdits` may ask (the injected
     `approver` blocks on a UI confirm-card), `auto`/`acceptEdits` apply inline;
  3. only then applied to disk. A refusal is a normal string returned to the model (never an
     exception), so the loop keeps going.
"""
from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
from typing import Callable, Optional

from looplab.core import _pathsafe
from looplab.tools._base import fn_spec
from looplab.tools.patch import SurfacePolicy, apply_patch as _apply_patch, gate as _gate
from looplab.tools.perm_modes import DEFAULT_PROTECT, decide, default_approver

_MAX_PREVIEW = 4000


class FileBackups:
    """Pre-mutation snapshots so a file edit can be reverted (undo). Each mutated path gets a stack of
    `.bak` snapshots under `<dir>/<hash(path)>/`; `revert` restores (or deletes, if the file was newly
    created) the most recent one and pops it. Best-effort — a backup failure never blocks the edit."""

    def __init__(self, directory):
        self.dir = Path(directory)

    def _key(self, path) -> Path:
        return self.dir / hashlib.sha1(str(Path(path).resolve()).encode()).hexdigest()[:16]

    def save(self, path) -> None:
        try:
            p = Path(path)
            d = self._key(p)
            d.mkdir(parents=True, exist_ok=True)
            # max(index)+1, NOT len(): a gap (from a revert pop or a failed save) must not reuse a
            # live slot and clobber an existing backup.
            existing = [int(b.stem) for b in d.glob("*.bak") if b.stem.isdigit()]
            n = (max(existing) + 1) if existing else 0
            (d / f"{n}.bak").write_bytes(p.read_bytes() if p.is_file() else b"")
            (d / f"{n}.meta").write_text(json.dumps({"path": str(p), "existed": p.is_file()}))
        except OSError:
            pass

    def revert(self, path) -> bool:
        p = Path(path)
        d = self._key(p)
        if not d.is_dir():
            return False
        baks = sorted(d.glob("*.bak"), key=lambda x: int(x.stem))
        if not baks:
            return False
        last = baks[-1]
        try:
            meta = json.loads((d / f"{last.stem}.meta").read_text())
        except (OSError, ValueError):
            meta = {"existed": True}
        if meta.get("existed"):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(last.read_bytes())
        elif p.exists():
            p.unlink()
        last.unlink(missing_ok=True)
        (d / f"{last.stem}.meta").unlink(missing_ok=True)
        return True


def _diff(path: str, old: str, new: str) -> str:
    d = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                             fromfile=f"a/{path}", tofile=f"b/{path}")
    return "".join(d)[:_MAX_PREVIEW]


class WriteTools:
    def __init__(self, roots, mode: str = "plan", protect: Optional[list] = None,
                 approver: Optional[Callable[[dict], str]] = None, repo_root=None, backup_dir=None):
        self._roots = _pathsafe.resolve_roots(roots)
        self.mode = mode
        self.protect = list(protect if protect is not None else DEFAULT_PROTECT)
        self.approver = approver or default_approver
        self.repo_root = Path(repo_root).resolve() if repo_root else (self._roots[0] if self._roots else Path.cwd())
        self.applied: list[dict] = []
        self.backups = FileBackups(backup_dir) if backup_dir else None

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        return [
            fn_spec("write_file",
                     "Create or OVERWRITE a text file with the given content (within the allowed "
                     "roots; refused for protected/secret paths). Prefer edit_file for small changes.",
                     {"path": {"type": "string"}, "content": {"type": "string"}},
                     ["path", "content"]),
            fn_spec("edit_file",
                     "Replace the SINGLE occurrence of old_str with new_str in a file (exact match; "
                     "errors if old_str is missing or appears more than once — add surrounding context "
                     "to disambiguate). Read the file first.",
                     {"path": {"type": "string"}, "old_str": {"type": "string"},
                      "new_str": {"type": "string"}}, ["path", "old_str", "new_str"]),
            fn_spec("apply_patch",
                     "Apply a unified git diff (a/… b/… headers) to the repo — for multi-file or "
                     "surgical changes. Gated + `git apply --check`ed before it touches disk.",
                     {"diff": {"type": "string"}}, ["diff"]),
            fn_spec("delete_file",
                     "Delete a file (within the allowed roots; refused for protected/secret paths).",
                     {"path": {"type": "string"}}, ["path"]),
            fn_spec("revert_file",
                     "Undo your most recent change to a file (restore the pre-edit snapshot).",
                     {"path": {"type": "string"}}, ["path"]),
        ]

    def execute(self, name: str, args: dict) -> str:
        args = args or {}
        try:
            if name == "write_file":
                return self._write(args.get("path", ""), args.get("content", ""))
            if name == "edit_file":
                return self._edit(args.get("path", ""), args.get("old_str", ""), args.get("new_str", ""))
            if name == "apply_patch":
                return self._patch(args.get("diff", ""))
            if name == "delete_file":
                return self._delete(args.get("path", ""))
            if name == "revert_file":
                return self._revert_tool(args.get("path", ""))
            return f"(unknown tool: {name})"
        except Exception as e:  # noqa: BLE001 - tools are advisory; never crash the loop
            return f"(error: {e})"

    # --- gating -------------------------------------------------------------
    def _rel(self, p: Path) -> str:
        for r in self._roots:
            try:
                return p.relative_to(r).as_posix()
            except ValueError:
                continue
        return p.name

    def _protected(self, rel: str) -> bool:
        # SurfacePolicy in protect-only form: surface=None (containment is enforced by
        # resolve_within on the resolved roots, not globs) and check_escapes=False (a traversal
        # can't survive resolve_within). Built per call so a live mutation of `self.protect`
        # keeps taking effect, exactly like the old inline loop.
        policy = SurfacePolicy(None, self.protect, check_escapes=False)
        return policy.check(rel) == SurfacePolicy.PROTECTED

    def _check(self, path: str):
        """Return (resolved_path, rel, None) or (None, None, refusal_string)."""
        p = _pathsafe.resolve_within(self._roots, path)
        if p is None:
            return None, None, f"(refused: {path} is outside the allowed roots)"
        rel = self._rel(p)
        # Secret check on the ROOT-RELATIVE path, not the absolute one: otherwise a secret-named
        # component in the ROOT prefix (e.g. a workspace under /srv/.docker/… or ~/.config/…) would
        # falsely refuse EVERY file under it. A real secret inside the workspace (`.env`, `.ssh/…`)
        # still matches on the relative path.
        if _pathsafe.looks_secret(Path(rel)):
            return None, None, f"(refused: {p.name} looks like a secret/credential — not writable)"
        if self._protected(rel):
            return None, None, f"(refused: {rel} is protected — run-integrity/grader/.git files are read-only)"
        return p, rel, None

    def _authorize(self, tool_kind: str, action: dict) -> Optional[str]:
        """None => proceed; a string => the refusal/declined message to return to the model."""
        d = decide(self.mode, tool_kind)
        if d == "deny":
            return (f"(plan mode is read-only — I can't {action.get('verb', 'do that')}. "
                    "Switch the assistant to default/acceptEdits/auto to apply changes.)")
        if d == "ask":
            # The approver's verdict vocabulary ("allow_once"/"allow_always"/"deny") is the permission-
            # decision wire protocol named in looplab/serve/protocol.py (PERM_*). It is string-matched
            # here rather than imported because tools must never import serve (layering).
            verdict = str(self.approver(action) or "deny")
            if not verdict.startswith("allow"):
                return f"(declined by the user: {action.get('label', 'change')})"
        return None

    # --- mutations ----------------------------------------------------------
    def _write(self, path: str, content: str) -> str:
        p, rel, err = self._check(path)
        if err:
            return err
        if p.exists() and p.is_dir():
            return f"(refused: {rel} is a directory)"
        old = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        preview = _diff(rel, old, content)
        action = {"tool": "write_file", "tool_kind": "write", "path": rel, "verb": f"write {rel}",
                  "label": f"{'overwrite' if old else 'create'} {rel}", "preview": preview, "abs_path": str(p)}
        refusal = self._authorize("write", action)
        if refusal:
            return refusal
        if self.backups:
            self.backups.save(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        self.applied.append(action)
        return f"(wrote {rel}, {len(content)} bytes)"

    def _edit(self, path: str, old_str: str, new_str: str) -> str:
        # Deliberately NOT tools/edit_match.py's apply_search_replace: the assistant's edit_file is
        # EXACT-match only (its own arg names + error strings are part of the tool contract here),
        # and adopting the repo developer's whitespace-tolerant fallback would silently change what
        # this tool applies. Keep the two matchers' semantics distinct on purpose.
        p, rel, err = self._check(path)
        if err:
            return err
        if not p.is_file():
            return f"(no such file: {rel})"
        if not old_str:
            return "(edit_file needs a non-empty old_str; use write_file to create a file)"
        text = p.read_text(encoding="utf-8", errors="replace")
        n = text.count(old_str)
        if n == 0:
            return f"(old_str not found in {rel} — read the file and copy the exact text)"
        if n > 1:
            return f"(old_str appears {n} times in {rel} — add surrounding context so it's unique)"
        new_text = text.replace(old_str, new_str, 1)
        preview = _diff(rel, text, new_text)
        action = {"tool": "edit_file", "tool_kind": "write", "path": rel, "verb": f"edit {rel}",
                  "label": f"edit {rel}", "preview": preview, "abs_path": str(p)}
        refusal = self._authorize("write", action)
        if refusal:
            return refusal
        if self.backups:
            self.backups.save(p)
        p.write_text(new_text, encoding="utf-8")
        self.applied.append(action)
        return f"(edited {rel})"

    def _patch(self, diff: str) -> str:
        if not diff.strip():
            return "(empty diff)"
        allow = ["**/*"]
        g = _gate(diff, allow, self.protect)
        if not g["ok"]:
            if g["rejected"]:
                return f"(refused: out-of-surface/protected paths: {', '.join(g['rejected'])})"
            return "(empty/unparseable patch)"
        # The surface gate doesn't know about credential files (DEFAULT_PROTECT has no secret patterns),
        # so apply the SAME secret guard the write/edit/delete paths enforce — a diff must not be able
        # to create/overwrite an .env / id_rsa / *.pem that write_file would refuse.
        secret = [rp for rp in g["paths"] if _pathsafe.looks_secret(Path(rp))]
        if secret:
            return f"(refused: patch touches secret/credential paths: {', '.join(secret)})"
        action = {"tool": "apply_patch", "tool_kind": "write",
                  "label": f"apply patch ({len(g['paths'])} file(s))", "verb": "apply this patch",
                  "preview": diff[:_MAX_PREVIEW], "paths": g["paths"]}
        refusal = self._authorize("write", action)
        if refusal:
            return refusal
        if self.backups:
            for rp in g["paths"]:
                self.backups.save(self.repo_root / rp)
        res = _apply_patch(diff, str(self.repo_root), allow, self.protect)
        if not res.get("applied"):
            return f"(patch failed: {res.get('error', 'unknown')})"
        self.applied.append(action)
        return f"(applied patch to {', '.join(res.get('paths', []))})"

    def _delete(self, path: str) -> str:
        p, rel, err = self._check(path)
        if err:
            return err
        if not p.exists():
            return f"(no such file: {rel})"
        if p.is_dir():
            return f"(refused: {rel} is a directory — delete files, not directories)"
        action = {"tool": "delete_file", "tool_kind": "write", "path": rel, "verb": f"delete {rel}",
                  "label": f"delete {rel}", "preview": f"delete {rel}", "abs_path": str(p)}
        refusal = self._authorize("write", action)
        if refusal:
            return refusal
        if self.backups:
            self.backups.save(p)
        p.unlink()
        self.applied.append(action)
        return f"(deleted {rel})"

    def _revert_tool(self, path: str) -> str:
        """The MODEL-invocable revert: same disk mutation as `revert`, but gated by the permission
        mode/approver like every other mutation (a revert overwrites the file from a snapshot —
        possibly clobbering manual fixes the user made since) and recorded in `applied` so the turn
        shows it. The bare `revert` below stays un-gated for the server's explicit user-clicked undo."""
        p, rel, err = self._check(path)
        if err:
            return err
        if not self.backups:
            return "(no snapshots available to revert)"
        # No abs_path in the record: the UI's per-change "undo" can't undo a revert (the snapshot is
        # popped), so don't offer the affordance.
        action = {"tool": "revert_file", "tool_kind": "write", "path": rel, "verb": f"revert {rel}",
                  "label": f"revert {rel}", "preview": f"restore {rel} from its pre-edit snapshot"}
        refusal = self._authorize("write", action)
        if refusal:
            return refusal
        ok = self.backups.revert(p)
        if ok:
            self.applied.append(action)
            return f"(reverted {rel})"
        return f"(no snapshot to revert for {rel})"

    def revert(self, path: str) -> str:
        p, rel, err = self._check(path)
        if err:
            return err
        if not self.backups:
            return "(no snapshots available to revert)"
        ok = self.backups.revert(p)
        return f"(reverted {rel})" if ok else f"(no snapshot to revert for {rel})"
