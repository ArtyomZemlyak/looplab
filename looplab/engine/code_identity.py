"""Per-attempt code identity: every evaluated lifecycle as a reviewable git commit (doc 79).

WHAT A COMMIT IS. The run's code repository (`engine/code_repo.py`, `RUN_DIR/code/repo.git`) holds
two kinds of commit:

  * a BASE commit per distinct copied seed (`workspace_seeded.base_revision.digest`): its tree is
    EXACTLY the immutable archive in `base_snapshots/<digest>` — the operator's working tree as the
    seed copied it, uncommitted edits and untracked protected scorers included (doc 37 §5d is why it
    cannot be the source repo's HEAD). Bases form a lineage: the run's first base is parented on the
    commit the operator's editable stood at when the run started (since version 2, 2026-10-16, when
    that commit's objects can be fetched read-only — `engine/code_origin.py`; else, and in every
    version-1 run, it is a root; once decided, a rebuild names the SAME parent even when its objects
    are gone, as its own shallow cut), so `git log` shows the operator's history and `git diff <that
    commit> <base>` is exactly the uncommitted edits; a base produced by the upstream lane
    (`base_advanced`) has the base it replaced as parent; a new digest the live source produced
    mid-run (an operator edit) has the previously seen base as parent.
  * an ATTEMPT commit per materialized node lifecycle, FIRST-parented on the base it was seeded on.
    Its diff against that base is the node's whole code change — `git show <attempt>` is the review.
    A lifecycle whose code changed during its evaluation (an in-place repair) gets a second commit,
    parented on the first, at the terminal. MERGE PARENTS ARE HONEST (version 2): an ensemble or a
    merge operator names its other parents in a `Looplab-Co-Parents` trailer and keeps ONE git
    parent — its overlay is a new change authored over the base, and a synthesized git merge would
    tell `git merge` that the other branch is merged when its change may have been dropped. Only a
    REAL merge gets further git parents: an injected node whose worktree history CONTAINS other
    node commits (`merged_node_commits`, each verified an ancestor of the worktree's head).

WHERE IT IS RECORDED. The engine appends the diagnostic `code_snapshot` row (fold-ignored, so it moves
no replay and no fence) with the commit, its base and the changed paths, bound to `(node_id,
generation, seed_event_seq)` — the same keys as the attempt's evaluation rows. Its absence is honest:
older runs carry none, and `sync_run` builds the same commits offline from the log (`source: "log"`).

Every failure here is a RECORD (`status: "unavailable"` with a reason), never a refusal: code
identity may not cost a node its evaluation.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Iterable, Optional

from looplab.engine.code_repo import (IDENTITY_VERSION, CodeRepo, CodeRepoError, REPO_REL,
                                      is_object_id, safe_path)

# 2 (2026-10-16): the first base is parented on the operator's source commit, attempts carry the
# co-parents trailer and real merge parents. A run builds at the version of its FIRST row, else of
# its repository's stamp (`code_origin.py::run_version`), and every new row of the run is stamped
# with THAT version — so a version-1 run, resumed or synced, reproduces its commits byte for byte.
SNAPSHOT_VERSION = IDENTITY_VERSION
# A real merge brings at most this many node commits in as git parents (an octopus beyond it says
# nothing a reviewer can use; the trailer still names every co-parent).
MAX_MERGE_PARENTS = 16
MAX_LISTED_PATHS = 64
MAX_OVERLAY_FILE_BYTES = 16 * 1024 * 1024
MAX_OVERLAY_TOTAL_BYTES = 64 * 1024 * 1024
BASE_REF = "refs/looplab/bases/"
ATTEMPT_REF = "refs/looplab/attempts/"
BASE_BRANCH = "refs/heads/base"
CHAMPION_BRANCH = "refs/heads/champion"


def node_branch(node_id: int) -> str:
    return f"refs/heads/nodes/n{int(node_id)}"


# The in-place repair commit's ref is a SIBLING of the lifecycle's first commit, never a child: the
# ref store holds `.../s5` as a FILE, so `.../s5/repair` would need it to be a directory and every
# repair commit failed with "cannot lock ref" (review 2026-10-15). `-` is legal in a ref component.
REPAIR_SUFFIX = "-repair"


def attempt_ref(node_id: int, generation: int, seed_seq, suffix: str = "") -> str:
    return f"{ATTEMPT_REF}n{int(node_id)}/g{int(generation)}/s{seed_seq}{suffix}"


# --------------------------------------------------------------------------------- the base lineage

def _digest(receipt) -> Optional[str]:
    if not isinstance(receipt, dict) or receipt.get("complete") is not True:
        return None
    digest = receipt.get("digest")
    return digest if isinstance(digest, str) and len(digest) == 64 else None


def _etype(e):
    return getattr(e, "type", None)


def _edata(e) -> dict:
    data = getattr(e, "data", None)
    return data if isinstance(data, dict) else {}


def base_lineage(events) -> dict:
    """`{digest: {parent, reason, seq, ts, advance?}}` in first-seen order.

    `reason` is `initial` (the run's first base), `upstream` (a `base_advanced` selector — `advance`
    carries the proposal, source node, summary, flag and gate row), or `source_changed` (a digest the
    seed produced later that no advance explains: the live source moved under the run)."""
    lineage: dict = {}
    active: Optional[str] = None
    for e in events or ():
        kind, data = _etype(e), _edata(e)
        digest, advance = None, None
        if kind == "run_started" and isinstance(data.get("seed_base"), dict):
            digest = data["seed_base"].get("digest")
        elif kind == "workspace_seeded" and type(data.get("node_id")) is int:
            # A NODE's seed only: the upstream lane also records a proposal's candidate base as a
            # `workspace_seeded` row with no node, and a proposal is not a base until it advances.
            digest = _digest(data.get("base_revision"))
        elif kind == "base_advanced":
            selector = data.get("selector")
            digest = selector.get("digest") if isinstance(selector, dict) else None
            advance = {k: data.get(k) for k in ("proposal_id", "source_node_id", "summary", "flag",
                                                "gate_seq", "evidence_token")
                       if data.get(k) is not None}
        if not isinstance(digest, str) or len(digest) != 64:
            continue
        if digest in lineage:
            if advance is not None:
                if lineage[digest]["reason"] != "initial":
                    lineage[digest].update(reason="upstream", advance=advance)
                active = digest
            continue
        if advance is not None:
            reason = "upstream"
        else:
            reason = "initial" if active is None else "source_changed"
        lineage[digest] = {"parent": active, "reason": reason, "seq": getattr(e, "seq", None),
                           "ts": getattr(e, "ts", None)}
        if advance is not None:
            lineage[digest]["advance"] = advance
        active = digest
    return lineage


def active_base_digest(events, lineage=None) -> Optional[str]:
    """The base the NEXT lifecycle starts from: the last `base_advanced` selector, else the last base
    first seen in the log."""
    lineage = lineage if lineage is not None else base_lineage(events)
    last = None
    for e in events or ():
        if _etype(e) == "base_advanced":
            sel = _edata(e).get("selector")
            if isinstance(sel, dict) and sel.get("digest") in lineage:
                last = sel["digest"]
    if last is not None:
        return last
    return next(reversed(lineage), None) if lineage else None


def _selector_for(events, digest):
    for e in events or ():
        data = _edata(e)
        for sel in (data.get("seed_base"), data.get("selector")):
            if isinstance(sel, dict) and sel.get("digest") == digest and sel.get("run_dir"):
                return sel
    return None


def _receipt_for(events, digest):
    for e in events or ():
        if _etype(e) == "workspace_seeded":
            receipt = _edata(e).get("base_revision")
            if _digest(receipt) == digest:
                return receipt
    return None


def archive_files(run_dir, events, digest) -> Optional[list]:
    """The archived bytes of base `digest` as (path, bytes, executable) rows, verified against the
    digest by the seed's own walk — this run's `base_snapshots/`, else the selected origin run's."""
    from looplab.engine.workspace_seed import seeded_base_revision
    candidates = [Path(run_dir) / "base_snapshots" / digest]
    selector = _selector_for(events, digest)
    if selector is not None:
        candidates.append(Path(selector["run_dir"]) / "base_snapshots" / digest)
    for path in candidates:
        try:
            if not path.is_dir() or path.is_symlink():
                continue
        except OSError:
            continue
        rows: list = []
        observed = seeded_base_revision(path, on_file=lambda n, d, x: rows.append((n, d, bool(x))))
        if observed.get("complete") and observed.get("digest") == digest:
            return rows
    return None


def _base_message(run_id: str, digest: str, info: dict, run_started: dict,
                  origin_parent: Optional[str] = None) -> str:
    # `origin_parent` adds ONE sentence and ONE trailer, and only when the base is parented on the
    # operator's commit: a root base's message stays byte-identical to version 1's.
    reason = info.get("reason")
    if reason == "upstream":
        adv = info.get("advance") or {}
        subject = (f"base {digest[:12]}: upstream from node {adv.get('source_node_id')} — "
                   f"{_one_line(adv.get('summary'))}")
    elif reason == "source_changed":
        subject = f"base {digest[:12]}: the live source changed during the run"
    else:
        subject = f"base {digest[:12]}: the code run {run_id} started from"
    lines = [subject[:200], ""]
    if reason == "initial":
        lines.append("The seeded working tree as LoopLab copied it, uncommitted edits included.")
        workspace = run_started.get("workspace")
        if isinstance(workspace, dict) and workspace:
            for label, value in sorted(workspace.items()):
                lines.append(f"Source {label}: {_one_line(value)}")
        dirty = run_started.get("dirty_inputs")
        if isinstance(dirty, list) and dirty:
            lines.append(f"Uncommitted inputs at run start: {len(dirty)}")
        if origin_parent:
            lines.append("Parent: the source commit the run started from; the diff from it is "
                         "what was never committed.")
        lines.append("")
    trailers = [("Looplab-Run", run_id), ("Looplab-Base-Digest", digest),
                ("Looplab-Base-Reason", reason or "initial")]
    if origin_parent:
        trailers.append(("Looplab-Source-Commit", origin_parent))
    adv = info.get("advance") or {}
    for key, name in (("proposal_id", "Looplab-Upstream-Proposal"),
                      ("source_node_id", "Looplab-Upstream-Source-Node"),
                      ("gate_seq", "Looplab-Upstream-Gate-Seq"),
                      ("evidence_token", "Looplab-Upstream-Evidence")):
        if adv.get(key) is not None:
            trailers.append((name, adv[key]))
    if isinstance(adv.get("flag"), dict):
        trailers.append(("Looplab-Upstream-Flag", json.dumps(adv["flag"], sort_keys=True)))
    if info.get("seq") is not None:
        trailers.append(("Looplab-Event-Seq", info["seq"]))
    lines += [f"{k}: {_one_line(v)}" for k, v in trailers]
    return "\n".join(lines) + "\n"


def _one_line(value, cap: int = 300) -> str:
    text = " ".join(str("" if value is None else value).split())
    return "".join(ch for ch in text if ch.isprintable())[:cap]


def _run_meta(events) -> tuple:
    for e in events or ():
        if _etype(e) == "run_started":
            data = _edata(e)
            return str(data.get("run_id") or "run"), data
    return "run", {}


def _is_first_base(info: dict) -> bool:
    # The run's FIRST base. An upstream-advanced base is never this one: its tree is the lane's.
    return not info.get("parent") and info.get("reason", "initial") == "initial"


def prepare_first_base(repo: CodeRepo, run_dir, events, digest: Optional[str], lineage: dict,
                       origin_source: Optional[str] = None) -> Optional[tuple]:
    """The first base's parent decision (`code_origin.py::first_base_parent`) when the lineage
    root of `digest` is the run's first base and is not built yet, else None. Called BEFORE
    `repo.lock()`: it may probe the operator's source and fetch from it (bounded, read-only), and
    holding the lock across that stalled every attempt record, worktree and sync of the run for up
    to two minutes on the first node's eval path (review 2026-10-16)."""
    hops = 0
    while digest and (lineage.get(digest) or {}).get("parent") and hops < 256:
        digest, hops = lineage[digest]["parent"], hops + 1
    if not digest or not _is_first_base(lineage.get(digest, {})) \
            or repo.rev(BASE_REF + digest) is not None:
        return None
    from looplab.engine.code_origin import first_base_parent
    return first_base_parent(repo, run_dir, events, origin_source)


def ensure_base(repo: CodeRepo, run_dir, events, digest: str, lineage=None, _depth: int = 0,
                origin: Optional[dict] = None, origin_source: Optional[str] = None,
                origin_decision: Optional[tuple] = None):
    """The base commit of `digest`, creating it (and its lineage parents) when missing. None when
    its archive cannot be read. `origin`, when given, receives the diagnostic of the FIRST base's
    parent decision (`code_origin.py::first_base_parent`) if this call created that base;
    `origin_decision` is that decision made before the caller locked (`prepare_first_base`)."""
    ref = BASE_REF + digest
    existing = repo.rev(ref)
    if existing is not None:
        return existing
    lineage = lineage if lineage is not None else base_lineage(events)
    info = lineage.get(digest, {"parent": None, "reason": "initial"})
    parent_sha, origin_parent, diag = None, None, {}
    if info.get("parent") and _depth < 64:
        parent_sha = ensure_base(repo, run_dir, events, info["parent"], lineage, _depth + 1,
                                 origin, origin_source, origin_decision)
    files = archive_files(run_dir, events, digest)
    if files is None:
        return None
    first = _is_first_base(info)
    if first:
        # Parented on the operator's source commit when the log (or, before any row, this run's
        # own decision record, then the source) says so — read-only on the operator side.
        if origin_decision is None:
            from looplab.engine.code_origin import first_base_parent
            origin_decision = first_base_parent(repo, run_dir, events, origin_source)
        origin_parent, diag = origin_decision[0], dict(origin_decision[1])
        parent_sha = origin_parent
        if origin is not None:
            origin.update(diag)
    run_id, started = _run_meta(events)
    when = int(info.get("ts") or 0)
    sha = repo.write_tree_commit(ref, files, message=_base_message(run_id, digest, info, started,
                                                                   origin_parent),
                                 when=when, parent=parent_sha)
    if first:
        from looplab.engine.code_origin import write_decision, write_sidecar
        # Durable beside the run (`code/origin/`, mirrored): a rebuild with no recorded row reads
        # this decision before it would probe a source that may be gone (`decided_parent`).
        write_decision(run_dir, origin_parent)
        if origin_parent and diag.get("via") == "cut":
            # The decided parent's objects are gone: the same id, walkable as its own cut.
            repo.add_shallow([sha])
        elif origin_parent:
            # The parent's objects ride beside the run, so a rebuild with the operator's
            # repository gone still writes this same commit id.
            sidecar = write_sidecar(repo, run_dir, origin_parent, sha)
            if origin is not None:
                origin["sidecar"] = sidecar
    return sha


def origin_parent_of(repo: CodeRepo, digest: Optional[str], lineage: dict) -> Optional[str]:
    """The operator commit the lineage ROOT of `digest` is parented on, read off the repository
    (None for a root base, a missing one, or a v1 run)."""
    hops = 0
    while digest and (lineage.get(digest) or {}).get("parent") and hops < 256:
        digest, hops = lineage[digest]["parent"], hops + 1
    sha = repo.rev(BASE_REF + digest) if digest else None
    parents = repo.commit_parents(sha) if sha else []
    return parents[0] if parents else None


def refresh_base_branch(repo: CodeRepo, events, lineage=None) -> Optional[str]:
    digest = active_base_digest(events, lineage)
    sha = repo.rev(BASE_REF + digest) if digest else None
    if sha is not None and repo.rev(BASE_BRANCH) != sha:
        repo.update_ref(BASE_BRANCH, sha)
    return sha


# ------------------------------------------------------------------------------ the attempt overlay

def changes_from_workdir(workdir, files: Iterable, deleted: Iterable = (),
                         extra: Iterable = ()) -> dict:
    """What the materialized workdir holds at the overlay's paths: `{path: (bytes, executable) |
    None}` — read off disk, so a protected name the materializer skipped reads as the base's bytes
    and an asset that won a collision reads as the asset. Links and non-files read as absent."""
    root = Path(workdir)
    out: dict = {}
    total = 0
    for name in sorted({*files, *deleted, *extra}):
        safe = safe_path(str(name))
        if safe is None:
            continue
        path = root / safe
        try:
            st = os.lstat(path)
        except OSError:
            out[safe] = None
            continue
        if not stat.S_ISREG(st.st_mode):
            out[safe] = None
            continue
        if st.st_size > MAX_OVERLAY_FILE_BYTES:
            continue  # bounded record: an oversized file keeps its base bytes in the commit
        total += st.st_size
        if total > MAX_OVERLAY_TOTAL_BYTES:
            break
        try:
            data = path.read_bytes()
        except OSError:
            continue
        out[safe] = (data, bool(st.st_mode & 0o111))
    return out


def changes_from_log(files: dict, deleted: Iterable = ()) -> dict:
    """The overlay as the log states it (offline sync): text written as UTF-8, deletions as None."""
    out: dict = {}
    for name, text in (files or {}).items():
        safe = safe_path(str(name))
        if safe is not None and isinstance(text, str):
            out[safe] = (text.encode("utf-8"), None)
    for name in deleted or ():
        safe = safe_path(str(name))
        if safe is not None and safe not in out:
            out[safe] = None
    return out


def _effective_changes(repo: CodeRepo, base_sha: str, changes: dict) -> dict:
    """Keep the base's mode where the overlay does not know it, and drop no-op rows."""
    entries = repo.entries(base_sha)
    out = {}
    for path, row in changes.items():
        if row is None:
            if path in entries:
                out[path] = None
            continue
        data, executable = row
        if executable is None:
            executable = entries.get(path, ("100644",))[0] == "100755"
        out[path] = (data, executable)
    return out


def _co_parents_trailer(co_parents) -> Optional[str]:
    """`n3=<commit>, n5` — each co-parent node, with the commit the log recorded for it when one
    exists; None when there is none (single-parent nodes keep their version-1 message)."""
    items = []
    for row in co_parents or ():
        if isinstance(row, dict) and type(row.get("node_id")) is int:
            commit = row.get("commit")
            items.append(f"n{row['node_id']}" + (f"={commit}" if isinstance(commit, str) else ""))
    return ", ".join(items) if items else None


def _attempt_message(run_id, node_id, generation, seed_seq, digest, idea, *, source, repair=False,
                     co_parents=None, merged=None):
    title = ""
    if isinstance(idea, dict):
        title = idea.get("title") or idea.get("hypothesis") or idea.get("rationale") or ""
        operator = idea.get("operator") or ""
    else:
        operator = ""
    head = f"node {node_id} g{generation}: " + ("in-place repair during evaluation" if repair
                                                 else (_one_line(title, 140) or operator or "attempt"))
    trailers = [("Looplab-Run", run_id), ("Looplab-Node", node_id), ("Looplab-Generation", generation),
                ("Looplab-Seed-Event", seed_seq), ("Looplab-Base-Digest", digest),
                ("Looplab-Snapshot-Source", source)]
    if operator:
        trailers.append(("Looplab-Operator", operator))
    co = _co_parents_trailer(co_parents)
    if co:
        trailers.append(("Looplab-Co-Parents", co))
    if merged:
        trailers.append(("Looplab-Merged-Nodes", ", ".join(
            f"n{m['node_id']}={m['commit']}" for m in merged)))
    return head[:200] + "\n\n" + "\n".join(f"{k}: {_one_line(v)}" for k, v in trailers) + "\n"


def _parse_attempt_ref(ref: str):
    """`(node_id, generation, seed_seq, repair)` of an attempt ref, else None."""
    m = re.match(r"^refs/looplab/attempts/n(\d+)/g(\d+)/s(\d+)(-repair)?$", ref)
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), bool(m.group(4))


def merged_node_commits(repo: CodeRepo, head: Optional[str]) -> list:
    """The node attempt commits a REAL merge brought into `head` — an agent worktree's committed
    history (the agent ran `git merge nodes/n<id>`, or started from `node:<id>`) — as
    `[{node_id, generation, commit}]`: every attempt commit reachable from `head` (`for-each-ref
    --merged`), reduced to the independent tips (a repair commit stands for the attempt it sits
    on), ordered by (node, generation, seed), at most `MAX_MERGE_PARENTS`. [] when `head` is not in
    the repository. A claim git can verify, never one taken from the submitter: `head` itself is
    server-derived (`inject_node.code_commit`)."""
    if not isinstance(head, str) or repo.rev(head) is None:
        return []
    # ONE call answers "which attempt commits does `head` contain": `--merged` keeps exactly the
    # refs whose commit is an ancestor of `head` (it used to be a 200k-commit `rev-list` of the
    # whole history intersected in Python, then an `--is-ancestor` per tip that could not fail).
    attempts = {}
    out = repo.git("for-each-ref", f"--merged={head}", "--format=%(objectname) %(refname)",
                   ATTEMPT_REF).stdout.decode("utf-8", "replace")
    for line in out.splitlines():
        sha, _, ref = line.partition(" ")
        parsed = _parse_attempt_ref(ref)
        if parsed is not None:
            attempts.setdefault(sha, parsed)
    if not attempts:
        return []
    # The independent tips WITHOUT an argv of every id (`merge-base --independent <all>` met the
    # 32 767-character Windows command line at ~780 attempts; review 2026-10-16): an attempt is NOT
    # a tip when another one reaches it, i.e. when it is reachable from some attempt's PARENTS. One
    # `rev-list --stdin`, stopped at the bases — an attempt never sits below a base, so the walk is
    # the attempt layer only, never the operator's history.
    feed = "".join(f"{sha}^@\n" for sha in sorted(attempts))
    feed += "".join(f"^{sha}\n" for sha in sorted(set(repo.refs(BASE_REF).values())))
    reached = set(repo.git("rev-list", "--stdin", input=feed.encode("ascii")).stdout.decode(
        "ascii", "replace").split())
    verified = sorted((t for t in attempts if t not in reached), key=lambda t: attempts[t])
    return [{"node_id": attempts[t][0], "generation": attempts[t][1], "commit": t}
            for t in verified[:MAX_MERGE_PARENTS]]


def co_parent_refs(events, parent_ids, before_seq) -> list:
    """`[{node_id, commit?}]` for an ensemble's OTHER parents (`parent_ids[1:]`, the Developer's
    `co_parents`): the commit is the last recorded `code_snapshot` of that node BEFORE `before_seq`,
    so the same log always names the same commits."""
    others = [p for p in list(parent_ids or ())[1:] if type(p) is int]
    if not others:
        return []
    last: dict = {}
    for e in events or ():
        seq = getattr(e, "seq", None)
        if type(before_seq) is int and type(seq) is int and seq >= before_seq:
            break
        if _etype(e) == "code_snapshot":
            d = _edata(e)
            if d.get("status") == "recorded" and d.get("node_id") in others and d.get("commit"):
                last[d["node_id"]] = d["commit"]
    return [{"node_id": p, **({"commit": last[p]} if p in last else {})} for p in others]


def _same_overlay(created: dict, files, deleted) -> bool:
    return (files is not None and dict(files or {}) == dict(created.get("files") or {})
            and sorted(deleted or ()) == sorted(created.get("deleted") or ()))


def injected_code_commit(events, node_id, generation, files=None, deleted=None) -> Optional[str]:
    """The agent worktree head an injected node's code came from (`node_created.code_commit`,
    stamped at intake from `code_from: {"worktree": …}`), for THAT lifecycle.

    A lifecycle with its own `node_created` reads that row. One opened WITHOUT a `node_created` — an
    eval `node_reset` or a holdout requeue moves the generation and keeps the code — reads the latest
    EARLIER `node_created` of the node, but only when the lifecycle's overlay (`files`, `deleted`)
    is still exactly that row's: the code the head describes, decided on content, so a reset that
    re-developed the code or a rebased overlay claims nothing (review 2026-10-16: lifecycle >= 1 of
    a merged node lost its merge parents with its code unchanged)."""
    own = earlier = None
    for e in events or ():
        if _etype(e) != "node_created":
            continue
        d = _edata(e)
        if d.get("node_id") != node_id:
            continue
        gen = d.get("generation") if type(d.get("generation")) is int else 0
        if gen == generation:
            own = d
        elif gen < generation:
            earlier = d
    row = own if own is not None else (
        earlier if earlier is not None and _same_overlay(earlier, files, deleted) else None)
    sha = row.get("code_commit") if row is not None else None
    return sha if is_object_id(sha) else None


def _payload_base(node_id, generation, seed_seq, version: int = SNAPSHOT_VERSION) -> dict:
    # `version` is the RUN's (`code_origin.py::run_version`): a resumed version-1 run's new rows
    # say 1, never this build's version (review 2026-10-16).
    return {"version": version, "node_id": node_id, "generation": generation,
            "seed_event_seq": seed_seq, "repo": REPO_REL}


def record_attempt(run_dir, events, *, node_id: int, generation: int, seed_event_seq,
                   base_digest: Optional[str], changes: dict, source: str, idea=None,
                   when: float = 0.0, repair_of: Optional[str] = None, parent_ids=(),
                   code_commit: Optional[str] = None,
                   origin_source: Optional[str] = None) -> dict:
    """Write the attempt commit and return the `code_snapshot` payload (never raises).

    `repair_of` names the commit this one supersedes WITHIN the lifecycle (an in-place repair):
    the new commit is parented on it, so its diff is the repair alone. `parent_ids` are the node's
    DAG parents (the others become the `Looplab-Co-Parents` trailer) and `code_commit` the agent
    worktree head an injected node came from (its verified node ancestors become git parents) —
    both only for a version-2 run (`code_origin.py::run_version`). `origin_source` is the live
    engine's editable path, for the first base's parent (offline, the task snapshot names it)."""
    from looplab.engine.code_origin import run_version
    repo = CodeRepo(run_dir)
    try:
        version = run_version(events, SNAPSHOT_VERSION, repo)
    except CodeRepoError:
        version = run_version(events, SNAPSHOT_VERSION)
    payload = _payload_base(node_id, generation, seed_event_seq, version)
    payload["source"] = source
    if base_digest is None:
        return {**payload, "status": "unavailable", "reason": "no_seed_base"}
    current = version >= 2
    co_parents = co_parent_refs(events, parent_ids, seed_event_seq) if current else []
    try:
        repo.ensure()
        lineage = base_lineage(events)
        if current and code_commit and not repair_of and repo.rev(code_commit) is None:
            # The head a REAL merge left is recorded but not here (a rebuilt repository before the
            # agent backup is restored): a commit written now would lack the merge parents the head
            # names, and its ref would pin that. Left unbuilt and said, so a later sync — after the
            # head is back — builds it whole (review 2026-10-16).
            return {**payload, "status": "unavailable", "reason": "merge_head_missing",
                    "message": f"merge head {code_commit[:12]} is not in the run repository"}
        decision = prepare_first_base(repo, run_dir, events, base_digest, lineage, origin_source)
        with repo.lock():
            origin: dict = {}
            base_sha = ensure_base(repo, run_dir, events, base_digest, lineage, origin=origin,
                                   origin_source=origin_source, origin_decision=decision)
            if base_sha is None:
                return {**payload, "status": "unavailable", "reason": "base_archive_unavailable",
                        "base": {"digest": base_digest}}
            parent = repair_of or base_sha
            # Real merge parents on the lifecycle's FIRST commit only: a repair commit sits on it
            # and inherits them through it.
            merged = (merged_node_commits(repo, code_commit)
                      if current and code_commit and not repair_of else [])
            merged = [m for m in merged if m["commit"] != base_sha and m["node_id"] != node_id]
            ref = attempt_ref(node_id, generation, seed_event_seq,
                              REPAIR_SUFFIX if repair_of else "")
            run_id, _ = _run_meta(events)
            message = _attempt_message(run_id, node_id, generation, seed_event_seq, base_digest,
                                       idea, source=source, repair=repair_of is not None,
                                       co_parents=co_parents, merged=merged)
            sha = repo.write_overlay_commit(ref, parent, _effective_changes(repo, parent, changes),
                                            message=message, when=int(when or 0),
                                            merges=[m["commit"] for m in merged])
            repo.update_ref(node_branch(node_id), sha)
            refresh_base_branch(repo, events, lineage)
            changed = repo.changed(base_sha, sha)
            stats = repo.numstat(base_sha, sha)
            origin_parent = origin_parent_of(repo, base_digest, lineage)
    except CodeRepoError as exc:
        return {**payload, "status": "unavailable", "reason": exc.code, "message": str(exc)[:300]}
    except OSError as exc:
        return {**payload, "status": "unavailable", "reason": "storage_unavailable",
                "message": type(exc).__name__}
    base = {"digest": base_digest, "commit": base_sha}
    if origin_parent:
        base["origin_parent"] = origin_parent      # what a rebuild reads back (`decided_parent`)
    if origin:
        base["origin"] = origin                    # only on the row whose call built the first base
    out = {**payload, "status": "recorded", "commit": sha, "ref": ref, "base": base,
           "changed": changed[:MAX_LISTED_PATHS], "changed_count": len(changed), "stats": stats}
    if repair_of:
        out["repair_of"] = repair_of
    if co_parents:
        out["co_parents"] = co_parents
    if merged:
        out["merge_parents"] = merged
    return out


# --------------------------------------------------------------------------- the engine's two hooks

def _seed_event(events, seq):
    for e in events or ():
        if getattr(e, "seq", None) == seq and _etype(e) == "workspace_seeded":
            return e
    return None


def _append_snapshot(engine, payload: dict) -> None:
    """The ONE writer of `code_snapshot`, its keys spelled out for the payload contract."""
    from looplab.events.types import EV_CODE_SNAPSHOT
    row = {"version": payload["version"], "node_id": payload["node_id"],
           "generation": payload["generation"], "seed_event_seq": payload["seed_event_seq"],
           "repo": payload["repo"], "source": payload["source"], "status": payload["status"]}
    for key in ("commit", "ref", "base", "changed", "changed_count", "stats", "repair_of",
                "co_parents", "merge_parents", "reason", "message"):
        if payload.get(key) is not None:
            row[key] = payload[key]
    engine.store.append(EV_CODE_SNAPSHOT, row)


def record_materialized(engine, node, workdir, seed) -> Optional[dict]:
    """After `WorkspaceSeeder.materialize` built a node's OWN workdir: append `code_snapshot`.

    Only the node's own `nodes/node_<id>` lifecycle workdir is recorded — a canary scratch or a
    confirm seed is the same code evaluated again, not a new attempt."""
    if not isinstance(seed, dict) or node is None:
        return None
    from looplab.engine.attempt_logs import is_lifecycle_workdir
    from looplab.engine.code_origin import engine_source
    wd = Path(workdir)
    if not is_lifecycle_workdir(wd, node.id):
        return None
    try:
        events = engine.store.read_all()
        seq = seed.get("seed_event_seq")
        seeded = _seed_event(events, seq)
        assets = list(getattr(engine, "_assets", None) or {})
        changes = changes_from_workdir(wd, node.files or {}, node.deleted or (), assets)
        idea = node.idea.model_dump() if hasattr(node.idea, "model_dump") else None
        generation = int(getattr(node, "attempt", 0) or 0)
        payload = record_attempt(engine.run_dir, events, node_id=node.id, generation=generation,
                                 seed_event_seq=seq, base_digest=_digest(seed), changes=changes,
                                 source="workspace", idea=idea,
                                 when=getattr(seeded, "ts", 0.0) or 0.0,
                                 parent_ids=list(getattr(node, "parent_ids", None) or ()),
                                 code_commit=injected_code_commit(events, node.id, generation,
                                                                  node.files or {},
                                                                  node.deleted or ()),
                                 origin_source=engine_source(engine))
        _append_snapshot(engine, payload)
        return payload
    except Exception:  # noqa: BLE001 - code identity is a record and may never cost a node its eval
        return None


def record_after_terminal(engine, attempt) -> Optional[dict]:
    """After a terminal: a lifecycle whose overlay changed during evaluation (an in-place repair)
    gets a second commit parented on the materialized one."""
    node, workdir = getattr(attempt, "node", None), getattr(attempt, "workdir", None)
    seed = getattr(attempt, "base_revision", None)
    if node is None or workdir is None or not isinstance(seed, dict):
        return None
    try:
        events = engine.store.read_all()
        seq = seed.get("seed_event_seq")
        prior = None
        for e in events:
            d = _edata(e)
            if (_etype(e) == "code_snapshot" and d.get("node_id") == node.id
                    and d.get("seed_event_seq") == seq and d.get("status") == "recorded"):
                prior = d
        if prior is None:
            return None
        repo = CodeRepo(engine.run_dir)
        assets = list(getattr(engine, "_assets", None) or {})
        paths = {*(node.files or {}), *(node.deleted or ())}
        changes = changes_from_workdir(workdir, node.files or {}, node.deleted or (), assets)
        current = {p: r for p, r in changes.items()}
        head = prior.get("commit")
        entries = repo.entries(head) if head else {}
        import hashlib
        differs = False
        for path, row in current.items():
            if row is None:
                differs = differs or path in entries
                continue
            blob = hashlib.sha1(b"blob %d\x00" % len(row[0]) + row[0]).hexdigest()
            if entries.get(path, (None, None))[1] != blob:
                differs = True
        if not differs or not paths:
            return None
        idea = node.idea.model_dump() if hasattr(node.idea, "model_dump") else None
        import time
        payload = record_attempt(engine.run_dir, events, node_id=node.id,
                                 generation=int(getattr(node, "attempt", 0) or 0),
                                 seed_event_seq=seq, base_digest=_digest(seed), changes=current,
                                 source="workspace", idea=idea, when=time.time(), repair_of=head)
        _append_snapshot(engine, payload)
        return payload
    except Exception:  # noqa: BLE001 - code identity is a record and may never cost a node its terminal
        return None


# ------------------------------------------------------------------------------ offline projection

def _read_events(run_dir):
    from looplab.events.eventstore import EventStore
    return EventStore(Path(run_dir) / "events.jsonl").read_all()


def recorded_snapshots(events) -> dict:
    """`{(node_id, generation, seed_event_seq): payload}` of every recorded `code_snapshot` (the
    last row wins, so a repair commit supersedes its lifecycle's first one)."""
    out: dict = {}
    for e in events or ():
        if _etype(e) == "code_snapshot":
            d = _edata(e)
            key = (d.get("node_id"), d.get("generation"), d.get("seed_event_seq"))
            if d.get("status") == "recorded" or key not in out:
                out[key] = d
    return out


def lifecycle_seeds(events) -> list:
    """`[(event, node_id, generation, digest)]` for the `workspace_seeded` rows that built a node's
    OWN lifecycle workdir — the same population `record_materialized` records.

    The eval canary (`canary/node_N`), the confirm phase and the noise floor materialize the SAME
    node again with the same node_id, generation and base receipt (review 2026-10-15: `sync_run`
    turned each into a phantom attempt commit that moved `refs/heads/nodes/n<id>`, and `code_status`
    listed it as a second attempt). A row carrying `workdir` (written since then) answers by path; a
    legacy row cannot, so per lifecycle only the FIRST is kept — the node's own seed precedes every
    re-run of it (prepare_workdir runs before the canary, and confirm/noise follow the terminal). A
    legacy re-seed of the same lifecycle after a resume is dropped with them: same overlay, same
    lifecycle, and a record that cannot tell the two apart says less rather than something false."""
    out: list = []
    legacy_seen: set = set()
    for e in events or ():
        if _etype(e) != "workspace_seeded":
            continue
        d = _edata(e)
        nid = d.get("node_id")
        if type(nid) is not int:
            continue
        gen = d.get("generation") if type(d.get("generation")) is int else 0
        workdir = d.get("workdir")
        if isinstance(workdir, str):
            if workdir != f"nodes/node_{nid}":
                continue
        elif (nid, gen) in legacy_seen:
            continue
        else:
            legacy_seen.add((nid, gen))
        out.append((e, nid, gen, _digest(d.get("base_revision"))))
    return out


def sync_run(run_dir, events=None, *, restore_agents: bool = False) -> dict:
    """Build the commits the log implies but no `code_snapshot` recorded (old runs, a kill between
    the seed and its record). Writes ONLY the run's code repository; appends no event. With
    `restore_agents` (an explicit restore point: `looplab code-status --sync`), also the agent
    worktrees a mirror restore's `code/agent_backup/` carries
    (`code_worktree.py::restore_agent_worktrees`, reported as `agent_worktrees`)."""
    from looplab.engine.shared import engine_fold as fold
    from looplab.events.git_export import node_lifecycles
    from looplab.engine.code_origin import run_version
    events = events if events is not None else _read_events(run_dir)
    recorded = recorded_snapshots(events)
    repo = CodeRepo(run_dir)
    version = run_version(events, SNAPSHOT_VERSION, repo)
    state = fold(events)
    superseded, _ = node_lifecycles(events)
    rebased: dict = {}
    built: list = []
    lifecycles = {id(row[0]): row for row in lifecycle_seeds(events)}
    for e in events:
        kind, d = _etype(e), _edata(e)
        if kind == "node_overlay_rebased":
            rebased[(d.get("node_id"), d.get("generation"))] = d
        if kind != "workspace_seeded" or id(e) not in lifecycles:
            continue                       # a canary / confirm / noise re-run is not an attempt
        _, nid, gen, digest = lifecycles[id(e)]
        if digest is None:
            continue
        if (nid, gen, e.seq) in recorded:
            continue
        overlay = rebased.get((nid, gen))
        node = state.nodes.get(nid)
        if overlay is not None:
            files, deleted = overlay.get("files") or {}, overlay.get("deleted") or []
        elif node is not None and int(node.attempt) == gen:
            files, deleted = node.files or {}, node.deleted or []
        elif (nid, gen) in superseded:
            lc = superseded[(nid, gen)]
            files, deleted = lc.files, list(lc.deleted)
        else:
            built.append({**_payload_base(nid, gen, e.seq, version), "status": "unavailable",
                          "reason": "overlay_unknown"})
            continue
        idea = node.idea.model_dump() if node is not None and hasattr(node.idea, "model_dump") else None
        holder = (node if node is not None and int(node.attempt) == gen
                  else superseded.get((nid, gen)))
        built.append(record_attempt(run_dir, events, node_id=nid, generation=gen,
                                    seed_event_seq=e.seq, base_digest=digest,
                                    changes=changes_from_log(files, deleted), source="log",
                                    idea=idea, when=getattr(e, "ts", 0.0) or 0.0,
                                    parent_ids=list(getattr(holder, "parent_ids", None) or ()),
                                    code_commit=injected_code_commit(events, nid, gen, files,
                                                                     deleted)))
    lineage = base_lineage(events)
    if repo.exists() or (lineage and recorded):
        try:
            repo.ensure()
            decision = prepare_first_base(repo, run_dir, events, next(iter(lineage), None),
                                          lineage)
            with repo.lock():
                # EVERY base of the lineage, not only those an unrecorded attempt sits on: after a
                # mirror restore (the mirror skips `code/repo.git`) this is what rebuilds the bases
                # — the same ids, the first one's operator parent from its sidecar when the source
                # is gone (`code_origin.py`) — before the agent backup below is unbundled onto them.
                for digest in lineage:
                    ensure_base(repo, run_dir, events, digest, lineage, origin_decision=decision)
                refresh_base_branch(repo, events, lineage)
                _refresh_champion(repo, state, {**recorded, **{
                    (b["node_id"], b["generation"], b["seed_event_seq"]): b for b in built}})
        except (CodeRepoError, OSError):
            pass                         # a record, never a refusal: `code-status` says what is missing
    out = {"built": built, "recorded": len(recorded)}
    # The one thing the log cannot rebuild: an external agent's unsubmitted worktree. A run restored
    # from the run mirror carries it as `code/agent_backup/` (`code_worktree.py::agent_backup`);
    # re-created here, AFTER the bases above, so `code-status --sync` brings the branch, its history
    # and its uncommitted files back (weekly-worktree-restore-loss). ONLY when asked: every code
    # path syncs first — a "submits nothing" overlay preview, `add_worktree`, `code-export` — and
    # none of them may create worktrees or re-link (and reset) an operator's outside directory
    # (review 2026-10-16).
    if restore_agents:
        from looplab.engine.code_worktree import restore_agent_worktrees
        try:
            agents = restore_agent_worktrees(run_dir)
        except (CodeRepoError, OSError) as exc:  # a record, never a refusal; the backup stays
            agents = {"pending": True, "error": f"{type(exc).__name__}: {exc}"[:300]}
        if agents is not None:
            out["agent_worktrees"] = agents
    return out


def _latest_by_lifecycle(snapshots: dict) -> dict:
    out: dict = {}
    for (nid, gen, seq), payload in snapshots.items():
        if payload.get("status") != "recorded" or type(seq) is not int:
            continue
        key = (nid, gen)
        if key not in out or seq >= out[key][0]:
            out[key] = (seq, payload)
    return {k: v[1] for k, v in out.items()}


def _refresh_champion(repo: CodeRepo, state, snapshots: dict) -> Optional[str]:
    best = state.best() if hasattr(state, "best") else None
    if best is None:
        return None
    row = _latest_by_lifecycle(snapshots).get((best.id, int(best.attempt)))
    sha = row.get("commit") if row else None
    if sha and repo.rev(CHAMPION_BRANCH) != sha:
        repo.update_ref(CHAMPION_BRANCH, sha)
    return sha


# ---------------------------------------------------------------------------- the operator's view

def _gate_evidence(events) -> dict:
    """{proposal_id: compact last gate verdict} from `upstream_gate_finished` rows."""
    out: dict = {}
    for e in events or ():
        if _etype(e) != "upstream_gate_finished":
            continue
        d = _edata(e)
        result = d.get("result") if isinstance(d.get("result"), dict) else {}
        checks = []
        for c in result.get("checks") or []:
            if isinstance(c, dict):
                checks.append({k: c.get(k) for k in ("kind", "name", "passed", "delta",
                                                      "tolerance", "old_mean", "new_mean")
                               if c.get(k) is not None})
        out[d.get("proposal_id")] = {"seq": e.seq, "passed": result.get("passed") is True,
                                     "code": result.get("code"), "checks": checks[:20],
                                     "executions": len(result.get("executions") or []),
                                     "evidence_token": d.get("evidence_token")}
    return out


def _metric(node):
    if node is None:
        return None
    return node.task_metric if getattr(node, "task_metric", None) is not None else node.metric


def upstream_readiness(task, settings) -> dict:
    """What stands between this run and a WORKING upstream lane, obligation by obligation (doc 79 §9
    leftover, 2026-10-14). An external-agent run usually declares none of them (the live MiniOneRec
    runs declare no `upstream`, no `seed_base` and no `cmd.scorer_boundary`), and the lane then
    stayed `off` with one reason string. The lane is DELIBERATELY not given defaults: its gate runs
    operator-declared probes on the host and compares scorer bytes, and a default would be a gate
    that checks nothing. So this says exactly what to declare — and that, until then, the run's
    fixes still reach the task repository through `looplab code-export` (node and base commits,
    no gate)."""
    missing = []

    def need(what: str, how: str):
        missing.append({"what": what, "how": how})

    from looplab.adapters.repo_task import RepoTask

    is_repo = isinstance(task, RepoTask)
    declared = task.upstream if is_repo else None
    if not is_repo:
        need("repo_task", "the upstream lane serves repository tasks only")
    else:
        if task.seed_base is None:
            need("seed_base", "declare the immutable initial seed (`seed_base`) in the task "
                              "BEFORE the run starts; it is pinned at run start and cannot be "
                              "added to a running run")
        scorer = task.eval.scorer_boundary if task.eval is not None else None
        if scorer is None:
            need("eval.scorer_boundary", "declare the evaluator files (`eval.scorer_boundary`) the "
                                         "gate must find byte-identical on both sides")
        if declared is None:
            need("upstream", "declare `upstream: {tests, regressions, repair_probes, repeats}` — "
                             "the measured gate a fix must pass before it becomes the base")
    mode = getattr(settings, "upstream_mode", None) if settings is not None else None
    if mode in (None, "off"):
        need("Settings.upstream_mode", "launch with upstream_mode=propose (gate, operator "
                                       "advances) or auto")
    if settings is not None and getattr(settings, "trust_mode", "trusted_local") != "trusted_local":
        need("Settings.trust_mode", "the gate runs on the host: trust_mode=trusted_local")
    return {"ready": not missing, "missing": missing,
            "meanwhile": ("looplab code-export RUN --repo <task repo> delivers this run's base "
                          "edits and the champion's change as a new branch, without a gate"),
            "example": {"seed_base": {"run_dir": "<absolute run or bundle directory>",
                                      "event_seq": "<its workspace_seeded seq>",
                                      "digest": "<the seed archive's SHA-256>"},
                        "eval": {"scorer_boundary": {"files": ["score.py"]}},
                        "upstream": {"tests": [{"name": "unit", "command": ["python", "-m",
                                                                            "pytest", "-q"]}],
                                     "repeats": 3}}}


def code_status(run_dir, events=None, *, task=None, settings=None, nominations: bool = True) -> dict:
    """The operator's one read of a run's code: bases (with upstream evidence), attempts (commit,
    changed paths, metric, effective config), what is pending upstream, and the branches.

    READ-ONLY: it reads the event log and the repository's refs; `sync_run` is the writer."""
    from looplab.core.upstream_board import upstream_board
    from looplab.engine.shared import engine_fold as fold
    events = events if events is not None else _read_events(run_dir)
    state = fold(events)
    repo = CodeRepo(run_dir)
    refs = repo.refs() if repo.exists() else {}
    lineage = base_lineage(events)
    active = active_base_digest(events, lineage)
    bases = []
    for digest, info in lineage.items():
        row = {"digest": digest, "commit": refs.get(BASE_REF + digest), "parent": info.get("parent"),
               "reason": info.get("reason"), "seq": info.get("seq"), "active": digest == active}
        if info.get("advance"):
            row["upstream"] = info["advance"]
        if row["commit"] and _is_first_base(info):
            # The operator commit the first base names, and whether its history is HERE: a base
            # whose decided parent's objects could not be had is written naming it anyway (the
            # same id) as its own shallow cut — said, so `git log` ending at the base is explained.
            try:
                parents = repo.commit_parents(row["commit"])
                if parents:
                    row["origin_parent"] = parents[0]
                    if not repo.has_object(parents[0]):
                        row["history"] = "unavailable"
                        row["history_reason"] = "origin_objects_missing"
            except CodeRepoError:
                pass                     # a read for the operator's view, never a refusal
        bases.append(row)
    snapshots = recorded_snapshots(events)
    configs: dict = {}
    for e in events:
        if _etype(e) == "effective_config":
            d = _edata(e)
            configs[(d.get("node_id"), d.get("generation"))] = {
                k: d.get(k) for k in ("effective", "by_stage", "mismatches", "conflicts", "unknown",
                                      "checked", "declared", "sources_read")}
    seeded = [(nid, gen, e.seq, digest) for e, nid, gen, digest in lifecycle_seeds(events)]
    best = state.best()
    attempts = []
    for nid, gen, seq, digest in seeded:
        snap = snapshots.get((nid, gen, seq))
        node = state.nodes.get(nid)
        current = node is not None and int(node.attempt) == gen
        ref = attempt_ref(nid, gen, seq)
        row = {"node_id": nid, "generation": gen, "seed_event_seq": seq, "base_digest": digest,
               "current": current, "commit": refs.get(ref + REPAIR_SUFFIX) or refs.get(ref),
               "recorded": snap is not None and snap.get("status") == "recorded",
               "source": (snap or {}).get("source"),
               "status": (node.status.value if current and node is not None else "superseded"),
               "metric": _metric(node) if current else None,
               "champion": bool(best is not None and current and best.id == nid)}
        if snap is not None:
            for k in ("changed", "changed_count", "stats", "reason", "repair_of", "co_parents",
                      "merge_parents"):
                if snap.get(k) is not None:
                    row[k] = snap[k]
            if snap.get("status") != "recorded":
                row["unavailable"] = snap.get("reason")
        elif row["commit"] and len(attempts) < 400:
            try:                             # synced offline: no row recorded it, the repo knows
                parent = repo.rev(row["commit"] + "^")
                if parent:
                    changed = repo.changed(parent, row["commit"])
                    row["changed"], row["changed_count"] = changed[:MAX_LISTED_PATHS], len(changed)
            except CodeRepoError:
                pass
        cfg = configs.get((nid, gen))
        if cfg is not None:
            row["effective_config"] = cfg
        attempts.append(row)
    board = upstream_board(state)
    gates = _gate_evidence(events)
    for proposal in board.get("proposals") or []:
        src = state.nodes.get(proposal.get("source_node_id"))
        proposal["source_metric"] = _metric(src)
        if proposal.get("proposal_id") in gates:
            proposal["gate"] = gates[proposal["proposal_id"]]
    pending = {"mode": None, "lane_configured": False, "proposals": board.get("proposals") or [],
               "in_flight": board.get("in_flight") or [], "advanced": board.get("advanced") or [],
               "nominations": [], "unlaned_changes": []}
    if settings is not None:
        pending["mode"] = getattr(settings, "upstream_mode", None)
    try:
        upstream_spec = task.upstream if task is not None else None   # only RepoTask declares one
    except AttributeError:
        upstream_spec = None
    pending["lane_configured"] = bool(upstream_spec)
    if nominations and upstream_spec and task is not None:
        try:
            from looplab.engine.upstream_state import upstream_candidates
            page = upstream_candidates(Path(run_dir), task, events, state=state, limit=100)
            for r in page.get("rows") or []:
                if r.get("classification") == "already_promoted":
                    continue
                pending["nominations"].append({
                    "node_id": r.get("node_id"), "generation": r.get("generation"),
                    "path": r.get("path"), "classification": r.get("classification"),
                    "origin": r.get("origin"), "hunk_hash": r.get("hunk_hash"),
                    "metric": _metric(state.nodes.get(r.get("node_id"))),
                    "pending_trigger_nodes": r.get("pending_trigger_nodes") or []})
        except Exception as exc:  # noqa: BLE001 - advice: an unreadable source yields no rows, said
            pending["nominations_unavailable"] = type(exc).__name__
    if not upstream_spec:
        # NO LANE: say which measured attempts changed CODE (not only config/recipe files), so the
        # operator sees what would need the lane — or a manual export — to reach the base.
        from looplab.engine.activation import is_config_path
        for row in attempts:
            if not row.get("current") or row.get("status") != "evaluated":
                continue
            code = [c["path"] for c in row.get("changed") or [] if not is_config_path(c["path"])]
            if code:
                pending["unlaned_changes"].append({"node_id": row["node_id"],
                                                   "generation": row["generation"],
                                                   "commit": row.get("commit"),
                                                   "metric": row.get("metric"), "paths": code[:20]})
    pending["readiness"] = upstream_readiness(task, settings)
    branches = {name[len("refs/heads/"):]: sha for name, sha in refs.items()
                if name.startswith("refs/heads/")}
    missing = sum(1 for a in attempts if not a["recorded"] and a.get("commit") is None)
    from looplab.engine.code_origin import run_version
    return {"version": SNAPSHOT_VERSION, "run_version": run_version(events, SNAPSHOT_VERSION, repo),
            "repo": {"path": REPO_REL, "exists": repo.exists()},
            "active_base": active, "bases": bases, "attempts": attempts, "branches": branches,
            "upstream": pending, "unsynced_attempts": missing,
            "champion": None if best is None else {"node_id": best.id, "generation": best.attempt,
                                                   "metric": _metric(best)}}
