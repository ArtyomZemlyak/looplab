"""THE AUTOMATED UPSTREAM AUTHOR (doc 73 §2.5, tracks 1 and 2), under `Settings.upstream_author`.

WHY (2026-10-08). The live lane (`engine/upstream_serve.py`) checks and advances proposals on its
own, but somebody still had to WRITE each one — the Assistant, an external agent or the operator. The
operator asked for the two writers doc 73 named: a FIX a repair made reaches the base so later nodes
stop hitting the same defect (track 1), and a new champion's reusable code reaches the base behind a
flag (track 2).

WHAT IT DOES, once per source lifecycle, only under `upstream_mode: auto`:

1. `author_next` (main task, deterministic over the fold and the source's seed archive) picks the
   next source: an evaluated node whose current lifecycle was REPAIRED, newest first, then the
   champion. Its nomination is the lane's own advice (`upstream_state.py::upstream_candidates`):
   the CAPABILITY hunks against the base it was seeded on, which must be the run's active base. A
   repair hunk whose pending trigger no declared repair probe carries is left out — the lane's admit
   would refuse it (`UpstreamLane._propose_admit`).
2. The worker makes TWO paid calls with the Developer's client (`agents/maintainer.py`): the
   Maintainer's draft (full base files, the flag, its documentation) and a separate critic's verdict.
   A `fail` is recorded and the source is not asked again.
3. A passed draft becomes an ordinary proposal body (`build_body`: the recipe is the source node's own
   files byte for byte, less the code paths the patch now owns, with the draft's override only on a
   configuration path the patch also changes) and continues as the
   lane's `propose` job; `auto` then checks it and advances it only if its MEASURED gate passes.

EVERY ROW is appended by the MAIN task (invariant #1): the diagnostic `lane_authored` row, then
the lane's own rows. The action id is a digest of the source lifecycle and its track
(`author_action_id`), so a re-entry never pays for the same source twice once its row landed; a
drafted body is RETAINED (`retain_draft`) before its row, so a crash between the row and the lane's
claim re-proposes it unpaid; a propose the lane refuses is recorded (`refused`).
`AUTHOR_MAX_PER_RUN` bounds the spend of a run whose every proposal fails. Nothing is drafted while
a lane claim is unresolved — the lane would refuse the proposal it bought.

A SOURCE THE BASE MOVED PAST (doc 73 §4.3, `Settings.upstream_author_rebase`). The lane admits a
source measured on the CURRENT base only, so a repaired node or the champion seeded before the last
advance used to be skipped for good. With rebasing on, `author_next` hands such a source to the worker
as a REBASE pick: `rebase_source` three-way merges its overlay (the base it was measured on → the
current base, git merge-file — `upstream_workspace.py::rebase_overlay`, the merge every migrating
lifecycle already takes) and nominates its capability hunks against the CURRENT base. A conflict (or a
source too large to merge in bounds) is recorded — `lane_authored {outcome: rebase_conflict, code,
conflicts}` — and the source is skipped on that base. A merge that could not RUN (Git timed out or
would not start, an archive unreadable) is no verdict on the source: nothing is recorded and the pick
is asked again after `REBASE_RETRY_AFTER_S` (`rebase_unavailable`); a failure nobody foresaw is
recorded once per base as an unpaid `skipped`, never as a paid `failed`. A clean merge is drafted
like any source, and its body carries the merged overlay (`rebase`), which the lane recomputes before
it admits it (`UpstreamLane._propose_admit`) and the gate runs as its OLD side on the current base.
What that comparison does and does not establish: under `full` the rebased source must still
reproduce the score the source measured, so a merge that changed the source's behaviour fails there.
Under `canary` both sides run the current base's slice, and under `repair_gate: probes` no
equivalence runs at all — in those profiles NOTHING compares the merged source with the source's own
measurement, so the merge is trusted as far as the probes and the canary pair reach (the gate's
`scope` says so). A repair's trigger probe still runs the current base as its failing old side. The
action id names the base it rebases onto (`author_rebase_action_id`); a lifecycle the author already
paid for on any base is never rebased on that track (`_paid_lifecycles`), nor one the lane already
holds a proposal from on that track (`_proposed_lifecycles`).

What the author is NOT: evidence. Its prose and the critic's verdict only admit a proposal to the
gate; the base moves on measurements alone.
"""
from __future__ import annotations

import difflib
import json
import subprocess
import time
from typing import Optional

AUTHOR_TRACKS = ("repair", "champion")
# A run whose every authored proposal fails its gate stops paying for new ones here.
AUTHOR_MAX_PER_RUN = 6
# The bounded context: a nominated file larger than this is not authored automatically.
AUTHOR_FILE_CHARS = 24_000
AUTHOR_MAX_PATHS = 8
AUTHOR_DECLARATION_CHARS = 4_000


def upstream_author_setting(settings) -> bool:
    """THE ONE READER of `Settings.upstream_author`. A duck-typed or absent settings object reads
    OFF — no paid authoring call."""
    return getattr(settings, "upstream_author", False) is True


def author_rebase_setting(settings) -> bool:
    """THE ONE READER of `Settings.upstream_author_rebase` (doc 73 §4.3). Absent or duck-typed reads
    OFF — the historical skip."""
    return getattr(settings, "upstream_author_rebase", False) is True


def author_usd_cap(settings) -> float:
    """THE ONE READER of `Settings.upstream_author_usd` (doc 73 §4.2 G4): the author's money for the
    whole run; 0 (or anything unreadable) = no money cap."""
    try:
        value = float(getattr(settings, "upstream_author_usd", 0.0))
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 and value != float("inf") else 0.0


def author_spent_usd(events) -> float:
    """What the author's calls cost so far: the `cost_usd` its `lane_authored` rows carry (a row
    written before the field existed counts 0 — an older run's spend is unknown, not free)."""
    total = 0.0
    for e in events:
        if e.type == "lane_authored":
            try:
                total += max(0.0, float(e.data.get("cost_usd") or 0.0))
            except (TypeError, ValueError):
                continue
    return total


def author_action_id(node, track: str) -> str:
    """The deterministic action id of one source LIFECYCLE on one track: a re-built node is a new
    source, and a repaired champion is asked once for its fix and once for its capability."""
    from looplab.engine.upstream_state import digest, node_signature
    return "auto-author-" + digest({"node_id": node.id, "generation": int(node.attempt or 0),
                                    "signature": node_signature(node), "track": track})[:24]


def author_rebase_action_id(node, track: str, onto_revision: str) -> str:
    """The deterministic action id of one source lifecycle on one track REBASED onto one base
    revision (`active_base(...)["revision"]`): a conflict on one base does not close the next one."""
    from looplab.engine.upstream_state import digest, node_signature
    return "auto-rebase-" + digest({"node_id": node.id, "generation": int(node.attempt or 0),
                                    "signature": node_signature(node), "track": track,
                                    "onto": onto_revision})[:24]


# Bounds of the rebase path: a source overlay with more paths than this is not merged automatically.
REBASE_MAX_PATHS = 32
# A merge that could not RUN is asked again after this long — the lane's own pause for a passing
# refusal (`upstream_serve.py::AUTO_RETRY_AFTER_S`). What a retry costs is Git work, never money.
REBASE_RETRY_AFTER_S = 300.0
# Outcomes the main task settles WITHOUT a `lane_authored` row (`upstream_serve.py::_settle_author`):
# `nothing` (a rebased source that nominates no capability, memoized against what could change that)
# and `rebase_unavailable` (a merge that could not run, memoized until `REBASE_RETRY_AFTER_S`). Each
# leaves its memo value in `pick["pending"]`.
UNRECORDED_OUTCOMES = ("nothing", "rebase_unavailable")


class RebaseRetry:
    """The per-process memo value of a rebase pick whose merge could not run: `author_next` passes
    it over until `deadline` (`time.monotonic()`), then asks again. Never equal to a nomination key,
    so it can only ever DELAY a pick, never close it."""

    __slots__ = ("deadline",)

    def __init__(self, deadline: float) -> None:
        self.deadline = deadline

    def __repr__(self) -> str:
        return f"RebaseRetry({self.deadline!r})"


def _memo_holds(memo: dict, action_id: str, key) -> bool:
    """Whether the per-process memo still passes `action_id` over: memoized for good (None), against
    the nomination key it still has, or a retry whose time has not come (`RebaseRetry`)."""
    if action_id not in memo:
        return False
    value = memo[action_id]
    if isinstance(value, RebaseRetry):
        return time.monotonic() < value.deadline
    return value in (None, key)


# Outcomes that cost the two paid calls — what `AUTHOR_MAX_PER_RUN` counts. `skipped` made none, and
# `refused` is the lane's later answer to a draft already counted as `drafted`.
PAID_OUTCOMES = ("drafted", "declined", "rejected", "failed")


def _attempted(events) -> tuple[set, int]:
    """The action ids the author already settled (or the lane already holds), and how many
    authoring attempts the run paid for."""
    authored = [e for e in events if e.type == "lane_authored"]
    ids = {e.data.get("action_id") for e in authored}
    ids |= {e.data.get("action_id") for e in events if e.type.startswith("upstream_proposal")
            or e.type == "upstream_proposed"}
    return ids, sum(1 for e in authored if e.data.get("outcome") in PAID_OUTCOMES)


def _paid_lifecycles(events) -> set:
    """The NATIVE action ids (`author_action_id`) of every source lifecycle the author already paid
    for on some base — its own row, or a rebased row naming it (`source_action_id`). Such a lifecycle
    is never rebased: its draft already had its answer."""
    out = set()
    for e in events:
        if e.type == "lane_authored" and e.data.get("outcome") in PAID_OUTCOMES + ("refused",):
            out.add(e.data.get("source_action_id") or e.data.get("action_id"))
    return out


def _proposed_lifecycles(events) -> set:
    """`(source_node_id, source_signature, track)` of every lifecycle the lane holds a proposal from,
    per TRACK like the action ids that name them (`author_action_id`, `author_rebase_action_id`): the
    repair track's proposal answers the repair's fix, not the champion's capability of the same
    lifecycle. The author's own proposal names its track on its `lane_authored` row (same action id);
    a proposal somebody else wrote (the Assistant, an external agent, the operator) names none and is
    recorded with track None — the answer for EVERY track of that lifecycle, as it always was."""
    tracks = {e.data.get("action_id"): e.data.get("track") for e in events
              if e.type == "lane_authored" and e.data.get("track") in AUTHOR_TRACKS}
    return {(e.data.get("source_node_id"), e.data.get("source_signature"),
             tracks.get(e.data.get("action_id")))
            for e in events if e.type == "upstream_proposed"}


def _repaired_ids(state, events) -> set:
    """Ids of the nodes whose CURRENT lifecycle a repair changed (`node_repaired` bound to it)."""
    from looplab.events.replay import event_generation_binds
    out = set()
    for e in events:
        if e.type != "node_repaired":
            continue
        node = state.nodes.get(e.data.get("node_id"))
        if node is not None and event_generation_binds(e.data, node.attempt):
            out.add(node.id)
    return out


def _sources(state, events, repaired=None) -> list[tuple[str, object]]:
    """`(track, node)` in the order the author asks them: repaired lifecycles newest first, then the
    champion — a repaired champion appears on both tracks (its fix, then its capability). Evaluated,
    live nodes only; the lane's own eligibility is checked after."""
    repaired = _repaired_ids(state, events) if repaired is None else repaired
    out = []
    for node in sorted((state.nodes[i] for i in repaired), key=lambda n: -n.id):
        if node.status.value == "evaluated" and not node.tombstoned:
            out.append(("repair", node))
    best = state.nodes.get(state.best_node_id) if state.best_node_id is not None else None
    if best is not None and not best.tombstoned:
        out.append(("champion", best))
    return out


def _nomination_key(node, state, revision, repaired: bool) -> tuple:
    """What a source with NO eligible capability hunk is memoized against (`author_next`): only a
    later promotion (the base revision) or — for a REPAIRED lifecycle alone — the pending nodes
    that can carry its repair's trigger can make one appear. A repair hunk's trigger is a pending
    node whose file on one of the source's own paths holds a failing value the repair replaced
    (`upstream_state.py::upstream_candidates`), so those files are the key; an unrepaired source's
    hunks never depend on the pending set at all. Keyed on every pending id instead, the memo was
    recomputed — a whole-log fold and a per-hunk replay of the repair — on nearly every node."""
    if not repaired:
        return (revision,)
    from looplab.engine.upstream_state import digest
    paths = sorted(set(node.files) | set(node.deleted))
    return (revision, tuple(sorted((n.id, n.attempt, digest([n.files.get(p) for p in paths]))
                                   for n in state.pending_nodes() if n.id != node.id)))


def _draft_path(rd, action_id: str):
    from looplab.engine.upstream_state import digest
    from looplab.engine.upstream_workspace import owned_path
    return owned_path(rd, "upstream/authored/" + digest(action_id)[:24] + ".json")


def retain_draft(rd, body: dict) -> None:
    """Publish a drafted body durably BEFORE its `lane_authored` row (the worker's last step): a
    crash between that row and the lane's claim re-proposes it from here, unpaid."""
    import json

    from looplab.core.atomicio import strict_atomic_write_bytes
    path = _draft_path(rd, body["action_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    strict_atomic_write_bytes(path, json.dumps(body, ensure_ascii=False).encode())


def retained_draft(rd, action_id: str) -> Optional[dict]:
    from looplab.engine.upstream_state import read_retained_json
    body = read_retained_json(_draft_path(rd, action_id))
    return body if isinstance(body, dict) and body.get("action_id") == action_id else None


def unproposed_draft(rd, events) -> Optional[tuple[dict, dict]]:
    """`(row data, body)` of a `drafted` row whose proposal the lane never claimed and never
    refused — a crash between the two — with its retained body; None otherwise."""
    lane_ids = {e.data.get("action_id") for e in events if e.type.startswith("upstream_proposal")
                or e.type == "upstream_proposed"}
    refused = {e.data.get("action_id") for e in events
               if e.type == "lane_authored" and e.data.get("outcome") == "refused"}
    for e in events:
        if (e.type == "lane_authored" and e.data.get("outcome") == "drafted"
                and e.data.get("action_id") not in lane_ids | refused):
            body = retained_draft(rd, e.data["action_id"])
            if body is not None:
                return dict(e.data), body
    return None


def author_next(rd, task, state, events, *, skipped=None, active=None,
                rebase=False) -> Optional[dict]:
    """The next source to author from, or None. `{"action_id", "track", "node", "rows", "archive",
    "revision"}` — plus, for a REBASE pick (`rebase=True`, a source measured on an older base),
    `"rebase": {"from_digest", "onto"}`, `"source_action_id"` and `"pending"`, with `rows` None: the
    worker merges and nominates (`rebase_source`), off the main task.

    `skipped` is the caller's per-process memo, `{action_id: signature}`, ADDED TO here when it is a
    dict. A source the lane can never take (unmeasured, built on an older base with rebasing off —
    the base only advances — or with no readable archive) is memoized for good (`None`); one with no
    eligible capability hunk is memoized against what could make one appear (`_nomination_key`),
    because a repair hunk whose trigger nodes are still pending becomes nominable once they settle.
    `active` is the caller's `active_base` of exactly these events (the live engine caches it)."""
    from looplab.core.errors import UpstreamRefusal
    from looplab.engine.seed_archive import verified_seed_archive
    from looplab.engine.upstream_state import (_source_receipt, active_base, node_signature,
                                               repair_probe_covers, upstream_candidates)
    upstream = task.upstream
    if not upstream:
        return None
    ids, spent = _attempted(events)
    if spent >= AUTHOR_MAX_PER_RUN:
        return None
    if active is None:
        try:
            active = active_base(events, task.seed_base)
        except Exception:  # noqa: BLE001 — an unreadable base is the lane's refusal to state; the author waits
            return None
    memo = skipped if isinstance(skipped, dict) else {}
    repaired = _repaired_ids(state, events)
    # The whole-log readings a stale source asks, made at most ONCE per call: asked per source, a
    # turn with several sources the base moved past re-scanned the log twice for each of them.
    by_seq = paid = proposed = None
    for track, node in _sources(state, events, repaired):
        action_id = author_action_id(node, track)
        key = _nomination_key(node, state, active["revision"], node.id in repaired)
        if action_id in ids or _memo_holds(memo, action_id, key):
            continue
        by_seq = {e.seq: e for e in events} if by_seq is None else by_seq
        try:
            receipt = _source_receipt(node, by_seq)
        except UpstreamRefusal:
            memo[action_id] = None
            continue
        if receipt.get("digest") != active["selector"]["digest"]:
            if rebase and paid is None:
                paid, proposed = _paid_lifecycles(events), _proposed_lifecycles(events)
            signature = node_signature(node)
            if not rebase or action_id in paid or (node.id, signature, track) in proposed or (
                    node.id, signature, None) in proposed:
                # Built on an older base: the lane refuses it as a source. Rebased (doc 73 §4.3) only
                # while nobody has proposed this lifecycle ON THIS TRACK yet — a source whose
                # capability already had its proposal (whoever wrote it, advanced or not) had its
                # answer. Keyed per lifecycle alone, the repair track's proposal closed the champion
                # track of the same lifecycle too; a proposal the author did not write names no
                # track and still answers every one (`_proposed_lifecycles`).
                memo[action_id] = None
                continue
            # doc 73 §4.3: rebase it onto the current base — in the worker, which merges with Git.
            rebase_id = author_rebase_action_id(node, track, active["revision"])
            if rebase_id in ids or _memo_holds(memo, rebase_id, key):
                continue
            archive = verified_seed_archive(rd, receipt)
            if archive is None:
                memo[rebase_id] = None
                continue
            return {"action_id": rebase_id, "track": track, "node": node, "rows": None,
                    "archive": archive, "revision": active["revision"], "source_action_id": action_id,
                    "rebase": {"from_digest": receipt["digest"], "onto": dict(active["selector"])},
                    "pending": key, "events": events}
        archive = verified_seed_archive(rd, receipt)
        if archive is None:
            memo[action_id] = None
            continue
        rows = [r for r in upstream_candidates(rd, task, events, source_node_id=node.id,
                                               state=state, archive=archive)["rows"]
                if r["classification"] == "capability"
                and (track == "champion" or r["origin"] == "repair")
                and not (r["origin"] == "repair" and r["pending_trigger_nodes"]
                         and not repair_probe_covers(r, upstream.get("repair_probes", [])))]
        if not rows:
            memo[action_id] = key
            continue
        return {"action_id": action_id, "track": track, "node": node, "rows": rows,
                "archive": archive, "revision": active["revision"]}
    return None


def rebase_source(rd, task, pick) -> dict:
    """The WORKER's half of a rebase pick (doc 73 §4.3): merge the source's overlay from the base it
    was measured on onto the current base, then nominate its capability hunks against the current
    base. Mutates `pick` in place (`archive` becomes the current base's, `source_files` /
    `source_deleted` the merged overlay, `rows` the nomination) and returns `{"outcome": "ready"}`, or
    `{"outcome": "rebase_conflict", "code", "conflicts", "reason"}`, or `{"outcome": "nothing"}` when
    the merged source nominates no capability, or `{"outcome": "rebase_unavailable", "code"}` when the
    merge could not RUN (`pick["pending"]` becomes a `RebaseRetry`), or — for a failure nobody
    foresaw — an unpaid `{"outcome": "skipped", "code": "upstream_rebase_failed", "reason"}`.
    Appends nothing; makes no paid call, so nothing it returns is a PAID outcome (`PAID_OUTCOMES`):
    before, an unforeseen exception here reached the main task as the job's error and was recorded
    as a paid `failed` draft, counted against `AUTHOR_MAX_PER_RUN` for a call never made."""
    from looplab.core.containment import refuse_budget_stop
    from looplab.core.errors import ConfigRefusal
    try:
        return _rebase_source(rd, task, pick)
    except (ConfigRefusal, OSError, subprocess.SubprocessError) as exc:
        # The merge could not RUN: the current base's archive unreadable (`seed_base.py::
        # selected_seed_base` refuses it as a `ConfigRefusal`), or Git timed out or would not start
        # (`upstream_workspace.py::_run_git` turns both into an `UpstreamRefusal`, a `ConfigRefusal`).
        # No merge was made, so it is no answer about this source on this base. Recorded as a
        # `rebase_conflict` it closed the source on the base for good (the action id names the base)
        # over a Git timeout; now nothing is recorded and the pick is asked again later.
        pick["pending"] = RebaseRetry(time.monotonic() + REBASE_RETRY_AFTER_S)
        return {"outcome": "rebase_unavailable",
                "code": str(getattr(exc, "code", "") or "upstream_rebase_unavailable")[:80]}
    except Exception as exc:  # noqa: BLE001 — the rebase makes no paid call: an unforeseen failure is an unpaid answer on this base, never a paid `failed` draft
        refuse_budget_stop(exc)
        return {"outcome": "skipped", "code": "upstream_rebase_failed",
                "reason": f"the rebase failed unexpectedly ({type(exc).__name__}); no call was made"}


def _rebase_source(rd, task, pick) -> dict:
    """`rebase_source` without its classification of what the merge raised."""
    from looplab.engine.seed_base import selected_seed_base
    from looplab.engine.upstream_state import repair_probe_covers, upstream_candidates
    from looplab.engine.upstream_workspace import rebase_overlay
    node = pick["node"]
    paths = sorted(set(node.files) | set(node.deleted))
    if len(paths) > REBASE_MAX_PATHS:
        return {"outcome": "rebase_conflict", "code": "upstream_rebase_too_large", "conflicts": [],
                "reason": f"the source overlay names {len(paths)} paths; at most {REBASE_MAX_PATHS} "
                          "are merged automatically"}
    new_archive, _ = selected_seed_base(pick["rebase"]["onto"])
    files, deleted, conflicts = rebase_overlay(dict(node.files), list(node.deleted),
                                               pick["archive"], new_archive)
    if conflicts:
        return {"outcome": "rebase_conflict", "code": "upstream_rebase_conflict",
                "conflicts": sorted(conflicts)[:AUTHOR_MAX_PATHS * 4],
                "reason": ("a three-way merge of the source's edits (the base it was measured on → "
                           "the run's current base) did not apply cleanly in "
                           + ", ".join(sorted(conflicts)[:8]))[:500]}
    pick.update(archive=new_archive, source_files=files, source_deleted=sorted(deleted))
    overlay = {"files": files, "deleted": sorted(deleted), "archive": new_archive}
    upstream = task.upstream or {}
    rows = [r for r in upstream_candidates(rd, task, pick["events"], source_node_id=node.id,
                                           overlay=overlay)["rows"]
            if r["classification"] == "capability"
            and (pick["track"] == "champion" or r["origin"] == "repair")
            and not (r["origin"] == "repair" and r["pending_trigger_nodes"]
                     and not repair_probe_covers(r, upstream.get("repair_probes", [])))]
    if not rows:
        return {"outcome": "nothing"}
    pick["rows"] = rows
    return {"outcome": "ready"}


def _source_files(pick) -> tuple[dict, list]:
    """The source overlay the author drafts from and the recipe copies: the merged one for a rebase
    pick, the node's own otherwise."""
    node = pick["node"]
    if pick.get("rebase") is not None and "source_files" in pick:
        return pick["source_files"], list(pick["source_deleted"])
    return node.files, list(node.deleted)


def _read(archive, path) -> Optional[str]:
    """A nominated file's BASE text: "" for a path the source ADDED (absent from the base), None —
    the source is not authored — for one present but unreadable (a link, a directory, a FIFO, not
    UTF-8). Reading an unreadable base file as empty showed the Maintainer a base with no such file,
    and its "full contents" patch then replaced code it never saw (critic 2026-10-08)."""
    from looplab.core.node_evidence import read_bounded_regular_file
    target = archive / path
    raw = read_bounded_regular_file(target, AUTHOR_FILE_CHARS * 4 + 1)
    if raw is None:
        try:
            target.lstat()
        except FileNotFoundError:
            return ""
        except OSError:
            return None
        return None
    try:
        return raw.decode("utf8")
    except UnicodeError:
        return None


def author_context(task, pick) -> Optional[str]:
    """The bounded text both calls read: the declaration, each nominated file's base and source
    contents and their diff. None when a nominated file is too large or not text — such a source is
    not authored automatically."""
    node, paths = pick["node"], sorted({r["path"] for r in pick["rows"]})
    if len(paths) > AUTHOR_MAX_PATHS:
        return None
    parts = [f"SOURCE: node #{node.id} ({pick['track']}).",
             "RATIONALE: " + " ".join(str(getattr(node.idea, "rationale", "") or "").split())[:600]]
    files, _deleted = _source_files(pick)
    if pick.get("rebase") is not None:
        parts.append("REBASED: the source was measured on an older base; its edits were three-way "
                     "merged onto the run's CURRENT base cleanly. BASE CONTENTS below are the current "
                     "base, and the source contents are the merged ones.")
    for r in pick["rows"]:
        if r.get("repair_reason"):
            parts.append(f"REPAIR ({r['path']}): {str(r['repair_reason'])[:400]}")
    declaration = json.dumps({k: [p.get("name", p.get("command", "")) for p in v] if isinstance(v, list)
                              else v for k, v in (task.upstream or {}).items()}, ensure_ascii=False)
    parts.append("UPSTREAM DECLARATION (the gate the proposal must pass): "
                 + declaration[:AUTHOR_DECLARATION_CHARS])
    recipe = sorted(set(files) - set(paths))
    if recipe:
        parts.append("SOURCE RECIPE FILES (stay with the node, copied unchanged): " + ", ".join(recipe))
    for path in paths:
        before = _read(pick["archive"], path)
        after = files.get(path, "")
        if before is None or len(before) > AUTHOR_FILE_CHARS or len(after) > AUTHOR_FILE_CHARS:
            return None
        diff = "".join(difflib.unified_diff(before.splitlines(keepends=True),
                                            after.splitlines(keepends=True),
                                            f"base/{path}", f"node/{path}"))
        parts.append(f"=== {path}\n--- BASE CONTENTS\n{before}\n--- DIFF (base -> source node)\n{diff}")
    return "\n\n".join(parts)


def build_body(pick, draft, critique, *, generation: str) -> dict:
    """The lane's `propose` body from a passed draft. The recipe is the source node's own files byte
    for byte (the lane refuses anything else outside the patch's paths), less the code paths the
    patch owns, with the draft's overrides only on a configuration path the patch also changes."""
    from looplab.agents.maintainer import AUTHOR_CRITIC_REVIEWER
    from looplab.engine.activation import is_config_path
    node = pick["node"]
    files, deleted = _source_files(pick)
    patch = set(draft.files) | set(draft.deleted)
    # The SHARED implementation is the base's alone: a recipe may not carry a code path the patch
    # writes (`UpstreamLane._validate_shared_patch`). A configuration path the patch also changes
    # stays the node's, unless the draft says what the recipe needs there to turn the flag on.
    shared = {p for p in patch if not is_config_path(p)}
    recipe = {p: t for p, t in files.items() if p not in shared}
    recipe.update({p: t for p, t in draft.recipe_overrides.items() if p in patch and p not in shared})
    # doc 73 §4.3: a rebased source travels with its merged overlay — what the lane recomputes before
    # it admits the proposal and what the gate runs as the OLD side on the current base. It repeats
    # most of `recipe_files` by contract; `body_floor_bytes` bounds the two before the paid draft.
    rebased =({"rebase": {"from_digest": pick["rebase"]["from_digest"], "files": dict(files),
                           "deleted": sorted(deleted)}} if pick.get("rebase") is not None else {})
    return {**rebased,"expected_generation": generation, "action_id": pick["action_id"],
            "source_node_id": node.id, "expected_base_revision": pick["revision"],
            "hunk_hashes": sorted({r["hunk_hash"] for r in pick["rows"]}),
            "files": dict(draft.files), "deleted": sorted(set(draft.deleted)),
            "recipe_files": recipe, "recipe_deleted": sorted(set(deleted) - shared),
            "summary": draft.summary, "flag": draft.flag.model_dump(),
            "documentation_path": draft.documentation_path,
            "critic": {"verdict": critique.verdict, "reason": critique.reason,
                       "reviewer": AUTHOR_CRITIC_REVIEWER}}


def body_floor_bytes(pick) -> int:
    """The encoded bytes of the part of `build_body`'s body that is known BEFORE the draft: a rebased
    source's merged overlay (`rebase.files`/`deleted`, carried whole) and the recipe on every source
    path the nomination does not name (`recipe_files`, the source's bytes unchanged). A rebased body
    carries most of the overlay TWICE by contract — the lane recomputes `rebase` and compares the
    recipe with it byte for byte (`UpstreamLane._propose_admit`), and the gate reads both from the
    manifest — so the request bound (`normalize_request`, `RETAINED_REQUEST_MAX_BYTES`) used to be
    met only by `precheck_draft`, after the draft was paid for. The draft only ADDS to this (its own
    files, the flag, the summary) unless it rewrites a path it was not nominated for, so a floor over
    the bound is refused before any call (`author_draft`)."""
    files, deleted = _source_files(pick)
    nominated = {r["path"] for r in pick["rows"] or []}
    known = {"recipe_files": {p: t for p, t in files.items() if p not in nominated}}
    if pick.get("rebase") is not None:
        known["rebase"] = {"from_digest": pick["rebase"]["from_digest"], "files": dict(files),
                           "deleted": sorted(deleted)}
    return len(json.dumps(known, ensure_ascii=False).encode())


def precheck_draft(pick, body, task) -> Optional[str]:
    """Why the lane would refuse this body whatever the critic says — or None. Run between the draft
    and the critic, so a draft that cannot be absorbed does not buy the second call. Every rule is
    the LANE'S OWN FUNCTION, called, never restated (a restated copy had already drifted: it missed
    the edit surface and the case-folded recipe collision — critic 2026-10-08): the request's bounds
    (`normalize_request`), the Maintainer contract (`Maintainer.validate`), the edit surface and
    protected names of the patch and of the recipe (`checked_overlay`), the absorbed implementation
    (`upstream.py::absorbed_implementation`) and the shared-patch boundary
    (`upstream.py::validate_shared_patch`) — the order `UpstreamLane._propose_admit` asks them in."""
    from looplab.agents.maintainer import Maintainer
    from looplab.core.errors import UpstreamRefusal
    from looplab.engine.upstream import absorbed_implementation, validate_shared_patch
    from looplab.engine.upstream_spec import normalize_request
    from looplab.engine.upstream_workspace import checked_overlay
    try:
        normalize_request("propose", body)
        Maintainer().validate(body)
        spec = task.repo_spec()
        checked_overlay(spec, body["files"], body["deleted"])
        checked_overlay(spec, body["recipe_files"], body.get("recipe_deleted", []))
        implementation, _recipes, patch = absorbed_implementation(pick["rows"], body)
        if not implementation or not implementation <= patch:
            return "upstream_capability_not_absorbed"
        validate_shared_patch(task.upstream, body, implementation)
    except UpstreamRefusal as exc:
        return exc.code
    except ValueError:      # a path the scorer-boundary grammar refuses (`checked_overlay`)
        return "upstream_patch_invalid"
    return None


def author_draft(engine, pick, *, generation: str) -> dict:
    """The worker's paid half: draft, local precheck, critic, body. Returns `{"outcome": "drafted",
    "body", "reason"}` (the body retained on disk first, `retain_draft`) or `{"outcome":
    "declined"|"rejected"|"skipped", "reason"|"code"}`. Raises what the model layer raises (a budget
    stop included — the main task re-raises it)."""
    from looplab.agents.maintainer import (MaintainerCritique, MaintainerDraft, author_messages,
                                           critic_messages)
    from looplab.core.parse import parse_structured
    from looplab.engine.shared import judge_evidence_kwargs
    from looplab.engine.upstream_state import RETAINED_REQUEST_MAX_BYTES
    client = getattr(getattr(engine, "developer", None), "client", None)
    if client is None:
        return {"outcome": "skipped", "reason": "no model client"}
    if body_floor_bytes(pick) > RETAINED_REQUEST_MAX_BYTES:
        # The proposal could never be admitted (`normalize_request` bounds a body): refused BEFORE
        # the draft, so it costs nothing — `precheck_draft` met it only after the first paid call.
        return {"outcome": "skipped", "code": "upstream_request_too_large",
                "reason": "the source's recipe (and a rebase's merged overlay) alone exceed the "
                          f"{RETAINED_REQUEST_MAX_BYTES} byte request bound"}
    context = author_context(engine.task, pick)
    if context is None:
        return {"outcome": "skipped", "reason": "a nominated file is too large or not text"}
    # The context and the draft are the candidate's own code and rationale: fenced evidence, with
    # the guard at system authority, while the run's untrusted-evidence envelope is on — the one
    # reading every engine judge asks (`core/evidence.py`).
    label = judge_evidence_kwargs(engine).get("tool_result_label") or ""
    draft = parse_structured(client, author_messages(pick["track"], context, evidence_label=label),
                             MaintainerDraft)
    # The Maintainer's summary and flag values land in the log too (the proposal, `base_advanced`,
    # the hint every Developer reads): through the engine's ONE redaction funnel
    # (`engine/audit.py::Engine._redact`) like the critic's reason below — before the precheck, so
    # what is checked is what is proposed. The flag NAME is an identifier the documentation must
    # spell, so it is checked (`Maintainer.validate`), never rewritten.
    redact = getattr(engine, "_redact", None)
    if callable(redact):
        draft = draft.model_copy(update={
            "summary": redact(draft.summary),
            "flag": draft.flag.model_copy(update={"default": redact(draft.flag.default),
                                                  "enabled": redact(draft.flag.enabled)})})
    unchecked = MaintainerCritique(verdict="pass", reason="(pending)")
    refusal = precheck_draft(pick, build_body(pick, draft, unchecked, generation=generation),
                             engine.task)
    if refusal is not None:
        return {"outcome": "rejected", "code": refusal}
    critique = parse_structured(
        client, critic_messages(pick["track"], context, draft, evidence_label=label),
        MaintainerCritique)
    # The critic's prose lands in the log twice (the row and the proposal): through the engine's ONE
    # redaction funnel first (`engine/audit.py::Engine._redact`).
    reason = (redact(critique.reason) if callable(redact) else critique.reason)[:500]
    critique = critique.model_copy(update={"reason": reason or "(no reason given)"})
    if critique.verdict != "pass":
        return {"outcome": "declined", "reason": critique.reason}
    body = build_body(pick, draft, critique, generation=generation)
    retain_draft(engine.run_dir, body)
    return {"outcome": "drafted", "body": body, "reason": critique.reason}
