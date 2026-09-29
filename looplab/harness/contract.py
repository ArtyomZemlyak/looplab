"""The external candidate's file authority, shared by HTTP intake and the engine."""
from __future__ import annotations


def candidate_surface_refusal(repo_spec: dict | None, files, deleted) -> str | None:
    paths = list(files or {}) + list(deleted or ())
    if not paths:
        return None
    # The ENGINE holds `{}` for a task with no repository (`orchestrator.py`: `task.repo_spec()` or
    # `{}`), the server's intake holds None; both mean "no editable tree", and `{}` used to fall
    # through to a KeyError on `repo_spec["editables"]` inside the engine's inject validation.
    if not repo_spec:
        return "file overlays require a repository task; submit script code instead"
    from looplab.tools.patch import SurfacePolicy

    prefixes = [e["name"] for e in repo_spec["editables"] if e["name"] not in (".", "")]
    policy = SurfacePolicy(repo_spec["edit_surface"],
                           ["solution.py", *repo_spec["protected_names"]], prefixes)
    for path in paths:
        reason = policy.check(path)
        if reason is not None:
            return f"candidate file {path!r} is {reason}"
    return None
