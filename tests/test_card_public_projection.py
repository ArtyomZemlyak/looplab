"""Public availability/privacy contract for the derived hypothesis-card board."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.serve.public_cards import (  # noqa: E402
    INTERNAL_CARD_STATE_FIELDS, PUBLIC_CARD_MAX_BYTES, PUBLIC_CARD_MAX_COUNT,
    PUBLIC_CARDS_MAX_BYTES, PUBLIC_CARDS_PROJECTION_MAX_BYTES, public_cards,
    public_cards_projection)
from looplab.serve.server import make_app  # noqa: E402


_OWNER = {"X-LoopLab-Token": "owner-secret"}
_SECRET = "sk-abcdefghijklmnopqrstuvwxyz1234567890"
_PRIVATE_MARKER = "PRIVATE-CARD-NOTE-MUST-NOT-SHIP"


def _seed_run(root):
    rd = root / "demo"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {
        "run_id": "demo", "task_id": "cards", "goal": "review cards", "direction": "max",
    })
    huge = _PRIVATE_MARKER + ("x" * 200_000)
    store.append("card_added", {
        "id": "card-safe", "statement": "a public direction", "source": "engine",
        "rationale": f"candidate note {_SECRET}",
        "idea": {"operator": "improve", "params": {"lr": 0.2}},
        # Body-shaped producer data must never ride the shared /state/SSE/review Card DTO. This is the
        # Card-side analogue of AppState's heavy node trim, even when such keys reach the event journal.
        "code": _PRIVATE_MARKER, "files": {"private.py": _PRIVATE_MARKER},
        "stdout": _PRIVATE_MARKER, "stderr": _PRIVATE_MARKER,
        "steering_context": [{
            "kind": "coverage", "ref": "axis/model", "reason": _SECRET,
            "private_note": huge,
        }],
        "private_note": huge,
    })
    store.append("card_merged", {
        "canonical": "card-safe", "aliases": ["card-alias"], "private_note": huge,
    })
    store.append("card_dropped", {
        "id": "unknown-card", "reason": "unused", "private_note": huge,
    })
    store.append("card_enriched", {
        "id": "card-safe",
        # Enrichment subdocuments are closed atomic snapshots: privacy/adversarial siblings belong at
        # the event envelope (covered below), never inside an otherwise valid footprint/prior receipt.
        "footprint": {"gpus": 2},
        "novelty_verdict": {
            "grade": "ALLOW", "level": 4, "near_generation": 3,
            "recommendation": "run",
        },
        "cross_run_prior": {
            "v": 2,
            "matched_concepts": ["model/tree"],
            "prior_runs": [{
                "run_id": "prior-1", "run_best_metric": 0.8,
                "outcomes": {"model/tree": 0.7},
                "source_receipt": {"concepts_total": 1, "concepts_complete": True},
            }],
            "prior_runs_total": 2,
            "prior_runs_omitted": 1,
            "prior_runs_complete": False,
            "concept_source": {"source_complete": False, "partial_capsules": 1},
        },
        "private_note": huge,
    })
    store.append("card_ranked", {
        "order": ["card-safe"], "reason": "bounded ranking", "private_note": huge,
    })
    store.append("card_resource_pinned", {
        "id": "card-safe", "gpus": 1, "gpu_mem_mib": 4_000,
        "source": "operator", "pinned": True, "private_note": huge,
    })
    store.append("run_finished", {"reason": "budget"})
    return rd


def _assert_public_card_payload(payload: dict) -> None:
    state = payload["state"]
    assert not (INTERNAL_CARD_STATE_FIELDS & state.keys())
    assert set(state["cards"]) == {"card-safe"}
    projection = state["cards_projection"]
    assert projection["source_valid"] is True
    assert (projection["total"], projection["returned"], projection["omitted"]) == (1, 1, 0)
    # Secret redaction is intentionally lossy and therefore fails the end-to-end receipt closed.
    assert projection["complete"] is False
    card_projection = projection["items"]["card-safe"]
    assert card_projection["complete"] is False
    # The closed replay boundary rejects the whole secret-bearing steering snapshot before it reaches
    # folded Card state. This outer receipt partitions that folded source (not the raw event journal),
    # so only the retained-and-redacted rationale is an omission at this boundary.
    assert {"rationale"} <= card_projection["omissions"].keys()
    assert "steering_context" not in card_projection["omissions"]
    card = state["cards"]["card-safe"]
    assert card["statement"] == "a public direction"
    assert card["operator"] == "improve" and card["params"] == {"lr": 0.2}
    assert card["footprint"] == {"gpus": 2}
    assert card["resource_pin"] == {
        "gpus": 1, "gpu_mem_mib": 4_000, "pinned_by": "operator",
    }
    assert card["novelty_verdict"] == {
        "grade": "ALLOW", "level": 4, "near_generation": 3, "recommendation": "run",
    }
    assert card["steering_context"] == []
    prior = card["cross_run_prior"]
    assert prior["v"] == 2
    assert prior["prior_runs_total"] == 2
    assert prior["prior_runs_omitted"] == 1
    assert prior["prior_runs_complete"] is False
    assert prior["concept_source"] == {"partial_capsules": 1, "source_complete": False}
    assert prior["prior_runs"][0]["source_receipt"] == {
        "concepts_complete": True, "concepts_total": 1,
    }
    encoded = json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(encoded) <= PUBLIC_CARD_MAX_BYTES
    assert {"code", "files", "stdout", "stderr", "stdout_tail", "raw", "preview"}.isdisjoint(card)
    raw = json.dumps(payload, ensure_ascii=False)
    assert "private_note" not in raw
    assert _PRIVATE_MARKER not in raw
    assert _SECRET not in raw


def test_owner_state_sse_and_review_share_the_bounded_card_projection(tmp_path, monkeypatch):
    _seed_run(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    client = TestClient(make_app(tmp_path))

    owner = client.get("/api/runs/demo/state", headers=_OWNER)
    assert owner.status_code == 200
    owner_payload = owner.json()
    _assert_public_card_payload(owner_payload)

    historical = client.get("/api/runs/demo/state?seq=0", headers=_OWNER)
    assert historical.status_code == 200
    assert historical.json()["state"]["cards"] == {}
    assert historical.json()["state"]["cards_projection"] == {
        "source_valid": True,
        "total": 0,
        "returned": 0,
        "omitted": 0,
        "complete": True,
        "items": {},
    }

    stream = client.get("/api/runs/demo/events", headers=_OWNER)
    assert stream.status_code == 200
    state_frame = next(frame for frame in stream.text.split("\n\n") if "event: state" in frame)
    sse_payload = json.loads(next(
        line[6:] for line in state_frame.splitlines() if line.startswith("data: ")))
    _assert_public_card_payload(sse_payload)
    assert sse_payload["state"]["cards"] == owner_payload["state"]["cards"]
    assert sse_payload["state"]["cards_projection"] == owner_payload["state"]["cards_projection"]

    created = client.post(
        "/api/runs/demo/reviews", headers=_OWNER,
        json={"ttl_seconds": 3600, "include_evidence": False},
    )
    assert created.status_code == 200
    review = client.get(
        "/api/review/state", headers={"X-LoopLab-Review": created.json()["token"]})
    assert review.status_code == 200
    review_payload = review.json()
    _assert_public_card_payload(review_payload)
    assert review_payload["state"]["cards"] == owner_payload["state"]["cards"]
    assert review_payload["state"]["cards_projection"] == owner_payload["state"]["cards_projection"]


def test_public_cards_are_count_size_total_and_deterministic():
    huge = ("z" * 200_000) + _PRIVATE_MARKER
    small_rows = {
        f"card-{index:03d}": {
            "id": f"spoof-{index}", "status": "evaluated", "verdict": "tested",
            "actionable": False, "statement": f"direction {index}", "created_at_node": index,
        }
        for index in reversed(range(PUBLIC_CARD_MAX_COUNT + 44))
    }
    small_rows["card-000"].update({
        "status": "running", "verdict": "open", "actionable": True,
        "priority": 0, "pinned": True, "scored_against": 999,
    })
    first = public_cards(small_rows)
    second = public_cards(dict(reversed(list(small_rows.items()))))
    first_envelope = public_cards_projection(small_rows).model_dump(mode="json")
    second_envelope = public_cards_projection(
        dict(reversed(list(small_rows.items())))).model_dump(mode="json")
    first_raw = json.dumps(first, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    second_raw = json.dumps(second, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    assert first_raw == second_raw
    assert first_envelope == second_envelope
    assert first_envelope["cards"] == first
    assert len(first) == PUBLIC_CARD_MAX_COUNT
    assert list(first) == ["card-000", *(f"card-{index:03d}" for index in range(45, 300))]
    assert first["card-000"]["id"] == "card-000"
    assert first["card-000"]["pinned"] is True
    first_projection = first_envelope["cards_projection"]
    assert (first_projection["total"], first_projection["returned"],
            first_projection["omitted"]) == (300, 256, 44)
    assert first_projection["complete"] is False
    assert set(first_projection["items"]) == set(first)
    assert all(item["complete"] for item in first_projection["items"].values())

    hostile_rows = {
        f"card-{index:03d}": {
            "id": f"spoof-{index}",
            "status": "evaluated",
            "verdict": "tested",
            "actionable": False,
            "statement": huge,
            "seed_statement": huge,
            "rationale": f"prose {_SECRET} {huge}",
            "confidence": float("nan"),
            "best_delta": float("inf"),
            "parent_id": 1.9,
            "priority": 2.7,
            "evidence": [1, 2.5, 3],
            "params": {"ok": 1.0, "bad": object()},
            "space": {"depth": [1, float("nan"), 3, object()]},
            "steering_context": [{
                "kind": "coverage", "private_note": huge, "reason": _SECRET,
                "value": {"private_note": huge},
            }],
            "cross_run_prior": {
                "v": 2,
                "matched_concepts": ["model/tree"],
                "prior_runs": [{"run_id": "r", "metric": float("inf"),
                                "private_note": huge}],
                "concept_source": {"source_complete": True, "private_note": huge},
                "private_note": huge,
            },
            "private_note": huge,
        }
        for index in reversed(range(PUBLIC_CARD_MAX_COUNT + 44))
    }
    hostile_rows.update({
        "x" * 257: {"status": "running", "actionable": True, "statement": "oversized id"},
        "line\nbreak": {"status": "running", "actionable": True, "statement": "control id"},
        _SECRET: {"status": "running", "actionable": True, "statement": "secret id"},
    })
    hostile_rows["card-000"]["status"] = {"malformed": True}
    hostile_rows["card-000"]["actionable"] = {"malformed": True}

    bounded = public_cards(hostile_rows)
    reversed_bounded = public_cards(dict(reversed(list(hostile_rows.items()))))
    hostile_envelope = public_cards_projection(hostile_rows).model_dump(mode="json")
    reversed_hostile_envelope = public_cards_projection(
        dict(reversed(list(hostile_rows.items())))).model_dump(mode="json")
    bounded_raw = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    reversed_raw = json.dumps(
        reversed_bounded, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    assert bounded_raw == reversed_raw
    assert hostile_envelope == reversed_hostile_envelope
    assert hostile_envelope["cards"] == bounded
    assert len(bounded_raw.encode("utf-8")) <= PUBLIC_CARDS_MAX_BYTES
    metadata_raw = json.dumps(
        hostile_envelope["cards_projection"], ensure_ascii=False, separators=(",", ":"))
    assert len(metadata_raw.encode("utf-8")) <= PUBLIC_CARDS_PROJECTION_MAX_BYTES
    assert _PRIVATE_MARKER not in bounded_raw and _SECRET not in bounded_raw
    assert "private_note" not in bounded_raw and "spoof-" not in bounded_raw
    assert "x" * 257 not in bounded and "line\nbreak" not in bounded and _SECRET not in bounded
    assert all(len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
               <= PUBLIC_CARD_MAX_BYTES for card in bounded.values())
    sample = next(iter(bounded.values()))
    assert "parent_id" not in sample and "priority" not in sample
    assert sample["evidence"] == [1, 3]
    metadata = hostile_envelope["cards_projection"]
    assert metadata["total"] == len(hostile_rows)
    assert metadata["returned"] == len(bounded)
    assert metadata["omitted"] == len(hostile_rows) - len(bounded)
    assert metadata["complete"] is False
    assert set(metadata["items"]) == set(bounded)
    for receipt in metadata["items"].values():
        fields = receipt["fields"]
        assert fields["total"] == fields["returned"] + fields["omitted"]
        for omission in receipt["omissions"].values():
            assert omission["total"] == omission["returned"] + omission["omitted"]


def test_per_card_receipts_count_bounded_text_lists_action_and_concepts_exactly():
    rows = {
        "card-bounded": {
            "status": "proposed",
            "verdict": "open",
            "actionable": True,
            "statement": "direction",
            "operator": "improve",
            "params": {f"p-{index:02d}": float(index) for index in range(40)},
            "space": {"depth": [float(index) for index in range(40)]},
            "evidence": list(range(40)),
            "concept_tags": [f"axis/tag-{index:02d}" for index in range(40)],
            "steering_context": [{"kind": "coverage", "at_node": index}
                                 for index in range(40)],
        },
        "card-text": {
            "status": "evaluated",
            "verdict": "tested",
            "actionable": False,
            "statement": "x" * 10_000,
        },
    }
    envelope = public_cards_projection(rows).model_dump(mode="json")
    metadata = envelope["cards_projection"]

    assert (metadata["total"], metadata["returned"], metadata["omitted"]) == (2, 2, 0)
    assert metadata["complete"] is False

    bounded = metadata["items"]["card-bounded"]
    assert bounded["omissions"]["evidence"] == {
        "unit": "items", "total": 40, "returned": 32, "omitted": 8, "complete": False,
    }
    assert bounded["omissions"]["concept_tags"] == {
        "unit": "items", "total": 40, "returned": 32, "omitted": 8, "complete": False,
    }
    assert bounded["omissions"]["params"] == {
        "unit": "entries", "total": 40, "returned": 32, "omitted": 8, "complete": False,
    }
    assert bounded["omissions"]["space"] == {
        "unit": "entries", "total": 1, "returned": 0, "omitted": 1, "complete": False,
    }
    assert bounded["omissions"]["steering_context"] == {
        "unit": "items", "total": 40, "returned": 32, "omitted": 8, "complete": False,
    }
    assert bounded["omissions"]["action"] == {
        "unit": "fields", "total": 3, "returned": 1, "omitted": 2, "complete": False,
    }
    assert bounded["omissions"]["concepts"] == {
        "unit": "fields", "total": 1, "returned": 0, "omitted": 1, "complete": False,
    }

    text = metadata["items"]["card-text"]
    assert text["omissions"]["statement"] == {
        "unit": "characters", "total": 10_000, "returned": 2_048,
        "omitted": 7_952, "complete": False,
    }
    assert len(envelope["cards"]["card-text"]["statement"].encode("utf-8")) == 2_048


def test_matched_concept_outcome_receipt_uses_its_own_closed_field_vocabulary():
    exact_outcome = {
        "concept": "model/tree",
        "outcome_retained": True,
        "outcome": 0.7,
    }
    exact = public_cards_projection({
        "card-exact": {
            "cross_run_prior": {
                "v": 2,
                "matched_concepts": ["model/tree"],
                "prior_runs": [{
                    "run_id": "prior-1",
                    "matched_concept_outcomes": [exact_outcome],
                }],
                "prior_runs_total": 1,
                "prior_runs_omitted": 0,
                "prior_runs_complete": True,
            },
        },
    }).model_dump(mode="json")

    assert exact["cards"]["card-exact"]["cross_run_prior"]["prior_runs"][0][
        "matched_concept_outcomes"] == [exact_outcome]
    assert exact["cards_projection"]["complete"] is True
    assert exact["cards_projection"]["items"]["card-exact"]["complete"] is True

    future_field = dict(exact_outcome, future_private_detail="not-public")
    lossy = public_cards_projection({
        "card-lossy": {
            "cross_run_prior": {
                "v": 2,
                "matched_concepts": ["model/tree"],
                "prior_runs": [{
                    "run_id": "prior-1",
                    "matched_concept_outcomes": [future_field],
                }],
                "prior_runs_total": 1,
                "prior_runs_omitted": 0,
                "prior_runs_complete": True,
            },
        },
    }).model_dump(mode="json")

    assert "future_private_detail" not in json.dumps(lossy["cards"])
    lossy_receipt = lossy["cards_projection"]["items"]["card-lossy"]
    assert lossy_receipt["complete"] is False
    assert "cross_run_prior" in lossy_receipt["omissions"]


@pytest.mark.parametrize(("field", "exact_value"), [
    ("footprint", {"gpus": 1}),
    ("resource_pin", {"gpus": 1, "pinned_by": "operator"}),
    ("novelty_verdict", {"grade": "ALLOW", "level": 4}),
    ("concept_source", {
        "kind": "card_added",
        "membership_present": True,
        "complete": True,
        "receipt_valid": True,
    }),
    ("steering_context", [{"kind": "coverage", "at_node": 1}]),
])
def test_nested_card_field_unknown_key_is_omitted_and_never_receipted_as_exact(
        field, exact_value):
    exact = public_cards_projection({
        "card-exact": {"concept_tags": [], field: exact_value},
    }).model_dump(mode="json")
    assert exact["cards_projection"]["items"]["card-exact"]["complete"] is True

    if isinstance(exact_value, list):
        lossy_value = [{**exact_value[0], "future_private_detail": "not-public"}]
    else:
        lossy_value = {**exact_value, "future_private_detail": "not-public"}
    lossy = public_cards_projection({
        "card-lossy": {"concept_tags": [], field: lossy_value},
    }).model_dump(mode="json")

    assert "future_private_detail" not in json.dumps(lossy["cards"])
    receipt = lossy["cards_projection"]["items"]["card-lossy"]
    assert receipt["complete"] is False
    assert receipt["omissions"][field]["omitted"] >= 1


@pytest.mark.parametrize("unknown_location", [
    "cross_run",
    "prior_run",
    "source_receipt",
    "cross_run_concept_source",
])
def test_cross_run_nested_unknown_key_is_omitted_and_never_receipted_as_exact(
        unknown_location):
    prior = {
        "run_id": "prior-1",
        "matched_concept_outcomes": [{
            "concept": "model/tree",
            "outcome_retained": True,
            "outcome": 0.7,
        }],
        "source_receipt": {"concepts_total": 1, "concepts_complete": True},
    }
    cross_run = {
        "v": 2,
        "matched_concepts": ["model/tree"],
        "prior_runs": [prior],
        "prior_runs_total": 1,
        "prior_runs_omitted": 0,
        "prior_runs_complete": True,
        "concept_source": {"source_complete": True},
    }
    exact = public_cards_projection({
        "card-exact": {"cross_run_prior": cross_run},
    }).model_dump(mode="json")
    assert exact["cards_projection"]["items"]["card-exact"]["complete"] is True

    if unknown_location == "cross_run":
        cross_run["future_private_detail"] = "not-public"
    elif unknown_location == "prior_run":
        prior["future_private_detail"] = "not-public"
    elif unknown_location == "source_receipt":
        prior["source_receipt"]["future_private_detail"] = "not-public"
    else:
        cross_run["concept_source"]["future_private_detail"] = "not-public"
    lossy = public_cards_projection({
        "card-lossy": {"cross_run_prior": cross_run},
    }).model_dump(mode="json")

    assert "future_private_detail" not in json.dumps(lossy["cards"])
    receipt = lossy["cards_projection"]["items"]["card-lossy"]
    assert receipt["complete"] is False
    assert receipt["omissions"]["cross_run_prior"]["omitted"] >= 1


def test_cross_run_default_insertion_is_not_receipted_as_exact_source_data():
    envelope = public_cards_projection({
        "card-lossy": {"cross_run_prior": {"v": 2}},
    }).model_dump(mode="json")

    assert envelope["cards"]["card-lossy"]["cross_run_prior"] == {
        "v": 2,
        "matched_concepts": [],
        "prior_runs": [],
    }
    receipt = envelope["cards_projection"]["items"]["card-lossy"]
    assert receipt["complete"] is False
    assert receipt["omissions"]["cross_run_prior"]["omitted"] >= 1


def test_projection_fails_closed_for_a_malformed_collection_source():
    envelope = public_cards_projection(None).model_dump(mode="json")
    assert envelope["cards"] == {}
    assert envelope["cards_projection"] == {
        "source_valid": False,
        "total": 0,
        "returned": 0,
        "omitted": 0,
        "complete": False,
        "items": {},
    }
    assert public_cards(None) == {}


def test_invalid_unicode_is_omitted_and_receipted_without_breaking_the_wire_json():
    envelope = public_cards_projection({
        "card-unicode": {
            "status": "proposed",
            "actionable": True,
            "statement": "valid-prefix\ud800invalid",
            "concept_tags": ["safe", "bad\ud800ref"],
        },
    }).model_dump(mode="json")

    card = envelope["cards"]["card-unicode"]
    assert "statement" not in card
    assert card["concept_tags"] == ["safe"]
    receipt = envelope["cards_projection"]["items"]["card-unicode"]
    assert receipt["complete"] is False
    assert receipt["omissions"]["statement"]["returned"] == 0
    assert receipt["omissions"]["concept_tags"] == {
        "unit": "items", "total": 2, "returned": 1, "omitted": 1, "complete": False,
    }
    json.dumps(envelope, ensure_ascii=False).encode("utf-8")
