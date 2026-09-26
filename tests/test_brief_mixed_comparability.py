"""The proposal brief says when the leaders were not all measured on one ruler (doc 68 68.1a).

`Settings.brief_mixed_comparability`, OFF by default. A run whose search nodes were scored on
`smoke` and whose endgame nodes on `full` (or whose scorer or source tree moved mid-run) carries
`mixed_comparability` on its champion on every operator surface; the Researcher, handed the leaders
every turn, read them as one ranking. Driven through the engine's own cue path over logs the REAL
fold builds.
"""
from __future__ import annotations

from looplab.core.cards import normalize_steering_context
from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings, settings_from_snapshot
from looplab.core.models import Idea, durable_idea_payload
from looplab.engine.comparability import _PROTOCOL_CLAUSES, difference_reason
from looplab.engine.options import EngineOptions
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from tests.factories import make_engine

SMOKE, FULL = "a" * 16, "b" * 16


def _record(*, key="k" * 16, authority="declared", substrate=None, **protocol) -> dict:
    record = {"version": 1, "authority": authority, "keys": {authority: key}}
    if substrate:
        record["substrate"] = substrate
    if protocol:
        record["protocol"] = protocol
    return record


def test_the_reason_names_what_differs_in_the_status_rules_own_order():
    base = _record(profile=SMOKE)
    assert difference_reason(base, _record(profile=FULL)) == _PROTOCOL_CLAUSES["profile"]
    assert difference_reason(base, _record(profile=SMOKE, scorer="s" * 16)) is None, (
        "a facet only one side recorded is silence, not a difference")
    assert difference_reason(_record(profile=SMOKE, scorer="s" * 16),
                             _record(profile=SMOKE, scorer="t" * 16)) == _PROTOCOL_CLAUSES["scorer"]
    assert difference_reason(_record(substrate="x" * 16, profile=SMOKE),
                             _record(substrate="y" * 16, profile=FULL)) == (
        "ran on a different source tree"), "the source tree is asked first"
    assert difference_reason(base, _record(key="j" * 16, profile=SMOKE)) == (
        "was measured against different evaluation inputs (its declared key differs)")
    assert difference_reason(base, _record(profile=SMOKE)) is None                  # SAME
    assert difference_reason(base, _record(authority="measured", profile=SMOKE)) is None  # UNKNOWN
    assert difference_reason(base, None) is None and difference_reason(None, base) is None


def _state(tmp_path, nodes):
    """`nodes` is [(id, metric, comparability record or None)], direction max."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    for node_id, metric, record in nodes:
        idea = Idea(operator="draft", params={"x": float(node_id)}, rationale=f"n{node_id}")
        store.append("node_created", {"node_id": node_id, "parent_ids": [], "operator": "draft",
                                      "idea": durable_idea_payload(idea), "code": "pass\n"})
        store.append("node_evaluated", {
            "node_id": node_id, "generation": 0, "metric": metric, "violations": [],
            **({"metric_provenance": {"comparability": record}} if record else {})})
    return fold(store.read_all())


def _cue(tmp_path, state, *, on: bool):
    engine = make_engine(tmp_path / f"engine-{on}", brief_mixed_comparability=on)
    return engine, engine._cue_mixed_comparability(state, None, engine.researcher)


def test_the_cue_names_each_leader_on_another_ruler_than_the_champion(tmp_path):
    state = _state(tmp_path, [(0, 0.90, _record(profile=SMOKE)), (1, 0.95, _record(profile=FULL)),
                              (2, 0.80, _record(profile=SMOKE)), (3, 0.70, None)])
    assert state.best().id == 1
    _engine, (text, steering) = _cue(tmp_path, state, on=True)
    # The champion is node 1 (full); nodes 0 and 2 were scored on smoke. Node 3 recorded nothing:
    # unknown, never named.
    assert text.startswith("\nMEASURED WITH DIFFERENT RULERS: node 0 (metric=0.9) ")
    assert "node 2 (metric=0.8) " + _PROTOCOL_CLAUSES["profile"] in text
    assert "node 3" not in text and "best so far, node 1" in text
    assert steering == [{"kind": "mixed_comparability", "node_ids": [0, 2]}]
    assert normalize_steering_context(steering) == steering, "a registered steering kind"


def test_one_ruler_or_an_unrecorded_one_says_nothing(tmp_path):
    same = _state(tmp_path / "same", [(0, 0.9, _record(profile=SMOKE)),
                                      (1, 0.8, _record(profile=SMOKE))])
    assert _cue(tmp_path / "same", same, on=True)[1] == ("", [])
    silent = _state(tmp_path / "silent", [(0, 0.9, None), (1, 0.8, _record(profile=FULL))])
    assert _cue(tmp_path / "silent", silent, on=True)[1] == ("", []), "the champion recorded none"


def test_off_the_cue_is_silent_and_the_complexity_hint_is_the_historical_one(tmp_path):
    state = _state(tmp_path, [(0, 0.90, _record(profile=SMOKE)), (1, 0.95, _record(profile=FULL))])
    assert _cue(tmp_path, state, on=False)[1] == ("", [])
    hints = {}
    for on in (False, True):
        engine = make_engine(tmp_path / f"hint-{on}", brief_mixed_comparability=on)
        engine._set_complexity_hint(state, None)
        hints[on] = engine.researcher._complexity_hint
    assert "MEASURED WITH DIFFERENT RULERS" not in hints[False]
    # The cue is the LAST `PROPOSAL_CUES` member, so ON is OFF plus exactly the note.
    note = _cue(tmp_path / "note", state, on=True)[1][0]
    assert note and hints[True] == hints[False] + note


def test_the_flag_ships_off_resumes_off_and_is_off_at_every_constructor():
    assert Settings().brief_mixed_comparability is False
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["brief_mixed_comparability"] is False
    legacy = {k: v for k, v in Settings().masked_snapshot().items()
              if k != "brief_mixed_comparability"}
    assert settings_from_snapshot(legacy).brief_mixed_comparability is False
    assert EngineOptions().brief_mixed_comparability is False
    on = Settings(brief_mixed_comparability=True)
    assert EngineOptions.from_settings(on).brief_mixed_comparability is True
