"""Bounded, always-redacted projections for untrusted cross-run memory.

Cross-run rows outlive the run that produced them and are reused in agent prompts, HTTP responses and
operator audit views.  Treat every persisted string as untrusted at those read boundaries, including
legacy rows written before durable sanitizers existed.
"""
from __future__ import annotations

import math
from itertools import islice

from looplab.trust.redact import is_secret_key_name, redact_persisted_text

_OPAQUE_IDENTITY_KEYS = frozenset({
    "action_id", "invocation_id", "claim_uid", "scope", "scope_task", "task_id", "run_id",
    "excluded_run", "metric", "key", "evidence_digest", "governance_digest", "corpus_digest",
    "retrieval_digest", "render_digest", "input_digest", "support", "oppose", "unverified",
    "runs", "scopes", "at",
})


def cross_run_text(value, *, max_chars: int, single_line: bool = True,
                   entropy: bool = True) -> str:
    """Apply the repository's always-on durable redaction contract to one cross-run field."""
    text = redact_persisted_text(
        value, max_chars=max_chars, entropy=entropy, single_line=single_line)
    # ``redact_persisted_text``'s honest truncation receipt is multi-line even when its input was
    # collapsed. Cross-run fields are prompt/API labels, so enforce the single-line contract after the
    # marker is added as well.
    return " ".join(text.split()) if single_line else text


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

    def walk(item, depth: int, *, entropy: bool = True):
        if remaining[0] <= 0:
            return ""
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, str):
            return safe_text(item, entropy=entropy)
        if type(item) is int:
            return item if -(1 << 63) <= item <= (1 << 63) - 1 else safe_text(item, cap=128)
        if type(item) is float:
            return item if math.isfinite(item) else safe_text(item, cap=32)
        if depth >= depth_cap:
            return "<depth-limited>"
        if isinstance(item, dict):
            out = {}
            try:
                for key, child in islice(item.items(), item_cap):
                    if total_items[0] <= 0 or remaining[0] <= 0:
                        break
                    total_items[0] -= 1
                    safe_key = safe_text(key, cap=160)
                    if not safe_key:
                        continue
                    if is_secret_key_name(key):
                        # CODEX AGENT: redact by the original structured key before stringifying the
                        # child.  A nested ``api_key`` must never rely on its value looking secret-like.
                        out[safe_key] = "***"
                        remaining[0] = max(0, remaining[0] - 3)
                    else:
                        child_entropy = str(key).casefold() not in _OPAQUE_IDENTITY_KEYS
                        out[safe_key] = walk(child, depth + 1, entropy=child_entropy)
                return out
            except Exception:  # noqa: BLE001 - legacy projections must fail closed, never fail the API
                return "<mapping unavailable>"
        if isinstance(item, (list, tuple)):
            out = []
            for child in islice(item, item_cap):
                if total_items[0] <= 0 or remaining[0] <= 0:
                    break
                total_items[0] -= 1
                out.append(walk(child, depth + 1, entropy=entropy))
            return out
        return safe_text(item)

    return walk(value, 0)
