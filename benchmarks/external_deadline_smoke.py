"""Real MCP deadline recovery against isolated CPU training and a private UI.

python -m benchmarks.external_deadline_smoke --out .tmp/new-deadline-proof
Runs extend-to-completion, stop, capped extension and disabled grace. The timer
before protected SGD is fault injection, not a claim about slow ML training.
Kills only its owned MCP process and restarts only its disposable UI. No model
calls, owner credential in MCP, user configuration changes or automatic verdicts.
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

from benchmarks.claude_harness_smoke import SCORER, wait_for
from benchmarks.external_session_smoke import Client, until
from looplab.events.eventstore import EventStore
from looplab.harness.mcp_server import HarnessAPI
from looplab.serve.server import make_app


CASES = {
    "completed_extend": {"delay": 12, "cap": 15, "verdict": "extend", "terminal": "evaluated"},
    "stop": {"delay": 60, "cap": 1, "verdict": "stop", "terminal": "failed"},
    "capped_extend": {"delay": 60, "cap": 1, "verdict": "extend", "terminal": "failed"},
    "disabled": {"delay": 60, "cap": 0, "verdict": None, "terminal": "failed"},
}
DELAYED_SCORER = """import time
from pathlib import Path
import json
delay = json.loads(Path('config.json').read_text())['delay_seconds']
end = time.monotonic() + delay
while time.monotonic() < end:
    print('timing fixture: waiting before protected SGD', flush=True)
    time.sleep(.1)
""" + SCORER


def run_case(root: Path, name: str):
    case = CASES[name]
    root.mkdir(parents=True, exist_ok=False)
    runs = root / "runs"; runs.mkdir()
    source = root / "source"; source.mkdir()
    config = {"steps": 5, "delay_seconds": case["delay"]}
    (source / "score.py").write_text(DELAYED_SCORER, encoding="utf8")
    (source / "config.json").write_text(json.dumps(config), encoding="utf8")
    scorer_hash = hashlib.sha256((source / "score.py").read_bytes()).hexdigest()
    task = {"id": "external-deadline-training", "goal": "Measure protected SGD after a timing fault.",
        "direction": "min", "repo": str(source), "edit_surface": ["config.json"], "protect": ["score.py"],
        "cmd": {"command": [sys.executable, "score.py"], "timeout": .8,
            "metric": {"reader": "stdout_json", "key": "metric"},
            "stages": [{"name": "score", "command": [sys.executable, "score.py"], "check": False}]}}
    task_path = root / "task.json"; task_path.write_text(json.dumps(task), encoding="utf8")
    token = secrets.token_hex(32)
    env_before = dict(os.environ)
    os.environ.update(LOOPLAB_HARNESS_TOKEN=token, LOOPLAB_UI_TOKEN=secrets.token_hex(32),
        LOOPLAB_MEMORY_DIR=str(root / "memory"), LOOPLAB_KNOWLEDGE_DIR=str(root / "knowledge"))
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("LOOPLAB_")}
    child_env.update(PYTHONIOENCODING="utf-8", CUDA_VISIBLE_DEVICES="")
    with socket.socket() as bind:
        bind.bind(("127.0.0.1", 0)); port = bind.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    server = thread = engine = None
    proof = {"case": name, "cap_seconds": case["cap"], "verdict": case["verdict"],
             "model_judgment_tested": False, "agent_connection": "not_measured"}
    log = (root / "engine.log").open("w", encoding="utf8")

    def start_ui():
        value = uvicorn.Server(uvicorn.Config(make_app(runs, bind_host="127.0.0.1"),
            host="127.0.0.1", port=port, log_level="warning"))
        worker = threading.Thread(target=value.run, daemon=True); worker.start()
        wait_for(lambda: value.started, bool)
        return value, worker

    async def question(client):
        payload = await until(lambda: client.read("harness-checkpoints"), lambda row: bool(row["pending"]))
        q, = payload["pending"]
        assert q["phase_id"] == "deadline_grace" and q["stage"] == "score", q
        step = (await client.progress())["next_step"]
        assert step["code"] == "answer_checkpoint" and step["phase_id"] == "deadline_grace", step
        await client.call("phases", {"query": step["phase_id"]})
        phase = await client.call("phase_info", {"phase_id": step["phase_id"]})
        assert phase["write_access"][step["action"]] == "external_agent"
        assert "extend" in step["detail"] and "stop" in step["detail"]
        assert "may keep running" in step["detail"] and "after" in step["detail"]
        return q

    async def session(label, generation, drive, kill=False):
        pid_file = root / f"{label}.pid"
        wrapper = "import os;from pathlib import Path;from looplab.harness.mcp_server import run_stdio;" + \
            f"Path({str(pid_file)!r}).write_text(str(os.getpid()));run_stdio()"
        params = StdioServerParameters(command=sys.executable, args=["-c", wrapper],
            env={**child_env, "LOOPLAB_HARNESS_URL": url, "LOOPLAB_HARNESS_TOKEN": token})
        async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as mcp:
            await mcp.initialize()
            client = Client(mcp, generation)
            assert (await client.call("capabilities", {}))["protocol_version"] == 4
            assert (await client.call("connection_check", {"run_id": "demo",
                "expected_generation": generation}))["ok"]
            for suffix in ("config", "harness-contract",
                           "artifact?root=run&path=task.snapshot.json&expected_generation=" + generation):
                await client.request("GET", suffix)
            await drive(client)
            if kill:
                # The PID is freshly written by this owned stdio process, never a saved host PID.
                os.kill(int(pid_file.read_text()), signal.SIGTERM)

    async def first(client):
        await client.progress()
        await client.call("phases", {"query": "implementation"})
        await client.call("phase_info", {"phase_id": "implementation"})
        await client.command("inject_node", {"idea": {"operator": "draft", "footprint": {"gpus": 0},
            "rationale": "Measure five SGD steps under an operator-declared timing budget."},
            "files": {"config.json": json.dumps(config)}}, "deadline:candidate")
        if case["verdict"] is not None:
            q = await question(client)
            proof["checkpoint_id"] = q["checkpoint_id"]
            node = (await client.state())["state"]["nodes"]["0"]
            assert node["status"] == "pending" and node["metric"] is None
            proof["journal_before"] = (runs / "demo" / "harness_checkpoints.jsonl").read_text(encoding="utf8")
            # Prove the wait is NOT a subprocess suspension or an overall time cap.
            # The deadline is .8s; this unanswered window alone exceeds it.
            command_log = runs / "demo" / "nodes" / "node_0" / "score.log"
            before = command_log.stat().st_size
            held = time.monotonic()
            await anyio.sleep(1.2)
            assert command_log.stat().st_size > before
            assert (await client.read("harness-checkpoints"))["pending"][0]["checkpoint_id"] == q["checkpoint_id"]
            node = (await client.state())["state"]["nodes"]["0"]
            assert node["status"] == "pending" and node["metric"] is None
            proof["unanswered_hold_seconds"] = round(time.monotonic() - held, 3)
            proof["command_continued_during_unanswered_wait"] = True
        else:
            await finish(client)

    async def recovered(client):
        await client.call("phases", {"query": "recovery"})
        await client.call("phase_info", {"phase_id": "recovery"})
        q = await question(client)
        assert q["checkpoint_id"] == proof["checkpoint_id"]
        assert (runs / "demo" / "harness_checkpoints.jsonl").read_text(encoding="utf8") == proof.pop("journal_before")
        progress = await client.progress()
        assert progress["execution"]["engine_running"] is True and engine.poll() is None
        assert progress["pending_checkpoint_count"] == 1
        node = (await client.state())["state"]["nodes"]["0"]
        assert node["status"] == "pending" and node["metric"] is None
        # The natural command is still before its SGD; recovery did not restart it.
        assert '"metric"' not in (runs / "demo" / "nodes" / "node_0" / "score.log").read_text(encoding="utf8")
        body = {"expected_generation": client.generation, "checkpoint_id": q["checkpoint_id"],
            "action_id": "deadline:verdict", "verdict": case["verdict"],
            "reason": "Reviewed the real command tail; choose the declared bounded extension or stop."}
        await client.request("POST", "harness-checkpoints", {**body, "action_id": "deadline:invalid",
            "verdict": "abort"}, status=400)
        assert (await client.read("harness-checkpoints"))["pending"][0]["checkpoint_id"] == q["checkpoint_id"]
        proof["answer_started"] = time.monotonic()
        await client.request("POST", "harness-checkpoints", body)
        assert (await client.request("POST", "harness-checkpoints", body))["replayed"]
        await finish(client)
        assert (await client.request("POST", "harness-checkpoints", body))["replayed"]
        proof["same_question_after_mcp_and_ui_restart"] = True

    async def finish(client):
        node = await client.terminal(0, case["terminal"])
        if "answer_started" in proof:
            elapsed = time.monotonic() - proof.pop("answer_started")
            proof["answer_to_terminal_seconds"] = round(elapsed, 3)
            if name == "capped_extend":
                # Includes answer polling, tree cleanup and terminal publication.
                # A loose upper bound distinguishes the cap from the 60s command.
                assert .8 * case["cap"] <= elapsed < case["cap"] + 5, elapsed
            elif name == "stop":
                assert elapsed < 5, elapsed
        if case["terminal"] == "evaluated":
            assert 0 < node["metric"] < 1
            summary = "После разрыва MCP восстановлен тот же вопрос deadline. Явное продление позволило завершить защищённое SGD и оценку. Один детерминированный запуск не подтверждает устойчивость; следующий шаг — независимые повторы."
        else:
            assert node["metric"] is None
            summary = ("После разрыва MCP агент явно остановил команду по deadline. Обучение не завершено, метрики нет. Следующий шаг — пересмотреть бюджет перед новой оценкой." if name == "stop" else
                "Одного продления deadline не хватило: runtime завершил команду в пределах установленного cap. Метрики нет. Следующий шаг — проверить длительность и бюджет." if name == "capped_extend" else
                "Продление deadline отключено оператором. Команда остановлена без checkpoint и без метрики; перед повтором нужно пересмотреть бюджет.")
        proof["metric"] = node["metric"]
        assert not (await client.read("harness-checkpoints"))["pending"]
        await client.commentary("node", "deadline:summary:node", summary, 0)
        await client.call("phases", {"query": "recovery"})
        await client.call("phase_info", {"phase_id": "recovery"})
        await client.command("run_abort", {"reason": "Completed disposable deadline acceptance."}, "deadline:finish")
        await client.commentary("run", "deadline:summary:run", "Проверка deadline явно завершена. " + summary)
        assert len((await client.state())["state"]["nodes"]) == 1

    try:
        server, thread = start_ui()
        flags = {"external_harness": True, "deep_research_every": -1, "report_every": 0, "novelty_mode": "off",
            "foresight": False, "track_hypotheses": False, "concept_pivot": False, "concept_run_base": False,
            "cross_run_concepts": False, "cross_run_curation": False, "reflection_priors": False,
            "lessons_every": 0, "comparative_lessons": False, "concurrent_research": False,
            "train_monitor": False, "asha_live": False, "stage_check_tools": False,
            "eval_stall_timeout_s": 0, "eval_deadline_grace_s": case["cap"], "memory_dir": str(root / "memory")}
        command = [sys.executable, "-m", "looplab.cli", "run", str(task_path), "--out", str(runs / "demo"),
                   "--backend", "toy", "--max-nodes", "2"]
        for key, value in flags.items():
            command += ["-s", f"{key}={str(value).lower() if isinstance(value, bool) else value}"]
        engine = subprocess.Popen(command, cwd=root, env=child_env, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        with HarnessAPI(url, token).client as http:
            current = wait_for(lambda: http.get("api/runs/demo/state?observe_only=true"),
                lambda row: row.status_code == 200 and row.json()["state"].get("setup_done")).json()
        generation = current["generation"]; proof["generation"] = generation
        anyio.run(session, "first", generation, first, case["verdict"] is not None)
        if case["verdict"] is not None:
            server.should_exit = True; thread.join(timeout=5)
            assert not thread.is_alive()
            server, thread = start_ui()
            anyio.run(session, "reconnected", generation, recovered)
        engine.wait(timeout=10)
        rd = runs / "demo"
        events = EventStore(rd / "events.jsonl").read_all()
        terminals = [r for r in events if r.type in ("node_evaluated", "node_failed")]
        assert len(terminals) == 1
        starts = [r for r in events if r.type == "phase_progress" and r.data.get("phase") == "stage"
                  and r.data.get("status") == "started"]
        assert len(starts) == 1 and starts[0].data["name"] == "score"
        journal = rd / "harness_checkpoints.jsonl"
        rows = [json.loads(line) for line in journal.read_text(encoding="utf8").splitlines()] if journal.exists() else []
        assert len(rows) == (2 if case["verdict"] else 0), rows
        proof["checkpoint_questions"] = sum(r["type"] == "question" for r in rows)
        proof["terminal_events"] = len(terminals)
        proof["score_executions"] = len(starts)
        stage, = [r for r in events if r.type == "stage_finished"]
        assert stage.data["status"] == ("ok" if case["terminal"] == "evaluated" else "timeout")
        if case["verdict"] == "extend":
            assert stage.data["deadline_grace_s"] == case["cap"]
            proof["recorded_grace_seconds"] = stage.data["deadline_grace_s"]
        else:
            assert "deadline_grace_s" not in stage.data
        if case["terminal"] == "failed":
            assert terminals[0].data["reason"] == "timeout"
        if case["terminal"] == "evaluated":
            settled, = [r for r in events if r.type == "eval_invocation_settled"]
            assert settled.data["result"]["stages"][0]["deadline_grace_s"] == case["cap"]
        for path in (source / "score.py", rd / "nodes" / "node_0" / "score.py"):
            assert hashlib.sha256(path.read_bytes()).hexdigest() == scorer_hash
        for operation in ("inspect", "replay"):
            result = subprocess.run([sys.executable, "-m", "looplab.cli", operation, str(rd)], cwd=root,
                env=child_env, capture_output=True, text=True, encoding="utf8", timeout=20)
            assert result.returncode == 0, (operation, result.stderr)
            (root / f"{operation}.txt").write_text(result.stdout, encoding="utf8")
        proof.update(protected_scorer_unchanged=True, inspect_replay="passed")
        (root / "acceptance.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf8")
        print(json.dumps(proof), flush=True)
        return proof
    finally:
        if engine is not None and engine.poll() is None:
            engine.terminate(); engine.wait(timeout=10)
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5)
        log.close()
        os.environ.clear(); os.environ.update(env_before)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="New disposable output directory")
    parser.add_argument("--case", choices=["all", *CASES], default="all")
    args = parser.parse_args()
    root = args.out.resolve(); root.mkdir(parents=True, exist_ok=False)
    selected = CASES if args.case == "all" else [args.case]
    results = [run_case(root / name, name) for name in selected]
    (root / "acceptance.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
