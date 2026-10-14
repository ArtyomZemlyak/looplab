"""Review 2026-10-14 §6 / §6.3 and Codex audit L-06: the report's history, its duplicates, its figures.

* The fold keeps ONE report; `GET /report/versions` lists every `report_generated` row through the
  fold's own sanitizer, marks the current one and the repeats (`duplicate_of`).
* An identical re-send is a `noop` with a `report_unchanged` notice, so a copy no longer counts as a
  new report; a changed word is a new version.
* A figure is uploaded from an artifact through the `/artifact/raw` fences, stored content-addressed
  beside the run, named by id in `content.figures`, and served back with nosniff + sandbox CSP.
"""
from __future__ import annotations

import base64
import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from looplab.core.config import Settings  # noqa: E402
from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.events.replay import fold  # noqa: E402
from looplab.serve.report_history import report_digest, report_versions  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402
from tests.factories import http_run_generation, post_command  # noqa: E402

AGENT = {"X-LoopLab-Token": "agent-secret"}
OWNER = {"X-LoopLab-Token": "operator-secret"}
# A 1x1 PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "operator-secret")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-secret")
    rd = tmp_path / "demo"
    rd.mkdir(parents=True)
    settings = Settings(backend="toy", external_harness=True, deep_research_every=-1,
                        report_every=0, track_hypotheses=False)
    (rd / "config.snapshot.json").write_text(json.dumps(settings.model_dump(mode="json")))
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "run_uid": "one", "task_id": "task",
                                 "goal": "g", "direction": "min"})
    store.append("pause", {})
    return TestClient(make_app(tmp_path)), rd


def _report(client, content, key):
    response = post_command(client, "report_generated", {"content": content}, key=key,
                            headers=AGENT)
    assert response.status_code == 200, response.text
    return response.json()


def test_an_identical_report_is_a_noop_and_a_changed_one_is_a_new_version(tmp_path, monkeypatch):
    client, rd = _client(tmp_path, monkeypatch)
    first = _report(client, {"headline": "D3 wins", "verdict": "D3 beats the base."}, "r1")
    assert first["status"] != "rejected", first
    again = _report(client, {"headline": "D3 wins", "verdict": "D3 beats the base."}, "r2")
    assert again["status"] == "noop", again
    notice = next(n for n in again["intake_notices"] if n["kind"] == "report_unchanged")
    assert notice["current_seq"] == fold(EventStore(rd / "events.jsonl").read_all()).report[
        "published_seq"]
    changed = _report(client, {"headline": "D3 wins", "verdict": "D3 beats the base by 3 %."}, "r3")
    assert changed["status"] != "noop"
    events = EventStore(rd / "events.jsonl").read_all()
    assert sum(e.type == "report_generated" for e in events) == 2
    listed = client.get("/api/runs/demo/report/versions", headers=OWNER).json()
    assert listed["total"] == 2 and listed["distinct"] == 2
    assert [v["current"] for v in listed["versions"]] == [True, False]
    assert listed["versions"][0]["publisher"] == "external_agent"
    old = listed["versions"][1]["seq"]
    body = client.get(f"/api/runs/demo/report/versions/{old}", headers=OWNER).json()
    assert body["content"]["verdict"] == "D3 beats the base."
    assert client.get("/api/runs/demo/report/versions/999", headers=OWNER).status_code == 404


def test_history_marks_the_duplicates_an_old_log_already_holds(tmp_path):
    """A log written before the noop rule holds identical re-sends; the history names them."""
    rd = tmp_path / "old"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "old", "task_id": "t", "goal": "g", "direction": "min"})
    for at_node in (1, 2, 3):
        store.append("report_generated", {"content": {"headline": "same"}, "at_node": at_node,
                                          "trigger": "external"})
    store.append("report_generated", {"content": {"headline": "new"}, "at_node": 4,
                                      "trigger": "external"})
    events = store.read_all()
    state = fold(events)
    listed = report_versions(events, current_seq=state.report["published_seq"])
    seqs = [v["seq"] for v in listed["versions"]]
    first_same = seqs[-1]
    assert [v.get("duplicate_of") for v in listed["versions"]] == [None, first_same, first_same, None]
    assert listed["distinct"] == 2 and listed["versions"][0]["current"] is True
    # Placement is not content: the same words at another node are the same report.
    assert report_digest({"headline": "same", "at_node": 1}) == report_digest(
        {"headline": "same", "at_node": 9, "published_seq": 4})


def test_a_figure_is_uploaded_from_a_fenced_artifact_and_rendered_by_id(tmp_path, monkeypatch):
    client, rd = _client(tmp_path, monkeypatch)
    (rd / "plots").mkdir()
    (rd / "plots" / "recall.png").write_bytes(PNG)
    (rd / "plots" / "fake.png").write_bytes(b"<svg onload=alert(1)>")
    generation = http_run_generation(client, "demo", headers=AGENT)
    uploaded = client.post("/api/runs/demo/report-figures", headers=AGENT, json={
        "root": "run", "path": "plots/recall.png", "expected_generation": generation,
        "caption": "Recall by week"})
    assert uploaded.status_code == 200, uploaded.text
    figure = uploaded.json()
    assert figure["mime"] == "image/png" and len(figure["figure_id"]) == 64
    # The same fences as /artifact/raw: a mislabelled body and a traversal are refused.
    assert client.post("/api/runs/demo/report-figures", headers=AGENT, json={
        "root": "run", "path": "plots/fake.png",
        "expected_generation": generation}).status_code == 415
    assert client.post("/api/runs/demo/report-figures", headers=AGENT, json={
        "root": "run", "path": "../../etc/passwd",
        "expected_generation": generation}).status_code == 404
    # A report may only name an uploaded figure.
    refused = post_command(client, "report_generated", {"content": {
        "headline": "x", "figures": [{"figure_id": "0" * 64, "caption": "nope"}]}},
        key="bad-fig", headers=AGENT)
    assert refused.json()["status"] == "rejected" or refused.status_code == 400
    published = _report(client, {"headline": "With a plot", "figures": [
        {"figure_id": figure["figure_id"], "caption": "Recall by week",
         "source": figure["source"]}]}, "fig-1")
    assert published["status"] != "rejected", published
    report = fold(EventStore(rd / "events.jsonl").read_all()).report
    assert report["figures"] == [{"figure_id": figure["figure_id"], "caption": "Recall by week",
                                  "mime": "image/png",
                                  "source": {"root": "run", "path": "plots/recall.png"}}]
    served = client.get(f"/api/runs/demo/report-figures/{figure['figure_id']}", headers=OWNER)
    assert served.status_code == 200 and served.content == PNG
    assert served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in served.headers["content-security-policy"]
    # A stored file edited in place is not served under its old id.
    stored = next((rd / "report_figures").glob("*.png"))
    stored.write_bytes(PNG + b"x")
    assert client.get(f"/api/runs/demo/report-figures/{figure['figure_id']}",
                      headers=OWNER).status_code == 404
    assert client.get("/api/runs/demo/report-figures/not-an-id", headers=OWNER).status_code == 404


def test_a_report_without_figures_folds_exactly_as_before():
    from looplab.core.advisory_payloads import sanitize_report_payload
    assert "figures" not in sanitize_report_payload({"headline": "h"})
    kept = sanitize_report_payload({"headline": "h", "figures": [
        {"figure_id": "a" * 64, "caption": "c", "mime": "text/html"}, {"figure_id": "../x"}, 7]})
    assert kept["figures"] == [{"figure_id": "a" * 64, "caption": "c"}]
