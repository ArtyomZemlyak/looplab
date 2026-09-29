"""`serve/control_validation.py::external_intent_refusal` — which control intents an EXTERNALLY
driven run refuses, stated as a truth table (critic 2026-09-29).

The restriction used to be a branch buried behind a hand-kept list of the intents it covered, read
before the rule ran: a refusal added to the rule for an intent missing from the list would never
fire. The rule is now asked FIRST and the run's snapshot read only for an intent it would refuse,
so there is no list to drift — and every `CONTROL_EVENTS` member is driven through it here, over
the payloads that reach its branches, against the set the harness manifest publishes.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException  # noqa: E402

from looplab.core.config import Settings  # noqa: E402
from looplab.events.types import (  # noqa: E402
    EV_DEEP_RESEARCH, EV_FORCE_ABLATE, EV_FORK, EV_INJECT_NODE, EV_NODE_RESET)
from looplab.serve.control_validation import (  # noqa: E402
    _external_mode_restriction, external_intent_refusal)
from looplab.serve.protocol import CONTROL_EVENTS  # noqa: E402

# Payloads that reach every branch the rule has: the empty intent, each reset-stage spelling, a
# ready-made inject in each of its shapes, and one that names a source node.
PROBES = [
    {}, {"from_stage": "propose"}, {"from_stage": "implement"}, {"from_stage": " implement "},
    {"from_stage": "eval"}, {"from_stage": "score"}, {"code": "print(1)"},
    {"files": {"a.py": "x"}}, {"deleted": ["a.py"]}, {"source_run": "prior", "source_node": 0},
]


def test_the_intents_an_external_run_refuses_are_the_published_five():
    """`harness/manifest.py` publishes fork, forced ablation, deep research, propose/implement
    resets and a code-less inject as refused in external mode; nothing else may be."""
    refused = {event for event in CONTROL_EVENTS
               if any(external_intent_refusal(event, dict(data)) is not None for data in PROBES)}
    assert refused == {EV_FORK, EV_FORCE_ABLATE, EV_DEEP_RESEARCH, EV_NODE_RESET, EV_INJECT_NODE}


@pytest.mark.parametrize("stage, refused", [
    ("propose", True), ("implement", True), (" implement ", True), ("eval", False),
    ("score", False), ("train", False), ("Implement", False), (None, False), (3, False),
])
def test_a_reset_is_refused_only_where_the_engine_would_re_develop(stage, refused):
    """The manifest's pair, compared as the normalizer stores it (stripped; case kept, because the
    fold's own match is exact). A pipeline-stage rescore is the engine's evaluation and is admitted
    — it was refused before, against the manifest. A non-string stage is the normalizer's 400."""
    data = {"node_id": 0} if stage is None else {"node_id": 0, "from_stage": stage}
    got = external_intent_refusal(EV_NODE_RESET, data)
    assert (got is not None) is refused
    if refused:
        assert got.status_code == 409


def test_an_inject_must_carry_a_ready_made_candidate():
    assert external_intent_refusal(
        EV_INJECT_NODE, {"idea": {"operator": "draft"}}).status_code == 400
    for shape in ({"code": "print(1)"}, {"files": {"a.py": "x"}}, {"deleted": ["a.py"]},
                  {"source_run": "prior", "source_node": 0}):
        assert external_intent_refusal(EV_INJECT_NODE, shape) is None, shape
    # a source run naming no node imports nothing: not ready-made
    assert external_intent_refusal(EV_INJECT_NODE, {"source_run": "prior"}) is not None


def _snapshot(rd, *, external: bool) -> None:
    (rd / "config.snapshot.json").write_text(json.dumps(
        Settings(backend="toy", external_harness=external).model_dump(mode="json")))


def test_the_snapshot_decides_whether_the_rule_applies(tmp_path):
    """The same reset from `implement` is admitted on an internal run (its engine re-develops it)
    and refused on an external one; a run with no snapshot is the internal case."""
    data = {"node_id": 0, "from_stage": "implement"}
    _external_mode_restriction(tmp_path, EV_NODE_RESET, dict(data))   # no snapshot
    _snapshot(tmp_path, external=False)
    _external_mode_restriction(tmp_path, EV_NODE_RESET, dict(data))
    _snapshot(tmp_path, external=True)
    with pytest.raises(HTTPException) as caught:
        _external_mode_restriction(tmp_path, EV_NODE_RESET, dict(data))
    assert caught.value.status_code == 409


def test_an_unreadable_snapshot_refuses_only_what_the_rule_would(tmp_path):
    """The run mode is unknowable, so an intent the rule WOULD refuse is refused with the coded
    answer (never the parse error); an intent it admits in every mode — a reset from `eval` —
    reads no snapshot and passes. MUTATION: read the snapshot before asking the rule -> the
    eval reset is refused over a file it does not need."""
    (tmp_path / "config.snapshot.json").write_bytes(b"{not json")
    _external_mode_restriction(tmp_path, EV_NODE_RESET, {"node_id": 0, "from_stage": "eval"})
    with pytest.raises(HTTPException) as caught:
        _external_mode_restriction(tmp_path, EV_FORK, {"from_node_id": 0})
    assert caught.value.status_code == 503
    assert caught.value.detail["code"] == "config_snapshot_unreadable"
    assert "Expecting" not in json.dumps(caught.value.detail)
