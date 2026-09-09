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

# The readers whose result is a FILE, and the ledger key for one of their calls: BOTH imported from
# `agents/tool_loop.py`, which owns them. This module carried a four-entry copy of that table and a
# line-for-line copy of `_canonical_read_path`'s body, held in step by a test — so a fifth reader
# added to the loop's table would have gone unrecorded here until a human read the failure.
# `tool_loop` imports nothing from this module, so the direction is safe. The derived view is
# `tool -> slot`, which is what the renderer needs to name the call that re-reads.
from looplab.agents.tool_loop import (_READ_TOOL_PATH_SLOTS, _canonical_read_path,  # noqa: E402
                                      _note_heads)

READ_TOOL_PATH_SLOTS: dict[str, str] = {tool: slot
                                        for tool, (slot, _paged) in _READ_TOOL_PATH_SLOTS.items()}
# …and the WRITERS, because a page carried forward under "do not re-fetch" must never be the
# version before an edit. THIS IS THE DEFECT THE PREDECESSOR OF THIS BLOCK WAS REMOVED FOR (the
# G2 read-dedup cache, P3): a file read in `plan`, rewritten in `plan_step`, and then seeded into
# the next phase as its "first page verbatim" is a confident lie about the working set. A write
# drops the CONTENT and keeps the row: "you read this N times and it has changed since" is true and
# useful, "here is what it said" is not.
#
# `declare_stages` is the ONLY writer of `repo_write_tools.STAGES_MANIFEST` and it takes no path
# argument at all — it writes that one constant — so it carries the CONSTANT as its slot rather
# than an argument name. The comment here CLAIMED it was registered while the table held three
# entries, which made the manifest the one page nothing in the run could ever invalidate: driven,
# a `read_file("looplab_stages.json")` followed by `declare_stages` still rendered the
# pre-declaration bytes under "first page verbatim, sha …".
_STAGES_MANIFEST_SLOT = object()        # "no argument: this writer's path is a fixed constant"
WRITE_TOOL_PATH_SLOTS: dict = {
    "write_file": "path",
    "edit_file": "path",
    "delete_file": "path",
    "declare_stages": _STAGES_MANIFEST_SLOT,
}
# A tool-loop note appended to a result is advice to the MODEL that made the call, not content of
# the file, and is stripped before the page is kept. Two rules, each from a driven counterexample:
#
#   * the heads come from the loop's OWN note constants (`tool_loop._note_heads`), never a retyped
#     `"\n(note: "`. That literal missed `_TRUNC_NOTE`, which opens `"\n…[truncated by the
#     tool-result cap"`, so a result the cap had CUT was stored, hashed and rendered as the file's
#     first page verbatim with the cap's own sentence inside it.
#   * a note is ONE LINE at the TAIL. Cutting at the FIRST occurrence anywhere truncated any file
#     whose own bytes contain such a line: driven, a 68-byte file with `(note: this is part of the
#     FILE)` on its second line was stored as its first 8 bytes, under a header promising the file
#     verbatim and a sha computed over the truncation.
# The reader's OWN refusal vocabulary (`tools/reposcout.REFUSAL_PREFIXES`, the tuple
# `read_file_checked` decides by) is what `record` rejects by, not a two-prefix guess: a refusal
# stored as content renders `(no such file: x)` under that same header.
_MAX_INDEX_ROWS = 24
DEFAULT_BUDGET_BYTES = 12288
DEFAULT_ITEM_BYTES = 6144

_HEADER = (
    "=== ALREADY ESTABLISHED IN THIS RUN (retrieved by earlier phases; do not re-fetch) ===\n"
    "Each item below was read by an earlier phase of this same run. Where its content follows, "
    "it is carried verbatim, so a call for it is a call you do not need to spend; where only an "
    "index row appears, the content was too large to carry — re-read it ONCE with the call named "
    "and then work from your copy.\n")




def _is_whole_or_first_page(args: dict) -> bool:
    """A read with no window, or one that starts at the top, is the page worth carrying: a later
    window is a fragment whose meaning depends on the marker that led to it."""
    start = (args or {}).get("start_line")
    # `max_lines` is `read_installed`'s documented legacy alias for `lines` (`tools/env_inspect.py`:
    # "older transcripts/models still pass it"), and a window arriving under it read as "no window
    # at all" — so a 20-line slice was stored, hashed and rendered as the file's first page verbatim.
    lines = (args or {}).get("lines")
    if lines is None:
        lines = (args or {}).get("max_lines")
    try:
        start_ok = start is None or int(start) in (0, 1)
    except (TypeError, ValueError):
        start_ok = False
    try:
        lines_ok = lines is None or int(lines) <= 0
    except (TypeError, ValueError):
        lines_ok = False
    return start_ok and lines_ok


def _strip_notes(result: str) -> tuple[str, bool]:
    """The result with the loop's trailing notes removed, and whether the cap had TRUNCATED it.

    A truncated page is never carried: those bytes are a prefix the cap chose, and a header calling
    that "the first page verbatim" is precisely the claim this block must not make.
    """
    text = str(result or "")
    truncated = False
    cutting = True
    while cutting:
        cutting = False
        for head, is_truncation in _note_heads():
            cut = text.rfind(head)
            # ONE LINE at the tail: everything from the head onward must be the note itself.
            if cut < 0 or "\n" in text[cut + 1:]:
                continue
            text, truncated, cutting = text[:cut], truncated or is_truncation, True
    return text, truncated


def _same_file(recorded: str, written: str) -> bool:
    """Does a write of `written` invalidate a page read as `recorded`?

    COMPONENT-wise and either direction, because the two spellings legitimately differ:
    `tools/reposcout.py` resolves an ABSOLUTE sandbox path (`…/nodes/node_59/solver.py`) onto the
    overlay key `solver.py`, so a page read absolutely and rewritten relatively — or the reverse —
    compared unequal and stayed carried. Over-invalidating only costs a re-read; under-invalidating
    serves the pre-write bytes, which is the whole defect. Component-wise, never a bare prefix, so
    `models/run` cannot be matched by `models/run-v7`.
    """
    def _parts(raw) -> list:
        return [p for p in str(raw or "").replace("\\", "/").strip().split("/")
                if p not in ("", ".")]

    a, b = _parts(recorded), _parts(written)
    if not a or not b:
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return long[-len(short):] == short


def _refusal_prefixes() -> tuple:
    """Every registered reader's OWN refusal vocabulary, imported rather than re-listed.

    `reposcout.REFUSAL_PREFIXES` is `read_file`/`repo_read`'s and covers two of the four readers;
    the other two answer in their own words — `read_asset` with `DataTools`' "(this task has NO data
    assets…" / "(no asset '…'", `read_installed` with "(read_installed: cannot locate …". None of
    those starts with a reposcout prefix, so each was stored, HASHED and rendered as
    "first page verbatim", which is the exact defect the filter was written to close, surviving for
    half the table. Resolved lazily and defensively: this module is imported by the factory before
    the tool packages are necessarily importable in every test shape, and a provider that cannot be
    imported must not make the store refuse to record anything.
    """
    out = ["(error"]
    try:
        from looplab.tools.reposcout import REFUSAL_PREFIXES
        out.extend(REFUSAL_PREFIXES)
    except Exception:  # noqa: BLE001 - a missing provider is not a reason to store its refusals
        pass
    # `read_asset` (tools/run_tools.py) and `read_installed` (tools/env_inspect.py) name themselves
    # in their refusals, which is what makes these prefixes exact rather than a guess.
    out.extend(("(this task has NO data assets", "(no asset ", "(read_installed:", "(refused:"))
    return tuple(out)


class EstablishedContext:
    """The per-run ledger of file reads, and the block a new chain is seeded with."""

    def __init__(self, budget_bytes: int = DEFAULT_BUDGET_BYTES,
                 item_bytes: int = DEFAULT_ITEM_BYTES):
        self.budget_bytes = max(0, int(budget_bytes))
        # DERIVED, never larger than the budget: an operator lowering `established_context_bytes`
        # below the item cap got a store that still ACCEPTED 6 KiB pages and whose `render` could
        # never fit one, so the feature degenerated to index rows with nothing saying why.
        self.item_bytes = max(0, min(int(item_bytes), self.budget_bytes))
        self._lock = threading.Lock()
        # key -> {"tool", "path", "count", "phases": [..], "content": str | None, "sha": str | None,
        #         "changed": bool, "workspace": <token>}
        self._items: dict[tuple[str, str], dict] = {}
        self._order: list[tuple[str, str]] = []
        # WHICH WORKING SET THE CARRIED PAGES CAME FROM. The store is per RUN and the Developer's
        # scouts read through `write.files`, the per-NODE staged overlay (`repo_developer.py::
        # _scout_tools`: "read/grep see the code the Developer is currently writing"), so without
        # this a page read while building node 3 was seeded into node 7's chain root under "carried
        # verbatim … do not re-fetch". `invalidate` cannot cover it: "this file changed" and "this
        # is a different experiment's file" are different events, and the second one writes nothing.
        #
        # PER THREAD, because the pointer it replaces was a per-STORE scalar and the store is
        # shared across CONCURRENT builds. Every build now runs in a worker
        # (`orchestrator.py::_offload_build`), and `enter_workspace` is called on that worker at
        # the node's own build boundary — so with one scalar, node 7 entering its workspace moved
        # node 3's pointer too: node 3's `record` stamped node 7's token onto its pages and node
        # 3's `render` read `here = <node 7's token>` and carried node 7's bytes into node 3's
        # chain root, under the header that says the content is carried verbatim and need not be
        # re-fetched. That is precisely the cross-node bleed this field exists to prevent, one
        # level up from where it was fixed. `threading.local` restores the invariant the scalar
        # assumed (one working set per caller) without changing anything for the serial path,
        # where there is exactly one thread and the default is the same `None`.
        #
        # The ITEM stamp stays shared, and that is the safe direction: two threads re-reading one
        # path re-stamp it, so a node whose page a sibling re-stamped falls to `render`'s
        # comparison and gets an INDEX ROW naming the call that re-reads it — the remedy a caller
        # has not already spent — instead of another experiment's bytes.
        self._ws = threading.local()

    @property
    def _workspace(self):
        """This CALLER's working set, or `None` before it named one.

        A property rather than an attribute so every existing read site — `record`'s stamp,
        `record`'s `stale_here` comparison and `render`'s `here` — keeps its spelling and cannot
        drift back onto shared state by being written directly. See `__init__` for why it is
        per-thread.
        """
        return getattr(self._ws, "token", None)

    # ---- recording -------------------------------------------------------------------------
    def record(self, tool: str, args: dict, result: str, *, phase: str = "") -> bool:
        """Record one read-tool result. Returns True when it was a read of a registered reader."""
        slot = READ_TOOL_PATH_SLOTS.get(str(tool or ""))
        if slot is None:
            return False
        path = _canonical_read_path(str(tool or ""), args or {})
        if path is None:
            return False
        text, truncated = _strip_notes(result)
        # A refusal or an error is not established content — the model was told the file could not
        # be read, and carrying that sentence forward would assert it twice, under a header saying
        # it is the file. Decided by the READER's own vocabulary, imported rather than re-listed.
        if not text.strip() or text.lstrip().startswith(_refusal_prefixes()):
            return False
        key = (str(tool), path)
        with self._lock:
            item = self._items.get(key)
            if item is None:
                item = {"tool": str(tool), "path": path, "count": 0, "phases": [],
                        "content": None, "sha": None, "changed": False,
                        "workspace": self._workspace}
                self._items[key] = item
                self._order.append(key)
            item["count"] += 1
            if phase and phase not in item["phases"]:
                item["phases"].append(phase)
            # RE-CARRY when the page belongs to another working set, not only when there is none:
            # `enter_workspace` no longer destroys content, so an item still holding node 3's bytes
            # would otherwise refuse node 7's re-read and go on being reported as another
            # experiment's page forever.
            stale_here = item.get("workspace") != self._workspace
            if (item["content"] is None or stale_here) and not truncated \
                    and _is_whole_or_first_page(args) \
                    and len(text.encode("utf-8")) <= self.item_bytes:
                # A re-read AFTER a write is the CURRENT bytes, so it may be carried again and the
                # row stops being "changed since" — the flag describes the carried page, not history.
                item["content"] = text
                item["sha"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
                item["changed"] = False
                item["workspace"] = self._workspace
        return True

    def invalidate(self, tool: str, args: dict) -> bool:
        """A write/edit/delete of a carried path drops its CONTENT and marks the row changed.

        Returns True when the call was a registered writer (whether or not anything was carried),
        so the hook can tell "handled" from "not a file tool"."""
        slot = WRITE_TOOL_PATH_SLOTS.get(str(tool or ""))
        if slot is None:
            return False
        if slot is _STAGES_MANIFEST_SLOT:
            # `declare_stages` names no path: it writes one constant, so the writer's OWN constant
            # is what says which page it invalidates (`adapters/repo_write_tools.STAGES_MANIFEST` — the same
            # rule that file states for `empty_build_refusal`, so tool and rule cannot drift).
            from looplab.adapters.repo_write_tools import STAGES_MANIFEST
            path = STAGES_MANIFEST
        else:
            # No separate canonicaliser: `_same_file` normalises separators and drops `.`/empty
            # components itself, which is the whole of what the read side's key rule does here.
            path = str((args or {}).get(slot) or "").strip()
        if not path:
            return True
        with self._lock:
            for item in self._items.values():
                if _same_file(item["path"], path):
                    item["content"] = None
                    item["sha"] = None
                    item["changed"] = True
        return True

    def enter_workspace(self, token) -> None:
        """Name the working set the following reads belong to.

        Called where the node's `RepoWriteTools` is minted (`repo_developer.py::_run`), the one
        place a build boundary exists — beside `self.last_edit_calls = 0`, which is reset there for
        the very same reason ("the developer is reused across nodes, so a stale count would
        attribute a sibling's edits here").

        IT ONLY MOVES THE POINTER; it destroys nothing. `render` carries a page only when the page's
        own workspace is the current one, so "this page belongs to another experiment" is DERIVED
        from a comparison rather than latched on a flag — and the first cut of this latched it, so a
        file legitimately re-read in the new workspace kept telling the model to re-read it, a
        remedy it had just spent (`tools/log_tools.py` rule 3, one file over). Not destroying also
        means re-entering a workspace restores its pages, which is what makes the boundary safe to
        call on a path that may or may not be a new node.

        The COUNTS and the index rows are workspace-INDEPENDENT and always survive: "you have read
        this file 9 times in this run" stays true across nodes and is half of what the block is for.
        It is the CONTENT — the claim "here is what it says" — that belongs to one workspace.
        """
        # No lock: `threading.local` gives each caller its own slot, so there is nothing to
        # serialize — and taking `self._lock` here would be claiming a mutual exclusion that the
        # per-thread storage has already made unnecessary.
        self._ws.token = token

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
        # A ZERO BUDGET IS OFF. `established_context_bytes` is `ge=0` and every other byte knob in
        # `Settings` reads 0 as off; at 0 this still emitted the ~460-byte header promising "where
        # its content follows, it is carried verbatim" above no content at all.
        if not rows or self.budget_bytes <= 0:
            return ""
        here = self._workspace
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
            # A PAGE BELONGS TO THE WORKING SET IT WAS READ IN. The Developer's scouts answer
            # through `write.files`, this node's staged overlay, so a page read while building node
            # 3 is not this node's `solver.py` — and there is no write to hang `invalidate` on,
            # because a new node writes NOTHING before its first phase renders this block.
            elsewhere = row.get("workspace") != here
            content = None if elsewhere else row["content"]
            if content is not None:
                body = (f"\n--- `{row['path']}` ({row['tool']}; {times}; first page verbatim, "
                        f"sha {row['sha']}) ---\n{content}\n")
                size = len(body.encode("utf-8"))
                if used + size <= self.budget_bytes:
                    out.append(body)
                    used += size
                    continue
            why = ("read while building a DIFFERENT experiment — re-read" if elsewhere
                   else "CHANGED since you read it — re-read" if row.get("changed")
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


_SETTINGS_STORE_ATTR = "_looplab_established_context"


def established_context_from_settings(settings) -> Optional[EstablishedContext]:
    """The ONE store a run's roles share, or None when the setting is off.

    CACHED ON THE SETTINGS OBJECT, exactly as `core/llm.py::run_cost_accountant` is and for the same
    reason: `agents/factory.py::make_roles` is its only caller and `make_roles` runs several times
    per run — the primary pair, a second one for the repair Developer when the two models differ,
    one per pooled pair under `llm_parallel > 1` (the AUTO default), and again on a Strategist
    developer swap. Minting per call made "one store per run" false in the shipped configuration:
    each pair kept its own ledger, so the repair session — which has the most to gain — started
    empty, and the `threading.Lock` justified by "the Developer's phases run on worker threads"
    was guarding objects that were never shared.

    Keyed on the settings object the roles were built from, so a run that deliberately builds roles
    from a DIFFERENT `Settings` (the speculation-calibration profile) still gets its own.
    """
    if not bool(getattr(settings, "established_context", False)):
        return None
    existing = getattr(settings, _SETTINGS_STORE_ATTR, None)
    if isinstance(existing, EstablishedContext):
        return existing
    store = EstablishedContext(
        budget_bytes=int(getattr(settings, "established_context_bytes", DEFAULT_BUDGET_BYTES)))
    try:
        object.__setattr__(settings, _SETTINGS_STORE_ATTR, store)
    except Exception:  # noqa: BLE001 - a settings object that refuses the cache still gets a store
        pass
    return store
