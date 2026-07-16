"""Bounded, always-redacted projections for untrusted cross-run memory.

Cross-run rows outlive the run that produced them and are reused in agent prompts, HTTP responses and
operator audit views.  Treat every persisted string as untrusted at those read boundaries, including
legacy rows written before durable sanitizers existed.
"""
from __future__ import annotations

import math
from itertools import islice

from looplab.trust.redact import (
    is_secret_key_name,
    redact_persisted_identity,
    redact_persisted_text,
)

_OPAQUE_IDENTITY_KEYS = frozenset({
    "action_id", "invocation_id", "claim_uid", "scope", "scope_task", "task_id", "run_id",
    "excluded_run", "metric", "key", "evidence_digest", "governance_digest", "corpus_digest",
    "retrieval_digest", "render_digest", "input_digest", "support", "oppose", "unverified",
    "runs", "scopes", "at",
})
_LIVE_DIRECTIONS = frozenset({"min", "max"})


def valid_live_direction(value) -> bool:
    """Whether ``value`` is an exact supported objective direction for agent-facing reuse."""
    return isinstance(value, str) and value in _LIVE_DIRECTIONS


def same_live_direction(current, persisted) -> bool:
    """Whether persisted evidence can influence a live run with ``current`` direction.

    Audit projections may deliberately show legacy rows without direction provenance, but an
    agent-facing prompt/tool must know that the objective polarity is identical before reusing them.
    """
    # CODEX AGENT: direction is part of the semantic identity of live guidance. Missing, coerced or
    # garbled provenance fails closed; an exact task id must never manufacture polarity compatibility.
    return (valid_live_direction(current) and isinstance(persisted, str)
            and persisted == current)


def cross_run_text(value, *, max_chars: int, single_line: bool = True,
                   entropy: bool = True) -> str:
    """Apply the repository's always-on durable redaction contract to one cross-run field."""
    text = redact_persisted_text(
        value, max_chars=max_chars, entropy=entropy, single_line=single_line)
    # ``redact_persisted_text``'s honest truncation receipt is multi-line even when its input was
    # collapsed. Cross-run fields are prompt/API labels, so enforce the single-line contract after the
    # marker is added as well.
    return " ".join(text.split()) if single_line else text


def cross_run_identity_text(value, *, max_chars: int) -> str:
    """Bound/redact an opaque cross-run identity without changing its Unicode equality key."""
    return redact_persisted_identity(value, max_chars=max_chars)


def sanitize_cross_run_projection(value, *, max_chars: int = 128_000,
                                  max_items: int = 256, max_depth: int = 6,
                                  max_total_items: int = 8_192,
                                  max_text_chars: int = 4_000):
    """Return a bounded JSON-compatible copy of a cross-run API/audit payload.

    The aggregate limits are intentionally generous enough for the documented endpoint pages while
    still preventing a malformed legacy/model row from expanding without bound.  Callers that page rows
    sanitize each row independently so redaction never changes the public page length/count contract.
    """
    remaining = [max(0, int(max_chars))]
    total_items = [max(0, int(max_total_items))]
    item_cap = max(0, int(max_items))
    depth_cap = max(0, int(max_depth))
    text_cap = max(0, int(max_text_chars))

    def safe_text(item, *, cap: int | None = None, entropy: bool = True) -> str:
        allowed = min(remaining[0], text_cap if cap is None else max(0, int(cap)))
        text = cross_run_text(item, max_chars=allowed, single_line=True, entropy=entropy)
        remaining[0] = max(0, remaining[0] - len(text))
        return text

    def safe_identity(item, *, cap: int | None = None) -> str:
        allowed = min(remaining[0], text_cap if cap is None else max(0, int(cap)))
        text = cross_run_identity_text(item, max_chars=allowed)
        remaining[0] = max(0, remaining[0] - len(text))
        return text

    def walk(item, depth: int, *, entropy: bool = True, identity: bool = False):
        if remaining[0] <= 0:
            return ""
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, str):
            return safe_identity(item) if identity else safe_text(item, entropy=entropy)
        if type(item) is int:
            return (item if -(1 << 63) <= item <= (1 << 63) - 1
                    else safe_identity(item, cap=128) if identity else safe_text(item, cap=128))
        if type(item) is float:
            return (item if math.isfinite(item)
                    else safe_identity(item, cap=32) if identity else safe_text(item, cap=32))
        if depth >= depth_cap:
            return "<depth-limited>"
        if isinstance(item, dict):
            out = {}
            try:
                candidates = []
                groups: dict[str, list[tuple[int, object, object, str]]] = {}
                key_cap = 160
                candidate_cap = min(item_cap, total_items[0])
                for index, (key, child) in enumerate(islice(item.items(), candidate_cap)):
                    safe_key = cross_run_text(
                        key, max_chars=key_cap, single_line=True, entropy=True)
                    if not safe_key:
                        continue
                    candidate = (index, key, child, safe_key)
                    candidates.append(candidate)
                    groups.setdefault(safe_key, []).append(candidate)

                selected: dict[int, tuple[int, object, object, str]] = {}
                for safe_key, aliases in groups.items():
                    if len(aliases) == 1:
                        selected[aliases[0][0]] = aliases[0]
                        continue
                    # CODEX AGENT: NFKC is useful for schema/display keys but is not injective.  A
                    # compatibility-spelled alias must never overwrite an exact governance field such as
                    # ``decision``. Prefer the unique exact spelling; if none exists, omit the ambiguous
                    # field entirely instead of making input order authoritative.
                    exact = [candidate for candidate in aliases
                             if isinstance(candidate[1], str) and candidate[1] == safe_key]
                    if len(exact) == 1:
                        selected[exact[0][0]] = exact[0]

                for index, key, child, safe_key in candidates:
                    if index not in selected:
                        continue
                    if total_items[0] <= 0 or remaining[0] <= 0:
                        break
                    total_items[0] -= 1
                    if len(safe_key) > remaining[0]:
                        break
                    remaining[0] -= len(safe_key)
                    if is_secret_key_name(key):
                        # CODEX AGENT: redact by the original structured key before stringifying the
                        # child.  A nested ``api_key`` must never rely on its value looking secret-like.
                        # (Classifying the truncated/entropy-masked ``safe_key`` here would let a secret
                        # name that the 160-char cap or entropy mask rewrote slip past and leak its value.)
                        out[safe_key] = "***"
                        remaining[0] = max(0, remaining[0] - 3)
                    else:
                        child_identity = safe_key.casefold() in _OPAQUE_IDENTITY_KEYS
                        out[safe_key] = walk(
                            child, depth + 1, entropy=not child_identity, identity=child_identity)
                return out
            except Exception:  # noqa: BLE001 - legacy projections must fail closed, never fail the API
                return "<mapping unavailable>"
        if isinstance(item, (list, tuple)):
            out = []
            for child in islice(item, item_cap):
                if total_items[0] <= 0 or remaining[0] <= 0:
                    break
                total_items[0] -= 1
                out.append(walk(child, depth + 1, entropy=entropy, identity=identity))
            return out
        return safe_identity(item) if identity else safe_text(item)

    return walk(value, 0)
