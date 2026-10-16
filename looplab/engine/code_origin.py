"""The operator's source HEAD as the PARENT of the run's first base commit (doc 79, 2026-10-16).

WHY. The run's first base commit (`code_identity.py::ensure_base`) used to be a ROOT commit: its tree
is the seed archive — the operator's working tree, uncommitted edits included — and nothing else. A
worktree of the private repository therefore showed no history: `git log` ended at "the code run X
started from", `git blame` blamed every line on LoopLab, and `git diff` could not say which bytes were
the operator's uncommitted edits. Parenting that commit on the commit the editable source stood at
when the run started (`run_started.workspace["editable:."]`, the value `_recorded_source_commit`
reads for the export) changes none of that tree — node materialization reads the seed ARCHIVE, never
a commit tree — and makes `git diff <source HEAD> <base>` exactly the edits the operator never
committed (doc 37 §5d stays solved: the tree is still the working tree, not HEAD).

READ-ONLY ON THE OPERATOR'S REPOSITORY. The parent's objects are FETCHED from it into the private
repository (`git fetch --depth=N <source> <sha>`: upload-pack on the operator's side, which reads;
measured byte-identical `.git` before and after, `rev-list --count --all` unchanged). Nothing is
ever written there — no ref, object, config or index.

WHY THE OBJECTS, NOT ONLY A `shallow` LINE (measured 2026-10-16, git 2.43). A base commit naming a
parent the repository does not hold is NOT made usable by listing that parent in `$GIT_DIR/shallow`:
`log`, `rev-list`, `fsck` and `bundle create` all fail with "Could not read <parent>". The parent
commit and its tree must be present, with the parent itself as the shallow cut when its own history
is not. So the parent is used only when its objects could be obtained, from (in order) the private
repository itself, the operator's repository (`HISTORY_DEPTH` commits, else depth 1) or this run's
SIDECAR PACK `code/origin/<sha>.pack` — the parent commit and those of its tree objects the seed tree
does not already hold (typically kilobytes), written once beside the run and carried by the run
mirror, so a restore rebuilds the SAME base id with the operator's repository gone.

DETERMINISM. Once a run has recorded a `code_snapshot`, the parent is DECIDED by the log: a
`version` 1 row (every run before this change) means a root base, a later row names its
`base.origin_parent` or none. Only a log with no recorded row (an older pre-doc-79 run, or a kill
before the first record) decides by probing the source, as the live engine does.

WHEN THE BASE STAYS A ROOT. A pinned `seed_base` (the archive is another run's), several editables or
one mounted under a name (the private repository's paths are not the source repository's), a
non-git editable, an editable that is a SUBDIRECTORY of its repository (its tree would carry a prefix
the attempt commits, overlays and export do not — see doc 79 §10), an unresolvable source commit and
every failure to obtain its objects. Each is a recorded reason (`base.origin` on the row), never a
refusal: code identity may not cost a node its evaluation.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from looplab.engine.code_repo import CodeRepo, CodeRepoError, run_git

ORIGIN_REL = "code/origin"
# The history fetched behind the parent: enough for `log`/`blame` over the recent past, bounded so
# a large monorepo is not cloned into every run. Not a Settings field: it moves no result.
HISTORY_DEPTH = 200
FETCH_TIMEOUT_S = 60.0
PROBE_TIMEOUT_S = 20.0
# The sidecar holds what the seed tree does not (committed versions of edited files, tracked files
# the seed left out); past this it is not written, and a restore without the source says so.
MAX_ORIGIN_PACK_BYTES = 64 * 1024 * 1024
FIRST_PARENTED_VERSION = 2
_SHA = re.compile(r"^[0-9a-f]{40}$")


def run_version(events, current: int) -> int:
    """The code-identity version this RUN's commits are built at: the `version` of its first
    recorded `code_snapshot` row, else `current` (nothing recorded yet, so nothing to reproduce)."""
    for e in events or ():
        if getattr(e, "type", None) != "code_snapshot":
            continue
        data = getattr(e, "data", None)
        if isinstance(data, dict) and data.get("status") == "recorded":
            version = data.get("version")
            return version if type(version) is int else 1
    return current


def decided_parent(events):
    """`(True, sha | None)` when the log already decided the first base's parent, `(False, None)`
    when no recorded row exists yet. A version-1 row decides a ROOT (historical bases reproduce)."""
    for e in events or ():
        if getattr(e, "type", None) != "code_snapshot":
            continue
        data = getattr(e, "data", None)
        if not isinstance(data, dict) or data.get("status") != "recorded":
            continue
        if not (type(data.get("version")) is int and data["version"] >= FIRST_PARENTED_VERSION):
            return True, None
        base = data.get("base") if isinstance(data.get("base"), dict) else {}
        sha = base.get("origin_parent")
        return True, (sha if isinstance(sha, str) and _SHA.match(sha) else None)
    return False, None


def engine_source(engine) -> Optional[str]:
    """The root editable's source path as a live engine holds it (`_repo_spec`); the offline
    reader is the task snapshot (`candidate`). None when there is not exactly one root editable."""
    spec = getattr(engine, "_repo_spec", None) or {}
    roots = [ed for ed in spec.get("editables") or () if isinstance(ed, dict)
             and str(ed.get("name") or ".") in (".", "")]
    if len(roots) != 1 or len(spec.get("editables") or ()) != 1:
        return None
    path = roots[0].get("origin_path") or roots[0].get("path")
    return str(path) if path else None


def candidate(run_dir, events, source_hint: Optional[str] = None) -> dict:
    """`{"commit", "source"}` the first base may be parented on, else `{"reason"}` — from the log
    and the live engine's source (`source_hint`), else the task snapshot (the probe of the source
    is `probe`)."""
    started: dict = {}
    for e in events or ():
        if getattr(e, "type", None) == "run_started" and isinstance(getattr(e, "data", None), dict):
            started = e.data
            break
    if not started:
        return {"reason": "no_run_started"}
    if started.get("seed_base") is not None:
        return {"reason": "seed_base_pinned"}
    workspace = started.get("workspace") if isinstance(started.get("workspace"), dict) else {}
    editables = {k[len("editable:"):]: v for k, v in workspace.items()
                 if isinstance(k, str) and k.startswith("editable:")}
    if len(editables) != 1 or next(iter(editables)) not in (".", ""):
        return {"reason": "not_one_root_editable"}
    value = next(iter(editables.values()))
    sha = value[4:] if isinstance(value, str) and value.startswith("git:") else None
    if sha is None or not _SHA.match(sha):
        return {"reason": "editable_not_git"}
    if source_hint:
        return {"commit": sha, "source": source_hint}
    from looplab.engine.code_export import _editable_sources
    sources = [src for name, src in (_editable_sources(Path(run_dir)) or ()) if name in (".", "")]
    return {"commit": sha, "source": sources[0] if sources else None}


def probe(source, commit: str) -> Optional[str]:
    """None when `source` is the TOP of a git repository holding `commit`, else the reason."""
    if not source:
        return "editable_source_unknown"
    try:
        top = run_git("-C", str(source), "rev-parse", "--show-toplevel", timeout=PROBE_TIMEOUT_S,
                      check=False)
        if top.returncode != 0:
            return "editable_not_git"
        if (os.path.realpath(top.stdout.decode("utf-8", "replace").strip())
                != os.path.realpath(os.path.expanduser(str(source)))):
            return "editable_is_a_subdirectory"
        if run_git("-C", str(source), "cat-file", "-e", commit + "^{commit}",
                   timeout=PROBE_TIMEOUT_S, check=False).returncode != 0:
            return "source_commit_missing"
    except CodeRepoError as exc:
        return exc.code
    return None


def _sidecar(run_dir, commit: str) -> Path:
    return Path(run_dir) / ORIGIN_REL / f"{commit}.pack"


def _cut_if_orphaned(repo: CodeRepo, commit: str) -> None:
    """Make `commit` a shallow cut when any parent it names is absent (a sidecar import)."""
    if any(not repo.has_object(p) for p in repo.commit_parents(commit)):
        repo.add_shallow([commit])


def obtain(repo: CodeRepo, run_dir, commit: str, source) -> dict:
    """Make `commit` (and its tree) present in the private repository. Returns `{"status":
    "present" | "fetched" | "sidecar", "history": ...}` or `{"status": "unavailable", "reason"}`.
    Callers hold `repo.lock()`."""
    if repo.has_object(commit + "^{tree}"):
        return {"status": "present"}
    reasons = []
    if source and os.path.isdir(os.path.expanduser(str(source))):
        src = os.path.expanduser(str(source))
        for depth in (HISTORY_DEPTH, 1):
            try:
                # BY SHA, so the commit need not be a ref tip any more (the operator committed
                # since the run started): protocol v2 serves any reachable object, v0 refuses an
                # unadvertised one (measured, git 2.43) — hence pinned, not left to config.
                repo.git("-c", "protocol.version=2", "fetch", "--quiet", "--no-tags",
                         "--no-write-fetch-head", f"--depth={depth}", src, commit,
                         timeout=FETCH_TIMEOUT_S)
            except CodeRepoError as exc:
                reasons.append(f"fetch_depth_{depth}:{exc.code}")
                continue
            if repo.has_object(commit + "^{tree}"):
                out = {"status": "fetched", "depth": depth}
                if reasons:
                    out["history"] = "unavailable"
                    out["history_reason"] = reasons[0]
                return out
    else:
        reasons.append("source_missing")
    pack = _sidecar(run_dir, commit)
    if pack.is_file():
        try:
            repo.git("index-pack", "--stdin", input=pack.read_bytes(), timeout=FETCH_TIMEOUT_S)
        except (CodeRepoError, OSError) as exc:
            reasons.append(f"sidecar:{getattr(exc, 'code', type(exc).__name__)}")
        else:
            if repo.has_object(commit + "^{tree}"):
                _cut_if_orphaned(repo, commit)
                return {"status": "sidecar", "history": "unavailable",
                        "history_reason": reasons[0] if reasons else "source_missing"}
            reasons.append("sidecar_incomplete")
    return {"status": "unavailable", "reason": ";".join(reasons)[:300] or "unavailable"}


def write_sidecar(repo: CodeRepo, run_dir, commit: str, base_commit: str) -> str:
    """Write `code/origin/<commit>.pack` once: `commit`, its tree and every object of that tree the
    base's tree does not hold. Returns `written` / `exists` / `too_large` / `unavailable`."""
    pack = _sidecar(run_dir, commit)
    if pack.is_file():
        return "exists"
    base_tree = repo.tree(base_commit)
    if base_tree is None:
        return "unavailable"
    try:
        # The parent commit ALONE (`--no-walk`; a negative rev would make it a range and walk the
        # fetched history), minus every object the base tree holds — a rebuild writes those first.
        listing = repo.git("ls-tree", "-r", "-t", "-z", base_tree).stdout.split(b"\x00")
        have = {item.split(b"\t", 1)[0].split(b" ")[-1] for item in listing if item}
        have.add(base_tree.encode("ascii"))
        rows = repo.git("rev-list", "--objects", "--no-walk", commit).stdout.splitlines()
        objects = b"".join(row + b"\n" for row in rows if row.split(b" ", 1)[0] not in have)
        data = repo.git("pack-objects", "--stdout", "--quiet", input=objects, timeout=300).stdout
    except CodeRepoError:
        return "unavailable"
    if len(data) > MAX_ORIGIN_PACK_BYTES:
        return "too_large"
    from looplab.core.atomicio import atomic_write_bytes
    try:
        pack.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(pack, data)
    except OSError:
        return "unavailable"
    return "written"


def first_base_parent(repo: CodeRepo, run_dir, events, source_hint: Optional[str] = None) -> tuple:
    """`(parent sha | None, diagnostic dict)` for the run's FIRST base commit. Never raises."""
    try:
        decided, sha = decided_parent(events)
        if decided and sha is None:
            return None, {"status": "root", "reason": "recorded_root"}
        found = candidate(run_dir, events, source_hint)
        if decided:
            source = found.get("source") if found.get("commit") == sha else None
        else:
            if found.get("reason"):
                return None, {"status": "root", "reason": found["reason"]}
            sha, source = found["commit"], found.get("source")
            reason = probe(source, sha)
            if reason:
                return None, {"status": "root", "reason": reason}
        got = obtain(repo, run_dir, sha, source)
        if got.get("status") == "unavailable":
            return None, {"status": "root", "reason": "origin_parent_unavailable",
                          "detail": got.get("reason")}
        return sha, {"status": "parented", "commit": sha, "via": got.pop("status"), **got}
    except (CodeRepoError, OSError) as exc:   # a record, never a refusal
        return None, {"status": "root", "reason": getattr(exc, "code", type(exc).__name__)}
