"""Campaign / per-query journals after review 2026-10-16, driven through the real routes: a re-filed
experiment is judged on its SUBSTANCE, a damaged journal names its file AND line, a same-key upload
accepted before a newer label rule replays, fingerprints of two methods are not compared, and the
narrative writer downgrades on a shape-dropped row like every other journal (`journals.shape_checked`)."""
from __future__ import annotations

import json

from looplab.harness import per_query
from tests.test_campaign_protocol import (AGENT, OWNER, _amend, _arrays, _client, _experiment,
                                          _make_run, _repo, _upload)


def test_a_refiled_experiment_is_the_same_declaration_on_its_substance(tmp_path, monkeypatch):
    """8a: a cosmetic title/notes/description edit, or a row an OLDER normalizer wrote (a default
    key absent), is the registered declaration; a different hypothesis is not."""
    root, client = _client(tmp_path, monkeypatch)
    rd, store = _make_run(root, "camp", _repo(tmp_path), nodes=())
    assert _amend(client, "camp", store, "experiment", experiment=_experiment()).status_code == 200
    journal = rd / "protocol_amendments.jsonl"
    row = json.loads(journal.read_text())
    for key in ("notes", "bootstrap_replicates"):           # what an older normalize left out
        row["payload"].pop(key)
    journal.write_text(json.dumps(row) + "\n")
    cosmetic = _experiment(title="Repeat vs fresh (v2)", notes="typo fixed",
                           arms={"A0": {"description": "the baseline arm"}, "A1": {}, "A2": {}})
    again = _amend(client, "camp", store, "experiment", action="e2", experiment=cosmetic)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "experiment_exists"
    assert again.json()["detail"]["same_declaration"] is True
    changed = _amend(client, "camp", store, "experiment", action="e3",
                     experiment=_experiment(hypothesis="A1 beats A0"))
    assert changed.status_code == 409 and changed.json()["detail"]["same_declaration"] is False


def test_a_damaged_journal_names_its_file_and_line(tmp_path, monkeypatch):
    """8b: the refusal stays (no reader repairs a journal), but it now says WHERE: the operator
    fixes that one line and the same action_id goes through."""
    root, client = _client(tmp_path, monkeypatch)
    rd, store = _make_run(root, "camp", _repo(tmp_path), nodes=())
    assert _amend(client, "camp", store, "experiment", experiment=_experiment()).status_code == 200
    journal = rd / "protocol_amendments.jsonl"
    good = journal.read_bytes()
    journal.write_bytes(good + b"\n" + json.dumps({"action_id": "lost"}).encode() + b"\n")
    refused = _amend(client, "camp", store, "experiment", action="e2",
                     experiment=_experiment(experiment_id="D5"))
    assert refused.status_code == 503
    detail = refused.json()["detail"]
    assert detail["code"] == "harness_history_incomplete" and detail["line"] == 3
    assert "protocol_amendments.jsonl line 3" in detail["message"]
    journal.write_bytes(good)
    assert _amend(client, "camp", store, "experiment", action="e2",
                  experiment=_experiment(experiment_id="D5")).status_code == 200


def test_a_same_key_upload_accepted_before_a_newer_label_rule_replays(tmp_path, monkeypatch):
    """8c: the label refusal ran BEFORE the action_id lookup, so a lost-response retry of an upload
    the server had accepted (before the TAB/newline rule) was a 400 instead of its receipt."""
    root, client = _client(tmp_path, monkeypatch)
    rd, store = _make_run(root, "camp", _repo(tmp_path), nodes=(0,), metrics={0: 0.5})
    payload = _arrays(0)[0]
    first = _upload(client, "camp", store, 0, payload, action="u1")
    assert first.status_code == 200, first.text
    monkeypatch.setattr(per_query, "_has_separator", lambda label: True)   # a stricter rule now
    again = _upload(client, "camp", store, 0, payload, action="u1")
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert again.json()["upload"]["upload_id"] == first.json()["upload"]["upload_id"]
    fresh = _upload(client, "camp", store, 0, payload, action="u2")
    assert fresh.status_code == 400 and "tab or a newline" in fresh.text


def test_fingerprints_of_two_methods_are_not_compared(tmp_path, monkeypatch):
    """8d: `lfp2` hashes directory links `lfp1` never saw; a refresh whose recorded fingerprint is
    an lfp1 one is `data_content_incomparable`, never a false `data_content_changed`."""
    root, client = _client(tmp_path, monkeypatch)
    rd, store = _make_run(root, "camp", _repo(tmp_path), nodes=(0,), metrics={0: 0.5})
    data = tmp_path / "e7"
    data.mkdir()
    (data / "train.csv").write_text("u,i\n1,2\n")
    added = _amend(client, "camp", store, "task", task={"data_add": {"e7": str(data)}})
    assert added.status_code == 200, added.text
    assert added.json()["amendment"]["payload"]["data_content"]["e7"]["after"]["method"] == "lfp2"
    journal = rd / "protocol_amendments.jsonl"
    row = json.loads(journal.read_text())
    row["payload"]["data_content"]["e7"]["after"].update(method="lfp1", digest="0" * 64)
    journal.write_text(json.dumps(row) + "\n")
    refreshed = _amend(client, "camp", store, "task", action="t2", task={"data_refresh": ["e7"]})
    assert refreshed.status_code == 200, refreshed.text
    payload = refreshed.json()["amendment"]["payload"]
    assert payload["data_content_changed"] == [] and payload["data_content_incomparable"] == ["e7"]


def test_a_shape_dropped_narrative_row_refuses_the_next_publication(tmp_path, monkeypatch):
    """A5: the narrative writer dropped a JSON object without a revision and published past it —
    possibly past this very action_id's revision. It now refuses like every journal writer."""
    from looplab.harness.campaign_report import report_path
    root, client = _client(tmp_path, monkeypatch)
    st = client.post("/api/supertasks", json={"name": "s"}, headers=AGENT).json()
    body = {"action_id": "n1", "expected_revision": 0, "title": "t", "body_markdown": "x",
            "key_findings": []}
    url = f"/api/supertasks/{st['id']}/campaign/narrative"
    assert client.post(url, json=body, headers=AGENT).status_code == 200
    path = report_path(root, st["id"])
    path.write_bytes(path.read_bytes() + json.dumps({"action_id": "n2"}).encode() + b"\n")
    refused = client.post(url, json={**body, "action_id": "n3", "expected_revision": 1},
                          headers=OWNER)
    assert refused.status_code == 503, refused.text
    assert refused.json()["detail"]["code"] == "harness_history_incomplete"
    assert refused.json()["detail"]["line"] == 2
