"""Public availability/privacy contract for the derived hypothesis-card board."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.serve.public_cards import (  # noqa: E402
    INTERNAL_CARD_STATE_FIELDS, PUBLIC_CARD_MAX_BYTES, PUBLIC_CARD_MAX_COUNT,
    PUBLIC_CARDS_MAX_BYTES, public_cards)
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
        "footprint": {"gpus": 2, "private_note": huge},
        "novelty_verdict": {
            "grade": "ALLOW", "level": 4, "near_generation": 3,
            "recommendation": "run", "private_note": huge,
        },
        "cross_run_prior": {
            "v": 2,
            "matched_concepts": ["model/tree"],
            "prior_runs": [{
                "run_id": "prior-1", "run_best_metric": 0.8,
                "outcomes": {"model/tree": 0.7},
                "source_receipt": {"concepts_total": 1, "concepts_complete": True,
                                   "private_note": huge},
                "private_note": huge,
            }],
            "prior_runs_total": 2,
            "prior_runs_omitted": 1,
            "prior_runs_complete": False,
            "concept_source": {
                "source_complete": False, "partial_capsules": 1, "private_note": huge,
            },
            "private_note": huge,
        },
        "private_note": huge,
    })
    store.append("card_ranked", {
        "order": ["card-safe"], "reason": "bounded ranking", "private_note": huge,
    })
    store.append("run_finished", {"reason": "budget"})
    return rd


def _assert_public_card_payload(payload: dict) -> None:
    state = payload["state"]
    assert not (INTERNAL_CARD_STATE_FIELDS & state.keys())
    assert set(state["cards"]) == {"card-safe"}
    card = state["cards"]["card-safe"]
    assert card["statement"] == "a public direction"
    assert card["operator"] == "improve" and card["params"] == {"lr": 0.2}
    assert card["footprint"] == {"gpus": 2}
    assert card["novelty_verdict"] == {
        "grade": "ALLOW", "level": 4, "near_generation": 3, "recommendation": "run",
    }
    assert card["steering_context"] == [{
        "kind": "coverage", "reason": "sk-***", "ref": "axis/model",
    }]
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

    stream = client.get("/api/runs/demo/events", headers=_OWNER)
    assert stream.status_code == 200
    state_frame = next(frame for frame in stream.text.split("\n\n") if "event: state" in frame)
    sse_payload = json.loads(next(
        line[6:] for line in state_frame.splitlines() if line.startswith("data: ")))
    _assert_public_card_payload(sse_payload)
    assert sse_payload["state"]["cards"] == owner_payload["state"]["cards"]

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
        "priority": 0, "scored_against": 999,
    })
    first = public_cards(small_rows)
    second = public_cards(dict(reversed(list(small_rows.items()))))
    first_raw = json.dumps(first, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    second_raw = json.dumps(second, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    assert first_raw == second_raw
    assert len(first) == PUBLIC_CARD_MAX_COUNT
    assert list(first) == ["card-000", *(f"card-{index:03d}" for index in range(45, 300))]
    assert first["card-000"]["id"] == "card-000"

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
    bounded_raw = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    reversed_raw = json.dumps(
        reversed_bounded, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    assert bounded_raw == reversed_raw
    assert len(bounded_raw.encode("utf-8")) <= PUBLIC_CARDS_MAX_BYTES
    assert _PRIVATE_MARKER not in bounded_raw and _SECRET not in bounded_raw
    assert "private_note" not in bounded_raw and "spoof-" not in bounded_raw
    assert "x" * 257 not in bounded and "line\nbreak" not in bounded and _SECRET not in bounded
    assert all(len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
               <= PUBLIC_CARD_MAX_BYTES for card in bounded.values())
    sample = next(iter(bounded.values()))
    assert "parent_id" not in sample and "priority" not in sample
    assert sample["evidence"] == [1, 3]
