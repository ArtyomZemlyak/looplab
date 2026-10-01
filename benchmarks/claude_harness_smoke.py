"""Opt-in acceptance through installed Claude Code; no paid model calls.

Run from the checkout with its [ui,harness] extras installed:
  python -m benchmarks.claude_harness_smoke --claude /path/to/claude --out .tmp/claude-smoke

Creates a NEW disposable run root (refuses reuse), real protected SGD scorer,
loopback UI/API and local scripted Messages provider. Claude's REAL MCP client
performs every scenario read/write; the fixture specifies actions, not metrics.
No hooks, saved chat, user settings, owner credential or built-in shell tools
are supplied to Claude. No permission bypass: only the named test MCP tools
are allowed for the authorized scenarios. CLI cost estimates use fixture token
counts and are NOT actual provider charges. This does not test model judgment.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time

import uvicorn

from benchmarks._claude_tool_fixture import local_provider, tool_result
from looplab.harness.mcp_server import HarnessAPI
from looplab.serve.server import make_app


SCORER = '''import json
from pathlib import Path
steps = json.loads(Path('config.json').read_text())['steps']
if steps <= 0: raise ValueError('training steps must be positive')
train = [(i/20, 2*i/20+1) for i in range(-20, 21)]
w, b = 0., 0.
for epoch in range(steps):
    dw = sum(2*(w*x+b-y)*x for x, y in train)/len(train)
    db = sum(2*(w*x+b-y) for x, y in train)/len(train)
    w -= .1*dw; b -= .1*db
valid = [(i/23, 2*i/23+1) for i in range(-23, 24)]
print(json.dumps({'metric': sum((w*x+b-y)**2 for x, y in valid)/len(valid)}))
'''
TOOLS = ("capabilities", "connection_check", "phases", "phase_info", "api_request",
         "run_progress", "command_receipt")


def wait_for(function, predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = function()
        if predicate(value):
            return value
        time.sleep(.05)
    raise TimeoutError("Local acceptance did not reach its expected state")


def read(method, path, body=None, key=None, status=200):
    args = {"method": method, "path": path}
    if body is not None:
        args["body"] = body
    if key is not None:
        args["idempotency_key"] = key
    result = tool_result((yield "api_request", args))
    assert result["status"] == status, (path, result["status"])
    return result.get("body")


def bootstrap(generation):
    manifest = tool_result((yield "capabilities", {}))
    assert manifest["protocol_version"] >= 4
    result = tool_result((yield "connection_check", {
        "run_id": "demo", "expected_generation": generation}))
    assert result["ok"] and result["evidence_complete"]
    assert result["generation"] == generation
    yield from read("GET", "/api/runs/demo/state?observe_only=true")
    for route in ("harness-contract", "config",
                  "artifact?root=run&path=task.snapshot.json&expected_generation=" + generation):
        yield from read("GET", "/api/runs/demo/" + route)
    assert tool_result((yield "phases", {"query": "implementation"}))
    assert tool_result((yield "phase_info", {"phase_id": "implementation"}))["commands"]
    result = tool_result((yield "run_progress", {"run_id": "demo", "expected_generation": generation}))
    assert result["status"] == 200


def candidate_body(generation, steps):
    return {"expected_generation": generation, "type": "inject_node", "data": {
        "idea": {"operator": "draft", "rationale": f"Measure {steps} training steps."},
        "files": {"config.json": json.dumps({"steps": steps})}}}


def notice_comment(generation, row, action):
    return {"expected_generation": generation, "action_id": action,
            "receipt_id": row["id"], "evidence_token": row["evidence_token"],
            "summary": ("Запуск завершён. Измерены две настройки; ошибка третьей сохранена. "
                        "Перед дальнейшим выбором нужны повторные seeds." if row["kind"] == "run" else
                        "Обучение не началось: конфиг задаёт нулевое число шагов. Измеренной метрики нет. "
                        "Следующий шаг — исправить конфиг и повторить оценку." if row["status"] == "failed" else
                        "Оценка завершена. Изменили число шагов обучения; сравнение ограничено "
                        "одним запуском. Следующий шаг — проверить измеренные результаты и конфиг.")}


def first_session(generation, observations):
    yield from bootstrap(generation)
    yield from read("POST", "/api/settings", {}, status=403)
    body = candidate_body(generation, 5)
    receipt = yield from read("POST", "/api/runs/demo/commands", body, "claude:node:0")
    observations["first_command_id"] = receipt["id"]
    # The client exits here; the engine may continue independently.


def recovered_session(generation, observations):
    yield from bootstrap(generation)
    receipt = tool_result((yield "command_receipt", {
        "run_id": "demo", "expected_generation": generation, "idempotency_key": "claude:node:0"}))
    assert receipt["status"] == 200
    assert receipt["body"]["command"]["id"] == observations["first_command_id"]
    retry = yield from read("POST", "/api/runs/demo/commands",
                            candidate_body(generation, 5), "claude:node:0")
    assert retry["id"] == observations["first_command_id"]
    notices = "/api/runs/demo/result-notices"
    for node_id, steps in enumerate((5, 80, 0)):
        if node_id:
            yield from read("POST", "/api/runs/demo/commands",
                            candidate_body(generation, steps), f"claude:node:{node_id}")
        deadline = time.monotonic() + 20
        while True:
            yield from read("GET", "/api/runs/demo/harness-checkpoints?expected_generation=" + generation)
            state = yield from read("GET", "/api/runs/demo/state?observe_only=true")
            node = state["state"].get("nodes", {}).get(str(node_id), {})
            if node.get("status") in ("evaluated", "failed"):
                break
            assert time.monotonic() < deadline, "Evaluation did not become terminal"
            time.sleep(.05)
        assert len(state["state"]["nodes"]) == node_id + 1
        result = yield from read("GET", notices + "?expected_generation=" + generation)
        row = next(item for item in result["items"] if item.get("node_id") == node_id)
        assert (row["status"] == "failed" and row["score"] is None) if steps == 0 else row["status"] == "evaluated"
        comment = notice_comment(generation, row, f"claude:summary:{node_id}")
        yield from read("POST", notices, comment)
        repeated = yield from read("POST", notices, comment)
        assert repeated["replayed"]
        observations.setdefault("nodes", []).append({"id": node_id, "status": row["status"], "score": row["score"]})
    assert observations["nodes"][1]["score"] < observations["nodes"][0]["score"]
    stale = tool_result((yield "connection_check", {"run_id": "demo", "expected_generation": "0" * 64}))
    assert stale["code"] == "generation_mismatch"
    yield from read("POST", "/api/runs/demo/commands", {
        "expected_generation": generation, "type": "run_abort", "data": {"reason": "CLI acceptance complete"}}, "claude:finish")
    deadline = time.monotonic() + 20
    while True:
        result = yield from read("GET", notices + "?expected_generation=" + generation)
        rows = [item for item in result["items"] if item["kind"] == "run"]
        if rows:
            break
        assert time.monotonic() < deadline
        time.sleep(.05)
    yield from read("POST", notices, notice_comment(generation, rows[0], "claude:summary:run"))
    assert rows[0]["selected_node"] == 1


def denied_session():
    result = yield "connection_check", {"run_id": "demo"}
    assert result.get("is_error"), "Unapproved tool unexpectedly executed"


def run_client(executable, workdir, mcp_config, token, scenario, label, *, allow=True):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("ANTHROPIC_", "CLAUDE_", "LOOPLAB_"))}
    with local_provider(scenario) as (url, fixture):
        env.update(ANTHROPIC_API_KEY="local-fixture-only", ANTHROPIC_BASE_URL=url,
                   CLAUDE_CONFIG_DIR=str(workdir / "private-config"),
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", ENABLE_CLAUDEAI_MCP_SERVERS="false",
                   ENABLE_TOOL_SEARCH="false", LOOPLAB_HARNESS_TOKEN=token)
        argv = [executable, "--bare", "--print", "--permission-mode", "default", "--no-session-persistence",
                "--setting-sources", "", "--strict-mcp-config", "--mcp-config", str(mcp_config),
                "--tools", "", "--model", "claude-sonnet-4-6", "--output-format", "json"]
        if allow:
            argv += ["--allowedTools", ",".join("mcp__looplab__" + name for name in TOOLS)]
        argv += ["--", "Run the isolated local LoopLab MCP acceptance scenario."]
        completed = subprocess.run(argv, cwd=workdir, env=env, capture_output=True,
                                   text=True, encoding="utf8", timeout=90)
        if completed.returncode or not fixture.done:
            (workdir / f"{label}-failure.txt").write_text(
                (completed.stdout + "\n" + completed.stderr).replace(token, "REDACTED"), encoding="utf8")
            print(json.dumps({"case": label, "completed_tools": fixture.calls}), flush=True)
        if fixture.error is not None:
            raise RuntimeError("Scripted acceptance failed") from fixture.error
        assert completed.returncode == 0 and fixture.done, "Claude did not complete the local scenario"
        final = json.loads(completed.stdout)
        assert bool(final.get("permission_denials")) is not allow
        assert not final["is_error"]
        # Store only boundary evidence, never full prompts, settings or credentials.
        return {"case": label, "tool_attempts": len(fixture.calls),
                "tool_errors": sum(bool(block.get("is_error")) for block in fixture.results),
                "exit_code": completed.returncode,
                "permission_denials": len(final.get("permission_denials", [])), "scripted_provider": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude", required=True, help="Installed Claude Code executable")
    parser.add_argument("--out", required=True, type=Path, help="New disposable output directory")
    args = parser.parse_args()
    executable = str(Path(args.claude).resolve())
    version = subprocess.check_output([executable, "--version"], text=True).strip()
    root = args.out.resolve()
    root.mkdir(parents=True, exist_ok=False)
    repo = root / "source"; repo.mkdir()
    client = root / "client"; client.mkdir()
    (repo / "score.py").write_text(SCORER, encoding="utf8")
    scorer_digest = hashlib.sha256((repo / "score.py").read_bytes()).digest()
    (repo / "config.json").write_text('{"steps":5}', encoding="utf8")
    task = {"id": "claude-client-training", "goal": "Reduce held-out linear regression MSE.",
            "direction": "min", "repo": str(repo), "edit_surface": ["config.json"], "protect": ["score.py"],
            "cmd": {"command": [sys.executable, "score.py"], "timeout": 20,
                    "metric": {"reader": "stdout_json", "key": "metric"}}}
    task_path = root / "task.json"; task_path.write_text(json.dumps(task))
    token, owner = secrets.token_hex(32), secrets.token_hex(32)
    os.environ.update(LOOPLAB_HARNESS_TOKEN=token, LOOPLAB_UI_TOKEN=owner,
                      LOOPLAB_MEMORY_DIR=str(root / "memory"), LOOPLAB_KNOWLEDGE_DIR=str(root / "knowledge"))
    child_env = {key: value for key, value in os.environ.items() if not key.startswith("LOOPLAB_")}
    child_env["PYTHONIOENCODING"] = "utf-8"
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    server = uvicorn.Server(uvicorn.Config(make_app(root, bind_host="127.0.0.1"),
                                         host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True); thread.start()
    flags = {"external_harness": True, "deep_research_every": -1, "report_every": 0, "novelty_mode": "off",
             "foresight": False, "track_hypotheses": False, "concept_pivot": False, "concept_run_base": False,
             "cross_run_concepts": False, "cross_run_curation": False, "reflection_priors": False,
             "lessons_every": 0, "comparative_lessons": False, "concurrent_research": False,
             "train_monitor": False, "asha_live": False, "stage_check_tools": False, "memory_dir": str(root / "memory")}
    command = [sys.executable, "-m", "looplab.cli", "run", str(task_path), "--out", str(root / "demo"),
               "--backend", "toy", "--max-nodes", "4"]
    for key, value in flags.items():
        command += ["-s", f"{key}={str(value).lower() if isinstance(value, bool) else value}"]
    config = {"mcpServers": {"looplab": {"command": sys.executable, "args": ["-m", "looplab.cli", "harness-mcp"],
                                       "env": {"LOOPLAB_HARNESS_URL": f"http://127.0.0.1:{port}"}}}}
    mcp_config = client / "mcp.json"; mcp_config.write_text(json.dumps(config))
    engine = None
    with (root / "engine.log").open("w", encoding="utf8") as log, HarnessAPI(f"http://127.0.0.1:{port}", token).client as http:
        try:
            wait_for(lambda: server.started, bool)
            engine = subprocess.Popen(command, cwd=root, env=child_env, stdout=log, stderr=subprocess.STDOUT,
                                      creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            current = wait_for(lambda: http.get("api/runs/demo/state?observe_only=true"),
                               lambda reply: reply.status_code == 200 and reply.json()["state"].get("setup_done")).json()
            generation = current["generation"]
            observations = {"client": version, "model_judgment_tested": False, "sessions": []}
            for name, scenario, allow in (("permission_denied", denied_session(), False),
                                         ("first_client", first_session(generation, observations), True),
                                         ("new_client_recovery", recovered_session(generation, observations), True)):
                observations["sessions"].append(run_client(executable, client, mcp_config, token, scenario, name, allow=allow))
                print(json.dumps(observations["sessions"][-1]), flush=True)
            assert hashlib.sha256((repo / "score.py").read_bytes()).digest() == scorer_digest
            assert all(hashlib.sha256((root / "demo" / "nodes" / f"node_{nid}" / "score.py").read_bytes()).digest()
                       == scorer_digest for nid in range(3))
            for operation in ("inspect", "replay"):
                result = subprocess.run([sys.executable, "-m", "looplab.cli", operation, str(root / "demo")],
                                        cwd=root, env=child_env, capture_output=True, text=True, encoding="utf8", timeout=20)
                assert result.returncode == 0
                (root / f"{operation}.txt").write_text(result.stdout, encoding="utf8")
            (root / "acceptance.json").write_text(json.dumps(observations, ensure_ascii=False, indent=2), encoding="utf8")
            print(json.dumps({"nodes": observations["nodes"], "inspect_replay": "passed"}), flush=True)
        finally:
            if engine is not None and engine.poll() is None:
                engine.terminate(); engine.wait(timeout=10)
            server.should_exit = True; thread.join(timeout=5)


if __name__ == "__main__":
    main()
