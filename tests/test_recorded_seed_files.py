"""Recorded base browsing is bounded, generation/attempt fenced and never reads the live repo."""
import hashlib
import json
import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from looplab.engine.seed_archive import capture_seed_archive
from looplab.events.eventstore import EventStore
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.serve import seed_files as projection
from looplab.serve.server import make_app


def _run(root, *, train=b"print('recorded runner')\n"):
    rd, src = root / "archive", root / "owner"
    rd.mkdir(); src.mkdir()
    (src / "train.py").write_bytes(train)
    (src / "recipe.env").write_bytes(b"MOMENTUM=0.2\n")
    (src / "binary.dat").write_bytes(b"\x00\xff")
    (src / "large.txt").write_bytes(b"x" * (projection.TEXT_LIMIT + 1))
    events = EventStore(rd / "events.jsonl")
    events.append("run_started", {"run_id": "archive", "task_id": "repo", "goal": "g", "direction": "min"})
    events.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft", "code": "",
        "files": {"recipe.env": "MOMENTUM=0.3\n"},
        "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    receipt = capture_seed_archive(src, rd / "base_snapshots")
    seed = events.append("workspace_seeded", {"node_id": 0, "generation": 0, "base_revision": receipt})
    receipt = {**receipt, "node_id": 0, "generation": 0, "seed_event_seq": seed.seq}
    events.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 1,
        "metric_provenance": {"base_revision": receipt}})
    client = TestClient(make_app(root))
    generation = client.get("/api/runs/archive/state").json()["generation"]
    return client, rd, src, events, receipt, {"expected_generation": generation, "attempt": 0}


def _read(client, params):
    return client.get("/api/runs/archive/nodes/0/seed-files", params=params)


def test_paged_inventory_and_text_use_recorded_base_not_overlay_or_changed_owner(tmp_path):
    client, rd, src, _, receipt, params = _run(tmp_path)
    before = hashlib.sha256((rd / "events.jsonl").read_bytes()).hexdigest()
    (src / "train.py").write_bytes(b"owner changed\n")
    page = _read(client, {**params, "limit": 2})
    assert page.status_code == 200
    body = page.json()
    assert body["scope"] == "recorded_seed_before_mounts_and_overlay"
    assert body["base_digest"] == receipt["digest"]
    assert body["total"] == 4 and body["next_offset"] == 2 and body["file"] is None
    next_page = _read(client, {**params, "limit": 2, "offset": 2}).json()
    assert next_page["next_offset"] is None
    assert [row["path"] for row in body["files"] + next_page["files"]] == [
        "binary.dat", "large.txt", "recipe.env", "train.py"]
    train = _read(client, {**params, "path": "train.py"}).json()["file"]
    assert train["text"] == "print('recorded runner')\n" and train["text_status"] == "utf8"
    assert train["sha256"] == hashlib.sha256(train["text"].encode()).hexdigest()
    recipe = _read(client, {**params, "path": "recipe.env"}).json()["file"]
    assert recipe["text"] == "MOMENTUM=0.2\n", "pre-overlay archive is labelled and is not the node recipe"
    assert hashlib.sha256((rd / "events.jsonl").read_bytes()).hexdigest() == before


def test_binary_large_path_and_corrupt_archive_do_not_supply_partial_or_live_text(tmp_path):
    client, rd, _, _, receipt, params = _run(tmp_path)
    for path, status in [("binary.dat", "binary"), ("large.txt", "too_large")]:
        file = _read(client, {**params, "path": path}).json()["file"]
        assert file["text_status"] == status and file["text"] is None
    assert _read(client, {**params, "path": "../owner/train.py"}).status_code == 409
    (rd / receipt["archive"]["path"] / "train.py").write_bytes(b"corrupt")
    refusal = _read(client, params)
    assert refusal.status_code == 409 and refusal.json()["detail"]["code"] == "seed_archive_unavailable"


@pytest.mark.parametrize("data, status", [
    ("\ufffd".encode(), "utf8"),
    ("кириллица 🚀\n".encode(), "utf8"),
    (b"\xef\xbb\xbfBOM\r\n", "utf8"),
    (b"", "utf8"),
    (b"\xed\xa0\x80", "binary"),  # An encoded surrogate is not UTF-8 source text.
    (b"\xff", "binary"),
    (b"\x00", "binary"),
])
def test_archive_unicode_is_exact_or_explicitly_binary(tmp_path, data, status):
    client, _, src, _, _, params = _run(tmp_path, train=data)
    (src / "train.py").write_bytes(b"changed owner text")
    response = _read(client, {**params, "path": "train.py"})
    assert response.status_code == 200
    file = response.json()["file"]
    assert file["text_status"] == status
    assert file["bytes"] == len(data) and file["sha256"] == hashlib.sha256(data).hexdigest()
    if status == "utf8":
        assert file["text"].encode("utf-8") == data
    else:
        assert file["text"] is None, "invalid source bytes are never replaced with invented text"


def test_executed_main_code_and_archived_entrypoint_remain_distinct_sources(tmp_path):
    rd, src, wd = tmp_path / "entrypoint", tmp_path / "owner", tmp_path / "workdir"
    for directory in (rd, src, wd):
        directory.mkdir()
    base = "print((0.0 - 3.0) ** 2)\n"
    code = "import json\nx = 0.0\nfor _ in range(100):\n    x -= 0.2 * (x - 3.0)\nprint(json.dumps({'metric': (x - 3.0) ** 2}))\n"
    (src / "solution.py").write_bytes(base.encode())
    (wd / "solution.py").write_bytes(base.encode())
    events = EventStore(rd / "events.jsonl")
    events.append("run_started", {"run_id": "entrypoint", "task_id": "quadratic", "goal": "min quadratic", "direction": "min"})
    events.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
        "code": code, "files": {}, "deleted": [],
        "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    receipt = capture_seed_archive(src, rd / "base_snapshots")
    seed = events.append("workspace_seeded", {"node_id": 0, "generation": 0, "base_revision": receipt})
    receipt = {**receipt, "node_id": 0, "generation": 0, "seed_event_seq": seed.seq}
    result = SubprocessSandbox().run(code, str(wd), timeout=10)
    assert result.exit_code == 0 and not result.timed_out, result.stderr
    assert result.metric is not None and 0 <= result.metric < 9
    assert (wd / "solution.py").read_bytes() == code.encode()
    events.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": result.metric,
        "metric_provenance": {"base_revision": receipt}})
    before = hashlib.sha256((rd / "events.jsonl").read_bytes()).hexdigest()
    client = TestClient(make_app(tmp_path))
    generation = client.get("/api/runs/entrypoint/state").json()["generation"]
    params = {"expected_generation": generation, "attempt": 0, "path": "solution.py"}
    response = client.get("/api/runs/entrypoint/nodes/0/seed-files", params=params)
    assert response.status_code == 200
    file = response.json()["file"]
    detail = client.get("/api/runs/entrypoint/nodes/0").json()
    assert file["text"] == base and detail["code"] == code
    assert detail["files"] == {} and detail["metric"] == result.metric
    assert hashlib.sha256((rd / "events.jsonl").read_bytes()).hexdigest() == before


@pytest.mark.skipif(os.name != "nt", reason="native case/separator materialization requires Windows")
@pytest.mark.parametrize("path, files, deleted, expected", [
    ("Readme.md", {"README.md": "edited\n"}, [], b"edited\n"),
    ("Readme.md", {}, ["README.md"], None),
    ("pkg/recipe.env", {"pkg\\RECIPE.env": "edited\n"}, [], b"edited\n"),
])
def test_windows_alias_edits_change_workdir_without_changing_the_recorded_base(
        tmp_path, path, files, deleted, expected):
    from types import SimpleNamespace
    from looplab.engine.seed_archive import verified_seed_archive
    from looplab.engine.workspace import WorkspaceSeeder
    src, rd, wd = tmp_path / "source", tmp_path / "run", tmp_path / "workdir"
    for directory in (src, rd, wd):
        directory.mkdir()
        if directory != rd:
            target = directory / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"base\n")
    receipt = capture_seed_archive(src, rd / "base_snapshots")
    WorkspaceSeeder(SimpleNamespace(_assets={}, _repo_spec={})).write_node_files(
        SimpleNamespace(files=files, deleted=deleted), wd)
    if expected is None:
        assert not (wd / path).exists()
    else:
        assert (wd / path).read_bytes() == expected
    seen = {}
    assert verified_seed_archive(rd, receipt, on_file=lambda name, data, executable:
        seen.__setitem__(name, data)) is not None
    assert seen == {path: b"base\n"}, "the readable archive is not the final workdir file"


def test_attempt_and_generation_are_required_and_reset_during_verification_is_refused(tmp_path, monkeypatch):
    client, _, _, events, _, params = _run(tmp_path)
    assert _read(client, {}).status_code == 422
    assert _read(client, {**params, "expected_generation": "f" * 64}).status_code == 409
    assert _read(client, {**params, "limit": 201}).status_code == 422
    verify = projection.verified_seed_archive
    def raced(*args, **kwargs):
        result = verify(*args, **kwargs)
        events.append("node_reset", {"node_id": 0, "generation": 0})
        return result
    monkeypatch.setattr(projection, "verified_seed_archive", raced)
    response = _read(client, params)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "node_attempt_changed"


def test_unbound_or_missing_receipt_does_not_grant_source_access(tmp_path):
    client, rd, _, events, receipt, params = _run(tmp_path)
    # An evaluated receipt referencing a different event cannot authorize the existing bytes.
    events.append("node_created", {"node_id": 1, "parent_ids": [], "operator": "draft", "code": "",
        "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    events.append("node_evaluated", {"node_id": 1, "generation": 0, "metric": 1,
        "metric_provenance": {"base_revision": {**receipt, "node_id": 1}}})
    response = client.get("/api/runs/archive/nodes/1/seed-files", params=params)
    assert response.status_code == 409
    assert "bound" in response.json()["detail"]["message"]


@pytest.mark.parametrize("during_read", [False, True])
def test_damaged_event_tail_never_grants_a_verified_prefix_read(tmp_path, monkeypatch, during_read):
    client, rd, _, _, _, params = _run(tmp_path)
    def damage():
        with (rd / "events.jsonl").open("ab") as stream:
            stream.write(b'{"incomplete":')
    if during_read:
        verify = projection.verified_seed_archive
        def raced(*args, **kwargs):
            result = verify(*args, **kwargs)
            damage()
            return result
        monkeypatch.setattr(projection, "verified_seed_archive", raced)
    else:
        damage()
    response = _read(client, params)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "seed_archive_unavailable"
    assert "events.jsonl" in response.json()["detail"]["message"]


def test_replaced_base_receipt_in_same_attempt_is_refused(tmp_path, monkeypatch):
    client, rd, _, _, receipt, params = _run(tmp_path)
    verify = projection.verified_seed_archive
    def raced(*args, **kwargs):
        result = verify(*args, **kwargs)
        log = rd / "events.jsonl"
        rows = [json.loads(line) for line in log.read_bytes().splitlines()]
        rows[-1]["data"]["metric_provenance"]["base_revision"] = {**receipt, "seed_event_seq": 999}
        log.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows))
        return result
    monkeypatch.setattr(projection, "verified_seed_archive", raced)
    response = _read(client, params)
    assert response.status_code == 409
    assert "evidence changed" in response.json()["detail"]["message"]
