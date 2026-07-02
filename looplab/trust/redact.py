"""B3 · Secret-leak redaction (ADR-13 trust). A `print(api_key)` or a traceback that echoes an env
secret would otherwise be persisted verbatim into the event log / spans / UI via the stdout/stderr
tail. Run a redaction pass — known credential patterns (always) plus conservative high-entropy token
masking — over every output tail before it is written.

Pure + deterministic. Known-pattern redaction is always safe; the entropy pass is conservative
(long tokens only) to avoid masking legitimate data hashes. Config-gated (`redact_output`, off by
default to preserve byte-identical behavior; recommended on for untrusted tiers).
"""
from __future__ import annotations

import math
import re

# Field names that merely CONTAIN a credential substring but are benign diagnostic output
# (tokenizer / max_tokens / *_tokens) — never mask these so operators keep model/token diagnostics.
_BENIGN_KEYS = {"tokenizer", "max_tokens", "num_tokens", "n_tokens",
                "total_tokens", "prompt_tokens", "completion_tokens", "tokens"}


def _keyval_repl(m: re.Match) -> str:
    if m.group(1).lower() in _BENIGN_KEYS:
        return m.group(0)                      # benign field name -> leave the value intact
    return f"{m.group(1)}{m.group(2)}***"


# Known credential shapes — always redacted (negligible false-positive risk).
_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "sk-***"),                     # OpenAI-style (incl. sk-proj-)
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA***"),                         # AWS access key id
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github_pat_***"),      # GitHub fine-grained PAT
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "gh***"),                 # GitHub token (classic)
    (re.compile(r"hf_[A-Za-z0-9]{20,}"), "hf_***"),                       # HuggingFace token
    (re.compile(r"xox[baprse]-[A-Za-z0-9-]{10,}"), "xox***"),             # Slack token
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"), "bearer ***"),     # Authorization: Bearer
    # key=VALUE / 'key': 'VALUE' assignments. The key NAME may be a compound identifier that merely
    # CONTAINS a credential word (AWS_SECRET_ACCESS_KEY, db_password, MY_API_KEY) — matching the
    # bare word with \b…\b misses those because `_` is a word char, so we allow identifier chars on
    # either side of the keyword. A quote may sit between the key and the separator (dict-repr form).
    (re.compile(r"(?i)([A-Za-z0-9_\-]*"
                r"(?:api[_-]?key|secret|access[_-]?key|token|password|passwd|credential)"
                r"[A-Za-z0-9_\-]*)"
                r"(['\"]?\s*[:=]\s*['\"]?)"
                r"([^\s'\"]{4,})"), _keyval_repl),
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
