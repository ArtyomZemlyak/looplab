"""Filtered and paged candidate replies must bind the explicit read request."""
import httpx
import pytest

from looplab.harness.mcp_server import HarnessAPI
from tests.test_upstream_lane import fixture


@pytest.mark.parametrize("fault", ["offset", "limit", "source", "mixed_source", "next", "missing", "partial"])
def test_unbound_candidate_page_is_unavailable(tmp_path, fault):
    lane, store, generation, _ = fixture(tmp_path)
    page = lane.read(generation, source_node_id=0, candidate_limit=1)
    candidates = page["candidates"]
    if fault == "offset":
        candidates["offset"] = 1
    elif fault == "limit":
        candidates["limit"] = 2
    elif fault == "source":
        candidates["source_node_id"] = 1
    elif fault == "mixed_source":
        candidates["rows"][0]["node_id"] = 1
    elif fault == "next":
        candidates["next_offset"] = 2
    elif fault == "missing":
        for key in ("offset", "next_offset", "source_node_id"):
            candidates.pop(key)
    else:
        candidates.pop("next_offset")
    before = store.path.read_bytes()
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=page)))
    result = api.upstream_status("run", generation, source_node_id=0, candidate_limit=1)
    assert result["outcome"] == "unavailable" and "body" not in result
    assert store.path.read_bytes() == before
    api.client.close()
