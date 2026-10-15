"""Runtime parameters (doc 77 §2): the dotenv carrier, the flat parameter projection and its
fenced read route. A key the documents do not state is ABSENT, never zero; secrets never leave."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from looplab.core import param_carriers
from looplab.core.models import Idea, Node
from looplab.serve import runtime_params as rp


def test_dotenv_grammar_is_the_common_subset_and_never_expands():
    src = (
        "# comment\n"
        "\n"
        "export LR=2e-4\n"
        "BATCH = 1024  # inline comment\n"
        "MODEL='Qwen2.5-0.5B # not a comment'\n"
        'NOTE="a \\"quoted\\" word\\nnext"\n'
        "PATHY=${HOME}/x\n"
        "QUOTED_NUM=\"512\"\n"
        "not an assignment\n"
        "1BAD=3\n"
        "LR=3e-4\n"                                   # last wins
        "OPEN='unterminated\n"
    )
    got = param_carriers.dotenv_assignments(src)
    assert got["LR"] == ("3e-4", 11, False)
    assert got["BATCH"][0] == "1024"
    assert got["MODEL"] == ("Qwen2.5-0.5B # not a comment", 5, True)
    assert got["NOTE"][0] == 'a "quoted" word\nnext'
    assert got["PATHY"][0] == "${HOME}/x"
    assert got["OPEN"][0] == "unterminated"
    assert "1BAD" not in got and "not an assignment" not in got
    assert list(got)[-1] == "OPEN"
    numbers = param_carriers.dotenv_numeric_paths(src)
    assert numbers[("LR",)] == (3e-4, 11) and numbers[("BATCH",)] == (1024.0, 4)
    assert ("QUOTED_NUM",) not in numbers and ("MODEL",) not in numbers


def test_dotenv_carrier_names():
    for name in ("experiment.env", "config/box.env", ".env", ".env.local", "A\\b.ENV"):
        assert param_carriers.is_dotenv_carrier(name), name
    for name in ("env.py", "environment.yaml", "envs/x.json", ""):
        assert not param_carriers.is_dotenv_carrier(name), name
    # The engine's dotted-path rung is unchanged: an env file is not a document carrier there.
    assert not param_carriers.is_document_carrier("experiment.env")


def _node(files, params=None, nid=0, attempt=0):
    return Node(id=nid, attempt=attempt, operator="draft",
                idea=Idea(operator="draft", params=params or {}),
                files=files)


def test_projection_reads_authored_carriers_masks_secrets_and_compares_declared():
    node = _node({
        "MiniOneRec/looplab/experiment.env": "NIGHT_ARM=D3\nLR=2e-4\nHF_TOKEN=hf_" + "a" * 30 + "\n",
        "conf/train.yaml": "train:\n  batch_size: 512\n  lr: 0.001\nmodel: qwen\nlayers: [64, 32]\n"
                           "api_key: sk-abcdef\n",
        "solution.py": "print(1)\n",
    }, params={"LR": 2e-4, "batch_size": 1024, "seed": 42, "night_arm": 3})
    out = rp.runtime_params(node, workdir=None)
    sources = {(c["path"], c["origin"], c["status"]) for c in out["carriers"]}
    assert sources == {("MiniOneRec/looplab/experiment.env", "authored", "read"),
                       ("conf/train.yaml", "authored", "read")}
    rows = {r["key"]: r for r in out["params"]}
    assert rows["NIGHT_ARM"]["value"] == "D3" and rows["NIGHT_ARM"]["line"] == 1
    assert rows["LR"]["numeric"] is True
    assert rows["HF_TOKEN"]["value"] == "***" and rows["HF_TOKEN"]["masked"] is True
    assert rows["api_key"]["value"] == "***"
    assert "hf_" + "a" * 30 not in json.dumps(out) and "sk-abcdef" not in json.dumps(out)
    assert rows["train.batch_size"]["value"] == "512"
    assert rows["layers"]["value"] == "[64, 32]"
    cmp = {c["param"]: c["match"] for c in out["comparisons"]}
    assert cmp == {"LR": "same", "batch_size": "different", "seed": "absent", "night_arm": "different"}


def test_projection_withholds_secret_named_files_and_refuses_bad_documents():
    node = _node({".env": "OPENAI_API_KEY=sk-x\n", "bad.json": "{not json", "list.yaml": "- 1\n- 2\n"})
    out = rp.runtime_params(node, workdir=None)
    status = {c["path"]: c["status"] for c in out["carriers"]}
    assert status == {".env": "withheld", "bad.json": "unparsed", "list.yaml": "unparsed"}
    assert out["params"] == []


def test_an_unprintable_integer_is_a_carrier_status_not_a_raise():
    """Review 2026-10-15: YAML and TOML read a hex literal of any size, and `str()` of an int past
    the 4 300-digit limit raises ValueError — as a value, inside a list, or as a YAML key. It escaped
    `runtime_params` (a 500 on the route); the module's contract is "every failure is a carrier
    status". The other carriers are still read."""
    huge = "0x" + "f" * 5000
    node = _node({"a.yaml": f"lr: 1\nbig: {huge}\n", "b.toml": f"big = {huge}\n",
                  "c.yaml": f"layers: [1, {huge}]\n", "d.yaml": f"{huge}: 1\n",
                  "ok.env": "EPOCHS=3\n"})
    out = rp.runtime_params(node, workdir=None)
    status = {c["path"]: c["status"] for c in out["carriers"]}
    assert status == {"a.yaml": "unparsed", "b.toml": "unparsed", "c.yaml": "unparsed",
                      "d.yaml": "unparsed", "ok.env": "read"}
    assert [r["key"] for r in out["params"]] == ["EPOCHS"]
    json.dumps(out)


def test_projection_reads_literal_edit_surface_from_the_workdir(tmp_path):
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "base.env").write_text("EPOCHS=3\n", encoding="utf-8")
    outside = tmp_path.parent / "outside.env"
    outside.write_text("LEAK=1\n", encoding="utf-8")
    node = _node({"cfg/arm.env": "ARM=A\n"})
    out = rp.runtime_params(node, workdir=tmp_path, edit_surface=[
        "cfg/arm.env", "./cfg/base.env", "cfg/missing.env", "**/*.env", "../outside.env",
        "src/train.py"])
    by_path = {c["path"]: (c["origin"], c["status"]) for c in out["carriers"]}
    assert by_path == {"cfg/arm.env": ("authored", "read"),
                       "cfg/base.env": ("workspace", "read"),
                       "cfg/missing.env": ("workspace", "absent"),
                       "../outside.env": ("workspace", "absent")}
    keys = {(r["key"], r["origin"]) for r in out["params"]}
    assert keys == {("ARM", "authored"), ("EPOCHS", "workspace")}


def test_conflicting_carriers_are_a_conflict_not_a_tie_break():
    node = _node({"a.env": "LR=1\n", "b.json": json.dumps({"lr": 2})}, params={"lr": 1})
    out = rp.runtime_params(node, workdir=None)
    (row,) = out["comparisons"]
    assert row["match"] == "conflict" and len(row["sources"]) == 2
    # …while an exact key beats a longer key that merely ends in it (resolve_declaration's rung 1).
    node = _node({"a.env": "LR=1\n", "b.json": json.dumps({"opt": {"lr": 2}})}, params={"lr": 1})
    (row,) = rp.runtime_params(node, workdir=None)["comparisons"]
    assert row["match"] == "same" and [s["source"] for s in row["sources"]] == ["a.env"]


# ------------------------------------------------------------------------------- the HTTP route

pytest.importorskip("fastapi")


def _route_run(tmp_path: Path):
    from looplab.events.eventstore import EventStore
    rd = tmp_path / "demo"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "max"})
    store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"arm_index": 0}, "rationale": "seed"},
        "code": "", "files": {"looplab/experiment.env": "NIGHT_ARM=D3\nSECRET_PASSWORD=hunter22hunter\n"}})
    (rd / "task.snapshot.json").write_text(json.dumps({
        "kind": "repo", "editable_path": str(tmp_path),
        "edit_surface": ["looplab/experiment.env", "config/box.env"]}), encoding="utf-8")
    workdir = rd / "nodes" / "node_0"
    (workdir / "config").mkdir(parents=True)
    (workdir / "config" / "box.env").write_text("BASE_MODEL=Qwen2.5-0.5B\n", encoding="utf-8")
    return rd


def test_route_returns_the_table_and_fences_generation_and_attempt(tmp_path):
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app
    _route_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    gen = client.get("/api/runs/demo/state").json()["generation"]
    url = "/api/runs/demo/nodes/0/runtime-params"
    r = client.get(url, params={"expected_generation": gen, "attempt": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_generation"] == gen and body["node_id"] == 0 and body["attempt"] == 0
    rows = {(p["key"], p["origin"]): p["value"] for p in body["params"]}
    assert rows == {("NIGHT_ARM", "authored"): "D3", ("SECRET_PASSWORD", "authored"): "***",
                    ("BASE_MODEL", "workspace"): "Qwen2.5-0.5B"}
    assert "hunter22hunter" not in r.text
    (only,) = body["comparisons"]
    assert only["param"] == "arm_index" and only["declared"] == 0 and only["match"] == "absent"
    stale = client.get(url, params={"expected_generation": "0" * 64, "attempt": 0})
    assert stale.status_code == 409
    moved = client.get(url, params={"expected_generation": gen, "attempt": 1})
    assert moved.status_code == 409 and moved.json()["detail"]["code"] == "node_attempt_changed"
    assert client.get("/api/runs/demo/nodes/7/runtime-params",
                      params={"expected_generation": gen, "attempt": 0}).status_code == 404
