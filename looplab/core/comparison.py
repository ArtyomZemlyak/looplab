"""Explicit comparability contracts for cross-run measurements.

Two scalar metrics are comparable only when the run author recorded the same dataset/candidate
population, evaluator, metric semantics, aggregation, and uncertainty protocol.  A matching task id,
metric label, or optimisation direction is not enough.  This module deliberately refuses to invent a
contract for legacy runs; callers may still display their measurements as unranked observations.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from looplab.trust.redact import is_secret_key_name, redact_persisted_text


COMPARISON_CONTRACT_SCHEMA = 1
_REQUIRED_TEXT_FIELDS = (
    "dataset_lineage",
    "split_or_candidate_pool_lineage",
    "evaluator_uid",
    "evaluator_version",
    "population",
    "filter",
    "metric_uid",
    "unit",
    "aggregation",
    "cutoff",
    "measurement_phase",
    "uncertainty_protocol",
    "constraints_digest",
)
_MAX_FIELD_CHARS = 512


class ComparisonContract(BaseModel):
    """Typed, fail-closed scientific identity persisted with every opted-in task.

    ``measurement_phase`` is deliberately a small enum.  Adding a phase requires adding an
    authoritative state-field mapping in :func:`comparison_measurement`; accepting arbitrary prose
    here would let a report silently rank one phase using another phase's metric.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: StrictInt = Field(
        default=COMPARISON_CONTRACT_SCHEMA, alias="schema", serialization_alias="schema")
    dataset_lineage: StrictStr = Field(min_length=1, max_length=_MAX_FIELD_CHARS)
    split_or_candidate_pool_lineage: StrictStr = Field(min_length=1, max_length=_MAX_FIELD_CHARS)
    evaluator_uid: StrictStr = Field(min_length=1, max_length=_MAX_FIELD_CHARS)
    evaluator_version: StrictStr = Field(min_length=1, max_length=_MAX_FIELD_CHARS)
    population: StrictStr = Field(min_length=1, max_length=_MAX_FIELD_CHARS)
    filter: StrictStr = Field(min_length=1, max_length=_MAX_FIELD_CHARS)
    metric_uid: StrictStr = Field(min_length=1, max_length=_MAX_FIELD_CHARS)
    unit: StrictStr = Field(min_length=1, max_length=_MAX_FIELD_CHARS)
    direction: Literal["min", "max"]
    aggregation: StrictStr = Field(min_length=1, max_length=_MAX_FIELD_CHARS)
    cutoff: StrictStr = Field(min_length=1, max_length=_MAX_FIELD_CHARS)
    measurement_phase: Literal["search", "confirmed", "holdout"]
    uncertainty_protocol: StrictStr = Field(min_length=1, max_length=_MAX_FIELD_CHARS)
    constraints_digest: StrictStr = Field(min_length=1, max_length=_MAX_FIELD_CHARS)
    baseline: StrictStr | None = Field(default=None, min_length=1, max_length=_MAX_FIELD_CHARS)
    contract_id: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema_is_exact_integer(cls, value: object) -> object:
        if type(value) is not int or value != COMPARISON_CONTRACT_SCHEMA:
            raise ValueError(f"schema must be the integer {COMPARISON_CONTRACT_SCHEMA}")
        return value

    @field_validator(*_REQUIRED_TEXT_FIELDS, "baseline")
    @classmethod
    def _text_is_safe_and_exact(cls, value: object) -> object:
        if value is None:
            return value
        if not isinstance(value, str):
            raise ValueError("comparison contract text must be a string")
        clean = redact_persisted_text(
            value, max_chars=_MAX_FIELD_CHARS, entropy=True, single_line=True)
        if not clean or clean != value:
            raise ValueError("comparison contract text must be bounded, secret-free, and single-line")
        return value

    @model_validator(mode="after")
    def _bind_contract_id(self) -> "ComparisonContract":
        semantic = self.model_dump(
            by_alias=True, exclude={"contract_id"}, exclude_none=True)
        encoded = json.dumps(
            semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        expected = hashlib.sha256(encoded).hexdigest()
        if self.contract_id is not None and self.contract_id != expected:
            raise ValueError("contract_id does not match the canonical comparison contract")
        self.contract_id = expected
        return self


def canonical_comparison_contract(value: object) -> dict | None:
    """Return a bounded canonical contract, or ``None`` when comparability is unproven.

    The digest is computed from the canonical semantic fields, not from display-redacted prose.  To
    keep it safe to persist and send to a model, contracts containing credential-like field names,
    controls, truncation, or oversized values are rejected rather than lossy-normalised.
    """
    if isinstance(value, ComparisonContract):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if not isinstance(value, Mapping):
        return None
    if any(not isinstance(key, str) or is_secret_key_name(key) for key in value):
        return None
    try:
        contract = ComparisonContract.model_validate(dict(value))
    except (ValidationError, TypeError, ValueError):
        return None
    return contract.model_dump(mode="json", by_alias=True, exclude_none=True)


def finite_measurement(value: object) -> float | int | None:
    """Keep bools, NaN, infinities, and opaque numeric-like objects out of comparisons."""
    if type(value) not in {int, float}:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def comparison_measurement(contract_value: object, best: object) -> dict | None:
    """Project the contract's phase from the authoritative champion fields.

    No phase falls back to another phase.  In particular, a requested confirmation or holdout
    measurement with missing/corrupt evidence is unavailable, never silently relabelled search
    evidence.  The returned object is the bounded receipt later copied into a scope report.
    """
    contract = canonical_comparison_contract(contract_value)
    if contract is None or best is None:
        return None
    phase = contract["measurement_phase"]
    source_by_phase = {
        "search": "best.metric",
        "confirmed": "best.confirmed_mean",
        "holdout": "best.holdout_metric",
    }
    attribute_by_phase = {
        "search": "metric",
        "confirmed": "confirmed_mean",
        "holdout": "holdout_metric",
    }
    value = finite_measurement(getattr(best, attribute_by_phase[phase], None))
    if value is None:
        return None
    uncertainty: dict[str, object] = {"protocol": contract["uncertainty_protocol"]}
    if phase == "confirmed":
        std = finite_measurement(getattr(best, "confirmed_std", None))
        seeds = getattr(best, "confirmed_seeds", None)
        if std is None or std < 0 or type(seeds) is not int or seeds <= 0:
            return None
        uncertainty.update({
            "std": std,
            "std_source": "best.confirmed_std",
            "seeds": seeds,
            "seeds_source": "best.confirmed_seeds",
        })
    # CODEX AGENT: phase, value source, and uncertainty evidence travel together.  Consumers must
    # never reconstruct this receipt from a generic `best_metric`, which would erase phase semantics.
    return {
        "value": value,
        "phase": phase,
        "source": source_by_phase[phase],
        "uncertainty": uncertainty,
    }
