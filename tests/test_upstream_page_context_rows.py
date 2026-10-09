"""The status page's history carries CONTEXT rows beside the lane's receipts (review 2026-10-09).

`engine/upstream.py::UpstreamLane.read` pages every `upstream_`-prefixed row, and three of them answer
no agent's write: the kill switch (`upstream_auto_set`) and the live engine's notice to its Developers
(`upstream_hint_issued`, `upstream_hint_delivered`). The harness's page check
(`harness/upstream_receipts.py::page_detail`) held each to a receipt's identities and refused the whole
page, so ONE switch from the CLI or the UI made every later MCP status read `invalid_upstream_page`.
"""
import httpx
import pytest

from looplab.events.types import (EV_UPSTREAM_AUTO_SET, EV_UPSTREAM_HINT_DELIVERED,
                                  EV_UPSTREAM_HINT_ISSUED)
from looplab.harness.mcp_server import HarnessAPI
from looplab.harness.upstream_receipts import CONTEXT_ROWS, page_detail
from tests.test_upstream_lane import fixture


def _with_context_rows(tmp_path):
    lane, store, generation, _ = fixture(tmp_path)
    store.append(EV_UPSTREAM_AUTO_SET, {"enabled": False, "reason": "review"})
    store.append(EV_UPSTREAM_HINT_ISSUED, {"hint_id": "h1", "proposal_id": "up_" + "0" * 24,
                                           "advance_seq": 1, "kind": "fix", "text": "t", "sessions": []})
    store.append(EV_UPSTREAM_HINT_DELIVERED, {"hint_id": "h1", "session": "s1"})
    store.append(EV_UPSTREAM_AUTO_SET, {"enabled": True})
    return lane, store, generation


def _status(page, generation):
    api = HarnessAPI("http://localhost",
                     transport=httpx.MockTransport(lambda _: httpx.Response(200, json=page)))
    try:
        return api.upstream_status("run", generation)
    finally:
        api.client.close()


def test_the_three_context_types_are_the_registry_constants():
    assert CONTEXT_ROWS == {EV_UPSTREAM_AUTO_SET, EV_UPSTREAM_HINT_ISSUED, EV_UPSTREAM_HINT_DELIVERED}


def test_a_page_carrying_the_switch_and_the_hints_stays_readable(tmp_path):
    lane, store, generation = _with_context_rows(tmp_path)
    page = lane.read(generation)
    assert {r["type"] for r in page["history"]} >= CONTEXT_ROWS, "the page carries all three"
    assert page_detail(page)
    result = _status(page, generation)
    assert result["status"] == 200 and not result.get("code"), result


@pytest.mark.parametrize("fault", ["seq", "order", "receipt_shape"])
def test_a_context_row_does_not_loosen_the_page_checks(tmp_path, fault):
    """A context row is read for its POSITION only: a non-integer seq or an out-of-order one still
    refuses the page, and a row of a RECEIPT type is still held to its identities."""
    lane, store, generation = _with_context_rows(tmp_path)
    page = lane.read(generation)
    rows = [r for r in page["history"] if r["type"] in CONTEXT_ROWS]
    if fault == "seq":
        rows[0]["seq"] = "7"
    elif fault == "order":
        rows[-1]["seq"] = rows[0]["seq"]
    else:
        rows[0]["type"] = "upstream_proposed"       # a receipt type with no receipt identities
    assert not page_detail(page)
    result = _status(page, generation)
    assert result["outcome"] == "unavailable" and "body" not in result
