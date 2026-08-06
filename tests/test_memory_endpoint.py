"""GET /api/memory must NOT be swallowed by the /api/{kind} catch-all (it was declared after it and 404'd
"unknown kind" the whole time, leaving the Memory panel silently empty). Regression: it returns the split
tiers, and /api/knowledge still resolves via the authoring catch-all."""
from __future__ import annotations

import json

import orjson
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.serve.server import make_app  # noqa: E402
import looplab.serve.routers.misc as misc_router  # noqa: E402


def test_memory_endpoint_not_shadowed_by_kind(tmp_path):
    c = TestClient(make_app(tmp_path))
    r = c.get("/api/memory")
    assert r.status_code == 200, "memory must resolve, not 404 as an 'unknown kind'"
    body = r.json()
    assert set(("dir", "cases", "lessons", "notes")) <= set(body)   # the split tiers are present


def test_knowledge_kind_still_resolves(tmp_path):
    c = TestClient(make_app(tmp_path))
    r = c.get("/api/knowledge")
    assert r.status_code == 200 and "files" in r.json()             # served by the authoring catch-all


def test_saved_memory_dir_drives_memory_and_atlas_reads(tmp_path, monkeypatch):
    # A UI-saved global default must govern the owner read surfaces immediately; requiring the operator to
    # duplicate it in the process environment made Settings appear successful while Atlas stayed empty.
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", "")
    memory_dir = tmp_path / "portfolio-memory"
    memory_dir.mkdir()
    (memory_dir / "lessons.jsonl").write_bytes(orjson.dumps({
        "statement": "saved memory is visible",
        "outcome": "supported",
        "evidence": [1],
        "run_id": "prior-run",
        "task_id": "task-a",
    }) + b"\n")
    client = TestClient(make_app(tmp_path / "runs"))

    saved = client.put("/api/settings", json={"settings": {"memory_dir": str(memory_dir)}})
    assert saved.status_code == 200

    memory = client.get("/api/memory")
    assert memory.status_code == 200
    assert memory.json()["dir"] == str(memory_dir)
    assert memory.json()["lessons"][0]["statement"] == "saved memory is visible"

    atlas = client.get("/api/cross-run/atlas")
    assert atlas.status_code == 200
    assert atlas.json()["n_claims"] == 1 and atlas.json()["n_runs"] == 1


def test_memory_endpoint_is_allowlisted_row_safe_and_bounded(tmp_path, monkeypatch):
    memory_dir = tmp_path / "portfolio-memory"
    memory_dir.mkdir()
    monkeypatch.setattr(misc_router, "_MEMORY_TIER_LIMIT", 3)
    monkeypatch.setattr(misc_router, "_MEMORY_SOURCE_ROWS", 20)

    cases = [
        [],
        {"task_id": "bad-params", "params": {"nested": {"too": {"deep": "value"}}}},
        *[{"task_id": f"task-{index}", "goal": "g", "direction": "min", "metric": index,
           "params": {"depth": index}} for index in range(5)],
    ]
    (memory_dir / "cases.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in cases), encoding="utf-8")
    (memory_dir / "lessons.jsonl").write_text(
        '{broken\n[]\n'
        + "".join(json.dumps({"statement": f"lesson-{index}", "run_id": f"r{index}"}) + "\n"
                  for index in range(5)), encoding="utf-8")
    (memory_dir / "meta_notes.jsonl").write_text(
        json.dumps({"task_id": "t", "note": "usable note"}) + "\n", encoding="utf-8")
    # This is an operator-governance ledger, not an episodic case tier.
    (memory_dir / "claim_decisions.jsonl").write_text(
        json.dumps({"task_id": "must-not-appear", "note": "not a case"}) + "\n", encoding="utf-8")

    client = TestClient(make_app(tmp_path / "runs"))
    assert client.put("/api/settings", json={"settings": {"memory_dir": str(memory_dir)}}).status_code == 200
    payload = client.get("/api/memory").json()

    assert payload["projection"] == "bounded_recent_tail"
    assert [row["task_id"] for row in payload["cases"]] == ["task-2", "task-3", "task-4"]
    assert [row["statement"] for row in payload["lessons"]] == ["lesson-2", "lesson-3", "lesson-4"]
    assert payload["notes"] == [{"note": "usable note", "task_id": "t"}]
    assert payload["page"]["truncated"] is True
    assert payload["page"]["tiers"]["lessons"]["skipped"] == 2
    assert "must-not-appear" not in json.dumps(payload)


def test_memory_endpoint_bounds_source_bytes_and_oversized_rows(tmp_path, monkeypatch):
    memory_dir = tmp_path / "portfolio-memory"
    memory_dir.mkdir()
    monkeypatch.setattr(misc_router, "_MEMORY_SOURCE_BYTES", 500)
    monkeypatch.setattr(misc_router, "_MEMORY_ROW_BYTES", 180)
    (memory_dir / "lessons.jsonl").write_text(
        "".join(json.dumps({"statement": "old-" + "x" * 80 + str(index)}) + "\n"
                  for index in range(20))
        + json.dumps({"statement": "z" * 300}) + "\n"
        + json.dumps({"statement": "recent usable"}) + "\n",
        encoding="utf-8",
    )
    client = TestClient(make_app(tmp_path / "runs"))
    assert client.put("/api/settings", json={"settings": {"memory_dir": str(memory_dir)}}).status_code == 200

    payload = client.get("/api/memory").json()
    receipt = payload["page"]["tiers"]["lessons"]

    assert payload["lessons"][-1]["statement"] == "recent usable"
    assert receipt["source_window_truncated"] is True and receipt["skipped"] >= 1


def test_lesson_evidence_and_generations_reach_the_wire(tmp_path):
    """A lesson's node provenance is on disk; it must not be dropped at the HTTP boundary.

    The scalar `evidence_count` alone lets a UI say "3 experiments supported this" and never say
    WHICH, so no per-experiment surface can answer "what did this experiment teach us".
    `evidence_sig` is the only thing binding a credited id to the node ATTEMPT it was distilled
    against — without it a re-run node silently inherits its previous life's conclusions.
    """
    memory_dir = tmp_path / "mem"
    memory_dir.mkdir()
    (memory_dir / "lessons.jsonl").write_text(
        json.dumps({
            "statement": "contrastive loss helped", "outcome": "supported",
            "run_id": "run-a", "task_id": "t", "evidence": [4, 2], "evidence_count": 3,
            "evidence_sig": {"4": "v2:a=1:t=0:x=0:evaluated:0.9", "2": "v2:a=0:t=0:x=0:evaluated:0.4"},
        }) + "\n"
        # A row whose ids carry no signature at all — real for pre-`evidence_sig` history, and for a
        # row whose consolidation base came from another run.
        + json.dumps({
            "statement": "no signatures here", "run_id": "run-a", "evidence": [9],
        }) + "\n",
        encoding="utf-8")
    client = TestClient(make_app(tmp_path / "runs"))
    assert client.put("/api/settings", json={"settings": {"memory_dir": str(memory_dir)}}).status_code == 200

    lessons = client.get("/api/memory").json()["lessons"]
    assert lessons[0]["evidence"] == [4, 2]
    assert lessons[0]["evidence_generations"] == {"4": 1, "2": 0}
    assert lessons[0]["evidence_count"] == 3, "the consolidation scalar is unchanged beside it"
    # Absent, not `{}` and not a null-valued map: "attempt unrecorded" must be distinguishable from
    # "attempt 0", which is precisely what a pair list with a null half would blur.
    assert lessons[1]["evidence"] == [9]
    assert "evidence_generations" not in lessons[1]


def test_lesson_evidence_refs_reject_malformed_ids_and_are_bounded(tmp_path, monkeypatch):
    memory_dir = tmp_path / "mem"
    memory_dir.mkdir()
    monkeypatch.setattr(misc_router, "_MEMORY_EVIDENCE_MAX", 3)
    (memory_dir / "lessons.jsonl").write_text(
        json.dumps({
            "statement": "hostile evidence", "run_id": "r",
            # bools are ints in Python; negatives, strings, nulls and duplicates all have to go.
            "evidence": [True, -1, "7", None, 5, 5, 6, 8, 11, 12],
            "evidence_sig": {"5": "not-a-signature", "6": "v2:a=2:t=0:x=0:evaluated:1"},
        }) + "\n", encoding="utf-8")
    client = TestClient(make_app(tmp_path / "runs"))
    assert client.put("/api/settings", json={"settings": {"memory_dir": str(memory_dir)}}).status_code == 200

    row = client.get("/api/memory").json()["lessons"][0]
    assert row["evidence"] == [5, 6, 8], "malformed ids dropped, duplicates collapsed, cap honoured"
    assert row["evidence_generations"] == {"6": 2}, "an unparseable signature yields no attempt"


def test_memory_run_filter_is_applied_inside_the_source_window(tmp_path, monkeypatch):
    """`?run_id=` must narrow BEFORE the per-tier cap, or a busy store hides the run entirely.

    Filtering the already-capped result would report "this run contributed nothing" whenever enough
    newer rows from other runs sat in front of it — the exact failure a per-experiment view would
    then render as "absorbed".
    """
    memory_dir = tmp_path / "mem"
    memory_dir.mkdir()
    monkeypatch.setattr(misc_router, "_MEMORY_TIER_LIMIT", 3)
    rows = [{"statement": "mine", "run_id": "wanted", "task_id": "t"}]
    rows += [{"statement": f"theirs-{index}", "run_id": "other", "task_id": "t"} for index in range(10)]
    (memory_dir / "lessons.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (memory_dir / "meta_notes.jsonl").write_text(
        json.dumps({"task_id": "t", "note": "mine", "run_id": "wanted"}) + "\n"
        + json.dumps({"task_id": "t", "note": "theirs", "run_id": "other"}) + "\n", encoding="utf-8")
    client = TestClient(make_app(tmp_path / "runs"))
    assert client.put("/api/settings", json={"settings": {"memory_dir": str(memory_dir)}}).status_code == 200

    unfiltered = client.get("/api/memory").json()
    assert [row["statement"] for row in unfiltered["lessons"]] == ["theirs-7", "theirs-8", "theirs-9"], \
        "the oldest row is genuinely outside the cap when unfiltered"

    scoped = client.get("/api/memory", params={"run_id": "wanted"}).json()
    assert [row["statement"] for row in scoped["lessons"]] == ["mine"], \
        "the filter must run before the cap, not after it"
    assert [row["note"] for row in scoped["notes"]] == ["mine"]
    receipt = scoped["page"]["tiers"]["lessons"]
    # A deliberately excluded row is not an unreadable one. Folding the two would make store-health
    # reasoning (and the UI's "absence proves nothing" gate) read a healthy filter as damage.
    assert receipt["filtered"] == 10 and receipt["skipped"] == 0
    assert receipt["returned"] == 1


def test_memory_without_run_filter_is_unchanged(tmp_path):
    """The parameter is additive: absent, this is byte-for-byte the whole-store projection."""
    memory_dir = tmp_path / "mem"
    memory_dir.mkdir()
    (memory_dir / "lessons.jsonl").write_text(
        json.dumps({"statement": "a", "run_id": "one"}) + "\n"
        + json.dumps({"statement": "b", "run_id": "two"}) + "\n"
        + json.dumps({"statement": "c"}) + "\n", encoding="utf-8")
    client = TestClient(make_app(tmp_path / "runs"))
    assert client.put("/api/settings", json={"settings": {"memory_dir": str(memory_dir)}}).status_code == 200

    assert client.get("/api/memory").json() == client.get("/api/memory", params={"run_id": ""}).json()
    assert [row["statement"] for row in client.get("/api/memory").json()["lessons"]] == ["a", "b", "c"]
    # A run id nobody wrote yields an honest empty result, not every row.
    assert client.get("/api/memory", params={"run_id": "nope"}).json()["lessons"] == []
