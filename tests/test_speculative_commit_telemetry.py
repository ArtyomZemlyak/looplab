"""A speculative Card commit publishes its build's own pick, never a pooled researcher's ranking.

Review 2026-09-22, SCJ-02. `speculation.py::_create_precoded_node` called `_emit_hypothesis_ranked`
and both halves of `_emit_foresight_selected` on the pooled pair the Layer-5 producer leases. Those
reads were dead on every real pair: the BUILD producer implements a Card an earlier proposal minted
and never proposes (it clears the pair's telemetry before it starts), and the pooled researcher
carries no panel that could rank (the exclusion `_producer_role_pair` states). Dead, and not
harmless: the leased pair is shared with the RAW-stage producer, so a value found on its researcher
belongs to ANOTHER proposal, and publishing it stamps that proposal's ranking onto this node.

The Developer half is live and kept: best-of-N's pick on the pooled Developer is this build's own.

DRIVEN through the real Card lane (the harness `tests/test_card_speculation_engine.py` owns): a ready
draft is elected, built on the producer's leased pair and committed by `_serve_card_builds`, with a
foreign ranking planted on the leased researcher in the window before the commit.
"""
from __future__ import annotations

from looplab.events.types import (EV_CARD_RANKED, EV_FORESIGHT_SELECTED, EV_HYPOTHESIS_RANKED,
                                  EV_NODE_CREATED)
# The receipt fixture is AUTOUSE in its own module and stays autouse when imported here: these tests
# admit speculation through the production boundary and then replace only the roles.
from tests.test_card_speculation_engine import (  # noqa: F401  (imported for its autouse effect)
    _add_ready_draft,
    _admit_unit_speculation_receipt,
    _build_result,
    _engine,
    _request,
    _start,
)


def test_a_speculative_commit_publishes_the_developer_pick_and_no_researcher_ranking(tmp_path):
    """MUTATION: restore `self._emit_hypothesis_ranked(node_id, 0, researcher=researcher)` (or the
    researcher half of `_emit_foresight_selected`) in `_create_precoded_node` -> the planted foreign
    ranking lands on this node as `hypothesis_ranked` / a second `foresight_selected`."""
    engine, _producer = _engine(tmp_path / "run")
    _start(engine)
    engine._ensure_speculation_state()
    _add_ready_draft(engine)
    result = _build_result(engine, _request(engine))
    assert result.success is True
    researcher, developer = result.roles
    assert (researcher, developer) == engine._producer_role_pair()

    # What a raw-stage proposal on the SAME leased pair would leave behind — not this build's.
    researcher.last_hyp_priority = {"order": ["card-7"], "confidence": 0.9,
                                    "reason": "another proposal's board order"}
    researcher.last_foresight = {"kind": "idea", "chosen": 0, "reason": "another proposal's pick"}
    # …and this build's own best-of-N pick on its Developer.
    developer.last_foresight_pick = {"kind": "code", "chosen": 1, "reason": "this build's pick"}

    engine._spec_builds[result.key] = result
    assert engine._serve_card_builds() is True

    events = engine.store.read_all()
    created = [e.data["node_id"] for e in events if e.type == EV_NODE_CREATED]
    assert created, "the control never committed"
    assert [e.type for e in events if e.type in (EV_HYPOTHESIS_RANKED, EV_CARD_RANKED)] == []
    picks = [e.data for e in events if e.type == EV_FORESIGHT_SELECTED]
    assert [(p["node_id"], p["reason"]) for p in picks] == [(created[-1], "this build's pick")]
    # Both channels are CONSUMED on the role objects all the same, so nothing leaks to the next node.
    assert developer.last_foresight_pick is None
    assert researcher.last_hyp_priority is None and researcher.last_foresight is None
