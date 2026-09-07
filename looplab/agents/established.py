"""The "already established" block — what earlier phases of THIS run already retrieved.

Every phase of the loop (Researcher `propose`/`repropose`, Developer `stages` -> `plan` ->
`plan_step`s, a repair session) starts a FRESH chain, and a fresh chain re-reads the same files:
the reference implementation, the manifest, the config, the timings. Measured over 97 AlgoTune
probe runs (docs/56 §200.2): 25,381 tool-calling turns carried $63.34 of prompt, of which 11,853
(46.7 %) requested NOTHING but content already retrieved in that run — $25.04 — and 11,235 of
those were retrieved by a DIFFERENT phase. The model asking itself twice is rare; the loop
throwing its context away between phases is the whole of it. And a turn is a minute of wall
clock, which is the half that matters more than the money.

The remedy is DATA, not a rule. `agents/answered_by_context.py` is the precedent: the same
knowledge as a prompt RULE ("read a file ONCE, don't re-read" has been in the plan prompt since
it was written) moved nothing, while the inventory as DATA moved 41.3 -> 17.7 calls. So this is a
small block, seeded ONCE per chain, carrying the content of the most re-fetched items verbatim
under a byte budget and an index row for the rest — never a wider read page (§162 measured a
bigger page at -$24) and never the whole reference file in every generation (§165 measured that
at break-even).

WHAT IT IS NOT. It changes no tool: a re-read still works exactly as before, every cap and every
refusal is untouched, and nothing here reaches a metric, a champion, a violation or a selection.
The block is prompt-side only, and `Settings.established_context=False` (the LEGACY snapshot
default, so a resumed run gains no prompt bytes) restores every prompt byte for byte — as does an
empty store, which is why a phase that read nothing renders nothing.

One store per RUN, shared by every role the factory builds (the Developer's phases run on worker
threads under `llm_parallel`, hence the lock); it records through the tool loop's per-call
`on_tool_result` hook, keyed on `(tool, path)` the way `tool_loop._READ_TOOL_PATH_SLOTS` keys the
read-loop nudge — the two are one reading of "which tools return a file".
"""
from __future__ import annotations

import hashlib
import threading
from typing import Callable, Optional

# The readers whose result is a FILE (or a bounded sample of one) and the argument that names
# it. Kept in step with `agents/tool_loop._READ_TOOL_PATH_SLOTS` by `tests/test_established_context.py`.
READ_TOOL_PATH_SLOTS: dict[str, str] = {
    "read_file": "path",
    "repo_read": "path",
    "read_installed": "module",
    "read_asset": "name",
}
# …and the WRITERS, because a page carried forward under "do not re-fetch" must never be the
# version before an edit. THIS IS THE DEFECT THE PREDECESSOR OF THIS BLOCK WAS REMOVED FOR (the
# G2 read-dedup cache, P3): a file read in `plan`, rewritten in `plan_step`, and then seeded into
# the next phase as its "first page verbatim" is a confident lie about the working set — and the
# store is per RUN, so without this the same page would cross NODE boundaries too. A write drops
# the CONTENT and keeps the row: "you read this N times and it has changed since" is true and
# useful, "here is what it said" is not. `looplab_stages.json` is written through `declare_stages`
# rather than these three, and is registered here for the same reason.
WRITE_TOOL_PATH_SLOTS: dict[str, str] = {
    "write_file": "path",
    "edit_file": "path",
    "delete_file": "path",
}
# A tool-loop note appended to a result (`_REPEAT_NOTE`, `_READ_LOOP_NOTE`) is advice to the
# model that made the call, not content of the file; it is stripped before the page is kept.
_NOTE_MARK = "\n(note: "
# The reader's OWN refusal vocabulary (`tools/reposcout.REFUSAL_PREFIXES`, the tuple
# `read_file_checked` decides by), not a two-prefix guess: a refusal stored as content renders
# `(no such file: x)` under a header promising the file's first page verbatim.
_MAX_INDEX_ROWS = 24
DEFAULT_BUDGET_BYTES = 12288
DEFAULT_ITEM_BYTES = 6144

_HEADER = (
    "=== ALREADY ESTABLISHED IN THIS RUN (retrieved by earlier phases; do not re-fetch) ===\n"
    "Each item below was read by an earlier phase of this same run. Where its content follows, "
    "it is carried verbatim, so a call for it is a call you do not need to spend; where only an "
    "index row appears, the content was too large to carry — re-read it ONCE with the call named "
    "and then work from your copy.\n")


def _canonical_path(raw) -> Optional[str]:
    if raw is None:
        return None
    path = str(raw).replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    return path or None


def _is_whole_or_first_page(args: dict) -> bool:
    """A read with no window, or one that starts at the top, is the page worth carrying: a later
    window is a fragment whose meaning depends on the marker that led to it."""
    start = (args or {}).get("start_line")
    lines = (args or {}).get("lines")
    try:
        start_ok = start is None or int(start) in (0, 1)
    except (TypeError, ValueError):
        start_ok = False
    try:
        lines_ok = lines is None or int(lines) <= 0
    except (TypeError, ValueError):
        lines_ok = False
    return start_ok and lines_ok


def _strip_notes(result: str) -> str:
    text = str(result or "")
    cut = text.find(_NOTE_MARK)
    return text if cut < 0 else text[:cut]


class EstablishedContext:
    """The per-run ledger of file reads, and the block a new chain is seeded with."""

    def __init__(self, budget_bytes: int = DEFAULT_BUDGET_BYTES,
                 item_bytes: int = DEFAULT_ITEM_BYTES):
        self.budget_bytes = max(0, int(budget_bytes))
        self.item_bytes = max(0, int(item_bytes))
        self._lock = threading.Lock()
        # key -> {"tool", "path", "count", "phases": [..], "content": str | None, "sha": str | None}
        self._items: dict[tuple[str, str], dict] = {}
        self._order: list[tuple[str, str]] = []

    # ---- recording -------------------------------------------------------------------------
    def record(self, tool: str, args: dict, result: str, *, phase: str = "") -> bool:
        """Record one read-tool result. Returns True when it was a read of a registered reader."""
        slot = READ_TOOL_PATH_SLOTS.get(str(tool or ""))
        if slot is None:
            return False
        path = _canonical_path((args or {}).get(slot))
        if path is None:
            return False
        text = _strip_notes(result)
        # A refusal or an error is not established content — the model was told the file could not
        # be read, and carrying that sentence forward would assert it twice, under a header saying
        # it is the file. Decided by the READER's own vocabulary, imported rather than re-listed.
        from looplab.tools.reposcout import REFUSAL_PREFIXES
        stripped = text.lstrip()
        if not text.strip() or stripped.startswith("(error") or stripped.startswith(REFUSAL_PREFIXES):
            return False
        key = (str(tool), path)
        with self._lock:
            item = self._items.get(key)
            if item is None:
                item = {"tool": str(tool), "path": path, "count": 0, "phases": [],
                        "content": None, "sha": None, "changed": False}
                self._items[key] = item
                self._order.append(key)
            item["count"] += 1
            if phase and phase not in item["phases"]:
                item["phases"].append(phase)
            if item["content"] is None and _is_whole_or_first_page(args) \
                    and len(text.encode("utf-8")) <= self.item_bytes:
                # A re-read AFTER a write is the CURRENT bytes, so it may be carried again and the
                # row stops being "changed since" — the flag describes the carried page, not history.
                item["content"] = text
                item["sha"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
                item["changed"] = False
        return True

    def invalidate(self, tool: str, args: dict) -> bool:
        """A write/edit/delete of a carried path drops its CONTENT and marks the row changed.

        Returns True when the call was a registered writer (whether or not anything was carried),
        so the hook can tell "handled" from "not a file tool"."""
        slot = WRITE_TOOL_PATH_SLOTS.get(str(tool or ""))
        if slot is None:
            return False
        path = _canonical_path((args or {}).get(slot))
        if path is None:
            return True
        with self._lock:
            for key, item in self._items.items():
                if key[1] == path:
                    item["content"] = None
                    item["sha"] = None
                    item["changed"] = True
        return True

    def hook(self, phase: str, inner: Optional[Callable] = None) -> Callable:
        """An `on_tool_result(name, args, result)` for `drive_tool_loop`, composed over an
        existing one so a caller that already observes results keeps observing them."""
        def _on_tool_result(name, args, result):
            # The write half first: a tool is one or the other, and a writer must drop the page it
            # invalidates even on the turn that also re-reads it.
            if not self.invalidate(name, args):
                self.record(name, args, result, phase=phase)
            if inner is not None:
                inner(name, args, result)
        return _on_tool_result

    # ---- reading ---------------------------------------------------------------------------
    def items(self) -> list[dict]:
        """Most re-fetched first, then in first-seen order — the item worth carrying is the one
        the run keeps paying for."""
        with self._lock:
            rows = [dict(self._items[k], phases=list(self._items[k]["phases"])) for k in self._order]
        return sorted(rows, key=lambda r: -r["count"])

    def render(self) -> str:
        """The block, or "" when nothing was recorded (so an untouched prompt stays byte-identical).

        Content is carried under `budget_bytes` in the order above; everything past the budget, and
        every item whose first page was never read whole, gets an index row naming the exact call
        that re-reads it — the remedy a caller has not already spent (`tools/log_tools.py` rule 3)."""
        rows = self.items()
        if not rows:
            return ""
        out = [_HEADER]
        used = len(_HEADER.encode("utf-8"))
        # EVERY line is charged, index rows included, and the rows are capped. An index row is
        # cheap and there is no bound on how many distinct paths a run reads: uncharged, the block
        # grew linearly with the run and was pasted into every chain root afterwards, which is the
        # opposite of what it is for. Over the budget the remainder becomes one counted line.
        indexed = 0
        omitted = 0
        for row in rows:
            phases = ", ".join(row["phases"]) or "an earlier phase"
            times = f"read {row['count']}x" + (f" across {phases}" if row["phases"] else "")
            slot = READ_TOOL_PATH_SLOTS[row["tool"]]
            call = f"{row['tool']}({slot}=\"{row['path']}\")"
            content = row["content"]
            if content is not None:
                body = (f"\n--- `{row['path']}` ({row['tool']}; {times}; first page verbatim, "
                        f"sha {row['sha']}) ---\n{content}\n")
                size = len(body.encode("utf-8"))
                if used + size <= self.budget_bytes:
                    out.append(body)
                    used += size
                    continue
            why = ("CHANGED since you read it — re-read" if row.get("changed")
                   else "not carried — re-read once")
            line = f"\n- `{row['path']}` ({row['tool']}; {times}; {why} with `{call}`)\n"
            size = len(line.encode("utf-8"))
            if indexed >= _MAX_INDEX_ROWS or used + size > self.budget_bytes:
                omitted += 1
                continue
            out.append(line)
            used += size
            indexed += 1
        if omitted:
            out.append(f"\n- (+{omitted} more file(s) read earlier in this run, not listed)\n")
        return "".join(out)


def established_context_from_settings(settings) -> Optional[EstablishedContext]:
    """The ONE store a run's roles share, or None when the setting is off. Built by
    `agents/factory.py::make_roles` and handed to every role that drives a tool loop."""
    if not bool(getattr(settings, "established_context", False)):
        return None
    return EstablishedContext(
        budget_bytes=int(getattr(settings, "established_context_bytes", DEFAULT_BUDGET_BYTES)))
