"""One CARD's whole story: the research that proposed it, then every node it produced.

A Card is one hypothesis; the Researcher proposes it and the Developer builds one or more NODES under
it. Those two halves lived on different screens because no join existed between them at all — see
`orchestrator.stamp_proposal_span` for the measurement. This projection is what that join was for.

The rule that matters most here is the one about NOT matching: a card whose research cannot be
identified returns an empty list rather than a guessed one. Attributing another hypothesis's
reasoning to this card is worse than showing none, and time-adjacency would do exactly that.
"""
from __future__ import annotations

from looplab.events.traceview import project_card_trace


def _span(name, tid, sid, *, parent=None, start=0.0, kind="operation", **attributes):
    return {"name": name, "kind": kind, "trace_id": tid, "span_id": sid, "parent_id": parent,
            "start": start, "duration_s": 1.0, "status": "OK", "attributes": attributes}


def _corpus():
    return [
        # card-1's proposal, stamped by the engine (draft / debug / improve paths).
        _span("propose", "t-prop", "p1", start=10.0, card_id="card-1", proposed_for_node=0,
              operator="draft"),
        _span("generation", "t-prop", "p1g", parent="p1", start=10.5, kind="generation",
              usage={"prompt": 100, "completion": 20, "total": 120}),
        # another card's proposal, which must never appear under card-1.
        _span("propose", "t-other", "o1", start=11.0, card_id="card-9"),
        # card-1's node 0 build.
        _span("create_node", "t-node0", "n0", start=20.0, node_id=0),
        _span("generation", "t-node0", "n0g", parent="n0", start=20.5, kind="generation",
              node_id=0, usage={"prompt": 10, "completion": 5, "total": 15}),
        # …and the re-proposal a node reset performs: nested in node 0's build trace, and it CANNOT
        # carry the card id because that path mints the replacement after the span closes.
        _span("propose", "t-node0-rebuild", "rp", start=30.0, proposed_for_node=0),
        _span("create_node", "t-node0-rebuild", "n0b", start=30.1, node_id=0),
    ]


def test_a_stamped_proposal_is_matched_by_its_card_id():
    out = project_card_trace(_corpus(), card_id="card-1", node_ids=[0],
                             node_trace_ids={"0": "t-node0"})
    stamped = [r for r in out["research"] if r["link"] == "card_id"]
    assert len(stamped) == 1
    assert stamped[0]["trace_id"] == "t-prop"
    assert stamped[0]["proposed_for_node"] == 0
    assert stamped[0]["operator"] == "draft"
    assert stamped[0]["generations"] == 1
    assert stamped[0]["tokens"]["total"] == 120


def test_a_re_proposal_is_reachable_through_the_trace_it_shares_with_the_node():
    """The reset path drops the old card and mints the replacement after the span closes, so the
    span cannot name it. The trace can: its `node_created` belongs to this card's node."""
    out = project_card_trace(_corpus(), card_id="card-1", node_ids=[0],
                             node_trace_ids={"0": "t-node0-rebuild"})
    links = {r["trace_id"]: r["link"] for r in out["research"]}
    assert links["t-node0-rebuild"] == "shared_trace"
    assert links["t-prop"] == "card_id", "the stamped one still matches directly"


def test_another_card_s_research_never_appears():
    out = project_card_trace(_corpus(), card_id="card-1", node_ids=[0],
                             node_trace_ids={"0": "t-node0"})
    assert all(r["trace_id"] != "t-other" for r in out["research"])


def test_an_unmatchable_card_returns_no_research_rather_than_a_guess():
    # Nothing stamped, nothing sharing a trace — and a proposal DOES exist a second before the node.
    # Time-adjacency would happily attribute it; this must not.
    out = project_card_trace(_corpus(), card_id="card-unknown", node_ids=[])
    assert out["research"] == []
    assert out["nodes"] == []
    assert out["projection"]["total_research"] == 0


def test_research_is_ordered_oldest_first_so_the_story_reads_forward():
    out = project_card_trace(_corpus(), card_id="card-1", node_ids=[0],
                             node_trace_ids={"0": "t-node0-rebuild"})
    starts = [r["start"] for r in out["research"]]
    assert starts == sorted(starts)


def test_each_node_section_carries_its_own_size_so_the_reader_can_choose():
    out = project_card_trace(_corpus(), card_id="card-1", node_ids=[0],
                             node_trace_ids={"0": "t-node0"})
    assert len(out["nodes"]) == 1
    node = out["nodes"][0]
    assert node["node_id"] == "0"
    assert node["trace_id"] == "t-node0"
    assert node["generations"] == 1 and node["spans"] >= 2


def test_a_card_with_several_nodes_lists_them_in_id_order():
    corpus = _corpus() + [
        _span("create_node", "t-node7", "n7", start=40.0, node_id=7),
        _span("create_node", "t-node10", "n10", start=50.0, node_id=10),
    ]
    out = project_card_trace(corpus, card_id="card-1", node_ids=[7, 0, 10],
                             node_trace_ids={"0": "t-node0", "7": "t-node7", "10": "t-node10"})
    assert [n["node_id"] for n in out["nodes"]] == ["0", "7", "10"], (
        "10 must not sort before 7 as a string")


def test_node_zero_gets_its_own_section_falsy_ids_are_still_ids():
    """`str(node_id or "")` gives node 0 an empty section while every other node renders — the
    falsy-zero class this codebase already has a whole UI test file about. Pinned here because node
    0 is the FIRST node of every card, so getting it wrong empties the common case."""
    out = project_card_trace(_corpus(), card_id="card-1", node_ids=[0],
                             node_trace_ids={"0": "t-node0"})
    assert out["nodes"][0]["spans"] > 0
    assert out["nodes"][0]["generations"] == 1


def test_the_research_cap_keeps_the_OLDEST_rows_whatever_order_the_file_is_in():
    """The cap is oldest-first, and the file order it must survive is not start order.

    `by_trace` is iterated in span-file insertion order, and `spans.jsonl` is appended as spans
    CLOSE — so file order tracks END time. A cap applied by stopping the match loop at N (which is
    how the expensive `_rollup` was first bounded) therefore kept the first N ENCOUNTERED, and on a
    long-first/short-last workload that drops the card's own FIRST proposal: the one row the
    oldest-first rule exists to preserve.

    Driven with starts DESCENDING in file order, which is exactly that shape.
    """
    from looplab.events.traceview import TRACE_CARD_RESEARCH_CAP

    total = TRACE_CARD_RESEARCH_CAP + 44
    spans = [{"span_id": f"s{i}", "trace_id": f"t{i}", "parent_id": None, "name": "propose",
              "kind": "operation", "start": float(total - i), "duration_s": 0.1, "status": "OK",
              "attributes": {"card_id": "card-0"}}
             for i in range(total)]
    out = project_card_trace(spans, card_id="card-0", node_ids=[])

    starts = [row["start"] for row in out["research"]]
    assert len(starts) == TRACE_CARD_RESEARCH_CAP
    assert starts == sorted(starts), "the surviving rows are still oldest-first"
    assert min(starts) == 1.0, (
        "the card's own FIRST proposal was dropped — the cap kept the first rows ENCOUNTERED "
        "rather than the oldest, and file order is close time, not start time")
    assert max(starts) == float(TRACE_CARD_RESEARCH_CAP)

    # …and the receipt reports the real total against what is visible, so the truncation is not
    # something a reader has to infer.
    projection = out["projection"]
    assert projection["total_research"] == total
    assert projection["visible_research"] == TRACE_CARD_RESEARCH_CAP
    assert projection["truncated"] is True


# --------------------------------------------------------- the index's narrowed selection (D-03)
# `card_trace_view` used to hand this projection the WHOLE run's light span list and let it rescan
# that list once per owned node (docs/34 D-03). The selection moved into `SpanIndex` on 2026-09-08;
# what these tests owe that decision is both halves of it — the answer is IDENTICAL, the work is not,
# and each of the two research rules is driven separately because the second is the one a
# trace-scoped narrowing would have looked correct without.
def _indexed(tmp_path, spans):
    """Write `spans` as a run's `spans.jsonl` and return its `SpanIndex`."""
    import orjson

    from looplab.events.span_index import get_index, invalidate

    rd = tmp_path / "demo"
    rd.mkdir()
    source = rd / "spans.jsonl"
    with open(source, "wb") as f:
        for span in spans:
            f.write(orjson.dumps(span) + b"\n")
    invalidate(source)
    return get_index(source)


def _noise(count):
    """Other cards' research and other nodes' builds — the run this card is one hypothesis of."""
    out = []
    for i in range(count):
        out.append(_span("propose", f"t-noise{i}", f"np{i}", start=100.0 + i,
                         card_id=f"card-noise-{i}"))
        out.append(_span("create_node", f"t-noise{i}", f"nn{i}", start=100.5 + i, node_id=500 + i))
        out.append(_span("generation", f"t-noise{i}", f"ng{i}", parent=f"nn{i}", start=100.6 + i,
                         kind="generation", node_id=500 + i,
                         usage={"prompt": 3, "completion": 1, "total": 4}))
    return out


def _card_corpus():
    """`_corpus()` plus the run-scoped BUILD trace node 0 claims — the Card lane's real shape.

    The build ran on a speculative producer before any node id existed, so not one of its spans
    carries `node_id`; the node names it after the fact on `materialize_node`. Included because it
    is exactly the part a naive trace-scoped narrowing would drop.
    """
    return _corpus() + [
        _span("card_build", "t-build", "b0", start=15.0),
        _span("generation", "t-build", "b0g", parent="b0", start=15.5, kind="generation",
              usage={"prompt": 7, "completion": 3, "total": 10}),
        _span("materialize_node", "t-node0", "m0", parent="n0", start=21.0, node_id=0,
              build_trace="t-build", generation=0),
    ]


def test_narrowed_selection_matches_the_whole_run_projection(tmp_path):
    """The card's story is the same story, whether the projection is handed the run or the card.

    This is the invisible-optimization property, driven rather than asserted: both sides run the
    real projection, and the sections must be byte-identical. The narrowing lives one layer down
    (`SpanIndex.card_trace_spans`) precisely so this equivalence is checkable at all.
    """
    idx = _indexed(tmp_path, _card_corpus() + _noise(200))
    node_trace_ids = {"0": "t-node0"}

    whole = project_card_trace(idx.light_spans(), card_id="card-1", node_ids=[0],
                               node_trace_ids=node_trace_ids, _normalized=True)
    spans, claimed = idx.card_trace_spans(
        "card-1", node_ids=[0], node_trace_ids=node_trace_ids)
    narrow = project_card_trace(spans, card_id="card-1", node_ids=[0],
                                node_trace_ids=node_trace_ids, claimed=claimed, _normalized=True)

    assert narrow["research"] == whole["research"]
    assert narrow["nodes"] == whole["nodes"]
    assert narrow["card_id"] == whole["card_id"]
    # The claimed BUILD trace is in the node's section on both sides — the half a trace-scoped
    # narrowing loses, and the reason the claim map travels with the selection.
    assert whole["nodes"][0]["spans"] == narrow["nodes"][0]["spans"] >= 4
    assert narrow["nodes"][0]["generations"] == 2
    # Only the receipt's span axis differs, and deliberately: it now counts the CARD's spans.
    for key in ("total_research", "visible_research", "total_nodes", "visible_nodes", "truncated"):
        assert narrow["projection"][key] == whole["projection"][key]
    assert narrow["projection"]["total_spans"] == len(spans)
    assert whole["projection"]["total_spans"] == idx.span_count()


def test_the_selection_copies_only_the_card_s_rows(tmp_path):
    """THE ACCOUNTANT for docs/34 D-03: rows COPIED, counted — not a stopwatch.

    The old call site copied every light row in the run (a 1 GB run's index is ~220 MB of dicts).
    What the selection may copy is stated exactly: the two research rules' candidate traces plus the
    rows of the nodes this card owns, and nothing else in the run.
    """
    idx = _indexed(tmp_path, _card_corpus() + _noise(200))
    spans, _claimed = idx.card_trace_spans(
        "card-1", node_ids=[0], node_trace_ids={"0": "t-node0"})

    assert {s["span_id"] for s in spans} == {
        "p1", "p1g",                 # rule one: the trace of the stamped `propose` root…
        "n0", "n0g", "m0",           # rule two: node 0's own trace (its `node_created` trace)…
        "b0", "b0g",                 # …the run-scoped build trace node 0 CLAIMS…
        "n0b",                       # …and node 0's row in the trace its RESET rebuilt it under.
    }
    # `rp` — the unstamped re-proposal root sharing that rebuild trace — is deliberately NOT here:
    # `_rows_for_node` keeps only rows whose effective node is 0, and a research root names none.
    # It enters the selection through the caller's OWNED traces instead, which is what
    # `test_an_unstamped_re_proposal_is_still_reached_through_the_owned_trace` drives.
    assert "rp" not in {s["span_id"] for s in spans}
    # The run is 600+ rows of other cards and other nodes; none of them is copied.
    assert idx.span_count() > 600
    assert len(spans) < idx.span_count() / 50
    assert not any(s["span_id"].startswith("n") and s["trace_id"].startswith("t-noise")
                   for s in spans)


def test_an_unstamped_re_proposal_is_still_reached_through_the_owned_trace(tmp_path):
    """Rule two survives the narrowing. The reset path's re-proposal carries NO card stamp, so the
    index dimension cannot see it; the caller's owned traces are what put it in the selection."""
    idx = _indexed(tmp_path, _card_corpus() + _noise(50))
    spans, claimed = idx.card_trace_spans(
        "card-1", node_ids=[0], node_trace_ids={"0": "t-node0-rebuild"})
    out = project_card_trace(spans, card_id="card-1", node_ids=[0],
                             node_trace_ids={"0": "t-node0-rebuild"}, claimed=claimed,
                             _normalized=True)
    links = {r["trace_id"]: r["link"] for r in out["research"]}
    assert links["t-node0-rebuild"] == "shared_trace"
    assert links["t-prop"] == "card_id"


def test_a_card_stamped_proposal_in_a_foreign_trace_is_found_by_lookup(tmp_path):
    """The rule that made a trace-scoped narrowing impossible, driven directly: a stamped `propose`
    root can live in a trace this card owns nothing else in, and the index must still find it."""
    idx = _indexed(tmp_path, _card_corpus() + _noise(50))
    assert idx.card_propose_tids["card-1"] == {"t-prop"}
    spans, claimed = idx.card_trace_spans("card-1", node_ids=[], node_trace_ids={})
    out = project_card_trace(spans, card_id="card-1", node_ids=[], claimed=claimed,
                             _normalized=True)
    assert [r["trace_id"] for r in out["research"]] == ["t-prop"]
    assert out["research"][0]["link"] == "card_id"
