"""Run-owned Maintainer worktrees and conservative whole-overlay three-way rebasing.

The owner checkout is never a write target. Immutable archive bytes, not git HEAD,
are authoritative. Git is a reviewable, reachable projection for this lane only.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

from looplab.core.atomicio import atomic_write_bytes
from looplab.core.node_evidence import read_bounded_regular_file
from looplab.core.pathsafe import contained_member, is_reparse
from looplab.engine.seed_base import pinned_editables, selected_seed_base, seed_pinned_workspace
from looplab.engine.upstream_state import UpstreamRefusal, active_base
from looplab.events.replay import fold


def owned_path(rd, relative):
    root = Path(rd)
    path = contained_member(root, relative)
    if path is None:
        raise UpstreamRefusal("upstream_storage_unavailable", "Run-owned upstream path escapes its run")
    lexical = root
    for part in relative.replace("\\", "/").split("/"):
        lexical = lexical / part
        if os.path.lexists(lexical) and is_reparse(lexical.lstat()):
            raise UpstreamRefusal("upstream_storage_unavailable", "Run-owned upstream paths cannot be aliases")
    return path


def checked_overlay(spec, files, deleted=()):
    from looplab.core.scorer_boundary import normalize_scorer_boundary
    from looplab.tools.patch import SurfacePolicy
    if not isinstance(files, dict) or not isinstance(deleted, (list, tuple)) or any(type(s) is not str for s in deleted) or len(files) + len(deleted) > 128 or any(type(s) is not str for s in files.values()):
        raise UpstreamRefusal("upstream_patch_invalid", "Use a bounded text file patch and deletion list")
    paths = list(files) + list(deleted)
    if paths:
        normalize_scorer_boundary({"files": paths})
    if sum(len(s.encode()) for s in files.values()) > 2 * 1024 * 1024:
        raise UpstreamRefusal("upstream_patch_invalid", "Upstream text patches are bounded to 2 MiB")
    policy = SurfacePolicy(spec["edit_surface"], spec["protected_names"],
                           [e["name"] for e in spec["editables"] if e["name"] not in ("", ".")])
    for name in paths:
        reason = policy.check(name)
        if reason:
            raise UpstreamRefusal("upstream_patch_forbidden", f"{name}: {reason}")
    return files


def write_overlay(work, files, deleted=()):
    for name in deleted:
        p = contained_member(work, name)
        if p is None:
            raise UpstreamRefusal("upstream_patch_invalid", "Unsafe deletion path")
        if p.exists():
            p.unlink()
    for name, text in files.items():
        p = contained_member(work, name)
        if p is None:
            raise UpstreamRefusal("upstream_patch_invalid", "Unsafe write path")
        executable = p.stat().st_mode & 0o111 if p.exists() else 0
        atomic_write_bytes(p, text.encode(), mode=0o600 | executable)


def git_at(root, *argv):
    from looplab.runtime.sandbox import git_subprocess_env
    result = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false",
                             "-C", str(root), *argv], env=git_subprocess_env(),
                            capture_output=True, timeout=30)
    if result.returncode:
        raise UpstreamRefusal("upstream_git_unavailable", "Maintainer git operation failed; inspect run-owned worktree")
    return result.stdout.decode("utf8").strip()


def maintainer_worktree(rd, spec, selector, proposal_id):
    root = owned_path(rd, "upstream/git")
    work = owned_path(rd, "upstream/proposals/" + proposal_id + "/worktree")
    base_ref = "refs/looplab/base/" + selector["digest"]
    if not root.exists():
        root.mkdir(parents=True)
        seed_pinned_workspace(selector, spec["editables"], root)
        git_at(root, "init", "--template=")
        git_at(root, "config", "user.name", "LoopLab Maintainer")
        git_at(root, "config", "user.email", "maintainer@looplab.invalid")
        hooks = root.parent / "empty-hooks"; hooks.mkdir()
        git_at(root, "config", "core.hooksPath", str(hooks))
        git_at(root, "add", "-A")
        git_at(root, "commit", "--allow-empty", "-m", "Recorded seed " + selector["digest"])
        git_at(root, "update-ref", base_ref, "HEAD")
    work.parent.mkdir(parents=True, exist_ok=False)
    git_at(root, "worktree", "add", "-b", "proposal/" + proposal_id, str(work), base_ref)
    return work


def snapshot_worktree(work, destination):
    """Copy same-read regular bytes, excluding only git's own worktree marker."""
    from looplab.engine.workspace_seed import seeded_base_revision
    destination = Path(destination); destination.mkdir()
    def add(name, data, executable):
        if name == ".git" or name.startswith(".git/"):
            return
        target = contained_member(destination, name)
        if target is None:
            raise UpstreamRefusal("upstream_patch_invalid", "Unsafe candidate member")
        atomic_write_bytes(target, data, mode=0o600 | executable)
    observed = seeded_base_revision(work, on_file=add)
    if not observed["complete"]:
        raise UpstreamRefusal("upstream_candidate_unavailable", "Maintainer tree is incomplete or exceeds archive bounds")
    return destination


def merge_text(old, new, overlay):
    if overlay == old or overlay == new:
        return new
    if new == old:
        return overlay
    if None in (old, new, overlay):
        return None  # add/delete conflict; never guess
    with tempfile.TemporaryDirectory(prefix="looplab-upstream-merge-") as td:
        paths = [Path(td) / str(i) for i in range(3)]
        for p, body in zip(paths, (overlay, old, new)):
            p.write_bytes(body)
        result = subprocess.run(["git", "merge-file", "-p", *(str(p) for p in paths)],
                                capture_output=True, timeout=10)
        return result.stdout if result.returncode == 0 else None


def rebase_overlay(files, deleted, old_archive, new_archive):
    out, removed, conflicts = {}, [], []
    for name in sorted(set(files) | set(deleted)):
        old = read_bounded_regular_file(old_archive / name, 2 * 1024 * 1024 + 1)
        new = read_bounded_regular_file(new_archive / name, 2 * 1024 * 1024 + 1)
        overlay = files[name].encode() if name in files else None
        if any(b is not None and len(b) > 2 * 1024 * 1024 for b in (old, new)):
            conflicts.append(name); continue
        value = merge_text(old, new, overlay)
        if value is None and not (overlay is None and (old == new or new is None)):
            conflicts.append(name); continue
        if value is None:
            removed.append(name)
        elif value != new:
            try:
                out[name] = value.decode("utf8")
            except UnicodeError:
                conflicts.append(name)
    return out, removed, conflicts


def materialization_plan(spec, node, events):
    """Pin once per fresh lifecycle. Repairs in an existing workdir never call this."""
    current = active_base(events, spec["seed_base"])
    created = next((e for e in reversed(events) if e.type == "node_created" and e.data.get("node_id") == node.id), None)
    origin = active_base([e for e in events if created is None or e.seq <= created.seq], spec["seed_base"])
    for e in events:
        if e.data.get("node_id") != node.id or (created is not None and e.seq < created.seq):
            continue
        if e.type == "workspace_seeded" and (e.data.get("base_revision") or {}).get("selection"):
            origin["selector"] = {k: e.data["base_revision"]["selection"][k] for k in ("run_dir", "event_seq", "digest")}
        elif e.type == "node_overlay_rebased":
            origin["selector"] = e.data["selector"]
    old, _ = selected_seed_base(origin["selector"])
    new, _ = selected_seed_base(current["selector"])
    overlay, removed = dict(node.files), list(node.deleted)
    absorbed = []
    for advancement in (e for e in events if e.type == "base_advanced"):
        proposal = next((e for e in events if e.type == "upstream_proposed" and e.data.get("proposal_id") == advancement.data.get("proposal_id")), None)
        if proposal is None:
            raise UpstreamRefusal("upstream_source_unavailable", "Advanced base has no recorded source proposal")
        source = fold([e for e in events if e.seq <= proposal.seq]).nodes.get(proposal.data["source_node_id"])
        if source is None:
            raise UpstreamRefusal("upstream_source_unavailable", "Advanced capability source lifecycle is unavailable")
        recipe = proposal.data["source_recipe"]
        # Only exact source implementations replaced by the validated generalization
        # may vanish. Scientific edits with different bytes still need three-way merge.
        local_absorbed = []
        for name, text in source.files.items():
            if name not in proposal.data["capability_paths"]:
                continue
            if name not in recipe["files"] and name in overlay and overlay[name] == text:
                overlay.pop(name)
                absorbed.append(name)
                local_absorbed.append(name)
        for name in list(removed):
            if name in proposal.data["capability_paths"] and name in source.deleted and name not in recipe["deleted"]:
                removed.remove(name)
                absorbed.append(name)
                local_absorbed.append(name)
        if local_absorbed:
            for name, text in recipe["files"].items():
                if name not in overlay and name not in removed:
                    overlay[name] = text
            removed = sorted(set(removed) | set(recipe["deleted"]))
    files, deleted, conflicts = rebase_overlay(overlay, removed, old, new)
    chosen = origin["selector"] if conflicts else current["selector"]
    effective = {**spec, "effective_seed_base": chosen,
                 "editables": pinned_editables(spec["editables"], chosen)}
    copied = node.model_copy(update={"files": node.files if conflicts else files,
                                     "deleted": node.deleted if conflicts else deleted})
    return effective, copied, {"status": "conflict" if conflicts else "rebased" if old != new else "unchanged",
        "from_digest": origin["selector"]["digest"], "to_digest": current["selector"]["digest"],
        "conflicts": conflicts, "absorbed_paths": sorted(set(absorbed)), "advance_seq": current["advance_seq"]}
