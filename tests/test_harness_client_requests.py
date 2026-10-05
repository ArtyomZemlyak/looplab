"""A new MCP process can recover exact intent without making another experiment."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json

import anyio
import httpx
import pytest

from looplab.harness.client_requests import ClientRequests
from looplab.harness.mcp_server import HarnessAPI, build_server

GEN = "a" * 64
URL = "http://localhost:8775/user/alice/proxy/"
TOKEN = "credential-not-for-the-client-record"
BODY = {"type": "inject_node", "expected_generation": GEN,
        "data": {"idea": {"operator": "draft", "concepts": ["search/grid"]},
                 "code": "print('Русский текст $& {0}')\n"}}


def api(root, handler, url=URL):
    return HarnessAPI(url, TOKEN, request_dir=root, transport=httpx.MockTransport(handler))


def restore(client, command_id, *, size=31, generation=GEN):
    raw, offset, digest = b"", 0, None
    while True:
        page = client.saved_command("demo", generation, command_id, offset, size, digest)
        assert page["source"] == "client" and page["server_effects"] == "unobserved"
        chunk = base64.b64decode(page["chunk"], validate=True)
        assert hashlib.sha256(chunk).hexdigest() == page["chunk_sha256"]
        digest = page["request_sha256"]
        raw += chunk
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert hashlib.sha256(raw).hexdigest() == digest
    return json.loads(raw)


def test_saved_before_http_then_new_process_recovers_without_network_or_secret(tmp_path):
    seen = []
    def lost(request):
        rows = list(tmp_path.rglob("cmd_*.json"))
        assert len(rows) == 1
        record = json.loads(rows[0].read_bytes())
        assert record["request"]["body"] == json.loads(request.content)
        assert record["request"]["idempotency_key"] == request.headers["Idempotency-Key"]
        seen.append(request)
        raise httpx.ReadTimeout("private sensitive transport error", request=request)
    first = api(tmp_path, lost)
    result = first.request("POST", "/api/runs/demo/commands", BODY, "original")
    assert result["outcome"] == "unknown" and len(seen) == 1
    first.client.close()
    second = api(tmp_path, lambda _: pytest.fail("local recovery made HTTP"))
    page = second.saved_commands("demo", GEN)
    assert page["store_exists"] and page["total"] == 1
    assert page["items"][0]["event_type"] == "inject_node"
    request = restore(second, page["items"][0]["command_id"])
    assert request == {"method": "POST", "path": "/api/runs/demo/commands",
                       "body": BODY, "idempotency_key": "original"}
    assert TOKEN not in b"".join(p.read_bytes() for p in tmp_path.rglob("*.json")).decode()
    assert "sensitive" not in str(result)


def test_exact_retry_preserves_identity_changed_body_is_not_sent(tmp_path):
    seen = []
    client = api(tmp_path, lambda r: (seen.append(r), httpx.Response(200, json={}))[1])
    first = client.request("POST", "/api/runs/demo/commands", BODY, "same")
    retry = client.request("POST", "/api/runs/demo/commands", json.loads(json.dumps(BODY)), "same")
    assert first["client_request"] == retry["client_request"] and len(seen) == 2
    files = {p: p.read_bytes() for p in tmp_path.rglob("cmd_*.json")}
    changed = {**BODY, "data": {"code": "new"}}
    refused = client.request("POST", "/api/runs/demo/commands", changed, "same")
    assert refused["outcome"] == "not_sent" and len(seen) == 2
    assert files == {p: p.read_bytes() for p in files}


@pytest.mark.parametrize("failure", ["claim", "lock", "contended"])
def test_unconfirmed_record_or_required_lock_prevents_http(tmp_path, monkeypatch, failure):
    import looplab.harness.client_requests as module
    from looplab.events.eventstore import EventStoreLockError, InterprocessLockContended
    client = api(tmp_path, lambda _: pytest.fail("unsaved command sent"))
    if failure == "claim":
        def broken(*_):
            raise OSError("private path")
        monkeypatch.setattr(module, "strict_atomic_write_bytes", broken)
    else:
        @contextmanager
        def broken(path, **kwargs):
            assert kwargs == {"required": True, "blocking": False}
            if failure == "contended":
                raise InterprocessLockContended(path)
            raise EventStoreLockError(path, OSError("private"))
            yield
        monkeypatch.setattr(module, "interprocess_lock", broken)
    result = client.request("POST", "/api/runs/demo/commands", BODY, "key")
    assert result["outcome"] == "not_sent" and result["code"] == "client_request_unavailable"
    assert "private" not in str(result)


@pytest.mark.parametrize("corruption", ["duplicate", "bool_version", "changed_body", "wrong_server",
                                       "wrong_key", "deep", "directory"])
def test_damaged_original_neither_becomes_empty_nor_is_overwritten(tmp_path, corruption):
    client = api(tmp_path, lambda _: httpx.Response(200, json={}))
    response = client.request("POST", "/api/runs/demo/commands", BODY, "key")
    path = next(tmp_path.rglob("cmd_*.json"))
    row = json.loads(path.read_bytes())
    if corruption == "directory":
        path.unlink(); path.mkdir()
        before = None
    else:
        if corruption == "duplicate": raw = '{"version":1,"version":1}'
        elif corruption == "deep": raw = '[' * 10000 + ']' * 10000
        else:
            if corruption == "bool_version": row["version"] = True
            if corruption == "changed_body": row["request"]["body"]["data"] = {}
            if corruption == "wrong_server": row["server"] = "http://other/"
            if corruption == "wrong_key": row["request"]["idempotency_key"] = "other"
            raw = json.dumps(row)
        path.write_text(raw)
        before = path.read_bytes()
    client.client = httpx.Client(base_url=URL, headers={"X-LoopLab-Token": TOKEN},
                                transport=httpx.MockTransport(lambda _: pytest.fail("corrupt original retried")))
    assert client.saved_commands("demo", GEN)["outcome"] == "unavailable"
    assert client.saved_command("demo", GEN, response["client_request"]["command_id"])["outcome"] == "unavailable"
    assert client.request("POST", "/api/runs/demo/commands", BODY, "key")["outcome"] == "not_sent"
    if before is not None:
        assert path.read_bytes() == before
    else:
        assert path.is_dir()


def test_generation_server_and_independent_intents_are_isolated(tmp_path):
    client = api(tmp_path, lambda _: httpx.Response(200, json={}))
    client.request("POST", "/api/runs/demo/commands", BODY, "first")
    client.request("POST", "/api/runs/demo/commands", BODY, "second")
    newer = {**BODY, "expected_generation": "b" * 64}
    client.request("POST", "/api/runs/demo/commands", newer, "first")
    assert client.saved_commands("demo", GEN)["total"] == 2
    assert client.saved_commands("demo", "b" * 64)["total"] == 1
    other = api(tmp_path, lambda _: pytest.fail("unexpected HTTP"), url="http://other/")
    assert other.saved_commands("demo", GEN)["total"] == 0
    assert not other.saved_commands("demo", GEN)["store_exists"]
    one = client.saved_commands("demo", GEN, limit=1)
    two = client.saved_commands("demo", GEN, offset=one["next_offset"], limit=1)
    assert one["items"][0] != two["items"][0] and two["next_offset"] is None


def test_literal_russian_run_id_saves_the_encoded_http_path(tmp_path):
    seen = []
    client = api(tmp_path, lambda r: (seen.append(r), httpx.Response(200, json={}))[1])
    result = client.request("POST", "/api/runs/проба/commands", BODY, "russian-run")
    assert result["status"] == 200 and len(seen) == 1
    rows = client.saved_commands("проба", GEN)
    page = client.saved_command("проба", GEN, rows["items"][0]["command_id"])
    original = json.loads(base64.b64decode(page["chunk"]))
    assert seen[0].url.raw_path.decode().endswith(original["path"])
    assert original["path"] == "/api/runs/%D0%BF%D1%80%D0%BE%D0%B1%D0%B0/commands"


def test_literal_percent_run_id_is_decoded_once(tmp_path):
    seen = []
    client = api(tmp_path, lambda r: (seen.append(r), httpx.Response(200, json={}))[1])
    result = client.request("POST", "/api/runs/recipe%2520/commands", BODY, "percent-run")
    assert result["status"] == 200 and len(seen) == 1
    rows = client.saved_commands("recipe%20", GEN)
    assert rows["total"] == 1
    page = client.saved_command("recipe%20", GEN, rows["items"][0]["command_id"])
    original = json.loads(base64.b64decode(page["chunk"]))
    assert original["path"] == "/api/runs/recipe%2520/commands"
    assert seen[0].url.raw_path.decode().endswith(original["path"])


def test_changed_http_server_cannot_send_under_old_local_record_scope(tmp_path):
    client = api(tmp_path, lambda _: pytest.fail("wrong server contacted"))
    client.client.base_url = "http://other/"
    assert client.request("POST", "/api/runs/demo/commands", BODY, "scope")["outcome"] == "not_sent"
    assert not list(tmp_path.rglob("cmd_*.json"))


def test_two_clients_keep_one_original_or_refuse_content_conflict(tmp_path):
    clients = [api(tmp_path, lambda _: httpx.Response(200, json={})) for _ in range(2)]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda c: c.request("POST", "/api/runs/demo/commands", BODY, "shared"), clients))
    assert any(r.get("status") == 200 for r in results)
    assert all(r.get("status") == 200 or r.get("outcome") == "not_sent" for r in results)
    assert len(list(tmp_path.rglob("cmd_*.json"))) == 1
    restored = restore(clients[0], clients[0].saved_commands("demo", GEN)["items"][0]["command_id"])
    assert restored["body"] == BODY


def test_reads_and_non_durable_operations_remain_unjournaled(tmp_path):
    seen = []
    client = api(tmp_path, lambda r: (seen.append(r), httpx.Response(200, json={}))[1])
    for method, path in [("GET", "/api/runs/demo/commands"),
                         ("POST", "/api/runs/demo/novelty-preview"),
                         ("PATCH", "/api/settings")]:
        client.request(method, path, {}, "not-a-command")
    assert len(seen) == 3 and not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("bad", ["credential", "generation", "key", "path", "query", "encoded_runs", "encoded_commands"])
def test_credential_or_noncanonical_command_cannot_be_sent_or_saved(tmp_path, bad):
    client = api(tmp_path, lambda _: pytest.fail("invalid original sent"))
    body, key, path = BODY, "key", "/api/runs/demo/commands"
    if bad == "credential": body = {**BODY, "data": {"code": TOKEN}}
    if bad == "generation": body = {**BODY, "expected_generation": "bad"}
    if bad == "key": key = ""
    if bad == "path": path = "/api/runs/%64emo/commands"
    if bad == "encoded_runs": path = "/api/%72uns/demo/commands"
    if bad == "encoded_commands": path = "/api/runs/demo/%63ommands"
    if bad == "query": path += "?other=1"
    assert client.request("POST", path, body, key)["outcome"] == "not_sent"
    assert not list(tmp_path.rglob("cmd_*.json"))


def test_store_limit_and_hash_fence_are_explicit_unavailable_not_prefix_evidence(tmp_path, monkeypatch):
    import looplab.harness.client_requests as module
    client = api(tmp_path, lambda _: httpx.Response(200, json={}))
    result = client.request("POST", "/api/runs/demo/commands", BODY, "one")
    monkeypatch.setattr(module, "MAX_RECORDS", 1)
    assert client.request("POST", "/api/runs/demo/commands", BODY, "two")["outcome"] == "not_sent"
    row = client.saved_command("demo", GEN, result["client_request"]["command_id"], expected_request_hash="b" * 64)
    assert row["outcome"] == "unavailable" and "chunk" not in row
    assert client.saved_command("demo", GEN, result["client_request"]["command_id"], offset=1)["outcome"] == "unavailable"


def test_saved_request_and_http_use_same_frozen_body_even_if_caller_changes_it(tmp_path, monkeypatch):
    body = json.loads(json.dumps(BODY))
    client = api(tmp_path, lambda r: httpx.Response(200, json=json.loads(r.content)))
    save = client.requests.save
    def mutate(*args, **kwargs):
        row = save(*args, **kwargs)
        body["data"]["code"] = "changed after saving"
        return row
    monkeypatch.setattr(client.requests, "save", mutate)
    result = client.request("POST", "/api/runs/demo/commands", body, "frozen")
    assert result["body"]["data"]["code"] == BODY["data"]["code"]
    assert restore(client, result["client_request"]["command_id"])["body"] == BODY


def test_lost_reply_real_command_reconnect_observation_and_exact_retry_add_one_event(tmp_path):
    from fastapi.testclient import TestClient
    from looplab.core.config import Settings
    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app
    from tests.factories import http_run_generation
    rd = tmp_path / "runs" / "demo"
    rd.mkdir(parents=True)
    (rd / "config.snapshot.json").write_text(json.dumps(Settings(backend="toy", external_harness=True).model_dump(mode="json")))
    events = EventStore(rd / "events.jsonl")
    events.append("run_started", {"run_id": "demo", "task_id": "task", "goal": "g", "direction": "min"})
    server = TestClient(make_app(rd.parent))
    generation = http_run_generation(server)
    lost = True
    seen = []
    def bridge(request):
        nonlocal lost
        seen.append(request.method)
        # Local owner test endpoint; transport credential is irrelevant to this fixture.
        response = server.request(request.method, request.url.raw_path.decode(), content=request.content,
                                  headers={"Content-Type": "application/json", "Idempotency-Key": request.headers.get("Idempotency-Key", "")})
        if request.method == "POST" and lost:
            lost = False
            assert response.status_code == 200 and response.json()["status"] == "succeeded", response.text
            raise httpx.ReadTimeout("lost after server applied", request=request)
        return httpx.Response(response.status_code, content=response.content)
    first = api(tmp_path / "client", bridge, url="http://testserver/")
    body = {"type": "research_completed", "expected_generation": generation,
            "data": {"memo": {"summary": "Measure a baseline before comparing changes.",
                              "claims": [], "open_questions": []}}}
    assert first.request("POST", "/api/runs/demo/commands", body, "original")["outcome"] == "unknown"
    first.client.close()
    second = api(tmp_path / "client", bridge, url="http://testserver/")
    rows = second.saved_commands("demo", generation)
    command_id = rows["items"][0]["command_id"]
    page = second.saved_command("demo", generation, command_id)
    original = json.loads(base64.b64decode(page["chunk"]))
    assert original["body"] == body and seen == ["POST"]
    receipt = second.command_receipt("demo", generation, idempotency_key=original["idempotency_key"])
    assert receipt["status"] == 200 and receipt["body"]["command"]["status"] == "succeeded"
    assert seen == ["POST", "GET"]
    before = (rd / "events.jsonl").read_bytes()
    retry = second.request(original["method"], original["path"], original["body"], original["idempotency_key"])
    assert retry["body"]["id"] == command_id and seen == ["POST", "GET", "POST"]
    assert (rd / "events.jsonl").read_bytes() == before
    assert sum(e.type == "research_completed" for e in events.read_all()) == 1


def test_live_external_toy_lost_inject_reply_new_client_does_not_repeat_evaluation(tmp_path, monkeypatch):
    from pathlib import Path
    from fastapi.testclient import TestClient
    from looplab.core.config import Settings
    from looplab.events.replay import fold
    from looplab.serve.server import make_app
    from tests.factories import make_engine
    from tests.test_external_run_mode import NoInternalRole
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", TOKEN)
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret-for-test-server")
    root = tmp_path / "runs"
    rd = root / "demo"
    rd.mkdir(parents=True)
    settings = Settings(backend="toy", external_harness=True, deep_research_every=-1,
                        concept_pivot=False, concept_run_base=False, cross_run_concepts=False,
                        track_hypotheses=False, report_every=0, novelty_mode="off", foresight=False, lessons_every=0,
                        cross_run_curation=False, concept_tidy=False, reflection_priors=False,
                        memory_dir=str(tmp_path / "memory"))
    (rd / "config.snapshot.json").write_text(json.dumps(settings.model_dump(mode="json")))
    (rd / "task.snapshot.json").write_bytes((Path(__file__).resolve().parents[1] / "examples/toy_task.json").read_bytes())
    engine = make_engine(rd, researcher=NoInternalRole(), developer=NoInternalRole(),
                         external_harness=True, n_seeds=1, max_nodes=3)
    server = TestClient(make_app(root))
    seen, lost = [], True
    def bridge(request):
        nonlocal lost
        seen.append(request.method)
        response = server.request(request.method, request.url.raw_path.decode(), content=request.content,
                                  headers={"Content-Type": "application/json", "Idempotency-Key": request.headers.get("Idempotency-Key", ""),
                                           "X-LoopLab-Token": request.headers.get("X-LoopLab-Token", "")})
        if request.method == "POST" and lost:
            lost = False
            assert response.status_code == 200 and response.json()["status"] in ("accepted", "running", "succeeded"), response.text
            raise httpx.ReadTimeout("lost inject reply", request=request)
        return httpx.Response(response.status_code, content=response.content)
    first = api(tmp_path / "client", bridge, url="http://testserver/")

    async def scenario():
        async with anyio.create_task_group() as group:
            result = {}
            async def run_engine():
                from looplab.cli import _engine_singleton
                with _engine_singleton(rd) as owned:
                    assert owned
                    state = await engine.run()
                result["state"] = state
            group.start_soon(run_engine)
            with anyio.fail_after(15):
                while not fold(engine.store.read_all()).setup_done:
                    await anyio.sleep(0.02)
                generation = first.request("GET", "/api/runs/demo/state?observe_only=true")["body"]["generation"]
                contract = first.request("GET", "/api/runs/demo/harness-contract")["body"]
                assert contract
                progress = first.run_progress("demo", generation)
                assert not progress.get("code") and progress["body"]["complete"]
                assert not progress["body"]["candidate_blockers_if_expanding"]
                assert not progress["body"]["candidate_decisions_per_idea"]
                body = {"type": "inject_node", "expected_generation": generation,
                        "data": {"idea": {"operator": "draft", "params": {"x": 3.0, "y": -1.0}},
                                 "code": "import json\nx,y=3.0,-1.0\nprint(json.dumps({'metric':(x-3)**2+(y+1)**2}))\n"}}
                sent = await anyio.to_thread.run_sync(lambda: first.request("POST", "/api/runs/demo/commands", body, "candidate"))
                assert sent["outcome"] == "unknown"
                first.client.close()
                second = api(tmp_path / "client", bridge, url="http://testserver/")
                local_reads_at = len(seen)
                rows = second.saved_commands("demo", generation)
                original = restore(second, rows["items"][0]["command_id"], generation=generation)
                assert len(seen) == local_reads_at and original["body"] == body
                while not fold(engine.store.read_all()).evaluated_nodes():
                    await anyio.sleep(0.02)
                receipt = second.command_receipt("demo", generation, idempotency_key=original["idempotency_key"])
                assert not receipt.get("code") and receipt["body"]["command"]["status"] == "succeeded"
                progress = second.run_progress("demo", generation)
                assert not progress.get("code")
                assert not progress["body"]["finish_pending_nodes"]
                assert not progress["body"]["finish_reviews_due"] and not progress["body"]["finish_report_due"]
                notices = second.result_notices("demo", generation)
                assert not notices.get("code") and len(notices["body"]["items"]) == 1
                row = notices["body"]["items"][0]
                assert row["score"] == 0.0
                comment = second.request("POST", "/api/runs/demo/result-notices",
                    {"expected_generation": generation, "receipt_id": row["id"], "evidence_token": row["evidence_token"],
                     "action_id": "node-comment", "summary": "В toy-задаче измерен минимум квадратичной функции. Это проверка восстановления клиента; качество ML-модели здесь не проверяется. Завершаю тестовый запуск."})
                assert comment["status"] == 200 and not comment.get("code")
                stopped = await anyio.to_thread.run_sync(lambda: second.request("POST", "/api/runs/demo/commands",
                    {"type": "run_abort", "expected_generation": generation, "data": {"reason": "measured toy scenario complete"}}, "finish"))
                assert stopped["status"] == 200 and stopped["body"]["status"] != "rejected"
                while "state" not in result:
                    await anyio.sleep(0.02)
                assert result["state"].finished and len(result["state"].evaluated_nodes()) == 1
                assert sum(e.type == "inject_node" for e in engine.store.read_all()) == 1
                assert sum(e.type == "node_evaluated" for e in engine.store.read_all()) == 1
                final = second.result_notices("demo", generation)
                assert not final.get("code")
                run_row = next(r for r in final["body"]["items"] if r["kind"] == "run")
                comment = second.request("POST", "/api/runs/demo/result-notices",
                    {"expected_generation": generation, "receipt_id": run_row["id"], "evidence_token": run_row["evidence_token"],
                     "action_id": "run-comment", "summary": "Тестовый запуск завершён: оценён один узел. Потерянный ответ восстановлен чтением исходной команды и серверной квитанции; повторная инъекция не потребовалась."})
                assert comment["status"] == 200 and not comment.get("code")
                second.client.close()
            group.cancel_scope.cancel()
    anyio.run(scenario)


def test_stdio_enables_client_records_at_configured_root_before_serving(tmp_path, monkeypatch):
    import looplab.harness.mcp_server as module
    monkeypatch.setenv("LOOPLAB_HARNESS_REQUEST_DIR", str(tmp_path / "retained"))
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-not-allowed")
    actual_api = module.HarnessAPI
    clients = []
    def create(url, token):
        client = actual_api(url, token, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
        clients.append(client)
        return client
    class Server:
        def run(self, **kwargs):
            assert kwargs == {"transport": "stdio"}
            response = clients[0].request("POST", "/api/runs/demo/commands", BODY, "stdio")
            assert response["client_request"]["source"] == "client"
    monkeypatch.setattr(module, "HarnessAPI", create)
    monkeypatch.setattr(module, "build_server", lambda _: Server())
    module.run_stdio(url=URL, token=TOKEN)
    assert clients[0].client.is_closed
    assert len(list((tmp_path / "retained").rglob("cmd_*.json"))) == 1


def test_fresh_process_reads_saved_body_without_ui_extra_or_network(tmp_path):
    import subprocess
    import sys
    client = api(tmp_path, lambda _: httpx.Response(200, json={}))
    result = client.request("POST", "/api/runs/demo/commands", BODY, "fresh-process")
    client.client.close()
    code = '''
import builtins, json, sys, base64
original = builtins.__import__
def no_ui(name, *args, **kwargs):
    if name.split('.')[0] in {'fastapi', 'uvicorn'}:
        raise ModuleNotFoundError(name)
    return original(name, *args, **kwargs)
builtins.__import__ = no_ui
from looplab.harness.mcp_server import HarnessAPI
api = HarnessAPI(sys.argv[2], request_dir=sys.argv[1])
rows = api.saved_commands('demo', 'a'*64)
assert rows['total'] == 1
page = api.saved_command('demo', 'a'*64, rows['items'][0]['command_id'], limit=16384)
print(json.dumps(json.loads(base64.b64decode(page['chunk'])), ensure_ascii=True))
api.client.close()
'''
    recovered = subprocess.run([sys.executable, "-c", code, str(tmp_path), URL], capture_output=True, text=True, timeout=20)
    assert recovered.returncode == 0, recovered.stderr
    request = json.loads(recovered.stdout)
    assert request["body"] == BODY and request["idempotency_key"] == "fresh-process"
    assert result["client_request"]["server_effects"] == "unobserved"


def test_mcp_local_read_tools_are_read_only_and_need_no_server(tmp_path):
    client = api(tmp_path, lambda _: httpx.Response(200, json={}))
    result = client.request("POST", "/api/runs/demo/commands", BODY, "tool")
    server = build_server(client)
    tools = {t.name: t for t in anyio.run(server.list_tools)}
    for name in ("saved_commands", "saved_command"):
        hints = tools[name].annotations.model_dump(by_alias=True)
        assert hints["readOnlyHint"] and not hints["destructiveHint"] and not hints["openWorldHint"]
    client.client.close()
    async def reads():
        listing = await server.call_tool("saved_commands", {"run_id": "demo", "expected_generation": GEN})
        assert not getattr(listing, "isError", False) and "unobserved" in str(listing)
        page = await server.call_tool("saved_command", {"run_id": "demo", "expected_generation": GEN,
                                                       "command_id": result["client_request"]["command_id"]})
        assert not getattr(page, "isError", False) and "chunk_sha256" in str(page)
    anyio.run(reads)
