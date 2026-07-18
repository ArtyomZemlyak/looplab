"""The process diagram must move with the Part IV/V architecture it documents."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIAGRAM = ROOT / "docs" / "infographic" / "agent-architecture.html"


def test_part_iv_v_flows_are_present_in_architecture_infographic():
    text = DIAGRAM.read_text(encoding="utf-8")
    required = {
        "run base event": "run_concepts",
        "operator retag event": "concept_tag_edited",
        "assistant governance provider": "ConceptGovernanceTools",
        "governance mutation set": "merge · purge/unpurge · split",
        "portfolio concept read": "cross_run_concept_map",
        "per-node concept read family": "concept tree·nodes·delta",
        "profit semantics": "direction-normalized concept profit",
        "taxonomy serialization": "ledger CAS",
    }
    missing = [label for label, token in required.items() if token not in text]
    assert not missing, f"Part IV/V flows missing from architecture infographic: {missing}"
    assert "diagram does not show" not in text
