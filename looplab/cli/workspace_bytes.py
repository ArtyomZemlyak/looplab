"""What a run's node workspaces actually WEIGH — the measurement half of `looplab workspace-bytes`.

WHY THIS EXISTS, stated first because the instrument's whole value is the misattribution it retires
(doc 37 §6, §8's R1). A node workspace's only statement in the event log is `workspace_seeded`, and
it says `.[auto]:75 tracked` — an accurate sentence about 0.9 MB, and the sole thing anyone reading
the log can see about a directory that measured 944,779,776 B on `rubertlite-dr-unified-v6` node 4,
937,847,296 of it three intermediate checkpoints plus a final, because that node's OWN trainer said
`save_total_limit=3`. Nothing in LoopLab measures a node workspace after the seed, so the one
visible number named the copy, and the copy got blamed for 727 GB — which is how a whole
`git worktree` migration proposal came to be written against a mechanism responsible for 0.096 % of
the bytes (doc 37, DECLINED with measurement).

So: the SEED CLAIM and the MEASUREMENT printed on the same row, per node.

THE BOUND, because a byte total over a tree is unbounded work and a number that silently truncated
is worse than a refusal. Doc 37 §9 records exactly this as R1's own open problem — "an `os.walk`
over a finished workspace is 168 files for v6 node_4 but is unbounded in general, and a per-node
walk on this mount is not free". One counter, `EntryBudget`, is spent per DIRECTORY ENTRY stat'ed
across the whole run; when it is gone the walk stops, every total it produced becomes a FLOOR that
renders with `>=`, and the rows it never reached are named as NOT WALKED rather than as zero. That
is `tools/_base.py`'s rule for every agent-facing reader — state the range you covered and the call
that continues past it — applied to a filesystem walk instead of a character cap, and the
continuation is a budget the caller has not already spent.

WHAT THE NUMBER IS, so it can be compared with `du`: the APPARENT size, i.e. the sum of file sizes
(`st_size`), not allocated blocks. `du` reports blocks, so it reads LARGER for a tree of many small
files and SMALLER for a sparse one; doc 37's own per-node figures are apparent sizes, and matching
them is what makes this instrument answer that document's question. Symlinks are never followed —
their own link size is counted and their target is not walked — because a `data:` mount is a symlink
into a dataset (189 GiB on the v1 testbed) that the node did not write and must not be billed for,
and because following one is how a walk escapes the run directory it claims to be measuring.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Iterable, Optional

from looplab.events.types import EV_WORKSPACE_SEEDED

# The default the CLI offers. 200k entries is ~1200x the 168 files of the workspace that motivated
# the instrument and still a walk that finishes: it is a bound on WORK, not a guess at a tree size,
# which is why crossing it prints a floor and a continuation instead of a smaller number.
DEFAULT_ENTRY_BUDGET = 200_000


class EntryBudget:
    """The ONE bound on this instrument's work: directory entries stat'ed, across the WHOLE run.

    Deliberately shared by every walk in one report rather than per node: a run whose first node
    holds a million checkpoint shards must not turn into a million entries PER NODE just because the
    per-node bound "looked" small. One counter means the operator sets one number and the report can
    say what that number bought.
    """

    def __init__(self, limit: int = DEFAULT_ENTRY_BUDGET) -> None:
        self.limit = max(0, int(limit))
        self.spent = 0

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.limit

    def take(self) -> bool:
        """Spend one entry. False means the budget is gone and the caller must stop, not skip."""
        if self.spent >= self.limit:
            return False
        self.spent += 1
        return True


@dataclass(frozen=True)
class Walk:
    """One subtree's totals. `truncated` makes `total_bytes`/`files` a FLOOR, never a measurement."""
    total_bytes: int = 0
    files: int = 0
    unreadable: int = 0        # directories/entries the walk could not stat — named, never dropped
    truncated: bool = False

    @property
    def floor(self) -> bool:
        return self.truncated


@dataclass(frozen=True)
class DirMeasure:
    """A directory measured ONE level deep: its own files plus a total per immediate subdirectory.

    One level is what doc 37 §8 R1 asks for ("naming the largest subtrees") and is also the depth
    the incident needed: `checkpoint-1200` beside `checkpoint-800` under a node directory is the row
    that would have named the 937 MB immediately. Deeper attribution is the same walk with a
    different `--node`, not a second reader.
    """
    name: str
    own_bytes: int = 0
    own_files: int = 0
    children: tuple[tuple[str, Walk], ...] = ()
    unwalked: tuple[str, ...] = ()      # subdirectories the budget never reached
    unreadable: int = 0
    truncated: bool = False
    missing: bool = False               # the directory itself could not be opened

    @property
    def total_bytes(self) -> int:
        return self.own_bytes + sum(w.total_bytes for _, w in self.children)

    @property
    def files(self) -> int:
        return self.own_files + sum(w.files for _, w in self.children)


@dataclass(frozen=True)
class NodeRow:
    """One node workspace: what the log CLAIMED at seed time beside what is on disk now."""
    name: str
    node_id: Optional[int]
    measure: DirMeasure
    seeded: Optional[str] = None        # the `workspace_seeded` row's `materialized`, verbatim


@dataclass(frozen=True)
class RunReport:
    run_dir: str
    budget_limit: int
    budget_spent: int
    record_bytes: int = 0               # the run dir's own top-level files: the RECORD, not a node
    record_files: int = 0
    nodes: tuple[NodeRow, ...] = ()
    unwalked_nodes: tuple[str, ...] = ()
    other: tuple[tuple[str, Walk], ...] = ()      # top-level directories that are not `nodes/`
    unwalked_other: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    truncated: bool = False


# ------------------------------------------------------------------------------- the bounded walk

def walk_tree(root, budget: EntryBudget) -> Walk:
    """Total a subtree under `budget`, iteratively and without following a single symlink.

    ITERATIVE on purpose: a recursive walk of a tree deeper than the interpreter's frame limit
    raises `RecursionError` out of a diagnostic, which is the one failure mode a "how big is this"
    command must not have. The stack is pushed in reverse-sorted order so entries are consumed in
    sorted order — a truncated walk under a bigger budget is then a SUPERSET of the smaller one
    rather than a different arbitrary slice.
    """
    total = files = unreadable = 0
    truncated = False
    stack = [Path(root)]
    while stack and not truncated:
        current = stack.pop()
        subdirs: list[Path] = []
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if not budget.take():
                        truncated = True
                        break
                    try:
                        # `follow_symlinks=False` twice, and it is the same decision both times: a
                        # symlinked directory is counted as the link it is (a few bytes) and never
                        # descended into.
                        if entry.is_dir(follow_symlinks=False):
                            subdirs.append(Path(entry.path))
                            continue
                        total += int(entry.stat(follow_symlinks=False).st_size)
                        files += 1
                    except OSError:
                        # A file that vanished between the listing and the stat (a live run reaps
                        # its own temporaries) is one unreadable entry, not a failed report.
                        unreadable += 1
        except OSError:
            unreadable += 1
            continue
        subdirs.sort(reverse=True)
        stack.extend(subdirs)
    return Walk(total_bytes=total, files=files, unreadable=unreadable, truncated=truncated)


def measure_dir(root, budget: EntryBudget, *, name: Optional[str] = None) -> DirMeasure:
    """`root`'s own files plus one `walk_tree` per immediate subdirectory (one level, see above)."""
    root = Path(root)
    label = name if name is not None else root.name
    own_bytes = own_files = unreadable = 0
    truncated = False
    subdirs: list[str] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not budget.take():
                    truncated = True
                    break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        subdirs.append(entry.name)
                        continue
                    own_bytes += int(entry.stat(follow_symlinks=False).st_size)
                    own_files += 1
                except OSError:
                    unreadable += 1
    except OSError:
        return DirMeasure(name=label, missing=True, unreadable=1)

    children: list[tuple[str, Walk]] = []
    unwalked: list[str] = []
    for child in sorted(subdirs):
        # Once the budget is gone the remaining children are NAMED, not walked to a zero: "0 B" and
        # "not measured" are opposite statements and only one of them is true here.
        if truncated or budget.exhausted:
            truncated = True
            unwalked.append(child)
            continue
        walk = walk_tree(root / child, budget)
        children.append((child, walk))
        truncated = truncated or walk.truncated
        unreadable += walk.unreadable
    return DirMeasure(name=label, own_bytes=own_bytes, own_files=own_files,
                      children=tuple(children), unwalked=tuple(unwalked),
                      unreadable=unreadable, truncated=truncated)


# -------------------------------------------------------------------------- the run's own account

def seed_claims(events: Iterable) -> dict[int, str]:
    """`node_id -> the `materialized` list this run RECORDED at seed time`, last write wins.

    The claim half of the report. It is read straight off the run's own log rather than folded,
    because `workspace_seeded` is a diagnostic event that `replay.fold` ignores by design — the
    projection would have nowhere to put it.
    """
    out: dict[int, str] = {}
    for event in events:
        if getattr(event, "type", None) != EV_WORKSPACE_SEEDED:
            continue
        data = getattr(event, "data", None) or {}
        node_id = data.get("node_id")
        if not isinstance(node_id, int) or isinstance(node_id, bool):
            continue
        materialized = data.get("materialized")
        if isinstance(materialized, (list, tuple)):
            out[node_id] = ", ".join(str(x) for x in materialized)
        elif materialized is not None:
            out[node_id] = str(materialized)
    return out


def _node_sort_key(name: str) -> tuple:
    """`node_12` sorts after `node_9`, and anything unparseable sorts last by name."""
    tail = name.rsplit("_", 1)[-1]
    return (0, int(tail), name) if tail.isdigit() else (1, 0, name)


def node_id_of(name: str) -> Optional[int]:
    tail = name.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else None


def measure_run(run_dir, *, budget: EntryBudget, only: Optional[str] = None,
                seeded: Optional[dict[int, str]] = None) -> RunReport:
    """Measure one run directory: the record's own files, then every node workspace, then the rest.

    THE ORDER IS THE POLICY, because it decides what survives a spent budget. The run directory's
    own top-level files (`events.jsonl`, the two snapshots, `engine.lock`, `spans.jsonl`) are a
    handful of entries and are measured first, so the record is always accounted for; the node
    workspaces are the SUBJECT and come next, in node order; every other top-level directory
    (`confirm/`, `ablate/`, `traces/`) is measured last because a budget spent there would answer a
    question nobody asked this command.
    """
    run_dir = Path(run_dir)
    seeded = seeded or {}
    notes: list[str] = []
    record_bytes = record_files = 0
    truncated = False
    top_dirs: list[str] = []

    try:
        with os.scandir(run_dir) as entries:
            for entry in entries:
                if not budget.take():
                    # The budget died while LISTING the run directory: the rows below are not a
                    # small run, they are an unfinished listing, and only this note says which.
                    truncated = True
                    notes.append("the budget ran out while listing the run directory itself — "
                                 "top-level entries are MISSING from everything below")
                    break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        top_dirs.append(entry.name)
                        continue
                    record_bytes += int(entry.stat(follow_symlinks=False).st_size)
                    record_files += 1
                except OSError:
                    notes.append(f"one top-level entry of {run_dir} could not be stat'ed")
    except OSError:
        notes.append(f"{run_dir} could not be listed at all — no measurement was taken")
        return RunReport(run_dir=str(run_dir), budget_limit=budget.limit, budget_spent=budget.spent,
                         notes=tuple(notes), truncated=True)

    nodes_dir = run_dir / "nodes"
    node_names: list[str] = []
    if "nodes" in top_dirs:
        try:
            with os.scandir(nodes_dir) as entries:
                for entry in entries:
                    if not budget.take():
                        truncated = True
                        notes.append("the budget ran out while listing nodes/ — node workspaces "
                                     "are MISSING from the table below, not absent from disk")
                        break
                    # A node workspace is a real directory. A symlink wearing a node's name is not
                    # walked (see the module header) and is reported rather than silently skipped.
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            node_names.append(entry.name)
                        elif entry.is_symlink():
                            notes.append(f"nodes/{entry.name} is a symlink — not followed, "
                                         "not measured")
                    except OSError:
                        notes.append(f"nodes/{entry.name} could not be stat'ed")
        except OSError:
            notes.append("nodes/ could not be listed")
    else:
        notes.append("this run has no nodes/ directory — nothing was ever materialized, or the "
                     "workspaces have already been removed")

    node_names.sort(key=_node_sort_key)
    if only is not None:
        wanted = only if only.startswith("node_") else f"node_{only}"
        if wanted not in node_names:
            notes.append(f"--node {only}: nodes/{wanted} is not a directory in this run")
            node_names = []
        else:
            node_names = [wanted]

    rows: list[NodeRow] = []
    unwalked_nodes: list[str] = []
    for name in node_names:
        if truncated or budget.exhausted:
            truncated = True
            unwalked_nodes.append(name)
            continue
        measure = measure_dir(nodes_dir / name, budget)
        truncated = truncated or measure.truncated
        rows.append(NodeRow(name=name, node_id=node_id_of(name), measure=measure,
                            seeded=seeded.get(node_id_of(name)) if node_id_of(name) is not None
                            else None))

    other: list[tuple[str, Walk]] = []
    unwalked_other: list[str] = []
    for name in sorted(d for d in top_dirs if d != "nodes"):
        if truncated or budget.exhausted:
            truncated = True
            unwalked_other.append(name)
            continue
        walk = walk_tree(run_dir / name, budget)
        other.append((name, walk))
        truncated = truncated or walk.truncated

    return RunReport(run_dir=str(run_dir), budget_limit=budget.limit, budget_spent=budget.spent,
                     record_bytes=record_bytes, record_files=record_files, nodes=tuple(rows),
                     unwalked_nodes=tuple(unwalked_nodes), other=tuple(other),
                     unwalked_other=tuple(unwalked_other), notes=tuple(notes), truncated=truncated)


# -------------------------------------------------------------------------------------- rendering

def human(n: int) -> str:
    """`944,779,776 B (901.0 MiB)` — the exact byte count first, because doc 37 quotes bytes."""
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{n:,} B ({value:.1f} {unit})" if unit != "B" else f"{n:,} B"
        value /= 1024
    return f"{n:,} B"


def render_workspace_bytes(report: RunReport, *, top: int = 3) -> list[str]:
    """The report as lines. Every total a spent budget made a floor is printed with `>=`."""
    ge = ">=" if report.truncated else ""
    out = [f"workspace bytes for {report.run_dir} — apparent size (sum of file sizes, what doc 37 "
           "quotes), symlinks NOT followed",
           f"entry budget: {report.budget_spent:,} of {report.budget_limit:,} directory entries "
           + ("SPENT — the walk stopped early (see the bottom)" if report.truncated
              else "spent; the walk COMPLETED")]
    for note in report.notes:
        out.append(f"  note: {note}")

    node_bytes = sum(row.measure.total_bytes for row in report.nodes)
    node_files = sum(row.measure.files for row in report.nodes)
    other_bytes = sum(w.total_bytes for _, w in report.other)
    other_files = sum(w.files for _, w in report.other)
    def _row(label: str, count: int, files: int, floor: str = ge) -> str:
        return f"{label:<22}{floor + format(count, ','):>22}{files:>10}"

    out.append("")
    out.append(f"{'where':<22}{'bytes':>22}{'files':>10}")
    out.append(_row("(the record)", report.record_bytes, report.record_files))
    out.append(_row("nodes/", node_bytes, node_files))
    for name, walk in report.other:
        out.append(_row(f"{name}/", walk.total_bytes, walk.files,
                        ">=" if walk.truncated or report.truncated else ""))
    total = report.record_bytes + node_bytes + other_bytes
    out.append(_row("run total", total, report.record_files + node_files + other_files))
    out.append(f"  run total = {ge}{human(total)}. `(the record)` is the run directory's own "
               "top-level files: the event log, the snapshots, the lock.")

    if report.nodes:
        out.append("")
        out.append(f"node workspaces, largest first (top {top} subtree(s) each):")
        for row in sorted(report.nodes, key=lambda r: -r.measure.total_bytes):
            mark = ">=" if row.measure.truncated else ""
            out.append(f"  {row.name}: {mark}{human(row.measure.total_bytes)} in "
                       f"{mark}{row.measure.files:,} file(s)")
            biggest = sorted(row.measure.children, key=lambda kv: -kv[1].total_bytes)[:max(0, top)]
            for child_name, walk in biggest:
                child_mark = ">=" if walk.truncated else ""
                out.append(f"      {child_name + '/':<28}{child_mark}{human(walk.total_bytes)}"
                           f"  in {child_mark}{walk.files:,} file(s)")
            if len(row.measure.children) > len(biggest):
                out.append(f"      … {len(row.measure.children) - len(biggest)} more subtree(s), "
                           f"smaller than the ones above (raise --top to see them)")
            if row.measure.own_files:
                out.append(f"      {'(files at the top)':<28}{human(row.measure.own_bytes)}"
                           f"  in {row.measure.own_files:,} file(s)")
            if row.measure.unwalked:
                out.append(f"      NOT WALKED (budget spent): {', '.join(row.measure.unwalked)}")
            if row.measure.unreadable:
                out.append(f"      {row.measure.unreadable} entry/entries could not be read")
            # THE POINT OF THE WHOLE COMMAND (doc 37 §6): the log's only workspace sentence, printed
            # against the measurement, so "0.9 MB of seed" can never again be read as the size of a
            # directory that is three orders of magnitude bigger.
            claim = row.seeded if row.seeded is not None else (
                "— no workspace_seeded row for this node in the log")
            out.append(f"      seeded (the log's only workspace fact): {claim}")

    if report.unwalked_nodes or report.unwalked_other:
        out.append("")
        if report.unwalked_nodes:
            out.append("NOT WALKED AT ALL (budget spent before they were reached): "
                       + ", ".join(report.unwalked_nodes))
        if report.unwalked_other:
            out.append("top-level directories not walked: " + ", ".join(report.unwalked_other))

    if report.truncated:
        # The continuation must be a call the caller has NOT already spent (`tools/_base.py`), so it
        # names a budget strictly larger than the one that just ran out, and the narrower call that
        # spends the same budget on one node.
        out.append("")
        out.append(f"BUDGET SPENT after {report.budget_spent:,} entries: every number above is a "
                   "FLOOR (>=), not a measurement.")
        out.append(f"  continue:  looplab workspace-bytes {report.run_dir} "
                   f"--max-entries {max(2 * report.budget_limit, 1)}")
        out.append(f"  or spend the whole budget on one node:  looplab workspace-bytes "
                   f"{report.run_dir} --node <id>")
    return out
