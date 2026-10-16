"""Doc 79 `code_from` on the server: ONE conversion of a worktree / patch / foreign branch into the
`files`/`deleted` overlay every inject carries, shared by the inject intake
(`control_validation.py::_apply_code_from`) and the `/code/overlay` preview
(`routers/upstream_ops.py`), plus the ONE mapping of the engine's code refusals to HTTP.

Why one module (review 2026-10-16). The intake and the preview each spelled the shape, the dispatch
and the error mapping, and they disagreed: the intake caught only `CodeRepoError` (an `OSError` from
the run's repository reached the caller as a 500), turned every `git_*` code into a 503 (a missing
worktree directory — `git -C <missing>` — read as "server unavailable", which the MCP client then
reported as an unknown WRITE outcome even for a pure preview), dropped the conflict's `paths` and
accepted an overlay that changed nothing (`node_build.inject_needs_developer` then bought a
Developer build). The checks every caller now gets, in order:

1. SHAPE — `serve/protocol.py::code_from_shape_error` (the vocabulary the composite reads too).
2. WHO MAY NAME A FOREIGN REPOSITORY — `require_git_source_permitted`: for the scoped agent token
   only the run's own recorded source repository or a git worktree OF it (the same common git
   directory). Any other absolute path was a read primitive on a shared hub: the server fetched any
   repository its process could read into the run's private repository, and a run worktree then
   checked it out. The owner credential may still name any path.
3. THE CONVERSION — `engine/code_worktree.py` / `engine/code_foreign.py`, with a missing worktree
   DIRECTORY refused by name (`code_worktree_missing`) before git is asked.
4. NOT EMPTY — `code_from_empty`.
5. ON THE CURRENT BASE — `require_current_base`: an overlay is WHOLE files over the base its start
   was seeded on, and the inject materializes it over the run's ACTIVE base. When an upstream
   advance moved the active base and changed a path the overlay also carries, the submission would
   silently revert that advance; refused as `code_from_stale_base` with the paths, unless the
   worktree merged the active base (`git merge base`) or carries exactly the active base's bytes
   there.

The conversion can run a long `git fetch` (`code_foreign.py::_ensure_commit`, each fetch bounded by `FETCH_TIMEOUT_S`), so
the command service runs it BEFORE it takes the run's command sequencer
(`run_commands.py::RunCommandService._precompute_code_from`) and the intake re-checks only step 5
under the lock, against the log as it is then.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import HTTPException

from looplab.serve.protocol import code_from_shape_error, normalize_code_from

# A refusal the SAME request may later succeed with: never recorded as a durable rejection (the
# same idempotency key then retries), and the only codes answered with a 5xx.
CODE_FROM_TRANSIENT_CODES = frozenset({"git_timeout", "git_missing", "code_storage_unavailable",
                                       "code_base_unavailable"})
# Refusals whose `paths` the agent resolves: kept on a rejected command record's error.
CODE_FROM_PATH_CODES = frozenset({"code_from_merge_conflict", "code_from_stale_base"})
MAX_REFUSAL_PATHS = 200


def code_http_error(exc: BaseException) -> HTTPException:
    """The ONE mapping of a code-repository failure to HTTP. Deterministic refusals (bad input, a
    git command that failed on this repository's state) are 4xx; only a git that is missing or
    timed out, and storage that cannot be read, are 503 — what a retry of the same request can cure."""
    from looplab.engine.code_foreign import OverlayConflict
    from looplab.engine.code_repo import CodeRepoError
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, OverlayConflict):
        return HTTPException(409, {"code": exc.code, "message": str(exc),
                                   "paths": list(exc.paths)[:MAX_REFUSAL_PATHS]})
    if isinstance(exc, CodeRepoError):
        if exc.code in CODE_FROM_TRANSIENT_CODES:
            return HTTPException(503, {"code": exc.code, "message": str(exc), "retryable": True})
        status = 409 if (exc.code.endswith("_exists") or exc.code == "git_failed") else 400
        return HTTPException(status, {"code": exc.code, "message": str(exc)})
    if isinstance(exc, OSError):
        return HTTPException(503, {"code": "code_storage_unavailable", "retryable": True,
                                   "message": "The run's code repository could not be read"})
    raise TypeError(f"not a code-repository failure: {type(exc).__name__}")


def _require_worktree_directory(rd: Path, name) -> None:
    """A worktree whose `.meta` row survives but whose directory is gone (a tmpfs wipe, a manual
    `rm -rf`): said by name, never as git's `cannot change to` failure."""
    from looplab.engine.code_worktree import list_worktrees
    meta = next((m for m in list_worktrees(rd) if isinstance(m, dict) and m.get("name") == name),
                None)
    if meta is None:
        return                                    # unknown names keep the engine's own refusal
    path = str(meta.get("path") or "")
    if not path or not Path(path).is_dir():
        raise HTTPException(409, {
            "code": "code_worktree_missing",
            "message": f"worktree {name}'s directory {path[:300]} no longer exists",
            "remediation": ("`looplab code-status --sync` restores a worktree a run-mirror backup "
                            "carries; otherwise create a new worktree (code_worktree with a new "
                            "name) and redo the change there")})


def _common_dir(path: Path) -> Optional[Path]:
    """The common git directory a path's repository (or worktree) shares, resolved; None when it
    is not in git. Read-only (`--no-optional-locks`, the hermetic `run_git`)."""
    from looplab.engine.code_repo import run_git
    if not path.is_dir():
        return None
    out = run_git("-C", str(path), "--no-optional-locks", "rev-parse", "--path-format=absolute",
                  "--git-common-dir", check=False, timeout=30)
    text = out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else ""
    return Path(text).resolve() if text else None


def _source_common_dirs(rd: Path) -> list:
    """The common git directories of the run's editable SOURCES (the operator's checkout,
    `origin_path` under a pinned `seed_base`), read through the task adapter."""
    from looplab.adapters.tasks import load_task
    from looplab.core.errors import OperatorRefusal
    try:
        task = load_task(Path(rd) / "task.snapshot.json", existing_run=True)
        spec = task.repo_spec() if hasattr(task, "repo_spec") else None
    except (OperatorRefusal, OSError, ValueError, TypeError, KeyError, AttributeError):
        return []                                 # nobody can say where the source is: none allowed
    out = []
    for editable in (spec or {}).get("editables") or ():
        source = editable.get("origin_path") or editable.get("path")
        if isinstance(source, str) and source:
            common = _common_dir(Path(source).expanduser())
            if common is not None and common not in out:
                out.append(common)
    return out


def require_git_source_permitted(rd: Path, foreign, *, agent_token: bool) -> None:
    """The scoped agent token may name only the run's own source repository or a git worktree of
    it (same common git directory); the owner credential may name any path (review 2026-10-16)."""
    if not agent_token:
        return
    repo = foreign.get("repo") if isinstance(foreign, dict) else None
    mine = (_common_dir(Path(repo)) if isinstance(repo, str) and repo and len(repo) <= 4096
            and Path(repo).is_absolute() else None)
    if mine is None or mine not in _source_common_dirs(rd):
        raise HTTPException(403, {
            "code": "code_from_git_not_permitted",
            "message": ("the agent token may merge a branch only from the run's own source "
                        "repository or a git worktree of it"),
            "remediation": ("commit your branch in a worktree of the run's source repository, or "
                            "use code_worktree (the run's private repository); the operator may "
                            "name any repository"),
            "retryable": False})


def require_current_base(rd: Path, overlay: dict, events=None) -> None:
    """Refuse an overlay computed over a base the run has since advanced past when it carries a path
    that advance changed (see the module docstring, step 5). Reads the log for the active base."""
    from looplab.engine.code_identity import BASE_REF, active_base_digest, sync_run
    from looplab.engine.code_repo import CodeRepo
    overlay_base = overlay.get("overlay_base")
    if not isinstance(overlay_base, str):
        return
    if events is None:
        from looplab.events.eventstore import EventStore
        events = EventStore(Path(rd) / "events.jsonl").read_all()
    digest = active_base_digest(events)
    if digest is None:
        return                                    # no base lineage: nothing can have advanced
    repo = CodeRepo(rd)
    active = repo.rev(BASE_REF + digest)
    if active is None:
        sync_run(rd, events)                      # the log names a base the repository lacks yet
        active = repo.rev(BASE_REF + digest)
    if active is None:
        raise HTTPException(503, {"code": "code_base_unavailable", "retryable": True,
                                  "message": "the run's active base is not in its code repository"})
    if active == overlay_base:
        return
    head = overlay.get("head_commit")
    if isinstance(head, str) and repo.git("merge-base", "--is-ancestor", active, head,
                                          check=False).returncode == 0:
        return                                    # the worktree merged the active base
    from looplab.engine.code_worktree import is_blob_marker
    moved = {row["path"] for row in repo.changed(overlay_base, active)}
    files, deleted = overlay.get("files") or {}, overlay.get("deleted") or []
    stale = []
    for path in sorted(moved & (set(files) | set(deleted))):
        now = repo.blob(active, path)
        if path in files and not is_blob_marker(files[path]) and now is not None \
                and now == str(files[path]).encode("utf-8"):
            continue                              # the overlay already holds the active bytes
        if path in deleted and now is None:
            continue
        stale.append(path)
    if stale:
        raise HTTPException(409, {
            "code": "code_from_stale_base",
            "message": ("the run's base advanced since this change's start and changed "
                        + ", ".join(stale[:10]) + " — submitting it would revert that advance"),
            "remediation": ("merge the current base into your change: in the worktree run `git "
                            "merge base` (resolve these paths) and resubmit, or create a fresh "
                            "worktree (code_worktree, start='base') and `git merge agent/<old "
                            "name>` there; a patch is regenerated against start='base'"),
            "paths": stale[:MAX_REFUSAL_PATHS], "retryable": False})


def compute_overlay(rd: Path, spec, *, agent_token: bool, include_files: bool = True) -> dict:
    """Shape -> permission -> conversion -> not empty -> current base. Raises `HTTPException`
    (through `code_http_error`) for every refusal; the result is the engine's overlay dict."""
    from looplab.engine.code_repo import CodeRepoError
    problem = code_from_shape_error(spec)
    if problem:
        raise HTTPException(400, {"code": "code_from_invalid", "message": problem})
    spec = normalize_code_from(spec)
    rd = Path(rd)
    try:
        if "worktree" in spec:
            from looplab.engine.code_worktree import overlay_from_worktree
            _require_worktree_directory(rd, spec["worktree"])
            overlay = overlay_from_worktree(rd, spec["worktree"])
        elif "git" in spec:
            from looplab.engine.code_foreign import overlay_from_git
            require_git_source_permitted(rd, spec["git"], agent_token=agent_token)
            overlay = overlay_from_git(rd, spec["git"])
        else:
            from looplab.engine.code_worktree import overlay_from_patch
            overlay = overlay_from_patch(rd, spec["patch"], start=str(spec["start"]))
        if not overlay.get("files") and not overlay.get("deleted"):
            raise HTTPException(400, {
                "code": "code_from_empty",
                "message": "the worktree/patch/branch changes nothing against the base",
                "remediation": "edit (and commit) the change first, then submit it"})
        require_current_base(rd, overlay)
    except (CodeRepoError, OSError) as exc:
        raise code_http_error(exc) from exc
    if not include_files:
        from looplab.engine.code_worktree import overlay_summary
        return overlay_summary(overlay)
    return overlay


__all__ = ["CODE_FROM_PATH_CODES", "CODE_FROM_TRANSIENT_CODES", "code_http_error",
           "compute_overlay", "require_current_base", "require_git_source_permitted"]
