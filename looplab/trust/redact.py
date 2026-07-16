"""B3 · Secret-leak redaction (ADR-13 trust). A `print(api_key)` or a traceback that echoes an env
secret would otherwise be persisted verbatim into the event log / spans / UI via the stdout/stderr
tail. Run a redaction pass — known credential patterns (always) plus conservative high-entropy token
masking — over every output tail before it is written.

Pure + deterministic. Known-pattern redaction is always safe; the entropy pass is conservative
(long tokens only) to avoid masking legitimate data hashes. Config-gated (`redact_output`, off by
default to preserve byte-identical behavior; recommended on for untrusted tiers).
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata

# Field names that merely CONTAIN a credential substring but are benign diagnostic output
# (tokenizer / max_tokens / *_tokens) — never mask these so operators keep model/token diagnostics.
_BENIGN_KEYS = {"tokenizer", "max_tokens", "num_tokens", "n_tokens",
                "total_tokens", "prompt_tokens", "completion_tokens", "tokens"}


_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|secret|access[_-]?key|token|password|passwd|credential)",
    re.IGNORECASE,
)
_ASSIGN_PREFIX = r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]+)(['\"]?\s*[:=]\s*)"


def is_secret_key_name(value) -> bool:
    """Whether a structured diagnostic key names credential material."""
    try:
        key = unicodedata.normalize("NFKC", str(value or "")).lower()
    except Exception:  # noqa: BLE001 - an opaque diagnostic key is safer masked than inspected twice
        return True
    return key not in _BENIGN_KEYS and _SECRET_KEY_RE.search(key) is not None


def _keyval_repl(m: re.Match) -> str:
    if not is_secret_key_name(m.group(1)):
        return m.group(0)                      # benign field name -> leave the value intact
    return f"{m.group(1)}{m.group(2)}***"


def _quoted_keyval_repl(m: re.Match) -> str:
    if not is_secret_key_name(m.group(1)):
        return m.group(0)
    value = m.group(3)
    quote = value[:1]
    closing = quote if len(value) > 1 and value.endswith(quote) else ""
    return f"{m.group(1)}{m.group(2)}{quote}***{closing}"


def _authorization_repl(m: re.Match) -> str:
    return f"{m.group(1)}{m.group(2)} ***"


# Known credential shapes — always redacted (negligible false-positive risk).
_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "sk-***"),                     # OpenAI-style (incl. sk-proj-)
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA***"),                         # AWS access key id
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github_pat_***"),      # GitHub fine-grained PAT
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "gh***"),                 # GitHub token (classic)
    (re.compile(r"hf_[A-Za-z0-9]{20,}"), "hf_***"),                       # HuggingFace token
    (re.compile(r"xox[baprse]-[A-Za-z0-9-]{10,}"), "xox***"),             # Slack token
    # RFC token68 credentials. Match the whole token (including + / _ ~ and = padding) and do not
    # impose a minimum length: short development credentials are still secrets when persisted.
    (re.compile(r"(?i)(\bauthorization\s*[:=]\s*)(bearer|basic)\s+"
                r"[A-Za-z0-9._~+/=-]+"), _authorization_repl),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "bearer ***"),
    # RFC-3986 userinfo occasionally appears in a consulted URL.  It is credential material even
    # without an explicit ``password=`` label, and source URLs are copied into durable memos.
    (re.compile(r"(?i)(?<=://)([^:/\s]+):([^@/\s]+)@"), "***:***@"),
    # Match a generic identifier once, then classify it in the callback. This avoids quadratic
    # backtracking on large untrusted fields. Quoted values are consumed through their quote or line
    # boundary so spaces cannot leak; the unquoted form accepts any non-empty value.
    (re.compile(
        _ASSIGN_PREFIX
        + r"(\"(?:\\.|[^\"\\\r\n])*(?:\"|(?=\r?\n|\Z))"
          r"|'(?:\\.|[^'\\\r\n])*(?:'|(?=\r?\n|\Z)))",
        re.IGNORECASE,
    ), _quoted_keyval_repl),
    (re.compile(_ASSIGN_PREFIX + r"([^\s'\",;&}\]]+)", re.IGNORECASE), _keyval_repl),
]


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def redact_secrets(text: str, *, entropy: bool = True,
                   entropy_cutoff: float = 4.2, min_len: int = 24) -> str:
    """Mask credentials in `text`. Known patterns are always redacted; if `entropy`, also mask long
    high-entropy tokens (likely base64/hex secrets) — conservative to avoid hashing false-positives."""
    if not text:
        return text
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    if entropy:
        def _mask(m: re.Match) -> str:
            tok = m.group(0)
            return "***REDACTED***" if _entropy(tok) >= entropy_cutoff else tok
        text = re.sub(rf"[A-Za-z0-9+/=_\-]{{{min_len},}}", _mask, text)
    return text


def redact_persisted_text(value, *, max_chars: int, entropy: bool = True,
                          single_line: bool = False) -> str:
    # This always-on sanitizer belongs at durable boundaries, independent of UI settings.
    """Return a deterministic, display-safe string for an always-durable diagnostic field.

    Unlike :func:`redact_secrets`, this helper is not controlled by ``redact_output``: callers use
    it only at explicit persistence boundaries (memos/traces), where credentials and terminal or
    bidi controls must never be retained.  The digest is over the already-redacted canonical text,
    so a truncation marker cannot become an oracle for the original secret.
    """
    try:
        text = "" if value is None else str(value)
    except Exception:  # noqa: BLE001 - diagnostics must not perturb the operation being recorded
        text = "<unavailable>"
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    # Persisted strings may be rendered by terminals and browsers. Keep an ordinary
    # newline only for multi-line prose; replace every other Unicode control/format character before
    # redaction, hashing, or truncation so ANSI/bidi/NUL payloads cannot survive in any representation.
    text = "".join(
        ch if (ch == "\n" and not single_line) or not unicodedata.category(ch).startswith("C")
        else " "
        for ch in text
    )
    text = redact_secrets(text, entropy=entropy)
    if single_line:
        text = " ".join(text.split())
    cap = max(0, int(max_chars))
    if len(text) <= cap:
        return text[:cap]
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    marker = f"\n[redacted preview: original_chars={len(text)} sha256={digest}]"
    if len(marker) >= cap:
        return marker[-cap:] if cap else ""
    return text[:cap - len(marker)] + marker
