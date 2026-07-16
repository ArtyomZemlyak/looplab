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

`claim_signature(statement, scope, metric)` is pure/deterministic. The subject is a light-stemmed,
role-aware content sequence; the polarity is negation-cue parity. A `merge_key`
identifies mergeable claims; a `contra_key` (polarity-agnostic) identifies contradiction partners; a stable
`uid` keys operator governance so a decision is scope-precise. Deliberately lean stemming/negation — a full
semantic parse (subject/intervention/comparator) is a further TODO, but this is already scope+polarity-safe.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

CLAIM_KEY_VERSION = 3

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
                     "stabilize fix solve speed strengthen cause lead")
_NEG_EFFECT = _stems("hurt degrade worsen reduce decrease harm break fail drop lower lose regress damage "
                     "slow weaken")
# Pure sentential negation modifiers (flip the assertion sign). ``avoid``/``prevent`` are beneficial
# relation verbs in claims such as "prevent leakage to improve validity"; treating them as global sign
# flippers inverted otherwise-positive assertions. Keep them out of identity without negating the sentence.
_NEGATE = _stems("not never without cannot nor no lacks")
_CONTROL_EFFECT = _stems("prevent avoid")
_SUBJECT_DROP = _STOP | _POS_EFFECT | _NEG_EFFECT | _NEGATE | _CONTROL_EFFECT


def _content(stems, *, allow_symbol: bool = False) -> tuple[str, ...]:
    """Keep an ordered, duplicate-free content sequence for one semantic role."""
    out: list[str] = []
    for s in stems:
        if (s and s not in _SUBJECT_DROP
                and (allow_symbol or len(s) > 1 or s.isdigit()) and s not in out):
            out.append(s)
    if not out and allow_symbol:
        # Single-letter experimental variables are genuine role occupants (A->B differs from B->A). The
        # article "a" is admitted only when it is the role's sole token, so prose such as "a model" still
        # normalizes to just ``model``.
        meaningful = [s for s in stems if s and (s not in _SUBJECT_DROP or s == "a")]
        if meaningful == ["a"]:
            out.append("a")
    return tuple(out)


def _analyze(statement: str) -> tuple:
    """Return ``(subject, roles, polarity, relation_sign, negated)`` with conservative role-aware identity.

    For an effect assertion, ``roles`` is ``(lhs, rhs)``. Keeping the sides separate means
    "A improves B" cannot merge with, or contradict, "B improves A" without a real semantic parser.
    With no recognized effect verb, all ordered content stays in one role.

    TWO independent sign axes (mega-review §21.20.5): the RELATION direction (helps=+1 / hurts=-1, from the
    effect verb) and sentential NEGATION (parity of `not`/`no`/`n't`). They must stay SEPARATE in identity —
    "X does NOT improve Y" (a null/refuted-positive: relation +1, negated) is a DIFFERENT claim from "X
    reduces Y" (a supported negative-effect: relation -1, asserted), even though both net to polarity -1. The
    old single parity bit collapsed them, pooling a "doesn't help" observation into a "hurts" claim
    ("Hurts is a supported negative-effect claim, not a refuted positive"). `polarity` (net stance) is kept
    for the contradiction pairing; `relation_sign`/`negated` enter the merge_key so null != harm never merge.
    """
    low = unicodedata.normalize("NFKC", str(statement or "")).casefold()
    words = _WORD.findall(low)
    stems = [_stem(w) for w in words]
    # Preserve the historical rule that short alphabetic tokens are not claim content, while numeric
    # literals of any length remain distinguishing content.
    eligible = [s if (len(w) > 2 or w.isdigit()) else "" for w, s in zip(words, stems)]
    relation_at = next((i for i, s in enumerate(stems) if s in (_POS_EFFECT | _NEG_EFFECT)), None)
    if relation_at is None:
        roles = (_content(eligible),)
        relation_sign = 0
    else:
        # Assertion roles are identity, not a bag of entities. Sorting A/B together made
        # "A improves B" collide with "B improves A" and let governance for one relation control the other.
        roles = (_content(stems[:relation_at], allow_symbol=True),
                 _content(stems[relation_at + 1:], allow_symbol=True))
        relation_sign = 1 if stems[relation_at] in _POS_EFFECT else -1
    subject = tuple(s for role in roles for s in role)
    if not subject:
        return (), roles, 0, 0, 0
    # SENTENTIAL negation only (not the relation verb): "no"/"not"/"never"/... + the "n't" contraction.
    negated = (sum(1 for s in stems if s in _NEGATE) + len(_NT.findall(low))) % 2
    # Net stance = relation direction (positive base when there is no effect verb) flipped by negation.
    base = relation_sign if relation_sign else 1
    polarity = -base if negated else base
    return subject, roles, polarity, relation_sign, negated


def claim_signature(statement: str, *, scope: str = "", metric: str = "") -> dict:
    """The structured semantic key for a claim. `scope` (task id) and `metric` qualify identity so the same
    words in two different tasks/metrics are two claims (CODEX). Returns:
      - subject:   ordered stemmed content-token tuple (flattened from ``roles``)
      - roles:     ordered semantic sides (lhs/rhs around the first recognized effect verb)
      - polarity:  +1 / -1 / 0
      - scope, metric: the qualifying context (normalized to str)
      - merge_key: identical => the same claim (subject+scope+metric+polarity) — mergeable
      - contra_key: polarity-agnostic — two claims sharing it with OPPOSITE polarity contradict
      - uid:       a stable opaque governance key over merge_key
    Pure/deterministic."""
    subj, roles, pol, relation_sign, negated = _analyze(statement)
    sc, mt = str(scope or ""), str(metric or "")
    role_payload = "\x1e".join("\x1f".join(f"{len(s)}:{s}" for s in role) for role in roles)
    subj_h = hashlib.sha256(role_payload.encode("utf-8")).hexdigest()[:32]
    # Length-prefix qualifiers: delimiter-bearing task/metric IDs must not alias another partitioning of
    # the same bytes (scope="a|b", metric="c" versus scope="a", metric="b|c").
    contra_key = f"{CLAIM_KEY_VERSION}|{len(sc)}:{sc}|{len(mt)}:{mt}|{subj_h}"
    # merge_key carries the RELATION direction and the NEGATION parity SEPARATELY (not the collapsed net
    # polarity), so a null-effect claim (relation +1, negated) never merges with a harm claim (relation -1,
    # asserted). contra_key stays polarity-agnostic; the net `polarity` field drives contradiction pairing.
    merge_key = f"{contra_key}|{relation_sign:+d}|{negated:d}"
    uid = "clm_" + hashlib.sha256(merge_key.encode("utf-8")).hexdigest()[:32]
    return {"version": CLAIM_KEY_VERSION, "subject": subj, "roles": roles,
            "polarity": pol, "relation_sign": relation_sign, "negated": bool(negated),
            "scope": sc, "metric": mt,
            "merge_key": merge_key, "contra_key": contra_key, "uid": uid}


def claim_uid(statement: str, *, scope: str = "", metric: str = "") -> str:
    """Shorthand for `claim_signature(...)["uid"]` — the stable governance key for a claim."""
    return claim_signature(statement, scope=scope, metric=metric)["uid"]
