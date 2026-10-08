"""THE COMPARABILITY RULE — the pure half of `engine/comparability.py`, moved DOWN to `core`.

`engine/comparability.py` builds a node's comparability RECORD (it reads task snapshots, input
provenance, eval protocols) and words the notices; this module holds only the RULE that compares two
records already written: `record_of`, `comparability_status` and the vocabulary they decide in. It
imports nothing but the standard library, so every layer may read it — `search/concept_effects.py`
needs the rule, and `search` may not import `engine` at any level
(`tests/test_calibration_profile_home.py::test_the_search_to_engine_edge_is_gone`; the same move
as doc 25 XP-07's `events/finalize_scope.py`). `engine/comparability.py` re-exports every name here
as the SAME object, so there is still ONE spelling of the rule and every existing import works.
"""
from __future__ import annotations

from typing import Optional

# The three AUTHORITIES a key can be decided at, strongest first. A closed vocabulary, because this
# slug rides on the record into a UI badge and into a CLI refusal, and a reader that cannot tell
# "the bytes matched" from "the operator said so" cannot tell a proof from a promise.
AUTHORITY_MEASURED = "measured"
AUTHORITY_DECLARED = "declared"
AUTHORITY_INFERRED = "inferred"
# Ordered strongest-first: `common_authority` walks this list and stops at the first family both
# records carry, so adding an authority means inserting it at its strength and nothing else.
AUTHORITIES = (AUTHORITY_MEASURED, AUTHORITY_DECLARED, AUTHORITY_INFERRED)

# Which authorities may CERTIFY sameness on equality. `inferred` is deliberately absent — see
# `engine/comparability.py`'s module docstring on the inversion. It may still refuse (inequality is `different` at every authority).
CERTIFYING = frozenset({AUTHORITY_MEASURED, AUTHORITY_DECLARED})

# The tri-state. `UNKNOWN` is a value, never a default for `SAME`.
SAME = "same"
DIFFERENT = "different"
UNKNOWN = "unknown"
STATUSES = (SAME, DIFFERENT, UNKNOWN)

# THE MEASUREMENT PROTOCOL's facets, in the order a notice names the first that differs. Each is a
# separate REFUSAL-ONLY discriminator — the `substrate` rule, per facet — and they are kept apart
# rather than hashed into one digest because ABSENCE differs per facet: a run that began printing a
# fingerprint half-way would otherwise make every node before it "different" from every node after
# it, when nothing about the measurement changed except that one side started to say so.
#
#   profile     — the resolved eval profile's OVERRIDE TOKENS (`command_eval.eval_protocol`): what
#                 separates a `smoke`-scored number from a `full`-scored one (doc 68 §1's live hole:
#                 the Strategist's fidelity puts search nodes on one and endgame nodes on the other,
#                 and `select_best_node` ranks them in one pool).
#   scorer      — the host scorer program's content digest (`host_scorer.program_sha256`): an
#                 operator who edits the scorer mid-run changes the ruler under every later node.
#   fingerprint — what the eval PRINTED about its own conditions (`sandbox.json_line_fingerprint`,
#                 key `eval_fingerprint`, carried as a sha256 of its canonical JSON): decoder
#                 settings, scorer version, split hash — the `repetition_penalty` 1.1-vs-1.0 case,
#                 which nothing the engine owns could see.
#
# `profile` records the overrides that REACHED the executed chain (`eval_stages.py::_eval_pipeline`):
# none under an operator-declared `eval.stages` list, which never runs the profile's command.
PROTOCOL_FACETS = ("profile", "scorer", "fingerprint")


def record_of(node) -> Optional[dict]:
    """One node's comparability record, read off `metric_provenance`. `None` for every old log.

    THE READER-SIDE DEFAULT (invariant #5), and it is the load-bearing line of the module: a node
    written before this shipped has no key, and `None` means `unknown` at every consumer — never
    "the same as mine". Total over junk, because `metric_provenance` is folded from untyped event
    data and a hand-edited log may hold a string where a dict belongs.
    """
    provenance = getattr(node, "metric_provenance", None)
    if not isinstance(provenance, dict):
        return None
    record = provenance.get("comparability")
    if not isinstance(record, dict):
        return None
    keys = record.get("keys")
    return record if isinstance(keys, dict) and keys else None


def common_authority(this: dict, other: dict) -> Optional[str]:
    """The strongest authority both records carry, or `None` when they share none."""
    this_keys = this.get("keys") if isinstance(this.get("keys"), dict) else {}
    other_keys = other.get("keys") if isinstance(other.get("keys"), dict) else {}
    return next((name for name in AUTHORITIES if this_keys.get(name) and other_keys.get(name)), None)


def substrate_mismatch(this: Optional[dict], other: Optional[dict]) -> Optional[tuple[str, str]]:
    """`(mine, theirs)` when both records name a source tree and the two differ, else `None`.

    ONE spelling, because the substrate is consulted twice — `comparability_status` decides on it and
    `comparability_notice` prints a sentence about it — and this module's whole stated purpose is
    that no surface writes a second copy of the rule. Written out twice, a tightening (say, treating
    a missing substrate as a refusal) would reach the status and leave the notice printing a mismatch
    the status no longer finds, which is exactly the "cannot be told apart from a bug in this file"
    failure the `_NOTICES` comment forbids.

    Both sides must carry one: a missing substrate is silence, never "the same tree", which is what
    keeps every pre-2026-08-24 log reading as it did. It can only ever REFUSE — agreeing here
    certifies nothing, because equal code over different data is not the same evaluation.
    """
    mine = (this or {}).get("substrate")
    theirs = (other or {}).get("substrate")
    if isinstance(mine, str) and isinstance(theirs, str) and mine and theirs and mine != theirs:
        return mine, theirs
    return None


def protocol_mismatch(this: Optional[dict],
                       other: Optional[dict]) -> Optional[tuple[str, str, str]]:
    """`(facet, mine, theirs)` for the FIRST protocol facet both records carry and disagree on.

    `substrate_mismatch`'s rule, per facet and for the same reasons: ONE spelling for the status
    and the notice, both sides must carry the facet (absence is silence, never "the same ruler"),
    and agreeing certifies nothing. Facets are asked in `PROTOCOL_FACETS` order so the notice names
    the same facet every time for one pair.
    """
    mine = (this or {}).get("protocol")
    theirs = (other or {}).get("protocol")
    if not isinstance(mine, dict) or not isinstance(theirs, dict):
        return None
    for facet in PROTOCOL_FACETS:
        left, right = mine.get(facet), theirs.get(facet)
        if isinstance(left, str) and isinstance(right, str) and left and right and left != right:
            return facet, left, right
    return None


def comparability_status(this: Optional[dict], other: Optional[dict]) -> str:
    """`SAME` | `DIFFERENT` | `UNKNOWN` for two comparability records. Never raises.

    THE RULE, in one place, so no surface may write a second one:
      * both carry a SUBSTRATE and they DIFFER       -> DIFFERENT   (checked FIRST, see below)
      * both carry a PROTOCOL facet and it DIFFERS   -> DIFFERENT   (refusal-only, like substrate)
      * either side absent, or no shared authority   -> UNKNOWN
      * the shared authority's keys DIFFER           -> DIFFERENT   (at every authority)
      * they are equal at a CERTIFYING authority     -> SAME
      * they are equal at `inferred` only            -> UNKNOWN     (the inversion)

    Reflexivity is NOT assumed and must not be: `comparability_status(None, None)` is `UNKNOWN`.
    Two records that say nothing have not agreed, and a caller that special-cases identity would
    make a run comparable with itself under a rule that says nothing about it.
    """
    if not isinstance(this, dict) or not isinstance(other, dict):
        return UNKNOWN
    # THE SUBSTRATE IS CHECKED FIRST AND CAN ONLY REFUSE. Two numbers produced from different source
    # trees are not on one scale whatever their input keys say — that is the whole point of recording
    # it — so a substrate mismatch outranks even a `measured` agreement. It never CERTIFIES: falling
    # through a matching substrate changes nothing below, because equal code with different data is
    # not the same evaluation. Both sides must carry one; a missing substrate is `unknown` and
    # deliberately not "the same", which is what keeps every pre-2026-08-24 log reading as it did.
    if substrate_mismatch(this, other) is not None:
        return DIFFERENT
    # …AND SO IS THE PROTOCOL, on the same ground: a number measured under a different ruler (a
    # `smoke` override set, an edited scorer, a decoder the eval says it ran differently) is not on
    # one scale with this one whatever the input keys say, and a matching protocol certifies nothing.
    if protocol_mismatch(this, other) is not None:
        return DIFFERENT
    authority = common_authority(this, other)
    if authority is None:
        return UNKNOWN
    if this["keys"][authority] != other["keys"][authority]:
        return DIFFERENT
    return SAME if authority in CERTIFYING else UNKNOWN
