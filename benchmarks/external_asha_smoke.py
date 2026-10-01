"""Offline real MCP ASHA acceptance using measured protected CPU SGD curves.

python -m benchmarks.external_asha_smoke --out .tmp/new-asha-proof
Tests same-rung stop recovery, absent resource declaration, absent peer rung and
operator retarget before evaluation or at an open question. Only private processes
and configuration are used. No model calls.
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
from looplab.serve.protocol import COMMAND_TERMINAL_STATUSES
from looplab.serve.server import make_app


SCORER = """import json,time
from pathlib import Path
c = json.loads(Path('config.json').read_text())
train = [(i/20, 2*i/20+1) for i in range(-20, 21)]
valid = [(i/23, 2*i/23+1) for i in range(-23, 24)]
w,b = 0.,0.
for step in range(1,c['steps']+1):
    dw = sum(2*(w*x+b-y)*x for x,y in train)/len(train)
    db = sum(2*(w*x+b-y) for x,y in train)/len(train)
    w -= c['lr']*dw; b -= c['lr']*db
    value = sum((w*x+b-y)**2 for x,y in valid)/len(valid)
    print(json.dumps({'metric':value,'epoch':step,'weight_norm':abs(w)+abs(b)}),flush=True)
    if step >= c['delay_from']: time.sleep(c['delay'])
Path('weights.json').write_text(json.dumps({'w':w,'b':b,'steps':c['steps']}))
"""
CASES = ("same_resource_stop", "missing_resource", "unmatched_rung", "retarget", "retarget_open")


def run_case(root, name):
    root.mkdir(parents=True, exist_ok=False)
    runs = root / "runs"; runs.mkdir()
    source = root / "source"; source.mkdir()
    (source / "score.py").write_text(SCORER, encoding="utf8")
    (source / "config.json").write_text('{}', encoding="utf8")
    digest = hashlib.sha256((source / "score.py").read_bytes()).hexdigest()
    metric = {"reader": "stdout_json", "key": "metric"}
    if name != "missing_resource":
        metric["resource_key"] = "epoch"
    task = {"id": "external-asha-training", "goal": "Measure SGD under explicit ASHA rank review.",
        "direction": "min", "repo": str(source), "edit_surface": ["config.json"], "protect": ["score.py"],
        "cmd": {"command": [sys.executable, "score.py"], "timeout": 45, "metric": metric,
            "metrics": {"weight_norm": {"kind": "stdout_json", "key": "weight_norm", "direction": "min"}},
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
    proof = {"case": name, "model_judgment_tested": False, "answers": [], "metrics": []}
    log = (root / "engine.log").open("w", encoding="utf8")

    def start_ui():
        value = uvicorn.Server(uvicorn.Config(make_app(runs, bind_host="127.0.0.1"),
            host="127.0.0.1", port=port, log_level="warning"))
        worker = threading.Thread(target=value.run, daemon=True); worker.start()
        wait_for(lambda: value.started, bool)
        return value, worker

    async def inject(client, nid, steps, lr, delay=0., delay_from=1):
        await client.progress()
        await client.call("phases", {"query": "implementation"})
        await client.call("phase_info", {"phase_id": "implementation"})
        config = {"steps": steps, "lr": lr, "delay": delay, "delay_from": delay_from}
        await client.command("inject_node", {"idea": {"operator": "draft", "footprint": {"gpus": 0},
            "rationale": "Measure the declared SGD configuration and its actual intermediate held-out error."},
            "files": {"config.json": json.dumps(config)}}, f"asha:candidate:{nid}")

    async def question(client):
        payload = await until(lambda: client.read("harness-checkpoints"), lambda row: bool(row["pending"]))
        q, = payload["pending"]
        assert q["node_id"] == 2 and q["phase_id"] == "asha_live" and q["stage"] == "train_eval", q
        step = (await client.progress())["next_step"]
        assert step["phase_id"] == "monitor" and step["code"] == "answer_checkpoint"
        await client.call("phases", {"query": "monitor"})
        phase = await client.call("phase_info", {"phase_id": "monitor"})
        assert phase["write_access"][step["action"]] == "external_agent"
        assert "same-resource peers=" in q["observation"] and "minimum before stop=3" in q["observation"]
        if name in ("missing_resource", "unmatched_rung"):
            assert "same-resource peers=[]" in q["observation"]
            assert "same-resource underperforming=None" in q["observation"]
        if name in ("retarget", "retarget_open") and (await client.state())["state"]["objective_key"] is not None:
            if name == "retarget":
                assert "Stop disabled" in q["observation"]
            assert q["stop_refusal"] == "objective_retargeted"
        node = (await client.state())["state"]["nodes"]["2"]
        assert node["status"] == "pending" and node["metric"] is None
        return q

    async def answer(client, q, verdict):
        body = {"expected_generation": client.generation, "checkpoint_id": q["checkpoint_id"],
            "action_id": "asha:" + q["checkpoint_id"], "verdict": verdict,
            "reason": "Review the declared resource, measured sibling curves and current objective before this explicit acceptance decision."}
        if not q["kill_enabled"]:
            await client.request("POST", "harness-checkpoints", {**body, "action_id": body["action_id"] + ":invalid",
                "verdict": "abort"}, status=400)
        await client.request("POST", "harness-checkpoints", body)
        assert (await client.request("POST", "harness-checkpoints", body))["replayed"]
        proof["answers"].append({"checkpoint_id": q["checkpoint_id"], "kill_enabled": q["kill_enabled"],
                                 "verdict": verdict})
        return body

    def operator_retarget(generation):
        # An explicit PRIVATE operator action; this credential is never supplied to MCP.
        with HarnessAPI(url, owner).client as http:
            key = "asha:operator:retarget"
            result = http.post("api/runs/demo/commands", headers={"Idempotency-Key": key}, json={
                "expected_generation": generation, "type": "metric_retarget",
                "data": {"key": "weight_norm", "direction": "min", "goal": "Minimize measured weight norm."}})
            assert result.status_code == 200, result.text
            receipt = wait_for(lambda: http.get("api/runs/demo/command-receipt",
                params={"expected_generation": generation}, headers={"Idempotency-Key": key}),
                lambda row: row.status_code == 200 and row.json()["command"]["status"] in COMMAND_TERMINAL_STATUSES).json()
            assert receipt["command"]["status"] == "succeeded", receipt

    async def first(client):
        for nid, steps in enumerate((2, 3) if name == "unmatched_rung" else (16, 32)):
            await inject(client, nid, steps, .1)
            node = await client.terminal(nid)
            proof["metrics"].append(node["metric"])
            if name != "missing_resource":
                # Fixture provenance check: the compact public node omits this ledger field.
                measured = next(r for r in EventStore(runs / "demo" / "events.jsonl").read_all()
                                if r.type == "node_evaluated" and r.data["node_id"] == nid)
                assert measured.data["resource_curve"]
            await client.commentary("node", f"asha:summary:{nid}", "Измерена базовая SGD-конфигурация и сохранены наблюдения. Перед ASHA-сравнением нужны peers на том же resource rung; финальный score не заменяет промежуточное измерение.", nid)
        if name == "retarget":
            await anyio.to_thread.run_sync(operator_retarget, client.generation)
            assert (await client.state())["state"]["objective_key"] == "weight_norm"
            await client.progress()
        await inject(client, 2, 300 if name == "same_resource_stop" else 70, 0., .1,
                     20 if name == "unmatched_rung" else 1)
        for index in range(3):
            q = await question(client)
            assert q["kill_enabled"] is (name in ("same_resource_stop", "retarget_open") and index == 2), q
            if name in ("same_resource_stop", "retarget_open") and index == 2:
                proof["checkpoint_id"] = q["checkpoint_id"]
                proof["journal_before"] = (runs / "demo" / "harness_checkpoints.jsonl").read_bytes().hex()
            else:
                await answer(client, q, "watch")
        if name not in ("same_resource_stop", "retarget_open"):
            await complete(client)

    async def recovered(client):
        await client.call("phases", {"query": "recovery"})
        await client.call("phase_info", {"phase_id": "recovery"})
        q = await question(client)
        assert q["checkpoint_id"] == proof["checkpoint_id"] and q["kill_enabled"] is (name == "same_resource_stop")
        if name == "retarget_open":
            assert q["recorded_kill_enabled"] is True and q["stop_refusal"] == "objective_retargeted"
        assert (runs / "demo" / "harness_checkpoints.jsonl").read_bytes().hex() == proof.pop("journal_before")
        assert (await client.progress())["execution"]["engine_running"] is True and engine.poll() is None
        body = await answer(client, q, "abort" if name == "same_resource_stop" else "continue")
        await complete(client)
        assert (await client.request("POST", "harness-checkpoints", body))["replayed"]
        proof["same_question_after_mcp_ui_restart"] = True

    async def complete(client):
        async def observed():
            state = await client.state()
            if state["state"]["nodes"]["2"]["status"] == "pending":
                for q in (await client.read("harness-checkpoints"))["pending"]:
                    assert q["kill_enabled"] is False and q["phase_id"] == "asha_live"
                    await client.progress()
                    await client.call("phases", {"query": "monitor"})
                    await client.call("phase_info", {"phase_id": "monitor"})
                    await answer(client, q, "continue")
            return state
        status = "failed" if name == "same_resource_stop" else "evaluated"
        state = await until(observed, lambda row: row["state"]["nodes"]["2"]["status"] == status)
        node = state["state"]["nodes"]["2"]
        assert (node["metric"] is None) is (status == "failed")
        proof["metrics"].append(node["metric"])
        if name in ("retarget", "retarget_open"):
            assert node["metric"] == 0 and node["task_metric"] > 2
            proof["task_metric"] = node["task_metric"]
        summary = ("После двух advisory наблюдений и watch ASHA подтвердил отставание на сопоставимом rung. Разрыв MCP сохранил вопрос; агент явно остановил контроль с lr=0. Итоговой метрики и checkpoint нет. Следующий шаг — новый кандидат, этот stop не доказывает качество общей стратегии." if status == "failed" else
            "После смены objective на weight norm ASHA остался advisory: его кривая относится к прежней task-метрике. SGD и scoring завершены, обе измеренные величины сохранены. Следующий шаг — проверить смысл выбранной objective." if name in ("retarget", "retarget_open") else
            "ASHA видел промежуточную ошибку, но не получил сопоставимых same-rung peers. Abort не был разрешён; SGD и scoring завершены. Следующий шаг — сохранить кривые на одинаковых ресурсах перед ранней остановкой.")
        await client.commentary("node", "asha:summary:2", summary, 2)
        await client.call("phases", {"query": "recovery"})
        await client.call("phase_info", {"phase_id": "recovery"})
        await client.command("run_abort", {"reason": "Completed disposable ASHA acceptance."}, "asha:finish")
        await client.commentary("run", "asha:summary:run", "Проверка ASHA явно завершена. " + summary)

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
            "train_monitor": False, "train_monitor_interval_s": .4, "asha_live": True,
            "asha_live_kill": True, "asha_live_min_siblings": 2, "stage_check_tools": False,
            "eval_deadline_grace_s": 0, "memory_dir": str(root / "memory")}
        command = [sys.executable, "-m", "looplab.cli", "run", str(task_path), "--out", str(runs / "demo"),
                   "--backend", "toy", "--max-nodes", "4"]
        for key, value in flags.items():
            command += ["-s", f"{key}={str(value).lower() if isinstance(value, bool) else value}"]
        engine = subprocess.Popen(command, cwd=root, env=child_env, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        with HarnessAPI(url, token).client as http:
            current = wait_for(lambda: http.get("api/runs/demo/state?observe_only=true"),
                lambda row: row.status_code == 200 and row.json()["state"].get("setup_done")).json()
        generation = current["generation"]; proof["generation"] = generation
        anyio.run(session, "first", generation, first, name in ("same_resource_stop", "retarget_open"))
        if name in ("same_resource_stop", "retarget_open"):
            if name == "retarget_open":
                operator_retarget(generation)
            server.should_exit = True; thread.join(timeout=5)
            assert not thread.is_alive()
            server, thread = start_ui()
            anyio.run(session, "reconnected", generation, recovered)
        engine.wait(timeout=10)
        rd = runs / "demo"
        events = EventStore(rd / "events.jsonl").read_all()
        terminals = [r for r in events if r.type in ("node_evaluated", "node_failed")]
        assert len(terminals) == 3 and sorted(r.data["node_id"] for r in terminals) == [0, 1, 2]
        if name == "same_resource_stop":
            assert terminals[-1].data["reason"] == "asha_underperforming"
            assert not (rd / "nodes" / "node_2" / "weights.json").exists()
        starts = [r for r in events if r.type == "phase_progress" and r.data.get("phase") == "stage"
                  and r.data.get("status") == "started"]
        assert len(starts) == 3 and all(r.data["name"] == "train_eval" for r in starts)
        journal = [json.loads(line) for line in (rd / "harness_checkpoints.jsonl").read_text(encoding="utf8").splitlines()]
        assert len(journal) == 2 * len(proof["answers"])
        assert sum(r["kill_enabled"] for r in journal if r["type"] == "question") == int(name in ("same_resource_stop", "retarget_open"))
        for directory in (source, *(rd / "nodes" / f"node_{nid}" for nid in range(3))):
            assert hashlib.sha256((directory / "score.py").read_bytes()).hexdigest() == digest
        for operation in ("inspect", "replay"):
            result = subprocess.run([sys.executable, "-m", "looplab.cli", operation, str(rd)], cwd=root,
                env=child_env, capture_output=True, text=True, encoding="utf8", timeout=20)
            assert result.returncode == 0, (operation, result.stderr)
            (root / f"{operation}.txt").write_text(result.stdout, encoding="utf8")
        proof.update(protected_scorer_unchanged=True, score_executions=len(starts), inspect_replay="passed")
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
