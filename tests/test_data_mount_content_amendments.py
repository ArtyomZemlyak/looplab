"""A data-mount amendment versions the mount's CONTENT, not only its declaration (doc 81 §8
leftover, 2026-10-14): `data_add` records a bounded content fingerprint, `data_refresh` records that
the bytes under an unchanged name changed (a protocol version of its own), `data_remove` keeps the
last fingerprint, and the protocol view names the current one."""
from __future__ import annotations

from looplab.harness import campaign
from looplab.harness.campaign import content_fingerprint
from test_campaign_protocol import _amend, _client, _make_run, _repo, OWNER


def test_the_fingerprint_follows_the_bytes_and_states_its_bounds(tmp_path, monkeypatch):
    d = tmp_path / "data"
    (d / "sub").mkdir(parents=True)
    (d / "a.csv").write_text("x,y\n1,2\n")
    (d / "sub" / "b.csv").write_text("z\n")
    first = content_fingerprint(str(d))
    assert first["files"] == 2 and not first["truncated"] and first["digest"]
    assert content_fingerprint(str(d))["digest"] == first["digest"]          # deterministic
    (d / "sub" / "b.csv").write_text("w\n")                                    # same size, new bytes
    assert content_fingerprint(str(d))["digest"] != first["digest"]
    assert content_fingerprint(str(tmp_path / "nope"))["missing"] is True
    monkeypatch.setattr(campaign, "_FP_MAX_FILES", 1)
    assert content_fingerprint(str(d))["truncated"] is True
    monkeypatch.setattr(campaign, "_FP_MAX_FILES", 20_000)
    monkeypatch.setattr(campaign, "_FP_HASH_BYTES", 0)
    sampled = content_fingerprint(str(d))
    assert sampled["sampled_files"] == 2 and sampled["hashed_bytes"] == 0


def test_add_refresh_and_remove_version_the_mount_content(tmp_path, monkeypatch):
    root, client = _client(tmp_path, monkeypatch)
    rd, store = _make_run(root, "camp", _repo(tmp_path), nodes=(0,), metrics={0: 0.5})
    data = tmp_path / "e7"
    data.mkdir()
    (data / "train.csv").write_text("u,i\n1,2\n")
    r = _amend(client, "camp", store, "task", task={"data_add": {"e7": str(data)}},
               reason="new E7 data")
    assert r.status_code == 200, r.text
    added = r.json()["amendment"]["payload"]["data_content"]["e7"]
    assert added["before"] is None and added["after"]["files"] == 1
    # Same declaration, new bytes: a refresh records both fingerprints and that they differ.
    (data / "train.csv").write_text("u,i\n1,3\n")
    r = _amend(client, "camp", store, "task", action="t2", task={"data_refresh": ["e7"]},
               reason="regenerated E7 with the dedup fix")
    assert r.status_code == 200, r.text
    payload = r.json()["amendment"]["payload"]
    pair = payload["data_content"]["e7"]
    assert pair["before"]["digest"] == added["after"]["digest"] != pair["after"]["digest"]
    assert payload["data_content_changed"] == ["e7"] and payload["diff"] == ""
    view = client.get("/api/runs/camp/protocol", headers=OWNER).json()
    assert view["data_content"]["e7"]["fingerprint"]["digest"] == pair["after"]["digest"]
    assert view["protocol_version"] == 2
    assert _amend(client, "camp", store, "task", action="t3",
                  task={"data_refresh": ["nope"]}).status_code == 400
    r = _amend(client, "camp", store, "task", action="t4", task={"data_remove": ["e7"]})
    assert r.status_code == 200, r.text
    gone = r.json()["amendment"]["payload"]["data_content"]["e7"]
    assert gone["before"]["digest"] == pair["after"]["digest"] and gone["after"] is None
