"""Every discoverable decision needs an actual read and an admissible external action."""
from looplab.core.prompts import PROMPT_KEYS, UNGOVERNED_PROMPT_FAMILIES
from looplab.harness.phases import PHASES, phase_catalog, phase_detail


def test_phase_catalog_covers_all_promptstore_decisions_and_legacy_families():
    assert {key for phase in PHASES for key in phase.prompts} == set(PROMPT_KEYS)
    assert {phase.legacy_prompt_family for phase in PHASES
            if phase.legacy_prompt_family} == {
                family for family, _ in UNGOVERNED_PROMPT_FAMILIES}
    assert all(phase.reads and phase.writes and phase.internal for phase in PHASES)
    assert phase_detail("research")["entity"] == "ResearchMemo"
    assert phase_catalog("research")[0]["id"] == "research"
    assert phase_detail("absent") is None


def test_external_writes_resolve_to_live_commands_or_http_routes(tmp_path):
    from looplab.serve.protocol import CONTROL_EVENTS
    from looplab.serve.server import make_app

    paths = make_app(tmp_path).openapi()["paths"]
    for phase in PHASES:
        for ref in (*phase.reads, *phase.writes):
            if ref.startswith("command:"):
                assert ref.removeprefix("command:") in CONTROL_EVENTS, (phase.id, ref)
                continue
            method, path = ref.split(" ", 1)
            assert path in paths and method.lower() in paths[path], (phase.id, ref)
