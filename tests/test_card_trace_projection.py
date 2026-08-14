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
