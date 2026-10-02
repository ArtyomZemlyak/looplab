"""Doc 72: complete source reads, immutable active bases and hunk nominations.

Nomination is advice. Only a measured gate and a fenced base_advanced publication
grant a new base; model prose and a supplied receipt grant nothing.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import math
import re
from pathlib import Path

from looplab.core.errors import UpstreamRefusal
from looplab.core.node_evidence import read_bounded_regular_file
from looplab.events.eventstore import decode_event_record, event_sequence_continues
from looplab.events.replay import event_generation_binds, fold
from looplab.events.run_generation import run_generation_token

UPSTREAM_EVENTS = frozenset({"upstream_proposed", "upstream_gate_started", "upstream_gate_finished",
                            "upstream_gate_abandoned", "base_advanced"})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def events_for(rd):
    path = Path(rd) / "events.jsonl"
    raw = read_bounded_regular_file(path, 32 * 1024 * 1024 + 1)
    if raw is None or len(raw) > 32 * 1024 * 1024 or not raw.endswith(b"\n"):
        raise UpstreamRefusal("upstream_source_unavailable", "events.jsonl is missing, oversized or incomplete; restore it explicitly before any upstream action")
    events, expected = [], 0
    try:
        for line in raw.splitlines():
            batch = decode_event_record(json.loads(line), strict=True)
            if not event_sequence_continues(batch, expected):
                raise ValueError("Incomplete event sequence")
            events.extend(batch)
            expected = batch[-1].seq + 1
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise UpstreamRefusal("upstream_source_unavailable", "Inspect and restore events.jsonl before any upstream action") from exc
    return events


def claimed_gate_executions(events, gate):
    """Bind measured charges to one preceding, matching, unrevoked gate claim.

    A complete result can arrive after operator abandonment or retain charges
    whose durable start is unavailable. Neither grants fresh CAS authority.
    Exact ACK reads remain historical; this fence is for a new base publication.
    """
    action, proposal = gate.data.get("action_id"), gate.data.get("proposal_id")
    message = "Inspect events.jsonl: the gate needs one matching current claim before its executions and one completion, without operator abandonment; check afresh with a new action ID"
    if not isinstance(action, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", action) is None:
        raise UpstreamRefusal("upstream_gate_required", message)
    related = [e for e in events if (e.type.startswith("upstream_") or e.type == "base_advanced")
               and e.data.get("action_id") == action]
    starts = [e for e in related if e.type == "upstream_gate_started"]
    finishes = [e for e in related if e.type == "upstream_gate_finished"]
    expected_hash = digest({"expected_generation": run_generation_token(events),
                            "action_id": action, "proposal_id": proposal})
    if (len(starts) != 1 or len(finishes) != 1 or finishes[0].seq != gate.seq
            or gate.data.get("request_hash") != expected_hash
            or any(e.type not in ("upstream_gate_started", "upstream_execution", "upstream_gate_finished") for e in related)
            or any(e.type == "upstream_gate_abandoned" and e.data.get("claim_action_id") == action for e in events)):
        raise UpstreamRefusal("upstream_gate_required", message)
    start = starts[0]
    result = gate.data.get("result")
    if (not isinstance(result, dict) or start.seq >= gate.seq
            or start.data.get("proposal_id") != proposal or start.data.get("request_hash") != expected_hash
            or start.data.get("input_identity") != result.get("input_identity")):
        raise UpstreamRefusal("upstream_gate_required", message)
    charges = [e for e in related if e.type == "upstream_execution"]
    if any(not start.seq < e.seq < gate.seq or e.data.get("proposal_id") != proposal
           or e.data.get("request_hash") != expected_hash for e in charges):
        raise UpstreamRefusal("upstream_gate_required", message)
    return [e.data.get("execution") for e in charges]


def active_base(events, initial):
    advanced = [e for e in events if e.type == "base_advanced"]
    last = advanced[-1] if advanced else None
    try:
        selector = last.data["selector"] if last else initial
        if selector is not None:
            from looplab.engine.seed_base import normalize_seed_base, selected_seed_base
            selector = normalize_seed_base(selector)
            selected_seed_base(selector)
    except (ValueError, TypeError, KeyError) as exc:
        raise UpstreamRefusal("upstream_source_unavailable", "Active base selection in events.jsonl is invalid") from exc
    return {"selector": selector, "revision": digest({"generation": run_generation_token(events),
            "selector": selector, "advance_seq": last.seq if last else None}),
            "advance_seq": last.seq if last else None}


def node_signature(node):
    return digest({"id": node.id, "generation": node.attempt, "files": node.files,
                   "idea": node.idea.model_dump(),
                   "deleted": node.deleted, "metric": node.task_metric if node.task_metric is not None else node.metric,
                   "status": node.status.value, "tombstoned": node.tombstoned,
                   "provenance": node.metric_provenance})


def source_node(events, node_id):
    node = fold(events).nodes.get(node_id)
    return node, _source_receipt(node, {e.seq: e for e in events})


def _source_receipt(node, events_by_seq):
    """Share primary-score and seed identity eligibility with nomination reads."""
    from looplab.engine.seed_archive import seed_archive_digest
    if (node is None or node.tombstoned or node.status.value != "evaluated"
            or node.metric is None or not math.isfinite(node.metric) or not node.feasible
            or node.violations or (node.metric_provenance or {}).get("salvaged")):
        raise UpstreamRefusal("upstream_source_not_measured", "Choose a current completed node with a primary measured score")
    receipt = (node.metric_provenance or {}).get("base_revision")
    message = "The source node has no complete archived seed identity"
    if not isinstance(receipt, dict) or any(type(receipt.get(k)) is not int or receipt[k] < 0
            for k in ("node_id", "generation", "seed_event_seq")):
        raise UpstreamRefusal("upstream_source_unavailable", message)
    seed = events_by_seq.get(receipt["seed_event_seq"])
    seed_base = seed.data.get("base_revision") if seed is not None else None
    archive_digest = seed_archive_digest(receipt)
    if (receipt["node_id"] != node.id or receipt["generation"] != node.attempt
            or seed is None or seed.type != "workspace_seeded"
            or type(node.terminal_event_seq) is not int or seed.seq >= node.terminal_event_seq
            or type(seed.data.get("node_id")) is not int or seed.data["node_id"] != node.id
            or not event_generation_binds(seed.data, node.attempt) or archive_digest is None
            or seed_archive_digest(seed_base) != archive_digest
            or any(seed_base[k] != receipt[k] for k in ("file_count", "bytes"))):
        raise UpstreamRefusal("upstream_source_unavailable", message)
    score = node.task_metric
    if score is None or not math.isfinite(score):
        raise UpstreamRefusal("upstream_source_not_measured", "The source task score is unavailable")
    return receipt


def repair_origin(events, node, name, changed_lines):
    """Only an applied repair in this lifecycle; triggers use failing, not fixed values."""
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if event.type != "node_repaired" or event.data.get("node_id") != node.id:
            continue
        prior = fold(events[:index]).nodes.get(node.id)
        if prior is None or prior.attempt != node.attempt or prior.status.value != "pending" or prior.tombstoned:
            continue
        repaired = fold(events[:index + 1]).nodes.get(node.id)
        old, new = prior.files.get(name, ""), repaired.files.get(name, "")
        if old == new or not any(line in changed_lines for line in set(new.splitlines()) - set(old.splitlines())):
            continue
        assignments = lambda text: {line.split("=", 1)[0]: line for line in map(str.strip, text.splitlines())
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", line)}
        before, after = assignments(old), assignments(new)
        tokens = [line for key, line in before.items() if after.get(key) != line]
        return event, tokens
    return None, []


def upstream_candidates(rd, task, events=None):
    """Bounded hunk/range/hash advice, including exact pending env repair triggers."""
    from looplab.engine.activation import CHANGE_CAPABILITY, is_config_path
    from looplab.engine.seed_archive import verified_seed_archive
    events = events if events is not None else events_for(rd)
    state, rows = fold(events), []
    events_by_seq = {e.seq: e for e in events}
    promoted = {h for e in events if e.type == "base_advanced" for h in e.data.get("hunk_hashes", [])}
    for node in sorted(state.nodes.values(), key=lambda n: (n.id != state.best_node_id, n.id)):
        try:
            receipt = _source_receipt(node, events_by_seq)
        except UpstreamRefusal:
            continue  # an ineligible source grants no nomination; reads stay diagnostic
        archive = verified_seed_archive(rd, receipt)
        if archive is None:
            continue
        for name in sorted(set(node.files) | set(node.deleted))[:128]:
            raw = read_bounded_regular_file(archive / name, 1024 * 1024 + 1)
            if raw is not None and len(raw) > 1024 * 1024:
                continue  # bounded advice; the full archive remains authoritative
            try:
                before = raw.decode("utf8") if raw is not None else ""
            except UnicodeError:
                continue
            after = node.files.get(name, "")
            a, b = before.splitlines(keepends=True), after.splitlines(keepends=True)
            for group in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_grouped_opcodes(3):
                changed = [x for x in group if x[0] != "equal"]
                if not changed:
                    continue
                i, j, k, l = changed[0][1], changed[-1][2], changed[0][3], changed[-1][4]
                signature = digest({"path": name, "old": a[i:j], "new": b[k:l]})
                repair, tokens = repair_origin(events, node, name, after.splitlines()[k:l])
                triggers = [n.id for n in state.pending_nodes() if n.id != node.id and
                            any(token in list(map(str.strip, n.files.get(name, "").splitlines())) for token in tokens)]
                capability = not is_config_path(name) or (repair is not None and bool(triggers))
                rows.append({"node_id": node.id, "generation": node.attempt, "path": name,
                    "old_range": [i + 1, j], "new_range": [k + 1, l], "hunk_hash": signature,
                    "classification": "already_promoted" if signature in promoted else
                        CHANGE_CAPABILITY if capability else "recipe",
                    "origin": "repair" if repair else "idea", "repair_seq": repair.seq if repair else None,
                    "repair_reason": ((repair.data.get("reason_summary") or repair.data.get("reason") or repair.data.get("rationale")) if repair else None),
                    "trigger_tokens": {name: tokens} if repair and triggers else {},
                    "pending_trigger_nodes": triggers, "source_signature": node_signature(node)})
                if len(rows) >= 200:
                    return {"rows": rows, "bounded": True, "limit": 200}
    return {"rows": rows, "bounded": False, "limit": 200}
