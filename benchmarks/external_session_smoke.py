"""Offline MCP recovery acceptance against a private UI and protected real training.

Run from a source checkout with [ui,harness] installed:
python -m benchmarks.external_session_smoke --out .tmp/new-session-proof
The output directory must be new. No provider/model calls or user configuration edits.
Faults affect only this disposable server/journal: two committed responses are replaced
with HTTP 503, one owned MCP process is killed, the UI server is restarted, and a
checkpoint tail is damaged then restored from this fixture's known valid bytes.
It also pauses the engine, reconnects without resuming it, explicitly resumes via
the production command spawner, and re-evaluates a node behind a fresh checkpoint.
Use --checkpoint-hold-seconds to observe a paused open checkpoint over a real
wall-clock interval; waiting never answers the checkpoint or resumes the run.
Use --engine-loss to terminate this fixture's engine at that checkpoint and
check explicit recovery with a fresh evaluator question.
Use --obligations to exercise enabled research, concept base, report and
lesson/skill reviews across MCP sessions and node reset.
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
from benchmarks._external_obligation_cycle import settle_obligations
from looplab.events.eventstore import EventStore
from looplab.harness.mcp_server import HarnessAPI
from looplab.serve.protocol import COMMAND_TERMINAL_STATUSES
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
    def __init__(self, session, generation, obligations=False):
        self.session, self.generation = session, generation
        self.obligations = obligations

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

    async def command(self, kind, data, key):
        await self.request("POST", "commands", {"expected_generation": self.generation,
            "type": kind, "data": data}, key)
        receipt = await until(lambda: self.receipt(key), lambda row:
            row["command"]["status"] in COMMAND_TERMINAL_STATUSES)
        assert receipt["command"]["status"] == "succeeded", receipt
        return receipt

    async def inject(self, steps, key, status=200):
        if self.obligations:
            await settle_obligations(self, expanding=True)
        assert (await self.progress())["complete"]
        await self.call("phases", {"query": "implementation"})
        await self.call("phase_info", {"phase_id": "implementation"})
        body = {"expected_generation": self.generation, "type": "inject_node", "data": {
            "idea": {"operator": "draft", "footprint": {"gpus": 0},
                     "rationale": f"Measure {steps} training steps with the unchanged scorer."},
            "files": {"config.json": json.dumps({"steps": steps})}}}
        if self.obligations:
            body["data"]["idea"]["concepts"] = ["model/linear"]
        return await self.request("POST", "commands", body, key, status)

    async def question(self):
        payload = await until(lambda: self.read("harness-checkpoints"), lambda value: bool(value["pending"]))
        question, = payload["pending"]
        step = (await self.progress())["next_step"]
        assert step["code"] == "answer_checkpoint"
        await self.call("phases", {"query": step["phase_id"]})
        phase = await self.call("phase_info", {"phase_id": step["phase_id"]})
        assert phase["write_access"][step["action"]] == "external_agent"
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
    parser.add_argument("--checkpoint-hold-seconds", type=float, default=0,
                        help="Observe the paused open checkpoint for 0..86400 real seconds")
    parser.add_argument("--engine-loss", action="store_true",
                        help="Terminate the owned engine at its open checkpoint before reconnect")
    parser.add_argument("--obligations", action="store_true",
                        help="Enable research/concept/report and lesson/skill reviews in the recovery scenario")
    args = parser.parse_args()
    if not 0 <= args.checkpoint_hold_seconds <= 86400:
        parser.error("--checkpoint-hold-seconds must be finite and between 0 and 86400")
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
        LOOPLAB_MEMORY_DIR=str(root / "memory"), LOOPLAB_KNOWLEDGE_DIR=str(root / "knowledge"),
        CUDA_VISIBLE_DEVICES="")
    child_env = {key: value for key, value in os.environ.items() if not key.startswith("LOOPLAB_")}
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["CUDA_VISIBLE_DEVICES"] = ""  # This pure CPU scorer needs no host GPU lease.
    bind = socket.socket(); bind.bind(("127.0.0.1", 0)); port = bind.getsockname()[1]; bind.close()
    url = f"http://127.0.0.1:{port}"
    server = thread = engine = None
    owned_engines = []
    faults = {"command": False, "answer": False}
    proof = {"agent_connection": "not_measured", "model_judgment_tested": False,
             "engine_loss": args.engine_loss, "obligations": args.obligations, "observations": []}
    log = (root / "engine.log").open("w", encoding="utf8")

    def start_ui():
        app = make_app(runs, bind_host="127.0.0.1")
        original_spawn = app.state.looplab.commands.spawn_engine

        def tracked_spawn(*args, **kwargs):
            # Keep production spawning; retain only this private server's children
            # for cleanup, never discover or terminate unrelated host processes.
            from looplab.serve.engine_proc import _spawned_engines

            pid = original_spawn(*args, **kwargs)
            if pid is not None:
                owned_engines.append(_spawned_engines[pid])
            return pid

        app.state.looplab.commands.spawn_engine = tracked_spawn

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
            client = Client(mcp, generation, args.obligations)
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
        await client.call("phases", {"query": "recovery"})
        await client.call("phase_info", {"phase_id": "recovery"})
        await client.progress()
        pause = await client.command("pause", {}, "session:pause:open")
        # Fixture observation only: receipt settlement can precede the engine ACK.
        # Fence that legitimate background append before checking read-only bytes.
        async def pause_ack():
            return any(row.type == "command_ack" and row.data.get("command_id") == pause["command"]["id"]
                       for row in EventStore(runs / "demo" / "events.jsonl").read_all())
        await until(pause_ack, bool)
        paused = await client.progress()
        assert paused["recorded_lifecycle"]["paused"] and paused["execution"]["engine_running"] is True
        assert paused["next_step"]["phase_id"] == "evaluation"
        assert "does not resume" in paused["next_step"]["detail"]
        proof["observations"].append("pause preserved the open checkpoint; engine stayed live awaiting its verdict")
        if args.engine_loss:
            engine.terminate()
            await anyio.to_thread.run_sync(lambda: engine.wait(timeout=10))
            assert (await client.progress())["execution"]["engine_running"] is False
            proof["observations"].append("owned engine terminated at the open checkpoint; node remained nonterminal")
        proof["observations"].append("command ack lost; natural checkpoint opened; owned MCP process killed")

    async def recovered(client):
        await client.call("phases", {"query": "recovery"})
        await client.call("phase_info", {"phase_id": "recovery"})
        rd = runs / "demo"
        before = {path: path.read_bytes() for path in (rd / "events.jsonl", rd / "harness_checkpoints.jsonl")}
        receipt = await client.receipt("session:candidate:0")
        assert receipt["command"]["id"] == proof["command_id"]
        progress = await client.progress()
        assert progress["execution"]["engine_running"] is (not args.engine_loss)
        assert progress["pending_checkpoint_count"] == 1
        if args.engine_loss:
            assert "refresh after resume" in progress["next_step"]["detail"]
        q = await client.question()
        assert q["checkpoint_id"] == proof["checkpoint"]
        assert all(path.read_bytes() == content for path, content in before.items())
        start = time.monotonic()
        deadline = start + args.checkpoint_hold_seconds
        samples = 0
        while time.monotonic() < deadline:
            waiting = await client.progress()
            assert waiting["complete"] and waiting["recorded_lifecycle"]["paused"]
            assert waiting["execution"]["engine_running"] is (not args.engine_loss)
            assert waiting["pending_checkpoint_count"] == 1
            assert waiting["next_step"]["phase_id"] == "evaluation"
            assert (await client.read("harness-checkpoints"))["pending"][0]["checkpoint_id"] == q["checkpoint_id"]
            node = (await client.state())["state"]["nodes"]["0"]
            assert node["status"] == "pending" and node["metric"] is None
            assert all(path.read_bytes() == content for path, content in before.items())
            samples += 1
            await anyio.sleep(max(0, min(5, deadline - time.monotonic())))
        proof["checkpoint_hold"] = {"requested_seconds": args.checkpoint_hold_seconds,
            "observed_seconds": round(time.monotonic() - start, 3), "samples": samples}
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
        await client.call("phases", {"query": "recovery"})
        await client.call("phase_info", {"phase_id": "recovery"})
        await client.progress()
        await client.command("resume", {}, "session:resume:inflight")
        if args.engine_loss:
            assert len(owned_engines) == 2 and engine.poll() is not None
            new_questions = await until(lambda: client.read("harness-checkpoints"), lambda row:
                len(row["pending"]) == 1 and row["pending"][0]["checkpoint_id"] != q["checkpoint_id"])
            fresh, = new_questions["pending"]
            assert fresh["node_generation"] == q["node_generation"]
            assert fresh["claim_seq"] > q["claim_seq"]
            before_stale_answer = journal.read_bytes()
            await client.request("POST", "harness-checkpoints", answer, status=409)
            assert journal.read_bytes() == before_stale_answer
            assert (await client.question())["checkpoint_id"] == fresh["checkpoint_id"]
            history = (await client.read("harness-progress"))["history"]["checkpoints"]["items"]
            old = next(row for row in history if row["question"]["checkpoint_id"] == q["checkpoint_id"])
            assert old["lifecycle"] == old["status"] == "superseded" and old["answer"] is None
            proof["engine_recovery"] = {"previous_claim_seq": q["claim_seq"],
                "current_claim_seq": fresh["claim_seq"], "same_node_attempt": True}
            answer = {**answer, "checkpoint_id": fresh["checkpoint_id"], "action_id": "session:stage:0:recovered"}
            proof["observations"].append("explicit resume replaced the dead engine; fresh evaluator claim rejected the old answer")
        else:
            assert len(owned_engines) == 1 and engine.poll() is None
            assert (await client.question())["checkpoint_id"] == q["checkpoint_id"]
            proof["observations"].append("explicit resume reused the live engine and original checkpoint without another score stage")
        assert not (await client.state())["state"]["paused"]
        await client.request("POST", "harness-checkpoints", answer, status=503)
        first_node = await client.terminal(0)
        assert (await client.request("POST", "harness-checkpoints", answer))["replayed"]
        summary = ("После остановки engine незавершённую оценку выполнили заново. Кандидат сохранился; новый evaluator потребовал свежий checkpoint, старый ответ отклонён. Получен один terminal-результат; повторяемость ещё не проверена."
                   if args.engine_loss else "Обучение завершено после восстановления MCP. Исходную команду нашли по ключу; кандидат не дублировали. Один результат требует проверки повторяемости.")
        await client.commentary("node", "session:summary:0", summary, 0)
        proof["observations"].append("new UI/MCP reads left history unchanged; lost answer replayed after terminal")

        await client.inject(80, "session:candidate:1")
        q = await client.question()
        await client.request("POST", "harness-checkpoints", {**answer, "checkpoint_id": q["checkpoint_id"], "action_id": "session:stage:1"})
        second = await client.terminal(1)
        assert second["metric"] < first_node["metric"]
        await client.commentary("node", "session:summary:1", "Увеличили число шагов обучения; ошибка на той же проверке стала ниже. Это один детерминированный пример, не подтверждение устойчивости. Далее нужны независимые seeds.", 1)
        proof["metrics"] = [first_node["metric"], second["metric"]]
        proof["reset_answer"] = {**answer, "checkpoint_id": q["checkpoint_id"], "action_id": "session:stage:1"}
        if args.obligations:
            await settle_obligations(client, expanding=False)
            proof["before_reset_evidence_revision"] = (await client.progress())["evidence_revision"]

        await client.call("phases", {"query": "recovery"})
        await client.call("phase_info", {"phase_id": "recovery"})
        await client.progress()
        await client.command("pause", {}, "session:pause")
        await until(client.state, lambda row: row["state"]["paused"])
        await anyio.to_thread.run_sync(lambda: owned_engines[-1].wait(timeout=10))
        assert (await client.progress())["execution"]["engine_running"] is False
        proof["observations"].append("explicit pause stopped the active engine after measured work")

    async def resumed(client):
        rd = runs / "demo"
        before = {path: path.read_bytes() for path in (rd / "events.jsonl", rd / "harness_checkpoints.jsonl")}
        assert (await client.state())["state"]["paused"]
        assert (await client.progress())["execution"]["engine_running"] is False
        assert (await client.receipt("session:candidate:1"))["command"]["status"] == "succeeded"
        if args.obligations:
            restored = await client.progress()
            assert not restored["finish_reviews_due"] and not restored["finish_report_due"]
            assert restored["evidence_revision"] == proof["before_reset_evidence_revision"]
        await anyio.sleep(.5)
        assert all(path.read_bytes() == content for path, content in before.items())
        await client.call("phases", {"query": "recovery"})
        await client.call("phase_info", {"phase_id": "recovery"})
        await client.command("resume", {}, "session:resume")
        await until(client.state, lambda row: not row["state"]["paused"])
        await until(client.progress, lambda row: row["execution"]["engine_running"] is True)
        assert len(owned_engines) == 2 + int(args.engine_loss)
        proof["observations"].append("third MCP session did not resume on reads; durable resume spawned one replacement engine")

        await client.call("phases", {"query": "evaluation"})
        await client.call("phase_info", {"phase_id": "evaluation"})
        await client.progress()
        await client.command("node_reset", {"node_id": 1, "from_stage": "eval", "generation": 0}, "session:reset:1")
        q = await client.question()
        assert q["node_id"] == 1 and q["node_generation"] == 1
        if args.obligations:
            refreshed = await client.progress()
            assert refreshed["finish_report_due"] and set(refreshed["finish_reviews_due"]) == {"lessons", "skill_candidates"}
            assert refreshed["evidence_revision"] != proof.pop("before_reset_evidence_revision")
            history = (await client.read("harness-progress"))["history"]["reviews"]["items"]
            assert history and all(row["validity"] == "superseded" for row in history)
            proof["reset_obligations"] = {"report_due": True, "reviews_due": refreshed["finish_reviews_due"],
                "superseded_reviews": len(history), "same_node_count": refreshed["at_node"]}
        old_answer = proof.pop("reset_answer")
        assert q["checkpoint_id"] != old_answer["checkpoint_id"]
        assert (await client.request("POST", "harness-checkpoints", old_answer))["replayed"]
        assert (await client.state())["state"]["nodes"]["1"]["status"] == "pending"
        assert (await client.read("harness-checkpoints"))["pending"][0]["checkpoint_id"] == q["checkpoint_id"]
        await client.request("POST", "harness-checkpoints", {**old_answer, "checkpoint_id": q["checkpoint_id"],
            "action_id": "session:stage:1:retry", "reason": "Reviewed fresh output from reset attempt."})
        repeated = await client.terminal(1)
        assert repeated["attempt"] == 1 and repeated["metric"] == proof["metrics"][1]
        proof["remeasurement"] = {"node_id": 1, "attempt": repeated["attempt"], "metric": repeated["metric"]}
        await client.commentary("node", "session:summary:1:retry", "После явного resume повторили оценку узла. Старый ответ остался квитанцией; новый stage потребовал отдельной проверки. Результат совпал в этой детерминированной задаче, независимые seeds ещё нужны.", 1)
        proof["observations"].append("reset retained old answer ACK but required a new checkpoint; node attempt 1 remeasured the same score")

        await client.inject(0, "session:candidate:2")
        failed = await client.terminal(2, "failed")
        assert failed["metric"] is None
        await client.commentary("node", "session:summary:2", "Конфигурация с нулём шагов отклонена scorer. Метрика отсутствует; это не завершённое обучение. Следующий шаг — исправить параметры, сохранив проверку.", 2)
        proof["metrics"].append(failed["metric"])
        await client.call("phases", {"query": "recovery"})
        await client.call("phase_info", {"phase_id": "recovery"})
        assert (await client.progress())["complete"]
        if args.obligations:
            proof["finish_obligations"] = await settle_obligations(client, expanding=False)
        await client.command("run_abort", {"reason": "Completed disposable multi-session recovery acceptance."}, "session:finish")
        summary = ("Запуск явно завершён: два успешных узла, третий failed без метрики. После остановки engine незавершённая оценка повторена с новым checkpoint; готовый узел позже переизмерен через reset. Все продолжения были явными. Для исследовательского вывода нужны другие seeds."
                   if args.engine_loss else "Запуск явно завершён: два успешных узла, один повторно оценён после resume; третья конфигурация неуспешна. Разрыв MCP сохранил результаты, продолжение выбрано явно. Для исследовательского вывода нужны другие seeds.")
        await client.commentary("run", "session:summary:run", summary)
        assert len((await client.state())["state"]["nodes"]) == 3

    try:
        server, thread = start_ui()
        flags = {"external_harness": True, "deep_research_every": -1, "report_every": 0, "novelty_mode": "off",
            "foresight": False, "track_hypotheses": False, "concept_pivot": False, "concept_run_base": False,
            "cross_run_concepts": False, "cross_run_curation": False, "reflection_priors": False,
            "lessons_every": 0, "comparative_lessons": False, "concurrent_research": False,
            "train_monitor": False, "asha_live": False, "stage_check_tools": False, "memory_dir": str(root / "memory")}
        if args.obligations:
            flags.update(deep_research_every=1, report_every=1, concept_run_base=True, reflection_priors=True,
                         lessons_every=1, comparative_lessons=True)
        command = [sys.executable, "-m", "looplab.cli", "run", str(task_path), "--out", str(runs / "demo"),
                   "--backend", "toy", "--max-nodes", "4"]
        for key, value in flags.items():
            command += ["-s", f"{key}={str(value).lower() if isinstance(value, bool) else value}"]
        engine = spawn(command)
        owned_engines.append(engine)
        with HarnessAPI(url, token).client as http:
            current = wait_for(lambda: http.get("api/runs/demo/state?observe_only=true"),
                lambda row: row.status_code == 200 and row.json()["state"].get("setup_done")).json()
        generation = current["generation"]; proof["generation"] = generation
        anyio.run(session, "first", generation, first)
        assert (engine.poll() is not None) == args.engine_loss
        server.should_exit = True; thread.join(timeout=5)
        assert not thread.is_alive()
        server, thread = start_ui()
        anyio.run(session, "reconnected", generation, recovered)
        anyio.run(session, "resumed", generation, resumed)
        for proc in owned_engines:
            proc.wait(timeout=10)
        assert faults == {"command": True, "answer": True}
        events = EventStore(runs / "demo" / "events.jsonl").read_all()
        first_stages = [row for row in events if row.type == "phase_progress"
            and row.data.get("node_id") == 0 and row.data.get("generation") == 0
            and row.data.get("phase") == "stage" and row.data.get("status") == "finished"]
        assert len(first_stages) == 1 + int(args.engine_loss)
        assert all(row.data["name"] == "score" for row in first_stages)
        proof["initial_score_stage_completions"] = len(first_stages)
        if args.obligations:
            counts = {kind: sum(row.type == kind for row in events) for kind in
                      ("research_completed", "run_concepts", "report_generated")}
            assert counts == {"research_completed": 3, "run_concepts": 1, "report_generated": 4}
            reviews = [json.loads(line) for line in (runs / "demo" / "harness_reviews.jsonl").read_text(encoding="utf8").splitlines()]
            assert len(reviews) == 8 and all(row["decision"] == "no_applicable_action" for row in reviews)
            proof["obligation_receipts"] = {**counts, "reviews": len(reviews)}
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
        for proc in owned_engines:
            if proc.poll() is None:
                proc.terminate(); proc.wait(timeout=10)
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5)
        log.close()
        os.environ.clear(); os.environ.update(env_before)


if __name__ == "__main__":
    main()
