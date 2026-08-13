"""Card identity: the digests, receipts and provenance the Card lane's ownership rests on.

Split out of ``core/models.py`` (doc 25 CO-02), which had grown to 2,320 lines around five separable
subsystems.  What lives here is everything a change to CARD IDENTITY touches, so that changing it no
longer churns the same file as ``Node``/``Idea``/``RunState``:

* the quantitative resource footprint — its tolerant durable reader, the operator-override merge, and
  the Developer's in-band marker parser.  ``footprint`` is one of the digested action fields, so its
  normalization IS part of the preimage;
* the closed steering-context vocabulary, the one bounded cue snapshot both live proposal writers and
  the durable replay boundary validate against;
* ``durable_idea_payload`` and the versioned ``idea_proposal_digest`` it is the preimage boundary for.
  The serializer moved WITH the digest deliberately: the digest's own comment ("Start at the durable
  boundary as well") makes it load-bearing for identity, and a change to it silently re-values every
  idea digest, so the two must be readable together;
* the versioned Card action digests (v1 / expanded-v1 / v2), ``valid_card_action_digest`` and the
  three ownership-receipt constructors;
* the belief identity a card is hash-joined on (``hypothesis_id`` and its sha256 sibling) — ``1 card =
  1 hypothesis``, so these ARE card identity, and ``Card``'s own fail-closed selection validator calls
  one of them;
* the Card provenance model family and ``Card`` itself, the aggregate of all of the above.

Every one of these names is re-exported from ``core.models`` (the seam ``core.concepts`` and
``core.fitness`` already established), so the ~360 existing import sites and every monkeypatch seam
keep resolving to the SAME objects.

Layering: below everything.  It imports only ``core.concepts`` and ``core.jsonutil``, both leaves, and
reaches ``core.models`` for nothing but the ``Idea`` ANNOTATION on two functions — a runtime import
would close the cycle, since ``models`` imports this module.

Identity is frozen.  Every digest below gates receipt acceptance on replay: if a value changes, every
already-issued receipt silently stops verifying and runs that were legitimately calibrated are refused
with no error that says why.  ``tests/test_card_identity_home.py`` pins the values AND the field sets
they were derived from, so the two causes of a shift can be told apart.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import TYPE_CHECKING, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from looplab.core.concepts import ConceptMaterializationReceipt
# Aliased so the shared digest tail does not become part of this module's public namespace: historical
# consumers import domain contracts from here, and `canonical_json_digest` belongs to core.jsonutil.
from looplab.core.jsonutil import (DIGEST_TEXT_CAP as _DIGEST_TEXT_CAP,
                                   canonical_json_digest as _canonical_json_digest,
                                   valid_digest_ref)

if TYPE_CHECKING:  # annotation only — importing `models` at runtime would close the import cycle
    from looplab.core.models import Idea


_RESOURCE_INT_MAX = (1 << 31) - 1


def _resource_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        number = int(value)
    else:
        return None
    return number if 0 <= number <= _RESOURCE_INT_MAX else None


def normalize_researcher_footprint(value) -> dict | None:
    """Tolerant durable reader for the researcher-owned quantitative resource declaration."""
    if not isinstance(value, dict):
        return None
    out: dict[str, int | None] = {}
    if "gpus" in value and (gpus := _resource_int(value.get("gpus"))) is not None:
        out["gpus"] = gpus
    if "gpu_mem_mib" in value:
        raw_mem = value.get("gpu_mem_mib")
        if raw_mem is None:
            out["gpu_mem_mib"] = None
        elif (memory := _resource_int(raw_mem)) is not None:
            out["gpu_mem_mib"] = memory
    # Authority fields (`pinned_by`/`finalized_by`) belong to later operator/developer events. Dropping
    # every non-quantitative key prevents a researcher-authored Idea from forging that provenance.
    return out or None


def effective_card_footprint(
    footprint,
    resource_pin,
    *,
    gpu_count: int | None = None,
    gpu_memory_mib: tuple[int, ...] | list[int] = (),
) -> dict | None:
    """Return the quantitative Card footprint after the independent operator override.

    ``Card.footprint`` is part of the immutable action ownership receipt, so an operator resource
    command must never rewrite it. The override is merged only at admission/freshness time and is
    optionally re-clamped to the current machine envelope. That last step makes a pin accepted on a
    larger host safe after resume on a smaller host without mutating replayed history.
    """
    base = normalize_researcher_footprint(footprint) or {}
    pin = normalize_researcher_footprint(resource_pin) or {}
    out = {**base, **pin}
    if not out:
        return None
    # An explicit CPU-only override owns the whole GPU dimension. In particular it clears inherited
    # GPU-memory demand even when the caller only needs a merge and has no live envelope to pass.
    if out.get("gpus") == 0:
        out.pop("gpu_mem_mib", None)
        return out
    if gpu_count is None:
        return out
    if isinstance(gpu_count, bool) or not isinstance(gpu_count, int) or gpu_count < 0:
        raise ValueError("gpu_count must be a non-negative integer or None")
    # a positive declaration on a zero-device host remains explicitly unavailable. The
    # scheduler consumes the same contract and emits a fail-closed reservation marker; only a genuine
    # operator/researcher ``gpus=0`` declaration owns CPU-only semantics.
    if "gpus" in out and (gpu_count > 0 or out["gpus"] == 0):
        # Match scheduler admission on a GPU-less host: an explicit positive declaration remains
        # unavailable instead of being silently rewritten into a CPU-only action. A genuine zero pin
        # is still authoritative and may clear inherited GPU-memory demand above.
        out["gpus"] = min(out["gpus"], gpu_count)
    required = out.get("gpus", 1 if gpu_count else 0)
    if required == 0:
        out.pop("gpu_mem_mib", None)
        return out
    memory = tuple(gpu_memory_mib or ())
    if (gpu_count > 0
            and isinstance(out.get("gpu_mem_mib"), int)
            and len(memory) == gpu_count
            and all(type(value) is int and value >= 0 for value in memory)):
        envelope = sorted(memory, reverse=True)[min(required, gpu_count) - 1]
        out["gpu_mem_mib"] = min(out["gpu_mem_mib"], envelope)
    return out


def valid_researcher_footprint(value) -> bool:
    if not isinstance(value, dict) or not value or not set(value) <= {"gpus", "gpu_mem_mib"}:
        return False
    if "gpus" in value and (type(value["gpus"]) is not int
                            or not 0 <= value["gpus"] <= _RESOURCE_INT_MAX):
        return False
    raw_mem = value.get("gpu_mem_mib")
    if ("gpu_mem_mib" in value and raw_mem is not None
            and (type(raw_mem) is not int or not 0 <= raw_mem <= _RESOURCE_INT_MAX)):
        return False
    return True


DEVELOPER_FOOTPRINT_MARKER = "# LOOPLAB_FOOTPRINT:"


def developer_artifact_footprint(proposed, code="", files=None) -> dict | None:
    """Resolve the Developer's quantitative finalization from its shipped artifact.

    An unspecified Researcher declaration deliberately stays unspecified for legacy scheduling. When
    resources were proposed, a Developer may confirm or scale them by placing one compact JSON marker in
    shipped code; absent/malformed markers conservatively retain the proposal. Only the two quantitative
    keys cross this boundary, so code comments cannot forge provenance.
    """
    fallback = normalize_researcher_footprint(proposed)
    if fallback is None:
        return None
    blobs: list[str] = [code] if isinstance(code, str) else []
    if isinstance(files, dict):
        for _name, body in sorted(files.items(), key=lambda row: str(row[0]))[:64]:
            if isinstance(body, str):
                blobs.append(body)
    remaining = 65_536
    for blob in blobs:
        if remaining <= 0:
            break
        sample = blob[:min(8_192, remaining)]
        remaining -= len(sample)
        for line in sample.splitlines()[:80]:
            text = line.strip()
            if not text.startswith(DEVELOPER_FOOTPRINT_MARKER):
                continue
            raw = text[len(DEVELOPER_FOOTPRINT_MARKER):].strip()
            if not raw or len(raw) > 256:
                continue
            try:
                decoded = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if valid_researcher_footprint(decoded):
                return normalize_researcher_footprint(decoded)
    return fallback


CARD_STEERING_CONTEXT_FIELDS = {
    "complexity": {"siblings", "level"},
    "eval_budget": {"remaining_seconds", "total_seconds", "stance"},
    "experiment_time_budget": {"seconds"},
    "gpu_constraint": {"mode"},
    "failure_reflection": {"node_ids"},
    "watchdog_reflection": set(),
    "trust_reflection": set(),
    "fault_localization": {"file_count"},
    "feature_engineering": set(),
    "reflection_prior": set(),
    "cross_run_advisory": {"ref", "status"},
    "cross_run_tools": set(),
    "concept_authoring": {"mode"},
    "concept_slug_reuse": set(),
    "research_memo": {"ref"},
    "strategy": {"novelty_stance", "fidelity"},
    "sweep": set(),
}
_CARD_STEERING_ENUMS = {
    "level": {"minimal", "moderate", "advanced"},
    "stance": {"explore", "selective", "exploit"},
    "mode": {"single_device", "declared_footprint", "delta", "full"},
    "status": {"available", "unavailable"},
    "novelty_stance": {"explore", "balanced", "exploit"},
    "fidelity": {"cheap", "balanced", "full"},
}


def normalize_steering_context(value) -> list[dict] | None:
    """Return one bounded ref/scalar-only Card cue snapshot, or fail the whole snapshot.

    The contract lives in ``core.cards`` (it was ``core.models``, doc 25 CO-02) because both live
    proposal writers and the durable replay
    boundary must apply the same closed vocabulary. A future prompt/body/path field is rejected until
    explicitly reviewed; silently projecting it away would make a false lossless receipt possible.
    """
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or len(value) > 32:
        return None
    out: list[dict] = []
    for raw in value:
        if not isinstance(raw, dict):
            return None
        kind = raw.get("kind")
        allowed = CARD_STEERING_CONTEXT_FIELDS.get(kind) if isinstance(kind, str) else None
        if allowed is None or "kind" not in raw or not set(raw) <= ({"kind"} | allowed):
            return None
        item = {"kind": kind}
        for key in sorted(allowed):
            if key not in raw:
                continue
            current = raw[key]
            if key == "node_ids":
                if (not isinstance(current, list) or len(current) > 16
                        or any(type(node_id) is not int or not 0 <= node_id <= (1 << 31) - 1
                               for node_id in current)):
                    return None
                item[key] = list(dict.fromkeys(current))
            elif key in {"siblings", "file_count"}:
                if type(current) is not int or not 0 <= current <= 1_000_000:
                    return None
                item[key] = current
            elif key in {"remaining_seconds", "total_seconds", "seconds"}:
                # An arbitrary-precision int from a corrupt/future/external writer overflows float()
                # (`int too large to convert to float`). A raise here would brick replay — the fold
                # loop has NO per-event guard — so treat an unconvertible value as fail-closed (None),
                # exactly like every other rejected shape, instead of crashing every replay/resume/view.
                if isinstance(current, bool) or not isinstance(current, (int, float)):
                    return None
                try:
                    fval = float(current)
                except (TypeError, ValueError, OverflowError):
                    return None
                if not math.isfinite(fval) or not 0 <= fval <= 1e12:
                    return None
                item[key] = round(fval, 3)
            elif key == "ref":
                if not valid_digest_ref(
                        current,
                        prefix="memo:sha256:" if kind == "research_memo" else "sha256:"):
                    return None
                item[key] = current
            elif key in _CARD_STEERING_ENUMS:
                if current not in _CARD_STEERING_ENUMS[key]:
                    return None
                item[key] = current
            else:
                return None
        out.append(item)
    return out


def durable_idea_payload(idea: Idea) -> dict[str, Any]:
    """Serialize an Idea for ``node_created`` without inventing a concept mode.

    The explicit pops are a regression-proof durable boundary in addition to Idea's nested serializer.
    Pydantic materializes list defaults during validation, so keep an explicitly supplied empty legacy
    field but do not turn a genuinely absent concept envelope into three authored empty lists.
    """
    payload = idea.model_dump(mode="json")
    if idea.concept_mode is None:
        payload.pop("concept_mode", None)
        for field in ("concepts", "concepts_added", "concepts_removed"):
            if field not in idea.model_fields_set:
                payload.pop(field, None)
    return payload


IDEA_PROPOSAL_DIGEST_V1_FIELDS = (
    "operator", "params", "rationale", "eval_profile", "eval_timeout", "theme",
    "concepts", "concept_mode", "concepts_added", "concepts_removed", "space",
    "hypothesis", "card_id", "footprint",
)


def idea_proposal_digest(idea: Idea) -> str | None:
    """Versioned exact digest of one bounded normalized durable Idea, or None when it is oversized."""
    try:
        # V1 is a frozen semantic field set. Hashing the whole model dump would make a
        # future additive Idea default invalidate every already-stamped event when an old log is replayed.
        # Start at the durable boundary as well: an absent legacy concept envelope and an explicitly
        # authored empty envelope replay differently, so model defaults must not collapse their identity.
        durable = durable_idea_payload(idea)
        payload = {
            field: durable[field]
            for field in IDEA_PROPOSAL_DIGEST_V1_FIELDS
            if field in durable
        }
    except Exception:  # noqa: BLE001 - an advisory binding must never block proposal admission
        return None
    budget = [4_096, 65_536]  # total JSON atoms, total string/key characters

    def _complete(value, depth=0):
        if depth > 8 or budget[0] <= 0:
            raise ValueError("idea identity exceeds structural budget")
        budget[0] -= 1
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            if abs(value) > (1 << 53) - 1:
                raise ValueError("idea identity integer is outside JSON-safe range")
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("idea identity contains a non-finite number")
            return 0.0 if value == 0.0 else value
        if isinstance(value, str):
            budget[1] -= len(value)
            if budget[1] < 0:
                raise ValueError("idea identity exceeds text budget")
            return value
        if isinstance(value, list):
            if len(value) > 256:
                raise ValueError("idea identity list is oversized")
            return [_complete(item, depth + 1) for item in value]
        if isinstance(value, dict):
            if len(value) > 256 or any(not isinstance(key, str) for key in value):
                raise ValueError("idea identity mapping is oversized or malformed")
            key_chars = 0
            for key in value:
                # Reject attacker-sized keys before ordering them. Sorting up to 256 huge strings would
                # otherwise pay comparison cost even though the digest must fail its text budget anyway.
                if len(key) > 512:
                    raise ValueError("idea identity key exceeds text budget")
                key_chars += len(key)
                if key_chars > budget[1]:
                    raise ValueError("idea identity exceeds text budget")
            budget[1] -= key_chars
            out = {}
            for key in sorted(value):
                out[key] = _complete(value[key], depth + 1)
            return out
        raise ValueError("idea identity contains a non-JSON value")

    try:
        bounded = _complete(payload)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    # The BOUNDING walker above is this identity's frozen v1 preimage and stays here; only the
    # dump/hash/cap tail is shared (doc 25 CO-08). Byte-identical: `canonical_json` passes the same
    # four json.dumps options this call site spelled out.
    return _canonical_json_digest(bounded, prefix="idea:v1:", cap=_DIGEST_TEXT_CAP)


def idea_proposal_ref(idea: Idea) -> dict | None:
    digest = idea_proposal_digest(idea)
    return {"v": 1, "digest": digest} if digest is not None else None


# The shipped v1 preimage is immutable. Lifecycle generations, eval_timeout and scored_against_empty were
# added later and therefore belong to v2; changing this tuple would invalidate durable historical Cards.
CARD_ACTION_DIGEST_V1_FIELDS = (
    "operator", "params", "space", "eval_profile", "parent_id", "parent_ids",
    "scored_against", "footprint",
)
CARD_ACTION_DIGEST_V2_FIELDS = (
    "operator", "params", "space", "eval_profile", "eval_timeout", "parent_id", "parent_ids",
    "parent_generations", "scored_against", "scored_against_generation",
    "scored_against_empty", "footprint",
)
_CARD_ACTION_DIGEST_VERSIONS = frozenset({1, 2})

# The proposal-time concept envelope a ``card_added`` idea block may carry — and the deliberate
# COMPLEMENT of the two digest tuples above: not one of these four is digested, so a Card's ownership
# receipt is byte-identical with and without them and recording what the Researcher tagged can never
# invalidate an already-minted Card. That exemption is why the envelope is allowed in the idea block
# at all: it is metadata with its own receipt (``Card.concept_source``), not executable action.
#
# It is ONE tuple because a writer and a reader have to agree on it EXACTLY.
# ``engine/card_reservation.py::_authored_card_concepts`` emits these keys;
# ``events/card_ledger.py`` decodes them (``_bounded_card_action``) and admits them
# (``_CARD_ADDED_ACTION_FIELDS``). A key one side knows and the other does not is read as a lossy
# FUTURE action member, which silently costs the Card its ``selection_ready`` — measured, and it is
# exactly why a DELTA proposal's membership stayed out of the Card lane after ``2acdb825`` carried a
# full one in. ``tests/test_card_concept_round_trip.py`` drives both the agreement and the exemption.
CARD_IDEA_CONCEPT_FIELDS = ("concept_mode", "concepts", "concepts_added", "concepts_removed")

# One semantic boundary for every Card producer, replay path, identity digest, and public projection.
# The UTF-8 cap is deliberately the worst-case encoding size of the character cap, so valid Unicode
# statements are never accepted by one layer and rejected by another.
#
# REVIEW (mega-review 2026-08-13): "every Card producer" is not yet true — two engine producer
# sites still hardcode the pre-widening 2,048 cap (`engine/card_reservation.py` `_card_statement`
# and its merge-payload sibling; grep for 2_048 there). Fail-closed direction, so no corruption,
# but the layers disagree about one statement: the HTTP boundary and replay accept 4,000 chars
# while an engine-minted Idea with a 2,049-char hypothesis is silently refused Card ownership.
# Point those two sites at this constant or narrow this comment's claim.
CARD_STATEMENT_MAX_CHARS = 4_000
CARD_STATEMENT_MAX_UTF8_BYTES = 16_000


def _card_action_digest(
        card_id: str, statement: str, action: dict, *, version: int,
        expanded_v1: bool = False) -> str | None:
    """Return one versioned exact bounded identity of a Card work item.

    This is deliberately narrower than :func:`idea_proposal_digest`: this digest binds ONLY the
    executable work-item identity, not the card's research-direction / belief facet.  The digest binds the stable
    card id and immutable seed statement to the concrete build action and its freshness/parent anchors.
    Concept membership is metadata with its own completeness receipt and is intentionally not a
    prerequisite for execution.
    """
    try:
        statement_bytes = len(statement.encode("utf-8")) if isinstance(statement, str) else 0
    except UnicodeError:
        return None
    if (version not in _CARD_ACTION_DIGEST_VERSIONS
            or (expanded_v1 and version != 1)
            or not isinstance(card_id, str) or not card_id or card_id != card_id.strip()
            or len(card_id) > 256 or not card_id.isprintable()
            or not isinstance(statement, str) or not statement.strip()
            or statement != statement.strip() or len(statement) > CARD_STATEMENT_MAX_CHARS
            or statement_bytes > CARD_STATEMENT_MAX_UTF8_BYTES
            or not isinstance(action, dict)):
        return None

    operator = action.get("operator")
    if (not isinstance(operator, str) or not operator or operator != operator.strip()
            or len(operator) > 64 or not operator.isprintable()):
        return None

    def _number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("card action values must be finite numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("card action values must be finite numbers")
        return 0.0 if number == 0.0 else number

    def _params(value) -> dict[str, float]:
        if value is None:
            return {}
        if (not isinstance(value, dict) or len(value) > 64
                or any(not isinstance(key, str) or not key or len(key) > 200
                       or not key.isprintable() for key in value)):
            raise ValueError("card params are malformed or oversized")
        return {key: _number(value[key]) for key in sorted(value)}

    def _space(value) -> dict[str, list[float]]:
        if value is None:
            return {}
        if (not isinstance(value, dict) or len(value) > 64
                or any(not isinstance(key, str) or not key or len(key) > 200
                       or not key.isprintable() for key in value)):
            raise ValueError("card search space is malformed or oversized")
        out: dict[str, list[float]] = {}
        for key in sorted(value):
            values = value[key]
            if not isinstance(values, list) or len(values) > 64:
                raise ValueError("card search-space values are malformed or oversized")
            out[key] = [_number(item) for item in values]
        return out

    def _node_id(value):
        if value is None:
            return None
        if type(value) is not int or not 0 <= value <= (1 << 31) - 1:
            raise ValueError("card node anchors must be bounded integers")
        return value

    def _generation(value):
        if value is None:
            return None
        if type(value) is not int or not 0 <= value <= (1 << 31) - 1:
            raise ValueError("card lifecycle generations must be bounded integers")
        return value

    try:
        raw_parent_ids = action.get("parent_ids", [])
        if (not isinstance(raw_parent_ids, list) or len(raw_parent_ids) > 64
                or len(set(raw_parent_ids)) != len(raw_parent_ids)):
            return None
        parent_ids = [_node_id(value) for value in raw_parent_ids]
        if any(value is None for value in parent_ids):
            return None
        parent_id = _node_id(action.get("parent_id"))
        scored_against = _node_id(action.get("scored_against"))
        profile = action.get("eval_profile")
        if (profile is not None and (not isinstance(profile, str) or len(profile) > 256
                                     or not profile.isprintable())):
            return None
        footprint = action.get("footprint")
        if footprint is not None:
            footprint = normalize_researcher_footprint(footprint)
            if footprint is None:
                return None
        action_payload = {
            "operator": operator,
            "params": _params(action.get("params")),
            "space": _space(action.get("space")),
            "eval_profile": profile,
            "parent_id": parent_id,
            "parent_ids": parent_ids,
            "scored_against": scored_against,
            "footprint": footprint,
        }
        if version == 2 or expanded_v1:
            raw_parent_generations = action.get("parent_generations")
            if raw_parent_generations is None:
                parent_generations = None
            else:
                if (not isinstance(raw_parent_generations, dict)
                        or len(raw_parent_generations) > 64
                        or set(raw_parent_generations) != {str(parent) for parent in parent_ids}):
                    return None
                parent_generations = {
                    key: _generation(raw_parent_generations[key])
                    for key in sorted(raw_parent_generations)
                }
                if any(value is None for value in parent_generations.values()):
                    return None
            scored_against_generation = _generation(action.get("scored_against_generation"))
            scored_against_empty = action.get("scored_against_empty", False)
            if type(scored_against_empty) is not bool:
                return None
            if ((scored_against is None and scored_against_generation is not None)
                    or (scored_against is not None and scored_against_empty)):
                return None
            raw_eval_timeout = action.get("eval_timeout")
            eval_timeout = None if raw_eval_timeout is None else _number(raw_eval_timeout)
            if eval_timeout is not None and eval_timeout <= 0:
                return None
            action_payload.update({
                "eval_timeout": eval_timeout,
                "parent_generations": parent_generations,
                "scored_against_generation": scored_against_generation,
                "scored_against_empty": scored_against_empty,
            })
        payload = {
            "v": version,
            "card_id": card_id,
            "statement": statement,
            "action": action_payload,
        }
    except (TypeError, ValueError, OverflowError, UnicodeError):
        return None
    # As in `idea_proposal_digest`: the versioned preimage above is frozen and stays here; the
    # dump/hash/cap tail is the shared one (doc 25 CO-08), byte-identical to the four options this
    # call site used to spell out.
    return _canonical_json_digest(payload, prefix=f"card-action:v{version}:", cap=_DIGEST_TEXT_CAP)


def card_action_digest(card_id: str, statement: str, action: dict) -> str | None:
    """Mint the current v2 Card action identity, including lifecycle and timeout fences."""
    return _card_action_digest(card_id, statement, action, version=2)


def legacy_card_action_digest_v1(card_id: str, statement: str, action: dict) -> str | None:
    """Recompute the frozen historical v1 Card action identity for replay verification only."""
    return _card_action_digest(card_id, statement, action, version=1)


def transitional_card_action_digest_v1(card_id: str, statement: str, action: dict) -> str | None:
    """Verify receipts minted by the short-lived expanded-v1 writer before v2 was introduced."""
    return _card_action_digest(card_id, statement, action, version=1, expanded_v1=True)


def valid_card_action_digest(value: object, *, version: int | None = None) -> bool:
    """Whether ``value`` is one canonical supported Card action digest."""
    versions = (version,) if version is not None else tuple(sorted(_CARD_ACTION_DIGEST_VERSIONS))
    if not isinstance(value, str):
        return False
    for candidate in versions:
        if candidate not in _CARD_ACTION_DIGEST_VERSIONS:
            continue
        if valid_digest_ref(value, prefix=f"card-action:v{candidate}:"):
            return True
    return False


def card_ownership_receipt(card_id: str, statement: str, action: dict) -> dict | None:
    """Create the current v2 durable ``card_added`` ownership receipt for a concrete work item."""
    digest = card_action_digest(card_id, statement, action)
    if digest is None:
        return None
    return {"v": 2, "card_id": card_id, "action_digest": digest}


def legacy_card_ownership_receipt_v1(
        card_id: str, statement: str, action: dict) -> dict | None:
    """Recompute a frozen historical v1 ownership receipt for replay verification only."""
    digest = legacy_card_action_digest_v1(card_id, statement, action)
    if digest is None:
        return None
    return {"v": 1, "card_id": card_id, "action_digest": digest}


def transitional_card_ownership_receipt_v1(
        card_id: str, statement: str, action: dict) -> dict | None:
    """Verify a short-lived expanded-v1 ownership receipt; new writers must never call this."""
    digest = transitional_card_action_digest_v1(card_id, statement, action)
    if digest is None:
        return None
    return {"v": 1, "card_id": card_id, "action_digest": digest}


def normalized_hypothesis_statement(statement: str) -> str:
    import re
    return re.sub(r"\s+", " ", (statement or "").strip().lower())


def hypothesis_statement_digest(statement: str) -> str:
    """Collision-resistant identity behind the short human-readable hypothesis id."""
    return hashlib.sha256(normalized_hypothesis_statement(statement).encode("utf-8")).hexdigest()


def hypothesis_id(statement: str) -> str:
    """Stable id for a hypothesis statement so the same claim (from different ideas / a human /
    a deep-research direction) links to ONE ledger entry that accumulates evidence. A normalized
    slug + short hash: readable in the log, collision-resistant across paraphrases-of-the-exact-same
    wording (paraphrase *variation* is intentionally a new hypothesis — dedup is by exact intent).

    md5, deliberately, and it stays (doc 25 CO-08): this is a 6-hex DISPLAY suffix that disambiguates
    two slugs, not a security boundary — `hypothesis_statement_digest` above is the sha256 identity —
    and it is a FROZEN key. Every hypothesis ledger entry and every capsule that joined on this id was
    written with it, so a different hash function orphans them all."""
    import re
    norm = normalized_hypothesis_statement(statement)
    slug = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")[:48] or "hypothesis"
    return f"{slug}-{hashlib.md5(norm.encode('utf-8')).hexdigest()[:6]}"


def hypothesis_concept_cache_keys(card) -> tuple[str, ...]:
    """The keys a research card's agentic concept tags may live under in the `hypothesis_concepts`
    cache: its live `id` AND its immutable seed-statement hash. Pre-migration rows were keyed by
    hypothesis_id(statement); a research card WITHOUT an explicit card_id already has that same hash as
    its id, but a migrated NATIVE card (a `card-N` id) does not — so an id-only join silently discards
    the recorded tags and re-tags the same belief on resume. Consumers look up the cache by BOTH keys
    (read-only tolerance; nothing about what is written or folded changes). Deterministic and ordered:
    the live id first (a fresh same-run tag wins), the seed hash second (recovers legacy rows)."""
    keys: list[str] = []
    cid = str(getattr(card, "id", "") or "")
    if cid:
        keys.append(cid)
    seed = (getattr(card, "seed_statement", "") or "").strip()
    if seed:
        seed_key = hypothesis_id(seed)
        if seed_key and seed_key != cid:
            keys.append(seed_key)
    return tuple(keys)


# The duplicate `Hypothesis` class was removed once Card became the canonical research-board model.
# `Card` carries the former hypothesis-facing fields (`seed_statement`, `verdict`, `evidence`, priority,
# best_delta), while `belief_id` keeps research-question identity distinct from work-item identity.
# Frozen `hypothesis_*` events still feed `_derive_cards` for old-log replay.


class CardConceptSource(BaseModel):
    """Exact owner receipt for ``Card.concept_tags``.

    A card may accumulate evidence from several nodes with different concept producers.  One scalar
    provenance label is therefore meaningful only when the displayed tags name the exact proposal/node
    they came from.  ``complete`` distinguishes an honest explicit empty membership from an absent or
    lossy one; ``materialization_receipt`` carries the folded delta/classifier corruption causes.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["card_added", "card_enriched", "node"]
    node_id: Optional[int] = Field(default=None, ge=0)
    node_generation: Optional[int] = Field(default=None, ge=0)
    provenance: Optional[Literal[
        "researcher-authored", "classifier", "operator-edited", "offline-heuristic",
        "untrusted-source",
    ]] = None
    membership_present: bool = False
    complete: bool = False
    receipt_valid: bool = True
    materialization_receipt: Optional[ConceptMaterializationReceipt] = None

    @model_validator(mode="after")
    def _coherent_owner(self) -> "CardConceptSource":
        # a node provenance label without an exact lifecycle owner is forgeable metadata,
        # not a receipt.  Proposal-only sources deliberately carry neither a node id nor trusted producer.
        if self.kind == "node":
            if self.node_id is None or self.node_generation is None:
                raise ValueError("node concept sources require node_id and node_generation")
        elif self.node_id is not None or self.node_generation is not None or self.provenance is not None:
            raise ValueError("proposal concept sources cannot claim node identity or provenance")
        if self.complete and (
                not self.membership_present or not self.receipt_valid
                or self.materialization_receipt is not None
                or (self.kind == "node" and self.provenance is None)):
            raise ValueError("complete concept sources require an exact present membership")
        return self


class CardIdentityProvenance(BaseModel):
    """Bounded proof of where a card work-item identity came from.

    ``native`` is intentionally receipt-based, never inferred from an id's spelling.  Until the
    engine's mint/link lifecycle writes ``card_added.ownership_receipt``, every hash join, unbound
    ``card_added`` row, and node-only ``Idea.card_id`` remains a non-selectable shadow projection.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["native", "legacy_hash", "synthesized_shadow"] = "synthesized_shadow"
    source: Literal[
        "card_added_receipt", "card_added_unbound", "hypothesis_shadow",
        "node_statement_hash", "node_card_id", "merge", "unknown",
    ] = "unknown"
    durable: bool = False
    receipt_valid: bool = False
    action_digest: Optional[str] = None

    @model_validator(mode="after")
    def _coherent_identity(self) -> "CardIdentityProvenance":
        native = self.kind == "native"
        valid_digest = valid_card_action_digest(self.action_digest)
        if native != (
                self.source == "card_added_receipt" and self.durable
                and self.receipt_valid and valid_digest):
            raise ValueError("native card identity requires one valid durable card_added receipt")
        if not native and (self.durable or self.receipt_valid or self.action_digest is not None):
            raise ValueError("shadow card identities cannot claim a durable native receipt")
        return self


CardSelectionBlocker = Literal[
    "identity_not_native", "action_owner_missing", "action_owner_ambiguous",
    "action_receipt_incomplete", "freshness_unknown", "freshness_stale",
    "work_in_flight", "work_terminal", "work_owner_unknown", "card_terminal",
    "merged_work_items",
]


class CardSelectionProvenance(BaseModel):
    """Complete, bounded inputs used to derive ``Card.selection_ready``."""

    model_config = ConfigDict(extra="forbid")

    action_source: Literal["card_added", "node", "mixed", "none"] = "none"
    action_owner_count: int = Field(default=0, ge=0, le=257)
    action_complete: bool = False
    freshness: Literal["current", "stale", "unknown"] = "unknown"
    owner_state: Literal["none", "in_flight", "terminal", "mixed", "unknown"] = "none"

    @model_validator(mode="after")
    def _coherent_selection_source(self) -> "CardSelectionProvenance":
        if (self.action_owner_count == 0) != (self.action_source == "none"):
            raise ValueError("zero action owners require action_source=none")
        if self.action_source == "mixed" and self.action_owner_count < 2:
            raise ValueError("mixed action provenance requires multiple owners")
        if self.action_complete and self.action_owner_count != 1:
            raise ValueError("only one exact action owner can be complete")
        return self


def surviving_work_item_aliases(card) -> list[str]:
    """The ids folded into ``card`` that may still own EXECUTABLE work — the `merged_work_items` set.

    Two very different things arrive through ``Card.aliases`` and this is the one place that tells them
    apart, because the answer decides whether a card can ever be selected and aliases never expire:

      * a WORK ITEM merged in (another native card, or any id some node names as its ``idea.card_id``).
        The merged chain must stay CLOSED — ``_derive_cards`` keys ``own_work_items_by_card`` by the
        CANONICAL id, so an alias's node is in the canonical card's own-work-item set by construction
        and would otherwise launder the debug-anchor exemption on a node this card never authored.
      * a pure research BELIEF merged in by the duplicate-belief consolidation cadence
        (``engine/research_cadence.py::_maybe_merge_hypotheses``). It owns no action, no node and no
        receipt; there is nothing to launder and nothing to be ambiguous about.

    Conflating them is not a theoretical cost. On ``runs/rubertlite-dr-unified-v6`` the consolidator
    folded eight paraphrases of one belief into their canonical at the 20:15 resume; two hours later
    the Researcher minted a native card for that SAME statement, ``_card_identity_map`` bridged the
    belief's hash onto the native id (that bridge is correct — it is one claim), and the eight
    paraphrases arrived as aliases of a work item that had never been touched. Its blockers were
    exactly ``['merged_work_items']``: native identity, one complete action owner, current freshness,
    no work in flight — the queue's only candidate, permanently unselectable, with the second H200
    idle. Consolidating duplicate beliefs must not kill the work item they name.

    So the rule is a SUBTRACTION and it fails closed: an alias blocks unless the fold certified it a
    belief in ``Card.belief_aliases``. A card's own seed-statement hash is likewise not another work
    item — it is this card's belief spelling — and stays exempt exactly as before.

    Pure and total over a Card or any duck-typed row, so the fold's blocker, the model's
    fail-closed validator and the public projection can all state the rule ONCE.
    """
    aliases = getattr(card, "aliases", None) or []
    beliefs = {alias for alias in (getattr(card, "belief_aliases", None) or [])
               if isinstance(alias, str)}
    seed_statement = getattr(card, "seed_statement", "") or ""
    own_belief_id = hypothesis_id(seed_statement) if seed_statement else None
    return [
        alias for alias in aliases
        if alias not in beliefs and (own_belief_id is None or alias != own_belief_id)
    ]


def card_score_fence_state(
    scored_against: Optional[int],
    scored_against_generation: Optional[int],
    scored_against_empty: object,
    *,
    anchor_live: bool,
    anchor_attempt: Optional[int],
    board_empty: bool,
) -> str:
    """The score half of the Card freshness fence: ``current`` | ``stale`` | ``unknown``.

    With an incumbent the fence answers exactly one question — **is the node this proposal was scored
    against still the same experiment it was scored against?** That is a per-ANCHOR liveness
    question, so the caller resolves the anchor and passes the two facts that answer it:

      * ``anchor_live`` — the node exists, is not tombstoned and is not aborted;
      * ``anchor_attempt`` — its CURRENT attempt, which must equal the recorded generation. A reset
        node re-ran; the metric the proposal was scored against no longer exists even though the id
        does, which is why the generation is part of the receipt at all.

    Missing legacy fence data stays ``unknown`` and never becomes selectable; a malformed one is
    ``stale``. Both keep the queue fail closed while old card shadows remain readable.

    **"A better champion appeared" is deliberately NOT part of the incumbent branch** (narrowed
    2026-08-13). Until then it also required ``state.best_node_id == scored_against`` — i.e. a card
    became permanently ``freshness_stale`` the instant ANY unrelated node outscored the champion it
    happened to be proposed under. Nothing ever un-stales a card (the receipt is immutable, and the
    only re-proposal path — ``_drop_card_once(..., reason="reproposed")`` — serves a node RESET, not
    a champion change), so that was a permanent death sentence delivered by an unrelated event.

    Its cost is measured, not theoretical. With one proposer producing one card at a time the
    sequence is: propose against champion C -> some node finishes -> the champion becomes C' -> the
    pending card is stale -> propose against C'. Two fresh selectable cards could therefore never
    coexist, so ``eval_parallel > 1`` had nothing to dispatch no matter what the operator configured.
    On ``runs/rubertlite-dr-unified-v6`` (14 h/eval, two H200s, ``eval_parallel`` settled to 2) the
    board at 03:33 was ``card-3 scored_against=1 status=proposed blockers=['freshness_stale']`` and
    ``card-5 scored_against=2 status=running`` — card-3's parent node 1 was alive, breedable and at
    the exact attempt it was scored on; its ONLY defect was that node 2 had since beaten node 1. It
    never ran, and GPU 1 was idle for the whole run.

    Nothing that clause really protects is lost, because none of it was ever carried HERE:

      * whether the proposal's own parents are executable is ``parent_state`` beside this, plus
        ``_card_action_has_live_anchors`` (fold) / ``_live_card_action`` (selection);
      * whether a MERGE still names the current policy top-2 is rechecked exactly, by metric, in
        ``search/card_selection.py::_live_card_action`` — the champion-equality clause was only ever
        a crude proxy for that, and an unsound one in both directions (the top-2 can change while the
        champion does not, and vice versa);
      * whether a claimed card is still the thing selection would choose NOW is
        ``speculative_card_is_fresh``'s counterfactual SET membership, which re-scores against the
        live board every turn.

    A superseded champion is RANKING information, not a validity verdict: an independent hypothesis
    does not become wrong because some other node scored higher. It stays fully derivable by any
    consumer that wants it (``card.scored_against != state.best_node_id``) without a fold-level
    blocker that no path can clear.

    **The EMPTY branch keeps its board check, and that is a scoped decision, not an oversight.**
    ``board_empty`` is ``state.best_node_id is None``, so requiring it is literally the same
    champion-equality clause for the case where the recorded champion was "none", and by the argument
    above it should go too: an action formed with no incumbent anchors nothing, so nothing about it
    can die. It stays for now because (a) the only window it can cost anything in is bootstrap —
    a draft staged before the FIRST evaluation lands, which the forced seed prefix builds long before
    that on any real cadence — and (b) removing it makes an empty-authority Card whose build head
    closed ``skipped="stale"`` survive and be RE-ELECTED, and the paired-run calibration receipt
    protocol cannot express that: ``search/speculation_quality.py::_validate_calibration_card_owners``
    requires the build-request ledger to map one-to-one onto the ``card_added`` registrations, in
    order, so a second request for the same card is refused outright. That is a change to a receipt
    protocol whose revision costs six GPU runs, and it belongs to that module's owner rather than to
    a fix for an idle second GPU.

    Pure and total, so the fold's tri-state and the selection-time recheck state the rule ONCE. The
    caller owns the state lookups because the two live on opposite sides of the layer boundary
    (``events`` may not import ``search``).
    """
    if scored_against is None:
        if scored_against_empty is not True:
            # A legacy row that simply never carried the fence. Visible, never selectable.
            return "unknown"
        # Modern complete EMPTY authority: the action was formed with no incumbent at all. A receipt
        # that claims empty authority AND an anchor generation is malformed, never merely empty.
        return (
            "current"
            if scored_against_generation is None and board_empty
            else "stale"
        )
    if not anchor_live:
        return "stale"
    if scored_against_generation is None:
        return "unknown"
    if (type(scored_against_generation) is not int
            or type(anchor_attempt) is not int
            or scored_against_generation != anchor_attempt):
        return "stale"
    return "current"


# Belief-vs-work-item identity (peer review): a Card is a WORK ITEM, but two cards that reuse the exact
# hypothesis wording are ONE belief. The Researcher proposal feed (roles._state_brief) and foresight
# ranking now consume `open_research_beliefs()` — `open_research_cards()` collapsed by seed-statement
# digest, the representative work-item id preserved for evidence joins — so the model no longer re-reads
# or re-ranks same-seed duplicates. `events/belief_projection.py::grouped_beliefs(st)` is the
# additive FULL-board belief view (evidence + verdict aggregated across a belief's cards), which lives
# with the other derived views rather than on the model (doc 25 CO-11).
# Both of those sites used to re-derive the belief key inline; they now read `Card.belief_id`, the one
# spelling the fold publishes (see the field's own comment below for why the belief facet needs an
# identity separate from the work item's, and what it cost when it had none).
class Card(BaseModel):
    """One stable-identity proposal/work item in the target Card queue (docs/23).

    Card is the canonical work-item/evidence row and carries the former Hypothesis-facing fields —
    ``seed_statement`` (the old statement/hash join key), ``verdict`` (the old status), ``evidence``,
    ``priority``, and ``best_delta``. It is not itself a unique belief: multiple work items can share
    one ``belief_id`` (for example a debug retry). The projection also
    materializes legacy/hash/synthesized Card shadows so old logs retain a useful board, but those rows
    are advisory and never selection-ready. Only a unique receipt-bound ``card_added`` establishes
    native work-item identity. Current Card selection consumes ``selection_ready`` and never infers
    executability from the compatibility ``actionable`` flag. The seed/action receipt is immutable;
    display text, priority, configured resources, and lifecycle are explicit replay-derived overlays.
    """
    # Fold-only loss fact for the bounded enrichment projection. A private attribute is deliberate:
    # it reaches the public completeness receipt without creating a new Card DTO/RunState field.
    _card_enrichment_complete: bool = PrivateAttr(default=True)

    id: str                                             # native engine-minted `card-{k}` or legacy statement hash
    statement: str                                      # the DISPLAY statement (operator-editable in L6)
    # Exact durable event that owns the current operator display edit. Public clients use this receipt
    # to acknowledge an edit even when secret redaction makes the displayed text non-prefix-equivalent.
    statement_edit_seq: Optional[int] = Field(default=None, ge=0)
    # The IMMUTABLE seed statement captured at card_added — the stable statement-hash JOIN key, held
    # separate from `statement` so an operator paraphrase (L6 card_edited) overlays DISPLAY only and never
    # un-links the card's hash-joined evidence. Defaults to `statement` for derived/legacy cards.
    seed_statement: str = ""
    source: str = "researcher"          # researcher | operator | engine | foresight | novelty | freshness
    rationale: str = ""
    created_at_node: int = 0
    # Lifecycle lane (DERIVED; frozen UI-contract vocabulary, kept OPEN so Layer 5 can add
    # speculating/built-awaiting-commit without a model rework): proposed (no node yet) | building
    # (node_building in flight) | running (pending eval) | evaluated (>=1 terminal) | gated (only
    # trust-gated / breed-excluded evidence) | dropped (drop/merge event). `coded` (pending node with
    # code) is a RESERVED lane the fold does NOT currently produce — every pending Node collapses
    # directly to `running` (see `_derive_cards`). NOT for want of a boundary any more: the log now
    # carries `events/types.py::EV_NODE_EVAL_STARTED`, folded to `Node.eval_started`. It is stamped
    # only on SPECULATIVE attempt-zero lifecycles (the ones whose budget refund needs it), and
    # `_derive_cards` reads none of it, so the lane stays unreachable for a different reason: the
    # projection, not the evidence. Deriving it means splitting the pending branch there.
    status: str = "proposed"
    # Research verdict (DERIVED via the shared `_evidence_verdict` helper — byte-identical to the
    # hash-joined hypothesis): open | testing | supported | tested | abandoned.
    verdict: str = "open"
    # Layer-1c compatibility flag for board filtering only: False for dropped/gated/abandoned, True for
    # proposed/building/running/evaluated. It deliberately does NOT imply one fresh executable action exists.
    actionable: bool = True
    # `actionable` is a compatibility/advisory board flag, never proof that a card is one executable
    # work item. Only the receipt-backed, fail-closed seam below is consumed by the active Card queue.
    # `selection_ready` stays False for every legacy/hash/synthesized runtime card.
    identity: CardIdentityProvenance = Field(default_factory=CardIdentityProvenance)
    selection_provenance: CardSelectionProvenance = Field(default_factory=CardSelectionProvenance)
    selection_blockers: list[CardSelectionBlocker] = Field(
        default_factory=lambda: ["identity_not_native"], max_length=16)
    selection_ready: bool = False
    evidence: list[int] = Field(default_factory=list)   # node ids that tested it (== node_ids)
    best_delta: Optional[float] = None                  # best improvement-over-parent among evidence (audit)
    # --- The RESEARCH-DIRECTION facet's own identity (DERIVED; `events/card_ledger.py`).
    # `id` is the WORK-ITEM identity and `identity.action_digest` binds the executable action; neither
    # can say "these two work items ask the same question". Until these two fields existed the work-item
    # identity was doing double duty, so a debug RETRY of a failed card — which reuses the parent's Idea
    # verbatim and only flips `operator` (`engine/orchestrator.py::_prepare_node_idea`) — minted a second
    # card with a different action digest and the board rendered ONE hypothesis TWICE. Measured live in
    # `runs/rubert-dr-0807`: card-0 (draft) and card-1 (debug) byte-identical in statement, rationale, all
    # six params and footprint, differing only in `idea.operator`.
    #
    # THAT WAS ONLY THE VIEW, and the operator said so after it happened AGAIN, identically, in
    # `runs/rubertlite-dr-unified-v5` (card-0 / card-3). Naming the duplicate does not stop it. The
    # engine now ATTACHES a retry to the card it retries instead of minting a twin —
    # `engine/card_reservation.py::_retry_attach_card`, the `attach` disposition — which is what the
    # card/node split was always for: one card, several nodes. These two fields keep their jobs
    # unchanged (a retry that legitimately re-scopes its statement still mints, and a pre-fix log
    # still folds to the same board), so the paragraph below stands as written.
    #
    # Both are DERIVED overlays exactly like `verdict`/`status`/`selection_ready`: no event carries them,
    # no digest covers them, and no receipt is minted from them. That is the whole point of putting the
    # belief facet HERE instead of widening the action digest — the digest must keep binding the
    # executable identity EXACTLY (a debug build genuinely is a different executable action), so the
    # research-direction facet needs an identity of its own rather than a share of that one.
    #
    # `belief_id` is the seed-statement digest — the SAME key `events/belief_projection.py::grouped_beliefs`
    # and `RunState.open_research_beliefs()` group on, published once here so those two sites and any
    # consumer read ONE spelling instead of three hand-synced re-derivations of `hypothesis_statement_digest`.
    # It is the FULL sha256, never the short display `hypothesis_id`: two distinct statements can share a
    # short id, and keying on that would silently merge unrelated beliefs (test_short_hash_collision_*).
    # None only for a card with no seed statement (a malformed `card_added`), which is never groupable.
    belief_id: Optional[str] = None
    # The work item this card is a RETRY of: for a `debug` card, the card that owned the failed node it
    # repairs. Derived by resolving the durable parent NODE anchor back to that node's own `idea.card_id`
    # (canonicalized through merges) — a link the payload has always carried and nothing consumed at the
    # CARD level: `parent_id`/`parent_ids`/`parent_generations` are read only as NODE anchors, by
    # `_card_action_has_live_anchors`, `_card_action_freshness` and `_build_parent_snapshot`.
    # This is strictly MORE than "same wording": it distinguishes a genuine retry of one executable
    # question from two different actions that merely share a formulaic statement (measured: the toy
    # adapter's "random seed point" names three DIFFERENT param points in `runs/spec-live-0804`).
    # The immediate edge only, never a walked chain — node parents are always older than their children,
    # so the edges form a DAG, and publishing one hop keeps it that way for every consumer.
    retry_of: Optional[str] = None
    # Identity / lineage.
    merged_into: Optional[str] = None                   # canonical id if this card was merged away
    aliases: list[str] = Field(default_factory=list)    # ids folded INTO this canonical card
    # The subset of `aliases` the FOLD proved owns no executable work — a pure research BELIEF that was
    # consolidated into this card, never a work item. It is a CERTIFICATE, not a second audit list:
    # `surviving_work_item_aliases` subtracts it, so an alias that is not certified keeps blocking. See
    # that function for why the two kinds must be told apart and what conflating them cost.
    belief_aliases: list[str] = Field(default_factory=list)
    dropped_reason: Optional[str] = None
    dropped_by: Optional[str] = None                    # operator | engine | freshness | novelty
    # Prospective parent anchor — the Layer-5 freshness gate re-derives improve/merge legality for a
    # not-yet-built card against state.best()/rank_by_metric[:2]/breedable_nodes().
    parent_id: Optional[int] = None
    parent_ids: list[int] = Field(default_factory=list)
    # Exact lifecycle attempts captured with the action. ``None`` is a legacy/missing fence; an
    # explicit empty mapping is the complete modern snapshot for a no-parent action.
    parent_generations: Optional[dict[str, int]] = None
    # Staleness fence: the best_node_id / event seq the card was scored against (Layer-5 freshness gate).
    scored_against: Optional[int] = None
    scored_against_generation: Optional[int] = Field(default=None, ge=0)
    # Distinguishes a modern action formed with no incumbent from a legacy missing score fence.
    scored_against_empty: bool = False
    # The idea block (what to run) — populated from the linked node's Idea in Layer 1a.
    operator: Optional[str] = None                      # draft | improve | merge | debug
    params: dict[str, float] = Field(default_factory=dict)
    space: dict[str, list[float]] = Field(default_factory=dict)
    eval_profile: Optional[str] = None
    eval_timeout: Optional[float] = Field(default=None, gt=0)
    concept_tags: list[str] = Field(default_factory=list)
    # exact, additive ownership receipt for concept_tags.  Without it a merged card could
    # show the union/override from several evidence nodes beside one misleading scalar provenance tier.
    concept_source: Optional[CardConceptSource] = None
    # Board prioritization (foresight) — stamped from card_ranked in Layer 1b; audit/UI only.
    priority: Optional[int] = None
    # True only when the final operator-priority overlay owns the priority; foresight ranks stay false.
    pinned: bool = False
    foresight_rank: Optional[int] = None
    confidence: Optional[float] = None
    # --- Layer 1b enrichment (RESERVED; populated in 1b — defaults keep Layer 1a a pure hypotheses shadow).
    # Ref-shaped ONLY (docs/23 decision 23): no verbatim source/captured-output on the card.
    footprint: Optional[dict] = None                    # immutable receipt-owned declaration + developer provenance
    # Layer-6 operator resource override. Kept separate from ``footprint`` because the latter is part
    # of the native Card action digest. Scheduling/freshness consume ``effective_card_footprint``;
    # replay/public UI retain this independent provenance-bearing pin for audit.
    resource_pin: Optional[dict] = None                 # {gpus?, gpu_mem_mib?, pinned_by="operator"}
    novelty_verdict: Optional[dict] = None              # {grade, level, near_node, recommendation}
    cross_run_prior: Optional[dict] = None              # {matched_concepts, prior_run_ids/outcomes} (refs)
    research_origin: Optional[str] = None               # memo id ref
    lesson_refs: list[str] = Field(default_factory=list)
    claim_refs: list[str] = Field(default_factory=list)
    steering_context: list[dict] = Field(default_factory=list)  # compact STRUCTURED cues (no verbatim capture)
    # Compatibility scalar for existing clients. Derived only from concept_source.provenance for an exact
    # node owner; proposal-only sources keep it None, and card_enriched cannot assign it independently.
    provenance_tier: Optional[str] = None

    @field_validator("parent_generations", mode="before")
    @classmethod
    def _bounded_parent_generations(cls, value):
        if value is None:
            return None
        if not isinstance(value, dict) or len(value) > 64:
            raise ValueError("parent_generations must be a bounded mapping")
        out: dict[str, int] = {}
        for key, generation in value.items():
            if (not isinstance(key, str) or len(key) > 10
                    or not key.isascii() or not key.isdecimal()
                    or key != str(int(key)) or type(generation) is not int
                    or not 0 <= generation <= (1 << 31) - 1):
                raise ValueError("parent_generations must contain canonical bounded attempts")
            out[key] = generation
        return dict(sorted(out.items()))

    @model_validator(mode="after")
    def _selection_readiness_is_fail_closed(self) -> "Card":
        if not self.selection_ready:
            return self
        provenance = self.selection_provenance
        if not (
            self.identity.kind == "native"
            and self.identity.durable
            and self.identity.receipt_valid
            and provenance.action_source == "card_added"
            and provenance.action_owner_count == 1
            and provenance.action_complete
            and provenance.freshness == "current"
            and provenance.owner_state == "none"
            and not self.selection_blockers
            and self.status == "proposed"
            and self.verdict == "open"
            and not self.evidence
            and not surviving_work_item_aliases(self)
            and self.dropped_reason is None
            and self.merged_into is None
        ):
            raise ValueError("selection_ready requires one fresh, native, unowned work item")
        return self

    @model_validator(mode="after")
    def _belief_certificate_names_only_folded_ids(self) -> "Card":
        # `belief_aliases` is what `surviving_work_item_aliases` SUBTRACTS, so a row certifying an id
        # it never absorbed could exempt an alias that is not there yet but will be. Coherence is
        # checkable without the log even though belief-ness is not, so check the half that is.
        if not set(self.belief_aliases) <= set(self.aliases):
            raise ValueError("belief_aliases must name ids folded into this card")
        return self
