"""A JSON envelope alone cannot bind cached evidence to the requested run or command."""
import httpx
import pytest
import subprocess
import sys

from looplab.harness.mcp_server import HarnessAPI
from looplab.serve.command_identity import command_identity
from tests.test_harness_connection import GEN, response


def _receipt(generation=GEN, command_id=None):
    return {"generation": generation, "terminal": True,
            "command": {"id": command_id or command_identity("original-key")[0], "status": "succeeded"}}


@pytest.mark.parametrize("tool", ["run_progress", "command_receipt"])
@pytest.mark.parametrize("generation", [None, "bad", "b" * 64])
def test_read_refuses_missing_or_mismatched_generation_without_echoing_evidence(tool, generation):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={**_receipt(generation), "private": "wrong-context-evidence"})
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(handler))
    value = (api.run_progress("demo", GEN) if tool == "run_progress" else
             api.command_receipt("demo", GEN, idempotency_key="original-key"))
    assert value["status"] == 200 and value["outcome"] == "unavailable"
    assert value["code"] == ("response_context_mismatch" if generation == "b" * 64 else "response_incomplete")
    assert value["reason"] == ("generation_mismatch" if generation == "b" * 64 else "invalid_response")
    assert "body" not in value and "wrong-context-evidence" not in str(value)
    assert len(seen) == 1 and seen[0].method == "GET"


@pytest.mark.parametrize("by_key", [True, False])
def test_receipt_refuses_a_different_command_even_in_the_same_generation(by_key):
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=_receipt(command_id="cmd_" + "f" * 32))))
    args = {"idempotency_key": "original-key"} if by_key else {"command_id": command_identity("original-key")[0]}
    value = api.command_receipt("demo", GEN, **args)
    assert value["code"] == "response_context_mismatch" and value["outcome"] == "unavailable"
    assert value["reason"] == "command_mismatch" and "body" not in value


@pytest.mark.parametrize("command", [None, [], {}, {"id": 123}])
def test_receipt_refuses_an_unbound_command_object(command):
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"generation": GEN, "command": command})))
    value = api.command_receipt("demo", GEN, idempotency_key="original-key")
    assert value["outcome"] == "unavailable" and value["code"] == "response_incomplete"


def test_valid_receipt_accepts_hex_case_without_reinterpreting_the_original_key():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_receipt(GEN.upper()))
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(handler))
    assert api.command_receipt("demo", GEN, idempotency_key="original-key")["body"] == _receipt(GEN.upper())
    assert len(seen) == 1 and seen[0].headers["Idempotency-Key"] == "original-key"


@pytest.mark.parametrize("changes", [{"run_id": "other"}, {"generation": "b" * 64},
    {"mode": "internal"}, {"server_paths": {}}, {"credential_configured": None}, {"engine_running": 1}])
def test_connection_rejects_invalid_handoff_before_reading_progress(changes):
    seen = []
    def handler(request):
        seen.append(request)
        value = response(request.url.path)
        if request.url.path.endswith("/harness-handoff"):
            value.update(changes)
        return httpx.Response(200, json=value)
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(handler))
    value = api.connection_check("demo", GEN)
    assert value["ok"] is False and value["code"] == "invalid_response"
    assert len(seen) == 2 and not any(r.url.path.endswith("/harness-progress") for r in seen)


def test_key_bound_receipt_client_needs_no_local_fastapi_or_uvicorn():
    code = '''
import builtins, sys, httpx
original = builtins.__import__
def without_ui(name, *args, **kwargs):
    if name.split('.')[0] in {'fastapi', 'uvicorn'}:
        raise ModuleNotFoundError(name)
    return original(name, *args, **kwargs)
builtins.__import__ = without_ui
from looplab.harness.mcp_server import HarnessAPI
payload = {'generation': 'a' * 64, 'terminal': True, 'command': {'id': sys.argv[1], 'status': 'succeeded'}}
api = HarnessAPI('http://localhost', transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
assert api.command_receipt('demo', 'a' * 64, idempotency_key='original-key')['body'] == payload
'''
    result = subprocess.run([sys.executable, "-c", code, command_identity("original-key")[0]],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
