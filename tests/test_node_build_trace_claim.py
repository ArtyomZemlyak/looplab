"""THE NODE'S BUILD IS PART OF THE NODE — the `build_trace` claim (F7).

The operator's report, on `runs/rubertlite-dr-unified-v7` Experiment #0: the node's trace shows
Train, Triage, "Developer · inline repair" and train_monitor — and no Developer BUILD at all. No
plan, no stages, no implement. The header still said "build and evaluation".

The measurement, 2026-08-14, over the whole `runs/` corpus:

* `rubertlite-dr-unified-v7` — 2,637 spans, 2,403 (91.1 %) attributable to NO node. 1,312 of them
  are the three `card_build` traces: the Developer's entire construction, `plan` and `stages`
  included, with `attributes={}` on every root. Node 2's trace was TWO spans.
* the serial-path run `rubert-dr-0804` — 14,846 spans, NINE unattributed (0.1 %). Nothing is broken
  there, because `orchestrator._create_node` opens `create_node` with the node id and the build runs
  INSIDE it. This is a Card-lane defect, not a tracing one.

WHY THE BUILD CANNOT SIMPLY CARRY `node_id`. `_build_requested_card` runs on a speculative producer
worker BEFORE any node id is reserved. The id it could compute is `_node_id_ceiling`, a PREDICTION
that `_claim_requested_card_build` re-derives after that span has already closed — and the build may
be refused (stale / budget / superseded) and mint no node at all. Stamping it would file a
1,300-span build under a node that need not exist, which is the one thing a trace must never do
(`orchestrator.stamp_proposal_span` states the same rule for `proposed_for_node`).

SO THE POINTER RUNS THE OTHER WAY: the node NAMES its build, on the one span where both facts exist
at once (`materialize_node`, which already carries `node_id` and `generation`). These tests drive
both halves — that the writer records the claim, and that every reader honours it, including the one
case that makes the map a parameter rather than a derivation: a `?before=` window anchored INSIDE the
build, where the claiming span is legitimately behind the anchor.
"""
from __future__ import annotations

import json

import pytest

from looplab.events.span_index import SpanIndex
from looplab.events.traceview import (build_conversation, build_trace_view, claimed_build_traces,
                                      claimed_trace_node_id, node_episodes)


class _Scalars:
    """The three fields the conversation/trace projections read off `RunState`."""

    run_id = "demo"
    task_id = "task"
    total_eval_seconds = 0.0


BUILD_TRACE = "b" * 32
NODE_TRACE = "n" * 32


def _card_lane_run(tmp_path, *, claim: bool = True, node_id: int = 0, generation: int = 0):
    """The exact shape of a Card-lane node: a run-scoped build trace + the node's own trace.

    Modelled on `runs/rubertlite-dr-unified-v7` trace `d8aa0534…`: a `card_build` root with `plan`
    and `stages` under it and the implement loop's turns stamped `phase=card_build`, none of them
    carrying a node id — and, in a DIFFERENT trace, the node's `create_node` -> `materialize_node`
    pair, which is where the id first exists.
    """
    rows: list[dict] = []

    def row(trace, span_id, parent, name, kind, start, attributes):
        rows.append({
            "trace_id": trace, "span_id": span_id, "parent_id": parent, "run_id": "demo",
            "name": name, "kind": kind, "start": start, "duration_s": 1.0, "status": "OK",
            "attributes": attributes, "events": [],
        })

    def turn(trace, span_id, parent, start, phase, text, attributes=None):
        row(trace, span_id, parent, "generation", "generation", start, {
            "phase": phase, "phase_span": parent,
            "input": [{"role": "user", "content": "build it"}], "output": text,
            **(attributes or {}),
        })

    # --- the build, in its own trace, with no node anywhere in it -------------------------------
    turn(BUILD_TRACE, "stages-gen", "stages", 2.0, "stages", "declare the stages")
    row(BUILD_TRACE, "stages", "card-build", "stages", "operation", 1.0, {})
    turn(BUILD_TRACE, "plan-gen", "plan", 4.0, "plan", "plan the change")
    row(BUILD_TRACE, "plan", "card-build", "plan", "operation", 3.0, {})
    turn(BUILD_TRACE, "implement-gen", "card-build", 5.0, "card_build", "write the code")
    # spans.jsonl is written on CLOSE, so the root lands last — the shape `trace_root_span` exists for.
    row(BUILD_TRACE, "card-build", None, "card_build", "operation", 0.0,
        {"card_id": "card-0", "card_build_generation": 0})

    # --- the node's own trace, which is where a node id first exists ----------------------------
    row(NODE_TRACE, "materialize", "create-node", "materialize_node", "operation", 10.0, {
        "node_id": node_id, "generation": generation, "operator": "draft",
        **({"build_trace": BUILD_TRACE} if claim else {}),
    })
    turn(NODE_TRACE, "train-gen", "train", 12.0, "train", "watch the training")
    row(NODE_TRACE, "train", "create-node", "train", "operation", 11.0,
        {"node_id": node_id, "generation": generation, "stage": "train"})
    row(NODE_TRACE, "create-node", None, "create_node", "operation", 9.0,
        {"node_id": node_id, "generation": generation})

    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "spans.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    index = SpanIndex(path)
    index._rebuild(path.stat().st_size)
    return index


def _labels(payload):
    return [stage["label"] for stage in payload["stages"]]


# --- the READER: what a node's trace now reaches ------------------------------------------------

def test_the_build_block_is_in_the_nodes_trace_and_is_gone_without_the_claim(tmp_path):
    """The operator's report, stated as a difference: the same spans, the claim the only variable."""
    claimed = _card_lane_run(tmp_path / "with", claim=True)
    unclaimed = _card_lane_run(tmp_path / "without", claim=False)

    # 8 build spans + 4 of the node's own; without the claim only the node's own four are reachable.
    assert claimed.node_span_count(0, generation=0) == 10
    assert unclaimed.node_span_count(0, generation=0) == 4

    with_build = build_conversation(
        _Scalars, claimed.full_spans_for_node(0, 512, generation=0), 0,
        claimed_traces=claimed.node_build_traces(0, generation=0), _normalized=True)
    without = build_conversation(
        _Scalars, unclaimed.full_spans_for_node(0, 512, generation=0), 0,
        claimed_traces=unclaimed.node_build_traces(0, generation=0), _normalized=True)

    # THE DEFECT: "no plan, no stages, no implement" — and the node's own work still there, which is
    # what made it read as a complete trace rather than a truncated one.
    assert _labels(without) == ["train"]
    # THE FIX. `card_build` is the implement loop's own band (its turns are stamped with its span).
    assert _labels(with_build) == ["stages", "plan", "card_build", "train"]


def test_the_episode_map_names_the_build_bands_so_they_can_be_sought_to(tmp_path):
    """A band the map does not name is a place the operator cannot point the window at."""
    index = _card_lane_run(tmp_path)
    episodes = node_episodes(
        index.light_spans_for_node(0, None, generation=0), 0,
        total_spans=index.node_span_count(0, generation=0), _normalized=True)["episodes"]

    assert [row["label"] for row in episodes] == ["stages", "plan", "card_build", "train"]
    # Every build band is anchorable, and its anchor is its own OPERATION span — the row that lands
    # last in an append-only log, so a window cut there ENDS with that band complete.
    assert [row["anchor"] for row in episodes] == ["stages", "plan", "card-build", "train"]
    assert all(row["trace_id"] == BUILD_TRACE for row in episodes[:3])


def test_seeking_into_the_build_still_renders_it_because_the_claim_comes_from_the_index(tmp_path):
    """THE reason the claim map is a parameter and not a derivation.

    The claim lives on `materialize_node`, in the node's OWN trace, which is written AFTER the whole
    build. So a window anchored on a band inside the build legitimately excludes it. A projection
    re-deriving the map from its own window would find no claim, drop every row, and answer an empty
    conversation for a read that had already paid to fetch the build.
    """
    index = _card_lane_run(tmp_path)
    window = index.full_spans_for_node(0, 512, generation=0, before="stages")
    assert index.has_span("stages")
    assert [span["name"] for span in window] == ["generation", "stages"]
    assert not any(span["name"] == "materialize_node" for span in window)

    seeking = build_conversation(
        _Scalars, window, 0, claimed_traces=index.node_build_traces(0, generation=0),
        _normalized=True)
    assert _labels(seeking) == ["stages"]

    # The same window with the map re-derived from itself — the failure this parameter prevents.
    assert _labels(build_conversation(_Scalars, window, 0, _normalized=True)) == []


def test_the_run_level_trace_view_stops_calling_the_build_unscoped(tmp_path):
    """`/trace` groups by node and everything else lands in `unscoped`; the build belongs to a node."""
    index = _card_lane_run(tmp_path)
    view = build_trace_view(_Scalars, index.light_spans(), light=True)
    assert view["unscoped"] == []
    assert view["rollups"]["0"]["generations"] == 4      # three build turns + the node's own


# --- the RULE: what a claim may and may not do --------------------------------------------------

def test_a_claim_never_takes_a_trace_that_already_names_a_node(tmp_path):
    """A claim fills a None. It is not an override, or one node could annex another's trace."""
    spans = [
        {"trace_id": "a" * 32, "span_id": "s1", "parent_id": None, "name": "create_node",
         "kind": "operation", "start": 0.0, "attributes": {"node_id": 1, "generation": 0}},
        {"trace_id": "c" * 32, "span_id": "s2", "parent_id": None, "name": "materialize_node",
         "kind": "operation", "start": 1.0,
         "attributes": {"node_id": 2, "generation": 0, "build_trace": "a" * 32}},
    ]
    view = build_trace_view(_Scalars, spans, light=True)
    # Node 1's own trace stays node 1's, claim or no claim.
    assert set(view["nodes"]) == {"1", "2"}
    assert [s["span_id"] for s in view["nodes"]["1"]] == ["s1"]


def test_a_span_cannot_claim_its_own_trace():
    """Self-claim says nothing `node_id` does not already say, and would re-attribute siblings."""
    assert claimed_build_traces([
        {"trace_id": "t" * 32, "span_id": "s", "parent_id": None, "name": "materialize_node",
         "kind": "operation", "start": 0.0,
         "attributes": {"node_id": 3, "build_trace": "t" * 32}},
    ]) == {}


def test_two_nodes_claiming_one_build_award_it_to_neither():
    """Only reachable from a corrupt/crafted log — and the answer must not depend on file order.

    The same refusal `trace_root_span` makes for a rootless trace: decline to name one rather than
    nominate whichever happened to be written first.
    """
    def claimant(span_id, node_id):
        return {"trace_id": "n" * 32, "span_id": span_id, "parent_id": None,
                "name": "materialize_node", "kind": "operation", "start": 0.0,
                "attributes": {"node_id": node_id, "build_trace": BUILD_TRACE}}

    rows = [claimant("s1", 1), claimant("s2", 2), claimant("s3", 1)]
    assert claimed_build_traces(rows) == {}
    assert claimed_build_traces(list(reversed(rows))) == {}


def test_two_nodes_claiming_one_build_are_refused_by_the_index_in_any_order(tmp_path):
    """The index resolves claims INCREMENTALLY, one appended row at a time — same answer required."""
    def row(span_id, node_id):
        return {"trace_id": NODE_TRACE, "span_id": span_id, "parent_id": None,
                "run_id": "demo", "name": "materialize_node", "kind": "operation",
                "start": 0.0, "duration_s": 1.0, "status": "OK",
                "attributes": {"node_id": node_id, "build_trace": BUILD_TRACE}, "events": []}

    for order in ([1, 2, 1], [1, 1, 2], [2, 1, 1]):
        path = tmp_path / f"spans-{''.join(str(o) for o in order)}.jsonl"
        path.write_text("".join(
            json.dumps(row(f"s{i}", node)) + "\n" for i, node in enumerate(order)), encoding="utf-8")
        index = SpanIndex(path)
        index._rebuild(path.stat().st_size)
        assert index.build_claims == {}, order
        assert index.node_build_traces(1) == {} and index.node_build_traces(2) == {}


def test_the_claim_carries_the_claiming_spans_lifecycle_not_the_builds(tmp_path):
    """A `node_reset` builds again; generation N must not reach generation N-1's build.

    The build's own trace root carries whatever generation context the producer thread inherited and
    never the node attempt it was built for, so the fence has to come from the claim.
    """
    index = _card_lane_run(tmp_path, generation=2)
    assert claimed_trace_node_id(index.build_claims, BUILD_TRACE, generation=2) == 0
    assert claimed_trace_node_id(index.build_claims, BUILD_TRACE, generation=1) is None
    # And the fenced read agrees: the abandoned attempt reaches none of this build's spans.
    assert index.node_span_count(0, generation=2) == 10
    assert index.node_span_count(0, generation=1) == 0


def test_a_foreign_row_inside_a_claimed_trace_still_belongs_to_its_own_node(tmp_path):
    """The claim supplies the trace's missing FALLBACK; it does not overwrite a span's own id.

    The Card lane's producer roles are reused sequentially, so one build trace CAN carry a row
    stamped for another node — the same per-span-first rule `effective_node_id` exists for.
    """
    rows = [
        {"trace_id": BUILD_TRACE, "span_id": "shared", "parent_id": "build-root", "run_id": "demo",
         "name": "generation", "kind": "generation", "start": 1.0, "duration_s": 1.0,
         "status": "OK", "attributes": {"node_id": 9}, "events": []},
        {"trace_id": BUILD_TRACE, "span_id": "mine", "parent_id": "build-root", "run_id": "demo",
         "name": "generation", "kind": "generation", "start": 2.0, "duration_s": 1.0,
         "status": "OK", "attributes": {}, "events": []},
        {"trace_id": BUILD_TRACE, "span_id": "build-root", "parent_id": None, "run_id": "demo",
         "name": "card_build", "kind": "operation", "start": 0.0, "duration_s": 3.0,
         "status": "OK", "attributes": {"card_id": "card-0"}, "events": []},
        {"trace_id": NODE_TRACE, "span_id": "materialize", "parent_id": None, "run_id": "demo",
         "name": "materialize_node", "kind": "operation", "start": 4.0, "duration_s": 1.0,
         "status": "OK", "attributes": {"node_id": 0, "generation": 0,
                                        "build_trace": BUILD_TRACE}, "events": []},
    ]
    path = tmp_path / "spans.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    index = SpanIndex(path)
    index._rebuild(path.stat().st_size)

    mine = {span["span_id"] for span in index.light_spans_for_node(0, None, generation=0)}
    assert mine == {"build-root", "mine", "materialize"}
    assert {span["span_id"] for span in index.light_spans_for_node(9, None)} == {"shared"}


# --- the WRITER: the claim has to survive the durable span boundary ------------------------------

def test_the_tracer_keeps_build_trace_as_a_structural_attribute(tmp_path):
    """`build_trace` is an ATTRIBUTION key, so it may not compete with payload for the text budget.

    The failure this guards is exactly the one `node_id`/`generation` were made structural for: a
    prompt-sized diagnostic written first spends the aggregate budget and silently erases the fence —
    here, silently deleting a node's whole build from its own trace.
    """
    from looplab.core.tracing import JsonlSpanExporter, Tracer

    tracer = Tracer(JsonlSpanExporter(tmp_path / "spans.jsonl"), run_id="demo")
    with tracer.span("materialize_node", node_id=0, generation=0,
                     build_trace=BUILD_TRACE, noise="x" * 200_000):
        pass
    written = [json.loads(line) for line in
               (tmp_path / "spans.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    attributes = written[-1]["attributes"]
    assert attributes["build_trace"] == BUILD_TRACE
    assert attributes["node_id"] == 0

    # And it survives the projection allow-list — an attribute the reader drops is an attribute the
    # writer never wrote, which is how `card_id` shipped inert the first time.
    index = SpanIndex(tmp_path / "spans.jsonl")
    index._rebuild((tmp_path / "spans.jsonl").stat().st_size)
    assert index.build_claims == {BUILD_TRACE: (0, 0)}


def test_arbitrary_metadata_cannot_forge_a_claim(tmp_path):
    """A reserved attribution key must not be reachable by a value that merely NORMALIZES onto it."""
    from looplab.core.tracing import JsonlSpanExporter, Tracer

    tracer = Tracer(JsonlSpanExporter(tmp_path / "spans.jsonl"), run_id="demo")
    with tracer.span("tool", node_id=0, kind="tool") as span:
        span.set("build_trace\n", "x" * 32)          # sanitizes to the reserved spelling
        span.set("build_trace", {"not": "an id"})    # right key, wrong shape
    written = [json.loads(line) for line in
               (tmp_path / "spans.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    attributes = written[-1]["attributes"]
    assert "build_trace" not in attributes
    assert set(attributes) >= {"field.build_trace"}


# --- the NO-INDEX fallback: the same rule, over the whole run -------------------------------------

def test_the_fallback_path_reaches_the_build_and_keeps_its_generation_fence(tmp_path):
    """`build_conversation` with no index receives the WHOLE run and derives the claim map itself.

    Also pins the fence this path has always applied and must keep: a trace belonging to another
    lifecycle is excluded WHOLE, which is a different answer from "this trace names no node" — the
    second still lets a span's own id attribute it, and collapsing the two would concatenate an
    abandoned attempt into the current one.
    """
    index = _card_lane_run(tmp_path)
    whole_run = index.light_spans()
    assert len(whole_run) == 10

    reached = build_conversation(_Scalars, whole_run, 0, _normalized=True)
    assert _labels(reached) == ["stages", "plan", "card_build", "train"]

    # The node here is generation 0, so a read fenced to attempt 1 must see none of it — neither its
    # own trace (fenced by its root) nor the build it claims (fenced by the claim).
    other = build_conversation(_Scalars, whole_run, 0, generation=1, _normalized=True)
    assert _labels(other) == []
    assert other["projection"]["visible_spans"] == 0


def test_the_trace_tab_keeps_the_build_under_its_node_when_the_window_seeks_into_it(tmp_path):
    """`/nodes/{n}/trace`'s tree, with the same anchored window the conversation is tested on.

    Without the index's map the block would land in `unscoped` — the node's own tree tab empty for a
    window the index had just selected for that node.
    """
    index = _card_lane_run(tmp_path)
    window = index.light_spans_for_node(0, 512, generation=0, before="stages")

    scoped = build_trace_view(_Scalars, window, light=True,
                              claimed_traces=index.node_build_traces(0, generation=0))
    assert scoped["unscoped"] == []
    assert set(scoped["nodes"]) == {"0"}

    assert build_trace_view(_Scalars, window, light=True)["nodes"] == {}
