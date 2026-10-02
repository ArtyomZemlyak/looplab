"""A bound identity cannot make incomplete command completion/error data usable."""
import httpx
import pytest

from looplab.harness.mcp_server import HarnessAPI
from looplab.serve.protocol import COMMAND_STATUSES, COMMAND_TERMINAL_STATUSES
from tests.test_mcp_read_identity import GEN, _receipt


def _read(page):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=page)
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(handler))
    result = api.command_receipt("demo", GEN, idempotency_key="original-key")
    assert len(seen) == 1 and seen[0].method == "GET"
    return result


def _unavailable(page):
    result = _read(page)
    assert result["status"] == 200 and result["code"] == "response_incomplete"
    assert result["outcome"] == "unavailable" and result["reason"] == "invalid_command_receipt"
    assert "body" not in result


@pytest.mark.parametrize("field", ["status", "event_type", "event_seq", "error_code", "retryable"])
def test_missing_command_fields_are_unavailable(field):
    page = _receipt()
    del page["command"][field]
    _unavailable(page)


@pytest.mark.parametrize("changes", [{"version": True}, {"version": 2}, {"terminal": 1}, {"terminal": False}])
def test_invalid_or_inconsistent_envelope(changes):
    _unavailable({**_receipt(), **changes})


@pytest.mark.parametrize("field", ["version", "terminal"])
def test_missing_envelope_fields(field):
    page = _receipt()
    del page[field]
    _unavailable(page)


@pytest.mark.parametrize("changes", [{"status": "done"}, {"status": None}, {"status": []},
    {"status": "executing"}, {"event_type": "not_a_control"}, {"event_type": []},
    {"event_seq": True}, {"event_seq": -1}, {"event_seq": "3"},
    {"error_code": None}, {"error_code": []}, {"error_code": "x" * 257},
    {"retryable": 1}, {"retryable": "false"}])
def test_malformed_completion_and_error_fields(changes):
    page = _receipt()
    page["command"].update(changes)
    _unavailable(page)


@pytest.mark.parametrize("status", sorted(COMMAND_STATUSES))
def test_all_real_statuses_keep_their_meaning_and_extra_fields(status):
    page = _receipt()
    page["terminal"] = status in COMMAND_TERMINAL_STATUSES
    page["command"].update(status=status, event_seq=None, error_code="source_unavailable",
                            retryable=True, future_metadata={"retained": True})
    assert _read(page) == {"status": 200, "body": page}
