"""Phase 3c: the GET /api/runs/{id}/concepts serve endpoint — per-lens hierarchy + per-concept
metrics/Δ + the lens pack, end to end (fold -> bounded core -> pure lens projection -> JSON)."""
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


def _assert_frame_parity(data):
    """The UI must be able to consume one internally coherent, self-contained frame."""
    included = data["completeness"]["included"]
    ref_count = sum(len(refs) for refs in data["experiment_refs"].values())
    assert included["memberships"] == included["experiment_refs"] == ref_count
    assert set(data["touch"]) == set(data["metrics"]["rows"]) == set(data["experiment_refs"])
    integrity = data["completeness"]["source_integrity"]
    assert data["authority"]["source_authoritative"] is (
        integrity["complete"] and integrity["generation_identified"])


def test_concepts_endpoint_is_a_lens(tmp_path):
    _demo_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    r = client.get("/api/runs/demo/concepts")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    data = r.json()
    assert data["schema"] == 1 and data["status"] == "complete"
    assert data["generation"] and len(data["generation"]) == 64
    assert data["captured_seq"] == data["max_seq"] == 4
    assert data["requested_seq"] is None and data["historical"] is False
    assert data["authoritative"] is True and data["authority"] == {
        "authoritative": True,
        "source_authoritative": True,
        "complete": True,
        "scope": "captured_recoverable_event_prefix",
        "semantic_claims_verified": False,
    }
    assert data["complete"] is True and data["completeness"]["complete"] is True
    assert data["lens"] == "is_a"
    assert data["requested_lens"] == data["effective_lens"] == "is_a"
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
    ref = data["experiment_refs"]["loss/contrastive/dcl"]
    assert ref == [{
        "node_id": 0, "node_generation": 0, "metric": 0.9,
        "metric_kind": "robust_metric", "status": "evaluated", "feasible": True,
        "is_best": True, "membership_provenance": "researcher-authored",
    }]
    assert data["provenance"]["membership_counts"] == {"researcher-authored": 3}
    _assert_frame_parity(data)


def test_concepts_endpoint_unknown_run_is_handled(tmp_path):
    client = TestClient(make_app(tmp_path))
    r = client.get("/api/runs/nope/concepts")
    assert r.status_code == 404
    assert r.headers["cache-control"] == "no-store"


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


def test_concepts_endpoint_uses_current_memberships_not_legacy_cooccurrence_weights(tmp_path):
    rd = tmp_path / "demo"
    rd.mkdir(parents=True, exist_ok=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {
        "run_id": "demo", "task_id": "toy", "goal": "g", "direction": "max",
    })
    store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "r",
                 "concepts": ["a", "b", "c"]},
    })
    store.append("node_created", {
        "node_id": 1, "parent_ids": [0], "operator": "improve",
        "idea": {"operator": "improve", "params": {}, "rationale": "r",
                 "concepts": ["a", "b"]},
    })
    store.append("node_created", {
        "node_id": 2, "parent_ids": [0], "operator": "improve",
        "idea": {"operator": "improve", "params": {}, "rationale": "r",
                 "concepts": ["a", "c"]},
    })
    # Legacy derived receipts are deliberately wrong/stale. Replay ignores them and ConceptFrame
    # derives a-b=2 and a-c=2 from the current three-node membership snapshot.
    store.append("concept_edge", {"edges": [
        {"src": "a", "rel": "co_occurs", "dst": "b",
         "confidence": 999.0, "provenance": "evidenced"},
        {"src": "a", "rel": "co_occurs", "dst": "c",
         "confidence": 777.0, "provenance": "evidenced"},
    ]})

    data = TestClient(make_app(tmp_path)).get(
        "/api/runs/demo/concepts?lens=co_occurs",
    ).json()

    # CODEX AGENT: current repeated co-tagging selects the requested lens without trusting the
    # unretractable max-only cache that older engines wrote.
    assert data["requested_lens"] == data["effective_lens"] == data["lens"] == "co_occurs"
    assert data["edges_present"] is True and data["lens_edges_present"] is True
    assert data["tree"]["nodes"]["b"]["parent"] == "a"
    assert data["tree"]["nodes"]["c"]["parent"] == "a"
    assert data["completeness"]["included"]["edges"] == 2
    assert data["completeness"]["included"]["derived_edges"] == 2
    assert data["completeness"]["source"]["edges"] == 0
    assert "invalid_edge" not in data["completeness"]["reasons"]
    assert data["authoritative"] is True


def test_concepts_endpoint_typed_lens_without_edges_falls_back_and_signals(tmp_path):
    # ?lens=uses on a run with no edges honestly reports the EFFECTIVE is_a projection while echoing the
    # requested lens, so the client can tell a fallback from a genuine is_a request.
    _demo_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    data = client.get("/api/runs/demo/concepts?lens=uses").json()
    assert data["lens"] == "is_a"
    assert data["requested_lens"] == "uses"
    assert data["edges_present"] is False
    assert data["effective_lens"] == "is_a"
    assert data["lens_contract"]["fallback"] == "no_matching_edges"


class _LensClient:
    """Fake LLM returning a fixed structured lens emit (tool_call parser)."""
    def __init__(self, out):
        self.out = out

    def complete_tool(self, messages, json_schema):
        return self.out

    def complete_text(self, messages):
        return "x"


def _lens_body(client, prompt):
    generation = client.get("/api/runs/demo/concepts").json()["generation"]
    return {"prompt": prompt, "expected_generation": generation}


def _lens_headers(key="concept-lens-test"):
    return {"Idempotency-Key": key}


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
    r = client.post("/api/runs/demo/concepts/lens",
                    json=_lens_body(client, "group by what uses what"),
                    headers=_lens_headers())
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    data = r.json()
    assert data["ok"] is True
    assert data["spec"]["rels"] == ["uses"] and data["spec"]["provenance"] == "agent"
    assert data["lens"] == "usage"
    assert data["schema"] == 1 and data["generation"] and data["captured_seq"] == data["max_seq"]
    assert data["requested_lens_spec"] == {
        "name": "usage", "rels": ["uses"], "kind": "edge",
        "registration": "ephemeral-validated",
    }
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
    data = client.post("/api/runs/demo/concepts/lens",
                       json=_lens_body(client, "nonsense"),
                       headers=_lens_headers()).json()
    assert data["ok"] is False and data["reason"] == "declined"


def test_derive_lens_endpoint_soft_fails_offline(tmp_path, monkeypatch):
    _edge_run(tmp_path)
    import looplab.serve.server as server_mod

    def _boom(*a, **k):
        raise RuntimeError("no model configured")
    monkeypatch.setattr(server_mod, "make_llm_client", _boom)
    client = TestClient(make_app(tmp_path))
    data = client.post("/api/runs/demo/concepts/lens",
                       json=_lens_body(client, "group by usage"),
                       headers=_lens_headers()).json()
    assert data["ok"] is False and data["reason"] == "no_model"


def test_derive_lens_endpoint_requires_a_prompt(tmp_path):
    _edge_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    assert client.post("/api/runs/demo/concepts/lens", json={"prompt": "  "}).status_code == 400
    assert client.post("/api/runs/demo/concepts/lens", json={}).status_code == 400


def test_derive_lens_fences_generation_before_paid_provider_call(tmp_path, monkeypatch):
    _edge_run(tmp_path)
    import looplab.serve.server as server_mod

    called = False

    def _must_not_call(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("generation mismatch reached the paid provider")

    monkeypatch.setattr(server_mod, "make_llm_client", _must_not_call)
    client = TestClient(make_app(tmp_path))
    current = client.get("/api/runs/demo/concepts").json()["generation"]
    stale = "0" * 64 if current != "0" * 64 else "1" * 64
    response = client.post("/api/runs/demo/concepts/lens", json={
        "prompt": "group by usage", "expected_generation": stale,
    }, headers=_lens_headers())
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "run_generation_changed"
    assert called is False

    missing = client.post("/api/runs/demo/concepts/lens", json={"prompt": "group by usage"})
    assert missing.status_code == 400
    assert missing.json()["detail"]["code"] == "invalid_run_generation"
    assert called is False


def test_derive_lens_endpoint_writes_only_diagnostic_receipts(tmp_path, monkeypatch):
    # The projection remains domain-replay-clean while paid-work claim and terminal receipts survive
    # process loss in the diagnostic event channel.
    rd = _edge_run(tmp_path)
    log = rd / "events.jsonl"
    before = log.read_text().count("\n")
    import looplab.serve.server as server_mod
    monkeypatch.setattr(server_mod, "make_llm_client",
                        lambda *a, **k: _LensClient({"name": "Usage", "label": "By usage", "rels": ["uses"]}))
    client = TestClient(make_app(tmp_path))
    assert client.post("/api/runs/demo/concepts/lens",
                       json=_lens_body(client, "group by usage"),
                       headers=_lens_headers()).json()["ok"] is True
    events = EventStore(log).read_all()
    assert log.read_text().count("\n") == before + 2
    assert [event.type for event in events[-2:]] == [
        "concept_lens_started", "concept_lens_completed"]


def test_concepts_get_rejects_malformed_rels_without_500(tmp_path):
    # CODEX AGENT: an invalid derived spec must never silently widen to a different projection. Reject
    # empty/mixed/unknown relation sets deterministically and keep even error responses non-cacheable.
    _edge_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    for query in ("rels=", "rels=%20%2C%20", "rels=teleports_to"):
        response = client.get(f"/api/runs/demo/concepts?lens=x&{query}")
        assert response.status_code == 400
        assert response.headers["cache-control"] == "no-store"


def test_concepts_endpoint_bare_concept_gets_a_metrics_row(tmp_path):
    # A single-segment authored concept ("agents") is a legitimate top-level concept: the tree, the
    # touch counts, AND the metric table must all include it (they read one node_concepts input). A node
    # tagged ONLY with a bare id must not become falsely untagged / metric-less.
    rd = tmp_path / "demo"
    rd.mkdir(parents=True, exist_ok=True)
    s = EventStore(rd / "events.jsonl")
    s.append("run_started", {"run_id": "demo", "task_id": "toy", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": "r",
                                       "concepts": ["agents", "loss/contrastive"]}})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.9})
    s.append("node_created", {"node_id": 1, "parent_ids": [0], "operator": "improve",
                              "idea": {"operator": "improve", "params": {}, "rationale": "r",
                                       "concepts": ["agents"]}})       # tagged ONLY with a bare id
    s.append("node_evaluated", {"node_id": 1, "metric": 0.5})
    client = TestClient(make_app(tmp_path))
    data = client.get("/api/runs/demo/concepts").json()
    assert "agents" in data["tree"]["nodes"] and data["tree"]["nodes"]["agents"]["tagged"] is True
    assert data["touch"]["agents"] == 2
    # the bare concept must have a metric row (both nodes touch it, best is 0.9)
    assert data["metrics"]["rows"]["agents"]["best"] == 0.9
    assert data["metrics"]["rows"]["agents"]["touched"] == 2


def test_folded_concepts_edge_collapse_mirrors_fold_tiebreak(tmp_path):
    # On a confidence TIE, the post-rename edge collapse keeps the same survivor the fold would: higher
    # provenance rank (asserted > evidenced) wins, regardless of raw-key order.
    from looplab.serve.routers.runs import _folded_concepts
    from looplab.events.replay import fold
    rd = tmp_path / "demo"
    rd.mkdir(parents=True, exist_ok=True)
    s = EventStore(rd / "events.jsonl")
    s.append("run_started", {"run_id": "demo", "task_id": "toy", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": "r",
                                       "concepts": ["a/x", "b/y"]}})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.9})
    # two edges that collapse to the SAME canonical triple at equal confidence, different provenance
    s.append("concept_edge", {"edges": [{"src": "a/x", "rel": "uses", "dst": "b-raw",
                                         "confidence": 0.5, "provenance": "evidenced"}]})
    s.append("concept_edge", {"edges": [{"src": "a/x", "rel": "uses", "dst": "b/y",
                                         "confidence": 0.5, "provenance": "asserted"}]})
    s.append("concept_consolidation", {"rename": {"b-raw": "b/y"}})
    _nc, _cids, edges, _touch = _folded_concepts(fold(s.read_all()))
    survivor = edges[("a/x", "uses", "b/y")]
    assert survivor["provenance"] == "asserted"        # higher rank wins the confidence tie


def test_concepts_get_replays_a_derived_lens_via_rels(tmp_path):
    # A derived lens is reproducible without another LLM call: GET with &rels=<subset> projects the exact
    # relation subset, so the derived lens refetches as the run grows like any default lens.
    _edge_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    response = client.get("/api/runs/demo/concepts", params={"lens": "Usage View", "rels": "uses,uses"})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["lens"] == data["requested_lens"] == data["effective_lens"] == "usage-view"
    assert data["requested_lens_spec"] == {
        "name": "usage-view", "rels": ["uses"], "kind": "edge",
        "registration": "ephemeral-validated",
    }
    assert data["lens_contract"] == {
        "requested": "usage-view", "effective": "usage-view",
        "registration": "ephemeral-validated", "fallback": None,
    }
    assert data["tree"]["nodes"]["agents/orchestrator"]["parent"] == "llm/gpt"
    # metrics are lens-independent (per-concept), so they still populate under the replayed lens
    assert data["metrics"]["rows"]["llm/gpt"]["best"] == 0.9


def test_concept_frame_historical_identity_comes_from_the_exact_prefix(tmp_path):
    _demo_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    current = client.get("/api/runs/demo/concepts").json()
    historical = client.get("/api/runs/demo/concepts?seq=2").json()
    assert historical["generation"] == current["generation"]
    assert historical["requested_seq"] == historical["captured_seq"] == 2
    assert historical["max_seq"] == 4 and historical["historical"] is True
    assert "loss/contrastive/mnr" not in historical["tree"]["nodes"]
    assert set(historical["experiment_refs"]) == {
        "architecture/moe", "loss/contrastive/dcl",
    }
    _assert_frame_parity(historical)

    before_start = client.get("/api/runs/demo/concepts?seq=-1").json()
    assert before_start["generation"] is None and before_start["captured_seq"] == -1
    assert before_start["max_seq"] == 4 and before_start["historical"] is True
    assert before_start["status"] == "partial" and before_start["complete"] is False
    assert before_start["authoritative"] is False
    assert before_start["authority"]["source_authoritative"] is False
    assert before_start["completeness"]["reasons"] == ["generation_unavailable"]
    assert before_start["tree"]["nodes"] == {} and before_start["experiment_refs"] == {}
    _assert_frame_parity(before_start)


def test_concept_frame_preserves_unknown_feasibility_as_null(tmp_path):
    # CODEX AGENT: legacy/recovery projections can carry tri-state feasibility. Never coerce unknown
    # to False in the transport: that would turn missing evidence into an infeasibility claim.
    from looplab.events.replay import fold
    from looplab.search.concept_graph import default_lenses
    from looplab.serve.concept_frame import build_frame

    rd = _demo_run(tmp_path)
    events = EventStore(rd / "events.jsonl").read_all()
    state = fold(events[:2])
    object.__setattr__(state.nodes[0], "feasible", None)
    frame = build_frame(
        state, run_id="demo", requested_lens="is_a", lens_pack=default_lenses(),
        generation="test-generation", requested_seq=1, captured_seq=1, max_seq=4,
        source_divergence=None)
    assert frame["experiment_refs"]["loss/contrastive/dcl"][0]["feasible"] is None
    _assert_frame_parity(frame)


def test_concept_frame_empty_is_authoritative_not_unavailable(tmp_path):
    rd = tmp_path / "demo"
    rd.mkdir(parents=True)
    EventStore(rd / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "toy", "goal": "g", "direction": "max"})
    response = TestClient(make_app(tmp_path)).get("/api/runs/demo/concepts")
    data = response.json()
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert data["status"] == "complete" and data["authoritative"] is True
    assert data["tree"]["nodes"] == {} and data["experiment_refs"] == {}
    _assert_frame_parity(data)


@pytest.mark.parametrize(("lens", "rels"), [
    ("teleports", None),
    ("usage", ""),
    ("usage", "uses,"),
    ("usage", "uses,teleports_to"),
    ("uses", "uses"),                         # shipped identity cannot be overridden by query rels
    ("x" * 65, "uses"),
])
def test_concept_frame_rejects_unregistered_or_malformed_lens_specs(tmp_path, lens, rels):
    _edge_run(tmp_path)
    params = {"lens": lens}
    if rels is not None:
        params["rels"] = rels
    response = TestClient(make_app(tmp_path)).get("/api/runs/demo/concepts", params=params)
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"


def test_registered_typed_lens_falls_back_when_only_other_relations_exist(tmp_path):
    _edge_run(tmp_path)  # contains one uses edge, but no part_of edge
    data = TestClient(make_app(tmp_path)).get(
        "/api/runs/demo/concepts?lens=part_of").json()
    assert data["edges_present"] is True and data["lens_edges_present"] is False
    assert data["requested_lens"] == "part_of" and data["effective_lens"] == "is_a"
    assert data["lens_contract"]["fallback"] == "no_matching_edges"


def test_concept_frame_caps_memberships_before_expanding_experiment_refs(tmp_path, monkeypatch):
    import looplab.serve.concept_frame as frame_module

    monkeypatch.setattr(frame_module, "MAX_MEMBERSHIPS", 1)
    _demo_run(tmp_path)
    data = TestClient(make_app(tmp_path)).get("/api/runs/demo/concepts").json()
    assert data["status"] == "partial" and data["complete"] is False
    assert data["authoritative"] is False
    assert data["authority"]["source_authoritative"] is True
    assert data["authority"]["complete"] is False
    assert data["completeness"]["truncated"] is True
    assert "membership_cap" in data["completeness"]["reasons"]
    assert data["completeness"]["included"]["experiment_refs"] == 1
    assert sum(map(len, data["experiment_refs"].values())) == 1
    _assert_frame_parity(data)


def test_concept_frame_tree_cap_combines_concept_paths_and_edge_endpoints(tmp_path, monkeypatch):
    import looplab.serve.concept_frame as frame_module

    monkeypatch.setattr(frame_module, "MAX_TREE_NODES", 2)
    rd = tmp_path / "demo"
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "task_id": "toy", "goal": "g", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                   "idea": {"operator": "draft", "params": {}, "rationale": "r",
                                            "concepts": ["a/x"]}})
    store.append("concept_edge", {"edges": [{"src": "a/x", "rel": "uses", "dst": "b/y",
                                                "confidence": 1.0, "provenance": "asserted"}]})
    data = TestClient(make_app(tmp_path)).get("/api/runs/demo/concepts?lens=uses").json()
    assert data["status"] == "partial" and data["authoritative"] is False
    assert "edge_endpoint_cap" in data["completeness"]["reasons"]
    assert len(data["tree"]["nodes"]) <= 2


def test_concept_frame_marks_corrupt_source_prefix_non_authoritative(tmp_path):
    rd = _demo_run(tmp_path)
    with (rd / "events.jsonl").open("ab") as stream:
        stream.write(b"{not-json}\n")
    response = TestClient(make_app(tmp_path)).get("/api/runs/demo/concepts")
    data = response.json()
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert data["status"] == "partial" and data["authoritative"] is False
    assert data["authority"]["source_authoritative"] is False
    assert data["completeness"]["source_integrity"]["complete"] is False
    assert "event_log_corruption" in data["completeness"]["reasons"]
    _assert_frame_parity(data)


def test_derive_lens_caps_body_prompt(tmp_path):
    _edge_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    too_long = client.post("/api/runs/demo/concepts/lens", json={"prompt": "x" * 801})
    assert too_long.status_code == 413 and too_long.headers["cache-control"] == "no-store"
    too_large = client.post("/api/runs/demo/concepts/lens", json={"prompt": "x" * 5_000})
    assert too_large.status_code == 413 and too_large.headers["cache-control"] == "no-store"

def test_derive_lens_mints_against_cap_truncated_partial_frame(tmp_path, monkeypatch):
    # REVIEW(2026-07-16): a cap-truncated (partial) frame is a faithful minting substrate — the SAME
    # bounded frame the GET path serves and the UI renders. A monotone cap must NOT permanently refuse
    # lens minting (the old all-or-nothing `if not complete` gate did, forever, on any large run). The
    # minted frame honestly reports status=partial with the cap reason, but ok=True.
    import looplab.serve.concept_frame as frame_module
    import looplab.serve.server as server_mod

    _edge_run(tmp_path)
    monkeypatch.setattr(server_mod, "make_llm_client",
                        lambda *a, **k: _LensClient({"name": "Usage", "label": "By usage", "rels": ["uses"]}))
    # Patch the cap BEFORE the first materialize: the concept core is cached by file-version, so a cap
    # raised after the GET would not rebuild it. The generation token is the first event, cap-independent.
    monkeypatch.setattr(frame_module, "MAX_MEMBERSHIPS", 1)  # force a monotone membership_cap
    client = TestClient(make_app(tmp_path))
    body = _lens_body(client, "group by usage")
    resp = client.post("/api/runs/demo/concepts/lens", json=body,
                       headers=_lens_headers("cap-truncated-mint"))
    payload = resp.json()
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    assert payload["ok"] is True and payload["lens"] == "usage"       # minted, not refused
    assert payload["status"] == "partial"                            # against the bounded frame
    assert payload["completeness"]["truncated"] is True
    assert "membership_cap" in payload["completeness"]["reasons"]


def test_truncation_cap_reasons_exclude_corruption_adjacent_rename_hop():
    # REVIEW: the POST lens-mint gate classifies "safe to mint against the partial frame" via the EXPLICIT
    # TRUNCATION_CAP_REASONS set, NOT an endswith("_cap") heuristic. rename_hop_cap ends in "_cap" but is a
    # corruption-adjacent UNRESOLVED-IDENTITY signal (rename chain over MAX_RENAME_HOPS drops the concept),
    # classified with its sibling rename_cycle as BLOCKING. This guard catches a suffix-heuristic regression.
    from looplab.serve.concept_frame import TRUNCATION_CAP_REASONS

    assert "rename_hop_cap" not in TRUNCATION_CAP_REASONS      # ends in _cap but must block
    assert "rename_cycle" not in TRUNCATION_CAP_REASONS
    assert "invalid_edge" not in TRUNCATION_CAP_REASONS and "event_log_corruption" not in TRUNCATION_CAP_REASONS
    assert TRUNCATION_CAP_REASONS == frozenset({
        "node_membership_cap", "concepts_per_node_cap", "membership_cap", "tree_node_cap",
        "edge_cap", "edge_endpoint_cap", "experiment_ref_cap"})


def test_derive_lens_refuses_corrupt_source(tmp_path):
    # A corruption-class reason (torn/invalid source) DOES block minting BEFORE the model call, and
    # rides back as a blocking_reasons receipt so the UI can explain WHY it is permanent instead of
    # telling the operator to rephrase a prompt that can never succeed. The generation token is the
    # FIRST event, so a corrupt tail line does not trip the generation guard — it reaches this gate.
    _edge_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    body = _lens_body(client, "group by usage")
    with (tmp_path / "demo" / "events.jsonl").open("ab") as stream:
        stream.write(b"{not-json}\n")
    resp = client.post("/api/runs/demo/concepts/lens", json=body,
                       headers=_lens_headers("corrupt-frame-refusal"))
    payload = resp.json()
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    assert payload["ok"] is False and payload["reason"] == "concept_frame_partial"
    assert "event_log_corruption" in payload["blocking_reasons"]


def test_concept_core_cache_reuses_fold_and_invalidates_every_file_version(tmp_path, monkeypatch):
    # CODEX AGENT: lens changes are view-only. They must not replay the same event prefix, while every
    # byte-version transition (append, corruption, atomic replacement) must force a new generation-safe
    # core and must never turn the process-local optimization into an HTTP-cacheable response.
    import looplab.serve.routers.runs as runs_router

    fold_calls = 0
    core_calls = 0
    real_fold = runs_router.fold
    real_build_core = runs_router._build_concept_core

    def counted_fold(events):
        nonlocal fold_calls
        fold_calls += 1
        return real_fold(events)

    def counted_build_core(*args, **kwargs):
        nonlocal core_calls
        core_calls += 1
        return real_build_core(*args, **kwargs)

    monkeypatch.setattr(runs_router, "fold", counted_fold)
    monkeypatch.setattr(runs_router, "_build_concept_core", counted_build_core)
    rd = _edge_run(tmp_path)
    log = rd / "events.jsonl"
    client = TestClient(make_app(tmp_path))

    first = client.get("/api/runs/demo/concepts")
    first_payload = first.json()
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    assert (fold_calls, core_calls) == (1, 1)
    for params in ({"lens": "uses"}, {"lens": "usage", "rels": "uses"}):
        response = client.get("/api/runs/demo/concepts", params=params)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert (fold_calls, core_calls) == (1, 1)                    # same core, two pure lenses

    historical = client.get("/api/runs/demo/concepts", params={"seq": 2})
    assert historical.status_code == 200 and (fold_calls, core_calls) == (2, 2)
    historical_other_lens = client.get(
        "/api/runs/demo/concepts", params={"seq": 2, "lens": "uses"})
    assert historical_other_lens.status_code == 200 and (fold_calls, core_calls) == (2, 2)

    EventStore(log).append(
        "node_created", {"node_id": 1, "parent_ids": [0], "operator": "improve",
                         "idea": {"operator": "improve", "params": {}, "rationale": "r",
                                  "concepts": ["cache/append"]}})
    appended = client.get("/api/runs/demo/concepts").json()
    assert "cache/append" in appended["tree"]["nodes"]
    assert (fold_calls, core_calls) == (3, 3)
    client.get("/api/runs/demo/concepts", params={"lens": "uses"})
    assert (fold_calls, core_calls) == (3, 3)                    # appended version is reusable too

    with log.open("ab") as stream:
        stream.write(b"{not-json}\n")
    corrupt = client.get("/api/runs/demo/concepts").json()
    assert corrupt["status"] == "partial"
    assert "event_log_corruption" in corrupt["completeness"]["reasons"]
    assert (fold_calls, core_calls) == (4, 4)                    # corruption invalidates clean core
    client.get("/api/runs/demo/concepts", params={"lens": "uses"})
    assert (fold_calls, core_calls) == (4, 4)                    # corrupt prefix stays reusable

    replacement_dir = tmp_path / "replacement"
    replacement_dir.mkdir()
    replacement_log = replacement_dir / "events.jsonl"
    replacement = EventStore(replacement_log)
    replacement.append(
        "run_started", {"run_id": "demo", "task_id": "new", "goal": "new", "direction": "max"})
    replacement.append(
        "node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                         "idea": {"operator": "draft", "params": {}, "rationale": "r",
                                  "concepts": ["replacement/generation"]}})
    replacement_log.replace(log)
    replaced = client.get("/api/runs/demo/concepts").json()
    assert (fold_calls, core_calls) == (5, 5)
    assert replaced["generation"] != first_payload["generation"]
    assert "replacement/generation" in replaced["tree"]["nodes"]
    assert "cache/append" not in replaced["tree"]["nodes"]


def test_concept_core_cache_bounds_prefixes_and_total_entries(monkeypatch):
    import looplab.serve.routers.runs as runs_router

    monkeypatch.setattr(runs_router, "_CONCEPT_CORE_CACHE_MAX_PREFIXES_PER_SOURCE", 2)
    monkeypatch.setattr(runs_router, "_CONCEPT_CORE_CACHE_MAX_ENTRIES", 3)
    cache = runs_router._ConceptCoreCache()
    source_a = ("a/events.jsonl", 1, 10, 100, 100, 100)
    source_b = ("b/events.jsonl", 1, 20, 100, 100, 100)
    source_c = ("c/events.jsonl", 1, 30, 100, 100, 100)
    for seq in (1, 2, 3):
        cache.put(source_a, seq, {"generation": "a"})
    assert len(cache._entries) == 2
    assert {key[1] for key in cache._entries} == {2, 3}

    cache.put(source_b, None, {"generation": "b"})
    cache.put(source_c, None, {"generation": "c"})
    assert len(cache._entries) == 3
    assert all(key[0] != source_a or key[1] == 3 for key in cache._entries)

    # A new byte identity at the same path proactively retires the prior generation's entries.
    replaced_a = ("a/events.jsonl", 1, 11, 101, 101, 101)
    assert cache.get(replaced_a, None) is None
    assert all(key[0][0] != source_a[0] for key in cache._entries)


def test_concept_core_cache_retries_unknown_identity_before_replacement(tmp_path, monkeypatch):
    import looplab.serve.routers.runs as runs_router

    rd = _demo_run(tmp_path)
    log = rd / "events.jsonl"
    replacement_dir = tmp_path / "replacement"
    replacement_dir.mkdir()
    replacement_log = replacement_dir / "events.jsonl"
    replacement = EventStore(replacement_log)
    replacement.append(
        "run_started", {"run_id": "demo", "task_id": "new", "goal": "new", "direction": "max"})
    replacement.append(
        "node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                         "idea": {"operator": "draft", "params": {}, "rationale": "r",
                                  "concepts": ["replacement/stable"]}})

    real_identity = runs_router._concept_event_file_identity
    identity_calls = 0

    def replace_after_unknown_before(path):
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls == 1:
            return None
        if identity_calls == 2:
            replacement_log.replace(log)
        return real_identity(path)

    monkeypatch.setattr(runs_router, "_concept_event_file_identity", replace_after_unknown_before)
    client = TestClient(make_app(tmp_path))
    first = client.get("/api/runs/demo/concepts").json()
    second = client.get("/api/runs/demo/concepts").json()
    assert "replacement/stable" in first["tree"]["nodes"]
    assert "replacement/stable" in second["tree"]["nodes"]
    assert "loss/contrastive/dcl" not in first["tree"]["nodes"]


def test_concept_core_cache_partitions_request_run_id_aliases():
    import looplab.serve.routers.runs as runs_router

    cache = runs_router._ConceptCoreCache()
    identity = ("real/events.jsonl", 1, 10, 100, 100, 100)
    first = {"generation": "g", "run_id": "first-alias"}
    cache.put(identity, None, first)
    assert cache.get(identity, None, run_id="second-alias") is None
    assert cache.get(identity, None, run_id="first-alias") is first
