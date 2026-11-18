"""A selectable staged Card cannot outlive the atomic prefix that justified its proposal."""
import pytest

from looplab.core.models import Idea
from looplab.engine.speculation import SpecRawStageResult
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from tests.factories import make_engine


AUDIT = (("novelty_rejected", {"reason": "reviewed competing option"}, "producer", "novelty"),
         ("policy_decision", {"reason": "proposal governance"}, None, None))


def _prepared(engine):
    state = fold(engine.store.read_all())
    return SpecRawStageResult(generation=0, action={"kind": "draft"}, proposal_state=state,
        proposal_node_ceiling=0, at_node=0, source="researcher", success=True,
        idea=Idea(operator="draft", hypothesis="one audited executable direction"), audit_events=AUDIT)


def _engine(tmp_path):
    engine = make_engine(tmp_path / "run", n_seeds=1, max_nodes=5, card_driven_selection=True)
    engine._spec_raw_stage_result = _prepared(engine)
    return engine


def _check_complete(events):
    card = next(e for e in events if e.type == "card_added")
    assert [(e.type, e.data, e.trace_id, e.span_id) for e in events if e.seq > card.seq] == list(AUDIT)
    assert fold(events).cards[card.data["id"]].selection_ready is True


def test_real_raw_stage_preserves_individual_spans_and_every_physical_crash_prefix(tmp_path):
    engine = _engine(tmp_path)
    before = engine.store.path.read_bytes() if engine.store.path.exists() else b""
    assert engine._serve_raw_card_stage() == (True, True, None)
    _check_complete(EventStore(engine.store.path).read_all())
    after = engine.store.path.read_bytes()
    assert len(after.splitlines()) == len(before.splitlines()) + 1
    batch = after[len(before):]
    path = tmp_path / "crash.jsonl"
    # Every byte boundary, including immediately before the final newline: no exposed Card/audit.
    for cut in range(len(batch)):
        path.write_bytes(before + batch[:cut])
        rows = EventStore(path).read_all()
        assert not fold(rows).cards, cut
        assert not any(e.type in {"novelty_rejected", "policy_decision"} for e in rows), cut
    path.write_bytes(after)
    _check_complete(EventStore(path).read_all())


def test_source_tail_race_retries_whole_card_and_audit_without_duplication(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    append = engine.store.append_many
    calls = []
    def race(records, **kwargs):
        calls.append(records)
        if len(calls) == 1:
            EventStore(engine.store.path).append("test_race", {})
        return append(records, **kwargs)
    monkeypatch.setattr(engine.store, "append_many", race)
    assert engine._serve_raw_card_stage() == (True, True, None)
    assert len(calls) == 2
    events = EventStore(engine.store.path).read_all()
    assert sum(e.type == "card_added" for e in events) == 1
    _check_complete(events)


@pytest.mark.parametrize("after_commit", [False, True])
def test_append_fault_leaves_no_or_fully_audited_card(tmp_path, monkeypatch, after_commit):
    engine = _engine(tmp_path)
    append = engine.store.append_many
    def fault(records, **kwargs):
        if after_commit:
            append(records, **kwargs)
        raise OSError("lost write or lost response")
    monkeypatch.setattr(engine.store, "append_many", fault)
    with pytest.raises(OSError):
        engine._serve_raw_card_stage()
    events = EventStore(engine.store.path).read_all()
    if after_commit:
        _check_complete(events)
    else:
        assert not fold(events).cards
        assert not any(e.type in {"novelty_rejected", "policy_decision"} for e in events)


@pytest.mark.parametrize("contexts", [[], [(None, None), (None, None)], [[None, None]],
                                    [(123, None)], [(None,)], ["bad"]])
def test_malformed_batch_contexts_never_write(tmp_path, contexts):
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r"})
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.append_many([("policy_decision", {})], record_contexts=contexts)
    assert store.path.read_bytes() == before
