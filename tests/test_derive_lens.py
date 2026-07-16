"""Phase 3b: derive_lens mints a LENS from a natural-language request. A lens is a pure projection
spec (rel-subset + optional root); it writes no events -> replay-clean. Best-effort: degrades to None
so the caller falls back to a default lens. The result feeds project_lens/project_hierarchy."""
from looplab.search.concept_graph import derive_lens, project_lens


class _LensClient:
    """Fake LLM: returns a fixed lens emit (tool_call)."""
    def __init__(self, out):
        self.out = out

    def complete_tool(self, messages, json_schema):
        return self.out

    def complete_text(self, messages):
        return "x"


class _BoomClient:
    def complete_tool(self, messages, json_schema):
        raise RuntimeError("boom")

    def complete_text(self, messages):
        return "x"


_EDGES = {
    "agents\tuses\tllm": {"src": "agents", "rel": "uses", "dst": "llm", "confidence": 1.0},
    "llm\tco_occurs\trag": {"src": "llm", "rel": "co_occurs", "dst": "rag", "confidence": 3.0},
}


def test_no_client_or_empty_prompt_returns_none():
    assert derive_lens("group by usage", _EDGES, None) is None
    assert derive_lens("   ", _EDGES, _LensClient({"rels": ["uses"]})) is None


def test_mints_a_lens_from_available_rels():
    spec = derive_lens("show what uses what",
                       _EDGES, _LensClient({"name": "Usage", "label": "By usage", "rels": ["uses"]}))
    assert spec["name"] == "usage" and spec["rels"] == ["uses"] and spec["kind"] == "edge"
    assert spec["provenance"] == "agent"
    # the minted spec drives project_lens directly
    h = project_lens(["agents", "llm"], _EDGES, spec)
    assert h["roots"] == ["llm"] and h["nodes"]["agents"]["parent"] == "llm"


def test_is_a_lens_is_path_kind():
    spec = derive_lens("group by family", _EDGES, _LensClient({"rels": ["is_a"]}))
    assert spec["rels"] == ["is_a"] and spec["kind"] == "path"


def test_filters_out_unavailable_rels():
    # the model hallucinates a relation not present in the graph -> filtered -> None
    assert derive_lens("x", _EDGES, _LensClient({"rels": ["teleports_to"]})) is None


def test_root_is_carried_when_valid():
    spec = derive_lens("focus on llm", _EDGES,
                       _LensClient({"rels": ["co_occurs"], "root": "LLM"}))
    assert spec["root"] == "llm"                       # normalized


def test_failure_degrades_to_none():
    assert derive_lens("x", _EDGES, _BoomClient()) is None
