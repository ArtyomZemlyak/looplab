"""Real short SGD/MCP recovery between experiments, with no unsettled nodes.

python -m benchmarks.external_idle_recovery_smoke --out .tmp/new-idle-proof
The private fixture kills its own MCP, optionally its own idle engine, and
restarts its own UI. Reads never resume; a durable explicit resume uses the
production spawner. No provider calls or user server/configuration changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import uvicorn

from benchmarks.claude_harness_smoke import wait_for
from benchmarks.external_asha_smoke import SCORER
from benchmarks.external_session_smoke import Client, until
from benchmarks._external_response_loss import ResponseLossProxy
from benchmarks._external_obligation_cycle import settle_obligations
from looplab.events.eventstore import EventStore
from looplab.harness.mcp_server import HarnessAPI
from looplab.serve.server import make_app


CASES = ("agent_loss", "engine_loss")


def run_case(root, name, quiet_hold_seconds=0, drop_command_response=False, response_fault="disconnect", read_fault="disconnect", discovery_fault="none", mcp_python=None, result_backlog=False, failed_first=False, obligations=False, damaged_journals=False, value_recovery=False, damaged_events=False, knowledge_recovery=False, seed_base_recovery=False):
    root.mkdir(parents=True, exist_ok=False)
    runs = root / "runs"; runs.mkdir()
    source = root / "source"; source.mkdir()
    (source / "score.py").write_text(SCORER, encoding="utf8")
    (source / "config.json").write_text('{}', encoding="utf8")
    if seed_base_recovery:
        (source / "experiment.env").write_text("BASE=old\n", encoding="utf8")
        for args in (["init", "-q"], ["add", "."],
                     ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "base"]):
            subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)
    digest = hashlib.sha256((source / "score.py").read_bytes()).hexdigest()
    task = {"id": "external-idle-recovery", "goal": "Measure real SGD before and after explicit idle recovery.",
        "direction": "min", "repo": str(source), "edit_surface": ["config.json"], "protect": ["score.py"],
        "cmd": {"command": [sys.executable, "score.py"], "timeout": 20,
            "metric": {"reader": "stdout_json", "key": "metric"},
            "stages": [{"name": "train_eval", "role": "training",
                        "command": [sys.executable, "score.py"], "check": False}]}}
    if seed_base_recovery:
        task["protect"] = []  # protection comes from the operator declaration in this probe
        task["cmd"]["protect_entrypoint"] = False
        task["cmd"]["scorer_boundary"] = {"files": ["score.py"]}
    task_path = root / "task.json"; task_path.write_text(json.dumps(task), encoding="utf8")
    token, owner = secrets.token_hex(32), secrets.token_hex(32)
    env_before = dict(os.environ)
    os.environ.update(LOOPLAB_HARNESS_TOKEN=token, LOOPLAB_UI_TOKEN=owner,
        LOOPLAB_MEMORY_DIR=str(root / "memory"), LOOPLAB_KNOWLEDGE_DIR=str(root / "knowledge"))
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("LOOPLAB_")}
    child_env.update(PYTHONIOENCODING="utf-8", CUDA_VISIBLE_DEVICES="")
    with socket.socket() as bind:
        bind.bind(("127.0.0.1", 0)); port = bind.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    server = thread = engine = None
    proxy = None
    knowledge_proxy = None
    owned_engines = []
    proof = {"case": name, "model_judgment_tested": False, "agent_connection": "not_measured",
             "metrics": [], "read_steps": []}
    saved_receipts = []
    if mcp_python:
        probe = subprocess.run([mcp_python, "-c", "import importlib.util,json;v={n:bool(importlib.util.find_spec(n)) for n in ('fastapi','uvicorn')};assert not v['fastapi'];print(json.dumps(v))"],
                               capture_output=True, text=True, timeout=20)
        assert probe.returncode == 0, "The separate MCP interpreter must have no FastAPI package"
        proof["mcp_client_ui_packages"] = json.loads(probe.stdout)
        proof["mcp_ui_imports_blocked"] = True
    if drop_command_response:
        proof["response_fault"] = response_fault
        proof["read_fault"] = read_fault
        proof["discovery_fault"] = discovery_fault
    log = (root / "engine.log").open("w", encoding="utf8")

    def start_ui():
        app = make_app(runs, bind_host="127.0.0.1")
        original_spawn = app.state.looplab.commands.spawn_engine

        def tracked_spawn(*args, **kwargs):
            # Observe only children spawned by THIS private server for owned cleanup.
            from looplab.serve.engine_proc import _spawned_engines

            pid = original_spawn(*args, **kwargs)
            if pid is not None:
                owned_engines.append(_spawned_engines[pid])
            return pid

        app.state.looplab.commands.spawn_engine = tracked_spawn
        value = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        worker = threading.Thread(target=value.run, daemon=True); worker.start()
        wait_for(lambda: value.started, bool)
        return value, worker

    def candidate_data(nid, steps):
        data = {"idea": {"operator": "draft", "footprint": {"gpus": 0},
            "rationale": "Measure the declared training steps with the protected SGD scorer."},
            "files": {"config.json": json.dumps({"steps": steps,
                "lr": "invalid" if failed_first and nid == 0 else .1,
                "delay": .08 if seed_base_recovery and nid == 0 else 0., "delay_from": 1})}}
        if failed_first and nid == 1:
            data.update(parent_id=0, parent_generations={"0": 0})
            data["idea"]["operator"] = "improve"
            data["idea"]["rationale"] = "Correct the invalid learning-rate type observed in the failed parent's stage log."
        if obligations:
            data["idea"]["concepts"] = ["model/linear"]
        return data

    async def candidate(client, nid, steps):
        await client.progress()
        await client.call("phases", {"query": "implementation"})
        await client.call("phase_info", {"phase_id": "implementation"})
        data = candidate_data(nid, steps)
        if obligations:
            settled = await settle_obligations(client, expanding=True, saved_receipts=saved_receipts)
            proof.setdefault("settled_before_candidates", []).append(settled)
            await client.call("phases", {"query": "novelty"})
            await client.call("phase_info", {"phase_id": "novelty"})
            body = {"expected_generation": client.generation, "phase_id": "novelty",
                "action_id": f"idle:novelty:{nid}", "idea": data["idea"], "decision": "submit",
                "reason": "Explicit fixture decision: test the invalid configuration or its authored correction; not an independent novelty model judgment."}
            reply = await client.request("POST", "harness-decisions", body)
            saved_receipts.append(("decisions", body, reply["decision"]))
        key = f"idle:candidate:{nid}"
        if proxy is not None and nid == 0:
            lost = await client.call("api_request", {"method": "POST", "path": "/api/runs/demo/commands",
                "body": {"expected_generation": client.generation, "type": "inject_node", "data": data},
                "idempotency_key": key})
            expected_status = {"disconnect": None, "server_error": 503}.get(response_fault, 200)
            assert lost["status"] == expected_status and lost["outcome"] == "unknown"
            assert lost["code"] == "request_outcome_unknown"
            assert lost["reason"] == {"disconnect": "transport_error", "server_error": "server_error",
                "invalid_json": "invalid_json", "oversized": "response_too_large"}[response_fault]
            receipt = await until(lambda: client.receipt(key), lambda row: row["command"]["status"] == "succeeded")
            replay = await client.command("inject_node", data, key)
            assert replay["command"]["id"] == receipt["command"]["id"]
            proof.update(lost_write=lost, exact_retry_receipt=receipt["command"]["id"])
        else:
            await client.command("inject_node", data, key)
        if seed_base_recovery and nid == 0:
            async def seeded():
                rows = EventStore(runs / "demo" / "events.jsonl").read_all()
                return next((row for row in rows if row.type == "workspace_seeded"
                             and row.data.get("node_id") == 0), None)
            seed = await until(seeded, bool)
            assert seed.data["base_revision"]["complete"]
            assert not any(row.type == "node_evaluated" for row in EventStore(runs / "demo" / "events.jsonl").read_all())
            (source / "experiment.env").write_text("BASE=new\n", encoding="utf8")
            proof["source_changed_before_terminal"] = True
        node = await client.terminal(nid, "failed" if failed_first and nid == 0 else "evaluated")
        if seed_base_recovery:
            revision = node["metric_provenance"]["base_revision"]
            rows = EventStore(runs / "demo" / "events.jsonl").read_all()
            seed = next(row for row in rows if row.seq == revision["seed_event_seq"])
            assert seed.type == "workspace_seeded" and seed.data["node_id"] == nid
            assert revision["digest"] == seed.data["base_revision"]["digest"]
            assert revision["file_count"] == seed.data["base_revision"]["file_count"] == 3
            assert revision["node_id"] == nid and revision["generation"] == 0 and revision["complete"]
            expected = "BASE=old\n" if nid == 0 else "BASE=new\n"
            assert (runs / "demo" / "nodes" / f"node_{nid}" / "experiment.env").read_text(encoding="utf8") == expected
            from looplab.engine.seed_archive import verified_seed_archive
            archived = verified_seed_archive(runs / "demo", revision)
            assert archived is not None
            assert (archived / "experiment.env").read_text(encoding="utf8") == expected
            assert hashlib.sha256((archived / "score.py").read_bytes()).hexdigest() == digest
            boundary = revision["scorer_boundary"]
            assert boundary["complete"] and boundary["declared_files"] == ["score.py"]
            assert boundary["members"][0]["sha256"] == digest
            assert boundary == seed.data["base_revision"]["scorer_boundary"]
            proof["declared_scorer_boundary_verified"] = True
            proof.setdefault("archived_seeds_verified", []).append(nid)
            proof.setdefault("base_revisions", []).append(revision)
            if nid == 1:
                assert proof["base_revisions"][0]["digest"] != revision["digest"]
        if failed_first and nid == 0:
            assert node["metric"] is None
        if failed_first and nid == 1:
            assert node["parent_ids"] == [0] and node["attempt"] == 0
        proof["metrics"].append(node["metric"])
        summary = "SGD и защищённый scoring завершены; результат измерен движком. Прочитайте исходную квитанцию при переподключении и явно выберите следующий эксперимент. Одного запуска недостаточно для вывода о повторяемости."
        if result_backlog:
            # Fixture simulates a reconnect before the agent has interpreted its
            # measured results. Commentary is not an engine admission/finish gate.
            proof.setdefault("deferred_result_nodes", []).append(nid)
        elif proxy is not None and nid == 0:
            await client.progress()
            await client.call("phases", {"query": "result_summary"})
            await client.call("phase_info", {"phase_id": "result_summary"})
            row = next(r for r in (await client.read("result-notices"))["items"]
                       if r["kind"] == "node" and r.get("node_id") == nid)
            body = {"expected_generation": client.generation, "receipt_id": row["id"],
                    "evidence_token": row["evidence_token"], "action_id": f"idle:summary:{nid}", "summary": summary}
            proxy.drop_next_write = "/api/runs/demo/result-notices"
            lost = await client.call("api_request", {"method": "POST", "path": "/api/runs/demo/result-notices", "body": body})
            expected_status = {"disconnect": None, "server_error": 503}.get(response_fault, 200)
            assert lost["status"] == expected_status and lost["outcome"] == "unknown"
            saved = next(r for r in (await client.read("result-notices"))["items"] if r["id"] == row["id"])
            assert saved["commentary"] == summary
            assert (await client.request("POST", "result-notices", body))["replayed"]
            proof.update(lost_commentary=lost, commentary_exact_retry=True)
        else:
            await client.commentary("node", f"idle:summary:{nid}", summary, nid)
        progress = await client.progress()
        assert progress["finish_pending_nodes"] == [] and progress["pending_checkpoint_count"] == 0
        assert progress["next_step"]["code"] == "choose_direction"

    async def first(client):
        if discovery_fault != "none":
            before = (runs / "demo" / "events.jsonl").read_bytes()
            proof["discovery_failures"] = []
            for tool, args in (("operations", {"query": "commands"}),
                               ("operation_schema", {"path": "/api/runs/{run_id}/commands"})):
                proxy.catalog_fault = discovery_fault
                lost = await client.call(tool, args)
                assert lost["status"] == (None if discovery_fault == "disconnect" else 200)
                assert lost["code"] == ("api_unreachable" if discovery_fault == "disconnect" else "response_incomplete")
                assert lost["outcome"] == "unavailable" and lost["at"] == "openapi"
                assert not any(key in lost for key in ("body", "matches", "operations"))
                proof["discovery_failures"].append({"tool": tool, **lost})
                recovered = await client.call(tool, args)
                if tool == "operations":
                    assert any(row["method"] == "POST" and row["path"] == "/api/runs/{run_id}/commands" for row in recovered["matches"])
                else:
                    assert "post" in recovered["operations"] and recovered["components"]
            assert (runs / "demo" / "events.jsonl").read_bytes() == before
            proof["discovery_reads_changed_no_work"] = True
        await candidate(client, 0, 16)
        proof["original_receipt"] = (await client.receipt("idle:candidate:0"))["command"]["id"]
        if read_fault == "incomplete_receipt":
            before = (runs / "demo" / "events.jsonl").read_bytes()
            rejected = await client.request("POST", "commands", {"expected_generation": generation,
                "type": "hint", "data": {"invalid": True}}, "idle:rejected-command")
            assert rejected["status"] == "rejected"
            assert (runs / "demo" / "events.jsonl").read_bytes() == before
        if proxy is not None:
            before = (runs / "demo" / "events.jsonl").read_bytes()
            proxy.drop_next_read = True
            args = {"run_id": "demo", "expected_generation": generation}
            if read_fault in ("wrong_receipt", "incomplete_receipt"):
                args["idempotency_key"] = "idle:rejected-command" if read_fault == "incomplete_receipt" else "idle:candidate:0"
            tool = {"wrong_receipt": "command_receipt", "incomplete_receipt": "command_receipt", "stale_result_generation": "result_notices", "incomplete_result_page": "result_notices"}.get(read_fault, "run_progress")
            lost = await client.call(tool, args)
            assert lost["status"] == (None if read_fault == "disconnect" else 200)
            assert lost["code"] == ("api_unreachable" if read_fault == "disconnect" else "response_incomplete" if read_fault in ("incomplete_result_page", "incomplete_receipt", "incomplete_progress") else "response_context_mismatch")
            assert lost["outcome"] == "unavailable"
            if read_fault != "disconnect":
                assert "body" not in lost
                assert lost["reason"] == ("command_mismatch" if read_fault == "wrong_receipt" else "invalid_result_page" if read_fault == "incomplete_result_page" else "invalid_command_receipt" if read_fault == "incomplete_receipt" else "invalid_progress" if read_fault == "incomplete_progress" else "generation_mismatch")
            if read_fault == "incomplete_receipt":
                observed = await client.receipt("idle:rejected-command")
                assert observed["terminal"] and observed["command"]["status"] == "rejected"
                assert observed["command"]["error_code"] and observed["command"]["event_seq"] is None
                proof["rejected_receipt_recovered_explicitly"] = observed["command"]
            if read_fault == "incomplete_progress":
                proxy.drop_next_read = True
                checked = await client.call("connection_check", args)
                assert checked["status"] == 200 and checked["ok"] is False
                assert checked["code"] == "invalid_response" and "source_health" not in checked
                recovered = await client.call("connection_check", args)
                assert recovered["ok"] and recovered["generation"] == generation
                current = await client.progress()
                assert "candidate_decisions_per_idea" in current and current["complete"]
                proof["incomplete_progress_connection_refused"] = checked
                proof["progress_gates_recovered_explicitly"] = current["candidate_decisions_per_idea"]
            if tool == "result_notices":
                recovered_page = await client.notices()
                assert recovered_page["generation"] == generation
                assert any(row["id"] == "node:0:0" for row in recovered_page["items"])
                proof["result_page_recovered_explicitly"] = True
            assert (await client.receipt("idle:candidate:0"))["command"]["id"] == proof["original_receipt"]
            assert (await client.progress())["complete"]
            assert (runs / "demo" / "events.jsonl").read_bytes() == before
            proof.update(lost_read=lost, failed_read_changed_no_work=True, failed_read_tool=tool)

    async def recovered(client):
        rd = runs / "demo"
        await client.call("phases", {"query": "recovery"})
        await client.call("phase_info", {"phase_id": "recovery"})
        receipt = await client.receipt("idle:candidate:0")
        assert receipt["command"]["id"] == proof["original_receipt"]
        assert receipt["command"]["status"] == "succeeded"
        before = (rd / "events.jsonl").read_bytes()
        for _ in range(3):
            progress = await client.progress()
            assert progress["finish_pending_nodes"] == [] and progress["pending_checkpoint_count"] == 0
            assert not any(progress["recorded_lifecycle"].values())
            assert progress["execution"]["engine_running"] is (name == "agent_loss")
            assert progress["execution"]["agent_connection"] == "not_measured"
            step = progress["next_step"]
            assert step["code"] == ("choose_direction" if name == "agent_loss" else "inspect_lifecycle")
            if name == "engine_loss":
                assert step["phase_id"] == "recovery" and step["action"] is None
                assert "before submitting" in step["detail"]
            proof["read_steps"].append(step["code"])
            await anyio.sleep(.3)
        assert (rd / "events.jsonl").read_bytes() == before
        assert len(owned_engines) == 1
        proof["reconnect_reads_started_no_work"] = True
        if failed_first:
            await client.call("phases", {"query": "repair"})
            phase = await client.call("phase_info", {"phase_id": "repair"})
            assert phase["write_access"]["command:inject_node"] == "external_agent"
            node = await client.read("nodes/0")
            assert node["status"] == "failed" and node["metric"] is None and node["attempt"] == 0
            suffix = f"nodes/0/logs?expected_generation={generation}&attempt=0&tail=8000"
            logs = await client.request("GET", suffix)
            assert logs["run_generation"] == generation and logs["node_id"] == 0 and logs["attempt"] == 0
            assert "TypeError" in logs["stages"]["train_eval"]
            await client.request("GET", suffix.replace("attempt=0", "attempt=1"), status=409)
            await client.request("GET", suffix.replace(generation, "f" * 64), status=409)
            # Accepted inject != successful training. An exact retry restores its
            # original receipt; changed code needs a new command key and new node.
            repeated = await client.command("inject_node", candidate_data(0, 16), "idle:candidate:0")
            assert repeated["command"]["id"] == proof["original_receipt"]
            await client.request("POST", "commands", {"expected_generation": generation,
                "type": "inject_node", "data": candidate_data(1, 32)}, "idle:candidate:0", status=409)
            assert (rd / "events.jsonl").read_bytes() == before
            proof.update(failed_parent_logs_read=True, stale_logs_refused=True,
                         failed_command_exact_retry_no_work=True, changed_command_key_refused=True)
        if name == "engine_loss":
            await client.command("resume", {}, "idle:resume")
            await until(client.progress, lambda row: row["execution"]["engine_running"] is True)
            assert len(owned_engines) == 2
            # Exact retry resolves the same durable command, not another child.
            await client.command("resume", {}, "idle:resume")
            assert len(owned_engines) == 2
        assert (await client.progress())["next_step"]["code"] == "choose_direction"
        await candidate(client, 1, 32)
        if result_backlog:
            async def settled_trust():
                return EventStore(rd / "events.jsonl").read_all()
            # The engine records trust_scan after node_evaluated. Establish that
            # existing lifecycle boundary before attributing any event to reads.
            await until(settled_trust, lambda rows: any(r.type == "trust_scan" and
                r.data.get("node_id") == 1 and r.data.get("generation") == 0 for r in rows))
            before = (rd / "events.jsonl").read_bytes()
            cursor, seen = None, []
            await client.call("phases", {"query": "result_summary"})
            await client.call("phase_info", {"phase_id": "result_summary"})
            while True:
                page = await client.notices(limit=1, cursor=cursor)
                row, = page["items"]
                assert row["kind"] == "node" and row["commentary"] is None
                seen.append(row["node_id"])
                summary = "Результат SGD измерен защищённым scorer и восстановлен после reconnect. Это один запуск; повторяемость ещё не проверена. Следующий эксперимент выберите по измеренным данным."
                if failed_first:
                    if row["node_id"] == 0:
                        assert row["status"] == "failed" and row["score"] is None
                        summary = "Scorer завершился с ошибкой типа learning rate, метрика отсутствует. После чтения лога исправление подано отдельным дочерним узлом; исходная неудача сохранена. Это не завершённое обучение."
                    else:
                        assert row["parents"] == []
                        summary = "Исправленный дочерний SGD измерен защищённым scorer после reconnect. У failed-родителя нет метрики, поэтому сравнение улучшения не поддерживается. Для проверки повторяемости нужны независимые seeds."
                body = {"expected_generation": generation, "receipt_id": row["id"],
                    "evidence_token": row["evidence_token"], "action_id": f"idle:summary:{row['node_id']}",
                    "summary": summary}
                assert not (await client.request("POST", "result-notices", body))["replayed"]
                assert (await client.request("POST", "result-notices", body))["replayed"]
                cursor = page["next_cursor"]
                assert page["has_more"] == (cursor is not None)
                if cursor is None:
                    break
            assert seen == [1, 0], seen
            after = (rd / "events.jsonl").read_bytes()
            assert after == before, after[len(before):].decode("utf8")
            proof.update(backlog_node_order=seen, backlog_commentary_changed_no_work=True,
                         typed_result_notices=True)
        if knowledge_recovery:
            memory = root / "memory"
            lesson = {"expected_generation": generation, "action_id": "idle:lesson",
                "statement": "Use isolated configuration edits and protected evaluation to compare bounded training budgets",
                "outcome": "supported", "evidence": [0, 1]}
            skill = {"expected_generation": generation, "action_id": "idle:skill",
                "lesson_action_id": "idle:lesson",
                "body": "Edit only the declared configuration surface. Keep the scoring source protected. Compare terminal measured outcomes and preserve receipt identities across reconnect. This protocol fixture does not establish general ML robustness."}
            for phase in ("lessons", "skill_candidates"):
                await client.call("phases", {"query": phase})
                await client.call("phase_info", {"phase_id": phase})
            def sources():
                # Attribute authoritative event/knowledge bytes, not server caches
                # or diagnostic files which ordinary reads may update.
                paths = [rd / "events.jsonl"] + [p for p in memory.rglob("*")
                    if p.is_file() and p.suffix in (".jsonl", ".md")]
                return {str(p): p.read_bytes() for p in paths}
            for route, body in (("lessons", lesson), ("skill-candidates", skill)):
                before = (rd / "events.jsonl").read_bytes()
                knowledge_proxy.drop_next_write = "/api/runs/demo/" + route
                lost = await client.call("api_request", {"method": "POST",
                    "path": "/api/runs/demo/" + route, "body": body})
                assert lost["outcome"] == "unknown" and lost["status"] is None
                accepted = sources()
                recovered = await client.request("POST", route, body)
                assert recovered["replayed"] and sources() == accepted
                assert (rd / "events.jsonl").read_bytes() == before
                if route == "lessons":
                    assert "role" not in recovered["lesson"]
                    await client.request("POST", route, {**body, "role": "developer"}, status=409)
                else:
                    assert recovered["skill"]["status"] == "candidate"
                    await client.request("POST", route, {**body, "body": "changed procedure"}, status=409)
            refusals = 0
            for path in (rd / "events.jsonl", memory / "lessons.jsonl", memory / "skill_candidate_actions.jsonl"):
                original = path.read_bytes()
                path.write_bytes(original + b'broken private knowledge recovery record\n')
                before = sources()
                try:
                    actions = [("lessons", lesson), ("skill-candidates", skill)] if path.name == "events.jsonl" else (
                        [("lessons", lesson)] if path.name == "lessons.jsonl" else [("skill-candidates", skill)])
                    for route, body in actions:
                        for payload in (body, {**body, "action_id": body["action_id"] + ":fresh"}):
                            denied = await client.request("POST", route, payload, status=503)
                            assert denied["detail"]["source"] == path.name
                            refusals += 1
                    if path.name == "lessons.jsonl":
                        denied = await client.request("POST", "skill-candidates", {**skill,
                            "action_id": "idle:skill:fresh"}, status=503)
                        assert denied["detail"]["source"] == path.name
                        refusals += 1
                    if path.name != "events.jsonl":
                        phase = "lessons" if path.name == "lessons.jsonl" else "skill_candidates"
                        denied = await client.request("POST", "harness-reviews", {
                            "expected_generation": generation, "action_id": "idle:damaged:" + phase,
                            "phase_id": phase, "decision": "completed", "evidence": [0, 1],
                            "action_ref": "idle:lesson" if phase == "lessons" else "idle:skill",
                            "reason": "A completed review cannot approve an incomplete knowledge source."}, status=503)
                        assert denied["detail"]["source"] == path.name
                        refusals += 1
                    after = sources()
                    assert after == before, [p for p in set(after) | set(before) if after.get(p) != before.get(p)]
                finally:
                    # Explicit fixture operator recovery of its own known-good bytes.
                    path.write_bytes(original)
                for route, body in (("lessons", lesson), ("skill-candidates", skill)):
                    assert (await client.request("POST", route, body))["replayed"]
            assert len((memory / "lessons.jsonl").read_text().splitlines()) == 1
            assert len((memory / "skill_candidate_actions.jsonl").read_text().splitlines()) == 1
            assert knowledge_proxy.dropped == [{"method": "POST", "route": name, "upstream_status": 200}
                for name in ("lessons", "skill-candidates")]
            proof.update(knowledge_lost_acks_recovered=True, knowledge_source_refusals=refusals,
                         knowledge_rows=1, skill_receipts=1, skill_status="candidate",
                         knowledge_recovery_changed_no_engine_work=True)
        if value_recovery:
            await client.call("phases", {"query": "strategy"})
            await client.call("phase_info", {"phase_id": "strategy"})
            await client.command("set_strategy", {"strategy": {"policy": "mcts"}}, "idle:mcts")
            await until(client.progress, lambda row: row["policy_preview"]["policy"] == "mcts")
            await client.call("phases", {"query": "value_estimate"})
            phase = await client.call("phase_info", {"phase_id": "value_estimate"})
            assert phase["write_access"]["POST /api/runs/{run_id}/harness-selection/values"] == "external_agent"
            observed = await client.read("harness-selection")
            assert len(observed["value_candidates"]) == 2 and observed["value_weight"] == .4
            body = {"expected_generation": generation, "action_id": "idle:values",
                "expected_evidence_revision": observed["evidence_revision"],
                "estimates": [{"node_id": n["node_id"], "generation": n["generation"],
                    "value": .5, "rationale": "Scripted recovery belief, not measured quality or a model judgment."}
                    for n in observed["value_candidates"]]}
            first = await client.request("POST", "harness-selection/values", body)
            assert not first["replayed"] and first["count"] == 2
            before = (rd / "events.jsonl").read_bytes()
            assert (await client.request("POST", "harness-selection/values", body))["replayed"]
            await client.request("POST", "harness-selection/values", {**body,
                "expected_evidence_revision": "f" * 64}, status=409)
            assert (rd / "events.jsonl").read_bytes() == before
            assert not (await client.read("harness-selection"))["value_candidates"]
            assert not any(row["phase_id"] == "value_estimate" for row in
                           (await client.progress())["candidate_blockers_if_expanding"])
            greedy = await client.command("set_strategy", {"strategy": {"policy": "greedy"}}, "idle:greedy")
            await until(client.progress, lambda row: row["policy_preview"]["policy"] == "greedy")
            await until(settled_trust, lambda rows: any(row.type == "command_ack" and
                row.data.get("command_id") == greedy["command"]["id"] for row in rows))
            before = (rd / "events.jsonl").read_bytes()
            assert (await client.request("POST", "harness-selection/values", body))["replayed"]
            await client.request("POST", "harness-selection/values", {**body,
                "expected_evidence_revision": "f" * 64}, status=409)
            assert (rd / "events.jsonl").read_bytes() == before
            assert (await client.read("harness-selection"))["value_weight"] == 0
            rows = EventStore(rd / "events.jsonl").read_all()
            assert len([row for row in rows if row.type == "node_value_estimated"]) == 2
            assert [row.data["strategy"]["policy"] for row in rows if row.type == "strategy_decision"] == ["mcts", "greedy"]
            proof.update(value_batch_count=2, value_retry_changed_no_work=True,
                         value_conflicting_revision_refused=True, value_replay_after_greedy=True,
                         explicit_policy_switches=["mcts", "greedy"])
            value_body = body
        if damaged_events:
            path = rd / "events.jsonl"
            original = path.read_bytes()
            journals = {p.name: p.read_bytes() for p in rd.glob("harness_*.jsonl")}
            path.write_bytes(original + b'broken event recovery fixture\n')
            damaged = path.read_bytes()
            try:
                current = await client.progress()
                assert not current["complete"] and current["next_step"]["code"] == "inspect_sources"
                assert not current["source_health"]["events"]["read_complete"]
                for route in ("harness-hypotheses", "harness-selection"):
                    denied = await client.request("GET", route + "?expected_generation=" + generation, status=503)
                    assert denied["detail"]["code"] == "harness_history_incomplete"
                    assert denied["detail"]["source"] == "events.jsonl"
                actions = [("harness-" + kind, body) for kind, body, _ in saved_receipts]
                if value_recovery:
                    actions.append(("harness-selection/values", value_body))
                for route, body in actions:
                    for payload in (body, {**body, "action_id": body["action_id"] + ":new"}):
                        denied = await client.request("POST", route, payload, status=503)
                        assert denied["detail"]["code"] == "harness_history_incomplete"
                        assert denied["detail"]["source"] == "events.jsonl"
                assert path.read_bytes() == damaged
                assert journals == {p.name: p.read_bytes() for p in rd.glob("harness_*.jsonl")}
            finally:
                # Only the operator fixture restores this private known-good backup.
                path.write_bytes(original)
            assert (await client.progress())["complete"]
            if value_recovery:
                assert (await client.request("POST", "harness-selection/values", value_body))["replayed"]
            assert path.read_bytes() == original
            proof.update(damaged_event_read_routes=["harness-hypotheses", "harness-selection"],
                         damaged_event_write_requests=len(actions) * 2,
                         damaged_event_refusals_changed_no_work=True, event_backup_restored_by_fixture=True)
        if obligations:
            before = (rd / "events.jsonl").read_bytes()
            journals = {kind: (rd / f"harness_{kind}.jsonl").read_bytes() for kind in ("reviews", "decisions")}
            if damaged_journals:
                proof["damaged_journal_cases"] = []
                for kind, original in journals.items():
                    path = rd / f"harness_{kind}.jsonl"
                    body = next(body for saved_kind, body, _ in saved_receipts if saved_kind == kind)
                    path.write_bytes(original + b'broken recovery fixture row\n')
                    damaged = path.read_bytes()
                    try:
                        current = await client.progress()
                        assert not current["complete"] and current["next_step"]["code"] == "inspect_sources"
                        assert current["source_health"][kind]["invalid_lines"] == 1
                        for payload in (body, {**body, "action_id": body["action_id"] + ":fresh"}):
                            denied = await client.request("POST", "harness-" + kind, payload, status=503)
                            assert denied["detail"]["code"] == "harness_history_incomplete"
                            assert denied["detail"]["source"] == path.name
                        if kind == "reviews":
                            denied = await client.request("POST", "commands", {"expected_generation": generation,
                                "type": "run_abort", "data": {"reason": "Incomplete reviews cannot prove finish."}},
                                "idle:damaged-journal-finish")
                            assert denied["status"] == "rejected" and denied["error"]["code"] == "harness_history_incomplete"
                        assert (rd / "events.jsonl").read_bytes() == before and path.read_bytes() == damaged
                    finally:
                        # Fixture operator restores only its own known-good backup.
                        # The harness has neither repaired nor removed a source row.
                        path.write_bytes(original)
                    assert (await client.progress())["complete"]
                    proof["damaged_journal_cases"].append(kind)
            for kind, body, saved in saved_receipts:
                reply = await client.request("POST", "harness-" + kind, body)
                assert reply["replayed"] and reply["review" if kind == "reviews" else "decision"] == saved
            assert (rd / "events.jsonl").read_bytes() == before
            assert all((rd / f"harness_{kind}.jsonl").read_bytes() == raw for kind, raw in journals.items())
            current = await client.progress()
            assert current["finish_reviews_due"] == ["lessons", "skill_candidates"] and current["finish_report_due"]
            full = await client.read("harness-progress")
            for kind in ("reviews", "decisions"):
                assert all(row["validity"] == "superseded" for row in full["history"][kind]["items"])
            denied = await client.request("POST", "commands", {"expected_generation": generation,
                "type": "run_abort", "data": {"reason": "Commentary cannot discharge reports/reviews."}},
                "idle:premature-finish")
            assert denied["status"] == "rejected" and denied["error"]["code"] == "external_report_required"
            assert (rd / "events.jsonl").read_bytes() == before
            await client.call("phases", {"query": "report"})
            await client.call("phase_info", {"phase_id": "report"})
            report_receipt = await client.command("report_generated", {"content": {"headline": "Recovery outcomes",
                "verdict": "Protocol acceptance, not independent ML robustness.",
                "summary": json.dumps({"terminal_metrics": proof["metrics"], "failed_parent_retained": failed_first})}},
                "idle:current-report")
            # The durable intake receipt can precede the engine's command_ack.
            # Attribute bytes only after that specific existing acknowledgement.
            await until(settled_trust, lambda rows: any(row.type == "command_ack" and
                row.data.get("command_id") == report_receipt["command"]["id"] for row in rows))
            after_report = (rd / "events.jsonl").read_bytes()
            denied = await client.request("POST", "commands", {"expected_generation": generation,
                "type": "run_abort", "data": {"reason": "Old reviews cannot discharge the new evidence window."}},
                "idle:premature-review-finish")
            assert denied["status"] == "rejected" and denied["error"]["code"] == "external_reviews_required"
            assert (rd / "events.jsonl").read_bytes() == after_report
            proof.update(old_receipts_replayed=len(saved_receipts), receipt_replay_changed_no_work=True,
                         old_receipts_stayed_superseded=True, commentary_did_not_discharge_finish=True,
                         fresh_report_still_required_fresh_reviews=True)
            proof["finish_obligations"] = await settle_obligations(client, expanding=False,
                completed_actions={"lessons": "idle:lesson", "skill_candidates": "idle:skill"} if knowledge_recovery else None)
        await client.call("phases", {"query": "recovery"})
        await client.call("phase_info", {"phase_id": "recovery"})
        await client.command("run_abort", {"reason": "Completed disposable idle recovery acceptance."}, "idle:finish")
        await client.commentary("run", "idle:summary:run",
            "Run явно завершён: исходный узел failed без метрики, исправленный дочерний узел измерен. История неудачи и логи сохранены. Повтор старой команды ничего не переобучал; исправление подано новым ключом. Это один успешный seed, повторяемость ещё не проверена."
            if failed_first else "Проверка явно завершена. Разрыв MCP сохранил измеренный результат и квитанцию. Чтения ничего не запускали; следующий кандидат оценён после явного продолжения. Это короткая проверка протокола, не оценка живости удалённого агента.")
        if result_backlog:
            cursor, seen = None, []
            while True:
                page = await client.notices(limit=1, cursor=cursor)
                row, = page["items"]
                assert row["commentary"]
                if failed_first and row["kind"] == "run":
                    assert row["failed"] == row["evaluated"] == 1 and row["selected_node"] == 1
                seen.append(row["id"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            assert seen == ["run", "node:1:0", "node:0:0"]
            proof["final_result_page_order"] = seen

    async def session(label, generation, drive, kill=False):
        pid_file = root / f"{label}.pid"
        wrapper = "import os;from pathlib import Path;from looplab.harness.mcp_server import run_stdio;" + \
            f"Path({str(pid_file)!r}).write_text(str(os.getpid()));run_stdio()"
        if mcp_python:
            # MCP may install uvicorn transitively; stdio must not import/run it.
            wrapper = ("import builtins\noriginal = builtins.__import__\n"
                       "def without_ui(name, *args, **kwargs):\n"
                       "    if name.split('.')[0] in {'fastapi','uvicorn'}: raise ModuleNotFoundError(name)\n"
                       "    return original(name, *args, **kwargs)\n"
                       "builtins.__import__ = without_ui\n") + wrapper
        params = StdioServerParameters(command=mcp_python or sys.executable, args=["-c", wrapper],
            env={**child_env, "LOOPLAB_HARNESS_URL": proxy.url if proxy else knowledge_proxy.url if knowledge_proxy else url, "LOOPLAB_HARNESS_TOKEN": token})
        async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as mcp:
            await mcp.initialize()
            client = Client(mcp, generation)
            checked = await client.call("connection_check", {"run_id": "demo", "expected_generation":
                generation.upper() if response_fault == "invalid_json" else generation})
            assert checked["ok"] and checked["agent_connection"] == "not_measured"
            assert checked["generation"] == generation
            if response_fault == "invalid_json":
                proof["uppercase_handoff_canonicalized"] = True
            await client.call("capabilities", {})
            if mcp_python:
                # Every local metadata tool is available on a harness-only client.
                listed = await mcp.call_tool("phases", {})
                assert not listed.is_error
                catalog = [json.loads(item.text) for item in listed.content]
                # List-returning tools emit one content block per item in this SDK.
                assert len(catalog) > 5 and all(isinstance(row, dict) for row in catalog)
                for phase in catalog:
                    info = await client.call("phase_info", {"phase_id": phase["id"]})
                    assert "commands" in info and "write_access" in info
                assert "external_harness" in (await client.call("settings_keys", {"query": "external_harness"}))["matches"]
                assert (await client.call("setting_info", {"name": "external_harness"}))["schema"]
                assert (await client.call("operations", {"query": "commands"}))["total"]
                assert "post" in (await client.call("operation_schema", {"path": "/api/runs/{run_id}/commands"}))["operations"]
                proof["remote_metadata_before_and_after_reconnect"] = True
            for suffix in ("config", "harness-contract",
                           "artifact?root=run&path=task.snapshot.json&expected_generation=" + generation):
                await client.request("GET", suffix)
            await drive(client)
            if kill:
                os.kill(int(pid_file.read_text()), signal.SIGTERM)

    try:
        if drop_command_response:
            proxy = ResponseLossProxy(url, response_fault,
                "stale_generation" if read_fault == "stale_result_generation" else read_fault)
        if knowledge_recovery:
            knowledge_proxy = ResponseLossProxy(url)
            knowledge_proxy.drop_next_write = None
        server, thread = start_ui()
        flags = {"external_harness": True, "deep_research_every": -1, "report_every": 0, "novelty_mode": "off",
            "foresight": False, "track_hypotheses": False, "concept_pivot": False, "concept_run_base": False,
            "cross_run_concepts": False, "cross_run_curation": False, "reflection_priors": False,
            "lessons_every": 0, "comparative_lessons": False, "concurrent_research": False,
            "train_monitor": False, "asha_live": False, "stage_check_tools": False,
            "eval_deadline_grace_s": 0, "memory_dir": str(root / "memory")}
        if obligations:
            flags.update(deep_research_every=1, report_every=1, novelty_mode="llm", concept_run_base=True,
                         reflection_priors=True, lessons_every=1, comparative_lessons=True)
        if value_recovery:
            flags.update(mcts_value_weight=.4)
        command = [sys.executable, "-m", "looplab.cli", "run", str(task_path), "--out", str(runs / "demo"),
                   "--backend", "toy", "--max-nodes", "3"]
        for key, value in flags.items():
            command += ["-s", f"{key}={str(value).lower() if isinstance(value, bool) else value}"]
        engine = subprocess.Popen(command, cwd=root, env=child_env, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        owned_engines.append(engine)
        with HarnessAPI(url, token).client as http:
            state = wait_for(lambda: http.get("api/runs/demo/state?observe_only=true"),
                lambda row: row.status_code == 200 and row.json()["state"].get("setup_done")).json()
        generation = state["generation"]; proof["generation"] = generation
        anyio.run(session, "first", generation, first, True)
        assert engine.poll() is None
        # The observer is an OPERATOR request, so it cannot make the dead MCP
        # appear active. A real hold tests the production monotonic expiry clock.
        with HarnessAPI(url, owner).client as http:
            params = {"expected_generation": generation, "brief": "true"}
            def observed_activity():
                response = http.get("api/runs/demo/harness-progress", params=params)
                assert response.status_code == 200, response.text
                return response.json()
            contact = observed_activity()
            assert contact["agent_activity"]["status"] == "recent_request"
            before_hold = (runs / "demo" / "events.jsonl").read_bytes()
            held = time.monotonic()
            while time.monotonic() - held < quiet_hold_seconds:
                time.sleep(max(0, min(.5, quiet_hold_seconds - (time.monotonic() - held))))
                assert engine.poll() is None
            quiet = observed_activity()
            expected = "quiet" if quiet_hold_seconds >= contact["agent_activity"]["quiet_after_s"] else "recent_request"
            assert quiet["agent_activity"]["status"] == expected
            assert quiet["agent_activity"]["last_seen_at"] == contact["agent_activity"]["last_seen_at"]
            assert quiet["execution"]["engine_running"] is True and quiet["finish_pending_node_count"] == 0
            assert (runs / "demo" / "events.jsonl").read_bytes() == before_hold
            proof.update(quiet_hold_seconds=quiet_hold_seconds, observed_activity=quiet["agent_activity"],
                         quiet_activity_changed_no_work=True)
        if name == "engine_loss":
            engine.terminate(); engine.wait(timeout=10)
        server.should_exit = True; thread.join(timeout=5)
        assert not thread.is_alive()
        server, thread = start_ui()
        with HarnessAPI(url, owner).client as http:
            clean = http.get("api/runs/demo/harness-progress", params={
                "expected_generation": generation, "brief": "true"}).json()
            assert clean["agent_activity"]["status"] == "not_observed"
            proof["ui_restart_forgot_contact"] = True
        anyio.run(session, "reconnected", generation, recovered)
        owned_engines[-1].wait(timeout=10)
        rd = runs / "demo"
        events = EventStore(rd / "events.jsonl").read_all()
        terminals = [r for r in events if r.type in ("node_evaluated", "node_failed")]
        assert len(terminals) == 2 and sorted(r.data["node_id"] for r in terminals) == [0, 1]
        assert [r.type for r in terminals] == (["node_failed", "node_evaluated"] if failed_first else ["node_evaluated"] * 2)
        if failed_first:
            assert not (rd / "nodes" / "node_0" / "weights.json").exists()
            child, = [r for r in events if r.type == "node_created" and r.data["node_id"] == 1]
            assert child.data["parent_ids"] == [0] and child.data["parent_generations"] == {"0": 0}
            proof.update(failed_first=True, measured_score_count=1, failed_score_count=1,
                         failed_has_no_weights=True)
        starts = [r for r in events if r.type == "phase_progress" and r.data.get("phase") == "stage"
                  and r.data.get("status") == "started"]
        assert len(starts) == 2 and all(r.data["name"] == "train_eval" for r in starts)
        for directory in (source, rd / "nodes" / "node_0", rd / "nodes" / "node_1"):
            assert hashlib.sha256((directory / "score.py").read_bytes()).hexdigest() == digest
        for operation in ("inspect", "replay"):
            result = subprocess.run([sys.executable, "-m", "looplab.cli", operation, str(rd)], cwd=root,
                env=child_env, capture_output=True, text=True, encoding="utf8", timeout=20)
            assert result.returncode == 0, (operation, result.stderr)
            (root / f"{operation}.txt").write_text(result.stdout, encoding="utf8")
        proof.update(protected_scorer_unchanged=True, score_executions=len(starts),
                     engine_processes=len(owned_engines), inspect_replay="passed")
        if seed_base_recovery:
            from looplab.core.atomicio import rmtree_readonly_aware
            from looplab.engine.bundle import verify_bundle
            from looplab.engine.seed_archive import verified_seed_archive
            import shutil
            # Deliberate export validation after both engine evaluations ended.
            # Source/workdirs are disposable fixture paths; retain the event record.
            before = (rd / "events.jsonl").read_bytes()
            rmtree_readonly_aware(source)
            rmtree_readonly_aware(rd / "nodes")
            bundle = root / "bundle"
            exported = subprocess.run([sys.executable, "-m", "looplab.cli", "export-bundle",
                str(rd), "--out", str(bundle)], cwd=root, env=child_env,
                capture_output=True, text=True, encoding="utf8", timeout=30)
            assert exported.returncode == 0, exported.stderr
            assert "2 archive(s)" in exported.stdout and verify_bundle(bundle) == []
            for revision in proof["base_revisions"]:
                assert verified_seed_archive(bundle, revision) is not None
            validation = root / "export-validation"
            shutil.copytree(verified_seed_archive(bundle, proof["base_revisions"][1]), validation)
            created = next(row for row in events if row.type == "node_created" and row.data["node_id"] == 1)
            (validation / "config.json").write_text(created.data["files"]["config.json"], encoding="utf8")
            assert hashlib.sha256((validation / "score.py").read_bytes()).hexdigest() == digest
            scored = subprocess.run([sys.executable, "score.py"], cwd=validation, env=child_env,
                capture_output=True, text=True, encoding="utf8", timeout=20)
            assert scored.returncode == 0, scored.stderr
            measured = json.loads(scored.stdout.splitlines()[-1])["metric"]
            assert abs(measured - proof["metrics"][1]) < 1e-12
            assert before == (rd / "events.jsonl").read_bytes() == (bundle / "events.jsonl").read_bytes()
            proof.update(bundle_verified_after_source_workdir_loss=True,
                         export_validation_executions=1, export_validation_metric=measured,
                         export_validation_changed_no_events=True)
        if result_backlog:
            assert len((rd / "result_commentary.jsonl").read_text(encoding="utf8").splitlines()) == 3
            proof["commentary_count"] = 3
        if proxy is not None:
            catalog_reads = ([{"method": "GET", "route": "openapi.json", "upstream_status": 200}] * 2
                             if discovery_fault != "none" else [])
            assert proxy.dropped == catalog_reads + [{"method": "POST", "route": "commands", "upstream_status": 200},
                                     {"method": "POST", "route": "result-notices", "upstream_status": 200},
                                     {"method": "GET", "route": {"wrong_receipt": "command-receipt", "incomplete_receipt": "command-receipt", "stale_result_generation": "result-notices", "incomplete_result_page": "result-notices"}.get(read_fault, "harness-progress"), "upstream_status": 200}] + (
                                     [{"method": "GET", "route": "harness-progress", "upstream_status": 200}] if read_fault == "incomplete_progress" else [])
            assert proof["exact_retry_receipt"] == proof["original_receipt"]
            assert len((rd / "result_commentary.jsonl").read_text(encoding="utf8").splitlines()) == 3
            proof["commentary_count"] = 3
            proof["dropped_responses"] = proxy.dropped
        (root / "acceptance.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf8")
        print(json.dumps(proof), flush=True)
        return proof
    finally:
        if proxy is not None:
            proxy.close()
        if knowledge_proxy is not None:
            knowledge_proxy.close()
        for child in owned_engines:
            if child.poll() is None:
                child.terminate(); child.wait(timeout=10)
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5)
        log.close()
        os.environ.clear(); os.environ.update(env_before)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--case", choices=["all", *CASES], default="all")
    parser.add_argument("--quiet-hold-seconds", type=float, default=0,
                        help="Real idle hold after owned MCP death; >=120 tests quiet activity without agent calls.")
    parser.add_argument("--drop-command-response", action="store_true",
                        help="Lose accepted command/commentary replies and one read reply through an owned TCP proxy.")
    parser.add_argument("--response-fault", choices=["disconnect", "invalid_json", "oversized", "server_error"],
                        default="disconnect", help="Replace accepted write replies instead of disconnecting; requires --drop-command-response.")
    parser.add_argument("--read-fault", choices=["disconnect", "stale_generation", "wrong_receipt", "stale_result_generation", "incomplete_result_page", "incomplete_receipt", "incomplete_progress"],
                        default="disconnect", help="Lose one successful read or replace its identity/page structure; requires --drop-command-response.")
    parser.add_argument("--discovery-fault", choices=["none", "disconnect", "invalid_json", "invalid_catalog"],
                        default="none", help="Fault both live discovery tools before the first candidate; requires --drop-command-response.")
    parser.add_argument("--mcp-python", type=Path,
                        help="Use a separate harness-only interpreter with FastAPI absent and UI imports blocked; server/engine stay on this interpreter.")
    parser.add_argument("--result-backlog", action="store_true",
                        help="Defer node commentary across reconnect, then drain result pages and publish/retry each summary.")
    parser.add_argument("--failed-first", action="store_true",
                        help="Fail the first scorer on invalid learning rate; inspect fenced logs and submit a corrected child. Requires --result-backlog.")
    parser.add_argument("--obligations", action="store_true",
                        help="Enable research/concepts/report/novelty and lesson/skill reviews; restore old journal receipts after new results. Requires --result-backlog.")
    parser.add_argument("--damaged-journals", action="store_true",
                        help="Damage private decision/review sources; prove refusal without writes, then restore fixture backups. Requires --obligations.")
    parser.add_argument("--value-recovery", action="store_true",
                        help="Switch greedy/MCTS and recover a complete value batch on two measured nodes. Requires --result-backlog, excludes --failed-first.")
    parser.add_argument("--damaged-events", action="store_true",
                        help="Damage the private event log; refuse domain reads/receipt writes, then restore fixture backup. Requires --obligations.")
    parser.add_argument("--knowledge-recovery", action="store_true",
                        help="Publish a protocol lesson/skill with lost ACKs and damaged source recovery. Requires backlog/obligations, excludes failed-first/response-loss.")
    parser.add_argument("--seed-base-recovery", action="store_true",
                        help="Record a tracked seed; change the owned source before terminal and verify a later seed after recovery.")
    args = parser.parse_args()
    if not 0 <= args.quiet_hold_seconds <= 14400:
        parser.error("quiet hold must be between zero and four hours")
    if args.drop_command_response and args.case != "agent_loss":
        parser.error("response-loss acceptance requires --case agent_loss")
    if args.response_fault != "disconnect" and not args.drop_command_response:
        parser.error("response fault requires --drop-command-response")
    if args.read_fault != "disconnect" and not args.drop_command_response:
        parser.error("read fault requires --drop-command-response")
    if args.discovery_fault != "none" and not args.drop_command_response:
        parser.error("discovery fault requires --drop-command-response")
    if args.result_backlog and args.drop_command_response:
        parser.error("result backlog uses deferred summaries; run response-loss probes separately")
    if args.failed_first and not args.result_backlog:
        parser.error("failed-first requires --result-backlog")
    if args.obligations and not args.result_backlog:
        parser.error("obligations requires --result-backlog")
    if args.damaged_journals and not args.obligations:
        parser.error("damaged-journals requires --obligations")
    if args.value_recovery and (not args.result_backlog or args.failed_first):
        parser.error("value-recovery requires --result-backlog and two successful nodes (no --failed-first)")
    if args.damaged_events and not args.obligations:
        parser.error("damaged-events requires --obligations")
    if args.knowledge_recovery and (not args.result_backlog or not args.obligations or args.failed_first or args.drop_command_response):
        parser.error("knowledge-recovery requires --result-backlog --obligations and two successful nodes, without --drop-command-response")
    if args.seed_base_recovery and (args.failed_first or args.drop_command_response):
        parser.error("seed-base-recovery requires two successful nodes, without failed-first/response-loss")
    root = args.out.resolve(); root.mkdir(parents=True, exist_ok=False)
    mcp_python = str(args.mcp_python.resolve()) if args.mcp_python else None
    if mcp_python and not Path(mcp_python).is_file():
        parser.error("MCP interpreter does not exist")
    proof = [run_case(root / name, name, args.quiet_hold_seconds, args.drop_command_response, args.response_fault, args.read_fault, args.discovery_fault, mcp_python, args.result_backlog, args.failed_first, args.obligations, args.damaged_journals, args.value_recovery, args.damaged_events, args.knowledge_recovery, args.seed_base_recovery)
             for name in CASES if args.case in ("all", name)]
    (root / "acceptance.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
