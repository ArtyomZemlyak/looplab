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

What the author is NOT: evidence. Its prose and the critic's verdict only admit a proposal to the
gate; the base moves on measurements alone.
"""
from __future__ import annotations

import difflib
import json
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


def author_action_id(node, track: str) -> str:
    """The deterministic action id of one source LIFECYCLE on one track: a re-built node is a new
    source, and a repaired champion is asked once for its fix and once for its capability."""
    from looplab.engine.upstream_state import digest, node_signature
    return "auto-author-" + digest({"node_id": node.id, "generation": int(node.attempt or 0),
                                    "signature": node_signature(node), "track": track})[:24]


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


def _sources(state, events) -> list[tuple[str, object]]:
    """`(track, node)` in the order the author asks them: repaired lifecycles newest first, then the
    champion — a repaired champion appears on both tracks (its fix, then its capability). Evaluated,
    live nodes only; the lane's own eligibility is checked after."""
    from looplab.events.replay import event_generation_binds
    out, seen = [], set()
    repaired = []
    for e in events:
        if e.type != "node_repaired":
            continue
        node = state.nodes.get(e.data.get("node_id"))
        if node is not None and event_generation_binds(e.data, node.attempt):
            repaired.append(node)
    for node in sorted(repaired, key=lambda n: -n.id):
        if node.id not in seen and node.status.value == "evaluated" and not node.tombstoned:
            seen.add(node.id)
            out.append(("repair", node))
    best = state.nodes.get(state.best_node_id) if state.best_node_id is not None else None
    if best is not None and not best.tombstoned:
        out.append(("champion", best))
    return out


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
    import json

    from looplab.core.node_evidence import read_bounded_regular_file
    raw = read_bounded_regular_file(_draft_path(rd, action_id), 2 * 1024 * 1024 + 1)
    try:
        body = json.loads(raw) if raw is not None and len(raw) <= 2 * 1024 * 1024 else None
    except ValueError:
        return None
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


def author_next(rd, task, state, events, *, skipped=None) -> Optional[dict]:
    """The next source to author from, or None. `{"action_id", "track", "node", "rows", "archive",
    "revision"}`.

    `skipped` is the caller's per-process memo, `{action_id: signature}`, ADDED TO here when it is a
    dict. A source the lane can never take (unmeasured, built on an older base — the base only
    advances — or with no readable archive) is memoized for good (`None`); one with no eligible
    capability hunk is memoized against the active base revision and the pending node ids, because
    a repair hunk whose trigger nodes are still pending becomes nominable once they settle."""
    from looplab.core.errors import UpstreamRefusal
    from looplab.engine.seed_archive import verified_seed_archive
    from looplab.engine.upstream_state import (_source_receipt, active_base, repair_probe_covers,
                                               upstream_candidates)
    upstream = getattr(task, "upstream", None)
    if not upstream:
        return None
    ids, spent = _attempted(events)
    if spent >= AUTHOR_MAX_PER_RUN:
        return None
    try:
        active = active_base(events, getattr(task, "seed_base", None))
    except Exception:  # noqa: BLE001 — an unreadable base is the lane's refusal to state; the author waits
        return None
    memo = skipped if isinstance(skipped, dict) else {}
    pending = (active["revision"], tuple(sorted(n.id for n in state.pending_nodes())))
    by_seq = {e.seq: e for e in events}
    for track, node in _sources(state, events):
        action_id = author_action_id(node, track)
        if action_id in ids or (action_id in memo and memo[action_id] in (None, pending)):
            continue
        try:
            receipt = _source_receipt(node, by_seq)
        except UpstreamRefusal:
            memo[action_id] = None
            continue
        if receipt.get("digest") != active["selector"]["digest"]:
            memo[action_id] = None        # built on an older base: the lane refuses it as a source
            continue
        archive = verified_seed_archive(rd, receipt)
        if archive is None:
            memo[action_id] = None
            continue
        rows = [r for r in upstream_candidates(rd, task, events, source_node_id=node.id)["rows"]
                if r["classification"] == "capability"
                and (track == "champion" or r["origin"] == "repair")
                and not (r["origin"] == "repair" and r["pending_trigger_nodes"]
                         and not repair_probe_covers(r, upstream.get("repair_probes", [])))]
        if not rows:
            memo[action_id] = pending
            continue
        return {"action_id": action_id, "track": track, "node": node, "rows": rows,
                "archive": archive, "revision": active["revision"]}
    return None


def _read(archive, path) -> Optional[str]:
    from looplab.core.node_evidence import read_bounded_regular_file
    raw = read_bounded_regular_file(archive / path, AUTHOR_FILE_CHARS * 4 + 1)
    if raw is None:
        return ""
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
    for r in pick["rows"]:
        if r.get("repair_reason"):
            parts.append(f"REPAIR ({r['path']}): {str(r['repair_reason'])[:400]}")
    declaration = json.dumps({k: [p.get("name", p.get("command", "")) for p in v] if isinstance(v, list)
                              else v for k, v in (task.upstream or {}).items()}, ensure_ascii=False)
    parts.append("UPSTREAM DECLARATION (the gate the proposal must pass): "
                 + declaration[:AUTHOR_DECLARATION_CHARS])
    recipe = sorted(set(node.files) - set(paths))
    if recipe:
        parts.append("SOURCE RECIPE FILES (stay with the node, copied unchanged): " + ", ".join(recipe))
    for path in paths:
        before = _read(pick["archive"], path)
        after = node.files.get(path, "")
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
    patch = set(draft.files) | set(draft.deleted)
    # The SHARED implementation is the base's alone: a recipe may not carry a code path the patch
    # writes (`UpstreamLane._validate_shared_patch`). A configuration path the patch also changes
    # stays the node's, unless the draft says what the recipe needs there to turn the flag on.
    shared = {p for p in patch if not is_config_path(p)}
    recipe = {p: t for p, t in node.files.items() if p not in shared}
    recipe.update({p: t for p, t in draft.recipe_overrides.items() if p in patch and p not in shared})
    return {"expected_generation": generation, "action_id": pick["action_id"],
            "source_node_id": node.id, "expected_base_revision": pick["revision"],
            "hunk_hashes": sorted({r["hunk_hash"] for r in pick["rows"]}),
            "files": dict(draft.files), "deleted": sorted(set(draft.deleted)),
            "recipe_files": recipe, "recipe_deleted": sorted(set(node.deleted) - shared),
            "summary": draft.summary, "flag": draft.flag.model_dump(),
            "documentation_path": draft.documentation_path,
            "critic": {"verdict": critique.verdict, "reason": critique.reason,
                       "reviewer": AUTHOR_CRITIC_REVIEWER}}


def precheck_draft(pick, body) -> Optional[str]:
    """Why the lane would refuse this body whatever the critic says — or None. Run between the draft
    and the critic, so a draft that cannot be absorbed does not buy the second call: the request's
    own bounds (`normalize_request`: 2 MiB, 128 hunks), the documented flag, and the rule
    `UpstreamLane._propose_admit` refuses `upstream_capability_not_absorbed` by — the nominated code
    paths (less a repair's pending recipe) must all be in the patch, and it must implement one."""
    from looplab.core.errors import UpstreamRefusal
    from looplab.engine.activation import is_config_path
    from looplab.engine.upstream_spec import normalize_request
    try:
        normalize_request("propose", body)
    except UpstreamRefusal as exc:
        return exc.code
    doc = body["files"].get(body["documentation_path"])
    if not isinstance(doc, str) or body["flag"]["name"] not in doc:
        return "upstream_maintainer_invalid"
    patch = set(body["files"]) | set(body["deleted"])
    nominated = {r["path"] for r in pick["rows"]}
    repair_recipes = {r["path"] for r in pick["rows"] if r["origin"] == "repair"
                      and r["pending_trigger_nodes"] and is_config_path(r["path"])}
    implementation = (nominated - repair_recipes) | {
        p for p in patch if not is_config_path(p) and p != body["documentation_path"]}
    if not implementation or not implementation <= patch:
        return "upstream_capability_not_absorbed"
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
    client = getattr(getattr(engine, "developer", None), "client", None)
    if client is None:
        return {"outcome": "skipped", "reason": "no model client"}
    context = author_context(engine.task, pick)
    if context is None:
        return {"outcome": "skipped", "reason": "a nominated file is too large or not text"}
    # The context and the draft are the candidate's own code and rationale: fenced evidence, with
    # the guard at system authority, while the run's untrusted-evidence envelope is on — the one
    # reading every engine judge asks (`core/evidence.py`).
    label = judge_evidence_kwargs(engine).get("tool_result_label") or ""
    draft = parse_structured(client, author_messages(pick["track"], context, evidence_label=label),
                             MaintainerDraft)
    unchecked = MaintainerCritique(verdict="pass", reason="(pending)")
    refusal = precheck_draft(pick, build_body(pick, draft, unchecked, generation=generation))
    if refusal is not None:
        return {"outcome": "rejected", "code": refusal}
    critique = parse_structured(
        client, critic_messages(pick["track"], context, draft, evidence_label=label),
        MaintainerCritique)
    # The critic's prose lands in the log twice (the row and the proposal): through the engine's ONE
    # redaction funnel first (`engine/audit.py::Engine._redact`).
    redact = getattr(engine, "_redact", None)
    reason = (redact(critique.reason) if callable(redact) else critique.reason)[:500]
    critique = critique.model_copy(update={"reason": reason or "(no reason given)"})
    if critique.verdict != "pass":
        return {"outcome": "declined", "reason": critique.reason}
    body = build_body(pick, draft, critique, generation=generation)
    retain_draft(engine.run_dir, body)
    return {"outcome": "drafted", "body": body, "reason": critique.reason}
