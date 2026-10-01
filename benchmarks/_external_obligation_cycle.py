"""Evidence-based report/review completion for the disposable MCP recovery probe."""
import json


async def settle_obligations(client, *, expanding):
    progress = await client.progress()
    assert progress["complete"] and not progress["finish_pending_node_count"]
    phases = {row["phase_id"] for row in progress["candidate_blockers_if_expanding"]} if expanding else set()
    if progress["finish_report_due"]:
        phases.add("report")
    phases.update(progress["finish_reviews_due"])
    assert phases <= {"concept_tags", "research", "report", "lessons", "skill_candidates"}, phases
    state = (await client.state())["state"]
    evidence = [int(nid) for nid, node in state["nodes"].items() if node["status"] in ("evaluated", "failed")]
    measured = [{"node_id": nid, "status": state["nodes"][str(nid)]["status"],
                 "attempt": state["nodes"][str(nid)]["attempt"],
                 "metric": state["nodes"][str(nid)]["metric"]} for nid in evidence]
    for phase_id in sorted(phases):
        await client.call("phases", {"query": phase_id})
        phase = await client.call("phase_info", {"phase_id": phase_id})
        key = f"obligations:{phase_id}:{progress['evidence_revision']}"
        if phase_id == "concept_tags":
            assert phase["write_access"]["command:run_concepts"] == "external_agent"
            await client.command("run_concepts", {"concepts": ["model/linear"]}, key)
        elif phase_id == "research":
            assert phase["write_access"]["command:research_completed"] == "external_agent"
            await client.command("research_completed", {"memo": {
                "summary": "Reviewed the protected deterministic SGD scorer and measured history. Only steps are editable; repeatability across seeds is untested.",
                "open_questions": ["How does declared training duration affect held-out MSE?"],
                "claims": []}}, key)
        elif phase_id == "report":
            assert phase["write_access"]["command:report_generated"] == "external_agent"
            await client.command("report_generated", {"content": {
                "headline": "Protected SGD recovery acceptance",
                "verdict": "Protocol evidence only; one deterministic task cannot establish ML robustness.",
                "summary": json.dumps(measured)}}, key)
        else:
            action = "POST /api/runs/{run_id}/harness-reviews"
            assert phase["write_access"][action] == "external_agent"
            body = {"expected_generation": client.generation, "phase_id": phase_id,
                "action_id": key, "decision": "no_applicable_action", "evidence": evidence,
                "reason": "One deterministic recovery fixture has no independent seeds or distinct tasks supporting a reusable lesson or skill."}
            await client.request("POST", "harness-reviews", body)
            assert (await client.request("POST", "harness-reviews", body))["replayed"]
        assert (await client.progress())["complete"]
    current = await client.progress()
    assert not current["finish_report_due"] and not current["finish_reviews_due"]
    if expanding:
        assert not current["candidate_blockers_if_expanding"]
    return {"phases": sorted(phases), "evidence": evidence,
            "expansion_only_left": current["candidate_blockers_if_expanding"]}
