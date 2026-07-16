"""Stable, credential-free identities for HTTP(S) research sources.

Display redaction is deliberately lossy. Evidence matching therefore carries a separate opaque
SHA-256 identity computed from the full canonical resource URL before entropy masking, while every
durable/UI-facing URL is stripped of userinfo, credential query fields, and fragments.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import NamedTuple, Optional
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from looplab.trust.redact import is_secret_key_name, redact_persisted_text


MAX_SOURCE_URL_INPUT = 8_192
_SOURCE_ID_RE = re.compile(r"^http-sha256:[0-9a-f]{64}$")
_URL_CREDENTIAL_KEYS = frozenset({
    "auth", "authorization", "key", "sig", "signature", "session", "sessionid", "jwt", "sas",
})


class SourceRef(NamedTuple):
    identity: str
    display_url: str


def valid_source_identity(value: object) -> bool:
    return isinstance(value, str) and _SOURCE_ID_RE.fullmatch(value) is not None


def _credential_query_key(value: str) -> bool:
    key = unicodedata.normalize("NFKC", value).strip().casefold()
    return (is_secret_key_name(key) or key in _URL_CREDENTIAL_KEYS
            or key.endswith(("_signature", "-signature")))


def _credential_free_query(query: str) -> str:
    """Drop explicitly credential-bearing query fields without reordering resource parameters."""
    kept: list[tuple[str, str]] = []
    separator = ""
    for token in re.split(r"([&;])", query):
        if token in {"&", ";"}:
            separator = token
            continue
        field = token
        encoded_key = field.partition("=")[0]
        try:
            key = unquote_plus(encoded_key)
        except Exception:  # noqa: BLE001 - malformed percent escapes are treated as opaque keys
            key = encoded_key
        if _credential_query_key(key):
            continue
        kept.append((separator if kept else "", field))
    return "".join(joiner + field for joiner, field in kept)


def canonical_source_ref(value: object, *, persisted_identity: object = None) -> Optional[SourceRef]:
    """Return a bounded stable identity and safe display URL, or ``None`` for invalid input.

    A syntactically valid persisted identity is accepted for idempotent writer/replay sanitization:
    the original opaque path may already have been display-redacted and cannot be reconstructed.
    New/raw records omit it and always receive an identity derived from their canonical URL.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_SOURCE_URL_INPUT:
        return None
    raw = unicodedata.normalize("NFKC", value).strip()
    if (not raw or len(raw) > MAX_SOURCE_URL_INPUT
            or any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in raw)):
        return None
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or parsed.hostname is None:
            return None
        port = parsed.port
        hostname = parsed.hostname
        if any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in hostname):
            return None
        if ":" in hostname:
            host = f"[{hostname.lower()}]"
        else:
            host = hostname.encode("idna").decode("ascii").lower()
        if port is not None and not ((scheme == "http" and port == 80)
                                     or (scheme == "https" and port == 443)):
            host += f":{port}"
        canonical = urlunsplit((
            scheme, host, parsed.path or "/", _credential_free_query(parsed.query), "",
        ))
    except (UnicodeError, ValueError):
        return None
    # CODEX AGENT: display redaction is lossy, so it must never be the evidence/cache identity.
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    derived = "http-sha256:" + digest
    identity = str(persisted_identity) if valid_source_identity(persisted_identity) else derived
    # Redact before bounding, then use a URL-safe marker. ``redact_persisted_text``'s prose
    # truncation marker contains whitespace/newlines and would not be parseable on the next replay.
    display = redact_persisted_text(
        canonical, max_chars=MAX_SOURCE_URL_INPUT, entropy=True, single_line=True)
    if len(display) > 1_600:
        marker = f".~looplab-source-{identity.removeprefix('http-sha256:')}"
        display = display[:1_600 - len(marker)] + marker
    return SourceRef(identity, display) if display else None
