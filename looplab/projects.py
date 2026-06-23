"""ClearML-style project organization for the live UI (a SEPARATE, UI-only concern — never
imported by the engine or `replay.fold`). Projects are a nestable folder tree that groups runs;
membership is metadata in `<run-root>/projects.json`, so runs stay physically where they are
(moving a run dir would break its append-only `events.jsonl` + resume). Single writer = the UI
server, so a plain read-modify-atomic-write is enough; no cross-process lock like the event log.

Shape of projects.json:
    {"projects": [{"id","name","parent_id"}, ...],
     "assignments": {"<run_id>": "<project_id>", ...}}
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from .models import Project


class ProjectError(ValueError):
    """Invalid project operation (unknown id, cycle, …) — the server maps this to HTTP 400."""


def _new_id() -> str:
    return "p_" + uuid.uuid4().hex[:10]


class ProjectStore:
    """Read/modify/atomic-write CRUD over `<run-root>/projects.json`. Each mutating method loads
    the current file, applies the change, persists, and returns the relevant result — so the
    on-disk file is always the source of truth and concurrent server workers never diverge."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        # Each mutating op is load→mutate→atomic-save; a lock makes that read-modify-write atomic
        # WITHIN the process. FastAPI runs sync handlers (e.g. delete_project) in a threadpool while
        # async handlers run on the event loop, so without this a threadpool write can interleave
        # with an event-loop write and clobber it (lost update). os.replace already makes reads
        # see a whole file, so read-only paths don't need the lock.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ load / save
    def load(self) -> dict:
        if not self.path.exists():
            return {"projects": [], "assignments": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"projects": [], "assignments": {}}
        data.setdefault("projects", [])
        data.setdefault("assignments", {})
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)   # atomic on POSIX + Windows

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
        with self._lock:
            data = self.load()
            if parent_id is not None:
                self._require(data, parent_id)
            proj = Project(id=_new_id(), name=(name or "untitled").strip() or "untitled",
                           parent_id=parent_id)
            data["projects"].append(proj.model_dump())
            self._save(data)
            return proj

    def rename(self, pid: str, name: str) -> Project:
        with self._lock:
            data = self.load()
            p = self._require(data, pid)
            p["name"] = (name or "").strip() or p["name"]
            self._save(data)
            return Project(**p)

    def reparent(self, pid: str, parent_id: str | None) -> Project:
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            data = self.load()
            if project_id is None:
                data["assignments"].pop(run_id, None)
            else:
                self._require(data, project_id)
                data["assignments"][run_id] = project_id
            self._save(data)

    def project_of(self, run_id: str) -> str | None:
        return self.load()["assignments"].get(run_id)
