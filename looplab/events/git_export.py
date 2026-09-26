"""The run's node DAG as GIT HISTORY (doc 67 67.15): one commit per node LIFECYCLE, its parents the
exact parent lifecycles it was built from, the lifecycle's own files as the tree, the metric and the
receipts that decide whether it counts as `Looplab-*` trailers.

WHY. Every node's code is already in the event log — `node_created` carries `code`, `files` and
`deleted` — but the only way to read two nodes side by side was LoopLab's own `tools/node_diff.py`
or the UI. The engineer's tools — `git log --graph`, `git diff node-3 node-7`, `git bisect` over a
regression between two measured nodes, `git blame` on the champion's training loop — need a
repository. This is a PROJECTION, never a second source of truth: a pure function of the log and its
fold, re-derived on every call and never read back, so `events.jsonl` stays authoritative (the
OpenResearch pattern the item cites keeps git as the source; LoopLab keeps the log).

A COMMIT PER LIFECYCLE, NOT PER NODE ID (review 2026-09-26). A node id survives `node_reset`: the
reset opens a new lifecycle GENERATION of the same id (`Node.attempt`), and a `propose`/`implement`
reset rebuilds it with new code. One commit per id wired every child to its parent's CURRENT
lifecycle, so after a reset and rebuild of node 0, `node-1^:solution.py` held code node 1 never saw.
The fold keeps the exact parent lifecycle each node was built from — `Node.parent_generations`, the
receipt the W3C-PROV export (`serve/routers/runs.py`'s `/prov`) reads for the same reason — so every
lifecycle is its own commit and a child's parents are wired through that map. The current lifecycle
is tag `node-<id>`; one a later reset superseded is `node-<id>.g<generation>`. A superseded tree is
what that lifecycle held when the reset ended it — an inline repair replaces the code WITHIN a
lifecycle, so the row that opened it is not enough — read off the fold's own raw state at that
boundary (`node_lifecycles`, one pass of `replay.FoldCursor`), never re-derived from the rows. Its
message says it was superseded and carries NO `Looplab-Metric`: the fold dropped that number at the
reset, and a script reading trailers must not count a candidate that no longer exists.

WHAT A COMMIT'S TREE IS, AND IS NOT. Every materialization is the task's base tree seeded first, then
the node's `files` written on top, then its `deleted` names removed, then the task's assets
(`engine/workspace.py::WorkspaceSeeder.materialize`, `write_node_files`) — so `files` is the node's
WHOLE edit set relative to that base, not a delta over its parent, and a tree is that set as the
checkout held it (`lifecycle_tree`):

  * `solution.py` is the node's `code` whenever it has one. The sandbox writes it from `code` and
    `write_node_files` never writes a `files["solution.py"]`, so letting that key win exported code
    that never ran — the case `engine/bundle.py::export_bundle` fixed as ENG3-15, mirrored here.
  * A name the materializer skips is not the node's file: `solution.py`, a task asset (the names the
    fold's `data_provenance` pinned at setup) and a ratified onboarding adapter. A repo task's own
    `protect` list is NOT in the log — it is derived from the operator's source tree when the task
    loads — so it cannot be honoured here; the Developer's write gate refuses those names anyway.
  * A name in both `files` and `deleted` is gone, as the write-then-delete order leaves it.

The base tree itself is not in the log (doc 67 67.12, `repo-base-tree-not-archived`), so it is not
here either; a node's `deleted` names are removals from that base and ride in the message.

WHAT IT REFUSES, per path component (`safe_tree_path`). A clone may be made by someone who never saw
the run, pushed to a host that runs `git fsck` on receipt, or checked out on Windows or a Mac, so a
name any of those would turn against its reader is left out: empty, `.`, `..` (so absolute too);
anything NTFS or HFS+ reads as `.git` once trailing dots and spaces, the code points HFS+ ignores and
case are taken away (`.git.`, `.GIT `, `.g\u200cit`, `GIT~1`); every other name starting `.git` — git
itself interprets `.gitattributes` (eol conversion, `working-tree-encoding`, `filter=lfs`, which runs
the READER's filter driver), `.gitmodules` and `.gitignore`, and the two largest hosts run
`.github/workflows` and `.gitlab-ci.yml` on push — plus `.lfsconfig`, `.mailmap` and the 8.3 short
names of all of them; a Windows device name (`con`, `NUL.py`, `com1`); a character Windows cannot
store (`<>:"|?*` — so an NTFS stream `x.py:stream` and a drive `C:x`) or a control character; a
trailing dot or space; a component over 255 bytes. Of two names one filesystem stores as ONE file —
`dir\\x` beside `dir/x`, `A.py` beside `a.py`, a file `a` beside a file `a/b.py` — the first in sorted
order keeps it (`solution.py` from `code` before any). Every name left out, for any reason, is
COUNTED by reason in `Looplab-Skipped-Paths`: the log keeps it, the export says it did not show it.

THE MESSAGE. Subject `node <id>: <operator> — <rationale>`, the hypothesis, then one `Looplab-*`
trailer per fact, every value one line with control characters stripped: a NUL makes `git fsck
--strict` refuse the commit (`nulInCommit`) and `git log` stop at it, and a newline in a free-text
field — the operator an `inject_node` names — forged a `Looplab-Metric` trailer. A metric ships WITH
the receipts that decide whether it counts: `Looplab-Feasible`, `Looplab-Violations`,
`Looplab-Counts-Toward-Best` (`core/fitness.py::counts_toward_best`, the ONE rule, read through
`promotion_eligible_nodes`), `Looplab-Salvaged` and `Looplab-Trust-Flagged`. ONE spelling per fact
about selection: `Looplab-Champion: yes` marks `RunState.best()` — the fold's selector pick, the node
the run row, the reviewer bundle, `champion_metric_caveats`, `mislead_gap`, the DAG ring and the
Inspector all crown, and what branch `champion` points at — with `Looplab-Champion-Caveats` beside it
when the caller computed them; there is no second "best" trailer. `Looplab-Promoted: yes` marks the
operator's promote alias (`RunState.champion`), published as branch `promoted` and never checked out
in the best's place: the alias checks no status, so it can name a weaker or a failed node. A log read
only up to a corrupt line says so on EVERY commit (`Looplab-Log-Incomplete`, in the store's own
`integrity_sentence` wording), because a reader of the repository never sees the CLI's stderr.

DETERMINISTIC. Parents before children (ties by node id, then generation), paths sorted, each commit
dated by its lifecycle's first ACCEPTED `node_created` row — or, for a lifecycle a reset opened and no
build followed (an `eval` reset re-scores the same code), by that reset — through the fold's one
usable-timestamp rule (`replay_ctx.event_timestamp`: nothing outside (epoch, 9999-12-31] is a date,
past it a `ts` is corruption or a unit mix-up, and git's own limits lie further out still — `fsck
--strict` refuses a date from 2**63, fast-import one at 2**64, measured on git 2.43), one fixed
identity at `+0000` — so one log exports to the same commit ids every time, and a reviewer can pin
one. Running git is the CLI's job (`cli/export_cmds.py::export_git`, hermetically): this module
writes no file and starts no process.
"""
from __future__ import annotations

import heapq
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Optional

from looplab.core.jsonutil import surrogate_safe
from looplab.core.models import coerce_node_id
from looplab.events.eventstore import integrity_sentence
from looplab.events.replay import (FoldCursor, flagged_node_ids, hard_flagged_ids,
                                   promotion_eligible_nodes)
from looplab.events.replay_ctx import event_timestamp
from looplab.events.types import EV_NODE_CREATED, EV_NODE_RESET

GIT_IDENTITY = "LoopLab <looplab@invalid>"
SOLUTION_PATH = "solution.py"
CHAMPION_BRANCH = "champion"
CHAMPION_REF = f"refs/heads/{CHAMPION_BRANCH}"
PROMOTED_REF = "refs/heads/promoted"
# The longest path kept; git's own limit is the platform's, and a longer one is no source file.
_MAX_PATH_BYTES = 4096
# NAME_MAX on every filesystem a clone is likely to land on (ext4, APFS, NTFS counts UTF-16 units).
_MAX_COMPONENT_BYTES = 255
# A trailer value past this is cut and says so: `Looplab-Params` is the only unbounded one.
_MAX_TRAILER_CHARS = 2000

# Every `Cc` code point: C0, DEL and C1.
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")
# The explicit bidi controls. Harmless to git, but they make `git log` DISPLAY a line other than the
# one a trailer parser reads (the Trojan-Source shape), so no message carries one.
_BIDI = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
# The code points HFS+ ignores when it compares names, so `.g\u200cit` IS `.git` on a Mac: exactly
# the set git's own `is_hfs_dotgit` skips (utf8.c `next_hfs_char`).
_HFS_IGNORABLE = re.compile("[\u200c-\u200f\u202a-\u202e\u206a-\u206f\ufeff]")
# NTFS 8.3 short names git refuses: `GIT~1` for `.git`, the hashed `gi<4 hex>~<n>` family, and the
# fixed prefixes for `.gitmodules` / `.gitattributes` / `.gitignore` / `.mailmap`
# (`is_ntfs_dot_generic`). Matched against a casefolded component.
_NTFS_SHORT_NAME = re.compile(r"git~\d+|gi[0-9a-f]{4}~\d+|(?:gitmod|gitatt|gitign|mailma)~\d+")
_WINDOWS_DEVICES = frozenset({"con", "prn", "aux", "nul", "conin$", "conout$",
                              *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))})
_WINDOWS_INVALID = frozenset('<>:"|?*')

# Why a name the log holds is not in a commit's tree, in the order the trailer states them.
_SKIP_REASONS = (("unsafe", "unsafe in a checkout"),
                 ("engine", "never materialized by the engine"),
                 ("collision", "colliding with another name on some checkout"),
                 ("deleted", "removed by the node's own deleted list"))


def node_tag(node_id: int, generation: Optional[int] = None) -> str:
    """The tag a lifecycle's commit is published under: `node-<id>` for the node's CURRENT lifecycle,
    `node-<id>.g<generation>` for one a later `node_reset` superseded."""
    tag = f"node-{int(node_id)}"
    return tag if generation is None else f"{tag}.g{int(generation)}"


def node_ref(node_id: int, generation: Optional[int] = None) -> str:
    """`node_tag` as the lightweight tag's full ref — a TAG, not a branch: a lifecycle never moves."""
    return f"refs/tags/{node_tag(node_id, generation)}"


def _refused_component(part: str) -> bool:
    if part in ("", ".", ".."):
        return True
    if len(part.encode("utf-8")) > _MAX_COMPONENT_BYTES:
        return True
    if _CONTROL.search(part) or any(ch in _WINDOWS_INVALID for ch in part):
        return True
    if part[-1] in ". ":
        return True                      # Windows drops them: `notes.` IS `notes` there
    # The name NTFS or HFS+ would store: ignorables out, trailing dots and spaces off, then case.
    # (Everything from a `:` on is an NTFS stream name — refused outright above.)
    stored = _HFS_IGNORABLE.sub("", part).rstrip(". ").casefold()
    if (stored.startswith(".git") or stored in (".lfsconfig", ".mailmap")
            or _NTFS_SHORT_NAME.fullmatch(stored)):
        return True
    return stored.split(".", 1)[0].rstrip(" ") in _WINDOWS_DEVICES


def safe_tree_path(path) -> Optional[str]:
    """`path` as a tree path no checkout can turn against its reader, or ``None`` (the module
    docstring's WHAT IT REFUSES). A Windows separator becomes `/`; nothing else is rewritten."""
    if not isinstance(path, str) or not path:
        return None
    norm = surrogate_safe(path).replace("\\", "/")
    if len(norm.encode("utf-8")) > _MAX_PATH_BYTES:
        return None
    if any(_refused_component(part) for part in norm.split("/")):
        return None
    return norm


def _collision_key(path: str) -> str:
    """The name a case-folding, normalizing filesystem (NTFS, APFS/HFS+) stores `path` under — the
    canonical caseless form, HFS+'s ignorable code points taken out."""
    decomposed = unicodedata.normalize("NFD", _HFS_IGNORABLE.sub("", path))
    return unicodedata.normalize("NFD", decomposed.casefold())


def lifecycle_tree(code, files, deleted, *, skip=frozenset()) -> tuple[dict, Counter]:
    """`({path: bytes}, Counter(reason -> names left out))` for one lifecycle, in materialization order:
    `solution.py` from `code`, then `files` (sorted by name) minus every name `skip` holds — the names
    the engine's materializer skips — then the names `deleted` removes. Each name the log holds and the
    tree does not show is counted under one `_SKIP_REASONS` key."""
    tree: dict[str, bytes] = {}
    skipped: Counter = Counter()
    stored: set = set()             # the collision key of every kept file…
    directories: set = set()        # …and of every directory one sits under

    def keep(path: str, data: bytes) -> bool:
        key = _collision_key(path)
        parts = key.split("/")
        ancestors = {"/".join(parts[:i]) for i in range(1, len(parts))}
        # One file under two spellings, a file where a directory is, a directory where a file is:
        # fast-import resolves the last by DROPPING the file (driven: `a` then `a/b.py` imports as
        # `a/b.py` alone), and a case-folding checkout writes the first twice. Keep the first.
        if key in stored or key in directories or not stored.isdisjoint(ancestors):
            return False
        stored.add(key)
        directories.update(ancestors)
        tree[path] = data
        return True

    if code:
        keep(SOLUTION_PATH, surrogate_safe(str(code)).encode("utf-8"))
    files = files if isinstance(files, dict) else {}
    for name in sorted(files, key=str):
        safe = safe_tree_path(name)
        if safe is None:
            skipped["unsafe"] += 1
        elif safe in skip:
            skipped["engine"] += 1
        elif not keep(safe, surrogate_safe(str(files[name])).encode("utf-8")):
            skipped["collision"] += 1
    # Deleted AFTER written, as `write_node_files` does it: a name in both is not in the checkout. A
    # name the materializer protects is never deleted (it skips those too), `solution.py` included.
    for name in (deleted if isinstance(deleted, (list, tuple)) else ()):
        safe = safe_tree_path(name)
        if safe is not None and safe not in skip and safe in tree:
            del tree[safe]
            skipped["deleted"] += 1
    return tree, skipped


def _quoted(path: str) -> bytes:
    """`path` C-quoted the way fast-import reads a quoted path — always quoted, which is simpler
    than deciding when a space, a quote or a newline would need it. `safe_tree_path` admits no
    quote, backslash or control character, so the escapes are the second lock, not the first."""
    out = ['"']
    for ch in path:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\%03o" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out).encode("utf-8")


def _one_line(value) -> str:
    """`value` as ONE trailer-safe line: control characters become spaces, bidi controls go, runs of
    whitespace collapse, and a value past `_MAX_TRAILER_CHARS` is cut and says so."""
    text = _BIDI.sub("", _CONTROL.sub(" ", surrogate_safe(str(value))))
    text = " ".join(text.split())
    return text if len(text) <= _MAX_TRAILER_CHARS else text[:_MAX_TRAILER_CHARS] + " … (cut)"


def _metric_text(value) -> str:
    return "none" if value is None else repr(float(value))


def _git_time(event) -> int:
    """The commit date for a lifecycle `event` opened: its `ts` under the fold's one usable-timestamp
    rule, else the epoch — deterministic either way, which a guess would not be."""
    ts = event_timestamp(event)
    return int(ts) if ts is not None else 0


@dataclass(frozen=True)
class _Lifecycle:
    """One node lifecycle as its commit needs it. The CURRENT one carries the finalized fold's `node`;
    a SUPERSEDED one is a copy of the fold's raw node taken just before the reset that ended it."""
    node_id: int
    generation: int
    operator: str
    idea: object
    code: str
    files: dict
    deleted: tuple
    parent_ids: tuple
    parent_generations: dict
    node: object = None
    reached: str = ""                # superseded only: the status the lifecycle had reached
    reset_stage: str = ""            # superseded only: the `from_stage` of the reset that ended it


def _lifecycle(node, *, superseded_by=None) -> _Lifecycle:
    files = getattr(node, "files", None)
    deleted = getattr(node, "deleted", None)
    idea = getattr(node, "idea", None)
    common = dict(node_id=node.id, generation=int(getattr(node, "attempt", 0) or 0),
                  operator=str(getattr(node, "operator", "") or ""),
                  code=str(getattr(node, "code", "") or ""),
                  files=files if isinstance(files, dict) else {},
                  deleted=tuple(deleted) if isinstance(deleted, (list, tuple)) else (),
                  parent_ids=tuple(getattr(node, "parent_ids", None) or ()),
                  parent_generations=dict(getattr(node, "parent_generations", None) or {}))
    if superseded_by is None:
        return _Lifecycle(idea=idea, node=node, **common)
    # COPIES, taken before the reset runs: the reset rebinds the node's code/files in place and the
    # next build installs a new Node, so nothing read later could still say what this lifecycle held.
    data = superseded_by.data if isinstance(superseded_by.data, dict) else {}
    status = getattr(node, "status", "")
    common["files"] = dict(common["files"])
    return _Lifecycle(idea=idea.model_copy(deep=True) if hasattr(idea, "model_copy") else idea,
                      reached=str(getattr(status, "value", status) or ""),
                      reset_stage=str(data.get("from_stage", "eval")), **common)


def node_lifecycles(events) -> tuple[dict, dict]:
    """`(superseded, born)` over the log, through the fold's own handlers (`FoldCursor`), in ONE pass.

    `superseded[(id, generation)]` is each lifecycle an ACCEPTED `node_reset` ended, as the fold held
    it just before that reset; `born[(id, generation)]` is the commit date of every lifecycle the fold
    opened — its first accepted `node_created`, else the reset that opened it. Accepted is READ off the
    fold, never re-derived: a `node_created` was accepted when the fold installed a new Node for its
    id, a reset when it moved the node to the next generation. A stale build, a late rebuild of a
    superseded lifecycle, a reset naming the wrong generation — each is decided by the handler that
    decides it for every other reader of the log, and this only watches."""
    cursor = FoldCursor()
    # The raw accumulated state the handlers mutate, before any post-pass — the thing `FoldCursor`
    # exists to keep (`tests/test_event_payload_contract.py` reads it the same way). `snapshot()`
    # would deep-copy and finalize the WHOLE state at every watched row to read one node.
    raw = cursor._state
    superseded: dict = {}
    built: dict = {}
    opened: dict = {}
    for event in events or ():
        etype = getattr(event, "type", None)
        data = getattr(event, "data", None)
        nid = (coerce_node_id(data)
               if etype in (EV_NODE_CREATED, EV_NODE_RESET) and isinstance(data, dict) else None)
        before = raw.nodes.get(nid) if nid is not None else None
        ending = (_lifecycle(before, superseded_by=event)
                  if etype == EV_NODE_RESET and before is not None else None)
        cursor.extend((event,))
        after = raw.nodes.get(nid) if nid is not None else None
        if after is None:
            continue
        if etype == EV_NODE_CREATED and after is not before:
            built.setdefault((nid, after.attempt), _git_time(event))
        elif ending is not None and after.attempt == ending.generation + 1:
            superseded[(nid, ending.generation)] = ending
            opened.setdefault((nid, after.attempt), _git_time(event))
    return superseded, {**opened, **built}


def _on_a_cycle(stuck: set, parents: dict) -> set:
    """The keys of `stuck` that lie ON a cycle — a strongly connected component of more than one key
    (self-edges are gone before this is asked). Iterative Tarjan, visited in sorted order."""
    index: dict = {}
    low: dict = {}
    on_stack: set = set()
    stack: list = []
    found: set = set()

    def ups(key):
        return iter(sorted({p for p in parents[key] if p in stuck and p != key}))

    for root in sorted(stuck):
        if root in index:
            continue
        index[root] = low[root] = len(index)
        stack.append(root)
        on_stack.add(root)
        work = [(root, ups(root))]
        while work:
            key, edges = work[-1]
            for up in edges:
                if up not in index:
                    index[up] = low[up] = len(index)
                    stack.append(up)
                    on_stack.add(up)
                    work.append((up, ups(up)))
                    break
                if up in on_stack:
                    low[key] = min(low[key], index[up])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[key])
                if low[key] == index[key]:
                    component = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == key:
                            break
                    if len(component) > 1:
                        found.update(component)
    return found


def _order(parents: dict) -> list:
    """Parents before children, ties by key — Kahn's algorithm over in-degrees with the ready set on
    a heap, so a long chain orders in O(n log n) (the re-scan-everything loop this replaced took 14.3 s
    over 16,000 nodes). A cycle, which no log the fold accepted can hold between lifecycles but a
    hand-edited one can, cannot hang it or cost edges it need not: when nothing is ready, the smallest
    key that sits ON a cycle is released — its edges from parents not yet emitted are the only ones
    dropped — and the walk goes on, so a descendant of the cycle keeps every edge it has."""
    waiting: dict = {}
    children: dict = {key: [] for key in parents}
    for key, ups in parents.items():
        real = {up for up in ups if up in parents and up != key}
        waiting[key] = len(real)
        for up in real:
            children[up].append(key)
    ready = [key for key, count in waiting.items() if count == 0]
    heapq.heapify(ready)
    done: set = set()
    ordered: list = []
    while len(ordered) < len(parents):
        if ready:
            key = heapq.heappop(ready)
            if key in done:
                continue
        else:
            stuck = {k for k in parents if k not in done}
            key = min(_on_a_cycle(stuck, parents) or stuck)
        done.add(key)
        ordered.append(key)
        for child in children[key]:
            waiting[child] -= 1
            if waiting[child] == 0 and child not in done:
                heapq.heappush(ready, child)
    return ordered


def champion_id(state) -> Optional[int]:
    """The node branch `champion` points at and `Looplab-Champion` marks: `RunState.best()`, the fold's
    selector pick — the node every other surface crowns. NOT the operator's promote alias
    (`promoted_id`), which an earlier cut preferred: promoting a weaker node crowned it here while the
    real best got no marker, and a promote of a FAILED node checked out code that never scored."""
    best = state.best() if hasattr(state, "best") else None
    return best.id if best is not None else None


def promoted_id(state) -> Optional[int]:
    """The operator's promote alias (`RunState.champion`), published beside the best, never over it."""
    nid = getattr(state, "champion", None)
    return nid if nid is not None and nid in (getattr(state, "nodes", None) or {}) else None


@dataclass(frozen=True)
class FastImport:
    """What `fast_import_stream` built: the stream, and the facts the CLI reports about it."""
    stream: bytes
    commits: int
    superseded: int
    skipped_paths: int
    champion: Optional[int]
    promoted: Optional[int]


@dataclass
class _Context:
    run_id: str
    state: object
    lifecycles: dict
    eligible: set
    hard_flagged: set
    enforced: set
    trust_gate: str
    aborted: set
    champion: Optional[int]
    promoted: Optional[int]
    caveats: tuple
    incomplete: str

    def tag(self, node_id, generation) -> str:
        """The tag the lifecycle `(node_id, generation)` is published under."""
        node = getattr(self.state, "nodes", {}).get(node_id)
        current = node is not None and node.attempt == generation
        tag = node_tag(node_id, None if current else generation)
        return tag if (node_id, generation) in self.lifecycles else f"{tag} (not in the log)"


def _parent_keys(lc: _Lifecycle, lifecycles: dict) -> list:
    """The lifecycles `lc` was built from, once each, in the log's order — `parent_generations` names
    the exact one (0 where a map lacks it, as `/prov` reads it). A node naming ITSELF (a rebuild after
    a reset may, and the fold accepts it) is no edge: its own lifecycle is not its parent."""
    keys = []
    for parent in dict.fromkeys(lc.parent_ids):
        if parent == lc.node_id:
            continue
        key = (parent, lc.parent_generations.get(str(parent), 0))
        if key in lifecycles:
            keys.append(key)
    return keys


def _receipts(node, ctx: _Context) -> list:
    """The current lifecycle's status, metric and the receipts that decide whether the metric counts."""
    status = getattr(node.status, "value", str(node.status))
    notes = []
    if getattr(node, "tombstoned", False):
        notes.append("tombstoned")
    if node.id in ctx.aborted:
        notes.append("aborted")
    if getattr(node, "rerun_from", None):
        notes.append(f"awaiting a rebuild from '{node.rerun_from}'")
    out = [("Looplab-Status", status + (f" ({'; '.join(notes)})" if notes else "")),
           ("Looplab-Metric", _metric_text(node.metric))]
    if node.metric is not None:
        rows = node.violations if isinstance(node.violations, list) else []
        names = sorted({str(r["name"]) for r in rows if isinstance(r, dict) and r.get("name")})
        out += [("Looplab-Feasible", "yes" if node.feasible else "no"),
                ("Looplab-Violations", f"{len(rows)}" + (f" ({', '.join(names)})" if names else "")),
                ("Looplab-Counts-Toward-Best", "yes" if node.id in ctx.eligible else "no")]
        provenance = getattr(node, "metric_provenance", None)
        if isinstance(provenance, dict) and provenance.get("salvaged"):
            out.append(("Looplab-Salvaged", "yes"))
    if node.id in ctx.hard_flagged:
        how = (f"enforced by trust_gate={ctx.trust_gate}" if node.id in ctx.enforced
               else f"recorded, not enforced under trust_gate={ctx.trust_gate}")
        out.append(("Looplab-Trust-Flagged", f"yes ({how})"))
    if node.confirmed_mean is not None:
        out.append(("Looplab-Confirmed-Mean",
                    f"{_metric_text(node.confirmed_mean)} (std {_metric_text(node.confirmed_std)}"
                    f", {node.confirmed_seeds} seeds)"))
    if getattr(node, "holdout_metric", None) is not None:
        out.append(("Looplab-Holdout-Metric", _metric_text(node.holdout_metric)))
    if node.error_reason:
        out.append(("Looplab-Error-Reason", node.error_reason))
    return out


def _message(lc: _Lifecycle, ctx: _Context, skipped: Counter) -> str:
    idea = lc.idea
    operator = _one_line(lc.operator) or "(no operator)"
    title = _one_line(getattr(idea, "rationale", "") or "")[:72] or "(no rationale)"
    node = lc.node
    head = (f"node {lc.node_id}: {operator} — {title}" if node is not None else
            f"node {lc.node_id}, generation {lc.generation} (superseded): {operator} — {title}")
    lines = [head, ""]
    hypothesis = _one_line(getattr(idea, "hypothesis", "") or "")
    if hypothesis:
        lines += [f"Hypothesis: {hypothesis}", ""]
    parents = [ctx.tag(p, lc.parent_generations.get(str(p), 0))
               for p in dict.fromkeys(lc.parent_ids) if p != lc.node_id]
    trailers = [("Looplab-Run", ctx.run_id),
                ("Looplab-Node", lc.node_id),
                ("Looplab-Generation", lc.generation),
                ("Looplab-Operator", operator),
                ("Looplab-Parents", ", ".join(parents) or "none")]
    if node is not None:
        trailers += _receipts(node, ctx)
    else:
        trailers.append(("Looplab-Status",
                         f"superseded — a node_reset from '{lc.reset_stage}' opened "
                         f"{ctx.tag(lc.node_id, lc.generation + 1)}; this lifecycle had reached "
                         f"'{lc.reached}' and is no longer a candidate"))
    params = getattr(idea, "params", None) or {}
    trailers.append(("Looplab-Params", json.dumps(params, sort_keys=True, default=str)))
    if lc.deleted:
        trailers.append(("Looplab-Deleted-From-Base", ", ".join(sorted(map(str, lc.deleted)))))
    if skipped:
        parts = [f"{skipped[key]} {label}" for key, label in _SKIP_REASONS if skipped.get(key)]
        trailers.append(("Looplab-Skipped-Paths",
                         f"{sum(skipped.values())} ({'; '.join(parts)}; the log keeps every one)"))
    if node is not None and lc.node_id == ctx.champion:
        trailers.append(("Looplab-Champion", "yes"))
        if ctx.caveats:
            trailers.append(("Looplab-Champion-Caveats", ", ".join(ctx.caveats)))
    if node is not None and lc.node_id == ctx.promoted:
        trailers.append(("Looplab-Promoted", "yes"))
    if ctx.incomplete:
        trailers.append(("Looplab-Log-Incomplete", ctx.incomplete))
    lines += [f"{key}: {_one_line(value)}" for key, value in trailers]
    return "\n".join(lines) + "\n"


def _materializer_skips(state) -> frozenset:
    """The names `write_node_files` never writes from a node, as far as the LOG knows them: the
    entrypoint, the task assets `data_provenance` pinned at setup, and a ratified onboarding
    adapter (`orchestrator.py::Engine._activate_spec` protects those)."""
    names = {SOLUTION_PATH}
    provenance = getattr(state, "data_provenance", None)
    assets = provenance.get("assets") if isinstance(provenance, dict) else None
    if isinstance(assets, dict):
        names.update(str(name).replace("\\", "/") for name in assets)
    spec = getattr(state, "proposed_spec", None)
    adapters = spec.get("adapter_files") if isinstance(spec, dict) else None
    if getattr(state, "spec_confirmed", False) and isinstance(adapters, dict):
        names.update(str(name).replace("\\", "/") for name in adapters)
    return frozenset(names)


def fast_import_stream(events, state, *, champion_caveats: Iterable[str] = (),
                       log_integrity: Optional[dict] = None) -> FastImport:
    """The `git fast-import` stream (`--done` terminated) for the run's whole lifecycle DAG.

    `events` is the log `state` was folded from. `champion_caveats` are
    `engine/champion_caveats.py::champion_metric_caveats(state)`, which the CALLER computes (this
    package may not import the engine), and `log_integrity` the store's receipt
    (`cli/__init__.py::log_integrity_from`) when the log was read only up to a corrupt line."""
    nodes = dict(getattr(state, "nodes", None) or {})
    superseded, born = node_lifecycles(events)
    lifecycles: dict = {(nid, node.attempt): _lifecycle(node) for nid, node in nodes.items()}
    current = {nid: (nid, node.attempt) for nid, node in nodes.items()}
    for key, lc in superseded.items():
        lifecycles.setdefault(key, lc)
    trust_gate = str(getattr(state, "trust_gate", "") or "audit")
    ctx = _Context(
        run_id=str(getattr(state, "run_id", "") or "unknown"), state=state, lifecycles=lifecycles,
        eligible={n.id for n in promotion_eligible_nodes(state)},
        hard_flagged=set(hard_flagged_ids(state)), enforced=set(flagged_node_ids(state)),
        trust_gate=trust_gate, aborted=set(getattr(state, "aborted_nodes", None) or ()),
        champion=champion_id(state), promoted=promoted_id(state),
        caveats=tuple(str(c) for c in champion_caveats or ()),
        incomplete=integrity_sentence(log_integrity,
                                      run_label=str(getattr(state, "run_id", "") or "this run")))
    skip = _materializer_skips(state)
    out: list[bytes] = []
    blob_marks: dict = {}
    next_mark = [1]

    def mark() -> int:
        value = next_mark[0]
        next_mark[0] += 1
        return value

    def blob(data: bytes) -> int:
        existing = blob_marks.get(data)
        if existing is not None:
            return existing
        m = mark()
        out.append(b"blob\nmark :%d\ndata %d\n" % (m, len(data)) + data + b"\n")
        blob_marks[data] = m
        return m

    identity = GIT_IDENTITY.encode("ascii")
    parents_of = {key: _parent_keys(lc, lifecycles) for key, lc in lifecycles.items()}
    commit_marks: dict = {}
    skipped_total = 0
    for key in _order(parents_of):
        lc = lifecycles[key]
        tree, skipped = lifecycle_tree(lc.code, lc.files, lc.deleted, skip=skip)
        skipped_total += sum(skipped.values())
        files = [(path, blob(tree[path])) for path in sorted(tree)]
        message = _message(lc, ctx, skipped).encode("utf-8")
        # Parents BEFORE this lifecycle's own mark exists, and each once, in the log's order.
        # fast-import accepts a parent named twice (git 2.43), but the commit then lists it twice —
        # `%P` prints the same id twice and `git log --graph` draws a doubled edge for what the DAG
        # holds as one parent. A parent a cycle-break released after this key is simply not here.
        parents = [commit_marks[up] for up in parents_of[key] if up in commit_marks]
        when = born.get(key, 0)
        ref = node_ref(lc.node_id, None if current.get(lc.node_id) == key else lc.generation)
        m = mark()
        commit_marks[key] = m
        header = (b"commit %s\nmark :%d\n" % (ref.encode("ascii"), m)
                  + b"author %s %d +0000\n" % (identity, when)
                  + b"committer %s %d +0000\n" % (identity, when)
                  + b"data %d\n" % len(message) + message)
        body = b""
        if parents:
            body += b"from :%d\n" % parents[0]
            body += b"".join(b"merge :%d\n" % p for p in parents[1:])
        # `deleteall` first: without it a child's tree starts from its FIRST parent's, and every
        # file only the parent had would appear in the child as if the child had written it.
        body += b"deleteall\n"
        body += b"".join(b"M 100644 :%d " % blob_mark + _quoted(path) + b"\n"
                         for path, blob_mark in files)
        out.append(header + body + b"\n")
    for ref, nid in ((CHAMPION_REF, ctx.champion), (PROMOTED_REF, ctx.promoted)):
        if nid is not None and current.get(nid) in commit_marks:
            out.append(b"reset %s\nfrom :%d\n\n" % (ref.encode("ascii"),
                                                    commit_marks[current[nid]]))
    out.append(b"done\n")
    return FastImport(stream=b"".join(out), commits=len(commit_marks),
                      superseded=len(commit_marks) - len(current), skipped_paths=skipped_total,
                      champion=ctx.champion, promoted=ctx.promoted)
