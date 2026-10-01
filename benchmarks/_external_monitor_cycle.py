"""Answer real advisory monitor questions in the private MCP recovery fixture."""


async def drain_monitors(client, nid):
    pending = (await client.read("harness-checkpoints"))["pending"]
    for question in pending:
        if question["node_id"] != nid or question["phase_id"] != "train_monitor":
            continue
        progress = await client.progress()
        assert progress["next_step"]["code"] == "answer_checkpoint"
        assert progress["next_step"]["phase_id"] == "monitor"
        assert "continue, watch" in progress["next_step"]["detail"]
        assert "does not grant abort authority" in progress["next_step"]["detail"]
        await client.call("phases", {"query": "monitor"})
        phase = await client.call("phase_info", {"phase_id": "monitor"})
        assert phase["write_access"][progress["next_step"]["action"]] == "external_agent"
        assert question["kill_enabled"] is False
        assert question["observation"].strip()
        node = (await client.state())["state"]["nodes"][str(nid)]
        assert node["status"] == "pending" and node["metric"] is None
        body = {"expected_generation": client.generation, "checkpoint_id": question["checkpoint_id"],
                "action_id": "monitor:" + question["checkpoint_id"], "verdict": "continue",
                "reason": "Reviewed actual protected command output. This first advisory question grants no early-stop authority; terminal scoring remains the engine's responsibility."}
        await client.request("POST", "harness-checkpoints", {**body, "verdict": "abort"}, status=400)
        assert any(q["checkpoint_id"] == question["checkpoint_id"]
                   for q in (await client.read("harness-checkpoints"))["pending"])
        await client.request("POST", "harness-checkpoints", body)
        assert (await client.request("POST", "harness-checkpoints", body))["replayed"]
        client.monitor_answers.append({"node_id": nid, "attempt": question["node_generation"],
            "checkpoint_id": question["checkpoint_id"], "advisory_abort_refused": True})
