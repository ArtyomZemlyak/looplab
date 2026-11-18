"""Non-secret external-harness receipts, separate from LoopLab's own LLM billing ledger.

Provider observations are client-adapter observations of a provider response, NOT server
attestation of the bill. An agent summary never acquires that provenance by naming a provider.
All records are delta segments; cumulative summaries are refused to prevent silent double counting.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping

from looplab.core.jsonutil import canonical_json_digest
from looplab.core.redact import redact_persisted_text

_HASH = re.compile(r"[0-9a-f]{64}")
_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+@=-]{0,127}")
REVISION_FIELDS = frozenset({"provider", "model", "cli_version", "endpoint_sha256",
    "generation_parameters_sha256", "prompt_sha256", "skills_sha256", "tools_sha256",
    "authority_scopes_sha256", "resource_budget_sha256", "language"})
AUTHOR_FIELDS = frozenset({"receipt_id", "node_id", "attempt", "revision", "usage", "wall_seconds"})
SERVER_FIELDS = frozenset({"version", "run_uid", "run_generation", "revision_id"})
USAGE_FIELDS = frozenset({"source", "source_id", "coverage", "input_tokens", "output_tokens",
                         "total_tokens", "cost", "currency"})


def _identifier(value):
    if not isinstance(value, str) or _IDENT.fullmatch(value) is None or "://" in value:
        raise ValueError("invalid_external_telemetry_identifier")
    if redact_persisted_text(value, max_chars=128, entropy=False, single_line=True) != value:
        raise ValueError("secret_like_external_identifier")
    return value


def _number(value, *, integer=False):
    if value is None:
        return None
    if type(value) not in ((int,) if integer else (int, float)) or not 0 <= value <= 10**15 or not math.isfinite(value):
        raise ValueError("invalid_external_telemetry_number")
    return value


def normalize_revision(value):
    if not isinstance(value, dict) or set(value) != REVISION_FIELDS:
        raise ValueError("invalid_harness_revision_fields")
    result = {}
    for key, item in value.items():
        if key.endswith("_sha256"):
            if item is not None and (not isinstance(item, str) or _HASH.fullmatch(item) is None):
                raise ValueError("invalid_harness_fingerprint")
            result[key] = item
        else:
            result[key] = None if item is None else _identifier(item)
    return result


def normalize_authored(value):
    if not isinstance(value, dict) or set(value) != AUTHOR_FIELDS:
        raise ValueError("invalid_external_telemetry_fields")
    revision = normalize_revision(value["revision"])
    for key in ("node_id", "attempt"):
        if type(value[key]) is not int or not 0 <= value[key] <= 10**9:
            raise ValueError("invalid_external_telemetry_target")
    usage = value["usage"]
    if not isinstance(usage, dict) or set(usage) != USAGE_FIELDS:
        raise ValueError("invalid_external_usage_fields")
    if usage["source"] not in ("agent_summary", "provider_response") or usage["coverage"] != "delta":
        raise ValueError("external_usage_requires_delta_source")
    normalized = {"source": usage["source"], "source_id": _identifier(usage["source_id"]), "coverage": "delta"}
    for key in ("input_tokens", "output_tokens", "total_tokens", "cost"):
        normalized[key] = _number(usage[key], integer=key.endswith("tokens"))
    a, b, total = (normalized[key] for key in ("input_tokens", "output_tokens", "total_tokens"))
    if all(x is not None for x in (a, b, total)) and a + b != total:
        raise ValueError("inconsistent_provider_token_counts")
    currency = usage["currency"]
    if (currency is None) != (normalized["cost"] is None) or (currency is not None and
            (not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None)):
        raise ValueError("external_cost_requires_currency")
    normalized["currency"] = currency
    if usage["source"] == "provider_response" and (revision["provider"] is None or revision["model"] is None):
        raise ValueError("provider_observation_requires_identity")
    out = {"receipt_id": _identifier(value["receipt_id"]), "node_id": value["node_id"],
           "attempt": value["attempt"], "revision": revision, "usage": normalized,
           "wall_seconds": _number(value["wall_seconds"])}
    if len(json.dumps(out, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()) > 4096:
        raise ValueError("external_telemetry_exceeds_budget")
    return out


def revision_id(revision):
    return canonical_json_digest(normalize_revision(revision))


def observation_key(record):
    """One provider request / agent delta segment cannot be counted again under a new receipt ID."""
    revision, usage = record["revision"], record["usage"]
    return canonical_json_digest({"provider": revision["provider"], "model": revision["model"],
        "endpoint_sha256": revision["endpoint_sha256"], "source": usage["source"], "source_id": usage["source_id"]})


def provider_observation(response, *, revision, receipt_id, node_id, attempt, wall_seconds=None):
    """Extract ONLY usage/id/model from a provider response. Never visit choices/output/reasoning.

    Call this at the provider client boundary, retain the returned sanitized receipt, then submit
    it through agent_checkpointed. No endpoint, prompt, provider body or output is persisted.
    Pricing helpers/estimates and SDK hidden fields are deliberately not read.
    """
    def get(obj, key):
        return obj.get(key) if isinstance(obj, Mapping) else getattr(obj, key, None)
    model = get(response, "model")
    if model != revision["model"]:
        raise ValueError("provider_model_differs_from_revision")
    raw = get(response, "usage")
    if raw is None:
        raise ValueError("provider_usage_unavailable")
    def count(primary, alternate):
        value = get(raw, primary)
        return get(raw, alternate) if value is None else value
    usage = {"source": "provider_response", "source_id": get(response, "id"), "coverage": "delta",
             "input_tokens": count("input_tokens", "prompt_tokens"),
             "output_tokens": count("output_tokens", "completion_tokens"),
             "total_tokens": get(raw, "total_tokens"), "cost": get(raw, "cost"),
             "currency": get(raw, "currency")}
    return normalize_authored({"receipt_id": receipt_id, "node_id": node_id, "attempt": attempt,
        "revision": revision, "usage": usage, "wall_seconds": wall_seconds})
