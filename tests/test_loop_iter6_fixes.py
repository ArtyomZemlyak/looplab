"""Regression tests for deferred review findings cleared in the hourly review loop (iter 6):
benign-key over-redaction, projects.json per-key coercion, and gap-safe node-id allocation."""
from __future__ import annotations

import json
from pathlib import Path

from looplab.events.eventstore import EventStore  # noqa: F401  (kept for symmetry with replay tests)
from looplab.serve.projects import ProjectStore
from looplab.trust.redact import redact_secrets
from looplab.events.replay import fold

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


# --------------------------------------------------------------------------- redact over-masking
def test_benign_token_fields_not_overmasked():
    # Field NAMES that merely contain a credential substring ("token") but are benign diagnostics
    # must NOT be masked — operators rely on these in the persisted stdout tail.
    assert redact_secrets("tokenizer=gpt2") == "tokenizer=gpt2"
    assert redact_secrets("max_tokens: 1024") == "max_tokens: 1024"
    assert redact_secrets("usage: total_tokens=512") == "usage: total_tokens=512"


def test_real_secret_fields_still_redacted():
    # The broad key-name match is preserved: genuine secret fields are still masked.
    for s in ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIKDENGbPxRfiCY",
              "db_password=hunter2hunter2",
              "MY_API_KEY=abcd1234efgh"):
        masked = redact_secrets(s)
        secret = s.split("=", 1)[1]
        assert "***" in masked and secret not in masked


# --------------------------------------------------------------------------- projects per-key coercion
def test_projects_load_coerces_wrong_typed_keys(tmp_path):
    # A hand-edited projects.json that IS a dict but has a wrong-typed inner key must be coerced to
    # the skeleton type for that key, not left to TypeError downstream (_index / assign).
    (tmp_path / "projects.json").write_text(json.dumps({"assignments": [], "projects": "oops"}))
    data = ProjectStore(tmp_path / "projects.json").load()
    assert data["assignments"] == {}     # list -> {} (skeleton)
    assert data["projects"] == []        # str  -> [] (skeleton)
    assert data["labels"] == {}          # missing key -> default


def test_projects_load_preserves_wellformed(tmp_path):
    good = {"projects": [{"id": "p1", "name": "X"}], "assignments": {"r1": "p1"},
            "labels": {}, "supertasks": [], "supertask_assignments": {}}
    (tmp_path / "p.json").write_text(json.dumps(good))
    assert ProjectStore(tmp_path / "p.json").load() == good


# --------------------------------------------------------------------------- gap-safe node-id alloc
def test_create_node_id_is_gap_safe(tmp_path):
    # A dropped/malformed node_created leaves a GAP in node ids (fold skips the bad event). The next
    # created node must take max(id)+1, NOT len(nodes) — len would collide with an existing higher id
    # and silently overwrite it (corrupting lineage/best-selection). Regression for that bug.
    from looplab.engine.orchestrator import Engine
    from looplab.search.policy import GreedyTree
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.adapters.toytask import ToyTask

    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    eng = Engine(tmp_path / "gap", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=8))
    eng.store.append("run_started", {"run_id": "gap", "task_id": "t", "direction": "min"})
    eng.store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": ""}})
    # node id 2 with NO id 1 -> a gap, as if node 1's event was dropped by fold's malformed-event guard
    eng.store.append("node_created", {"node_id": 2, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {"x": 9.0}, "rationale": ""}})
    assert set(fold(eng.store.read_all()).nodes) == {0, 2}   # gap at 1; len(nodes)==2 would hit node 2

    eng._create_node({"kind": "draft"})

    after = fold(eng.store.read_all())
    assert 2 in after.nodes and after.nodes[2].idea.params["x"] == 9.0   # node 2 NOT overwritten
    assert 3 in after.nodes                                              # new node took max+1, not len(=2)
