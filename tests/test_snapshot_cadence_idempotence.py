"""The at_node idempotence gate both snapshot producers share, and the concept producer's steps.

doc 25 EC-09. Two properties live here.

The GATE was spelled three times — once as `StrategyCadenceMixin._already_covered_at`, twice inline
in the concept cadence — and each copy composed the same two halves: `at_node == n`, which is what
makes resume-by-replay idempotent, and the analytics-projection match, which is what stops a stale
row from suppressing the cadence for the rest of that node count after an abort/reset/tag edit. Three
copies means three chances for one half to go missing, and NEITHER half fails loudly: drop the first
and the cadence re-emits (and, on the concept side, re-BUYS) every pass; drop the second and it goes
silent. One `search/coverage.py::already_covered_at` now states it, parametrized over the snapshot
LIST because the two producers keep independent ones and must not satisfy each other's gate.

The STEPS: `_concept_coverage_snapshot` is now a driver over `_reusable_node_tags` ->
`_refresh_concept_tags` (-> `_record_node_concept_tags`) -> `_assert_concept_edges` ->
`_tag_hypothesis_concepts`. A driver that quietly stopped calling one of them would still return a
perfectly well-formed snapshot, so the guard here is the four DURABLE effects, not the four calls.
"""
from __future__ import annotations

import inspect

import pytest

from looplab.core.models import Idea, Node, RunState
from looplab.engine.concept_cadence import ConceptCadenceMixin
from looplab.engine.strategy import StrategyCadenceMixin
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.search.coverage import (already_covered_at, analytics_projection_token,
                                     snapshot_matches_analytics_projection)


def _store(tmp_path, n_nodes=3) -> EventStore:
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": "max"})
    for i in range(n_nodes):
        s.append("node_created", {"node_id": i, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"seed": float(i)},
                                           "rationale": f"r{i}"}})
        s.append("node_evaluated", {"node_id": i, "metric": 0.5 + i * 0.01})
    return s


# --------------------------------------------------------------------------- #
# The predicate's truth table
# --------------------------------------------------------------------------- #

def _clean_state(n=3) -> RunState:
    st = RunState(direction="max", goal="g")
    st.nodes = {i: Node(id=i, operator="draft", idea=Idea(operator="draft", params={}))
                for i in range(n)}
    return st


def test_already_covered_at_truth_table():
    st = _clean_state(3)
    token = analytics_projection_token(st)
    live = {"at_node": 3, "projection_token": token}

    assert already_covered_at(st, 3, []) is False               # nothing recorded yet
    assert already_covered_at(st, 3, None) is False             # absent list, not a crash
    assert already_covered_at(st, 3, [live]) is True            # the row this cadence just wrote
    assert already_covered_at(st, 6, [live]) is False           # a later decision point is uncovered
    assert already_covered_at(st, 3, [None, live]) is True      # a junk row does not hide a live one
    assert already_covered_at(st, 3, [None]) is False


def test_a_row_at_the_right_node_count_with_a_stale_projection_leaves_the_gate_OPEN():
    """The half a bare `at_node == n` check loses.

    An abort/reset/tag edit changes what the snapshot would say WITHOUT allocating a node, so the
    node count alone cannot decide whether the run is still described by the row already on disk.
    """
    st = _clean_state(3)
    stale = {"at_node": 3, "projection_token": analytics_projection_token(st)}
    st.aborted_nodes.append(0)                    # same node count, different live projection

    assert snapshot_matches_analytics_projection(st, stale) is False
    assert already_covered_at(st, 3, [stale]) is False
    # ...and once the cadence records a row against the NEW projection, the gate closes again.
    fresh = {"at_node": 3, "projection_token": analytics_projection_token(st)}
    assert already_covered_at(st, 3, [stale, fresh]) is True


def test_the_two_producers_cannot_satisfy_each_others_gate():
    """Parametrizing over the LIST is the point: `coverage_snapshots` and `concept_coverage_snapshots`
    advance independently (different cadences), exactly as `cadence_due` takes each consumer's own
    marks. A single hard-coded list would let the cheap flat snapshot close the PAID concept gate."""
    st = _clean_state(3)
    st.coverage_snapshots.append({"at_node": 3, "projection_token": analytics_projection_token(st)})

    assert already_covered_at(st, 3, st.coverage_snapshots) is True
    assert already_covered_at(st, 3, st.concept_coverage_snapshots) is False


# --------------------------------------------------------------------------- #
# All three gate SITES, driven
# --------------------------------------------------------------------------- #

class _FlatHost(StrategyCadenceMixin):
    """The engine surface `_maybe_snapshot_coverage` reads: the flag, the cadence, a store."""

    def __init__(self, store):
        self.store = store
        self._coverage_context = True
        self.archive_resolution = 1.0

    def _should_consult(self, state, *, marks=None):
        return True                                  # off-cadence is a different gate; keep it open


class _ConceptHost(ConceptCadenceMixin):
    """The engine surface `_maybe_snapshot_concept_coverage` reads, with a scripted producer so the
    two gate sites (before the paid call, and again after it against the re-folded log) are reachable
    without a wired LLM."""

    def __init__(self, store, producer):
        self.store = store
        self._concept_pivot = True
        self._producer = producer
        self.produced = 0

    def _should_consult_concepts(self, state, *, marks=None):
        return True

    def _concept_coverage_snapshot(self, state):
        self.produced += 1
        return self._producer(self, state)


def _rows(store, event_type):
    return [e for e in store.read_all() if e.type == event_type]


def test_the_flat_cadence_records_once_per_node_count(tmp_path):
    store = _store(tmp_path)
    host = _FlatHost(store)
    state = host._maybe_snapshot_coverage(fold(store.read_all()))
    again = host._maybe_snapshot_coverage(state)

    assert len(_rows(store, "coverage_snapshot")) == 1
    assert len(again.coverage_snapshots) == 1


def test_the_concept_cadence_does_not_re_buy_a_node_count_it_already_covered(tmp_path):
    """Gate site 1 — BEFORE the paid producer. The concept snapshot dispatches LLM tagging and
    consolidation with no claim fence in front of it, so a gate that let the cadence re-enter would
    re-spend, not merely re-append."""
    store = _store(tmp_path)
    host = _ConceptHost(store, lambda _h, _s: {"fired": False})
    state = fold(store.read_all())
    store.append("concept_coverage_snapshot", {
        "at_node": len(state.nodes), "projection_token": analytics_projection_token(state),
        "fired": False})

    out = host._maybe_snapshot_concept_coverage(fold(store.read_all()))

    assert host.produced == 0, "the paid producer ran against an already-covered node count"
    assert len(_rows(store, "concept_coverage_snapshot")) == 1
    assert len(out.concept_coverage_snapshots) == 1


def test_the_concept_cadence_re_checks_the_gate_against_the_post_producer_log(tmp_path):
    """Gate site 2 — AFTER the producer, against a FRESH fold.

    The producer persists tags/consolidation while it works, so the state handed into the cadence is
    stale by the time it returns. This site is what stops the cadence from appending a second row for
    a node count that became covered during the call; site 1 alone cannot see it, because it ran
    before the write existed."""
    store = _store(tmp_path)

    def producer(host, state):
        # Stand in for anything that lands a snapshot for this node count while the paid producer is
        # running (a concurrent writer, or a resumed process replaying into the same log).
        host.store.append("concept_coverage_snapshot", {
            "at_node": len(state.nodes),
            "projection_token": analytics_projection_token(state), "fired": True})
        return {"fired": True}

    host = _ConceptHost(store, producer)
    out = host._maybe_snapshot_concept_coverage(fold(store.read_all()))

    assert host.produced == 1
    assert len(_rows(store, "concept_coverage_snapshot")) == 1, "a second row for the same node count"
    assert len(out.concept_coverage_snapshots) == 1


def test_a_stale_snapshot_at_the_same_node_count_lets_the_concept_cadence_record_again(tmp_path):
    """The projection half, at the live site: an abort does not allocate a node, so `at_node` alone
    would pin the cadence shut for the rest of that node count."""
    store = _store(tmp_path)
    host = _ConceptHost(store, lambda _h, _s: {"fired": False})
    first = host._maybe_snapshot_concept_coverage(fold(store.read_all()))
    store.append("node_abort", {"node_id": 0, "generation": 0})

    refreshed = host._maybe_snapshot_concept_coverage(fold(store.read_all()))

    assert len(_rows(store, "concept_coverage_snapshot")) == 2
    snaps = refreshed.concept_coverage_snapshots
    assert snaps[0]["at_node"] == snaps[-1]["at_node"] == len(first.nodes)
    assert snaps[0]["projection_token"] != snaps[-1]["projection_token"]


@pytest.mark.parametrize("method", [
    StrategyCadenceMixin._maybe_snapshot_coverage,
    ConceptCadenceMixin._maybe_snapshot_concept_coverage,
])
def test_no_gate_site_re_derives_the_predicate(method):
    """AST for the positive half (comments are not AST nodes), substring for the negative half.

    `_maybe_snapshot_concept_coverage` carries BOTH concept sites, so one `already_covered_at` call
    is not enough for it — the count is the property.
    """
    from tests._source_scan import called_names

    calls = called_names(method)
    expected = 2 if method is ConceptCadenceMixin._maybe_snapshot_concept_coverage else 1
    assert calls.count("already_covered_at") == expected, (
        f"{method.__name__} reaches the shared at_node gate {calls.count('already_covered_at')} "
        f"time(s), expected {expected}")
    source = inspect.getsource(method)
    assert "snapshot_matches_analytics_projection" not in source, (
        f"{method.__name__} re-derives the projection half instead of calling already_covered_at")


# --------------------------------------------------------------------------- #
# The producer's steps — the four durable effects, not the four calls
# --------------------------------------------------------------------------- #

class _ProducerHost(ConceptCadenceMixin):
    def __init__(self, store):
        self.store = store

    def _reflect_client(self):
        return object()                              # any non-None client takes the agentic path


def test_the_concept_producer_lands_all_four_of_its_recording_steps(tmp_path, monkeypatch):
    """One pass must leave consolidation, per-node tags, is_a edges and card tags in the log.

    A driver that stopped calling one of its steps still returns a well-formed snapshot dict, so the
    return value proves nothing — only the log does. Each of the four is written by a DIFFERENT step,
    and two of them (edges, card tags) swallow their own exceptions, which is exactly the shape that
    disappears quietly.
    """
    from looplab.search import concept_analytics as ca
    from looplab.search import concept_graph as cg
    from looplab.search import concept_map as cm
    from looplab.search import concept_tagging as ct

    s = _store(tmp_path, n_nodes=2)
    s.append("card_added", {"id": "card-1", "statement": "r-drop helps recall"})
    state = fold(s.read_all())

    graph = cg.dense_retrieval_skeleton()
    tags = {0: frozenset({"loss/decoupled-contrastive"}), 1: frozenset({"regularization/r-drop"})}

    def fake_build(*args, **kwargs):
        return {"graph": graph, "tags": tags, "raw_tags": tags,
                "coverage": ca.concept_coverage(state, graph, tags),
                "important_uncovered": [],
                "consolidated": {"loss/dcl": "loss/decoupled-contrastive"},
                "mode": "llm"}

    monkeypatch.setattr(cm, "build_concept_map", fake_build)
    monkeypatch.setattr(ct, "tag_text_llm", lambda *a, **k: {"regularization/r-drop"})

    assert _ProducerHost(s)._concept_coverage_snapshot(state) is not None

    assert [r.data["rename"] for r in _rows(s, "concept_consolidation")] == [
        {"loss/dcl": "loss/decoupled-contrastive"}]
    assert {r.data["node_id"] for r in _rows(s, "node_concepts")} == {0, 1}
    edges = _rows(s, "concept_edge")
    assert edges and any(e["rel"] == "is_a" for e in edges[0].data["edges"])
    assert [r.data["hyp_id"] for r in _rows(s, "hypothesis_concepts")] == ["card-1"]
