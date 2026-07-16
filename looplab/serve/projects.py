"""ClearML-style project organization for the live UI (a SEPARATE, UI-only concern — never
imported by the engine or `replay.fold`). Projects are a nestable folder tree that groups runs;
membership is metadata in `<run-root>/projects.json`, so runs stay physically where they are
(moving a run dir would break its append-only `events.jsonl` + resume). Multiple UI processes may
share a run root, so every read-modify-write is serialized by a stable required interprocess lock.

Shape of projects.json:
    {"projects": [{"id","name","parent_id"}, ...],
     "assignments": {"<run_id>": "<project_id>", ...},
     "labels": {"<run_id>": "<display name>", ...},
     "supertasks": [{"id","name","task_id"}, ...],
     "supertask_assignments": {"<run_id>": "<supertask_id>", ...}}

`labels` is a UI-only display name for a run; the run's directory (its id) never changes, so the
event log + resume stay intact — exactly like assignments, this is a non-destructive overlay.

`supertasks` are a SECOND, flat (non-nested) grouping axis orthogonal to projects: a user-named
"global task" that many runs attack (what the cross-run sweep auto-groups by `task_id`, but
user-managed — create it, then assign existing/new runs). A run can sit in a project AND a
super-task at once; `supertask_assignments` is the same non-destructive run_id→id overlay.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from looplab.core.atomicio import atomic_write_text
from looplab.core.models import Project


class ProjectError(ValueError):
    """Invalid project operation (unknown id, cycle, …) — the server maps this to HTTP 400."""


class ProjectStoreLockError(RuntimeError):
    """A project mutation could not obtain its required cross-process serialization guarantee."""


def _new_id() -> str:
    return "p_" + uuid.uuid4().hex[:10]


def _new_st_id() -> str:
    return "st_" + uuid.uuid4().hex[:10]


class ProjectStore:
    """Read/modify/atomic-write CRUD over `<run-root>/projects.json`. Each mutating method loads
    the current file, applies the change, persists, and returns the relevant result — so the
    on-disk file is always the source of truth and concurrent server processes do not lose updates."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        # The local lock serializes FastAPI's threadpool and event-loop handlers. The stable sibling
        # lock extends the SAME load→mutate→atomic-save transaction across UI processes/workers.
        # Read-only paths need neither: os.replace always exposes one complete file.
        self._lock = threading.Lock()
        self._lock_path = Path(str(self.path) + ".lock")

    @contextmanager
    def _transaction(self):
        """Hold both locks before the mutator re-reads current state; fail closed if unsupported."""
        from looplab.events.eventstore import EventStoreLockError, _interprocess_lock

        try:
            with self._lock, _interprocess_lock(self._lock_path, required=True):
                yield
        except EventStoreLockError as exc:
            raise ProjectStoreLockError(
                f"project metadata lock is unavailable for {self.path}: {exc.cause}"
            ) from exc

    @contextmanager
    def transaction(self):
        """Public compound mutation fence for operations that also change another run-root resource."""
        with self._transaction():
            yield

    # ------------------------------------------------------------------ load / save
    @staticmethod
    def _empty() -> dict:
        return {"projects": [], "assignments": {}, "labels": {}, "supertasks": [], "supertask_assignments": {}}

    def load(self) -> dict:
        if not self.path.exists():
            return self._empty()
        # Only treat genuinely-corrupt CONTENT (unparseable JSON, or valid-but-wrong-shape) as empty.
        # A transient OSError (Windows sharing violation, AV/indexer lock, disk hiccup) is NOT caught
        # here: returning _empty() on a transient read failure would let the next mutating op _save() an
        # empty store over a perfectly good file, wiping every project/label/assignment. Let it raise.
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):   # corrupt content (bad JSON or bad UTF-8) -> skeleton; OSError still raises
            return self._empty()
        if not isinstance(data, dict):           # hand-edited to a list/str/number -> skeleton, not AttributeError
            return self._empty()
        for k, v in self._empty().items():       # backfill missing keys AND coerce wrong-typed ones
            cur = data.get(k)
            data[k] = cur if isinstance(cur, type(v)) else v
        return data

    def _save(self, data: dict) -> None:
        # atomic_write_text gives a UNIQUE temp (no fixed `.json.tmp` two writers can collide on) +
        # best-effort fsync, so a concurrent engine/UI write and a FUSE/S3 mount don't corrupt or abort.
        atomic_write_text(self.path, json.dumps(data, indent=2))

    # ------------------------------------------------------------------ queries
    def _index(self, data: dict) -> dict[str, dict]:
        return {p["id"]: p for p in data["projects"]}

    def _require(self, data: dict, pid: str) -> dict:
        idx = self._index(data)
        if pid not in idx:
            raise ProjectError(f"no such project: {pid!r}")
        return idx[pid]

    def _descendants(self, data: dict, pid: str) -> set[str]:
        """All projects transitively under `pid` (excluding pid itself)."""
        kids: dict[str, list[str]] = {}
        for p in data["projects"]:
            kids.setdefault(p.get("parent_id"), []).append(p["id"])
        out: set[str] = set()
        stack = list(kids.get(pid, []))
        while stack:
            cur = stack.pop()
            if cur in out:
                continue
            out.add(cur)
            stack.extend(kids.get(cur, []))
        return out

    # ------------------------------------------------------------------ CRUD
    def create(self, name: str, parent_id: str | None = None) -> Project:
        with self._transaction():
            data = self.load()
            if parent_id is not None:
                self._require(data, parent_id)
            proj = Project(id=_new_id(), name=(name or "untitled").strip() or "untitled",
                           parent_id=parent_id)
            data["projects"].append(proj.model_dump())
            self._save(data)
            return proj

    def rename(self, pid: str, name: str) -> Project:
        with self._transaction():
            data = self.load()
            p = self._require(data, pid)
            p["name"] = (name or "").strip() or p["name"]
            self._save(data)
            return Project(**p)

    def reparent(self, pid: str, parent_id: str | None) -> Project:
        with self._transaction():
            data = self.load()
            p = self._require(data, pid)
            if parent_id is not None:
                if parent_id == pid:
                    raise ProjectError("a project cannot be its own parent")
                self._require(data, parent_id)
                if parent_id in self._descendants(data, pid):
                    raise ProjectError("cannot move a project under its own descendant (cycle)")
            p["parent_id"] = parent_id
            self._save(data)
            return Project(**p)

    def delete(self, pid: str) -> None:
        """Delete a project; reparent its direct child projects and reassign its runs to the
        deleted project's parent (so nothing is orphaned — matches ClearML's behavior)."""
        with self._transaction():
            data = self.load()
            p = self._require(data, pid)
            parent = p.get("parent_id")
            data["projects"] = [q for q in data["projects"] if q["id"] != pid]
            for q in data["projects"]:
                if q.get("parent_id") == pid:
                    q["parent_id"] = parent
            for run_id, proj in list(data["assignments"].items()):
                if proj == pid:
                    if parent is None:
                        del data["assignments"][run_id]
                    else:
                        data["assignments"][run_id] = parent
            self._save(data)

    def assign(self, run_id: str, project_id: str | None) -> None:
        """Put a run in a project (or unassign when project_id is None)."""
        with self._transaction():
            data = self.load()
            if project_id is None:
                data["assignments"].pop(run_id, None)
            else:
                self._require(data, project_id)
                data["assignments"][run_id] = project_id
            self._save(data)

    def project_of(self, run_id: str) -> str | None:
        return self.load()["assignments"].get(run_id)

    def descendants(self, pid: str) -> set[str]:
        """Public: all projects transitively under `pid` (excluding pid). Used to scope a folder
        report to the project AND everything nested under it."""
        return self._descendants(self.load(), pid)

    # ------------------------------------------------------------------ super-tasks (flat axis)
    def _require_st(self, data: dict, sid: str) -> dict:
        st = next((s for s in data["supertasks"] if s["id"] == sid), None)
        if st is None:
            raise ProjectError(f"no such super-task: {sid!r}")
        return st

    def create_supertask(self, name: str, task_id: str | None = None) -> dict:
        with self._transaction():
            data = self.load()
            st = {"id": _new_st_id(), "name": (name or "untitled").strip() or "untitled",
                  "task_id": (task_id or None)}
            data["supertasks"].append(st)
            self._save(data)
            return st

    def rename_supertask(self, sid: str, name: str) -> dict:
        with self._transaction():
            data = self.load()
            st = self._require_st(data, sid)
            st["name"] = (name or "").strip() or st["name"]
            self._save(data)
            return st

    def delete_supertask(self, sid: str) -> None:
        """Delete a super-task and unassign its runs (flat axis — nothing to reparent)."""
        with self._transaction():
            data = self.load()
            self._require_st(data, sid)
            data["supertasks"] = [s for s in data["supertasks"] if s["id"] != sid]
            data["supertask_assignments"] = {r: v for r, v in data["supertask_assignments"].items()
                                             if v != sid}
            self._save(data)

    def assign_supertask(self, run_id: str, supertask_id: str | None) -> None:
        """Put a run in a super-task (or clear it when supertask_id is None)."""
        with self._transaction():
            data = self.load()
            if supertask_id is None:
                data["supertask_assignments"].pop(run_id, None)
            else:
                self._require_st(data, supertask_id)
                data["supertask_assignments"][run_id] = supertask_id
            self._save(data)

    def supertask_of(self, run_id: str) -> str | None:
        return self.load()["supertask_assignments"].get(run_id)

    # ------------------------------------------------------------------ run labels (UI display name)
    def set_label(self, run_id: str, label: str | None) -> None:
        """Give a run a display name (or clear it with None/empty). UI-only overlay — the run's
        directory id is never touched, so its event log and resume stay valid."""
        with self._transaction():
            data = self.load()
            label = (label or "").strip()
            if label:
                data["labels"][run_id] = label
            else:
                data["labels"].pop(run_id, None)
            self._save(data)

    def forget(self, run_id: str) -> None:
        """Drop all UI metadata for a run (used when its directory is deleted)."""
        with self._transaction():
            self.forget_locked(run_id)

    def forget_locked(self, run_id: str) -> None:
        """Drop run metadata while the caller already owns :meth:`transaction`."""
        data = self.load()
        data["assignments"].pop(run_id, None)
        data["labels"].pop(run_id, None)
        data["supertask_assignments"].pop(run_id, None)
        self._save(data)
