"""Read external checkpoint receipts on one native event prefix; never amend billing totals."""
from __future__ import annotations

from looplab.core.external_telemetry import (AUTHOR_FIELDS, SERVER_FIELDS, normalize_authored,
                                           observation_key, revision_id)
from looplab.core.jsonutil import canonical_json_digest


def _subtotal(records, key):
    values = [r["wall_seconds"] if key == "wall_seconds" else r["usage"][key] for r in records]
    known = [v for v in values if v is not None]
    return {"known_sum": sum(known) if known else None, "known_count": len(known),
            "unknown_count": len(values) - len(known), "complete": bool(values) and len(known) == len(values)}


def project(events, state, generation, *, offset=0, limit=20, include_items=False):
    records, digests, conflicts, malformed, identity_keys = {}, {}, set(), 0, {}
    for event in events:
        if event.type != "agent_checkpointed" or "external_telemetry" not in event.data:
            continue
        raw = event.data["external_telemetry"]
        try:
            if not isinstance(raw, dict) or set(raw) != AUTHOR_FIELDS | SERVER_FIELDS:
                raise ValueError("invalid_record")
            value = normalize_authored({k: raw[k] for k in AUTHOR_FIELDS})
            if (type(raw["version"]) is not int or raw["version"] != 1 or raw["run_uid"] != state.run_uid
                    or raw["run_generation"] != generation or raw["revision_id"] != revision_id(value["revision"])):
                raise ValueError("invalid_record_identity")
            node = state.nodes.get(value["node_id"])
            if node is None or value["attempt"] > node.attempt:
                raise ValueError("invalid_record_target")
            key = observation_key(value)
            digest = canonical_json_digest({k: v for k, v in value.items() if k != "receipt_id"})
            # Conflicting receipt IDs AND repeated provider request IDs are unavailable evidence,
            # not a reason to choose the first/last number. Exact duplicates never add another cost.
            keys = ("receipt:" + value["receipt_id"], "observation:" + key)
            for identity in keys:
                identity_keys.setdefault(identity, set()).add(key)
                if identity in digests and digests[identity] != digest:
                    conflicts.add(identity)
                digests[identity] = digest
            records.setdefault(key, {**raw, "event_seq": event.seq, "recorded_at": event.ts,
                "observation_level": "provider_observed" if value["usage"]["source"] == "provider_response" else "agent_declared",
                "observer": "external_client_adapter" if value["usage"]["source"] == "provider_response" else "external_agent",
                "provider_attestation": "not_independently_attested",
                "revision_complete": all(item is not None for item in value["revision"].values())})
        except (ValueError, TypeError, KeyError, UnicodeError):
            malformed += 1
    conflicted_keys = set().union(*(identity_keys[k] for k in conflicts)) if conflicts else set()
    usable = [row for key, row in records.items() if key not in conflicted_keys]
    buckets = []
    for level in ("provider_observed", "agent_declared"):
        rows = [r for r in usable if r["observation_level"] == level]
        currencies = sorted({r["usage"]["currency"] for r in rows if r["usage"]["currency"] is not None})
        buckets.append({"observation_level": level, "receipt_count": len(rows),
            "tokens": {k: _subtotal(rows, k) for k in ("input_tokens", "output_tokens", "total_tokens")},
            "cost_by_currency": [{"currency": c, **_subtotal([r for r in rows if r["usage"]["currency"] == c], "cost")} for c in currencies[:16]],
            "omitted_currency_count": max(0, len(currencies) - 16),
            "cost_unknown_count": sum(r["usage"]["cost"] is None for r in rows)})
    native_keys = ("cost", "calls", "priced_calls", "prompt_tokens", "completion_tokens", "total_tokens", "tokens", "cached_tokens")
    native_totals = ({key: state.llm_cost[key] for key in native_keys if key in state.llm_cost
                      and type(state.llm_cost[key]) in (int, float)} if state.llm_cost is not None else None)
    native = {"llm": {"source": "existing_native_llm_ledger", "totals": native_totals, "currency": None},
              "evaluation_wall_seconds": {"source": "existing_native_eval_ledger", "value": state.total_eval_seconds},
              "gpu_hours": {"value": None, "coverage": "not_derived_from_wall_seconds"}}
    result = {"version": 1, "run_uid": state.run_uid, "generation": generation,
        "event_seq": events[-1].seq if events else None,
        "state": "unavailable" if malformed or conflicts else ("recorded" if records else "not_recorded"),
        "record_count": len(records), "usable_record_count": len(usable), "malformed_count": malformed,
        "conflict_count": len(conflicts), "source_records_complete": not malformed and not conflicts, "llm": buckets, "agent_reported_wall_seconds": _subtotal(usable, "wall_seconds"),
        "native": native, "combined_total": None,
        "meaning": "Independent external usage is separate from the native LLM ledger. Provider observations are supplied by a client adapter, not independently attested billing. Delta segments only; missing is unknown. Reported wall segments may overlap and are not elapsed time. GPU hours are not inferred from wall time. No combined monetary/GPU/wall total exists."}
    if include_items:
        rows = sorted(usable, key=lambda row: row["event_seq"], reverse=True)
        result["items"] = [{**row, "outcome": _outcome(state, row)} for row in rows[offset:offset + limit]]
        result["offset"], result["limit"] = offset, limit
        result["has_more"] = offset + limit < len(rows)
        result["next_offset"] = offset + len(result["items"])
        result["prefix_digest"] = canonical_json_digest(rows + [{"conflicts": sorted(conflicts), "malformed": malformed}])
    return result


def _outcome(state, row):
    node = state.nodes.get(row["node_id"])
    if node is None or node.attempt != row["attempt"]:
        return {"state": "historical_attempt", "node_id": row["node_id"], "attempt": row["attempt"],
                "status": None, "metric": None, "meaning": "Current node outcome is withheld after reset; inspect this exact historical lifecycle."}
    return {"state": "current_attempt", "node_id": node.id, "attempt": node.attempt,
            "status": node.status, "metric": node.metric if node.status == "evaluated" else None,
            "meaning": "Recorded native outcome linked by attempt; no causal attribution to the harness is inferred."}
