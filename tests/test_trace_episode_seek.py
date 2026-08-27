"""REACHING AN EARLIER STEP OF A NODE — the episode map and the window anchor (F6).

The operator's report: "make the conversation trace more convenient. You cannot see traces from
earlier versions of a node (bugs happened and repair kicked in)."

The measurement behind everything here, taken 2026-08-13 against `runs/rubert-dr-0804` node 1:
14,507 spans across 2,345 inline repairs over 3 h 50 m, ALL of them lifecycle generation 0. Two
independent facts follow, and each defeats one of the two controls that already existed:

* the ATTEMPT picker cannot reach them. `Node.attempt` is bumped by `node_reset`, never by inline
  repair, so that node has exactly one attempt and the picker does not render at all;
* WIDENING cannot reach them. The window is a TAIL, so a bigger `limit` is the same tail extended:
  the 512-span default covers that node's last 7.6 minutes and the 4,096-span ceiling its last 59.3.
  Raising the ceiling is not the fix either — the conversation costs 3.4 ms/span of request thread.

So the window learned to MOVE (`?before=<span_id>`) and the node learned to publish the places worth
moving to (`/nodes/{n}/episodes`). These drive both, and the properties they hold are the ones a
reader of the code cannot check: that the anchored window really contains the chosen episode and
really excludes the newest one, that a validator cannot carry one anchor's body to another, and that
an anchor the server cannot place is refused rather than answered with the tail.
"""
from __future__ import annotations

import json

import pytest

from looplab.events.traceview import TRACE_CONVERSATION_SPAN_CAP, TRACE_NODE_SPAN_CAP_MAX


# Past the CEILING, not merely past the default window: 1,400 repairs is 5,603 spans (four each — a
# triage generation and its operation, a repair generation and its operation) against a
# `TRACE_NODE_SPAN_CAP_MAX` of 4,096. That is the whole point — a node the widen ladder cannot reach
# the beginning of even at its last rung, which is the shape of the node the operator reported
# (rubert-dr-0804 node 1 is 14,507 spans, three and a half times the ceiling). `settle_node_span_cap`
# never shrinks a window below its default, so a smaller fixture could not state this property at all.
REPAIRS = 1400
SMALL_REPAIRS = 40    # a node whose whole trace fits in one window (163 spans)


def _repaired_node(tmp_path, *, repairs: int = REPAIRS):
    """A node that was inline-repaired `repairs` times inside ONE lifecycle generation.

    Deliberately the shape the operator hit: one long-lived trace, every span generation 0, and the
    repair ordinal stamped exactly where `engine/evaluate.py` stamps it (`attempt` on the repair's
    own operation span). No `node_reset` anywhere — the attempt picker has nothing to offer here and
    that is the point.
    """
    from fastapi.testclient import TestClient

    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app

    run_dir = tmp_path / "demo"
    run_dir.mkdir()
    EventStore(run_dir / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "task", "goal": "g", "direction": "min"})
    rows = [{
        "trace_id": "t", "span_id": "root", "parent_id": None, "run_id": "demo",
        "name": "create_node", "kind": "operation", "start": 0.0, "duration_s": 1.0,
        "status": "OK", "attributes": {"node_id": 0, "generation": 0}, "events": [],
    }]

    def op(span_id, name, start, attributes):
        rows.append({
            "trace_id": "t", "span_id": span_id, "parent_id": "root", "run_id": "demo",
            "name": name, "kind": "operation", "start": start, "duration_s": 5.0, "status": "OK",
            "attributes": {"node_id": 0, **attributes}, "events": [],
        })

    def gen(span_id, parent, start, phase, text):
        rows.append({
            "trace_id": "t", "span_id": span_id, "parent_id": parent, "run_id": "demo",
            "name": "generation", "kind": "generation", "start": start, "duration_s": 1.0,
            "status": "OK",
            "attributes": {"node_id": 0, "phase": phase, "phase_span": parent,
                           "input": [{"role": "user", "content": "fix it"}], "output": text},
            "events": [],
        })

    op("implement", "implement", 1.0, {})
    gen("implement-gen", "implement", 2.0, "implement", "the first code")
    for index in range(1, repairs + 1):
        base = 10.0 + index * 10
        # spans.jsonl is written on CLOSE, so an operation's row lands AFTER its children — which is
        # what makes the operation span the right anchor for "the window ending with this episode".
        gen(f"triage-gen-{index}", f"triage-{index}", base + 1, "triage", f"why it broke {index}")
        op(f"triage-{index}", "triage", base, {"attempt": index, "reason": "crash"})
        gen(f"repair-gen-{index}", f"repair-{index}", base + 3, "inline_repair", f"repair {index}")
        op(f"repair-{index}", "inline_repair", base + 2, {"attempt": index})
    (run_dir / "spans.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return TestClient(make_app(tmp_path)), len(rows)


def _labels(payload):
    return [stage["label"] for stage in payload["stages"]]


def test_the_map_names_every_repair_and_its_seek_key(tmp_path):
    pytest.importorskip("fastapi")
    client, _spans = _repaired_node(tmp_path, repairs=SMALL_REPAIRS)

    body = client.get("/api/runs/demo/nodes/0/episodes")
    assert body.status_code == 200
    episodes = body.json()["episodes"]
    repairs = [row for row in episodes if row["label"] == "inline_repair"]
    assert len(repairs) == SMALL_REPAIRS
    # The engine's OWN ordinal, not the map's position — a map that renumbered would assert that its
    # oldest row is repair #1 even when its ceiling cut the older ones off.
    assert [row["ordinal"] for row in repairs] == list(range(1, SMALL_REPAIRS + 1))
    # Every row can actually be sought to. An anchorless row is a choice that cannot be made, and the
    # client model drops it — so a map full of them would be a control that does nothing.
    assert all(row["anchor"] for row in episodes)
    assert {row["label"] for row in episodes} >= {"implement", "triage", "inline_repair"}
    # A MAP, not a reading: no band's contents travel with it (the whole node is one poll otherwise).
    assert "turns" not in json.dumps(episodes)
    # And the triage band carries the crash reason it recorded, which is what makes one repair worth
    # choosing over another.
    assert any(row.get("reason") == "crash" for row in episodes)


def test_episode_pages_reach_the_beginning_without_overlap(tmp_path):
    """The map's response cap is a page size, not a permanent cut through old history."""
    pytest.importorskip("fastapi")
    client, _spans = _repaired_node(tmp_path, repairs=SMALL_REPAIRS)

    whole = client.get("/api/runs/demo/nodes/0/episodes").json()["episodes"]
    combined = []
    cursor = None
    snapshot = None
    previous_older = None
    while True:
        params = {"limit": 7}
        if cursor is not None:
            params["before"] = cursor
            params["snapshot"] = snapshot
        response = client.get("/api/runs/demo/nodes/0/episodes", params=params)
        assert response.status_code == 200
        payload = response.json()
        rows = payload["episodes"]
        page = payload["page"]

        assert 0 < len(rows) <= 7
        assert page["before"] == cursor
        snapshot = snapshot or page["snapshot"]
        assert page["snapshot"] == snapshot
        assert not ({row["anchor"] for row in rows}
                    & {row["anchor"] for row in combined})
        combined = rows + combined
        if previous_older is not None:
            assert page["older_episodes"] < previous_older
        previous_older = page["older_episodes"]

        if not page["has_older"]:
            assert page["next_before"] is None
            assert page["older_episodes"] == 0
            break
        assert page["next_before"] == rows[0]["anchor"]
        cursor = page["next_before"]

    assert combined == whole
    first_repair = next(row for row in combined
                        if row["label"] == "inline_repair" and row["ordinal"] == 1)
    seeked = client.get("/api/runs/demo/nodes/0/conversation",
                        params={"before": first_repair["anchor"]})
    assert seeked.status_code == 200
    assert seeked.json()["stages"][-1]["ordinal"] == 1


def test_episode_page_snapshot_excludes_a_new_live_tail(tmp_path):
    """A live append after page one must not move page two under the operator."""
    pytest.importorskip("fastapi")
    client, _spans = _repaired_node(tmp_path, repairs=SMALL_REPAIRS)
    first = client.get("/api/runs/demo/nodes/0/episodes", params={"limit": 7}).json()
    page = first["page"]

    late = [
        {"trace_id": "t", "span_id": "late-repair-gen", "parent_id": "late-repair",
         "run_id": "demo", "name": "generation", "kind": "generation", "start": 999_001.0,
         "duration_s": 1.0, "status": "OK",
         "attributes": {"node_id": 0, "phase": "inline_repair",
                        "phase_span": "late-repair", "output": "late"}, "events": []},
        {"trace_id": "t", "span_id": "late-repair", "parent_id": "root", "run_id": "demo",
         "name": "inline_repair", "kind": "operation", "start": 999_000.0,
         "duration_s": 2.0, "status": "OK",
         "attributes": {"node_id": 0, "attempt": SMALL_REPAIRS + 1}, "events": []},
    ]
    with (tmp_path / "demo" / "spans.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("".join(json.dumps(row) + "\n" for row in late))

    older = client.get("/api/runs/demo/nodes/0/episodes", params={
        "limit": 7, "before": page["next_before"], "snapshot": page["snapshot"],
    })
    assert older.status_code == 200
    body = older.json()
    assert body["page"]["snapshot"] == page["snapshot"]
    assert body["projection"]["total_episodes"] == first["projection"]["total_episodes"]
    assert "late-repair" not in {row["anchor"] for row in body["episodes"]}


def test_episode_page_cursor_is_scoped_to_an_episode_of_that_node_lifecycle(tmp_path):
    pytest.importorskip("fastapi")
    client, _spans = _repaired_node(tmp_path, repairs=SMALL_REPAIRS)
    first_page = client.get("/api/runs/demo/nodes/0/episodes", params={"limit": 7}).json()
    snapshot = first_page["page"]["snapshot"]

    # `implement-gen` is a real indexed span in this node and lifecycle, but not an episode boundary.
    # A run-global index check would accept it and return a plausible wrong page; the episode map must
    # validate the cursor against the exact stage population it is paging.
    for cursor in ("implement-gen", "no-such-span", "x" * 500):
        response = client.get("/api/runs/demo/nodes/0/episodes",
                              params={"limit": 7, "before": cursor, "snapshot": snapshot})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "trace_episode_cursor_unknown"
        assert response.json()["detail"]["cursor_field"] == "before"

    response = client.get("/api/runs/demo/nodes/0/episodes",
                          params={"limit": 7, "before": first_page["page"]["next_before"],
                                  "snapshot": "implement-gen"})
    assert response.status_code == 409
    assert response.json()["detail"]["cursor_field"] == "snapshot"

    for limit in (0, 10001):
        assert client.get("/api/runs/demo/nodes/0/episodes",
                          params={"limit": limit}).status_code == 422


def test_the_first_repair_is_unreachable_by_widening_and_reachable_by_anchoring(tmp_path):
    """The defect and its fix, in one test, on one node.

    The window here is deliberately the SMALL one a client asks for by default; the node is larger
    than it. Widening to the ceiling is the control that existed, and on the real node it still could
    not reach the beginning — this reproduces that at 1/100 scale so the property is stated where it
    can be driven rather than only measured.
    """
    pytest.importorskip("fastapi")
    client, span_count = _repaired_node(tmp_path)
    assert span_count > TRACE_NODE_SPAN_CAP_MAX, "the widen ladder must run out before the node does"

    tail = client.get("/api/runs/demo/nodes/0/conversation").json()
    assert tail["before"] is None
    assert tail["projection"]["omitted_spans"] > 0
    # The tail holds the NEWEST repairs and cannot hold the first one at this window…
    tail_ordinals = [stage.get("ordinal") for stage in tail["stages"]]
    assert max(o for o in tail_ordinals if o) == REPAIRS
    assert 1 not in tail_ordinals

    # …and widening does not change WHICH end it holds. Every wider read is the same tail extended,
    # so the first repair arrives only once the window is big enough for the WHOLE node — which on
    # the node this test is modelled on is 14,507 spans, three times the ceiling.
    wider = client.get("/api/runs/demo/nodes/0/conversation",
                       params={"limit": TRACE_NODE_SPAN_CAP_MAX}).json()
    assert [stage.get("ordinal") for stage in wider["stages"]][-1] == REPAIRS
    assert 1 not in [stage.get("ordinal") for stage in wider["stages"]]

    episodes = client.get("/api/runs/demo/nodes/0/episodes").json()["episodes"]
    first_repair = next(row for row in episodes
                        if row["label"] == "inline_repair" and row["ordinal"] == 1)

    seeked = client.get("/api/runs/demo/nodes/0/conversation",
                        params={"before": first_repair["anchor"]}).json()
    # The window ENDS at the chosen episode: repair #1 is the last band, and the newest repair — the
    # only one the tail could show — is not in it at all.
    assert seeked["before"] == first_repair["anchor"]
    assert _labels(seeked)[-1] == "inline_repair"
    assert seeked["stages"][-1]["ordinal"] == 1
    assert REPAIRS not in [stage.get("ordinal") for stage in seeked["stages"]]
    # It reads FORWARD from the beginning of the node, which is what "show me where it first broke"
    # means: the build, then the first crash, then the first repair.
    assert _labels(seeked)[:3] == ["implement", "triage", "inline_repair"]
    # Same window, same cost. The anchor moves the window; it never enlarges it.
    assert seeked["projection"]["visible_spans"] <= tail["projection"]["visible_spans"]
    # And the omission receipt still describes the NODE, not the part of it behind the anchor — an
    # operator reading "12 of 12" while 100 spans exist would think they had the whole thing.
    assert seeked["projection"]["total_spans"] == tail["projection"]["total_spans"] == span_count

    # The span-tree twin of the same window answers about the same anchor, because they are two
    # views of ONE surface and a picker that moved only one of them would be worse than none.
    tree = client.get("/api/runs/demo/nodes/0/trace",
                      params={"before": first_repair["anchor"]}).json()
    assert tree["before"] == first_repair["anchor"]
    ids = json.dumps(tree["nodes"])
    assert "repair-gen-1" in ids and f"repair-gen-{REPAIRS}" not in ids


def test_an_anchor_the_server_cannot_place_is_refused_not_answered_with_the_tail(tmp_path):
    """Silently ignoring an anchor is the one failure worse than refusing it.

    The surface labels its window with the episode the operator picked. Answering that with the
    node's newest spans is a caption that lies, and it lies in the direction the operator is least
    able to detect — they asked for the OLDEST steps and would be reading the newest.
    """
    pytest.importorskip("fastapi")
    client, _spans = _repaired_node(tmp_path, repairs=SMALL_REPAIRS)

    for suffix in ("/trace", "/conversation"):
        for bogus in ("no-such-span", "x" * 500, "../../etc/passwd"):
            response = client.get(f"/api/runs/demo/nodes/0{suffix}", params={"before": bogus})
            assert response.status_code == 409, (suffix, bogus)
            assert response.json()["detail"]["code"] == "trace_anchor_unknown"
        # A blank anchor is ABSENT, not malformed: it is what "back to the latest steps" sends.
        for blank in ("", "   "):
            response = client.get(f"/api/runs/demo/nodes/0{suffix}", params={"before": blank})
            assert response.status_code == 200, (suffix, repr(blank))
            assert response.json()["before"] is None


def test_a_real_anchor_from_another_node_or_lifecycle_is_still_refused(tmp_path):
    """Run-global span membership is not enough to caption a per-node seek.

    File row numbers are global. Before the node/lifecycle fence, either valid id below passed the
    route check and `_anchored` cut node 0's rows at that foreign offset, returning a plausible wrong
    window instead of the selected episode. Both trace readings must reject the same two identities.
    """
    pytest.importorskip("fastapi")
    client, _spans = _repaired_node(tmp_path, repairs=SMALL_REPAIRS)
    foreign = [
        {
            "trace_id": "node-one-trace", "span_id": "node-one-anchor", "parent_id": None,
            "run_id": "demo", "name": "create_node", "kind": "operation", "start": 9999.0,
            "duration_s": 1.0, "status": "OK",
            "attributes": {"node_id": 1, "generation": 0}, "events": [],
        },
        {
            "trace_id": "node-zero-next-lifecycle", "span_id": "attempt-one-anchor",
            "parent_id": None, "run_id": "demo", "name": "create_node", "kind": "operation",
            "start": 10000.0, "duration_s": 1.0, "status": "OK",
            "attributes": {"node_id": 0, "generation": 1}, "events": [],
        },
    ]
    with (tmp_path / "demo" / "spans.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("".join(json.dumps(row) + "\n" for row in foreign))

    for suffix in ("/trace", "/conversation"):
        for anchor in ("node-one-anchor", "attempt-one-anchor"):
            response = client.get(
                f"/api/runs/demo/nodes/0{suffix}", params={"attempt": 0, "before": anchor})
            assert response.status_code == 409, (suffix, anchor, response.text)
            assert response.json()["detail"]["code"] == "trace_anchor_unknown"


def test_a_validator_never_carries_one_anchor_s_body_to_another(tmp_path):
    """The conditional read is per REPRESENTATION, and an anchor is part of one.

    Without this the seek is worse than useless on a live node: the tail's cursor is in hand, the
    seek re-sends it, and a 304 hands back the tail the operator was seeking away from — under the
    chosen episode's label, with no request in flight to correct it.
    """
    pytest.importorskip("fastapi")
    client, _spans = _repaired_node(tmp_path)
    episodes = client.get("/api/runs/demo/nodes/0/episodes").json()["episodes"]
    first_repair = next(row for row in episodes
                        if row["label"] == "inline_repair" and row["ordinal"] == 1)

    tail = client.get("/api/runs/demo/nodes/0/conversation")
    cursor = tail.json()["cursor"]
    assert cursor, "a stable snapshot must publish its validator or this test proves nothing"
    # The same request re-validates…
    again = client.get("/api/runs/demo/nodes/0/conversation",
                       headers={"If-None-Match": cursor})
    assert again.status_code == 304
    # …and the anchored one must NOT, however identical everything else about it is.
    seeked = client.get("/api/runs/demo/nodes/0/conversation",
                        params={"before": first_repair["anchor"]},
                        headers={"If-None-Match": cursor})
    assert seeked.status_code == 200
    assert seeked.json()["before"] == first_repair["anchor"]
    assert seeked.json()["cursor"] != cursor


def test_the_default_window_renders_every_band_it_read(tmp_path):
    """The other half of F6: the reasoning was truncated by the render caps, not by the window.

    On the measured node a 512-span window derived 256 stages and 425 turns and rendered 64 and 105 —
    the server paid 3.4 ms/span to read evidence it then dropped. The caps are now the window's own
    arithmetic bound, so `omitted_stages > 0` can only ever mean "move or widen the span window".
    """
    pytest.importorskip("fastapi")
    client, span_count = _repaired_node(tmp_path, repairs=SMALL_REPAIRS)
    assert span_count < TRACE_CONVERSATION_SPAN_CAP   # the window is NOT what binds here

    whole = client.get("/api/runs/demo/nodes/0/conversation").json()
    assert whole["projection"]["omitted_spans"] == 0
    assert whole["projection"]["omitted_stages"] == 0
    assert whole["projection"]["omitted_turns"] == 0
    assert len(whole["stages"]) == 2 * SMALL_REPAIRS + 1   # every triage, every repair, the build
    assert whole["stages"][-1]["ordinal"] == SMALL_REPAIRS


def test_the_map_and_the_window_agree_about_what_a_step_is(tmp_path):
    """The map IS `_conversation_bands`, and this is what that buys.

    A second reading of "what are this node's steps" would drift — the failure `traceSurfaceModel.js`
    records for the card trace, where a surface named a trace the node's own rollup could not
    describe. So every episode the map offers must be a band the conversation actually renders when
    the window is placed on it.
    """
    pytest.importorskip("fastapi")
    client, _spans = _repaired_node(tmp_path, repairs=SMALL_REPAIRS)
    episodes = client.get("/api/runs/demo/nodes/0/episodes").json()["episodes"]
    whole = client.get("/api/runs/demo/nodes/0/conversation").json()

    assert [row["label"] for row in episodes] == [stage["label"] for stage in whole["stages"]]
    assert [row["ordinal"] for row in episodes] == [stage["ordinal"] for stage in whole["stages"]]
    # And each map row's anchor is the band's own identity in the rendered conversation.
    assert [row["band"] for row in episodes] == [stage["band"] for stage in whole["stages"]]

    # Seeking to an arbitrary middle episode lands on exactly that band, not near it.
    middle = episodes[len(episodes) // 2]
    seeked = client.get("/api/runs/demo/nodes/0/conversation",
                        params={"before": middle["anchor"]}).json()
    assert seeked["stages"][-1]["band"] == middle["band"]


# =========== the 2026-08-18 review finding: the snapshot cursor a LIVE node invalidated

def _live_node_client(tmp_path, *, extra_content: int = 0, close_band: bool = False):
    """A node whose newest band is OPEN — its operation span has not flushed yet, which is exactly
    the state the operator is in while they watch a node work."""
    from fastapi.testclient import TestClient

    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app

    run_dir = tmp_path / "demo"
    run_dir.mkdir(exist_ok=True)          # the same tmp_path is re-seeded to model the run moving on
    EventStore(run_dir / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "task", "goal": "g", "direction": "min"})
    rows = [{
        "trace_id": "t", "span_id": "root", "parent_id": None, "run_id": "demo",
        "name": "create_node", "kind": "operation", "start": 0.0, "duration_s": 1.0,
        "status": "OK", "attributes": {"node_id": 0, "generation": 0}, "events": [],
    }]

    def gen(span_id, parent, start, phase, text):
        rows.append({
            "trace_id": "t", "span_id": span_id, "parent_id": parent, "run_id": "demo",
            "name": "generation", "kind": "generation", "start": start, "duration_s": 1.0,
            "status": "OK",
            "attributes": {"node_id": 0, "phase": phase, "phase_span": parent,
                           "input": [{"role": "user", "content": "go"}], "output": text},
            "events": [],
        })

    # one CLOSED band, so there is something to page back to
    gen("done-gen", "done", 2.0, "implement", "the first code")
    rows.append({
        "trace_id": "t", "span_id": "done", "parent_id": "root", "run_id": "demo",
        "name": "implement", "kind": "operation", "start": 1.0, "duration_s": 5.0, "status": "OK",
        "attributes": {"node_id": 0}, "events": [],
    })
    # ...and one OPEN band: its children are on disk, its own operation span is NOT (yet)
    for index in range(1 + extra_content):
        gen(f"live-gen-{index}", "live", 10.0 + index, "inline_repair", f"working {index}")
    if close_band:
        rows.append({
            "trace_id": "t", "span_id": "live", "parent_id": "root", "run_id": "demo",
            "name": "inline_repair", "kind": "operation", "start": 9.0, "duration_s": 5.0,
            "status": "OK", "attributes": {"node_id": 0, "attempt": 1}, "events": [],
        })
    (run_dir / "spans.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return TestClient(make_app(tmp_path))


def test_a_snapshot_survives_an_append_into_the_open_band(tmp_path):
    """A band's `anchor` is its operation span only once that span has FLUSHED; an open band falls
    back to its latest CONTENT span, which moves on every append. So the snapshot a live node handed
    back on page 2 matched no band, and backward paging 409ed on every walk — on exactly the live
    node this feature was built for."""
    first = _live_node_client(tmp_path).get(
        "/api/runs/demo/nodes/0/episodes", params={"limit": 1}).json()
    snapshot = first["page"]["snapshot"]
    assert snapshot

    # the run keeps working: more generations land inside the still-open band
    grown = _live_node_client(tmp_path, extra_content=3)
    page = grown.get("/api/runs/demo/nodes/0/episodes",
                     params={"limit": 1, "snapshot": snapshot})
    assert page.status_code == 200, page.text


def test_a_snapshot_survives_the_band_closing(tmp_path):
    """The other direction, and the same cursor: when the operation span finally flushes, the band's
    anchor changes from its content-span fallback to the op span id."""
    first = _live_node_client(tmp_path).get(
        "/api/runs/demo/nodes/0/episodes", params={"limit": 1}).json()
    snapshot = first["page"]["snapshot"]

    closed = _live_node_client(tmp_path, close_band=True)
    page = closed.get("/api/runs/demo/nodes/0/episodes",
                      params={"limit": 1, "snapshot": snapshot})
    assert page.status_code == 200, page.text


def test_a_cursor_that_names_no_band_at_all_is_still_refused(tmp_path):
    """The fix widens what MATCHES, never what is accepted: an unplaceable cursor must still 409
    rather than be answered with the tail."""
    client = _live_node_client(tmp_path)
    page = client.get("/api/runs/demo/nodes/0/episodes",
                      params={"limit": 1, "snapshot": "no-such-span"})
    assert page.status_code == 409, page.text
