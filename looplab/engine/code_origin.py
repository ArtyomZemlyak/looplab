"""The operator's source HEAD as the PARENT of the run's first base commit (doc 79, 2026-10-16).

WHY. The run's first base commit (`code_identity.py::ensure_base`) used to be a ROOT commit: its tree
is the seed archive — the operator's working tree, uncommitted edits included — and nothing else. A
worktree of the private repository therefore showed no history: `git log` ended at "the code run X
started from", `git blame` blamed every line on LoopLab, and `git diff` could not say which bytes were
the operator's uncommitted edits. Parenting that commit on the commit the editable source stood at
when the run started (`run_started.workspace["editable:."]`, the value `run_source` reads for the
export, the base parent and a foreign branch's merge base alike) changes none of that tree — node
materialization reads the seed ARCHIVE, never a commit tree — and makes `git diff <source HEAD>
<base>` exactly the edits the operator never committed (doc 37 §5d stays solved: the tree is still
the working tree, not HEAD).

READ-ONLY ON THE OPERATOR'S REPOSITORY. The parent's objects are FETCHED from it into the private
repository (`CodeRepo.fetch_readonly`: upload-pack on the operator's side, which reads; measured
byte-identical `.git` before and after, `rev-list --count --all` unchanged). Nothing is ever written
there — no ref, object, config or index.

WHY THE OBJECTS, NOT ONLY A `shallow` LINE (measured 2026-10-16, git 2.43). A base commit naming a
parent the repository does not hold is NOT made usable by listing that parent in `$GIT_DIR/shallow`:
`log`, `rev-list` and `bundle create` fail with "Failed to traverse parents". So a parent is chosen
only when its objects could be obtained, from (in order) the private repository itself, the
operator's repository (`HISTORY_DEPTH` commits, else depth 1, both inside ONE `FETCH_BUDGET_S`) or
this run's SIDECAR PACK `code/origin/<sha>.pack` — the parent commit, its root tree and those of its
other tree objects the seed tree does not already hold (typically kilobytes), written once beside the
run and carried by the run mirror, so a restore rebuilds the SAME base id with the operator's
repository gone.

DETERMINISM: ONCE DECIDED, THE ID NEVER MOVES (review 2026-10-16). The parent is DECIDED by, in
order: the first recorded `code_snapshot` (a `version` 1 row — every run before this change — means a
root; a later row names its `base.origin_parent` or none), then this run's own durable record of the
first build (`code/origin/decision.json`, or a sidecar pack for the run's start commit — written by
the build itself, so a run whose rows were never appended, e.g. a kill between base and row or an
offline `sync_run`, still rebuilds the same base after a mirror restore), and only then — nothing
decided yet — by probing the source, as the live engine does. When the decision names a parent whose
objects can no longer be had (no source, no complete sidecar), the base is STILL written naming it
(the same id), as its own shallow cut — measured usable — and `base.origin.via` says `cut`; it is
never silently rebuilt as a root with another id, which would leave every id in the log and in
exported branches naming a commit the rebuilt repository cannot produce.

WHEN THE BASE STAYS A ROOT. A pinned `seed_base` (the archive is another run's), several editables or
one mounted under a name (the private repository's paths are not the source repository's), a
non-git editable, an editable that is a SUBDIRECTORY of its repository (its tree would carry a prefix
the attempt commits, overlays and export do not — see doc 79 §10), an unresolvable source commit and,
before anything was decided, every failure to obtain its objects. Each is a recorded reason
(`base.origin` on the row), never a refusal: code identity may not cost a node its evaluation.

COST ON THE EVAL PATH (review 2026-10-16). The first node's materialization builds the first base, so
the fetch is bounded: one budget for both depths, run OUTSIDE the run repository's lock
(`code_identity.py::record_attempt` prepares the decision before it locks), and paid once per run —
the built base, the decision file and the sidecar are its durable result.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from looplab.engine.code_repo import (IDENTITY_VERSION, CodeRepo, CodeRepoError, is_dubious_ownership,
                                      is_object_id, readonly_git)

ORIGIN_REL = "code/origin"
DECISION_NAME = "decision.json"
# The history fetched behind the parent: enough for `log`/`blame` over the recent past, bounded so
# a large monorepo is not cloned into every run. Not a Settings field: it moves no result.
HISTORY_DEPTH = 200
# ONE budget for the whole obtain (depth `HISTORY_DEPTH`, then depth 1 with what is left): it runs
# on the first node's eval path (it used to be 60 s per depth, up to two minutes before the first
# eval). A local fetch is seconds; past the budget the base is a recorded root.
FETCH_BUDGET_S = 30.0
FETCH_TIMEOUT_S = 60.0          # a sidecar's `index-pack` (local bytes, no source involved)
PROBE_TIMEOUT_S = 20.0
# The sidecar holds what the seed tree does not (committed versions of edited files, tracked files
# the seed left out); past this it is not written, and a restore without the source says so.
MAX_ORIGIN_PACK_BYTES = 64 * 1024 * 1024
FIRST_PARENTED_VERSION = 2


def _first_snapshot(events, *, recorded: bool) -> Optional[dict]:
    for e in events or ():
        if getattr(e, "type", None) != "code_snapshot":
            continue
        data = getattr(e, "data", None)
        if isinstance(data, dict) and (not recorded or data.get("status") == "recorded"):
            return data
    return None


def run_version(events, current: int = IDENTITY_VERSION, repo: Optional[CodeRepo] = None) -> int:
    """The code-identity version this RUN's commits are built at (review 2026-10-16):

    1. the `version` of its FIRST `code_snapshot` row of ANY status (an `unavailable` row was still
       written by an engine of that version, and a resumed version-1 run keeps stamping 1 —
       `code_identity.py::record_attempt`); a recorded row without one is version 1;
    2. else the version its repository was first built at (`CodeRepo.identity_version`; a
       repository holding bases but no stamp predates the stamp, i.e. version 1);
    3. else `current`. Only a run with no row and no repository lands here: a pre-doc-79 run, or one
       killed before its first record. Its ids were never published in the log, so nothing in it can
       disagree with them — except an export of an EARLIER offline sync whose repository is gone
       since (a mirror restore skips `code/repo.git`): stated, not reproducible, and the price of
       not guessing a version from nothing."""
    first = _first_snapshot(events, recorded=False)
    if first is not None:
        version = first.get("version")
        if type(version) is int:
            return version
        if first.get("status") == "recorded":
            return 1
    if repo is not None and repo.exists():
        stamped = repo.identity_version()
        if stamped is not None:
            return stamped
        if repo.refs("refs/looplab/bases/"):
            return 1
    return current


def run_source(events, run_dir, source_hint: Optional[str] = None) -> tuple:
    """`(root editable's source path | None, source commit | None)` — THE one reading of where the
    run's code came from, shared by the first base's parent (`candidate`), the export's `onto` and
    drift (`code_export.py`) and a foreign branch's merge base (`code_foreign.py`), which used to
    re-derive it with diverging rules (`~` expanded in one of three).

    The commit is the root editable's `run_started.workspace` value when the run has one (git or
    not), else the one commit every named editable agrees on (`_recorded_source_commit`). The path is
    `source_hint` (a live engine's `_repo_spec`, `engine_source`), else the task snapshot's root
    editable read through the adapter — `~` expanded either way."""
    from looplab.engine.code_export import _editable_sources, _recorded_source_commit
    started: dict = {}
    for e in events or ():
        if getattr(e, "type", None) == "run_started" and isinstance(getattr(e, "data", None), dict):
            started = e.data
            break
    workspace = started.get("workspace") if isinstance(started.get("workspace"), dict) else {}
    commit = _recorded_source_commit(workspace)
    path = source_hint
    if not path and run_dir is not None:
        path = next((src for name, src in (_editable_sources(Path(run_dir)) or ())
                     if name in (".", "") and src), None)
    return (os.path.expanduser(str(path)) if path else None,
            commit if is_object_id(commit) else None)


def _decision_path(run_dir) -> Path:
    return Path(run_dir) / ORIGIN_REL / DECISION_NAME


def read_decision(run_dir) -> Optional[dict]:
    """This run's own record of the first base's parent (`write_decision`), None when absent."""
    if run_dir is None:
        return None
    try:
        data = json.loads(_decision_path(run_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "parent" not in data:
        return None
    parent = data.get("parent")
    return {"parent": parent if is_object_id(parent) else None}


def write_decision(run_dir, parent: Optional[str]) -> None:
    """Record the first base's parent decision beside the run, once (never rewritten): what a
    rebuild with no recorded row reads before probing the source (`decided_parent`)."""
    path = _decision_path(run_dir)
    if path.exists():
        return
    from looplab.core.atomicio import atomic_write_text
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"version": IDENTITY_VERSION, "parent": parent}))
    except OSError:
        pass        # a record of a record: the row on the log still decides for every live run


def decided_parent(events, run_dir=None, repo: Optional[CodeRepo] = None):
    """`(True, sha | None)` when the first base's parent is already decided, `(False, None)` when
    nothing decided it yet (see the module docstring for the order)."""
    first = _first_snapshot(events, recorded=True)
    if first is not None:
        if not (type(first.get("version")) is int and first["version"] >= FIRST_PARENTED_VERSION):
            return True, None
        base = first.get("base") if isinstance(first.get("base"), dict) else {}
        sha = base.get("origin_parent")
        return True, (sha if is_object_id(sha) else None)
    if run_version(events, IDENTITY_VERSION, repo) < FIRST_PARENTED_VERSION:
        return True, None
    decision = read_decision(run_dir)
    if decision is not None:
        return True, decision["parent"]
    if run_dir is not None:
        # A sidecar for the run's start commit is written only by a build that PARENTED the base on
        # it (a run built before `decision.json` existed): the same decision, durably.
        _, commit = run_source(events, run_dir)
        if commit and _sidecar(run_dir, commit).is_file():
            return True, commit
    return False, None


def engine_source(engine) -> Optional[str]:
    """The root editable's source path as a live engine holds it (`_repo_spec`); the offline
    reader is the task snapshot (`run_source`). None when there is not exactly one root editable."""
    spec = getattr(engine, "_repo_spec", None) or {}
    roots = [ed for ed in spec.get("editables") or () if isinstance(ed, dict)
             and str(ed.get("name") or ".") in (".", "")]
    if len(roots) != 1 or len(spec.get("editables") or ()) != 1:
        return None
    path = roots[0].get("origin_path") or roots[0].get("path")
    return os.path.expanduser(str(path)) if path else None


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
    editables = [k for k in workspace if isinstance(k, str) and k.startswith("editable:")]
    if len(editables) != 1 or editables[0][len("editable:"):] not in (".", ""):
        return {"reason": "not_one_root_editable"}
    source, sha = run_source(events, run_dir, source_hint)
    if sha is None:
        return {"reason": "editable_not_git"}
    return {"commit": sha, "source": source}


def _same_dir(a: str, b: str) -> bool:
    """One directory, however spelled: `samefile` answers through symlinks and a case-insensitive
    filesystem (macOS, Windows), where comparing two `realpath` strings said "subdirectory"."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def probe(source, commit: str) -> Optional[str]:
    """None when `source` is the TOP of a git repository holding `commit`, else the reason. Every
    call is read-only (`readonly_git`), and a repository another uid owns is said as such
    (`source_dubious_ownership`) rather than as "not git"."""
    if not source:
        return "editable_source_unknown"
    src = os.path.expanduser(str(source))
    try:
        top = readonly_git(src, "rev-parse", "--show-toplevel", timeout=PROBE_TIMEOUT_S, check=False)
        if top.returncode != 0:
            return "source_dubious_ownership" if is_dubious_ownership(top) else "editable_not_git"
        if not _same_dir(top.stdout.decode("utf-8", "replace").strip(), src):
            return "editable_is_a_subdirectory"
        if readonly_git(src, "cat-file", "-e", commit + "^{commit}", timeout=PROBE_TIMEOUT_S,
                        check=False).returncode != 0:
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


def obtain(repo: CodeRepo, run_dir, commit: str, source, *,
           budget_s: Optional[float] = None) -> dict:
    """Make `commit` (and its root tree) present in the private repository. Returns `{"status":
    "present" | "fetched" | "sidecar", "history": ...}` or `{"status": "unavailable", "reason"}`.

    Needs no `repo.lock()`: the fetch and `index-pack` write under git's own lockfiles and the cut
    under `shallow.lock` (`CodeRepo.add_shallow`), so the caller runs it BEFORE it locks."""
    if repo.has_object(commit + "^{tree}"):
        return {"status": "present"}
    reasons = []
    src = os.path.expanduser(str(source)) if source else None
    if src and os.path.isdir(src):
        deadline = time.monotonic() + (FETCH_BUDGET_S if budget_s is None else budget_s)
        for depth in (HISTORY_DEPTH, 1):
            left = deadline - time.monotonic()
            if left < 1.0:
                reasons.append(f"fetch_depth_{depth}:budget_spent")
                break
            try:
                # BY SHA, so the commit need not be a ref tip any more (the operator committed
                # since the run started): protocol v2 serves any reachable object, v0 refuses an
                # unadvertised one (measured, git 2.43) — pinned in `fetch_readonly`.
                repo.fetch_readonly(src, [commit], depth=depth, timeout=left)
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
    """Write `code/origin/<commit>.pack` once: `commit`, its ROOT tree and every other object of
    that tree the base's tree does not hold. Returns `written` / `exists` / `too_large` /
    `unavailable`.

    The root tree is ALWAYS in it (review 2026-10-16): a rebuild reads the pack BEFORE it writes
    the base, and `obtain` asks for `<commit>^{tree}` — a clean checkout (seed tree == the commit's
    tree) used to leave only the commit in the pack, read as `sidecar_incomplete`, and the base was
    rebuilt as a root with another id. What the base's tree holds below the root is written by the
    base itself, right after."""
    pack = _sidecar(run_dir, commit)
    if pack.is_file():
        return "exists"
    base_tree = repo.tree(base_commit)
    if base_tree is None:
        return "unavailable"
    try:
        # The parent commit ALONE (`--no-walk`; a negative rev would make it a range and walk the
        # fetched history), minus every object below the base's root tree. `--no-object-names`:
        # an object id per line, whatever bytes a path holds (a name with a newline split a row).
        listing = repo.git("ls-tree", "-r", "-t", "-z", base_tree).stdout.split(b"\x00")
        have = {item.split(b"\t", 1)[0].split(b" ")[-1] for item in listing if item}
        rows = repo.git("rev-list", "--objects", "--no-object-names", "--no-walk",
                        commit).stdout.split()
        objects = b"".join(row + b"\n" for row in rows if row not in have)
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
    """`(parent sha | None, diagnostic dict)` for the run's FIRST base commit. Never raises.

    A parent the decision names but whose objects cannot be had comes back with `via: "cut"`: the
    base is written naming it anyway (`CodeRepo.write_tree_commit`) and made its own shallow cut."""
    decided, sha = False, None
    try:
        decided, sha = decided_parent(events, run_dir, repo)
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
            if decided:
                return sha, _cut(sha, got.get("reason"))
            return None, {"status": "root", "reason": "origin_parent_unavailable",
                          "detail": got.get("reason")}
        return sha, {"status": "parented", "commit": sha, "via": got.pop("status"), **got}
    except (CodeRepoError, OSError) as exc:   # a record, never a refusal
        reason = getattr(exc, "code", type(exc).__name__)
        if decided and sha:
            return sha, _cut(sha, reason)     # decided: the id may not move, whatever failed
        return None, {"status": "root", "reason": reason}


def _cut(sha: str, detail) -> dict:
    return {"status": "parented", "commit": sha, "via": "cut", "history": "unavailable",
            "history_reason": "origin_objects_missing", "detail": detail}
