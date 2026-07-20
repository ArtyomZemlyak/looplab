"""The process diagram must move with the Part IV/V architecture it documents."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIAGRAM = ROOT / "docs" / "infographic" / "agent-architecture.html"
ONE_PAGER = ROOT / "docs" / "infographic" / "architecture-one-pager.svg"


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


def test_research_claim_one_pager_names_current_receipts_and_defaults():
    text = ONE_PAGER.read_text(encoding="utf-8")
    assert "v3 scoped evidence + receipts" in text
    assert "product flag ON · library default OFF" in text
    assert "v2 scoped evidence store" not in text
    assert "reads opt-in" not in text


def test_finalize_diagram_keeps_paid_stewards_before_the_cost_boundary():
    text = DIAGRAM.read_text(encoding="utf-8")
    # CODEX AGENT: all three paid stewards must precede llm_cost or their usage disappears from the
    # terminal roll-up; the diagram is an operator-facing architecture contract, not decorative copy.
    ordered = ["→ reflection", "→ concept steward → claim steward",
               "→ task facets → llm_cost → completion"]
    positions = [text.index(token) for token in ordered]
    assert positions == sorted(positions)
