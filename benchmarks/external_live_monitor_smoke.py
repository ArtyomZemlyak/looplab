"""Real MCP decisions over live protected SGD, without provider/model calls.

python -m benchmarks.external_live_monitor_smoke --out .tmp/new-live-monitor-proof
Tests improving-loss protection, deliberate stop of a frozen-optimizer control
after MCP/UI recovery, and spent training authority during checkpoint validation.
Only the fixture's own processes are stopped; the output directory must be new.
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

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import uvicorn

from benchmarks.claude_harness_smoke import wait_for
from benchmarks.external_session_smoke import Client, until
from looplab.events.eventstore import EventStore
from looplab.harness.mcp_server import HarnessAPI
from looplab.serve.server import make_app


TRAINER = """import json,time
from pathlib import Path
c = json.loads(Path('config.json').read_text())
train = [(i/20, 2*i/20+1) for i in range(-20, 21)]
w, b = 0., 0.
for step in range(c['steps']):
    dw = sum(2*(w*x+b-y)*x for x,y in train)/len(train)
    db = sum(2*(w*x+b-y) for x,y in train)/len(train)
    w -= c['lr']*dw; b -= c['lr']*db
    loss = sum((w*x+b-y)**2 for x,y in train)/len(train)
    print(f'step={step+1} loss={loss}', flush=True)
    time.sleep(.1)
Path('weights.json').write_text(json.dumps({'w':w,'b':b,'steps':c['steps']}))
print('checkpoint_saved weights.json', flush=True)
for tick in range(c['validation_ticks']):
    valid = [(i/23, 2*i/23+1) for i in range(-23, 24)]
    error = sum((w*x+b-y)**2 for x,y in valid)/len(valid)
    print(f'checkpoint validation tick={tick} heldout_mse={error}', flush=True)
    time.sleep(.1)
"""
SCORER = """import json
from pathlib import Path
checkpoint = json.loads(Path('weights.json').read_text())
w,b = checkpoint['w'],checkpoint['b']
valid = [(i/23, 2*i/23+1) for i in range(-23, 24)]
print(json.dumps({'metric':sum((w*x+b-y)**2 for x,y in valid)/len(valid)}))
"""
CASES = {
    "improving": {"steps": 80, "lr": .05, "validation_ticks": 0},
    "frozen_stop": {"steps": 300, "lr": 0., "validation_ticks": 0},
    "checkpoint_validation": {"steps": 15, "lr": 0., "validation_ticks": 60},
}


def run_case(root, name):
    root.mkdir(parents=True, exist_ok=False)
    runs = root / "runs"; runs.mkdir()
    source = root / "source"; source.mkdir()
    for filename, content in (("train.py", TRAINER), ("score.py", SCORER),
                              ("config.json", json.dumps(CASES[name]))):
        (source / filename).write_text(content, encoding="utf8")
    hashes = {filename: hashlib.sha256((source / filename).read_bytes()).hexdigest()
              for filename in ("train.py", "score.py")}
    task = {"id": "external-live-monitor-training", "goal": "Measure SGD with explicit live monitor decisions.",
        "direction": "min", "repo": str(source), "edit_surface": ["config.json"],
        "protect": ["train.py", "score.py"], "cmd": {
            "command": [sys.executable, "score.py"], "timeout": 45,
            "metric": {"reader": "stdout_json", "key": "metric"}, "stages": [
                {"name": "train", "role": "training", "command": [sys.executable, "train.py"],
                 "check": False, "expect": {"files": ["weights.json"]}},
                {"name": "score", "command": [sys.executable, "score.py"], "check": False}]}}
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
    proof = {"case": name, "model_judgment_tested": False, "answers": []}
    log = (root / "engine.log").open("w", encoding="utf8")

    def start_ui():
        value = uvicorn.Server(uvicorn.Config(make_app(runs, bind_host="127.0.0.1"),
            host="127.0.0.1", port=port, log_level="warning"))
        worker = threading.Thread(target=value.run, daemon=True); worker.start()
        wait_for(lambda: value.started, bool)
        return value, worker

    async def logs(client):
        return await client.read("nodes/0/logs")

    async def question(client):
        payload = await until(lambda: client.read("harness-checkpoints"), lambda row: bool(row["pending"]))
        q, = payload["pending"]
        assert q["phase_id"] == "train_monitor" and q["stage"] == "train", q
        step = (await client.progress())["next_step"]
        assert step["phase_id"] == "monitor" and step["code"] == "answer_checkpoint"
        await client.call("phases", {"query": "monitor"})
        phase = await client.call("phase_info", {"phase_id": "monitor"})
        assert phase["write_access"][step["action"]] == "external_agent"
        assert "loss=" in q["observation"]
        node = (await client.state())["state"]["nodes"]["0"]
        assert node["status"] == "pending" and node["metric"] is None
        return q

    async def answer(client, q, verdict):
        body = {"expected_generation": client.generation, "checkpoint_id": q["checkpoint_id"],
            "action_id": "live:" + q["checkpoint_id"], "verdict": verdict,
            "reason": ("Deliberately stop the zero-learning-rate acceptance control; this is not a general conclusion about plateaus."
                       if verdict == "abort" else "Review actual current-attempt loss and checkpoint state before the next decision.")}
        if not q["kill_enabled"]:
            await client.request("POST", "harness-checkpoints", {**body, "action_id": body["action_id"] + ":invalid",
                "verdict": "abort"}, status=400)
        await client.request("POST", "harness-checkpoints", body)
        assert (await client.request("POST", "harness-checkpoints", body))["replayed"]
        proof["answers"].append({"checkpoint_id": q["checkpoint_id"], "kill_enabled": q["kill_enabled"],
                                 "verdict": verdict})

    async def first(client):
        await client.progress()
        await client.call("phases", {"query": "implementation"})
        await client.call("phase_info", {"phase_id": "implementation"})
        await client.command("inject_node", {"idea": {"operator": "draft", "footprint": {"gpus": 0},
            "rationale": "Measure the declared SGD control with unchanged training and scoring scripts."},
            "files": {"config.json": json.dumps(CASES[name])}}, "live:candidate")
        q = await question(client)
        assert q["kill_enabled"] is False
        if name == "checkpoint_validation":
            await until(lambda: logs(client), lambda row: "checkpoint_saved" in row["stages"].get("train", ""))
            assert (runs / "demo" / "nodes" / "node_0" / "weights.json").exists()
        else:
            # Enough real loss points for a measured trend before requesting another look.
            await until(lambda: logs(client), lambda row: row["stages"].get("train", "").count("loss=") >= 12)
        await answer(client, q, "watch")
        q = await question(client)
        assert q["kill_enabled"] is (name == "frozen_stop"), q
        if name == "frozen_stop":
            assert not (runs / "demo" / "nodes" / "node_0" / "weights.json").exists()
            proof["checkpoint_id"] = q["checkpoint_id"]
            proof["journal_before"] = (runs / "demo" / "harness_checkpoints.jsonl").read_bytes().hex()
        else:
            if name == "checkpoint_validation":
                assert "checkpoint validation" in q["observation"]
            await answer(client, q, "continue")
            await complete(client)

    async def recovered(client):
        await client.call("phases", {"query": "recovery"})
        await client.call("phase_info", {"phase_id": "recovery"})
        q = await question(client)
        assert q["checkpoint_id"] == proof["checkpoint_id"] and q["kill_enabled"] is True
        assert (runs / "demo" / "harness_checkpoints.jsonl").read_bytes().hex() == proof.pop("journal_before")
        assert (await client.progress())["execution"]["engine_running"] is True and engine.poll() is None
        await answer(client, q, "abort")
        await complete(client)
        proof["same_stop_question_after_mcp_ui_restart"] = True

    async def complete(client):
        async def observed():
            state = await client.state()
            if state["state"]["nodes"]["0"]["status"] == "pending":
                for q in (await client.read("harness-checkpoints"))["pending"]:
                    assert q["phase_id"] == "train_monitor" and q["stage"] == "train"
                    assert q["kill_enabled"] is False
                    await client.progress()
                    await client.call("phases", {"query": "monitor"})
                    await client.call("phase_info", {"phase_id": "monitor"})
                    await answer(client, q, "continue")
            return state
        status = "failed" if name == "frozen_stop" else "evaluated"
        state = await until(observed, lambda row: row["state"]["nodes"]["0"]["status"] == status)
        node = state["state"]["nodes"]["0"]
        assert (node["metric"] is None) is (status == "failed")
        proof["metric"] = node["metric"]
        summary = ("После watch и разрыва MCP восстановлен вопрос с разрешением abort. Замороженный optimizer явно остановлен; checkpoint и завершённой метрики нет. Это контроль протокола, а не вывод о вреде plateau. Следующий шаг — исправить lr и оценить новый кандидат." if name == "frozen_stop" else
            "Улучшающаяся loss curve сохранила защиту от abort после watch. Защищённые SGD и scoring завершены; следующий шаг — независимые повторы, один детерминированный запуск недостаточен." if name == "improving" else
            "После записи checkpoint monitor оставался advisory во время его валидации: обучение уже завершено. Только protected scorer дал итоговую метрику. Следующий шаг — сравнить конфигурацию с ненулевым lr.")
        await client.commentary("node", "live:summary:node", summary, 0)
        await client.call("phases", {"query": "recovery"})
        await client.call("phase_info", {"phase_id": "recovery"})
        await client.command("run_abort", {"reason": "Completed disposable live training monitor acceptance."}, "live:finish")
        await client.commentary("run", "live:summary:run", "Проверка live monitor явно завершена. " + summary)

    async def session(label, generation, drive, kill=False):
        pid_file = root / f"{label}.pid"
        wrapper = "import os;from pathlib import Path;from looplab.harness.mcp_server import run_stdio;" + \
            f"Path({str(pid_file)!r}).write_text(str(os.getpid()));run_stdio()"
        params = StdioServerParameters(command=sys.executable, args=["-c", wrapper],
            env={**child_env, "LOOPLAB_HARNESS_URL": url, "LOOPLAB_HARNESS_TOKEN": token})
        async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as mcp:
            await mcp.initialize()
            client = Client(mcp, generation)
            assert (await client.call("connection_check", {"run_id": "demo", "expected_generation": generation}))["ok"]
            await client.call("capabilities", {})
            for suffix in ("config", "harness-contract",
                           "artifact?root=run&path=task.snapshot.json&expected_generation=" + generation):
                await client.request("GET", suffix)
            await drive(client)
            if kill:
                os.kill(int(pid_file.read_text()), signal.SIGTERM)

    try:
        server, thread = start_ui()
        flags = {"external_harness": True, "deep_research_every": -1, "report_every": 0, "novelty_mode": "off",
            "foresight": False, "track_hypotheses": False, "concept_pivot": False, "concept_run_base": False,
            "cross_run_concepts": False, "cross_run_curation": False, "reflection_priors": False,
            "lessons_every": 0, "comparative_lessons": False, "concurrent_research": False,
            "train_monitor": True, "train_monitor_kill": True, "train_monitor_interval_s": .4,
            "asha_live": False, "stage_check_tools": False, "eval_deadline_grace_s": 0,
            "memory_dir": str(root / "memory")}
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
        anyio.run(session, "first", generation, first, name == "frozen_stop")
        if name == "frozen_stop":
            server.should_exit = True; thread.join(timeout=5)
            assert not thread.is_alive()
            server, thread = start_ui()
            anyio.run(session, "reconnected", generation, recovered)
        engine.wait(timeout=10)
        rd = runs / "demo"
        events = EventStore(rd / "events.jsonl").read_all()
        terminals = [r for r in events if r.type in ("node_evaluated", "node_failed")]
        assert len(terminals) == 1
        stages = [r.data["name"] for r in events if r.type == "phase_progress"
                  and r.data.get("phase") == "stage" and r.data.get("status") == "started"]
        assert stages == (["train"] if name == "frozen_stop" else ["train", "score"]), stages
        if name == "frozen_stop":
            assert terminals[0].data["reason"] == "monitor_broken"
            assert not (rd / "nodes" / "node_0" / "weights.json").exists()
        journal = [json.loads(line) for line in (rd / "harness_checkpoints.jsonl").read_text(encoding="utf8").splitlines()]
        assert len(journal) == 2 * len(proof["answers"])
        assert sum(r["kill_enabled"] for r in journal if r["type"] == "question") == int(name == "frozen_stop")
        for filename, digest in hashes.items():
            for directory in (source, rd / "nodes" / "node_0"):
                assert hashlib.sha256((directory / filename).read_bytes()).hexdigest() == digest
        for operation in ("inspect", "replay"):
            result = subprocess.run([sys.executable, "-m", "looplab.cli", operation, str(rd)], cwd=root,
                env=child_env, capture_output=True, text=True, encoding="utf8", timeout=20)
            assert result.returncode == 0, (operation, result.stderr)
            (root / f"{operation}.txt").write_text(result.stdout, encoding="utf8")
        proof.update(protected_sources_unchanged=True, stage_executions=stages, inspect_replay="passed")
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
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--case", choices=["all", *CASES], default="all")
    args = parser.parse_args()
    root = args.out.resolve(); root.mkdir(parents=True, exist_ok=False)
    selected = CASES if args.case == "all" else [args.case]
    proof = [run_case(root / name, name) for name in selected]
    (root / "acceptance.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
