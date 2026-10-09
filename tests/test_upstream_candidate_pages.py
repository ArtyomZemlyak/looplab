"""A bounded advice page must not hide later measured upstream sources."""
import httpx
from fastapi.testclient import TestClient

from benchmarks._upstream_sgd import TRAIN, SOURCE, GENERAL
from looplab.harness.mcp_server import HarnessAPI
from looplab.serve.server import make_app
from looplab.tools.upstream_tools import UpstreamTools
from tests.test_upstream_lane import fixture
from tests.test_upstream_multibase import create, evaluate


def crowded(tmp_path):
    # Distinct unchanged context keeps 201 genuine code diffs in separate hunks.
    # Both nodes execute real SGD; these diagnostic constants do not alter scores.
    old = "".join(f"OPTION_{i} = 0\n" + "".join(f"# context {i} {j}\n" for j in range(7)) for i in range(201))
    changed = old.replace(" = 0\n", " = 1\n")
    # LF throughout: `fixture` writes the base as the bytes it is given (it used `write_text`, CRLF on
    # Windows, which these sources once matched with `os.linesep`).
    lane, store, generation, body = fixture(tmp_path, base_train=TRAIN + old,
        source_files={"train.py": SOURCE + changed, "recipe.env": "MOMENTUM=0.2\n"})
    create(store, 1, {"train.py": SOURCE + old, "recipe.env": "MOMENTUM=0.2\n"})
    evaluate(lane, store, 1)
    target = {**body, "source_node_id": 1, "hunk_hashes": body["hunk_hashes"][:1],
        "files": {**body["files"], "train.py": GENERAL + old}}
    return lane, store, generation, target


def test_later_measured_source_can_propose_check_and_advance(tmp_path):
    lane, store, generation, body = crowded(tmp_path)
    initial = lane.read(generation)["candidates"]
    assert initial["bounded"] and len(initial["rows"]) == 200
    assert {r["node_id"] for r in initial["rows"]} == {0}
    proposed = lane.propose(body)
    check = {"expected_generation": generation, "action_id": "later:check", "proposal_id": proposed["proposal_id"]}
    verdict = lane.check(check)
    assert verdict["status"] == "succeeded" and len(verdict["result"]["executions"]) == 7
    before = store.path.read_bytes()
    assert lane.propose(body) == proposed and lane.check(check) == verdict
    assert store.path.read_bytes() == before
    advanced = lane.advance({**check, "action_id": "later:advance",
        "expected_base_revision": body["expected_base_revision"], "evidence_token": verdict["evidence_token"]})
    assert advanced["source_node_id"] == 1


def test_http_mcp_and_assistant_can_read_all_hunks_without_writes(tmp_path, monkeypatch):
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    lane, store, generation, body = crowded(tmp_path)
    before = store.path.read_bytes()
    with TestClient(make_app(tmp_path)) as client:
        def bridge(request):
            reply = client.request(request.method, str(request.url), content=request.content, headers=request.headers)
            return httpx.Response(reply.status_code, content=reply.content)
        api = HarnessAPI("http://localhost", transport=httpx.MockTransport(bridge))
        rows, offset = [], 0
        while True:
            reply = api.upstream_status("run", generation, candidate_offset=offset, candidate_limit=60)
            assert reply["status"] == 200, reply
            page = reply["body"]["candidates"]
            assert page["offset"] == offset and page["limit"] == 60
            rows.extend(page["rows"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        assert len(rows) > 200 and {r["node_id"] for r in rows} == {0, 1}
        assert len({(r["node_id"], r["hunk_hash"]) for r in rows}) == len(rows)
        reply = api.upstream_status("run", generation, source_node_id=1, candidate_offset=1, candidate_limit=1)
        assert reply["status"] == 200 and reply["body"]["candidates"]["rows"][0]["hunk_hash"] == body["hunk_hashes"][0]
        api.client.close()
    tool = UpstreamTools(tmp_path, mode="plan")
    reply = tool.execute("upstream_status", {"run_id": "run", "expected_generation": generation,
        "source_node_id": 1, "candidate_limit": 1, "limit": 1})
    assert not reply.is_error and {r["node_id"] for r in reply.structured["candidates"]["rows"]} == {1}
    assert store.path.read_bytes() == before
