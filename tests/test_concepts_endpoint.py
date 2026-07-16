"""Phase 3c: the GET /api/runs/{id}/concepts serve endpoint — per-lens hierarchy + per-concept
metrics/Δ + the lens pack, end to end (fold -> read-models -> JSON). Pure; recomputed each call."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient                       # noqa: E402

from looplab.events.eventstore import EventStore                # noqa: E402
from looplab.serve.server import make_app                       # noqa: E402


def _demo_run(root):
    rd = root / "demo"
    rd.mkdir(parents=True, exist_ok=True)
    s = EventStore(rd / "events.jsonl")
    s.append("run_started", {"run_id": "demo", "task_id": "toy", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": "r",
                                       "concepts": ["loss/contrastive/dcl", "architecture/moe"]}})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.9})
    s.append("node_created", {"node_id": 1, "parent_ids": [0], "operator": "improve",
                              "idea": {"operator": "improve", "params": {}, "rationale": "r",
                                       "concepts": ["loss/contrastive/mnr"]}})
    s.append("node_evaluated", {"node_id": 1, "metric": 0.7})
    return rd


def test_concepts_endpoint_is_a_lens(tmp_path):
    _demo_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    r = client.get("/api/runs/demo/concepts")
    assert r.status_code == 200
    data = r.json()
    assert data["lens"] == "is_a"
    assert [item["name"] for item in data["lenses"]][0] == "is_a"  # the lens pack ships, is_a default
    nodes = data["tree"]["nodes"]
    # the is_a tree materializes the full path chain from the authored deep tags
    assert {"loss", "loss/contrastive", "loss/contrastive/dcl", "loss/contrastive/mnr",
            "architecture", "architecture/moe"} <= set(nodes)
    assert nodes["loss/contrastive/dcl"]["tagged"] is True
    assert nodes["loss"]["tagged"] is False                       # synthetic ancestor group
    # per-concept metrics reach the UI (multi-membership node 0 counts fully in both its concepts)
    rows = data["metrics"]["rows"]
    assert rows["loss/contrastive/dcl"]["best"] == 0.9
    assert rows["architecture/moe"]["best"] == 0.9
    assert data["touch"]["loss/contrastive/dcl"] == 1


def test_concepts_endpoint_unknown_run_is_handled(tmp_path):
    client = TestClient(make_app(tmp_path))
    r = client.get("/api/runs/nope/concepts")
    assert r.status_code in (200, 404)                           # resolved-empty or not-found, never 500


def test_concepts_endpoint_typed_lens_canonicalizes_edge_endpoints(tmp_path):
    # An edge emitted with a RAW id that a later consolidation retires must project under the CANONICAL
    # id — never resurrect the retired id as an untagged ghost node alongside its canonical twin.
    rd = tmp_path / "demo"
    rd.mkdir(parents=True, exist_ok=True)
    s = EventStore(rd / "events.jsonl")
    s.append("run_started", {"run_id": "demo", "task_id": "toy", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                               "idea": {"operator": "draft", "params": {}, "rationale": "r",
                                        "concepts": ["loss/contrast", "loss/contrastive",
                                                     "architecture/moe"]}})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.9})
    # a "uses" edge authored against the RAW id "loss/contrast" ...
    s.append("concept_edge", {"edges": [{"src": "architecture/moe", "rel": "uses",
                                         "dst": "loss/contrast", "confidence": 0.8,
                                         "provenance": "asserted"}]})
    # ... which a consolidation later renames to the canonical "loss/contrastive"
    s.append("concept_consolidation", {"rename": {"loss/contrast": "loss/contrastive"}})
    client = TestClient(make_app(tmp_path))
    data = client.get("/api/runs/demo/concepts?lens=uses").json()
    assert data["lens"] == "uses"
    assert data["requested_lens"] == "uses"
    assert data["edges_present"] is True
    nodes = data["tree"]["nodes"]
    assert "loss/contrastive" in nodes                           # canonical endpoint present
    assert "loss/contrast" not in nodes                          # retired raw id NOT a ghost node
    # the directed uses-edge makes the canonical concept the parent of architecture/moe
    assert nodes["architecture/moe"]["parent"] == "loss/contrastive"
    # The raw+canonical pair collapses to one membership on node 0; touch is distinct nodes, not tag count.
    assert data["touch"]["loss/contrastive"] == 1


def test_concepts_endpoint_typed_lens_without_edges_falls_back_and_signals(tmp_path):
    # ?lens=uses on a run with no edges honestly reports the EFFECTIVE is_a projection while echoing the
    # requested lens, so the client can tell a fallback from a genuine is_a request.
    _demo_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    data = client.get("/api/runs/demo/concepts?lens=uses").json()
    assert data["lens"] == "is_a"
    assert data["requested_lens"] == "uses"
    assert data["edges_present"] is False


class _LensClient:
    """Fake LLM returning a fixed structured lens emit (tool_call parser)."""
    def __init__(self, out):
        self.out = out

    def complete_tool(self, messages, json_schema):
        return self.out

    def complete_text(self, messages):
        return "x"


def _edge_run(root):
    rd = root / "demo"
    rd.mkdir(parents=True, exist_ok=True)
    s = EventStore(rd / "events.jsonl")
    s.append("run_started", {"run_id": "demo", "task_id": "toy", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": "r",
                                       "concepts": ["agents/orchestrator", "llm/gpt"]}})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.9})
    s.append("concept_edge", {"edges": [{"src": "agents/orchestrator", "rel": "uses",
                                         "dst": "llm/gpt", "confidence": 1.0,
                                         "provenance": "asserted"}]})
    return rd


def test_derive_lens_endpoint_mints_and_projects(tmp_path, monkeypatch):
    _edge_run(tmp_path)
    import looplab.serve.server as server_mod
    monkeypatch.setattr(server_mod, "make_llm_client",
                        lambda *a, **k: _LensClient({"name": "Usage", "label": "By usage", "rels": ["uses"]}))
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/runs/demo/concepts/lens", json={"prompt": "group by what uses what"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["spec"]["rels"] == ["uses"] and data["spec"]["provenance"] == "agent"
    assert data["lens"] == "usage"
    # the minted uses-lens nests agents/orchestrator under llm/gpt and reports metrics per concept
    assert data["tree"]["nodes"]["agents/orchestrator"]["parent"] == "llm/gpt"
    assert data["metrics"]["rows"]["llm/gpt"]["best"] == 0.9


def test_derive_lens_endpoint_soft_fails_when_model_declines(tmp_path, monkeypatch):
    _edge_run(tmp_path)
    import looplab.serve.server as server_mod
    # the model picks a relation not present in the graph -> derive_lens returns None -> soft fail
    monkeypatch.setattr(server_mod, "make_llm_client",
                        lambda *a, **k: _LensClient({"rels": ["teleports_to"]}))
    client = TestClient(make_app(tmp_path))
    data = client.post("/api/runs/demo/concepts/lens", json={"prompt": "nonsense"}).json()
    assert data["ok"] is False and data["reason"] == "declined"


def test_derive_lens_endpoint_soft_fails_offline(tmp_path, monkeypatch):
    _edge_run(tmp_path)
    import looplab.serve.server as server_mod

    def _boom(*a, **k):
        raise RuntimeError("no model configured")
    monkeypatch.setattr(server_mod, "make_llm_client", _boom)
    client = TestClient(make_app(tmp_path))
    data = client.post("/api/runs/demo/concepts/lens", json={"prompt": "group by usage"}).json()
    assert data["ok"] is False and data["reason"] == "no_model"


def test_derive_lens_endpoint_requires_a_prompt(tmp_path):
    _edge_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    assert client.post("/api/runs/demo/concepts/lens", json={"prompt": "  "}).status_code == 400
    assert client.post("/api/runs/demo/concepts/lens", json={}).status_code == 400


def test_concepts_get_replays_a_derived_lens_via_rels(tmp_path):
    # A derived lens is reproducible without another LLM call: GET with &rels=<subset> projects the exact
    # relation subset, so the derived lens refetches as the run grows like any default lens.
    _edge_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    data = client.get("/api/runs/demo/concepts?lens=usage&rels=uses").json()
    assert data["lens"] == "usage"
    assert data["tree"]["nodes"]["agents/orchestrator"]["parent"] == "llm/gpt"
    # metrics are lens-independent (per-concept), so they still populate under the replayed lens
    assert data["metrics"]["rows"]["llm/gpt"]["best"] == 0.9
