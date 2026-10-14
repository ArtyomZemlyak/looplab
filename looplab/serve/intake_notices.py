"""What the intake did to an AUTHORED command payload, said back to its author (review 2026-10-14).

An external agent publishes memos, reports, questions and comments through `POST /commands`. The
payload crosses the durable-boundary sanitizers (`core/advisory_payloads.py`, `core/redact.py`),
which bound every field and mask credential-shaped text — and until this module the command receipt
said `succeeded` whatever they did. Measured on the MiniOneRec agent's request bodies: findings of
7,200 characters stored as 1,200 plus a hash receipt, a fact about the data stored as
`***REDACTED***`, and 28.9 % of its prose lines glued into one token — all with a clean receipt.

Three kinds of notice, every one ADVISORY (the command is admitted exactly as before; an old client
that ignores the new receipt field loses nothing):

* `truncated` / `dropped` / `items_omitted` — a field or list the bounds cut, with the numbers;
* `redacted` — a field whose text the secret screen masked, and which screen (`credential_shape`,
  `env_value`, `entropy`), so an author can rephrase a false positive instead of losing the fact;
* `readability` — prose that looks glued (words without spaces). Never corrected here: inserting
  spaces heuristically can corrupt identifiers (doc 76 §2); the author is asked to resend.

`intake_notices` is a pure function of the submitted and the normalized payload. It is computed at
admission and stored on the command RECORD (`run_commands.py`), never in the event: the event log
stays byte-identical to what it was.
"""
from __future__ import annotations

import re

# The fields the notices are worth computing for: authored prose. Server-derived receipts beside
# them (`claims_receipt`, `at_node`, verification) are not the author's text.
_SKIP_KEYS = frozenset({"at_node", "trigger", "claims_receipt", "evidence_receipt", "verification",
                        "memo_id", "provenance", "numbers", "url_identities", "url_identity",
                        "snapshot_superseded", "served_manual", "node_generation", "generation"})
_TRUNCATION = re.compile(r"\[redacted preview: original_chars=(\d+) sha256=[0-9a-f]{64}\]\Z")
MAX_NOTICES = 64
MAX_SAMPLES = 3

# --------------------------------------------------------------------------- readability
# A GLUED token: prose whose words lost their spaces. The rules are the review prototype's
# (`prototypes/detect.py`), which measured 0 false positives on 658 readable lines (the internal
# researcher's memos and the agent's own rewritten text) and flagged 28.9 % of the agent's lines
# >= 40 characters before its 2026-10-13 rule, 10.1 % after:
#   * Cyrillic: >= 6 Cyrillic letters with a Cyrillic/Latin switch INSIDE the token, or a Cyrillic
#     run of >= 22 letters (Russian words that long are vanishingly rare);
#   * Latin: >= 24 characters with a lowercase run followed by an UPPERCASE run of >= 2 or the
#     reverse, or an all-lowercase run of >= 26 letters.
# Paths, URLs, hex digests and identifiers carrying `_ . / : =` are never flagged.
_CYR = "А-Яа-яЁё"
_TOKEN = re.compile(r"\S{16,}")
_STRIP = ".,;:()[]{}\"'«»—-!?`"


def glued_tokens(text) -> list[str]:
    """The tokens of `text` that look like words glued without spaces (bounded scan)."""
    out: list[str] = []
    for match in _TOKEN.finditer(str(text or "")[:20_000]):
        core = match.group(0).strip(_STRIP)
        if (not core or core.startswith(("http://", "https://", "/", "`"))
                or re.fullmatch(r"[0-9a-fA-F]{16,}", core)):
            continue
        cyr = len(re.findall(f"[{_CYR}]", core))
        switches = len(re.findall(f"[{_CYR}][A-Za-z]|[A-Za-z][{_CYR}]", core))
        if cyr >= 6 and (switches >= 1 or re.search(f"[{_CYR}]{{22,}}", core)):
            if not re.search(r"[_/=]", core):
                out.append(core)
            continue
        if cyr == 0 and not re.search(r"[_/=.:@]", core):
            letters = len(re.findall("[A-Za-z]", core))
            if (len(core) >= 24 and letters >= 20 and re.search(r"[a-z]{3,}", core)
                    and re.search(r"[a-z]{4,}[A-Z]{2,}|[A-Z]{2,}[a-z]{6,}", core)):
                out.append(core)
            elif re.fullmatch(r"[a-z]{26,}", core):
                out.append(core)
    return out


READABILITY_HINT = ("Some prose looks like words glued without spaces. Write complete sentences "
                    "with normal spaces between words; put identifiers, paths and metric names in "
                    "backticks. LoopLab stored the text as sent and never inserts spaces itself.")


def _redaction_reason(before: str, after: str) -> str:
    if after.count("***REDACTED_ENV***") > before.count("***REDACTED_ENV***"):
        return "env_value"
    if after.count("***REDACTED***") > before.count("***REDACTED***"):
        return "entropy"
    return "credential_shape"


def _walk(raw, kept, path: str, out: list) -> None:
    if len(out) >= MAX_NOTICES:
        return
    if isinstance(raw, str) and isinstance(kept, str):
        cut = _TRUNCATION.search(kept)
        if cut is not None and _TRUNCATION.search(raw) is None:
            out.append({"kind": "truncated", "field": path,
                        "original_chars": int(cut.group(1)),
                        "kept_chars": max(0, cut.start() - 1)})
        elif raw.strip() and not kept.strip():
            out.append({"kind": "dropped", "field": path, "original_chars": len(raw),
                        "reason": "total_text_budget"})
        if kept.count("***") > raw.count("***"):
            out.append({"kind": "redacted", "field": path,
                        "reason": _redaction_reason(raw, kept)})
        return
    if isinstance(raw, list) and isinstance(kept, list):
        if len(raw) > len(kept):
            out.append({"kind": "items_omitted", "field": path,
                        "submitted": len(raw), "kept": len(kept)})
        for index, (r, k) in enumerate(zip(raw, kept)):
            _walk(r, k, f"{path}[{index}]", out)
        return
    if isinstance(raw, dict) and isinstance(kept, dict):
        for key, value in raw.items():
            if key in _SKIP_KEYS or key not in kept:
                continue
            _walk(value, kept[key], f"{path}.{key}" if path else str(key), out)


def _readability(kept, path: str, out: list) -> None:
    if len(out) >= MAX_NOTICES:
        return
    if isinstance(kept, str):
        glued = glued_tokens(kept)
        if glued:
            out.append({"kind": "readability", "field": path,
                        "samples": [g[:80] for g in glued[:MAX_SAMPLES]]})
    elif isinstance(kept, list):
        for index, item in enumerate(kept):
            _readability(item, f"{path}[{index}]", out)
    elif isinstance(kept, dict):
        for key, value in kept.items():
            if key not in _SKIP_KEYS and key not in ("urls", "url", "node_ids", "concepts",
                                                     "question_concepts", "evidence_ids"):
                _readability(value, f"{path}.{key}" if path else str(key), out)


# Which part of each authored command is prose an author wrote, by event type. A type not named
# here gets no notices: its fields are ids, numbers or operator controls.
_AUTHORED_ROOTS = {
    "research_completed": ("memo",),
    "report_generated": ("content",),
    "hypothesis_added": ("statement",),
    "card_edited": ("statement", "title", "rationale"),
    "comment_created": ("text", "body"),
    "annotation": ("text", "note"),
    "inject_node": ("idea",),
}


def intake_notices(event_type: str, raw: dict, normalized: dict) -> list[dict]:
    """Every notice for one admitted command; `[]` when the intake changed nothing worth saying."""
    roots = _AUTHORED_ROOTS.get(str(event_type))
    if not roots or not isinstance(raw, dict) or not isinstance(normalized, dict):
        return []
    out: list[dict] = []
    for root in roots:
        if root in raw and root in normalized:
            _walk(raw[root], normalized[root], root, out)
    readability: list[dict] = []
    for root in roots:
        if root in normalized:
            value = normalized[root]
            if root == "idea" and isinstance(value, dict):
                # An idea's CODE and files are not prose; its explanation is.
                value = {k: value.get(k) for k in ("title", "rationale", "hypothesis",
                                                   "description", "summary") if k in value}
            _readability(value, root, readability)
    if readability:
        out.extend(readability[:MAX_NOTICES - len(out)] if len(out) < MAX_NOTICES else [])
        if len(out) < MAX_NOTICES + 1:
            out.append({"kind": "readability_hint", "message": READABILITY_HINT})
    return out[:MAX_NOTICES + 1]


# The prose fields of the harness POST bodies (decisions, reviews, lessons, skills, checkpoint
# answers, result commentary). Ids, action ids, code and evidence references are never scanned.
_PROSE_FIELDS = frozenset({"reason", "summary", "statement", "rationale", "text", "verdict",
                           "lesson", "description", "notes", "explanation", "comment", "title",
                           "body", "outcome", "justification", "headline"})


def readability_warnings(body) -> list[dict]:
    """`readability` notices (plus the hint) for the prose fields of one harness POST body."""
    try:
        data = body.model_dump() if hasattr(body, "model_dump") else body
    except (TypeError, ValueError):
        return []
    found: list[dict] = []

    def walk(value, path):
        if len(found) >= MAX_NOTICES:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if key in _PROSE_FIELDS and isinstance(child, str):
                    _readability(child, child_path, found)
                elif isinstance(child, (dict, list)):
                    walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(data, "")
    if found:
        found.append({"kind": "readability_hint", "message": READABILITY_HINT})
    return found


def with_readability(result, body):
    """`result` with `readability_warnings` added when the body's prose looks glued (advisory)."""
    if isinstance(result, dict):
        warnings = readability_warnings(body)
        if warnings:
            return {**result, "readability_warnings": warnings}
    return result


# --------------------------------------------------------------------------- question board
# External memos carried 212 open questions (69 unique) and the board registered none: the
# internal researcher's steering step that turns `open_questions` into cards does not run for an
# external author, and the receipt said `succeeded` (review 2026-10-14 section 4). Until a server-side
# projection exists these notices SAY so, with the exact command that files the question, and
# remind an experiment to name the question it answers (`question_filing`).
_CLOSED_CARD_STATUSES = frozenset({"dropped", "deleted", "abandoned"})


def _norm_statement(text) -> str:
    return " ".join(str(text or "").lower().split())


def _open_questions(state) -> list:
    cards = getattr(state, "cards", None) or {}
    return [card for card in cards.values()
            if getattr(card, "card_kind", None) == "direction"
            and getattr(card, "merged_into", None) is None
            and getattr(card, "status", None) not in _CLOSED_CARD_STATUSES]


def board_notices(event_type: str, normalized: dict, state) -> list[dict]:
    """Question-board notices for one admitted command against the run's current board."""
    if state is None or not isinstance(normalized, dict):
        return []
    if event_type == "research_completed":
        memo = normalized.get("memo") if isinstance(normalized.get("memo"), dict) else {}
        board = {_norm_statement(getattr(card, "statement", "")) for card in
                 (getattr(state, "cards", None) or {}).values()}
        missing = [q for q in memo.get("open_questions") or []
                   if isinstance(q, str) and q.strip() and _norm_statement(q) not in board]
        if missing:
            return [{"kind": "questions_not_on_board", "count": len(missing),
                     "samples": [q[:160] for q in missing[:MAX_SAMPLES]],
                     "message": ("These open questions are not on the Research question board. "
                                 "File each one that experiments will answer with a "
                                 "hypothesis_added command (statement, concepts), then name it "
                                 "in idea.parent_card_id of those experiments.")}]
    elif event_type == "inject_node":
        idea = normalized.get("idea") if isinstance(normalized.get("idea"), dict) else {}
        if not idea.get("parent_card_id"):
            questions = _open_questions(state)
            if questions:
                return [{"kind": "experiment_not_filed", "open_questions": len(questions),
                         "message": ("This experiment names no research question. Set "
                                     "idea.parent_card_id to the question it answers (GET /state "
                                     "cards with card_kind=direction), or file it later with "
                                     "card_filed.")}]
    return []
