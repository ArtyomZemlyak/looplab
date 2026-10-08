"""`core/jsonutil.py::strict_json_loads` is the ONE duplicate-key JSON reader, and its four former
private copies keep the refusal each of them gave — plus the two canonical-bytes call sites in
`harness/` that now spell their persisted identities through `canonical_json`."""
from __future__ import annotations

import hashlib
import json

import pytest

from looplab.core.jsonutil import (DuplicateJSONKey, JSONTooDeep, NonFiniteJSONConstant,
                                   StrictJSONError, canonical_json, strict_json_loads)

_DEEP = "[" * 50_000 + "]" * 50_000


@pytest.mark.parametrize("text, kind", [
    ('{"a": 1, "a": 2}', DuplicateJSONKey),
    ('{"x": [{"b": 1, "b": 1}]}', DuplicateJSONKey),     # nested, and an EXACT repeat is still one
    ('{"a": NaN}', NonFiniteJSONConstant),
    ("[Infinity]", NonFiniteJSONConstant),
    ("-Infinity", NonFiniteJSONConstant),
    (_DEEP, JSONTooDeep),
], ids=["dup", "nested-dup", "nan", "inf", "neg-inf", "deep"])
def test_every_refusal_is_a_value_error_never_a_last_wins_dict_or_a_recursion_error(text, kind):
    with pytest.raises(kind) as caught:
        strict_json_loads(text)
    assert isinstance(caught.value, StrictJSONError) and isinstance(caught.value, ValueError)


def test_the_messages_are_the_ones_speculation_quality_recorded():
    with pytest.raises(DuplicateJSONKey, match=r"^duplicate JSON key: a$") as dup:
        strict_json_loads('{"a": 1, "a": 2}')
    assert dup.value.key == "a"
    with pytest.raises(NonFiniteJSONConstant, match=r"^non-finite JSON number: NaN$"):
        strict_json_loads("NaN")


def test_ordinary_documents_and_bytes_read_exactly_as_json_loads_reads_them():
    for doc in ['{"a": {"b": [1, 2.5, null, true]}, "c": "é"}', "[]", '"x"', "1e999"]:
        assert strict_json_loads(doc) == json.loads(doc)
    assert strict_json_loads('{"k": "v"}'.encode("utf-8")) == {"k": "v"}


def test_speculation_quality_keeps_its_byte_identical_error_strings():
    from looplab.search.speculation_quality import _json_loads
    for data, message in [(b'{"a":1,"a":2}', "invalid strict JSON: duplicate JSON key: a"),
                          (b"[NaN]", "invalid strict JSON: non-finite JSON number: NaN")]:
        with pytest.raises(ValueError) as caught:
            _json_loads(data)
        assert str(caught.value) == message
    with pytest.raises(ValueError, match="^file is not UTF-8$"):
        _json_loads(b"\xff")
    with pytest.raises(ValueError, match="^invalid strict JSON: "):
        _json_loads(_DEEP.encode())


def test_governance_ledgers_refuse_duplicate_constant_and_deep_rows_as_malformed(tmp_path):
    from looplab.engine.governance_health import GovernanceLedgerUnavailable, read_governance_rows
    for row in (b'{"a": 1, "a": 1}', b'{"a": NaN}', _DEEP.encode()):
        path = tmp_path / "ledger.jsonl"
        path.write_bytes(row + b"\n")
        with pytest.raises(GovernanceLedgerUnavailable) as caught:
            read_governance_rows(path, ledger="claim_decisions", validate=lambda _row: None)
        assert caught.value.reason == "malformed_json"


@pytest.mark.parametrize("body, message", [
    ('{"v": 2, "v": 2}', "duplicate curation claim field"),
    ('{"v": NaN}', "non-finite curation claim value"),
    ('{"v": 2', "invalid curation claim encoding"),
])
def test_a_paid_curation_claim_keeps_each_of_its_sentences(tmp_path, body, message):
    from looplab.engine.curation_protocol import CurationProtocolMixin
    path = tmp_path / "claim.json"
    path.write_bytes(body.encode() + b"\n")
    with pytest.raises(ValueError) as caught:
        CurationProtocolMixin._read_curation_claim(None, path, "log", "kind", "key")
    assert type(caught.value) is ValueError and str(caught.value) == message


def test_a_saved_client_request_with_a_repeated_field_is_refused_as_before(tmp_path):
    from looplab.harness.client_requests import ClientRequests
    store = ClientRequests(tmp_path / "requests", "http://127.0.0.1:8765")
    generation = "a" * 64
    row = store.save("run", generation, {"type": "pause", "expected_generation": generation}, "key-1")
    path = store._directory("run", generation) / (row["command_id"] + ".json")
    good = path.read_bytes()
    assert store._read(path, "run", generation) == row
    path.write_bytes(good[:-1] + b',"version":1}')
    with pytest.raises(ValueError, match="^duplicate client request field$"):
        store._read(path, "run", generation)
    path.write_bytes(_DEEP.encode())
    with pytest.raises(ValueError, match="^client request structure invalid$"):
        store._read(path, "run", generation)


_OLD_SPELLING = dict(sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


@pytest.mark.parametrize("value", [
    ["run", "a" * 64], {"b": 1, "a": [1.5, None, True]}, {"é": "ü 🚀", "nested": {"z": {}, "y": []}},
    ["upstream", "act-1", "cp_9"], {"method": "POST", "body": {"x": 1e300, "y": -0.0}},
])
def test_client_identity_bytes_are_unchanged_by_routing_through_canonical_json(value):
    """`request_sha256`, the run directory and every `act_` id are hashes over these bytes and are
    already on disk: the reuse is only admissible because they are byte-identical."""
    from looplab.harness import client_requests
    assert client_requests._bytes(value) == json.dumps(value, **_OLD_SPELLING).encode()
    assert canonical_json(value) == json.dumps(value, **_OLD_SPELLING).encode()


@pytest.mark.parametrize("action", ["a", "upstream-1", "A.b:c_d-9", "x" * 128])
def test_the_retained_request_proposal_id_is_the_servers_mint_and_the_old_client_spelling(action):
    from looplab.engine.upstream_state import digest
    old = "up_" + hashlib.sha256(json.dumps(action).encode()).hexdigest()[:24]
    new = "up_" + hashlib.sha256(canonical_json(action)).hexdigest()[:24]
    assert new == old == "up_" + digest(action)[:24]
