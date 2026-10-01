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
from looplab.events.eventstore import EventStore
from looplab.harness.mcp_server import HarnessAPI
from looplab.serve.server import make_app


CASES = ("agent_loss", "engine_loss")


def run_case(root, name, quiet_hold_seconds=0, drop_command_response=False):
    root.mkdir(parents=True, exist_ok=False)
    runs = root / "runs"; runs.mkdir()
    source = root / "source"; source.mkdir()
    (source / "score.py").write_text(SCORER, encoding="utf8")
    (source / "config.json").write_text('{}', encoding="utf8")
    digest = hashlib.sha256((source / "score.py").read_bytes()).hexdigest()
    task = {"id": "external-idle-recovery", "goal": "Measure real SGD before and after explicit idle recovery.",
        "direction": "min", "repo": str(source), "edit_surface": ["config.json"], "protect": ["score.py"],
        "cmd": {"command": [sys.executable, "score.py"], "timeout": 20,
            "metric": {"reader": "stdout_json", "key": "metric"},
            "stages": [{"name": "train_eval", "role": "training",
                        "command": [sys.executable, "score.py"], "check": False}]}}
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
    owned_engines = []
    proof = {"case": name, "model_judgment_tested": False, "agent_connection": "not_measured",
             "metrics": [], "read_steps": []}
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

    async def candidate(client, nid, steps):
        await client.progress()
        await client.call("phases", {"query": "implementation"})
        await client.call("phase_info", {"phase_id": "implementation"})
        data = {"idea": {"operator": "draft", "footprint": {"gpus": 0},
            "rationale": "Measure the declared training steps with the protected SGD scorer."},
            "files": {"config.json": json.dumps({"steps": steps, "lr": .1, "delay": 0., "delay_from": 1})}}
        key = f"idle:candidate:{nid}"
        if proxy is not None and nid == 0:
            lost = await client.call("api_request", {"method": "POST", "path": "/api/runs/demo/commands",
                "body": {"expected_generation": client.generation, "type": "inject_node", "data": data},
                "idempotency_key": key})
            assert lost["status"] is None and lost["outcome"] == "unknown"
            assert lost["code"] == "request_outcome_unknown"
            receipt = await until(lambda: client.receipt(key), lambda row: row["command"]["status"] == "succeeded")
            replay = await client.command("inject_node", data, key)
            assert replay["command"]["id"] == receipt["command"]["id"]
            proof.update(lost_write=lost, exact_retry_receipt=receipt["command"]["id"])
        else:
            await client.command("inject_node", data, key)
        node = await client.terminal(nid)
        proof["metrics"].append(node["metric"])
        summary = "SGD и защищённый scoring завершены; результат измерен движком. Прочитайте исходную квитанцию при переподключении и явно выберите следующий эксперимент. Одного запуска недостаточно для вывода о повторяемости."
        if proxy is not None and nid == 0:
            await client.progress()
            await client.call("phases", {"query": "result_summary"})
            await client.call("phase_info", {"phase_id": "result_summary"})
            row = next(r for r in (await client.read("result-notices"))["items"]
                       if r["kind"] == "node" and r.get("node_id") == nid)
            body = {"expected_generation": client.generation, "receipt_id": row["id"],
                    "evidence_token": row["evidence_token"], "action_id": f"idle:summary:{nid}", "summary": summary}
            proxy.drop_next_write = "/api/runs/demo/result-notices"
            lost = await client.call("api_request", {"method": "POST", "path": "/api/runs/demo/result-notices", "body": body})
            assert lost["status"] is None and lost["outcome"] == "unknown"
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
        await candidate(client, 0, 16)
        proof["original_receipt"] = (await client.receipt("idle:candidate:0"))["command"]["id"]
        if proxy is not None:
            before = (runs / "demo" / "events.jsonl").read_bytes()
            proxy.drop_next_read = True
            lost = await client.call("run_progress", {"run_id": "demo", "expected_generation": generation})
            assert lost["status"] is None and lost["code"] == "api_unreachable"
            assert lost["outcome"] == "unavailable"
            assert (await client.progress())["complete"]
            assert (runs / "demo" / "events.jsonl").read_bytes() == before
            proof.update(lost_read=lost, failed_read_changed_no_work=True)

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
        if name == "engine_loss":
            await client.command("resume", {}, "idle:resume")
            await until(client.progress, lambda row: row["execution"]["engine_running"] is True)
            assert len(owned_engines) == 2
            # Exact retry resolves the same durable command, not another child.
            await client.command("resume", {}, "idle:resume")
            assert len(owned_engines) == 2
        assert (await client.progress())["next_step"]["code"] == "choose_direction"
        await candidate(client, 1, 32)
        await client.call("phases", {"query": "recovery"})
        await client.call("phase_info", {"phase_id": "recovery"})
        await client.command("run_abort", {"reason": "Completed disposable idle recovery acceptance."}, "idle:finish")
        await client.commentary("run", "idle:summary:run",
            "Проверка явно завершена. Разрыв MCP сохранил измеренный результат и квитанцию. Чтения ничего не запускали; следующий кандидат оценён после явного продолжения. Это короткая проверка протокола, не оценка живости удалённого агента.")

    async def session(label, generation, drive, kill=False):
        pid_file = root / f"{label}.pid"
        wrapper = "import os;from pathlib import Path;from looplab.harness.mcp_server import run_stdio;" + \
            f"Path({str(pid_file)!r}).write_text(str(os.getpid()));run_stdio()"
        params = StdioServerParameters(command=sys.executable, args=["-c", wrapper],
            env={**child_env, "LOOPLAB_HARNESS_URL": proxy.url if proxy else url, "LOOPLAB_HARNESS_TOKEN": token})
        async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as mcp:
            await mcp.initialize()
            client = Client(mcp, generation)
            checked = await client.call("connection_check", {"run_id": "demo", "expected_generation": generation})
            assert checked["ok"] and checked["agent_connection"] == "not_measured"
            await client.call("capabilities", {})
            for suffix in ("config", "harness-contract",
                           "artifact?root=run&path=task.snapshot.json&expected_generation=" + generation):
                await client.request("GET", suffix)
            await drive(client)
            if kill:
                os.kill(int(pid_file.read_text()), signal.SIGTERM)

    try:
        if drop_command_response:
            proxy = ResponseLossProxy(url)
        server, thread = start_ui()
        flags = {"external_harness": True, "deep_research_every": -1, "report_every": 0, "novelty_mode": "off",
            "foresight": False, "track_hypotheses": False, "concept_pivot": False, "concept_run_base": False,
            "cross_run_concepts": False, "cross_run_curation": False, "reflection_priors": False,
            "lessons_every": 0, "comparative_lessons": False, "concurrent_research": False,
            "train_monitor": False, "asha_live": False, "stage_check_tools": False,
            "eval_deadline_grace_s": 0, "memory_dir": str(root / "memory")}
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
        assert all(r.type == "node_evaluated" for r in terminals)
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
        if proxy is not None:
            assert proxy.dropped == [{"method": "POST", "route": "commands", "upstream_status": 200},
                                     {"method": "POST", "route": "result-notices", "upstream_status": 200},
                                     {"method": "GET", "route": "harness-progress", "upstream_status": 200}]
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
    args = parser.parse_args()
    if not 0 <= args.quiet_hold_seconds <= 14400:
        parser.error("quiet hold must be between zero and four hours")
    if args.drop_command_response and args.case != "agent_loss":
        parser.error("response-loss acceptance requires --case agent_loss")
    root = args.out.resolve(); root.mkdir(parents=True, exist_ok=False)
    proof = [run_case(root / name, name, args.quiet_hold_seconds, args.drop_command_response)
             for name in CASES if args.case in ("all", name)]
    (root / "acceptance.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
