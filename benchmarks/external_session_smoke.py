"""Offline MCP recovery acceptance against a private UI and protected real training.

Run from a source checkout with [ui,harness] installed:
python -m benchmarks.external_session_smoke --out .tmp/new-session-proof
The output directory must be new. No provider/model calls or user configuration edits.
Faults affect only this disposable server/journal: two committed responses are replaced
with HTTP 503, one owned MCP process is killed, the UI server is restarted, and a
checkpoint tail is damaged then restored from this fixture's known valid bytes.
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
from starlette.responses import JSONResponse
import uvicorn

from benchmarks.claude_harness_smoke import SCORER, wait_for
from looplab.harness.mcp_server import HarnessAPI
from looplab.serve.server import make_app


async def until(function, predicate, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = await function()
        if predicate(value):
            return value
        await anyio.sleep(.05)
    raise TimeoutError("External session acceptance did not settle")


class Client:
    def __init__(self, session, generation):
        self.session, self.generation = session, generation

    async def call(self, name, args):
        result = await self.session.call_tool(name, args)
        assert not result.is_error, result
        value = json.loads(result.content[0].text)
        assert "error" not in value, (name, value)
        return value

    async def request(self, method, suffix, body=None, key=None, status=200):
        args = {"method": method, "path": "/api/runs/demo/" + suffix}
        if body is not None:
            args["body"] = body
        if key is not None:
            args["idempotency_key"] = key
        result = await self.call("api_request", args)
        assert result["status"] == status, (suffix, result)
        return result["body"]

    async def read(self, suffix):
        return await self.request("GET", suffix + "?expected_generation=" + self.generation)

    async def state(self):
        return await self.request("GET", "state?observe_only=true")

    async def progress(self):
        result = await self.call("run_progress", {"run_id": "demo", "expected_generation": self.generation})
        assert result["status"] == 200, result
        return result["body"]

    async def receipt(self, key):
        result = await self.call("command_receipt", {"run_id": "demo", "expected_generation": self.generation,
                                                   "idempotency_key": key})
        assert result["status"] == 200, result
        return result["body"]

    async def inject(self, steps, key, status=200):
        assert (await self.progress())["complete"]
        await self.call("phases", {"query": "implementation"})
        await self.call("phase_info", {"phase_id": "implementation"})
        body = {"expected_generation": self.generation, "type": "inject_node", "data": {
            "idea": {"operator": "draft", "footprint": {"gpus": 0},
                     "rationale": f"Measure {steps} training steps with the unchanged scorer."},
            "files": {"config.json": json.dumps({"steps": steps})}}}
        return await self.request("POST", "commands", body, key, status)

    async def question(self):
        await self.call("phases", {"query": "stage"})
        await self.call("phase_info", {"phase_id": "evaluation"})
        payload = await until(lambda: self.read("harness-checkpoints"), lambda value: bool(value["pending"]))
        question, = payload["pending"]
        assert question["phase_id"] == "stage_check"
        observed = json.loads(question["observation"].strip().splitlines()[-1])
        assert 0 < observed["metric"] < 1
        return question

    async def terminal(self, nid, status="evaluated"):
        value = await until(self.state, lambda row: row["state"]["nodes"].get(str(nid), {}).get("status") == status)
        return value["state"]["nodes"][str(nid)]

    async def commentary(self, kind, action, summary, nid=None):
        await self.progress()
        await self.call("phases", {"query": "result_summary"})
        await self.call("phase_info", {"phase_id": "result_summary"})
        rows = await until(lambda: self.read("result-notices"), lambda value: any(
            row["kind"] == kind and (nid is None or row.get("node_id") == nid) for row in value["items"]))
        row = next(row for row in rows["items"] if row["kind"] == kind and (nid is None or row.get("node_id") == nid))
        body = {"expected_generation": self.generation, "receipt_id": row["id"],
                "evidence_token": row["evidence_token"], "action_id": action, "summary": summary}
        await self.request("POST", "result-notices", body)
        assert (await self.request("POST", "result-notices", body))["replayed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="New disposable output directory")
    args = parser.parse_args()
    root = args.out.resolve()
    root.mkdir(parents=True, exist_ok=False)
    runs = root / "runs"; runs.mkdir()
    source = root / "source"; source.mkdir()
    (source / "score.py").write_text(SCORER, encoding="utf8")
    (source / "config.json").write_text('{"steps":5}', encoding="utf8")
    scorer_hash = hashlib.sha256((source / "score.py").read_bytes()).hexdigest()
    task = {"id": "external-session-training", "goal": "Measure held-out regression MSE after declared training.",
        "direction": "min", "repo": str(source), "edit_surface": ["config.json"], "protect": ["score.py"],
        "cmd": {"command": [sys.executable, "score.py"], "timeout": 20,
            "metric": {"reader": "stdout_json", "key": "metric"},
            "stages": [{"name": "score", "command": [sys.executable, "score.py"], "check": True,
                "expect": {"assert": "Declared training completes and held-out MSE is finite."}}]}}
    task_path = root / "task.json"; task_path.write_text(json.dumps(task), encoding="utf8")
    token, owner = secrets.token_hex(32), secrets.token_hex(32)
    env_before = dict(os.environ)
    os.environ.update(LOOPLAB_HARNESS_TOKEN=token, LOOPLAB_UI_TOKEN=owner,
        LOOPLAB_MEMORY_DIR=str(root / "memory"), LOOPLAB_KNOWLEDGE_DIR=str(root / "knowledge"))
    child_env = {key: value for key, value in os.environ.items() if not key.startswith("LOOPLAB_")}
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["CUDA_VISIBLE_DEVICES"] = ""  # This pure CPU scorer needs no host GPU lease.
    bind = socket.socket(); bind.bind(("127.0.0.1", 0)); port = bind.getsockname()[1]; bind.close()
    url = f"http://127.0.0.1:{port}"
    server = thread = engine = None
    faults = {"command": False, "answer": False}
    proof = {"agent_connection": "not_measured", "model_judgment_tested": False, "observations": []}
    log = (root / "engine.log").open("w", encoding="utf8")

    def start_ui():
        app = make_app(runs, bind_host="127.0.0.1")

        @app.middleware("http")
        async def discard_ack(request, call_next):
            response = await call_next(request)
            if response.status_code == 200 and request.method == "POST":
                fault = ("command" if request.url.path.endswith("/commands")
                    and request.headers.get("Idempotency-Key") == "session:candidate:0" else
                    "answer" if request.url.path.endswith("/harness-checkpoints") else None)
                if fault and not faults[fault]:
                    faults[fault] = True
                    return JSONResponse({"detail": "Disposable acceptance fault: committed acknowledgement withheld"}, status_code=503)
            return response

        value = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        worker = threading.Thread(target=value.run, daemon=True); worker.start()
        wait_for(lambda: value.started, bool)
        return value, worker

    def spawn(command):
        return subprocess.Popen(command, cwd=root, env=child_env, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    async def session(name, generation, drive):
        pid_file = root / f"{name}.pid"
        wrapper = "import os;from pathlib import Path;from looplab.harness.mcp_server import run_stdio;" + \
            f"Path({str(pid_file)!r}).write_text(str(os.getpid()));run_stdio()"
        params = StdioServerParameters(command=sys.executable, args=["-c", wrapper],
            env={**child_env, "LOOPLAB_HARNESS_URL": url, "LOOPLAB_HARNESS_TOKEN": token})
        async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as mcp:
            await mcp.initialize()
            client = Client(mcp, generation)
            assert (await client.call("capabilities", {}))["protocol_version"] == 4
            assert (await client.call("connection_check", {"run_id": "demo", "expected_generation": generation}))["ok"]
            for suffix in ("config", "harness-contract", "artifact?root=run&path=task.snapshot.json&expected_generation=" + generation):
                await client.request("GET", suffix)
            await drive(client)
            if name == "first":
                os.kill(int(pid_file.read_text()), signal.SIGTERM)

    async def first(client):
        await client.inject(5, "session:candidate:0", status=503)
        q = await client.question()
        proof["checkpoint"] = q["checkpoint_id"]
        receipt = await client.receipt("session:candidate:0")
        proof["command_id"] = receipt["command"]["id"]
        proof["observations"].append("command ack lost; natural checkpoint opened; owned MCP process killed")

    async def recovered(client):
        await client.call("phases", {"query": "recovery"})
        await client.call("phase_info", {"phase_id": "recovery"})
        rd = runs / "demo"
        before = {path: path.read_bytes() for path in (rd / "events.jsonl", rd / "harness_checkpoints.jsonl")}
        receipt = await client.receipt("session:candidate:0")
        assert receipt["command"]["id"] == proof["command_id"]
        progress = await client.progress()
        assert progress["execution"]["engine_running"] is True and progress["pending_checkpoint_count"] == 1
        q = await client.question()
        assert q["checkpoint_id"] == proof["checkpoint"]
        assert all(path.read_bytes() == content for path, content in before.items())
        # The second candidate is not submitted until measured evidence from the first is terminal.
        answer = {"expected_generation": client.generation, "checkpoint_id": q["checkpoint_id"],
                  "action_id": "session:stage:0", "verdict": "proceed", "reason": "Inspected actual finite training output."}
        # Disposable fault injection, never an automatic production repair recipe.
        journal = rd / "harness_checkpoints.jsonl"
        valid = journal.read_bytes()
        journal.write_bytes(valid + b'{broken\n')
        damaged = journal.read_bytes()
        await client.request("GET", "harness-checkpoints?expected_generation=" + client.generation, status=503)
        await client.request("POST", "harness-checkpoints", answer, status=503)
        partial = await client.progress()
        assert partial["complete"] is False
        assert (await client.state())["state"]["nodes"]["0"]["status"] == "pending"
        assert journal.read_bytes() == damaged
        assert (rd / "events.jsonl").read_bytes() == before[rd / "events.jsonl"]
        journal.write_bytes(valid)
        assert (await client.question())["checkpoint_id"] == q["checkpoint_id"]
        proof["observations"].append("damaged checkpoint tail refused reads/answers; engine stayed pending; fixture bytes explicitly restored")
        await client.request("POST", "harness-checkpoints", answer, status=503)
        first_node = await client.terminal(0)
        assert (await client.request("POST", "harness-checkpoints", answer))["replayed"]
        await client.commentary("node", "session:summary:0", "Обучение завершено после восстановления MCP. Исходную команду нашли по ключу; кандидат не дублировали. Один результат требует проверки повторяемости.", 0)
        proof["observations"].append("new UI/MCP reads left history unchanged; lost answer replayed after terminal")

        await client.inject(80, "session:candidate:1")
        q = await client.question()
        await client.request("POST", "harness-checkpoints", {**answer, "checkpoint_id": q["checkpoint_id"], "action_id": "session:stage:1"})
        second = await client.terminal(1)
        assert second["metric"] < first_node["metric"]
        await client.commentary("node", "session:summary:1", "Увеличили число шагов обучения; ошибка на той же проверке стала ниже. Это один детерминированный пример, не подтверждение устойчивости. Далее нужны независимые seeds.", 1)

        await client.inject(0, "session:candidate:2")
        failed = await client.terminal(2, "failed")
        assert failed["metric"] is None
        await client.commentary("node", "session:summary:2", "Конфигурация с нулём шагов отклонена scorer. Метрика отсутствует; это не завершённое обучение. Следующий шаг — исправить параметры, сохранив проверку.", 2)
        proof["metrics"] = [first_node["metric"], second["metric"], failed["metric"]]
        await client.call("phases", {"query": "recovery"})
        await client.call("phase_info", {"phase_id": "recovery"})
        assert (await client.progress())["complete"]
        await client.request("POST", "commands", {"expected_generation": client.generation, "type": "run_abort",
            "data": {"reason": "Completed disposable multi-session recovery acceptance."}}, "session:finish")
        await client.commentary("run", "session:summary:run", "Запуск явно завершён: два измеренных обучения и одна неуспешная конфигурация. Разрыв MCP не остановил engine; повторное подключение сохранило результаты. Для исследовательского вывода нужны другие seeds.")
        assert len((await client.state())["state"]["nodes"]) == 3

    try:
        server, thread = start_ui()
        flags = {"external_harness": True, "deep_research_every": -1, "report_every": 0, "novelty_mode": "off",
            "foresight": False, "track_hypotheses": False, "concept_pivot": False, "concept_run_base": False,
            "cross_run_concepts": False, "cross_run_curation": False, "reflection_priors": False,
            "lessons_every": 0, "comparative_lessons": False, "concurrent_research": False,
            "train_monitor": False, "asha_live": False, "stage_check_tools": False, "memory_dir": str(root / "memory")}
        command = [sys.executable, "-m", "looplab.cli", "run", str(task_path), "--out", str(runs / "demo"),
                   "--backend", "toy", "--max-nodes", "4"]
        for key, value in flags.items():
            command += ["-s", f"{key}={str(value).lower() if isinstance(value, bool) else value}"]
        engine = spawn(command)
        with HarnessAPI(url, token).client as http:
            current = wait_for(lambda: http.get("api/runs/demo/state?observe_only=true"),
                lambda row: row.status_code == 200 and row.json()["state"].get("setup_done")).json()
        generation = current["generation"]; proof["generation"] = generation
        anyio.run(session, "first", generation, first)
        assert engine.poll() is None
        server.should_exit = True; thread.join(timeout=5)
        assert not thread.is_alive()
        server, thread = start_ui()
        anyio.run(session, "reconnected", generation, recovered)
        engine.wait(timeout=10)
        assert faults == {"command": True, "answer": True}
        for path in [source / "score.py", *((runs / "demo" / "nodes" / f"node_{nid}" / "score.py") for nid in range(3))]:
            assert hashlib.sha256(path.read_bytes()).hexdigest() == scorer_hash
        for operation in ("inspect", "replay"):
            result = subprocess.run([sys.executable, "-m", "looplab.cli", operation, str(runs / "demo")],
                cwd=root, env=child_env, capture_output=True, text=True, encoding="utf8", timeout=20)
            assert result.returncode == 0, operation
            (root / f"{operation}.txt").write_text(result.stdout, encoding="utf8")
        proof.update(protected_scorer_unchanged=True, inspect_replay="passed", simulated_lost_acks=faults)
        (root / "acceptance.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf8")
        print(json.dumps({"metrics": proof["metrics"], "inspect_replay": "passed"}), flush=True)
    finally:
        if engine is not None and engine.poll() is None:
            engine.terminate(); engine.wait(timeout=10)
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5)
        log.close()
        os.environ.clear(); os.environ.update(env_before)


if __name__ == "__main__":
    main()
