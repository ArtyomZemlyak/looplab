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
from pathlib import Path
from typing import Callable, Optional

from . import _pathsafe
from .knowledge_tools import _fn_spec
from .patch import _ci, _match, apply_patch as _apply_patch, gate as _gate
from .perm_modes import DEFAULT_PROTECT, decide, default_approver

_MAX_PREVIEW = 4000


def _diff(path: str, old: str, new: str) -> str:
    d = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                             fromfile=f"a/{path}", tofile=f"b/{path}")
    return "".join(d)[:_MAX_PREVIEW]


class WriteTools:
    def __init__(self, roots, mode: str = "plan", protect: Optional[list] = None,
                 approver: Optional[Callable[[dict], str]] = None, repo_root=None):
        self._roots = _pathsafe.resolve_roots(roots)
        self.mode = mode
        self.protect = list(protect if protect is not None else DEFAULT_PROTECT)
        self.approver = approver or default_approver
        self.repo_root = Path(repo_root).resolve() if repo_root else (self._roots[0] if self._roots else Path.cwd())
        self.applied: list[dict] = []

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        return [
            _fn_spec("write_file",
                     "Create or OVERWRITE a text file with the given content (within the allowed "
                     "roots; refused for protected/secret paths). Prefer edit_file for small changes.",
                     {"path": {"type": "string"}, "content": {"type": "string"}},
                     ["path", "content"]),
            _fn_spec("edit_file",
                     "Replace the SINGLE occurrence of old_str with new_str in a file (exact match; "
                     "errors if old_str is missing or appears more than once — add surrounding context "
                     "to disambiguate). Read the file first.",
                     {"path": {"type": "string"}, "old_str": {"type": "string"},
                      "new_str": {"type": "string"}}, ["path", "old_str", "new_str"]),
            _fn_spec("apply_patch",
                     "Apply a unified git diff (a/… b/… headers) to the repo — for multi-file or "
                     "surgical changes. Gated + `git apply --check`ed before it touches disk.",
                     {"diff": {"type": "string"}}, ["diff"]),
            _fn_spec("delete_file",
                     "Delete a file (within the allowed roots; refused for protected/secret paths).",
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
        pc = _ci(rel)
        return any(pc == _ci(g) or _match(pc, _ci(g)) for g in self.protect)

    def _check(self, path: str):
        """Return (resolved_path, rel, None) or (None, None, refusal_string)."""
        p = _pathsafe.resolve_within(self._roots, path)
        if p is None:
            return None, None, f"(refused: {path} is outside the allowed roots)"
        if _pathsafe.looks_secret(p):
            return None, None, f"(refused: {p.name} looks like a secret/credential — not writable)"
        rel = self._rel(p)
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
                  "label": f"{'overwrite' if old else 'create'} {rel}", "preview": preview}
        refusal = self._authorize("write", action)
        if refusal:
            return refusal
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        self.applied.append(action)
        return f"(wrote {rel}, {len(content)} bytes)"

    def _edit(self, path: str, old_str: str, new_str: str) -> str:
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
                  "label": f"edit {rel}", "preview": preview}
        refusal = self._authorize("write", action)
        if refusal:
            return refusal
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
        action = {"tool": "apply_patch", "tool_kind": "write",
                  "label": f"apply patch ({len(g['paths'])} file(s))", "verb": "apply this patch",
                  "preview": diff[:_MAX_PREVIEW], "paths": g["paths"]}
        refusal = self._authorize("write", action)
        if refusal:
            return refusal
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
                  "label": f"delete {rel}", "preview": f"delete {rel}"}
        refusal = self._authorize("write", action)
        if refusal:
            return refusal
        p.unlink()
        self.applied.append(action)
        return f"(deleted {rel})"
