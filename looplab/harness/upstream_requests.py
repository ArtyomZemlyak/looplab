"""Typed bounded retained-request pages, with no automatic paging or retry."""
import base64
import hashlib
import json
import re


def valid_page(page, generation, proposal_id, request_hash, offset, limit, content_hash):
    if not isinstance(page, dict):
        return False
    sha = lambda value: isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
    integer = lambda value: type(value) is int and 0 <= value <= 2 ** 53 - 1
    action = page.get("action_id")
    if (type(page.get("version")) is not int or page["version"] != 1
            or page.get("generation") != generation or page.get("proposal_id") != proposal_id
            or page.get("request_hash") != request_hash or not sha(page.get("request_generation"))
            or page.get("authority") != "diagnostic_only"
            or page.get("source_health") != {"events": "complete", "request": "complete"}
            or page.get("request_path") != f"upstream/requests/{proposal_id}/request.json"
            or not isinstance(action, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", action) is None
            or proposal_id != "up_" + hashlib.sha256(json.dumps(action).encode()).hexdigest()[:24]
            or not sha(page.get("content_sha256")) or not sha(page.get("chunk_sha256"))
            or content_hash is not None and page["content_sha256"] != content_hash
            or page.get("encoding") != "base64" or not isinstance(page.get("chunk"), str)
            or len(page["chunk"]) > 5464
            or not integer(page.get("offset")) or page["offset"] != offset
            or type(page.get("limit")) is not int or page["limit"] != limit
            or not integer(page.get("total_bytes")) or not offset < page["total_bytes"] <= 2 * 1024 * 1024):
        return False
    status, claim, settlement = (page.get(key) for key in ("claim_status", "claim_seq", "settlement_seq"))
    if (status not in ("unclaimed", "unresolved", "completed", "abandoned")
            or "claim_seq" not in page or "settlement_seq" not in page
            or (claim is not None and not integer(claim))
            or (settlement is not None and (not integer(settlement) or claim is None or settlement <= claim))
            or (status == "unclaimed") != (claim is None)
            or (status in ("completed", "abandoned")) != (settlement is not None)):
        return False
    try:
        chunk = base64.b64decode(page["chunk"], validate=True)
    except (ValueError, UnicodeError):
        return False
    end = offset + len(chunk)
    if offset == 0 and end == page["total_bytes"]:
        # A complete single-page response can verify the entire source now;
        # multi-page callers must assemble and verify explicitly themselves.
        try:
            body = json.loads(chunk)
            canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False, allow_nan=False).encode()
            if (not isinstance(body, dict) or body.get("action_id") != action
                    or body.get("expected_generation") != page["request_generation"]
                    or hashlib.sha256(chunk).hexdigest() != page["content_sha256"]
                    or hashlib.sha256(canonical).hexdigest() != request_hash):
                return False
        except (ValueError, TypeError, RecursionError):
            return False
    return (len(chunk) == min(limit, page["total_bytes"] - offset)
            and base64.b64encode(chunk).decode() == page["chunk"]
            and hashlib.sha256(chunk).hexdigest() == page["chunk_sha256"]
            and "next_offset" in page
            and (page["next_offset"] is None if end == page["total_bytes"] else
                 integer(page["next_offset"]) and page["next_offset"] == end))
