"""The one unicode word tokenizer the identity/retrieval layers share (doc 25 EM-15).

`re.compile(r"[^\\W_]+", re.UNICODE)` had been declared independently in four modules — the cross-run
memory fingerprint, the concept registry, the claim key and the claim health/retrieval reader — and
the NFKC→casefold→findall pipeline in three. That is not merely repetition: every one of those four
feeds a persisted IDENTITY (a fingerprint, a concept key, a claim uid, a retrieval token set), so a
unicode-handling fix applied to three of them re-keys three stores and leaves the fourth pointing at
the old spelling, with no error anywhere and nothing but a quiet drop in match rate to show for it.

`engine/memory.py` keeps its separate `_WORD_ASCII` variant, deliberately: that one is a VERSIONED
compatibility contract (the pre-unicode `[a-z0-9]+` fingerprint mode a running portfolio must still
match), not a second copy of this rule.
"""
from __future__ import annotations

import re
import unicodedata

# Underscore stays a SEPARATOR (`[^\W_]` excludes it): `train_loss` is two tokens, not one, which is
# what makes an identifier-shaped statement match a prose one. No alphabet allowlist — every script
# tokenizes, so a Cyrillic or CJK goal is not silently reduced to nothing.
WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def normalize_text(text) -> str:
    """NFKC-normalize then casefold — the canonical form every token rule below runs against.

    Both steps are load-bearing and ordered: NFKC first folds compatibility forms (a full-width or
    ligature spelling becomes the ordinary one) so two honest spellings of one statement produce the
    same key; `.casefold()` rather than `.lower()` because lower() does not case-fold correctly
    outside ASCII (ß, İ, Σ/ς), which would split one concept into two keys by script alone.

    Exposed separately from `tokenize` because a caller sometimes needs BOTH the tokens and the
    normalized string — `claim_key` matches the "n't" contraction against the string after
    tokenizing it — and re-deriving the string there would be a fifth copy of this pipeline."""
    return unicodedata.normalize("NFKC", str(text or "")).casefold()


def tokenize(text) -> list[str]:
    """`normalize_text` then split into unicode word tokens."""
    return WORD_RE.findall(normalize_text(text))


def fingerprint_similarity(a: list[str], b: list[str]) -> float:
    """Jaccard overlap of two fingerprints in [0,1]. 1.0 = identical token sets.

    Lives here rather than in `engine/memory` (doc 25 EM-10) because it is pure set math over the
    token lists this module already defines the vocabulary for — and because the concept-capsule
    split needed it BELOW both modules: capsules import it, memory imports it, and neither may
    import the other.
    """
    sa, sb = set(a or []), set(b or [])
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
