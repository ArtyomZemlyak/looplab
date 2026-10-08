"""Semantic recovery retains exact evidence, never a fresh server approval."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import subprocess
import sys

import anyio
import httpx
import pytest

from looplab.harness.client_requests import ACTION_ROUTES, ACTION_SCOPE_FIELDS
from looplab.harness.mcp_server import HarnessAPI, build_server
from tests.test_harness_client_requests import GEN, TOKEN, URL, api


def restore(client, request_id, generation=GEN, size=53):
    raw, offset, digest = b"", 0, None
    while True:
        page = client.saved_action("demo", generation, request_id, offset, size, digest)
        assert page["source"] == "client" and page["server_effects"] == "unobserved"
        chunk = base64.b64decode(page["chunk"], validate=True)
        assert hashlib.sha256(chunk).hexdigest() == page["chunk_sha256"]
        raw += chunk
        digest = page["request_sha256"]
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert hashlib.sha256(raw).hexdigest() == digest
    return json.loads(raw)


@pytest.mark.parametrize("suffix", sorted(ACTION_ROUTES))
def test_every_supported_action_saved_before_http_and_recovered_after_lost_reply(tmp_path, suffix):
    body = {"expected_generation": GEN, "action_id": "original",
            "reason": "Исходное решение", "expected_evidence_revision": "b" * 64,
            # A narrower server scope is part of the identity (a checkpoint answer).
            **{field: "c" * 32 for field in ACTION_SCOPE_FIELDS.get(suffix, ())}}
    path = "/api/runs/demo/" + suffix
    def lost(request):
        row, = list(tmp_path.rglob("act_*.json"))
        record = json.loads(row.read_bytes())
        assert record["request"]["body"] == json.loads(request.content) == body
        assert record["request"]["path"] == path
        assert TOKEN.encode() not in row.read_bytes()
        raise httpx.ReadTimeout("lost private reply", request=request)
    first = api(tmp_path, lost)
    result = first.request("POST", path, body)
    assert result["outcome"] == "unknown"
    first.client.close()
    second = api(tmp_path, lambda _: pytest.fail("local recovery made HTTP"))
    row, = second.saved_actions("demo", GEN)["items"]
    assert row["action_id"] == "original" and row["path"] == path
    assert row["request_id"] == result["client_request"]["request_id"]
    original = restore(second, row["request_id"])
    assert original == {"method": "POST", "path": path, "body": body, "idempotency_key": ""}
    assert second.saved_commands("demo", GEN)["total"] == 0


@pytest.mark.parametrize("change", ["revision", "body", "key", "upstream_operation"])
def test_changed_original_action_is_refused_without_overwriting_or_http(tmp_path, change):
    seen = []
    client = api(tmp_path, lambda r: (seen.append(r), httpx.Response(200, json={}))[1])
    suffix = "upstream/check" if change == "upstream_operation" else "harness-selection/values"
    path = "/api/runs/demo/" + suffix
    body = {"expected_generation": GEN, "action_id": "original", "expected_evidence_revision": "b" * 64}
    result = client.request("POST", path, body, "header")
    row, = list(tmp_path.rglob("act_*.json"))
    before = row.read_bytes()
    changed, key = dict(body), "header"
    if change == "revision": changed["expected_evidence_revision"] = "c" * 64
    if change == "body": changed["estimates"] = []
    if change == "key": key = "another"
    if change == "upstream_operation": path = "/api/runs/demo/upstream/advance"
    refusal = client.request("POST", path, changed, key)
    assert refusal["outcome"] == "not_sent" and len(seen) == 1 and row.read_bytes() == before
    assert restore(client, result["client_request"]["request_id"])["body"] == body


@pytest.mark.parametrize("bad", ["duplicate", "version", "hash", "path", "action_id", "generation", "server", "directory"])
def test_damaged_action_source_refuses_read_and_same_identity_write(tmp_path, bad):
    client = api(tmp_path, lambda _: httpx.Response(200, json={}))
    path = "/api/runs/demo/harness-reviews"
    body = {"expected_generation": GEN, "action_id": "original"}
    result = client.request("POST", path, body)
    row, = list(tmp_path.rglob("act_*.json"))
    record = json.loads(row.read_bytes())
    if bad == "duplicate":
        row.write_text('{"version":1,"version":1}')
    elif bad == "directory":
        row.unlink()
        row.mkdir()
    else:
        if bad == "version": record["version"] = True
        if bad == "hash": record["request"]["body"]["reason"] = "changed"
        if bad == "path": record["request"]["path"] += "?alias=1"
        if bad == "action_id": record["request"]["body"]["action_id"] = "other"
        if bad == "generation": record["generation"] = "c" * 64
        if bad == "server": record["server"] = "http://other/"
        row.write_text(json.dumps(record))
    client.client.close()
    second = api(tmp_path, lambda _: pytest.fail("corrupt source sent HTTP"))
    assert second.saved_actions("demo", GEN)["outcome"] == "unavailable"
    assert second.saved_action("demo", GEN, result["client_request"]["request_id"])["outcome"] == "unavailable"
    assert second.request("POST", path, body)["outcome"] == "not_sent"


@pytest.mark.parametrize("bad", ["generation", "missing_id", "blank_id", "token", "query", "encoded_path", "header"])
def test_invalid_original_refuses_before_http(tmp_path, bad):
    client = api(tmp_path, lambda _: pytest.fail("invalid original sent"))
    body, path, key = {"expected_generation": GEN, "action_id": "original"}, "/api/runs/demo/harness-reviews", ""
    if bad == "generation": body["expected_generation"] = "bad"
    if bad == "missing_id": body.pop("action_id")
    if bad == "blank_id": body["action_id"] = " original "
    if bad == "token": body["reason"] = TOKEN
    if bad == "query": path += "?other=1"
    if bad == "encoded_path": path = "/api/runs/demo/%68arness-reviews"
    if bad == "header": key = "invalid\nheader"
    assert client.request("POST", path, body, key)["outcome"] == "not_sent"
    assert not list(tmp_path.rglob("act_*.json"))


def test_mixed_records_paging_scopes_limits_and_hash_fence(tmp_path, monkeypatch):
    import looplab.harness.client_requests as module
    client = api(tmp_path, lambda _: httpx.Response(200, json={}))
    body = {"expected_generation": GEN, "action_id": "one"}
    path = "/api/runs/demo/harness-reviews"
    result = client.request("POST", path, body)
    client.request("POST", "/api/runs/demo/commands", {"expected_generation": GEN, "type": "run_pause"}, "key")
    client.request("POST", path, {**body, "action_id": "two"})
    rows = client.saved_actions("demo", GEN, limit=1)
    assert rows["total"] == 2 and rows["next_offset"] == 1 and len(rows["items"]) == 1
    assert client.saved_actions("demo", GEN, offset=1)["next_offset"] is None
    assert client.saved_commands("demo", GEN)["total"] == 1
    assert client.saved_actions("demo", "b" * 64)["total"] == 0
    other = api(tmp_path, lambda _: pytest.fail("local read contacted different server"), url="http://other/")
    assert other.saved_actions("demo", GEN)["total"] == 0
    identity = result["client_request"]["request_id"]
    assert client.saved_action("demo", GEN, identity, offset=1)["outcome"] == "unavailable"
    assert client.saved_action("demo", GEN, identity, expected_request_hash="b" * 64)["outcome"] == "unavailable"
    monkeypatch.setattr(module, "MAX_RECORDS", 2)
    assert client.request("POST", path, {**body, "action_id": "three"})["outcome"] == "not_sent"
    assert client.request("POST", path, body)["status"] == 200


def test_concurrent_clients_keep_one_original(tmp_path):
    clients = [api(tmp_path, lambda _: httpx.Response(200, json={})) for _ in range(2)]
    body = {"expected_generation": GEN, "action_id": "original"}
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda c: c.request("POST", "/api/runs/demo/harness-reviews", body), clients))
    assert any(r.get("status") == 200 for r in results)
    assert all(r.get("status") == 200 or r.get("outcome") == "not_sent" for r in results)
    row, = list(tmp_path.rglob("act_*.json"))
    assert json.loads(row.read_bytes())["request"]["body"] == body


@pytest.mark.parametrize("failure", ["publish", "lock", "contended"])
def test_required_durability_failure_refuses_before_http(tmp_path, monkeypatch, failure):
    import looplab.harness.client_requests as module
    from looplab.events.eventstore import EventStoreLockError, InterprocessLockContended
    client = api(tmp_path, lambda _: pytest.fail("unconfirmed action sent"))
    if failure == "publish":
        def broken(*_):
            raise OSError("private path")
        monkeypatch.setattr(module, "strict_atomic_write_bytes", broken)
    else:
        @contextmanager
        def broken(path, **kwargs):
            assert kwargs == {"required": True, "blocking": False}
            if failure == "contended": raise InterprocessLockContended(path)
            raise EventStoreLockError(path, OSError("private path"))
            yield
        monkeypatch.setattr(module, "interprocess_lock", broken)
    result = client.request("POST", "/api/runs/demo/harness-reviews", {"expected_generation": GEN, "action_id": "original"})
    assert result["outcome"] == "not_sent" and result["code"] == "client_request_unavailable"
    assert "private path" not in str(result)


@pytest.mark.parametrize("run_id", ["демо", "%41"])
def test_action_uses_exact_wire_path_for_unicode_and_literal_percent_run(tmp_path, run_id):
    from urllib.parse import quote
    seen = []
    client = api(tmp_path, lambda r: (seen.append(r), httpx.Response(200, json={}))[1])
    path = f"/api/runs/{quote(run_id, safe='')}/harness-reviews"
    result = client.request("POST", path, {"expected_generation": GEN, "action_id": "original"})
    page = client.saved_action(run_id, GEN, result["client_request"]["request_id"])
    original = json.loads(base64.b64decode(page["chunk"]))
    assert original["path"] == path
    assert seen[0].url.raw_path.decode().endswith(path)


def test_action_freezes_caller_body_before_http(tmp_path, monkeypatch):
    from looplab.harness.client_requests import ClientActions
    body = {"expected_generation": GEN, "action_id": "original", "estimates": [{"value": 0.2}]}
    save = ClientActions.save_action
    def mutate(self, *args, **kwargs):
        row = save(self, *args, **kwargs)
        body["estimates"][0]["value"] = 1
        return row
    monkeypatch.setattr(ClientActions, "save_action", mutate)
    client = api(tmp_path, lambda r: httpx.Response(200, json=json.loads(r.content)))
    result = client.request("POST", "/api/runs/demo/harness-selection/values", body)
    assert result["body"]["estimates"][0]["value"] == 0.2
    assert restore(client, result["client_request"]["request_id"])["body"] == result["body"]


def test_typed_upstream_unknown_ack_keeps_local_intent_without_untrusted_body(tmp_path):
    client = api(tmp_path, lambda _: httpx.Response(200, json={"bad": "not a receipt"}))
    body = {"expected_generation": GEN, "action_id": "original", "proposal_id": "up_" + "b" * 24}
    result = client.upstream_write("demo", "check", body)
    assert result["outcome"] == "unknown" and result["reason"] == "invalid_upstream_receipt"
    assert "body" not in result
    assert restore(client, result["client_request"]["request_id"])["body"] == body


def test_lost_real_value_reply_after_reset_recovers_old_batch_without_refreshing_approval(tmp_path):
    from tests.test_external_selection import _session
    rd, store, server, generation = _session(tmp_path, policy="mcts", mcts_value_weight=0.4)
    selection = "/api/runs/demo/harness-selection"
    observed = server.get(selection, params={"expected_generation": generation}).json()
    body = {"expected_generation": generation, "action_id": "original",
            "expected_evidence_revision": observed["evidence_revision"],
            "estimates": [{"node_id": n["node_id"], "generation": n["generation"], "value": .2,
                           "rationale": "Several promising next changes remain"} for n in observed["value_candidates"]]}
    responses, posts = [], []
    def bridge(request):
        response = server.request(request.method, request.url.raw_path.decode(), content=request.content,
                                  headers={"Content-Type": "application/json"})
        if request.method == "POST":
            posts.append(request)
            assert response.status_code == 200, response.text
            responses.append(response.json())
            if len(responses) == 1: raise httpx.ReadTimeout("lost value reply", request=request)
        return httpx.Response(response.status_code, content=response.content)
    first = api(tmp_path / "client", bridge, url="http://testserver/")
    assert first.request("POST", selection + "/values", body)["outcome"] == "unknown"
    first.client.close()
    store.append("node_reset", {"node_id": 0, "from_stage": "eval"})
    store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": .8})
    current = server.get(selection, params={"expected_generation": generation}).json()
    assert current["evidence_revision"] != body["expected_evidence_revision"] and current["value_candidates"]
    second = api(tmp_path / "client", bridge, url="http://testserver/")
    row, = second.saved_actions("demo", generation)["items"]
    original = restore(second, row["request_id"], generation)
    assert original["body"] == body and len(posts) == 1
    before = (rd / "events.jsonl").read_bytes()
    replay = second.request(original["method"], original["path"], original["body"])
    assert replay["body"]["replayed"] and (rd / "events.jsonl").read_bytes() == before
    # Values return a minimal replay acknowledgement, not a newly stamped batch.
    assert responses[0]["ok"] and responses[0]["count"] == 2 and responses[1]["ok"]
    assert server.get(selection, params={"expected_generation": generation}).json()["value_candidates"]
    changed = {**body, "expected_evidence_revision": current["evidence_revision"]}
    assert second.request("POST", selection + "/values", changed)["outcome"] == "not_sent" and len(posts) == 2


@pytest.mark.parametrize("kind", ["decision", "review", "checkpoint", "commentary"])
def test_real_server_lost_reply_restart_exact_retry_preserves_original_receipt(tmp_path, kind):
    from looplab.events.run_generation import run_generation_token
    from tests.test_external_progress import _run
    from tests.test_external_checkpoints import seeded
    from looplab.harness.checkpoints import ask
    rd, store, server = seeded(tmp_path) if kind == "checkpoint" else _run(tmp_path)
    if kind == "checkpoint":
        store.append("eval_invocation_claimed", {"node_id": 0, "generation": 0, "attempt": 0, "invocation_id": "live"})
    generation = run_generation_token(store.read_all())
    body = {"expected_generation": generation, "action_id": "original"}
    if kind == "decision":
        suffix = "harness-decisions"
        body.update(phase_id="novelty", idea={"operator": "draft"}, decision="submit", reason="The idea differs from previous trials")
    elif kind == "review":
        suffix = "harness-reviews"
        body.update(phase_id="concept_merge", decision="no_applicable_action", reason="The measured nodes share no concept alias to consolidate")
    elif kind == "checkpoint":
        suffix = "harness-checkpoints"
        body.update(checkpoint_id=ask(rd, 0, 0, "stage_check")["checkpoint_id"], verdict="proceed", reason="Reviewed the original stage evidence")
    else:
        suffix = "result-notices"
        notices = server.get("/api/runs/demo/result-notices", params={"expected_generation": generation}).json()
        receipt = notices["items"][0]
        body.update(receipt_id=receipt["id"], evidence_token=receipt["evidence_token"], summary="Результат сохранён. Сравним следующий вариант с этим запуском.")
    path, seen, responses = "/api/runs/demo/" + suffix, [], []
    def bridge(request):
        seen.append(request.method)
        response = server.request(request.method, request.url.raw_path.decode(), content=request.content,
                                  headers={"Content-Type": "application/json"})
        if request.method == "POST":
            assert response.status_code == 200, response.text
            responses.append(response.json())
            if len(responses) == 1:
                raise httpx.ReadTimeout("lost after server committed", request=request)
        return httpx.Response(response.status_code, content=response.content)
    first = api(tmp_path / "client", bridge, url="http://testserver/")
    assert first.request("POST", path, body)["outcome"] == "unknown"
    first.client.close()
    second = api(tmp_path / "client", bridge, url="http://testserver/")
    row, = second.saved_actions("demo", generation)["items"]
    original = restore(second, row["request_id"], generation)
    assert original["body"] == body and seen == ["POST"]
    progress = second.run_progress("demo", generation)
    assert progress["status"] == 200 and progress["body"]["complete"]
    before = {str(p): p.read_bytes() for p in rd.rglob("*.jsonl")}
    retry = second.request(original["method"], original["path"], original["body"], original["idempotency_key"])
    assert retry["status"] == 200 and seen == ["POST", "GET", "POST"]
    assert {str(p): p.read_bytes() for p in rd.rglob("*.jsonl")} == before
    # The replay marker can differ; all original receipt evidence must stay identical.
    assert {k: v for k, v in responses[0].items() if k != "replayed"} == {k: v for k, v in responses[1].items() if k != "replayed"}


def test_fresh_process_and_mcp_tools_read_original_without_ui_extra_or_network(tmp_path):
    body = {"expected_generation": GEN, "action_id": "original", "expected_evidence_revision": "b" * 64}
    client = api(tmp_path, lambda _: httpx.Response(200, json={}))
    result = client.request("POST", "/api/runs/demo/harness-selection/values", body)
    server = build_server(client)
    tools = {t.name: t for t in anyio.run(server.list_tools)}
    for name in ("saved_actions", "saved_action"):
        hints = tools[name].annotations.model_dump(by_alias=True)
        assert hints["readOnlyHint"] and hints["idempotentHint"] and not hints["destructiveHint"] and not hints["openWorldHint"]
    client.client.close()
    async def reads():
        listing = await server.call_tool("saved_actions", {"run_id": "demo", "expected_generation": GEN})
        assert "unobserved" in str(listing) and not getattr(listing, "isError", False)
        page = await server.call_tool("saved_action", {"run_id": "demo", "expected_generation": GEN,
                                                      "request_id": result["client_request"]["request_id"]})
        assert "chunk_sha256" in str(page) and not getattr(page, "isError", False)
    anyio.run(reads)
    code = '''
import builtins, json, sys, base64, httpx
original = builtins.__import__
def no_ui(name, *args, **kwargs):
    if name.split('.')[0] in {'fastapi', 'uvicorn'}: raise ModuleNotFoundError(name)
    return original(name, *args, **kwargs)
builtins.__import__ = no_ui
from looplab.harness.mcp_server import HarnessAPI
def no_http(request): raise AssertionError('recovery made HTTP')
api = HarnessAPI(sys.argv[2], request_dir=sys.argv[1], transport=httpx.MockTransport(no_http))
rows = api.saved_actions('demo', 'a'*64)
assert rows['total'] == 1
page = api.saved_action('demo', 'a'*64, rows['items'][0]['request_id'], limit=16384)
print(json.dumps(json.loads(base64.b64decode(page['chunk']))))
api.client.close()
'''
    recovered = subprocess.run([sys.executable, "-c", code, str(tmp_path), URL], capture_output=True, text=True, timeout=20)
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout)["body"] == body


def test_checkpoint_identity_follows_the_servers_per_checkpoint_scope(tmp_path):
    """The server keys a checkpoint answer on `checkpoint_id` (`harness/checkpoints.py::respond`),
    so "answer" is a legal action_id for every checkpoint. The client identity carries the
    checkpoint too, or it refused locally (not_sent, no HTTP) what the server accepts."""
    from tests.test_external_checkpoints import seeded
    from looplab.harness.checkpoints import answer_for, ask
    from looplab.serve.run_commands import run_generation_token
    rd, store, server = seeded(tmp_path / "server")
    generation = run_generation_token(store.read_all())
    first = ask(rd, 0, 0, "asha_live", observation="objective=0.4", kill_enabled=False)
    second = ask(rd, 0, 0, "train_monitor", observation="loss=1", kill_enabled=False)
    assert first["checkpoint_id"] != second["checkpoint_id"]
    def forward(request):  # The client's real request, answered by the real route.
        reply = server.request(request.method, request.url.path, content=request.content,
                               headers={"content-type": "application/json"})
        return httpx.Response(reply.status_code, content=reply.content,
                              headers={"content-type": reply.headers.get("content-type", "")})
    client = HarnessAPI("http://testserver/", TOKEN, request_dir=tmp_path / "client",
                        transport=httpx.MockTransport(forward))
    path = "/api/runs/demo/harness-checkpoints"
    body = {"expected_generation": generation, "action_id": "answer", "verdict": "watch",
            "reason": "keep watching"}
    one = client.request("POST", path, {**body, "checkpoint_id": first["checkpoint_id"]})
    two = client.request("POST", path, {**body, "checkpoint_id": second["checkpoint_id"]})
    assert one["status"] == two["status"] == 200, (one, two)
    assert one["client_request"]["request_id"] != two["client_request"]["request_id"]
    assert answer_for(rd, first["checkpoint_id"])["action_id"] == "answer"
    assert answer_for(rd, second["checkpoint_id"])["action_id"] == "answer"
    assert client.saved_actions("demo", generation)["total"] == 2
    # Same checkpoint + action_id with changed content conflicts on BOTH sides; the client
    # says so in its own words, before HTTP.
    client.client = httpx.Client(base_url="http://testserver/", transport=httpx.MockTransport(
        lambda _: pytest.fail("conflicting answer sent")))
    changed = client.request("POST", path, {**body, "checkpoint_id": first["checkpoint_id"],
                                            "reason": "changed my mind"})
    assert changed["outcome"] == "not_sent" and changed["code"] == "client_request_conflict"
    assert "durably saved" not in changed["message"]


def test_checkpoint_answer_without_checkpoint_id_is_invalid_not_unavailable(tmp_path):
    client = api(tmp_path, lambda _: pytest.fail("invalid original sent"))
    result = client.request("POST", "/api/runs/demo/harness-checkpoints",
                            {"expected_generation": GEN, "action_id": "answer"})
    assert result["outcome"] == "not_sent" and result["code"] == "client_request_invalid"
    assert "checkpoint_id" in result["message"] and "durably saved" not in result["message"]
    assert not list(tmp_path.rglob("act_*.json"))


@pytest.mark.parametrize("suffix,namespace", [("harness-reviews", "harness-reviews"),
                                              ("upstream/check", "upstream"),
                                              ("result-notices", "result-notices")])
def test_unscoped_route_identity_is_byte_for_byte_unchanged(tmp_path, suffix, namespace):
    # Records saved before the checkpoint scope landed must still read back.
    client = api(tmp_path, lambda _: httpx.Response(200, json={}))
    result = client.request("POST", "/api/runs/demo/" + suffix,
                            {"expected_generation": GEN, "action_id": "original"})
    legacy = json.dumps([namespace, "original"], sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False).encode()
    assert result["client_request"]["request_id"] == "act_" + hashlib.sha256(legacy).hexdigest()


def test_reused_action_id_with_changed_body_names_the_conflict(tmp_path):
    seen = []
    client = api(tmp_path, lambda r: (seen.append(r), httpx.Response(200, json={}))[1])
    path, body = "/api/runs/demo/harness-reviews", {"expected_generation": GEN, "action_id": "one"}
    assert client.request("POST", path, body)["status"] == 200
    refused = client.request("POST", path, {**body, "reason": "other"})
    assert refused["outcome"] == "not_sent" and refused["code"] == "client_request_conflict"
    assert "durably saved" not in refused["message"] and len(seen) == 1


def test_a_checkpoint_answer_saved_before_its_scope_joined_the_identity_still_lists(tmp_path, monkeypatch):
    """Records are never rewritten in place: one saved under the earlier two-item identity must
    read back, or `listing` failed for every saved action of its generation."""
    client = api(tmp_path, lambda request: httpx.Response(200, json={"ok": True}))
    path = "/api/runs/demo/harness-checkpoints"
    body = {"expected_generation": GEN, "action_id": "answer", "verdict": "watch",
            "reason": "keep watching", "checkpoint_id": "a" * 32}
    monkeypatch.delitem(ACTION_SCOPE_FIELDS, "harness-checkpoints")   # the pre-scope client
    assert client.request("POST", path, body)["status"] == 200
    monkeypatch.undo()
    assert client.request("POST", "/api/runs/demo/lessons", {
        "expected_generation": GEN, "action_id": "l1", "statement": "x"})["status"] == 200
    listed = client.saved_actions("demo", GEN)
    assert listed.get("total") == 2, listed
