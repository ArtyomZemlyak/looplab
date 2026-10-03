"""Run-owned Maintainer worktrees and conservative whole-overlay three-way rebasing.

The owner checkout is never a write target. Immutable archive bytes, not git HEAD,
are authoritative. Git is a reviewable, reachable projection for this lane only.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

from looplab.core.atomicio import atomic_write_bytes, durable_no_replace_rename
from looplab.core.node_evidence import read_bounded_regular_file
from looplab.core.pathsafe import contained_member, is_reparse
from looplab.engine.seed_base import normalize_seed_base, pinned_editables, selected_seed_base, seed_pinned_workspace
from looplab.engine.upstream_state import UpstreamRefusal, active_base, node_signature, source_node
from looplab.events.replay import event_generation_binds
from looplab.engine.shared import engine_fold as fold


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
    # A private review projection cannot inherit filters/config that execute code
    # or change the archived bytes. Keep the credential scrubber and identities.
    env = {k: v for k, v in git_subprocess_env().items() if not k.startswith("GIT_CONFIG_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    # Run-owned proposal paths plus a full digest ref can exceed Windows MAX_PATH.
    # Enable long paths per invocation, without changing owner/global Git config.
    result = subprocess.run(["git", "-c", "core.longpaths=true",
                             "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false",
                             "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull,
                             "-c", "core.attributesFile=" + os.devnull,
                             "-C", str(root), *argv], env=env,
                            capture_output=True, timeout=30)
    if result.returncode:
        raise UpstreamRefusal("upstream_git_unavailable", "Maintainer git operation failed; inspect run-owned worktree")
    return result.stdout.decode("utf8").strip()


def raw_git_attributes(rd, relative="upstream/git"):
    """Highest-precedence attributes keep this Git projection byte preserving."""
    path = owned_path(rd, relative + "/.git/info/attributes")
    expected = b"* -text -filter -ident -working-tree-encoding\n"
    if path.exists():
        if read_bounded_regular_file(path, len(expected) + 1) != expected:
            raise UpstreamRefusal("upstream_git_unavailable", "Restore private Git byte-preserving attributes before authoring")
    else:
        atomic_write_bytes(path, expected, mode=0o600)


def maintainer_worktree(rd, spec, selector, proposal_id):
    root = owned_path(rd, "upstream/git")
    work = owned_path(rd, "upstream/proposals/" + proposal_id + "/worktree")
    base_ref = "refs/looplab/base/" + selector["digest"]
    if not root.exists():
        # Publish only a complete private repository. Process loss after mkdir,
        # init, add or commit must not make root.exists() certify readiness and
        # brick the next explicitly abandoned/new proposal. Keep interrupted
        # staging directories for inspection; a fresh action owns a fresh one.
        relative = "upstream/.git-init-" + proposal_id
        staging = owned_path(rd, relative)
        staging.mkdir(parents=True)
        seed_pinned_workspace(selector, spec["editables"], staging)
        if any(p.name.casefold() == ".git" for p in staging.rglob("*")):
            raise UpstreamRefusal("upstream_git_unavailable", "Recorded seed contains Git metadata; it cannot initialize a private repository")
        git_at(staging, "init", "--template=")
        raw_git_attributes(rd, relative)
        git_at(staging, "config", "user.name", "LoopLab Maintainer")
        git_at(staging, "config", "user.email", "maintainer@looplab.invalid")
        hooks = root.parent / "empty-hooks"; hooks.mkdir(exist_ok=True)
        git_at(staging, "config", "core.hooksPath", str(hooks))
        git_at(staging, "add", "-f", "-A")
        git_at(staging, "commit", "--allow-empty", "-m", "Recorded seed " + selector["digest"])
        git_at(staging, "update-ref", base_ref, "HEAD")
        durable_no_replace_rename(staging, root, label="upstream Git repository")
    raw_git_attributes(rd)
    work.parent.mkdir(parents=True, exist_ok=False)
    git_at(root, "worktree", "add", "-b", "proposal/" + proposal_id, str(work), base_ref)
    from looplab.engine.workspace_seed import seeded_base_revision
    _, receipt = selected_seed_base(selector)
    actual = seeded_base_revision(snapshot_worktree(work, work.parent / "base-check"))
    if not actual["complete"] or any(actual[k] != receipt[k]
        for k in ("version", "scope", "digest", "file_count", "bytes")):
        raise UpstreamRefusal("upstream_git_unavailable", "Maintainer worktree differs from its recorded base; inspect Git refs and restore the projection")
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


def verify_approved_candidate(selector, files, deleted, actual):
    """Bind the whole candidate to its approved request, without another full copy."""
    import hashlib
    from looplab.engine.workspace_seed import seeded_base_revision, seeded_base_digest
    archive, receipt = selected_seed_base(selector)
    members = {}
    def add(name, data, executable):
        members[name] = (executable, hashlib.sha256(data).hexdigest(), len(data))
    original = seeded_base_revision(archive, on_file=add)
    if not original["complete"] or original["digest"] != receipt["digest"]:
        raise UpstreamRefusal("upstream_source_unavailable", "Original archive changed while binding the approved request")
    for name in deleted:
        members.pop(name, None)
    for name, text in files.items():
        data = text.encode()
        members[name] = (members.get(name, (0,))[0], hashlib.sha256(data).hexdigest(), len(data))
    expected = seeded_base_digest([[name, row[0], row[1]] for name, row in members.items()])
    if (not actual["complete"] or actual["digest"] != expected
            or actual["file_count"] != len(members) or actual["bytes"] != sum(r[2] for r in members.values())):
        raise UpstreamRefusal("upstream_candidate_changed", "Candidate snapshot differs from the approved request; inspect the worktree and author a fresh proposal")


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
    """Migrate pending lifecycles; confirmations retain the measured implementation."""
    current = active_base(events, spec["seed_base"])
    if node.status.value == "evaluated":
        # Confirmation/noise probes repeat this terminal experiment, not
        # new candidates. A passing upstream gate covers its source recipe only;
        # it cannot certify arbitrary older scientific overlays on the new base.
        measured, receipt = source_node(events, node.id)
        if node_signature(measured) != node_signature(node):
            raise UpstreamRefusal("upstream_source_changed", "Terminal implementation changed; read its current lifecycle before repeating evaluation")
        selection = receipt.get("selection")
        if (not isinstance(selection, dict) or any(k not in selection for k in ("run_dir", "event_seq", "digest"))
                or selection["digest"] != receipt["digest"]):
            raise UpstreamRefusal("upstream_source_unavailable", "Terminal experiment has no matching recorded base selection; restore its primary evidence before repeating evaluation")
        try:
            selector = normalize_seed_base({k: selection[k] for k in ("run_dir", "event_seq", "digest")})
        except (ValueError, TypeError) as exc:
            raise UpstreamRefusal("upstream_source_unavailable", "Terminal base selection is invalid; restore its primary evidence before repeating evaluation") from exc
        effective = {**spec, "effective_seed_base": selector,
                     "editables": pinned_editables(spec["editables"], selector)}
        return effective, node.model_copy(), {"status": "unchanged", "from_digest": receipt["digest"],
            "to_digest": receipt["digest"], "conflicts": [], "absorbed_paths": [], "advance_seq": current["advance_seq"]}
    state = fold(events)
    current_node = state.nodes.get(node.id)
    if (current_node is None or current_node.attempt != node.attempt
            or current_node.status.value != "pending" or current_node.tombstoned
            or node.id in state.aborted_nodes or current_node.files != node.files
            or current_node.deleted != node.deleted or current_node.code != node.code
            or (node.creation_event_seq is not None and node.creation_event_seq != current_node.creation_event_seq)):
        raise UpstreamRefusal("upstream_source_changed", "Read the current pending lifecycle before materializing its overlay")
    created = next((e for e in events if e.seq == current_node.creation_event_seq and e.type == "node_created"), None)
    if created is None:
        raise UpstreamRefusal("upstream_source_unavailable", "Pending overlay has no replay-applied authoring event")
    origin = active_base([e for e in events if e.seq <= created.seq], spec["seed_base"])
    for index, e in enumerate(events):
        if e.data.get("node_id") != node.id or e.seq < created.seq:
            continue
        if e.type not in ("workspace_seeded", "node_overlay_rebased"):
            continue
        # A late old attempt or a terminal confirmation is diagnostic only. It
        # cannot redefine the basis of a pending scientific overlay after reset.
        prior_state = fold(events[:index])
        prior = prior_state.nodes.get(node.id)
        if (prior is None or prior.status.value != "pending" or prior.tombstoned
                or node.id in prior_state.aborted_nodes
                or not event_generation_binds(e.data, prior.attempt)):
            continue
        if e.type == "workspace_seeded" and (e.data.get("base_revision") or {}).get("selection"):
            origin["selector"] = {k: e.data["base_revision"]["selection"][k] for k in ("run_dir", "event_seq", "digest")}
        elif e.type == "node_overlay_rebased":
            applied = fold(events[:index + 1]).nodes[node.id]
            if applied.files == e.data.get("files") and applied.deleted == e.data.get("deleted"):
                origin["selector"] = e.data["selector"]
    old, _ = selected_seed_base(origin["selector"])
    new, _ = selected_seed_base(current["selector"])
    overlay, removed = dict(node.files), list(node.deleted)
    absorbed = []
    advancements = [e for e in events if e.type == "base_advanced"]
    # Absorption belongs to transitions AFTER this overlay's actual basis. A new
    # node or a post-migration repair can deliberately restore an older source;
    # replaying an already-applied promotion would silently erase that experiment.
    basis_seq = next((e.seq for e in reversed(advancements)
                      if e.data["selector"] == origin["selector"]), -1)
    for advancement in (e for e in advancements if e.seq > basis_seq):
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
