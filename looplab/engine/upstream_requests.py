"""Bounded diagnostic recovery of retained proposal bytes; never execution authority."""
import base64
import hashlib
import json
import re

from looplab.core.errors import UpstreamRefusal
from looplab.core.node_evidence import read_bounded_regular_file
from looplab.engine.upstream_spec import normalize_request
from looplab.engine.upstream_state import digest
from looplab.engine.upstream_workspace import owned_path

MAX_BYTES = 2 * 1024 * 1024
MAX_PAGE = 4096


def validate_selector(proposal_id, request_hash, offset, limit, content_hash):
    if (not isinstance(proposal_id, str) or re.fullmatch(r"up_[0-9a-f]{24}", proposal_id) is None
            or not isinstance(request_hash, str) or re.fullmatch(r"[0-9a-f]{64}", request_hash) is None
            or type(offset) is not int or not 0 <= offset < MAX_BYTES
            or type(limit) is not int or not 1 <= limit <= MAX_PAGE
            or offset > 0 and content_hash is None
            or content_hash is not None and (not isinstance(content_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", content_hash) is None)):
        raise ValueError("Use a proposal/hash, byte offset, limit 1..4096; subsequent pages require the original content hash")


def request_page(lane, generation, proposal_id, request_hash, *, offset=0, limit=2048, content_hash=None):
    try:
        validate_selector(proposal_id, request_hash, offset, limit, content_hash)
    except ValueError as exc:
        raise UpstreamRefusal("upstream_request_invalid",
            "Use the recorded proposal/hash and a bounded byte page; continuation requires the original content hash") from exc
    lane._current(generation)
    relative = f"upstream/requests/{proposal_id}/request.json"
    try:
        raw = read_bounded_regular_file(owned_path(lane.rd, relative), MAX_BYTES + 1)
        if raw is None or not 0 < len(raw) <= MAX_BYTES:
            raise ValueError("Unavailable retained source")
        body = json.loads(raw)
        normalized = normalize_request("propose", body)
        action_id = normalized["action_id"]
        if (body != normalized or not isinstance(action_id, str)
                or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", action_id) is None):
            raise ValueError("Invalid retained identity")
        actual_hash = digest(body)
    except (OSError, ValueError, TypeError, RecursionError, UpstreamRefusal) as exc:
        raise UpstreamRefusal("upstream_request_unavailable",
            f"Inspect {relative}: restore the original bounded regular proposal request. This read starts no work") from exc
    if actual_hash != request_hash or proposal_id != "up_" + digest(action_id)[:24]:
        raise UpstreamRefusal("upstream_request_changed", "Retained proposal identity differs; preserve the source and inspect the original claim")
    sha = hashlib.sha256(raw).hexdigest()
    if content_hash is not None and sha != content_hash:
        raise UpstreamRefusal("upstream_request_changed", "Request bytes changed between pages; discard the partial read and refresh explicitly")
    if offset >= len(raw):
        raise UpstreamRefusal("upstream_request_offset_invalid", "Offset is outside the retained request")
    # No exclusive sequencer wait on GET. Re-read the generation and claim source
    # after the bounded file read; a lifecycle reset invalidates this page.
    events = lane._current(generation)
    claims = [e for e in events if e.type == "upstream_proposal_started" and e.data.get("proposal_id") == proposal_id]
    if any(e.data.get("action_id") != action_id or e.data.get("request_hash") != request_hash for e in claims):
        raise UpstreamRefusal("upstream_request_changed", "Retained request differs from its recorded claim")
    claim = claims[-1] if claims else None
    settlements = [e for e in events if claim is not None and e.seq > claim.seq and
        (e.type in ("upstream_proposed", "upstream_proposal_failed") and e.data.get("action_id") == action_id
         or e.type == "upstream_gate_abandoned" and e.data.get("claim_action_id") == action_id)]
    settled = settlements[-1] if settlements else None
    status = ("unclaimed" if claim is None else "unresolved" if settled is None else
              "abandoned" if settled.type == "upstream_gate_abandoned" else "completed")
    chunk = raw[offset:offset + limit]
    end = offset + len(chunk)
    return {"version": 1, "generation": generation, "proposal_id": proposal_id,
        "action_id": action_id, "request_generation": body["expected_generation"],
        "request_hash": request_hash, "request_path": relative, "content_sha256": sha,
        "source_health": {"events": "complete", "request": "complete"},
        "authority": "diagnostic_only", "claim_status": status,
        "claim_seq": claim.seq if claim else None, "settlement_seq": settled.seq if settled else None,
        "offset": offset, "limit": limit, "total_bytes": len(raw),
        "next_offset": end if end < len(raw) else None,
        "encoding": "base64", "chunk": base64.b64encode(chunk).decode("ascii"),
        "chunk_sha256": hashlib.sha256(chunk).hexdigest()}
