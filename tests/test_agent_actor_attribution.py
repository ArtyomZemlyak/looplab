"""The scoped harness credential is the EXTERNAL AGENT, and the record says so.

Codex audit 2026-10-14 L-09 (and doc 70 70.8b): questions filed, card statements edited and
comments written with `LOOPLAB_HARNESS_TOKEN` were stamped exactly like the owner's — `source:
"operator"` on the card rows and `actor_kind: "deployment_owner"` on comments — so the Research view
read "filed by the operator" over 24 of Codex's own filings and the comment thread showed the agent's
note as the owner's. The intake now stamps the principal (`_ControlIntake.actor_kind`); the fold
carries it to `Card.filed_by_actor` / `statement_edit_actor` and the comment's `actor_kind`; rows
written before the change carry no actor and fold exactly as before.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from looplab.core.models import Event, Idea  # noqa: E402
from looplab.events.comment_projection import ACTOR_LABELS, project_comments  # noqa: E402
from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.events.replay import fold  # noqa: E402
from looplab.events.types import (  # noqa: E402
    EV_CARD_DROPPED, EV_CARD_EDITED, EV_CARD_FILED, EV_CARD_REOPENED, EV_CARD_REPRIORITIZED,
    EV_COMMENT_CREATED, EVENT_PAYLOAD_KEYS)
from looplab.serve.control_validation import normalize_control  # noqa: E402
from looplab.serve.public_cards import public_cards  # noqa: E402


class _Srv:
    owner_auth_enabled = True

    def state(self, rd: Path):
        return fold(EventStore(rd / "events.jsonl").read_all())

    def events(self, rd: Path):
        return EventStore(rd / "events.jsonl").read_all()


def _external_run(tmp_path: Path) -> Path:
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "config.snapshot.json").write_text('{"external_harness": true, "backend": "toy"}')
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    store.append("hypothesis_added", {"statement": "does x help?", "source": "external_research"})
    idea = Idea(operator="draft", hypothesis="a concrete experiment")
    store.append("node_created", {"node_id": 0, "generation": 0, "operator": "draft",
                                  "parent_ids": [], "idea": idea.model_dump(mode="json")})
    return rd


def _ids(rd: Path):
    st = fold(EventStore(rd / "events.jsonl").read_all())
    question = next(cid for cid, c in st.cards.items() if c.card_kind == "direction")
    experiment = next(cid for cid, c in st.cards.items() if c.card_kind == "experiment")
    return question, experiment


def test_the_agent_token_is_stamped_as_the_external_agent_on_every_card_control(tmp_path):
    rd = _external_run(tmp_path)
    question, experiment = _ids(rd)
    srv = _Srv()
    rows = [
        (EV_CARD_FILED, {"id": experiment, "parent_card_id": question}),
        (EV_CARD_EDITED, {"id": question, "statement": "does x help on cold users?"}),
        (EV_CARD_REPRIORITIZED, {"id": experiment, "priority": 3}),
        (EV_CARD_DROPPED, {"id": experiment, "reason": "superseded"}),
    ]
    for event_type, payload in rows:
        agent = normalize_control(srv, rd, event_type, dict(payload), agent_token=True)
        owner = normalize_control(srv, rd, event_type, dict(payload))
        assert agent["actor_kind"] == "harness_agent", event_type
        assert owner["actor_kind"] == "deployment_owner", event_type
        # The control-plane marker the fold keys on is unchanged: still "not the engine's".
        assert {k: v for k, v in agent.items() if k != "actor_kind"} == {
            k: v for k, v in owner.items() if k != "actor_kind"}
        # The payload contract the log is checked against declares the new key.
        assert "actor_kind" in EVENT_PAYLOAD_KEYS[event_type].optional
    assert "actor_kind" in EVENT_PAYLOAD_KEYS[EV_CARD_REOPENED].optional


def test_a_client_cannot_forge_the_actor(tmp_path):
    rd = _external_run(tmp_path)
    question, experiment = _ids(rd)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        normalize_control(_Srv(), rd, EV_CARD_FILED, {
            "id": experiment, "parent_card_id": question, "actor_kind": "deployment_owner"},
            agent_token=True)
    assert exc.value.status_code == 400


def test_the_board_names_the_agent_and_legacy_rows_name_nobody(tmp_path):
    rd = _external_run(tmp_path)
    question, experiment = _ids(rd)
    store = EventStore(rd / "events.jsonl")
    legacy = fold(store.read_all() + [Event(
        seq=99, ts=0.0, type=EV_CARD_FILED,
        data={"id": experiment, "parent_card_id": question, "source": "operator"})])
    assert legacy.cards[experiment].filed_by == "operator"
    assert legacy.cards[experiment].filed_by_actor is None, "an old row must not be guessed at"

    store.append(EV_CARD_FILED, normalize_control(
        _Srv(), rd, EV_CARD_FILED, {"id": experiment, "parent_card_id": question},
        agent_token=True))
    store.append(EV_CARD_EDITED, normalize_control(
        _Srv(), rd, EV_CARD_EDITED, {"id": question, "statement": "does x help on cold users?"},
        agent_token=True))
    st = fold(store.read_all())
    card = st.cards[experiment]
    assert card.parent_card_id == question and card.filed_by == "operator"
    assert card.filed_by_actor == "harness_agent"
    assert st.cards[question].statement_edit_actor == "harness_agent"
    wire = public_cards(st.cards)
    assert wire[experiment]["filed_by_actor"] == "harness_agent"
    assert wire[question]["statement_edit_actor"] == "harness_agent"


def test_replay_ignores_an_unknown_actor_value(tmp_path):
    rd = _external_run(tmp_path)
    question, experiment = _ids(rd)
    st = fold(EventStore(rd / "events.jsonl").read_all() + [Event(
        seq=99, ts=0.0, type=EV_CARD_FILED,
        data={"id": experiment, "parent_card_id": question, "source": "operator",
              "actor_kind": ["not", "a", "kind"]})])
    assert st.cards[experiment].parent_card_id == question
    assert st.cards[experiment].filed_by_actor is None


def test_an_agent_comment_reads_as_the_external_agent(tmp_path):
    rd = _external_run(tmp_path)
    store = EventStore(rd / "events.jsonl")
    st = fold(store.read_all())
    generation = st.nodes[0].attempt
    payload = normalize_control(_Srv(), rd, EV_COMMENT_CREATED, {
        "node_id": 0, "node_generation": generation, "text": "checked the stage log"},
        agent_token=True)
    assert payload["actor_kind"] == "harness_agent"
    store.append(EV_COMMENT_CREATED, payload)
    comments, _rows = project_comments(store.read_all())
    (comment,) = comments.values()
    assert comment.actor_kind == "harness_agent"
    assert ACTOR_LABELS[comment.actor_kind] == "External agent"


def test_the_harness_manifest_does_not_promise_capsules_from_authored_tags():
    """UI review 2026-10-14 §4: the manifest said concept capsules are projected "from authored
    evidence", while `engine/lessons.py` admits only classifier-provenance concepts ("authored ...
    labels never cross this wall") — an external agent tagged every node expecting cross-run
    knowledge that could not arrive."""
    import json
    from looplab.harness.manifest import harness_manifest
    text = json.dumps(harness_manifest())
    assert "concept capsules are still deterministic run-end projections" not in text
    assert "classifier-tagged" in text
