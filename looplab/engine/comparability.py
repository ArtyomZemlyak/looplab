"""THE COMPARABILITY KEY — what two recorded numbers must SHARE before their values may be ordered.

THE DEFECT, and it is a live one on this box rather than a hypothetical.
`runs/` holds recall@100 values of 0.8776, 0.793426, 0.792082 and 0.774207. They were compared all
day. Some were measured on one test set and some on another, some against one product index and some
against a bigger one — and **nothing in any record says which**, so nothing could refuse the
comparison. A larger corpus makes recall@100 strictly harder; a different judged-positive rule moves
it either way. Neither is a fact about the model, and both are invisible in the log.

WHAT ALREADY EXISTED, AND EXACTLY WHERE EACH ONE STOPS. This module is a composition of three
mechanisms that were already here, plus the ONE half none of them had. Nothing here re-derives a rule
another module owns — that is how two derivations of one population came to disagree once already
(`engine/champion_caveats.py`, the retracted "1 of 297").

  * `runtime/metric_subject.py` binds a metric to the ARTEFACT THAT PRODUCED IT — path, `file_identity`,
    size, digest. It is the OUTPUT side and it is solid. It says nothing about what the number was
    measured AGAINST.
  * `core/comparison.py::ComparisonContract` is a typed 13-facet scientific identity — `dataset_lineage`,
    `split_or_candidate_pool_lineage`, `evaluator_uid`/`_version`, `population`, `filter`, `metric_uid`,
    `unit`, `aggregation`, `cutoff`, `measurement_phase`, `uncertainty_protocol`, `constraints_digest`.
    It is the right vocabulary and it is honest about its own limit, in its own words: `"authority":
    "declared"` — "equality proves equality of adapter-declared semantics, not an independent
    fingerprint of the actual dataset/evaluator/budget". It is also, measured 2026-08-20, declared by
    **0 of the task snapshots under `runs/`** — the field is optional and no task on this box sets it.
  * `engine/eval_contract.py` derives an identity from the task snapshot — metric reader + eval command
    + declared paths — and is wired into the foreign-run READING tools. It is a fact about two
    DECLARATIONS. `e5small-dr-unified-v2` and `e5small-dr-unified-v4` have byte-identical contracts by
    that rule (`python -m vectorsearch.test`, `RECALL@100: ([0-9.]+)`, one editable at
    `/home/jovyan/data/vectorizer-unified`) and are exactly the pair the operator cannot compare.
  * `data_provenance` (`events/types.py::EV_DATA_PROVENANCE`) is recorded as SHIPPED and hashes "every
    task asset". Counted 2026-08-20 across all 8 run directories under `runs/` that have an event log
    (4,691 + 2,309 + 133 + 1,624 + 2,539 + 2,456 + 6,415 + 3,330 records): it fires **zero times**.
    `orchestrator.py`'s emission is gated on `if prov:` over `self._assets`, and
    `adapters/repo_task.py::assets()` returns `{}` for every repo task — "repo/data are tree-mounted,
    not flat assets". The one mechanism aimed at input content has never covered the tasks we run.

SO THE MISSING HALF IS THE **MEASURED** ONE: the content identity of the bytes the number was measured
AGAINST, captured at eval time, in the node's own workdir. That is what `eval.inputs` declares and what
`runtime/metric_inputs.bind_inputs` binds — through the SAME `metric_subject.bind_one` the subject
side uses, so an input and a subject differ in which DIRECTION they face and in the two policies that
direction decides (confinement and freshness), and in nothing else.

WHY THE INDEX'S IDENTITY MAY NOT BE ITS PATH, which is the case that decides the shape.
Two different indexes at one path, and one index at two paths, are both routine here — the corpus is
rebuilt in place and the same file is reached through a mount, a symlink and an absolute path. A key
over PATHS calls the first pair identical and the second pair different, i.e. it is wrong in both
directions on exactly the corpus it exists for. The declaration names a path; the KEY is the digest.

--------------------------------------------------------------------------------------------------
THE INVERSION, which is the whole defect and not a nicety.

An absent key must read `unknown`. It must NEVER read "the same as mine". Every existing row on this
box has no key, so a rule that defaulted absent-to-equal would certify the entire corpus as mutually
comparable — which is precisely the false statement we have been acting on. `unknown` is therefore a
THIRD value and not a default for `same`, and `unknown` vs `unknown` is `unknown` and not `same`:
two rows that both say nothing have not agreed about anything.

`comparability_status` is that tri-state. The three AUTHORITIES it can decide at, strongest first,
and what equality PROVES at each:

    measured  — the eval's declared inputs were bound to their content identity at eval time.
                Equality proves the numbers were measured against the same bytes.       -> `same`
    declared  — the task carries a `ComparisonContract`. Equality proves the OPERATOR asserted the
                same 13 facets. A human deliberately made that claim, so it is honoured.  -> `same`
    inferred  — nothing was asserted and nothing was fingerprinted; the engine merely noticed that
                two task files look alike (`eval_contract`). Equality proves that two DECLARATIONS
                match, which is the exact statement v2-vs-v4 satisfies while being incomparable.
                                                                                        -> `unknown`

Inequality is `different` at EVERY authority, including `inferred`: two runs whose eval command or
metric reader differ are provably not on one scale, and that claim is `eval_contract`'s own and was
already shipped. The asymmetry is deliberate — a weak authority may REFUSE a comparison but may not
CERTIFY one.

The status is decided at the strongest authority the two records have IN COMMON. A record that
carries `measured` is never compared against one that carries only `inferred` at the measured level:
that would report "different" for two runs that merely recorded different amounts of evidence, which
is the same false-negative the fail-open rule in `eval_contract.comparable()` exists to prevent.

--------------------------------------------------------------------------------------------------
WHAT THIS DOES NOT DO, stated so the next reader does not look for it.

It does not read a dataset. It does not know that `k` is 100, that the queries carry an `e5` `query:`
prefix, or that the corpus holds 1.8 M documents. Those factors live inside the candidate's own repo,
where LoopLab has no reader and must not grow one — a rule that parsed the candidate's config would
be deciding comparability from bytes the candidate controls. They reach the key by exactly two
sanctioned routes: a file the operator names in `eval.inputs` (its CONTENT decides), or a facet the
operator writes into the `ComparisonContract` (their WORD decides, and the record says so). Anything
else is `unknown`, on purpose, and `unknown` is visible.

It also does not RANK, exclude, or move a metric. Every consumer either refuses to order two numbers
or prints a sentence beside them. The values themselves were correctly measured by real evaluations
and remain true facts about their own runs.
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

# The three AUTHORITIES a key can be decided at, strongest first. A closed vocabulary, because this
# slug rides on the record into a UI badge and into a CLI refusal, and a reader that cannot tell
# "the bytes matched" from "the operator said so" cannot tell a proof from a promise.
AUTHORITY_MEASURED = "measured"
AUTHORITY_DECLARED = "declared"
AUTHORITY_INFERRED = "inferred"
# Ordered strongest-first: `_common_authority` walks this list and stops at the first family both
# records carry, so adding an authority means inserting it at its strength and nothing else.
AUTHORITIES = (AUTHORITY_MEASURED, AUTHORITY_DECLARED, AUTHORITY_INFERRED)

# Which authorities may CERTIFY sameness on equality. `inferred` is deliberately absent — see the
# module docstring's inversion. It may still refuse (inequality is `different` at every authority).
CERTIFYING = frozenset({AUTHORITY_MEASURED, AUTHORITY_DECLARED})

# The tri-state. `UNKNOWN` is a value, never a default for `SAME`.
SAME = "same"
DIFFERENT = "different"
UNKNOWN = "unknown"
STATUSES = (SAME, DIFFERENT, UNKNOWN)

# Bumped only when the COMPOSITION changes in a way that makes an old digest mean something else.
# It is IN the preimage of every family digest, so a key written under v1 can never collide with a
# v2 key that happened to hash the same fields — the two would compare `different`, which is the
# conservative answer and is what a schema change actually means for two numbers.
KEY_VERSION = 1

# How much of the sha256 rides on the record. 16 hex = 64 bits, matching `data_provenance`'s own
# truncation, and this is an identity for comparison rather than a security claim: the parties that
# could collide it are the operator's own declarations.
_KEY_CHARS = 16

# Bounds, for the same reason `eval_contract` has them: every input here can arrive from a snapshot
# written by another binary, and this runs on a path that must not raise.
_MAX_INPUTS = 16
_MAX_FIELD_CHARS = 1024


def _int(value) -> int:
    """A bounded integer from an arbitrary record value. Never raises.

    Total for the same reason every read in `eval_contract` is: a `metric_provenance` dict is folded
    from untyped event data and a hand-edited log may hold anything, and this runs on the node
    terminal's path, where an exception costs the node its `node_evaluated` and re-dies on resume.
    """
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _digest(payload) -> str:
    """The canonical digest of one family's material. Never raises; `""` when unrenderable."""
    try:
        encoded = json.dumps({"v": KEY_VERSION, "m": payload}, ensure_ascii=False,
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        return ""
    return hashlib.sha256(encoded).hexdigest()[:_KEY_CHARS]


def measured_material(inputs_prov) -> Optional[list]:
    """The MEASURED family's material from a bound `eval.inputs` record, or `None`.

    `inputs_prov` is what `runtime/metric_inputs.bind_inputs` produced. Only a record in which EVERY
    declared input bound contributes: a key over a half-bound input set would be a digest of "the two
    files we could read", and two runs that each failed to read a different file would hash the same
    material. `None` there, not a weaker key — the absence of proof is `unknown`, which is the only
    honest answer and the one the inversion protects.

    THE PATH IS NOT IN THE MATERIAL, and that is the whole point of the module docstring's
    two-indexes-one-path case. What is hashed per input is `(kind, digest-or-shape)`:
      * a FILE contributes its `digest` and `digest_mode` — content, path-independent.
      * a DIRECTORY contributes `entries` + `bytes`, which is what `metric_subject._dir_identity`
        can afford for a subject and is exactly the pair that separates one product index from a
        bigger one. A corpus that grew by a document changes `entries`; one that was rebuilt with
        the same count changes `bytes` unless it is byte-for-byte the same size, and a caller who
        needs more than that declares the index FILE rather than its directory.
    Sorted, so the order two paths were declared in cannot change the key.
    """
    if not isinstance(inputs_prov, dict) or not inputs_prov.get("inputs_bound"):
        return None
    rows = inputs_prov.get("inputs")
    if not isinstance(rows, list) or not rows:
        return None
    material = []
    for row in rows[:_MAX_INPUTS]:
        if not isinstance(row, dict) or not row.get("bound"):
            return None
        kind = str(row.get("kind") or "")[:32]
        if kind == "dir":
            material.append(["dir", _int(row.get("entries")), _int(row.get("bytes"))])
        else:
            digest = str(row.get("digest") or "")[:128]
            if not digest:
                return None
            material.append(["file", str(row.get("digest_mode") or "")[:32], digest])
    return sorted(material, key=lambda item: json.dumps(item, sort_keys=True))


def declared_material(task) -> Optional[str]:
    """The DECLARED family's material — the task's `ComparisonContract.contract_id`, or `None`.

    A pure read of a value `core/comparison.py` already bound and already validated. It is NOT
    re-derived here: `_bind_contract_id` is that module's rule, it rejects a contract whose id does
    not match its own semantic fields, and a second hashing of the same 13 facets in this file is how
    two spellings of one identity start disagreeing.
    """
    if not isinstance(task, dict):
        return None
    contract = task.get("comparison_contract")
    if not isinstance(contract, dict):
        return None
    contract_id = contract.get("contract_id")
    if not isinstance(contract_id, str) or len(contract_id) != 64:
        return None
    return contract_id


def inferred_material(task) -> Optional[list]:
    """The INFERRED family's material — `engine/eval_contract.py`'s key, or `None`.

    A CALL to that module and not a second reading of the snapshot: it already owns which snapshot
    fields name an evaluation (and, just as important, which deliberately do not — `direction`,
    `task_id`, the goal prose, timeouts and seeds). Its `None` for a snapshot with no evaluation
    identity at all rides straight through: 14 of the 46 snapshots on this box are toy/probe runs
    whose eval is a builtin, and an all-empty key would make every one of them "the same" as every
    other, which is the inversion again one authority down.
    """
    from looplab.engine.eval_contract import contract_from_task

    contract = contract_from_task(task)
    if contract is None:
        return None
    kind, key, command, paths = contract.key()
    return [str(kind)[:_MAX_FIELD_CHARS], str(key)[:_MAX_FIELD_CHARS],
            [str(token)[:256] for token in command], [str(path)[:256] for path in paths]]


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


# The key a settle record carries the `profile` facet under, ALREADY DIGESTED — never the override
# tokens themselves (second critic pass, 2026-09-26: the settle row had become the one event carrying
# raw operator argv past the redaction funnel). `engine/settled_recovery.py` writes it through
# `digested_protocol`; `_profile_facet` passes it through, so a node finalized from its settle record
# records the digest a live terminal would.
PROFILE_DIGEST_KEY = "profile_digest"


def _profile_facet(eval_protocol) -> str:
    """The `profile` facet of one `eval_protocol` record: the digest of its override tokens, the
    pre-digested `PROFILE_DIGEST_KEY` a settle record carries, or `""` (absent) for anything else."""
    if not isinstance(eval_protocol, dict):
        return ""
    overrides = eval_protocol.get("overrides")
    if isinstance(overrides, list):
        return _digest(["overrides", [str(token)[:256] for token in overrides[:64]]])
    digest = eval_protocol.get(PROFILE_DIGEST_KEY)
    if (isinstance(digest, str) and len(digest) == _KEY_CHARS
            and all(ch in "0123456789abcdef" for ch in digest)):
        return digest
    return ""


def digested_protocol(eval_protocol) -> Optional[dict]:
    """`eval_protocol` as a settle record may persist it — `{PROFILE_DIGEST_KEY: digest}`, or `None`
    when it carries no readable profile facet."""
    digest = _profile_facet(eval_protocol)
    return {PROFILE_DIGEST_KEY: digest} if digest else None


def protocol_record(*, eval_protocol=None, host_scorer=None, fingerprint=None) -> Optional[dict]:
    """`{facet: digest}` for the facets this measurement recorded, or `None` when it recorded none.

    Total over junk (every input is read off a `RunResult` or a hand-edited log). A facet that
    cannot be read is ABSENT, never a digest of nothing: `_protocol_mismatch` only refuses over a
    facet BOTH sides carry, so an absent facet keeps every pre-2026-09-26 record reading as it did.
    """
    facets = {"profile": _profile_facet(eval_protocol)}
    program = host_scorer.get("program_sha256") if isinstance(host_scorer, dict) else None
    if isinstance(program, str) and program:
        facets["scorer"] = _digest(["program_sha256", program[:128]])
    if isinstance(fingerprint, str) and fingerprint:
        facets["fingerprint"] = _digest(["eval_fingerprint", fingerprint[:128]])
    facets = {name: value for name, value in facets.items() if value}
    return facets or None


def comparability_record(*, task=None, inputs_prov=None, substrate=None,
                         protocol=None) -> Optional[dict]:
    """The record that rides beside a metric — `{"version", "authority", "keys"}` — or `None`.

    `None` when no family could be built at all, which is `unknown` at every consumer. It is a
    deliberate `None` and not an empty record: an empty record with an empty `keys` map would
    compare EQUAL to every other empty record, and "two runs that recorded nothing are the same
    evaluation" is the exact statement this module exists to refuse.

    `authority` is the STRONGEST family this record carries. It is derived here rather than left to
    each reader, because it is the one field a UI badge and a CLI line both render and they must not
    disagree about which word to print.
    """
    keys = {}
    measured = measured_material(inputs_prov)
    if measured is not None:
        keys[AUTHORITY_MEASURED] = _digest(measured)
    declared = declared_material(task)
    if declared is not None:
        keys[AUTHORITY_DECLARED] = _digest(declared)
    inferred = inferred_material(task)
    if inferred is not None:
        keys[AUTHORITY_INFERRED] = _digest(inferred)
    keys = {name: value for name, value in keys.items() if value}
    if not keys:
        return None
    authority = next(name for name in AUTHORITIES if name in keys)
    record = {"version": KEY_VERSION, "authority": authority, "keys": keys}
    # THE SUBSTRATE — the editable source tree this number was produced ON, as a digest.
    #
    # It is deliberately NOT an authority and is kept OUTSIDE `keys`. An authority answers "may
    # these two numbers be read on one scale", and equality at one is a positive statement: SAME.
    # Equal substrate says nothing of the kind — two nodes built from the same repo against
    # different corpora are not the same evaluation — so admitting it to `keys` would let a code
    # match certify a comparison the data refutes. It can only DISCRIMINATE: a DIFFERENT substrate
    # makes two nodes incomparable no matter how their authorities line up.
    #
    # Why this exists at all: `RunState.repair_candidates()` ranks the files a run's nodes keep
    # re-fixing precisely so an operator will promote one into the source repo, and promoting moves
    # the ground every later node is measured on. Before this the record could not say which side of
    # that move a node ran on. Absent when nothing was fingerprinted, which is every task with no
    # editable repo and every log written before this shipped — and absence is `unknown`, never
    # "the same", exactly as it is for every authority.
    digest = _digest(substrate) if substrate else None
    if digest:
        record["substrate"] = digest
    # THE PROTOCOL — the conditions the number was measured under (`protocol_record`). Outside `keys`
    # for the substrate's reason: a matching protocol certifies nothing (the same ruler over
    # different data is not one evaluation), so it may only ever DISCRIMINATE. Absent when nothing
    # was recorded, which is every log before 2026-09-26 and every solution-tier eval.
    if isinstance(protocol, dict):
        facets = {name: value for name, value in protocol.items()
                  if name in PROTOCOL_FACETS and isinstance(value, str) and value}
        if facets:
            record["protocol"] = facets
    return record


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


def _common_authority(this: dict, other: dict) -> Optional[str]:
    """The strongest authority both records carry, or `None` when they share none."""
    this_keys = this.get("keys") if isinstance(this.get("keys"), dict) else {}
    other_keys = other.get("keys") if isinstance(other.get("keys"), dict) else {}
    return next((name for name in AUTHORITIES if this_keys.get(name) and other_keys.get(name)), None)


def _substrate_mismatch(this: Optional[dict], other: Optional[dict]) -> Optional[tuple[str, str]]:
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


def _protocol_mismatch(this: Optional[dict],
                       other: Optional[dict]) -> Optional[tuple[str, str, str]]:
    """`(facet, mine, theirs)` for the FIRST protocol facet both records carry and disagree on.

    `_substrate_mismatch`'s rule, per facet and for the same reasons: ONE spelling for the status
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
    if _substrate_mismatch(this, other) is not None:
        return DIFFERENT
    # …AND SO IS THE PROTOCOL, on the same ground: a number measured under a different ruler (a
    # `smoke` override set, an edited scorer, a decoder the eval says it ran differently) is not on
    # one scale with this one whatever the input keys say, and a matching protocol certifies nothing.
    if _protocol_mismatch(this, other) is not None:
        return DIFFERENT
    authority = _common_authority(this, other)
    if authority is None:
        return UNKNOWN
    if this["keys"][authority] != other["keys"][authority]:
        return DIFFERENT
    return SAME if authority in CERTIFYING else UNKNOWN


# What an operator is told, per status. The `DIFFERENT` sentence is the one that has to be checkable:
# it names the authority, because "different key" alone cannot be told apart from a bug in this file.
_SUBSTRATE_NOTICE = (
    "NOT COMPARABLE: {who}ran on a different source tree (substrate {theirs} vs {mine}). A fix "
    "promoted into the editable repo moves the ground every later experiment is measured on, so the "
    "two values are not on one scale whatever their input keys say.")
# One clause per protocol facet, naming what differed in words an operator can act on.
_PROTOCOL_CLAUSES = {
    "profile": "was scored under a different eval profile (a different set of profile overrides "
               "— e.g. a `smoke` pass against a `full` one)",
    "scorer": "was scored by a different host scorer program (its bytes changed between the two)",
    "fingerprint": "printed a different `eval_fingerprint` (the eval itself says it measured under "
                   "different conditions)",
}
_PROTOCOL_NOTICE = (
    "NOT COMPARABLE: {who}{clause} (protocol {facet} {theirs} vs {mine}). The two values were "
    "measured with different rulers, so neither is a target for the other.")
_NOTICES = {
    DIFFERENT: ("NOT COMPARABLE: {who}measured its number against a different evaluation "
                "({authority} key {theirs} vs {mine}). The two values are not on one scale and "
                "neither is a target for the other."),
    UNKNOWN: ("COMPARABILITY UNKNOWN: {who}records no evidence that its number was measured "
              "against the same data and protocol as this one, so the two values are observations "
              "and not a ranking. Declare `eval.inputs` (or a `comparison_contract`) on both to "
              "make this decidable."),
}


def comparability_notice(this: Optional[dict], other: Optional[dict], *,
                         other_run_id: str = "") -> str:
    """The one sentence a surface prints beside a foreign number. `""` only when it is `SAME`.

    NOT empty for `UNKNOWN` — and that is the difference from `eval_contract.contract_notice`, which
    is silent there on purpose. That module ANNOTATES a value it has no standing to doubt; this one
    answers "may these be ranked", and silence on `unknown` is read as assent by every human who
    looks at a table. The inversion is only real if it is on the screen.
    """
    status = comparability_status(this, other)
    if status == SAME:
        return ""
    who = f"run {other_run_id} " if other_run_id else "the other measurement "
    if status == DIFFERENT:
        # THE SUBSTRATE MISMATCH GETS ITS OWN SENTENCE, for the reason recorded above `_NOTICES`: a
        # `DIFFERENT` that names an authority must be checkable against that authority's keys, and
        # here those keys may be IDENTICAL — the refusal came from the source tree, not from them.
        # Falling through would print "different inferred key" with two `?` placeholders, which is
        # exactly the "cannot be told apart from a bug in this file" failure that comment forbids.
        pair = _substrate_mismatch(this, other)
        if pair is not None:
            return _SUBSTRATE_NOTICE.format(who=who, theirs=pair[1], mine=pair[0])
        # …and so does a protocol mismatch, for the same reason: the authority keys may agree.
        facet_pair = _protocol_mismatch(this, other)
        if facet_pair is not None:
            facet, mine, theirs = facet_pair
            return _PROTOCOL_NOTICE.format(who=who, clause=_PROTOCOL_CLAUSES[facet], facet=facet,
                                           theirs=theirs, mine=mine)
        authority = _common_authority(this or {}, other or {}) or AUTHORITY_INFERRED
        return _NOTICES[DIFFERENT].format(
            who=who, authority=authority,
            theirs=(other or {}).get("keys", {}).get(authority, "?"),
            mine=(this or {}).get("keys", {}).get(authority, "?"))
    return _NOTICES[UNKNOWN].format(who=who)


def group_token(record: Optional[dict]) -> str:
    """`"<authority>:<key>"` for one record, or `""` when it has none — a PARTITION label.

    The shape a durable row and a browser partition both store, because a grouping key has to be one
    comparable scalar and the pair (authority, digest) is what decides membership. `""` is its own
    group and NOT a wildcard: rows that recorded nothing group together and keep behaving exactly as
    they did before any of this shipped, and a row that DID record a key leaves that group rather
    than joining it. That asymmetry is the inversion again, in the one place it changes durable
    state — a keyed result must never be silently elected champion over unkeyed ones on the strength
    of a comparison nobody made.

    NOT `comparability_status`, and the difference matters: the status is a decision about a PAIR and
    knows that `inferred` may refuse but not certify. A partition label cannot express that, so a
    grouping built on it is deliberately COARSER — two runs sharing an `inferred` token still share a
    group. That is the pre-existing behaviour (the token is derived from the same task snapshot the
    old grouping had), so grouping never becomes stricter than the evidence, only never looser.
    """
    if not isinstance(record, dict):
        return ""
    authority = str(record.get("authority") or "")
    keys = record.get("keys")
    key = keys.get(authority) if isinstance(keys, dict) else None
    # THE SUBSTRATE IS DELIBERATELY NOT IN THE TOKEN, and it was for one commit on 2026-08-25. The
    # argument for adding it reads well — `comparability_status` refuses a pair whose source trees
    # differ, so a token ignoring that puts two "incomparable" rows in one group — and it is wrong
    # twice over.
    #
    # It contradicts this function's own contract three paragraphs up: the grouping is "deliberately
    # COARSER" than the status and "never becomes stricter than the evidence, only never looser".
    # Making it stricter is the one direction ruled out.
    #
    # And the cost is concrete, because this token is PERSISTED: `engine/lessons.py` writes it as
    # `cases.jsonl`'s `comparability` field, where it partitions the cross-run case library that
    # elects a prior champion. The substrate moves on every commit to the editable repo — which is
    # exactly what `looplab repair-candidates` urges an operator to do — so one promoted fix would
    # put every later run in a singleton group and the library would stop electing anything at all.
    # A refusal about ONE PAIR must not become a fact about a whole corpus.
    #
    # THE PROTOCOL IS LEFT OUT FOR THE FIRST OF THOSE TWO REASONS (2026-09-26). It moves less often
    # than the substrate, but a token that splits on it is STRICTER than the evidence all the same,
    # and a run whose endgame rescored on `full` would split its own cases in two.
    return f"{authority}:{key}" if key else ""


def run_split_by_key(nodes) -> bool:
    """Do this run's own evaluated nodes carry a PROVABLY DIFFERENT pair of comparability records?

    True is the within-run refusal: the run compared its own candidates against different data, or
    under different evaluation protocols (a `protocol` facet both sides recorded and that differs —
    a `smoke` profile beside a `full` one, an edited scorer), so its champion is the winner of a
    mixed field. The NAME predates the protocol facets; it asks `comparability_status`, which
    checks both. Cheap by construction — inside one run the key is
    normally constant, so this walks the nodes and finds one pair.

    Asked PAIRWISE through `comparability_status` rather than by counting distinct keys, because two
    records may differ in their `keys` maps (one node bound its inputs, a later one did not) without
    either being evidence that the DATA changed. Only a genuine `DIFFERENT` at a shared authority
    counts, which is the same asymmetry the module docstring states.
    """
    records = [record for record in (record_of(node) for node in (nodes or [])) if record]
    for index, record in enumerate(records):
        for other in records[index + 1:]:
            if comparability_status(record, other) == DIFFERENT:
                return True
    return False
