"""Structured semantic CLAIM key (§21.20.13, full-CR of the lean fuzzy merge) — a scope- and
polarity-safe identity for a cross-run claim.

The lean claim identity was the display STATEMENT (`normalize_statement`, a 160-char prose truncation), and
the lean merge was a token-Jaccard union-find. CODEX flagged three failure modes this module closes:

1. GLOBAL prose identity — rejecting a claim in task A rejected a same-worded claim in task B, and two long
   claims sharing a 160-char prefix collided. -> the key carries SCOPE (task) + optional metric.
2. POLARITY blindness — "dropout improves generalization" and "dropout NEVER improves generalization" share
   almost every token, so Jaccard 0.6 merged them and a ratified member could inherit a rejected member's
   maturity. -> polarity is part of the key: opposite-polarity claims never merge; they are surfaced as a
   CONTRADICTION instead (same `contra_key`, differing polarity).
3. O(n^2) single-linkage transitivity — union-find chained A~B~C past the threshold. -> identity here is an
   EXACT structured key (hash-bucketed grouping, O(n)); two claims merge only on identical
   (subject-stems, scope, metric, polarity), never by transitive bridging.

`claim_signature(statement, scope, metric)` is pure/deterministic. The subject is a light-stemmed content
token SET (order/duplication/inflection-insensitive); the polarity is negation-cue parity. A `merge_key`
identifies mergeable claims; a `contra_key` (polarity-agnostic) identifies contradiction partners; a stable
`uid` keys operator governance so a decision is scope-precise. Deliberately lean stemming/negation — a full
semantic parse (subject/intervention/comparator) is a further TODO, but this is already scope+polarity-safe.
"""
from __future__ import annotations

import hashlib
import re

CLAIM_KEY_VERSION = 1

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
# The "n't" contraction (a negation modifier the tokenizer splits off). The apostrophe is REQUIRED — a
# `?`-optional apostrophe would match ANY word ending in "nt" (gradient, component, prevent, current, …) and
# silently flip a claim's polarity (mega-review finding). Matches don't/doesn't/isn't/won't → the "n't" tail.
_NT = re.compile(r"n['’]t\b", re.UNICODE)


def _stem(w: str) -> str:
    """Light suffix stripping so plural/tense variants of one word collapse (improves/improving/improved ->
    'improv'). Deterministic and consistent (a signature, not a linguist): the exact stem string matters
    only in that the SAME word always maps to it."""
    if len(w) > 5 and w.endswith("ing"):
        w = w[:-3]
    elif len(w) > 4 and w.endswith("ed"):
        w = w[:-2]
    elif len(w) > 4 and w.endswith("es"):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s"):
        w = w[:-1]
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return w


def _stems(words: str) -> frozenset:
    return frozenset(_stem(w) for w in words.split())


# Function words + degree/qualifier adverbs: carried by the statement but NOT part of its subject identity
# (so a paraphrase that only adds "greatly"/"significantly" keeps the same subject).
_STOP = _stems("""
the a an and or of to in on for with is are was were be been being it its this that these those
we our you your they their he she his her as at by from into over under than then so such but
does do did done has have had having will would can could should may might must more most less least very
greatly slightly significantly marginally somewhat lot much really quite highly big small large huge tiny
overall generally consistently substantially better best worse worst good bad strong strongly weak weakly
""")

# EFFECT verbs carry the assertion's DIRECTION, not its subject: "X improves Y" and "X degrades Y" are two
# opposite assertions about the SAME entities {X, Y}. Stripping both from the subject and encoding their
# sign as polarity makes them contradiction partners (same subject, opposite polarity) instead of two
# unrelated claims — the core of the structured key (CODEX).
_POS_EFFECT = _stems("improve boost help increase raise gain enhance benefit outperform beat accelerate "
                     "stabilize fix solve speed strengthen")
_NEG_EFFECT = _stems("hurt degrade worsen reduce decrease harm break fail drop lower lose regress damage "
                     "slow weaken")
# Pure negation MODIFIERS (flip the sign without being effect verbs themselves).
_NEGATE = _stems("not never without cannot nor no lacks prevent avoid")
_SUBJECT_DROP = _STOP | _POS_EFFECT | _NEG_EFFECT | _NEGATE


def _analyze(statement: str) -> tuple:
    """Return (subject-tuple, polarity). Subject = stemmed content ENTITIES (effect/negation/function words
    removed), sorted+deduped. Polarity = -1 iff an ODD number of sign-flippers (negation modifiers +
    negative-effect verbs + "n't") applies, else +1; 0 when there is no subject content."""
    low = str(statement or "").casefold()
    words = _WORD.findall(low)
    # The SUBJECT keeps content words (len>2) AND numeric literals of ANY length — a bare number is
    # distinguishing content (two parameterized facts differing only in their values are NOT paraphrases), so
    # stripping it would over-merge (the very failure mode this key exists to prevent).
    subject = tuple(sorted({s for s in (_stem(w) for w in words if len(w) > 2 or w.isdigit())
                            if s and s not in _SUBJECT_DROP and (len(s) > 1 or s.isdigit())}))
    if not subject:
        return (), 0
    # POLARITY flips are counted over EVERY token (not just the >2-char subject tokens), so a short negation
    # like "no" — dropped from the subject by the length filter — still flips the sign (mega-review finding).
    flips = sum(1 for s in (_stem(w) for w in words) if s in _NEGATE or s in _NEG_EFFECT) \
        + len(_NT.findall(low))
    return subject, (-1 if flips % 2 else 1)


def claim_signature(statement: str, *, scope: str = "", metric: str = "") -> dict:
    """The structured semantic key for a claim. `scope` (task id) and `metric` qualify identity so the same
    words in two different tasks/metrics are two claims (CODEX). Returns:
      - subject:   sorted stemmed content-token tuple
      - polarity:  +1 / -1 / 0
      - scope, metric: the qualifying context (normalized to str)
      - merge_key: identical => the same claim (subject+scope+metric+polarity) — mergeable
      - contra_key: polarity-agnostic — two claims sharing it with OPPOSITE polarity contradict
      - uid:       a stable opaque governance key over merge_key
    Pure/deterministic."""
    subj, pol = _analyze(statement)
    sc, mt = str(scope or ""), str(metric or "")
    subj_h = hashlib.sha1(("\x1f".join(subj)).encode("utf-8")).hexdigest()[:16]
    contra_key = f"{CLAIM_KEY_VERSION}|{sc}|{mt}|{subj_h}"
    merge_key = f"{contra_key}|{pol:+d}"
    uid = "clm_" + hashlib.sha1(merge_key.encode("utf-8")).hexdigest()[:16]
    return {"subject": subj, "polarity": pol, "scope": sc, "metric": mt,
            "merge_key": merge_key, "contra_key": contra_key, "uid": uid}


def claim_uid(statement: str, *, scope: str = "", metric: str = "") -> str:
    """Shorthand for `claim_signature(...)["uid"]` — the stable governance key for a claim."""
    return claim_signature(statement, scope=scope, metric=metric)["uid"]
