"""The run's node DAG as GIT HISTORY (doc 67 67.15): one commit per node, its `parent_ids` as the
commit's parents, the node's own files as the tree, the metric and receipts in the message.

WHY. Every node's code is already in the event log — `node_created` carries `code`, `files` and
`deleted` — but the only way to read two nodes side by side was LoopLab's own `tools/node_diff.py`
or the UI. The engineer's tools — `git log --graph`, `git diff node-3 node-7`, `git bisect` over a
regression between two measured nodes, `git blame` on the champion's training loop — need a
repository. This is a PROJECTION, never a second source of truth: a pure function of the log and its
fold, re-derived on every call and never read back, so `events.jsonl` stays authoritative (the
OpenResearch pattern the item cites keeps git as the source; LoopLab keeps the log).

WHAT A COMMIT'S TREE IS, AND IS NOT. Every materialization is the task's base tree seeded first,
then the node's `files` on top, then the task's assets (`engine/workspace.py::WorkspaceSeeder.
materialize`) — so `files` is the node's WHOLE edit set relative to that base, not a delta over its
parent, and a commit's tree is exactly those files, plus `solution.py` for a node with `code`. The
base tree itself is not in the log (doc 67 67.12, `repo-base-tree-not-archived`), so it is not here
either; a node's `deleted` names are removals from that base and ride in the message. Task assets
belong to the task, not the node, and are left out.

WHAT IT REFUSES. A path a checkout could turn against its reader — absolute, `..`, a `.git`
component, a NUL — is left out of the tree and COUNTED in the message (`Looplab-Skipped-Paths`),
never written: the export may be cloned by someone who never saw the run.

DETERMINISTIC. Parents before children (ties by node id), paths sorted, each commit dated by its
node's first `node_created` row, one fixed identity — so one log exports to the same commit ids
every time, and a reviewer can pin one.
"""
from __future__ import annotations

import json
from typing import Iterable, Optional

from looplab.core.jsonutil import surrogate_safe

GIT_IDENTITY = "LoopLab <looplab@invalid>"
SOLUTION_PATH = "solution.py"
CHAMPION_REF = "refs/heads/champion"
# The longest path kept; git's own limit is the platform's, and a longer one is no source file.
_MAX_PATH_BYTES = 4096
# A trailer value past this is cut and says so: `Looplab-Params` is the only unbounded one.
_MAX_TRAILER_CHARS = 2000


def node_ref(node_id: int) -> str:
    """The lightweight tag each node's commit is published under."""
    return f"refs/tags/node-{int(node_id)}"


def safe_tree_path(path) -> Optional[str]:
    """`path` as a tree path a checkout cannot turn against its reader, or ``None``."""
    if not isinstance(path, str) or not path:
        return None
    norm = surrogate_safe(path).replace("\\", "/")
    parts = norm.split("/")
    if (norm.startswith("/") or "\x00" in norm or any(part in ("", ".", "..") for part in parts)
            or any(part.lower() == ".git" for part in parts)
            or (len(parts[0]) >= 2 and parts[0][1] == ":")          # a Windows drive, `C:x`
            or len(norm.encode("utf-8")) > _MAX_PATH_BYTES):
        return None
    return norm


def _quoted(path: str) -> bytes:
    """`path` C-quoted the way fast-import reads a quoted path — always quoted, which is simpler
    than deciding when a space, a quote or a newline would need it."""
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
    text = " ".join(surrogate_safe(str(value)).split())
    return text if len(text) <= _MAX_TRAILER_CHARS else text[:_MAX_TRAILER_CHARS] + " … (cut)"


def _metric_text(value) -> str:
    return "none" if value is None else repr(float(value))


def _birth_times(events: Iterable) -> dict:
    """{node id: unix seconds of its FIRST `node_created` row}."""
    from looplab.events.types import EV_NODE_CREATED

    born: dict = {}
    for event in events or ():
        if getattr(event, "type", None) != EV_NODE_CREATED:
            continue
        node_id = (event.data or {}).get("node_id")
        if isinstance(node_id, int) and not isinstance(node_id, bool) and node_id not in born:
            try:
                born[node_id] = max(0, int(float(event.ts or 0)))
            except (TypeError, ValueError, OverflowError):
                born[node_id] = 0
    return born


def _order(nodes: dict) -> list:
    """Parents before children, ties by id — Kahn's algorithm over the parent edges this DAG holds.
    A cycle no well-formed log contains cannot hang it: what is left is emitted in id order."""
    remaining = {nid: {p for p in node.parent_ids if p in nodes and p != nid}
                 for nid, node in nodes.items()}
    ordered: list = []
    while remaining:
        ready = sorted(nid for nid, parents in remaining.items() if not parents)
        if not ready:
            ready = sorted(remaining)                 # a cycle: break it deterministically
        for nid in ready:
            ordered.append(nid)
            del remaining[nid]
        for parents in remaining.values():
            parents.difference_update(ready)
    return ordered


def _message(state, node, *, champion_id, skipped: int) -> str:
    idea = node.idea
    title = " ".join(surrogate_safe(str(getattr(idea, "rationale", "") or "")).split())
    lines = [f"node {node.id}: {node.operator} — {title[:72] or '(no rationale)'}", ""]
    hypothesis = " ".join(surrogate_safe(str(getattr(idea, "hypothesis", "") or "")).split())
    if hypothesis:
        lines += [f"Hypothesis: {hypothesis}", ""]
    status = node.status.value + (" (tombstoned)" if getattr(node, "tombstoned", False) else "")
    trailers = [
        ("Looplab-Run", state.run_id or "unknown"),
        ("Looplab-Node", node.id),
        ("Looplab-Attempt", getattr(node, "attempt", 0)),
        ("Looplab-Operator", node.operator),
        ("Looplab-Parents", ", ".join(str(p) for p in node.parent_ids) or "none"),
        ("Looplab-Status", status),
        ("Looplab-Metric", _metric_text(node.metric)),
    ]
    if node.confirmed_mean is not None:
        trailers.append(("Looplab-Confirmed-Mean",
                         f"{_metric_text(node.confirmed_mean)} (std {_metric_text(node.confirmed_std)}"
                         f", {node.confirmed_seeds} seeds)"))
    if getattr(node, "holdout_metric", None) is not None:
        trailers.append(("Looplab-Holdout-Metric", _metric_text(node.holdout_metric)))
    if node.error_reason:
        trailers.append(("Looplab-Error-Reason", node.error_reason))
    params = getattr(idea, "params", None) or {}
    trailers.append(("Looplab-Params", json.dumps(params, sort_keys=True, default=str)))
    if node.deleted:
        trailers.append(("Looplab-Deleted-From-Base", ", ".join(sorted(map(str, node.deleted)))))
    if skipped:
        trailers.append(("Looplab-Skipped-Paths", f"{skipped} (unsafe in a checkout; see the log)"))
    if node.id == champion_id:
        trailers.append(("Looplab-Champion", "yes"))
    lines += [f"{key}: {_one_line(value)}" for key, value in trailers]
    return "\n".join(lines) + "\n"


def champion_id(state) -> Optional[int]:
    """The node the champion branch points at: the promoted champion, else the fold's best."""
    if getattr(state, "champion", None) is not None and state.champion in state.nodes:
        return state.champion
    best = state.best() if hasattr(state, "best") else None
    return best.id if best is not None else None


def fast_import_stream(events, state) -> bytes:
    """The `git fast-import` stream for the run's whole node DAG (`--done` terminated)."""
    nodes = dict(state.nodes or {})
    born = _birth_times(events)
    winner = champion_id(state)
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

    commit_marks: dict = {}
    for nid in _order(nodes):
        node = nodes[nid]
        tree: dict = {}
        skipped = 0
        for path, content in (node.files or {}).items():
            safe = safe_tree_path(path)
            if safe is None:
                skipped += 1
                continue
            tree[safe] = surrogate_safe(str(content)).encode("utf-8")
        if node.code and SOLUTION_PATH not in tree:
            tree[SOLUTION_PATH] = surrogate_safe(str(node.code)).encode("utf-8")
        files = [(path, blob(tree[path])) for path in sorted(tree)]
        message = _message(state, node, champion_id=winner, skipped=skipped).encode("utf-8")
        when = born.get(nid, 0)
        m = mark()
        commit_marks[nid] = m
        header = (b"commit %s\nmark :%d\n" % (node_ref(nid).encode("ascii"), m)
                  + b"author %s %d +0000\n" % (GIT_IDENTITY.encode("ascii"), when)
                  + b"committer %s %d +0000\n" % (GIT_IDENTITY.encode("ascii"), when)
                  + b"data %d\n" % len(message) + message)
        # Once each, in the log's order: fast-import refuses a commit naming one parent twice.
        parents = list(dict.fromkeys(commit_marks[p] for p in node.parent_ids if p in commit_marks))
        body = b""
        if parents:
            body += b"from :%d\n" % parents[0]
            body += b"".join(b"merge :%d\n" % p for p in parents[1:])
        body += b"deleteall\n"
        body += b"".join(b"M 100644 :%d " % blob_mark + _quoted(path) + b"\n"
                         for path, blob_mark in files)
        out.append(header + body + b"\n")
    if winner is not None and winner in commit_marks:
        out.append(b"reset %s\nfrom :%d\n\n" % (CHAMPION_REF.encode("ascii"), commit_marks[winner]))
    out.append(b"done\n")
    return b"".join(out)
