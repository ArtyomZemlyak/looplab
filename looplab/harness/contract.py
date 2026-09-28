"""The external candidate's file authority, shared by HTTP intake and the engine."""
from __future__ import annotations


def candidate_surface_refusal(repo_spec: dict | None, files, deleted) -> str | None:
    paths = list(files or {}) + list(deleted or ())
    if not paths:
        return None
    if repo_spec is None:
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
